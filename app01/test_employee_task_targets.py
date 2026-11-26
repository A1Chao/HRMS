"""Employee-sourced task choices and renamed-account seed compatibility."""
from io import StringIO

from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase

from app01 import models
from app01.admin import TaskAdmin
from app01.utils.employee_accounts import sync_employee_accounts
from app01.utils.form import TaskModelForm


class EmployeeTaskTargetsTestCase(TestCase):
    def setUp(self):
        self.department = models.Department.objects.create(title="技术部")
        self.operator = models.User.objects.create_superuser(
            username="target_admin", password="StrongPass!2026", real_name="管理员",
        )
        self.employee = models.Employee.objects.create(
            name="任务员工", position="研发工程师", age=28, gender=1,
            mobile="13830000001", department=self.department,
        )
        self.recipient = models.User.objects.create_user(
            username="old_account", password="StrongPass!2026", real_name="旧账号备注",
            employee=self.employee,
        )
        self.client.force_login(self.operator)

    def test_options_use_employee_name_and_department_not_account_remark(self):
        form = TaskModelForm(operator=self.operator)
        field = form.fields["assignee"]
        self.assertEqual(list(field.queryset.values_list("id", flat=True)), [self.recipient.id])
        self.assertEqual(field.label_from_instance(self.recipient), "任务员工（技术部）")
        page = self.client.get("/task/list/")
        self.assertContains(page, "任务员工（技术部）")
        self.assertNotContains(page, ">旧账号备注</option>")

    def test_inactive_accounts_and_unbound_administrators_cannot_be_new_targets(self):
        response = self.client.post("/task/ajax/add/", {
            "level": 2, "title": "非法账号目标", "detail": "未绑定员工不能出现在选人名单",
            "target_type": "user", "assignee": self.operator.id,
        })
        self.assertFalse(response.json()["status"])
        self.recipient.is_active = False
        self.recipient.save(update_fields=["is_active"])
        self.assertFalse(TaskModelForm(operator=self.operator).fields["assignee"].queryset.exists())

    def test_ajax_target_display_is_employee_identity(self):
        response = self.client.post("/task/ajax/add/", {
            "level": 2, "title": "员工派发验证", "detail": "后台使用账号映射但展示真实员工",
            "target_type": "user", "assignee": self.recipient.id,
        })
        result = response.json()
        self.assertTrue(result["status"], result)
        self.assertEqual(result["data"]["dispatch_target"], "任务员工（技术部）")
        task = models.Task.objects.get(id=result["data"]["id"])
        self.assertEqual(task.assignee_id, self.recipient.id)

    def test_legacy_unbound_account_task_is_preserved_and_visible(self):
        task = models.Task.objects.create(
            title="历史管理任务", detail="不改旧派发对象或任务内容", assignee=self.operator,
        )
        self.assertEqual(task.dispatch_target_display, "管理员")
        self.assertContains(self.client.get("/task/list/"), "历史管理任务")
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.operator.id)

    def test_choice_labels_preload_employee_and_department(self):
        form = TaskModelForm(operator=self.operator)
        field = form.fields["assignee"]
        with self.assertNumQueries(1):
            labels = [field.label_from_instance(user) for user in field.queryset]
        self.assertEqual(labels, ["任务员工（技术部）"])

    def test_django_admin_uses_same_employee_choices(self):
        request = RequestFactory().get("/admin/")
        request.user = self.operator
        admin = TaskAdmin(models.Task, AdminSite())
        field = admin.formfield_for_foreignkey(models.Task._meta.get_field("assignee"), request)
        self.assertEqual(list(field.queryset.values_list("id", flat=True)), [self.recipient.id])
        self.assertEqual(field.label_from_instance(self.recipient), "任务员工（技术部）")


class EmployeeAccountSeedCompatibilityTestCase(TestCase):
    def test_seed_reuses_name_accounts_after_sync_and_keeps_existing_tasks(self):
        call_command("init_data", stdout=StringIO())
        manager = models.User.objects.get(employee__name="张三")
        staff = models.User.objects.get(employee__name="王五")
        old_user_ids = (manager.id, staff.id)
        old_passwords = (manager.password, staff.password)
        admin = models.User.objects.get(username="admin")
        historical = models.Task.objects.get(title="整理员工档案")
        historical.assignee = admin
        historical.save(update_fields=["assignee"])
        receipt = models.TaskRead.objects.create(task=historical, user=admin)
        old_tasks = list(models.Task.objects.order_by("id").values("id", "title", "detail", "assignee_id"))
        models.Employee.objects.filter(name="张三").update(position="研发负责人")
        sync_employee_accounts(apply=True, initial_password="StrongPass!2026")
        call_command("init_data", stdout=StringIO())
        manager.refresh_from_db()
        staff.refresh_from_db()
        self.assertEqual((manager.id, staff.id), old_user_ids)
        self.assertEqual((manager.password, staff.password), old_passwords)
        self.assertEqual((manager.username, staff.username), ("张三", "王五"))
        self.assertEqual(models.User.objects.count(), 9)
        self.assertFalse(models.User.objects.filter(username__in=["manager", "staff"]).exists())
        self.assertEqual(list(models.Task.objects.order_by("id").values("id", "title", "detail", "assignee_id")), old_tasks)
        self.assertTrue(models.TaskRead.objects.filter(id=receipt.id).exists())
        self.assertEqual(models.Employee.objects.get(name="张三").position, "研发负责人")

    def test_seed_does_not_take_over_unbound_name_collision(self):
        outsider = models.User.objects.create_user(username="张三", password="StrongPass!2026")
        with self.assertRaises(CommandError):
            call_command("init_data", stdout=StringIO())
        outsider.refresh_from_db()
        self.assertIsNone(outsider.employee_id)
        self.assertEqual(models.User.objects.count(), 1)
