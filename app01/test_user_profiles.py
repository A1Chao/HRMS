"""用户资料只从确定的员工关联补全，且不改变账号身份、权限或凭据。"""
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import RequestFactory, TestCase

from app01 import models
from app01.utils.employee_accounts import REQUIRED_READ_ONLY_PERMISSIONS, sync_employee_accounts
from app01.utils.form import UserEditModelForm, UserModelForm
from app01.utils.user_profiles import employee_profile_defaults, repair_user_profiles
from app01.views.admin import admin_list


TEST_PASSWORD = "ProfileRegression!2026_Q9"


class UserProfileFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.department = models.Department.objects.create(title="资料研发部")
        cls.other_department = models.Department.objects.create(title="资料市场部")
        cls.employee = models.Employee.objects.create(
            name="资料员工甲", age=28, gender=1,
            mobile="13840000001", department=cls.department,
        )
        cls.other_employee = models.Employee.objects.create(
            name="资料员工乙", age=29, gender=2,
            mobile="13840000002", department=cls.other_department,
        )
        cls.regular_role = models.Role.objects.create(name="普通员工")
        permissions = [
            models.Permission.objects.create(
                code=code, module=code.split(".", 1)[0], name=code,
            )
            for code in sorted(REQUIRED_READ_ONLY_PERMISSIONS)
        ]
        cls.regular_role.permissions.set(permissions)

    def create_bound_user(self, **overrides):
        values = {
            "username": "profile_legacy", "password": TEST_PASSWORD,
            "real_name": "", "phone": "", "employee": self.employee,
        }
        values.update(overrides)
        user = models.User.objects.create_user(**values)
        user.roles.set([self.regular_role])
        return user


class EmployeeProfileDefaultsTest(UserProfileFixtureMixin, TestCase):
    def test_defaults_are_source_backed_and_do_not_mutate_objects(self):
        original = (self.employee.name, self.employee.mobile)
        with self.assertNumQueries(0):
            values = employee_profile_defaults(self.employee)
        self.assertEqual(values, {
            "real_name": self.employee.name, "phone": self.employee.mobile,
        })
        self.assertEqual((self.employee.name, self.employee.mobile), original)

    def test_explicit_fields_are_preserved_independently(self):
        self.assertEqual(employee_profile_defaults(
            self.employee, real_name="账号独立备注", phone="13940000001",
        ), {})
        self.assertEqual(employee_profile_defaults(
            self.employee, real_name="账号独立备注",
        ), {"phone": self.employee.mobile})
        self.assertEqual(employee_profile_defaults(
            self.employee, phone="13940000001",
        ), {"real_name": self.employee.name})

    def test_whitespace_is_blank_but_missing_sources_are_not_invented(self):
        self.assertEqual(employee_profile_defaults(
            self.employee, real_name="  ", phone="\t",
        ), {"real_name": self.employee.name, "phone": self.employee.mobile})
        self.assertEqual(employee_profile_defaults(None), {})
        self.assertEqual(employee_profile_defaults(models.Employee(name="", mobile="")), {})

    def test_profile_properties_fall_back_without_persisting(self):
        user = self.create_bound_user()
        self.assertEqual(user.profile_name, self.employee.name)
        self.assertEqual(user.profile_phone, self.employee.mobile)
        self.assertEqual(user.display_name, self.employee.name)
        self.assertEqual((user.real_name, user.phone), ("", ""))
        user.refresh_from_db()
        self.assertEqual((user.real_name, user.phone), ("", ""))

    def test_explicit_profile_properties_take_priority_and_unbound_name_stays_unknown(self):
        user = models.User(
            username="independent_login", real_name="账号备注", phone="13940000001",
            employee=self.employee,
        )
        self.assertEqual((user.profile_name, user.profile_phone), ("账号备注", "13940000001"))
        unbound = models.User(username="login_is_not_a_real_name")
        self.assertEqual((unbound.profile_name, unbound.profile_phone), ("", ""))
        self.assertEqual(unbound.display_name, unbound.username)


