"""P35 F2p·F2b·F12, P36 F1: 런타임 프록시 `AgentRuntimeProxy` (D21, `services/agent_runtime.py`)와
라우트 도우미(`api/_agent_proxy.py`), 중지 라우트, 원격 클러스터의 경로(D41).

가짜 런타임 ASGI 앱(tests/helpers/runtime.py 의 `fake_runtime`)에 대고 SSE 프록시(`/chat`·재개 턴),
조회 넷(세션 목록·상세·보류·메트릭), 중지(D65), 승인 헤더, 런타임 상태·본문·Retry-After 그대로
넘기기, 플랫폼 형식의 오류, 런타임 능력 가르기(`require_runtime_feature`)를 든다. P36 이 넓힌
것은 아무 메서드와 쿼리의 JSON·바이트 스트림 호출, 롤링 창의 409 runtime_rolling(FastAPI 기본
404·405 만), 상태를 한 번 새로 읽는 phase 판단, 런타임 헤더를 버리는 내려받기, 상태의 `chat`
이다. 직행이 아닌 경로는 `portforward_client` 자리에 fake 브리지를 넣어(test_svc_tokens.py 의 꼴)
Bearer 가 터널로 가는 것, 터널의 파드 고르기, 터널이 헤더와 읽기 시간을 싣는 것, 목록 조회의
런타임 수치 생략, 스트림 문맥 닫기, 플랫폼 `ClosingStreamingResponse`, 허브 목록의 같은 함수 쓰기를
든다. 렌더는 test_agent_runtime.py, 상태·재배포·삭제는 test_agent_runtime_lifecycle.py, 준비되지
않은 파드의 이유(T21)는 test_agent_runtime_unready.py 가 본다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import Any

import httpx2
import pytest
from fastapi import HTTPException, Request

import daegam.k8s.portforward as pf
from daegam.api._agent_proxy import (
    proxied_download,
    runtime_error,
    stream_error,
)
from daegam.api._download import download_headers
from daegam.api._sse import ClosingStreamingResponse
from daegam.audit.middleware import _error_code
from daegam.k8s.portforward import PortForwardError, ready_pod_for_service
from daegam.services.agent_features import CANCEL, FEATURES_HEADER, IMAGE_HEADER, RUNTIME_ROLLING
from daegam.services.agent_runtime import (
    APPROVER_HEADER,
    CHAT_READ_TIMEOUT,
    RUNTIME_TIMEOUT,
    ProxyResponse,
)
from tests.helpers.agent_runtime import deployed_agent, token_of
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, IMAGE, T2
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import detail_code
from tests.helpers.loop_guard import allow_on_loop
from tests.helpers.runtime import (
    fake_runtime,
    record_call,
    replace_get,
    session_row,
)
from tests.helpers.svc_tokens import header_echo_server


def _events(body: str) -> list[str]:
    return [line.split(" ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]


def test_chat_proxy_streams_sse_and_passes_runtime_status_through(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo)
    fake = agent.fake
    url = agent.url

    r = client.post(f"{url}/chat", json={"message": "3 더하기 4"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-runtime"] == "fake" and "content-length" not in r.headers
    assert _events(r.text) == ["token", "tool_call", "done"]
    assert '"session_id": "s-new"' in r.text
    call = agent.runtime.state.calls[-1]
    assert call["path"] == "/chat"
    assert call["body"] == {
        "session_id": None,
        "message": "3 더하기 4",
        "user_sub": "header",
        "ephemeral": False,
    }
    assert call["headers"]["authorization"] == f"Bearer {agent.token}"
    assert APPROVER_HEADER.lower() not in call["headers"]
    # 세션 이어가기와 429 + Retry-After 그대로
    r = client.post(f"{url}/chat", json={"message": "다음", "session_id": "s1"})
    assert r.status_code == 200 and agent.runtime.state.calls[-1]["body"]["session_id"] == "s1"
    # 테스트 채팅은 ephemeral 을 실어 이 턴에 장기 기억 저장소를 안 붙인다 (D34)
    r = client.post(f"{url}/chat", json={"message": "던져 보는 말", "ephemeral": True})
    assert r.status_code == 200 and agent.runtime.state.calls[-1]["body"]["ephemeral"] is True
    # 스트리밍 경로의 오류도 플랫폼 형식이다. 코드·설명·Retry-After 가 UI 가 읽는 자리에 온다
    r = client.post(f"{url}/chat", json={"message": "busy"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert r.json() == {
        "detail": {
            "code": "max_concurrent_sessions",
            "detail": "동시에 열 수 있는 세션 2개가 모두 찼다",
            "retry_after": "60",
        }
    }
    # 본문 검증은 플랫폼이 한다
    assert client.post(f"{url}/chat", json={"message": ""}).status_code == 422
    assert client.post(f"{url}/chat", json={"message": "x", "extra": 1}).status_code == 422

    # 세션·보류·메트릭. platform-admin(header 모드)은 approver 다
    r = client.get(f"{url}/sessions")
    assert r.status_code == 200 and r.json()["sessions"][0]["user_sub"] == "header"
    assert agent.runtime.state.calls[-1]["params"] == {"user_sub": "header", "limit": "100"}
    assert agent.runtime.state.calls[-1]["headers"][APPROVER_HEADER.lower()] == "1"
    r = client.get(f"{url}/sessions", params={"all_users": "true", "status": "pending"})
    assert r.status_code == 200 and r.json()["sessions"][0]["user_sub"] == "anyone"
    assert agent.runtime.state.calls[-1]["params"] == {"status": "pending", "limit": "100"}
    assert client.get(f"{url}/sessions", params={"status": "nope"}).status_code == 422
    r = client.get(f"{url}/sessions/s1")
    assert r.status_code == 200 and r.json()["pending"] is True
    assert agent.runtime.state.calls[-1]["params"] == {"user_sub": "header"}
    # 다른 사람 세션도 approver 라 볼 수 있다. 없는 세션은 런타임 404 가 플랫폼 형식으로 온다
    assert client.get(f"{url}/sessions/other").status_code == 200
    r = client.get(f"{url}/sessions/missing")
    assert detail_code(r) == (404, "session_not_found")
    r = client.get(f"{url}/sessions/s1/pending")
    assert r.status_code == 200 and r.json()["action_requests"][0]["name"] == "Divide"
    r = client.get(f"{url}/metrics")
    assert r.status_code == 200 and r.json()["sessions"] == {"active": 2, "pending": 1, "closed": 5}
    # 소스마다 맺은 MCP 규약 판과 이미지가 아는 이름은 응답 모델에 적힌 칸이라 그대로 지난다(P35).
    # 옛 런타임의 파드 카운터(pod, turns)는 버리고 턴의 수는 이벤트 저장소에서 센다(P37 11절)
    body = r.json()
    assert body["tools"]["protocols"] == {"tools-calculator": "2026-07-28"}
    assert body["runtime"] == {"features": [CANCEL]} and body["runtime_error"] is None
    assert "pod" not in body and body["turns"]["done"] == 0

    # 결정: 재개 턴은 SSE 이고 approver 헤더와 결정자 sub 가 간다
    r = client.post(f"{url}/sessions/s1/decisions", json={"decisions": [{"type": "approve"}]})
    assert r.status_code == 200 and _events(r.text) == ["token", "tool_call", "done"]
    call = agent.runtime.state.calls[-1]
    assert call["path"] == "/sessions/s1/decisions"
    approve_body = {"decisions": [{"type": "approve"}], "ephemeral": False}
    assert call["body"] == {**approve_body, "user_sub": "header"}
    assert call["headers"][APPROVER_HEADER.lower()] == "1"
    r = client.post(
        f"{url}/sessions/s1/decisions",
        json={"decisions": [{"type": "approve"}, {"type": "reject"}]},
    )
    assert detail_code(r) == (422, "decision_count")
    assert client.post(f"{url}/sessions/s1/decisions", json={"decisions": []}).status_code == 422
    # 고치기 검사의 422 는 코드와 extra(rule)가 그대로 플랫폼 형식으로 온다(D65)
    edit = {"type": "edit", "edited_action": {"name": "Multiply", "args": {"a": 1, "b": 2}}}
    r = client.post(f"{url}/sessions/s1/decisions", json={"decisions": [edit]})
    assert detail_code(r) == (422, "edit_name_mismatch")
    edit = {"type": "edit", "edited_action": {"name": "Divide", "args": {"a": 1, "b": 666}}}
    r = client.post(f"{url}/sessions/s1/decisions", json={"decisions": [edit]})
    assert r.status_code == 422
    assert r.json()["detail"] == {
        "code": "guardrail_blocked",
        "detail": "그 수는 안 된다",
        "rule": "deny_pattern",
    }

    # P73 2절: 런타임이 쓴 모양(메시지 요약, 전사, 보류 요청)은 서버 스키마에서 자유 객체라 null
    # 칸과 새 전사 종류(새 런타임 이미지)가 500 없이 그대로 간다. 전사를 싣지 않는 옛 런타임이면
    # `transcript` 는 null 이다
    items = [
        {"kind": "error", "code": None, "message": "boom", "turn_id": None},
        {"kind": "fallback", "from": None, "to": None},
        {"kind": "future_kind", "anything": [1, None]},
    ]
    messages = [{"role": "tool", "text": "x", "tool": None, "status": None}]

    async def session(sid: str):
        transcript = {"items": items, "limit": None}
        return {
            "session": session_row(sid, "header"),
            "messages": messages,
            "pending": False,
            "transcript": transcript,
        }

    async def pending(sid: str):
        requests = [{"name": "Divide", "args": None, "description": None, "files": None}]
        configs = [{"action_name": None}]
        return {"session_id": None, "action_requests": requests, "review_configs": configs}

    replace_get(agent.runtime, "/sessions/{sid}", session)
    replace_get(agent.runtime, "/sessions/{sid}/pending", pending)
    r = client.get(f"{url}/sessions/s1")
    assert r.status_code == 200, r.text
    assert r.json()["messages"] == messages
    assert r.json()["transcript"] == {"items": items, "limit": None}
    r = client.get(f"{url}/sessions/s1/pending")
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] is None
    assert r.json()["action_requests"][0]["args"] is None

    async def old_session(sid: str):
        return {"session": session_row(sid, "header"), "messages": [], "pending": False}

    replace_get(agent.runtime, "/sessions/{sid}", old_session)
    r = client.get(f"{url}/sessions/s1")
    assert r.status_code == 200, r.text
    assert r.json()["transcript"] is None

    # 런타임이 토큰을 거부하면 SSE 경로도 JSON 경로도 플랫폼 형식으로 온다
    fake.service_app = fake_runtime("another")
    r = client.post(f"{url}/chat", json={"message": "hi"})
    assert detail_code(r) == (401, "unauthorized")
    r = client.get(f"{url}/sessions")
    assert detail_code(r) == (401, "unauthorized")
    fake.unreachable = True
    r = client.post(f"{url}/chat", json={"message": "hi"})
    assert detail_code(r) == (502, "cluster_unreachable")
    fake.unreachable = False
    # 없는 문서·다른 테넌트는 404
    assert client.post(f"{AG}/nope/chat", json={"message": "hi"}).status_code == 404
    assert client.post(f"{url}/chat", json={"message": "hi"}, headers=T2).status_code == 404


def test_token_cache_is_dropped_when_the_token_secret_is_rewritten(client, fake_clusters, mongo):
    """회수된 인스턴스 토큰은 재배포가 새로 내어 Secret 을 다시 쓴다. 프록시는 캐시한 옛 토큰이
    아니라 다시 쓴 Secret 의 토큰을 붙인다(`forget_token`)."""
    agent = deployed_agent(client, fake_clusters, mongo)
    fake = agent.fake
    svc = client.app.state.agent_service
    url = agent.url
    old = agent.token
    assert client.get(f"{url}/sessions").status_code == 200  # 캐시가 찬다
    client.portal.call(lambda: svc.tokens.revoke_for_instance(agent.id))
    r = client.post(f"{url}/redeploy")
    assert r.status_code == 200, r.text
    new = token_of(fake, r.json())
    assert new != old
    runtime = fake_runtime(new)
    fake.service_app = runtime
    r = client.get(f"{url}/sessions")
    assert r.status_code == 200, r.text
    assert runtime.state.calls[-1]["headers"]["authorization"] == f"Bearer {new}"


# -- 중지 (D65) --------------------------------------------------------------------------------


def test_cancel_route_gates_on_the_record_and_proxies_with_the_decision_subject(
    client, fake_clusters, mongo
):
    """기록에 `cancel` 이 없으면 런타임을 부르지 않고 409 다. init 이 알린 뒤에는 결정과 같은 주체로
    프록시하고 런타임의 202·409·404 를 플랫폼 형식으로 넘긴다."""
    agent = deployed_agent(client, fake_clusters, mongo)
    url = agent.url
    auth = bearer(agent.token)

    def cancel_calls() -> list[dict]:
        return [c for c in agent.runtime.state.calls if c["path"].endswith("/cancel")]

    # 기록이 없다(첫 배포). 런타임을 부르지 않는다
    r = client.post(f"{url}/sessions/s1/cancel")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "runtime_feature_missing",
        "detail": "the deployed runtime image has not reported the 'cancel' capability",
        "feature": "cancel",
    }
    # cancel 을 모르는 이미지(이름 머리 없는 옛 init)의 기록도 같다
    assert client.get(f"{url}/agent.json", headers={**auth, IMAGE_HEADER: IMAGE}).status_code == 200
    r = client.post(f"{url}/sessions/s1/cancel")
    assert detail_code(r) == (409, "runtime_feature_missing")
    assert cancel_calls() == []
    # init 이 cancel 을 알리면 프록시한다. platform-admin(header 모드)은 approver 라 머리가 간다
    reported = {**auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: "cancel"}
    assert client.get(f"{url}/agent.json", headers=reported).status_code == 200
    r = client.post(f"{url}/sessions/s1/cancel")
    assert r.status_code == 202, r.text
    assert r.json() == {"session_id": "s1"}
    call = cancel_calls()[-1]
    assert call["path"] == "/sessions/s1/cancel" and call["body"] == {"user_sub": "header"}
    assert call["headers"][APPROVER_HEADER.lower()] == "1"
    # 런타임의 409(진행 중 턴 없음)·404 는 플랫폼 형식이다
    r = client.post(f"{url}/sessions/idle/cancel")
    assert detail_code(r) == (409, "no_turn_in_progress")
    r = client.post(f"{url}/sessions/missing/cancel")
    assert detail_code(r) == (404, "session_not_found")
    # 없는 문서는 404. 상태 조회의 runtime 칸이 화면의 중단 단추 판정 원천이다
    assert client.post(f"{AG}/nope/sessions/s1/cancel").status_code == 404
    st = client.get(url).json()["status"]
    assert st["runtime"] == {"image": IMAGE, "features": ["cancel"]}

    # 서비스 수준: approver 가 아니면(viewer) 머리가 없어 남의 세션은 런타임의 403 이다
    svc = client.app.state.agent_service

    async def cancel(approver: bool):
        doc = await svc.agents.get(agent.id)
        return await svc.proxy.cancel(doc, "other", user_sub="viewer-1", approver=approver)

    st_code, body = client.portal.call(lambda: cancel(False))
    assert st_code == 403 and body["code"] == "not_session_owner"
    assert APPROVER_HEADER.lower() not in cancel_calls()[-1]["headers"]
    assert cancel_calls()[-1]["body"] == {"user_sub": "viewer-1"}
    st_code, body = client.portal.call(lambda: cancel(True))
    assert st_code == 202 and body == {"session_id": "other"}


def test_cancel_names_the_attempt(client, fake_clusters, mongo):
    """중지 본문의 turn_id 는 런타임까지 가고 없으면 싣지 않는다(P78 2절)."""
    agent = deployed_agent(client, fake_clusters, mongo, features="cancel")
    url = agent.url

    def cancel_calls() -> list[dict]:
        return [c for c in agent.runtime.state.calls if c["path"].endswith("/cancel")]

    r = client.post(f"{url}/sessions/s1/cancel", json={"turn_id": "t-7"})
    assert r.status_code == 202, r.text
    assert cancel_calls()[-1]["body"] == {"user_sub": "header", "turn_id": "t-7"}
    r = client.post(f"{url}/sessions/s1/cancel", json={})
    assert r.status_code == 202, r.text
    assert cancel_calls()[-1]["body"] == {"user_sub": "header"}
    assert client.post(f"{url}/sessions/s1/cancel", json={"turn_id": ""}).status_code == 422
    assert client.post(f"{url}/sessions/s1/cancel", json={"other": 1}).status_code == 422


def test_progress_read_carries_the_poll_interval_of_the_route(
    client, fake_clusters, mongo, monkeypatch
):
    """`view=progress` 는 런타임까지 가고, 답에 화면이 다시 읽을 간격이 붙는다. Service 에 바로
    닿으면 3초, API 서버의 터널이면 10초다(P78 4절). 전체 읽기에는 없다. 세션 줄의 running·turn_id
    는 그대로 넘어간다(2절)."""
    agent = deployed_agent(client, fake_clusters, mongo)
    url = agent.url

    async def session(sid: str, request: Request):
        record_call(agent.runtime, request)
        row = session_row(sid, "header", running=True, turn_id="t-1")
        return {"session": row, "pending": False, "transcript": {"items": [], "limit": 500}}

    replace_get(agent.runtime, "/sessions/{sid}", session)
    r = client.get(f"{url}/sessions/s1", params={"view": "progress"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["poll_seconds"] == 3
    assert body["session"]["running"] is True and body["session"]["turn_id"] == "t-1"
    assert agent.runtime.state.calls[-1]["params"] == {"user_sub": "header", "view": "progress"}
    r = client.get(f"{url}/sessions/s1")
    assert r.json()["poll_seconds"] is None
    assert agent.runtime.state.calls[-1]["params"] == {"user_sub": "header"}
    assert client.get(f"{url}/sessions/s1", params={"view": "full"}).status_code == 422

    agent.fake.service_via = "apiserver"
    FakeBridge(monkeypatch)
    r = client.get(f"{url}/sessions/s1", params={"view": "progress"})
    assert r.status_code == 200, r.text
    assert r.json()["poll_seconds"] == 10


# -- 넓힌 프록시, 롤링 창, 내려받기, 상태의 chat (D61, P36 F1) -------------------------------------


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


async def _read(body: Any) -> bytes:
    return b"".join([chunk async for chunk in body])


def test_only_the_framework_404_and_405_mean_a_rolling_pod() -> None:
    """능력을 준 라우트에서 본문에 `code` 가 없고 `detail` 이 글자인 404·405 만 409
    runtime_rolling 이다. 런타임이 낸 404, 다른 모양, 다른 상태, 능력을 주지 않은 라우트는
    그대로다."""
    for status_code, detail in ((404, "Not Found"), (405, "Method Not Allowed")):
        e = runtime_error(status_code, {"detail": detail}, feature=CANCEL)
        assert e.status_code == 409
        assert e.detail == {
            "code": RUNTIME_ROLLING,
            "detail": "a runtime pod without the 'cancel' capability answered; "
            "the rollout is still in progress",
            "feature": CANCEL,
        }
    missing = {"code": "session_not_found", "message": "세션이 없다"}
    ours = runtime_error(404, missing, feature=CANCEL)
    assert ours.status_code == 404
    assert ours.detail == {"code": "session_not_found", "detail": "세션이 없다"}
    for body in ({"detail": {"code": "x"}}, {"detail": ["x"]}, None, {"code": "", "detail": "x"}):
        assert runtime_error(404, body, feature=CANCEL).status_code == 404
    assert runtime_error(404, {"detail": "Not Found"}).status_code == 404
    assert runtime_error(410, {"detail": "Gone"}, feature=CANCEL).status_code == 410


def test_a_runtime_validation_error_keeps_the_platform_code() -> None:
    """런타임의 검증 실패(P76 F1)는 플랫폼 형식으로 옮겨도 `validation_error` 이고 detail 은 화면이
    플랫폼 자신의 검증 실패에서 만드는 `loc: msg` 글이다. 감사 기록도 같은 코드로 읽는다."""
    body = {
        "code": "validation_error",
        "message": "body.message: Field required",
        "errors": [{"loc": ["body", "message"], "msg": "Field required", "type": "missing"}],
    }
    e = runtime_error(422, body)
    assert e.status_code == 422
    assert e.detail == {
        "code": "validation_error",
        "detail": "body.message: Field required",
        "errors": body["errors"],
    }
    assert _error_code(json.dumps({"detail": e.detail}).encode()) == "validation_error"


def test_proxied_download_rebuilds_the_headers_and_maps_errors() -> None:
    """런타임 바이트 스트림은 런타임의 헤더를 버리고 내려받기 헤더 다섯으로 나간다. 오류는 스트림
    오류와 같은 규칙이라 옛 파드의 404 는 409 runtime_rolling, 런타임의 410 은 그대로다."""
    asyncio.run(_proxied_download_rebuilds_the_headers_and_maps_errors())


async def _proxied_download_rebuilds_the_headers_and_maps_errors() -> None:
    runtime_headers = {"content-type": "text/html", "content-disposition": "inline", "x-a": "1"}
    ok = ProxyResponse(200, runtime_headers, _chunks(b"<h1>", b"hi</h1>"))
    response = await proxied_download(ok, "/site/index.html", feature=CANCEL)
    expected = {k.lower(): v for k, v in download_headers("/site/index.html").items()}
    assert {k: response.headers.get(k) for k in expected} == expected
    assert "x-a" not in response.headers
    assert await _read(response.body_iterator) == b"<h1>hi</h1>"

    json_type = {"content-type": "application/json"}
    rolling = ProxyResponse(404, json_type, _chunks(b'{"detail": "Not Found"}'))
    with pytest.raises(HTTPException) as info:
        await proxied_download(rolling, "/a.txt", feature=CANCEL)
    assert info.value.status_code == 409 and info.value.detail["code"] == RUNTIME_ROLLING
    closed = json.dumps({"code": "session_closed", "message": "닫힌 세션이다"}).encode()
    e = await stream_error(ProxyResponse(410, json_type, _chunks(closed)), feature=CANCEL)
    assert e.status_code == 410
    assert e.detail == {"code": "session_closed", "detail": "닫힌 세션이다"}


# -- 원격 클러스터의 경로 (D41, P35 F12) --------------------------------------------------------


class FakeBridge:
    """`portforward_client` 의 fake (test_svc_tokens.py 의 꼴). 터널 대신 가짜 런타임 ASGI 앱에
    붙고, 터널을 연 인자와 문맥을 닫은 횟수를 남긴다. `bearer_service_client` 가 모듈 이름으로
    부르므로 그 자리를 바꾼다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[dict[str, Any]] = []
        self.closed = 0
        monkeypatch.setattr(pf, "portforward_client", self.client)

    @contextlib.asynccontextmanager
    async def client(
        self,
        client: FakeClusterClient,
        namespace: str,
        service: str,
        port: int,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        read_timeout: float | None = None,
    ):
        self.opened.append(
            {
                "namespace": namespace,
                "service": service,
                "port": port,
                "headers": dict(headers or {}),
                "timeout": timeout,
                "read_timeout": read_timeout,
            }
        )
        hc = httpx2.AsyncClient(
            base_url="http://127.0.0.1:1",
            transport=httpx2.ASGITransport(app=client.service_app),
            headers={"Host": f"{service}.{namespace}.svc:{port}", **(headers or {})},
        )
        try:
            async with hc:
                yield hc
        finally:
            self.closed += 1


