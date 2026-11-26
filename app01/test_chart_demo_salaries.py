"""统计完整性与演示薪资补齐回归；所有写入都在 Django 隔离测试库里。"""
import datetime
import json
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from app01 import models
from app01.utils.demo_salaries import fill_demo_salaries
from app01.views.chart import chart_depart_data, chart_salary_data


class ChartDemoSalaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("init_data", stdout=StringIO())
        cls.admin = models.User.objects.get(username="admin")
        cls.managers = []
        for index, (name, title) in enumerate((
            ("熊大", "技术部"), ("熊二", "市场部"), ("光头强", "人力资源部"),
        )):
            department = models.Department.objects.get(title=title)
            employee = models.Employee.objects.create(
                name=name, age=30, gender=1, mobile="1990000000" + str(index),
                department=department, position=title + "经理", is_demo=True,
                create_time=timezone.make_aware(datetime.datetime(2024, 1, 1, 9)),
            )
            user = models.User.objects.create(
                username=name, employee=employee, managed_department=department,
                password=cls.admin.password,
            )
            cls.managers.append(user)

    def chart_response(self, user):
        request = RequestFactory().get("/chart/depart/data/")
        request.user = user
        return chart_depart_data(request)

    def test_all_eleven_employees_include_managers_and_employees_without_accounts(self):
        payload = json.loads(self.chart_response(self.admin).content)
        self.assertEqual(payload["total"], 11)
        self.assertEqual({row["name"]: row["value"] for row in payload["data"]},
                         {"技术部": 4, "市场部": 4, "人力资源部": 3})
        self.assertIn("熊大", payload["members"]["技术部"])
        self.assertIn("熊二", payload["members"]["市场部"])
        self.assertIn("光头强", payload["members"]["人力资源部"])
        self.assertIn("李四", payload["members"]["技术部"])
        self.assertEqual(sum(map(len, payload["members"].values())), payload["total"])

    def test_manager_cannot_see_other_department_member_names(self):
        payload = json.loads(self.chart_response(self.managers[0]).content)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(set(payload["members"]), {"技术部"})
        self.assertNotIn("熊二", str(payload))
        self.assertNotIn("光头强", str(payload))

    def test_chart_has_no_cache_headers_and_bounded_queries(self):
        with self.assertNumQueries(2):
            response = self.chart_response(self.admin)
        self.assertIn("no-store", response["Cache-Control"])

    def test_salary_chart_totals_include_all_eleven_after_repair(self):
        fill_demo_salaries(apply=True, allow_demo=True)
        request = RequestFactory().get("/chart/salary/data/")
        request.user = self.admin
        with self.assertNumQueries(1):
            response = chart_salary_data(request)
        payload = json.loads(response.content)
        self.assertEqual(payload["data"]["months"], ["2025-01", "2025-02", "2025-03"])
        self.assertEqual(payload["data"]["totals"], [93500.0, 95700.0, 97900.0])
        self.assertIn("no-store", response["Cache-Control"])

    def test_preview_identifies_only_nine_missing_records_and_does_not_write(self):
        before = list(models.Salary.objects.order_by("pk").values())
        plan = fill_demo_salaries()
        self.assertFalse(plan["applied"])
        self.assertEqual(plan["created"], 9)
        self.assertEqual(plan["skipped_existing"], 24)
        self.assertEqual({row["name"] for row in plan["rows"]}, {"熊大", "熊二", "光头强"})
        self.assertEqual(list(models.Salary.objects.order_by("pk").values()), before)

    def test_apply_creates_three_months_each_and_preserves_existing_salary_and_passwords(self):
        salaries = list(models.Salary.objects.order_by("pk").values())
        accounts = list(models.User.objects.order_by("pk").values())
        plan = fill_demo_salaries(apply=True, allow_demo=True)
        self.assertEqual(plan["created"], 9)
        self.assertEqual(models.Salary.objects.count(), 33)
        for user in self.managers:
            self.assertEqual(list(models.Salary.objects.filter(employee=user.employee)
                                  .order_by("month").values_list("total", flat=True)),
                             [8500, 8700, 8900])
        self.assertEqual(list(models.Salary.objects.filter(pk__in=[row["id"] for row in salaries])
                              .order_by("pk").values()), salaries)
        self.assertEqual(list(models.User.objects.order_by("pk").values()), accounts)

    def test_repeated_apply_is_idempotent(self):
        fill_demo_salaries(apply=True, allow_demo=True)
        before = list(models.Salary.objects.order_by("pk").values())
        result = fill_demo_salaries(apply=True, allow_demo=True)
        self.assertEqual(result["created"], 0)
        self.assertEqual(list(models.Salary.objects.order_by("pk").values()), before)

    def test_existing_custom_salary_is_not_reset(self):
        salary = models.Salary.objects.create(
            employee=self.managers[0].employee, month="2025-01", base_salary=12345,
            bonus=678, deduction=9,
        )
        before = models.Salary.objects.values().get(pk=salary.pk)
        fill_demo_salaries(apply=True, allow_demo=True)
        self.assertEqual(models.Salary.objects.values().get(pk=salary.pk), before)

    def test_real_employee_is_not_given_fake_salary(self):
        employee = self.managers[0].employee
        employee.is_demo = False
        employee.save(update_fields=["is_demo"])
        result = fill_demo_salaries(apply=True, allow_demo=True)
        self.assertEqual(result["created"], 6)
        self.assertFalse(models.Salary.objects.filter(employee=employee).exists())
        with self.assertRaises(ValidationError):
            fill_demo_salaries(apply=True, allow_demo=True, employee_ids=[employee.id])

    def test_salary_before_hire_month_is_skipped(self):
        employee = self.managers[0].employee
        employee.create_time = timezone.make_aware(datetime.datetime(2025, 2, 20, 9))
        employee.save(update_fields=["create_time"])
        result = fill_demo_salaries(apply=True, allow_demo=True, employee_ids=[employee.id])
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["skipped_before_hire"], 1)
        self.assertFalse(models.Salary.objects.filter(employee=employee, month="2025-01").exists())

    def test_unknown_employee_rejects_whole_batch(self):
        before = models.Salary.objects.count()
        with self.assertRaises(ValidationError):
            fill_demo_salaries(apply=True, allow_demo=True, employee_ids=[self.managers[0].employee_id, 999999])
        self.assertEqual(models.Salary.objects.count(), before)

    def test_authorization_flags_must_be_paired(self):
        for options in ({"apply": True}, {"allow_demo": True}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                fill_demo_salaries(**options)
        self.assertEqual(models.Salary.objects.count(), 24)

    def test_write_error_rolls_back_entire_batch(self):
        original_save = models.Salary.save
        written = []
        def fail_second(record, *args, **kwargs):
            written.append(record)
            if len(written) == 2:
                raise RuntimeError("simulated write failure")
            return original_save(record, *args, **kwargs)
        with mock.patch.object(models.Salary, "save", fail_second):
            with self.assertRaises(RuntimeError):
                fill_demo_salaries(apply=True, allow_demo=True)
        self.assertEqual(models.Salary.objects.count(), 24)

    def test_command_defaults_to_read_only_preview(self):
        output = StringIO()
        call_command("fill_demo_salaries", "--json", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["created"], 9)
        self.assertEqual(models.Salary.objects.count(), 24)
