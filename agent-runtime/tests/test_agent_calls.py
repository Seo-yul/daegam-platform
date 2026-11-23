"""다른 Agent 를 Tool 로 부르기(기능 agent_tools): 문서의 Agent 소스로 만든 `ask_*` Tool, 호출
경로와의 계약(시작, 묻기, 다시 묻기, 취소의 경로와 본문과 상태 코드), 위임 토큰과 위임 없는 턴의
머리, 바깥 내용 머리, 제한 시간, 컴파일.

`Routes` 가 플랫폼의 호출 경로 셋의 대역이다. 이 시험이 가정한 계약이 거기 모여 있다.

- `POST /api/internal/agent-calls/{agent id}` 본문 `{message}`. 답 200 `{run_id, status, answer?,
  code?}`.
- `GET /api/internal/agent-calls/{agent id}/runs/{run_id}?wait=<0..25 의 정수 초>`. 답은 같은
  꼴이다.
- `POST /api/internal/agent-calls/{agent id}/runs/{run_id}/cancel` 본문 `{reason}`
  (`caller_timeout`, `caller_ended`). 2xx 면 받아들인 것이다.
- `status` 가 `succeeded`·`waiting_approval`·`failed`·`refused`·`cancelled` 면 끝났고 그 밖은 더
  묻는다.
- 거절은 4xx 와 플랫폼 오류 본문 `{detail: {code, detail, limit?}}` 다. 묻기의 429 와 5xx 는 다시
  묻는다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from langchain_core.messages import ToolMessage
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from runtime import agent_calls
from runtime.agent_calls import (
    OUTSIDE_HEADER,
    OWNER_HEADER,
    SESSION_HEADER,
    TURN_HEADER,
    WAITING_MESSAGE,
    AgentCalls,
)
from runtime.compile import compile_agent
from runtime.config import ConfigError, parse_config
from runtime.features import AGENT_TOOLS, DOCUMENT_FEATURES
from runtime.long_term import TurnContext
from runtime.mcp import ToolSources
from runtime.outside import OutsideFlag
from runtime.state import Settings
from runtime.turn_grants import END_DONE, TurnGrant, TurnGrants, TurnRef
from tests.app_fixtures import tool_message
from tests.conftest import events_named, load_fixture, offline_doc, parse_sse, serve
from tests.safety_fixtures import interrupt_of, safety_app, send

CALLED = "ag-research-0001"
TOOL = "ask_research"
REF = "tools-research"
CALL_PATH = f"/api/internal/agent-calls/{CALLED}"
TARGET = ("agent", CALLED)
INSTANCE_TOKEN = "instance-token-value"
TURN = TurnRef("s-1", "t-1")
NOT_SENT = f"대화를 시작한 사용자의 Tool 권한을 다시 확인하지 못해 {TOOL} 호출을 보내지 않았다."
UNKNOWN_TURN = "어느 턴의 호출인지 알 수 없어 Tool 호출을 보내지 않았다."


def agent_doc(base_url: str = "http://platform.test", **source: Any) -> dict[str, Any]:
    """Agent 소스 하나만 가진 문서다. 표본의 계산기 소스를 뺐다."""
    doc = offline_doc()
    doc["entry"]["greeting"] = None
    doc["features"] = [AGENT_TOOLS]
    doc["tools"] = [
        {
            "ref": REF,
            "source": {"kind": "agent", "id": CALLED, "name": "research"},
            "url": f"{base_url}{CALL_PATH}",
            "allow": [TOOL],
            "approval": {},
            "timeouts": {},
            "description": "Answers research questions.",
            "timeout_seconds": 30,
            **source,
        }
    ]
    doc["main"]["tools"] = [REF]
    return doc


def answer(status: str, run_id: str = "r1", **extra: Any) -> tuple[int, dict[str, Any]]:
    return 200, {"run_id": run_id, "status": status, **extra}


def refusal(status: int, code: str, detail: str = "", **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"detail": {"code": code, "detail": detail, **extra}}


Reply = tuple[int, Any] | Exception


class Routes:
    """호출 경로 셋의 대역이다. 받은 요청을 적고, 경로마다 차례로 정해 둔 답을 한다. 마지막 답은
    되풀이한다."""

    def __init__(
        self,
        *,
        start: list[Reply] | None = None,
        poll: list[Reply] | None = None,
        cancel: list[Reply] | None = None,
    ) -> None:
        self.replies = {
            "start": start or [answer("succeeded", answer="42")],
            "poll": poll or [answer("succeeded", answer="42")],
            "cancel": cancel or [(200, {"cancelled": True})],
        }
        self.seen: list[dict[str, Any]] = []

    def kinds(self) -> list[str]:
        return [request["kind"] for request in self.seen]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [request for request in self.seen if request["kind"] == kind]

    def take(self, method: str, path: str, query: dict[str, str], headers: Any, body: Any) -> Reply:
        """요청 하나를 계약의 경로와 맞대어 적고 그 답을 고른다. 다른 경로는 계약 밖이다."""
        if (method, path) == ("POST", CALL_PATH):
            kind = "start"
        elif method == "GET" and path == f"{CALL_PATH}/runs/r1":
            kind = "poll"
        elif (method, path) == ("POST", f"{CALL_PATH}/runs/r1/cancel"):
            kind = "cancel"
        else:
            raise AssertionError(f"계약에 없는 요청이다: {method} {path}")
        self.seen.append({"kind": kind, "query": query, "headers": headers, "body": body})
        replies = self.replies[kind]
        return replies.pop(0) if len(replies) > 1 else replies[0]

    def client(self) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content) if request.content else None
            reply = self.take(
                request.method, request.url.path, dict(request.url.params), request.headers, body
            )
            if isinstance(reply, Exception):
                raise reply
            status, payload = reply
            if isinstance(payload, str):
                return httpx2.Response(status, text=payload)
            return httpx2.Response(status, json=payload)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))

    def app(self) -> Starlette:
        """같은 대역을 소켓으로 띄울 때의 앱이다(앱을 통째로 도는 시험)."""

        async def handle(request: Request) -> JSONResponse:
            raw = await request.body()
            reply = self.take(
                request.method,
                request.url.path,
                dict(request.query_params),
                request.headers,
                json.loads(raw) if raw else None,
            )
            assert not isinstance(reply, Exception)
            return JSONResponse(reply[1], status_code=reply[0])

        return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "POST"])])


def grant(*, target: tuple[str, str] = TARGET, token: str = "tok-alice") -> TurnGrant:
    kind, ident = target
    return TurnGrant.model_validate(
        {
            "id": "g-alice",
            "secret": "grant-secret-value",
            "sub": "alice",
            "tokens": [{"kind": kind, "id": ident, "token": token}],
            "expires_in": 300,
            "renew_url": "http://platform.test/api/agents/a1/turn-grants/g-alice",
        }
    )


def grant_platform() -> httpx2.AsyncClient:
    """위임의 갱신과 끝내기를 받는 플랫폼이다. 이 시험은 그 답을 보지 않는다."""
    return httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(204)))


@asynccontextmanager
async def calling(
    routes: Routes,
    *,
    given: TurnGrant | None = None,
    turn: TurnRef | None = TURN,
    instance_token: str | None = INSTANCE_TOKEN,
    now: Callable[[], float] | None = None,
    **source: Any,
) -> AsyncIterator[Callable[..., Any]]:
    """`ask_research` 를 턴 하나 안에서 부르는 함수를 준다. 턴은 제 태스크에서 돌아 문맥 변수가
    시험의 문맥에 남지 않는다. turn 이 None 이면 턴을 열지 않고 부른다."""
    grants = TurnGrants(instance_token=instance_token, http=grant_platform(), **_clock(now))
    calls = AgentCalls(
        parse_config(agent_doc(**source)),
        grants=grants,
        instance_token=instance_token,
        http=routes.client(),
    )
    (tool,) = calls.tools_by_ref[REF]

    async def call(message: str = "질문", *, context: TurnContext | None = None) -> ToolMessage:
        runtime = SimpleNamespace(context=context or turn_context())
        args = {"message": message, "runtime": runtime}
        request = {"type": "tool_call", "id": "c1", "name": TOOL, "args": args}

        async def in_turn() -> ToolMessage:
            opened = grants.open(given, turn=turn) if turn is not None else None
            try:
                return await tool.ainvoke(request)
            finally:
                await grants.close(opened, END_DONE)

        return await asyncio.create_task(in_turn())

    try:
        yield call
    finally:
        await grants.aclose()


def _clock(now: Callable[[], float] | None) -> dict[str, Any]:
    return {} if now is None else {"now": now}


def turn_context(*, outside: bool = False, owner: str = "alice", **fields: Any) -> TurnContext:
    return TurnContext(owner_sub=owner, outside=OutsideFlag(flagged=outside), **fields)


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """끝나지 않았다는 답과 실패한 묻기 뒤의 쉬는 시간을 줄인다."""
    monkeypatch.setattr(agent_calls, "POLL_RETRY_MAX_SECONDS", 0.02)
    monkeypatch.setattr(agent_calls.AgentCall, "poll_min_interval", 0.01)
    monkeypatch.setattr(agent_calls.AgentCall, "retry_initial", 0.01)


# -- 문서와 컴파일 --------------------------------------------------------------------


def test_the_feature_is_a_document_feature() -> None:
    assert AGENT_TOOLS == "agent_tools" and AGENT_TOOLS in DOCUMENT_FEATURES


def test_an_agent_source_needs_the_feature() -> None:
    doc = agent_doc()
    del doc["features"]
    with pytest.raises(ConfigError) as e:
        parse_config(doc)
    assert e.value.code == "schema"


def test_the_tool_is_built_from_the_entry_without_listing() -> None:
    config = parse_config(agent_doc())
    # 목록을 읽는 쪽은 Agent 소스를 모른다. 읽을 소스가 없으니 플랫폼이 답하지 않아도 준비된다
    sources = ToolSources(config)
    assert sources.refs == [] and sources.ready
    calls = AgentCalls(config, grants=sources.grants, instance_token=None)
    (tool,) = calls.tools_by_ref[REF]
    assert tool.name == TOOL
    assert tool.description.startswith("Answers research questions.")
    assert list(tool.tool_call_schema.model_json_schema()["properties"]) == ["message"]


def test_compile_puts_the_tool_under_its_source() -> None:
    config = parse_config(agent_doc())
    calls = AgentCalls(config, grants=TurnGrants(instance_token=None), instance_token=None)
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None, agent_calls=calls)
    assert [tool.name for tool in agent.tools] == [TOOL]
    assert agent.tool_sources[TOOL] == {"kind": "agent", "id": CALLED, "name": "research"}
    with pytest.raises(ConfigError) as e:
        compile_agent(config, tools_by_ref={}, checkpointer=None)
    assert e.value.code == "agent_calls_missing"


def test_the_callers_approval_rule_covers_the_tool() -> None:
    config = parse_config(agent_doc(approval={TOOL: ["approve", "reject"]}))
    calls = AgentCalls(config, grants=TurnGrants(instance_token=None), instance_token=None)
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None, agent_calls=calls)
    assert agent.interrupt_on == {TOOL: {"allowed_decisions": ["approve", "reject"]}}


# -- 시작과 답 ------------------------------------------------------------------------


async def test_a_call_that_ends_in_the_start_answers_at_once() -> None:
    routes = Routes()
    async with calling(routes, given=grant()) as call:
        message = await call("올해 매출은?")
    assert message.status == "success" and message.content == "42"
    assert message.artifact == {"structured_content": {"run_id": "r1", "status": "succeeded"}}
    (start,) = routes.seen
    assert start["kind"] == "start" and start["body"] == {"message": "올해 매출은?"}


async def test_an_unfinished_call_is_polled_until_it_settles() -> None:
    routes = Routes(
        start=[answer("starting")],
        poll=[answer("running"), answer("succeeded", answer="끝")],
    )
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.content == "끝" and routes.kinds() == ["start", "poll", "poll"]
    # 묻기는 플랫폼에 25초까지 기다려 달라고 한다
    assert [poll["query"] for poll in routes.of("poll")] == [{"wait": "25"}] * 2


async def test_a_failed_poll_is_asked_again() -> None:
    routes = Routes(
        start=[answer("running")],
        poll=[
            (503, {"detail": "restarting"}),
            httpx2.ConnectError("refused"),
            (429, {"detail": {"code": "rate_limited", "detail": ""}}),
            (200, "not json"),
            (200, {"status": "succeeded"}),
            answer("succeeded", answer="끝"),
        ],
    )
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.status == "success" and message.content == "끝"
    assert routes.kinds() == ["start", *["poll"] * 6]


async def test_a_failed_start_is_not_sent_again() -> None:
    routes = Routes(start=[httpx2.ConnectError("refused"), answer("succeeded")])
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.status == "error" and "ConnectError" in message.content
    assert routes.kinds() == ["start"]


async def test_a_waiting_run_answers_the_fixed_text_at_once() -> None:
    routes = Routes(start=[answer("waiting_approval")])
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.status == "success" and message.content == WAITING_MESSAGE
    assert message.artifact["structured_content"]["run_id"] == "r1"
    assert routes.kinds() == ["start"]


@pytest.mark.parametrize(
    ("status", "code"),
    [("failed", "acts_as_unavailable"), ("refused", "budget_exceeded"), ("cancelled", "stopped")],
)
async def test_a_run_that_ends_without_an_answer_is_a_tool_error(status: str, code: str) -> None:
    routes = Routes(start=[answer("running")], poll=[answer(status, code=code)])
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.status == "error"
    assert f"code: {code}" in message.content and "run_id: r1" in message.content


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (refusal(409, "agent_call_cycle", "The called Agent is in the chain."), "agent_call_cycle"),
        (refusal(409, "agent_calls_disabled", "Agent calls are off."), "agent_calls_disabled"),
        (refusal(429, "agent_call_busy", "The Agent is busy."), "The Agent is busy."),
        (refusal(403, "acts_as_unavailable"), "acts_as_unavailable"),
        (refusal(422, "input_too_long", "Too long.", limit=2000), "(limit: 2000)"),
        ((404, {"detail": "Not Found"}), "http_404"),
    ],
)
async def test_a_call_refused_before_a_run_is_a_tool_error_with_its_code(
    reply: Reply, expected: str
) -> None:
    routes = Routes(start=[reply])
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.status == "error" and expected in message.content
    assert routes.kinds() == ["start"]


async def test_a_poll_the_platform_refuses_ends_the_call() -> None:
    routes = Routes(start=[answer("running")], poll=[(404, {"detail": "Not Found"})])
    async with calling(routes, given=grant()) as call:
        message = await call()
    assert message.status == "error" and "http_404" in message.content
    assert routes.kinds() == ["start", "poll"]


# -- 제한 시간과 취소 -----------------------------------------------------------------


async def test_a_call_past_its_timeout_cancels_the_run_and_says_so() -> None:
    routes = Routes(start=[answer("running")], poll=[answer("running")])
    async with calling(routes, given=grant(), timeout_seconds=1) as call:
        message = await call()
    assert message.status == "error"
    assert "제한 시간 1초" in message.content and "실행도 취소했다" in message.content
    (cancel,) = routes.of("cancel")
    assert cancel["body"] == {"reason": "caller_timeout"}
    assert cancel["headers"]["authorization"] == "Bearer tok-alice"
    # 남은 시간이 1초라 묻기는 그보다 오래 기다려 달라고 하지 않는다
    assert {poll["query"]["wait"] for poll in routes.of("poll")} <= {"0", "1"}


async def test_a_cancel_that_fails_is_said() -> None:
    routes = Routes(
        start=[answer("running")], poll=[answer("running")], cancel=[(503, {"detail": "down"})]
    )
    async with calling(routes, given=grant(), timeout_seconds=1) as call:
        message = await call()
    assert message.status == "error" and "취소를 확인하지 못했다" in message.content


async def test_the_turns_remaining_time_cuts_the_wait() -> None:
    routes = Routes(start=[answer("running")], poll=[answer("running")])
    async with calling(routes, given=grant(), timeout_seconds=600) as call:
        soon = asyncio.get_running_loop().time() + 0.2
        message = await call(context=turn_context(deadline=soon))
    assert message.status == "error" and "제한 시간" in message.content
    assert routes.of("cancel")


async def test_a_stopped_turn_cancels_the_run() -> None:
    polled = asyncio.Event()
    routes = Routes(start=[answer("running")], poll=[answer("running")])
    take = routes.take

    def noting(*args: Any) -> Reply:
        reply = take(*args)
        if routes.kinds()[-1] == "poll":
            polled.set()
        return reply

    routes.take = noting  # type: ignore[method-assign]
    async with calling(routes) as call:
        task = asyncio.create_task(call())
        await polled.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    (cancel,) = routes.of("cancel")
    assert cancel["body"] == {"reason": "caller_ended"}


# -- 누구의 호출인가 ------------------------------------------------------------------


async def test_a_granted_turn_sends_only_its_token() -> None:
    routes = Routes(start=[answer("running")])
    async with calling(routes, given=grant()) as call:
        await call()
    for request in routes.seen:
        headers = request["headers"]
        assert headers["authorization"] == "Bearer tok-alice"
        assert headers[OUTSIDE_HEADER] == "0"
        for name in (SESSION_HEADER, TURN_HEADER, OWNER_HEADER):
            assert name not in headers


async def test_a_turn_without_a_grant_names_itself_with_the_instance_token() -> None:
    routes = Routes(start=[answer("running")])
    async with calling(routes, turn=TurnRef("s-9", "t-9")) as call:
        await call(context=turn_context(owner="앨리스@example.com"))
    for request in routes.seen:
        headers = request["headers"]
        assert headers["authorization"] == f"Bearer {INSTANCE_TOKEN}"
        assert (headers[SESSION_HEADER], headers[TURN_HEADER]) == ("s-9", "t-9")
        # 소유자는 UTF-8 의 퍼센트 인코딩이다
        assert headers[OWNER_HEADER] == "%EC%95%A8%EB%A6%AC%EC%8A%A4%40example.com"


async def test_a_grant_for_another_target_does_not_reach_the_call_routes() -> None:
    routes = Routes()
    other = grant(target=("gateway_instance", "gi-calc-0001"), token="tok-gateway")
    async with calling(routes, given=other) as call:
        await call()
    (start,) = routes.seen
    assert start["headers"]["authorization"] == f"Bearer {INSTANCE_TOKEN}"
    assert "tok-gateway" not in str(start["headers"])


async def test_an_expired_token_sends_nothing() -> None:
    # 위임을 열 때만 0 초이고 그 뒤로는 수명(300초)을 지난 시각이다
    opened_at = iter([0.0])
    routes = Routes()
    async with calling(routes, given=grant(), now=lambda: next(opened_at, 301.0)) as call:
        message = await call()
    # 인스턴스 토큰으로 내려가지 않는다
    assert message.status == "error" and message.content == NOT_SENT
    assert routes.seen == []


async def test_a_call_outside_a_turn_sends_nothing() -> None:
    routes = Routes()
    async with calling(routes, turn=None) as call:
        message = await call()
    assert message.status == "error" and message.content == UNKNOWN_TURN
    assert routes.seen == []


# -- 바깥 내용 머리 -------------------------------------------------------------------


async def test_every_request_says_whether_the_session_holds_outside_content() -> None:
    routes = Routes(start=[answer("running")])
    async with calling(routes, given=grant()) as call:
        await call(context=turn_context(outside=True))
    assert [r["headers"][OUTSIDE_HEADER] for r in routes.seen] == ["1", "1"]
    # 표시를 모르는 문맥은 가진 것으로 알린다
    routes = Routes()
    async with calling(routes, given=grant()) as call:
        await call(context=TurnContext(owner_sub="alice"))
    assert routes.seen[0]["headers"][OUTSIDE_HEADER] == "1"


# -- 앱을 통째로 ----------------------------------------------------------------------


async def test_a_chat_turn_calls_the_agent_and_marks_the_answer_outside() -> None:
    """위임 없는 턴(헤더 모드)의 호출이 제 세션과 턴, 소유자를 알리고, 답은 바깥 내용으로 표시되며,
    그 뒤의 호출은 쓰기라 승인을 묻는다."""
    routes = Routes(start=[answer("succeeded", answer="답이다")])
    ask = tool_message(TOOL, message="질문")
    async with serve(routes.app()) as base_url:
        doc = agent_doc(base_url)
        doc["features"] = [AGENT_TOOLS, "tool_safety"]
        doc["memory"] = load_fixture("support-agent.json")["memory"]
        settings = Settings(instance_token=INSTANCE_TOKEN)
        async with safety_app(doc, settings=settings) as running:
            auth = {"Authorization": f"Bearer {INSTANCE_TOKEN}"}
            running.client.headers.update(auth)
            response = await running.client.post(
                "/chat", json={"message": ask, "user_sub": "alice"}
            )
            assert response.status_code == 200, response.text
            (result,) = events_named(parse_sse(response.text), "tool_result")
            assert result["status"] == "success" and "답이다" in result["content"]
            assert result["untrusted"] == "outside"
            (start,) = routes.seen
            headers = start["headers"]
            assert headers["authorization"] == f"Bearer {INSTANCE_TOKEN}"
            assert headers[SESSION_HEADER] == response.headers["X-Daegam-Session-Id"]
            assert headers[TURN_HEADER] == response.headers["X-Daegam-Turn-Id"]
            assert headers[OWNER_HEADER] == "alice" and headers[OUTSIDE_HEADER] == "0"
            # 답을 받은 대화는 바깥 내용을 가졌으므로 다음 호출은 쓰기로서 승인을 묻는다
            sid = response.headers["X-Daegam-Session-Id"]
            asked = interrupt_of(await send(running, ask, session_id=sid))
            assert [r["name"] for r in asked["action_requests"]] == [TOOL]
            assert len(routes.seen) == 1
