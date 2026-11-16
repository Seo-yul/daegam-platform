"""요청 본문 크기 상한 (body_limit.py)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from daegam.body_limit import MULTIPART_SLACK, BodyLimitMiddleware
from daegam.urls import API_PREFIX
from tests.helpers.http import api_client, make_app

# 플랫폼 앱에 거는 업로드 상한. 요청 상한은 여기에 MULTIPART_SLACK 을 더한 값이다
LIMIT = 2_000


def _too_large_body(max_bytes: int) -> dict[str, dict[str, str]]:
    return {
        "detail": {
            "code": "payload_too_large",
            "detail": f"request body exceeds {max_bytes} bytes",
        }
    }


TOO_LARGE_100 = _too_large_body(100)


async def _read_then_answer(scope: Scope, receive: Receive, send: Send) -> None:
    """예외 처리기가 없는 맨 ASGI 앱. 본문을 다 읽고 답한다."""
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"read"})


async def _answer_then_read(scope: Scope, receive: Receive, send: Send) -> None:
    """응답을 먼저 시작하고 본문을 읽는 스트리밍 앱."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"first", "more_body": True})
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.body", "body": b"rest"})


def _app(max_bytes: int, **limits: Any) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"len": len(await request.body())}

    app.mount("/stream", _answer_then_read)
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_bytes, **limits)
    return app


def _chunked(body: bytes) -> Iterator[bytes]:
    """Content-Length 없이 64 KiB 씩 흘려보낸다."""
    for i in range(0, len(body), 1 << 16):
        yield body[i : i + (1 << 16)]


@pytest.fixture
def small_client():
    with TestClient(_app(100)) as c:
        yield c


def test_a_tighter_path_limit_applies_only_to_its_path():
    """경로 상한(`tighter`)은 맞는 경로에만 걸린다. 선언한 길이도 받으면서 센 길이도 그 상한으로
    끊고, 다른 경로는 앱 전체의 상한이다."""
    with TestClient(_app(100, tighter=[(re.compile(r"^/echo$"), 10)])) as c:
        assert c.post("/echo", content=b"x" * 10).json() == {"len": 10}
        r = c.post("/echo", content=b"x" * 11)
        assert r.status_code == 413 and r.json() == _too_large_body(10)
        # Content-Length 없이 흘려보내면 라우트가 본문을 읽던 중에 넘는다. 413 은 앱의 HTTPException
        # 처리기가 낸다
        r = c.post("/echo", content=_chunked(b"y" * 11))
        assert r.status_code == 413 and r.json() == _too_large_body(10)
        assert c.post("/stream/", content=b"x" * 50).content == b"firstrest"


def test_an_app_without_a_handler_gets_the_413_from_the_middleware():
    """HTTPException 처리기가 없는 앱에서는 예외가 미들웨어까지 올라온다. 응답 전이므로 미들웨어가
    같은 413 을 낸다. lifespan 을 모르는 앱이라 TestClient 를 with 로 열지 않는다."""
    c = TestClient(BodyLimitMiddleware(_read_then_answer, max_bytes=100))
    r = c.post("/", content=_chunked(b"y" * 150))
    assert r.status_code == 413
    assert r.json() == TOO_LARGE_100
    assert c.post("/", content=_chunked(b"y" * 100)).content == b"read"


def test_a_response_under_way_is_left_as_it_is(small_client, caplog):
    """응답이 시작된 뒤에 넘으면 413 을 낼 수 없다. Starlette 가 처리기 대신 올리는 RuntimeError 를
    밖으로 내지 않고 본문 읽기만 끊는다. 이미 나간 스트리밍 응답의 머리는 그대로다(끝나지 않은
    본문은 TestClient 가 돌려주지 않는다). 삼킨 예외는 종류를 로그에 남긴다."""
    with caplog.at_level(logging.WARNING, logger="daegam.body_limit"):
        r = small_client.post("/stream/", content=_chunked(b"y" * 150))
    assert r.status_code == 200
    assert any("then RuntimeError" in m for m in caplog.messages), caplog.messages
    r = small_client.post("/stream/", content=_chunked(b"y" * 100))
    assert r.status_code == 200 and r.content == b"firstrest"


