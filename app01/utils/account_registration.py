"""交互式新账号注册的统一重名校验；不清理或改名既有账号。

登录名已有数据库唯一约束；注册姓名还要核对现有账号的显示姓名/关联员工
姓名。MySQL 下用同一个 ContentType 行锁串行执行两个注册入口，避免两次
请求同时通过姓名检查。员工档案本身不强制姓名唯一；不开户仍可登记同名人。
"""
from contextlib import contextmanager

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, Q, Value
from django.db.models.functions import Coalesce, NullIf, Trim

from app01 import models


def registration_username(username):
    username = (username or "").strip()
    if models.User.normalize_username(username) != username:
        raise ValidationError("登录名规范化后会发生变化，请使用明确且未占用的登录名。")
    models.User._meta.get_field("username").clean(username, None)
    if models.User.objects.filter(username__iexact=username).exists():
        raise ValidationError("登录名已被注册，不能重名。")
    return username


def registration_name(real_name, *, username="", employee=None):
    name = (real_name or "").strip() or (employee.name.strip() if employee else "") or username
    if not name:
        return
    accounts = models.User.objects.annotate(
        _registration_display_name=Coalesce(
            NullIf(Trim("real_name"), Value("")),
            NullIf(Trim("employee__name"), Value("")),
            Trim("username"), output_field=CharField(),
        )
    )
    if accounts.filter(Q(_registration_display_name__iexact=name) | Q(username__iexact=name)).exists():
        raise ValidationError("该姓名已有登录账号，不能重复注册；请先核查已有账号。")


@contextmanager
def registration_lock():
    with transaction.atomic():
        # Django 迁移后的 post_migrate 已创建该记录，无需另外增加业务表。
        content_type = ContentType.objects.get_for_model(models.User)
        ContentType.objects.select_for_update().get(pk=content_type.pk)
        yield


def save_registered_user(user, save_relations):
    with registration_lock():
        registration_username(user.username)
        registration_name(user.real_name, username=user.username, employee=user.employee if user.employee_id else None)
        user.save()
        save_relations()
