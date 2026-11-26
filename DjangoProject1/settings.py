"""
Django settings for DjangoProject1 project.

《企业员工管理系统》（HRMS）配置说明（对应课程设计任务书 T1 / T13）：

- 数据库：MySQL（默认库名 ems_hrms），中文 zh-hans，时区 Asia/Shanghai；
- 自定义用户模型：app01.User（继承 AbstractUser），必须配置 AUTH_USER_MODEL，
  否则 Django 自带的 auth_user 和我们的 User 会同时存在，权限、admin 后台都会乱；
- 安全加固：DEBUG 默认关闭；本地开发设置 DJANGO_DEBUG=1，生产密钥、数据库和
  ALLOWED_HOSTS 均通过环境变量提供。

新手阅读：本文件回答“应用有哪些、请求经过谁、连哪个数据库、模板在哪里”。
推荐顺序：环境变量 → INSTALLED_APPS → MIDDLEWARE → TEMPLATES → DATABASES → 安全与日志。
这里配置的是连接口令；网页登录密码由 User.set_password() 管理，两者不能混淆。
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    """把环境变量字符串转换成布尔值，例如 "1"/"true" 表示启用。"""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name, default=""):
    """把逗号分隔的环境变量转换成列表，并忽略空项。"""
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# SECURITY WARNING: don't run with debug turned on in production!
# 默认关闭；本地 HTTP 开发显式设置 DJANGO_DEBUG=1。
DEBUG = _env_bool("DJANGO_DEBUG", default=False)

# 生产必须显式提供稳定且高熵的密钥；开发模式保留开箱即用能力。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-local-development-only-change-before-production-2026"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is disabled."
        )

# 逗号分隔，例如 DJANGO_ALLOWED_HOSTS=hrms.example.com,www.example.com。
ALLOWED_HOSTS = _env_list(
    "DJANGO_ALLOWED_HOSTS",
    (
        "127.0.0.1,localhost,frp-any.com,kubernetes.docker.internal"
        if DEBUG else ""
    ),
)
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must contain at least one host when DJANGO_DEBUG is disabled."
    )

# HTTPS FRP 会把公网请求转发到本机 HTTP；开发模式默认信任当前映射来源。
# 若映射域名或端口变化，可用逗号分隔的 DJANGO_CSRF_TRUSTED_ORIGINS 覆盖。
CSRF_TRUSTED_ORIGINS = _env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "https://frp-any.com:25659" if DEBUG else "",
)


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'app01.apps.App01Config'
]

# 列表顺序就是请求处理顺序：先有会话/认证用户，再做业务登录与权限判断。
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'app01.middleware.avatar_upload.AvatarUploadLimitMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # ① 登录校验中间件：未登录一律跳登录页（T4）
    'app01.middleware.auth.AuthMiddleware',
    # ② 权限校验中间件：登录后按 RBAC 权限拦截接口（T5）
    'app01.middleware.permission.PermissionMiddleware',
]

ROOT_URLCONF = 'DjangoProject1.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates']
        ,
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'app01.context_processors.pending_notifications',
            ],
        },
    },
]

WSGI_APPLICATION = 'DjangoProject1.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases
# 本地开发延续原有 root/123456；生产环境必须显式提供专用数据库用户和口令。
DB_USER = os.environ.get("DJANGO_DB_USER")
DB_PASSWORD = os.environ.get("DJANGO_DB_PASSWORD")
if DEBUG:
    DB_USER = DB_USER or "root"
    # DB credentials are supplied by the local operator, not shared in source.
    DB_PASSWORD = DB_PASSWORD if DB_PASSWORD is not None else ""
elif not DB_USER or not DB_PASSWORD:
    raise ImproperlyConfigured(
        "DJANGO_DB_USER and DJANGO_DB_PASSWORD must be set in production."
    )

# 数据库名固定为 ems_hrms；改环境变量不会自动创建数据库，首次部署需先建库。
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'ems_hrms',  # 新版系统固定使用独立数据库 ems_hrms
        'USER': DB_USER,
        'PASSWORD': DB_PASSWORD,
        'HOST': os.environ.get('DJANGO_DB_HOST', '127.0.0.1'),
        'PORT': os.environ.get('DJANGO_DB_PORT', '3306'),
        'OPTIONS': {'charset': 'utf8mb4'},
    }
}

# 自定义用户模型（T1 / T4）：迁移之前必须配置好，迁移之后再改非常麻烦
AUTH_USER_MODEL = 'app01.User'

# 登录失败 / 权限不足的跳转地址（Django 内置 auth 装饰器会用到）
LOGIN_URL = '/login/'


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

# 这是新建/重置表单调用的密码规则；验证器不会自动更换任何既有密码。
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'zh-hans'

TIME_ZONE = 'Asia/Shanghai'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
# collectstatic 的汇总目录（开发时 runserver 会自动用 STATICFILES_DIRS/app 里的 static）
STATIC_ROOT = BASE_DIR / 'staticfiles'

# 员工头像等用户上传文件（T3）
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ---------------------------------------------------------------------------
# T13 安全加固：安全响应头与生产 HTTPS
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True       # 禁止浏览器猜 MIME 类型
X_FRAME_OPTIONS = 'DENY'                 # 禁止被 iframe 嵌套，防点击劫持
SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_HTTPONLY = True           # JS 读不到 sessionid
CSRF_COOKIE_HTTPONLY = False             # 保留可读，AJAX 取 csrftoken 方便
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

# 开发模式允许 runserver 使用 HTTP；生产模式强制 HTTPS 并启用长期 HSTS。
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

# ---------------------------------------------------------------------------
# T13 分级日志：控制台 INFO，文件 WARNING（按大小滚动）
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# 控制台 INFO 用于开发观察，文件 WARNING 保留异常；不要把密码或密钥写入日志。
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file': {
            'level': 'WARNING',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'django.log'),
            'maxBytes': 1024 * 1024,
            'backupCount': 3,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        # 项目自己的日志：写文件
        'app01': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        # Django 框架日志：只在控制台输出
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}