class UserProfileFormTest(UserProfileFixtureMixin, TestCase):
    def payload(self, **overrides):
        values = {
            "username": "profile_new", "real_name": "", "phone": "", "email": "",
            "roles": [self.regular_role.id], "employee": str(self.employee.id),
            "managed_department": "", "password": TEST_PASSWORD,
            "confirm_password": TEST_PASSWORD, "is_active": "on",
        }
        values.update(overrides)
        return values

    def test_create_form_fills_missing_fields_before_model_validation(self):
        form = UserModelForm(data=self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual((user.real_name, user.phone), (self.employee.name, self.employee.mobile))
        self.assertEqual(user.employee_id, self.employee.id)
        self.assertIsNone(user.managed_department_id)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password(TEST_PASSWORD))

    def test_create_form_preserves_explicit_contact_information(self):
        form = UserModelForm(data=self.payload(
            real_name="明确姓名", phone="13940000003", email="known@example.test",
        ))
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual((user.real_name, user.phone, user.email), (
            "明确姓名", "13940000003", "known@example.test",
        ))

    def test_invalid_explicit_phone_is_not_replaced_by_employee_phone(self):
        form = UserModelForm(data=self.payload(phone="not-a-phone"))
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)
        self.assertNotIn("phone", form.cleaned_data)
        self.assertFalse(models.User.objects.filter(username="profile_new").exists())

    def test_invalid_employee_phone_is_validated_after_filling(self):
        models.Employee.objects.filter(id=self.employee.id).update(mobile="invalid")
        form = UserModelForm(data=self.payload())
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)
        self.assertFalse(models.User.objects.filter(username="profile_new").exists())

    def test_unbound_user_does_not_receive_fabricated_fields(self):
        form = UserModelForm(data=self.payload(employee="", roles=[]))
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual((user.real_name, user.phone, user.email), ("", "", ""))
        self.assertIsNone(user.employee_id)

    def test_required_role_associations_are_still_validated(self):
        regular_form = UserModelForm(data=self.payload(employee=""))
        self.assertFalse(regular_form.is_valid())
        self.assertIn("employee", regular_form.errors)
        manager_role = models.Role.objects.create(name="部门管理员")
        manager_form = UserModelForm(data=self.payload(employee="", roles=[manager_role.id]))
        self.assertFalse(manager_form.is_valid())
        self.assertIn("managed_department", manager_form.errors)
        self.assertIn("employee", manager_form.errors)

    def test_manager_with_scope_still_requires_an_employee(self):
        manager_role = models.Role.objects.create(name="部门管理员")
        form = UserModelForm(data=self.payload(
            employee="", roles=[manager_role.id],
            managed_department=str(self.other_department.id),
        ))
        self.assertFalse(form.is_valid())
        self.assertIn("employee", form.errors)
        self.assertNotIn("managed_department", form.errors)
        self.assertFalse(models.User.objects.filter(username="profile_new").exists())

    def test_manager_membership_and_managed_department_may_differ(self):
        manager_role = models.Role.objects.create(name="部门管理员")
        form = UserModelForm(data=self.payload(
            roles=[manager_role.id], managed_department=str(self.other_department.id),
        ))
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.employee_id, self.employee.id)
        self.assertEqual(user.employee.department_id, self.department.id)
        self.assertEqual(user.managed_department_id, self.other_department.id)
        self.assertTrue(user.check_password(TEST_PASSWORD))

    def test_edit_manager_cannot_clear_employee_or_management_scope(self):
        manager_role = models.Role.objects.create(name="部门管理员")
        user = self.create_bound_user(managed_department=self.other_department)
        user.roles.set([manager_role])
        before = (user.password, user.employee_id, user.managed_department_id)
        for cleared_field in ("employee", "managed_department"):
            with self.subTest(cleared_field=cleared_field):
                data = self.payload(
                    username=user.username, roles=[manager_role.id],
                    managed_department=str(self.other_department.id),
                )
                data[cleared_field] = ""
                form = UserEditModelForm(data=data, instance=user)
                self.assertFalse(form.is_valid())
                self.assertIn(cleared_field, form.errors)
                user.refresh_from_db()
                self.assertEqual((user.password, user.employee_id, user.managed_department_id), before)
                self.assertEqual(list(user.roles.values_list("id", flat=True)), [manager_role.id])

    def test_edit_form_fills_blanks_without_changing_password_or_access(self):
        user = self.create_bound_user()
        old_password = user.password
        form = UserEditModelForm(data=self.payload(username=user.username), instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual((saved.real_name, saved.phone), (self.employee.name, self.employee.mobile))
        self.assertEqual(saved.password, old_password)
        self.assertIsNone(saved.managed_department_id)
        self.assertEqual(list(saved.scope_employees().values_list("id", flat=True)), [self.employee.id])
        self.assertEqual(list(saved.roles.values_list("name", flat=True)), ["普通员工"])

    def test_edit_removing_employee_does_not_fill_from_old_instance(self):
        user = self.create_bound_user()
        old_password = user.password
        form = UserEditModelForm(data=self.payload(
            username=user.username, employee="", roles=[],
        ), instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        user.refresh_from_db()
        self.assertIsNone(user.employee_id)
        self.assertEqual((user.real_name, user.phone), ("", ""))
        self.assertEqual(user.password, old_password)

    def test_commit_false_returns_filled_instance_without_persisting_it(self):
        form = UserModelForm(data=self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save(commit=False)
        self.assertIsNone(user.pk)
        self.assertEqual((user.real_name, user.phone), (self.employee.name, self.employee.mobile))
        self.assertFalse(models.User.objects.filter(username="profile_new").exists())


class UserProfileRepairTest(UserProfileFixtureMixin, TestCase):
    def setUp(self):
        self.missing = self.create_bound_user(
            email="existing@example.test", managed_department=self.other_department,
            is_staff=True, is_active=False,
        )
        self.partial = self.create_bound_user(
            username="profile_partial", employee=self.other_employee, real_name="保留账号备注",
        )
        self.unbound = models.User.objects.create_user(
            username="profile_unbound", password=TEST_PASSWORD,
        )

    def protected_state(self):
        return (
            list(models.User.objects.order_by("id").values(
                "id", "username", "password", "email", "employee_id", "managed_department_id",
                "is_active", "is_staff", "is_superuser",
            )),
            list(models.User.roles.through.objects.order_by("user_id", "role_id").values_list(
                "user_id", "role_id",
            )),
        )

    def test_default_dry_run_reports_only_ids_usernames_and_field_names(self):
        before = self.protected_state()
        with mock.patch.object(models.User, "save", side_effect=AssertionError("dry-run saved a user")):
            result = repair_user_profiles()
        self.assertFalse(result.applied)
        self.assertEqual(result.updated_count, 2)
        self.assertEqual([(row.user_id, row.username, row.fields) for row in result.rows], [
            (self.missing.id, self.missing.username, ("real_name", "phone")),
            (self.partial.id, self.partial.username, ("phone",)),
        ])
        self.assertEqual(before, self.protected_state())
        self.missing.refresh_from_db()
        self.partial.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone, self.partial.phone), ("", "", ""))
        self.assertNotIn(self.employee.mobile, repr(result))
        self.assertNotIn(self.missing.password, repr(result))

    def test_apply_only_fills_missing_fields_and_preserves_task_identity(self):
        task = models.Task.objects.create(
            title="资料修复保留任务", detail="资料回填不得更换个人负责人", assignee=self.missing,
        )
        receipt = models.TaskRead.objects.create(task=task, user=self.missing)
        before = self.protected_state()
        result = repair_user_profiles(apply=True)
        self.assertTrue(result.applied)
        self.assertEqual(result.updated_count, 2)
        self.assertEqual(before, self.protected_state())
        self.missing.refresh_from_db()
        self.partial.refresh_from_db()
        self.unbound.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone), (
            self.employee.name, self.employee.mobile,
        ))
        self.assertEqual((self.partial.real_name, self.partial.phone), (
            "保留账号备注", self.other_employee.mobile,
        ))
        self.assertEqual((self.unbound.real_name, self.unbound.phone, self.unbound.email), ("", "", ""))
        task.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual((task.assignee_id, receipt.user_id), (self.missing.id, self.missing.id))
        second = repair_user_profiles(apply=True)
        self.assertEqual((second.updated_count, second.rows), (0, ()))

    def test_explicit_phone_different_from_employee_is_not_overwritten(self):
        self.missing.phone = "13940000009"
        self.missing.save(update_fields=["phone"])
        repair_user_profiles(apply=True)
        self.missing.refresh_from_db()
        self.assertEqual(self.missing.phone, "13940000009")

    def test_invalid_source_preflight_rejects_entire_batch_before_any_write(self):
        models.Employee.objects.filter(id=self.other_employee.id).update(mobile="invalid")
        with self.assertRaises(ValidationError):
            repair_user_profiles(apply=True)
        self.missing.refresh_from_db()
        self.partial.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone, self.partial.phone), ("", "", ""))

    def test_write_failure_rolls_back_earlier_profile_changes(self):
        original_save = models.User.save

        def failing_save(user, *args, **kwargs):
            if user.pk == self.partial.pk:
                raise DatabaseError("forced profile write failure")
            return original_save(user, *args, **kwargs)

        with mock.patch.object(models.User, "save", new=failing_save):
            with self.assertRaises(DatabaseError):
                repair_user_profiles(apply=True)
        self.missing.refresh_from_db()
        self.partial.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone, self.partial.phone), ("", "", ""))

    def test_command_defaults_to_dry_run_and_apply_is_explicit(self):
        output = StringIO()
        call_command("repair_user_profiles", stdout=output)
        self.missing.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone), ("", ""))
        self.assertNotIn(self.employee.mobile, output.getvalue())
        self.assertNotIn(self.missing.password, output.getvalue())
        call_command("repair_user_profiles", "--apply", stdout=StringIO())
        self.missing.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone), (self.employee.name, self.employee.mobile))

    def test_command_invalid_source_is_a_clear_error_without_partial_repair(self):
        models.Employee.objects.filter(id=self.other_employee.id).update(mobile="invalid")
        with self.assertRaises(CommandError):
            call_command("repair_user_profiles", "--apply", stdout=StringIO())
        self.missing.refresh_from_db()
        self.assertEqual((self.missing.real_name, self.missing.phone), ("", ""))


