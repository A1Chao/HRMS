"""Focused tests for the employee-name account synchronization workflow."""
import csv
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connections
from django.test import TestCase, TransactionTestCase

from app01 import models
from app01.utils.employee_accounts import (
    DEFAULT_PASSWORD_ENV,
    PRESERVED_PASSWORD_MARKER,
    EmployeeAccountSyncError,
    sync_employee_accounts,
)


STRONG_INITIAL_PASSWORD = "CourseDesign!2026_XyZ"
REQUIRED_CODES = (
    "employee.view",
    "attendance.view",
    "salary.view",
    "task.view",
)


class EmployeeAccountFixtureMixin:
    def create_permission_baseline(self):
        permissions = []
        for code in REQUIRED_CODES:
            module = code.split(".", 1)[0]
            permissions.append(models.Permission.objects.create(
                module=module, name="{}只读".format(module), code=code
            ))
        self.regular_role = models.Role.objects.create(name="普通员工")
        self.regular_role.permissions.set(permissions)
        self.department = models.Department.objects.create(title="技术部")
        self.other_department = models.Department.objects.create(title="市场部")

    def create_employee(self, name, index, department=None):
        return models.Employee.objects.create(
            name=name,
            age=20 + (index % 30),
            gender=1 if index % 2 else 2,
            account="0.00",
            mobile="1389{:07d}".format(index),
            department=department or self.department,
        )


