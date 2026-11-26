"""
表单层（对应任务书 T2 ~ T9 的 ModelForm 校验；沿用教程 utils/form.py 的分层）

所有表单都继承 BootStrapModelForm，页面上自动是 Bootstrap 样式。
校验重点：
    DepartmentModelForm  部门名称不能重复
    EmployeeModelForm    姓名长度、年龄 18~60、手机号正则 + 唯一（编辑时排除自己）
    AttendanceModelForm  状态由模型自动算，表单只收上下班时间；同一天不能重复
    SalaryModelForm      月份格式、同一员工同一月份不能重复
    NoticeModelForm      标题 / 内容必填
    TaskModelForm        AJAX 提交用，字段校验失败会以 JSON 返回
    UserModelForm        自定义用户的新增（密码用 set_password 存 PBKDF2 哈希）
    RoleModelForm        RBAC 角色 + 权限多选

新手阅读：ModelForm 把模型字段变成输入框，is_valid() 完成字段及模型校验。
GET 时显示空表单；POST 时绑定 request.POST；校验失败后用同一个 form 回显错误与原输入。
user/operator 参数给关联下拉框和角色授予施加权限边界，不是仅供显示的参数。
"""
import datetime

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from app01 import models
from app01.models import mobile_validator
from app01.utils.bootstrap import BootStrapModelForm
from app01.utils.user_profiles import employee_profile_defaults
from app01.utils.avatars import normalize_avatar
from app01.utils.domain_validation import parse_salary_month
from app01.utils.account_authorization import grantable_roles, permissions_within
from app01.utils.account_registration import registration_name, registration_username, save_registered_user
from app01.utils.employee_onboarding import (
    can_open_employee_login, new_account_candidate, ordinary_employee_role,
    save_employee_and_optional_login,
)


class AvatarUploadForm(forms.Form):
    avatar = forms.FileField(
        label="头像图片", required=True,
        help_text="JPEG、PNG、WebP 静态图片，最大 2 MB / 400 万像素；规范化后图片内容存入数据库。",
        widget=forms.FileInput(attrs={"class": "form-control", "accept": "image/jpeg,image/png,image/webp"}),
    )

    def clean_avatar(self):
        return normalize_avatar(self.cleaned_data["avatar"])


class DepartmentModelForm(BootStrapModelForm):
    """ 部门新增 / 编辑（T2） """

    class Meta:
        model = models.Department
        fields = ["title"]

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise ValidationError("部门名称不能为空")
        exists = models.Department.objects.filter(title=title).exclude(pk=self.instance.pk).exists()
        if exists:
            raise ValidationError("部门名称已存在")
        return title


