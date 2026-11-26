"""
公告与任务弹窗的逐用户已读回执。

新手阅读：全局提醒已读回执。只记录此次页面展示的 ID，并再次限制任务接收范围。
bulk_create(ignore_conflicts=True) 配合唯一约束保证重复确认不会生成重复回执。
"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from app01 import models


def _positive_ids(values):
    """把重复或非法的表单值整理为正整数集合。"""
    result = set()
    for value in values:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            result.add(value)
    return result


@login_required
@require_POST
def notification_read(request):
    """确认本次弹窗实际展示的公告与本人任务，重复提交也保持幂等。"""
    notice_ids = _positive_ids(request.POST.getlist("notice_ids"))
    task_ids = _positive_ids(request.POST.getlist("task_ids"))

    valid_notice_ids = models.Notice.objects.filter(id__in=notice_ids).values_list("id", flat=True)
    models.NoticeRead.objects.bulk_create(
        [models.NoticeRead(notice_id=notice_id, user=request.user) for notice_id in valid_notice_ids],
        ignore_conflicts=True,
    )

    # 任务回执必须再次经过统一接收人范围过滤，不能替别人或其他部门确认。
    valid_task_ids = models.Task.objects.assigned_to(request.user).filter(
        id__in=task_ids
    ).values_list("id", flat=True)
    models.TaskRead.objects.bulk_create(
        [models.TaskRead(task_id=task_id, user=request.user) for task_id in valid_task_ids],
        ignore_conflicts=True,
    )

    return JsonResponse({"status": True})
