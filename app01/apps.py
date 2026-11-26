"""
应用登记信息：settings.INSTALLED_APPS 通过 App01Config 启用 app01。
这里保持轻量，不在启动阶段初始化数据库或自动覆盖测试账号。
"""
from django.apps import AppConfig


class App01Config(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'app01'
