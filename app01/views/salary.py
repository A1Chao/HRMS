"""
薪资管理（T7）：CRUD + 按月份/员工筛选 + 选定月份薪资总额统计

实发工资 = 基本工资 + 奖金 - 扣款，由 Salary.save() 统一计算（total 字段）。
统计用 ORM 聚合：Salary.objects.filter(...).aggregate(Sum("total"))。

新手阅读：薪资 CRUD 和筛选总额（T7）。月份、姓名过滤后对全部匹配记录聚合，而不是只加当前页。
实发金额在 Salary.save() 计算，Salary.clean() 检查入职月份和总额容量。
"""
from decimal import Decimal

from django.db.models import Sum
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01.utils.request_validation import validate_post_id

from app01 import models
from app01.utils.form import SalaryModelForm
from app01.utils.pagination import Pagination


def salary_list(request):
    """ 薪资列表：按月份、员工姓名筛选；底部显示“筛选结果的实发工资总额” """
    q = request.GET.get("q", "").strip()
    month = request.GET.get("month", "").strip()

    # 数据范围与性能：只查询授权员工，并一次预载员工及其部门。
    scoped_queryset = (
        models.Salary.objects
        .filter(employee__in=request.user.scope_employees())
        .select_related("employee__department")
        .order_by("-month", "employee_id")
    )
    queryset = scoped_queryset
    if q:
        queryset = queryset.filter(employee__name__contains=q)
    if month:
        queryset = queryset.filter(month=month)

    # T7 的统计需求：汇总当前筛选结果的实发工资（空结果时为 None，显示成 0.00）
    # 统计对象是筛选后的完整集合，不能仅对 page_queryset 当前页求和。
    total_salary = queryset.aggregate(total=Sum("total"))["total"] or Decimal("0.00")

    page_object = Pagination(request, queryset)

    # 月份下拉框的选项：数据库里已经出现过的月份
    months = scoped_queryset.order_by().values_list("month", flat=True).distinct().order_by("-month")

    context = {
        "queryset": page_object.page_queryset,
        "page_string": page_object.page_html(),
        "q": q,
        "month": month,
        "months": months,
        "total_salary": total_salary,
        "total_count": page_object.total_count,
    }
    return render(request, "salary_list.html", context)


def salary_add(request):
    """ 新增薪资：实发工资自动计算 """
    title = "新增薪资"
    if request.method == "GET":
        form = SalaryModelForm(user=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/salary/list/"})

    form = SalaryModelForm(data=request.POST, user=request.user)
    if form.is_valid():
        form.save()
        return redirect("/salary/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/salary/list/"})


def salary_edit(request, nid):
    """ 编辑薪资 """
    row_object = (
        models.Salary.objects
        .filter(employee__in=request.user.scope_employees(), id=nid)
        .select_related("employee__department")
        .first()
    )
    if row_object is None:
        return redirect("/salary/list/")

    title = "编辑薪资：{} {}".format(row_object.employee.name, row_object.month)

    if request.method == "GET":
        form = SalaryModelForm(instance=row_object, user=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/salary/list/"})

    form = SalaryModelForm(data=request.POST, instance=row_object, user=request.user)
    if form.is_valid():
        form.save()
        return redirect("/salary/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/salary/list/"})


@require_POST
@validate_post_id
def salary_delete(request):
    """ 删除薪资记录：仅接受 POST，并按员工数据范围校验。 """
    nid = request.POST.get("nid")
    if nid:
        models.Salary.objects.filter(
            employee__in=request.user.scope_employees(), id=nid
        ).delete()
    return redirect("/salary/list/")