"""两个交互式注册入口的登录名/姓名去重；不变更历史账户。"""
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from app01 import models
from app01.utils.account_registration import save_registered_user
from app01.utils.form import UserModelForm


class RegistrationUniquenessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("init_data", stdout=StringIO())
        cls.admin = models.User.objects.get(username="admin")

    def payload(self, **changes):
        values = {
            "username": "new_login", "real_name": "新账号姓名", "phone": "", "email": "",
            "roles": [], "managed_department": "", "employee": "",
            "password": "NewLogin!Random2026", "confirm_password": "NewLogin!Random2026",
        }
        values.update(changes)
        return values

    def test_system_user_registration_rejects_case_insensitive_username_collision(self):
        form = UserModelForm(data=self.payload(username="ADMIN"), operator=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    def test_system_user_registration_rejects_same_name_with_different_login_name(self):
        form = UserModelForm(data=self.payload(real_name="张三"), operator=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("real_name", form.errors)

    def test_blank_legacy_name_uses_bound_employee_for_conflict_check(self):
        models.User.objects.filter(username="张三").update(real_name="")
        form = UserModelForm(data=self.payload(real_name="张三"), operator=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("real_name", form.errors)

    def test_missing_personal_name_falls_back_to_username(self):
        models.User.objects.create(username="old_independent_user")
        form = UserModelForm(data=self.payload(real_name="old_independent_user"), operator=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("real_name", form.errors)

    def test_name_whitespace_cannot_bypass_conflict(self):
        form = UserModelForm(data=self.payload(real_name=" 张三 "), operator=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("real_name", form.errors)

    def test_conflict_after_validation_is_checked_again_during_save(self):
        form = UserModelForm(data=self.payload(), operator=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        models.User.objects.create(username="concurrent_other_login", real_name="新账号姓名")
        with self.assertRaises(ValidationError):
            form.save()
        self.assertFalse(models.User.objects.filter(username="new_login").exists())

    def test_duplicate_submission_registers_one_account_only(self):
        self.client.force_login(self.admin)
        first = self.client.post("/admin/add/", self.payload())
        second = self.client.post("/admin/add/", self.payload())
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(models.User.objects.filter(username="new_login").count(), 1)

    def test_failed_registration_preserves_all_existing_account_fields(self):
        self.client.force_login(self.admin)
        before = list(models.User.objects.order_by("pk").values())
        response = self.client.post("/admin/add/", self.payload(real_name="张三"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(models.User.objects.order_by("pk").values()), before)

    def test_registration_relation_failure_rolls_back_account(self):
        user = models.User(username="atomic_registration", real_name="事务测试用户")
        user.set_password("NewLogin!Random2026")
        def fail():
            raise RuntimeError("simulated relationship failure")
        with self.assertRaises(RuntimeError):
            save_registered_user(user, fail)
        self.assertFalse(models.User.objects.filter(username="atomic_registration").exists())

    def test_django_admin_creation_cannot_bypass_registered_name_check(self):
        from app01.admin import HRMSAdminUserCreationForm
        payload = self.payload(real_name="张三")
        payload["password1"] = payload.pop("password")
        payload["password2"] = payload.pop("confirm_password")
        form = HRMSAdminUserCreationForm(data=payload, operator=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("real_name", form.errors)
