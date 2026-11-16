"""앱을 띄워 턴을 돌리는 테스트들이 함께 쓰는 도우미다. 앱을 띄우는 run_app·chat 은 conftest.py 에
있다.

- 세션과 승인: 세션 id, 승인 결정(decide 는 응답, resume 은 chat 처럼 응답과 SSE), 다시 생성
  (regenerate 도 응답과 SSE), 화면 전사로 접기, 세션 상세의 전사와 체크포인트의 사람 메시지 읽기다.
- 끊긴 턴과 진행 중인 턴: /chat 응답을 직접 다뤄 끊는 것, `{"sleep": n}` 턴, 도구 뒤에 자는
  스텁이다.
- 문서: 장기 기억 문서, 계산 서브에이전트, 가드레일 묶음과 가리기·막기 규칙이다.
- 도구와 스텁 모델: 계산기 도구, 스텁이 도구를 부르게 하는 메시지, 구조화 출력 스텁(JsonModel), 한
  번만 실패하는 스텁(FailOnceModel), 파일 도구를 부르는 스텁(FileToolModel)이다.
- 장기 기억을 켠 앱(memory_app), 인스턴스 토큰과 그 헤더, CORS 출처, 경로 검사가 거절하는 키, 인용
  표본이다.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import ANY

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import PrivateAttr

from runtime import routes
from runtime.auth import Caller
from runtime.long_term import LongTermMemory
from runtime.routes import ChatRequest
from runtime.sessions import SessionIndex
from runtime.stream import snapshot_messages, thread_config
from runtime.stub import (
    NO_TOOL_PREFIX,
    RESULT_PREFIX,
    SUMMARY_MARKER,
    StubChatModel,
    parse_tool_fragment,
)
from runtime.transcript import TurnTranscript, merge_resumed_calls
from runtime.turn_gate import FILE_KEY_MAX_CHARS
from tests.conftest import (
    RunningApp,
    event_names,
    load_fixture,
    parse_sse,
    run_app,
    wait_for,
    with_sse,
)
from tests.store_fixtures import pod_store

ORIGIN = "https://app.example.com"
"""CORS 로 허용하는 브라우저 출처다."""
INSTANCE_TOKEN = "s3cret-instance-token"
"""플랫폼과 파드가 나눠 갖는 인스턴스 토큰이다(`Settings.instance_token`)."""
# 경로 검사가 거절하는 키다. `.`·`..` 조각은 인코딩해야 HTTP 클라이언트가 정규화하지 않는다.
# 마지막 키는 길이 상한을 한 글자 넘는다. 장기 기억(test_memories_api.py)과 세션
# 파일(test_session_files.py)이 같은 키로 본다
BAD_KEYS = [
    "%2E%2E/prefs.md",
    "notes/%2E/prefs.md",
    "notes/%2E%2E",
    "a%00b",
    "가" * (FILE_KEY_MAX_CHARS + 1),
]
CITATIONS_FIXTURE = load_fixture("chat/citations.json")
"""검색 턴의 tool_result·citations 이벤트와 전사 표본이다(P36 7절). UI 의 되살리기 테스트와 msw
목이 같은 파일을 읽는다."""


def status_and_code(response: httpx.Response) -> tuple[int, str]:
    """오류 응답의 상태와 본문의 `code` 다."""
    return response.status_code, response.json()["code"]


def bearer(token: str, **extra: str) -> dict[str, str]:
    """token 을 Bearer 로 싣는 요청 헤더다. extra 는 함께 보낼 헤더다."""
    return {"Authorization": f"Bearer {token}", **extra}


# --- 세션과 승인 ---------------------------------------------------------------------------


def sid_of(sse: list[tuple[str, dict[str, Any]]], ending: str | None = None) -> str:
    """턴을 맺는 마지막 이벤트(done·interrupt·error)가 실은 세션 id 다. ending 을 주면 턴이 그
    이벤트로 끝났는지도 본다."""
    event, data = sse[-1]
    assert ending in (None, event), f"턴이 {ending} 이벤트로 끝나지 않았다: {event_names(sse)}"
    return data["session_id"]


async def decide(
    running: RunningApp,
    sid: str,
    decisions: list[dict[str, Any]],
    *,
    user_sub: str = "alice",
    headers: dict[str, str] | None = None,
    approval_choices: list[dict[str, str]] | None = None,
    run_id: str | None = None,
) -> httpx.Response:
    """승인 결정을 보낸다. 200 이면 본문이 재개 턴의 SSE 다. run_id 는 실행 세션의 결정이 싣는
    그 세션의 실행이다(P41 9절)."""
    body: dict[str, Any] = {"user_sub": user_sub, "decisions": decisions}
    if approval_choices is not None:
        body["approval_choices"] = approval_choices
    if run_id is not None:
        body["run_id"] = run_id
    return await running.client.post(f"/sessions/{sid}/decisions", json=body, headers=headers or {})


async def resume(
    running: RunningApp,
    sid: str,
    decisions: list[dict[str, Any]],
    *,
    user_sub: str = "alice",
    headers: dict[str, str] | None = None,
    approval_choices: list[dict[str, str]] | None = None,
    run_id: str | None = None,
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    """decide 로 재개한 턴의 응답과 SSE 다. chat 처럼 200 이 아니면 SSE 는 빈 목록이다."""
    response = await decide(
        running,
        sid,
        decisions,
        user_sub=user_sub,
        headers=headers,
        approval_choices=approval_choices,
        run_id=run_id,
    )
    return with_sse(response)


async def regenerate(
    running: RunningApp,
    sid: str,
    *,
    message: str | None = None,
    user_sub: str = "alice",
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    """마지막 턴을 다시 생성한 턴의 응답과 SSE 다. message 를 주면 고쳐 보내기다. chat 처럼 200 이
    아니면 SSE 는 빈 목록이다."""
    body: dict[str, Any] = {"user_sub": user_sub}
    if message is not None:
        body["message"] = message
    return with_sse(
        await running.client.post(f"/sessions/{sid}/regenerate", json=body, headers=headers or {})
    )


Turn = tuple[str | None, list[tuple[str, dict[str, Any]]]]


def folded(*turns: Turn) -> list[dict[str, Any]]:
    """SSE 로 받은 것을 화면 항목으로 접은 결과다. 세션에 쌓여 있어야 할 것이기도 하다.

    UI 의 applyChatEvent 와 같은 규칙(`apply_event`)이고, 사용자 말풍선은 보낼 때 화면이 먼저
    붙이므로 턴 앞에 붙인다(재개 턴은 None 이다). 접는 단위가 턴 하나인 것은 런타임이 턴마다
    접어 덧붙이기 때문이다. 앞 턴의 항목을 고치지 않아야 파드 여럿이 서로의 전사를 밟지 않는다.
    승인 뒤 재개 스트림은 같은 tool_call 을 한 번 더 내므로(HITL) 저장된 전사에는 카드가 턴마다
    하나씩 남는다. 읽을 때 `merge_resumed_calls` 가 둘을 합쳐 스트림으로 본 화면처럼 카드 하나가
    된다.

    사용자 항목은 그 시도의 turn_id 를 든다(P79 3절). 턴의 마지막 이벤트(done·error)가 실은 값이고,
    승인 대기로 끝난 턴처럼 그 이벤트가 없으면 어떤 값이든 받는다.
    """
    out: list[dict[str, Any]] = []
    for message, sse in turns:
        turn = TurnTranscript()
        if message is not None:
            ended = [data for event, data in sse if event in ("done", "error")]
            turn.add_user(message, turn_id=ended[-1]["turn_id"] if ended else ANY)
        for event, data in sse:
            turn.add(event, data)
        out.extend(turn.take())
    merge_resumed_calls(out)
    return out


async def transcript_items(running: RunningApp, sid: str) -> list[dict[str, Any]]:
    """세션 상세가 주는 화면 전사의 항목이다."""
    return (await running.client.get(f"/sessions/{sid}")).json()["transcript"]["items"]


async def checkpoint_humans(running: RunningApp, sid: str) -> list[HumanMessage]:
    """체크포인트의 가장 최근 상태에 든 사람 메시지다."""
    snapshot = await running.runtime.agent.graph.aget_state(thread_config(sid))
    return [m for m in snapshot_messages(snapshot) if isinstance(m, HumanMessage)]


# --- 끊긴 턴과 진행 중인 턴 (D65) ----------------------------------------------------------


async def open_chat(
    running: RunningApp, message: str, session_id: str | None = None
) -> tuple[str, Any]:
    """/chat 라우트가 여는 응답과 그 세션 id 다. ASGI 전송은 응답을 다 모아 돌려주므로 끊김은
    응답 객체를 직접 다뤄 만든다. 앱에는 세션이 하나뿐이어야 한다."""
    caller = Caller(sub="alice", external=False, approver=False)
    body = ChatRequest(message=message, user_sub="alice", session_id=session_id)
    response = await routes.chat(body, caller, running.runtime)
    (record,) = await running.runtime.store.index.list()
    return record.session_id, response


async def cut_while_sending(response: Any, event: bytes) -> None:
    """응답을 ASGI 로 돌리다 그 이벤트를 보내는 send 도중에 연결을 끊는다. 느린 클라이언트라 그
    send 는 끊김이 온 뒤에도 돌아오지 않는다."""
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message.get("body", b"").startswith(event):
            disconnected.set()
            await asyncio.sleep(30)

    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)


def read_in_background(response: Any) -> asyncio.Task[list[str]]:
    """응답을 끝까지 읽는 태스크다. 턴은 이것과 상관없이 돈다."""

    async def read() -> list[str]:
        return [chunk async for chunk in response.body_iterator]

    return asyncio.create_task(read())


def last_event(chunks: Sequence[str]) -> tuple[str, str | None]:
    """읽은 조각의 마지막 이벤트 이름과 그 코드다. 오류 이벤트가 아니면 코드는 None 이다."""
    event, data = parse_sse("".join(chunks))[-1]
    return event, data.get("code")


async def turns_ended(running: RunningApp, status: str, count: int = 1) -> bool:
    """그 status 의 turn_end 가 count 개인가. wait_for 의 조건으로 쓴다."""
    return running.ended(status) == count


async def lease_free(index: SessionIndex, sid: str) -> bool:
    """세션의 턴 리스가 풀렸는가. 풀렸으면 그 턴의 마감이 끝났다(리스는 마지막 전사를 붙인 뒤
    풀린다)."""
    record = await index.get(sid)
    return record is not None and not record.running


async def sleeping_turn(running: RunningApp, seconds: int) -> asyncio.Task[httpx.Response]:
    """`{"sleep": n}` 턴을 실제 소켓으로 열고 응답 전부를 모으는 태스크다. 스텁이 자는 동안 턴은
    진행 중인 모델 호출을 하나 든다. 리스가 잡혀 감시가 걸린 뒤에 돌아온다. 감시가 걸리기 전에 턴이
    끝나거나 wait_for 의 기한이 지나면 실패한다."""
    body = {"message": f'{{"sleep": {seconds}}}', "user_sub": "alice"}

    async def read() -> httpx.Response:
        async with running.client.stream("POST", "/chat", json=body) as response:
            await response.aread()
            return response

    task = asyncio.create_task(read())

    async def watched() -> bool:
        if task.done():
            ended = task.result()
            raise AssertionError(f"감시 전에 턴이 끝났다: {ended.status_code} {ended.text}")
        return bool(running.runtime.cancels)

    try:
        await wait_for(watched, what="진행 중인 턴의 중지 감시")
    except BaseException:
        task.cancel()
        raise
    return task


def running_sid(running: RunningApp) -> str:
    """진행 중인 턴 하나의 세션 id 다(sleeping_turn 이 연 턴)."""
    (sid,) = running.runtime.cancels
    return sid


class ToolThenSleepModel(StubChatModel):
    """첫 호출은 도구를 부르고 결과를 받은 둘째 호출에서 `pause` 초(기본 30) 잔다. 멈춘 턴에 끝난
    호출 하나와 진행 중인 호출 하나를 남긴다. 짧게 자면 읽기가 떠난 뒤에도 턴이 끝까지 가는지 본다.
    `sleeping` 은 둘째 호출이 시작됐다는 신호이고 `cancelled` 는 그 호출이 취소로 끝났다는
    표시다."""

    pause: float = 30.0
    _sleeping: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _cancelled: bool = PrivateAttr(default=False)

    @property
    def sleeping(self) -> asyncio.Event:
        return self._sleeping

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        if isinstance(messages[-1], ToolMessage):
            self._sleeping.set()
            try:
                await asyncio.sleep(self.pause)
            except asyncio.CancelledError:
                self._cancelled = True
                raise
        return await super()._agenerate(messages, stop, run_manager, **kw)


# --- 문서 -----------------------------------------------------------------------------------


def long_term_doc(
    *, retention_days: int | None = 30, with_knowledge: bool = True
) -> dict[str, Any]:
    """schema_version "3" 문서다. knowledge fixture 에 memory.long_term 을 더해 만든다(D34).

    계약 fixture 넷은 플랫폼 테스트가 이름까지 고정하므로 파일을 늘리지 않는다.
    with_knowledge 가 거짓이면 knowledge 키 셋을 빈 목록으로 둔다("3" 은 빈 목록도 받는다).
    """
    doc = load_fixture("knowledge-agent.json")
    doc["schema_version"] = "3"
    doc["memory"]["long_term"] = {"retention_days": retention_days}
    if not with_knowledge:
        doc["knowledge"] = []
        doc["main"]["knowledge"] = []
        for sub in doc["subagents"]:
            sub["knowledge"] = []
    return doc


def memory_doc(**kwargs: Any) -> dict[str, Any]:
    """장기 기억을 켠 "3" 문서다. 파일 도구를 켜고 knowledge 항목은 두지 않는다."""
    doc = long_term_doc(with_knowledge=False, **kwargs)
    doc["main"]["builtin_tools"]["filesystem"] = True
    return doc


def subagent_spec(
    doc: dict[str, Any],
    name: str = "calc",
    *,
    tools: list[str] | None = None,
    interrupt_on: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """메인의 모델을 쓰는 서브에이전트 하나다. 도구는 기본이 계산기 소스이고 턴 제한은 30초다."""
    return {
        "name": name,
        "description": "계산을 맡긴다",
        "system_prompt": "계산만 한다",
        "model": copy.deepcopy(doc["main"]["model"]),
        "tools": ["tools-calculator"] if tools is None else tools,
        "limits": {"max_tool_calls_per_turn": 2, "turn_timeout_seconds": 30, "retries": 0},
        "interrupt_on": interrupt_on or {},
    }


def with_subagent(doc: dict[str, Any], name: str = "calc", **spec: Any) -> None:
    """메인이 task 로 위임하는 서브에이전트 하나만 두고 메인에 붙인다(`subagent_spec`). 승인은
    interrupt_on 을 줄 때만 있다."""
    doc["subagents"] = [subagent_spec(doc, name, **spec)]
    doc["main"]["subagents"] = [name]


def guardrail(
    position: str, rules: list[dict[str, Any]], on_violation: str, message: str | None = None
) -> dict[str, Any]:
    """agent.json `guardrails` 의 묶음 하나다."""
    return {"position": position, "rules": rules, "on_violation": on_violation, "message": message}


def mask_rule(regex: str) -> dict[str, Any]:
    """정규식에 맞는 글을 가리는 pii_mask 규칙이다."""
    return {"kind": "pii_mask", "value": {"types": ["regex"], "regex": regex}}


def deny_rule(pattern: str) -> dict[str, Any]:
    """정규식 pattern 에 맞는 글을 거르는 deny_pattern 규칙이다."""
    return {"kind": "deny_pattern", "value": pattern}


def with_input_deny(doc: dict[str, Any], pattern: str, on_violation: str) -> dict[str, Any]:
    """doc 의 사본에 입력의 pattern 을 거르는 묶음을 더한 문서다. 재배포로 규칙을 강화한 판이다."""
    guarded = copy.deepcopy(doc)
    guarded["guardrails"].append(guardrail("input", [deny_rule(pattern)], on_violation))
    return guarded


# --- 도구와 스텁 모델 -----------------------------------------------------------------------


@tool
def Add(a: int, b: int) -> int:
    """두 수를 더한다."""
    return a + b


@tool
def Divide(a: int, b: int) -> float:
    """나눈다."""
    return a / b


TOOLS = {"tools-calculator": [Add, Divide]}
"""계산기 소스를 MCP 서버 없이 컴파일할 때 넘기는 도구다(`compile_agent(tools_by_ref=)`)."""


def tool_message(name: str, **args: Any) -> str:
    """스텁이 name 도구를 args 로 부르게 하는 사용자 메시지다."""
    return json.dumps({"tool": name, "args": args}, ensure_ascii=False)


def _call(name: str, **args: Any) -> str:
    """앞에 말이 붙은 도구 호출 메시지다. 스텁은 글 안의 조각을 찾아 부른다."""
    return "이렇게 해라 " + tool_message(name, **args)


def write_message(path: str, content: str) -> str:
    return _call("write_file", file_path=path, content=content)


def read_message(path: str) -> str:
    return _call("read_file", file_path=path)


STRUCTURED_VALUE = {"answer": "번호 123", "items": ["456", 7]}
"""JsonModel 이 답하는 구조화 결과다. 세 자리 숫자가 있어 가리기 테스트에도 쓴다."""
STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


class JsonModel(StubChatModel):
    """구조화 출력을 아는 스텁이다(profile.structured_output). provider 전략으로 돌아
    STRUCTURED_VALUE 를 JSON 글로 답한다. 응답 형식이 json_schema 인 문서와 함께 쓴다."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("profile", {"max_input_tokens": 200_000, "structured_output": True})
        super().__init__(**kwargs)

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self._text(json.dumps(STRUCTURED_VALUE, ensure_ascii=False), 10)