class EmployeeAccountPhoneSyncTest(UserProfileFixtureMixin, TestCase):
    def test_new_accounts_copy_phone_without_granting_department_scope(self):
        result = sync_employee_accounts(apply=True, initial_password=TEST_PASSWORD)
        self.assertEqual(result.created_count, 2)
        for employee in (self.employee, self.other_employee):
            user = models.User.objects.get(employee=employee)
            self.assertEqual(user.phone, employee.mobile)
            self.assertIsNone(user.managed_department_id)
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertEqual(list(user.scope_employees().values_list("id", flat=True)), [employee.id])

    def test_matching_names_with_blank_phone_are_updated_once_and_existing_phone_is_preserved(self):
        missing = self.create_bound_user(username=self.employee.name, real_name=self.employee.name)
        explicit = self.create_bound_user(
            username=self.other_employee.name, employee=self.other_employee,
            real_name=self.other_employee.name, phone="13940000008",
        )
        old_password = missing.password
        result = sync_employee_accounts(apply=True, initial_password=None)
        self.assertEqual((result.created_count, result.updated_count, result.unchanged_count), (0, 1, 1))
        missing.refresh_from_db()
        explicit.refresh_from_db()
        self.assertEqual(missing.phone, self.employee.mobile)
        self.assertEqual(missing.password, old_password)
        self.assertEqual(explicit.phone, "13940000008")
        second = sync_employee_accounts(apply=True, initial_password=None)
        self.assertEqual((second.created_count, second.updated_count, second.unchanged_count), (0, 0, 2))


