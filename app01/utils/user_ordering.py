"""系统账号按实际授权优先排序，而不是只看角色名称或职位。

超级管理员 > Django 后台账号 > 全局通配授权 > 账号/角色管理授权 >
其他写入授权 > 只读授权 > 无权限。同级按不同权限数量降序，再按 ID 稳定排序。
Exists 与 distinct Count 在数据库中完成，多个角色共享权限不重复计数，
不会因为 Python 排序破坏分页，也不会增加逐用户查询。
"""
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When

from app01 import models


def permission_priority_ordering(queryset):
    permissions = models.Permission.objects.filter(role__users=OuterRef("pk"))
    queryset = queryset.annotate(
        _has_global_permission=Exists(permissions.filter(code="*")),
        _has_account_management=Exists(permissions.filter(code__in=(
            "user.edit", "user.*", "role.edit", "role.*",
        ))),
        _has_write_permission=Exists(permissions.filter(
            Q(code__endswith=".edit") | Q(code__endswith=".*")
        )),
        _has_any_permission=Exists(permissions),
        _distinct_permission_count=Count("roles__permissions", distinct=True),
    ).annotate(
        _permission_priority=Case(
            When(is_superuser=True, then=Value(0)),
            When(is_staff=True, then=Value(1)),
            When(_has_global_permission=True, then=Value(2)),
            When(_has_account_management=True, then=Value(3)),
            When(_has_write_permission=True, then=Value(4)),
            When(_has_any_permission=True, then=Value(5)),
            default=Value(6), output_field=IntegerField(),
        )
    )
    return queryset.order_by("_permission_priority", "-_distinct_permission_count", "id")
