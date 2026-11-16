"""내려받기 헤더 (D61, `api/_download.py`).

세션 파일, 인용 원문, 피드백 내보내기는 모두 같은 헤더 다섯으로 나간다. 헤더는 표본
`agent-runtime/schema/fixtures/chat/download-headers.json` 과 같아야 한다. 화면의
`lib/download.ts` 가 같은 표본으로 파일 이름을 읽는다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daegam.api._download import download_headers, download_name, download_response
from tests.helpers.agents import AGENT_FIXTURES

FIXTURE = AGENT_FIXTURES / "chat" / "download-headers.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@pytest.mark.parametrize("case", CASES, ids=[c["filename"] for c in CASES])
def test_headers_match_the_shared_fixture(case: dict[str, Any]) -> None:
    """한글 이름은 `filename*` 에 퍼센트 인코딩으로, ASCII 대체 이름에는 `_` 로 간다. CR·LF·따옴표는
    빠지고 역슬래시는 대체 이름에서만 바뀐다. `.html` 도 형식을 짐작하지 못하는 첨부다."""
    assert download_name(case["path"]) == case["filename"]
    headers = download_headers(case["path"], media_type=case["media_type"])
    assert _lower(headers) == case["headers"]


def test_responses_carry_the_headers_for_bytes_and_streams() -> None:
    """바이트 본문은 한 번에, 반복자는 조각마다 보낸다. 어느 쪽이든 헤더 다섯이 그대로 실린다."""
    case = CASES[0]
    app = FastAPI()

    async def chunks():
        yield b"line 1\n"
        yield b"line 2\n"

    @app.get("/bytes")
    async def as_bytes():
        return download_response(b"line 1\nline 2\n", case["path"])

    @app.get("/stream")
    async def as_stream():
        return download_response(chunks(), case["path"])

    with TestClient(app) as client:
        for route in ("/bytes", "/stream"):
            r = client.get(route)
            assert r.status_code == 200 and r.content == b"line 1\nline 2\n"
            assert {k: r.headers.get(k) for k in case["headers"]} == case["headers"]