def test_platform_app_caps_json_routes_at_the_upload_limit(
    settings, mongo, fake_clusters, fake_secrets
):
    """업로드 라우트만 스스로 세던 상한이 모든 라우트에 걸린다. 업로드 상한 + multipart 여유다.
    Content-Length 를 보고 읽기 전에 끊은 413 도 보호 헤더를 받고 감사에 남는다."""
    app = make_app(settings, fake_clusters, fake_secrets, max_upload_bytes=LIMIT)
    with api_client(app) as c:
        big = {"name": "x" * (LIMIT + MULTIPART_SLACK)}
        r = c.post("/backend-apis", json=big)
        assert r.status_code == 413, r.text
        assert r.json() == _too_large_body(LIMIT + MULTIPART_SLACK)
        assert r.headers["x-content-type-options"] == "nosniff"
        # 상한 안의 요청은 평소대로 라우트가 처리한다 (여기서는 검증 오류)
        r = c.post("/backend-apis", json={"name": "x" * 100})
        assert r.status_code in (201, 422), r.text
        # 업로드 상한 바로 위의 파일은 미들웨어가 아니라 업로드 라우트가 판정한다(200 의 too-large)
        r = c.post("/backend-apis/verify", files={"file": ("big.yaml", b"x" * (LIMIT + 1))})
        assert r.status_code == 200, r.text
        verdict = r.json()["verdict"]
        assert verdict["level"] == "error"
        assert [i["code"] for i in verdict["items"]] == ["too-large"]
        # 스펙 업로드는 다른 업로드 라우트와 같은 413 too_large 다 (P74 2.3절)
        files = {"file": ("big.yaml", b"x" * (LIMIT + 1))}
        r = c.post("/backend-apis", data={"name": "big"}, files=files)
        assert r.status_code == 413, r.text
        assert r.json()["detail"]["code"] == "too_large"
    [event] = mongo["audit_events"].find(
        {"path": f"{API_PREFIX}/backend-apis", "status": 413, "error_code": "payload_too_large"}
    )
    assert event["error_code"] == "payload_too_large"


def _multipart(size: int) -> tuple[bytes, str]:
    """(본문, content-type). 경계는 httpx 가 만든 것을 그대로 쓴다."""
    req = httpx.Request(
        "POST", "http://x", data={"name": "big"}, files={"file": ("big.yaml", b"x" * size)}
    )
    return req.read(), req.headers["content-type"]


@pytest.mark.parametrize(
    "path, kind",
    [
        # FastAPI 가 본문 모델(ClusterCreate)로 JSON 을 읽는 라우트
        ("/clusters", "json"),
        # FastAPI 가 File·Form 인자로 폼을 읽는 라우트
        ("/backend-apis", "multipart"),
        # 라우트가 request.form() 을 직접 부르는 라우트
        ("/backend-apis/verify", "multipart"),
    ],
)
def test_platform_app_answers_413_to_a_chunked_body_over_the_limit(
    settings, mongo, fake_clusters, fake_secrets, path, kind
):
    """chunked 본문은 라우트가 읽는 동안 상한을 넘는다. FastAPI 는 본문을 읽다 난 예외를 400
    ("There was an error parsing the body")으로 감싸므로, 어느 라우트든 그 400 이 아니라 413 을
    플랫폼 오류 꼴로 답해야 한다. 감사 기록도 사용자가 받은 413 을 남긴다."""
    app = make_app(settings, fake_clusters, fake_secrets, max_upload_bytes=LIMIT)
    over = LIMIT + MULTIPART_SLACK + 1
    if kind == "json":
        body, ctype = json.dumps({"name": "x" * over}).encode(), "application/json"
    else:
        body, ctype = _multipart(over)
    with api_client(app) as c:
        r = c.post(path, content=_chunked(body), headers={"content-type": ctype})
    assert r.status_code == 413, r.text
    assert r.json() == _too_large_body(LIMIT + MULTIPART_SLACK)
    [event] = mongo["audit_events"].find({"path": f"{API_PREFIX}{path}"})
    assert event["status"] == 413 and event["error_code"] == "payload_too_large"
