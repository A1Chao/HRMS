"""
登录 / 注销 / 图片验证码（T4 登录认证 + T5 权限体系的入口）

相对教程的改动：
    1. 账号从 app01.User（继承 AbstractUser）里查，密码用 Django 的 check_password 校验
       （PBKDF2 哈希，数据库里不存明文，也不再用裸 MD5）；
    2. 登录成功后除了写 session["info"]（教程 13.5 导航栏用），还调用 auth.login()，
       这样中间件和模板里都能直接拿到 request.user；
    3. 验证码继续沿用第 14 章的做法：生成图片 → 存 session → 登录时一次性校验。

新手阅读：登录、验证码、注销和首页跳转（T4）。先读 login()，再看两个认证/权限中间件。
auth_login 写登录会话；session["info"] 只是显示资料，并不是权限凭证。
"""
import logging
import time
from io import BytesIO

from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from app01 import models
from app01.utils.code import check_code

logger = logging.getLogger("app01")

# 验证码有效期（秒）
IMAGE_CODE_TIMEOUT = 60


def _check_image_code(request):
    """校验图片验证码，返回 (是否通过, 错误提示)

    验证码是“一次性”的：不管校验成功还是失败，都会先从 session 里删掉，
    这样同一个验证码不能重复提交；页面上重新加载时会再生成一张新图。
    """
    code = (request.POST.get("code") or "").strip()
    saved_code = request.session.pop("image_code", None)
    saved_time = request.session.pop("image_code_time", 0) or 0

    # 1.没填
    if not code:
        return False, "请输入图片验证码"
    # 2.session 里没有验证码（没点开图片 / 换了浏览器 / session 过期）
    if not saved_code:
        return False, "验证码已失效，请点击图片刷新"
    # 3.超过有效期
    if time.time() - saved_time > IMAGE_CODE_TIMEOUT:
        return False, "验证码已过期，请点击图片刷新"
    # 4.比对（不区分大小写，图片里生成的都是大写字母）
    if code.upper() != saved_code.upper():
        return False, "验证码错误"
    return True, ""


def _default_landing_url(user):
    """按权限返回首个可访问业务页；普通员工优先进入自己的任务页。"""
    routes = (
        ("chart.view", "/chart/"),
        ("task.view", "/task/list/"),
        ("employee.view", "/user/list/"),
        ("attendance.view", "/attendance/list/"),
        ("salary.view", "/salary/list/"),
        ("notice.view", "/notice/list/"),
        ("depart.view", "/depart/list/"),
        ("user.view", "/admin/list/"),
        ("role.view", "/role/list/"),
        ("pretty.view", "/pretty/list/"),
    )
    for permission_code, url in routes:
        if user.has_perm_code(permission_code):
            return url
    return None


def home(request):
    """系统统一入口：跳到当前账号真正有权访问的页面。"""
    landing_url = _default_landing_url(request.user)
    if landing_url:
        return redirect(landing_url)
    return render(request, "home.html")


def login(request):
    """ 登录（T4） """
    # GET：显示登录页面（页面里的 <img src="/image/code/"> 会再请求验证码图片）
    if request.method == "GET":
        return render(request, "login.html")

    # POST：先校验图片验证码，再校验用户名和密码
    username = request.POST.get("username", "").strip()
    password = request.POST.get("password", "")

    code_ok, code_error = _check_image_code(request)
    if not code_ok:
        # 校验失败：回到登录页显示提示，并把用户名带回输入框（不用重新输）
        return render(request, "login.html", {"error": code_error, "username": username})

    user = models.User.objects.filter(username=username).first()
    # check_password 内部用 PBKDF2 校验，数据库里存的是哈希，不是明文
    if user is None or not user.check_password(password):
        logger.info("登录失败：用户名或密码错误，username=%s", username)
        return render(request, "login.html", {"error": "用户名或密码错误", "username": username})

    if not user.is_active:
        logger.warning("登录失败：账号已禁用，username=%s", username)
        return render(request, "login.html", {"error": "账号已被禁用，请联系管理员", "username": username})

    # 登录成功：Django 自己的 auth 体系也登录一次（写 session，request.user 可用）
    auth_login(request, user)
    # 教程 13.5 的约定：session["info"] 里放 id 和显示名
    request.session["info"] = {"id": user.id, "name": user.display_name}
    logger.info("登录成功：username=%s", username)

    # 支持 ?next=/xxx/ 登录后跳回原页面（只接受站内路径，防止被拿去做开放重定向）
    next_url = request.GET.get("next", "").strip()
    if not (
        next_url.startswith("/")
        and not next_url.startswith("//")
        and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        next_url = _default_landing_url(user) or "/"
    return redirect(next_url)


@require_POST
def logout(request):
    """ 注销（T4）：仅接受 POST，清空登录信息后回到登录页。 """
    auth_logout(request)
    request.session.clear()
    return redirect("/login/")


def image_code(request):
    """ 生成图片验证码（T4 登录页用） """
    # 1.调用 check_code 生成图片和验证码字符串
    img, code_string = check_code()

    # 2.把验证码写进 session，并记下生成时间（用来判断 60 秒有效期）
    request.session["image_code"] = code_string
    request.session["image_code_time"] = time.time()

    # 3.把图片写进内存，返回给浏览器（<img src="/image/code/"> 直接显示）
    stream = BytesIO()
    img.save(stream, "png")
    return HttpResponse(stream.getvalue(), content_type="image/png")