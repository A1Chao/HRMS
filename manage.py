#!/usr/bin/env python
"""
命令行入口：执行 migrate、init_data、runserver、test 等 Django 命令。
新手阅读：sys.argv 保存命令行参数，main() 把参数交给 Django；这里不写业务功能。
注意：开发入口仅在外部未设置时默认 DJANGO_DEBUG=1，生产检查必须显式设置为 0。
"""
import os
import sys

# 只给课堂启动提供默认值；外部 DJANGO_DEBUG=0 不会被 setdefault 覆盖。
os.environ.setdefault("DJANGO_DEBUG", "1")
def main():
    """设置配置模块，并把命令行参数交给 Django 的命令执行器。"""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoProject1.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
