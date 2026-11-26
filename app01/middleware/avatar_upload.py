"""
头像上传入口限流：在 CSRF 中间件读取 request.POST 之前替换上传处理器。
只接收一个 avatar 字段，限制字节并存入内存；正式图片解码校验在 utils/avatars.py。
"""
import re
from io import BytesIO

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpResponseBadRequest
from app01.utils.avatars import MAX_AVATAR_BYTES

AVATAR_UPLOAD_PATH = re.compile(r"^(?:/profile/avatar/|/avatars/\d+/edit/)$")


class AvatarMemoryUploadHandler(FileUploadHandler):
    chunk_size = 64 * 1024

    def __init__(self, request):
        super().__init__(request)
        self.file_count = 0
        self.received = 0
        self.file = None

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.file_count += 1
        if self.file_count > 1 or self.field_name != "avatar":
            self.request._avatar_upload_error = "每次只能上传一张头像图片。"
            raise StopUpload(connection_reset=True)
        self.file = BytesIO()
        self.received = 0

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > MAX_AVATAR_BYTES:
            self.request._avatar_upload_error = "头像图片不能超过 2 MB。"
            self.file.close()
            raise StopUpload(connection_reset=True)
        self.file.write(raw_data)
        return None

    def file_complete(self, file_size):
        self.file.seek(0)
        return InMemoryUploadedFile(self.file, self.field_name, self.file_name,
                                    self.content_type, self.received, self.charset,
                                    content_type_extra=self.content_type_extra)

    def upload_interrupted(self):
        if self.file:
            self.file.close()


class AvatarUploadLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and AVATAR_UPLOAD_PATH.fullmatch(request.path_info):
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                return HttpResponseBadRequest("上传长度不合法。")
            # A little room for CSRF/multipart framing, never for another large file.
            if length < 0 or length > MAX_AVATAR_BYTES + 64 * 1024:
                return HttpResponseBadRequest("头像图片不能超过 2 MB。", status=413)
            request.upload_handlers = [AvatarMemoryUploadHandler(request)]
        return self.get_response(request)
