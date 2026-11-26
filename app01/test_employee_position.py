"""Employee.position 字段、表单和员工列表展示的独立回归测试。"""
from django.test import TestCase
from django.urls import reverse

from app01 import models
from app01.utils.form import EmployeeModelForm


class EmployeePositionTestCase(TestCase):
    """职位可新增、编辑并在列表展示；空职位使用短横线占位。"""

    @classmethod
    def setUpTestData(cls):
        cls.department = models.Department.objects.create(title="研发部")
        cls.admin_user = models.User.objects.create_superuser(
            username="position_admin",
            password="CourseDesign!2026_Position",
            real_name="职位测试管理员",
        )
        cls.positioned_employee = models.Employee.objects.create(
            name="已有职位员工",
            position="高级工程师",
            age=30,
            gender=1,
            account="1000.00",
            mobile="13830000001",
            department=cls.department,
        )
        cls.blank_position_employee = models.Employee.objects.create(
            name="空职位员工",
            age=28,
            gender=2,
            account="2000.00",
            mobile="13830000002",
            department=cls.department,
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    def _payload(self, *, mobile, position, name="职位测试员工"):
        return {
            "name": name,
            "position": position,
            "age": "29",
            "gender": "1",
            "account": "3000.00",
            "create_time": "2026-01-02",
            "mobile": mobile,
            "department": str(self.department.id),
        }

    def test_position_field_contract_and_default(self):
        field = models.Employee._meta.get_field("position")
        self.assertEqual(field.max_length, 64)
        self.assertTrue(field.blank)
        self.assertEqual(field.get_default(), "")
        self.assertEqual(self.blank_position_employee.position, "")

    def test_employee_form_exposes_position_and_enforces_max_length(self):
        form = EmployeeModelForm(user=self.admin_user)
        self.assertIn("position", form.fields)
        self.assertTrue(form.fields["position"].required)

        invalid_form = EmployeeModelForm(
            data=self._payload(mobile="13830000003", position="岗" * 65),
            user=self.admin_user,
        )
        self.assertFalse(invalid_form.is_valid())
        self.assertIn("position", invalid_form.errors)

    def test_form_rejects_blank_and_whitespace_positions(self):
        for position in ("", "   ", "\t\n"):
            with self.subTest(position=repr(position)):
                form = EmployeeModelForm(
                    data=self._payload(mobile="13830000003", position=position),
                    user=self.admin_user,
                )
                self.assertFalse(form.is_valid())
                self.assertIn("position", form.errors)
        self.assertFalse(models.Employee.objects.filter(mobile="13830000003").exists())

    def test_add_employee_without_position_does_not_write_a_record(self):
        payload = self._payload(mobile="13830000003", position="产品经理")
        payload.pop("position")
        response = self.client.post(reverse("user_add"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("position", response.context["form"].errors)
        self.assertFalse(models.Employee.objects.filter(mobile="13830000003").exists())

    def test_edit_cannot_clear_an_existing_position(self):
        before = self.positioned_employee.position
        response = self.client.post(
            reverse("user_edit", args=[self.positioned_employee.id]),
            self._payload(
                mobile=self.positioned_employee.mobile, position="   ",
                name=self.positioned_employee.name,
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("position", response.context["form"].errors)
        self.positioned_employee.refresh_from_db()
        self.assertEqual(self.positioned_employee.position, before)

    def test_add_employee_persists_position(self):
        response = self.client.post(
            reverse("user_add"),
            self._payload(mobile="13830000003", position="产品经理"),
        )
        self.assertEqual(response.status_code, 302)
        employee = models.Employee.objects.get(mobile="13830000003")
        self.assertEqual(employee.position, "产品经理")

    def test_edit_employee_updates_position(self):
        response = self.client.post(
            reverse("user_edit", args=[self.positioned_employee.id]),
            self._payload(
                mobile=self.positioned_employee.mobile,
                position="技术总监",
                name=self.positioned_employee.name,
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.positioned_employee.refresh_from_db()
        self.assertEqual(self.positioned_employee.position, "技术总监")

    def test_employee_list_shows_position_and_blank_placeholder(self):
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>职位</th>", html=True)
        self.assertContains(
            response,
            '<td class="employee-position">高级工程师</td>',
            html=True,
        )
        self.assertContains(
            response,
            '<td class="employee-position">-</td>',
            html=True,
        )

    def test_employee_list_empty_row_spans_all_columns(self):
        models.Employee.objects.all().delete()
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<td colspan="11" class="text-center text-muted">暂无数据</td>',
            html=True,
        )
