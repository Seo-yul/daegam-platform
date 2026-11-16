"""P14 F1b: 비서 턴이 도는 방식 (stub provider).

세션에 붙는 provider, 턴 SSE(tool_call → tool_result → done)와 저장, 한도·타임아웃, 리스 409 와
사용자당 동시 턴 1, 같은 세션의 동시 턴과 결정, 다른 파드가 쥔 턴, SSE 를 끊어도 태스크가
끝까지 가고 저장됨, 종료 취소 뒤 리스 해제, 전사 상한, 도구 묶음, 재시도, 도구 없는 모델, 루프가
죽은 턴, 스트리밍 도중 실패를 든다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from daegam.services.assistant.loop import TurnLoop
from daegam.services.assistant.stub import AssistantStub
from daegam.services.assistant.tools import ToolResult
from daegam.services.llm_endpoint import PLACEHOLDER_KEY, Endpoint
from daegam.store.leases import Lease
from tests.helpers.assistant import (
    STUB,
    Gate,
    assistant_leases,
    await_task,
    enable,
    names,
    new_session,
    parse_sse,
    say,
    session_doc,
    start_via_service,
    steps,
    tool,
    turn,
)
from tests.helpers.consts import A
from tests.helpers.http import API_BASE_URL, detail_code


def test_turn_provider_is_chosen_once_and_sticks_to_the_session(client, mongo):
    """D45 셋째: 고른 provider 는 그 대화에 붙는다. 첫 턴에 오면 세션이 기억하고 다음 턴은 보내지
    않아도 같은 것으로 간다. 이름 검증은 지금 provider 해소가 쓰는 규칙 그대로다."""
    ant = {"name": "ant", "kind": "anthropic", "api_key_secret": "llm-keys"}
    nokey = {"name": "nokey", "kind": "anthropic"}
    enable(client, providers=(STUB, ant, nokey), defaults={"assistant": "local-stub"})
    # 키 유무는 Secret 을 읽어야 알므로 목록이 걸러 주지 않는다. 고르면 첫 턴이 422 로 말한다
    assert [p["name"] for p in client.get(f"{A}/config").json()["providers"]] == [
        "local-stub",
        "ant",
        "nokey",
    ]

    svc = client.app.state.assistant
    used: list[str] = []

    def remember(provider, effort):
        used.append(provider.name)
        return AssistantStub()

    svc.model_factory = remember

    sid = new_session(client)
    # 고르지 않은 대화는 설정의 기본으로 돈다
    turn(client, sid, steps(say("기본")))
    assert used == ["local-stub"] and session_doc(mongo, sid)["provider"] is None

    chosen = new_session(client)
    r = client.post(
        f"{A}/sessions/{chosen}/turns", json={"message": steps(say("첫 턴")), "provider": "ant"}
    )
    assert r.status_code == 200, r.text
    assert names(parse_sse(r.text)) == ["token", "done"]
    assert used[-1] == "ant" and session_doc(mongo, chosen)["provider"] == "ant"
    # 두 번째 턴은 보내지 않아도 세션이 기억한 것으로 간다
    turn(client, chosen, steps(say("두 번째")))
    assert used[-1] == "ant"
    # 목록과 상세가 어느 모델과 나눈 대화인지 말한다
    items = {i["id"]: i["provider"] for i in client.get(f"{A}/sessions").json()["items"]}
    assert items == {sid: None, chosen: "ant"}
    assert client.get(f"{A}/sessions/{chosen}").json()["provider"] == "ant"
    # 도중에 바꾸면 그 뒤로는 바꾼 것으로 간다
    r = client.post(
        f"{A}/sessions/{chosen}/turns",
        json={"message": steps(say("바꾼 뒤")), "provider": "local-stub"},
    )
    assert r.status_code == 200, r.text
    assert used[-1] == "local-stub" and session_doc(mongo, chosen)["provider"] == "local-stub"
    svc.model_factory = None

    # 등록되지 않은 이름·키가 없는 이름·허용되지 않는 stub 은 턴을 시작하지 않는다
    before = session_doc(mongo, chosen)["turns"]
    r = client.post(f"{A}/sessions/{chosen}/turns", json={"message": "hi", "provider": "gone"})
    assert detail_code(r) == (422, "unknown_provider")
    r = client.post(f"{A}/sessions/{chosen}/turns", json={"message": "hi", "provider": "nokey"})
    assert detail_code(r) == (422, "provider_no_key")
    svc.settings = dataclasses.replace(svc.settings, allow_stub_provider=False)
    body = {"message": "hi", "provider": "local-stub"}
    r = client.post(f"{A}/sessions/{chosen}/turns", json=body)
    assert detail_code(r) == (422, "stub_provider_disallowed")
    svc.settings = client.app.state.settings
    # 세션에 붙은 provider 가 설정에서 사라지면 조용히 기본으로 가지 않고 멈춘다
    enable(client, providers=(ant,), defaults={"assistant": "ant"})
    r = client.post(f"{A}/sessions/{chosen}/turns", json={"message": "hi"})
    assert detail_code(r) == (422, "unknown_provider")
    # 거절된 턴은 세지 않고 대화가 기억한 provider 도 그대로다
    doc = session_doc(mongo, chosen)
    assert doc["turns"] == before and doc["provider"] == "local-stub"


def test_limits_timeout_and_lease_release(client, mongo, monkeypatch):
    enable(client, max_tool_calls_per_turn=2)
    sid = new_session(client)
    events = turn(
        client,
        sid,
        steps(tool("list_clusters"), tool("list_clusters"), tool("list_clusters"), say("x")),
    )
    assert names(events) == ["tool_call", "tool_result", "tool_call", "tool_result", "error"]
    assert events[-1][1]["code"] == "tool_limit" and events[-1][1]["session_id"] == sid
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body["state"] == "idle" and body["transcript"][-1]["kind"] == "error"
    assert assistant_leases(mongo) == []
    # 타임아웃: 도구가 오래 걸리면 turn_timeout 으로 닫히고 리스가 풀린다
    enable(client, turn_timeout_seconds=1)
    svc = client.app.state.assistant

    async def slow(*a, **kw):
        await asyncio.sleep(5)
        return ToolResult("success", {})

    monkeypatch.setattr(svc.executor, "call", slow)
    events = turn(client, sid, steps(tool("list_clusters"), say("x")))
    assert names(events) == ["tool_call", "error"] and events[-1][1]["code"] == "turn_timeout"
    assert assistant_leases(mongo) == []
    doc = session_doc(mongo, sid)
    assert doc["state"] == "idle"
    assert doc["context"][-1]["type"] == "tool"
    assert json.loads(doc["context"][-1]["data"]["content"])["code"] == "turn_timeout"


def test_session_lease_and_one_turn_per_user(client, mongo):
    enable(client)
    sid = new_session(client)
    sid2 = new_session(client)
    db = client.app.state.backend_apis.db
    other = Lease(db, "other-pod/x", ttl=timedelta(seconds=60))
    # 다른 파드가 세션 리스를 쥐고 있다
    assert client.portal.call(lambda: other.acquire(f"assistant-session:{sid}"))
    r = client.post(f"{A}/sessions/{sid}/turns", json={"message": "hi"})
    assert detail_code(r) == (409, "turn_in_progress")
    assert detail_code(client.delete(f"{A}/sessions/{sid}")) == (409, "turn_in_progress")
    client.portal.call(lambda: other.release(f"assistant-session:{sid}"))
    # 다른 파드가 사용자 리스를 쥐고 있다 (다른 세션의 턴)
    assert client.portal.call(lambda: other.acquire("assistant-turn:header"))
    r = client.post(f"{A}/sessions/{sid}/turns", json={"message": "hi"})
    assert detail_code(r) == (409, "turn_in_progress")
    # 실패한 시도는 세션 리스를 남기지 않는다
    assert assistant_leases(mongo) == ["assistant-turn:header"]
    client.portal.call(lambda: other.release("assistant-turn:header"))
    # 이 파드에서 도는 턴: 같은 세션도, 같은 사용자의 다른 세션도 409
    svc = client.app.state.assistant
    gate = Gate(svc)
    gate.install()
    try:
        handle = start_via_service(client, sid, steps(tool("list_clusters"), say("x")))
        assert assistant_leases(mongo) == [f"assistant-session:{sid}", "assistant-turn:header"]
        r = client.post(f"{A}/sessions/{sid}/turns", json={"message": "hi"})
        assert detail_code(r) == (409, "turn_in_progress")
        r = client.post(f"{A}/sessions/{sid2}/turns", json={"message": "hi"})
        assert detail_code(r) == (409, "turn_in_progress")
        r = client.delete(f"{A}/sessions/{sid}")
        assert r.status_code == 409
        assert client.get(f"{A}/sessions/{sid}").json()["state"] == "running"
        gate.open(client)
        assert await_task(client, handle.task).kind == "done"
    finally:
        gate.restore()
    assert assistant_leases(mongo) == []
    assert client.get(f"{A}/sessions/{sid}").json()["state"] == "idle"
    # 다른 사용자의 세션은 다른 리스 이름이라 막히지 않는다
    events = turn(client, sid2, steps(say("ok")))
    assert names(events) == ["token", "done"]


def test_stop_cancels_running_turn_saves_transcript_and_releases_leases(client, mongo):
    enable(client)
    sid = new_session(client)
    svc = client.app.state.assistant
    gate = Gate(svc)
    gate.install()
    try:
        handle = start_via_service(client, sid, steps(tool("list_clusters"), say("x")))
        assert len(assistant_leases(mongo)) == 2
        gate.wait_entered()
        client.portal.call(svc.stop)
        assert handle.task.cancelled() or handle.task.done()
    finally:
        gate.restore()
    assert svc.tasks == set() and svc.running == {}
    assert assistant_leases(mongo) == []
    doc = session_doc(mongo, sid)
    assert doc["state"] == "idle"
    assert [it["kind"] for it in doc["transcript"]] == ["user", "tool_call"]
    # 끊긴 tool_call 은 ToolMessage 로 닫혀 문맥이 provider 계약을 지킨다
    assert [m["type"] for m in doc["context"]] == ["human", "ai", "tool"]
    assert json.loads(doc["context"][-1]["data"]["content"])["code"] == "turn_failed"
    # 그 뒤 새 턴이 정상으로 돈다
    events = turn(client, sid, steps(say("다시")))
    assert names(events) == ["token", "done"]


# -- 모델 실패·재시도 -----------------------------------------------------------------------------


class Flaky(BaseChatModel):
    """처음 n 번은 status_code 가 있는 예외를 내고 그 뒤 스텁에 맡긴다."""

    fail_first: int = 0
    status: int = 429
    error_code: str | None = None
    # 게이트웨이의 답을 흉내 내는 응답 머리와 본문이다 (P38 7절)
    headers: dict[str, str] | None = None
    body: Any = None
    _inner: Any = None
    _failures: int = 0

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._inner = AssistantStub()

    @property
    def _llm_type(self) -> str:
        return "flaky"

    def bind_tools(self, tools, **kw):
        self._inner.bind_tools(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        if self._failures < self.fail_first:
            self._failures += 1
            e = RuntimeError(f"fail {self._failures}")
            e.status_code = self.status  # type: ignore[attr-defined]
            e.code = self.error_code  # type: ignore[attr-defined]
            if self.headers is not None:
                e.response = SimpleNamespace(headers=self.headers)  # type: ignore[attr-defined]
                e.body = self.body  # type: ignore[attr-defined]
            raise e
        return ChatResult(generations=[ChatGeneration(message=self._inner.reply(messages))])


class _GatewayRoute:
    """모든 턴을 게이트웨이 경로로 보내는 길 (P38 6절)."""

    async def resolve_endpoint(self, cfg: Any, *, tenant: str, purpose: str) -> Endpoint:
        assert purpose == "assistant"
        gw = "http://llm-gateway.daegam-llm.svc.cluster.local:8080"
        return Endpoint(f"{gw}/p/x/v1", PLACEHOLDER_KEY, lambda: "token", "gateway")


def test_a_gateway_turn_ends_with_the_gateway_code_without_retrying(client):
    """게이트웨이 경로(P38 F6)의 턴이 게이트웨이의 한도에 걸리면 다시 시도하지 않고
    llm_gateway_rate_limited 로 끝난다. 1분 창이라 1·2초 뒤의 답도 같기 때문이다. 같은 꼴의 오류도
    직접 경로면 provider 의 429 라 다시 시도한다."""
    enable(client)
    svc = client.app.state.assistant
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    svc.sleep = no_sleep
    direct = svc.endpoints
    svc.endpoints = _GatewayRoute()
    limited = {"x-ratelimit-remaining": "0"}
    try:
        sid = new_session(client)
        svc.model_factory = lambda provider, effort: Flaky(fail_first=1, headers=limited, body="")
        events = turn(client, sid, steps(say("ok")))
        assert names(events) == ["error"]
        assert events[0][1]["code"] == "llm_gateway_rate_limited" and delays == []
    finally:
        svc.endpoints = direct
        svc.model_factory = None


class NoTools(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "no-tools"

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage("x"))])


def test_model_retries_with_backoff_then_fails_and_provider_without_tools(client):
    enable(client)
    svc = client.app.state.assistant
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    svc.sleep = no_sleep
    sid = new_session(client)
    # 429 두 번 뒤 성공: 백오프 1초·2초
    svc.model_factory = lambda provider, effort: Flaky(fail_first=2)
    events = turn(client, sid, steps(say("ok")))
    assert names(events) == ["token", "done"] and delays == [1.0, 2.0]
    # 세 번 실패하면 turn_failed. 재시도는 2회다
    delays.clear()
    svc.model_factory = lambda provider, effort: Flaky(fail_first=3, status=503)
    events = turn(client, sid, steps(say("ok")))
    assert names(events) == ["error"] and events[0][1]["code"] == "turn_failed"
    # 원인 문장("fail 3")은 로그에만 남고 화면에는 예외의 종류만 간다
    msg = events[0][1]["message"]
    assert delays == [1.0, 2.0] and "RuntimeError" in msg and "fail 3" not in msg
    # 400 은 다시 시도하지 않는다
    delays.clear()
    svc.model_factory = lambda provider, effort: Flaky(fail_first=1, status=400)
    events = turn(client, sid, steps(say("ok")))
    assert names(events) == ["error"] and delays == []
    # 크레딧 소진은 429 여도 다시 시도하지 않고 quota_exhausted 로 끝난다(T20)
    svc.model_factory = lambda provider, effort: Flaky(
        fail_first=1, error_code="insufficient_quota"
    )
    events = turn(client, sid, steps(say("ok")))
    assert names(events) == ["error"] and events[0][1]["code"] == "quota_exhausted"
    assert delays == []
    assert client.get(f"{A}/sessions/{sid}").json()["state"] == "idle"
    # 도구 호출을 못 하는 모델은 첫 턴에서 provider_no_tools
    svc.model_factory = lambda provider, effort: NoTools()
    events = turn(client, sid, "hi")
    assert names(events) == ["error"] and events[0][1]["code"] == "provider_no_tools"
    svc.model_factory = None


class TalkThenCall(BaseChatModel):
    """말을 조금 흘린 뒤 도구를 부르는 모델. 충돌 경로가 흐르던 말풍선을 닫는지 보려고 쓴다."""

    @property
    def _llm_type(self) -> str:
        return "talk-then-call"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        call = {"name": "list_clusters", "args": {}, "id": "c1", "type": "tool_call"}
        ai = AIMessage("살펴본다", tool_calls=[call])
        return ChatResult(generations=[ChatGeneration(message=ai)])


def test_crashed_turn_records_the_error_and_keeps_the_cause_in_the_log(client, mongo, monkeypatch):
    """루프 버그로 턴이 죽어도 전사에 error 가 남고 흐르던 말풍선은 닫힌다. 예외 원문은 로그에만
    남고 화면에는 예외의 종류만 간다."""
    enable(client)
    svc = client.app.state.assistant

    async def boom(self, ctx, ai):
        raise RuntimeError("hunter2 in the cause")

    monkeypatch.setattr(svc, "model_factory", lambda provider, effort: TalkThenCall())
    monkeypatch.setattr(TurnLoop, "_handle_tool_calls", boom)
    sid = new_session(client)
    events = turn(client, sid, "hi")
    assert names(events) == ["token", "error"]
    error = events[-1][1]
    assert error["code"] == "turn_failed"
    assert "RuntimeError" in error["message"] and "hunter2" not in error["message"]
    doc = session_doc(mongo, sid)
    assert doc["state"] == "idle"
    assert [it["kind"] for it in doc["transcript"]] == ["user", "assistant", "error"]
    assert doc["transcript"][1]["streaming"] is False
    assert doc["transcript"][-1] == {
        "kind": "error",
        "code": "turn_failed",
        "message": error["message"],
    }
    # 끊긴 tool_call 도 ToolMessage 로 닫히고 리스는 풀린다
    assert [m["type"] for m in doc["context"]] == ["human", "ai", "tool"]
    assert assistant_leases(mongo) == []


# -- 같은 파드 같은 세션의 동시 턴 --------------------------------------------------------------


def test_concurrent_turns_on_the_same_session_let_only_one_through(client, mongo):
    enable(client)
    sid = new_session(client)
    body = {"message": steps(say("ok"))}

    async def both():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as http:
            return await asyncio.gather(
                http.post(f"{A}/sessions/{sid}/turns", json=body),
                http.post(f"{A}/sessions/{sid}/turns", json=body),
            )

    responses = client.portal.call(both)
    assert sorted(r.status_code for r in responses) == [200, 409]
    lost = next(r for r in responses if r.status_code == 409)
    assert lost.json()["detail"]["code"] == "turn_in_progress"
    won = next(r for r in responses if r.status_code == 200)
    assert names(parse_sse(won.text))[-1] == "done"
    body_after = client.get(f"{A}/sessions/{sid}").json()
    assert body_after["turns"] == 1 and body_after["state"] == "idle"
    assert assistant_leases(mongo) == []
    # 리스 자체도 배타적이다. holder 에 턴 고유값이 들어가 같은 세션의 다른 턴이 못 잡는다
    db = client.app.state.backend_apis.db
    first = Lease(db, "test-pod/s1/turn-a", ttl=timedelta(seconds=60))
    second = Lease(db, "test-pod/s1/turn-b", ttl=timedelta(seconds=60))
    assert client.portal.call(lambda: first.acquire("assistant-session:s1")) is True
    assert client.portal.call(lambda: second.acquire("assistant-session:s1")) is False
    assert client.portal.call(lambda: first.acquire("assistant-session:s1")) is True
    client.portal.call(lambda: first.release("assistant-session:s1"))
    # 결정 재개도 같은 길이다. 대기 중인 세션에 결정 둘을 동시에 보내면 하나만 지나간다
    events = turn(
        client,
        sid,
        steps(tool("deploy_mcp_server", server_id="p"), say("$0.code")),
    )
    assert names(events) == ["interrupt"]
    call_id = client.get(f"{A}/sessions/{sid}").json()["pending"]["calls"][0]["id"]
    decision = {"decisions": [{"id": call_id, "type": "reject"}]}

    async def both_decisions():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as http:
            return await asyncio.gather(
                http.post(f"{A}/sessions/{sid}/decisions", json=decision),
                http.post(f"{A}/sessions/{sid}/decisions", json=decision),
            )

    responses = client.portal.call(both_decisions)
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert client.get(f"{A}/sessions/{sid}").json()["state"] == "idle"


# -- 스트리밍 도중 실패는 재시도하지 않는다 ---------------------------------------------------


# -- Starlette 생성기 취소 경로의 끊김 ----------------------------------------------------------


def drive_until_first_event(client, sid: str, message: str) -> list[bytes]:
    """앱을 ASGI 로 직접 몰아 첫 SSE 청크를 받자마자 http.disconnect 를 준다. Starlette 가 스트리밍
    생성기를 취소하는 바로 그 경로다."""
    app = client.app
    payload = json.dumps({"message": message}).encode()

    async def drive() -> list[bytes]:
        first = asyncio.Event()
        chunks: list[bytes] = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await first.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                first.set()

        path = f"/api{A}/sessions/{sid}/turns"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "state": {},
        }
        await app(scope, receive, send)
        return chunks

    return client.portal.call(drive)


def test_client_disconnect_through_starlette_cancels_only_the_generator(client, mongo):
    enable(client)
    sid = new_session(client)
    svc = client.app.state.assistant
    gate = Gate(svc)
    gate.install()
    try:
        chunks = drive_until_first_event(client, sid, steps(tool("list_clusters"), say("끝")))
        assert chunks and chunks[0].startswith(b"event: tool_call")
        handle = svc.running[sid]
        assert handle is not None and handle.queue.closed is True and not handle.task.done()
        gate.open(client)
        assert await_task(client, handle.task).kind == "done"
    finally:
        gate.restore()
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body["state"] == "idle"
    assert [it["kind"] for it in body["transcript"]] == ["user", "tool_call", "assistant", "done"]
    assert body["transcript"][-2]["text"] == "끝"
    assert assistant_leases(mongo) == [] and svc.running == {}
