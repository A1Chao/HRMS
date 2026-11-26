"""
WSGI 服务入口：生产 Web 服务器通过 application 把 HTTP 请求交给 Django。
课堂开发使用 manage.py runserver 即可；不要把业务逻辑或密码写进此文件。
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoProject1.settings')

application = get_wsgi_application()
