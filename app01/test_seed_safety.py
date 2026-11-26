"""Non-destructive init_data regression tests.

All database work in this module is performed by Django TestCase against its
isolated test database. Merely importing this module does not run bootstrap.
"""
import datetime
from contextlib import ExitStack
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from app01 import models
from app01.management.commands.init_data import (
    DEPARTMENTS,
    DEMO_ATTENDANCE_DATES,
    DEMO_JOINED_AT,
    EMPLOYEES,
    EMPLOYEE_POSITIONS,
    PERMISSIONS,
    ROLE_PERMISSIONS,
)
from app01.utils.employee_accounts import sync_employee_accounts


STATE_MODELS = (
    ("permissions", models.Permission),
    ("roles", models.Role),
    ("role_permissions", models.Role.permissions.through),
    ("departments", models.Department),
    ("employees", models.Employee),
    ("users", models.User),
    ("user_roles", models.User.roles.through),
    ("user_groups", models.User.groups.through),
    ("user_permissions", models.User.user_permissions.through),
    ("attendance", models.Attendance),
    ("salary", models.Salary),
    ("notices", models.Notice),
    ("notice_reads", models.NoticeRead),
    ("tasks", models.Task),
    ("task_reads", models.TaskRead),
    ("pretties", models.PrettyNum),
)


def seed():
    output = StringIO()
    call_command("init_data", stdout=output, stderr=StringIO())
    return output.getvalue()


def snapshot():
    """Compare every persisted field, primary key and relevant M2M row."""
    return {
        label: list(model.objects.order_by("pk").values())
        for label, model in STATE_MODELS
    }


def persisted(instance):
    return type(instance).objects.values().get(pk=instance.pk)


def local_datetime(year, month, day, hour=9):
    return timezone.make_aware(
        datetime.datetime(year, month, day, hour), timezone.get_default_timezone()
    )