# 表单 Meta.fields 是可编辑字段白名单；客户端多传字段不会因此获得修改权限。
class EmployeeModelForm(BootStrapModelForm):
    """ 员工新增 / 编辑（T3）

    任务书校验要求：
        - 姓名长度：这里限定 2~10 个字符；
        - 年龄范围：18~60 岁；
        - 手机号正则 ^1[3-9]\\d{9}$ 且唯一（编辑时要排除自己）。
    """

    # 入职时间用日期选择器，不用 DateTimeField 默认的“日期+时间”两个输入框
    create_time = forms.DateField(
        label="入职时间",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        input_formats=["%Y-%m-%d"],
    )

    class Meta:
        model = models.Employee
        # 头像由独立的 UserAvatar 页面维护并存入数据库；这里保留模型旧字段仅用于兼容历史文件。
        fields = ["name", "position", "age", "gender", "account", "create_time", "mobile", "department"]
        widgets = {
            "position": forms.TextInput(attrs={"placeholder": "例如：研发工程师、销售专员"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if "position" in self.fields:
            self.fields["position"].required = True
            self.fields["position"].error_messages["required"] = "请填写员工职位"
        if user is None or user.is_superuser or "department" not in self.fields:
            return
        if user.managed_department_id:
            self.fields["department"].queryset = models.Department.objects.filter(
                id=user.managed_department_id
            )
        elif user.employee_id:
            self.fields["department"].queryset = models.Department.objects.filter(
                employee__id=user.employee_id
            ).distinct()
        else:
            self.fields["department"].queryset = models.Department.objects.none()

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if len(name) < 2 or len(name) > 10:
            raise ValidationError("姓名长度需为 2~10 个字符")
        return name

    def clean_position(self):
        return (self.cleaned_data.get("position") or "").strip()

    def clean_age(self):
        age = self.cleaned_data.get("age")
        if age is None or age < 18 or age > 60:
            raise ValidationError("年龄必须在 18~60 岁之间")
        return age

    def clean_account(self):
        account = self.cleaned_data.get("account")
        if account is not None and account < 0:
            raise ValidationError("账户余额不能为负数")
        return account

    def clean_mobile(self):
        mobile = (self.cleaned_data.get("mobile") or "").strip()
        # 手机号唯一：编辑时排除“自己”这一条，否则不改手机号也会提示重复
        exists = models.Employee.objects.filter(mobile=mobile).exclude(pk=self.instance.pk).exists()
        if exists:
            raise ValidationError("手机号已存在")
        return mobile

    def clean_create_time(self):
        """ DateField 拿到的是 date，转成带时区的 datetime 再交给 DateTimeField 存 """
        day = self.cleaned_data.get("create_time")
        if day is None:
            return timezone.now()
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


class EmployeeCreateModelForm(EmployeeModelForm):
    """新增专用：有账号授权时默认勾选开通；取消勾选仍可只保存档案。"""
    create_login = forms.BooleanField(label="同时开通登录账号", required=False, initial=True)
    login_username = forms.CharField(
        label="登录名", required=False, max_length=150,
        help_text="留空默认使用员工姓名；同名账号不会被自动关联。",
    )
    initial_password = forms.CharField(
        label="初始密码", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="仅用于这个新账号，使用系统现有密码规则；不修改任何既有密码。",
    )
    confirm_initial_password = forms.CharField(
        label="确认初始密码", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.operator = user
        super().__init__(*args, user=user, **kwargs)
        self.can_open_login = can_open_employee_login(user)
        if not self.can_open_login:
            self.fields["create_login"].initial = False
            self.fields["create_login"].disabled = True
            self.fields["create_login"].help_text = "当前账号无登录账号开通权限，只能保存员工档案。"
            for name in ("login_username", "initial_password", "confirm_initial_password"):
                self.fields.pop(name)

    @property
    def employee_fields(self):
        return [self[name] for name in EmployeeModelForm.Meta.fields]

    def clean(self):
        cleaned = super().clean()
        if not self.can_open_login:
            if forms.BooleanField(required=False).clean(self.data.get("create_login")):
                raise ValidationError("当前账号无权同时开通登录账号。")
            return cleaned
        if not cleaned.get("create_login"):
            return cleaned
        try:
            ordinary_employee_role(self.operator)
        except ValidationError as exc:
            self.add_error("create_login", exc)
        username = cleaned.get("login_username") or cleaned.get("name") or ""
        cleaned["login_username"] = username
        password = cleaned.get("initial_password")
        confirmation = cleaned.get("confirm_initial_password")
        if not password:
            self.add_error("initial_password", "勾选开通账号时，请填写初始密码。")
        if not confirmation:
            self.add_error("confirm_initial_password", "请再次输入初始密码。")
        elif password != confirmation:
            self.add_error("confirm_initial_password", "两次输入的密码不一致。")
        if username and password and cleaned.get("name") and cleaned.get("mobile"):
            try:
                new_account_candidate(username, cleaned["name"], cleaned["mobile"], password)
            except ValidationError as exc:
                # 用户名/密码错误统一回显，不写入任何半成品档案。
                self.add_error(None, exc)
        return cleaned

    def save(self, commit=True):
        if not commit:
            raise ValueError("员工与可选账号必须一起提交，不支持 commit=False。")
        employee = super().save(commit=False)
        opening = self.cleaned_data.get("create_login", False)
        return save_employee_and_optional_login(
            employee, self.operator,
            username=self.cleaned_data["login_username"] if opening else None,
            password=self.cleaned_data["initial_password"] if opening else None,
        )


class AttendanceModelForm(BootStrapModelForm):
    """ 考勤新增 / 编辑（T6）：状态字段不在表单里，保存时自动算 """

    class Meta:
        model = models.Attendance
        fields = ["employee", "date", "check_in", "check_out"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "check_in": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "check_out": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and "employee" in self.fields:
            self.fields["employee"].queryset = user.scope_employees().order_by("id")

    def clean(self):
        cleaned = super().clean()
        employee = cleaned.get("employee")
        day = cleaned.get("date")
        check_in = cleaned.get("check_in")
        check_out = cleaned.get("check_out")

        if check_in and check_out and check_out <= check_in:
            self.add_error("check_out", "下班时间必须晚于上班时间")

        # 同一个员工同一天只能有一条考勤记录（模型层也加了 unique_together）
        if employee and day:
            exists = models.Attendance.objects.filter(employee=employee, date=day).exclude(
                pk=self.instance.pk
            ).exists()
            if exists:
                self.add_error("date", "该员工当天已有考勤记录")
        return cleaned


class SalaryModelForm(BootStrapModelForm):
    """ 薪资新增 / 编辑（T7）：实发工资由模型 save() 自动计算 """

    class Meta:
        model = models.Salary
        fields = ["employee", "month", "base_salary", "bonus", "deduction"]
        widgets = {
            # HTML5 的 month 控件提交格式就是 2025-01
            "month": forms.TextInput(attrs={"type": "month", "placeholder": "例如 2025-01"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and "employee" in self.fields:
            self.fields["employee"].queryset = user.scope_employees().order_by("id")

    def clean_month(self):
        month = (self.cleaned_data.get("month") or "").strip()
        parse_salary_month(month)
        return month

    def clean(self):
        cleaned = super().clean()
        employee = cleaned.get("employee")
        month = cleaned.get("month")
        if employee and month:
            exists = models.Salary.objects.filter(employee=employee, month=month).exclude(
                pk=self.instance.pk
            ).exists()
            if exists:
                self.add_error("month", "该员工当月薪资已存在")
        for field in ("base_salary", "bonus", "deduction"):
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, "金额不能为负数")
        return cleaned


class NoticeModelForm(BootStrapModelForm):
    """ 公告发布 / 编辑（T8） """

    class Meta:
        model = models.Notice
        fields = ["title", "content", "is_top"]
        widgets = {
            "content": forms.Textarea(attrs={"rows": 6}),
        }

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if len(title) < 2:
            raise ValidationError("标题至少 2 个字符")
        return title


class EmployeeAccountChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.task_assignee_label


class TaskModelForm(BootStrapModelForm):
    """任务（T9）：派发对象必须严格选择一个个人或一个部门组。"""

    assignee = EmployeeAccountChoiceField(
        label="个人负责人", queryset=models.User.objects.none(), required=False,
    )
    target_type = forms.ChoiceField(
        label="派发方式",
        choices=(("user", "派发给个人"), ("department", "派发给部门组")),
        initial="user",
        required=False,  # 兼容旧客户端：可根据唯一已提交的目标自动推断。
    )

    class Meta:
        model = models.Task
        fields = [
            "level", "title", "detail", "target_type", "assignee", "assignee_department",
        ]
        widgets = {
            "detail": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        operator = kwargs.pop("operator", None)
        super().__init__(*args, **kwargs)
        if operator is not None:
            self.fields["assignee"].queryset = operator.task_target_users()
            self.fields["assignee_department"].queryset = operator.task_target_departments()
        else:
            self.fields["assignee"].queryset = models.User.objects.filter(
                is_active=True, employee__isnull=False
            ).select_related("employee__department").order_by("employee_id")
            self.fields["assignee_department"].queryset = models.Department.objects.order_by("id")

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if len(title) < 2:
            raise ValidationError("标题至少 2 个字符")
        return title

    def clean_detail(self):
        detail = (self.cleaned_data.get("detail") or "").strip()
        if len(detail) < 5:
            raise ValidationError("详情至少 5 个字符")
        return detail

    def clean(self):
        cleaned = super().clean()
        target_type = cleaned.get("target_type")
        assignee = cleaned.get("assignee")
        department = cleaned.get("assignee_department")

        # 兼容此前只提交 assignee 的 AJAX 调用，同时不接受两空或两有。
        if not target_type:
            if assignee and not department:
                target_type = "user"
            elif department and not assignee:
                target_type = "department"
            else:
                self.add_error("target_type", "请选择派发给个人或部门组")
                return cleaned

        if target_type == "user":
            if not assignee:
                self.add_error("assignee", "请选择个人负责人")
            if department:
                self.add_error("assignee_department", "派发给个人时不能同时选择部门组")
        elif target_type == "department":
            if not department:
                self.add_error("assignee_department", "请选择负责人组（部门）")
            if assignee:
                self.add_error("assignee", "派发给部门组时不能同时选择个人负责人")
        else:
            self.add_error("target_type", "无效的派发方式")
        return cleaned


# ---------------------------------------------------------------------------
# RBAC 用户 / 角色（T5）
# ---------------------------------------------------------------------------
# Mixin 是可复用的一组行为；两个账号表单和后台表单共用资料来源规则。
class UserProfileFormMixin:
    """仅从本次提交关联的员工补空资料，再交给 ModelForm 的模型验证。"""

    def __init__(self, *args, operator=None, **kwargs):
        self.operator = operator
        super().__init__(*args, **kwargs)
        if operator is not None and not operator.is_superuser:
            self.fields["roles"].queryset = grantable_roles(operator)
            self.fields["managed_department"].queryset = models.Department.objects.filter(
                pk=operator.managed_department_id
            )
            if self.instance.pk == operator.pk:
                # Self-service edits cannot turn identity/range changes into read-access escalation.
                self.fields["employee"].queryset = models.Employee.objects.filter(pk=operator.employee_id)
            elif operator.managed_department_id:
                self.fields["employee"].queryset = operator.scope_employees()
        help_texts = {
            "real_name": "留空时使用本次选择的关联员工姓名；未关联时不会猜测。",
            "phone": "留空时使用关联员工手机号；单独填写的联系方式优先保留。",
            "managed_department": "部门管理员的管理范围，不等同于员工所属部门。",
            "employee": "普通员工和部门管理员必须关联本人员工档案；系统超级管理员可独立存在。",
        }
        for field, text in help_texts.items():
            if field in self.fields:
                self.fields[field].help_text = text

    def clean(self):
        cleaned = super().clean()
        values = employee_profile_defaults(
            cleaned.get("employee"),
            real_name=cleaned.get("real_name", ""),
            phone=cleaned.get("phone", ""),
        )
        for field_name, value in values.items():
            # 无效的显式输入不能被员工档案回退掩盖。
            if field_name not in self.errors:
                cleaned[field_name] = value
        roles = cleaned.get("roles")
        if self.operator is not None and roles is not None:
            if not permissions_within(self.operator, (code for code in roles.values_list("permissions__code", flat=True) if code is not None)):
                self.add_error("roles", "不能授予超出当前账号授权范围的角色")
        role_names = set(roles.values_list("name", flat=True)) if roles is not None else set()
        if "部门管理员" in role_names:
            if not cleaned.get("managed_department"):
                self.add_error("managed_department", "部门管理员必须选择负责部门")
            if not cleaned.get("employee"):
                self.add_error("employee", "部门管理员必须关联本人员工档案")
        if "普通员工" in role_names and not cleaned.get("employee"):
            self.add_error("employee", "普通员工必须关联本人员工档案")
        if self.instance._state.adding:
            try:
                registration_name(
                    cleaned.get("real_name"), username=cleaned.get("username", ""),
                    employee=cleaned.get("employee"),
                )
            except ValidationError as exc:
                self.add_error("real_name", exc)
        return cleaned


# 表单收到明文只用于校验，save 调用 set_password 后才将哈希写入 User。
class UserModelForm(UserProfileFormMixin, BootStrapModelForm):
    """ 新建系统用户：密码 + 确认密码，保存时用 set_password 做 PBKDF2 哈希 """

    password = forms.CharField(
        label="密码", min_length=6, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    confirm_password = forms.CharField(
        label="确认密码", min_length=6, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = models.User
        fields = [
            "username", "real_name", "phone", "email", "roles",
            "managed_department", "employee",
        ]

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if password:
            validate_password(password, user=self.instance)
        return password

    def clean_username(self):
        return registration_username(self.cleaned_data.get("username"))

    def clean_confirm_password(self):
        password = self.cleaned_data.get("password")
        confirm = self.cleaned_data.get("confirm_password")
        if password and confirm and password != confirm:
            raise ValidationError("两次输入的密码不一致")
        return confirm

    def save(self, commit=True):
        user = super().save(commit=False)
        # 关键：绝不能直接存明文，set_password 会生成 PBKDF2 哈希
        user.set_password(self.cleaned_data["password"])
        if commit:
            save_registered_user(user, self.save_m2m)
        return user


class UserEditModelForm(UserProfileFormMixin, BootStrapModelForm):
    """ 编辑系统用户：不改密码（改密码走“重置密码”） """

    class Meta:
        model = models.User
        fields = [
            "username", "real_name", "phone", "email", "roles",
            "managed_department", "employee", "is_active",
        ]

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        exists = models.User.objects.filter(username=username).exclude(pk=self.instance.pk).exists()
        if exists:
            raise ValidationError("用户名已存在")
        return username


class UserResetPasswordModelForm(BootStrapModelForm):
    """ 重置系统用户密码 """

    password = forms.CharField(
        label="新密码", min_length=6, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    confirm_password = forms.CharField(
        label="确认密码", min_length=6, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = models.User
        fields = []

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if password:
            validate_password(password, user=self.instance)
        return password

    def clean_confirm_password(self):
        password = self.cleaned_data.get("password")
        confirm = self.cleaned_data.get("confirm_password")
        if password and confirm and password != confirm:
            raise ValidationError("两次输入的密码不一致")
        return confirm

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
            self.save_m2m()
        return user


class RoleModelForm(BootStrapModelForm):
    """ 角色新增 / 编辑：勾选权限（多选） """

    def __init__(self, *args, operator=None, **kwargs):
        self.operator = operator
        super().__init__(*args, **kwargs)
        if operator is not None and not operator.is_superuser:
            allowed_ids = [permission.pk for permission in models.Permission.objects.all()
                           if operator.has_perm_code(permission.code)]
            self.fields["permissions"].queryset = models.Permission.objects.filter(pk__in=allowed_ids)

    class Meta:
        model = models.Role
        fields = ["name", "description", "permissions"]
        widgets = {
            "permissions": forms.CheckboxSelectMultiple(),
        }

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if self.operator is not None and not self.operator.is_superuser and name == "超级管理员":
            raise ValidationError("只有超级管理员可以维护保留的管理员角色")
        exists = models.Role.objects.filter(name=name).exclude(pk=self.instance.pk).exists()
        if exists:
            raise ValidationError("角色名称已存在")
        return name


# ---------------------------------------------------------------------------
# 教程遗留：靓号（保留）
# ---------------------------------------------------------------------------
class PrettyModelForm(BootStrapModelForm):
    """ 新建靓号：正则校验 + 手机号不能重复 """

    mobile = forms.CharField(
        label="手机号",
        validators=[mobile_validator],
        widget=forms.TextInput(attrs={"placeholder": "手机号"}),
    )

    class Meta:
        model = models.PrettyNum
        fields = ["mobile", "price", "level", "status"]

    def clean_mobile(self):
        mobile = self.cleaned_data["mobile"]
        if models.PrettyNum.objects.filter(mobile=mobile).exists():
            raise ValidationError("手机号已存在")
        return mobile


class PrettyEditModelForm(BootStrapModelForm):
    """ 编辑靓号：排除自己以外，手机号不能重复 """

    mobile = forms.CharField(
        label="手机号",
        validators=[mobile_validator],
        widget=forms.TextInput(attrs={"placeholder": "手机号"}),
    )

    class Meta:
        model = models.PrettyNum
        fields = ["mobile", "price", "level", "status"]

    def clean_mobile(self):
        mobile = self.cleaned_data["mobile"]
        exists = models.PrettyNum.objects.filter(mobile=mobile).exclude(pk=self.instance.pk).exists()
        if exists:
            raise ValidationError("手机号已存在")
        return mobile
