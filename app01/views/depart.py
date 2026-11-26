"""
部门管理（T2）：新增、查询、编辑、删除

路由：/depart/list/、/depart/add/、/depart/<nid>/edit/、/depart/delete/
模板：templates/depart_list1.html（列表）、templates/change.html（新增/编辑共用）

新手阅读：部门 CRUD（T2），是新手理解 MTV 链路的最佳起点。
GET list 查询列表；GET add/edit 展示表单；POST add/edit 校验并保存；删除只接受 POST。
"""
from django.db.models.deletion import ProtectedError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01.utils.request_validation import validate_post_id

from app01 import models
from app01.utils.form import DepartmentModelForm
from app01.utils.pagination import Pagination


def depart_list(request):
    """ 部门列表：支持按名称搜索 + 分页 """
    q = request.GET.get("q", "").strip()

    queryset = models.Department.objects.all().order_by("id")
    if q:
        queryset = queryset.filter(title__contains=q)

    page_object = Pagination(request, queryset)
    context = {
        "queryset": page_object.page_queryset,
        "page_string": page_object.page_html(),
        "q": q,
        "total_count": page_object.total_count,
    }
    return render(request, "depart_list1.html", context)


def depart_add(request):
    """ 新建部门（T2）：ModelForm 校验，成功后 PRG 回列表 """
    title = "新建部门"

    if request.method == "GET":
        form = DepartmentModelForm()
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/depart/list/"})

    form = DepartmentModelForm(data=request.POST)
    if form.is_valid():
        form.save()
        return redirect("/depart/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/depart/list/"})


def depart_edit(request, nid):
    """ 编辑部门（T2） """
    row_object = models.Department.objects.filter(id=nid).first()
    if row_object is None:          # 防止手输不存在的 id
        return redirect("/depart/list/")

    title = "编辑部门：{}".format(row_object.title)

    if request.method == "GET":
        form = DepartmentModelForm(instance=row_object)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/depart/list/"})

    form = DepartmentModelForm(data=request.POST, instance=row_object)
    if form.is_valid():
        form.save()
        return redirect("/depart/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/depart/list/"})


@require_POST
@validate_post_id
def depart_delete(request):
    """ 删除部门：仅接受 POST；有员工时阻止删除。 """
    nid = request.POST.get("nid")
    if nid:
        # 部门下还有员工时不让删，避免级联把员工档案一起删掉
        if models.Employee.objects.filter(department_id=nid).exists():
            return render(request, "error_alert.html",
                          {"error": "该部门下还有员工，不能删除", "back_url": "/depart/list/"})
        if models.Task.objects.filter(assignee_department_id=nid).exists():
            return render(request, "error_alert.html",
                          {"error": "该部门仍有组任务，请先删除或重新处理这些任务", "back_url": "/depart/list/"})
        if models.User.objects.filter(managed_department_id=nid).exists():
            return render(request, "error_alert.html", {
                "error": "该部门仍有管理账号，请先调整负责部门，不能直接删除。",
                "back_url": "/depart/list/",
            })
        try:
            models.Department.objects.filter(id=nid).delete()
        except ProtectedError:
            return render(request, "error_alert.html", {
                "error": "该部门仍有业务关联，不能直接删除。", "back_url": "/depart/list/",
            })
    return redirect("/depart/list/")