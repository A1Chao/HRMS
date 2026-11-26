"""
头像安全工具：校验图片、重编码成无元数据 PNG，并写入 UserAvatar 数据库表。
读取前和写入前都检查对象授权；不要拿上传文件的原始名称拼接路径或直接返回其内容。
注意：关联 Employee.avatar 仅用于兼容历史文件，新头像并不直接保存到 media 文件目录。
"""
import hashlib
import warnings
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from app01 import models

MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_PIXELS = 4_000_000
MAX_AVATAR_DIMENSION = 512
ALLOWED_AVATAR_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


@dataclass(frozen=True)
class NormalizedAvatar:
    data: bytes
    mime_type: str
    size: int
    sha256: str
    width: int
    height: int


def normalize_avatar(upload):
    """有界读取并真正解码图片，去除元数据后返回规范化 PNG，不信任扩展名或声明的 MIME。"""
    try:
        if getattr(upload, "size", 0) > MAX_AVATAR_BYTES:
            raise ValidationError("头像图片不能超过 2 MB。")
        upload.seek(0)
        raw = upload.read(MAX_AVATAR_BYTES + 1)
        if not raw:
            raise ValidationError("请选择非空图片。")
        if len(raw) > MAX_AVATAR_BYTES:
            raise ValidationError("头像图片不能超过 2 MB。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                if probe.format not in ALLOWED_AVATAR_FORMATS:
                    raise ValidationError("只支持 JPEG、PNG、WebP 静态图片，不支持 SVG 或 GIF。")
                width, height = probe.size
                if width <= 0 or height <= 0 or width * height > MAX_AVATAR_PIXELS:
                    raise ValidationError("图片总像素不能超过 400 万，请先缩小图片。")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValidationError("不支持动画或多帧图片，请上传静态头像。")
                probe.verify()
            with Image.open(BytesIO(raw)) as decoded:
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                mode = "RGBA" if ("A" in oriented.getbands() or "transparency" in oriented.info) else "RGB"
                converted = oriented.convert(mode)
                converted.thumbnail((MAX_AVATAR_DIMENSION, MAX_AVATAR_DIMENSION), Image.Resampling.LANCZOS)
                # A fresh image discards EXIF/GPS/ICC/comments and trailing payload bytes.
                clean = Image.new(mode, converted.size)
                clean.paste(converted)
                output = BytesIO()
                clean.save(output, format="PNG", optimize=True)
                data = output.getvalue()
                final_width, final_height = clean.size
        if len(data) > MAX_AVATAR_BYTES:
            raise ValidationError("处理后的头像超过 2 MB，请使用更小的图片。")
    except ValidationError:
        raise
    except (OSError, ValueError, SyntaxError, AttributeError, TypeError, UnidentifiedImageError,
            Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValidationError("图片无法安全读取，请重新选择完整的 JPEG、PNG 或 WebP 图片。") from exc
    return NormalizedAvatar(data=data, mime_type="image/png", size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), width=final_width, height=final_height)


def _active_actor(actor):
    return bool(actor and getattr(actor, "is_authenticated", False) and actor.is_active and actor.pk)


def avatar_users_for(actor, *, manage=False):
    """本人可访问；非超级管理员访问他人还需要员工权限和负责部门范围。"""
    queryset = models.User.objects.select_related("employee")
    if not _active_actor(actor):
        return queryset.none()
    if actor.is_superuser:
        return queryset
    condition = Q(pk=actor.pk)
    permission = "employee.edit" if manage else "employee.view"
    if actor.managed_department_id and actor.has_perm_code(permission):
        condition |= Q(employee__department_id=actor.managed_department_id, is_superuser=False)
    return queryset.filter(condition)


def can_manage_avatar(actor, target):
    """模板按钮判断，复用与后端相同的账号、权限和部门条件。"""
    if not _active_actor(actor) or not target or not getattr(target, "pk", None):
        return False
    if actor.pk == target.pk or actor.is_superuser:
        return True
    if target.is_superuser or not actor.managed_department_id or not target.employee_id:
        return False
    return bool(target.employee.department_id == actor.managed_department_id
                and actor.has_perm_code("employee.edit"))


def _locked_target(actor, user_id):
    if not _active_actor(actor):
        raise Http404("头像用户不可访问")
    # Re-fetch authority: stale form/session objects must not restore a revoked scope.
    fresh_actor = models.User.objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
    if not fresh_actor:
        raise Http404("头像用户不可访问")
    target = avatar_users_for(fresh_actor, manage=True).select_for_update().filter(pk=user_id).first()
    if not target:
        raise Http404("头像用户不可访问")
    return fresh_actor, target


def _normalized_fields(avatar):
    if not isinstance(avatar, NormalizedAvatar) or not isinstance(avatar.data, bytes):
        raise ValidationError("只能保存经过安全图片校验的头像。")
    if (avatar.mime_type != "image/png" or avatar.size != len(avatar.data)
            or not 0 < avatar.size <= MAX_AVATAR_BYTES
            or avatar.sha256 != hashlib.sha256(avatar.data).hexdigest()
            or not 0 < avatar.width <= MAX_AVATAR_DIMENSION
            or not 0 < avatar.height <= MAX_AVATAR_DIMENSION):
        raise ValidationError("头像数据或图片元信息不一致。")
    return {"data": avatar.data, "mime_type": avatar.mime_type, "size": avatar.size,
            "sha256": avatar.sha256, "width": avatar.width, "height": avatar.height}


def save_avatar(actor, user_id, avatar):
    """重新锁定并检查目标，原子保存规范化图片到 UserAvatar，不写文件存储后端。"""
    fields = _normalized_fields(avatar)
    with transaction.atomic():
        fresh_actor, target = _locked_target(actor, user_id)
        result, _ = models.UserAvatar.objects.update_or_create(
            user=target, defaults={**fields, "updated_by": fresh_actor},
        )
        return result


def delete_avatar(actor, user_id):
    """在授权和事务保护下移除数据库头像，并清空旧头像引用以防旧图重新出现。"""
    with transaction.atomic():
        _, target = _locked_target(actor, user_id)
        deleted, _ = models.UserAvatar.objects.filter(user=target).delete()
        # Explicit removal also clears the old *reference*, not the underlying file.
        # Otherwise a removed database avatar could unexpectedly reveal a legacy one.
        if target.employee_id:
            models.Employee.objects.filter(pk=target.employee_id, avatar__isnull=False).exclude(avatar="").update(avatar="")
        return deleted


def legacy_avatar_for(user):
    """仅在目标已获授权后读取旧 Employee.avatar，禁止直接暴露历史文件 URL。"""
    if not user.employee_id or not user.employee.avatar:
        return None
    try:
        with user.employee.avatar.open("rb") as stream:
            return normalize_avatar(stream)
    except (OSError, ValueError, SuspiciousFileOperation, ValidationError):
        return None


@lru_cache(maxsize=1)
def placeholder_avatar():
    """生成默认头像并在进程内缓存，避免每次请求重新绘制。"""
    image = Image.new("RGB", (128, 128), "#edf2f7")
    draw = ImageDraw.Draw(image)
    draw.ellipse((43, 22, 85, 64), fill="#7e92a7")
    draw.rounded_rectangle((25, 71, 103, 130), radius=32, fill="#7e92a7")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()
