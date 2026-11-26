"""
考勤管理（T6）：CRUD + 考勤状态自动计算 + 按员工/日期筛选

考勤状态（按时 / 迟到 / 早退 / 缺勤）不让人手动选，
由 Attendance.save() 根据上下班时间自动算出来（规则见 models.py）。

新手阅读：考勤 CRUD（T6）。列表按姓名/日期过滤；写入字段只有员工、日期和打卡时间。
考勤状态不是手动下拉字段，而是 Attendance.save() 根据打卡时间计算。
"""
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from app01.utils.request_validation import validate_post_id

from app01 import models
from app01.utils.form import AttendanceModelForm
from app01.utils.pagination import Pagination


def attendance_list(request):
    """ 考勤列表：按员工姓名、考勤日期筛选 + 分页 """
    q = request.GET.get("q", "").strip()
    date = request.GET.get("date", "").strip()

    # 数据范围与性能：只查询授权员工，并一次预载员工及其部门。
    queryset = (
        models.Attendance.objects
        .filter(employee__in=request.user.scope_employees())
        .select_related("employee__department")
        .order_by("-date", "employee_id")
    )
    if q:
        queryset = queryset.filter(employee__name__contains=q)
    if date:
        try:
            parsed_date = parse_date(date) if len(date) == 10 else None
        except ValueError:
            parsed_date = None
        if parsed_date is None:
            return HttpResponseBadRequest("考勤筛选日期格式应为 YYYY-MM-DD，且必须是真实日期")
        queryset = queryset.filter(date=parsed_date)

    page_object = Pagination(request, queryset)
    context = {
        "queryset": page_object.page_queryset,
        "page_string": page_object.page_html(),
        "q": q,
        "date": date,
        "total_count": page_object.total_count,
    }
    return render(request, "attendance_list.html", context)


def attendance_add(request):
    """ 新增考勤：状态在保存时按上下班时间自动计算 """
    title = "新增考勤"
    if request.method == "GET":
        form = AttendanceModelForm(user=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/attendance/list/"})

    form = AttendanceModelForm(data=request.POST, user=request.user)
    if form.is_valid():
        form.save()
        return redirect("/attendance/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/attendance/list/"})


def attendance_edit(request, nid):
    """ 编辑考勤 """
    row_object = (
        models.Attendance.objects
        .filter(employee__in=request.user.scope_employees(), id=nid)
        .select_related("employee__department")
        .first()
    )
    if row_object is None:
        return redirect("/attendance/list/")

    title = "编辑考勤：{} {}".format(row_object.employee.name, row_object.date)

    if request.method == "GET":
        form = AttendanceModelForm(instance=row_object, user=request.user)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/attendance/list/"})

    form = AttendanceModelForm(data=request.POST, instance=row_object, user=request.user)
    if form.is_valid():
        form.save()
        return redirect("/attendance/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/attendance/list/"})


@require_POST
@validate_post_id
def attendance_delete(request):
    """ 删除考勤记录：仅接受 POST，并按员工数据范围校验。 """
    nid = request.POST.get("nid")
    if nid:
        models.Attendance.objects.filter(
            employee__in=request.user.scope_employees(), id=nid
        ).delete()
    return redirect("/attendance/list/")