"""
数据模型（对应课程设计任务书 T1 ~ T10）

模型总览（先有 ER 关系，再写代码）：
    User          系统用户（继承 AbstractUser，T1 / T4）
    Role          角色（RBAC 的“角色”）
    Permission    权限（RBAC 的“权限”，用编码 code 表示，如 employee.edit）
    Department    部门（T2）
    Employee      员工档案（T3）
    Attendance    考勤记录（T6）
    Salary        薪资记录（T7）
    Notice        公告（T8）
    NoticeRead    公告逐用户阅读回执
    Task          任务（T9，AJAX 提交、完成状态）
    TaskRead      任务派发逐用户阅读回执
    PrettyNum     靓号（教程遗留模块，保留不影响本系统）

关系：
    User  --多对多-->  Role  --多对多-->  Permission      （用户-角色-权限，T5）
    Department  --一对多-->  Employee                    （一个部门多个员工）
    Employee  --一对多-->  Attendance / Salary           （一个员工多条考勤、薪资）
    User  --一对多-->  Notice（发布人） / Task（负责人）   （外键指向用户）
    User  --多对多-->  Notice / Task（通过阅读记录跟踪每位用户是否已读）

新手阅读：每个 models.Model 对应一张表，字段对应列，外键表达表之间的关联。
推荐顺序：Department/Employee → Attendance/Salary → User/Role/Permission → Notice/Task。
clean() 负责跨字段校验，save() 维护计算结果；普通 save 不会自动执行完整 full_clean。
User 是登录账号，Employee 是人事档案；二者通过 employee 字段关联，而非同一张表。
"""
import copy
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, router, transaction
from django.utils import timezone

from app01.utils.domain_validation import employee_hire_date, parse_salary_month

# 手机号正则：1 开头，第二位 3~9，共 11 位（任务书 T3 要求 ^1[3-9]\d{9}$）
mobile_validator = RegexValidator(r"^1[3-9]\d{9}$", "手机号格式不正确（需为 1 开头的 11 位数字）")


# ---------------------------------------------------------------------------
# 一、RBAC：用户 - 角色 - 权限（T5）
# ---------------------------------------------------------------------------
class Permission(models.Model):
    """ 权限表：接口级权限的最小单位，例如 employee.edit / salary.view """

    module = models.CharField(verbose_name="模块", max_length=32)
    name = models.CharField(verbose_name="权限名称", max_length=64)
    code = models.CharField(verbose_name="权限编码", max_length=64, unique=True)

    class Meta:
        verbose_name = "权限"
        verbose_name_plural = verbose_name
        ordering = ["code"]

    def __str__(self):
        return "{}（{}）".format(self.name, self.code)


class Role(models.Model):
    """ 角色表：一个角色可以拥有多个权限，一个用户可以有多个角色 """

    name = models.CharField(verbose_name="角色名称", max_length=32, unique=True)
    description = models.CharField(verbose_name="说明", max_length=200, blank=True)
    permissions = models.ManyToManyField(verbose_name="权限", to="Permission", blank=True)

    class Meta:
        verbose_name = "角色"
        verbose_name_plural = verbose_name
        ordering = ["id"]

    def __str__(self):
        return self.name


