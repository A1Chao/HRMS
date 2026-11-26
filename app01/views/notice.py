"""
公告管理（T8）：发布、列表、置顶、搜索、分页

- 发布时间：模型 create_time 用 auto_now_add 自动写入；
- 发布人：外键指向自定义用户模型 User，发布时自动填当前登录用户；
- 置顶：列表排序是 ["-is_top", "-create_time"]，置顶的永远排最前。

新手阅读：公告发布、查询、置顶及维护（T8）。发布人由服务器填当前用户，不信任表单传来的用户 ID。
publisher__employee 预载姓名回退所需关系，避免每条公告再查询发布人的员工档案。
"""
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from app01.utils.request_validation import validate_post_id

from app01 import models
from app01.utils.form import NoticeModelForm
from app01.utils.pagination import Pagination


def notice_list(request):
    """ 公告列表：标题模糊搜索 + 分页，置顶优先 """
    q = request.GET.get("q", "").strip()

    # T11：select_related 把发布人一起查出来，模板里 obj.publisher.display_name 不额外查库
    queryset = models.Notice.objects.select_related("publisher__employee").order_by("-is_top", "-create_time")
    if q:
        queryset = queryset.filter(title__contains=q)

    page_object = Pagination(request, queryset)
    context = {
        "queryset": page_object.page_queryset,
        "page_string": page_object.page_html(),
        "q": q,
        "total_count": page_object.total_count,
    }
    return render(request, "notice_list.html", context)


def notice_add(request):
    """ 发布公告：发布人自动取当前登录用户 """
    title = "发布公告"
    if request.method == "GET":
        form = NoticeModelForm()
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/notice/list/"})

    form = NoticeModelForm(data=request.POST)
    if form.is_valid():
        notice = form.save(commit=False)
        notice.publisher = request.user      # 发布人 = 当前登录用户
        notice.save()
        models.NoticeRead.objects.get_or_create(notice=notice, user=request.user)
        return redirect("/notice/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/notice/list/"})


def notice_edit(request, nid):
    """ 编辑公告 """
    row_object = models.Notice.objects.filter(id=nid).first()
    if row_object is None:
        return redirect("/notice/list/")

    title = "编辑公告：{}".format(row_object.title)

    if request.method == "GET":
        form = NoticeModelForm(instance=row_object)
        return render(request, "change.html", {"form": form, "title": title, "back_url": "/notice/list/"})

    form = NoticeModelForm(data=request.POST, instance=row_object)
    if form.is_valid():
        form.save()
        return redirect("/notice/list/")
    return render(request, "change.html", {"form": form, "title": title, "back_url": "/notice/list/"})


@require_POST
@validate_post_id
def notice_top(request):
    """ 置顶 / 取消置顶：仅接受 POST。 """
    nid = request.POST.get("nid")
    if nid:
        row_object = models.Notice.objects.filter(id=nid).first()
        if row_object:
            row_object.is_top = not row_object.is_top
            row_object.save(update_fields=["is_top"])
    return redirect("/notice/list/")


@require_POST
@validate_post_id
def notice_delete(request):
    """ 删除公告：仅接受 POST。 """
    nid = request.POST.get("nid")
    if nid:
        models.Notice.objects.filter(id=nid).delete()
    return redirect("/notice/list/")