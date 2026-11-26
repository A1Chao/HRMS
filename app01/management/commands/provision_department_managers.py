"""
新手阅读：按明确指定的姓名/部门/员工身份开通管理账号，默认预演。
必须显式确认员工 ID 或创建测试档案，不能把同名或已有账号当作可接管对象。
"""
import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app01 import models
from app01.management.commands.init_data import ROLE_PERMISSIONS
from app01.utils.demo_workforce import demo_manager_employee
from app01.utils.user_profiles import employee_profile_defaults

DEFAULT_PASSWORD_ENV = "DEPARTMENT_MANAGER_INITIAL_PASSWORD"
MANAGER_ROLE = "部门管理员"


class Command(BaseCommand):
    help = "明确 用户名=负责部门 开通管理员并关联本人档案；默认预演，不接管同名身份。"

    def add_arguments(self, parser):
        parser.add_argument("--assignment", action="append", required=True, metavar="USER=DEPARTMENT")
        parser.add_argument("--employee", action="append", default=[], metavar="USER=ID", help="显式确认关联员工ID，不按同名猜测。")
        parser.add_argument("--demo-employees", action="store_true", help="明确允许新建虚构测试员工资料。")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV)
        parser.add_argument("--allow-weak", action="store_true", help="仅本次显式apply允许弱初始密码，全局策略不变。")

    def handle(self, *args, **options):
        apply = options["apply"]
        if options["allow_weak"] and not apply:
            raise CommandError("--allow-weak 只能与 --apply 一起显式使用。")
        assignments, usernames, department_titles = [], set(), set()
        for assignment in options["assignment"]:
            username, separator, title = assignment.partition("=")
            if not separator or not username or not title.strip() or username != username.strip():
                raise CommandError("分配格式必须是非空 用户名=部门，用户名不能有首尾空格。")
            title = title.strip()
            if models.User.normalize_username(username) != username:
                raise CommandError("用户名不能在规范化后改变。")
            try:
                models.User._meta.get_field("username").clean(username, None)
                models.User._meta.get_field("real_name").clean(username, None)
            except ValidationError as exc:
                raise CommandError("账号姓名无效：" + "; ".join(exc.messages)) from exc
            if username.casefold() in usernames or title.casefold() in department_titles:
                raise CommandError("同一批分配不能重复用户名或负责部门。")
            usernames.add(username.casefold())
            department_titles.add(title.casefold())
            assignments.append((username, title))
        employee_ids = {}
        for selection in options["employee"]:
            name, separator, employee_id = selection.partition("=")
            if not separator or name.casefold() not in usernames or not employee_id.isdecimal() or int(employee_id) <= 0:
                raise CommandError("关联格式必须为已分配 USER=有效员工ID。")
            if name.casefold() in employee_ids:
                raise CommandError("同一用户不能重复指定员工ID。")
            employee_ids[name.casefold()] = int(employee_id)
        if len(set(employee_ids.values())) != len(employee_ids):
            raise CommandError("同一员工不能关联多个管理员。")

        with transaction.atomic():
            role_query = models.Role.objects.all()
            departments_query = models.Department.objects.all()
            users_query = models.User.objects.select_related("employee").prefetch_related("roles")
            employees_query = models.Employee.objects.select_related("department").all()
            if apply:
                role_query = role_query.select_for_update()
                departments_query = departments_query.select_for_update()
                users_query = users_query.select_for_update()
                employees_query = employees_query.select_for_update()
            role = role_query.filter(name=MANAGER_ROLE).first()
            if role is None:
                raise CommandError("缺少部门管理员角色，请先配置角色权限。")
            if set(role.permissions.values_list("code", flat=True)) != set(ROLE_PERMISSIONS[MANAGER_ROLE]):
                raise CommandError("部门管理员角色权限与安全基线不一致，拒绝隐式提权或修改角色。")
            departments = {department.title: department for department in departments_query}
            users = {user.username.casefold(): user for user in users_query}
            employees = {employee.id: employee for employee in employees_query}
            occupied = {employee.mobile for employee in employees.values()} | {user.phone for user in users.values() if user.phone}
            plan, missing_profiles = [], []
            for username, title in assignments:
                department = departments.get(title)
                if department is None:
                    raise CommandError("负责部门不存在：{}".format(title))
                user = users.get(username.casefold())
                if user is not None and not (
                    user.username == username and user.real_name == username
                    and user.managed_department_id == department.id
                    and {item.id for item in user.roles.all()} == {role.id}
                    and user.is_active and not user.is_staff and not user.is_superuser
                ):
                    raise CommandError("用户 {} 已存在且配置不同，拒绝覆盖或重置。".format(username))
                selected_id = employee_ids.get(username.casefold())
                if user and user.employee_id and selected_id and user.employee_id != selected_id:
                    raise CommandError("用户 {} 已关联其他员工，禁止更换身份。".format(username))
                employee = employees.get(selected_id or (user.employee_id if user else None))
                if selected_id and employee is None:
                    raise CommandError("指定员工ID不存在。")
                if employee is not None:
                    if employee.name != username or employee.department_id != department.id:
                        raise CommandError("关联员工姓名或所属部门与明确分配不一致。")
                    if any(other.employee_id == employee.id and (user is None or other.id != user.id) for other in users.values()):
                        raise CommandError("指定员工已属于其他账号，禁止接管。")
                    try:
                        employee.full_clean()
                    except ValidationError as exc:
                        raise CommandError("员工档案无效：" + "; ".join(exc.messages)) from exc
                elif options["demo_employees"]:
                    if any(item.name == username for item in employees.values()):
                        raise CommandError("已存在同名员工，必须用 --employee 显式确认，不能重复创建或猜测。")
                    try:
                        employee = demo_manager_employee(username, department, occupied)
                    except ValidationError as exc:
                        raise CommandError("测试员工资料无效：" + "; ".join(exc.messages)) from exc
                else:
                    missing_profiles.append(username)
                plan.append((username, department, user, employee))
            if apply and missing_profiles:
                raise CommandError("必须关联本人档案；请提供 --employee USER=ID，或授权 --demo-employees：" + "、".join(missing_profiles))
            created_count = sum(user is None for _, _, user, _ in plan)
            password = os.environ.get(options["password_env"])
            if apply and created_count:
                if not password:
                    raise CommandError("待新建账号的初始密码环境变量不能为空。")
                if not options["allow_weak"]:
                    for username, _, user, _ in plan:
                        if user is None:
                            try:
                                validate_password(password, models.User(username=username, real_name=username))
                            except ValidationError as exc:
                                raise CommandError("初始密码未通过校验：{}；经授权的弱密码须显式 --apply --allow-weak。".format("; ".join(exc.messages))) from exc
                else:
                    self.stderr.write("本次经显式授权允许弱初始密码；全局密码策略保持不变，请尽快更换。")
            if apply:
                for username, department, user, employee in plan:
                    if not employee.pk:
                        employee.save()
                    if user is None:
                        user = models.User.objects.create_user(
                            username=username, real_name=username, phone=employee.mobile, password=password,
                            employee=employee, managed_department=department, is_active=True,
                            is_staff=False, is_superuser=False,
                        )
                        user.roles.set([role])
                        if employee.is_demo:
                            user.email = "user-{}@example.test".format(user.id)
                            user.save(update_fields=["email"])
                    else:
                        changes = employee_profile_defaults(employee, real_name=user.real_name, phone=user.phone)
                        if not user.employee_id:
                            changes["employee"] = employee
                        if changes:
                            for field, value in changes.items():
                                setattr(user, field, value)
                            user.save(update_fields=list(changes))
        for username, department, _, employee in plan:
            self.stdout.write("{} → {}；{}".format(username, department.title, "已确认本人员工" if employee else "待提供员工资料"))
        self.stdout.write("{}：{} 个新账号，{} 个现有账号；不重置现有密码。".format(
            "APPLY" if apply else "DRY-RUN", created_count, len(plan) - created_count,
        ))
        if not apply:
            self.stdout.write("本次未写入数据库；实际新建员工必须明确提供信息或 --demo-employees。")