def test_remote_route_calls_the_runtime_through_the_bridge(
    client, fake_clusters, mongo, monkeypatch
):
    """`via=apiserver` 면 채팅·세션·메트릭·중지가 pods/portforward 브리지로 가고 인스턴스 토큰이
    Authorization 으로 실린다. API 서버 services/proxy 는 그 헤더를 지우기 때문이다(D41). 채팅은
    긴 읽기 시간이고, 본문이 끝나면 문맥이 닫힌다. 직행이면 터널 없이 Service 주소다."""
    agent = deployed_agent(client, fake_clusters, mongo)
    fake = agent.fake
    fake.service_via = "apiserver"
    url = agent.url
    auth = bearer(agent.token)
    reported = {**auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: "cancel"}
    assert client.get(f"{url}/agent.json", headers=reported).status_code == 200
    # 터널을 못 열면(Ready 파드가 없다, 클러스터 권한이 모자라다) 502 runtime_unreachable 이다
    r = client.get(f"{url}/sessions")
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "runtime_unreachable"
    assert "no ready pod" in r.json()["detail"]["detail"]
    # 지표는 런타임 몫만 비고 그 까닭을 싣는다(P37 11절)
    metrics = client.get(f"{url}/metrics").json()
    assert metrics["runtime_error"]["code"] == "runtime_unreachable"
    assert metrics["sessions"] is None and metrics["turns"]["done"] == 0
    client.app.state.agent_metrics._cache.clear()
    fake.denied = {("", "services", "get")}
    r = client.get(f"{url}/sessions")
    assert r.status_code == 502, r.text
    assert r.json()["detail"] == {
        "code": "runtime_unreachable",
        "detail": f"forbidden: services:get@{DEFAULT_NS}",
    }
    fake.denied = set()
    assert agent.runtime.state.calls == []

    bridge = FakeBridge(monkeypatch)
    r = client.post(f"{url}/chat", json={"message": "안녕"})
    assert r.status_code == 200, r.text
    assert _events(r.text) == ["token", "tool_call", "done"]
    call = agent.runtime.state.calls[-1]
    assert call["headers"]["authorization"] == f"Bearer {agent.token}"
    assert call["headers"]["host"] == f"agent-support.{DEFAULT_NS}.svc:8080"
    assert bridge.opened == [
        {
            "namespace": DEFAULT_NS,
            "service": "agent-support",
            "port": 8080,
            "headers": auth,
            "timeout": RUNTIME_TIMEOUT,
            "read_timeout": CHAT_READ_TIMEOUT,
        }
    ]
    assert bridge.closed == 1
    assert client.get(f"{url}/sessions").status_code == 200
    assert client.get(f"{url}/metrics").status_code == 200
    assert client.post(f"{url}/sessions/s1/cancel").status_code == 202
    assert [o["read_timeout"] for o in bridge.opened] == [CHAT_READ_TIMEOUT, None, None, None]
    assert all(o["headers"] == auth for o in bridge.opened)
    assert bridge.closed == 4
    for call in agent.runtime.state.calls[-3:]:
        assert call["headers"]["authorization"] == f"Bearer {agent.token}"
        assert call["headers"]["host"] == f"agent-support.{DEFAULT_NS}.svc:8080"
    # 직행이면 터널 없이 Service 주소다
    fake.service_via = "direct"
    assert client.get(f"{url}/sessions").status_code == 200
    call = agent.runtime.state.calls[-1]
    assert call["headers"]["authorization"] == f"Bearer {agent.token}"
    assert call["headers"]["host"] == f"agent-support.{DEFAULT_NS}.svc.cluster.local:8080"
    assert len(bridge.opened) == 4


