"""턴 루프 (D31, P14 F1b). `langchain-core` 의 `bind_tools`·`astream`·`AIMessage.tool_calls` 만 쓰는
평범한 에이전트 루프다. deepagents·langgraph 를 플랫폼에 들이지 않는다.

한 턴은 모델을 부르고 tool_calls 를 처리하기를 되풀이한다. `astream` 의 청크를 `+` 로 합쳐 마지막
메시지를 만들고 `token` 이벤트에는 `chunk.text` 만 싣는다(Anthropic thinking 블록은 토큰으로 내지
않는다).
한 AIMessage 의 tool_calls 가운데 승인이 필요 없는 것은 바로 실행해 ToolMessage 를 붙이고, 승인이
필요한 것은 세션 `pending.calls[]` 에 두고 `interrupt` 를 낸 뒤 턴을 닫는다. `ask_user` 도 tool_call
이라 실행하지 않고 `interrupt{kind: question}` 을 내며, 사용자의 다음 메시지가 그 id 의 ToolMessage
가 된다. 같은 AIMessage 에 승인 대상이 섞이면 그것들은 `deferred` ToolMessage 로 닫는다. 그래서
다음 모델 호출 전에는 그 AIMessage 의 모든 tool_call id 에 ToolMessage 가 있고(provider 계약),
`pending.kind` 는 항상 하나다.

승인 대상은 등급이 정하되 가져온 데이터가 얹히면 한 단계 올라간다. 인자에 `$file:` 참조가 하나라도
있으면 read 가 아닌 등급은 `confirm_deploy` 와 무관하게 승인 뒤이고(D31), 그 호출의
`interrupt.action_requests[].files` 에 참조한 파일 이름과 출처 URL 을 실어 카드가 무엇을 심는지
보이게 한다.

Agent 정의의 제안 도구(`create_agent`·`update_agent`, P48 6.1절)는 늘 사람의 결정을 기다리고 홀로
기다린다. 한 AIMessage 에 제안이 있으면 같은 메시지의 다른 승인 대상은 두 번째 제안까지 `deferred`
로 닫는다. 제안은 기다리기 전에 검사를 거친다(`proposals.py`). 넘지 못하는 선에 걸리면 tool_call 과
그 결과로 보이고 모델이 이어서 고친다. 지나면 그 호출의 저장된 인자에 예약 칸(`PROPOSAL_KEY`)을
넣고 `interrupt.action_requests[0].proposal` 에 카드의 덩어리를 싣는다. 모델이 보낸 예약 칸은
처음에 뺀다. 패치할 정의의 읽기(`get_agent_definition`)는 4KB 로 자르지 않고 모델에 주되
화면과 전사에는 자른 사본을 준다. 제안 인자도 화면과 전사의 `tool_call` 에는 4KB 로 자른 사본이
가고 카드는 그 덩어리가 그린다. 사람의 두 메시지 사이의 제안과 그 읽기에는 한도가 있다(6.6절).

한도는 `max_tool_calls_per_turn`·`turn_timeout_seconds` 이고 모델 재시도는 2회(연결·429·5xx, 1초부터
2배 지수 백오프, 30초 상한)다. 크레딧 소진은 429 여도 다시 시도하지 않고 quota_exhausted 다(T20).
청크를 하나라도 받은 뒤의 실패는 재시도하지 않는다. 이미 흘린 `token` 이 다시 흘러 화면에 겹치기
때문이다. 넘으면 `error{code: tool_limit|turn_timeout|turn_failed|quota_exhausted}` 로 닫는다. 도구
결과의 401 은 되먹이지 않고 `error{unauthorized}` 로 턴을 닫되, 그 AIMessage 의 남은 tool_call 에는
ToolMessage 를 붙여 다음 턴의 문맥이 provider 계약을 지키게 한다.

비밀은 이벤트·전사·문맥·pending 에 남기지 않는다. 표의 `secret_fields` pointer 자리는 `tool_call`
이벤트, 전사, 저장하는 AIMessage(tool_calls·tool_use 블록·additional_kwargs),
`pending.calls[].args`, `interrupt.action_requests[].args` 에 넣기 전에 `***` 로 바꾼다. 바로
실행하는 호출은 실행기가 원본 인자로 판정한다(`secret_in_chat`). 승인을 거친 호출은 문서에 남은
마스킹 값을 받는데, `***` 도 값이 있는 것이라 실행기가 같은 이유로 거부한다.

문맥은 HumanMessage 앞에서만 자른다. 최근 HumanMessage 12개 구간을 남기고 앞 구간은 모델에 한 번
보내 한 문단 요약을 만들어 SystemMessage 뒤 첫 HumanMessage 로 넣는다. 요약 호출도 usage 에 센다.

도구는 묶음으로 싣는다(D57). 모델을 부를 때마다 기본 묶음과 세션에 열린 묶음의 도구만 넘긴다.
모델이 `use_tools` 로 묶음을 열면 다음 호출부터 그 도구가 보인다. `use_tools` 는 실행기로 가지 않고
화면 이벤트도 내지 않는다. 모델이 열지 않은 묶음의 도구를 이름으로 부르면 그 묶음을 열고 그대로
실행한다. 연 묶음은 세션 문서에 남아 다음 턴과 승인 재개로 이어진다.

루프는 상태(메시지·전사·파일·usage·pending)를 들고만 있고 저장은 하지 않는다. 서비스가 이벤트
싱크·리스 갱신·체크포인트를 콜백으로 넣고, 턴이 끝나면 상태를 세션 문서에 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langchain_core.runnables import Runnable

from daegam import llm_http
from daegam.services.assistant.catalog import CORE_GROUP, OTHER_GROUP, TOOL_GROUPS, group_of
from daegam.services.assistant.model import prompt_cache
from daegam.services.assistant.prompt import SUMMARY_MARKER, SUMMARY_SYSTEM, system_content
from daegam.services.assistant.proposals import (
    CANVAS_SAVED,
    CARD_REJECTED,
    DEFINITION_TOOL,
    PROPOSAL_KEY,
    Proposals,
    SpanUsage,
    span_usage,
    without_proposal,
)
from daegam.services.assistant.schema import ToolDef
from daegam.services.assistant.tools import (
    SECRET_MASK,
    ToolExecutor,
    ToolResult,
    TurnContext,
    dumps,
    error_result,
    file_refs,
    mask_secrets,
    truncate,
)
from daegam.services.llm_providers import retryable_status
from daegam.services.usage.ledger import CallMeter, unmetered
from daegam.store.assistant_sessions import (
    TRANSCRIPT_LIMIT,
    FetchedFile,
    Pending,
    PendingCall,
    Usage,
)

logger = logging.getLogger("daegam.assistant")

KEEP_HUMANS = 12
SUMMARY_FLAG = "daegam_summary"
SUMMARY_PREFIX = "[이전 대화 요약]\n"
ALLOWED_DECISIONS = ["approve", "reject"]

# 못 실행한 호출을 닫을 때 쓰는 코드와 문장. 상황마다 코드가 다르다.
DEFERRED = "deferred"
DEFERRED_DETAIL = "turn ended before this call was executed"
"""턴이 먼저 끝나서 시작조차 못 한 호출. 실행 중에 끊긴 호출은 서비스가 `turn_failed` 와
"turn ended before this call finished" 로 닫으므로(`service.py` 의 `_run_task`) 상황이 다르다."""
ASK_FIRST_DETAIL = "ask the user first"
PROPOSAL_FIRST_DETAIL = "the person decides the proposal first; repeat this call after the decision"
SUPERSEDED_DETAIL = "superseded: the user sent a new message before deciding"
TOOL_LIMIT_DETAIL = "tool call limit reached"

# 도구 묶음을 여는 도구 (D57). 루프가 처리하고 실행기로 보내지 않는다
USE_TOOLS = "use_tools"

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
Renew = Callable[[], Awaitable[bool]]
Checkpoint = Callable[[], Awaitable[None]]
Masker = Callable[[str, dict[str, Any]], dict[str, Any]]
OutcomeKind = Literal["done", "interrupt", "error"]


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 24
    timeout: float = 300.0
    retries: int = 2
    backoff: float = 1.0
    backoff_cap: float = 30.0


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    code: str | None = None


@dataclass
class Decision:
    id: str
    type: Literal["approve", "reject"]
    message: str | None = None
    # 제안 호출의 결정이 되돌려 보내는 지문 (P48 6.1절). 서비스가 기다리는 호출의 것과 견준다
    proposal_digest: str | None = None
    # 사람이 캔버스에서 제 판을 저장한 뒤 그 제안 호출을 닫는 거절이다 (P48 6.5절). 카드 거절로
    # 세지 않는다
    canvas_saved: bool = False


class ModelFailed(Exception):
    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


# -- 순수 도우미 --------------------------------------------------------------------------------


def needs_confirm(
    risk: str, confirm_deploy: bool, *, uses_fetched: bool = False, proposal: bool = False
) -> bool:
    """deploy 는 설정에 따라, destructive 는 항상 승인 뒤다. 제안 도구는 등급과 설정에 상관없이 늘
    승인 뒤다(P48 6.1절).

    가져온 파일(`$file:` 참조)이 인자에 들어 있으면 read 가 아닌 등급은 `confirm_deploy` 와
    무관하게 승인 뒤다(D31). 남의 문서를 플랫폼에 심는 일이라 사용자가 한 번 보는 것이 맞고,
    `confirm_deploy=false` 는 "우리가 만든 인자로 하는 배포는 묻지 말라" 는 뜻이지 "가져온
    데이터로 하는 쓰기도 묻지 말라" 는 뜻이 아니다. read 등급(`verify_spec`)은 부작용이 없어
    그대로 둔다."""
    if proposal or (uses_fetched and risk != "read"):
        return True
    return risk == "destructive" or (risk == "deploy" and confirm_deploy)


def approval_summary(description: str) -> str:
    """승인 카드에 보일 도구 설명이다. 첫 문장만 남긴다.

    뒤 문장은 모델에게 주는 규칙("exposure 는 요청했을 때만 넣는다" 같은 것)이다.
    결정하는 사람에게는 군더더기다.
    """
    head, sep, _ = description.partition(". ")
    return f"{head}." if sep else description


def retryable(e: BaseException, *, gateway: bool = False) -> bool:
    """연결 오류와 429·5xx 만 다시 시도한다. 크레딧 소진(T20)과 게이트웨이 경로(gateway)에서
    게이트웨이가 낸 한도·토큰 거절(P38 7절)은 429 여도 다시 시도하지 않는다.

    상태 규칙은 스펙 생성기와 같은 것을 쓴다(`llm_providers.retryable_status`). langchain 예외는
    SDK 예외를 상속하므로 status_code 를 그대로 본다. 상태가 없는 예외는 연결 실패로 보는데,
    타입 검사 뒤에 클래스 이름 검사가 한 겹 더 있는 것은 SDK 가 httpx 예외를 자기 클래스로
    감싸 올려 `httpx.TransportError` 검사에 걸리지 않는 경우가 있기 때문이다."""
    if llm_http.not_retried(e, gateway=gateway):
        return False
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return retryable_status(status)
    if isinstance(e, httpx.TransportError | TimeoutError | ConnectionError):
        return True
    name = type(e).__name__
    return "Connection" in name or "Timeout" in name


def is_summary(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage) and bool(message.additional_kwargs.get(SUMMARY_FLAG))


def tool_message(call_id: str, name: str, result: ToolResult) -> ToolMessage:
    return ToolMessage(content=result.text, tool_call_id=call_id, name=name, status=result.status)


def masked_ai_message(ai: AIMessage, mask: Masker) -> AIMessage:
    """저장·재전송할 AIMessage 의 도구 인자를 마스킹한 복사본. tool_calls, Anthropic 의 tool_use
    블록 `input`, OpenAI 의 additional_kwargs.tool_calls[].function.arguments 세 자리를 본다."""
    if not ai.tool_calls and not ai.invalid_tool_calls and not isinstance(ai.content, list):
        return ai
    tool_calls = [{**c, "args": mask(c["name"], dict(c["args"] or {}))} for c in ai.tool_calls]
    # 깨진 인자는 원문과 파서 오류 메시지(원문을 품는다) 둘 다 통째로 가린다. 원문이 깨져 있어
    # 비밀 자리만 골라낼 수 없기 때문이다
    invalid = [
        {
            **c,
            "args": SECRET_MASK if c.get("args") else c.get("args"),
            "error": SECRET_MASK if c.get("error") else c.get("error"),
        }
        for c in ai.invalid_tool_calls
    ]
    content: Any = ai.content
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block = _mask_tool_use_block(block, mask)
            blocks.append(block)
        content = blocks
    extra = dict(ai.additional_kwargs)
    raw_calls = extra.get("tool_calls")
    if isinstance(raw_calls, list):
        masked_raw = []
        for raw in raw_calls:
            fn = raw.get("function") if isinstance(raw, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str) and fn["arguments"]:
                # 깨진 원문은 invalid_tool_calls 처럼 통째로 가린다
                parsed = _parse_object(fn["arguments"])
                arguments = (
                    dumps(mask(str(fn.get("name") or ""), parsed))
                    if parsed is not None
                    else SECRET_MASK
                )
                raw = {**raw, "function": {**fn, "arguments": arguments}}
            masked_raw.append(raw)
        extra["tool_calls"] = masked_raw
    return ai.model_copy(
        update={
            "tool_calls": tool_calls,
            "invalid_tool_calls": invalid,
            "content": content,
            "additional_kwargs": extra,
        }
    )


def _parse_object(text: str) -> dict[str, Any] | None:
    """인자 JSON 문자열을 객체로 푼다. 깨졌거나 객체가 아니면 None 이다."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mask_tool_use_block(block: dict[str, Any], mask: Masker) -> dict[str, Any]:
    """Anthropic tool_use 블록. 스트리밍으로 합친 블록은 `input` 이 비고 인자 전체가
    `partial_json` 문자열에 있다. 파싱해 마스킹한 뒤 `input` 에 넣고 `partial_json` 은 지운다.
    못 풀면 통째로 가린다."""
    name = str(block.get("name") or "")
    out = dict(block)
    partial = out.pop("partial_json", None)
    if isinstance(out.get("input"), dict) and out["input"]:
        out["input"] = mask(name, out["input"])
    if isinstance(partial, str) and partial:
        parsed = _parse_object(partial)
        out["input"] = mask(name, parsed) if parsed is not None else {"_": SECRET_MASK}
    return out


