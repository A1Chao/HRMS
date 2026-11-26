"""
新手阅读：资料补空入口，默认只列计划，--apply 才写入。
只信任明确绑定的 Employee；已有独立姓名或手机号不被盲目覆盖。
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from app01.utils.user_profiles import repair_user_profiles


class Command(BaseCommand):
    help = "仅从明确关联的员工档案补空姓名/手机号；默认预演，--apply 才写入。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="在一个事务中实际补齐资料。")

    def handle(self, *args, **options):
        try:
            result = repair_user_profiles(apply=options["apply"])
        except ValidationError as exc:
            raise CommandError("资料来源预检失败：" + "; ".join(exc.messages)) from exc
        for row in result.rows:
            self.stdout.write("用户 ID {}：{}".format(row.user_id, ", ".join(row.fields)))
        mode = "APPLY" if result.applied else "DRY-RUN"
        self.stdout.write("{}：待补齐/已补齐 {} 个账号；不修改密码、角色、部门或身份关联。".format(
            mode, result.updated_count
        ))
        if not result.applied:
            self.stdout.write("本次未写入数据库，确认后使用 --apply。")
