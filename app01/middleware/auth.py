"""
登录校验中间件（T4）

作用：所有请求先经过这里，判断用户有没有登录：
    - 没登录 → 直接 redirect 到 /login/，后面的视图函数根本不会执行；
    - 已登录 → 返回 None，继续往后走（进视图函数）。

放行名单（免登录）：
    /login/          登录页本身
    /static/         Bootstrap、jQuery 等静态资源（登录页要用）
    /image/code/     图片验证码接口（登录页的 <img> 要能加载）

说明：中间件顺序在 settings.MIDDLEWARE 里，AuthenticationMiddleware 在本中间件之前，
所以这里可以直接用 request.user（Django 已经根据 session 还原好了用户对象）。

新手阅读：认证回答“有没有登录”，并不是“有权访问哪些数据”。
AuthenticationMiddleware 先还原 request.user，本中间件再把匿名业务请求跳到登录页。
"""
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin


class AuthMiddleware(MiddlewareMixin):

    def process_request(self, request):
        # 0.排除那些不需要登录就能访问的页面
        if request.path_info == "/login/":
            return

        # 静态资源（bootstrap、jquery 等）也放行：
        # 登录页本身要用这些文件，不放行的话样式/脚本会被重定向到 /login/，登录页就“裸奔”了。
        if request.path_info.startswith("/static/"):
            return

        # 图片验证码：登录页上的 <img src="/image/code/"> 要能在未登录时加载
        if request.path_info.startswith("/image/code/"):
            return

        # 1.已登录（session 里有登录状态，request.user 是真实用户对象）→ 继续往后走
        if request.user.is_authenticated:
            return

        # 2.没有登录过，重新回到登录页面（带上 next，登录成功后能跳回原页面）
        return redirect("/login/?next={}".format(request.path_info))