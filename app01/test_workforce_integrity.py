"""Workforce integrity contracts; execute only through Django's isolated test runner.

These tests intentionally do not seed, flush, rename IDs, or repair a live database.
They cover explicit employee consent, conservative repairs, protected business
references, account-source drift and transactional rollback.
"""
import datetime
import json
import os
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, models as db_models
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from app01 import models
from app01.management.commands.init_data import ROLE_PERMISSIONS
from app01.utils.database_integrity import audit_hrms_data
from app01.utils.demo_workforce import repair_demo_workforce
from app01.utils.form import EmployeeModelForm


TEST_PASSWORD = "WorkforceIntegrity!2026_Q9"
PASSWORD_ENV = "DEPARTMENT_MANAGER_INITIAL_PASSWORD"


def joined_at(year=2024, month=1, day=1):
    return timezone.make_aware(
        datetime.datetime(year, month, day, 9), timezone.get_default_timezone(),
    )


def records(model):
    return list(model.objects.order_by("pk").values())


class IntegrityFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.tech = models.Department.objects.create(title="技术部")
        cls.market = models.Department.objects.create(title="市场部")
        cls.manager_role = models.Role.objects.create(name="部门管理员")
        cls.regular_role = models.Role.objects.create(name="普通员工")
        permissions = {}
        for code in set(ROLE_PERMISSIONS["部门管理员"]) | set(ROLE_PERMISSIONS["普通员工"]):
            permissions[code] = models.Permission.objects.create(
                code=code, module=code.split(".", 1)[0], name=code,
            )
        cls.manager_role.permissions.set(
            [permissions[code] for code in ROLE_PERMISSIONS["部门管理员"]]
        )
        cls.regular_role.permissions.set(
            [permissions[code] for code in ROLE_PERMISSIONS["普通员工"]]
        )
        cls.admin = models.User.objects.create_superuser(
            username="integrity_admin", password=TEST_PASSWORD,
            real_name="完整性测试管理员", email="admin@example.test",
        )

    def employee(self, name="研发员工", mobile="13995000001", **overrides):
        values = {
            "name": name, "position": "研发工程师", "age": 30, "gender": 1,
            "account": Decimal("2500.00"), "mobile": mobile,
            "department": self.tech, "create_time": joined_at(), "is_demo": False,
        }
        values.update(overrides)
        return models.Employee.objects.create(**values)

    def linked_user(self, employee, **overrides):
        values = {
            "username": employee.name, "real_name": employee.name,
            "phone": employee.mobile, "email": "keep@example.test",
            "employee": employee, "password": TEST_PASSWORD,
        }
        values.update(overrides)
        user = models.User.objects.create_user(**values)
        user.roles.set([self.regular_role])
        return user

    def full_snapshot(self):
        model_list = (
            models.Department, models.Employee, models.User, models.Role,
            models.Permission, models.Attendance, models.Salary, models.Notice,
            models.NoticeRead, models.Task, models.TaskRead,
            models.User.roles.through, models.Role.permissions.through,
            models.User.groups.through, models.User.user_permissions.through,
        )
        return {model._meta.label_lower: records(model) for model in model_list}

    def business_snapshot(self):
        return {
            model._meta.label_lower: records(model)
            for model in (models.Attendance, models.Salary, models.Notice,
                          models.NoticeRead, models.Task, models.TaskRead)
        }

    def authority_snapshot(self):
        return (
            list(models.User.objects.order_by("id").values(
                "id", "username", "password", "employee_id", "managed_department_id",
                "is_active", "is_staff", "is_superuser", "first_name", "last_name",
                "last_login", "date_joined",
            )),
            records(models.User.roles.through), records(models.Role),
            records(models.Permission), records(models.Role.permissions.through),
            records(models.User.groups.through), records(models.User.user_permissions.through),
            records(models.Department),
        )


