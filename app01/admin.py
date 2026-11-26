"""
Django 自带 admin 后台注册（/admin/）

自定义用户模型必须用 UserAdmin 注册，并把扩展字段 real_name / phone / roles /
managed_department / employee 加到表单里，否则后台无法维护数据范围。

新手阅读：@admin.register 注册 Django 自带后台，不是 /admin/list/ 的业务账号管理页。
后台同样复用业务表单、数据范围；账号/角色/权限这类安全配置只有超级管理员可以写。
get_queryset 控制可见记录，get_form 给表单传入当前用户，不能只隐藏后台按钮。
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from app01 import models
from app01.utils.account_registration import registration_username, save_registered_user
from app01.utils.form import AttendanceModelForm, EmployeeModelForm, SalaryModelForm, UserProfileFormMixin


class HRMSAdminUserChangeForm(UserProfileFormMixin, UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = models.User
        fields = "__all__"


class HRMSAdminUserCreationForm(UserProfileFormMixin, AdminUserCreationForm):
    def clean_username(self):
        return registration_username(self.cleaned_data.get("username"))

    class Meta(AdminUserCreationForm.Meta):
        model = models.User
        fields = ("username", "real_name", "phone", "roles", "managed_department", "employee")



class SuperuserConfigurationMixin:
    """Only superusers change security configuration in Django's separate admin."""

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(request, obj)