class EmployeeAccountSyncServiceTest(EmployeeAccountFixtureMixin, TestCase):
    def setUp(self):
        self.create_permission_baseline()

    def test_eight_employees_reuse_two_user_ids_create_six_and_are_idempotent(self):
        names = ["张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十"]
        employees = [
            self.create_employee(name, index + 1, self.department if index < 4 else self.other_department)
            for index, name in enumerate(names)
        ]

        manager_permission = models.Permission.objects.create(
            module="task", name="维护任务", code="task.edit"
        )
        manager_role = models.Role.objects.create(name="部门管理员")
        manager_role.permissions.set([manager_permission])

        manager = models.User.objects.create_user(
            username="manager",
            password="ExistingManager!2026",
            real_name="旧经理名",
            employee=employees[0],
            managed_department=self.department,
            phone="13900000001",
            email="manager@example.com",
            is_staff=True,
        )
        manager.roles.set([manager_role])
        staff = models.User.objects.create_user(
            username="staff",
            password="ExistingStaff!2026",
            real_name="旧员工名",
            employee=employees[2],
            is_active=True,
        )
        staff.roles.set([self.regular_role])

        manager_id = manager.id
        staff_id = staff.id
        manager_password_hash = manager.password
        staff_password_hash = staff.password
        manager_role_ids = set(manager.roles.values_list("id", flat=True))

        task = models.Task.objects.create(
            level=1,
            title="保留个人任务",
            detail="账号同步不得改变任务外键",
            assignee=manager,
        )
        receipt = models.TaskRead.objects.create(task=task, user=manager)

        result = sync_employee_accounts(
            apply=True,
            initial_password=STRONG_INITIAL_PASSWORD,
        )
        self.assertEqual(result.created_count, 6)
        self.assertEqual(result.updated_count, 2)
        self.assertEqual(result.unchanged_count, 0)

        manager.refresh_from_db()
        staff.refresh_from_db()
        self.assertEqual(manager.id, manager_id)
        self.assertEqual(staff.id, staff_id)
        self.assertEqual((manager.username, manager.real_name), ("张三", "张三"))
        self.assertEqual((staff.username, staff.real_name), ("王五", "王五"))
        self.assertEqual(manager.password, manager_password_hash)
        self.assertEqual(staff.password, staff_password_hash)
        self.assertEqual(set(manager.roles.values_list("id", flat=True)), manager_role_ids)
        self.assertEqual(manager.managed_department, self.department)
        self.assertTrue(manager.is_staff)
        self.assertFalse(manager.is_superuser)
        self.assertTrue(manager.is_active)
        self.assertEqual(manager.phone, "13900000001")
        self.assertEqual(manager.email, "manager@example.com")

        task.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual(task.assignee_id, manager_id)
        self.assertEqual(receipt.user_id, manager_id)

        for employee in employees:
            user = models.User.objects.get(employee=employee)
            if employee.id not in {employees[0].id, employees[2].id}:
                self.assertTrue(user.check_password(STRONG_INITIAL_PASSWORD))
                self.assertTrue(user.is_active)
                self.assertFalse(user.is_staff)
                self.assertFalse(user.is_superuser)
                self.assertIsNone(user.managed_department_id)
                self.assertEqual(list(user.roles.values_list("name", flat=True)), ["普通员工"])

        self.assertTrue(self.client.login(username="张三", password="ExistingManager!2026"))
        self.client.logout()
        self.assertTrue(self.client.login(username="李四", password=STRONG_INITIAL_PASSWORD))

        second = sync_employee_accounts(apply=True, initial_password=None)
        self.assertEqual(second.created_count, 0)
        self.assertEqual(second.updated_count, 0)
        self.assertEqual(second.unchanged_count, 8)
        self.assertEqual(models.User.objects.filter(employee__isnull=False).count(), 8)

    def test_duplicate_invalid_and_occupied_names_fail_closed_without_suffixes(self):
        existing_employee = self.create_employee("现有员工", 20)
        existing_user = models.User.objects.create_user(
            username="old_username",
            password="Existing!2026",
            employee=existing_employee,
        )
        self.create_employee("重复姓名", 21)
        self.create_employee("重复姓名", 22)
        models.User.objects.create_user(username="占用姓名", password="External!2026")
        occupied_employee = self.create_employee("占用姓名", 23)
        invalid_employee = self.create_employee("非法 姓名!", 24)

        with self.assertRaises(EmployeeAccountSyncError) as context:
            sync_employee_accounts(
                apply=True,
                initial_password=STRONG_INITIAL_PASSWORD,
            )
        message = str(context.exception)
        self.assertIn("姓名冲突", message)
        self.assertIn("已被账号", message)
        self.assertIn("不能作为严格用户名", message)

        existing_user.refresh_from_db()
        self.assertEqual(existing_user.username, "old_username")
        self.assertFalse(models.User.objects.filter(employee=occupied_employee).exists())
        self.assertFalse(models.User.objects.filter(employee=invalid_employee).exists())
        self.assertFalse(models.User.objects.filter(username__startswith="重复姓名_").exists())

    def test_regular_role_must_have_required_views_and_no_write_permissions(self):
        employee = self.create_employee("权限员工", 30)
        task_view = models.Permission.objects.get(code="task.view")
        self.regular_role.permissions.remove(task_view)
        with self.assertRaises(EmployeeAccountSyncError) as missing_context:
            sync_employee_accounts(apply=True, initial_password=STRONG_INITIAL_PASSWORD)
        self.assertIn("task.view", str(missing_context.exception))
        self.assertFalse(models.User.objects.filter(employee=employee).exists())

        self.regular_role.permissions.add(task_view)
        dangerous = models.Permission.objects.create(
            module="task", name="维护任务", code="task.edit"
        )
        self.regular_role.permissions.add(dangerous)
        with self.assertRaises(EmployeeAccountSyncError) as unsafe_context:
            sync_employee_accounts(apply=True, initial_password=STRONG_INITIAL_PASSWORD)
        self.assertIn("非只读权限", str(unsafe_context.exception))
        self.assertFalse(models.User.objects.filter(employee=employee).exists())

    def test_weak_password_requires_explicit_override_without_changing_global_validators(self):
        employee = self.create_employee("弱密员工", 40)
        with self.assertRaises(EmployeeAccountSyncError):
            sync_employee_accounts(apply=True, initial_password="123456")
        self.assertFalse(models.User.objects.filter(employee=employee).exists())

        result = sync_employee_accounts(
            apply=True,
            initial_password="123456",
            allow_weak=True,
        )
        self.assertEqual(result.created_count, 1)
        user = models.User.objects.get(employee=employee)
        self.assertTrue(user.check_password("123456"))
        with self.assertRaises(ValidationError):
            validate_password("123456", user=models.User(username="another_user"))

    def test_csv_has_bom_new_password_only_and_preserved_marker_for_existing(self):
        existing_employee = self.create_employee("旧账号员工", 50)
        existing_user = models.User.objects.create_user(
            username="legacy_login",
            password="ExistingSecret!2026",
            employee=existing_employee,
        )
        self.create_employee("新账号员工", 51)
        existing_hash = existing_user.password

        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "accounts.csv"
            result = sync_employee_accounts(
                apply=True,
                initial_password=STRONG_INITIAL_PASSWORD,
                output_path=report,
            )
            self.assertEqual(result.report_path, report)
            raw = report.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            decoded = raw.decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(decoded)))
            by_name = {row["employee_name"]: row for row in rows}
            self.assertEqual(
                by_name["旧账号员工"]["initial_password"],
                PRESERVED_PASSWORD_MARKER,
            )
            self.assertEqual(
                by_name["新账号员工"]["initial_password"],
                STRONG_INITIAL_PASSWORD,
            )
            self.assertNotIn(existing_hash, decoded)

    def test_existing_output_refusal_rolls_back_database_and_does_not_overwrite(self):
        employee = self.create_employee("回滚员工", 60)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "existing.csv"
            report.write_bytes(b"sentinel")
            with self.assertRaises(EmployeeAccountSyncError):
                sync_employee_accounts(
                    apply=True,
                    initial_password=STRONG_INITIAL_PASSWORD,
                    output_path=report,
                )
            self.assertEqual(report.read_bytes(), b"sentinel")
            self.assertFalse(models.User.objects.filter(employee=employee).exists())


