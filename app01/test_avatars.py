"""Database-backed avatar contracts; run only in Django's isolated test database.

Fixture images are generated in BytesIO.  Legacy-file fixtures, when needed, use
an isolated temporary MEDIA_ROOT; avatar writes must never use file storage.
"""
import hashlib
import io
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from PIL import Image, PngImagePlugin, features
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import DatabaseError, models as db_models
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from app01 import models
from app01.utils import avatars
from app01.utils.form import EmployeeModelForm


TEST_PASSWORD = "AvatarRegression!2026_Q9"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_bytes(image_format="PNG", size=(32, 24), color=(23, 45, 67), mode="RGB", **options):
    output = io.BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format=image_format, **options)
    return output.getvalue()


def upload(data=None, name="portrait.png", content_type="image/png"):
    return SimpleUploadedFile(name, image_bytes() if data is None else data, content_type=content_type)


def records(model):
    return list(model.objects.order_by("pk").values())


class AvatarFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.tech = models.Department.objects.create(title="头像技术部")
        cls.market = models.Department.objects.create(title="头像市场部")
        cls.view_permission = models.Permission.objects.create(
            module="employee", name="查看员工", code="employee.view",
        )
        cls.edit_permission = models.Permission.objects.create(
            module="employee", name="维护员工", code="employee.edit",
        )
        cls.reader_role = models.Role.objects.create(name="头像只读角色")
        cls.reader_role.permissions.add(cls.view_permission)
        cls.editor_role = models.Role.objects.create(name="头像编辑角色")
        cls.editor_role.permissions.add(cls.edit_permission)
        cls.manager_role = models.Role.objects.create(name="头像管理角色")
        cls.manager_role.permissions.add(cls.view_permission, cls.edit_permission)
        cls.empty_manager_role = models.Role.objects.create(name="部门管理员")
        cls.no_employee = models.User.objects.create_user(
            username="avatar_self", password=TEST_PASSWORD, real_name="自助头像用户",
            phone="13984000000", email="self@example.test",
        )
        password_hash = cls.no_employee.password

        def make_user(username, *, department=None, managed_department=None, role=None,
                      is_active=True, is_superuser=False, is_staff=False):
            index = models.User.objects.count()
            employee = None
            if department is not None:
                employee = models.Employee.objects.create(
                    name="头像员工{}".format(index), position="测试工程师", age=30, gender=1,
                    mobile="13984{:06d}".format(index), account=Decimal("1500.00"),
                    department=department,
                )
            user = models.User.objects.create(
                username=username, password=password_hash,
                real_name=employee.name if employee else username,
                phone=employee.mobile if employee else "", email=username + "@example.test",
                employee=employee, managed_department=managed_department,
                is_active=is_active, is_superuser=is_superuser, is_staff=is_staff,
            )
            if role is not None:
                user.roles.add(role)
            return user

        cls.admin = make_user("avatar_admin", is_superuser=True, is_staff=True)
        cls.reader = make_user("avatar_reader", department=cls.market,
                               managed_department=cls.tech, role=cls.reader_role)
        cls.editor = make_user("avatar_editor", department=cls.market,
                               managed_department=cls.tech, role=cls.editor_role)
        cls.manager = make_user("avatar_manager", department=cls.market,
                                managed_department=cls.tech, role=cls.manager_role)
        cls.tech_user = make_user("avatar_tech", department=cls.tech)
        cls.market_user = make_user("avatar_market", department=cls.market)
        cls.membership_target = make_user("avatar_member", department=cls.tech,
                                          managed_department=cls.market)
        cls.scope_only_target = make_user("avatar_scope_only", department=cls.market,
                                          managed_department=cls.tech)
        cls.empty_manager = make_user("avatar_empty_manager", managed_department=cls.tech,
                                      role=cls.empty_manager_role)
        cls.missing_scope = make_user("avatar_missing_scope", department=cls.tech,
                                      role=cls.manager_role)
        cls.inactive = make_user("avatar_inactive", managed_department=cls.tech,
                                 role=cls.manager_role, is_active=False)

    def scope_ids(self, actor, manage=False):
        return set(avatars.avatar_users_for(actor, manage=manage).values_list("id", flat=True))

    def business_snapshot(self):
        model_list = (
            models.User, models.Employee, models.Department, models.Role, models.Permission,
            models.User.roles.through, models.Role.permissions.through,
            models.User.groups.through, models.User.user_permissions.through,
            models.Attendance, models.Salary, models.Notice, models.NoticeRead,
            models.Task, models.TaskRead,
        )
        return {model._meta.label_lower: records(model) for model in model_list}

    def assert_private_png(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";", 1)[0], "image/png")
        self.assertIn("private", response["Cache-Control"].lower())
        self.assertIn("no-store", response["Cache-Control"].lower())
        self.assertEqual(response["X-Content-Type-Options"].lower(), "nosniff")
        self.assertTrue(response.content.startswith(PNG_SIGNATURE))
        with Image.open(io.BytesIO(response.content)) as image:
            image.load()
            self.assertEqual(image.format, "PNG")