@admin.register(models.User)
class UserAdmin(SuperuserConfigurationMixin, BaseUserAdmin):
    """ 自定义用户模型的后台配置 """

    form = HRMSAdminUserChangeForm
    add_form = HRMSAdminUserCreationForm

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        # Django admin 的外层事务会一直持有同一注册锁至角色/关联保存结束。
        save_registered_user(obj, lambda: None)

    list_display = (
        "id", "username", "real_name", "phone", "managed_department", "employee",
        "is_active", "is_superuser",
    )
    search_fields = ("username", "real_name", "phone")
    # 在默认的 fieldsets 后面追加扩展字段
    fieldsets = BaseUserAdmin.fieldsets + (
        ("扩展信息", {"fields": ("real_name", "phone", "roles", "managed_department", "employee")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("扩展信息", {"fields": ("real_name", "phone", "roles", "managed_department", "employee")}),
    )


@admin.register(models.Role)
class RoleAdmin(SuperuserConfigurationMixin, admin.ModelAdmin):
    list_display = ("id", "name", "description")
    search_fields = ("name",)
    filter_horizontal = ("permissions",)


@admin.register(models.Permission)
class PermissionAdmin(SuperuserConfigurationMixin, admin.ModelAdmin):
    list_display = ("id", "module", "name", "code")
    list_filter = ("module",)
    search_fields = ("name", "code")


@admin.register(models.Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "create_time")
    search_fields = ("title",)


# 后台是第二个写入口，不能因为有 Django change 权限就放开所有部门记录。
class EmployeeScopedAdminMixin:
    """Keep Django admin's object range aligned with the business employee scope."""
    rbac_module = "employee"
    employee_lookup = "pk"

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        if not (request.user.has_perm_code(self.rbac_module + ".view")
                or request.user.has_perm_code(self.rbac_module + ".edit")):
            return queryset.none()
        return queryset.filter(**{self.employee_lookup + "__in": request.user.scope_employees()})

    def _may_write(self, request, obj=None):
        if request.user.is_superuser:
            return True
        return request.user.has_perm_code(self.rbac_module + ".edit") and (
            obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
        )

    def has_add_permission(self, request):
        return self._may_write(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._may_write(request, obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._may_write(request, obj) and super().has_delete_permission(request, obj)

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and (
            request.user.is_superuser or (
                (request.user.has_perm_code(self.rbac_module + ".view")
                 or request.user.has_perm_code(self.rbac_module + ".edit"))
                and (obj is None or self.get_queryset(request).filter(pk=obj.pk).exists())
            )
        )

    def get_form(self, request, obj=None, **kwargs):
        base_form = super().get_form(request, obj, **kwargs)

        class ScopedForm(base_form):
            def __init__(self, *args, **form_kwargs):
                form_kwargs["user"] = request.user
                super().__init__(*args, **form_kwargs)

        return ScopedForm


@admin.register(models.Employee)
class EmployeeAdmin(EmployeeScopedAdminMixin, admin.ModelAdmin):
    form = EmployeeModelForm
    readonly_fields = ("is_demo",)
    list_display = ("id", "name", "position", "is_demo", "age", "gender", "mobile", "department", "account", "create_time")
    list_filter = ("department", "gender", "position")
    search_fields = ("name", "mobile", "position")


@admin.register(models.UserAvatar)
class UserAvatarAdmin(admin.ModelAdmin):
    """头像图片只能走业务页面的安全校验流程；后台仅查看存储元数据。"""

    list_display = ("id", "user", "mime_type", "size", "width", "height", "updated_by", "updated_at")
    search_fields = ("user__username", "user__real_name", "sha256")
    readonly_fields = (
        "user", "mime_type", "size", "sha256", "width", "height", "updated_by", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(models.Attendance)
class AttendanceAdmin(EmployeeScopedAdminMixin, admin.ModelAdmin):
    rbac_module = "attendance"
    employee_lookup = "employee"
    form = AttendanceModelForm
    list_display = ("id", "employee", "date", "check_in", "check_out", "status")
    list_filter = ("status", "date")
    search_fields = ("employee__name",)


@admin.register(models.Salary)
class SalaryAdmin(EmployeeScopedAdminMixin, admin.ModelAdmin):
    rbac_module = "salary"
    employee_lookup = "employee"
    form = SalaryModelForm
    list_display = ("id", "employee", "month", "base_salary", "bonus", "deduction", "total")
    list_filter = ("month",)
    search_fields = ("employee__name",)


@admin.register(models.Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "publisher", "is_top", "create_time")
    list_filter = ("is_top",)
    search_fields = ("title",)


@admin.register(models.NoticeRead)
class NoticeReadAdmin(admin.ModelAdmin):
    list_display = ("id", "notice", "user", "read_time")
    list_filter = ("read_time",)
    search_fields = ("notice__title", "user__username", "user__real_name")
    readonly_fields = ("read_time",)


@admin.register(models.Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "id", "level", "title", "assignee", "assignee_department", "is_completed",
        "completed_by", "completed_at", "create_time",
    )
    list_filter = ("level", "is_completed", "assignee_department")
    search_fields = ("title",)
    readonly_fields = ("completed_by", "completed_at")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "assignee":
            kwargs["queryset"] = request.user.task_target_users()
        elif db_field.name == "assignee_department":
            kwargs["queryset"] = request.user.task_target_departments()
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name == "assignee":
            field.label_from_instance = lambda obj: obj.task_assignee_label
        return field

    def get_readonly_fields(self, request, obj=None):
        # 首次派发后保持目标不变；需改派时先删旧任务再创建新任务，确保重新提醒。
        fields = super().get_readonly_fields(request, obj)
        if obj is not None:
            return fields + ("assignee", "assignee_department")
        return fields

    def save_model(self, request, obj, form, change):
        if not change or "is_completed" in form.changed_data:
            obj.set_completed(obj.is_completed, request.user)
        super().save_model(request, obj, form, change)


@admin.register(models.TaskRead)
class TaskReadAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "user", "read_time")
    list_filter = ("read_time",)
    search_fields = ("task__title", "user__username", "user__real_name")
    readonly_fields = ("read_time",)


@admin.register(models.PrettyNum)
class PrettyNumAdmin(admin.ModelAdmin):
    """ 教程遗留：靓号表 """

    list_display = ("id", "mobile", "price", "level", "status")
    list_display_links = ("id", "mobile")
    list_filter = ("level", "status")
    search_fields = ("mobile",)
