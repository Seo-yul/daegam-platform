"""deepagents astream 이벤트를 SSE 이벤트로 옮긴다.

SSE 이벤트는 token·tool_call·tool_result·citations·interrupt·guardrail·model_fallback·done·error
다. greeting 은 그래프 이벤트가 아니라 routes 가 새 세션의 첫 이벤트로 내는 것이고(D65) 이름만
여기 둔다.
astream(stream_mode=["messages","updates","custom"], subgraphs=True) 은 (namespace, mode, data)
를 낸다.
- messages: (AIMessageChunk | AIMessage, metadata). 텍스트 조각이 token 이다. 요약 미들웨어의
  내부 호출은 metadata 의 lc_internal_call 표식으로 거른다.
- updates: {node: update}. model 노드의 AIMessage.tool_calls 가 tool_call, tools 노드의
  ToolMessage 가 tool_result, "__interrupt__" 가 interrupt 다. 오류가 아닌 ToolMessage 의
  artifact 에 인용 항목이 있으면 tool_result 바로 뒤에 citations 를 낸다(citations.py). 바깥 내용인
  결과(artifact 의 `untrusted`)는 tool_result 에 그 종류(`web`, `outside`)를 싣는다. 옛 판의 웹
  결과(true)는 `web` 이다. interrupt 의 항목은 설명 첫 줄의 이유를 `reason` 칸에 싣는다(P43).
  메인 model 노드의 structured_response 는 마지막 None 아닌 값을 `TurnState.structured` 에
  둔다(구조화 결과가 없는 호출마다 None 이 실린다).
- done{usage} 는 실행의 트레이서(`TurnState.trace`, trace.py)가 센 chat 모델 호출의 합이다.
  트레이서는 astream 의 callbacks 로 가서 서브에이전트와 요약 미들웨어의 호출까지 센다(P37).
- custom: 미들웨어가 signals.emit 으로 보낸 신호다. guardrail·model_fallback·tool_limit 은 SSE 로
  나가고 budget·model_retry·web_guard 는 SSE 없이 호출자(stdout 관측)에게만 간다. 예산 초과 신호는
  턴 오류 budget_exceeded 가 된다.

출력 가드레일에 글을 바꾸는 규칙(pii_mask, block 인 max_length·deny_pattern)이 있으면
`CompiledAgent.output_filter` 가 있고, 그때는 messages 조각의 글을 버리고 model 노드의 updates 에
온 AIMessage 마다 token 하나를 필터를 지난 글로 낸다(메인과 서브 모두). 그래프의 훅은 체크포인트만
고치고 조각은 원문이 흘러 나가기 때문이다. 같은 필터가 구조화 결과(문자열 잎마다 가리고, 막기는
그래프의 OutputGuard 가 본 것과 같은 전체에 댄다)와 메인의 task 결과(서브에이전트의 글)에도 걸린다.
도구 전략의 구조화 출력 도구(tool_names.STRUCTURED_RESPONSE_TOOL)의 이벤트는 필터와 상관없이 내지
않고 세션 요약에도 싣지 않는다. 결과를 한 번 더 싣는 배관이고 가리지 않은 값이 인자와 결과로 나가기
때문이다. 필터가 없으면 조각을 지금처럼 흘린다.

update 에 실린 메시지가 모두 이번 턴의 것은 아니다. 메시지 목록 전체를 되돌리는 노드가 있다.
deepagents 는 답 없는 도구 호출이 남으면 이력 전부를 메워 되돌리고, 출력 PII 가리기는 가린 뒤
목록 전체를 낸다. 이어지는 턴은 체크포인트의 메시지로 `TurnState.seeded` 를 만들어 이력의
글·도구 이벤트를 다시 내지 않고, 턴 안에서 낸 id 도 곧바로 씨앗에 더해 같은 턴의
되돌림도 거른다(D65).

재개 턴이 다시 알리는 tool_call 은 실제로 도는 호출이다. langchain 1.4.3 은 메시지에 모델의 호출을
그대로 두고 실행 순간 바꿔 끼우므로, 같은 update 에 HITL 상태 키(guards.HITL_EDITED_CALLS_KEY)의
고친 호출이 있으면 그 이름과 인자를 싣는다. 서브에이전트의 update 에는 그 키가 오지 않는다.
deepagents 가 서브에이전트를 ainvoke 로 돌려 출력 채널(messages 등)만 부모 스트림에 실리기
때문이다. 그래서 씨앗은 결정의 edit(`TurnState.edits`)도 들고, 키가 없는 재알림은 같은 이름·인자의
보류 요청에 내린 edit 에서 고친 인자를 찾는다. 어느 길이든 카드와 전사는 실제로 돈 인자를 보인다.

턴 문맥(astream(context=))에는 세션 소유자, 이 턴의 인용 장부(`TurnState.citations`), 세션의
첨부(`TurnState.uploads`), 첨부를 받은 세션인가(`holds_attachments`)와 웹 Tool 의 몫(`web`)이
실린다. 장기 기억의 네임스페이스가 소유자로 갈리고(D34), 검색 도구가 장부에서 인용 번호를 받으며,
`/uploads/` 백엔드와 이미지 채우기가 첨부를 읽고, 웹 가드가 허용 목록과 한도를 본다(web_guard.py).
기능 tool_safety 의 바깥 표시, 기억한 선택, 재개할 대기 요청도 실린다(tool_safety.py).

astream 은 `_relay` 의 별도 태스크가 돌린다(D65). 연결이 끊기면 Starlette 의 취소 범위가 응답
태스크를 취소하는데, anyio 는 그 범위 안의 await 마다 취소를 다시 건다. 취소가 LangGraph 의 생성기
안에 떨어지면 뒤처리(`AsyncPregelLoop.__aexit__` 가 별도 태스크로 도는 스택 정리)가 그 되풀이
취소에 잘려 진행 중인 노드 태스크, 곧 모델 호출이 취소되지 않고 새어 나간다(실측). 그래서 그래프는
취소 범위 밖의 태스크가 돌리고, 소비자가 취소되거나 닫히면 그 태스크에 취소를 한 번만 전한 뒤
끝나기를 막이 안에서 기다린다. 턴 타임아웃과 생성기 닫힘도 같은 길이라 진행 중이던 모델 호출은
어느 길로든 멈춘다. 요청으로 멈추는 것(cancel.py 의 `CancelWatch`)도 턴 제한 시간을 지금으로 당겨
같은 길을 탄다. 취소는 여기서 받지 않고 그대로 올린다. 마감은 turn.py 의 몫이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import anyio
from langchain.agents.middleware.internal_call_transformer import INTERNAL_CALL_METADATA_KEY
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolCall, ToolMessage
from langgraph.types import Command, StateSnapshot

from runtime import llm_http, signals, tool_limit
from runtime.approvals import PendingCalls, with_reason
from runtime.budget import ACTION_TURN_ENDED, SCOPE_SESSION
from runtime.cancel import CancelWatch, arm
from runtime.citations import CitationLedger, emitted_citations, untrusted_kind
from runtime.compile import CompiledAgent
from runtime.guards import HITL_EDITED_CALLS_KEY, OutputFilter, edited_call
from runtime.long_term import TurnContext
from runtime.outside import OutsideFlag
from runtime.skills import SKILLS_METADATA_KEY
from runtime.tool_names import STRUCTURED_RESPONSE_TOOL, SUBAGENT_TOOL
from runtime.trace import RunTracer, Usage
from runtime.turn_record import human_message_id
from runtime.uploads import TurnUploads
from runtime.web_guard import WebBudget

log = logging.getLogger("daegam.agent-runtime.stream")

# 새 세션의 첫 이벤트다. stream_turn 이 아니라 routes 가 입구의 인사말로 낸다(D65)
GREETING = "greeting"
TOKEN = "token"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
CITATIONS = "citations"
INTERRUPT = "interrupt"
GUARDRAIL = signals.GUARDRAIL
MODEL_FALLBACK = signals.MODEL_FALLBACK
DONE = "done"
ERROR = "error"

BUDGET = signals.BUDGET
MODEL_RETRY = signals.MODEL_RETRY
WEB_GUARD = signals.WEB_GUARD
INJECTION_CHECK = signals.INJECTION_CHECK
TOOL_LIMIT = signals.TOOL_LIMIT
INTERNAL_EVENTS = frozenset({BUDGET, MODEL_RETRY, WEB_GUARD, INJECTION_CHECK})
"""stream_turn 이 내지만 SSE 로는 나가지 않는 이벤트다. 호출자가 관측 이벤트로만 남긴다."""
CHUNK_END = "chunk_end"
"""그래프 조각 하나를 다 낸 자리의 표시다(P78 3절). `chunk_marks` 일 때만 내고 SSE 로도 관측
이벤트로도 나가지 않는다. 턴이 이 자리에서 닫힌 전사 항목을 저장한다."""

# 턴 오류 코드다. error 이벤트의 code 이자 turn_end 의 status 다. 멈춘 턴의 셋(turn_cancelled,
# turn_access_lost, turn_aborted)은 cancel.py 에 있다
BUDGET_EXCEEDED = "budget_exceeded"
TURN_TIMEOUT = "turn_timeout"
TURN_FAILED = "turn_failed"
# provider 의 크레딧·사용 한도가 바닥나 모델을 부르지 못했다(T20). 다시 보내도 같으므로 turn_failed
# 와 가른다. 화면은 결제·한도를 확인하라고 안내한다
QUOTA_EXHAUSTED = "quota_exhausted"

STREAM_MODES = ["messages", "updates", "custom"]

# 그래프 태스크에 취소를 전한 뒤 LangGraph 가 진행 중 노드 태스크를 취소하고 끝나기를 기다리는
# 상한이다
GRAPH_CLOSE_SECONDS = 10.0


@dataclass(frozen=True)
class DecidedEdit:
    """보류 요청 하나에 내린 edit 결정이다. `requested` 는 모델이 낸 인자, `args` 는 고친 인자다."""

    name: str
    requested: dict[str, Any]
    args: dict[str, Any]


@dataclass
class TurnState:
    """한 턴이 지나며 모은 것이다. 스트림이 끝난 뒤 색인·관측이 읽는다.

    씨앗 셋은 이미 낸 것을 다시 내지 않게 한다. after_model 훅(HITL·가드레일)이 같은 AIMessage 를
    다시 update 로 내고, 목록 전체를 되돌리는 노드는 이력까지 낸다. 이어지는 턴은 `seeded` 로
    체크포인트의 이력을 미리 넣고 새 세션은 빈 채로 시작한다.
    """

    trace: RunTracer = field(default_factory=RunTracer)
    """이 실행의 트레이서다. 스팬을 모으고 턴 사용량을 센다(trace.py). 턴이 `begin` 으로 연다."""
    tool_calls: int = 0
    text_parts: list[str] = field(default_factory=list)
    structured: Any = None
    """메인의 마지막 구조화 결과다. 출력 필터가 있으면 필터를 지난 값이고 막히면 None 이다. 값이
    있을 때만 done 과 turn_end(record_bodies 일 때)에 실린다(turn.py)."""
    interrupted: bool = False
    error: tuple[str, str] | None = None
    charged: bool = False
    """쓴 토큰을 오늘 카운터에 더하는 마감 단계를 지났다(turn.py `_charge_daily`). 리스를 잃은
    마감이 두 번 더하지 않게 한다."""
    closes_session: bool = False
    """마감이 백그라운드 실행의 세션을 색인에서 닫았다(P41 9절, turn.py `_write_end`).
    리스를 푼 뒤 체크포인트와 첨부를 지운다."""
    index_settled: bool = False
    """마감의 색인 쓰기가 끝났다(turn.py `_write_end`). 끝나지 않은 턴은 리스를 풀지 않아 되살리기가
    마감한다(P79 3절)."""
    seen_message_ids: set[str] = field(default_factory=set)
    """글을 낸 AIMessage id 다."""
    known_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    """tool_call 을 낸 호출 id 와 그때의 인자다. 같은 id 는 인자가 달라졌을 때만 다시 낸다."""
    closed_call_ids: set[str] = field(default_factory=set)
    """tool_result 를 낸 호출 id 다. 이 id 의 결과는 다시 내지 않는다."""
    edits: list[DecidedEdit] = field(default_factory=list)
    """재개 턴이 받은 edit 결정이다. 호출 id 에 맞춘 것은 뺀다(모듈 머리의 서브에이전트 까닭)."""
    edited_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    """id 에 맞춘 고친 인자다. 같은 메시지가 다시 와도 같은 인자다."""
    citations: CitationLedger = field(default_factory=CitationLedger)
    """이 턴의 인용 장부다. 턴 문맥으로 도구에 가고, 턴이 끝나면 마지막 번호를 색인에 적는다.
    turn.py 가 세션의 `cite_seq` 에서 이어지게 채운다."""
    uploads: TurnUploads = field(default_factory=TurnUploads)
    """이 턴이 보는 세션의 첨부다. 턴 문맥으로 파일 백엔드와 모델 미들웨어에 간다. 턴을 열 때
    attachments.py 가 채운다."""
    holds_attachments: bool = True
    """세션이 첨부를 저장한 적이 있거나 웹훅 실행의 세션인가다(P41 9절). 턴 문맥으로 웹 가드에 간다.
    turn.py 가 턴을 열 때 색인의 `upload_bytes` 와 `untrusted` 로 채운다."""
    web: WebBudget = field(default_factory=WebBudget)
    """웹 Tool 의 턴 상한과 사용자별 카운터다. 턴 문맥으로 웹 가드에 간다. turn.py 가 채운다."""
    history_outside: bool = False
    """씨앗의 메시지가 바깥 내용을 가졌는가다(P43 3절). 씨앗을 읽는 자리가 채우고 turn.py 가 턴을 열
    때 세션의 바깥 표시에 더한다. 새 세션은 기록이 없어 거짓이다."""
    outside: OutsideFlag = field(default_factory=OutsideFlag)
    """세션의 바깥 표시다. 턴 문맥으로 바깥 표시 층과 판정 층에 간다. turn.py 가 턴을 열 때
    채운다(outside.py)."""
    approval_choices: dict[str, str] = field(default_factory=dict)
    """이 턴의 사람이 기억한 선택이다. 턴 문맥으로 판정 층에 간다(approvals.py)."""
    pending_calls: PendingCalls | None = None
    """재개할 대기 요청이다. 판정 지도가 없는 옛 메시지를 그것으로 재개한다(approvals.py)."""

    def decided_call(self, call: ToolCall) -> ToolCall:
        """결정이 고친 호출이면 고친 인자를 얹은 호출이고 아니면 그대로다. 처음 보는 id 는 같은
        이름·인자의 보류 요청에 내린 첫 edit 에 맞춘다."""
        call_id = call.get("id")
        if call_id is None:
            return call
        args = self.edited_by_id.get(call_id)
        if args is None:
            args = self._take_edit(call["name"], call.get("args") or {})
            if args is None:
                return call
            self.edited_by_id[call_id] = args
        return {**call, "args": args}

    def _take_edit(self, name: str, requested: dict[str, Any]) -> dict[str, Any] | None:
        for index, edit in enumerate(self.edits):
            if edit.name == name and edit.requested == requested:
                del self.edits[index]
                return edit.args
        return None

    @classmethod
    def seeded(
        cls, messages: list[BaseMessage], *, resumed: bool, history_outside: bool = False
    ) -> TurnState:
        """체크포인트의 메시지로 씨앗을 채운 상태다. 이어지는 턴이 이력을 다시 내지 않게 한다.

        AIMessage id 는 모두 넣는다. 도구 호출은 새 턴이면 답 없는 것까지 닫는다. deepagents 가
        다음 턴에 메워 넣는 "cancelled" 결과가 새 턴의 고아 카드로 뜨지 않게 하려는 것이다. 재개
        턴은 답(ToolMessage)이 있는 호출만 넣는다. 승인을 기다리던 호출은 재개가 다시 알려야 화면
        카드의 승인 대기가 풀리고 재개 턴의 전사도 인자를 가진 카드로 시작한다.
        """
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        state = cls(history_outside=history_outside)
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            if message.id is not None:
                state.seen_message_ids.add(message.id)
            for call in message.tool_calls:
                call_id = call.get("id")
                if call_id is None or (resumed and call_id not in answered):
                    continue
                state.known_calls[call_id] = call.get("args") or {}
                state.closed_call_ids.add(call_id)
        return state

    @property
    def usage(self) -> Usage:
        """이 실행의 chat 모델 호출의 합이다. 재개 실행은 앞 실행을 다시 세지 않는다."""
        return self.trace.usage

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def status(self) -> str:
        if self.error is not None:
            return self.error[0]
        if self.interrupted:
            return "pending"
        return "done"


def thread_config(
    session_id: str, checkpoint_id: str | None = None, *, checkpoint_ns: str | None = None
) -> dict[str, Any]:
    """세션의 스레드 설정이다. checkpoint_id 를 주면 그 체크포인트에서 갈래를 낸다(다시 생성).
    checkpoint_ns 를 주면 그 네임스페이스로 좁힌다. 주지 않은 alist 는 서브에이전트의
    네임스페이스까지 읽는다."""
    configurable: dict[str, Any] = {"thread_id": session_id}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    if checkpoint_ns is not None:
        configurable["checkpoint_ns"] = checkpoint_ns
    return {"configurable": configurable}


def _human_input(turn_input: str | HumanMessage, turn_id: str | None) -> HumanMessage:
    """턴을 연 사람 메시지다. 다시 생성은 입구에서 다시 실을 메시지를 만들어 그대로 넘긴다."""
    if isinstance(turn_input, HumanMessage):
        return turn_input
    return HumanMessage(content=turn_input, id=human_message_id(turn_id) if turn_id else None)


def snapshot_messages(snapshot: StateSnapshot) -> list[BaseMessage]:
    """체크포인트 스냅숏의 메시지 목록이다. 값이 아직 없는 스레드면 빈 목록이다."""
    return list(snapshot.values.get("messages", [])) if snapshot.values else []


async def seed_from(agent: CompiledAgent, config: dict[str, Any], *, ephemeral: bool) -> TurnState:
    """config 가 가리키는 체크포인트의 메시지로 새 턴의 씨앗(`TurnState.seeded`)을 만든다. 이어지는
    턴은 스레드의 가장 최근 체크포인트, 다시 생성은 입력 체크포인트에서 읽는다."""
    snapshot = await agent.graph_for(ephemeral=ephemeral).aget_state(config)
    messages = snapshot_messages(snapshot)
    return TurnState.seeded(
        messages, resumed=False, history_outside=agent.history_outside(messages)
    )


def _namespace_label(namespace: tuple[str, ...]) -> str | None:
    """서브에이전트 안에서 난 이벤트면 그 깊이를 표시한다. 메인이면 None 이다."""
    return ":".join(namespace) if namespace else None


async def stream_turn(
    agent: CompiledAgent,
    *,
    session_id: str,
    turn_input: str | HumanMessage | Command,
    state: TurnState,
    owner_sub: str,
    ephemeral: bool = False,
    cancel: CancelWatch | None = None,
    turn_id: str | None = None,
    checkpoint_id: str | None = None,
    chunk_marks: bool = False,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """(event, data) 를 낸다. done 과 error 는 여기서 내지 않는다. 호출자가 색인을 갱신한 뒤 낸다.

    턴 타임아웃은 agent.turn_timeout 이다. 넘으면 state.error 에 turn_timeout 을 적고 끝난다.
    멈추라는 말(cancel)은 같은 제한 시간을 지금으로 당기므로 같은 자리에서 잡되 그 까닭의 코드로
    적는다(중지 요청, 접근을 잃은 사람, 파드 종료). 태스크의 취소는 그대로 올리고, 어느 길로 끝나든
    그래프 태스크를 그 자리에서 멈춘다(`_relay`).

    owner_sub 는 세션 소유자다. 문맥으로 실려 장기 기억의 네임스페이스가 된다(D34). 결정 재개
    턴은 승인자가 열 수 있으므로 호출자가 아니라 소유자여야 한다. 같은 문맥이 state 의 인용 장부를
    검색 도구에 건넨다. ephemeral 이면 영구 저장소를 안 붙인 그래프로 돈다(플랫폼 테스트 채팅).
    turn_id 를 주면 턴을 연 사람 메시지에 `u-<turn_id>` id 를 단다(turn_record.py). 다시 생성은
    다시 실을 사람 메시지와 갈래를 낼 부모 체크포인트(checkpoint_id)를 준다(regenerate.py).
    chunk_marks 면 그래프 조각마다 그 이벤트들 뒤에 `CHUNK_END` 를 낸다(turn.py 의 전사 저장).
    """
    if isinstance(turn_input, Command):
        graph_input: Any = turn_input
    else:
        graph_input = {"messages": [_human_input(turn_input, turn_id)]}
        if agent.reloads_skills:
            # 재배포 뒤 이어지는 세션도 새 Skill 목록을 읽게 한다(skills.py)
            graph_input[SKILLS_METADATA_KEY] = None
    config = thread_config(session_id, checkpoint_id)
    config["callbacks"] = [state.trace]
    context = TurnContext(
        owner_sub=owner_sub,
        citations=state.citations,
        uploads=state.uploads,
        trace_id=state.trace.trace_id,
        holds_attachments=state.holds_attachments,
        web=state.web,
        outside=state.outside,
        approval_choices=state.approval_choices,
        pending_calls=state.pending_calls,
        deadline=time.monotonic() + agent.turn_timeout,
    )
    chunks = agent.graph_for(ephemeral=ephemeral).astream(
        graph_input, config=config, stream_mode=STREAM_MODES, subgraphs=True, context=context
    )
    try:
        async with (
            asyncio.timeout(agent.turn_timeout) as deadline,
            arm(cancel, deadline),
            aclosing(_relay(chunks)) as graph_events,
        ):
            async for namespace, mode, data in graph_events:
                for event in _translate(namespace, mode, data, state, agent.output_filter):
                    yield event
                if chunk_marks:
                    yield CHUNK_END, {}
    except TimeoutError:
        if cancel is not None and cancel.reason is not None:
            state.error = (cancel.reason.code, cancel.reason.detail)
        else:
            state.error = (TURN_TIMEOUT, f"턴이 {agent.turn_timeout:g}초를 넘겨 끊었다")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001  모델·툴·그래프 어디서 나든 사용자에게는 error 이벤트다
        # 화면과 전사에는 예외의 종류만 나가고 원인 문장은 파드 로그에만 남는다(D39). provider
        # SDK 의 인증 오류 문장은 키를 일부 가린 채 담을 수 있어 그대로 내면 키 조각이 전사에 남는다
        log.warning("turn failed: %s", error_detail(e))
        state.error = (turn_error_code(e, gateway=agent.gateway), describe_error(e))


_END = object()


@dataclass(frozen=True)
class _Raised:
    """그래프 태스크가 낸 예외다. 큐로 넘겨 소비자 쪽에서 다시 올린다."""

    error: BaseException


def _cancelled_by_consumer() -> bool:
    """이 태스크가 받은 취소가 소비자(`_relay` 의 finally)가 건 것인지 본다. 그래프 안에서 난
    취소는 예외처럼 소비자에게 넘겨야 소비자가 큐에서 영영 기다리지 않는다."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _relay(source: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """source 를 별도 태스크가 돌리고 조각을 하나씩 넘긴다. 모듈 머리의 까닭이다.

    큐는 한 칸이라 그래프는 소비자가 받는 만큼만 나아간다(오늘의 생성기와 같다). 소비자가 취소되거나
    이 생성기가 닫히면 그 태스크에 취소를 한 번만 전하고 끝나기를 막이 안에서 기다린다. 그 한 번의
    취소가 LangGraph 의 뒤처리를 온전히 돌려 진행 중인 노드 태스크를 취소한다. 그래프의 예외는
    소비자 쪽에서 다시 올린다.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

    async def drive() -> None:
        try:
            async with aclosing(source) as items:
                async for item in items:
                    await queue.put(item)
        except BaseException as e:  # noqa: BLE001  소비자가 건 취소만 그대로 두고 나머지는 넘긴다
            if isinstance(e, asyncio.CancelledError) and _cancelled_by_consumer():
                raise
            await queue.put(_Raised(e))
            return
        await queue.put(_END)

    worker = asyncio.create_task(drive())
    try:
        while True:
            item = await queue.get()
            if item is _END:
                return
            if isinstance(item, _Raised):
                raise item.error
            yield item
    finally:
        worker.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.wait({worker}, timeout=GRAPH_CLOSE_SECONDS)
        if not worker.done():
            log.warning("그래프 태스크가 %g초 안에 멈추지 않았다", GRAPH_CLOSE_SECONDS)