class User(AbstractUser):
    """ 自定义用户模型（继承 AbstractUser）

    任务书要求“认证：自定义用户模型（继承 AbstractUser）”与“自定义用户模型 + 密码安全哈希”。
    AbstractUser 已经自带 username / password / email / is_staff / is_active 等字段，
    密码通过 set_password() 用 PBKDF2 哈希存储，严禁明文或裸 MD5。
    """

    real_name = models.CharField(verbose_name="姓名", max_length=32, blank=True)
    phone = models.CharField(verbose_name="手机号", max_length=11, blank=True, validators=[mobile_validator])
    roles = models.ManyToManyField(verbose_name="角色", to="Role", blank=True, related_name="users")
    managed_department = models.ForeignKey(
        verbose_name="负责部门",
        to="Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="managers",
    )
    # User 与 Employee 一对一关联；空值允许独立系统管理员，但普通/部门账号由表单要求绑定。
    employee = models.OneToOneField(
        verbose_name="关联员工",
        to="Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="user_account",
    )

    class Meta:
        verbose_name = "系统用户"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.display_name

    @property
    def profile_name(self):
        """真实姓名优先，缺失时仅回退到已明确关联的员工；不猜测账号身份。"""
        if (self.real_name or "").strip():
            return self.real_name
        return self.employee.name if self.employee_id else ""

    @property
    def profile_phone(self):
        """单独维护的联系方式优先，缺失时只读显示关联员工手机号。"""
        if (self.phone or "").strip():
            return self.phone
        return self.employee.mobile if self.employee_id else ""

    @property
    def display_name(self):
        """页面导航栏显示资料姓名；没有可靠姓名来源时使用登录名。"""
        return self.profile_name or self.username

    @property
    def task_assignee_label(self):
        """任务界面以员工档案为准；历史未绑定员工的任务保留原账号显示。"""
        if self.employee_id:
            employee = self.employee
            return "{}（{}）".format(employee.name, employee.department.title)
        return self.display_name

    @property
    def role_names(self):
        """ 角色名称列表，导航栏 / 用户列表里用 """
        return [role.name for role in self.roles.all()]

    def permission_codes(self):
        """当前用户的权限编码集合；在当前用户实例上缓存，避免单个请求内重复查询。"""
        if hasattr(self, "_permission_codes_cache"):
            return self._permission_codes_cache

        if self.is_superuser:
            codes = {"*"}
        else:
            roles = getattr(self, "_prefetched_objects_cache", {}).get("roles")
            if roles is not None and all("permissions" in getattr(role, "_prefetched_objects_cache", {}) for role in roles):
                codes = {permission.code for role in roles for permission in role.permissions.all()}
            else:
                codes = set(self.roles.values_list("permissions__code", flat=True))
                codes.discard(None)
        self._permission_codes_cache = codes
        return codes

    def has_perm_code(self, code):
        """ 判断当前用户是否有某个权限编码（支持 "employee.*" 通配） """
        if self.is_superuser:
            return True
        codes = self.permission_codes()
        if "*" in codes or code in codes:
            return True
        return "{}.*".format(code.split(".")[0]) in codes

    # 关键安全链路：有模块权限仍不能查询其他部门；所有员工相关视图应复用此范围。
    def scope_employees(self, queryset=None):
        """返回当前用户有权访问的员工 QuerySet。"""
        if queryset is None:
            queryset = Employee.objects.all()
        if self.is_superuser:
            return queryset
        if self.managed_department_id:
            return queryset.filter(department_id=self.managed_department_id)
        if self.employee_id:
            return queryset.filter(id=self.employee_id)
        return queryset.none()

    def task_department_ids(self):
        """返回当前关联员工所在部门的任务组，并在请求内缓存；管理范围不等于组成员身份。"""
        if hasattr(self, "_task_department_ids_cache"):
            return self._task_department_ids_cache

        department_ids = set()
        if self.employee_id:
            employee_department_id = Employee.objects.filter(
                id=self.employee_id
            ).values_list("department_id", flat=True).first()
            if employee_department_id:
                department_ids.add(employee_department_id)

        self._task_department_ids_cache = department_ids
        return department_ids

    def task_target_users(self):
        """按员工档案选有效账号；部门管理员限本部门或本人已绑定的员工账号。"""
        if not self.is_active or not self.has_perm_code("task.edit"):
            return User.objects.none()
        if not self.is_superuser and not self.managed_department_id and "部门管理员" in self.role_names:
            return User.objects.none()
        queryset = User.objects.filter(is_active=True, employee__isnull=False).select_related(
            "employee__department"
        )
        if self.managed_department_id and not self.is_superuser:
            queryset = queryset.filter(
                models.Q(employee__department_id=self.managed_department_id)
                | models.Q(id=self.id)
            )
        return queryset.order_by("employee_id")

    def task_target_departments(self):
        """可派发的部门组；部门管理员仅能向负责部门派发。"""
        if not self.is_active or not self.has_perm_code("task.edit"):
            return Department.objects.none()
        if not self.is_superuser and not self.managed_department_id and "部门管理员" in self.role_names:
            return Department.objects.none()
        queryset = Department.objects.all()
        if self.managed_department_id and not self.is_superuser:
            queryset = queryset.filter(id=self.managed_department_id)
        return queryset.order_by("id")