class WorkforceAuditTest(IntegrityFixtureMixin, TestCase):
    def test_normal_framework_tables_and_primary_key_gaps_are_not_corruption(self):
        self.employee(name="研发员工甲", mobile="13995000001", pk=101)
        self.employee(name="研发员工乙", mobile="13995000002", pk=207)
        before = self.full_snapshot()
        with mock.patch.object(models.Employee, "save", side_effect=AssertionError("audit wrote an employee")), \
                mock.patch.object(models.User, "save", side_effect=AssertionError("audit wrote a user")):
            result = audit_hrms_data()
        self.assertEqual(self.full_snapshot(), before)
        self.assertEqual(result["employees"], 2)
        self.assertEqual(result["foreign_key_orphans"], 0)
        self.assertEqual(result["employee_positions_missing"], 0)
        self.assertEqual(result["department_managers_without_employee"], 0)
        self.assertIn("django_migrations", result["normal_framework_tables"])
        self.assertIn("auth_group", result["normal_framework_tables"])
        self.assertIn(models.User.roles.through._meta.db_table, result["table_counts"])
        self.assertFalse(any(row["severity"] == "error" for row in result["findings"]))
        self.assertIn("primary-key gaps", result["note"])

    def test_audit_reports_missing_profiles_and_business_anomalies_without_writing(self):
        employee = self.employee(position="", create_time=joined_at(2026, 1, 1))
        manager = models.User.objects.create_user(
            username="熊大", real_name="熊大", password=TEST_PASSWORD,
            managed_department=self.tech, email="manager@example.test",
        )
        manager.roles.set([self.manager_role])
        salary = models.Salary.objects.create(
            employee=employee, month="2025-01", base_salary=Decimal("5000.00"),
        )
        attendance = models.Attendance.objects.create(
            employee=employee, date=datetime.date(2025, 1, 2),
            check_in=datetime.time(8, 50), check_out=datetime.time(18, 10),
        )
        models.Salary.objects.filter(pk=salary.pk).update(total=Decimal("1.00"))
        models.Attendance.objects.filter(pk=attendance.pk).update(status=4)
        before = self.full_snapshot()
        result = audit_hrms_data()
        codes = {row["code"] for row in result["findings"]}
        self.assertTrue({
            "missing_position", "manager_without_employee", "business_records_before_hire",
            "salary_total_difference", "attendance_status_difference",
        }.issubset(codes))
        self.assertEqual(result["department_managers_without_employee"], 1)
        self.assertEqual(result["employee_positions_missing"], 1)
        self.assertEqual(self.full_snapshot(), before)
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn(TEST_PASSWORD, serialized)
        self.assertNotIn(manager.password, serialized)

    def test_independent_account_phone_is_informational_and_is_preserved(self):
        employee = self.employee()
        user = self.linked_user(employee, phone="13995009999")
        before = self.full_snapshot()
        result = audit_hrms_data()
        differences = [row for row in result["findings"]
                       if row["code"] == "account_employee_phone_difference" and row["id"] == user.id]
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0]["severity"], "info")
        self.assertEqual(self.full_snapshot(), before)

    def test_json_audit_command_returns_inventory_without_credentials_or_changes(self):
        before = self.full_snapshot()
        output = StringIO()
        call_command("audit_hrms_data", "--json", stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(result["users"], models.User.objects.count())
        self.assertEqual(result["foreign_key_orphans"], 0)
        self.assertEqual(self.full_snapshot(), before)
        self.assertNotIn(TEST_PASSWORD, output.getvalue())
        self.assertNotIn(self.admin.password, output.getvalue())


class ManagerEmployeeSourceTest(IntegrityFixtureMixin, TestCase):
    def command(self, *flags, assignments=("熊大=技术部",), password=TEST_PASSWORD):
        args = []
        for assignment in assignments:
            args.extend(["--assignment", assignment])
        environment = {} if password is None else {PASSWORD_ENV: password}
        with mock.patch.dict(os.environ, environment, clear=False):
            if password is None:
                os.environ.pop(PASSWORD_ENV, None)
            output = StringIO()
            call_command("provision_department_managers", *args, *flags, stdout=output)
        return output.getvalue()

    def test_explicit_employee_id_reuses_only_that_employee_without_overwriting_it(self):
        employee = self.employee(name="熊大", position="资深研发负责人")
        before_employees = records(models.Employee)
        self.command("--apply", "--employee", "熊大={}".format(employee.id))
        user = models.User.objects.get(username="熊大")
        self.assertEqual(user.employee_id, employee.id)
        self.assertEqual(user.managed_department_id, self.tech.id)
        self.assertEqual(user.phone, employee.mobile)
        self.assertEqual(list(user.roles.values_list("id", flat=True)), [self.manager_role.id])
        self.assertTrue(user.check_password(TEST_PASSWORD))
        self.assertEqual(records(models.Employee), before_employees)

    def test_unbound_same_name_employee_is_not_taken_over_even_with_demo_consent(self):
        self.employee(name="熊大")
        before = self.full_snapshot()
        for flags in (("--apply",), ("--apply", "--demo-employees")):
            with self.subTest(flags=flags):
                with self.assertRaises(CommandError):
                    self.command(*flags)
                self.assertEqual(self.full_snapshot(), before)

    def test_employee_already_bound_to_another_account_rejects_the_whole_batch(self):
        employee = self.employee(name="熊大")
        self.linked_user(employee, username="existing_owner")
        before = self.full_snapshot()
        with self.assertRaises(CommandError):
            self.command(
                "--apply", "--demo-employees", "--employee", "熊大={}".format(employee.id),
                assignments=("熊二=市场部", "熊大=技术部"),
            )
        self.assertEqual(self.full_snapshot(), before)

    def test_partial_employee_selection_cannot_partially_apply_a_batch(self):
        employee = self.employee(name="熊大")
        before = self.full_snapshot()
        with self.assertRaises(CommandError):
            self.command(
                "--apply", "--employee", "熊大={}".format(employee.id),
                assignments=("熊大=技术部", "熊二=市场部"),
            )
        self.assertEqual(self.full_snapshot(), before)

    def test_invalid_employee_selection_is_rejected_before_any_writes(self):
        before = self.full_snapshot()
        for selection in ("熊大=0", "熊大=-1", "熊大=not-id", "熊大=999999", "未分配=1"):
            with self.subTest(selection=selection):
                with self.assertRaises(CommandError):
                    self.command("--apply", "--employee", selection)
                self.assertEqual(self.full_snapshot(), before)

    def test_legacy_manager_gets_own_demo_employee_without_password_reset(self):
        user = models.User.objects.create_user(
            username="熊大", real_name="熊大", password=TEST_PASSWORD,
            managed_department=self.tech, phone="13995008888", email="legacy@example.test",
        )
        user.roles.set([self.manager_role])
        before = self.authority_snapshot()
        before_user = models.User.objects.filter(pk=user.pk).values().get()
        with mock.patch.object(models.User, "set_password", side_effect=AssertionError("existing password reset")):
            self.command("--apply", "--demo-employees", password=None)
        user.refresh_from_db()
        self.assertIsNotNone(user.employee_id)
        self.assertEqual(user.employee.name, user.username)
        self.assertTrue(user.employee.is_demo)
        self.assertEqual(user.phone, before_user["phone"])
        self.assertEqual(user.email, before_user["email"])
        after = self.authority_snapshot()
        expected_users = [dict(row) for row in before[0]]
        for row in expected_users:
            if row["id"] == user.id:
                row["employee_id"] = user.employee_id
        self.assertEqual(after, (expected_users,) + before[1:])
        linked_state = self.full_snapshot()
        with mock.patch.object(models.User, "set_password", side_effect=AssertionError("idempotent password reset")):
            self.command("--apply", password=None)
        self.assertEqual(self.full_snapshot(), linked_state)

    def test_demo_dry_run_does_not_create_employee_or_account(self):
        before = self.full_snapshot()
        with mock.patch.object(models.Employee, "save", side_effect=AssertionError("dry-run employee write")), \
                mock.patch.object(models.User, "save", side_effect=AssertionError("dry-run user write")):
            output = self.command("--demo-employees", password=None)
        self.assertIn("DRY-RUN", output)
        self.assertEqual(self.full_snapshot(), before)


class DemoWorkforceRepairTest(IntegrityFixtureMixin, TestCase):
    def setUp(self):
        self.demo = self.employee(
            name="演示研发员", mobile="13996000001", position="", is_demo=True,
            create_time=joined_at(2026, 1, 1),
        )
        self.demo_user = self.linked_user(
            self.demo, username="demo_login_alias", real_name="", phone="", email="",
        )
        self.real = self.employee(
            name="真实研发员", mobile="13996000002", position="", is_demo=False,
            create_time=joined_at(2026, 1, 1),
        )
        self.real_user = self.linked_user(
            self.real, username="custom_login_alias", real_name="独立联系备注",
            phone="13996008888", email="personal@example.test",
        )
        self.seed = self.employee(
            name="张三", mobile="13800000001", position="保留既有研发负责人", is_demo=False,
            create_time=joined_at(2026, 1, 1),
        )
        for employee in (self.demo, self.real, self.seed):
            models.Salary.objects.create(
                employee=employee, month="2025-01", base_salary=Decimal("5500.00"),
                bonus=Decimal("200.00"), deduction=Decimal("30.00"),
            )
        models.Attendance.objects.create(
            employee=self.demo, date=datetime.date(2025, 1, 2),
            check_in=datetime.time(8, 50), check_out=datetime.time(18, 10),
        )
        notice = models.Notice.objects.create(
            title="保留公告", content="业务内容不能被资料修复覆盖", publisher=self.admin,
        )
        models.NoticeRead.objects.create(notice=notice, user=self.demo_user)
        task = models.Task.objects.create(
            title="保留个人任务", detail="保留原目标和阅读回执", assignee=self.demo_user,
        )
        models.TaskRead.objects.create(task=task, user=self.demo_user)

    def test_default_repair_is_a_read_only_plan_and_does_not_dump_credentials(self):
        before = self.full_snapshot()
        with mock.patch.object(models.Employee, "save", side_effect=AssertionError("dry-run employee write")), \
                mock.patch.object(models.User, "save", side_effect=AssertionError("dry-run user write")):
            result = repair_demo_workforce()
        self.assertFalse(result.applied)
        self.assertGreater(result.updated_employees, 0)
        self.assertGreater(result.updated_users, 0)
        self.assertEqual(self.full_snapshot(), before)
        serialized = json.dumps(result.as_dict(), ensure_ascii=False, default=str)
        self.assertNotIn(TEST_PASSWORD, serialized)
        self.assertNotIn(self.demo_user.password, serialized)

    def test_apply_requires_explicit_demo_consent_and_consent_requires_apply(self):
        before = self.full_snapshot()
        for flags in (("--apply",), ("--allow-demo",)):
            with self.subTest(flags=flags):
                with self.assertRaises(CommandError):
                    call_command("repair_demo_workforce", *flags, stdout=StringIO())
                self.assertEqual(self.full_snapshot(), before)

    def test_apply_fills_only_blanks_and_preserves_credentials_scopes_and_business(self):
        before_authority = self.authority_snapshot()
        before_business = self.business_snapshot()
        immutable_fields = ("id", "name", "age", "gender", "account", "mobile", "department_id", "avatar")
        before_employee_details = list(models.Employee.objects.order_by("id").values(*immutable_fields))
        before_profiles = list(models.User.objects.order_by("id").values("id", "real_name", "phone", "email"))
        real_joined = self.real.create_time
        result = repair_demo_workforce(apply=True, allow_demo=True)
        self.assertTrue(result.applied)
        self.assertEqual(result.created_employees, 0)
        self.demo.refresh_from_db()
        self.real.refresh_from_db()
        self.seed.refresh_from_db()
        self.demo_user.refresh_from_db()
        self.real_user.refresh_from_db()
        self.assertTrue(self.demo.position.strip())
        self.assertTrue(self.real.position.strip())
        self.assertEqual(self.seed.position, "保留既有研发负责人")
        self.assertLess(timezone.localtime(self.demo.create_time).date(), datetime.date(2025, 1, 1))
        self.assertLess(timezone.localtime(self.seed.create_time).date(), datetime.date(2025, 1, 1))
        self.assertEqual(self.real.create_time, real_joined)
        self.assertEqual((self.demo_user.real_name, self.demo_user.phone), (self.demo.name, self.demo.mobile))
        models.User._meta.get_field("email").clean(self.demo_user.email, self.demo_user)
        self.assertTrue(self.demo_user.email)
        self.assertEqual((self.real_user.real_name, self.real_user.phone, self.real_user.email),
                         ("独立联系备注", "13996008888", "personal@example.test"))
        self.assertEqual(self.authority_snapshot(), before_authority)
        self.assertEqual(self.business_snapshot(), before_business)
        self.assertEqual(list(models.Employee.objects.order_by("id").values(*immutable_fields)), before_employee_details)
        after_profiles = {row["id"]: row for row in models.User.objects.values("id", "real_name", "phone", "email")}
        for row in before_profiles:
            for field in ("real_name", "phone", "email"):
                if (row[field] or "").strip():
                    self.assertEqual(after_profiles[row["id"]][field], row[field])
        self.assertFalse(self.real.is_demo)
        self.assertIn("测试职位", self.real.position)

    def test_repeated_repair_is_idempotent_and_never_promotes_unknown_history_to_demo(self):
        original_real_joined = self.real.create_time
        repair_demo_workforce(apply=True, allow_demo=True)
        after_first = self.full_snapshot()
        second = repair_demo_workforce(apply=True, allow_demo=True)
        self.assertEqual((second.created_employees, second.updated_employees, second.updated_users), (0, 0, 0))
        self.assertEqual(self.full_snapshot(), after_first)
        self.real.refresh_from_db()
        self.assertEqual(self.real.create_time, original_real_joined)

    def test_neither_seed_name_nor_seed_mobile_alone_authorizes_hire_date_changes(self):
        same_name = self.employee(
            name="王五", mobile="13996000003", position="自定义岗位", create_time=joined_at(2026, 1, 1),
        )
        same_mobile = self.employee(
            name="手机号已转用", mobile="13800000002", position="实际员工岗位", create_time=joined_at(2026, 1, 1),
        )
        for employee in (same_name, same_mobile):
            models.Salary.objects.create(employee=employee, month="2025-01", base_salary=Decimal("6000.00"))
        before_dates = (same_name.create_time, same_mobile.create_time)
        repair_demo_workforce(apply=True, allow_demo=True)
        same_name.refresh_from_db()
        same_mobile.refresh_from_db()
        self.assertEqual((same_name.create_time, same_mobile.create_time), before_dates)
        self.assertFalse(same_name.is_demo)
        self.assertFalse(same_mobile.is_demo)

    def test_invalid_source_contact_fails_before_any_partial_repair(self):
        models.Employee.objects.filter(pk=self.demo.pk).update(mobile="invalid")
        before = self.full_snapshot()
        with self.assertRaises(CommandError):
            call_command("repair_demo_workforce", "--apply", "--allow-demo", stdout=StringIO())
        self.assertEqual(self.full_snapshot(), before)

    def test_database_failure_rolls_back_all_employee_and_user_updates(self):
        before = self.full_snapshot()
        original_save = models.User.save

        def fail_after_actual_user_write(user, *args, **kwargs):
            original_save(user, *args, **kwargs)
            raise DatabaseError("forced repair user write failure")

        with mock.patch.object(models.User, "save", new=fail_after_actual_user_write):
            with self.assertRaises(DatabaseError):
                call_command("repair_demo_workforce", "--apply", "--allow-demo", stdout=StringIO())
        self.assertEqual(self.full_snapshot(), before)

    def test_legacy_manager_repair_adds_only_a_demo_employee_and_keeps_account_history(self):
        manager = models.User.objects.create_user(
            username="熊大", real_name="熊大", password=TEST_PASSWORD,
            managed_department=self.tech, email="manager@example.test",
        )
        manager.roles.set([self.manager_role])
        task = models.Task.objects.create(title="经理旧任务", detail="不能重建账户", assignee=manager)
        receipt = models.TaskRead.objects.create(task=task, user=manager)
        before_user = models.User.objects.filter(pk=manager.pk).values().get()
        before_business = self.business_snapshot()
        before_roles = records(models.User.roles.through)
        with mock.patch.object(models.User, "set_password", side_effect=AssertionError("repair reset a password")):
            result = repair_demo_workforce(apply=True, allow_demo=True)
        manager.refresh_from_db()
        self.assertEqual(result.created_employees, 1)
        self.assertEqual(manager.employee.name, "熊大")
        self.assertTrue(manager.employee.is_demo)
        for field in ("id", "username", "password", "managed_department_id", "is_active", "is_staff", "is_superuser", "email"):
            self.assertEqual(getattr(manager, field), before_user[field])
        self.assertEqual(records(models.User.roles.through), before_roles)
        self.assertEqual(self.business_snapshot(), before_business)
        self.assertTrue(models.TaskRead.objects.filter(pk=receipt.pk, task_id=task.pk, user_id=manager.pk).exists())


class EmployeeAccountDriftTest(IntegrityFixtureMixin, TestCase):
    def setUp(self):
        self.person = self.employee(name="来源研发员", mobile="13997000001")
        self.user = self.linked_user(self.person, managed_department=self.market, is_staff=True)
        self.user.roles.set([self.manager_role, self.regular_role])
        group = Group.objects.create(name="保留的Django组")
        self.user.groups.add(group)
        task = models.Task.objects.create(title="身份更新保留任务", detail="外键必须稳定", assignee=self.user)
        models.TaskRead.objects.create(task=task, user=self.user)
        models.Salary.objects.create(employee=self.person, month="2025-01", base_salary=Decimal("3000.00"))

    def stable_state(self):
        authority = self.authority_snapshot()
        users = [dict(row) for row in authority[0]]
        for row in users:
            if row["id"] == self.user.id:
                row.pop("username")
        return (users,) + authority[1:]

    def test_source_backed_name_and_phone_drift_sync_without_credential_or_fk_changes(self):
        before = self.stable_state()
        business = self.business_snapshot()
        self.person.name = "更新研发员"
        self.person.mobile = "13997000002"
        with mock.patch.object(models.User, "set_password", side_effect=AssertionError("employee edit reset password")):
            self.person.save()
        self.user.refresh_from_db()
        self.assertEqual((self.user.username, self.user.real_name, self.user.phone),
                         ("更新研发员", "更新研发员", "13997000002"))
        self.assertEqual(self.stable_state(), before)
        self.assertEqual(self.business_snapshot(), business)

    def test_explicit_account_alias_and_contact_fields_are_not_overwritten(self):
        self.user.username = "custom_account_alias"
        self.user.real_name = "账号独立姓名备注"
        self.user.phone = "13997008888"
        self.user.save(update_fields=["username", "real_name", "phone"])
        before = self.authority_snapshot()
        self.person.name = "更新研发员"
        self.person.mobile = "13997000002"
        self.person.save()
        self.user.refresh_from_db()
        self.assertEqual((self.user.username, self.user.real_name, self.user.phone),
                         ("custom_account_alias", "账号独立姓名备注", "13997008888"))
        self.assertEqual(self.authority_snapshot(), before)

    def test_blank_source_fields_are_filled_without_changing_a_custom_login_alias(self):
        self.user.username = "custom_account_alias"
        self.user.real_name = "   "
        self.user.phone = " "
        self.user.save(update_fields=["username", "real_name", "phone"])
        before = self.authority_snapshot()
        self.person.name = "更新研发员"
        self.person.mobile = "13997000002"
        self.person.save()
        self.user.refresh_from_db()
        self.assertEqual((self.user.username, self.user.real_name, self.user.phone),
                         ("custom_account_alias", "更新研发员", "13997000002"))
        self.assertEqual(self.authority_snapshot(), before)

    def test_partial_position_save_does_not_leak_unsaved_name_or_mobile(self):
        before = self.full_snapshot()
        self.person.name = "尚未保存的姓名"
        self.person.mobile = "13997000002"
        self.person.position = "测试负责人"
        self.person.save(update_fields=["position"])
        after = self.full_snapshot()
        expected = {key: [dict(row) for row in rows] for key, rows in before.items()}
        for row in expected[models.Employee._meta.label_lower]:
            if row["id"] == self.person.id:
                row["position"] = "测试负责人"
        self.assertEqual(after, expected)

    def test_name_only_save_uses_persisted_mobile_when_backfilling_blank_phone(self):
        self.user.phone = ""
        self.user.save(update_fields=["phone"])
        old_mobile = self.person.mobile
        self.person.name = "更新研发员"
        self.person.mobile = "13997000002"
        self.person.save(update_fields=["name"])
        self.person.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.person.mobile, old_mobile)
        self.assertEqual(self.user.phone, old_mobile)
        self.assertEqual((self.user.username, self.user.real_name), ("更新研发员", "更新研发员"))

    def test_username_collision_rolls_back_employee_and_account(self):
        models.User.objects.create_user(username="冲突研发员", password=TEST_PASSWORD)
        before = self.full_snapshot()
        self.person.name = "冲突研发员"
        self.person.mobile = "13997000002"
        with self.assertRaises(ValidationError) as raised:
            self.person.save()
        self.assertIn("name", raised.exception.message_dict)
        self.assertEqual(self.full_snapshot(), before)

    def test_username_collision_is_also_a_form_error_without_partial_save(self):
        models.User.objects.create_user(username="冲突研发员", password=TEST_PASSWORD)
        before = self.full_snapshot()
        form = EmployeeModelForm(data={
            "name": "冲突研发员", "position": self.person.position, "age": self.person.age,
            "gender": self.person.gender, "account": self.person.account,
            "create_time": "2024-01-01", "mobile": self.person.mobile,
            "department": self.person.department_id,
        }, instance=self.person, user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)
        self.assertEqual(self.full_snapshot(), before)

    def test_account_write_failure_rolls_back_the_employee_edit(self):
        before = self.full_snapshot()
        original_save = models.User.save

        def fail_after_actual_account_write(user, *args, **kwargs):
            original_save(user, *args, **kwargs)
            raise DatabaseError("forced synchronized account write failure")

        self.person.name = "更新研发员"
        self.person.mobile = "13997000002"
        with mock.patch.object(models.User, "save", new=fail_after_actual_account_write):
            with self.assertRaises(DatabaseError):
                self.person.save()
        self.assertEqual(self.full_snapshot(), before)


