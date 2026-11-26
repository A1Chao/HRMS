"""
Bootstrap 样式的公共表单基类（对应教程 utils/bootstrap.py）

作用：让每个 ModelForm 都继承它，字段自动带上 Bootstrap 的 form-control 样式，
不用在每个表单里重复写 attrs={"class": "form-control", ...}。

说明（相对教程的扩展）：
- 复选框、文件上传框加 form-control 反而不像样，这里跳过；
- DateInput / TimeInput 需要保留 type="date" / type="time"，只补 class，不覆盖已有属性。

表单外观工具：给 Django 字段的 widget.attrs 加 Bootstrap CSS 类。
这里只改变输入框样式，不替代年龄、手机号、密码或权限校验。
"""
from django import forms

# 这些控件不加 form-control（加了样式会变丑，或者破坏原生的多选框）
SKIP_WIDGETS = (
    forms.CheckboxInput,
    forms.CheckboxSelectMultiple,
    forms.RadioSelect,
    forms.FileInput,
    forms.ClearableFileInput,
)


class BootStrapModelForm(forms.ModelForm):
    """ 公共的 ModelForm 基类：批量给所有字段加上 Bootstrap 样式 """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # self.fields 是这个表单里的所有字段（字段名 -> 字段对象）
        # 循环一遍，统一加上 form-control 类，页面上就是 Bootstrap 的输入框样式
        for name, field in self.fields.items():
            if isinstance(field.widget, SKIP_WIDGETS):
                continue

            # 字段原来有属性就保留，没有才新建，避免覆盖自定义的 attrs（如 type="date"）
            field.widget.attrs["class"] = "form-control"
            if not isinstance(field.widget, (forms.DateInput, forms.TimeInput)):
                field.widget.attrs.setdefault("placeholder", field.label)