class UserAvatar(models.Model):
    """用户头像的规范化 PNG 内容；不经过文件存储后端。"""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="database_avatar", verbose_name="所属用户")
    data = models.BinaryField(verbose_name="PNG 图片内容", editable=False)
    mime_type = models.CharField(verbose_name="图片类型", max_length=32, default="image/png", editable=False)
    size = models.PositiveIntegerField(verbose_name="图片字节数", editable=False)
    sha256 = models.CharField(verbose_name="内容摘要", max_length=64, editable=False)
    width = models.PositiveSmallIntegerField(verbose_name="宽度", editable=False)
    height = models.PositiveSmallIntegerField(verbose_name="高度", editable=False)
    updated_at = models.DateTimeField(verbose_name="修改时间", auto_now=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="修改人", editable=False)

    class Meta:
        verbose_name = "用户头像（数据库）"
        verbose_name_plural = verbose_name
        constraints = [
            models.CheckConstraint(condition=models.Q(size__gt=0, size__lte=2097152), name="user_avatar_size_valid"),
            models.CheckConstraint(condition=models.Q(width__gt=0, width__lte=512, height__gt=0, height__lte=512),
                                   name="user_avatar_dimensions_valid"),
        ]

    def __str__(self):
        return "用户 {} 的数据库头像".format(self.user_id)


# ---------------------------------------------------------------------------
# 二、部门与员工（T2 / T3）
# ---------------------------------------------------------------------------
class Department(models.Model):
    """ 部门表（T2） """

    title = models.CharField(verbose_name="部门名称", max_length=100, unique=True)
    create_time = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "部门"
        verbose_name_plural = verbose_name
        ordering = ["id"]

    def __str__(self):
        return self.title


