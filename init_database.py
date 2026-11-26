#!/usr/bin/env python
"""给组员使用的数据库建库/建表入口。

先安装 requirements.txt，再执行：
    python init_database.py              # 只创建库、应用迁移
    python init_database.py --with-demo  # 另外补齐基础演示数据/测试账号
    python init_database.py --with-demo --with-demo-managers  # 完整 11 人演示

脚本不能脱离项目目录单独使用，但可从任意工作目录通过完整路径运行。
表结构的唯一来源是项目 migrations，而不是本文件里的另一套 CREATE TABLE。
连接配置沿用 Django settings 和 DJANGO_DB_* 环境变量；不修改任何密码。
"""
import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent


class BootstrapError(Exception):
    """适合展示给组员的初始化错误；错误信息不能包含数据库口令。"""


def mysql_options(config):
    """连接 MySQL 服务时先不指定数据库，这样 ems_hrms 尚不存在也能建库。"""
    if config.get("ENGINE") != "django.db.backends.mysql":
        raise BootstrapError("此脚本仅支持项目使用的 MySQL 数据库。")
    name = config.get("NAME")
    if not isinstance(name, str) or not name or len(name) > 64 or "\x00" in name:
        raise BootstrapError("数据库名无效，请检查 Django DATABASES 配置。")
    try:
        port = int(config.get("PORT") or 3306)
    except (TypeError, ValueError):
        raise BootstrapError("DJANGO_DB_PORT 必须是 1 到 65535 之间的整数。") from None
    if not 1 <= port <= 65535:
        raise BootstrapError("DJANGO_DB_PORT 必须是 1 到 65535 之间的整数。")
    options = {
        "user": config.get("USER") or "",
        "passwd": config.get("PASSWORD") or "",
        "port": port,
        "charset": "utf8mb4",
        "connect_timeout": 10,
    }
    host = config.get("HOST") or "localhost"
    # 兼容 Linux 的 MySQL socket；Windows 通常是 127.0.0.1。
    options["unix_socket" if host.startswith("/") else "host"] = host
    return name, options


@contextmanager
def database_session(config, driver):
    """建库并持有 MySQL 命名锁，避免两个人同时对同一库执行本初始化脚本。"""
    name, options = mysql_options(config)
    server = driver.connect(**options)
    lock_name = "hrms-init:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:54]
    locked = False
    try:
        with server.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 30))
            row = cursor.fetchone()
            if not row or row[0] != 1:
                raise BootstrapError("另一个初始化程序正在操作此数据库，请稍后重试。")
            locked = True

            # 库名来自 settings，但仍转义标识符；口令不拼接到 SQL 中。
            quoted_name = "`" + name.replace("`", "``") + "`"
            cursor.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME = %s", (name,)
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS {quoted_name} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                cursor.execute(
                    "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME = %s", (name,)
                )
                row = cursor.fetchone()
            if not row or row[0] != "utf8mb4":
                raise BootstrapError(
                    "已有数据库不是 utf8mb4；为避免改动已有数据，脚本停止。"
                    "请先备份，再让数据库管理员确认字符集。"
                )
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
                (name,),
            )
            tables = {row[0] for row in cursor.fetchall()}
            if tables and "django_migrations" not in tables:
                raise BootstrapError(
                    "目标库已有表，但没有 Django 迁移记录。脚本不会猜测表结构、"
                    "删除表或使用 fake 迁移；请先备份并检查目标库。"
                )
        yield name
    finally:
        try:
            if locked:
                with server.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        finally:
            server.close()


def verify_schema(connection, registered_apps):
    """迁移完成后再核对模型需要的表和列，包括自动生成的多对多关系表。

    这一步能发现迁移记录存在但表/列被手动删除的常见损坏；不是完整 DBA
    审计，不会尝试修复手动改过的索引、字段类型或数据库中的业务数据。
    """
    missing = []
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        for model in registered_apps.get_models(include_auto_created=True):
            meta = model._meta
            if not meta.managed or meta.proxy or meta.swapped:
                continue
            table = meta.db_table
            if table not in tables:
                missing.append(f"缺少表 {table}")
                continue
            columns = {
                column.name
                for column in connection.introspection.get_table_description(cursor, table)
            }
            for field in meta.local_fields:
                if field.column not in columns:
                    missing.append(f"缺少列 {table}.{field.column}")
    if missing:
        raise BootstrapError(
            "迁移记录与实际表结构不匹配：" + "；".join(missing)
            + "。请先备份并检查，脚本不会删除或重建已有表。"
        )


