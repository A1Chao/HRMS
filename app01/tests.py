"""
单元测试（任务书 T12）

覆盖关键流程：
    LoginTestCase       登录 / 验证码 / 未登录重定向
    RBACTestCase        角色-权限中间件（403 / 放行）
    EmployeeTestCase    员工 CRUD 的校验规则（年龄、手机号格式与唯一）
    AttendanceTestCase  考勤状态自动计算
    SalaryTestCase      实发工资计算 + 月度总额统计
    NoticeTaskTestCase  公告发布 / 置顶排序、AJAX 任务提交
    ChartTestCase       可视化数据接口

运行：python manage.py test

新手阅读：以 test_ 开头的方法描述自动验收场景，assertEqual/assertContains 判定结果。
必须经 Django 测试运行器执行，所有测试写入均进入测试库，不是正在使用的 ems_hrms。
"""
import datetime
import json
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings as dj_settings
from django.contrib.admin.sites import AdminSite
from django.contrib.sessions.backends.db import SessionStore
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from app01 import models
from app01.admin import TaskAdmin
from app01.utils.form import UserModelForm


def set_session(client, **kwargs):
    """给测试客户端塞一个已登录（或带验证码）的 session。

    Django 测试客户端的 client.session 每次访问都是新的 SessionStore，
    直接改它不会持久化，所以这里手动创建 session 并写 cookie。
    """
    store = SessionStore()
    store.create()
    store.update(kwargs)
    store.save()
    client.cookies[dj_settings.SESSION_COOKIE_NAME] = store.session_key
    return store


class BaseDataTestCase(TestCase):
    """ 公共测试数据：权限、角色、用户、部门 """

    @classmethod
    def setUpTestData(cls):
        cls.depart = models.Department.objects.create(title="技术部")

        cls.perm_emp_view = models.Permission.objects.create(
            module="employee", name="查看员工", code="employee.view")
        cls.perm_emp_edit = models.Permission.objects.create(
            module="employee", name="维护员工", code="employee.edit")
        cls.perm_chart = models.Permission.objects.create(
            module="chart", name="查看统计", code="chart.view")

        cls.role_viewer = models.Role.objects.create(name="只读角色")
        cls.role_viewer.permissions.set([cls.perm_emp_view, cls.perm_chart])

        cls.admin_user = models.User.objects.create_superuser(
            username="admin", password="123456", real_name="管理员")
        cls.viewer = models.User.objects.create_user(
            username="viewer", password="123456", real_name="只读用户")
        cls.viewer.roles.set([cls.role_viewer])


