"""新增员工可选账号与系统账号排序；所有写入在 Django 隔离测试库中。"""
import datetime
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase

from app01 import models
from app01.utils.form import EmployeeCreateModelForm
from app01.utils.user_ordering import permission_priority_ordering


class EmployeeOnboardingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("init_data", stdout=StringIO())
        cls.admin = models.User.objects.get(username="admin")
        cls.department = models.Department.objects.get(title="技术部")
        cls.manager = models.User.objects.get(username="张三")

    def setUp(self):
        self.client.force_login(self.admin)

    def payload(self, **changes):
        values = {
            "name": "新员工", "position": "软件工程师", "age": 25, "gender": 1,
            "account": "0.00", "create_time": "2026-01-01", "mobile": "13898765432",
            "department": self.department.id, "create_login": "on", "login_username": "",
            "initial_password": "NewEmployee!Random2026", "confirm_initial_password": "NewEmployee!Random2026",
        }
        values.update(changes)
        return values

    def test_get_defaults_to_checked_and_shows_new_password_fields(self):
        response = self.client.get("/user/add/")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertTrue(form["create_login"].value())
        self.assertContains(response, 'id="id_create_login" checked')
        self.assertContains(response, "确认初始密码")

    def test_checked_creates_employee_and_linked_read_only_account(self):
        accounts = list(models.User.objects.order_by("pk").values())
        response = self.client.post("/user/add/", self.payload())
        self.assertEqual(response.status_code, 302)
        employee = models.Employee.objects.get(mobile="13898765432")
        user = models.User.objects.get(employee=employee)
        self.assertEqual(user.username, employee.name)
        self.assertTrue(user.check_password("NewEmployee!Random2026"))
        self.assertEqual(user.real_name, employee.name)
        self.assertEqual(user.phone, employee.mobile)
        self.assertEqual(user.role_names, ["普通员工"])
        self.assertFalse(user.is_superuser or user.is_staff)
        self.assertIsNone(user.managed_department_id)
        self.assertFalse(models.Salary.objects.filter(employee=employee).exists())
        self.assertEqual(list(models.User.objects.filter(pk__in=[row["id"] for row in accounts])
                              .order_by("pk").values()), accounts)

    def test_changing_login_name_cannot_bypass_duplicate_registered_person_name(self):
        old = models.User.objects.get(username="张三")
        password = old.password
        response = self.client.post("/user/add/", self.payload(name="张三", login_username="zhangsan_new"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())
        old.refresh_from_db()
        self.assertEqual(old.password, password)
        self.assertFalse(models.User.objects.filter(username="zhangsan_new").exists())

    def test_unique_employee_can_choose_explicit_login_name(self):
        response = self.client.post("/user/add/", self.payload(login_username="new_person_login"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(models.User.objects.get(username="new_person_login").employee.mobile, "13898765432")

    def test_unchecked_creates_only_employee_and_ignores_password(self):
        payload = self.payload(initial_password="", confirm_initial_password="")
        payload.pop("create_login")
        before = models.User.objects.count()
        response = self.client.post("/user/add/", payload)
        self.assertEqual(response.status_code, 302)
        employee = models.Employee.objects.get(mobile="13898765432")
        self.assertFalse(models.User.objects.filter(employee=employee).exists())
        self.assertEqual(models.User.objects.count(), before)
        self.assertContains(self.client.get("/user/list/"), "未开通登录账号")

    def test_checked_requires_password_and_confirmation(self):
        for fields in ({"initial_password": ""}, {"confirm_initial_password": ""},
                       {"confirm_initial_password": "Mismatch!2026"}):
            with self.subTest(fields=fields):
                response = self.client.post("/user/add/", self.payload(**fields))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())

    def test_new_weak_password_is_rejected_without_changing_existing_demo_passwords(self):
        before = list(models.User.objects.order_by("pk").values_list("pk", "password"))
        response = self.client.post("/user/add/", self.payload(initial_password="123456", confirm_initial_password="123456"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())
        self.assertEqual(list(models.User.objects.order_by("pk").values_list("pk", "password")), before)

    def test_username_collision_does_not_leave_employee_or_relink_existing_account(self):
        before = list(models.User.objects.order_by("pk").values())
        response = self.client.post("/user/add/", self.payload(login_username="ADMIN"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())
        self.assertEqual(list(models.User.objects.order_by("pk").values()), before)

    def test_invalid_username_fails_without_writes(self):
        response = self.client.post("/user/add/", self.payload(login_username="bad login name"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())

    def test_invalid_employee_fails_before_account_creation(self):
        response = self.client.post("/user/add/", self.payload(age=17))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.User.objects.filter(username="新员工").exists())

    def test_extra_privilege_fields_cannot_elevate_new_account(self):
        response = self.client.post("/user/add/", self.payload(
            is_superuser="on", is_staff="on", managed_department=self.department.id,
            roles=[models.Role.objects.get(name="超级管理员").id],
        ))
        self.assertEqual(response.status_code, 302)
        user = models.User.objects.get(username="新员工")
        self.assertFalse(user.is_superuser or user.is_staff)
        self.assertIsNone(user.managed_department_id)
        self.assertEqual(user.role_names, ["普通员工"])

    def test_manager_without_account_permission_sees_disabled_option(self):
        self.client.force_login(self.manager)
        response = self.client.get("/user/add/")
        form = response.context["form"]
        self.assertFalse(form["create_login"].value())
        self.assertTrue(form.fields["create_login"].disabled)
        self.assertNotIn("initial_password", form.fields)

    def test_manager_can_create_employee_only_but_cannot_forge_account_option(self):
        self.client.force_login(self.manager)
        response = self.client.post("/user/add/", self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())
        payload = self.payload()
        payload.pop("create_login")
        response = self.client.post("/user/add/", payload)
        self.assertEqual(response.status_code, 302)
        employee = models.Employee.objects.get(mobile="13898765432")
        self.assertFalse(models.User.objects.filter(employee=employee).exists())

    def test_manager_still_cannot_create_cross_department_employee(self):
        self.client.force_login(self.manager)
        payload = self.payload(department=models.Department.objects.get(title="市场部").id)
        payload.pop("create_login")
        response = self.client.post("/user/add/", payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())

    def test_missing_or_elevated_regular_role_rejects_combined_creation(self):
        role = models.Role.objects.get(name="普通员工")
        role.permissions.add(models.Permission.objects.get(code="user.edit"))
        response = self.client.post("/user/add/", self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())
        role.delete()
        response = self.client.post("/user/add/", self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())

    def test_role_change_after_validation_is_rechecked_before_save(self):
        form = EmployeeCreateModelForm(data=self.payload(), user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        models.Role.objects.get(name="普通员工").permissions.add(models.Permission.objects.get(code="user.edit"))
        with self.assertRaises(ValidationError):
            form.save()
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())

    def test_role_binding_failure_rolls_back_both_objects(self):
        form = EmployeeCreateModelForm(data=self.payload(), user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        manager_type = type(self.admin.roles)
        with mock.patch.object(manager_type, "set", side_effect=RuntimeError("simulated M2M failure")):
            with self.assertRaises(RuntimeError):
                form.save()
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())
        self.assertFalse(models.User.objects.filter(username="新员工").exists())

    def test_database_unique_race_returns_error_and_rolls_back_employee(self):
        form = EmployeeCreateModelForm(data=self.payload(), user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch.object(models.User, "save", side_effect=IntegrityError("simulated duplicate")):
            with self.assertRaises(ValidationError):
                form.save()
        self.assertFalse(models.Employee.objects.filter(mobile="13898765432").exists())

    def test_failed_post_does_not_echo_plaintext_password(self):
        response = self.client.post("/user/add/", self.payload(age=17))
        self.assertNotContains(response, "NewEmployee!Random2026")
        self.assertTrue(response.context["form"]["create_login"].value())

    def test_edit_page_does_not_contain_account_creation_option(self):
        response = self.client.get("/user/{}/edit/".format(self.manager.employee_id))
        self.assertNotContains(response, 'id="id_create_login"')


class UserPriorityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ordinary = models.User.objects.create(username="ordinary_first")
        cls.root = models.User.objects.create(username="root_last", is_superuser=True)
        cls.staff = models.User.objects.create(username="backend", is_staff=True)
        cls.global_user = models.User.objects.create(username="global")
        cls.account_manager = models.User.objects.create(username="account_manager")
        cls.department_manager = models.User.objects.create(username="department_manager")
        cls.no_permissions = models.User.objects.create(username="no_permissions")
        for user, name, codes in (
            (cls.ordinary, "普通员工", ["employee.view", "salary.view"]),
            (cls.global_user, "全局授权", ["*"]),
            (cls.account_manager, "账号管理员", ["user.edit"]),
            (cls.department_manager, "部门管理员", ["employee.edit", "salary.edit", "employee.view"]),
        ):
            role = models.Role.objects.create(name=name)
            for code in codes:
                permission, _ = models.Permission.objects.get_or_create(code=code, defaults={"name": code, "module": code.split(".")[0]})
                role.permissions.add(permission)
            user.roles.add(role)

    def ordered(self):
        return list(permission_priority_ordering(models.User.objects.all()))

    def test_privileged_users_precede_earlier_created_ordinary_users(self):
        self.assertEqual([user.id for user in self.ordered()], [
            self.root.id, self.staff.id, self.global_user.id, self.account_manager.id,
            self.department_manager.id, self.ordinary.id, self.no_permissions.id,
        ])

    def test_duplicate_permissions_from_multiple_roles_do_not_inflate_score(self):
        role = models.Role.objects.create(name="重叠只读角色")
        role.permissions.set(models.Permission.objects.filter(code__in=["employee.view", "salary.view"]))
        self.ordinary.roles.add(role)
        user = next(user for user in self.ordered() if user.id == self.ordinary.id)
        self.assertEqual(user._distinct_permission_count, 2)

    def test_role_title_does_not_make_unprivileged_account_high_priority(self):
        role = models.Role.objects.create(name="超级管理员")
        self.no_permissions.roles.add(role)
        self.assertEqual(self.ordered()[-1].id, self.no_permissions.id)

    def test_equal_priority_and_permission_count_use_stable_id_order(self):
        second = models.User.objects.create(username="ordinary_second")
        second.roles.add(models.Role.objects.get(name="普通员工"))
        ids = [user.id for user in self.ordered()]
        self.assertLess(ids.index(self.ordinary.id), ids.index(second.id))

    def test_list_search_and_pagination_remain_compatible(self):
        self.client.force_login(self.root)
        response = self.client.get("/admin/list/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["queryset"])[0].id, self.root.id)
        response = self.client.get("/admin/list/", {"q": "ordinary"})
        self.assertEqual([user.id for user in response.context["queryset"]], [self.ordinary.id])

    def test_sorting_is_one_sql_query_without_duplicate_users(self):
        with self.assertNumQueries(1):
            users = self.ordered()
        self.assertEqual(len(users), models.User.objects.count())

    def test_module_wildcard_is_classified_as_write_authority(self):
        role = models.Role.objects.create(name="模块通配")
        permission = models.Permission.objects.create(code="task.*", name="任务通配", module="task")
        role.permissions.add(permission)
        self.no_permissions.roles.add(role)
        user = next(user for user in self.ordered() if user.id == self.no_permissions.id)
        self.assertEqual(user._permission_priority, 4)
