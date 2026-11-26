"""
系统用户管理（T5 RBAC 里“用户”这一层）

路由：/admin/list/、/admin/add/、/admin/<nid>/edit/、/admin/<nid>/reset/、/admin/delete/
模板：templates/admin_list.html、templates/change.html

说明：这里的 /admin/ 是“系统用户管理”业务页面，和 Django 自带的 /admin/ 后台是两回事。
urls.py 里把这些路由写在 'admin/' 之前，所以 /admin/list/ 会优先命中这里。

密码安全（任务书技术规范）：
    - 新建 / 重置密码都走 User.set_password()，数据库里是 PBKDF2 哈希；
    - 页面上密码列统一显示 ******，不暴露任何密文。

新手阅读：系统登录账号管理（T5），地址 /admin/list/，与 Django 原生后台分开。
修改目标账号前检查 can_manage_user，构建资料表单时传 operator 限制可授予范围。
"""
from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01 import models
from app01.utils.form import UserEditModelForm, UserModelForm, UserResetPasswordModelForm
from app01.utils.pagination import Pagination
from app01.utils.account_authorization import can_manage_user
from app01.utils.user_ordering import permission_priority_ordering
from app01.utils.request_validation import positive_id, validate_post_id


def _forbidden(request):
    return render(request, "403.html", {
        "required_perm": "授权范围内的 user.edit（超级管理员和后台管理账号受保护）",
        "path": request.path_info,
    }, status=403)



def admin_list(request):
    """ 系统用户列表：支持关键字搜索 + 分页；roles 用 prefetch_related 预取（T11） """
    q = request.GET.get("q", "").strip()

    # T11：prefetch_related 一次把角色查出来，模板里循环 obj.roles.all 不再逐行查库
    queryset = (
        models.User.objects
        .select_related("managed_department", "employee__department")
        .prefetch_related("roles")
    )
    if not request.user.is_superuser:
        queryset = queryset.prefetch_related("roles__permissions")
    if q:
        queryset = queryset.filter(
            Q(username__contains=q) | Q(real_name__contains=q) | Q(employee__name__contains=q)
        )

    # 先在数据库排序再分页；角色名字不等于授权，权限以实际权限码为准。
    queryset = permission_priority_ordering(queryset)
    page_object = Pagination(request, queryset)
    rows = page_object.page_queryset
    for target in rows:
        target.can_manage_account = can_manage_user(request.user, target)
    context = {
        "queryset": rows,
        "page_string": page_object.page_html(),
        "q": q,
        "total_count": page_object.total_count,
    }
    return render(request, "admin_list.html", context)


def admin_add(request):
    """ 新建系统用户：密码用 set_password 存哈希 """
    title = "新建系统用户"

    if request.method == "GET":
        form = UserModelForm(operator=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/admin/list/"})

    form = UserModelForm(data=request.POST, operator=request.user)
    if form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        except IntegrityError:
            form.add_error("username", "登录名或关联员工已被使用，本次未创建账号，请检查后重试。")
        else:
            return redirect("/admin/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/admin/list/"})


@transaction.atomic
def admin_edit(request, nid):
    """ 编辑系统用户：只改资料和角色，不改密码 """
    row_object = models.User.objects.select_for_update().select_related("employee").filter(id=nid).first()
    if row_object is None:
        return redirect("/admin/list/")
    if not can_manage_user(request.user, row_object):
        return _forbidden(request)

    title = "编辑系统用户：{}".format(row_object.username)

    if request.method == "GET":
        form = UserEditModelForm(instance=row_object, operator=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/admin/list/"})

    form = UserEditModelForm(data=request.POST, instance=row_object, operator=request.user)
    if form.is_valid():
        form.save()
        return redirect("/admin/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/admin/list/"})


@transaction.atomic
def admin_reset(request, nid):
    """ 重置密码：新密码用 set_password 存哈希 """
    row_object = models.User.objects.select_for_update().select_related("employee").filter(id=nid).first()
    if row_object is None:
        return redirect("/admin/list/")
    if not can_manage_user(request.user, row_object):
        return _forbidden(request)

    title = "重置密码：{}".format(row_object.username)

    if request.method == "GET":
        form = UserResetPasswordModelForm(instance=row_object)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/admin/list/"})

    form = UserResetPasswordModelForm(data=request.POST, instance=row_object)
    if form.is_valid():
        form.save()
        return redirect("/admin/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/admin/list/"})


@require_POST
@validate_post_id
@transaction.atomic
def admin_delete(request):
    """删除系统用户：不允许删除自己，也不允许删除仍承担个人任务的账号。"""
    value = request.POST.get("nid")
    nid = positive_id(value) if value else None
    if nid and nid != request.user.id:
        row_object = models.User.objects.select_for_update().select_related("employee").filter(id=nid).first()
        if row_object and not can_manage_user(request.user, row_object):
            return _forbidden(request)
        if row_object and models.Task.objects.filter(assignee=row_object).exists():
            return render(
                request,
                "error_alert.html",
                {"error": "该用户仍有个人任务，请先删除这些任务", "back_url": "/admin/list/"},
            )
        if row_object:
            row_object.delete()
    return redirect("/admin/list/")