class Employee(models.Model):
    """ 员工表（T3）

    字段包含姓名、职位、年龄、性别、账户余额、入职时间、手机号、所属部门、头像。
    手机号在数据库层加 unique=True，编辑时 ModelForm 会自动排除自身做重复校验。
    """

    gender_choices = (
        (1, "男"),
        (2, "女"),
    )

    name = models.CharField(verbose_name="姓名", max_length=32)
    position = models.CharField(verbose_name="职位", max_length=64, blank=True, default="")
    is_demo = models.BooleanField(verbose_name="测试数据", default=False)
    age = models.IntegerField(
        verbose_name="年龄",
        validators=[MinValueValidator(18), MaxValueValidator(60)],
    )
    gender = models.SmallIntegerField(verbose_name="性别", choices=gender_choices, default=1)
    account = models.DecimalField(verbose_name="账户余额", max_digits=10, decimal_places=2, default=0)
    create_time = models.DateTimeField(verbose_name="入职时间", default=timezone.now)
    mobile = models.CharField(
        verbose_name="手机号", max_length=11, unique=True, validators=[mobile_validator]
    )
    department = models.ForeignKey(
        verbose_name="所属部门", to="Department", to_field="id", on_delete=models.PROTECT
    )
    # 历史文件兼容字段；新头像由 UserAvatar 存储，员工普通资料表单不直接写文件。
    avatar = models.ImageField(verbose_name="头像", upload_to="avatars/", blank=True, null=True)

    class Meta:
        verbose_name = "员工"
        verbose_name_plural = verbose_name
        ordering = ["id"]

    def __str__(self):
        return self.name

    @staticmethod
    def _validate_account_username(account, old_name, new_name):
        if account and account.username == old_name and old_name != new_name:
            try:
                User._meta.get_field("username").clean(new_name, account)
            except ValidationError as exc:
                raise ValidationError({"name": exc.messages}) from exc
            if User.objects.filter(username=new_name).exclude(pk=account.pk).exists():
                raise ValidationError({"name": "该姓名已被另一个登录账号占用，不能更换本人员工身份。"})

    # 模型 clean 是 ModelForm 的跨字段验证入口；直接 ORM save 不会自动调用完整 full_clean。
    def clean(self):
        super().clean()
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values("name").first()
            if old:
                account = User.objects.filter(employee_id=self.pk).first()
                self._validate_account_username(account, old["name"], self.name)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            old = type(self).objects.select_for_update().filter(pk=self.pk).values("name", "mobile").first() if self.pk else None
            fields = kwargs.get("update_fields")
            account, changes = None, {}
            if old and (fields is None or {"name", "mobile"}.intersection(fields)):
                account = User.objects.select_for_update().filter(employee_id=self.pk).first()
                name = self.name if fields is None or "name" in fields else old["name"]
                mobile = self.mobile if fields is None or "mobile" in fields else old["mobile"]
                self._validate_account_username(account, old["name"], name)
                if account:
                    if account.username == old["name"] and name != old["name"]:
                        changes["username"] = name
                    if not (account.real_name or "").strip() or account.real_name == old["name"]:
                        if account.real_name != name:
                            changes["real_name"] = name
                    if not (account.phone or "").strip() or account.phone == old["mobile"]:
                        if account.phone != mobile:
                            changes["phone"] = mobile
                    for field, value in changes.items():
                        User._meta.get_field(field).clean(value, account)
            super().save(*args, **kwargs)
            if changes:
                for field, value in changes.items():
                    setattr(account, field, value)
                account.save(update_fields=list(changes))


def _save_derived_value(instance, save, source_fields, derived_field, compute_method, args, kwargs):
    """统一保存考勤状态/实发金额；部分更新只采用本次字段和数据库中其他已保存值。"""
    update_fields = kwargs.get("update_fields")

    def prepare(source):
        value = getattr(source, compute_method)()
        if derived_field == "total":
            try:
                instance._meta.get_field(derived_field).clean(value, instance)
            except ValidationError as exc:
                raise ValidationError("实发工资超出允许的金额范围") from exc
        setattr(instance, derived_field, value)

    if update_fields is None:
        prepare(instance)
        return save(*args, **kwargs)
    fields = set(update_fields)
    kwargs["update_fields"] = fields
    if not fields or not (fields.intersection(source_fields) or derived_field in fields):
        return save(*args, **kwargs)
    using = kwargs.get("using") or router.db_for_write(type(instance), instance=instance)
    with transaction.atomic(using=using):
        source = copy.copy(instance)
        if instance.pk:
            persisted = type(instance).objects.using(using).select_for_update().values(*source_fields).get(pk=instance.pk)
            for field in source_fields:
                if field not in fields:
                    setattr(source, field, persisted[field])
        prepare(source)
        kwargs["update_fields"] = fields | {derived_field}
        return save(*args, **kwargs)


