"""
头像模板工具：复用后端头像管理范围，决定页面是否显示头像管理入口。
头像实际内容经受保护的 avatar_image 接口读取，不能用模板绕过授权。
"""
from django import template
from app01.utils.avatars import can_manage_avatar as allowed_to_manage_avatar

register = template.Library()


@register.simple_tag(takes_context=True)
def can_manage_avatar(context, target):
    request = context.get("request")
    return allowed_to_manage_avatar(request.user if request else None, target)