def apply_event(items: list[dict[str, Any]], name: str, data: dict[str, Any]) -> None:
    """SSE 이벤트 하나를 화면용 ChatItem 목록에 접는다(제자리).

    비서가 내는 이벤트(token·tool_call·tool_result·interrupt·resource·done·error)는 UI 의
    applyChatEvent 와 같은 규칙으로 접는다. tool_result 는 결과가 아직 없는 같은 id 의 호출
    가운데 앞의 것에 붙는다(UI 의 findIndex 와 같은 차례). 비서는 같은 id 의 tool_call 을 두 번
    내지 않으므로 UI 의 같은 id 합치기는 두지 않는다. `ui` 는 순간적인 것이라 전사에 남지 않는다.
    에이전트 런타임의 done·error 가 싣는 `turn_id`, 도구 카드에 붙는 `citations`,
    `model_fallback` 을 접은 `fallback` 항목(P36)은 비서 루프가 내지 않으므로 여기서 다루지
    않는다. 항목이 `TRANSCRIPT_LIMIT` 를 넘으면 앞을 버린다."""
    last = items[-1] if items else None
    bubble = last if last and last.get("kind") == "assistant" and last.get("streaming") else None
    if name == "token" and bubble is not None:
        bubble["text"] += data["text"]
        return
    if bubble is not None:
        bubble["streaming"] = False
    if name == "token":
        items.append({"kind": "assistant", "text": data["text"], "streaming": True})
    elif name == "tool_call":
        items.append({"kind": "tool_call", "call": dict(data), "result": None})
    elif name == "tool_result":
        call = next(
            (
                it
                for it in items
                if it.get("kind") == "tool_call"
                and it["call"]["id"] == data["id"]
                and not it["result"]
            ),
            None,
        )
        if call is not None:
            call["result"] = dict(data)
        else:
            items.append(
                {
                    "kind": "tool_call",
                    "call": {"id": data["id"], "name": data["name"], "args": {}},
                    "result": dict(data),
                }
            )
    elif name == "interrupt":
        items.append({"kind": "interrupt", "pending": dict(data), "resolved": False})
    elif name == "resource":
        items.append({"kind": "resource", "resource": dict(data)})
    elif name == "done":
        items.append(
            {
                "kind": "done",
                "usage": dict(data["usage"]),
                "turn": data["turn"],
                "tool_calls": data["tool_calls"],
            }
        )
    elif name == "error":
        items.append({"kind": "error", "code": data["code"], "message": data["message"]})
    if len(items) > TRANSCRIPT_LIMIT:
        del items[: len(items) - TRANSCRIPT_LIMIT]


