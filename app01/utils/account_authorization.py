"""
账号和角色委派规则：user.edit/role.edit 不代表可以接管更高权限账号。
功能权限、目标权限层级、部门范围都要满足；Django staff 是另一套后台权限，不能绕过保护。
修改共享角色会影响所有关联账号，因此检查的是整组用户，而不只当前登录人。
"""
from app01 import models


def _active(actor):
    return bool(actor and actor.is_authenticated and actor.is_active)


def permissions_within(actor, codes):
    """检查请求授予的每个权限码都在当前操作者已有授权内。"""
    return _active(actor) and all(actor.has_perm_code(code) for code in codes)


def user_within_boundary(actor, target):
    """判断目标账号是否在操作者可管理的权限层级和部门范围内。"""
    if not _active(actor):
        return False
    if actor.is_superuser:
        return True
    # Django staff permissions are a separate authority, not represented by RBAC codes.
    if target.is_superuser or target.is_staff:
        return False
    if actor.managed_department_id and target.pk != actor.pk:
        if not target.employee_id or target.employee.department_id != actor.managed_department_id:
            return False
    return permissions_within(actor, target.permission_codes())


def can_manage_user(actor, target):
    """同时要求 user.edit 和目标账号边界，供视图及按钮复用。"""
    return _active(actor) and actor.has_perm_code("user.edit") and user_within_boundary(actor, target)


def grantable_roles(actor):
    """返回当前操作者可以授予的角色下拉选项。"""
    if actor.is_superuser:
        return models.Role.objects.all()
    ids = [role.pk for role in models.Role.objects.prefetch_related("permissions")
           if role.name != "超级管理员"
           and permissions_within(actor, (permission.code for permission in role.permissions.all()))]
    return models.Role.objects.filter(pk__in=ids)


def can_manage_role(actor, role):
    """要求 role.edit，并检查共享角色影响的所有账号是否都可管理。"""
    if not _active(actor) or not actor.has_perm_code("role.edit"):
        return False
    if actor.is_superuser:
        return True
    if role.name == "超级管理员" or not permissions_within(
        actor, (permission.code for permission in role.permissions.all())
    ):
        return False
    # Editing a shared role changes the capabilities of every associated account.
    return all(user_within_boundary(actor, user) for user in
               role.users.select_related("employee").prefetch_related("roles__permissions"))
