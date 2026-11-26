"""
员工展示排序：SQL Case 按职位名称推断展示层级，同层级再按 ID 稳定排序。
这里不是登录权限、工资等级或正式人事职级；组织可调整 LEADERSHIP_GROUPS。
"""
from django.db.models import Case, IntegerField, Q, Value, When


# Keep title rules here so an organisation can adapt its display hierarchy.
LEADERSHIP_GROUPS = (
    (0, ("董事长", "总裁", "总经理", "首席执行官", "首席财务官", "首席技术官", "首席运营官"),
        ("CEO", "CFO", "CTO", "COO")),
    (10, ("总监", "director"), ()),
    (20, ("经理", "部长", "负责人", "manager"), ()),
    (30, ("主管", "主任", "组长", "科长", "supervisor"), ()),
)


def leadership_priority():
    """生成 SQL Case 排序表达式，不改员工数据，也不赋予登录权限。"""
    non_leadership = Q()
    for keyword in ("助理", "秘书", "assistant", "secretary"):
        non_leadership |= Q(position__icontains=keyword)
    rules = []
    for priority, keywords, exact_titles in LEADERSHIP_GROUPS:
        condition = Q()
        for keyword in keywords:
            condition |= Q(position__icontains=keyword)
        for title in exact_titles:
            condition |= Q(position__iexact=title)
        rules.append(When(condition & ~non_leadership, then=Value(priority)))
    return Case(*rules, default=Value(100), output_field=IntegerField())
