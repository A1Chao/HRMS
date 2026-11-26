"""
测试员工资料修复工具：只处理已明确标记或确认的测试数据。
真实档案、主键空隙、用户自定义联系方式不是可随意清理的错误。
"""
import datetime
from dataclasses import asdict, dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from app01 import models
from app01.utils.user_profiles import employee_profile_defaults

DEMO_POSITIONS = {
    "张三": "研发主管", "李四": "软件工程师", "王五": "市场专员", "赵六": "销售专员",
    "钱七": "人事专员", "孙八": "招聘专员", "周九": "测试工程师", "吴十": "市场助理",
}
MANAGER_POSITIONS = {"技术部": "技术部经理", "市场部": "市场部经理", "人力资源部": "人力资源经理"}


def demo_joined_at():
    return timezone.make_aware(datetime.datetime(2024, 1, 1, 9), timezone.get_default_timezone())


def demo_manager_employee(username, department, occupied_mobiles):
    for index in range(1, 100000000):
        mobile = "199{:08d}".format(index)
        if mobile not in occupied_mobiles:
            occupied_mobiles.add(mobile)
            break
    else:
        raise ValidationError("没有可分配的唯一测试手机号。")
    employee = models.Employee(
        name=username, department=department, position=MANAGER_POSITIONS.get(department.title, department.title + "经理"),
        age=30, gender=1, mobile=mobile, account=Decimal("0.00"),
        create_time=demo_joined_at(), is_demo=True,
    )
    employee.full_clean()
    return employee


@dataclass(frozen=True)
class DemoWorkforceRepairResult:
    applied: bool
    created_employees: int
    updated_employees: int
    updated_users: int
    rows: tuple

    def as_dict(self):
        return asdict(self)


def _earliest_business_date(employee):
    dates = list(models.Attendance.objects.filter(employee=employee).values_list("date", flat=True))
    for month in models.Salary.objects.filter(employee=employee).values_list("month", flat=True):
        try:
            year, number = map(int, month.split("-"))
            dates.append(datetime.date(year, number, 1))
        except (TypeError, ValueError):
            raise ValidationError("员工 ID {} 的薪资月份无效，请先核查。".format(employee.id))
    return min(dates) if dates else None


def repair_demo_workforce(*, apply=False, allow_demo=False):
    """Plan all changes before writes; never resets accounts or existing nonblank contacts."""
    if apply and not allow_demo:
        raise ValidationError("生成测试资料必须显式授权 --apply --allow-demo。")
    if allow_demo and not apply:
        raise ValidationError("--allow-demo 只能与 --apply 一起使用。")
    # Seed signatures identify existing demonstration rows without guessing identity.
    from app01.management.commands.init_data import EMPLOYEES
    seed_signatures = {(row[0], row[3]) for row in EMPLOYEES}
    with transaction.atomic():
        employee_query = models.Employee.objects.select_related("department").order_by("id")
        user_query = models.User.objects.select_related("employee", "managed_department").prefetch_related("roles").order_by("id")
        if apply:
            employee_query = employee_query.select_for_update()
            user_query = user_query.select_for_update()
        employees, users = list(employee_query), list(user_query)
        employee_map = {employee.id: employee for employee in employees}
        occupied = {employee.mobile for employee in employees} | {user.phone for user in users if user.phone}
        new_links, employee_changes, user_changes, rows = {}, {}, {}, []

        for user in users:
            if "部门管理员" not in user.role_names or user.is_superuser or user.employee_id:
                continue
            if not user.managed_department_id:
                raise ValidationError("管理员 {} 缺少负责部门，不能猜测。".format(user.username))
            if any(employee.name == user.username for employee in employees):
                raise ValidationError("管理员 {} 存在同名但未确认关联的员工，请显式核查员工 ID。".format(user.username))
            employee = demo_manager_employee(user.username, user.managed_department, occupied)
            new_links[user.id] = employee
            rows.append({"model": "Employee", "action": "create-demo", "username": user.username})

        for employee in employees:
            changes = {}
            is_known_demo = employee.is_demo or (employee.name, employee.mobile) in seed_signatures
            if not (employee.position or "").strip():
                position = DEMO_POSITIONS.get(employee.name, employee.department.title + "专员")
                changes["position"] = position if is_known_demo else position + "（测试职位）"
            if is_known_demo:
                earliest = _earliest_business_date(employee)
                if earliest and timezone.localtime(employee.create_time).date() > earliest:
                    candidate = timezone.make_aware(datetime.datetime.combine(earliest - datetime.timedelta(days=1), datetime.time(9)))
                    changes["create_time"] = min(demo_joined_at(), candidate)
                if not employee.is_demo:
                    changes["is_demo"] = True
            for field, value in changes.items():
                models.Employee._meta.get_field(field).clean(value, employee)
            if changes:
                employee_changes[employee.id] = changes
                rows.append({"model": "Employee", "action": "update", "id": employee.id, "fields": sorted(changes)})

        for user in users:
            employee = new_links.get(user.id) or employee_map.get(user.employee_id)
            changes = employee_profile_defaults(employee, real_name=user.real_name, phone=user.phone)
            if not (user.email or "").strip():
                changes["email"] = "user-{}@example.test".format(user.id)
            for field, value in changes.items():
                models.User._meta.get_field(field).clean(value, user)
            if user.id in new_links:
                changes["employee"] = new_links[user.id]
            if changes:
                user_changes[user.id] = changes
                rows.append({"model": "User", "action": "update", "id": user.id, "fields": sorted(changes)})

        if apply:
            for employee in new_links.values():
                employee.save()
            for employee in employees:
                changes = employee_changes.get(employee.id)
                if changes:
                    for field, value in changes.items():
                        setattr(employee, field, value)
                    employee.save(update_fields=list(changes))
            for user in users:
                changes = user_changes.get(user.id)
                if changes:
                    for field, value in changes.items():
                        setattr(user, field, value)
                    user.save(update_fields=list(changes))
        return DemoWorkforceRepairResult(
            applied=apply, created_employees=len(new_links), updated_employees=len(employee_changes),
            updated_users=len(user_changes), rows=tuple(rows),
        )
