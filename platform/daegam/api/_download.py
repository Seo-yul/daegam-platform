"""내려받기 응답의 헤더 (D61). 세션 파일(P36 F12), 인용 원문(F6), 피드백 내보내기(F5), Skill
리비전 내보내기(D64)가 모두 이 한 곳에서 헤더를 만든다.

플랫폼 origin 에서 에이전트가 쓴 `.html` 이 문서로 열리면 저장형 XSS 이므로 어떤 형식도
inline 으로 내지 않는다. 런타임이 프록시로 돌려준 헤더는 넘기지 않고 여기서 다시 만든다. 헤더
다섯은 표본 `agent-runtime/schema/fixtures/chat/download-headers.json` 과 같아야 하고, 런타임이
외부 호출자에게 직접 내는 헤더도 같은 표본에 댄다.

파일 이름은 경로의 마지막 조각에서 CR·LF·따옴표를 뺀 것이다. `filename` 은 인쇄할 수 있는
ASCII 만 남긴 대체 이름이고, 브라우저는 퍼센트 인코딩한 UTF-8 원래 이름 `filename*` 를 먼저
읽는다.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from starlette.responses import Response

from daegam.api._sse import ClosingStreamingResponse

OCTET_STREAM = "application/octet-stream"
NDJSON = "application/x-ndjson"  # 피드백 내보내기(P36 F5)만 이 형식이다
FALLBACK_NAME = "download"
# 파일 이름에서 빼는 글자다. 헤더를 끊거나(CR·LF) 따옴표 문자열을 닫는다(따옴표)
_UNSAFE_NAME = re.compile(r'[\r\n"]')
# ASCII 대체 이름에서 `_` 로 바꾸는 글자다. 인쇄할 수 있는 ASCII 밖과 따옴표 문자열의
# 이스케이프 글자다
_NOT_PLAIN_ASCII = re.compile(r"[^\x20-\x7e]|\\")


def download_name(path: str) -> str:
    """경로의 마지막 조각에서 CR·LF·따옴표를 뺀 파일 이름이다. 남는 것이 없으면
    `download` 다."""
    return _UNSAFE_NAME.sub("", path.rsplit("/", 1)[-1]) or FALLBACK_NAME


def content_disposition(name: str) -> str:
    """`attachment` 와 ASCII 대체 이름, UTF-8 원래 이름이다. name 은 `download_name` 을
    지난 값이다."""
    fallback = _NOT_PLAIN_ASCII.sub("_", name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def download_headers(path: str, *, media_type: str = OCTET_STREAM) -> dict[str, str]:
    """내려받기 헤더 다섯이다. 형식을 짐작하지 못하게 막고, 캐시하지 않으며, 문서로 열려도
    스크립트가 돌지 않게 sandbox 다."""
    return {
        "Content-Type": media_type,
        "Content-Disposition": content_disposition(download_name(path)),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    }


def download_openapi(description: str) -> dict[int | str, dict[str, Any]]:
    """바이트로 내려받는 라우트의 OpenAPI 200 응답이다. 인용 원문, 세션 파일, Skill 리비전
    내보내기(D64)가 쓴다."""
    binary = {"schema": {"type": "string", "format": "binary"}}
    return {200: {"description": description, "content": {OCTET_STREAM: binary}}}


def download_response(
    body: bytes | AsyncIterator[bytes], path: str, *, media_type: str = OCTET_STREAM
) -> Response:
    """본문을 내려받기 헤더로 낸다. 바이트는 한 번에 보내고, 반복자는 조각마다 보내다가 끊기면
    곧바로 닫는다. 바이트 갈래는 인용 원문(P36 F6)처럼 플랫폼이 다 읽은 본문을 낼 때 쓰고,
    반복자 갈래는 런타임 스트림을 넘기는 `proxied_download` 가 쓴다."""
    headers = download_headers(path, media_type=media_type)
    if isinstance(body, bytes):
        return Response(body, headers=headers)
    return ClosingStreamingResponse(body, headers=headers)