def resolve_interrupts(items: list[dict[str, Any]]) -> None:
    for it in items:
        if it.get("kind") == "interrupt":
            it["resolved"] = True


def render_for_summary(messages: Sequence[BaseMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"user: {m.text}")
        elif isinstance(m, AIMessage):
            calls = ", ".join(f"{c['name']}({dumps(c['args'])})" for c in m.tool_calls)
            lines.append(f"assistant: {m.text}" + (f" [tool calls: {calls}]" if calls else ""))
        elif isinstance(m, ToolMessage):
            lines.append(f"tool {m.name}: {m.text[:500]}")
    return "\n".join(lines)


# -- 루프 -----------------------------------------------------------------------------------


@dataclass
class TurnLoop:
    """세션 하나의 턴 루프. 상태는 서비스가 세션 문서에서 읽어 넣고 끝나면 다시 쓴다."""

    model: BaseChatModel
    tool_defs: list[ToolDef]
    executor: ToolExecutor
    emit: EventSink
    renew: Renew
    checkpoint: Checkpoint
    limits: Limits = field(default_factory=Limits)
    confirm_deploy: bool = True
    # 상태
    messages: list[BaseMessage] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, FetchedFile] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    pending: Pending | None = None
    ui_context: dict[str, Any] | None = None
    # 세션에 열린 도구 묶음 (D57). 기본 묶음은 늘 열려 있어 여기에 없다
    tool_groups: set[str] = field(default_factory=set)
    # 테스트가 백오프 대기를 바꾸는 자리
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    # 모델 호출 한 번의 원장 계량 (P37 7절). 서비스가 `UsageLedger.call` 에 이 턴의 자리를
    # 묶어 넣는다
    meter: Callable[[], AbstractAsyncContextManager[CallMeter]] = unmetered
    # 모델 호출이 LLM 게이트웨이 경로인가 (P38 6절). 턴 오류가 게이트웨이의 세 코드를 가른다
    gateway: bool = False
    # 이번 턴의 집계. 결정 재개는 같은 턴이므로 서비스가 세션 문서의 값을 넣어 이어 센다
    turn_usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0
    _defs: dict[str, ToolDef] = field(init=False, repr=False)
    _groups: dict[str, str] = field(init=False, repr=False)
    # Claude 호출의 캐시 표지 (T80). 다른 provider 는 None 이고 아무것도 싣지 않는다
    _cache: dict[str, str] | None = field(init=False, repr=False)
    proposals: Proposals = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._defs = {d.name: d for d in self.tool_defs}
        self._groups = {d.name: group_of(d.spec) for d in self.tool_defs}
        self._cache = prompt_cache(self.model)
        self.proposals = Proposals(self.executor)

    # -- 도구 묶음 (D57) ---------------------------------------------------------------

    def active_defs(self) -> list[ToolDef]:
        """이번 호출에 실을 도구. 표의 순서를 지켜 같은 묶음이면 프롬프트 앞부분이 같다(캐시)."""
        open_groups = {CORE_GROUP, OTHER_GROUP, *self.tool_groups}
        return [d for d in self.tool_defs if self._groups[d.name] in open_groups]

    def _use_tools(self, call: dict[str, Any]) -> None:
        """`use_tools` 하나를 처리한다. 묶음을 열고 ToolMessage 만 남긴다(화면 이벤트 없음)."""
        raw = (call.get("args") or {}).get("groups")
        wanted = [g for g in raw if isinstance(g, str)] if isinstance(raw, list) else []
        unknown = [g for g in wanted if g not in TOOL_GROUPS]
        if not wanted or unknown:
            result = error_result(
                "unknown_group",
                f"groups must be a non-empty list of {', '.join(TOOL_GROUPS)}; got {raw!r}",
            )
        else:
            before = {d.name for d in self.active_defs()}
            self.tool_groups.update(wanted)
            added = [d.name for d in self.active_defs() if d.name not in before]
            opened = {"opened": wanted, "open_groups": sorted(self.tool_groups), "tools": added}
            result = ToolResult("success", opened)
        self.messages.append(tool_message(call["id"], call["name"], result))

    def _open_groups_of(self, calls: Sequence[dict[str, Any]]) -> None:
        """열지 않은 묶음의 도구를 이름으로 부른 호출이 있으면 그 묶음을 연다."""
        for call in calls:
            group = self._groups.get(call["name"])
            if group is not None and group not in (CORE_GROUP, OTHER_GROUP):
                self.tool_groups.add(group)

    # -- 못 실행한 호출 닫기 -----------------------------------------------------------

    def _close_quietly(self, call_id: str, name: str, code: str, detail: str) -> None:
        """실행하지 않은 호출 하나를 문맥에서만 닫는다.

        한 AIMessage 의 모든 tool_call id 에 ToolMessage 가 있어야 한다는 provider 계약을 지키는
        자리다. 하나라도 빠지면 다음 모델 호출이 400 으로 죽는다. 화면 이벤트를 내지 않는 이유는
        둘 중 하나다. 사용자가 그 호출 카드를 아직 보지 못했거나(승인 대기에 들어가지도 못한
        호출), 취소·타임아웃 경로라 await 할 수 없거나다."""
        self.messages.append(tool_message(call_id, name, error_result(code, detail)))

    async def _close_call(
        self, call_id: str, name: str, args: dict[str, Any], result: ToolResult
    ) -> None:
        """실행하지 않은 호출 하나를 그 결과로 화면에도 알리며 닫는다(미뤄진 호출, 거절, 걸린 제안).

        사용자가 이미 보았거나 이제 보아야 하는 호출이라 `tool_call`·`tool_result` 이벤트를 함께
        내 카드와 답이 짝을 이룬다. `_emit_result` 가 결과를 먼저 자르므로 화면과 문맥이 같은
        본문을 본다."""
        await self._emit_call(call_id, name, args)
        await self._emit_result(call_id, name, result)
        self.messages.append(tool_message(call_id, name, result))

    # -- 진입 ---------------------------------------------------------------------------

    async def run_message(
        self, ctx: TurnContext, message: str, ui_context: dict[str, Any] | None = None
    ) -> Outcome:
        """새 사용자 메시지로 턴을 돈다. 승인 대기가 있으면 superseded 로 닫고, 질문 대기면 답이
        된다."""
        self.ui_context = ui_context
        if self.pending is not None and self.pending.kind == "question":
            (call,) = self.pending.calls
            self.pending = None
            resolve_interrupts(self.transcript)
            self.transcript.append({"kind": "user", "text": message})
            self.messages.append(
                ToolMessage(
                    content=dumps({"answer": message}), tool_call_id=call.id, name=call.name
                )
            )
            return await self._run(ctx)
        if self.pending is not None:
            calls = self.pending.calls
            self.pending = None
            resolve_interrupts(self.transcript)
            for call in calls:
                superseded = error_result("rejected", SUPERSEDED_DETAIL)
                await self._close_call(call.id, call.name, call.args, superseded)
        self.transcript.append({"kind": "user", "text": message})
        self.messages.append(HumanMessage(content=message))
        return await self._run(ctx)

    async def run_decisions(self, ctx: TurnContext, decisions: list[Decision]) -> Outcome:
        """승인 대기를 결정으로 닫고 루프를 잇는다. 개수·id 검사는 서비스가 먼저 했다."""
        assert self.pending is not None and self.pending.kind == "confirm"
        calls = {c.id: c for c in self.pending.calls}
        self.pending = None
        resolve_interrupts(self.transcript)
        stop: Outcome | None = None
        for decision in decisions:
            call = calls[decision.id]
            if stop is not None:
                self._close_quietly(call.id, call.name, DEFERRED, DEFERRED_DETAIL)
                continue
            if decision.type == "approve":
                await self._emit_call(call.id, call.name, call.args)
                stop = await self._execute(ctx, call.id, call.name, call.args)
            else:
                # 거절 주체가 사용자임을 detail 에 남긴다. 실측에서 모델이 "플랫폼이 거부했다" 고
                # 말했다. `card` 칸으로 한 구간의 카드 거절 수를 센다(P48 6.6절). 사람이 캔버스에서
                # 제 판을 저장해 닫은 호출은 카드 거절이 아니라서 제 detail 을 싣고 세지
                # 않는다(6.5절)
                detail = CANVAS_SAVED if decision.canvas_saved else CARD_REJECTED
                if decision.message:
                    detail = f"{decision.message} ({detail})"
                if decision.canvas_saved:
                    rejected = error_result("rejected", detail)
                else:
                    rejected = error_result("rejected", detail, card=True)
                await self._close_call(call.id, call.name, call.args, rejected)
        if stop is not None:
            return stop
        return await self._run(ctx)

    # -- 루프 본체 ------------------------------------------------------------------------

    async def _run(self, ctx: TurnContext) -> Outcome:
        try:
            async with asyncio.timeout(self.limits.timeout):
                return await self._loop(ctx)
        except TimeoutError:
            detail = f"turn exceeded {self.limits.timeout:g}s"
            self.close_open_calls("turn_timeout", detail)
            return await self._fail(ctx, "turn_timeout", detail)
        except ModelFailed as e:
            logger.warning("assistant turn %s failed: %s", ctx.session_id, e)
            # 원인 문장은 provider 응답·파일 경로·키 조각을 담을 수 있어 로그에만 남긴다.
            # 화면에는 예외의 종류만 준다. 크레딧 소진은 결제·한도를 보라고 안내하도록 가른다(T20)
            quota = llm_http.quota_exhausted(e.cause)
            gateway = llm_http.gateway_error(e.cause, gateway=self.gateway)
            code = "quota_exhausted" if quota else gateway or "turn_failed"
            return await self._fail(
                ctx,
                code,
                f"model call failed ({type(e.cause).__name__}); details are in the server log",
            )

    def close_open_calls(self, code: str, detail: str) -> int:
        """마지막 AIMessage 의 tool_call 가운데 ToolMessage 가 없는 것을 error 로 닫는다. 타임아웃·
        취소로 턴이 중간에 끊겨도 다음 턴의 문맥이 provider 계약을 지키게 한다. 승인 대기 중인 id 는
        결정이 닫으므로 건드리지 않는다. 닫은 개수를 돌려준다."""
        index = next(
            (
                i
                for i in range(len(self.messages) - 1, -1, -1)
                if isinstance(self.messages[i], AIMessage)
            ),
            None,
        )
        if index is None:
            return 0
        ai = self.messages[index]
        answered = {
            m.tool_call_id for m in self.messages[index + 1 :] if isinstance(m, ToolMessage)
        }
        waiting = {c.id for c in self.pending.calls} if self.pending is not None else set()
        closed = 0
        for call in ai.tool_calls:
            if call["id"] in answered or call["id"] in waiting:
                continue
            self._close_quietly(call["id"], call["name"], code, detail)
            closed += 1
        return closed

    async def _loop(self, ctx: TurnContext) -> Outcome:
        bound: Runnable[Any, Any] | None = None
        bound_names: tuple[str, ...] = ()
        while True:
            await self._trim()
            # 묶음이 열리면 다음 호출부터 그 도구를 싣는다. 같으면 다시 묶지 않는다
            defs = self.active_defs()
            names = tuple(d.name for d in defs)
            if bound is None or names != bound_names:
                # Claude 에는 요청의 cache_control 도 싣는다. 표지가 대화의 마지막 블록에 붙어
                # 호출마다 앞으로 옮겨 가므로 다음 호출이 앞선 대화를 캐시에서 읽는다 (T80)
                cache = {"cache_control": self._cache} if self._cache else {}
                try:
                    bound = self.model.bind_tools([d.to_dict() for d in defs], **cache)
                except NotImplementedError:
                    return await self._fail(
                        ctx, "provider_no_tools", "this model cannot call tools"
                    )
                bound_names = names
            ai = await self._call_model(bound)
            # 실행은 원본 인자로, 저장은 마스킹한 사본으로
            self.messages.append(masked_ai_message(ai, self._mask))
            if not ai.tool_calls and not ai.invalid_tool_calls:
                return await self._done(ctx, ai.text)
            outcome = await self._handle_tool_calls(ctx, ai)
            if outcome is not None:
                return outcome

    async def _call_model(self, bound: Runnable[Any, Any]) -> AIMessage:
        attempt = 0
        while True:
            received = [0]
            try:
                return await self._stream(bound, received)
            except Exception as e:  # noqa: BLE001 - provider 예외 종류는 SDK 마다 다르다
                again = retryable(e, gateway=self.gateway)
                if received[0] or not again or attempt >= self.limits.retries:
                    raise ModelFailed(e) from e
                delay = min(self.limits.backoff * (2**attempt), self.limits.backoff_cap)
                attempt += 1
                logger.info("assistant model retry %d in %.1fs: %s", attempt, delay, e)
                await self.sleep(delay)

    async def _stream(self, bound: Runnable[Any, Any], received: list[int]) -> AIMessage:
        """청크를 합쳐 AIMessage 하나로. received[0] 은 받은 청크 수이고 호출자가 재시도 가부에
        쓴다."""
        final: Any = None
        prompt: list[BaseMessage] = [
            SystemMessage(content=system_content(self.ui_context, self._cache))
        ]
        sent = prompt + self.messages
        async with self.meter() as meter:
            async for chunk in bound.astream(sent):
                received[0] += 1
                # 텍스트 블록만 싣는다. thinking 블록은 `.text` 에 들지 않는다
                text = chunk.text
                if text:
                    await self._emit("token", {"text": text})
                final = chunk if final is None else final + chunk
            if final is None:
                raise RuntimeError("model returned an empty stream")
            ai = message_chunk_to_message(final) if isinstance(final, AIMessageChunk) else final
            if not isinstance(ai, AIMessage):
                raise RuntimeError(f"model returned {type(ai).__name__}, not AIMessage")
            spent = _measure(meter, sent, ai)
        self._count(spent)
        return ai

    async def _handle_tool_calls(self, ctx: TurnContext, ai: AIMessage) -> Outcome | None:
        # 예약 칸은 루프만 넣는다. 모델이 보낸 것은 실행 전에 뺀다 (P48 6.1절)
        calls = [{**c, "args": without_proposal(c.get("args"))} for c in ai.tool_calls]
        invalid = list(ai.invalid_tool_calls)
        if self.tool_calls + len(calls) + len(invalid) > self.limits.max_tool_calls:
            for call in calls:
                self._close_quietly(call["id"], call["name"], "tool_limit", TOOL_LIMIT_DETAIL)
            for inv in invalid:
                self._close_invalid(inv)
            return await self._fail(
                ctx, "tool_limit", f"more than {self.limits.max_tool_calls} tool calls in one turn"
            )
        for inv in invalid:
            self._close_invalid(inv)
        for call in [c for c in calls if c["name"] == USE_TOOLS]:
            self._use_tools(call)
        calls = [c for c in calls if c["name"] != USE_TOOLS]
        self._open_groups_of(calls)
        if not calls:
            # 묶음만 열었다. 다음 호출이 새 도구를 보고 이어 간다
            return None
        asks = [c for c in calls if c["name"] == "ask_user"]
        confirm = [c for c in calls if c not in asks and self._needs_confirm(c["name"], c["args"])]
        immediate = [c for c in calls if c not in asks and c not in confirm]
        for i, call in enumerate(immediate):
            await self._emit_call(call["id"], call["name"], call["args"])
            stop = await self._execute(ctx, call["id"], call["name"], call["args"])
            if stop is not None:
                for rest in immediate[i + 1 :] + asks + confirm:
                    self._close_quietly(rest["id"], rest["name"], DEFERRED, DEFERRED_DETAIL)
                return stop
        if asks:
            question = asks[0]
            for extra in asks[1:] + confirm:
                ask_first = error_result(DEFERRED, ASK_FIRST_DETAIL)
                await self._close_call(extra["id"], extra["name"], extra["args"], ask_first)
            self.pending = Pending(kind="question", calls=[self._pending_call(question)])
            return await self._interrupt(ctx)
        proposals = [c for c in confirm if self._is_proposal(c["name"])]
        if proposals:
            # 제안은 홀로 기다린다. 카드와 캔버스가 늘 호출 하나를 결정하게 한다 (P48 6.1절)
            first = proposals[0]
            for extra in confirm:
                if extra is not first:
                    later = error_result(DEFERRED, PROPOSAL_FIRST_DETAIL)
                    await self._close_call(extra["id"], extra["name"], extra["args"], later)
            return await self._propose(ctx, first)
        if confirm:
            self.pending = Pending(kind="confirm", calls=[self._pending_call(c) for c in confirm])
            return await self._interrupt(ctx)
        return None

    async def _propose(self, ctx: TurnContext, call: dict[str, Any]) -> Outcome | None:
        """제안 하나를 검사하고 지나면 기다리게 한다. 걸리면 그 결과를 보이고 모델이 이어 간다.
        검사도 플랫폼을 부르므로 턴의 도구 호출 수에 든다."""
        call_id, name, args = call["id"], call["name"], call["args"]
        if not await self.renew():
            await self._emit_call(call_id, name, args)
            return await self._lease_lost(ctx, call_id, name)
        self.tool_calls += 1
        checked = await self.proposals.check(ctx, self.tool_calls, name, args, self._span())
        if checked.refusal is not None:
            await self._close_call(call_id, name, args, checked.refusal)
            if checked.refusal.http_status == 401:
                return await self._unauthorized(ctx)
            return None
        pending = self._pending_call(call)
        pending.args[PROPOSAL_KEY] = checked.reserved
        self.pending = Pending(kind="confirm", calls=[pending])
        return await self._interrupt(ctx, proposal=checked.block)

    def _close_invalid(self, inv: dict[str, Any]) -> None:
        """인자 JSON 이 깨진 tool_call 도 id 가 있으면 답이 있어야 한다. id 가 없으면 provider 도
        답을 기다리지 않는다."""
        call_id = inv.get("id")
        if not call_id:
            return
        name = inv.get("name") or "unknown"
        detail = str(inv.get("error") or "malformed arguments")
        self._close_quietly(call_id, name, "invalid_arguments", detail)

    async def _lease_lost(self, ctx: TurnContext, call_id: str, name: str) -> Outcome:
        result = error_result("lease_lost", "another pod took over this session")
        await self._emit_result(call_id, name, result)
        self.messages.append(tool_message(call_id, name, result))
        return await self._fail(ctx, "turn_failed", "turn lease was lost")

    async def _unauthorized(self, ctx: TurnContext) -> Outcome:
        return await self._fail(ctx, "unauthorized", "the platform rejected the session identity")

    async def _execute(
        self, ctx: TurnContext, call_id: str, name: str, args: dict[str, Any]
    ) -> Outcome | None:
        """도구 하나를 실행하고 이벤트·ToolMessage 를 남긴다. 턴을 끝내야 하면 Outcome. 승인된
        제안은 검사할 때 이미 셌으므로 다시 세지 않는다."""
        if not await self.renew():
            return await self._lease_lost(ctx, call_id, name)
        if self._is_proposal(name):
            result = await self.proposals.approve(ctx, self.tool_calls, name, args)
        elif name == DEFINITION_TOOL:
            self.tool_calls += 1
            result = await self.proposals.read(ctx, self.tool_calls, args, self._span())
        else:
            self.tool_calls += 1
            result = await self.executor.call(
                name, args, ctx=ctx, n=self.tool_calls, files=self.files
            )
        if result.file is not None:
            fname, fetched = result.file
            self.files[fname] = fetched
        await self._emit_result(call_id, name, result)
        if result.resource is not None:
            await self._emit("resource", result.resource)
        if result.event is not None:
            await self._emit("ui", result.event)
        self.messages.append(tool_message(call_id, name, result))
        if result.status == "error" and result.http_status == 401:
            return await self._unauthorized(ctx)
        return None

    def _span(self) -> SpanUsage:
        return span_usage(self.messages, {d.name for d in self.tool_defs if d.spec.proposal})

    async def _emit_call(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        """`tool_call` 이벤트. 제안의 인자는 64 KiB 까지라 화면과 전사에는 4KB 로 자른 사본을
        싣는다. 카드는 제안 덩어리가 그리고, 기다리는 호출은 인자 전체를 갖는다 (P48 6.6절)."""
        shown = self._mask(name, args)
        if self._is_proposal(name):
            cut = truncate(ToolResult("success", shown))
            shown = cut.content if isinstance(cut.content, dict) else {}
        await self._emit("tool_call", {"id": call_id, "name": name, "args": shown})

    async def _emit_result(self, call_id: str, name: str, result: ToolResult) -> None:
        """화면과 전사에 결과를 낸다. 4KB 로 자르고, 통째로 주는 결과(exempt)는 자른 사본을 내고
        ToolMessage 에는 원본이 간다(P48 6.2절). 그 밖에는 같은 잘린 본문을 화면과 문맥이 본다."""
        shown = truncate(replace(result)) if result.exempt else truncate(result)
        await self._emit(
            "tool_result",
            {"id": call_id, "name": name, "status": shown.status, "content": shown.text},
        )

    def _mask(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """표의 secret_fields 자리를 `***` 로. 표에 없는 이름은 그대로다. 예약 칸은 빼고 화면
        이벤트와 저장하는 AIMessage 에 싣지 않는다(기다리는 호출에는 `_propose` 가 따로 넣는다)."""
        args = without_proposal(args)
        d = self._defs.get(name)
        return mask_secrets(d, args) if d is not None else args

    def _is_proposal(self, name: str) -> bool:
        d = self._defs.get(name)
        return d is not None and d.spec.proposal

    def _needs_confirm(self, name: str, args: dict[str, Any] | None) -> bool:
        d = self._defs.get(name)
        # 표에 없는 이름은 실행기가 unknown_tool 로 답한다. 승인 없이 바로 그 답을 받는다
        if d is None:
            return False
        uses_fetched = bool(file_refs(args))
        return needs_confirm(
            d.spec.risk, self.confirm_deploy, uses_fetched=uses_fetched, proposal=d.spec.proposal
        )

    def _fetched_files(self, args: dict[str, Any] | None) -> list[dict[str, Any]]:
        """승인 카드가 보여 줄 "이 호출이 심는 것". 이름과 출처 URL 이고, 옛 세션이나 없는
        이름이면 URL 없이 이름만이다."""
        out: list[dict[str, Any]] = []
        for name in file_refs(args):
            entry: dict[str, Any] = {"name": name}
            fetched = self.files.get(name)
            if fetched is not None and fetched.url:
                entry["url"] = fetched.url
            out.append(entry)
        return out

    def _pending_call(self, call: dict[str, Any]) -> PendingCall:
        d = self._defs.get(call["name"])
        return PendingCall(
            id=call["id"],
            name=call["name"],
            args=self._mask(call["name"], dict(call["args"] or {})),
            risk=d.spec.risk if d is not None else "read",
        )

    async def _interrupt(
        self, ctx: TurnContext, *, proposal: dict[str, Any] | None = None
    ) -> Outcome:
        """pending 을 먼저 저장하고 interrupt 를 낸다. 클라이언트가 끊겨도 문서에는 남아 있다.
        proposal 은 홀로 기다리는 제안의 카드 덩어리이고 그 요청의 `proposal` 에 실린다."""
        assert self.pending is not None
        pending = self.pending
        data: dict[str, Any] = {"session_id": ctx.session_id, "kind": pending.kind}
        if pending.kind == "confirm":
            requests = []
            for c in pending.calls:
                d = self._defs.get(c.name)
                request: dict[str, Any] = {"name": c.name, "args": c.args}
                if d is not None:
                    request["description"] = approval_summary(d.spec.description)
                files = self._fetched_files(c.args)
                if files:
                    request["files"] = files
                if proposal is not None:
                    request["proposal"] = proposal
                requests.append(request)
            data["action_requests"] = requests
            data["review_configs"] = [
                {"action_name": c.name, "allowed_decisions": list(ALLOWED_DECISIONS)}
                for c in pending.calls
            ]
        else:
            (call,) = pending.calls
            data["action_requests"] = []
            data["review_configs"] = []
            question: dict[str, Any] = {"text": str(call.args.get("question") or "")}
            if isinstance(call.args.get("options"), list):
                question["options"] = [str(o) for o in call.args["options"]]
            if call.args.get("field"):
                question["field"] = str(call.args["field"])
            data["question"] = question
        apply_event(self.transcript, "interrupt", data)
        await self.checkpoint()
        await self.emit("interrupt", data)
        return Outcome("interrupt")

    async def _done(self, ctx: TurnContext, text: str) -> Outcome:
        await self._emit(
            "done",
            {
                "session_id": ctx.session_id,
                "turn": ctx.turn,
                "usage": self.turn_usage.model_dump(),
                "tool_calls": self.tool_calls,
                "text": text,
            },
        )
        return Outcome("done")

    def record_error(self, session_id: str, code: str, message: str) -> dict[str, Any]:
        """`error` 이벤트의 data 를 만들어 전사에 남기고 돌려준다. 흐르던 말풍선도 여기서 닫힌다.
        루프의 `_fail` 과 서비스의 충돌 경로(`_run_task`)가 함께 쓴다."""
        data = {"session_id": session_id, "code": code, "message": message}
        apply_event(self.transcript, "error", data)
        return data

    async def _fail(self, ctx: TurnContext, code: str, message: str) -> Outcome:
        await self.emit("error", self.record_error(ctx.session_id, code, message))
        return Outcome("error", code)

    async def _emit(self, name: str, data: dict[str, Any]) -> None:
        apply_event(self.transcript, name, data)
        await self.emit(name, data)

    def _count(self, meta: Mapping[str, Any] | None) -> None:
        self.usage.add(meta)
        self.turn_usage.add(meta)

    # -- 문맥 자르기 ------------------------------------------------------------------------

    async def _trim(self) -> None:
        humans = [
            i
            for i, m in enumerate(self.messages)
            if isinstance(m, HumanMessage) and not is_summary(m)
        ]
        if len(humans) <= KEEP_HUMANS:
            return
        cut = humans[-KEEP_HUMANS]
        head, tail = self.messages[:cut], self.messages[cut:]
        summary = await self._summarize(head)
        self.messages = [
            HumanMessage(content=SUMMARY_PREFIX + summary, additional_kwargs={SUMMARY_FLAG: True})
        ] + tail

    async def _summarize(self, head: Sequence[BaseMessage]) -> str:
        rendered = render_for_summary(head)
        prompt: list[BaseMessage] = [
            SystemMessage(content=SUMMARY_SYSTEM),
            HumanMessage(content=f"{SUMMARY_MARKER}\n{rendered}"),
        ]
        try:
            async with self.meter() as meter:
                ai = await self.model.ainvoke(prompt)
                spent = _measure(meter, prompt, ai) if isinstance(ai, AIMessage) else None
        except Exception as e:  # noqa: BLE001 - 요약 실패가 턴을 죽이면 안 된다
            logger.warning("assistant summary failed, keeping user lines: %s", e)
            return "\n".join(f"- {m.text[:200]}" for m in head if isinstance(m, HumanMessage))
        self._count(spent)
        return ai.text


def _measure(meter: CallMeter, prompt: Sequence[BaseMessage], ai: AIMessage) -> Mapping[str, Any]:
    """응답의 사용량을 계량에 알리고 세션이 셀 사용량을 돌려준다(`CallMeter.measure`)."""
    return meter.measure([m.text for m in prompt], ai)


__all__ = [
    "ALLOWED_DECISIONS",
    "KEEP_HUMANS",
    "SUMMARY_FLAG",
    "Decision",
    "Limits",
    "ModelFailed",
    "Outcome",
    "TurnContext",
    "TurnLoop",
    "apply_event",
    "is_summary",
    "masked_ai_message",
    "needs_confirm",
    "render_for_summary",
    "resolve_interrupts",
    "retryable",
    "tool_message",
]
