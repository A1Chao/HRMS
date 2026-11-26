"""
RBAC 模板标签（T5：前端按权限隐藏按钮）

用法：
    {% load rbac %}
    {% has_perm "employee.edit" as can_edit %}
    {% if can_edit %}<a class="btn btn-success" href="/user/add/">新建员工</a>{% endif %}

判断逻辑和中间件一致：超级管理员永远 True；
普通用户看“用户 → 角色 → 权限”里有没有这个编码（支持 module.* 通配）。

模板权限标签：{% has_perm "employee.edit" as can_edit %} 决定是否显示按钮。
模板隐藏不是安全边界；即使用户手写 URL，后端也必须再次检查权限。
"""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def has_perm(context, code):
    """ 当前登录用户是否有某个权限编码 """
    request = context.get("request")
    user = getattr(request, "user", None) if request else None
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return user.has_perm_code(code)