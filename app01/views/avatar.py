"""
新手阅读：头像自助与授权管理。my_avatar 始终以登录账号为目标；管理他人还须满足后端范围。
上传表单返回规范化 PNG 对象，再调用保存服务；图片接口只输出已授权内容。
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from app01 import models
from app01.utils.avatars import avatar_users_for, delete_avatar, legacy_avatar_for, placeholder_avatar, save_avatar
from app01.utils.form import AvatarUploadForm


def _avatar_edit(request, user_id):
    target = get_object_or_404(avatar_users_for(request.user, manage=True), pk=user_id)
    form = AvatarUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        upload_error = getattr(request, "_avatar_upload_error", None)
        valid = form.is_valid()
        if upload_error:
            form.add_error("avatar", upload_error)
            valid = False
        if valid:
            try:
                save_avatar(request.user, target.pk, form.cleaned_data["avatar"])
            except ValidationError as exc:
                form.add_error("avatar", exc)
            else:
                messages.success(request, "头像已保存到数据库。")
                return redirect("my_avatar") if target.pk == request.user.pk else redirect("avatar_edit", user_id=target.pk)
    own = target.pk == request.user.pk
    back_url = "/" if own else ("/user/list/" if request.user.has_perm_code("employee.view") else "/profile/avatar/")
    return render(request, "avatar_form.html", {
        "form": form, "avatar_user": target, "avatar_url": reverse("avatar_image", args=[target.pk]),
        "delete_url": reverse("avatar_delete", args=[target.pk]), "back_url": back_url,
        "title": "我的头像" if own else "管理 {} 的头像".format(target.display_name),
    })


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
@csrf_protect
def my_avatar(request):
    # No submitted user/employee/role field can change this target.
    return _avatar_edit(request, request.user.pk)


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
@csrf_protect
def avatar_edit(request, user_id):
    return _avatar_edit(request, user_id)


@login_required(login_url="/login/")
@require_POST
@csrf_protect
def avatar_delete(request, user_id):
    delete_avatar(request.user, user_id)
    messages.success(request, "头像已移除，恢复默认头像。")
    return redirect("my_avatar") if user_id == request.user.pk else redirect("avatar_edit", user_id=user_id)


@login_required(login_url="/login/")
@require_safe
def avatar_image(request, user_id):
    # 编辑权限本身需要读取当前头像用于预览；查看权限和管理权限任一满足即可。
    target = avatar_users_for(request.user).filter(pk=user_id).first()
    if target is None:
        target = get_object_or_404(avatar_users_for(request.user, manage=True), pk=user_id)
    avatar = models.UserAvatar.objects.filter(user=target).only("data").first()
    if avatar and avatar.data:
        data = bytes(avatar.data)
    else:
        legacy = legacy_avatar_for(target)
        data = legacy.data if legacy else placeholder_avatar()
    response = HttpResponse(b"" if request.method == "HEAD" else data, content_type="image/png")
    response["Content-Length"] = str(len(data))
    response["Content-Disposition"] = 'inline; filename="avatar-user-{}.png"'.format(target.pk)
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