# ---------------------------------------------------------------------------
# 三、考勤（T6）
# ---------------------------------------------------------------------------
class Attendance(models.Model):
    """ 考勤表（T6）

    考勤状态不让人手动选，而是在 save() 里根据上下班时间自动计算：
        上班时间 / 下班时间 缺任意一个 → 缺勤
        上班时间晚于 09:00          → 迟到
        下班时间早于 18:00          → 早退
        其余                        → 按时
    """

    status_choices = (
        (1, "按时"),
        (2, "迟到"),
        (3, "早退"),
        (4, "缺勤"),
    )

    WORK_START = datetime.time(9, 0)   # 规定上班时间
    WORK_END = datetime.time(18, 0)    # 规定下班时间

    employee = models.ForeignKey(verbose_name="员工", to="Employee", on_delete=models.PROTECT)
    date = models.DateField(verbose_name="日期")
    check_in = models.TimeField(verbose_name="上班时间", null=True, blank=True)
    check_out = models.TimeField(verbose_name="下班时间", null=True, blank=True)
    status = models.SmallIntegerField(verbose_name="考勤状态", choices=status_choices, default=1, editable=False)

    class Meta:
        verbose_name = "考勤记录"
        verbose_name_plural = verbose_name
        unique_together = ("employee", "date")
        ordering = ["-date", "employee_id"]

    def compute_status(self):
        """ 根据上下班时间计算考勤状态（T6 的自测点） """
        if not self.check_in or not self.check_out:
            return 4  # 缺勤
        if self.check_in > self.WORK_START:
            return 2  # 迟到
        if self.check_out < self.WORK_END:
            return 3  # 早退
        return 1      # 按时

    def clean(self):
        super().clean()
        errors = {}
        if (isinstance(self.check_in, datetime.time) and isinstance(self.check_out, datetime.time)
                and self.check_out <= self.check_in):
            errors["check_out"] = "下班时间必须晚于上班时间"
        if self.employee_id and isinstance(self.date, datetime.date):
            try:
                if self.date < employee_hire_date(self.employee):
                    errors["date"] = "考勤日期不能早于员工入职日期"
            except Employee.DoesNotExist:
                pass  # The foreign-key field reports the invalid employee.
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        return _save_derived_value(
            self, super().save, {"check_in", "check_out"}, "status", "compute_status", args, kwargs,
        )

    def __str__(self):
        return "{} {}".format(self.employee.name, self.date)


# ---------------------------------------------------------------------------
# 四、薪资（T7）
# ---------------------------------------------------------------------------
class Salary(models.Model):
    """ 薪资表（T7）

    实发工资 = 基本工资 + 奖金 - 扣款，在 save() 里统一计算（total 字段不给用户手填）。
    月份用 "2025-01" 这种字符串存，方便按月筛选、按月聚合统计。
    """

    employee = models.ForeignKey(verbose_name="员工", to="Employee", on_delete=models.PROTECT)
    month = models.CharField(verbose_name="月份", max_length=7)   # 例如 2025-01
    base_salary = models.DecimalField(verbose_name="基本工资", max_digits=10, decimal_places=2, default=0)
    bonus = models.DecimalField(verbose_name="奖金", max_digits=10, decimal_places=2, default=0)
    deduction = models.DecimalField(verbose_name="扣款", max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(verbose_name="实发工资", max_digits=10, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "薪资记录"
        verbose_name_plural = verbose_name
        unique_together = ("employee", "month")
        ordering = ["-month", "employee_id"]

    # 金额使用 Decimal 避免二进制浮点误差，quantize 固定两位小数。
    def compute_total(self):
        """ 实发 = 基本工资 + 奖金 - 扣款 """
        total = (self.base_salary or 0) + (self.bonus or 0) - (self.deduction or 0)
        return Decimal(total).quantize(Decimal("0.01"))

    def clean(self):
        super().clean()
        errors = {}
        try:
            month = parse_salary_month(self.month)
        except ValidationError as exc:
            errors["month"] = exc.messages
        else:
            if self.employee_id:
                try:
                    joined = employee_hire_date(self.employee)
                    if (month.year, month.month) < (joined.year, joined.month):
                        errors["month"] = "薪资月份不能早于员工入职月份"
                except Employee.DoesNotExist:
                    pass
        amounts_valid = True
        for field in ("base_salary", "bonus", "deduction"):
            try:
                amount = Decimal(getattr(self, field))
                if not amount.is_finite():
                    amounts_valid = False
                elif amount < 0:
                    errors[field] = "金额不能为负数"
            except (TypeError, ValueError, InvalidOperation):
                amounts_valid = False  # Field validation supplies the original error.
        if amounts_valid:
            try:
                self._meta.get_field("total").clean(self.compute_total(), self)
            except (ValidationError, InvalidOperation):
                errors["__all__"] = "实发工资超出允许的金额范围"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        return _save_derived_value(
            self, super().save, {"base_salary", "bonus", "deduction"}, "total", "compute_total", args, kwargs,
        )

    def __str__(self):
        return "{} {}".format(self.employee.name, self.month)


# ---------------------------------------------------------------------------
# 五、公告（T8）
# ---------------------------------------------------------------------------
class Notice(models.Model):
    """ 公告表（T8）：发布时间用 auto_now_add 自动写入 """

    title = models.CharField(verbose_name="标题", max_length=120)
    content = models.TextField(verbose_name="内容")
    publisher = models.ForeignKey(
        verbose_name="发布人", to="User", on_delete=models.CASCADE, related_name="notices"
    )
    create_time = models.DateTimeField(verbose_name="发布时间", auto_now_add=True)
    is_top = models.BooleanField(verbose_name="是否置顶", default=False)

    class Meta:
        verbose_name = "公告"
        verbose_name_plural = verbose_name
        # 置顶的排前面，其余按发布时间倒序
        ordering = ["-is_top", "-create_time"]

    def __str__(self):
        return self.title


class NoticeRead(models.Model):
    """公告阅读记录：同一用户对同一公告只记录一次。"""

    notice = models.ForeignKey(
        verbose_name="公告", to="Notice", on_delete=models.CASCADE, related_name="read_records"
    )
    user = models.ForeignKey(
        verbose_name="阅读人", to="User", on_delete=models.CASCADE, related_name="notice_read_records"
    )
    read_time = models.DateTimeField(verbose_name="阅读时间", auto_now_add=True)

    class Meta:
        verbose_name = "公告阅读记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["notice", "user"], name="unique_notice_read_user"
            )
        ]
        ordering = ["-read_time"]

    def __str__(self):
        return "{} - {}".format(self.user.display_name, self.notice.title)