# 오류 꼴의 상한. 플랫폼 수집 경로가 본문 밖의 글을 256자에서 자른다
ERROR_TEXT_MAX = 256


def describe_error(e: BaseException) -> str:
    """사용자에게 보이는 꼴. 예외의 종류만 잇는다. anyio TaskGroup(MCP 세션)이 낸 ExceptionGroup
    은 "unhandled errors in a TaskGroup" 만 보여 원인을 감추므로 잎 예외의 종류까지 펼쳐 잇는다
    (클러스터 실측에서 프록시 타임아웃이 이 문구 뒤에 숨었다). 문장은 싣지 않는다. 스팬의
    error_type 도 이 꼴이다(trace.py)."""
    if isinstance(e, BaseExceptionGroup):
        leaves = [describe_error(inner) for inner in e.exceptions]
        return (f"{type(e).__name__}: " + "; ".join(leaves))[:ERROR_TEXT_MAX]
    return type(e).__name__[:ERROR_TEXT_MAX]


def _causes(e: BaseException) -> Iterator[BaseException]:
    """예외와 그 원인들이다. ExceptionGroup 은 안의 예외를, 그 밖은 `__cause__` 를 따라간다.
    병렬 도구 호출이나 서브에이전트를 지나 올라온 턴 오류를 가를 때 쓴다."""
    yield e
    if isinstance(e, BaseExceptionGroup):
        for inner in e.exceptions:
            yield from _causes(inner)
    elif e.__cause__ is not None:
        yield from _causes(e.__cause__)