class AvatarScopeTest(AvatarFixtureMixin, TestCase):
    def test_anonymous_missing_and_inactive_actors_have_no_scope(self):
        for actor in (None, AnonymousUser(), self.inactive):
            with self.subTest(actor=actor):
                self.assertEqual(self.scope_ids(actor), set())
                self.assertEqual(self.scope_ids(actor, manage=True), set())

    def test_active_superuser_can_view_and_manage_every_account(self):
        all_ids = set(models.User.objects.values_list("id", flat=True))
        self.assertEqual(self.scope_ids(self.admin), all_ids)
        self.assertEqual(self.scope_ids(self.admin, manage=True), all_ids)
        self.assertIn(self.inactive.id, self.scope_ids(self.admin))

    def test_self_service_does_not_require_an_employee_or_any_role(self):
        self.assertIsNone(self.no_employee.employee_id)
        self.assertFalse(self.no_employee.roles.exists())
        self.assertEqual(self.scope_ids(self.no_employee), {self.no_employee.id})
        self.assertEqual(self.scope_ids(self.no_employee, manage=True), {self.no_employee.id})

    def test_actual_view_and_edit_permissions_grant_only_the_corresponding_scope(self):
        tech_ids = set(models.User.objects.filter(employee__department=self.tech).values_list("id", flat=True))
        self.assertEqual(self.scope_ids(self.reader), tech_ids | {self.reader.id})
        self.assertEqual(self.scope_ids(self.reader, manage=True), {self.reader.id})
        self.assertEqual(self.scope_ids(self.editor), {self.editor.id})
        self.assertEqual(self.scope_ids(self.editor, manage=True), tech_ids | {self.editor.id})
        self.assertEqual(self.scope_ids(self.manager), tech_ids | {self.manager.id})
        self.assertEqual(self.scope_ids(self.manager, manage=True), tech_ids | {self.manager.id})

    def test_target_membership_is_not_inferred_from_its_managed_department(self):
        for manage in (False, True):
            with self.subTest(manage=manage):
                allowed = self.scope_ids(self.manager, manage=manage)
                self.assertIn(self.membership_target.id, allowed)
                self.assertNotIn(self.scope_only_target.id, allowed)
                self.assertNotIn(self.market_user.id, allowed)

    def test_missing_scope_and_a_manager_role_without_real_permissions_are_self_only(self):
        for actor in (self.empty_manager, self.missing_scope):
            with self.subTest(actor=actor.username):
                self.assertEqual(self.scope_ids(actor), {actor.id})
                self.assertEqual(self.scope_ids(actor, manage=True), {actor.id})

    def test_existing_department_scope_without_any_employee_permission_is_self_only(self):
        self.assertEqual(self.scope_ids(self.scope_only_target), {self.scope_only_target.id})
        self.assertEqual(self.scope_ids(self.scope_only_target, manage=True), {self.scope_only_target.id})


