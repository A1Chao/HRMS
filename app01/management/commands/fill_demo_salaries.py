"""补齐测试薪资的显式入口；不调用本命令就不会自动编造工资。"""
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from app01.utils.demo_salaries import fill_demo_salaries


class Command(BaseCommand):
    help = "预演补齐测试员工 2025-01~03 的缺失薪资；写入需 --apply --allow-demo。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--allow-demo", action="store_true", help="确认仅生成虚构的测试工资。")
        parser.add_argument("--employee-id", action="append", type=int, help="限定员工 ID，可重复指定。")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            result = fill_demo_salaries(
                apply=options["apply"], allow_demo=options["allow_demo"],
                employee_ids=options["employee_id"],
            )
        except ValidationError as exc:
            raise CommandError("；".join(exc.messages)) from exc
        if options["json"]:
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            self.stdout.write("{}：新增 {}，保留已有 {}，跳过入职前月份 {}。".format(
                "APPLY" if result["applied"] else "DRY-RUN", result["created"],
                result["skipped_existing"], result["skipped_before_hire"],
            ))
            for row in result["rows"]:
                self.stdout.write("{name}（ID {employee_id}）{month}：演示实发 {total}".format(**row))
            self.stdout.write("仅测试工资，不是实际收入；不覆盖已有工资、不修改密码。")