# ---------------------------------------------------------------------------
# 六、任务（T9，AJAX）
# ---------------------------------------------------------------------------
class TaskQuerySet(models.QuerySet):
    """任务接收范围：直接派给本人，或派给本人当前所属的部门组。"""

    def assigned_to(self, user):
        if user is None or not user.is_authenticated or not user.is_active:
            return self.none()

        condition = models.Q(assignee_id=user.id)
        department_ids = user.task_department_ids()
        if department_ids:
            condition |= models.Q(assignee_department_id__in=department_ids)
        return self.filter(condition)

    def editable_by(self, user):
        if user is None or not user.is_authenticated or not user.is_active:
            return self.none()
        if not user.has_perm_code("task.edit"):
            return self.none()
        if not user.is_superuser and not user.managed_department_id and "部门管理员" in user.role_names:
            return self.none()
        if user.is_superuser or not user.managed_department_id:
            return self
        return self.filter(
            models.Q(assignee_department_id=user.managed_department_id)
            | models.Q(assignee__employee__department_id=user.managed_department_id)
            | models.Q(assignee_id=user.id)
        )

    def visible_to(self, user):
        if user is None or not user.is_authenticated or not user.is_active:
            return self.none()
        if not user.is_superuser and not user.managed_department_id and "部门管理员" in user.role_names:
            return self.none()
        if user.is_superuser or (user.has_perm_code("task.edit") and not user.managed_department_id):
            return self
        return self.filter(
            models.Q(id__in=self.assigned_to(user).values("id"))
            | models.Q(id__in=self.editable_by(user).values("id"))
        )


