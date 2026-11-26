"""
ASGI 服务入口：向支持异步协议的服务器暴露 application。
本项目主要使用普通同步视图；ASGI/WSGI 是部署入口，不是新的业务模块。
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoProject1.settings')

application = get_asgi_application()
