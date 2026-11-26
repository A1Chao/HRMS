"""
日期/月份共用规则：业务表单、Django 后台和数据审计使用同一套解析方式。
工资按“年月”与入职年月比较，不能把入职当月工资误当作发生在该月 1 日。
"""
import datetime
import re

from django.core.exceptions import ValidationError
from django.utils import timezone


MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def parse_salary_month(value):
    """严格解析 YYYY-MM，返回该年月对应的 date；非法格式或年份用 ValidationError 回显。"""
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise ValidationError("月份格式应为 YYYY-MM，例如 2025-01")
    year, month = map(int, value.split("-"))
    try:
        return datetime.date(year, month, 1)
    except ValueError as exc:
        raise ValidationError("月份年份必须在 0001 到 9999 之间") from exc


def employee_hire_date(employee):
    """把员工入职时间转换为当前配置时区中的日期，兼容带/不带时区的时间。"""
    joined = employee.create_time
    if timezone.is_aware(joined):
        joined = timezone.localtime(joined)
    return joined.date()
