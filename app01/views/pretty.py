"""
靓号管理（对应教程 9.2 ~ 9.4）

涉及到的东西：
    模型：app01/models.py 的 PrettyNum（手机号、价格、级别、状态）
    表单：app01/utils/form.py 的 PrettyModelForm / PrettyEditModelForm
          —— 负责“手机号正则校验”和“手机号不能重复”
    分页：app01/utils/pagination.py 的 Pagination
    路由：/pretty/list/、/pretty/add/、/pretty/<nid>/edit/、/pretty/delete/
    模板：templates/pretty_list.html、pretty_add.html、pretty_edit.html

整体流程都是 Django 最常见的“GET 显示页面 / POST 处理提交”套路：
    GET  → 把数据（表单对象、queryset）传给模板 render 出来
    POST → 校验 → 保存 → redirect 回列表（PRG：Post-Redirect-Get，避免刷新重复提交）

新手阅读：靓号教程遗留模块，不属于 T1–T13 核心验收内容。
可作为额外 ModelForm/分页练习，但不要把 PrettyNum 当成员工或登录账号。
"""
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01.utils.request_validation import validate_post_id

from app01 import models
from app01.utils.form import PrettyEditModelForm, PrettyModelForm
from app01.utils.pagination import Pagination


def pretty_list(request):
    """
    靓号列表（9.2）：把数据库里的靓号查出来给模板循环显示。
    """

    # 第 1 步：读取搜索条件
    q = request.GET.get('q', '').strip()
    # 把“查询条件”放进字典，再用 **data_dict 展开传给 filter()，
    data_dict = {}
    if q:
        data_dict['mobile__contains'] = q
    # order_by('id') 让列表顺序固定（默认按 id 升序），
    queryset = models.PrettyNum.objects.filter(**data_dict).order_by('id')
    # 第 2 步：分页
    # 注意：这里不要再传 page_size，否则会覆盖 pagination.py 里的默认值（每页条数只在一处配置）
    page_object = Pagination(request, queryset)
    # 第 3 步：把数据交给模板
    #   queryset     → 当前页的数据，模板里 {% for obj in queryset %} 循环
    #   page_string  → 分页条的 HTML，模板里用 {{ page_string|safe }} 输出
    #   q            → 搜索关键字，模板里回填到搜索框，刷新/翻页后输入框里还有内容
    #   total_count  → 一共多少条（本页暂时没用到，留着以后显示“共 N 条”）
    # ------------------------------------------------------------------
    context = {
        'queryset': page_object.page_queryset,
        'page_string': page_object.page_html(),
        'q': q,
        'total_count': page_object.total_count,
    }
    return render(request, 'pretty_list.html', context)


def pretty_add(request):
    """新建靓号（9.3）：列表页点“新建靓号”跳转到 /pretty/add/。

    校验规则全部写在 PrettyModelForm 里：
      - 手机号必填
      - 手机号要符合正则校验（1 开头、9~11 位数字）
      - 手机号不能和数据库里已有的重复（clean_mobile 里查库）
    """
    # GET：第一次打开页面，给模板一个“空表单”
    if request.method == 'GET':
        form = PrettyModelForm()
        return render(request, 'pretty_add.html', {'form': form})
    form = PrettyModelForm(data=request.POST)

    # is_valid()校验重复
    if form.is_valid():
        form.save()
        return redirect('/pretty/list/')

    return render(request, 'pretty_add.html', {'form': form})


def pretty_edit(request, nid):
    """编辑靓号（9.4）：列表页点“编辑”跳转到 /pretty/<id>/edit/。
    nid 是 URL 里用 <int:nid> 捕获到的编号，由 Django 直接以参数传进来。
    """
    #取
    row_object = models.PrettyNum.objects.filter(id=nid).first()
    if row_object is None:      # 防止乱输 id
        return redirect('/pretty/list/')
    # 填进表单
    if request.method == 'GET':
        form = PrettyEditModelForm(instance=row_object)
        return render(request, 'pretty_edit.html', {'form': form})
    form = PrettyEditModelForm(data=request.POST, instance=row_object)
    if form.is_valid():
        form.save()
        return redirect('/pretty/list/')

    # 校验失败：同样把 form 带回模板，保留用户填的数据和错误提示
    return render(request, 'pretty_edit.html', {'form': form})


@require_POST
@validate_post_id
def pretty_delete(request):
    """删除靓号：仅接受 POST。"""
    nid = request.POST.get('nid')
    if nid:
        models.PrettyNum.objects.filter(id=nid).delete()
    return redirect('/pretty/list/')