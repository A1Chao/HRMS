"""
只读数据审计：检查表、外键、资料、业务时间和派生值，输出发现而不是自动修复。
非法记录应逐条报告；不要因一条坏月份使整批审计中断。
"""
from collections import Counter

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from app01 import models

from app01.utils.domain_validation import employee_hire_date, parse_salary_month


def audit_hrms_data():
    findings = []

    def report(code, severity, model, pk=None, **details):
        findings.append({"code": code, "severity": severity, "model": model,
                         "id": pk, "details": details})

    tables = connection.introspection.table_names()
    expected_tables = {model._meta.db_table for model in apps.get_models(include_auto_created=True)
                       if model._meta.managed and not model._meta.proxy}
    expected_tables.add(MigrationRecorder.Migration._meta.db_table)
    table_counts, orphan_count, foreign_keys = {}, 0, 0
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute("SELECT COUNT(*) FROM {}".format(quote(table)))
            table_counts[table] = cursor.fetchone()[0]
            constraints = connection.introspection.get_constraints(cursor, table)
            for constraint_name, constraint in constraints.items():
                target = constraint.get("foreign_key")
                columns = constraint.get("columns", [])
                if not target or len(columns) != 1:
                    continue
                foreign_keys += 1
                target_table, target_column = target
                column = columns[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p ON c.{fk}=p.{pk} "
                    "WHERE c.{fk} IS NOT NULL AND p.{pk} IS NULL".format(
                        child=quote(table), parent=quote(target_table), fk=quote(column), pk=quote(target_column)
                    )
                )
                count = cursor.fetchone()[0]
                if count:
                    orphan_count += count
                    report("orphan_foreign_key", "error", table, constraint=constraint_name,
                           target=target_table, count=count)
    for table in sorted(set(tables) - expected_tables):
        report("unmanaged_table", "warning", table, action="review; do not automatically drop")
    for table in sorted(expected_tables - set(tables)):
        report("missing_managed_table", "error", table)

    employees = list(models.Employee.objects.select_related("department").order_by("id"))
    employee_map = {employee.id: employee for employee in employees}
    for employee in employees:
        try:
            employee.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            report("invalid_employee_fields", "error", "Employee", employee.id,
                   fields=sorted(exc.message_dict))
        if not (employee.position or "").strip():
            report("missing_position", "warning", "Employee", employee.id, name=employee.name)

    users = list(models.User.objects.select_related("employee", "managed_department")
                 .prefetch_related("roles").order_by("id"))
    for user in users:
        roles = set(user.role_names)
        if "部门管理员" in roles and not user.is_superuser:
            if not user.managed_department_id:
                report("manager_without_scope", "error", "User", user.id, username=user.username)
            if not user.employee_id:
                report("manager_without_employee", "error", "User", user.id, username=user.username)
        if "普通员工" in roles and not user.employee_id:
            report("staff_without_employee", "error", "User", user.id, username=user.username)
        if user.employee_id:
            employee = employee_map.get(user.employee_id)
            if employee is not None and user.phone and user.phone != employee.mobile:
                report("account_employee_phone_difference", "info", "User", user.id,
                       username=user.username, employee_id=employee.id,
                       note="may be an independently maintained number; review, never blindly overwrite")
            if employee is not None and not user.phone:
                report("missing_linked_phone", "warning", "User", user.id, username=user.username)
        if not (user.email or "").strip():
            report("missing_test_email", "info", "User", user.id, username=user.username)

    chronology = Counter()
    for attendance in models.Attendance.objects.select_related("employee"):
        if attendance.status != attendance.compute_status():
            report("attendance_status_difference", "error", "Attendance", attendance.id)
        if attendance.date < employee_hire_date(attendance.employee):
            chronology[attendance.employee_id] += 1
    for salary in models.Salary.objects.select_related("employee"):
        try:
            month = parse_salary_month(salary.month)
        except ValidationError:
            report("invalid_salary_month", "error", "Salary", salary.id)
            continue
        if salary.total != salary.compute_total():
            report("salary_total_difference", "error", "Salary", salary.id)
        joined = employee_hire_date(salary.employee)
        if (month.year, month.month) < (joined.year, joined.month):
            chronology[salary.employee_id] += 1
    for employee_id, count in sorted(chronology.items()):
        report("business_records_before_hire", "warning", "Employee", employee_id,
               name=employee_map[employee_id].name, affected_records=count)
    for task in models.Task.objects.all():
        try:
            task.full_clean(validate_unique=False, validate_constraints=True)
        except ValidationError as exc:
            report("invalid_task_state", "error", "Task", task.id, fields=sorted(exc.message_dict))

    severity_counts = dict(Counter(finding["severity"] for finding in findings))
    return {
        "database": connection.settings_dict["NAME"],
        "table_count": len(tables), "table_counts": table_counts,
        "foreign_keys_checked": foreign_keys, "foreign_key_orphans": orphan_count,
        "normal_framework_tables": sorted(table for table in tables if not table.startswith("app01_")),
        "users": len(users), "employees": len(employees),
        "employee_positions_missing": sum(not (employee.position or "").strip() for employee in employees),
        "department_managers_without_employee": sum(
            "部门管理员" in user.role_names and not user.employee_id for user in users
        ),
        "severity_counts": severity_counts, "findings": findings,
        "note": "Django auth/session/migration and M2M tables are normal; primary-key gaps are not corruption.",
    }