FAIL_MARK = "실패"


class FailOnceModel(StubChatModel):
    """사람 메시지에 FAIL_MARK 가 든 첫 호출 하나만 실패한다. 같은 질문을 다시 보내면 성공한다.
    문서의 retries 를 0 으로 두어야 그 턴이 오류로 끝난다."""

    _failed: bool = PrivateAttr(default=False)

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        last = messages[-1] if messages else None
        if not self._failed and isinstance(last, HumanMessage) and FAIL_MARK in last.text:
            self._failed = True
            raise RuntimeError("한 번만 실패한다")
        return await super()._agenerate(messages, stop, run_manager, **kw)


class FileToolModel(StubChatModel):
    """사용자 메시지의 `{"tool": ..., "args": {...}}` 조각을 그대로 부르는 스텁이다.

    기본 스텁은 내장 도구를 부르지 않으므로(그것이 D26 의 규칙이다) 파일 도구를 시키려면
    이것이 필요하다. 도구 결과가 오면 그 내용을 담은 텍스트로 턴을 끝낸다.

    모델 이름을 따로 두는 것은 HarnessProfile 등록이 프로세스 안에서 누적(합집합)이라
    같은 키에 파일 도구를 껐다 켤 수 없기 때문이다(compile.register_profile).
    """

    model_name: str = "stub-files"

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        last = messages[-1] if messages else None
        input_chars = sum(len(m.text) for m in messages)
        if isinstance(last, HumanMessage) and SUMMARY_MARKER in last.text:
            return super().reply(messages)
        if isinstance(last, ToolMessage):
            return self._text(RESULT_PREFIX + last.text, input_chars)
        user_text = next((m.text for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        fragment = parse_tool_fragment(user_text)
        if fragment is None:
            return self._text(NO_TOOL_PREFIX + user_text, input_chars)
        name, args = fragment
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "tool_call",
                }
            ],
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


# --- 장기 기억을 켠 앱 ----------------------------------------------------------------------


@asynccontextmanager
async def memory_app(
    memory: LongTermMemory, doc: dict[str, Any] | None = None, **app: Any
) -> AsyncIterator[RunningApp]:
    """장기 기억을 켠 앱이다. doc 이 없으면 memory_doc 이고 모델은 따로 주지 않으면 FileToolModel
    이다. app 은 run_app 에 그대로 넘긴다. 문서의 memory 노드대로 열면 실제 저장소가 있어야 하므로
    세션 저장소는 memory 노드 없는 파드 안 저장소(pod_store)를 따로 넘긴다."""
    app.setdefault("model", FileToolModel())
    async with (
        pod_store(load_fixture("minimal.json")) as store,
        run_app(doc or memory_doc(), store=store, long_term=memory, **app) as running,
    ):
        yield running