class FreshSeedBootstrapTest(TestCase):
    def test_public_constants_keep_original_import_and_six_tuple_contract(self):
        self.assertEqual(DEPARTMENTS, ["技术部", "市场部", "人力资源部"])
        self.assertEqual(len(EMPLOYEES), 8)
        self.assertTrue(all(len(row) == 6 for row in EMPLOYEES))
        self.assertEqual(set(EMPLOYEE_POSITIONS), {row[0] for row in EMPLOYEES})
        self.assertEqual(set(ROLE_PERMISSIONS), {"超级管理员", "部门管理员", "普通员工"})
        self.assertEqual(len(PERMISSIONS), 19)
        self.assertTrue(all(len(row) == 3 for row in PERMISSIONS))
        self.assertEqual(set(ROLE_PERMISSIONS["超级管理员"]), {row[2] for row in PERMISSIONS})
        self.assertTrue(all(code.endswith(".view") for code in ROLE_PERMISSIONS["普通员工"]))
        self.assertFalse(models.Employee._meta.get_field("is_demo").get_default())

    def test_fresh_bootstrap_creates_consistent_demo_employees_and_three_accounts(self):
        seed()
        self.assertEqual(models.Employee.objects.count(), 8)
        self.assertEqual(models.User.objects.count(), 3)
        self.assertEqual(set(models.User.objects.values_list("username", flat=True)), {"admin", "张三", "王五"})
        self.assertEqual(models.Department.objects.count(), 3)
        self.assertEqual(models.Attendance.objects.count(), 6)
        self.assertEqual(models.Salary.objects.count(), 24)
        self.assertEqual(models.Notice.objects.count(), 3)
        self.assertEqual(models.Task.objects.count(), 3)
        self.assertEqual(models.PrettyNum.objects.count(), 4)

        for name, age, gender, mobile, department_title, account in EMPLOYEES:
            with self.subTest(employee=name):
                employee = models.Employee.objects.get(mobile=mobile)
                self.assertEqual(employee.name, name)
                self.assertEqual(employee.age, age)
                self.assertEqual(employee.gender, gender)
                self.assertEqual(employee.account, Decimal(account))
                self.assertEqual(employee.position, EMPLOYEE_POSITIONS[name])
                self.assertEqual(employee.department.title, department_title)
                self.assertTrue(employee.is_demo)
                self.assertTrue(timezone.is_aware(employee.create_time))
                self.assertEqual(employee.create_time, DEMO_JOINED_AT)
                self.assertEqual(
                    timezone.localtime(employee.create_time, timezone.get_default_timezone()).replace(tzinfo=None),
                    datetime.datetime(2024, 1, 1, 9),
                )

        admin = models.User.objects.get(username="admin")
        self.assertIsNone(admin.employee_id)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        for username, role_name in (("张三", "部门管理员"), ("王五", "普通员工")):
            user = models.User.objects.select_related("employee").get(username=username)
            self.assertEqual(user.real_name, user.employee.name)
            self.assertEqual(user.phone, user.employee.mobile)
            self.assertEqual(list(user.roles.values_list("name", flat=True)), [role_name])
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.is_staff)
            self.assertTrue(user.is_active)
            if role_name == "部门管理员":
                self.assertEqual(user.managed_department_id, user.employee.department_id)
                self.assertEqual(user.employee.department.title, "技术部")
            else:
                self.assertIsNone(user.managed_department_id)
        for user in models.User.objects.all():
            self.assertTrue(user.check_password("123456"))
            self.assertNotEqual(user.password, "123456")
            self.assertTrue(user.email.endswith("@example.test"))
        for role_name, codes in ROLE_PERMISSIONS.items():
            role = models.Role.objects.get(name=role_name)
            self.assertEqual(set(role.permissions.values_list("code", flat=True)), set(codes))
        self.assertEqual(set(models.Attendance.objects.values_list("date", flat=True)), set(DEMO_ATTENDANCE_DATES))
        self.assertEqual(set(models.Attendance.objects.values_list("status", flat=True)), {1, 2, 3, 4})
        for salary in models.Salary.objects.select_related("employee"):
            self.assertLess(salary.employee.create_time, local_datetime(2025, 1, 1))
            self.assertEqual(salary.total, salary.compute_total())

    def test_existing_employee_is_not_rewritten_or_relabelled_as_demo(self):
        department = models.Department.objects.create(title="已确认部门")
        employee = models.Employee.objects.create(
            name="已确认档案姓名", position="已有高级岗位", age=47, gender=2,
            account=Decimal("24680.13"), mobile=EMPLOYEES[0][3],
            create_time=local_datetime(2017, 6, 10), department=department,
            avatar="avatars/retained.png", is_demo=False,
        )
        before = persisted(employee)
        seed()
        self.assertEqual(persisted(employee), before)
        self.assertEqual(models.Employee.objects.count(), 8)
        manager = models.User.objects.get(employee=employee)
        self.assertEqual((manager.username, manager.real_name), (employee.name, employee.name))
        self.assertEqual(manager.phone, employee.mobile)
        self.assertEqual(manager.managed_department_id, employee.department_id)
        self.assertEqual(list(manager.roles.values_list("name", flat=True)), ["部门管理员"])

    def test_existing_permission_metadata_and_empty_role_are_not_repaired(self):
        permission = models.Permission.objects.create(
            code="employee.view", module="retained_module", name="人工确认权限名称",
        )
        role = models.Role.objects.create(name="普通员工", description="保留人工配置")
        permission_before, role_before = persisted(permission), persisted(role)
        seed()
        self.assertEqual(persisted(permission), permission_before)
        self.assertEqual(persisted(role), role_before)
        self.assertFalse(role.permissions.exists())
        self.assertEqual(models.Permission.objects.count(), len(PERMISSIONS))
        staff = models.User.objects.get(username="王五")
        self.assertEqual(list(staff.roles.values_list("id", flat=True)), [role.id])
        self.assertFalse(role.permissions.exists())

    def test_later_existing_hire_date_does_not_get_preemployment_demo_rows(self):
        department = models.Department.objects.create(title="技术部")
        employee = models.Employee.objects.create(
            name="张三", position="保留岗位", age=29, gender=1,
            mobile=EMPLOYEES[0][3], department=department,
            create_time=local_datetime(2030, 1, 1), is_demo=False,
        )
        before = persisted(employee)
        seed()
        self.assertEqual(persisted(employee), before)
        self.assertFalse(models.Attendance.objects.filter(employee=employee).exists())
        self.assertFalse(models.Salary.objects.filter(employee=employee).exists())
        self.assertEqual(models.User.objects.get(employee=employee).managed_department_id, department.id)

    def test_explicit_sync_completes_eight_bindings_and_second_seed_preserves_them(self):
        seed()
        original_users = list(models.User.objects.order_by("id").values("id", "password", "employee_id"))
        result = sync_employee_accounts(apply=True, initial_password="SeedRegression!2026_R9")
        self.assertEqual(result.created_count, 6)
        self.assertEqual(models.User.objects.count(), 9)
        self.assertEqual(models.User.objects.filter(employee__isnull=False).count(), 8)
        for row in original_users:
            user = models.User.objects.get(pk=row["id"])
            self.assertEqual(user.password, row["password"])
            self.assertEqual(user.employee_id, row["employee_id"])
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)
        self.assertFalse(models.User.objects.filter(username__in=["manager", "staff"]).exists())


class ExistingSeedPreservationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_repeated_bootstrap_across_days_is_exactly_read_only(self):
        before = snapshot()
        role = models.Role.objects.get(name="部门管理员")
        user = models.User.objects.get(username="张三")
        with ExitStack() as stack:
            for _, model in STATE_MODELS:
                stack.enter_context(mock.patch.object(
                    model, "save", side_effect=AssertionError("repeat seed called save"),
                ))
            for manager_type in (type(role.permissions), type(user.roles)):
                stack.enter_context(mock.patch.object(
                    manager_type, "set", side_effect=AssertionError("repeat seed changed M2M membership"),
                ))
            stack.enter_context(mock.patch(
                "app01.management.commands.init_data.timezone.localdate",
                return_value=datetime.date(2032, 7, 8),
            ))
            seed()
        self.assertEqual(snapshot(), before)

    def test_existing_employee_fields_and_demo_marker_are_all_preserved(self):
        employee = models.Employee.objects.get(mobile=EMPLOYEES[0][3])
        other_department = models.Department.objects.get(title="市场部")
        models.Employee.objects.filter(pk=employee.pk).update(
            name="员工档案保留姓名", position="人工确认职位", age=51, gender=2,
            account=Decimal("34567.89"), create_time=local_datetime(2016, 9, 12),
            department=other_department, avatar="avatars/confirmed.png", is_demo=False,
        )
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)
        employee.refresh_from_db()
        self.assertEqual(employee.mobile, EMPLOYEES[0][3])
        self.assertFalse(employee.is_demo)

    def test_existing_blank_position_is_not_filled_by_bootstrap(self):
        models.Employee.objects.filter(name="李四").update(position="", is_demo=False)
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)
        self.assertEqual(models.Employee.objects.get(name="李四").position, "")

    def test_existing_user_profiles_passwords_status_roles_and_identity_are_preserved(self):
        manager = models.User.objects.get(username="张三")
        staff = models.User.objects.get(username="王五")
        admin = models.User.objects.get(username="admin")
        extra_role = models.Role.objects.create(name="人工附加角色", description="不可被seed移除")
        extra_role.permissions.add(models.Permission.objects.get(code="task.view"))
        manager.roles.add(extra_role)
        staff.roles.clear()
        admin.roles.set([extra_role])
        old_date = local_datetime(2019, 2, 3)
        models.User.objects.filter(pk=manager.pk).update(
            real_name="人工姓名备注", phone="13970000001", email="kept@example.test",
            first_name="Kept", last_name="Manager", password="!preserve-unusable-password",
            is_active=False, is_staff=True, is_superuser=False,
            managed_department=models.Department.objects.get(title="人力资源部"),
            date_joined=old_date, last_login=old_date,
        )
        models.User.objects.filter(pk=staff.pk).update(
            real_name="保留员工备注", phone="13970000002", email="staff-kept@example.test",
            managed_department=models.Department.objects.get(title="技术部"),
        )
        models.User.objects.filter(pk=admin.pk).update(
            real_name="人工管理员备注", phone="13970000003", email="admin-kept@example.test",
            employee=models.Employee.objects.get(name="李四"),
            is_superuser=False, is_staff=False, is_active=False,
        )
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)

    def test_non_alias_logins_and_blank_user_contacts_are_not_synchronized(self):
        models.User.objects.filter(username="张三").update(
            username="confirmed_manager_login", real_name="", phone="", email="",
        )
        models.User.objects.filter(username="王五").update(
            username="confirmed_staff_login", real_name="", phone="", email="",
        )
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)

    def test_existing_role_memberships_permissions_and_descriptions_are_preserved(self):
        manager_role = models.Role.objects.get(name="部门管理员")
        manager_role.permissions.clear()
        regular_role = models.Role.objects.get(name="普通员工")
        regular_role.permissions.add(models.Permission.objects.get(code="user.edit"))
        models.Role.objects.filter(pk__in=[manager_role.pk, regular_role.pk]).update(description="已有人工配置")
        models.Permission.objects.filter(code="task.view").update(module="custom_task", name="自定义说明")
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)
        self.assertFalse(manager_role.permissions.exists())
        self.assertTrue(regular_role.permissions.filter(code="user.edit").exists())

    def test_existing_attendance_salary_notices_tasks_and_read_receipts_are_preserved(self):
        manager = models.User.objects.get(username="张三")
        staff = models.User.objects.get(username="王五")
        department = models.Department.objects.get(title="技术部")
        attendance = models.Attendance.objects.filter(employee=manager.employee).first()
        models.Attendance.objects.filter(pk=attendance.pk).update(
            check_in=datetime.time(12, 15), check_out=datetime.time(14, 20), status=3,
        )
        salary = models.Salary.objects.get(employee=manager.employee, month="2025-01")
        models.Salary.objects.filter(pk=salary.pk).update(
            base_salary=Decimal("12345.67"), bonus=Decimal("765.43"),
            deduction=Decimal("98.76"), total=Decimal("321.09"),
        )
        notice = models.Notice.objects.get(title="系统上线公告")
        models.Notice.objects.filter(pk=notice.pk).update(content="已发布的人工公告", publisher=staff, is_top=True)
        models.NoticeRead.objects.create(notice=notice, user=manager)
        task = models.Task.objects.get(title="整理员工档案")
        models.Task.objects.filter(pk=task.pk).update(
            level=3, detail="历史人工任务内容", assignee=None, assignee_department=department,
            is_completed=True, completed_at=local_datetime(2025, 3, 5), completed_by=manager,
        )
        models.TaskRead.objects.create(task=task, user=manager)
        models.PrettyNum.objects.filter(mobile="13800008888").update(price=888, level=4, status=2)
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)

    def test_duplicate_historical_titles_and_mobile_rows_are_kept_in_full(self):
        staff = models.User.objects.get(username="王五")
        notice = models.Notice.objects.create(
            title="系统上线公告", content="同标题的独立历史公告", publisher=staff, is_top=True,
        )
        task = models.Task.objects.create(
            title="整理员工档案", detail="同标题的独立历史任务", assignee=staff,
        )
        models.PrettyNum.objects.create(mobile="13800008888", price=789, level=3, status=2)
        models.NoticeRead.objects.create(notice=notice, user=staff)
        models.TaskRead.objects.create(task=task, user=staff)
        before = snapshot()
        seed()
        self.assertEqual(snapshot(), before)
        self.assertEqual(models.Notice.objects.filter(title="系统上线公告").count(), 2)
        self.assertEqual(models.Task.objects.filter(title="整理员工档案").count(), 2)
        self.assertEqual(models.PrettyNum.objects.filter(mobile="13800008888").count(), 2)

    def test_bound_legacy_aliases_only_replace_allowed_placeholder_names(self):
        manager = models.User.objects.get(username="张三")
        staff = models.User.objects.get(username="王五")
        task = models.Task.objects.get(title="整理员工档案")
        models.TaskRead.objects.create(task=task, user=manager)
        for placeholder in ("", "部门经理", "普通员工", "manager", "staff"):
            with self.subTest(placeholder=placeholder):
                models.User.objects.filter(pk=manager.pk).update(username="manager", real_name=placeholder)
                models.User.objects.filter(pk=staff.pk).update(username="staff", real_name=placeholder)
                expected = snapshot()
                for row in expected["users"]:
                    if row["id"] in (manager.pk, staff.pk):
                        row["username"] = "张三" if row["id"] == manager.pk else "王五"
                        row["real_name"] = row["username"]
                seed()
                self.assertEqual(snapshot(), expected)

    def test_bound_aliases_use_current_employee_name_and_preserve_custom_remarks(self):
        manager = models.User.objects.get(username="张三")
        staff = models.User.objects.get(username="王五")
        models.Employee.objects.filter(pk=manager.employee_id).update(name="张三已确认档案")
        models.Employee.objects.filter(pk=staff.employee_id).update(name="王五已确认档案")
        models.User.objects.filter(pk=manager.pk).update(
            username="manager", real_name="保留管理员备注", phone="13971000001",
        )
        models.User.objects.filter(pk=staff.pk).update(
            username="staff", real_name="保留员工备注", phone="13971000002",
        )
        notice = models.Notice.objects.get(title="系统上线公告")
        models.NoticeRead.objects.create(notice=notice, user=manager)
        task = models.Task.objects.get(title="整理员工档案")
        models.TaskRead.objects.create(task=task, user=manager)
        expected = snapshot()
        for row in expected["users"]:
            if row["id"] == manager.pk:
                row["username"] = "张三已确认档案"
            elif row["id"] == staff.pk:
                row["username"] = "王五已确认档案"
        seed()
        self.assertEqual(snapshot(), expected)
        seed()
        self.assertEqual(snapshot(), expected)


