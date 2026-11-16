"""턴 위임 토큰 (P53 6절): 받기와 사람 맞추기, 대상으로 고르기, 턴마다 클라이언트, 갱신과 그 규칙,
끝내기, 닫힌 쪽으로 넘어지기, 외부 경로가 위임 토큰과 위임을 받지 않기."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from runtime import mcp
from runtime.approvals import ApprovalChoice
from runtime.auth import AGENT_TARGET, Caller, CallerRules
from runtime.config import parse_config
from runtime.grant_gate import GRANT_MISMATCH, check_grant, holds_input, refuse_external_fields
from runtime.hitl import Decision
from runtime.mcp import ToolSources
from runtime.oidc import Claims
from runtime.turn_grants import (
    END_DONE,
    END_SHUTDOWN,
    RENEW_MIN_WAIT_SECONDS,
    RETRY_INITIAL_SECONDS,
    TURN_GRANT_HEADER,
    TurnCalls,
    TurnCredentials,
    TurnGrant,
    TurnGrants,
)
from tests.app_fixtures import tool_message, with_subagent
from tests.conftest import events_named, local_doc, parse_sse, run_app, serve

TARGET = ("gateway_instance", "gi-calc-0001")
SECRET = "grant-secret-value"


def grant(
    sub: str = "alice", *, token: str = "tok-alice", target=TARGET, **extra: Any
) -> TurnGrant:
    kind, ident = target
    return TurnGrant.model_validate(
        {
            "id": f"g-{sub}",
            "secret": SECRET,
            "sub": sub,
            "tokens": [{"kind": kind, "id": ident, "token": token}],
            "expires_in": 300,
            "renew_url": "http://platform.test/api/agents/a1/turn-grants/g1",
            **extra,
        }
    )


# -- 받기와 사람 맞추기 ----------------------------------------------------------------


OWNER = Caller(sub="alice", external=False, approver=False)
APPROVER = Caller(sub="bob", external=False, approver=True)
OUTSIDE = Caller(sub="alice", external=True, approver=False)


def _code(e: pytest.ExceptionInfo[HTTPException]) -> tuple[int, str]:
    return e.value.status_code, e.value.detail["code"]


def test_a_grant_must_name_the_turns_person() -> None:
    assert check_grant(OWNER, None, owner="alice") is None
    assert check_grant(OWNER, grant("alice"), owner="alice") is not None
    with pytest.raises(HTTPException) as e:
        check_grant(OWNER, grant("carol"), owner="alice")
    assert _code(e) == (409, GRANT_MISMATCH)


def test_an_approvers_input_never_runs_under_the_owners_grant() -> None:
    """승인만 한 결정은 소유자의 위임으로, 입력을 실은 결정은 결정자 자신의 위임으로 잇는다. 빈
    글의 거절도 입력이다 (P53 5.3절)."""
    approve = [Decision(type="approve")]
    assert check_grant(APPROVER, grant("alice"), owner="alice", decisions=approve) is not None
    for decisions in (
        [Decision(type="edit", edited_action={"name": "Add", "args": {"a": 1}})],
        [Decision(type="respond", message="x")],
        [Decision(type="reject", message="")],
        [Decision(type="approve"), Decision(type="reject", message="no")],
    ):
        assert holds_input(decisions)
        with pytest.raises(HTTPException) as e:
            check_grant(APPROVER, grant("alice"), owner="alice", decisions=decisions)
        assert _code(e) == (409, GRANT_MISMATCH)
        assert check_grant(APPROVER, grant("bob"), owner="alice", decisions=decisions)
    assert not holds_input([Decision(type="approve"), Decision(type="reject")])
    # 승인자로 표시되지 않은 결정자는 자기 위임을 낼 수 없다
    with pytest.raises(HTTPException):
        check_grant(OWNER, grant("bob"), owner="carol", decisions=approve)
    # 소유자 자신의 입력은 소유자의 위임이다
    edit = [Decision(type="edit", edited_action={"name": "Add", "args": {}})]
    assert check_grant(OWNER, grant("alice"), owner="alice", decisions=edit) is not None


def test_the_external_path_refuses_a_grant() -> None:
    refuse_external_fields(OUTSIDE, turn_grant=None)
    with pytest.raises(HTTPException) as e:
        refuse_external_fields(OUTSIDE, turn_grant=grant("alice"), approval_choices=None)
    assert _code(e) == (422, "validation_error")
    assert [err["loc"] for err in e.value.detail["errors"]] == [["body", "turn_grant"]]
    # 기억한 승인 선택(P43 5절)도 플랫폼 경로만 싣는다
    choices = [ApprovalChoice(tool="Add", choice="approve")]
    with pytest.raises(HTTPException) as e:
        refuse_external_fields(OUTSIDE, turn_grant=grant("alice"), approval_choices=choices)
    assert [err["loc"] for err in e.value.detail["errors"]] == [
        ["body", "turn_grant"],
        ["body", "approval_choices"],
    ]
    refuse_external_fields(OWNER, turn_grant=grant("alice"), approval_choices=choices)


def test_the_external_path_refuses_delegation_tokens_not_bound_to_this_agent() -> None:
    """발급자가 플랫폼(local 모드)이면 act 나 bound_to 를 실은 토큰은 이 Agent 에 묶인 것만 받는다.
    발급자가 IdP 인 oidc 모드(target 없음)는 그대로 받는다 (P53 3.3절)."""
    target = AGENT_TARGET.format("a1")
    local = CallerRules(delegation_target=target)
    oidc = CallerRules()

    def claims(**raw: Any) -> Claims:
        return Claims(
            sub="alice", email=None, groups=[], client_id=None, raw={"sub": "alice", **raw}
        )

    for raw in ({"act": {"sub": "svc:agent:x"}}, {"bound_to": "gateway:g"}, {"act": {}}):
        with pytest.raises(HTTPException) as e:
            local.caller(claims(**raw))
        assert _code(e) == (403, "forbidden")
        assert oidc.caller(claims(**raw)).sub == "alice"
    assert local.caller(claims(bound_to=target, act={"sub": "x"})).sub == "alice"
    assert local.caller(claims()).sub == "alice"


# -- 갱신 --------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


def credentials(
    handler: Callable[[httpx2.Request], httpx2.Response], clock: Clock
) -> tuple[TurnCredentials, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    creds = TurnCredentials(grant(), "instance-tok", lambda: http, now=clock, sleep=clock.sleep)
    return creds, seen


def renewed(token: str, expires_in: float = 300) -> httpx2.Response:
    body = {
        "tokens": [{"kind": TARGET[0], "id": TARGET[1], "token": token}],
        "expires_in": expires_in,
    }
    return httpx2.Response(200, json=body)


async def test_renewal_comes_when_a_third_of_the_life_remains() -> None:
    clock = Clock()
    creds, seen = credentials(lambda _r: renewed("tok-2"), clock)
    assert await creds._renew_once() is None and creds.token(TARGET) == "tok-2"
    request = seen[0]
    assert request.url.path.endswith("/turn-grants/g1/renew")
    assert request.headers["authorization"] == "Bearer instance-tok"
    assert json.loads(request.content) == {"secret": SECRET}
    clock2 = Clock()
    creds2, seen2 = credentials(lambda _r: renewed("tok-3"), clock2)
    task = asyncio.create_task(creds2._renew_loop())
    for _ in range(20):
        await asyncio.sleep(0)
        if len(seen2) >= 2:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert clock2.waits[:2] == [200.0, 200.0]


async def test_renewal_retries_transient_failures_and_stops_on_a_refusal() -> None:
    clock = Clock()
    answers = iter(
        [
            httpx2.ConnectError("down"),
            httpx2.Response(429, headers={"retry-after": "7"}),
            httpx2.Response(503),
            renewed("tok-2"),
        ]
    )

    def handler(_r: httpx2.Request) -> httpx2.Response:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    creds, seen = credentials(handler, clock)
    await creds._renew()
    assert len(seen) == 4 and creds.token(TARGET) == "tok-2" and not creds.stopped
    assert 7.0 in clock.waits
    for status in (401, 403, 404, 410):
        clock = Clock()
        creds, seen = credentials(lambda _r, s=status: httpx2.Response(s), clock)
        await creds._renew()
        assert creds.stopped and len(seen) == 1


async def test_a_malformed_renewal_answer_is_retried_and_never_logged(caplog) -> None:
    """모양이 맞지 않는 200 은 다시 한다. 답에 토큰이 있을 수 있어 로그에는 위임 id 만 남는다."""
    clock = Clock()
    leak = {"tokens": [{"kind": TARGET[0], "id": TARGET[1], "token": "tok-leak"}], "expires_in": -1}
    answers = iter(
        [httpx2.Response(200, content=b"not json"), httpx2.Response(200, json=leak), renewed("t2")]
    )
    creds, seen = credentials(lambda _r: next(answers), clock)
    with caplog.at_level("WARNING"):
        await creds._renew()
    assert len(seen) == 3 and creds.token(TARGET) == "t2" and not creds.stopped
    assert "g-alice" in caplog.text and "tok-leak" not in caplog.text


async def test_a_token_expires_on_the_pods_own_clock() -> None:
    clock = Clock()
    creds, _seen = credentials(lambda _r: httpx2.Response(503), clock)
    assert creds.token(TARGET) == "tok-alice" and creds.holds(TARGET)
    clock.t = 299.0
    assert creds.token(TARGET) == "tok-alice"
    clock.t = 300.0
    assert creds.token(TARGET) is None and creds.holds(TARGET)
    # 재시도는 만료에서 멈춘다
    clock.t = 250.0
    await creds._renew()
    assert creds.stopped and clock.t >= 300.0


# -- 턴의 호출 -------------------------------------------------------------------------


@pytest.fixture
def seen_server() -> tuple[FastMCP, list[str]]:
    """Add 하나를 가진 MCP 서버. 호출마다 받은 Authorization 을 적는다."""
    seen: list[str] = []
    server = FastMCP("calc")

    @server.tool(name="Add")
    def add(a: int, b: int) -> int:
        seen.append(get_http_headers(include={"authorization"}).get("authorization", ""))
        return a + b

    return server, seen


CALL = {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 3, "b": 4}}
# 런타임 Tool 오류 두 줄 (P53 화면 문구 표)의 {tool} 을 Add 로 채운
# 글이다. UNKNOWN_TURN 은 같은 표의 세 번째 줄이다
UNKNOWN_TURN = "어느 턴의 호출인지 알 수 없어 Tool 호출을 보내지 않았다."
NOT_SENT = "대화를 시작한 사용자의 Tool 권한을 다시 확인하지 못해 Add 호출을 보내지 않았다."
STOPPED = (
    "대화를 시작한 사용자의 Tool 권한을 다시 확인하지 못해 Add 호출을 도중에 멈췄다. "
    "이미 실행됐을 수 있다."
)


def platform_http() -> tuple[httpx2.AsyncClient, list[dict[str, Any]]]:
    """renew·end 를 받는 가짜 플랫폼. /end 의 위임 경로와 까닭을 적는다."""
    ended: list[dict[str, Any]] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/end"):
            ended.append({"path": request.url.path, **json.loads(request.content)})
            return httpx2.Response(204)
        return httpx2.Response(503)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(answer)), ended


def in_turn(grants: TurnGrants, given: TurnGrant | None, call: Callable[[], Any]) -> Any:
    """턴 하나를 제 태스크에서 돈다. 위임을 열어 그 문맥에 두고, call 을 부르고, 닫는다. 태스크가
    따로라 문맥 변수가 시험의 문맥에 남지 않는다."""

    async def turn() -> Any:
        calls = grants.open(given)
        try:
            return await call()
        finally:
            await grants.close(calls, END_DONE)

    return asyncio.create_task(turn())


async def test_a_turn_call_carries_its_grant_and_a_refresh_the_pods_identity(seen_server) -> None:
    server, seen = seen_server
    http, _ended = platform_http()
    async with serve(server.http_app()) as base_url:
        grants = TurnGrants(instance_token=None, http=http)
        sources = ToolSources(parse_config(local_doc(f"{base_url}/mcp")), grants=grants)
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]
        message = await in_turn(grants, grant(), lambda: add.ainvoke(CALL))
        assert message.status == "success" and seen[-1] == "Bearer tok-alice"
        # 위임 없는 턴과 위임이 이 대상에 토큰을 싣지 않은 턴은 파드 자신의 자격이다
        message = await in_turn(grants, None, lambda: add.ainvoke(CALL))
        assert message.status == "success" and seen[-1] == ""
        other = grant("carol", token="tok-carol", target=("mcp_instance", "other"))
        message = await in_turn(grants, other, lambda: add.ainvoke(CALL))
        assert message.status == "success" and seen[-1] == ""

        # 끝난 토큰은 보내지 않고 Tool 오류다. Agent 의 자격으로 내려가지 않는다
        async def expired() -> Any:
            [calls] = grants.open_calls
            calls.credentials.expires_at = 0.0
            return await add.ainvoke(CALL)

        before = len(seen)
        message = await in_turn(grants, grant(), expired)
        assert message.status == "error" and NOT_SENT in str(message.content)
        assert len(seen) == before
        # 턴 밖의 호출은 이 파드에 열린 위임이 있으면 보내지 않는다
        held = asyncio.Event()
        holder = in_turn(grants, grant(), held.wait)
        await asyncio.sleep(0)
        message = await add.ainvoke(CALL)
        assert message.status == "error" and str(message.content) == UNKNOWN_TURN
        assert len(seen) == before
        held.set()
        await holder
        # 열린 위임이 없으면 턴 밖의 호출도 파드의 자격으로 간다
        message = await add.ainvoke(CALL)
        assert message.status == "success" and seen[-1] == ""
    await http.aclose()


async def test_two_peoples_turns_on_one_pod_reach_the_server_with_their_own_tokens(
    seen_server,
) -> None:
    server, seen = seen_server
    http, _ended = platform_http()
    async with serve(server.http_app()) as base_url:
        grants = TurnGrants(instance_token=None, http=http)
        sources = ToolSources(parse_config(local_doc(f"{base_url}/mcp")), grants=grants)
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]

        async def three() -> None:
            for _ in range(3):
                assert (await add.ainvoke(CALL)).status == "success"

        await asyncio.gather(
            in_turn(grants, grant("alice", token="tok-alice"), three),
            in_turn(grants, grant("bob", token="tok-bob"), three),
        )
    await http.aclose()
    assert sorted(seen) == ["Bearer tok-alice"] * 3 + ["Bearer tok-bob"] * 3


async def test_a_turns_close_ends_only_its_own_grant() -> None:
    """한 세션의 앞 턴이 늦게 닫혀도 다음 턴의 위임은 열린 채 갱신을 이어 간다. 닫기는 두 번 불러도
    한 번만 끝낸다. 파드가 내려가면 남은 위임을 shutdown 으로 끝낸다."""
    http, ended = platform_http()
    grants = TurnGrants(instance_token=None, http=http)

    async def opened(given: TurnGrant) -> TurnCalls:
        calls = grants.open(given)
        assert calls is not None
        return calls

    first, second = [
        await asyncio.create_task(opened(grant(id=g, renew_url=f"http://platform.test/{g}")))
        for g in ("g-1", "g-2")
    ]
    await grants.close(first, END_DONE)
    await grants.close(first, END_DONE)
    assert grants.open_calls == {second} and first.credentials.token(TARGET) is None
    task = second.credentials._task
    assert task is not None and not task.done() and second.credentials.token(TARGET)
    await grants.aclose()
    assert [(e["path"], e["reason"]) for e in ended] == [
        ("/g-1/end", END_DONE),
        ("/g-2/end", END_SHUTDOWN),
    ]
    assert task.done() and not grants.open_calls
    await http.aclose()


async def test_a_turn_without_a_grant_never_takes_another_turns_grant(seen_server) -> None:
    """같은 세션의 앞 턴 위임이 아직 닫히지 않았어도 위임 없는 다음 턴은 파드의 자격이다."""
    server, seen = seen_server
    http, _ended = platform_http()
    async with serve(server.http_app()) as base_url:
        grants = TurnGrants(instance_token=None, http=http)
        sources = ToolSources(parse_config(local_doc(f"{base_url}/mcp")), grants=grants)
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]
        held = asyncio.Event()
        late = in_turn(grants, grant(), held.wait)
        await asyncio.sleep(0)
        message = await in_turn(grants, None, lambda: add.ainvoke(CALL))
        assert message.status == "success" and seen == [""]
        held.set()
        await late
    await http.aclose()


@pytest.mark.parametrize("ends_at", ["connect", "call"])
async def test_a_token_that_ends_mid_call_fails_the_call_at_once(
    seen_server, monkeypatch, ends_at
) -> None:
    """입구의 만료 검사를 지난 뒤 토큰이 끝나면 요청을 보내지 않고 곧바로 Tool 오류다. 연결 중이면
    fastmcp 가 RuntimeError 로, 연결 뒤면 MCP 전송이 인증의 예외를 삼켜 Tool 타임아웃까지 기다리게
    되는 자리다. 끊은 호출은 이미 실행됐을 수 있다고 알린다. 끊긴 클라이언트는 닫고 버려서, 갱신으로
    토큰이 다시 살면 같은 턴의 다음 호출이 새 클라이언트로 나간다."""
    server, seen = seen_server
    http, _ended = platform_http()
    clock = Clock()
    built: list[Any] = []
    async with serve(server.http_app()) as base_url:
        grants = TurnGrants(instance_token=None, http=http, now=clock)
        doc = local_doc(f"{base_url}/mcp")
        sources = ToolSources(parse_config(doc), grants=grants, default_timeout=60)
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]
        build = mcp.build_client

        def expire() -> None:
            # 첫 클라이언트에서 한 번만 끝낸다
            if len(built) == 1:
                clock.t = 1000.0

        def build_then_expire(source: dict[str, Any], auth: Any) -> Any:
            client = build(source, auth)
            built.append(client)
            if ends_at == "connect":
                expire()
                return client
            call_tool = client.call_tool

            async def expire_then_call(*args: Any, **kwargs: Any) -> Any:
                expire()
                return await call_tool(*args, **kwargs)

            client.call_tool = expire_then_call
            return client

        monkeypatch.setattr(mcp, "build_client", build_then_expire)

        async def call() -> Any:
            async with asyncio.timeout(5):
                return await add.ainvoke(CALL)

        async def lapse_then_renew() -> tuple[Any, Any]:
            cut = await call()
            clock.t = 0.0  # 갱신이 토큰을 되살린 자리다
            return cut, await call()

        cut, again = await in_turn(grants, grant(), lapse_then_renew)
    await http.aclose()
    assert cut.status == "error" and STOPPED in str(cut.content)
    assert again.status == "success" and seen == ["Bearer tok-alice"]
    assert len(built) == 2 and not built[0].is_connected()


async def test_a_bad_retry_after_and_a_short_life_do_not_spin_the_renewal() -> None:
    """Retry-After 가 초가 아니면 기본 간격이다. 아주 짧은 수명을 받아도 다음 갱신은 가장 짧은 간격
    뒤다."""
    clock = Clock()
    answers = iter(
        [httpx2.Response(503, headers={"retry-after": "²".encode("latin-1")}), renewed("t2", 0.001)]
    )
    creds, seen = credentials(lambda _r: next(answers, renewed("t3", 0.001)), clock)
    task = asyncio.create_task(creds._renew_loop())
    for _ in range(50):
        await asyncio.sleep(0)
        if len(seen) >= 4:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert clock.waits[0] == 200.0 and clock.waits[1] == RETRY_INITIAL_SECONDS
    assert all(w >= RENEW_MIN_WAIT_SECONDS for w in clock.waits[2:])


# -- 한 턴 전체 -----------------------------------------------------------------------


@asynccontextmanager
async def platform_stub() -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """renew·end 를 받는 플랫폼 자리. 받은 본문과 경로를 적는다."""
    seen: list[dict[str, Any]] = []

    async def record(request: Request) -> JSONResponse:
        seen.append({"path": request.url.path, "body": await request.json()})
        return JSONResponse({}, status_code=204 if request.url.path.endswith("/end") else 404)

    app = Starlette(routes=[Route("/{rest:path}", record, methods=["POST"])])
    async with serve(app) as base_url:
        yield base_url, seen


async def test_a_turn_uses_its_grant_echoes_its_id_and_ends_it(seen_server) -> None:
    """턴의 Tool 호출이 위임 토큰으로 나가고, 응답 머리가 위임 id 를 되돌리며, turn_start 는 위임 id
    만 싣고, 턴이 끝나면 /end 를 부른다. 토큰과 비밀은 이벤트·전사에 없다."""
    server, seen = seen_server
    async with serve(server.http_app()) as base_url, platform_stub() as (platform, ended):
        given = grant(renew_url=f"{platform}/api/agents/a1/turn-grants/g-alice").model_dump()
        async with run_app(local_doc(f"{base_url}/mcp")) as running:
            await running.runtime.refresh_tools()
            index = running.runtime.store.index
            release = index.release_turn
            at_release: list[tuple[int, int]] = []

            async def recording(session_id: str, token: str) -> None:
                at_release.append((len(running.runtime.grants.open_calls), len(ended)))
                await release(session_id, token)

            index.release_turn = recording
            body = {"message": tool_message("Add", a=3, b=4), "user_sub": "alice"}
            response = await running.client.post("/chat", json={**body, "turn_grant": given})
            assert response.status_code == 200, response.text
            assert response.headers[TURN_GRANT_HEADER] == "g-alice"
            sse = parse_sse(response.text)
            assert events_named(sse, "tool_result") and seen == ["Bearer tok-alice"]
            [start] = running.observed("turn_start")
            assert start["grant_id"] == "g-alice"
            sid = response.headers["X-Daegam-Session-Id"]
            transcript = (
                await running.client.get(f"/sessions/{sid}", params={"user_sub": "alice"})
            ).text
            for secret in (SECRET, "tok-alice"):
                assert secret not in running.log.getvalue() and secret not in transcript
            assert not running.runtime.grants.open_calls
            # 위임은 리스를 풀기 전에 끝냈다
            assert at_release == [(0, 1)]
            # 다른 사람의 위임은 409 다
            other = grant("carol").model_dump()
            again = await running.client.post(
                "/chat", json={**body, "session_id": sid, "turn_grant": other}
            )
            assert again.status_code == 409 and again.json()["code"] == GRANT_MISMATCH
    assert [e["path"].rsplit("/", 1)[-1] for e in ended] == ["end"]
    assert ended[0]["body"] == {"secret": SECRET, "reason": "done"}


async def test_an_old_body_without_a_grant_runs_as_today(seen_server) -> None:
    server, seen = seen_server
    async with serve(server.http_app()) as base_url:
        async with run_app(local_doc(f"{base_url}/mcp")) as running:
            await running.runtime.refresh_tools()
            body = {"message": tool_message("Add", a=1, b=2), "user_sub": "alice"}
            response = await running.client.post("/chat", json=body)
            assert response.status_code == 200 and TURN_GRANT_HEADER not in response.headers
    assert seen == [""]


async def test_a_subagents_calls_carry_the_turns_grant(seen_server) -> None:
    """서브에이전트의 실행은 턴의 문맥을 물려받아 그 Tool 호출도 턴의 위임 토큰으로 나간다."""
    server, seen = seen_server
    async with serve(server.http_app()) as base_url, platform_stub() as (platform, ended):
        doc = local_doc(f"{base_url}/mcp")
        with_subagent(doc)
        given = grant(renew_url=f"{platform}/api/agents/a1/turn-grants/g-alice").model_dump()
        description = tool_message("Add", a=1, b=2)
        task = {"tool": "task", "args": {"description": description, "subagent_type": "calc"}}
        async with run_app(doc) as running:
            await running.runtime.refresh_tools()
            body = {"message": json.dumps(task), "user_sub": "alice", "turn_grant": given}
            response = await running.client.post("/chat", json=body)
            assert response.status_code == 200, response.text
            results = events_named(parse_sse(response.text), "tool_result")
            assert any(r["name"] == "Add" and r.get("agent") for r in results)
    assert seen == ["Bearer tok-alice"] and [e["body"]["reason"] for e in ended] == [END_DONE]


async def test_the_credentials_report_a_refused_renewal() -> None:
    """갱신이 다시 할 수 없는 4xx 로 거절되면 그 상태 코드를 알린다."""
    seen: list[int] = []
    creds, _ = credentials(lambda _r: httpx2.Response(403), Clock())
    creds.on_refused = seen.append
    await creds._renew()
    assert creds.stopped and seen == [403]
