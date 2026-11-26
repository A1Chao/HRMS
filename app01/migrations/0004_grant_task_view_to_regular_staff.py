from django.db import migrations


def grant_task_view_to_regular_staff(apps, schema_editor):
    """让已有普通员工角色也能查看并完成派发给自己的任务。"""
    Permission = apps.get_model("app01", "Permission")
    Role = apps.get_model("app01", "Role")
    permission = Permission.objects.filter(code="task.view").first()
    role = Role.objects.filter(name="普通员工").first()
    if permission is not None and role is not None:
        role.permissions.add(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("app01", "0003_alter_task_options_task_completed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(grant_task_view_to_regular_staff, migrations.RunPython.noop),
    ]