class Task(models.Model):
    """ 任务表（T9）：AJAX 异步提交，校验错误回显到页面 """

    level_choices = (
        (1, "紧急"),
        (2, "重要"),
        (3, "普通"),
    )

    level = models.SmallIntegerField(verbose_name="级别", choices=level_choices, default=3)
    title = models.CharField(verbose_name="标题", max_length=100)
    detail = models.TextField(verbose_name="详情")
    assignee = models.ForeignKey(
        verbose_name="个人负责人", to="User", on_delete=models.PROTECT,
        null=True, blank=True, related_name="tasks",
    )
    assignee_department = models.ForeignKey(
        verbose_name="负责人组（部门）", to="Department", on_delete=models.PROTECT,
        null=True, blank=True, related_name="group_tasks",
    )
    create_time = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)
    is_completed = models.BooleanField(verbose_name="是否已完成", default=False, db_index=True)
    completed_at = models.DateTimeField(verbose_name="完成时间", null=True, blank=True)
    completed_by = models.ForeignKey(
        verbose_name="完成人", to="User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="completed_tasks",
    )

    objects = TaskQuerySet.as_manager()

    class Meta:
        verbose_name = "任务"
        verbose_name_plural = verbose_name
        # 未完成任务优先；已完成任务自动下沉，各组内按创建时间倒序。
        ordering = ["is_completed", "-create_time"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(assignee__isnull=False, assignee_department__isnull=True)
                    | models.Q(assignee__isnull=True, assignee_department__isnull=False)
                ),
                name="task_exactly_one_dispatch_target",
            )
        ]

    @property
    def dispatch_target_display(self):
        if self.assignee_id:
            return self.assignee.task_assignee_label
        if self.assignee_department_id:
            return "{}（组）".format(self.assignee_department.title)
        return "-"

    def clean(self):
        super().clean()
        has_user = bool(self.assignee_id)
        has_department = bool(self.assignee_department_id)
        if has_user == has_department:
            raise ValidationError({
                "assignee": "个人负责人和部门组必须且只能选择一个",
                "assignee_department": "个人负责人和部门组必须且只能选择一个",
            })
        if has_department and self._state.adding:
            has_recipient = User.objects.filter(
                is_active=True, employee__department_id=self.assignee_department_id
            ).exists()
            if not has_recipient:
                raise ValidationError({
                    "assignee_department": "该部门组暂无有效接收账号，请先为部门员工关联启用的系统账号"
                })

    # 完成标记、时间和完成人必须一起维护，不能只改布尔字段造成历史状态矛盾。
    def set_completed(self, completed, user=None):
        """统一维护完成状态、时间和完成人，避免三个字段互相矛盾。"""
        self.is_completed = completed
        self.completed_at = timezone.now() if completed else None
        self.completed_by = user if completed else None

    def __str__(self):
        return self.title


class TaskRead(models.Model):
    """任务派发提醒阅读记录：仅任务负责人会产生记录。"""

    task = models.ForeignKey(
        verbose_name="任务", to="Task", on_delete=models.CASCADE, related_name="read_records"
    )
    user = models.ForeignKey(
        verbose_name="阅读人", to="User", on_delete=models.CASCADE, related_name="task_read_records"
    )
    read_time = models.DateTimeField(verbose_name="阅读时间", auto_now_add=True)

    class Meta:
        verbose_name = "任务提醒阅读记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["task", "user"], name="unique_task_read_user"
            )
        ]
        ordering = ["-read_time"]

    def __str__(self):
        return "{} - {}".format(self.user.display_name, self.task.title)


# ---------------------------------------------------------------------------
# 七、教程遗留模块：靓号（保留，不影响本系统）
# ---------------------------------------------------------------------------
class PrettyNum(models.Model):
    """ 靓号表（教程 9 章遗留模块） """

    mobile = models.CharField(verbose_name="手机号", max_length=11)
    price = models.IntegerField(verbose_name="价格", default=0)
    level_choices = (
        (1, "1级"),
        (2, "2级"),
        (3, "3级"),
        (4, "4级"),
    )
    level = models.SmallIntegerField(verbose_name="级别", choices=level_choices, default=1)
    status_choices = (
        (1, "已占用"),
        (2, "未使用"),
    )
    status = models.SmallIntegerField(verbose_name="状态", choices=status_choices, default=2)

    def __str__(self):
        return self.mobile