def _service_with_pods(fake: FakeClusterClient, pods: dict[str, tuple[bool, int]]) -> None:
    """Service `agent-x`(selector app=x, 8080 → 이름 붙은 포트 http)와 파드를 둔다. pods 는 이름 →
    (Ready 여부, http 컨테이너 포트)다."""
    # async 테스트가 부르는 준비 단계라 루프 검사를 끈다 (P69)
    with allow_on_loop():
        fake.apply(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "agent-x", "namespace": DEFAULT_NS},
                "spec": {"selector": {"app": "x"}, "ports": [{"port": 8080, "targetPort": "http"}]},
            }
        )
        for name, (ready, port) in pods.items():
            fake.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": name, "namespace": DEFAULT_NS, "labels": {"app": "x"}},
                    "spec": {
                        "containers": [
                            {"name": "agent", "ports": [{"name": "http", "containerPort": port}]}
                        ]
                    },
                }
            )
            condition = {"type": "Ready", "status": "True" if ready else "False"}
            fake.set_status(
                "v1", "Pod", name, DEFAULT_NS, {"phase": "Running", "conditions": [condition]}
            )


def test_tunnel_picks_a_ready_pod_at_random() -> None:
    """터널의 파드는 Service 의 selector 에 맞는 Ready 파드 가운데 무작위다. 첫 파드로 고정하면
    원격 채팅이 레플리카 하나에 몰린다. 이름 붙은 targetPort 는 고른 파드의 컨테이너 포트로 푼다."""
    fake = FakeClusterClient()
    _service_with_pods(fake, {"a": (True, 9001), "b": (True, 9002), "c": (False, 9003)})
    picks = {ready_pod_for_service(fake, DEFAULT_NS, "agent-x", 8080) for _ in range(40)}
    assert picks == {("a", 9001), ("b", 9002)}
    # Ready 조건이 없거나 Running 이 아니면 후보가 아니다
    fake.set_status("v1", "Pod", "a", DEFAULT_NS, {"phase": "Running", "conditions": []})
    fake.set_status(
        "v1",
        "Pod",
        "b",
        DEFAULT_NS,
        {"phase": "Succeeded", "conditions": [{"type": "Ready", "status": "True"}]},
    )
    with pytest.raises(PortForwardError, match="no ready pod"):
        ready_pod_for_service(fake, DEFAULT_NS, "agent-x", 8080)


