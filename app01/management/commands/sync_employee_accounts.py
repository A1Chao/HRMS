"""
新手阅读：员工到账号的显式同步入口。默认预演，--apply 才真正写入；--output 必填且不覆盖已有文件。
handle() 只负责参数/输出，实际身份检查和事务在 utils/employee_accounts.py。
"""
import os

from django.core.management.base import BaseCommand, CommandError

from app01.utils.employee_accounts import (
    DEFAULT_PASSWORD_ENV,
    EmployeeAccountSyncError,
    sync_employee_accounts,
)


class Command(BaseCommand):
    help = (
        "按员工姓名严格同步登录账号；默认只预演，--apply 才写数据库，"
        "CSV 使用 UTF-8-BOM 且绝不覆盖已有文件。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="实际写入数据库；省略时为 dry-run。",
        )
        parser.add_argument(
            "--allow-weak",
            action="store_true",
            help=(
                "仅本次允许新账号使用未通过 Django 校验的初始密码；"
                "不会修改全局密码验证器。"
            ),
        )
        parser.add_argument(
            "--password-env",
            default=DEFAULT_PASSWORD_ENV,
            metavar="NAME",
            help="读取新账号初始密码的环境变量名（默认：%(default)s）。",
        )
        parser.add_argument(
            "--output",
            required=True,
            metavar="PATH",
            help="CSV 报告输出路径；路径已存在时命令拒绝覆盖。",
        )

    def handle(self, *args, **options):
        password_env = options["password_env"]
        initial_password = os.environ.get(password_env)

        try:
            result = sync_employee_accounts(
                apply=options["apply"],
                initial_password=initial_password,
                allow_weak=options["allow_weak"],
                output_path=options["output"],
            )
        except EmployeeAccountSyncError as exc:
            raise CommandError(str(exc)) from exc

        mode = "APPLY" if result.applied else "DRY-RUN"
        style = self.style.SUCCESS if result.applied else self.style.WARNING
        self.stdout.write(style(
            "{} 完成：新建 {}，同步现有账号 {}，无需修改 {}。".format(
                mode,
                result.created_count,
                result.updated_count,
                result.unchanged_count,
            )
        ))
        if result.report_path is not None:
            self.stdout.write("CSV 报告：{}".format(result.report_path.resolve()))
        if not result.applied:
            self.stdout.write(self.style.WARNING(
                "本次未修改数据库；确认报告后使用 --apply。弱密码还必须显式加 --allow-weak。"
            ))
