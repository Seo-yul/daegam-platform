"""다른 Agent 에게 작업을 맡기는 Tool 다섯이다(agent_task_tools.py). 잠긴 deepagents 의 비동기
서브에이전트 Tool 과 이름, 인자, 설명을 맞대고, Agent Protocol 경로의 대역으로 Tool 이 보내는 요청과
읽는 칸, 상태에 적는 작업, deepagents 와 다르게 답하는 곳을 본다. 끝에서는 앱을 통째로 띄워 맡긴
작업이 세션의 상태에 남아 다음 턴이 확인하는 것을 본다.
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
from deepagents.middleware import async_subagents as upstream
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from runtime.agent_calls import OUTSIDE_HEADER, AgentCalls
from runtime.agent_task_tools import (
    CANCEL,
    CHECK,
    FINAL_STATUSES,
    LIST,
    START,
    STATE_KEY,
    TASK_TOOLS,
    UPDATE,
    AsyncTaskState,
    task_tools_by_ref,
)
from runtime.compile import compile_agent
from runtime.config import ConfigError, parse_config
from runtime.features import AGENT_TASKS, AGENT_TOOLS, CAPABILITIES
from runtime.state import Settings
from runtime.turn_grants import END_DONE, TurnGrants
from tests.app_fixtures import tool_message
from tests.conftest import events_named, load_fixture, offline_doc, parse_sse, serve
from tests.safety_fixtures import interrupt_of, safety_app, send
from tests.test_agent_calls import (
    INSTANCE_TOKEN,
    TURN,
    grant,
    grant_platform,
    refusal,
    turn_context,
)

CALLED = "ag-research-0001"
OTHER = "ag-billing-0001"
REF = "tools-research"
BASE = f"/api/internal/agent-protocol/{CALLED}"
THREAD = "th-1"
RUN = "r-1"
RUN_PATH = f"{BASE}/threads/{THREAD}/runs/{RUN}"
# 맡기는 실행의 본문이다. 잇는 실행은 여기에 `multitask_strategy` 를 더한다
RUN_BODY: dict[str, Any] = {
    "input": {"messages": [{"role": "user", "content": "미수금을 찾아 줘"}]},
    "stream_mode": "values",
    "stream_subgraphs": False,
    "stream_resumable": False,
    "assistant_id": CALLED,
}


def task_source(ref: str = REF, called: str = CALLED, name: str = "research", **over: Any):
    return {
        "ref": ref,
        "source": {"kind": "agent", "id": called, "name": name},
        "url": f"http://platform.test/api/internal/agent-protocol/{called}",
        "allow": list(TASK_TOOLS),
        "approval": {},
        "timeouts": {},
        "description": f"Answers {name} questions.",
        "mode": "task",
        **over,
    }


def task_doc(*sources: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
    """작업을 맡기는 Agent 소스만 가진 문서다. base_url 을 주면 소스의 주소를 그 서버로 바꾼다."""
    doc = offline_doc()
    doc["entry"]["greeting"] = None
    doc["features"] = [AGENT_TOOLS]
    doc["tools"] = list(sources) or [task_source()]
    if base_url is not None:
        for source in doc["tools"]:
            source["url"] = source["url"].replace("http://platform.test", base_url)
    doc["main"]["tools"] = [source["ref"] for source in doc["tools"]]
    return doc


def tracked(status: str = "running", task_id: str = THREAD, **over: Any) -> dict[str, Any]:
    """세션의 상태에 적혀 있는 작업 하나다."""
    stamp = "2026-01-01T00:00:00Z"
    return {
        "task_id": task_id,
        "agent_name": "research",
        "thread_id": task_id,
        "run_id": RUN,
        "status": status,
        "created_at": stamp,
        "last_checked_at": stamp,
        "last_updated_at": stamp,
        **over,
    }


Reply = tuple[int, Any] | Exception


class Protocol:
    """Agent Protocol 경로의 대역이다. 받은 요청을 적고 (메서드, 경로)마다 정해 둔 답을 한다. 정해
    두지 않은 요청은 계약 밖이다."""

    def __init__(self, replies: dict[tuple[str, str], Reply]) -> None:
        self.replies = replies
        self.seen: list[dict[str, Any]] = []

    def take(
        self, method: str, path: str, query: dict[str, str], headers: Any, raw: bytes
    ) -> Reply:
        if (method, path) not in self.replies:
            raise AssertionError(f"계약에 없는 요청이다: {method} {path}")
        self.seen.append(
            {"call": (method, path), "query": query, "headers": headers, "content": raw}
        )
        return self.replies[(method, path)]

    def calls(self) -> list[tuple[str, str]]:
        return [request["call"] for request in self.seen]

    def body(self, index: int) -> Any:
        return json.loads(self.seen[index]["content"])

    def client(self) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            reply = self.take(
                request.method,
                request.url.path,
                dict(request.url.params),
                request.headers,
                request.content,
            )
            if isinstance(reply, Exception):
                raise reply
            status, payload = reply
            if payload is None or isinstance(payload, str):
                return httpx2.Response(status, text=payload or "")
            return httpx2.Response(status, json=payload)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))

    def app(self) -> Starlette:
        async def handle(request: Request) -> Response:
            reply = self.take(
                request.method,
                request.url.path,
                dict(request.query_params),
                request.headers,
                await request.body(),
            )
            assert not isinstance(reply, Exception)
            if reply[1] is None:
                return Response(status_code=reply[0])
            return JSONResponse(reply[1], status_code=reply[0])

        return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "POST"])])


def started() -> dict[tuple[str, str], Reply]:
    """작업 하나를 맡기는 두 요청의 답이다."""
    return {
        ("POST", f"{BASE}/threads"): (200, {"thread_id": THREAD}),
        ("POST", f"{BASE}/threads/{THREAD}/runs"): (
            200,
            {"run_id": RUN, "thread_id": THREAD, "status": "pending"},
        ),
    }


def run_is(status: str, **extra: Any) -> dict[tuple[str, str], Reply]:
    return {
        ("GET", RUN_PATH): (200, {"run_id": RUN, "thread_id": THREAD, "status": status, **extra})
    }


def answer_is(*contents: str) -> dict[tuple[str, str], Reply]:
    messages = [{"type": "ai", "content": content} for content in contents]
    return {("GET", f"{BASE}/threads/{THREAD}"): (200, {"values": {"messages": messages}})}


Call = Callable[..., Any]


@asynccontextmanager
async def tasks(
    protocol: Protocol, *sources: dict[str, Any], state: dict[str, Any] | None = None
) -> AsyncIterator[Call]:
    """작업 Tool 을 위임을 가진 턴 하나 안에서 부르는 함수를 준다. state 는 세션에 적혀 있는
    작업이다."""
    grants = TurnGrants(instance_token=INSTANCE_TOKEN, http=grant_platform())
    config = parse_config(task_doc(*sources))
    calls = AgentCalls(config, grants=grants, instance_token=INSTANCE_TOKEN, http=protocol.client())
    tools = {tool.name: tool for tool in task_tools_by_ref(config, calls)[REF]}

    async def call(name: str, **args: Any) -> ToolMessage | Command | str:
        runtime = SimpleNamespace(
            context=turn_context(), state={STATE_KEY: state or {}}, tool_call_id="c1"
        )
        request = {
            "type": "tool_call",
            "id": "c1",
            "name": name,
            "args": {**args, "runtime": runtime},
        }

        async def in_turn() -> Any:
            opened = grants.open(grant(), turn=TURN)
            try:
                return await tools[name].ainvoke(request)
            finally:
                await grants.close(opened, END_DONE)

        return await asyncio.create_task(in_turn())

    try:
        yield call
    finally:
        await grants.aclose()


def said(result: Any) -> str:
    """Tool 결과의 글이다. 상태를 고친 결과는 그 메시지의 글이다."""
    if isinstance(result, Command):
        return result.update["messages"][0].content
    return result.content if isinstance(result, ToolMessage) else result


def noted(result: Any) -> dict[str, Any]:
    """Tool 결과가 상태에 적는 작업이다."""
    assert isinstance(result, Command)
    return result.update[STATE_KEY]


def failed(result: Any) -> str:
    """Tool 오류의 글이다."""
    assert isinstance(result, ToolMessage) and result.status == "error", result
    return result.content


# -- deepagents 와 맞대기 --------------------------------------------------------------


def _schema(tool: Any) -> dict[str, Any]:
    """Tool 의 인자 스키마에서 모델 이름과 그 설명을 뺀 것이다."""
    schema = tool.args_schema.model_json_schema()
    return {k: v for k, v in schema.items() if k not in ("title", "description")}


def test_the_five_tools_match_the_locked_deepagents() -> None:
    """이름, 인자 스키마, 설명이 잠긴 deepagents 의 것과 같다. deepagents 가 바꾸면 여기서
    깨진다."""
    spec = {
        "name": "research",
        "description": "Answers research questions.",
        "graph_id": CALLED,
        "url": "http://platform.test",
    }
    theirs = upstream.AsyncSubAgentMiddleware(async_subagents=[spec]).tools
    config = parse_config(task_doc())
    calls = AgentCalls(config, grants=TurnGrants(instance_token=None), instance_token=None)
    ours = task_tools_by_ref(config, calls)[REF]
    assert tuple(tool.name for tool in ours) == TASK_TOOLS
    assert [tool.name for tool in ours] == [tool.name for tool in theirs]
    for mine, their in zip(ours, theirs, strict=True):
        assert _schema(mine) == _schema(their), mine.name
        assert mine.description == their.description, mine.name


def test_the_final_statuses_and_the_state_are_deepagents_own() -> None:
    assert FINAL_STATUSES == upstream._TERMINAL_STATUSES
    assert AsyncTaskState.state_schema is upstream.AsyncSubAgentMiddleware.state_schema
    assert STATE_KEY in upstream.AsyncSubAgentState.__annotations__


def test_the_capability_is_the_one_the_called_side_reports() -> None:
    assert AGENT_TASKS in CAPABILITIES


# -- 문서와 컴파일 ---------------------------------------------------------------------


def test_task_sources_of_the_main_agent_share_one_set_of_tools() -> None:
    billing = task_source("tools-billing", OTHER, "billing")
    config = parse_config(task_doc(task_source(), billing))
    calls = AgentCalls(config, grants=TurnGrants(instance_token=None), instance_token=None)
    assert calls.tools_by_ref == {}
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None, agent_calls=calls)
    assert tuple(tool.name for tool in agent.tools) == TASK_TOOLS
    start = next(tool for tool in agent.tools if tool.name == START)
    assert "- research: Answers research questions." in start.description
    assert "- billing: Answers billing questions." in start.description


def test_a_subagent_may_not_point_at_a_task_source() -> None:
    doc = task_doc()
    (sub,) = load_fixture("knowledge-agent.json")["subagents"]
    # 표본의 서브에이전트에서 이 문서의 판에 없는 knowledge 칸을 뺀다
    kept = {key: value for key, value in sub.items() if key != "knowledge"}
    doc["subagents"] = [{**kept, "tools": [REF]}]
    doc["main"]["subagents"] = [sub["name"]]
    config = parse_config(doc)
    calls = AgentCalls(config, grants=TurnGrants(instance_token=None), instance_token=None)
    with pytest.raises(ConfigError) as e:
        compile_agent(config, tools_by_ref={}, checkpointer=None, agent_calls=calls)
    assert e.value.code == "agent_task_not_main"


def test_the_callers_approval_rule_covers_a_task_tool() -> None:
    config = parse_config(task_doc(task_source(approval={START: ["approve", "reject"]})))
    calls = AgentCalls(config, grants=TurnGrants(instance_token=None), instance_token=None)
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None, agent_calls=calls)
    assert agent.interrupt_on == {START: {"allowed_decisions": ["approve", "reject"]}}


# -- 요청 다섯 -------------------------------------------------------------------------


async def test_start_creates_a_thread_then_a_run_and_tracks_the_task() -> None:
    protocol = Protocol(started())
    async with tasks(protocol) as call:
        result = await call(START, description="미수금을 찾아 줘", subagent_type="research")
    assert said(result) == f"Launched async subagent. task_id: {THREAD}"
    assert protocol.calls() == [
        ("POST", f"{BASE}/threads"),
        ("POST", f"{BASE}/threads/{THREAD}/runs"),
    ]
    assert protocol.body(0) == {} and protocol.body(1) == RUN_BODY
    (task,) = noted(result).values()
    assert task["task_id"] == task["thread_id"] == THREAD
    assert (task["run_id"], task["agent_name"], task["status"]) == (RUN, "research", "running")


async def test_every_request_carries_the_turns_token_and_no_sdk_header() -> None:
    protocol = Protocol(started())
    async with tasks(protocol) as call:
        await call(START, description="x", subagent_type="research")
    for request in protocol.seen:
        headers = request["headers"]
        assert headers["authorization"] == "Bearer tok-alice"
        assert headers[OUTSIDE_HEADER] == "0"
        assert "x-api-key" not in headers and "x-auth-scheme" not in headers


async def test_an_unknown_agent_and_an_untracked_task_send_nothing() -> None:
    protocol = Protocol({})
    async with tasks(protocol) as call:
        unknown = await call(START, description="x", subagent_type="nobody")
        assert "Unknown async subagent type `nobody`" in said(unknown)
        assert "`research`" in said(unknown)
        for name, args in ((CHECK, {}), (UPDATE, {"message": "m"}), (CANCEL, {})):
            lost = await call(name, task_id="th-9", **args)
            assert said(lost) == "No tracked task found for task_id: 'th-9'"
    assert protocol.seen == []


async def test_check_reads_the_run_and_the_answer_only_after_success() -> None:
    protocol = Protocol(run_is("running"))
    async with tasks(protocol, state={THREAD: tracked()}) as call:
        result = await call(CHECK, task_id=f" {THREAD} ")
    assert json.loads(said(result)) == {"status": "running", "thread_id": THREAD}
    assert protocol.calls() == [("GET", RUN_PATH)]

    protocol = Protocol({**run_is("success"), **answer_is("먼저 한 말", "미수금은 셋이다")})
    async with tasks(protocol, state={THREAD: tracked()}) as call:
        result = await call(CHECK, task_id=THREAD)
    assert json.loads(said(result)) == {
        "status": "success",
        "thread_id": THREAD,
        "result": "미수금은 셋이다",
    }
    assert protocol.calls() == [("GET", RUN_PATH), ("GET", f"{BASE}/threads/{THREAD}")]
    assert noted(result)[THREAD]["status"] == "success"


async def test_check_says_why_a_run_failed_or_was_cancelled() -> None:
    for status in ("error", "cancelled"):
        protocol = Protocol(run_is(status, error="acts_as_lost_access: the person lost access"))
        async with tasks(protocol, state={THREAD: tracked()}) as call:
            result = await call(CHECK, task_id=THREAD)
        assert json.loads(said(result)) == {
            "status": status,
            "thread_id": THREAD,
            "error": "acts_as_lost_access: the person lost access",
        }
    protocol = Protocol(run_is("error"))
    async with tasks(protocol, state={THREAD: tracked()}) as call:
        result = await call(CHECK, task_id=THREAD)
    assert json.loads(said(result))["error"] == "The async subagent encountered an error."


async def test_update_starts_the_next_run_with_interrupt_on_the_same_thread() -> None:
    replies = {("POST", f"{BASE}/threads/{THREAD}/runs"): (200, {"run_id": "r-2"})}
    protocol = Protocol(replies)
    async with tasks(protocol, state={THREAD: tracked("success")}) as call:
        result = await call(UPDATE, task_id=THREAD, message="미수금을 찾아 줘")
    assert said(result) == f"Updated async subagent. task_id: {THREAD}"
    assert protocol.body(0) == {**RUN_BODY, "multitask_strategy": "interrupt"}
    task = noted(result)[THREAD]
    assert (task["run_id"], task["status"]) == ("r-2", "running")


async def test_cancel_sends_no_body_and_takes_an_empty_answer() -> None:
    protocol = Protocol({("POST", f"{RUN_PATH}/cancel"): (202, None)})
    async with tasks(protocol, state={THREAD: tracked()}) as call:
        result = await call(CANCEL, task_id=THREAD)
    assert said(result) == f"Cancelled async subagent task: {THREAD}"
    (request,) = protocol.seen
    assert request["query"] == {"wait": "0", "action": "interrupt"}
    assert request["content"] == b"" and "content-type" not in request["headers"]
    assert noted(result)[THREAD]["status"] == "cancelled"


async def test_list_reads_only_the_tasks_that_can_still_change() -> None:
    state = {
        THREAD: tracked("pending"),
        "th-done": tracked("success", "th-done"),
        "th-gone": tracked("cancelled", "th-gone"),
    }
    protocol = Protocol(run_is("success"))
    async with tasks(protocol, state=state) as call:
        result = await call(LIST)
        assert protocol.calls() == [("GET", RUN_PATH)]
        assert said(result).splitlines() == [
            "3 tracked task(s):",
            f"- task_id: {THREAD}  agent: research  status: success",
            "- task_id: th-done  agent: research  status: success",
            "- task_id: th-gone  agent: research  status: cancelled",
        ]
        assert noted(result)[THREAD]["status"] == "success"
        assert said(await call(LIST, status_filter="error")) == "No async subagent tasks tracked."


# -- deepagents 와 다른 곳 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("replies", "code"),
    [
        ({("POST", f"{BASE}/threads"): refusal(401, "unauthorized")}, "unauthorized"),
        (
            {**started(), ("POST", f"{BASE}/threads/{THREAD}/runs"): refusal(429, "rate_limited")},
            "rate_limited",
        ),
    ],
)
async def test_a_refused_start_is_a_tool_error_with_the_platforms_code(replies, code) -> None:
    async with tasks(Protocol(replies)) as call:
        result = await call(START, description="x", subagent_type="research")
    assert f"(code: {code})" in failed(result)


async def test_a_refused_check_update_and_cancel_are_tool_errors_with_the_code() -> None:
    replies = {
        ("GET", RUN_PATH): refusal(404, "not_found"),
        ("POST", f"{BASE}/threads/{THREAD}/runs"): refusal(409, "agent_task_waiting", "waits"),
        ("POST", f"{RUN_PATH}/cancel"): refusal(404, "not_found"),
    }
    async with tasks(Protocol(replies), state={THREAD: tracked()}) as call:
        assert "(code: not_found)" in failed(await call(CHECK, task_id=THREAD))
        waiting = failed(await call(UPDATE, task_id=THREAD, message="m"))
        assert "(code: agent_task_waiting)" in waiting and waiting.endswith("waits")
        assert "(code: not_found)" in failed(await call(CANCEL, task_id=THREAD))


async def test_an_answer_that_cannot_be_read_is_a_tool_error_not_an_empty_result() -> None:
    unread = refusal(503, "agent_task_answer_unread")
    protocol = Protocol({**run_is("success"), ("GET", f"{BASE}/threads/{THREAD}"): unread})
    async with tasks(protocol, state={THREAD: tracked()}) as call:
        assert "(code: agent_task_answer_unread)" in failed(await call(CHECK, task_id=THREAD))

    down = httpx2.ConnectError("down")
    protocol = Protocol({**run_is("success"), ("GET", f"{BASE}/threads/{THREAD}"): down})
    async with tasks(protocol, state={THREAD: tracked()}) as call:
        assert "ConnectError" in failed(await call(CHECK, task_id=THREAD))


@pytest.mark.parametrize(
    ("replies", "tool", "args"),
    [
        (
            {("POST", f"{BASE}/threads"): (200, {})},
            START,
            {"description": "x", "subagent_type": "research"},
        ),
        (
            {**started(), ("POST", f"{BASE}/threads/{THREAD}/runs"): (200, {"status": "pending"})},
            START,
            {"description": "x", "subagent_type": "research"},
        ),
        (
            {("POST", f"{BASE}/threads/{THREAD}/runs"): (200, "not json")},
            UPDATE,
            {"task_id": THREAD, "message": "m"},
        ),
        ({("GET", RUN_PATH): (200, {"run_id": RUN})}, CHECK, {"task_id": THREAD}),
    ],
)
async def test_an_answer_without_its_id_or_status_is_a_tool_error(replies, tool, args) -> None:
    async with tasks(Protocol(replies), state={THREAD: tracked()}) as call:
        assert "알 수 없는 답" in failed(await call(tool, **args))


async def test_the_running_filter_also_lists_pending_tasks() -> None:
    state = {THREAD: tracked("pending"), "th-done": tracked("success", "th-done")}
    protocol = Protocol(run_is("pending"))
    async with tasks(protocol, state=state) as call:
        result = await call(LIST, status_filter="running")
    assert said(result).splitlines() == [
        "1 tracked task(s):",
        f"- task_id: {THREAD}  agent: research  status: pending",
    ]


async def test_a_list_keeps_the_noted_status_of_a_task_it_cannot_read() -> None:
    for reply in (refusal(500, "internal"), (200, {"run_id": RUN}), httpx2.ConnectError("down")):
        protocol = Protocol({("GET", RUN_PATH): reply})
        async with tasks(protocol, state={THREAD: tracked()}) as call:
            result = await call(LIST)
        assert said(result).endswith("status: running")


# -- 앱을 통째로 ----------------------------------------------------------------------


async def test_a_task_started_in_one_turn_is_checked_in_the_next() -> None:
    """맡긴 작업은 세션의 상태에 남는다. 맡기기의 결과는 바깥 내용이 아니고, 다음 턴의 확인이
    읽어 온 답은 바깥 내용이다. 그 뒤의 확인은 읽기라 그대로 돌고 맡기기는 쓰기라 승인을 묻는다."""
    protocol = Protocol({**started(), **run_is("success"), **answer_is("미수금은 셋이다")})
    start = tool_message(START, description="미수금을 찾아 줘", subagent_type="research")
    async with serve(protocol.app()) as base_url:
        doc = task_doc(
            task_source(read=[CHECK, LIST], inside=[START, UPDATE, CANCEL, LIST]), base_url=base_url
        )
        doc["features"] = [AGENT_TOOLS, "tool_safety"]
        doc["memory"] = load_fixture("support-agent.json")["memory"]
        async with safety_app(doc, settings=Settings(instance_token=INSTANCE_TOKEN)) as running:
            running.client.headers.update({"Authorization": f"Bearer {INSTANCE_TOKEN}"})
            first = await running.client.post("/chat", json={"message": start, "user_sub": "alice"})
            assert first.status_code == 200, first.text
            (launched,) = events_named(parse_sse(first.text), "tool_result")
            assert launched["content"] == f"Launched async subagent. task_id: {THREAD}"
            assert "untrusted" not in launched
            sid = first.headers["X-Daegam-Session-Id"]

            check = tool_message(CHECK, task_id=THREAD)
            body = {"message": check, "user_sub": "alice", "session_id": sid}
            second = await running.client.post("/chat", json=body)
            (checked,) = events_named(parse_sse(second.text), "tool_result")
            assert "미수금은 셋이다" in checked["content"] and checked["untrusted"] == "outside"
            again = await running.client.post("/chat", json=body)
            assert len(events_named(parse_sse(again.text), "tool_result")) == 1

            asked = interrupt_of(await send(running, start, session_id=sid))
            assert [r["name"] for r in asked["action_requests"]] == [START]
    assert protocol.calls() == [
        ("POST", f"{BASE}/threads"),
        ("POST", f"{BASE}/threads/{THREAD}/runs"),
        *[("GET", RUN_PATH), ("GET", f"{BASE}/threads/{THREAD}")] * 2,
    ]
