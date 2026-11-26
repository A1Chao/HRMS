"""
新手阅读：针对越权、非法输入、日期和金额边界、部分保存等审计问题的回归测试。
本文件的快速 MD5 hasher 仅在隔离测试类中启用，实际登录仍走默认 Django 安全哈希。
"""
import datetime
from decimal import Decimal
from io import StringIO

from django.contrib import admin
from django.contrib.auth.models import Permission as DjangoPermission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from app01 import models
from app01.utils.database_integrity import audit_hrms_data
from app01.utils.form import AttendanceModelForm, SalaryModelForm, UserEditModelForm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SecurityFixRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tech = models.Department.objects.create(title="审计技术部")
        cls.market = models.Department.objects.create(title="审计市场部")
        cls.codes = {}
        for code in ("user.view", "user.edit", "role.view", "role.edit", "employee.view",
                     "employee.edit", "salary.view", "salary.edit", "attendance.view",
                     "attendance.edit", "task.view", "task.edit"):
            cls.codes[code] = models.Permission.objects.create(
                code=code, module=code.split(".")[0], name=code,
            )
        cls.delegate_role = models.Role.objects.create(name="测试账号维护员")
        cls.delegate_role.permissions.set([cls.codes[c] for c in
            ("user.view", "user.edit", "role.view", "role.edit", "employee.view", "task.view")])
        cls.low_role = models.Role.objects.create(name="测试只读账号")
        cls.low_role.permissions.add(cls.codes["employee.view"])
        cls.high_role = models.Role.objects.create(name="测试薪资维护员")
        cls.high_role.permissions.add(cls.codes["salary.edit"])
        cls.root = models.User.objects.create_superuser(username="audit_root", password="RootOld!2026_q8")
        cls.employee = cls.make_employee("测试甲", "13890000001", cls.tech, 2024, 1, 1)
        cls.other_employee = cls.make_employee("测试乙", "13890000002", cls.market, 2024, 1, 1)
        cls.recent = cls.make_employee("测试丙", "13890000003", cls.tech, 2026, 11, 6)
        cls.actor_employee = cls.make_employee("测试丁", "13890000004", cls.tech, 2024, 1, 1)
        cls.actor = models.User.objects.create_user(
            username="audit_delegate", password="Delegate!2026_q8", employee=cls.actor_employee,
            managed_department=cls.tech,
        )
        cls.actor.roles.add(cls.delegate_role)
        cls.target = models.User.objects.create_user(username="audit_target", password="TargetOld!2026_q8",
                                                     employee=cls.employee)
        cls.target.roles.add(cls.low_role)
        cls.outside = models.User.objects.create_user(username="audit_outside", employee=cls.other_employee)
        cls.outside.roles.add(cls.low_role)
        cls.global_actor = models.User.objects.create_user(username="audit_global")
        cls.global_actor.roles.add(cls.delegate_role)

    @staticmethod
    def make_employee(name, mobile, department, year, month, day):
        return models.Employee.objects.create(
            name=name, position="测试工程师", age=25, mobile=mobile, department=department,
            create_time=timezone.make_aware(datetime.datetime(year, month, day, 9)),
        )

    def setUp(self):
        self.client.force_login(self.root)

    def salary_data(self, **changes):
        data = {"employee": self.employee.pk, "month": "2026-11", "base_salary": "5000",
                "bonus": "0", "deduction": "0"}
        data.update(changes)
        return data

    def user_data(self, target=None, **changes):
        target = target or self.target
        data = {"username": target.username, "real_name": target.real_name, "phone": target.phone,
                "email": "", "roles": [self.low_role.pk], "employee": target.employee_id or "",
                "managed_department": target.managed_department_id or "", "is_active": "on"}
        data.update(changes)
        return data

    def test_non_superuser_cannot_edit_reset_disable_or_delete_superuser(self):
        self.client.force_login(self.actor)
        old_password = self.root.password
        for operation in ("edit", "reset"):
            with self.subTest(operation=operation):
                self.assertEqual(self.client.get(f"/admin/{self.root.pk}/{operation}/").status_code, 403)
                payload = self.user_data(self.root) if operation == "edit" else {
                    "password": "Changed!2026_q8", "confirm_password": "Changed!2026_q8"}
                self.assertEqual(self.client.post(f"/admin/{self.root.pk}/{operation}/", payload).status_code, 403)
        self.assertEqual(self.client.post("/admin/delete/", {"nid": self.root.pk}).status_code, 403)
        self.root.refresh_from_db()
        self.assertEqual(self.root.password, old_password)
        self.assertTrue(self.root.is_active)

    def test_leading_zero_id_cannot_bypass_self_delete_protection(self):
        response = self.client.post("/admin/delete/", {"nid": "0" + str(self.root.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(models.User.objects.filter(pk=self.root.pk).exists())

    def test_superuser_protection_does_not_depend_on_csrf_rejection(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        client.get(f"/admin/{self.target.pk}/reset/")
        token = client.cookies["csrftoken"].value
        response = client.post(f"/admin/{self.root.pk}/reset/", {
            "password": "Changed!2026_q8", "confirm_password": "Changed!2026_q8",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(response.status_code, 403)
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password("RootOld!2026_q8"))

    def test_delegate_can_reset_lower_privilege_account_in_scope(self):
        self.client.force_login(self.actor)
        response = self.client.post(f"/admin/{self.target.pk}/reset/", {
            "password": "Changed!2026_q8", "confirm_password": "Changed!2026_q8",
        })
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("Changed!2026_q8"))

    def test_delegate_cannot_reset_outside_department(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(f"/admin/{self.outside.pk}/reset/").status_code, 403)

    def test_delegate_cannot_reset_higher_privilege_or_django_staff(self):
        self.client.force_login(self.global_actor)
        self.target.roles.add(self.high_role)
        self.assertEqual(self.client.get(f"/admin/{self.target.pk}/reset/").status_code, 403)
        self.target.roles.set([self.low_role])
        self.target.is_staff = True
        self.target.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(f"/admin/{self.target.pk}/reset/").status_code, 403)

    def test_delegate_cannot_assign_privileges_above_their_own(self):
        self.client.force_login(self.actor)
        response = self.client.post(f"/admin/{self.target.pk}/edit/", self.user_data(roles=[self.high_role.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("roles", response.context["form"].errors)
        self.assertEqual(set(self.target.roles.values_list("pk", flat=True)), {self.low_role.pk})

    def test_delegate_cannot_expand_own_department_or_rebind_identity(self):
        for data in (self.user_data(self.actor, managed_department=self.market.pk, roles=[self.delegate_role.pk]),
                     self.user_data(self.actor, employee=self.recent.pk, roles=[self.delegate_role.pk])):
            form = UserEditModelForm(data=data, instance=models.User.objects.get(pk=self.actor.pk), operator=self.actor)
            self.assertFalse(form.is_valid())
        self.actor.refresh_from_db()
        self.assertEqual(self.actor.managed_department_id, self.tech.pk)
        self.assertEqual(self.actor.employee_id, self.actor_employee.pk)

    def test_global_delegate_cannot_grant_department_scope(self):
        form = UserEditModelForm(data=self.user_data(managed_department=self.tech.pk),
                                 instance=self.target, operator=self.global_actor)
        self.assertFalse(form.is_valid())
        self.assertIn("managed_department", form.errors)

    def test_superuser_normal_account_management_remains_available(self):
        response = self.client.post(f"/admin/{self.target.pk}/edit/", self.user_data(roles=[self.high_role.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(self.target.roles.values_list("pk", flat=True)), {self.high_role.pk})

    def test_protected_account_buttons_are_hidden(self):
        self.client.force_login(self.global_actor)
        response = self.client.get("/admin/list/")
        self.assertNotContains(response, f'/admin/{self.root.pk}/reset/')
        self.assertContains(response, f'/admin/{self.target.pk}/reset/')

    def test_role_editor_cannot_grant_new_authority(self):
        self.client.force_login(self.global_actor)
        response = self.client.post("/role/add/", {
            "name": "测试越权角色", "description": "", "permissions": [self.codes["salary.edit"].pk],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("permissions", response.context["form"].errors)
        self.assertFalse(models.Role.objects.filter(name="测试越权角色").exists())

    def test_role_editor_cannot_modify_shared_role_of_protected_user(self):
        self.root.roles.add(self.low_role)
        self.client.force_login(self.global_actor)
        self.assertEqual(self.client.get(f"/role/{self.low_role.pk}/edit/").status_code, 403)
        self.assertEqual(self.client.post("/role/delete/", {"nid": self.low_role.pk}).status_code, 403)
        self.assertTrue(models.Role.objects.filter(pk=self.low_role.pk).exists())

    def test_department_role_editor_cannot_modify_cross_department_shared_role(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(f"/role/{self.low_role.pk}/edit/").status_code, 403)

    def test_role_editor_can_create_role_within_own_capabilities(self):
        self.client.force_login(self.global_actor)
        response = self.client.post("/role/add/", {
            "name": "测试合法角色", "description": "", "permissions": [self.codes["employee.view"].pk],
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(models.Role.objects.filter(name="测试合法角色").exists())

    def test_django_admin_security_writes_require_superuser(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        self.actor.user_permissions.set(DjangoPermission.objects.filter(
            content_type__app_label="app01", codename__in=["change_user", "change_role", "change_permission"],
        ))
        request = RequestFactory().get("/admin/")
        request.user = models.User.objects.get(pk=self.actor.pk)
        for model in (models.User, models.Role, models.Permission):
            self.assertFalse(admin.site._registry[model].has_change_permission(request))

    def test_django_admin_salary_and_attendance_use_business_rules(self):
        request = RequestFactory().get("/admin/")
        request.user = self.root
        salary_form = admin.site._registry[models.Salary].get_form(request)
        for changes in ({"month": "2026-99"}, {"month": "0000-01"}, {"base_salary": "-5000"},
                        {"base_salary": "99999999.99", "bonus": "99999999.99"}):
            self.assertFalse(salary_form(data=self.salary_data(**changes)).is_valid())
        attendance_form = admin.site._registry[models.Attendance].get_form(request)
        self.assertFalse(attendance_form(data={"employee": self.employee.pk, "date": "2026-11-06",
                                              "check_in": "18:00", "check_out": "09:00"}).is_valid())

    def test_django_admin_employee_scope_cannot_be_bypassed(self):
        self.actor.roles.add(self.high_role)
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        self.actor.user_permissions.add(DjangoPermission.objects.get(
            content_type__app_label="app01", codename="change_salary"))
        request = RequestFactory().get("/admin/")
        request.user = models.User.objects.get(pk=self.actor.pk)
        salary_admin = admin.site._registry[models.Salary]
        salary = models.Salary.objects.create(employee=self.other_employee, month="2026-11", base_salary=1)
        self.assertFalse(salary_admin.get_queryset(request).filter(pk=salary.pk).exists())
        form = salary_admin.get_form(request)(data=self.salary_data(employee=self.other_employee.pk))
        self.assertFalse(form.is_valid())

    def test_read_only_django_admin_pages_remain_usable(self):
        read_role = models.Role.objects.create(name="后台只读测试")
        read_role.permissions.set([self.codes["attendance.view"], self.codes["salary.view"]])
        self.actor.roles.add(read_role)
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        self.actor.user_permissions.set(DjangoPermission.objects.filter(
            content_type__app_label="app01", codename__in=["view_user", "view_employee", "view_attendance", "view_salary"],
        ))
        attendance = models.Attendance.objects.create(employee=self.employee, date="2026-11-06",
                                                       check_in=datetime.time(9), check_out=datetime.time(18))
        salary = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000)
        self.client.force_login(self.actor)
        for model, pk in (("user", self.target.pk), ("employee", self.employee.pk),
                          ("attendance", attendance.pk), ("salary", salary.pk)):
            with self.subTest(model=model):
                response = self.client.get(f"/admin/app01/{model}/{pk}/change/")
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["has_change_permission"])

    def test_model_clean_rejects_bad_attendance_order(self):
        obj = models.Attendance(employee=self.employee, date=datetime.date(2026, 11, 6),
                                check_in=datetime.time(18), check_out=datetime.time(9))
        with self.assertRaises(ValidationError):
            obj.full_clean()

    def test_salary_overflow_returns_form_error_without_creating_row(self):
        response = self.client.post("/salary/add/", self.salary_data(
            base_salary="99999999.99", bonus="99999999.99"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "实发工资超出允许的金额范围")
        self.assertFalse(models.Salary.objects.exists())

    def test_salary_maximum_total_and_same_hire_month_are_valid(self):
        form = SalaryModelForm(user=self.root, data=self.salary_data(
            employee=self.recent.pk, base_salary="99999999.99"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().total, Decimal("99999999.99"))

    def test_attendance_and_salary_before_hire_are_rejected(self):
        attendance = AttendanceModelForm(user=self.root, data={
            "employee": self.recent.pk, "date": "2026-11-05", "check_in": "09:00", "check_out": "18:00"})
        salary = SalaryModelForm(user=self.root, data=self.salary_data(employee=self.recent.pk, month="2026-10"))
        self.assertFalse(attendance.is_valid())
        self.assertIn("date", attendance.errors)
        self.assertFalse(salary.is_valid())
        self.assertIn("month", salary.errors)

    def test_attendance_on_hire_date_is_valid(self):
        form = AttendanceModelForm(user=self.root, data={
            "employee": self.recent.pk, "date": "2026-11-06", "check_in": "09:00", "check_out": "18:00"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_calendar_years_and_unicode_months_are_rejected(self):
        for month in ("0000-01", "2026-13", "２０２６-01", "2026-01x"):
            form = SalaryModelForm(user=self.root, data=self.salary_data(month=month))
            self.assertFalse(form.is_valid())
            self.assertIn("month", form.errors)

    def test_invalid_attendance_filter_returns_400(self):
        for value in ("not-a-date", "2026-02-30", "2026-13-01", "0" * 5000):
            self.assertEqual(self.client.get("/attendance/list/", {"date": value}).status_code, 400)

    def test_valid_attendance_filter_remains_available(self):
        self.assertEqual(self.client.get("/attendance/list/", {"date": "2026-11-06"}).status_code, 200)

    def test_invalid_delete_ids_and_department_filter_return_400(self):
        for value in ("abc", "-1", "0", "9" * 5000, "9223372036854775808"):
            for route in ("/user/delete/", "/depart/delete/", "/attendance/delete/", "/salary/delete/",
                          "/notice/delete/", "/notice/top/", "/pretty/delete/", "/admin/delete/", "/role/delete/"):
                with self.subTest(route=route, value=value[:20]):
                    self.assertEqual(self.client.post(route, {"nid": value}).status_code, 400)
            self.assertEqual(self.client.get("/user/list/", {"department_id": value}).status_code, 400)

    def test_huge_page_on_empty_queryset_is_safe(self):
        self.assertEqual(self.client.get("/attendance/list/", {"page": "9" * 5000}).status_code, 200)
        self.assertEqual(self.client.get("/attendance/list/", {"page": "9999999999999999999"}).status_code, 200)

    def test_task_pagination_exposes_all_105_tasks_once(self):
        models.Task.objects.bulk_create([models.Task(title=f"分页任务{i}", detail="测试任务详情", assignee=self.target)
                                        for i in range(105)])
        ids = []
        for page in range(1, 7):
            response = self.client.get("/task/list/", {"page": page})
            self.assertEqual(response.context["total_count"], 105)
            self.assertTrue(response.context["page_string"])
            rows = response.context["queryset"]
            self.assertLessEqual(len(rows), 20)
            ids.extend(task.pk for task in rows)
        self.assertEqual(len(ids), 105)
        self.assertEqual(len(set(ids)), 105)

    def test_task_status_filter_and_pagination_preserve_scope(self):
        models.Task.objects.bulk_create([models.Task(title=f"完成任务{i}", detail="测试任务详情", assignee=self.target,
                                                     is_completed=True) for i in range(25)])
        models.Task.objects.create(title="未完成任务", detail="测试任务详情", assignee=self.target)
        response = self.client.get("/task/list/", {"completed": "1", "page": "2"})
        self.assertEqual(response.context["total_count"], 25)
        self.assertEqual(len(response.context["queryset"]), 5)
        self.assertIn("completed=1", response.context["page_string"])
        self.client.force_login(self.actor)
        response = self.client.get("/task/list/", {"completed": "1"})
        self.assertEqual(response.context["total_count"], 0)

    def test_attendance_partial_save_persists_recomputed_status(self):
        row = models.Attendance.objects.create(employee=self.employee, date="2026-11-06",
                                               check_in=datetime.time(9), check_out=datetime.time(18))
        row.check_in = datetime.time(10)
        row.save(update_fields=["check_in"])
        row.refresh_from_db()
        self.assertEqual(row.status, 2)

    def test_attendance_partial_save_ignores_unsaved_other_source(self):
        row = models.Attendance.objects.create(employee=self.employee, date="2026-11-06",
                                               check_in=datetime.time(9), check_out=datetime.time(18))
        row.check_in = datetime.time(10)  # Not in update_fields: must not affect persisted status.
        row.check_out = datetime.time(17)
        row.save(update_fields=["check_out"])
        row.refresh_from_db()
        self.assertEqual(row.check_in, datetime.time(9))
        self.assertEqual(row.status, 3)

    def test_salary_partial_save_persists_recomputed_total(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000)
        row.base_salary = Decimal("6000")
        row.save(update_fields=["base_salary"])
        row.refresh_from_db()
        self.assertEqual(row.total, Decimal("6000"))

    def test_salary_partial_save_ignores_unsaved_other_source(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000, bonus=1000)
        row.base_salary, row.bonus = Decimal("6000"), Decimal("9999")
        row.save(update_fields=["base_salary"])
        row.refresh_from_db()
        self.assertEqual(row.bonus, Decimal("1000"))
        self.assertEqual(row.total, Decimal("7000"))

    def test_salary_unrelated_update_does_not_recompute_unsaved_amounts(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000)
        row.base_salary, row.month = Decimal("6000"), "2026-12"
        row.save(update_fields=["month"])
        row.refresh_from_db()
        self.assertEqual(row.total, Decimal("5000"))

    def test_derived_only_update_uses_persisted_sources(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000)
        row.base_salary, row.total = Decimal("6000"), Decimal("9999")
        row.save(update_fields=["total"])
        row.refresh_from_db()
        self.assertEqual(row.total, Decimal("5000"))

    def test_deferred_partial_save_is_consistent(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000, bonus=1000)
        deferred = models.Salary.objects.only("id", "base_salary").get(pk=row.pk)
        deferred.base_salary = Decimal("6000")
        deferred.save(update_fields=["base_salary"])
        row.refresh_from_db()
        self.assertEqual(row.total, Decimal("7000"))

    def test_update_fields_generator_does_not_become_an_empty_update(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000)
        row.month = "2026-12"
        row.save(update_fields=(field for field in ["month"]))
        row.refresh_from_db()
        self.assertEqual(row.month, "2026-12")

    def test_partial_save_on_fresh_instance_uses_existing_other_amounts(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000, bonus=1000)
        fresh = models.Salary(pk=row.pk, base_salary=Decimal("6000"))
        fresh.save(update_fields=["base_salary"])
        row.refresh_from_db()
        self.assertEqual(row.total, Decimal("7000"))

    def test_empty_update_fields_remains_a_noop(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=5000)
        row.base_salary = Decimal("6000")
        with CaptureQueriesContext(connection) as queries:
            row.save(update_fields=[])
        self.assertEqual(len(queries), 0)
        row.refresh_from_db()
        self.assertEqual(row.total, Decimal("5000"))

    def test_partial_salary_overflow_rolls_back(self):
        row = models.Salary.objects.create(employee=self.employee, month="2026-11", base_salary=Decimal("99999999.99"))
        row.bonus = Decimal("99999999.99")
        with self.assertRaises(ValidationError):
            row.save(update_fields=["bonus"])
        row.refresh_from_db()
        self.assertEqual(row.bonus, Decimal("0"))
        self.assertEqual(row.total, Decimal("99999999.99"))

    def test_audit_accepts_hire_month_and_reports_bad_year_without_crashing(self):
        row = models.Salary.objects.create(employee=self.recent, month="2026-11", base_salary=5000)
        result = audit_hrms_data()
        self.assertFalse(any(item["code"] == "business_records_before_hire" and item["id"] == self.recent.pk
                             for item in result["findings"]))
        models.Salary.objects.filter(pk=row.pk).update(month="0000-01")
        result = audit_hrms_data()
        self.assertTrue(any(item["code"] == "invalid_salary_month" and item["id"] == row.pk
                            for item in result["findings"]))

    def test_demo_passwords_remain_unchanged_as_requested(self):
        old_password = self.root.password
        call_command("init_data", stdout=StringIO())
        self.root.refresh_from_db()
        self.assertEqual(self.root.password, old_password)
        for name in ("admin", "张三", "王五"):
            self.assertTrue(models.User.objects.get(username=name).check_password("123456"))
