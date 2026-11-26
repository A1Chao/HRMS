"""
URL 路由（T1 ~ T10）

路由按功能模块分组：每个模块的视图放在 app01/views/<模块>.py 里，
这里只负责把 URL 和视图函数对应起来。

注意：/admin/list/ 这些“系统用户管理”路由必须写在 'admin/'（Django 自带后台）
之前，Django 按顺序匹配路由，先命中谁就用谁。

新手阅读：URL 是网页入口，path() 把地址交给对应 views 文件中的函数。
例如 /user/list/ 调用员工视图，而 /admin/list/ 是系统账号视图；/admin/ 才是 Django 后台。
新增接口还要检查权限中间件规则及对象范围，不能只在此注册地址。
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

# app01/views/admin.py 和 django.contrib.admin 同名，这里起个别名区分开
from app01.views import (
    account, avatar, admin as admin_views, attendance, chart, depart, notice, notification,
    pretty, rbac, salary, task, user,
)

urlpatterns = [
    # 登录 / 注销 / 图片验证码（T4，account.py）
    path('login/', account.login, name='login'),
    path('logout/', account.logout, name='logout'),
    path('image/code/', account.image_code, name='image_code'),

    # 用户头像：本人自助维护；管理员只能维护其员工权限范围内的账号
    path('profile/avatar/', avatar.my_avatar, name='my_avatar'),
    path('avatars/<int:user_id>/edit/', avatar.avatar_edit, name='avatar_edit'),
    path('avatars/<int:user_id>/delete/', avatar.avatar_delete, name='avatar_delete'),
    path('avatars/<int:user_id>/image/', avatar.avatar_image, name='avatar_image'),

    # 系统用户管理（T5，views/admin.py）—— 必须写在 'admin/' 之前
    path('admin/list/', admin_views.admin_list, name='admin_list'),
    path('admin/add/', admin_views.admin_add, name='admin_add'),
    path('admin/<int:nid>/edit/', admin_views.admin_edit, name='admin_edit'),
    path('admin/<int:nid>/reset/', admin_views.admin_reset, name='admin_reset'),
    path('admin/delete/', admin_views.admin_delete, name='admin_delete'),

    # Django 自带后台（/admin/ 进入，用来直接看数据库表）
    path('admin/', admin.site.urls),

    # 部门管理（T2，views/depart.py）
    path('depart/list/', depart.depart_list, name='depart_list'),
    path('depart/add/', depart.depart_add, name='depart_add'),
    path('depart/<int:nid>/edit/', depart.depart_edit, name='depart_edit'),
    path('depart/delete/', depart.depart_delete, name='depart_delete'),

    # 员工管理（T3，views/user.py）
    path('user/list/', user.user_list, name='user_list'),
    path('user/add/', user.user_add, name='user_add'),
    path('user/<int:nid>/edit/', user.user_edit, name='user_edit'),
    path('user/delete/', user.user_delete, name='user_delete'),

    # 考勤管理（T6，views/attendance.py）
    path('attendance/list/', attendance.attendance_list, name='attendance_list'),
    path('attendance/add/', attendance.attendance_add, name='attendance_add'),
    path('attendance/<int:nid>/edit/', attendance.attendance_edit, name='attendance_edit'),
    path('attendance/delete/', attendance.attendance_delete, name='attendance_delete'),

    # 薪资管理（T7，views/salary.py）
    path('salary/list/', salary.salary_list, name='salary_list'),
    path('salary/add/', salary.salary_add, name='salary_add'),
    path('salary/<int:nid>/edit/', salary.salary_edit, name='salary_edit'),
    path('salary/delete/', salary.salary_delete, name='salary_delete'),

    # 公告管理（T8，views/notice.py）
    path('notice/list/', notice.notice_list, name='notice_list'),
    path('notice/add/', notice.notice_add, name='notice_add'),
    path('notice/<int:nid>/edit/', notice.notice_edit, name='notice_edit'),
    path('notice/top/', notice.notice_top, name='notice_top'),
    path('notice/delete/', notice.notice_delete, name='notice_delete'),

    # 全局提醒：确认本次弹窗中实际展示的公告和本人任务
    path('notifications/read/', notification.notification_read, name='notification_read'),

    # 任务管理（T9，views/task.py）：列表页 + AJAX 提交 + 完成状态
    path('task/list/', task.task_list, name='task_list'),
    path('task/ajax/add/', task.task_ajax_add, name='task_ajax_add'),
    path('task/<int:nid>/status/', task.task_status, name='task_status'),
    path('task/<int:nid>/delete/', task.task_delete, name='task_delete'),

    # 数据统计可视化（T10，views/chart.py）：页面 + 两个 JSON 取数接口
    path('chart/', chart.chart, name='chart'),
    path('chart/depart/data/', chart.chart_depart_data, name='chart_depart_data'),
    path('chart/salary/data/', chart.chart_salary_data, name='chart_salary_data'),

    # 角色 / 权限（T5，views/rbac.py）
    path('role/list/', rbac.role_list, name='role_list'),
    path('role/add/', rbac.role_add, name='role_add'),
    path('role/<int:nid>/edit/', rbac.role_edit, name='role_edit'),
    path('role/delete/', rbac.role_delete, name='role_delete'),

    # 靓号管理（教程遗留模块，views/pretty.py）
    path('pretty/list/', pretty.pretty_list, name='pretty_list'),
    path('pretty/add/', pretty.pretty_add, name='pretty_add'),
    path('pretty/<int:nid>/edit/', pretty.pretty_edit, name='pretty_edit'),
    path('pretty/delete/', pretty.pretty_delete, name='pretty_delete'),

    # 首页：根据当前账号权限跳到首个可访问业务页
    path('', account.home, name='home'),
]

# 开发环境下让 runserver 能直接访问员工头像等上传文件（/media/...）
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