class ProtectedWorkforceReferenceTest(IntegrityFixtureMixin, TestCase):
    def test_all_five_foreign_keys_use_protect(self):
        for model, field_name in (
            (models.User, "employee"), (models.User, "managed_department"),
            (models.Employee, "department"), (models.Attendance, "employee"),
            (models.Salary, "employee"),
        ):
            with self.subTest(model=model.__name__, field=field_name):
                self.assertIs(model._meta.get_field(field_name).remote_field.on_delete, db_models.PROTECT)

    def test_bound_employee_cannot_be_deleted_by_object_or_queryset(self):
        employee = self.employee()
        self.linked_user(employee)
        before = self.full_snapshot()
        for delete in (employee.delete, lambda: models.Employee.objects.filter(pk=employee.pk).delete()):
            with self.assertRaises(ProtectedError):
                delete()
            self.assertEqual(self.full_snapshot(), before)

    def test_attendance_and_salary_each_protect_an_otherwise_unbound_employee(self):
        for index, kind in enumerate((models.Attendance, models.Salary), start=1):
            with self.subTest(kind=kind.__name__):
                employee = self.employee(name="历史员工{}".format(index), mobile="1399800000{}".format(index))
                if kind is models.Attendance:
                    kind.objects.create(employee=employee, date=datetime.date(2025, 1, 1))
                else:
                    kind.objects.create(employee=employee, month="2025-01", base_salary=Decimal("1000.00"))
                before = self.full_snapshot()
                with self.assertRaises(ProtectedError):
                    employee.delete()
                self.assertEqual(self.full_snapshot(), before)

    def test_employee_membership_protects_its_department(self):
        department = models.Department.objects.create(title="仅有员工所属部")
        self.employee(department=department)
        before = self.full_snapshot()
        with self.assertRaises(ProtectedError):
            department.delete()
        self.assertEqual(self.full_snapshot(), before)

    def test_empty_managed_department_is_protected_and_delete_view_is_non_destructive(self):
        department = models.Department.objects.create(title="仅有管理账号部")
        manager = models.User.objects.create_user(
            username="scope_only", password=TEST_PASSWORD, managed_department=department,
        )
        manager.roles.set([self.manager_role])
        self.client.force_login(self.admin)
        before = self.full_snapshot()
        with self.assertRaises(ProtectedError):
            department.delete()
        response = self.client.post("/depart/delete/", {"nid": department.id})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "error_alert.html")
        self.assertEqual(self.full_snapshot(), before)
        manager.refresh_from_db()
        self.assertEqual(manager.managed_department_id, department.id)

    def test_employee_delete_view_reports_protected_history_instead_of_cascading(self):
        employee = self.employee()
        user = self.linked_user(employee)
        models.Salary.objects.create(employee=employee, month="2025-01", base_salary=Decimal("1000.00"))
        self.client.force_login(self.admin)
        before = self.full_snapshot()
        response = self.client.post("/user/delete/", {"nid": employee.id})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "error_alert.html")
        self.assertEqual(self.full_snapshot(), before)
        user.refresh_from_db()
        self.assertEqual(user.employee_id, employee.id)

    def test_truly_unreferenced_employee_and_department_remain_deletable(self):
        employee = self.employee()
        employee_id = employee.id
        empty_department = models.Department.objects.create(title="可删除空部")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post("/user/delete/", {"nid": employee_id}).status_code, 302)
        self.assertFalse(models.Employee.objects.filter(pk=employee_id).exists())
        self.assertEqual(self.client.post("/depart/delete/", {"nid": empty_department.id}).status_code, 302)
        self.assertFalse(models.Department.objects.filter(pk=empty_department.id).exists())

    def test_manager_without_scope_fails_closed_for_tasks_but_superuser_does_not(self):
        employee = self.employee()
        manager = self.linked_user(employee, username="scope_lost", managed_department=None)
        manager.roles.set([self.manager_role])
        task = models.Task.objects.create(title="负责范围缺失任务", detail="不能因此获得全局权限", assignee=manager)
        self.assertTrue(manager.has_perm_code("task.edit"))
        self.assertFalse(models.Task.objects.editable_by(manager).exists())
        self.assertFalse(models.Task.objects.visible_to(manager).exists())
        self.assertFalse(manager.task_target_users().exists())
        self.assertFalse(manager.task_target_departments().exists())
        self.admin.roles.add(self.manager_role)
        self.assertTrue(models.Task.objects.editable_by(self.admin).filter(pk=task.pk).exists())
        self.assertTrue(models.Task.objects.visible_to(self.admin).filter(pk=task.pk).exists())
