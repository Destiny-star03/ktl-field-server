"""사진 업로드 본문 크기 제한 (02 §1.1: 413 FILE_TOO_LARGE — 사진 20 MB 초과).

multipart 본문은 라우터에 닿기 전에 임시 파일로 끝까지 받아지므로, 그 전에 끊지 않으면
아주 큰 요청이 노트북 디스크를 채울 수 있다. 사진 업로드 경로만 본문을 세어 넘으면 바로 413으로 끊는다.
"""
from __future__ import annotations

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response
from app.services.storage import MAX_PHOTO_BYTES, TOO_LARGE_MESSAGE

_PHOTO_UPLOAD_PATH = re.compile(r"/cases/[^/]+/photos/?$")
# multipart 경계·폼 칸(unit_index 등) 몫
_MULTIPART_OVERHEAD = 64 * 1024


class PhotoUploadSizeLimit:
    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else MAX_PHOTO_BYTES + _MULTIPART_OVERHEAD

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or not _PHOTO_UPLOAD_PATH.search(scope["path"]):
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            await _send_413(scope, receive, send)
            return

        received = 0
        rejected = False

        async def limited_receive() -> Message:
            # 넘는 순간 413을 직접 보내고, 안쪽 앱에는 연결이 끊긴 것으로 알려 남은 본문을 받지 않게 한다
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    await _send_413(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:  # 413을 보낸 뒤 안쪽 앱이 내는 응답(400 등)은 버린다
                await send(message)

        await self.app(scope, limited_receive, guarded_send)


async def _send_413(scope: Scope, receive: Receive, send: Send) -> None:
    await error_response(413, "FILE_TOO_LARGE", TOO_LARGE_MESSAGE)(scope, receive, send)
