"""
权限校验中间件（T5：接口级权限校验）

思路：
    1. 维护一张“URL 前缀 → 权限编码”的表（RULES），按顺序匹配，先匹配到的生效；
    2. 用户登录后，从“用户 → 角色 → 权限”里取出所有权限编码（User.permission_codes）；
    3. 有权限 → 放行；没权限 → 渲染 403 页面（HTTP 403），前端按钮也会用 {% has_perm %} 隐藏。

配合 app01/templatetags/rbac.py 的 {% has_perm %} 模板标签，形成
“中间件（后端拦截） + 模板标签（前端隐藏按钮）”两层控制。

新手阅读：这里做模块/动作权限检查；部门或本人范围还要在视图/表单里过滤。
写接口的匹配规则要放在 view 规则之前；403 是主动拒绝，不是程序崩溃。
未配置 RULES 的地址目前默认放行；新增业务路由必须同时补权限及对象范围。
"""
import re

from django.shortcuts import render
from django.utils.deprecation import MiddlewareMixin

# 正则 → 权限编码；顺序很重要：把 edit / delete 这类“写操作”放在 view 前面
RULES = [
    (r"^/depart/(?:add/|\d+/edit/|delete/)", "depart.edit"),
    (r"^/depart/", "depart.view"),

    (r"^/user/(?:add/|\d+/edit/|delete/)", "employee.edit"),
    (r"^/user/", "employee.view"),

    (r"^/attendance/(?:add/|\d+/edit/|delete/)", "attendance.edit"),
    (r"^/attendance/", "attendance.view"),

    (r"^/salary/(?:add/|\d+/edit/|delete/)", "salary.edit"),
    (r"^/salary/", "salary.view"),

    (r"^/notice/(?:add/|\d+/edit/|delete/|top/)", "notice.edit"),
    (r"^/notice/", "notice.view"),

    (r"^/task/(?:ajax/add/|\d+/delete/)", "task.edit"),
    (r"^/task/", "task.view"),

    (r"^/chart/", "chart.view"),

    (r"^/admin/(?:add/|\d+/(?:edit|reset)/|delete/)", "user.edit"),
    (r"^/admin/", "user.view"),

    (r"^/role/(?:add/|\d+/edit/|delete/)", "role.edit"),
    (r"^/role/", "role.view"),

    (r"^/pretty/(?:add/|\d+/edit/|delete/)", "pretty.edit"),
    (r"^/pretty/", "pretty.view"),
]

# 这些路径不参与权限检查（登录 / 注销 / 验证码 / 静态资源 / 上传文件）
EXEMPT_PREFIXES = ("/login/", "/logout/", "/image/code/", "/static/", "/media/")


class PermissionMiddleware(MiddlewareMixin):

    def process_request(self, request):
        # 未登录的请求交给 AuthMiddleware 处理，这里不重复判断
        if not request.user.is_authenticated:
            return

        path = request.path_info
        if path.startswith(EXEMPT_PREFIXES):
            return

        # 超级管理员拥有所有权限
        if request.user.is_superuser:
            return

        for pattern, code in RULES:
            if re.match(pattern, path):
                if request.user.has_perm_code(code):
                    return
                return render(
                    request,
                    "403.html",
                    {"required_perm": code, "path": path},
                    status=403,
                )

        # 没有配置规则的路径默认放行（例如首页重定向）
        return