def test_tunnel_skips_a_pod_that_is_being_deleted() -> None:
    """지워지는 파드는 Ready 여도 터널의 후보가 아니다. Service 의 끝점에서 빠지는 것과 같다.
    내려가는 런타임은 새 연결을 받지 않는다(P78 5절)."""
    fake = FakeClusterClient()
    _service_with_pods(fake, {"a": (True, 9001), "b": (True, 9002)})
    fake.objects[("v1", "Pod", DEFAULT_NS, "a")]["metadata"]["deletionTimestamp"] = "2026-10-26"
    picks = {ready_pod_for_service(fake, DEFAULT_NS, "agent-x", 8080) for _ in range(20)}
    assert picks == {("b", 9002)}


def test_bearer_service_client_carries_the_bearer_and_the_read_timeout(monkeypatch) -> None:
    """직행이 아니고 Bearer 가 있으면 `portforward_client` 가 Ready 파드로 터널을 열어
    Authorization·Host 와 읽기 시간을 싣는다. 직행이면 Service 주소의 클라이언트에 같은 헤더와
    시간이다. 기준 URL 은 끝에 `/` 가 없다."""
    asyncio.run(_bearer_service_client_carries_the_bearer_and_the_read_timeout(monkeypatch))


async def _bearer_service_client_carries_the_bearer_and_the_read_timeout(monkeypatch) -> None:
    server, port = await header_echo_server()
    tunnels: list[tuple[str, int]] = []

    def fake_open_tunnel(client, namespace, pod, target):
        tunnels.append((pod, target))
        return socket.create_connection(("127.0.0.1", port)), lambda: None

    monkeypatch.setattr(pf, "open_tunnel", fake_open_tunnel)
    fake = FakeClusterClient()
    _service_with_pods(fake, {"a": (True, 9001)})
    opts: dict[str, Any] = {
        "headers": pf.bearer_header("tok"),
        "timeout": 5.0,
        "read_timeout": 77.0,
    }
    fake.service_via = "apiserver"
    try:
        async with pf.bearer_service_client(fake, DEFAULT_NS, "agent-x", 8080, **opts) as (
            hc,
            base_url,
        ):
            assert base_url.startswith("http://127.0.0.1:") and not base_url.endswith("/")
            assert (hc.timeout.connect, hc.timeout.read) == (5.0, 77.0)
            got = (await hc.get(f"{base_url}/metrics")).json()
    finally:
        server.close()
        await server.wait_closed()
    assert tunnels == [("a", 9001)]
    assert got["request"].startswith("GET /metrics ")
    assert got["headers"]["authorization"] == "Bearer tok"
    assert got["headers"]["host"] == f"agent-x.{DEFAULT_NS}.svc:8080"
    fake.service_via = "direct"
    async with pf.bearer_service_client(fake, DEFAULT_NS, "agent-x", 8080, **opts) as (
        hc,
        base_url,
    ):
        assert base_url == f"http://agent-x.{DEFAULT_NS}.svc.cluster.local:8080"
        assert hc.headers["authorization"] == "Bearer tok"
        assert (hc.timeout.connect, hc.timeout.read) == (5.0, 77.0)
    assert len(tunnels) == 1


