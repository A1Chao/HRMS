"""
员工管理（T3）：完整 CRUD + ModelForm 校验 + 模糊搜索 + 部门筛选 + 分页

路由：/user/list/、/user/add/、/user/<nid>/edit/、/user/delete/
模板：templates/user_list.html（列表）、templates/change.html（新增/编辑共用）

性能（T11）：列表页用 select_related("department") 一次 JOIN 查出部门，
避免“循环里每行再查一次部门”的 N+1 查询。

新手阅读：员工档案 CRUD（T3），不是系统登录账号管理。
先 scope_employees 限定可见记录，再应用姓名/部门/职位筛选，最后领导优先排序与分页。
"""
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01.utils.request_validation import positive_id, validate_post_id

from app01 import models
from app01.utils.form import EmployeeCreateModelForm, EmployeeModelForm
from app01.utils.pagination import Pagination
from app01.utils.employee_ordering import leadership_priority


def user_list(request):
    """员工列表：领导岗位优先，支持姓名、部门及职位组合筛选。"""
    q = request.GET.get("q", "").strip()
    department_id = request.GET.get("department_id", "").strip()
    position = request.GET.get("position", "").strip()

    # 数据范围：超级管理员看全部；部门管理员看负责部门；普通员工只看本人。
    # 必须先约束身份范围；随后搜索、职位选项、排序及分页都基于同一个可见集合。
    scoped_queryset = request.user.scope_employees(
        models.Employee.objects.select_related("department", "user_account")
    )
    # Options come from authorised records, not from the current page/search results.
    positions = (scoped_queryset.order_by().exclude(position="")
                 .values_list("position", flat=True).distinct().order_by("position"))
    queryset = scoped_queryset.annotate(_leadership_priority=leadership_priority()).order_by(
        "_leadership_priority", "id",
    )
    if position:
        queryset = queryset.filter(position=position)
    if q:
        queryset = queryset.filter(name__contains=q)
    if department_id:
        try:
            department_pk = positive_id(department_id)
        except ValueError:
            return HttpResponseBadRequest("部门 ID 必须为有效的正整数")
        queryset = queryset.filter(department_id=department_pk)

    page_object = Pagination(request, queryset)

    # 筛选下拉只展示当前账号有权查看的部门；部门管理员即使暂时没有员工也能选其负责部门。
    if request.user.is_superuser:
        departments = models.Department.objects.all().order_by("id")
    elif request.user.managed_department_id:
        departments = models.Department.objects.filter(id=request.user.managed_department_id)
    elif request.user.employee_id:
        departments = models.Department.objects.filter(employee__id=request.user.employee_id).distinct()
    else:
        departments = models.Department.objects.none()

    context = {
        "queryset": page_object.page_queryset,
        "page_string": page_object.page_html(),
        "q": q,
        "department_id": department_id,
        "departments": departments,
        "position": position,
        "positions": positions,
        "total_count": page_object.total_count,
    }
    return render(request, "user_list.html", context)


def user_add(request):
    """ 新建员工（T3）：ModelForm 负责姓名长度、年龄、手机号正则/唯一等校验 """
    title = "新建员工"
    if request.method == "GET":
        form = EmployeeCreateModelForm(user=request.user)
        return render(request, "employee_add.html", {"form": form, "title": title, "back_url": "/user/list/"})

    form = EmployeeCreateModelForm(data=request.POST, user=request.user)
    if form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("/user/list/")
    return render(request, "employee_add.html", {"form": form, "title": title, "back_url": "/user/list/"})


def user_edit(request, nid):
    """ 编辑员工（T3）：手机号唯一校验会自动排除自己 """
    row_object = request.user.scope_employees().filter(id=nid).first()
    if row_object is None:
        return redirect("/user/list/")

    title = "编辑员工：{}".format(row_object.name)
    if request.method == "GET":
        form = EmployeeModelForm(instance=row_object, user=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/user/list/"})

    form = EmployeeModelForm(data=request.POST, instance=row_object, user=request.user)
    if form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("/user/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/user/list/"})


@require_POST
@validate_post_id
def user_delete(request):
    """ 删除员工：仅接受 POST，并按当前账号的数据范围校验对象。 """
    nid = request.POST.get("nid")
    if nid:
        try:
            request.user.scope_employees().filter(id=nid).delete()
        except ProtectedError:
            return render(request, "error_alert.html", {
                "error": "该员工仍有关联账号、考勤或薪资记录，不能直接删除；请保留档案或先处理关联。",
                "back_url": "/user/list/",
            })
    return redirect("/user/list/")
