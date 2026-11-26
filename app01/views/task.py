"""
任务管理（T9，AJAX 派发、完成状态与删除）。

新手阅读：任务 AJAX 派发、列表、完成/恢复、删除（T9）。个人和部门组必须且只能选一个。
三个 QuerySet 方法区分 assigned_to（接收）、editable_by（管理）和 visible_to（可见）。
失败返回字段错误 JSON，不刷新页面；成功回首页刷新分页和计数是有意设计。
"""
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from app01 import models
from app01.utils.form import TaskModelForm
from app01.utils.pagination import Pagination


def _forbidden(request, required_perm):
    return render(
        request,
        "403.html",
        {"required_perm": required_perm, "path": request.path_info},
        status=403,
    )


def task_list(request):
    """编辑者按管理范围查看；普通用户仅看个人任务和本人所属部门组任务。"""
    can_edit = request.user.has_perm_code("task.edit")
    queryset = (
        models.Task.objects.visible_to(request.user)
        .select_related("assignee__employee__department", "assignee_department", "completed_by")
        .order_by("is_completed", "-create_time", "-id")
    )
    completed = request.GET.get("completed", "")
    if completed in ("0", "1"):
        queryset = queryset.filter(is_completed=completed == "1")
    page_object = Pagination(request, queryset, page_size=20)
    task_rows = list(page_object.page_queryset)
    editable_ids = set(
        models.Task.objects.editable_by(request.user)
        .filter(id__in=[item.id for item in task_rows]).values_list("id", flat=True)
    )
    for task_obj in task_rows:
        # 可见任务都是本人/本组任务或权限范围内的任务，完成操作均允许。
        task_obj.can_update_status = True
        task_obj.can_delete = task_obj.id in editable_ids

    form = TaskModelForm(operator=request.user)
    departments = form.fields["assignee_department"].queryset.annotate(
        recipient_count=Count(
            "employee__user_account", filter=Q(employee__user_account__is_active=True)
        )
    )
    context = {
        "queryset": task_rows,
        "page_string": page_object.page_html(),
        "total_count": page_object.total_count,
        "completed_filter": completed,
        "form": form,
        "users": form.fields["assignee"].queryset,
        "departments": departments,
        "can_edit": can_edit,
    }
    return render(request, "task_list.html", context)


def task_ajax_add(request):
    """AJAX 新增任务：派发目标必须是一个个人或一个部门组。"""
    if request.method != "POST":
        return JsonResponse({"status": False, "message": "只接受 POST 请求"}, status=405)
    if not request.user.has_perm_code("task.edit"):
        return _forbidden(request, "task.edit")

    # 把操作者交给表单，禁止客户端通过手写其他部门的负责人 ID 绕过派发范围。
    form = TaskModelForm(data=request.POST, operator=request.user)
    if form.is_valid():
        task = form.save()
        # 派发者若恰好也是本人或目标部门成员，其自己的提醒直接记为已读。
        if models.Task.objects.assigned_to(request.user).filter(id=task.id).exists():
            models.TaskRead.objects.get_or_create(task=task, user=request.user)
        return JsonResponse({
            "status": True,
            "data": {
                "id": task.id,
                "level_display": task.get_level_display(),
                "title": task.title,
                "detail": task.detail,
                "dispatch_target": task.dispatch_target_display,
                "target_type": "user" if task.assignee_id else "department",
                "create_time": timezone.localtime(task.create_time).strftime("%Y-%m-%d %H:%M:%S"),
                "is_completed": task.is_completed,
            },
        })

    errors = {
        field: [item["message"] for item in error_list]
        for field, error_list in form.errors.get_json_data().items()
    }
    return JsonResponse({"status": False, "errors": errors})


@require_POST
def task_status(request, nid):
    """个人负责人、部门组成员或任务编辑者可以切换完成状态。"""
    task_obj = get_object_or_404(models.Task, id=nid)
    can_manage = models.Task.objects.editable_by(request.user).filter(id=nid).exists()
    is_recipient = models.Task.objects.assigned_to(request.user).filter(id=nid).exists()
    if not can_manage and not is_recipient:
        return _forbidden(request, "任务接收人或 task.edit")

    completed_value = request.POST.get("completed")
    if completed_value not in ("0", "1"):
        return HttpResponseBadRequest("完成状态必须为 0（未完成）或 1（已完成）")
    completed = completed_value == "1"
    # 按旧完成状态有条件更新，同一完成请求重复到达时不会覆盖第一次完成人。
    if completed:
        models.Task.objects.filter(id=nid, is_completed=False).update(
            is_completed=True,
            completed_at=timezone.now(),
            completed_by=request.user,
        )
    else:
        models.Task.objects.filter(id=nid, is_completed=True).update(
            is_completed=False,
            completed_at=None,
            completed_by=None,
        )

    if is_recipient:
        models.TaskRead.objects.get_or_create(task=task_obj, user=request.user)
    return redirect("/task/list/")


@require_POST
def task_delete(request, nid):
    """删除任务及其阅读回执；仅 task.edit 或超级管理员可执行。"""
    if not request.user.has_perm_code("task.edit"):
        return _forbidden(request, "task.edit")
    task_obj = get_object_or_404(models.Task, id=nid)
    if not models.Task.objects.editable_by(request.user).filter(id=nid).exists():
        return _forbidden(request, "管理范围内的 task.edit")
    task_obj.delete()
    return redirect("/task/list/")
