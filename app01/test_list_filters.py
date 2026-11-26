"""
新手阅读：检查自动筛选的模板接线、职位排序、组合条件、分页及账号可见范围。
模板断言与 JavaScript 测试互补；仅看到服务器返回 200 还不足以证明筛选行为正确。
"""
import datetime
import re
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs

from django.test import TestCase, override_settings
from django.utils import timezone

from app01 import models


class FilterFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = attrs.get("id")
            if self.current:
                self.forms[self.current] = {"attrs": attrs, "controls": {}}
        elif self.current and tag in ("input", "select", "button") and attrs.get("name"):
            self.forms[self.current]["controls"][attrs["name"]] = attrs

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ListFilterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tech = models.Department.objects.create(title="筛选技术部")
        cls.market = models.Department.objects.create(title="筛选市场部")
        cls.root = models.User.objects.create_superuser(username="filter_admin", password="FilterAdmin!2026")
        cls.ordinary = cls.employee("王员工", "软件工程师", 1)
        cls.assistant = cls.employee("王助理", "总经理助理", 2)
        cls.supervisor = cls.employee("王主管", "研发主管", 3)
        cls.manager = cls.employee("张经理", "技术部经理", 4)
        cls.director = cls.employee("李总监", "技术总监", 5)
        cls.executive = cls.employee("李总经理", "总经理", 6)
        cls.outside = cls.employee("赵经理", "薪酬经理", 7, cls.market)
        cls.senior_engineer = cls.employee("王工程师", "高级工程师", 8)
        cls.blank = cls.employee("空职位", "", 9)
        cls.escaped = cls.employee("转义员工", '<script>alert("job")</script>', 10)
        permissions = [models.Permission.objects.create(code=code, name=code, module=code.split(".")[0])
                       for code in ("employee.view", "attendance.view", "salary.view")]
        cls.role = models.Role.objects.create(name="筛选查看员")
        cls.role.permissions.set(permissions)
        cls.dept_user = models.User.objects.create_user(username="filter_manager", employee=cls.manager,
                                                        managed_department=cls.tech)
        cls.dept_user.roles.add(cls.role)
        cls.staff = models.User.objects.create_user(username="filter_staff", employee=cls.ordinary)
        cls.staff.roles.add(cls.role)
        for day in (5, 6):
            models.Attendance.objects.create(employee=cls.ordinary, date=datetime.date(2026, 11, day),
                                              check_in=datetime.time(9), check_out=datetime.time(18))
        for month in ("2026-10", "2026-11"):
            models.Salary.objects.create(employee=cls.ordinary, month=month, base_salary=5000)

    @classmethod
    def employee(cls, name, position, index, department=None):
        return models.Employee.objects.create(name=name, position=position, age=30, department=department or cls.tech,
            mobile="1389100{:04d}".format(index), create_time=timezone.make_aware(datetime.datetime(2024, 1, 1, 9)))

    def setUp(self):
        self.client.force_login(self.root)

    def form_controls(self, response, form_id):
        parser = FilterFormParser()
        parser.feed(response.content.decode())
        form = parser.forms[form_id]
        self.assertEqual(form["attrs"]["method"], "get")
        self.assertNotIn("page", form["controls"])
        self.assertContains(response, 'js/list-filters.js')
        return form["controls"]

    def test_date_filter_is_wired_for_auto_submit_and_preserves_name(self):
        response = self.client.get("/attendance/list/", {"date": "2026-11-06", "q": "王"})
        controls = self.form_controls(response, "attendance-filter-form")
        self.assertIn("data-auto-submit", controls["date"])
        self.assertEqual(controls["date"]["value"], "2026-11-06")
        self.assertEqual(controls["q"]["value"], "王")
        self.assertEqual(response.context["total_count"], 1)

    def test_month_filter_is_wired_for_auto_submit_and_recalculates_total(self):
        response = self.client.get("/salary/list/", {"month": "2026-11", "q": "王"})
        controls = self.form_controls(response, "salary-filter-form")
        self.assertIn("data-auto-submit", controls["month"])
        self.assertEqual(controls["q"]["value"], "王")
        self.assertEqual(response.context["month"], "2026-11")
        self.assertEqual(response.context["total_count"], 1)
        self.assertEqual(response.context["total_salary"], Decimal("5000"))

    def test_clearing_date_or_month_returns_all_authorized_records(self):
        self.assertEqual(self.client.get("/attendance/list/", {"date": "", "q": "王"}).context["total_count"], 2)
        self.assertEqual(self.client.get("/salary/list/", {"month": "", "q": "王"}).context["total_count"], 2)

    def test_employee_controls_auto_submit_department_and_position(self):
        response = self.client.get("/user/list/")
        controls = self.form_controls(response, "employee-filter-form")
        self.assertIn("data-auto-submit", controls["department_id"])
        self.assertIn("data-auto-submit", controls["position"])
        self.assertNotIn("data-auto-submit", controls["q"])
        self.assertContains(response, "全部职位")

    def test_default_order_is_leadership_first_not_creation_order(self):
        response = self.client.get("/user/list/")
        self.assertEqual(list(response.context["queryset"].values_list("pk", flat=True)),
                         [self.executive.pk, self.director.pk, self.manager.pk, self.outside.pk, self.supervisor.pk])

    def test_same_priority_keeps_stable_order_across_pages(self):
        first = list(self.client.get("/user/list/", {"page": 1}).context["queryset"].values_list("pk", flat=True))
        second = list(self.client.get("/user/list/", {"page": 2}).context["queryset"].values_list("pk", flat=True))
        self.assertEqual(second, [self.ordinary.pk, self.assistant.pk, self.senior_engineer.pk, self.blank.pk, self.escaped.pk])
        self.assertEqual(len(set(first + second)), 10)

    def test_senior_technical_and_assistant_positions_are_not_misclassified_as_leaders(self):
        for employee in (self.assistant, self.senior_engineer, self.ordinary):
            row = self.client.get("/user/list/", {"q": employee.name}).context["queryset"].get()
            self.assertEqual(row._leadership_priority, 100)

    def test_english_director_does_not_match_cto_abbreviation(self):
        employee = self.employee("英文总监", "Engineering Director", 11)
        row = self.client.get("/user/list/", {"q": employee.name}).context["queryset"].get()
        self.assertEqual(row._leadership_priority, 10)

    def test_position_filter_uses_exact_title(self):
        response = self.client.get("/user/list/", {"position": "技术部经理"})
        self.assertEqual(list(response.context["queryset"].values_list("pk", flat=True)), [self.manager.pk])
        self.assertContains(response, '<option value="技术部经理" selected>技术部经理</option>', html=True)
        self.assertEqual(self.client.get("/user/list/", {"position": "经理"}).context["total_count"], 0)

    def test_name_department_and_position_filters_are_combined(self):
        response = self.client.get("/user/list/", {"position": "技术部经理", "q": "张", "department_id": self.tech.pk})
        self.assertEqual(list(response.context["queryset"].values_list("pk", flat=True)), [self.manager.pk])
        self.assertEqual(self.client.get("/user/list/", {"position": "技术部经理", "department_id": self.market.pk}).context["total_count"], 0)

    def test_position_choices_are_unique_and_not_restricted_to_current_page(self):
        self.employee("王同岗", "软件工程师", 11)
        response = self.client.get("/user/list/", {"q": "绝不存在"})
        positions = list(response.context["positions"])
        self.assertIn("软件工程师", positions)
        self.assertIn("薪酬经理", positions)
        self.assertEqual(positions.count("软件工程师"), 1)
        self.assertNotIn("", positions)

    def test_position_options_and_results_respect_department_scope(self):
        self.client.force_login(self.dept_user)
        response = self.client.get("/user/list/")
        self.assertNotIn("薪酬经理", list(response.context["positions"]))
        self.assertEqual(self.client.get("/user/list/", {"position": "薪酬经理"}).context["total_count"], 0)

    def test_regular_employee_only_sees_own_position(self):
        self.client.force_login(self.staff)
        response = self.client.get("/user/list/")
        self.assertEqual(list(response.context["positions"]), ["软件工程师"])
        self.assertEqual(list(response.context["queryset"].values_list("pk", flat=True)), [self.ordinary.pk])

    def test_unknown_position_returns_empty_result_without_losing_controls(self):
        response = self.client.get("/user/list/", {"position": "不存在的职位"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 0)
        self.assertTrue(list(response.context["positions"]))

    def test_position_options_are_html_escaped(self):
        response = self.client.get("/user/list/")
        self.assertContains(response, '&lt;script&gt;alert(&quot;job&quot;)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert("job")</script>')

    def test_pagination_preserves_name_department_and_position(self):
        for index in range(11, 18):
            self.employee("王同岗" + str(index), "软件工程师", index)
        response = self.client.get("/user/list/", {"position": "软件工程师", "q": "王", "department_id": self.tech.pk})
        urls = re.findall(r'href="\?([^"#]+)"', response.context["page_string"])
        self.assertTrue(urls)
        for url in urls:
            params = parse_qs(url)
            self.assertEqual(params["position"], ["软件工程师"])
            self.assertEqual(params["q"], ["王"])
            self.assertEqual(params["department_id"], [str(self.tech.pk)])
