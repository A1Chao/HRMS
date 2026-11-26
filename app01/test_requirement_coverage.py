"""任务书 T11：公告发布人回退到员工姓名时也不能产生逐行额外查询。

所有测试写入均由 Django 测试运行器放在独立测试库中；不操作业务库。
"""
from unittest import mock

from django.test import RequestFactory, TestCase, override_settings

from app01 import models
from app01.context_processors import pending_notifications
from app01.views.notice import notice_list


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class NoticeFallbackPerformanceTest(TestCase):
    """MD5 仅用于加速此隔离测试类，实际系统仍使用 Django 默认安全哈希。"""

    @classmethod
    def setUpTestData(cls):
        department = models.Department.objects.create(title="发布部门")
        employee = models.Employee.objects.create(name="发布员工", position="工程师", age=25,
                                                 mobile="13899000001", department=department)
        publisher = models.User.objects.create_user(username="fallback_publisher", employee=employee, real_name="")
        cls.reader = models.User.objects.create_superuser(username="notice_reader", password="NoticeReader!2026")
        for index in range(5):
            models.Notice.objects.create(title="回退公告" + str(index), content="测试公告", publisher=publisher)

    def request(self):
        request = RequestFactory().get("/notice/list/")
        request.user = self.reader
        return request

    def test_notice_list_fallback_names_use_one_join_query(self):
        with mock.patch("app01.views.notice.render") as render_page:
            notice_list(self.request())
        rows = render_page.call_args.args[2]["queryset"]
        with self.assertNumQueries(1):
            names = [notice.publisher.display_name for notice in rows]
        self.assertEqual(names, ["发布员工"] * 5)

    def test_global_notice_notifications_do_not_requery_each_publisher(self):
        with self.assertNumQueries(2):
            context = pending_notifications(self.request())
            names = [notice.publisher.display_name for notice in context["pending_notice_notifications"]]
        self.assertEqual(names, ["发布员工"] * 5)
