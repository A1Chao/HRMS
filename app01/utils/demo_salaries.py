"""仅补齐明确标记 is_demo=True 的测试员工薪资，不猜测真实工资。

默认月份与 init_data 保持相同：2025 年第一季度。所有写入在事务中完成，
已有员工/月记录原样保留，入职之前的月份跳过，也不会修改任何账号密码。
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from app01 import models
from app01.utils.domain_validation import employee_hire_date

DEMO_SALARY_MONTHS = ("2025-01", "2025-02", "2025-03")


def demo_salary_defaults(month_index):
    """init_data 与补齐命令共用的演示金额，避免两份默认工资规则漂移。"""
    return {
        "base_salary": Decimal("8000.00"),
        "bonus": Decimal(500 + 300 * month_index),
        "deduction": Decimal(100 * month_index),
    }


def fill_demo_salaries(*, apply=False, allow_demo=False, employee_ids=None):
    """先校验整批计划再写入；返回可用于预演/验收的明细。"""
    if bool(apply) != bool(allow_demo):
        raise ValidationError("实际补齐必须同时指定 --apply --allow-demo。")
    with transaction.atomic():
        query = models.Employee.objects.select_related("department").order_by("id")
        if employee_ids is None:
            query = query.filter(is_demo=True)
        else:
            query = query.filter(id__in=employee_ids)
        if apply:
            query = query.select_for_update()
        employees = list(query)
        if employee_ids is not None and {employee.id for employee in employees} != set(employee_ids):
            raise ValidationError("指定员工不存在，请确认员工 ID。")
        if any(not employee.is_demo for employee in employees):
            raise ValidationError("只能为已标记 is_demo=True 的测试员工生成虚构薪资。")
        existing = set(models.Salary.objects.filter(
            employee__in=employees, month__in=DEMO_SALARY_MONTHS,
        ).values_list("employee_id", "month"))
        records, rows = [], []
        skipped_existing = skipped_before_hire = 0
        for employee in employees:
            hire_month = employee_hire_date(employee).strftime("%Y-%m")
            for index, month in enumerate(DEMO_SALARY_MONTHS):
                if (employee.id, month) in existing:
                    skipped_existing += 1
                    continue
                if month < hire_month:
                    skipped_before_hire += 1
                    continue
                record = models.Salary(
                    employee=employee, month=month, **demo_salary_defaults(index),
                )
                record.full_clean()
                records.append(record)
                rows.append({
                    "employee_id": employee.id, "name": employee.name, "month": month,
                    "base_salary": str(record.base_salary), "bonus": str(record.bonus),
                    "deduction": str(record.deduction), "total": str(record.compute_total()),
                })
        if apply:
            for record in records:
                record.save()  # 复用模型的 Decimal 实发计算，不手工写 total。
        return {
            "applied": apply, "created": len(records), "skipped_existing": skipped_existing,
            "skipped_before_hire": skipped_before_hire, "rows": rows,
        }