class EmployeeAccountCommandTest(EmployeeAccountFixtureMixin, TestCase):
    def setUp(self):
        self.create_permission_baseline()
        self.employee = self.create_employee("命令员工", 70)

    def test_command_is_dry_run_by_default_and_reads_password_from_environment(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {DEFAULT_PASSWORD_ENV: STRONG_INITIAL_PASSWORD}, clear=False
        ):
            report = Path(directory) / "dry-run.csv"
            stdout = io.StringIO()
            call_command(
                "sync_employee_accounts",
                "--output", str(report),
                stdout=stdout,
            )
            self.assertIn("DRY-RUN", stdout.getvalue())
            self.assertTrue(report.exists())
            self.assertFalse(models.User.objects.filter(employee=self.employee).exists())

    def test_command_needs_apply_and_allow_weak_for_requested_123456(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {DEFAULT_PASSWORD_ENV: "123456"}, clear=False
        ):
            rejected_report = Path(directory) / "rejected.csv"
            with self.assertRaises(CommandError):
                call_command(
                    "sync_employee_accounts",
                    "--apply",
                    "--output", str(rejected_report),
                )
            self.assertFalse(rejected_report.exists())
            self.assertFalse(models.User.objects.filter(employee=self.employee).exists())

            applied_report = Path(directory) / "applied.csv"
            call_command(
                "sync_employee_accounts",
                "--apply",
                "--allow-weak",
                "--output", str(applied_report),
                stdout=io.StringIO(),
            )
            user = models.User.objects.get(employee=self.employee)
            self.assertTrue(user.check_password("123456"))
            self.assertTrue(applied_report.exists())


class EmployeeAccountCommitFailureTest(EmployeeAccountFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.create_permission_baseline()
        self.employee = self.create_employee("提交失败员工", 80)

    def test_commit_failure_removes_report_created_by_this_run(self):
        database_connection = connections["default"]
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "commit-failure.csv"
            with mock.patch.object(
                database_connection,
                "commit",
                side_effect=DatabaseError("forced commit failure"),
            ):
                with self.assertRaises(EmployeeAccountSyncError):
                    sync_employee_accounts(
                        apply=True,
                        initial_password=STRONG_INITIAL_PASSWORD,
                        output_path=report,
                    )
            self.assertFalse(report.exists())
            self.assertFalse(models.User.objects.filter(employee=self.employee).exists())
