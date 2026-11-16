"""요청 본문 크기 상한. 순수 ASGI 미들웨어라 `audit/middleware.py` 와 같은 꼴이다.

JSON 라우트는 본문 크기를 보지 않았다. 업로드 라우트만 `max_upload_bytes` 를 스스로 세고, 나머지는
FastAPI 가 본문을 통째로 읽어 파싱했다. 수백 MB 의 patch 하나가 파드 메모리를 다 쓴다.

같은 상한을 모든 요청에 건다. 업로드 상한에 multipart 경계·필드 여유(`MULTIPART_SLACK`)를 더한
값이 요청 상한이다. 여유가 없으면 상한 크기의 파일이 경계 몇 줄 때문에 여기서 413 으로 끊겨,
업로드 라우트가 내던 "too_large" 리포트를 사용자가 보지 못한다. `Content-Length` 가 있으면 읽기
전에 413 으로 끊고, 없으면(chunked) 받으면서 세다가 넘는 순간 끊는다. 스트리밍 응답(SSE)은 응답
쪽이라 상관없다.

chunked 본문은 라우트가 읽는 도중에 상한을 넘으므로 413 은 앱이 낸다. 그래서 receive 래퍼는
HTTPException(`_payload_too_large`)을 던진다. FastAPI 는 본문을 읽다 난 예외를 400("There was an
error parsing the body")으로 감싸지만 HTTPException 은 그대로 올린다. 본문 모델이나 폼 인자로 읽는
라우트와 `request.form()` 을 직접 부르는 라우트가 모두 앱의 예외 처리기에서 같은 413 을 플랫폼 오류
꼴로 낸다. 이 미들웨어는 감사·보호 헤더 미들웨어의 안쪽에 있어(`main.py`) 읽기 전에 끊은 413 과 앱이
낸 413 이 모두 감사에 413 으로 남고 보호 헤더도 받는다.

앱 전체보다 작은 상한을 둘 경로는 `tighter` 로 받는다. 채팅 첨부 업로드(문서 10MiB)가 그렇다.
FastAPI 는 폼 인자를 라우트보다 먼저 읽으므로 라우트에서는 본문을 받기 전에 끊을 수 없다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from daegam.errors import error
from daegam.request_body import declared_length

logger = logging.getLogger("daegam.body_limit")

MULTIPART_SLACK = 1 << 20  # 1 MiB. multipart 경계·파일 이름·폼 필드 몫이다
# (경로 정규식, 상한). 앱 전체보다 작은 상한을 둘 경로다
PathLimits = Sequence[tuple[re.Pattern[str], int]]


def _payload_too_large(max_bytes: int) -> HTTPException:
    """상한을 넘은 본문의 413. receive 래퍼가 던지면 앱의 HTTPException 처리기가 내고, 미들웨어가
    직접 끊을 때도 같은 본문을 쓴다."""
    return error(413, "payload_too_large", f"request body exceeds {max_bytes} bytes")


def _too_large(max_bytes: int) -> tuple[bytes, list[tuple[bytes, bytes]]]:
    body = json.dumps({"detail": _payload_too_large(max_bytes).detail}).encode()
    return body, [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int, tighter: PathLimits = ()):
        self.app = app
        self.max_bytes = max_bytes
        self.tighter = tuple(tighter)

    def _limit(self, scope: Scope) -> int:
        """이 요청의 상한. 경로가 맞는 첫 `tighter` 상한이고 없으면 앱 전체의 상한이다."""
        path = scope.get("path", "")
        return next((n for pattern, n in self.tighter if pattern.match(path)), self.max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        declared = declared_length(scope)
        if declared is not None and declared > limit:
            await _reject(send, limit)
            return

        received = 0
        cut = False
        started = False

        async def counting_receive() -> Message:
            nonlocal received, cut
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    cut = True
                    raise _payload_too_large(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except Exception as e:
            # 보통은 앱의 처리기가 413 을 내서 여기까지 오지 않는다. 오는 것은 HTTPException
            # 처리기가 없는 앱이거나, 응답이 이미 시작돼 Starlette 가 처리기 대신 RuntimeError 를
            # 올린 경우다. 본문을 끊은 요청이 아니면 그대로 올린다
            if not cut:
                raise
            # 끊은 뒤에 난 다른 예외는 삼키되 그 종류를 남긴다
            if not (isinstance(e, HTTPException) and e.status_code == 413):
                logger.warning("request body cut at %d bytes, then %s", limit, type(e).__name__)
            # 응답 전이면 여기서 413 을 낸다. 시작됐다면 이미 늦었다
            if not started:
                await _reject(send, limit)


async def _reject(send: Send, max_bytes: int) -> None:
    body, headers = _too_large(max_bytes)
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": body})
