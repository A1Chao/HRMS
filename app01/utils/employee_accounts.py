"""
员工账号同步服务：先检查姓名/身份冲突，再预览或在事务中应用变更。
姓名是本项目约定的登录名，但同名不能证明同一身份；不能强行接管占名账号。
导出的新账号 CSV 可能包含初始密码，只供受控交接，不能提交 Git 或公开分享。
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from app01 import models
from app01.utils.user_profiles import employee_profile_defaults


REGULAR_EMPLOYEE_ROLE = "普通员工"
REQUIRED_READ_ONLY_PERMISSIONS = frozenset({
    "employee.view",
    "attendance.view",
    "salary.view",
    "task.view",
})
DEFAULT_PASSWORD_ENV = "EMPLOYEE_ACCOUNT_INITIAL_PASSWORD"
PRESERVED_PASSWORD_MARKER = "保留原密码"

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_UNCHANGED = "unchanged"


class EmployeeAccountSyncError(Exception):
    """The requested synchronization is unsafe or could not be completed."""


@dataclass(frozen=True)
class AccountSyncRow:
    employee_id: int
    employee_name: str
    user_id: Optional[int]
    username: str
    action: str
    password_note: str


@dataclass(frozen=True)
class EmployeeAccountSyncResult:
    applied: bool
    created_count: int
    updated_count: int
    unchanged_count: int
    rows: tuple[AccountSyncRow, ...]
    report_path: Optional[Path]


@dataclass(frozen=True)
class _PlanItem:
    employee: models.Employee
    user: Optional[models.User]
    action: str


@dataclass(frozen=True)
class _SyncPlan:
    items: tuple[_PlanItem, ...]
    regular_role: Optional[models.Role]

    @property
    def create_count(self):
        return sum(item.action == ACTION_CREATE for item in self.items)


# initial_password 可能是新账号交接口令；报告不是可公开的普通测试日志。
CSV_COLUMNS = (
    "employee_id",
    "employee_name",
    "user_id",
    "username",
    "action",
    "initial_password",
)


def _validate_strict_username(name, username_field):
    if not name:
        raise ValidationError("姓名不能为空")
    if name != name.strip():
        raise ValidationError("姓名前后不能包含空白字符")
    normalized = models.User.normalize_username(name)
    if normalized != name:
        raise ValidationError("姓名经用户名规范化后发生变化")
    username_field.run_validators(name)


def _load_plan(*, lock=False):
    employee_queryset = models.Employee.objects.order_by("id")
    user_queryset = models.User.objects.order_by("id")
    role_queryset = models.Role.objects
    if lock:
        employee_queryset = employee_queryset.select_for_update()
        user_queryset = user_queryset.select_for_update()
        role_queryset = role_queryset.select_for_update()

    employees = list(employee_queryset)
    users = list(user_queryset)
    bound_by_employee_id = {
        user.employee_id: user for user in users if user.employee_id is not None
    }

    errors = []
    username_field = models.User._meta.get_field("username")
    employees_by_folded_name = {}
    for employee in employees:
        try:
            _validate_strict_username(employee.name, username_field)
        except ValidationError as exc:
            errors.append(
                "员工 ID {} 的姓名 {!r} 不能作为严格用户名：{}".format(
                    employee.id, employee.name, "; ".join(exc.messages)
                )
            )
        employees_by_folded_name.setdefault(employee.name.casefold(), []).append(employee)

    for same_name_employees in employees_by_folded_name.values():
        if len(same_name_employees) > 1:
            errors.append(
                "员工姓名冲突 {!r}，员工 ID：{}；禁止自动添加后缀".format(
                    same_name_employees[0].name,
                    [employee.id for employee in same_name_employees],
                )
            )

    users_by_folded_username = {}
    for user in users:
        users_by_folded_username.setdefault(user.username.casefold(), []).append(user)

    items = []
    for employee in employees:
        bound_user = bound_by_employee_id.get(employee.id)
        occupying_users = [
            user
            for user in users_by_folded_username.get(employee.name.casefold(), [])
            if bound_user is None or user.id != bound_user.id
        ]
        if occupying_users:
            errors.append(
                "员工 ID {} 要求的用户名 {!r} 已被账号 ID {} 占用；禁止绑定占名账号".format(
                    employee.id,
                    employee.name,
                    [user.id for user in occupying_users],
                )
            )

        profile_values = employee_profile_defaults(
            employee,
            real_name=bound_user.real_name if bound_user else "",
            phone=bound_user.phone if bound_user else "",
        )
        for field_name, value in profile_values.items():
            try:
                models.User._meta.get_field(field_name).clean(value, bound_user)
            except ValidationError as exc:
                errors.append("员工 ID {} 的 {} 来源无效：{}".format(
                    employee.id, field_name, "; ".join(exc.messages)
                ))

        if bound_user is None:
            action = ACTION_CREATE
        elif (
            bound_user.username != employee.name
            or bound_user.real_name != employee.name
            or profile_values
        ):
            action = ACTION_UPDATE
        else:
            action = ACTION_UNCHANGED
        items.append(_PlanItem(employee=employee, user=bound_user, action=action))

    regular_role = None
    if any(item.action == ACTION_CREATE for item in items):
        regular_role = role_queryset.filter(name=REGULAR_EMPLOYEE_ROLE).first()
        if regular_role is None:
            errors.append("缺少“{}”角色，拒绝创建无权限基线的新账号".format(REGULAR_EMPLOYEE_ROLE))
        else:
            role_codes = set(
                regular_role.permissions.values_list("code", flat=True)
            )
            missing_codes = sorted(REQUIRED_READ_ONLY_PERMISSIONS - role_codes)
            unsafe_codes = sorted(
                code for code in role_codes if not code.endswith(".view")
            )
            if missing_codes:
                errors.append(
                    "“{}”角色缺少必要只读权限：{}".format(
                        REGULAR_EMPLOYEE_ROLE, ", ".join(missing_codes)
                    )
                )
            if unsafe_codes:
                errors.append(
                    "“{}”角色包含非只读权限，拒绝给新员工隐式提权：{}".format(
                        REGULAR_EMPLOYEE_ROLE, ", ".join(unsafe_codes)
                    )
                )

    if errors:
        raise EmployeeAccountSyncError("账号同步预检失败：\n- " + "\n- ".join(errors))
    return _SyncPlan(items=tuple(items), regular_role=regular_role)


def _validate_initial_password(plan, initial_password, *, allow_weak):
    if not plan.create_count:
        return
    if not isinstance(initial_password, str) or not initial_password:
        raise EmployeeAccountSyncError("存在待创建账号，但初始密码环境变量为空")
    if allow_weak:
        return

    errors = []
    for item in plan.items:
        if item.action != ACTION_CREATE:
            continue
        candidate = models.User(
            username=item.employee.name,
            real_name=item.employee.name,
            employee=item.employee,
        )
        try:
            validate_password(initial_password, user=candidate)
        except ValidationError as exc:
            errors.extend(exc.messages)
    if errors:
        unique_errors = list(dict.fromkeys(errors))
        raise EmployeeAccountSyncError(
            "初始密码未通过 Django 强密码校验：{}。若这是经授权的一次性弱密码初始化，"
            "必须显式使用 --apply --allow-weak；全局密码校验不会被修改。".format(
                "; ".join(unique_errors)
            )
        )


def _preview_rows(plan, initial_password):
    return tuple(
        AccountSyncRow(
            employee_id=item.employee.id,
            employee_name=item.employee.name,
            user_id=item.user.id if item.user else None,
            username=item.employee.name,
            action=item.action,
            password_note=(
                initial_password if item.action == ACTION_CREATE else PRESERVED_PASSWORD_MARKER
            ),
        )
        for item in plan.items
    )


def _apply_plan(plan, initial_password):
    rows = []
    for item in plan.items:
        employee = item.employee
        user = item.user
        if item.action == ACTION_CREATE:
            user = models.User(
                username=employee.name,
                real_name=employee.name,
                phone=employee_profile_defaults(employee).get("phone", ""),
                employee=employee,
                is_active=True,
                is_staff=False,
                is_superuser=False,
            )
            user.set_password(initial_password)
            user.save()
            user.roles.set([plan.regular_role])
        elif item.action == ACTION_UPDATE:
            user.username = employee.name
            user.real_name = employee.name
            fields = ["username", "real_name"]
            values = employee_profile_defaults(
                employee, real_name=user.real_name, phone=user.phone
            )
            for field_name, value in values.items():
                setattr(user, field_name, value)
                fields.append(field_name)
            user.save(update_fields=fields)

        rows.append(AccountSyncRow(
            employee_id=employee.id,
            employee_name=employee.name,
            user_id=user.id,
            username=employee.name,
            action=item.action,
            password_note=(
                initial_password if item.action == ACTION_CREATE else PRESERVED_PASSWORD_MARKER
            ),
        ))
    return tuple(rows)


def _write_report_exclusive(path, rows):
    path = Path(path)
    if not path.parent.is_dir():
        raise EmployeeAccountSyncError("CSV 输出目录不存在：{}".format(path.parent))

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        file_descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise EmployeeAccountSyncError("CSV 输出文件已存在，拒绝覆盖：{}".format(path)) from exc
    except OSError as exc:
        raise EmployeeAccountSyncError("无法创建 CSV 报告 {}：{}".format(path, exc)) from exc

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "employee_id": row.employee_id,
                    "employee_name": row.employee_name,
                    "user_id": "" if row.user_id is None else row.user_id,
                    "username": row.username,
                    "action": row.action,
                    "initial_password": row.password_note,
                })
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return path


def _remove_own_report(path, original_error):
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as cleanup_error:
        raise EmployeeAccountSyncError(
            "数据库提交失败，且无法清理本次生成的报告 {}：{}".format(
                path, cleanup_error
            )
        ) from original_error


def sync_employee_accounts(
    *,
    apply=False,
    initial_password=None,
    allow_weak=False,
    output_path=None,
):
    """Plan or apply strict employee-to-account synchronization.

    Args:
        apply: ``False`` performs a database dry-run; ``True`` writes changes.
        initial_password: Used only for newly created accounts.  Existing account
            password hashes are never changed or exported.
        allow_weak: Skip ``validate_password`` only for the new-account initial
            password in this call.  It does not alter Django's validators.
        output_path: Optional exclusive-create CSV path.  Existing files are
            never overwritten.  CSV is UTF-8 with BOM.
    """
    report_path = Path(output_path) if output_path is not None else None
    created_report = None

    try:
        if not apply:
            plan = _load_plan(lock=False)
            _validate_initial_password(plan, initial_password, allow_weak=allow_weak)
            rows = _preview_rows(plan, initial_password)
            if report_path is not None:
                created_report = _write_report_exclusive(report_path, rows)
        else:
            with transaction.atomic():
                plan = _load_plan(lock=True)
                _validate_initial_password(plan, initial_password, allow_weak=allow_weak)
                rows = _apply_plan(plan, initial_password)
                if report_path is not None:
                    created_report = _write_report_exclusive(report_path, rows)

        return EmployeeAccountSyncResult(
            applied=bool(apply),
            created_count=sum(row.action == ACTION_CREATE for row in rows),
            updated_count=sum(row.action == ACTION_UPDATE for row in rows),
            unchanged_count=sum(row.action == ACTION_UNCHANGED for row in rows),
            rows=rows,
            report_path=created_report,
        )
    except EmployeeAccountSyncError as exc:
        if apply and created_report is not None:
            _remove_own_report(created_report, exc)
        raise
    except Exception as exc:
        if apply and created_report is not None:
            _remove_own_report(created_report, exc)
        raise EmployeeAccountSyncError("账号同步失败：{}".format(exc)) from exc