class SeedIdentitySafetyTest(TestCase):
    def assert_rejected_without_writes(self):
        before = snapshot()
        with self.assertRaises(CommandError):
            seed()
        self.assertEqual(snapshot(), before)

    def test_unbound_name_or_legacy_alias_accounts_are_not_taken_over(self):
        for username in ("张三", "王五", "manager", "staff"):
            with self.subTest(username=username):
                outsider = models.User.objects.create_user(
                    username=username, password="IndependentAccount!2026_R9", real_name="独立账号",
                )
                password_hash = outsider.password
                self.assert_rejected_without_writes()
                outsider.refresh_from_db()
                self.assertIsNone(outsider.employee_id)
                self.assertIsNone(outsider.managed_department_id)
                self.assertEqual(outsider.password, password_hash)
                self.assertFalse(outsider.roles.exists())
                outsider.delete()

    def test_name_account_bound_to_another_employee_is_not_rebound(self):
        department = models.Department.objects.create(title="外部员工部门")
        employee = models.Employee.objects.create(
            name="独立员工", position="人工岗位", age=37, gender=1,
            mobile="13999000001", department=department, create_time=local_datetime(2018, 1, 1),
        )
        user = models.User.objects.create_user(
            username="张三", password="IndependentAccount!2026_R9", employee=employee,
        )
        self.assert_rejected_without_writes()
        user.refresh_from_db()
        self.assertEqual(user.employee_id, employee.pk)

    def test_legacy_alias_bound_to_wrong_seed_mobile_fails_closed(self):
        department = models.Department.objects.create(title="其他部门")
        employee = models.Employee.objects.create(
            name="张三", position="保留真实岗位", age=38, gender=1,
            mobile="13999000002", department=department, create_time=local_datetime(2018, 1, 1),
        )
        user = models.User.objects.create_user(
            username="manager", password="ExistingAlias!2026_R9", employee=employee,
        )
        self.assert_rejected_without_writes()
        user.refresh_from_db()
        self.assertEqual((user.username, user.employee_id), ("manager", employee.pk))

    def test_changed_seed_mobile_with_explicit_account_is_not_guessed_or_reset(self):
        seed()
        manager = models.User.objects.get(username="张三")
        models.Employee.objects.filter(pk=manager.employee_id).update(mobile="13999000003")
        self.assert_rejected_without_writes()
        self.assertEqual(models.Employee.objects.get(pk=manager.employee_id).mobile, "13999000003")
        self.assertEqual(models.Employee.objects.count(), 8)

    def test_later_alias_collision_rolls_back_earlier_rename(self):
        seed()
        manager = models.User.objects.get(username="张三")
        staff = models.User.objects.get(username="王五")
        models.User.objects.filter(pk=manager.pk).update(username="manager", real_name="部门经理")
        models.User.objects.filter(pk=staff.pk).update(username="staff", real_name="普通员工")
        models.User.objects.create_user(username="王五", password="IndependentAccount!2026_R9")
        self.assert_rejected_without_writes()
        manager.refresh_from_db()
        staff.refresh_from_db()
        self.assertEqual((manager.username, staff.username), ("manager", "staff"))

    def test_write_failure_rolls_back_new_rows_and_existing_alias_rename(self):
        department = models.Department.objects.create(title="技术部")
        employee = models.Employee.objects.create(
            name="张三", position="保留岗位", age=36, gender=1,
            mobile=EMPLOYEES[0][3], department=department,
            create_time=local_datetime(2018, 1, 1), is_demo=False,
        )
        manager = models.User.objects.create_user(
            username="manager", real_name="部门经理", password="ExistingManager!2026_R9",
            employee=employee, managed_department=department,
        )
        task = models.Task.objects.create(title="历史关联任务", detail="事务失败也要保留", assignee=manager)
        models.TaskRead.objects.create(task=task, user=manager)
        before = snapshot()
        original_save = models.User.save

        def failing_save(user, *args, **kwargs):
            if user.username == "王五":
                raise DatabaseError("forced seed user creation failure")
            return original_save(user, *args, **kwargs)

        with mock.patch.object(models.User, "save", new=failing_save):
            with self.assertRaises(DatabaseError):
                seed()
        self.assertEqual(snapshot(), before)
        manager.refresh_from_db()
        self.assertEqual((manager.username, manager.real_name), ("manager", "部门经理"))
