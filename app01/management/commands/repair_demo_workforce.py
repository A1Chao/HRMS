"""
新手阅读：测试员工资料修复入口，默认只预演，--apply 才应用。
先检查档案确实是测试数据；真实人事资料和合法的主键空隙不能当作垃圾清理。
"""
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from app01.utils.demo_workforce import repair_demo_workforce


class Command(BaseCommand):
    help = "预演补齐测试员工职位、管理员档案和缺失资料；实际写入需 --apply --allow-demo。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--allow-demo", action="store_true", help="明确允许生成虚构测试资料，不能用于真实人员。")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            result = repair_demo_workforce(apply=options["apply"], allow_demo=options["allow_demo"])
        except ValidationError as exc:
            raise CommandError("；".join(exc.messages)) from exc
        if options["json"]:
            self.stdout.write(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        else:
            self.stdout.write("{}：新增员工 {}，更新员工 {}，更新账号 {}。".format(
                "APPLY" if result.applied else "DRY-RUN", result.created_employees,
                result.updated_employees, result.updated_users,
            ))
            for row in result.rows:
                self.stdout.write("{} {} {} {}".format(row["model"], row["action"], row.get("id", row.get("username")), row.get("fields", [])))
            self.stdout.write("测试资料不是实际个人信息；不重置密码、不改角色权限、不删除业务记录。")
