"""新增员工时可选开通账号；档案、账号、角色绑定在同一个事务中完成。

只创建普通员工账号，不接管同名账号、不授予管理权限、不生成虚构工资。
这里复核权限和默认角色，不能把前端复选框当作授权凭据。
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from contextlib import nullcontext

from app01 import models
from app01.utils.account_registration import registration_lock, registration_name, registration_username
from app01.utils.account_authorization import permissions_within
from app01.utils.employee_accounts import REGULAR_EMPLOYEE_ROLE, REQUIRED_READ_ONLY_PERMISSIONS


def can_open_employee_login(actor):
    return bool(actor and actor.is_authenticated and actor.is_active
                and actor.has_perm_code("user.edit")
                and permissions_within(actor, REQUIRED_READ_ONLY_PERMISSIONS))


def ordinary_employee_role(actor, *, lock=False):
    if not can_open_employee_login(actor):
        raise ValidationError("当前账号没有开通员工登录账号的授权，请联系系统管理员。")
    query = models.Role.objects.select_for_update() if lock else models.Role.objects
    role = query.filter(name=REGULAR_EMPLOYEE_ROLE).first()
    if role is None:
        raise ValidationError("缺少普通员工角色，请先由系统管理员完成权限初始化。")
    codes = set(role.permissions.values_list("code", flat=True))
    if codes != REQUIRED_READ_ONLY_PERMISSIONS:
        raise ValidationError("普通员工角色的权限偏离默认只读基线，请管理员核查后再开通账号。")
    return role


def new_account_candidate(username, name, mobile, password):
    """使用 Django 用户名/密码校验；只校验新口令，绝不触碰既有密码。"""
    username = registration_username(username)
    registration_name(name, username=username)
    candidate = models.User(
        username=username, real_name=name, phone=mobile, is_active=True,
        is_staff=False, is_superuser=False, managed_department=None,
    )
    candidate.full_clean(exclude=["employee", "password"])
    validate_password(password, candidate)
    return candidate


def save_employee_and_optional_login(employee, actor, *, username=None, password=None):
    if employee.pk is not None:
        raise ValidationError("此入口仅用于新建员工，不能用于修改既有档案或账号。")
    try:
        with transaction.atomic(), (registration_lock() if username is not None else nullcontext()):
            # 使用新的账号实例复核当前权限，避免复用页面请求早期的权限缓存。
            operator = models.User.objects.select_for_update().get(pk=actor.pk)
            if not operator.is_active or not operator.has_perm_code("employee.edit"):
                raise ValidationError("当前账号没有新增员工权限。")
            if not operator.is_superuser:
                department_id = operator.managed_department_id
                if not department_id and operator.employee_id:
                    department_id = operator.employee.department_id
                if not department_id or employee.department_id != department_id:
                    raise ValidationError("只能在当前授权部门内新增员工。")
            role = ordinary_employee_role(operator, lock=True) if username is not None else None
            account = new_account_candidate(username, employee.name, employee.mobile, password) if role else None
            employee.full_clean()
            employee.save()
            if account:
                account.employee = employee
                account.set_password(password)
                account.save()
                account.roles.set([role])
            return employee
    except models.User.DoesNotExist:
        raise ValidationError("当前操作账号已不存在，请重新登录。") from None
    except IntegrityError:
        # 捕获在 atomic 外，保证事务完整回滚后才能重新展示表单。
        raise ValidationError("保存冲突：手机号或登录名已被使用，请检查后重试；本次未创建员工或账号。") from None