def caused_by_quota(e: BaseException) -> bool:
    """예외나 그 원인 가운데 하나가 크레딧 소진인가(T20). 폴백 미들웨어는 마지막 모델의 예외를
    그대로 올리므로, 폴백이 다른 이유로 실패하면 turn_failed 다."""
    return any(llm_http.quota_exhausted(c) for c in _causes(e))


def turn_error_code(e: BaseException, *, gateway: bool) -> str:
    """턴 오류의 코드다. 크레딧 소진이 먼저이고, 게이트웨이 경로면 그다음이 게이트웨이의 세 코드다
    (P38 7절, `llm_http.gateway_error`). 그 밖은 turn_failed 다."""
    if caused_by_quota(e):
        return QUOTA_EXHAUSTED
    codes = (llm_http.gateway_error(c, gateway=gateway) for c in _causes(e))
    return next((code for code in codes if code is not None), TURN_FAILED)


def error_detail(e: BaseException) -> str:
    """로그용 꼴. 종류와 문장을 잎까지 잇는다. 화면·전사에는 쓰지 않는다."""
    if isinstance(e, BaseExceptionGroup):
        leaves = [error_detail(inner) for inner in e.exceptions]
        return f"{type(e).__name__}: " + "; ".join(leaves)
    return f"{type(e).__name__}: {e}"


