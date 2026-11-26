"""
新手阅读：只读检查入口；--json 输出结构化结果，--output 另存报告。
本命令不修复、不删表、不重排主键、不修改密码。
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from app01.utils.database_integrity import audit_hrms_data


class Command(BaseCommand):
    help = "只读审计账号/员工/职位/业务日期/外键和表结构，不清库、不重排主键。"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="输出完整 JSON 审计。")
        parser.add_argument("--output", help="保存到不存在的 JSON 文件；禁止覆盖已有记录。")

    def handle(self, *args, **options):
        result = audit_hrms_data()
        if options["output"]:
            path = Path(options["output"])
            try:
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
            except (OSError, ValueError) as exc:
                raise CommandError("无法独占创建审计文件：{}".format(path)) from exc
        if options["json"]:
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            self.stdout.write("数据库 {}：{} 张表，{} 个账号，{} 个员工。".format(
                result["database"], result["table_count"], result["users"], result["employees"]
            ))
            self.stdout.write("检查 {} 个外键；孤立引用 {}；职位缺失 {}；管理员缺员工档案 {}。".format(
                result["foreign_keys_checked"], result["foreign_key_orphans"],
                result["employee_positions_missing"], result["department_managers_without_employee"]
            ))
            self.stdout.write("审计计数：{}".format(result["severity_counts"]))
            for finding in result["findings"]:
                self.stdout.write("{} {} {} ID {}".format(
                    finding["severity"].upper(), finding["code"], finding["model"], finding["id"]
                ))
            self.stdout.write(result["note"])