def test_closing_streaming_response_closes_the_generator_in_place() -> None:
    """브라우저가 send 도중에 끊는다. 생성기는 yield 에 멈춘 채인데 응답 호출이 돌아오기 전에 같은
    태스크에서 닫혀 마감이 곧바로 돈다. 런타임의 같은 이름 클래스와 같은 꼴이다."""
    asyncio.run(_closing_streaming_response_closes_the_generator_in_place())


async def _closing_streaming_response_closes_the_generator_in_place() -> None:
    closed: list[str] = []

    async def body():
        try:
            yield b"event: token\n\n"
            yield b"event: done\n\n"
        finally:
            await asyncio.sleep(0)  # 마감이 양보한다
            closed.append("finally")

    disconnected = asyncio.Event()
    sent: list[bytes] = []

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        chunk = message.get("body", b"")
        sent.append(chunk)
        if chunk:
            # 느린 클라이언트다. 이 send 가 돌아오기 전에 연결이 끊긴다
            disconnected.set()
            await asyncio.sleep(30)

    response = ClosingStreamingResponse(body(), media_type="text/event-stream")
    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    assert closed == ["finally"] and sent[-1] == b"event: token\n\n"
    assert response.body_iterator.ag_frame is None, "생성기가 아직 닫히지 않았다"


