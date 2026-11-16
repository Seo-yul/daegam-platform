"""요청 본문을 상한까지만 읽는 자리. 본문 크기 상한 미들웨어(`body_limit.py`)와 그보다 작은 상한을
스스로 두는 자리(OAuth 등록·토큰 라우트, 문서 파싱 서비스 `daegam-docparse`)가 함께 쓴다.

선언한 길이(`Content-Length`)가 상한을 넘으면 본문을 읽지 않는다. 없으면(chunked) 받으면서 세다가
넘는 순간 멈춘다. starlette 만 부르므로 문서 파싱 서버가 FastAPI 를 끌어오지 않는다.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import Scope


def declared_length(scope: Scope) -> int | None:
    """요청이 선언한 본문 길이. 헤더가 없거나 숫자가 아니면 None 이다."""
    for k, v in scope.get("headers") or []:
        if k == b"content-length":
            try:
                return int(v)
            except ValueError:
                return None
    return None


async def body_upto(request: Request, limit: int) -> bytes | None:
    """본문을 limit 바이트까지만 읽는다. 선언한 길이가 넘으면 읽지 않고, 받으면서 넘으면 읽기를
    멈춘다. 둘 다 None 이다."""
    declared = declared_length(request.scope)
    if declared is not None and declared > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


__all__ = ["body_upto", "declared_length"]