def apply_migrations(connection, registered_apps, command, with_demo=False, with_demo_managers=False):
    """先验证模型与迁移同步，再应用已有迁移；不生成迁移、不 fake、不清库。"""
    command("check")
    # --check --dry-run：模型改了却忘记提交迁移时立即失败，不悄悄造一套新表。
    command("makemigrations", check=True, dry_run=True, interactive=False)
    command("migrate", interactive=False)
    command("migrate", check_unapplied=True, interactive=False, verbosity=0)
    verify_schema(connection, registered_apps)
    if with_demo:
        # init_data 本身只补缺失演示记录，不重置已有密码。
        command("init_data")
    if with_demo_managers:
        # 只有显式选择完整演示才创建这三个测试账号；既有密码仍不重置。
        password_env = "HRMS_DEMO_MANAGER_INITIAL_PASSWORD"
        had_password = password_env in os.environ
        os.environ.setdefault(password_env, "123456")
        try:
            command(
                "provision_department_managers",
                "--assignment=熊大=技术部", "--assignment=熊二=市场部",
                "--assignment=光头强=人力资源部",
                demo_employees=True, apply=True, password_env=password_env, allow_weak=True,
            )
        finally:
            if not had_password:
                os.environ.pop(password_env, None)
        command("fill_demo_salaries", apply=True, allow_demo=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="创建 MySQL 数据库并按项目 Django 迁移建表（不会清库或改密码）。"
    )
    parser.add_argument(
        "--with-demo", action="store_true",
        help="建表后补齐演示数据；新建 admin/张三/王五 的测试口令为 123456。",
    )
    parser.add_argument(
        "--with-demo-managers", action="store_true",
        help="配合 --with-demo 新建熊大/熊二/光头强演示管理账号并补齐测试薪资。",
    )
    args = parser.parse_args(argv)
    if args.with_demo_managers and not args.with_demo:
        parser.error("--with-demo-managers 必须与 --with-demo 一起使用。")
    if sys.version_info < (3, 10):
        print("初始化失败：Django 5.2 需要 Python 3.10+，推荐 Python 3.12。", file=sys.stderr)
        return 1
    if not (PROJECT_ROOT / "manage.py").is_file():
        print("初始化失败：请将本文件保留在项目根目录，与 manage.py 放在一起。", file=sys.stderr)
        return 1

    sys.path.insert(0, str(PROJECT_ROOT))
    # 与 manage.py 一样仅提供开发默认值，显式的生产环境变量不会被覆盖。
    os.environ.setdefault("DJANGO_DEBUG", "1")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "DjangoProject1.settings")
    try:
        import django
        import MySQLdb
        from django.apps import apps
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.db import DatabaseError, connections
    except ImportError:
        print(
            "初始化失败：缺少依赖，请在 Python 3.12 虚拟环境执行 "
            "python -m pip install -r requirements.txt。", file=sys.stderr,
        )
        return 1

    try:
        django.setup()
        config = settings.DATABASES["default"]
        name, options = mysql_options(config)
        print(f"目标：MySQL {options.get('host') or options.get('unix_socket')}:{options['port']} / {name}")
        print("不会删除已有数据库或重置密码；已有库只应用尚未执行的项目迁移。")
        with database_session(config, MySQLdb):
            # 强制之后的 Django 连接看到新建的库，而非复用旧连接。
            connections["default"].close()
            apply_migrations(
                connections["default"], apps, call_command,
                with_demo=args.with_demo, with_demo_managers=args.with_demo_managers,
            )
        print("初始化完成：已有迁移全部应用，模型所需表/列核对通过。")
        if not args.with_demo:
            print("未导入你电脑上的现有数据，也未创建业务账号。")
            print("需要默认演示账号/数据：python init_database.py --with-demo")
        print("启动项目：python manage.py runserver 127.0.0.1:8000")
        return 0
    except (BootstrapError, ImproperlyConfigured, CommandError) as exc:
        # 框架配置错误及我们自己的错误不包含口令；MySQL 错误另行脱敏处理。
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    except (MySQLdb.Error, DatabaseError) as exc:
        candidate = exc.args[0] if exc.args else None
        code = candidate if isinstance(candidate, int) else "未知"
        print(
            f"初始化失败：MySQL 错误码 {code}。请检查 MySQL 服务、连接地址、"
            "本机口令及建库/建表权限。脚本没有自动清库。"
            "MySQL 部分结构变更不能回滚，失败后先确认状态再重试。",
            file=sys.stderr,
        )
        return 1
    except SystemExit as exc:
        # makemigrations --check / migrate --check 以 SystemExit 表达失败。
        print("初始化失败：模型与迁移不同步，或仍有未应用迁移；请查看上面的提示。", file=sys.stderr)
        return int(exc.code or 1)
    finally:
        connections.close_all()


if __name__ == "__main__":
    raise SystemExit(main())
