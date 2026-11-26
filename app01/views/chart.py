"""
数据统计可视化（T10）：ECharts + AJAX 取数

    GET /chart/                 图表页（两个图表容器）
    GET /chart/depart/data/     部门人数分布（饼图）JSON
    GET /chart/salary/data/     各月薪资趋势（折线图）JSON

后端只用 ORM 聚合（values + annotate）出数据，前端 ECharts 通过 $.getJSON 拿数据渲染，
不在模板里拼接数据，页面和接口解耦。

新手阅读：ECharts 数据接口（T10）。后端输出普通 JSON，图表外观在 templates/chart.html 设置。
部门人数用 Count，工资趋势用 Sum；先限制员工范围，再聚合，不能泄露跨部门统计。
"""
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from app01 import models


@never_cache
def chart(request):
    """ 图表页 """
    return render(request, "chart.html")


@never_cache
def chart_depart_data(request):
    """ 部门人数分布：按部门分组统计员工数 """
    # 统计员工档案，而不是登录账号；包含经理、无登录账号员工及测试员工。
    # superuser 看全公司，部门管理员仍只能看自己的负责部门。
    employees = request.user.scope_employees()
    rows = (
        employees.values("department__title")
        .annotate(value=Count("id"))
        .order_by("-value", "department__title")
    )
    data = [
        {"name": row["department__title"] or "未分配", "value": row["value"]}
        for row in rows
    ]
    # 只增加一条范围受限的明细查询，前端用文字列出成员，便于核对是否漏人。
    members = {}
    for row in employees.order_by("department_id", "id").values("department__title", "name"):
        members.setdefault(row["department__title"] or "未分配", []).append(row["name"])
    return JsonResponse({
        "status": True, "data": data,
        "total": sum(row["value"] for row in data), "members": members,
    })


@never_cache
def chart_salary_data(request):
    """ 各月薪资趋势：按月汇总实发工资 """
    rows = (
        models.Salary.objects
        .filter(employee__in=request.user.scope_employees())
        .values("month")
        .annotate(value=Sum("total"))
        .order_by("month")
    )
    data = {
        "months": [row["month"] for row in rows],
        "totals": [float(row["value"] or 0) for row in rows],
    }
    return JsonResponse({"status": True, "data": data})
