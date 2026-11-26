"""
全局提醒上下文：为所有已登录页面提供未读公告和新派发任务。

模板上下文处理器：render() 渲染页面时，把未读公告和本人任务加入模板变量。
公告是系统广播；任务仍经过 assigned_to() 按本人/当前部门组过滤。
“已读”记录逐用户保存；一次确认不会替其他人确认。
"""

from app01 import models


def pending_notifications(request):
    """返回当前用户尚未确认的公告与任务提醒。

    只在模板实际渲染时执行；匿名用户和登录页不会查询业务表。
    阅读状态按用户单独记录，因此一个人的确认不会影响其他人。
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {
            "pending_notice_notifications": [],
            "pending_task_notifications": [],
        }

    # 模板会读取 publisher.display_name；回退员工姓名需要把 publisher__employee 一并预载。
    notices = list(
        models.Notice.objects.select_related("publisher__employee")
        .exclude(read_records__user=user)
        .order_by("-is_top", "-create_time")
    )
    tasks = list(
        models.Task.objects.assigned_to(user)
        .select_related("assignee__employee__department", "assignee_department", "completed_by")
        .exclude(read_records__user=user)
        .order_by("is_completed", "-create_time")
    )
    return {
        "pending_notice_notifications": notices,
        "pending_task_notifications": tasks,
    }