class LoginTestCase(BaseDataTestCase):
    """ T4：登录 + 图片验证码 """

    def test_anonymous_redirect_to_login(self):
        """ 未登录访问受保护页面 → 302 到登录页（带 next） """
        response = self.client.get("/depart/list/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_login_page_has_captcha_image(self):
        """ 登录页包含用户名、密码、验证码输入框和验证码图片 """
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'name="code"')
        self.assertContains(response, 'src="/image/code/"')

    def test_image_code_returns_png(self):
        """ 验证码接口返回 PNG，并把验证码写进 session """
        response = self.client.get("/image/code/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("image_code", self.client.session)

    def test_login_missing_captcha(self):
        response = self.client.post("/login/", {"username": "admin", "password": "123456", "code": ""})
        self.assertContains(response, "请输入图片验证码")

    def test_login_wrong_captcha(self):
        set_session(self.client, image_code="ABCDE", image_code_time=datetime.datetime.now().timestamp())
        response = self.client.post("/login/", {"username": "admin", "password": "123456", "code": "ZZZZZ"})
        self.assertContains(response, "验证码错误")

    def test_login_expired_captcha(self):
        import time as _time
        set_session(self.client, image_code="ABCDE", image_code_time=_time.time() - 3600)
        response = self.client.post("/login/", {"username": "admin", "password": "123456", "code": "ABCDE"})
        self.assertContains(response, "验证码已过期")

    def test_login_success(self):
        """ 验证码正确 + 密码正确 → 登录成功，跳转首页 """
        import time as _time
        set_session(self.client, image_code="ABCDE", image_code_time=_time.time())
        response = self.client.post("/login/", {"username": "admin", "password": "123456", "code": "abcde"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/chart/")
        self.assertEqual(self.client.session["info"]["name"], "管理员")

    def test_task_only_employee_login_uses_authorized_default_page(self):
        import time as _time
        task_view = models.Permission.objects.create(
            module="task", name="查看任务", code="task.view"
        )
        employee_role = models.Role.objects.create(name="登录员工角色")
        employee_role.permissions.set([task_view])
        employee_user = models.User.objects.create_user(
            username="landing_staff", password="StrongPass!2026", real_name="落地页员工"
        )
        employee_user.roles.set([employee_role])

        set_session(self.client, image_code="ABCDE", image_code_time=_time.time())
        response = self.client.post("/login/", {
            "username": "landing_staff", "password": "StrongPass!2026", "code": "ABCDE",
        })
        self.assertRedirects(response, "/task/list/")

        response = self.client.get("/")
        self.assertRedirects(response, "/task/list/")

    def test_user_without_business_permission_gets_safe_home_instead_of_loop(self):
        user = models.User.objects.create_user(
            username="no_permission", password="StrongPass!2026", real_name="未授权用户"
        )
        self.client.force_login(user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "暂未配置业务页面权限")

    def test_anonymous_home_redirects_to_login_with_next(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?next=/")

    def test_login_preserves_safe_internal_next(self):
        import time as _time
        set_session(self.client, image_code="ABCDE", image_code_time=_time.time())
        response = self.client.post("/login/?" + urlencode({"next": "/depart/list/?q=tech"}), {
            "username": "admin", "password": "123456", "code": "ABCDE",
        })
        self.assertRedirects(response, "/depart/list/?q=tech")

    def test_login_rejects_external_and_backslash_next(self):
        import time as _time
        for next_url in ("https://example.invalid/", "//example.invalid/", "/" + chr(92) + "example.invalid/"):
            with self.subTest(next_url=next_url):
                set_session(self.client, image_code="ABCDE", image_code_time=_time.time())
                response = self.client.post("/login/?" + urlencode({"next": next_url}), {
                    "username": "admin", "password": "123456", "code": "ABCDE",
                })
                self.assertRedirects(response, "/chart/")

    def test_login_wrong_password(self):
        """ 验证码正确但密码错误 → 提示用户名或密码错误，且不登录 """
        import time as _time
        set_session(self.client, image_code="ABCDE", image_code_time=_time.time())
        response = self.client.post("/login/", {"username": "admin", "password": "bad", "code": "ABCDE"})
        self.assertContains(response, "用户名或密码错误")
        self.assertNotIn("info", self.client.session)


class RBACTestCase(BaseDataTestCase):
    """ T5：权限中间件 + 前端按钮权限 """

    def test_no_permission_403(self):
        """ 只读角色访问员工列表（有 employee.view）→ 放行；访问新增（需要 employee.edit）→ 403 """
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get("/user/list/").status_code, 200)

        response = self.client.get("/user/add/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "403", status_code=403)

    def test_superuser_pass(self):
        """ 超级管理员不受权限限制 """
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get("/user/add/").status_code, 200)
        self.assertEqual(self.client.get("/admin/list/").status_code, 200)

    def test_numeric_edit_route_requires_write_permission(self):
        self.client.force_login(self.viewer)
        response = self.client.get("/user/999999/edit/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.context["required_perm"], "employee.edit")

    def test_has_perm_code(self):
        self.assertTrue(self.viewer.has_perm_code("employee.view"))
        self.assertFalse(self.viewer.has_perm_code("employee.edit"))
        self.assertTrue(self.admin_user.has_perm_code("anything.at.all"))


class EmployeeTestCase(BaseDataTestCase):
    """ T3：员工 CRUD + 校验 """

    def setUp(self):
        self.client.force_login(self.admin_user)
        self.mobile = "13800000001"

    def _payload(self, **overrides):
        data = {
            "name": "测试员工",
            "position": "测试工程师",
            "age": 30,
            "gender": 1,
            "account": "1000.00",
            "create_time": "2025-01-01",
            "mobile": self.mobile,
            "department": self.depart.id,
        }
        data.update(overrides)
        return data

    def test_add_employee_success(self):
        response = self.client.post("/user/add/", self._payload())
        self.assertEqual(response.status_code, 302)
        employee = models.Employee.objects.get(mobile=self.mobile)
        self.assertEqual(employee.name, "测试员工")
        self.assertEqual(employee.department, self.depart)

    def test_add_employee_invalid_age(self):
        response = self.client.post("/user/add/", self._payload(age=17))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "18~60")
        self.assertFalse(models.Employee.objects.filter(mobile=self.mobile).exists())

    def test_add_employee_invalid_mobile(self):
        response = self.client.post("/user/add/", self._payload(mobile="12345"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "手机号格式不正确")

    def test_add_employee_short_name(self):
        response = self.client.post("/user/add/", self._payload(name="张"))
        self.assertContains(response, "姓名长度需为 2~10 个字符")

    def test_duplicate_mobile(self):
        models.Employee.objects.create(
            name="已有员工", age=30, gender=1, account=Decimal("0"),
            mobile=self.mobile, department=self.depart,
        )
        response = self.client.post("/user/add/", self._payload(name="新员工"))
        self.assertContains(response, "手机号已存在")

    def test_edit_keep_same_mobile(self):
        """ 编辑时不改手机号，唯一性校验要排除自己（不能提示重复） """
        employee = models.Employee.objects.create(
            name="已有员工", age=30, gender=1, account=Decimal("0"),
            mobile=self.mobile, department=self.depart,
        )
        response = self.client.post("/user/{}/edit/".format(employee.id), self._payload(name="改名员工"))
        self.assertEqual(response.status_code, 302)
        employee.refresh_from_db()
        self.assertEqual(employee.name, "改名员工")

    def test_search_and_filter(self):
        models.Employee.objects.create(
            name="张三", age=30, gender=1, account=Decimal("0"),
            mobile="13800000002", department=self.depart,
        )
        self.client.force_login(self.admin_user)
        response = self.client.get("/user/list/", {"q": "张", "department_id": self.depart.id})
        self.assertContains(response, "张三")
        self.assertNotContains(response, "李四")


class AttendanceTestCase(BaseDataTestCase):
    """ T6：考勤状态自动计算 """

    def setUp(self):
        self.employee = models.Employee.objects.create(
            name="张三", age=30, gender=1, account=Decimal("0"),
            mobile="13800000011", department=self.depart,
        )

    def _create(self, check_in, check_out):
        return models.Attendance.objects.create(
            employee=self.employee, date=datetime.date(2025, 3, 1),
            check_in=check_in, check_out=check_out,
        )

    def test_on_time(self):
        self.assertEqual(self._create(datetime.time(8, 50), datetime.time(18, 10)).status, 1)

    def test_late(self):
        self.assertEqual(self._create(datetime.time(9, 10), datetime.time(18, 10)).status, 2)

    def test_early_leave(self):
        self.assertEqual(self._create(datetime.time(8, 50), datetime.time(17, 30)).status, 3)

    def test_absent(self):
        self.assertEqual(self._create(None, None).status, 4)

    def test_status_recomputed_on_save(self):
        attendance = self._create(datetime.time(8, 50), datetime.time(18, 10))
        attendance.check_in = datetime.time(9, 30)
        attendance.save()
        attendance.refresh_from_db()
        self.assertEqual(attendance.status, 2)


class SalaryTestCase(BaseDataTestCase):
    """ T7：实发工资计算 + 月度总额统计 """

    def setUp(self):
        self.employee = models.Employee.objects.create(
            name="张三", age=30, gender=1, account=Decimal("0"),
            mobile="13800000021", department=self.depart,
        )

    def test_total_computed(self):
        salary = models.Salary.objects.create(
            employee=self.employee, month="2025-01",
            base_salary=Decimal("8000.00"), bonus=Decimal("1000.00"), deduction=Decimal("500.00"),
        )
        self.assertEqual(salary.total, Decimal("8500.00"))

    def test_salary_view_total_sum(self):
        models.Salary.objects.create(
            employee=self.employee, month="2025-01",
            base_salary=Decimal("8000.00"), bonus=Decimal("1000.00"), deduction=Decimal("500.00"),
        )
        models.Salary.objects.create(
            employee=self.employee, month="2025-02",
            base_salary=Decimal("8000.00"), bonus=Decimal("0.00"), deduction=Decimal("0.00"),
        )
        self.client.force_login(self.admin_user)
        response = self.client.get("/salary/list/", {"month": "2025-01"})
        self.assertEqual(response.context["total_salary"], Decimal("8500.00"))

    def test_salary_month_format(self):
        self.client.force_login(self.admin_user)
        response = self.client.post("/salary/add/", {
            "employee": self.employee.id, "month": "2025-13",
            "base_salary": "8000", "bonus": "0", "deduction": "0",
        })
        self.assertContains(response, "月份格式应为 YYYY-MM")


class NoticeTaskTestCase(BaseDataTestCase):
    """ T8 公告 + T9 AJAX 任务 """

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_notice_publisher_auto(self):
        response = self.client.post("/notice/add/", {
            "title": "测试公告", "content": "公告内容测试", "is_top": "",
        })
        self.assertEqual(response.status_code, 302)
        notice = models.Notice.objects.get(title="测试公告")
        self.assertEqual(notice.publisher, self.admin_user)

    def test_notice_top_first(self):
        models.Notice.objects.create(title="普通公告", content="内容", publisher=self.admin_user)
        models.Notice.objects.create(title="置顶公告", content="内容", publisher=self.admin_user, is_top=True)
        response = self.client.get("/notice/list/")
        titles = [obj.title for obj in response.context["queryset"]]
        self.assertEqual(titles[0], "置顶公告")

    def test_task_ajax_success(self):
        employee = models.Employee.objects.create(
            name="派发员工", age=28, gender=1, mobile="13820009902", department=self.depart,
        )
        self.viewer.employee = employee
        self.viewer.save(update_fields=["employee"])
        response = self.client.post("/task/ajax/add/", {
            "level": 1, "title": "测试任务", "detail": "任务详情内容", "assignee": self.viewer.id,
        })
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload["status"])
        self.assertEqual(payload["data"]["title"], "测试任务")
        self.assertTrue(models.Task.objects.filter(title="测试任务").exists())

    def test_task_ajax_validation_error(self):
        response = self.client.post("/task/ajax/add/", {
            "level": 1, "title": "x", "detail": "短", "assignee": "",
        })
        payload = json.loads(response.content)
        self.assertFalse(payload["status"])
        self.assertIn("title", payload["errors"])
        self.assertIn("detail", payload["errors"])

    def test_task_ajax_can_dispatch_to_department_group(self):
        employee = models.Employee.objects.create(
            name="接收员工", age=28, gender=1, account=Decimal("0"),
            mobile="13820009901", department=self.depart,
        )
        self.viewer.employee = employee
        self.viewer.save(update_fields=["employee"])
        response = self.client.post("/task/ajax/add/", {
            "level": 2,
            "title": "部门组任务",
            "detail": "派发给技术部全组处理",
            "target_type": "department",
            "assignee": "",
            "assignee_department": self.depart.id,
        })
        payload = response.json()
        self.assertTrue(payload["status"], payload)
        task = models.Task.objects.get(title="部门组任务")
        self.assertIsNone(task.assignee_id)
        self.assertEqual(task.assignee_department, self.depart)
        self.assertEqual(payload["data"]["dispatch_target"], "技术部（组）")

    def test_task_ajax_rejects_zero_or_two_dispatch_targets(self):
        no_target = self.client.post("/task/ajax/add/", {
            "level": 2, "title": "无目标任务", "detail": "派发目标不能为空",
            "target_type": "user", "assignee": "", "assignee_department": "",
        }).json()
        self.assertFalse(no_target["status"])
        self.assertIn("assignee", no_target["errors"])

        two_targets = self.client.post("/task/ajax/add/", {
            "level": 2, "title": "双目标任务", "detail": "不能同时选择两个目标",
            "target_type": "user", "assignee": self.admin_user.id,
            "assignee_department": self.depart.id,
        }).json()
        self.assertFalse(two_targets["status"])
        self.assertIn("assignee_department", two_targets["errors"])

    def test_database_constraint_rejects_task_without_unique_target(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                models.Task.objects.create(
                    level=3, title="数据库无目标", detail="数据库约束必须拒绝两空"
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                models.Task.objects.create(
                    level=3, title="数据库双目标", detail="数据库约束必须拒绝两有",
                    assignee=self.admin_user, assignee_department=self.depart,
                )


class NotificationWorkflowTestCase(BaseDataTestCase):
    """登录后的公告/任务提醒必须按用户隔离，确认后不再重复弹出。"""

    def setUp(self):
        self.client.force_login(self.viewer)

    def test_unread_notice_forces_modal_until_current_user_confirms(self):
        notice = models.Notice.objects.create(
            title="必须阅读的公告", content="请确认公告内容", publisher=self.admin_user,
        )

        response = self.client.get("/chart/")
        self.assertContains(response, 'id="system-notification-modal"')
        self.assertContains(response, 'data-backdrop="static"')
        self.assertContains(response, "必须阅读的公告")

        confirm = self.client.post("/notifications/read/", {"notice_ids": [notice.id]})
        self.assertEqual(confirm.status_code, 200)
        self.assertTrue(confirm.json()["status"])
        self.assertTrue(models.NoticeRead.objects.filter(notice=notice, user=self.viewer).exists())

        response = self.client.get("/chart/")
        self.assertNotContains(response, "必须阅读的公告")

    def test_notice_read_is_user_specific_and_idempotent(self):
        notice = models.Notice.objects.create(
            title="用户隔离公告", content="公告阅读互不影响", publisher=self.admin_user,
        )
        for _ in range(2):
            self.client.post("/notifications/read/", {"notice_ids": [notice.id]})
        self.assertEqual(
            models.NoticeRead.objects.filter(notice=notice, user=self.viewer).count(), 1
        )

        self.client.force_login(self.admin_user)
        response = self.client.get("/chart/")
        self.assertContains(response, "用户隔离公告")

    def test_task_popup_only_contains_current_assignee_tasks(self):
        own_task = models.Task.objects.create(
            level=1, title="我的新任务", detail="仅负责人可确认的任务", assignee=self.viewer,
        )
        other_task = models.Task.objects.create(
            level=2, title="他人的新任务", detail="不应出现在当前用户弹窗", assignee=self.admin_user,
        )

        response = self.client.get("/chart/")
        self.assertContains(response, "我的新任务")
        self.assertNotContains(response, "他人的新任务")

        self.client.post(
            "/notifications/read/", {"task_ids": [own_task.id, other_task.id]}
        )
        self.assertTrue(models.TaskRead.objects.filter(task=own_task, user=self.viewer).exists())
        self.assertFalse(models.TaskRead.objects.filter(task=other_task, user=self.viewer).exists())
        self.assertNotContains(self.client.get("/chart/"), "我的新任务")

    def test_notification_read_requires_post_and_csrf(self):
        notice = models.Notice.objects.create(
            title="CSRF 提醒", content="提醒确认必须安全提交", publisher=self.admin_user,
        )
        self.assertEqual(self.client.get("/notifications/read/").status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True, SERVER_NAME="127.0.0.1")
        csrf_client.force_login(self.viewer)
        self.assertEqual(
            csrf_client.post("/notifications/read/", {"notice_ids": [notice.id]}).status_code,
            403,
        )
        csrf_client.get("/chart/")
        token = csrf_client.cookies["csrftoken"].value
        response = csrf_client.post(
            "/notifications/read/",
            {"notice_ids": [notice.id], "csrfmiddlewaretoken": token},
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)


class TaskWorkflowTestCase(BaseDataTestCase):
    """任务只向负责人展示；完成后灰度下沉，状态修改执行对象级权限。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.task_view = models.Permission.objects.create(
            module="task", name="查看任务", code="task.view"
        )
        cls.task_edit = models.Permission.objects.create(
            module="task", name="维护任务", code="task.edit"
        )
        cls.role_viewer.permissions.add(cls.task_view)
        cls.other_user = models.User.objects.create_user(
            username="other_worker", password="StrongPass!2026", real_name="其他员工"
        )
        cls.other_user.roles.set([cls.role_viewer])

        cls.viewer_employee = models.Employee.objects.create(
            name="组内员工", age=29, gender=1, account=Decimal("0"),
            mobile="13820000001", department=cls.depart,
        )
        cls.viewer.employee = cls.viewer_employee
        cls.viewer.save(update_fields=["employee"])

        cls.other_department = models.Department.objects.create(title="市场部")
        cls.other_employee = models.Employee.objects.create(
            name="外组员工", age=30, gender=2, account=Decimal("0"),
            mobile="13820000002", department=cls.other_department,
        )
        cls.other_user.employee = cls.other_employee
        cls.other_user.save(update_fields=["employee"])

    def setUp(self):
        self.client.force_login(self.viewer)

    def test_regular_user_sees_only_own_tasks_and_completed_tasks_sink(self):
        pending = models.Task.objects.create(
            level=2, title="我的未完成任务", detail="应排在列表前面", assignee=self.viewer,
        )
        completed = models.Task.objects.create(
            level=1, title="我的已完成任务", detail="即使更新也必须下沉", assignee=self.viewer,
        )
        completed.set_completed(True, self.viewer)
        completed.save(update_fields=["is_completed", "completed_at", "completed_by"])
        models.Task.objects.create(
            level=3, title="他人的任务", detail="普通员工不能看到", assignee=self.other_user,
        )

        response = self.client.get("/task/list/")
        titles = [task.title for task in response.context["queryset"]]
        self.assertEqual(titles, [pending.title, completed.title])
        self.assertNotContains(response, "他人的任务")
        self.assertContains(response, 'class="task-completed"')
        self.assertContains(response, "已完成")

    def test_assignee_can_complete_and_restore_own_task(self):
        task = models.Task.objects.create(
            level=1, title="负责人操作", detail="负责人可以切换完成状态", assignee=self.viewer,
        )
        response = self.client.post(
            "/task/{}/status/".format(task.id), {"completed": "1"}
        )
        self.assertRedirects(response, "/task/list/")
        task.refresh_from_db()
        self.assertTrue(task.is_completed)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.completed_by, self.viewer)
        self.assertTrue(models.TaskRead.objects.filter(task=task, user=self.viewer).exists())

        response = self.client.post(
            "/task/{}/status/".format(task.id), {"completed": "0"}
        )
        self.assertRedirects(response, "/task/list/")
        task.refresh_from_db()
        self.assertFalse(task.is_completed)
        self.assertIsNone(task.completed_at)
        self.assertIsNone(task.completed_by)

    def test_non_assignee_cannot_change_task_status(self):
        task = models.Task.objects.create(
            level=2, title="他人负责", detail="不能越权修改完成状态", assignee=self.other_user,
        )
        response = self.client.post(
            "/task/{}/status/".format(task.id), {"completed": "1"}
        )
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertFalse(task.is_completed)

    def test_superuser_can_change_any_task_status(self):
        task = models.Task.objects.create(
            level=3, title="管理员代办", detail="管理员可维护任意任务", assignee=self.other_user,
        )
        self.client.force_login(self.admin_user)
        response = self.client.post(
            "/task/{}/status/".format(task.id), {"completed": "1"}
        )
        self.assertRedirects(response, "/task/list/")
        task.refresh_from_db()
        self.assertTrue(task.is_completed)
        self.assertEqual(task.completed_by, self.admin_user)

    def test_view_only_user_cannot_dispatch_task_and_status_get_is_rejected(self):
        response = self.client.post("/task/ajax/add/", {
            "level": 1,
            "title": "越权派发",
            "detail": "普通员工不能派发新的任务",
            "assignee": self.viewer.id,
        })
        self.assertEqual(response.status_code, 403)

        task = models.Task.objects.create(
            level=3, title="仅 POST", detail="状态接口不能通过 GET 修改", assignee=self.viewer,
        )
        self.assertEqual(self.client.get("/task/{}/status/".format(task.id)).status_code, 405)

    def test_department_group_task_visible_only_to_current_group_members(self):
        group_task = models.Task.objects.create(
            level=1, title="技术部组任务", detail="技术部当前成员共同处理",
            assignee_department=self.depart,
        )
        response = self.client.get("/task/list/")
        self.assertContains(response, group_task.title)
        self.assertContains(response, "技术部（组）")

        self.client.force_login(self.other_user)
        response = self.client.get("/task/list/")
        self.assertNotContains(response, group_task.title)

    def test_group_member_gets_independent_popup_and_can_complete(self):
        second_employee = models.Employee.objects.create(
            name="第二组员", age=27, gender=2, account=Decimal("0"),
            mobile="13820000003", department=self.depart,
        )
        second_member = models.User.objects.create_user(
            username="second_member", password="StrongPass!2026",
            real_name="第二组员", employee=second_employee,
        )
        second_member.roles.set([self.role_viewer])
        group_task = models.Task.objects.create(
            level=2, title="组提醒任务", detail="每位组员都应独立收到提醒",
            assignee_department=self.depart,
        )

        first_page = self.client.get("/task/list/")
        self.assertIn(group_task.id, [task.id for task in first_page.context["pending_task_notifications"]])
        self.client.post("/notifications/read/", {"task_ids": [group_task.id]})
        self.assertTrue(models.TaskRead.objects.filter(task=group_task, user=self.viewer).exists())
        self.assertFalse(models.TaskRead.objects.filter(task=group_task, user=second_member).exists())
        first_page = self.client.get("/task/list/")
        self.assertNotIn(group_task.id, [task.id for task in first_page.context["pending_task_notifications"]])

        second_client = Client()
        second_client.force_login(second_member)
        second_page = second_client.get("/task/list/")
        self.assertIn(group_task.id, [task.id for task in second_page.context["pending_task_notifications"]])
        second_client.post("/notifications/read/", {"task_ids": [group_task.id]})
        second_page = second_client.get("/task/list/")
        self.assertNotIn(group_task.id, [task.id for task in second_page.context["pending_task_notifications"]])
        response = second_client.post(
            "/task/{}/status/".format(group_task.id), {"completed": "1"}
        )
        self.assertRedirects(response, "/task/list/")
        group_task.refresh_from_db()
        self.assertTrue(group_task.is_completed)
        self.assertEqual(group_task.completed_by, second_member)
        completed_at = group_task.completed_at
        self.client.post("/task/{}/status/".format(group_task.id), {"completed": "1"})
        group_task.refresh_from_db()
        self.assertEqual(group_task.completed_by, second_member)
        self.assertEqual(group_task.completed_at, completed_at)

    def test_managed_department_without_employee_is_not_group_membership(self):
        manager_only = models.User.objects.create_user(
            username="manager_only", password="StrongPass!2026",
            real_name="仅管理范围账号", managed_department=self.depart,
        )
        manager_only.roles.set([self.role_viewer])
        group_task = models.Task.objects.create(
            level=3, title="成员定义测试", detail="管理范围不等于部门成员",
            assignee_department=self.depart,
        )
        manager_client = Client()
        manager_client.force_login(manager_only)
        self.assertNotContains(manager_client.get("/task/list/"), group_task.title)

    def test_task_delete_requires_edit_permission_and_cascades_receipts(self):
        task = models.Task.objects.create(
            level=2, title="待删除任务", detail="删除时同时清理阅读回执", assignee=self.viewer,
        )
        models.TaskRead.objects.create(task=task, user=self.viewer)

        forbidden = self.client.post("/task/{}/delete/".format(task.id))
        self.assertEqual(forbidden.status_code, 403)
        self.assertTrue(models.Task.objects.filter(id=task.id).exists())

        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get("/task/{}/delete/".format(task.id)).status_code, 405)
        response = self.client.post("/task/{}/delete/".format(task.id))
        self.assertRedirects(response, "/task/list/")
        self.assertFalse(models.Task.objects.filter(id=task.id).exists())
        self.assertFalse(models.TaskRead.objects.filter(task_id=task.id).exists())

    def test_delete_button_only_visible_to_task_editors(self):
        own_task = models.Task.objects.create(
            level=3, title="删除按钮权限", detail="普通接收人不能删除任务", assignee=self.viewer,
        )
        response = self.client.get("/task/list/")
        self.assertNotContains(response, "/task/{}/delete/".format(own_task.id))

        self.client.force_login(self.admin_user)
        response = self.client.get("/task/list/")
        self.assertContains(response, "/task/{}/delete/".format(own_task.id))


    def test_non_superuser_task_editor_can_delete_task(self):
        self.role_viewer.permissions.add(self.task_edit)
        task = models.Task.objects.create(
            title="普通编辑者删除", detail="数字删除路由也必须允许有写权限的账号",
            assignee=self.other_user,
        )
        response = self.client.post("/task/{}/delete/".format(task.id))
        self.assertRedirects(response, "/task/list/")
        self.assertFalse(models.Task.objects.filter(id=task.id).exists())

    def test_department_manager_is_limited_to_managed_department(self):
        manager = models.User.objects.create_user(
            username="scoped_manager", password="StrongPass!2026",
            managed_department=self.depart,
        )
        manager_role = models.Role.objects.create(name="任务部门管理员")
        manager_role.permissions.set([self.task_view, self.task_edit])
        manager.roles.set([manager_role])
        own_task = models.Task.objects.create(
            title="本部门任务", detail="允许部门管理员维护", assignee=self.viewer,
        )
        foreign_task = models.Task.objects.create(
            title="其他部门任务", detail="禁止跨部门查看或维护", assignee=self.other_user,
        )
        foreign_group = models.Task.objects.create(
            title="其他部门组任务", detail="禁止跨部门组查看或维护",
            assignee_department=self.other_department,
        )
        self.client.force_login(manager)
        response = self.client.get("/task/list/")
        self.assertEqual([task.id for task in response.context["queryset"]], [own_task.id])
        self.assertNotIn(self.other_user.id, [user.id for user in response.context["users"]])
        self.assertEqual([department.id for department in response.context["departments"]], [self.depart.id])
        for task in (foreign_task, foreign_group):
            self.assertEqual(self.client.post(
                "/task/{}/status/".format(task.id), {"completed": "1"}
            ).status_code, 403)
            self.assertEqual(self.client.post("/task/{}/delete/".format(task.id)).status_code, 403)
        for target in (
            {"target_type": "user", "assignee": self.other_user.id},
            {"target_type": "department", "assignee_department": self.other_department.id},
        ):
            payload = {"level": 2, "title": "跨部门派发", "detail": "伪造目标仍必须被拦截", **target}
            self.assertFalse(self.client.post("/task/ajax/add/", payload).json()["status"])
        allowed = self.client.post("/task/ajax/add/", {
            "level": 2, "title": "合法部门派发", "detail": "可以派发给自己负责的组",
            "target_type": "department", "assignee_department": self.depart.id,
        }).json()
        self.assertTrue(allowed["status"], allowed)

    def test_empty_and_inactive_only_department_groups_cannot_receive_tasks(self):
        empty_department = models.Department.objects.create(title="空接收组")
        self.client.force_login(self.admin_user)
        payload = {
            "level": 2, "title": "空组派发", "detail": "没有有效接收账号就不能派发",
            "target_type": "department", "assignee_department": empty_department.id,
        }
        result = self.client.post("/task/ajax/add/", payload).json()
        self.assertFalse(result["status"])
        self.assertIn("assignee_department", result["errors"])
        employee = models.Employee.objects.create(
            name="停用组员", age=30, gender=1, mobile="13820000004", department=empty_department,
        )
        models.User.objects.create_user(
            username="inactive_recipient", password="StrongPass!2026",
            employee=employee, is_active=False,
        )
        self.assertFalse(self.client.post("/task/ajax/add/", payload).json()["status"])
        self.assertFalse(models.Task.objects.filter(title="空组派发").exists())

    def test_other_department_cannot_confirm_or_complete_group_task(self):
        group_task = models.Task.objects.create(
            title="外组权限验证", detail="其他部门不能确认提醒或完成任务",
            assignee_department=self.other_department,
        )
        self.client.post("/notifications/read/", {"task_ids": [group_task.id]})
        self.assertFalse(models.TaskRead.objects.filter(task=group_task, user=self.viewer).exists())
        response = self.client.post("/task/{}/status/".format(group_task.id), {"completed": "1"})
        self.assertEqual(response.status_code, 403)
        group_task.refresh_from_db()
        self.assertFalse(group_task.is_completed)

    def test_group_membership_tracks_current_employee_department(self):
        old_group = models.Task.objects.create(
            title="原部门组任务", detail="调岗后不再属于原部门组", assignee_department=self.depart,
        )
        new_group = models.Task.objects.create(
            title="新部门组任务", detail="调岗后可以接收新部门组", assignee_department=self.other_department,
        )
        self.client.post("/notifications/read/", {"task_ids": [old_group.id]})
        models.Employee.objects.filter(id=self.viewer_employee.id).update(department=self.other_department)
        page = self.client.get("/task/list/")
        self.assertEqual([task.id for task in page.context["queryset"]], [new_group.id])
        self.assertIn(new_group.id, [task.id for task in page.context["pending_task_notifications"]])
        models.Employee.objects.filter(id=self.viewer_employee.id).update(department=self.depart)
        page = self.client.get("/task/list/")
        self.assertEqual([task.id for task in page.context["queryset"]], [old_group.id])
        self.assertNotIn(old_group.id, [task.id for task in page.context["pending_task_notifications"]])

    def test_invalid_completed_value_never_reopens_task(self):
        task = models.Task.objects.create(title="状态参数验证", detail="状态参数必须显式合法", assignee=self.viewer)
        task.set_completed(True, self.viewer)
        task.save(update_fields=["is_completed", "completed_at", "completed_by"])
        completed_at = task.completed_at
        for payload in ({}, {"completed": ""}, {"completed": "yes"}, {"completed": "2"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/task/{}/status/".format(task.id), payload).status_code, 400)
                task.refresh_from_db()
                self.assertTrue(task.is_completed)
                self.assertEqual(task.completed_at, completed_at)

    def test_delete_requires_csrf_and_anonymous_user_cannot_delete(self):
        task = models.Task.objects.create(title="删除保护验证", detail="删除任务必须带安全凭据", assignee=self.viewer)
        anonymous = Client()
        self.assertEqual(anonymous.post("/task/{}/delete/".format(task.id)).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True, SERVER_NAME="127.0.0.1")
        csrf_client.force_login(self.admin_user)
        csrf_client.get("/task/list/")
        token = csrf_client.cookies["csrftoken"].value
        self.assertEqual(csrf_client.post("/task/{}/delete/".format(task.id)).status_code, 403)
        self.assertTrue(models.Task.objects.filter(id=task.id).exists())
        response = csrf_client.post("/task/{}/delete/".format(task.id), {"csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(models.Task.objects.filter(id=task.id).exists())
        self.assertEqual(csrf_client.post("/task/999999/delete/", {"csrfmiddlewaretoken": token}).status_code, 404)

    def test_dispatch_targets_are_protected_from_deletion(self):
        models.Task.objects.create(title="个人历史保护", detail="不能删除仍承担任务的账号", assignee=self.viewer)
        models.Task.objects.create(title="组历史保护", detail="不能删除仍承担任务的部门", assignee_department=self.depart)
        with self.assertRaises(ProtectedError):
            self.viewer.delete()
        with self.assertRaises(ProtectedError):
            self.depart.delete()
        self.assertTrue(models.User.objects.filter(id=self.viewer.id).exists())
        self.assertTrue(models.Department.objects.filter(id=self.depart.id).exists())

    def test_backend_keeps_dispatch_targets_readonly_after_creation(self):
        task = models.Task.objects.create(title="后台目标保护", detail="改派应以新任务重新提醒", assignee=self.viewer)
        request = RequestFactory().get("/admin/")
        request.user = self.admin_user
        admin = TaskAdmin(models.Task, AdminSite())
        fields = admin.get_readonly_fields(request, task)
        self.assertIn("assignee", fields)
        self.assertIn("assignee_department", fields)
        self.assertIn("completed_by", fields)
        self.assertIn("completed_at", fields)
        self.assertNotIn("assignee", admin.get_readonly_fields(request))


class TaskTargetMigrationTestCase(TransactionTestCase):
    """旧无目标任务必须在 DDL 前失败；修复数据后同一迁移可以重试。"""

    def test_old_task_preflight_fails_before_ddl_and_can_retry(self):
        self.assertNotEqual(connection.settings_dict["NAME"], "ems_hrms")
        previous = ("app01", "0004_grant_task_view_to_regular_staff")
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate([previous])
        old_apps = executor.loader.project_state([previous]).apps
        User = old_apps.get_model("app01", "User")
        Task = old_apps.get_model("app01", "Task")
        TaskRead = old_apps.get_model("app01", "TaskRead")
        owner = User.objects.create(username="migration_owner", password="unused-in-migration-test")
        task = Task.objects.create(title="旧空目标任务", detail="预检必须先于表结构改变")
        receipt = TaskRead.objects.create(task=task, user=owner)
        try:
            with connection.cursor() as cursor:
                before_columns = [field.name for field in connection.introspection.get_table_description(cursor, "app01_task")]
            with self.assertRaisesRegex(RuntimeError, "历史任务"):
                MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as cursor:
                after_columns = [field.name for field in connection.introspection.get_table_description(cursor, "app01_task")]
            self.assertEqual(before_columns, after_columns)
            self.assertNotIn("assignee_department_id", after_columns)
            Task.objects.filter(id=task.id).update(assignee_id=owner.id)
            MigrationExecutor(connection).migrate(latest)
            migrated_task = models.Task.objects.get(id=task.id)
            self.assertEqual(migrated_task.assignee_id, owner.id)
            self.assertIsNone(migrated_task.assignee_department_id)
            self.assertTrue(models.TaskRead.objects.filter(id=receipt.id, task_id=task.id).exists())
        finally:
            Task.objects.filter(id=task.id).update(assignee_id=owner.id)
            MigrationExecutor(connection).migrate(latest)


class ChartTestCase(BaseDataTestCase):
    """ T10：可视化数据接口 """

    def setUp(self):
        self.client.force_login(self.admin_user)
        models.Employee.objects.create(
            name="张三", age=30, gender=1, account=Decimal("0"),
            mobile="13800000031", department=self.depart,
        )

    def test_depart_data(self):
        response = self.client.get("/chart/depart/data/")
        payload = json.loads(response.content)
        self.assertTrue(payload["status"])
        self.assertEqual(payload["data"][0]["name"], "技术部")
        self.assertEqual(payload["data"][0]["value"], 1)

    def test_salary_data(self):
        employee = models.Employee.objects.get(mobile="13800000031")
        models.Salary.objects.create(
            employee=employee, month="2025-01",
            base_salary=Decimal("8000"), bonus=Decimal("0"), deduction=Decimal("0"),
        )
        response = self.client.get("/chart/salary/data/")
        payload = json.loads(response.content)
        self.assertEqual(payload["data"]["months"], ["2025-01"])
        self.assertEqual(payload["data"]["totals"], [8000.0])

    def test_chart_page(self):
        response = self.client.get("/chart/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "echarts.min.js")


class DataScopeTestCase(TestCase):
    """角色数据范围：部门管理员仅本部门，普通员工仅本人。"""

    @classmethod
    def setUpTestData(cls):
        cls.tech = models.Department.objects.create(title="技术部")
        cls.market = models.Department.objects.create(title="市场部")
        cls.tech_employee = models.Employee.objects.create(
            name="技术员工", age=30, gender=1, account=Decimal("0"),
            mobile="13810000001", department=cls.tech,
        )
        cls.market_employee = models.Employee.objects.create(
            name="市场员工", age=28, gender=2, account=Decimal("0"),
            mobile="13810000002", department=cls.market,
        )

        permission_specs = [
            ("employee", "查看员工", "employee.view"),
            ("employee", "维护员工", "employee.edit"),
            ("attendance", "查看考勤", "attendance.view"),
            ("attendance", "维护考勤", "attendance.edit"),
            ("salary", "查看薪资", "salary.view"),
            ("salary", "维护薪资", "salary.edit"),
            ("chart", "查看统计", "chart.view"),
        ]
        permissions = {
            code: models.Permission.objects.create(module=module, name=name, code=code)
            for module, name, code in permission_specs
        }
        manager_role = models.Role.objects.create(name="部门管理员")
        manager_role.permissions.set(permissions.values())
        staff_role = models.Role.objects.create(name="普通员工")
        staff_role.permissions.set([
            permissions["employee.view"],
            permissions["attendance.view"],
            permissions["salary.view"],
        ])

        cls.manager = models.User.objects.create_user(
            username="manager_scope", password="StrongPass!2026",
            managed_department=cls.tech, employee=cls.tech_employee,
        )
        cls.manager.roles.set([manager_role])
        cls.staff = models.User.objects.create_user(
            username="staff_scope", password="StrongPass!2026",
            employee=cls.market_employee,
        )
        cls.staff.roles.set([staff_role])

        for employee, amount in (
            (cls.tech_employee, Decimal("1000.00")),
            (cls.market_employee, Decimal("2000.00")),
        ):
            models.Attendance.objects.create(
                employee=employee, date=datetime.date(2025, 4, 1),
                check_in=datetime.time(8, 50), check_out=datetime.time(18, 10),
            )
            models.Salary.objects.create(
                employee=employee, month="2025-04", base_salary=amount,
                bonus=Decimal("0"), deduction=Decimal("0"),
            )

    def test_department_manager_sees_only_managed_department(self):
        self.client.force_login(self.manager)
        employee_response = self.client.get("/user/list/")
        attendance_response = self.client.get("/attendance/list/")
        salary_response = self.client.get("/salary/list/")
        self.assertEqual(employee_response.context["total_count"], 1)
        self.assertEqual(attendance_response.context["total_count"], 1)
        self.assertEqual(salary_response.context["total_count"], 1)
        self.assertEqual(salary_response.context["total_salary"], Decimal("1000.00"))
        self.assertContains(employee_response, "技术员工")
        self.assertNotContains(employee_response, "市场员工")

    def test_regular_employee_sees_only_self(self):
        self.client.force_login(self.staff)
        employee_response = self.client.get("/user/list/")
        attendance_response = self.client.get("/attendance/list/")
        salary_response = self.client.get("/salary/list/")
        self.assertEqual(employee_response.context["total_count"], 1)
        self.assertEqual(attendance_response.context["total_count"], 1)
        self.assertEqual(salary_response.context["total_count"], 1)
        self.assertEqual(salary_response.context["total_salary"], Decimal("2000.00"))
        self.assertContains(employee_response, "市场员工")
        self.assertNotContains(employee_response, "技术员工")

    def test_cross_department_edit_and_delete_are_blocked(self):
        self.client.force_login(self.manager)
        edit_response = self.client.get("/user/{}/edit/".format(self.market_employee.id))
        self.assertRedirects(edit_response, "/user/list/")
        delete_response = self.client.post("/user/delete/", {"nid": self.market_employee.id})
        self.assertRedirects(delete_response, "/user/list/")
        self.assertTrue(models.Employee.objects.filter(id=self.market_employee.id).exists())

    def test_chart_uses_scoped_data_and_staff_has_no_chart_permission(self):
        self.client.force_login(self.manager)
        depart_payload = json.loads(self.client.get("/chart/depart/data/").content)
        salary_payload = json.loads(self.client.get("/chart/salary/data/").content)
        self.assertEqual(depart_payload["data"], [{"name": "技术部", "value": 1}])
        self.assertEqual(salary_payload["data"]["totals"], [1000.0])

        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/chart/").status_code, 403)


class StateChangingSecurityTestCase(BaseDataTestCase):
    """删除、置顶和注销不得由 GET 触发，并接受 CSRF 保护。"""

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_state_changing_get_requests_return_405(self):
        urls = [
            "/depart/delete/", "/user/delete/", "/attendance/delete/",
            "/salary/delete/", "/notice/top/", "/notice/delete/",
            "/admin/delete/", "/role/delete/", "/pretty/delete/", "/logout/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_notice_top_requires_csrf_and_post_succeeds_with_token(self):
        notice = models.Notice.objects.create(
            title="CSRF 测试", content="安全测试公告", publisher=self.admin_user,
        )
        csrf_client = Client(enforce_csrf_checks=True, SERVER_NAME="127.0.0.1")
        csrf_client.force_login(self.admin_user)
        self.assertEqual(
            csrf_client.post("/notice/top/", {"nid": notice.id}).status_code,
            403,
        )
        csrf_client.get("/notice/list/")
        token = csrf_client.cookies["csrftoken"].value
        response = csrf_client.post(
            "/notice/top/", {"nid": notice.id, "csrfmiddlewaretoken": token},
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertRedirects(response, "/notice/list/")
        notice.refresh_from_db()
        self.assertTrue(notice.is_top)


class PerformanceRegressionTestCase(BaseDataTestCase):
    """固定 RBAC 权限缓存和嵌套外键预载，防止 N+1 回归。"""

    def test_permission_codes_are_cached_on_user_instance(self):
        user = models.User.objects.get(id=self.viewer.id)
        with CaptureQueriesContext(connection) as context:
            self.assertTrue(user.has_perm_code("employee.view"))
            self.assertFalse(user.has_perm_code("employee.edit"))
            self.assertTrue(user.has_perm_code("chart.view"))
        permission_queries = [
            item for item in context.captured_queries
            if "app01_permission" in item["sql"] and "app01_role_permissions" in item["sql"]
        ]
        self.assertEqual(len(permission_queries), 1)

    def test_nested_employee_department_is_eager_loaded(self):
        employee = models.Employee.objects.create(
            name="性能员工", age=30, gender=1, account=Decimal("0"),
            mobile="13810000003", department=self.depart,
        )
        models.Attendance.objects.create(
            employee=employee, date=datetime.date(2025, 5, 1),
            check_in=datetime.time(8, 50), check_out=datetime.time(18, 10),
        )
        models.Salary.objects.create(
            employee=employee, month="2025-05", base_salary=Decimal("5000"),
            bonus=Decimal("0"), deduction=Decimal("0"),
        )
        with self.assertNumQueries(1):
            attendance_rows = list(
                models.Attendance.objects.select_related("employee__department")
            )
            [row.employee.department.title for row in attendance_rows]
        with self.assertNumQueries(1):
            salary_rows = list(models.Salary.objects.select_related("employee__department"))
            [row.employee.department.title for row in salary_rows]


class PasswordPolicyTestCase(TestCase):
    """自定义用户表单必须执行 Django 密码验证器并安全哈希。"""

    def test_common_numeric_password_is_rejected(self):
        form = UserModelForm(data={
            "username": "weak_user", "real_name": "弱密码用户",
            "phone": "", "email": "", "roles": [],
            "managed_department": "", "employee": "",
            "password": "123456", "confirm_password": "123456",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_strong_password_is_saved_as_hash(self):
        raw_password = "CourseDesign!2026_XyZ"
        form = UserModelForm(data={
            "username": "strong_user", "real_name": "强密码用户",
            "phone": "", "email": "", "roles": [],
            "managed_department": "", "employee": "",
            "password": raw_password, "confirm_password": raw_password,
        })
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertNotEqual(user.password, raw_password)
        self.assertTrue(user.check_password(raw_password))