class AvatarNormalizationTest(SimpleTestCase):
    def test_image_is_normalized_to_bounded_metadata_free_png(self):
        source = image_bytes("JPEG", size=(1024, 600), color=(90, 120, 150), quality=95)
        normalized = avatars.normalize_avatar(upload(source, name="portrait.jpg", content_type="image/jpeg"))

        self.assertEqual(normalized.mime_type, "image/png")
        self.assertEqual(normalized.size, len(normalized.data))
        self.assertEqual(normalized.sha256, hashlib.sha256(normalized.data).hexdigest())
        self.assertEqual((normalized.width, normalized.height), (512, 300))
        self.assertTrue(normalized.data.startswith(PNG_SIGNATURE))

    def test_invalid_or_unsupported_content_is_rejected(self):
        for item in (
            upload(b"not an image", name="fake.png"),
            upload(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", name="avatar.svg", content_type="image/svg+xml"),
        ):
            with self.subTest(name=item.name), self.assertRaises(ValidationError):
                avatars.normalize_avatar(item)


class AvatarWorkflowTest(AvatarFixtureMixin, TestCase):
    def test_user_can_upload_read_and_delete_own_database_avatar(self):
        self.client.force_login(self.no_employee)
        before = self.business_snapshot()

        response = self.client.post(reverse("my_avatar"), {"avatar": upload()})
        self.assertRedirects(response, reverse("my_avatar"), fetch_redirect_response=False)

        stored = models.UserAvatar.objects.get(user=self.no_employee)
        self.assertEqual(stored.updated_by_id, self.no_employee.id)
        self.assertEqual(stored.mime_type, "image/png")
        self.assertEqual(stored.size, len(bytes(stored.data)))
        self.assertEqual(self.business_snapshot(), before)

        response = self.client.get(reverse("avatar_image", args=[self.no_employee.id]))
        self.assert_private_png(response)
        self.assertEqual(response.content, bytes(stored.data))

        response = self.client.post(reverse("avatar_delete", args=[self.no_employee.id]))
        self.assertRedirects(response, reverse("my_avatar"), fetch_redirect_response=False)
        self.assertFalse(models.UserAvatar.objects.filter(user=self.no_employee).exists())
        self.assertEqual(self.business_snapshot(), before)

    def test_department_manager_can_only_manage_accounts_in_own_scope(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("avatar_edit", args=[self.tech_user.id]),
            {"avatar": upload(image_bytes(color=(140, 40, 70)))},
        )
        self.assertRedirects(
            response,
            reverse("avatar_edit", args=[self.tech_user.id]),
            fetch_redirect_response=False,
        )
        self.assertTrue(models.UserAvatar.objects.filter(user=self.tech_user, updated_by=self.manager).exists())

        response = self.client.post(
            reverse("avatar_edit", args=[self.market_user.id]),
            {"avatar": upload(image_bytes(color=(20, 140, 70)))},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(models.UserAvatar.objects.filter(user=self.market_user).exists())

    def test_view_only_permission_cannot_modify_avatar(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("avatar_image", args=[self.tech_user.id]))
        self.assert_private_png(response)

        response = self.client.post(
            reverse("avatar_edit", args=[self.tech_user.id]),
            {"avatar": upload()},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(models.UserAvatar.objects.filter(user=self.tech_user).exists())

    def test_edit_only_permission_can_preview_the_avatar_it_manages(self):
        avatars.save_avatar(self.editor, self.tech_user.id, avatars.normalize_avatar(upload()))
        self.client.force_login(self.editor)

        response = self.client.get(reverse("avatar_image", args=[self.tech_user.id]))
        self.assert_private_png(response)

    def test_employee_form_no_longer_exposes_filesystem_avatar_upload(self):
        form = EmployeeModelForm(user=self.admin)
        self.assertNotIn("avatar", form.fields)
