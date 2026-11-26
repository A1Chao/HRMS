"""
账号资料回退/补齐：只从明确关联的员工档案补空姓名和手机号。
用户单独填写的联系方式优先保留；不根据“看起来同名”猜测、换绑员工身份。
"""
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from app01 import models


def employee_profile_defaults(employee, *, real_name="", phone=""):
    """只从本次明确关联的员工补空资料，保留显式填写的姓名及联系方式。"""
    if employee is None:
        return {}
    values = {}
    if not (real_name or "").strip() and (employee.name or "").strip():
        values["real_name"] = employee.name.strip()
    if not (phone or "").strip() and (employee.mobile or "").strip():
        values["phone"] = employee.mobile.strip()
    return values


@dataclass(frozen=True)
class UserProfileRepairRow:
    user_id: int
    username: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class UserProfileRepairResult:
    applied: bool
    updated_count: int
    rows: tuple[UserProfileRepairRow, ...]


def repair_user_profiles(*, apply=False):
    """Preflight every source, then optionally fill blanks in one transaction.

    Only already-linked users are considered.  Passwords, usernames, roles,
    flags, employee links and managed departments are deliberately untouched.
    Returned audit rows contain field names, not phone numbers or password data.
    """
    with transaction.atomic():
        queryset = models.User.objects.filter(employee__isnull=False).select_related(
            "employee"
        ).order_by("id")
        if apply:
            queryset = queryset.select_for_update()
        plan = []
        errors = []
        for user in queryset:
            values = employee_profile_defaults(
                user.employee, real_name=user.real_name, phone=user.phone
            )
            for field_name, value in values.items():
                try:
                    values[field_name] = models.User._meta.get_field(field_name).clean(value, user)
                except ValidationError as exc:
                    errors.append("用户 ID {} 的 {} 来源无效：{}".format(
                        user.id, field_name, "; ".join(exc.messages)
                    ))
            if values:
                plan.append((user, values))
        if errors:
            raise ValidationError(errors)

        rows = []
        for user, values in plan:
            fields = tuple(values)
            if apply:
                for field_name, value in values.items():
                    setattr(user, field_name, value)
                user.save(update_fields=list(fields))
            rows.append(UserProfileRepairRow(user.id, user.username, fields))
        return UserProfileRepairResult(bool(apply), len(rows), tuple(rows))