def _translate(
    namespace: tuple[str, ...],
    mode: str,
    data: Any,
    state: TurnState,
    output_filter: OutputFilter | None,
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    agent_label = _namespace_label(namespace)
    if mode == "messages":
        return _translate_chunk(data, agent_label, state) if output_filter is None else out
    if mode == "custom":
        parsed = signals.parse(data)
        if parsed is not None:
            out.extend(_translate_signal(*parsed, agent_label, state))
        return out
    if mode != "updates" or not isinstance(data, dict):
        return out
    for node, update in data.items():
        if node == "__interrupt__":
            state.interrupted = True
            out.append((INTERRUPT, interrupt_payload(update)))
            continue
        if not isinstance(update, dict):
            continue
        messages = update.get("messages")
        if not isinstance(messages, list):
            continue
        edited = update.get(HITL_EDITED_CALLS_KEY)
        for message in messages:
            out.extend(_translate_message(message, agent_label, state, output_filter, edited))
        structured = update.get("structured_response") if agent_label is None else None
        if structured is not None:
            out.extend(_keep_structured(structured, messages, state, output_filter))
    return out


def _translate_chunk(
    data: Any, agent_label: str | None, state: TurnState
) -> list[tuple[str, dict[str, Any]]]:
    """messages 모드의 글 조각이다. 출력 필터가 없을 때만 오고, 글은 조각마다 바로 나간다."""
    message, metadata = data
    if not isinstance(message, AIMessage) or tool_limit.hidden(message):
        return []
    if metadata and metadata.get(INTERNAL_CALL_METADATA_KEY):
        return []
    # 목록 전체를 되돌린 노드가 이력의 메시지를 다시 흘린 것이다. 글은 한 번만 낸다
    if message.id in state.seen_message_ids:
        return []
    return _text_event(message.text, agent_label, state)


def _text_event(
    text: str, agent_label: str | None, state: TurnState
) -> list[tuple[str, dict[str, Any]]]:
    """글 하나를 token 으로 낸다. 메인의 글만 done.text 에 든다. 빈 글은 내지 않는다."""
    if not text:
        return []
    if agent_label is None:
        state.text_parts.append(text)
    return [(TOKEN, _with_agent({"text": text}, agent_label))]


def _translate_message(
    message: BaseMessage,
    agent_label: str | None,
    state: TurnState,
    output_filter: OutputFilter | None,
    edited: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(message, AIMessage):
        return _translate_ai_message(message, agent_label, state, output_filter, edited)
    if isinstance(message, ToolMessage):
        return _translate_tool_message(message, agent_label, state, output_filter)
    return []


def _translate_ai_message(
    message: AIMessage,
    agent_label: str | None,
    state: TurnState,
    output_filter: OutputFilter | None,
    edited: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """tool_call 은 씨앗에 없거나 인자가 달라진 호출만 낸다. 사용량은 트레이서가 센다.

    출력 필터가 있으면 글도 여기서 낸다. 처음 보는 메시지의 글을 필터를 지나 token 하나로 내고,
    같은 id 의 뒤 업데이트(가드가 고친 판, 목록 전체의 되돌림)는 건너뛴다. tool_call 은 실제로 도는
    호출을 싣는다. 결정의 edit(`state.decided_call`)을 먼저 얹고, 같은 update 에 실린 HITL 의 고친
    호출(`edited`)이 있으면 그것이 이긴다. 실행 순간 바꿔 끼우는 값이기 때문이다.
    """
    if tool_limit.hidden(message):
        # 한도가 서브에이전트를 닫으며 메인 모델에게 남긴 글이다(tool_limit.py). 화면에 내지 않는다
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    if message.id is None or message.id not in state.seen_message_ids:
        if message.id is not None:
            state.seen_message_ids.add(message.id)
        if output_filter is not None:
            out.extend(_text_event(output_filter.apply(message.text), agent_label, state))
    for model_call in message.tool_calls:
        call = edited_call(state.decided_call(model_call), edited)
        call_id = call.get("id")
        args = call.get("args") or {}
        if _is_structured_plumbing(call["name"], agent_label):
            continue
        if not _announce_call(state, call_id, args):
            continue
        payload = {"id": call_id, "name": call["name"], "args": args}
        out.append((TOOL_CALL, _with_agent(payload, agent_label)))
    return out


def _is_structured_plumbing(tool_name: str | None, agent_label: str | None) -> bool:
    """메인의 구조화 출력 도구 호출·결과인가다. 결과는 done 의 구조화 결과 하나로만 나간다."""
    return agent_label is None and tool_name == STRUCTURED_RESPONSE_TOOL


def _keep_structured(
    value: Any,
    messages: list[BaseMessage],
    state: TurnState,
    output_filter: OutputFilter | None,
) -> list[tuple[str, dict[str, Any]]]:
    """메인 model 노드의 구조화 결과를 필터를 지나 state.structured 에 둔다.

    가리기는 문자열 잎마다 건다. 막기는 그래프의 OutputGuard 가 본 것과 같은 전체에 댄다. 모델이
    JSON 글로 답했으면(provider 전략) 그 글이고, 도구 전략이면 모델 글이 비어 있어 값을 직렬화한
    것이다. 잎마다 대면 패턴이 JSON 경계를 넘을 때 done.text 와 체크포인트가 갈린다. 막히면 칸을
    비우고 그 그룹의 문구가 done.text 다. 모델 글이 있으면 같은 글에 댄 token 이 이미 그 문구를
    냈다.
    """
    if output_filter is None:
        state.structured = value
        return []
    model_text = next((m.text for m in messages if isinstance(m, AIMessage) and m.text), "")
    whole = model_text or json.dumps(value, ensure_ascii=False, default=str)
    blocked = output_filter.block_message(whole)
    if blocked is None:
        state.structured = output_filter.mask_leaves(value)
        return []
    state.structured = None
    return [] if model_text else _text_event(blocked, None, state)


def _announce_call(state: TurnState, call_id: str | None, args: dict[str, Any]) -> bool:
    """이 호출의 tool_call 을 낼지 정하고 씨앗을 갱신한다. 새 id 만 호출 수에 든다.

    같은 id 가 같은 인자로 다시 오면 after_model 훅이나 목록 전체의 되돌림이라 내지 않는다. 인자가
    달라졌으면(승인자가 고친 호출) 다시 내어 화면 카드와 전사가 고친 값을 갖게 한다.
    """
    if call_id is None:
        state.tool_calls += 1
        return True
    known = state.known_calls.get(call_id)
    if known is None:
        state.tool_calls += 1
    elif known == args:
        return False
    state.known_calls[call_id] = args
    return True


def _translate_tool_message(
    message: ToolMessage,
    agent_label: str | None,
    state: TurnState,
    output_filter: OutputFilter | None,
) -> list[tuple[str, dict[str, Any]]]:
    """tool_result 는 호출 id 로 한 번만 낸다. 씨앗에 닫힌 이력의 결과는 내지 않는다.

    메인의 task 결과는 서브에이전트의 글이라 출력 필터를 지나 나간다. 체크포인트의 결과는 원문이다.
    메인 모델이 읽는 도구 결과이기 때문이다. 막히지 않은 결과의 artifact 에 인용 항목이 있으면 바로
    뒤에 citations 를 낸다. 막힌 결과는 status 가 error 이고 ToolGuard 가 artifact 도 비운다.
    """
    call_id = message.tool_call_id
    if call_id in state.closed_call_ids or _is_structured_plumbing(message.name, agent_label):
        return []
    state.closed_call_ids.add(call_id)
    payload = {
        "id": call_id,
        "name": message.name,
        "status": message.status,
        "content": _filtered_text(message, output_filter if agent_label is None else None),
    }
    kind = untrusted_kind(message.status, message.artifact)
    if kind is not None:
        payload["untrusted"] = kind
    out = [(TOOL_RESULT, _with_agent(payload, agent_label))]
    items = emitted_citations(message.status, message.artifact)
    if items is not None:
        out.append((CITATIONS, _with_agent({"call_id": call_id, "items": items}, agent_label)))
    return out


def _filtered_text(message: BaseMessage, output_filter: OutputFilter | None) -> str:
    """메시지의 글이다. 에이전트의 글(AIMessage 와 task 결과)은 출력 필터를 지난다. 다른 도구 결과는
    ToolGuard 가 이미 다뤘고 사용자의 말은 출력 규칙의 대상이 아니다."""
    text = message.text
    is_agent_text = isinstance(message, AIMessage) or (
        isinstance(message, ToolMessage) and message.name == SUBAGENT_TOOL
    )
    if output_filter is None or not is_agent_text or not text:
        return text
    return output_filter.apply(text)


def _translate_signal(
    kind: str, payload: dict[str, Any], agent_label: str | None, state: TurnState
) -> list[tuple[str, dict[str, Any]]]:
    if kind == GUARDRAIL:
        return [(GUARDRAIL, _with_agent(dict(payload), agent_label))]
    if kind == BUDGET:
        if payload.get("scope") == SCOPE_SESSION and payload.get("action") == ACTION_TURN_ENDED:
            state.error = (
                BUDGET_EXCEEDED,
                f"세션 토큰 예산 {payload.get('limit')} 을 넘어 턴을 끝냈다"
                f"(누적 {payload.get('used')})",
            )
        return [(BUDGET, dict(payload))]
    if kind in (MODEL_FALLBACK, TOOL_LIMIT, MODEL_RETRY, WEB_GUARD, INJECTION_CHECK):
        return [(kind, _with_agent(dict(payload), agent_label))]
    return []


def _with_agent(data: dict[str, Any], agent_label: str | None) -> dict[str, Any]:
    if agent_label is not None:
        data["agent"] = agent_label
    return data


def interrupt_payload(update: Any) -> dict[str, Any]:
    """Interrupt 튜플에서 action_requests 와 review_configs 를 꺼낸다. 병렬 툴이면 이어 붙인다.
    항목 설명의 첫 줄에 이유가 있으면 `reason` 칸으로 옮긴다(P43 4절, approvals.py). 서브에이전트의
    인터럽트도 같은 꼴로 바깥까지 온다.

    스트림의 interrupt 이벤트와 보류 조회(hitl.read_pending)가 같이 쓴다.
    """
    action_requests: list[Any] = []
    review_configs: list[Any] = []
    for interrupt in update if isinstance(update, list | tuple) else [update]:
        value = getattr(interrupt, "value", interrupt)
        if isinstance(value, dict):
            action_requests.extend(with_reason(a) for a in value.get("action_requests") or [])
            review_configs.extend(value.get("review_configs") or [])
    return {"action_requests": action_requests, "review_configs": review_configs}


def summarize_messages(
    messages: list[BaseMessage],
    *,
    max_chars: int = 200,
    output_filter: OutputFilter | None = None,
) -> list[dict[str, Any]]:
    """세션 상세용 메시지 요약이다. 본문은 앞 max_chars 만 싣는다. 에이전트의 글은 스트림과 같은
    출력 필터를 지난다. 체크포인트의 task 결과가 원문이기 때문이다. 구조화 출력 도구의 호출과
    결과(검증 실패 포함)는 스트림처럼 싣지 않는다. 가리지 않은 값을 한 번 더 싣는 배관이다."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ToolMessage) and _is_structured_plumbing(message.name, None):
            continue
        text = _filtered_text(message, output_filter)
        entry: dict[str, Any] = {"role": message.type, "text": text[:max_chars]}
        if isinstance(message, AIMessage):
            names = [c["name"] for c in message.tool_calls]
            calls = [name for name in names if not _is_structured_plumbing(name, None)]
            if calls:
                entry["tool_calls"] = calls
        if isinstance(message, ToolMessage):
            entry["tool"] = message.name
            entry["status"] = message.status
        out.append(entry)
    return out