def test_hub_tool_list_uses_the_bearer_service_client(monkeypatch) -> None:
    """허브의 tools/list 도 같은 `bearer_service_client` 로 간다. 직행·터널 판정이 두 곳에 없다."""
    asyncio.run(_hub_tool_list_uses_the_bearer_service_client(monkeypatch))


async def _hub_tool_list_uses_the_bearer_service_client(monkeypatch) -> None:
    import daegam.mcp_client as mcp_client
    from daegam.services import hub as hub_module

    seen: list[Any] = []

    @contextlib.asynccontextmanager
    async def fake_bearer_service_client(
        client, namespace, service, port, *, headers, timeout, read_timeout=None, scheme="http"
    ):
        seen.append((namespace, service, port, headers, timeout, read_timeout))
        async with httpx2.AsyncClient(base_url="http://127.0.0.1:1") as hc:
            yield hc, "http://127.0.0.1:1"

    class _Res:
        tools = ["Add"]

    async def fake_list_tools(url, *, timeout, http_client=None):
        seen.append((url, http_client is not None))
        return _Res()

    monkeypatch.setattr(hub_module, "bearer_service_client", fake_bearer_service_client)
    monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)
    url = f"http://mcp-a-proxy.{DEFAULT_NS}.svc.cluster.local:8080/mcp"
    tok = {"Authorization": "Bearer tok"}
    assert await hub_module._default_tool_lister(object(), url, tok) == ["Add"]
    assert seen == [
        (DEFAULT_NS, "mcp-a-proxy", 8080, tok, hub_module.TOOLS_LIST_TIMEOUT, None),
        ("http://127.0.0.1:1/mcp", True),
    ]
