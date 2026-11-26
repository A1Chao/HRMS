"""
非破坏性的测试数据 bootstrap（任务书 T1）。

用法：
    python manage.py init_data

仅为缺失记录写入示例值；已有员工、用户资料/密码/权限范围和业务记录
均不重置。旧 manager / staff 账号只有在员工身份已明确绑定时才可同步
登录名，绝不接管同名未绑定账号。发生身份或姓名冲突时整批回滚。

全新初始化保留三个演示登录账号：admin / 张三 / 王五，初始密码仅在
新建时设为 123456。其余员工账号由 sync_employee_accounts 显式开通。
所有新建员工标记为测试数据，并采用固定早于 2025 年示例工资的入职时间。
这不是线上数据库修复命令，不用于覆盖或“清理”已有数据。

新手阅读：仅补齐缺失的演示数据；初始 admin/张三/王五 是测试账号。
此命令有写入行为，不是只读审计；既有账号密码、资料和业务记录不能被重置。
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from app01 import models
from app01.utils.demo_salaries import DEMO_SALARY_MONTHS, demo_salary_defaults

# (模块, 权限名称, 权限编码)
PERMISSIONS = [
    ("depart", "查看部门", "depart.view"),
    ("depart", "维护部门", "depart.edit"),
    ("employee", "查看员工", "employee.view"),
    ("employee", "维护员工", "employee.edit"),
    ("attendance", "查看考勤", "attendance.view"),
    ("attendance", "维护考勤", "attendance.edit"),
    ("salary", "查看薪资", "salary.view"),
    ("salary", "维护薪资", "salary.edit"),
    ("notice", "查看公告", "notice.view"),
    ("notice", "维护公告", "notice.edit"),
    ("task", "查看任务", "task.view"),
    ("task", "维护任务", "task.edit"),
    ("chart", "查看统计", "chart.view"),
    ("user", "查看用户", "user.view"),
    ("user", "维护用户", "user.edit"),
    ("role", "查看角色", "role.view"),
    ("role", "维护角色", "role.edit"),
    ("pretty", "查看靓号", "pretty.view"),
    ("pretty", "维护靓号", "pretty.edit"),
]

# 角色 → 权限编码；provision 等现有导入依赖这一公开静态基线。
ROLE_PERMISSIONS = {
    "超级管理员": [code for _, _, code in PERMISSIONS],
    "部门管理员": [
        "depart.view",
        "employee.view", "employee.edit",
        "attendance.view", "attendance.edit",
        "salary.view", "salary.edit",
        "notice.view", "notice.edit",
        "task.view", "task.edit",
        "chart.view",
    ],
    "普通员工": [
        "employee.view",
        "attendance.view",
        "salary.view",
        "task.view",
    ],
}

DEPARTMENTS = ["技术部", "市场部", "人力资源部"]

# (姓名, 年龄, 性别, 手机号, 部门, 账户余额)
EMPLOYEES = [
    ("张三", 28, 1, "13800000001", "技术部", "12000.00"),
    ("李四", 32, 1, "13800000002", "技术部", "9800.50"),
    ("王五", 26, 2, "13800000003", "市场部", "7600.00"),
    ("赵六", 35, 2, "13800000004", "市场部", "10300.00"),
    ("钱七", 41, 1, "13800000005", "人力资源部", "11500.00"),
    ("孙八", 24, 2, "13800000006", "人力资源部", "6800.00"),
    ("周九", 30, 1, "13800000007", "技术部", "13000.00"),
    ("吴十", 22, 2, "13800000008", "市场部", "5200.00"),
]

# 不向 EMPLOYEES 的公开 6 元组追加字段，以兼容现有导入和解包代码。
EMPLOYEE_POSITIONS = {
    "张三": "研发主管",
    "李四": "软件工程师",
    "王五": "市场专员",
    "赵六": "销售专员",
    "钱七": "人事专员",
    "孙八": "招聘专员",
    "周九": "测试工程师",
    "吴十": "市场助理",
}

DEMO_JOINED_AT = timezone.make_aware(
    datetime.datetime(2024, 1, 1, 9, 0), timezone.get_default_timezone()
)

# 日期固定，避免重跑 bootstrap 时按“今天/昨天”不断添加新考勤。
DEMO_ATTENDANCE_DATES = (datetime.date(2025, 3, 3), datetime.date(2025, 3, 4))


class Command(BaseCommand):
    help = "仅补齐缺失测试数据；不重置已有员工、用户资料、密码、权限或业务记录。"

    @staticmethod
    def _login_name(employee, user=None):
        """登录名只取已经明确定位的 Employee；占名账号不代表同一身份。"""
        name = employee.name
        if not name or name != name.strip() or models.User.normalize_username(name) != name:
            raise CommandError("员工 ID {} 的姓名不能直接作为演示登录名，请先核查。".format(employee.pk))
        try:
            models.User._meta.get_field("username").clean(name, user)
        except ValidationError as exc:
            raise CommandError("员工姓名不能作为登录名：{}".format("; ".join(exc.messages))) from exc
        conflict = models.User.objects.filter(username__iexact=name)
        if user is not None:
            conflict = conflict.exclude(pk=user.pk)
        if conflict.exists():
            raise CommandError("姓名账号 {} 已被其他用户占用，拒绝接管或覆盖其身份。".format(name))
        if models.Employee.objects.filter(name__iexact=name).exclude(pk=employee.pk).exists():
            raise CommandError("员工姓名 {} 不唯一，拒绝猜测演示账号身份。".format(name))
        return name

    def _employee_account(self, alias, employee, role):
        """新账号直接绑定员工；既有账号只允许有明确身份的旧别名改名。"""
        users = models.User.objects.select_for_update()
        user = users.filter(employee_id=employee.pk).first()
        legacy_user = users.filter(username=alias).first()
        if legacy_user is not None and legacy_user.employee_id != employee.pk:
            raise CommandError(
                "演示别名 {} 未明确绑定目标种子手机号的员工，拒绝猜测或换绑身份。".format(alias)
            )
        if user is not None and user.username in {"manager", "staff"} and user.username != alias:
            raise CommandError("旧演示别名 {} 已绑定其他种子身份，拒绝重新分配。".format(user.username))

        if user is None:
            username = self._login_name(employee)
            user = models.User.objects.create_user(
                username=username, real_name=employee.name, phone=employee.mobile,
                email="{}@example.test".format(alias), password="123456",
                employee=employee,
                managed_department=employee.department if alias == "manager" else None,
                is_superuser=False, is_staff=False, is_active=True,
            )
            user.roles.set([role])
        elif user.username == alias:
            # 这一例外经明确确认：同一个员工、仍为旧别名，且目标姓名未被占用。
            username = self._login_name(employee, user)
            update_fields = []
            if user.username != username:
                user.username = username
                update_fields.append("username")
            if (not (user.real_name or "").strip() or user.real_name in {
                "部门经理", "普通员工", "manager", "staff",
            }) and user.real_name != employee.name:
                user.real_name = employee.name
                update_fields.append("real_name")
            if update_fields:
                user.save(update_fields=update_fields)
        # 正常姓名账号/其他既有登录名、资料、密码、角色、状态、范围和员工外键都不写。
        return user

    @staticmethod
    def _joined_date(employee):
        joined_at = employee.create_time
        if timezone.is_aware(joined_at):
            joined_at = timezone.localtime(joined_at, timezone.get_default_timezone())
        return joined_at.date()

    @staticmethod
    def _create_missing(model, lookup, defaults):
        # 这些历史字段没有 unique 约束；保留全部重复记录，不选一个覆盖，
        # 也不让 get_or_create 的 MultipleObjectsReturned 中止安全 bootstrap。
        if not model.objects.filter(**lookup).exists():
            model.objects.create(**lookup, **defaults)

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("开始补齐缺失测试数据（不覆盖已有记录） ...")

        # 1. 权限：既有名称和模块描述属于现有配置，仅新建时写演示描述。
        permission_map = {}
        for module, name, code in PERMISSIONS:
            permission, _ = models.Permission.objects.get_or_create(
                code=code, defaults={"module": module, "name": name}
            )
            permission_map[code] = permission

        # 2. 角色：只为本次新建角色设置缺省权限；不修复/扩充已有权限集合。
        role_map = {}
        for role_name, codes in ROLE_PERMISSIONS.items():
            role, created = models.Role.objects.get_or_create(name=role_name)
            if created:
                role.permissions.set([permission_map[code] for code in codes])
            role_map[role_name] = role

        # 3. 部门
        department_map = {}
        for title in DEPARTMENTS:
            department, _ = models.Department.objects.get_or_create(title=title)
            department_map[title] = department

        # 4. 员工：只按唯一种子手机号定位，不以同名猜身份，也不重写任何旧字段。
        employee_map = {}
        for name, age, gender, mobile, department_title, account in EMPLOYEES:
            employee, _ = models.Employee.objects.get_or_create(
                mobile=mobile,
                defaults={
                    "name": name,
                    "position": EMPLOYEE_POSITIONS[name],
                    "age": age,
                    "gender": gender,
                    "account": Decimal(account),
                    "department": department_map[department_title],
                    "create_time": DEMO_JOINED_AT,
                    "is_demo": True,
                },
            )
            employee_map[name] = employee

        # 5. 保留三个初始登录账号；其他六名员工由独立 sync 命令开通。
        # admin 是独立系统管理员，不强行绑定员工或接管既有 admin 的权限。
        admin_user, admin_created = models.User.objects.get_or_create(
            username="admin",
            defaults={
                "real_name": "系统管理员", "phone": "13900000001",
                "email": "admin@example.test",
                "is_superuser": True, "is_staff": True, "is_active": True,
            },
        )
        if admin_created:
            admin_user.set_password("123456")
            admin_user.save(update_fields=["password"])
            admin_user.roles.set([role_map["超级管理员"]])

        manager_user = self._employee_account("manager", employee_map["张三"], role_map["部门管理员"])
        staff_user = self._employee_account("staff", employee_map["王五"], role_map["普通员工"])

        # 6. 固定演示考勤：跨日重跑不增加新日期、不触发旧记录的状态重算。
        first_day, second_day = DEMO_ATTENDANCE_DATES
        attendance_rows = [
            ("张三", first_day, datetime.time(8, 50), datetime.time(18, 10)),
            ("李四", first_day, datetime.time(9, 25), datetime.time(18, 5)),
            ("王五", first_day, datetime.time(8, 55), datetime.time(17, 30)),
            ("赵六", first_day, None, None),
            ("张三", second_day, datetime.time(8, 58), datetime.time(18, 2)),
            ("李四", second_day, datetime.time(9, 10), datetime.time(18, 30)),
        ]
        for name, day, check_in, check_out in attendance_rows:
            employee = employee_map[name]
            if day < self._joined_date(employee):
                # 旧档案入职日期也不能重置，更不能为其新增入职前的假考勤。
                continue
            models.Attendance.objects.get_or_create(
                employee=employee,
                date=day,
                defaults={"check_in": check_in, "check_out": check_out},
            )

        # 7. 2025-01 ~ 2025-03 工资：实发只在新记录 save 时计算，旧工资一律不写。
        for month_index, month in enumerate(DEMO_SALARY_MONTHS):
            for name, _, _, _, _, _ in EMPLOYEES:
                employee = employee_map[name]
                if month < self._joined_date(employee).strftime("%Y-%m"):
                    continue
                models.Salary.objects.get_or_create(
                    employee=employee,
                    month=month,
                    defaults=demo_salary_defaults(month_index),
                )

        # 8. 公告：已有同标题记录（包括重复历史公告）的内容/作者/置顶均不变。
        notices = [
            ("关于 2025 年第一季度绩效考核的通知", "请各部门于本月底前完成员工绩效考核自评，提交给人力资源部。", True),
            ("系统上线公告", "企业员工管理系统已正式上线，账号问题请联系系统管理员。", False),
            ("三月考勤统计说明", "三月考勤以系统记录为准，迟到、早退请及时说明原因。", False),
        ]
        for title, content, is_top in notices:
            self._create_missing(
                models.Notice, {"title": title},
                {"content": content, "publisher": admin_user, "is_top": is_top},
            )

        # 9. 任务：不重派、不重置完成状态、不删除任何任务或阅读回执。
        tasks = [
            (1, "修复考勤统计异常", "部分员工 3 月 1 日的考勤状态计算错误，需要排查并修复。", manager_user),
            (2, "整理员工档案", "核对技术部员工档案信息，补充缺失的手机号。", manager_user),
            (3, "发布四月排班表", "完成四月排班表并发布到公告栏。", staff_user),
        ]
        for level, title, detail, assignee in tasks:
            self._create_missing(
                models.Task, {"title": title},
                {"level": level, "detail": detail, "assignee": assignee},
            )

        # 10. 靓号：保留同手机号的全部旧记录和值。
        pretties = [
            ("13800008888", 2000, 1, 1),
            ("13800009999", 1500, 2, 2),
            ("13900001234", 800, 3, 2),
            ("13700005678", 300, 4, 2),
        ]
        for mobile, price, level, status in pretties:
            self._create_missing(
                models.PrettyNum, {"mobile": mobile},
                {"price": price, "level": level, "status": status},
            )

        self.stdout.write(self.style.SUCCESS("测试 bootstrap 完成（已有记录不重置）："))
        self.stdout.write("  权限 {} 个，角色 {} 个".format(
            models.Permission.objects.count(), models.Role.objects.count()))
        self.stdout.write("  用户 {} 个（初始演示用户名：admin / 张三 / 王五；已有账号密码不变）".format(
            models.User.objects.count()))
        self.stdout.write("  部门 {} 个，员工 {} 人，考勤 {} 条，薪资 {} 条，公告 {} 条，任务 {} 条".format(
            models.Department.objects.count(),
            models.Employee.objects.count(),
            models.Attendance.objects.count(),
            models.Salary.objects.count(),
            models.Notice.objects.count(),
            models.Task.objects.count(),
        ))
