"""三部门管理员开通：显式映射、最小权限、默认预演、幂等和整批拒绝。"""
import copy
import datetime
import os
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from app01 import models
from app01.management.commands.init_data import ROLE_PERMISSIONS


PASSWORD_ENV = "DEPARTMENT_MANAGER_INITIAL_PASSWORD"
TEST_PASSWORD = "ManagerRegression!2026_R9"
ASSIGNMENTS = (
    "熊大=技术部", "熊二=市场部", "光头强=人力资源部",
)
EXPECTED_DEPARTMENTS = {
    "熊大": "技术部", "熊二": "市场部", "光头强": "人力资源部",
}


class DepartmentManagerCommandTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # 仅测试执行时创建隔离测试数据；开通命令不能重跑此初始化命令。
        call_command("init_data", stdout=StringIO())
        cls.manager_role = models.Role.objects.get(name="部门管理员")

    def assignment_args(self, assignments=ASSIGNMENTS):
        arguments = []
        for assignment in assignments:
            arguments.extend(["--assignment", assignment])
        return arguments

    def command(self, *flags, assignments=ASSIGNMENTS, password=TEST_PASSWORD):
        output = StringIO()
        with mock.patch.dict(os.environ, {PASSWORD_ENV: password}, clear=False):
            call_command(
                "provision_department_managers", *self.assignment_args(assignments),
                *flags, stdout=output,
            )
        return output.getvalue()

    def manager_users(self):
        return models.User.objects.filter(username__in=EXPECTED_DEPARTMENTS).order_by("username")

    def assert_no_requested_managers(self):
        self.assertFalse(self.manager_users().exists())

    def account_state(self, queryset=None):
        queryset = queryset if queryset is not None else models.User.objects.all()
        return list(queryset.order_by("id").values(
            "id", "username", "password", "real_name", "phone", "email", "employee_id",
            "managed_department_id", "is_active", "is_superuser", "is_staff",
        ))

    def test_default_is_dry_run_without_user_or_permission_changes(self):
        before_users = self.account_state()
        before_permissions = list(self.manager_role.permissions.order_by("code").values_list("code", flat=True))
        before_employees = list(models.Employee.objects.order_by("id").values())
        with mock.patch.object(models.User, "save", side_effect=AssertionError("dry-run wrote an account")):
            self.command()
        self.assert_no_requested_managers()
        self.assertEqual(self.account_state(), before_users)
        self.assertEqual(list(models.Employee.objects.order_by("id").values()), before_employees)
        self.assertEqual(list(self.manager_role.permissions.order_by("code").values_list("code", flat=True)), before_permissions)

    def test_apply_creates_exact_three_mappings_with_explicit_demo_employee_consent(self):
        self.command("--apply", "--demo-employees")
        self.assertEqual(self.manager_users().count(), 3)
        employee_ids = set()
        mobiles = set()
        for username, department_title in EXPECTED_DEPARTMENTS.items():
            with self.subTest(username=username):
                user = models.User.objects.get(username=username)
                self.assertEqual(user.real_name, username)
                self.assertEqual(user.managed_department.title, department_title)
                self.assertEqual(list(user.roles.values_list("name", flat=True)), ["部门管理员"])
                self.assertEqual(user.permission_codes(), set(ROLE_PERMISSIONS["部门管理员"]))
                self.assertTrue(user.is_active)
                self.assertFalse(user.is_staff)
                self.assertFalse(user.is_superuser)
                self.assertIsNotNone(user.employee_id)
                employee = user.employee
                self.assertEqual(employee.name, username)
                self.assertEqual(employee.department.title, department_title)
                self.assertTrue(employee.is_demo)
                self.assertTrue(employee.position.strip())
                self.assertEqual((employee.age, employee.gender, employee.account), (30, 1, Decimal("0.00")))
                self.assertEqual(timezone.localtime(employee.create_time).date(), datetime.date(2024, 1, 1))
                employee.full_clean()
                employee_ids.add(employee.id)
                mobiles.add(employee.mobile)
                self.assertEqual(user.profile_phone, employee.mobile)
                self.assertTrue(user.check_password(TEST_PASSWORD))
                self.assertNotEqual(user.password, TEST_PASSWORD)
        self.assertEqual(len(employee_ids), 3)
        self.assertEqual(len(mobiles), 3)
        self.assertEqual(models.Employee.objects.count(), 11)
        self.assertEqual(models.Employee.objects.filter(name__in=EXPECTED_DEPARTMENTS).count(), 3)

    def test_apply_without_employee_source_rejects_the_whole_batch(self):
        before_users = self.account_state()
        before_employees = list(models.Employee.objects.order_by("id").values())
        with self.assertRaises(CommandError):
            self.command("--apply")
        self.assert_no_requested_managers()
        self.assertEqual(self.account_state(), before_users)
        self.assertEqual(list(models.Employee.objects.order_by("id").values()), before_employees)

    def test_managers_are_limited_to_their_department_without_user_admin_access(self):
        self.command("--apply", "--demo-employees")
        for username, department_title in EXPECTED_DEPARTMENTS.items():
            with self.subTest(username=username):
                user = models.User.objects.get(username=username)
                expected_ids = set(models.Employee.objects.filter(
                    department__title=department_title,
                ).values_list("id", flat=True))
                self.assertEqual(set(user.scope_employees().values_list("id", flat=True)), expected_ids)
                self.assertEqual(list(user.task_target_departments().values_list("title", flat=True)), [department_title])
                self.assertEqual(user.task_department_ids(), {user.employee.department_id})
                self.assertTrue(user.has_perm_code("employee.edit"))
                self.assertTrue(user.has_perm_code("task.edit"))
                self.assertFalse(user.has_perm_code("user.edit"))
                self.assertFalse(user.has_perm_code("role.edit"))
                self.client.force_login(user)
                response = self.client.get("/user/list/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.context["queryset"].values_list("id", flat=True)), expected_ids)
                self.assertEqual(self.client.get("/admin/list/").status_code, 403)

    def test_opening_managers_preserves_existing_zhangsan_account_tasks_and_receipts(self):
        zhangsan = models.User.objects.get(employee__name="张三")
        old_account = self.account_state(models.User.objects.filter(id=zhangsan.id))
        old_roles = list(zhangsan.roles.values_list("id", flat=True))
        task = models.Task.objects.filter(assignee=zhangsan).first()
        receipt = models.TaskRead.objects.create(task=task, user=zhangsan)
        old_tasks = list(models.Task.objects.order_by("id").values())
        old_employees = list(models.Employee.objects.order_by("id").values())
        old_employee_ids = [row["id"] for row in old_employees]
        self.command("--apply", "--demo-employees")
        self.assertEqual(self.account_state(models.User.objects.filter(id=zhangsan.id)), old_account)
        zhangsan.refresh_from_db()
        self.assertEqual(list(zhangsan.roles.values_list("id", flat=True)), old_roles)
        self.assertEqual(list(models.Task.objects.order_by("id").values()), old_tasks)
        self.assertEqual(list(models.Employee.objects.filter(id__in=old_employee_ids).order_by("id").values()), old_employees)
        self.assertEqual(models.Employee.objects.count(), len(old_employees) + 3)
        receipt.refresh_from_db()
        self.assertEqual(receipt.user_id, zhangsan.id)

    def test_identical_repeated_apply_preserves_ids_and_passwords(self):
        self.command("--apply", "--demo-employees")
        before = self.account_state(self.manager_users())
        before_employees = list(models.Employee.objects.order_by("id").values())
        self.command("--apply", password="DifferentStrong!2026_New9")
        self.assertEqual(self.account_state(self.manager_users()), before)
        self.assertEqual(list(models.Employee.objects.order_by("id").values()), before_employees)
        self.assertEqual(self.manager_users().count(), 3)
        for user in self.manager_users():
            self.assertTrue(user.check_password(TEST_PASSWORD))
            self.assertFalse(user.check_password("DifferentStrong!2026_New9"))

    def test_identical_existing_accounts_do_not_need_a_new_initial_password(self):
        self.command("--apply", "--demo-employees")
        before = self.account_state(self.manager_users())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PASSWORD_ENV, None)
            call_command(
                "provision_department_managers", *self.assignment_args(), "--apply",
                stdout=StringIO(),
            )
        self.assertEqual(self.account_state(self.manager_users()), before)

    def test_unrelated_existing_username_rejects_entire_batch_without_takeover(self):
        outsider = models.User.objects.create_user(
            username="熊二", password=TEST_PASSWORD, real_name="已有独立账号",
        )
        before = self.account_state()
        with self.assertRaises(CommandError):
            self.command("--apply", "--demo-employees")
        self.assertEqual(self.account_state(), before)
        self.assertFalse(models.User.objects.filter(username__in=["熊大", "光头强"]).exists())
        outsider.refresh_from_db()
        self.assertIsNone(outsider.managed_department_id)
        self.assertIsNone(outsider.employee_id)
        self.assertFalse(outsider.roles.exists())

    def test_existing_manager_with_different_department_is_not_silently_reassigned(self):
        outsider = models.User.objects.create_user(
            username="熊二", real_name="熊二", password=TEST_PASSWORD,
            managed_department=models.Department.objects.get(title="技术部"),
        )
        outsider.roles.set([self.manager_role])
        before = self.account_state()
        with self.assertRaises(CommandError):
            self.command("--apply", "--demo-employees")
        self.assertEqual(self.account_state(), before)
        self.assertFalse(models.User.objects.filter(username__in=["熊大", "光头强"]).exists())

    def test_existing_manager_with_extra_role_or_privileged_flags_is_a_conflict(self):
        for variation in ("extra_role", "is_staff", "is_superuser"):
            with self.subTest(variation=variation):
                values = {variation: True} if variation != "extra_role" else {}
                outsider = models.User.objects.create_user(
                    username="熊二", real_name="熊二", password=TEST_PASSWORD,
                    managed_department=models.Department.objects.get(title="市场部"), **values,
                )
                outsider.roles.set([self.manager_role])
                if variation == "extra_role":
                    outsider.roles.add(models.Role.objects.get(name="普通员工"))
                before = self.account_state()
                with self.assertRaises(CommandError):
                    self.command("--apply", "--demo-employees")
                self.assertEqual(self.account_state(), before)
                self.assertFalse(models.User.objects.filter(username__in=["熊大", "光头强"]).exists())
                outsider.delete()

    def test_existing_employee_binding_is_not_removed_to_make_account_match(self):
        employee = models.Employee.objects.get(name="赵六")
        outsider = models.User.objects.create_user(
            username="熊二", real_name="熊二", password=TEST_PASSWORD, employee=employee,
            managed_department=models.Department.objects.get(title="市场部"),
        )
        outsider.roles.set([self.manager_role])
        before = self.account_state()
        with self.assertRaises(CommandError):
            self.command("--apply", "--demo-employees")
        self.assertEqual(self.account_state(), before)
        outsider.refresh_from_db()
        self.assertEqual(outsider.employee_id, employee.id)

    def test_malformed_duplicate_or_unknown_department_assignment_fails_closed(self):
        cases = (
            ("熊大技术部", "熊二=市场部", "光头强=人力资源部"),
            ("熊大=技术部", "熊大=市场部", "光头强=人力资源部"),
            ("熊大=技术部", "熊二=不存在的部门", "光头强=人力资源部"),
        )
        before = self.account_state()
        for assignments in cases:
            with self.subTest(assignments=assignments):
                with self.assertRaises(CommandError):
                    self.command("--apply", assignments=assignments)
                self.assertEqual(self.account_state(), before)
                self.assert_no_requested_managers()

    def test_missing_assignment_is_rejected(self):
        with mock.patch.dict(os.environ, {PASSWORD_ENV: TEST_PASSWORD}, clear=False):
            with self.assertRaises(CommandError):
                call_command("provision_department_managers", "--apply", stdout=StringIO())
        self.assert_no_requested_managers()

    def test_role_permissions_must_match_baseline_not_be_repaired_or_expanded_implicitly(self):
        for variation in ("missing_view", "extra_user_edit"):
            with self.subTest(variation=variation):
                original = list(self.manager_role.permissions.all())
                if variation == "missing_view":
                    self.manager_role.permissions.remove(models.Permission.objects.get(code="task.view"))
                else:
                    self.manager_role.permissions.add(models.Permission.objects.get(code="user.edit"))
                changed_codes = set(self.manager_role.permissions.values_list("code", flat=True))
                with self.assertRaises(CommandError):
                    self.command("--apply", "--demo-employees")
                self.assert_no_requested_managers()
                self.assertEqual(set(self.manager_role.permissions.values_list("code", flat=True)), changed_codes)
                self.manager_role.permissions.set(original)

    def test_missing_department_manager_role_rejects_account_creation(self):
        self.manager_role.delete()
        before = self.account_state()
        with self.assertRaises(CommandError):
            self.command("--apply", "--demo-employees")
        self.assertEqual(self.account_state(), before)
        self.assert_no_requested_managers()

    def test_new_accounts_require_initial_password_environment(self):
        before_employees = list(models.Employee.objects.order_by("id").values())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PASSWORD_ENV, None)
            with self.assertRaises(CommandError):
                call_command(
                    "provision_department_managers", *self.assignment_args(), "--apply", "--demo-employees",
                    stdout=StringIO(),
                )
        self.assert_no_requested_managers()
        self.assertEqual(list(models.Employee.objects.order_by("id").values()), before_employees)

    def test_weak_password_needs_apply_and_explicit_one_time_override(self):
        validator_configuration = copy.deepcopy(settings.AUTH_PASSWORD_VALIDATORS)
        with self.assertRaises(CommandError):
            self.command("--apply", "--demo-employees", password="123456")
        self.assert_no_requested_managers()
        with self.assertRaises(CommandError):
            self.command("--allow-weak", password="123456")
        self.assert_no_requested_managers()
        self.command("--apply", "--demo-employees", "--allow-weak", password="123456")
        for user in self.manager_users():
            self.assertTrue(user.check_password("123456"))
        self.assertEqual(settings.AUTH_PASSWORD_VALIDATORS, validator_configuration)
        with self.assertRaises(ValidationError):
            validate_password("123456", user=models.User(username="policy_must_still_reject"))

    def test_custom_password_environment_is_used_instead_of_default(self):
        custom_env = "HRMS_TEST_MANAGER_PASSWORD"
        with mock.patch.dict(os.environ, {
            PASSWORD_ENV: "123456", custom_env: TEST_PASSWORD,
        }, clear=False):
            call_command(
                "provision_department_managers", *self.assignment_args(), "--apply", "--demo-employees",
                "--password-env", custom_env, stdout=StringIO(),
            )
        self.assertEqual(self.manager_users().count(), 3)
        for user in self.manager_users():
            self.assertTrue(user.check_password(TEST_PASSWORD))

    def test_apply_write_failure_rolls_back_all_new_accounts_and_employees(self):
        original_save = models.User.save
        before = self.account_state()
        before_employees = list(models.Employee.objects.order_by("id").values())

        def failing_save(user, *args, **kwargs):
            if user.username == "熊二":
                raise DatabaseError("forced manager write failure")
            return original_save(user, *args, **kwargs)

        with mock.patch.object(models.User, "save", new=failing_save):
            with self.assertRaises(DatabaseError):
                self.command("--apply", "--demo-employees")
        self.assert_no_requested_managers()
        self.assertEqual(self.account_state(), before)
        self.assertEqual(list(models.Employee.objects.order_by("id").values()), before_employees)
