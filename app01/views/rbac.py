"""
角色 / 权限管理（T5 RBAC 里“角色-权限”这一层）

路由：/role/list/、/role/add/、/role/<nid>/edit/、/role/delete/
模板：templates/role_list.html（列表）、templates/change.html（新增/编辑共用）

权限编码的约定：<模块>.<动作>，例如
    employee.view / employee.edit / salary.view / salary.edit / role.edit ...
中间件 app01/middleware/permission.py 里维护 URL 前缀 → 权限编码的映射，
前端按钮用 templatetags/rbac.py 的 {% has_perm %} 控制显示。

新手阅读：角色与权限的管理页（T5）。User.roles 和 Role.permissions 是两层多对多关系。
共享角色影响多个账号；can_manage_role 负责检查整组影响，表单限制可选择的权限。
"""
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01 import models
from app01.utils.form import RoleModelForm
from app01.utils.pagination import Pagination
from app01.utils.account_authorization import can_manage_role
from app01.utils.request_validation import validate_post_id


def _forbidden(request):
    return render(request, "403.html", {
        "required_perm": "授权范围内的 role.edit", "path": request.path_info,
    }, status=403)



def role_list(request):
    """ 角色列表：prefetch_related 预取权限，避免 N+1 查询（T11） """
    q = request.GET.get("q", "").strip()

    queryset = models.Role.objects.prefetch_related("permissions").order_by("id")
    if q:
        queryset = queryset.filter(name__contains=q)

    page_object = Pagination(request, queryset)
    context = {
        "queryset": page_object.page_queryset,
        "page_string": page_object.page_html(),
        "q": q,
        "total_count": page_object.total_count,
    }
    return render(request, "role_list.html", context)


def role_add(request):
    """ 新建角色并勾选权限 """
    title = "新建角色"

    if request.method == "GET":
        form = RoleModelForm(operator=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/role/list/"})

    form = RoleModelForm(data=request.POST, operator=request.user)
    if form.is_valid():
        form.save()
        return redirect("/role/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/role/list/"})


@transaction.atomic
def role_edit(request, nid):
    """ 编辑角色 / 权限 """
    row_object = models.Role.objects.select_for_update().filter(id=nid).first()
    if row_object is None:
        return redirect("/role/list/")
    if not can_manage_role(request.user, row_object):
        return _forbidden(request)

    title = "编辑角色：{}".format(row_object.name)

    if request.method == "GET":
        form = RoleModelForm(instance=row_object, operator=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/role/list/"})

    form = RoleModelForm(data=request.POST, instance=row_object, operator=request.user)
    if form.is_valid():
        form.save()
        return redirect("/role/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/role/list/"})


@require_POST
@validate_post_id
@transaction.atomic
def role_delete(request):
    """ 删除角色：仅接受 POST。 """
    nid = request.POST.get("nid")
    if nid:
        role = models.Role.objects.select_for_update().filter(id=nid).first()
        if role:
            if not can_manage_role(request.user, role):
                return _forbidden(request)
            role.delete()
    return redirect("/role/list/")