class UserProfileListTest(UserProfileFixtureMixin, TestCase):
    def setUp(self):
        self.admin_user = models.User.objects.create_superuser(
            username="profile_list_admin", password=TEST_PASSWORD, real_name="资料列表管理员",
        )
        self.fallback = self.create_bound_user(username="unrelated_login")
        self.manager_role = models.Role.objects.create(name="部门管理员")
        self.manager = models.User.objects.create_user(
            username="manager_without_employee", password=TEST_PASSWORD, real_name="仅负责部门",
            managed_department=self.other_department,
        )
        self.manager.roles.set([self.manager_role])
        self.client.force_login(self.admin_user)

    def user_row(self, response, user):
        import re
        match = re.search(
            r"<tr>\s*<th>\s*{}\s*</th>(.*?)</tr>".format(user.id),
            response.content.decode("utf-8"), re.DOTALL,
        )
        self.assertIsNotNone(match, "用户行未出现在列表中")
        return match.group(1)

    def test_list_shows_source_fallback_and_distinguishes_membership_from_management(self):
        response = self.client.get("/admin/list/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>所属部门</th>", html=True)
        self.assertContains(response, "<th>负责部门</th>", html=True)
        employee_row = self.user_row(response, self.fallback)
        self.assertIn(self.employee.name, employee_row)
        self.assertIn(self.employee.mobile, employee_row)
        self.assertIn(self.department.title, employee_row)
        self.assertIn("不适用", employee_row)
        manager_row = self.user_row(response, self.manager)
        self.assertIn(self.other_department.title, manager_row)
        self.assertIn("未填写", manager_row)
        self.assertIn("未关联", manager_row)
        self.fallback.refresh_from_db()
        self.assertEqual((self.fallback.real_name, self.fallback.phone), ("", ""))

    def test_search_can_find_displayed_employee_name_when_user_fields_are_blank(self):
        response = self.client.get("/admin/list/", {"q": self.employee.name})
        self.assertEqual(list(response.context["queryset"].values_list("id", flat=True)), [self.fallback.id])

    def test_related_employee_department_and_roles_are_loaded_without_per_row_queries(self):
        request = RequestFactory().get("/admin/list/")
        request.user = self.admin_user
        with mock.patch("app01.views.admin.render") as render_page:
            admin_list(request)
        queryset = render_page.call_args.args[2]["queryset"].all()
        with self.assertNumQueries(2):
            for user in queryset:
                _ = user.profile_name, user.profile_phone
                if user.employee_id:
                    _ = user.employee.department.title
                if user.managed_department_id:
                    _ = user.managed_department.title
                list(user.roles.all())

    def test_empty_search_result_spans_all_eleven_columns(self):
        response = self.client.get("/admin/list/", {"q": "绝不存在的资料用户名"})
        self.assertContains(response, 'colspan="11"')
        self.assertContains(response, "暂无数据")

    def test_profile_values_remain_html_escaped(self):
        user = models.User.objects.create_user(
            username="escaped_profile", password=TEST_PASSWORD,
            real_name="<script>alert(1)</script>",
        )
        response = self.client.get("/admin/list/")
        row = self.user_row(response, user)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", row)
        self.assertNotIn("<script>alert(1)</script>", row)

    def test_view_only_user_has_no_management_buttons(self):
        permission = models.Permission.objects.create(
            code="user.view", module="user", name="只读系统用户",
        )
        role = models.Role.objects.create(name="资料只读")
        role.permissions.add(permission)
        viewer = models.User.objects.create_user(username="profile_viewer", password=TEST_PASSWORD)
        viewer.roles.add(role)
        self.client.force_login(viewer)
        response = self.client.get("/admin/list/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "只读")
        self.assertNotContains(response, 'href="/admin/add/"')
        self.assertNotContains(response, 'href="/admin/{}/edit/"'.format(self.fallback.id))
