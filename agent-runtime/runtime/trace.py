"""한 실행(run)의 스팬을 모아 관측 이벤트로 낸다(P37 1·2절, D62).

실행은 `/chat`, 결정 재개, 다시 생성 요청 하나이고 트레이스 하나다. id 는 W3C Trace Context 모양으로
런타임이 뽑는다. 실행 스팬은 turn_end 이고, 그 안의 모델 호출 하나가 `model_call`, 도구 실행 하나가
`tool_exec` 스팬이다. 스팬은 끝날 때 한 번, 시작 시각과 길이를 들고 나간다. 모델 호출의 호출 id 는
`<trace_id>-<span_id>` 이고 원장의 `_id` 와 상관 헤더가 같은 값이다.

`RunTracer` 는 실행마다 astream 의 callbacks 로 넘기는 콜백이다(stream.py). 콜백은 안쪽 runnable
모두에 닿으므로 서브에이전트 그래프, task 도구, 요약 미들웨어의 모델 호출도 잡힌다. `run_inline`
이라 langchain 이 부른 자리의 문맥에서 기다린다. 그래서 모델 호출을 열 때 둔 호출
id(`llm_http.set_call`)를 그 호출의 요청 훅이 읽고, 병렬 서브에이전트는 저마다의 태스크 문맥을
가진다.

- 체인의 시작·끝은 run id 의 부모 표다. 새 스팬의 부모는 가장 가까운 도구 스팬이고(task 면 그 아래가
  서브에이전트다) 없으면 실행 스팬이다. 끝난 체인은 표에서 뺀다
- 모델 호출의 provider·경로·역할은 모델 metadata 의 `daegam_provider`·`daegam_route`·
  `daegam_role` 에서, 요약 호출은 `lc_source` 에서 온다. 사용량은 응답의 usage_metadata 이고
  스트림이면 마지막 조각에도 실린다
- 도구의 출처는 컴파일이 만든 표(`CompiledAgent.tool_sources`)에서 찾는다. 표에 없으면 builtin 이고
  task 는 subagent 다
- 취소된 모델·도구 호출은 끝 훅이 없고 오류 훅(CancelledError)은 버린다. 그래서 턴
  마감(`turn._finish_turn`)이 turn_end 앞에 `close(code)` 를 불러 열린 스팬을 닫는다. 사용량
  조각을 받은 모델 호출은 그 사용량으로 ok 이고 나머지는 토큰 없이 cancelled 이며 턴을 끝낸
  코드가 error_type 이다. close 뒤에 오는 끝 훅(@shielded on_llm_end)은 버린다. 그래서 스팬 id 는
  한 번만 나간다
- 도구 타임아웃은 status error 인 ToolMessage 로 접히면 종류를 잃는다. 감싼 코루틴(mcp.py)이
  `daegam.tool_timeout` 을, 서브에이전트 턴 제한(resilience.py)이
  `daegam.subagent_timeout{tool_call_id}` 를 보내고 여기서 그 스팬을 timeout 으로 닫는다.
  서브에이전트가 끊기면 그 아래 열린 스팬은 cancelled 다
- 검색 도구의 임베딩 요청은 `daegam.embeddings` 로 와서 `model_call{operation: embeddings}` 가
  된다. 사용량은 글자 수 추정이다. 사용량 없이 끝난 chat 호출(스트림 사용량을 끈 provider)도 글자
  수로 어림하고 `estimated` 를 단다

턴 사용량(`usage`)은 이 실행의 chat 모델 호출의 합이다. 서브에이전트와 요약 호출이 들고, 재개 실행은
앞 실행을 다시 세지 않는다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.outputs import LLMResult
from langgraph.types import Command

from runtime import events as ev
from runtime.llm_http import (
    USAGE_KEYS,
    estimate_tokens,
    new_span_id,
    new_trace_id,
    set_call,
    usage_from_metadata,
)
from runtime.tool_names import SUBAGENT_TOOL, SUBAGENT_TYPE_ARG

if TYPE_CHECKING:
    from runtime.events import EventLog

# 도구 타임아웃과 서브에이전트 턴 제한이 보내는 custom 콜백 이벤트다
TOOL_TIMEOUT = "daegam.tool_timeout"
# 도구 소스가 호출을 거절했거나(403) 한도에 걸렸을 때(429) 보내는 custom 콜백 이벤트다(mcp.py). data
# 의 kind 가 스팬의 error_type 이다
TOOL_REFUSED = "daegam.tool_refused"
SUBAGENT_TIMEOUT = "daegam.subagent_timeout"
# 검색 도구가 임베딩 요청 하나를 마칠 때마다 보내는 custom 콜백 이벤트다(knowledge.py)
EMBEDDINGS = "daegam.embeddings"

# build_chat_model 이 모델 metadata 에 싣는 키다. 콜백 metadata 로 합쳐져 온다
PROVIDER_KEY = "daegam_provider"
ROUTE_KEY = "daegam_route"
ROLE_KEY = "daegam_role"
# 요약 미들웨어가 자기 모델 호출의 metadata 에 싣는 출처다
SOURCE_KEY = "lc_source"

OPERATION_CHAT = "chat"
OPERATION_EMBEDDINGS = "embeddings"
ROUTE_DIRECT = "direct"
ROUTE_GATEWAY = "gateway"
ROLE_MAIN = "main"
ROLE_FALLBACK = "fallback"
ROLE_SUBAGENT = "subagent"
# 바깥 결과의 판정 모델(P43 7절, judge.py)이다. 트레이스의 표시일 뿐이다
ROLE_GUARD = "guard"
PURPOSE_AGENT = "agent"
PURPOSE_SUMMARIZATION = "summarization"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
# 도구 결과(ToolMessage)의 성공 status 다. tool_exec 는 이 어휘를 그대로 쓴다
TOOL_SUCCESS = "success"
ERROR_TIMEOUT = "timeout"
ERROR_REFUSED = "refused"
ERROR_RATE_LIMITED = "rate_limited"

SOURCE_BUILTIN = {"kind": "builtin"}
SOURCE_SUBAGENT = {"kind": "subagent"}
# tool_exec 에서 record_bodies 의 본문 규칙(`EventLog.body`)을 지나는 칸이다
BODY_KEYS = ("args", "content")


@dataclass
class Usage:
    """턴의 모델 사용량이다. 다섯 수는 `llm_http.usage_from_metadata` 의 것이라 input 은 cache 읽기·
    쓰기를, output 은 reasoning 을 품는다(langchain 과 GenAI 의 정의)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, counts: Mapping[str, int]) -> None:
        for key in USAGE_KEYS:
            setattr(self, key, getattr(self, key) + int(counts.get(key) or 0))

    def to_dict(self) -> dict[str, int]:
        """done·turn_end 의 usage 다. `cached_input_tokens` 는 cache 읽기를 화면이 읽는 이름이다
        (D57 의 "캐시 N")."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "cached_input_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def error_kind(e: BaseException) -> str:
    """스팬의 error_type 이다. 화면의 오류처럼 예외의 종류만 싣고 문장은 싣지 않는다(D39). 규칙은
    stream.py 의 `describe_error` 하나다. stream.py 가 이 모듈을 읽으므로 부를 때 읽는다."""
    from runtime.stream import describe_error

    return describe_error(e)


async def signal(name: str, data: dict[str, Any]) -> None:
    """실행의 트레이서에 custom 콜백 이벤트를 보낸다. 콜백 문맥 밖(코루틴을 바로 부른 테스트)이면
    받을 실행이 없어 버린다."""
    try:
        await adispatch_custom_event(name, data)
    except RuntimeError:
        return


@dataclass
class _Span:
    """열린 스팬 하나다. 끝날 때 한 번 나간다."""

    event: str
    span_id: str
    parent: _Span | None
    """부모 도구 스팬이다. None 이면 실행 스팬이 부모다."""
    agent: str | None
    """이 스팬이 도는 서브에이전트의 이름이다. 메인이면 None 이다."""
    start: datetime
    t0: float
    fields: dict[str, Any] = field(default_factory=dict)
    first_token: float | None = None
    streamed: UsageMetadata | None = None
    """스트림 조각이 실어 온 사용량이다. provider 는 마지막 조각에만 싣는다."""
    response: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    """신호로 안 실패의 종류다(도구 타임아웃)."""
    prompt_tokens: int = 0
    """보낸 메시지의 글자 수 추정이다. 사용량 없이 끝난 호출의 입력 토큰이다."""

    @property
    def inner_agent(self) -> str | None:
        """이 스팬 아래에서 도는 에이전트다. task 면 그 서브에이전트이고 아니면 이 스팬의 것이다."""
        return self.fields.get("subagent") or self.agent

    def under(self, ancestor: _Span) -> bool:
        node = self.parent
        while node is not None:
            if node is ancestor:
                return True
            node = node.parent
        return False


class RunTracer(AsyncCallbackHandler):
    """한 실행의 스팬을 모으는 콜백이다. 모듈 머리의 까닭이다.

    id 는 만들 때 뽑는다. 결정 재개의 `decision` 이벤트가 실행을 열기 전에 트레이스 id 를 싣기
    때문이다. 이벤트로 내려면 턴이 `begin` 으로 이벤트 기록과 자리 칸을 건넨다. 건네지 않은
    트레이서는 사용량만 센다.
    """

    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self.trace_id = new_trace_id()
        self.span_id = new_span_id()
        self.start = datetime.now(UTC)
        self.usage = Usage()
        self._log: EventLog | None = None
        self._where: dict[str, Any] = {}
        self._sources: Mapping[str, Mapping[str, str]] = {}
        self._parents: dict[UUID, UUID | None] = {}
        self._open: dict[UUID, _Span] = {}
        self._calls: dict[str, UUID] = {}
        """열린 도구 스팬의 tool_call_id 와 run id 다. 서브에이전트 턴 제한 신호가 찾는다."""
        self._last: dict[str, tuple[str, int]] = {}
        """부모 스팬 id 마다 마지막 모델 호출의 (status, attempt) 다."""
        self._closed = False

    def begin(
        self, log: EventLog, *, where: Mapping[str, Any], sources: Mapping[str, Mapping[str, str]]
    ) -> None:
        """실행을 연다. 스팬은 log 로 나가며 where(세션·사용자·턴)를 들고, 도구의 출처는 sources
        에서 찾는다. 실행의 시작은 지금이다."""
        self._log = log
        self._where = dict(where)
        self._sources = sources
        self.start = datetime.now(UTC)

    @property
    def run_fields(self) -> dict[str, str]:
        """turn_start·turn_end 가 드는 실행 스팬의 칸이다."""
        return {"trace_id": self.trace_id, "span_id": self.span_id}

    @property
    def point_fields(self) -> dict[str, str]:
        """실행 안의 점 이벤트(가드레일, 인터럽트, 결정, 예산, 재시도, 폴백)가 드는 칸이다."""
        return {"trace_id": self.trace_id, "parent_span_id": self.span_id}

    def close(self, code: str | None) -> None:
        """실행을 닫는다. 열린 스팬을 닫고 뒤에 오는 훅을 버린다. code 는 턴을 끝낸 오류 코드다."""
        if self._closed:
            return
        self._closed = True
        for span in list(self._open.values()):
            if span.event == ev.MODEL_CALL and span.streamed:
                self._end_model(span, STATUS_OK, usage=span.streamed)
            else:
                self._end(span, STATUS_CANCELLED, code)
        self._open.clear()
        self._parents.clear()
        self._calls.clear()

    # -- 체인: 부모 표 ---------------------------------------------------------------------------

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if not self._closed:
            self._parents[run_id] = parent_run_id

    async def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._parents.pop(run_id, None)

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._parents.pop(run_id, None)

    # -- 모델 호출 ---------------------------------------------------------------------------------

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._closed:
            return
        meta = metadata or {}
        span = self._start(ev.MODEL_CALL, run_id, parent_run_id)
        role = meta.get(ROLE_KEY) or (ROLE_SUBAGENT if span.agent else ROLE_MAIN)
        span.fields.update(
            operation=OPERATION_CHAT,
            provider={"name": meta.get(PROVIDER_KEY), "kind": meta.get("ls_provider")},
            route=meta.get(ROUTE_KEY) or ROUTE_DIRECT,
            role=role,
            model=meta.get("ls_model_name"),
            purpose=(
                PURPOSE_SUMMARIZATION
                if meta.get(SOURCE_KEY) == PURPOSE_SUMMARIZATION
                else PURPOSE_AGENT
            ),
            attempt=self._attempt(span),
            fallback=role == ROLE_FALLBACK,
        )
        span.prompt_tokens = estimate_tokens([m.text for batch in messages for m in batch])
        set_call(self.trace_id, span.span_id)

    async def on_llm_new_token(
        self, token: str, *, chunk: Any = None, run_id: UUID, **kwargs: Any
    ) -> None:
        span = self._open.get(run_id)
        if span is None:
            return
        message = getattr(chunk, "message", None)
        if span.first_token is None and (token or getattr(message, "tool_call_chunks", None)):
            span.first_token = time.monotonic()
        if isinstance(message, AIMessage):
            if message.usage_metadata:
                span.streamed = add_usage(span.streamed, message.usage_metadata)
            span.response.update(message.response_metadata)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._open.pop(run_id, None)
        if span is None:
            return
        message = _response_message(response)
        usage: Mapping[str, Any] | None = span.streamed
        calls = None
        estimated = False
        if message is not None:
            span.response.update(message.response_metadata)
            usage = message.usage_metadata or usage
            calls = [{"name": call["name"], "id": call.get("id")} for call in message.tool_calls]
            if usage is None:
                # 스트림 사용량을 끈 호환 서버다. 글자 수로 어림하고 턴 사용량과 예산에 센다
                output = estimate_tokens([message.text])
                usage = {"input_tokens": span.prompt_tokens, "output_tokens": output}
                estimated = True
        self._end_model(span, STATUS_OK, usage=usage, tool_calls=calls, estimated=estimated)

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        # 취소는 close 나 서브에이전트 턴 제한이 그 까닭의 코드로 닫는다
        if isinstance(error, asyncio.CancelledError):
            return
        span = self._open.pop(run_id, None)
        if span is not None:
            self._end_model(span, STATUS_ERROR, error_type=error_kind(error))

    # -- 도구 실행 ---------------------------------------------------------------------------------

    async def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._closed:
            return
        name = (serialized or {}).get("name") or kwargs.get("name")
        call_id = kwargs.get("tool_call_id")
        span = self._start(ev.TOOL_EXEC, run_id, parent_run_id)
        span.fields.update(
            tool=name,
            call_id=call_id,
            source=self._source_of(name),
            subagent=(inputs or {}).get(SUBAGENT_TYPE_ARG) if name == SUBAGENT_TOOL else None,
            args=inputs if inputs is not None else input_str,
        )
        if call_id:
            self._calls[call_id] = run_id

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._open.pop(run_id, None)
        if span is None:
            return
        message = _tool_message(output)
        if message is None:
            self._end_tool(span, TOOL_SUCCESS, content=output)
        else:
            self._end_tool(span, message.status, content=message.text)

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        # 모델과 같다. langchain-core 1.7 부터 취소된 도구도 이 훅을 부른다
        if isinstance(error, asyncio.CancelledError):
            return
        span = self._open.pop(run_id, None)
        if span is not None:
            self._end_tool(span, STATUS_ERROR, error_type=error_kind(error))

    # -- 신호 --------------------------------------------------------------------------------------

    async def on_custom_event(self, name: str, data: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if self._closed:
            return
        if name == TOOL_TIMEOUT:
            # 감싼 코루틴 안에서 보내므로 run_id 가 그 도구의 실행이다
            span = self._open.get(run_id)
            if span is not None:
                span.error_type = ERROR_TIMEOUT
        elif name == TOOL_REFUSED and isinstance(data, dict):
            span = self._open.get(run_id)
            kind = data.get("kind")
            if span is not None and kind in (ERROR_REFUSED, ERROR_RATE_LIMITED):
                span.error_type = kind
        elif name == SUBAGENT_TIMEOUT and isinstance(data, dict):
            self._subagent_timed_out(str(data.get("tool_call_id")))
        elif name == EMBEDDINGS and isinstance(data, dict):
            self._embedded(run_id, data)

    def _embedded(self, run_id: UUID, data: dict[str, Any]) -> None:
        """검색 도구의 임베딩 요청 하나를 `model_call{operation: embeddings}` 로 낸다. 스팬 id 는
        요청의 호출 id 를 정한 도구가 준다. 사용량은 글자 수 추정이고 턴 사용량에 들지 않는다. 턴
        예산은 chat 모델의 토큰을 묶는다."""
        parent = self._parent_span(run_id)
        seconds = float(data.get("latency_ms") or 0) / 1000
        span = _Span(
            event=ev.MODEL_CALL,
            span_id=str(data.get("span_id") or new_span_id()),
            parent=parent,
            agent=parent.inner_agent if parent is not None else None,
            start=datetime.now(UTC) - timedelta(seconds=seconds),
            t0=time.monotonic() - seconds,
        )
        span.fields.update(
            operation=OPERATION_EMBEDDINGS,
            provider=data.get("provider"),
            route=data.get("route") or ROUTE_DIRECT,
            role=ROLE_SUBAGENT if span.agent else ROLE_MAIN,
            model=data.get("model"),
            purpose=PURPOSE_AGENT,
            attempt=int(data.get("attempt") or 1),
            fallback=False,
        )
        counts = usage_from_metadata({"input_tokens": int(data.get("input_tokens") or 0)})
        status = STATUS_OK if data.get("error_type") is None else STATUS_ERROR
        self._emit(span, status, usage=counts, estimated=True, error_type=data.get("error_type"))

    def _subagent_timed_out(self, call_id: str) -> None:
        """끊긴 task 스팬을 timeout 으로 닫고 그 아래 열린 스팬을 cancelled 로 닫는다."""
        run_id = self._calls.get(call_id)
        task = self._open.pop(run_id) if run_id is not None and run_id in self._open else None
        if task is None:
            return
        for other_id, other in list(self._open.items()):
            if other.under(task):
                del self._open[other_id]
                self._end(other, STATUS_CANCELLED, ERROR_TIMEOUT)
        self._end_tool(task, STATUS_ERROR, error_type=ERROR_TIMEOUT)

    # -- 안쪽 --------------------------------------------------------------------------------------

    def _start(self, event: str, run_id: UUID, parent_run_id: UUID | None) -> _Span:
        parent = self._parent_span(parent_run_id)
        span = _Span(
            event=event,
            span_id=new_span_id(),
            parent=parent,
            agent=parent.inner_agent if parent is not None else None,
            start=datetime.now(UTC),
            t0=time.monotonic(),
        )
        self._open[run_id] = span
        return span

    def _parent_span(self, run_id: UUID | None) -> _Span | None:
        """run_id 에서 위로 올라가며 만나는 첫 도구 스팬이다. 없으면 None(실행 스팬)이다."""
        while run_id is not None:
            span = self._open.get(run_id)
            if span is not None and span.event == ev.TOOL_EXEC:
                return span
            run_id = self._parents.get(run_id)
        return None

    def _parent_id(self, span: _Span) -> str:
        return span.parent.span_id if span.parent is not None else self.span_id

    def _attempt(self, span: _Span) -> int:
        """같은 부모의 앞 모델 호출이 실패했으면 그 시도에 하나를 더하고 아니면 1 이다."""
        last = self._last.get(self._parent_id(span))
        return last[1] + 1 if last is not None and last[0] == STATUS_ERROR else 1

    def _source_of(self, name: str | None) -> Mapping[str, str]:
        if name == SUBAGENT_TOOL:
            return SOURCE_SUBAGENT
        return self._sources.get(name or "", SOURCE_BUILTIN)

    def _end(self, span: _Span, status: str, error_type: str | None) -> None:
        if span.event == ev.MODEL_CALL:
            self._end_model(span, status, error_type=error_type)
        else:
            self._end_tool(span, status, error_type=error_type)

    def _end_model(
        self,
        span: _Span,
        status: str,
        *,
        usage: Mapping[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        error_type: str | None = None,
        estimated: bool = False,
    ) -> None:
        counts = usage_from_metadata(usage)
        if status == STATUS_OK:
            self.usage.add(counts)
        self._last[self._parent_id(span)] = (status, span.fields["attempt"])
        response = span.response
        self._emit(
            span,
            status,
            response_model=response.get("model_name") or response.get("model"),
            finish_reason=response.get("finish_reason") or response.get("stop_reason"),
            ttft_ms=_ms(span.first_token - span.t0) if span.first_token is not None else None,
            tool_calls=tool_calls,
            usage=counts,
            estimated=estimated,
            error_type=error_type,
        )

    def _end_tool(
        self,
        span: _Span,
        status: str,
        *,
        content: Any = None,
        error_type: str | None = None,
    ) -> None:
        call_id = span.fields.get("call_id")
        if call_id:
            self._calls.pop(call_id, None)
        self._emit(span, status, content=content, error_type=span.error_type or error_type)

    def _emit(self, span: _Span, status: str, **extra: Any) -> None:
        log = self._log
        if log is None:
            return
        fields = {**span.fields, **extra}
        for key in BODY_KEYS:
            fields[key] = log.body(fields.get(key))
        log.emit_span(
            span.event,
            start=span.start,
            duration_ms=_ms(time.monotonic() - span.t0),
            **self._where,
            trace_id=self.trace_id,
            span_id=span.span_id,
            parent_span_id=self._parent_id(span),
            agent=span.agent,
            status=status,
            **fields,
        )


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _response_message(response: LLMResult) -> AIMessage | None:
    generations = response.generations[0] if response.generations else []
    message = getattr(generations[0], "message", None) if generations else None
    return message if isinstance(message, AIMessage) else None


def _tool_message(output: Any) -> ToolMessage | None:
    """도구 결과의 ToolMessage 다. task 처럼 Command 로 답한 도구는 그 update 의 메시지다."""
    if isinstance(output, ToolMessage):
        return output
    if isinstance(output, Command) and isinstance(output.update, dict):
        messages = output.update.get("messages") or []
        return next((m for m in messages if isinstance(m, ToolMessage)), None)
    return None
