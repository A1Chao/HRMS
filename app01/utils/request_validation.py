"""
请求参数边界：先把外部 ID 校验为正的 64 位整数，再交给 ORM。
装饰器保留原视图签名；空 ID 由原视图处理，非法的非空 ID 返回 400 而不是服务器异常。
"""
from functools import wraps

from django.http import HttpResponseBadRequest


def positive_id(value):
    """返回校验后的整数 ID，非法/超长/非正数抛 ValueError，避免直接把坏参数送进 ORM。"""
    text = str(value) if value is not None else ""
    if not text or len(text) > 19 or not text.isascii() or not text.isdecimal():
        raise ValueError("ID must be a positive 64-bit integer")
    result = int(text)
    if not 0 < result <= 9223372036854775807:
        raise ValueError("ID is out of range")
    return result


def validate_post_id(view):
    """包装写视图，先验证 POST 中的 nid，非法非空值直接返回 HTTP 400。"""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        value = request.POST.get("nid")
        if value:
            try:
                positive_id(value)
            except ValueError:
                return HttpResponseBadRequest("记录 ID 必须为有效的正整数")
        return view(request, *args, **kwargs)
    return wrapped
