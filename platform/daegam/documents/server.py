"""문서 파싱 서비스 `daegam-docparse` (D61). 플랫폼 이미지를 명령만 바꿔 띄운다.

    python -m daegam.documents.server        (env DAEGAM_DOCPARSE_TOKEN, 포트 9100)

받는 것은 `POST /extract?format=&purpose=` 하나와 준비 확인 `GET /healthz` 다. 본문은 문서 바이트
그대로이고(10MiB, 넘으면 413 `too_large`) 요청은 Bearer 토큰으로 확인한다(없거나 틀리면 401). 형식은
파서가 필요한 여섯(html·pdf·docx·pptx·xlsx·hwpx)뿐이고 텍스트 형식은 플랫폼이 스스로 푼다. 답은
`{text, pages, sheets, truncated}` 이고 오류는 `{code, detail}` 이다. 추출 오류는 422 다
(`documents/result.py`).

레플리카마다 동시 추출은 둘이고 넘치면 429 `extract_busy`(`Retry-After`)다. 두 자리 가운데
하나는 첨부 몫이라 인덱서 요청(`purpose=index`)은 한 자리만 쓴다. 인덱서가 몰려도 첨부가 자리를
잃지 않는다. 추출은 요청마다 새 하위 프로세스이고 가두기와 거두기는 `sandbox` 가 한다. 서버는
기동할 때 스스로 dumpable 을 0 으로 내린다.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from daegam.documents import DOCPARSE_PORT, TOKEN_ENV
from daegam.documents.formats import MAX_DOCUMENT_BYTES, PARSED_FORMATS
from daegam.documents.result import INDEX, PURPOSES, Extracted, ExtractError
from daegam.documents.sandbox import EXTRACT_TIMEOUT, run_worker, set_non_dumpable
from daegam.error_codes import status_of
from daegam.request_body import body_upto

logger = logging.getLogger("daegam.docparse")

SLOTS = 2
INDEX_SLOTS = 1
BUSY_RETRY_AFTER = 5
PORT_ENV = "DAEGAM_DOCPARSE_PORT"
Runner = Callable[..., Awaitable[dict[str, Any]]]


class Slots:
    """레플리카 하나의 추출 자리. 이벤트 루프 하나에서만 세므로 잠금이 없다."""

    def __init__(self, total: int = SLOTS, index: int = INDEX_SLOTS):
        self.total = total
        self.index = index
        self.busy = 0
        self.index_busy = 0

    def take(self, purpose: str) -> bool:
        if self.busy >= self.total or (purpose == INDEX and self.index_busy >= self.index):
            return False
        self.busy += 1
        self.index_busy += purpose == INDEX
        return True

    def give_back(self, purpose: str) -> None:
        self.busy -= 1
        self.index_busy -= purpose == INDEX


def _error(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail}, status, headers=headers)


def _authorized(request: Request, token: str) -> bool:
    given = request.headers.get("authorization", "")
    return hmac.compare_digest(given.encode(), f"Bearer {token}".encode())


def _query(request: Request) -> tuple[str, str] | JSONResponse:
    """(형식, 목적). 틀리면 422 응답이다."""
    fmt, purpose = request.query_params.get("format"), request.query_params.get("purpose")
    if fmt is None or fmt not in PARSED_FORMATS:
        return _error(422, "unsupported_format", f"format must be one of {sorted(PARSED_FORMATS)}")
    if purpose is None or purpose not in PURPOSES:
        return _error(422, "invalid_purpose", f"purpose must be one of {list(PURPOSES)}")
    return fmt, purpose


def create_app(
    token: str,
    *,
    run: Runner = run_worker,
    slots: Slots | None = None,
    timeout: float = EXTRACT_TIMEOUT,
) -> Starlette:
    """`run` 과 `timeout` 은 테스트가 바꿔 끼우는 자리다."""
    seats = slots or Slots()

    async def extract(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _error(401, "unauthorized", "a valid bearer token is required")
        query = _query(request)
        if isinstance(query, JSONResponse):
            return query
        fmt, purpose = query
        if not seats.take(purpose):
            busy = {"Retry-After": str(BUSY_RETRY_AFTER)}
            return _error(429, "extract_busy", "all extraction slots are busy", busy)
        try:
            return await _extract(request, fmt, purpose)
        finally:
            seats.give_back(purpose)

    async def _extract(request: Request, fmt: str, purpose: str) -> JSONResponse:
        data = await body_upto(request, MAX_DOCUMENT_BYTES)
        if data is None:
            return _error(413, "too_large", f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
        request_line = {"task": "extract", "format": fmt, "purpose": purpose}
        try:
            result = Extracted.from_dict(await run(request_line, data, timeout=timeout))
        except ExtractError as e:
            if e.code in ("extract_failed", "extract_timeout"):
                logger.warning("extraction of a %s document failed: %s", fmt, e)
            return _error(status_of(e.code, 422), e.code, e.detail)
        return JSONResponse(result.to_dict())

    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/extract", extract, methods=["POST"]), Route("/healthz", healthz)]
    )


def main() -> int:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        print(f"daegam-docparse: {TOKEN_ENV} is empty", file=sys.stderr)
        return 2
    set_non_dumpable()
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get(PORT_ENV) or DOCPARSE_PORT)
    uvicorn.run(create_app(token), host="0.0.0.0", port=port, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
