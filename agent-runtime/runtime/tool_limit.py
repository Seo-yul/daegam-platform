"""턴의 Tool 호출 한도 (`limits.max_tool_calls_per_turn`, T82).

langchain 의 `ToolCallLimitMiddleware` 를 한 실행(run)의 한도로 쓴다. 메인과 서브에이전트가 각자
하나씩 두고, 서브에이전트의 한도는 task 호출 하나마다 새로 센다. 재개는 새 실행이라 다시 센다.

langchain 의 두 끝내기는 사람에게 맞지 않는다. `end` 는 한도를 넘는 순간 모델을 다시 부르지 않고
영어 문장("Tool call limit reached ...")을 답으로 붙이고, `continue` 는 막힌 호출에 오류 결과만 주어
모델이 계속 Tool 을 부르면 턴 타임아웃까지 돈다. 그래서 둘을 잇는다.

- **처음 넘을 때.** 한도 안의 호출은 그대로 돌고 넘는 호출만 오류 결과(`LIMIT_NOTICE`)를 받는다.
  모델은 한 번 더 불려 지금까지 얻은 결과로 사람의 말로 답한다. 신호 `tool_limit` 을 한 번 낸다.
- **알린 뒤에도 부를 때.** 아직 결과가 없는 호출들은 돌지 않고 턴이 닫힌다. 메인은 답으로 붙이는
  글이 없고 화면은 `tool_limit` 알림과 그때까지의 답을 보인다. 서브에이전트는 메인 모델이 task
  결과로 읽을 글(`SUBAGENT_STOP`)을 숨긴 AIMessage 로 남긴다. deepagents 는 서브 실행의 마지막 글
  있는 AIMessage 를 task 결과로 삼는데, 그것이 없으면 메인 모델은 빈 결과나 앞선 머리말을 서브의
  답으로 읽기 때문이다. 숨긴 표시(`HIDDEN_KEY`)가 있는 메시지는 stream.py 가 내지 않는다.
- **구조화 결과 Tool.** 응답 형식 json_schema 를 ToolStrategy 로 돌리면 답이 `structured_response`
  호출이다. 모델 노드가 그 결과를 이미 붙이므로 한도는 그 호출을 세지도 막지도 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage

from runtime import signals
from runtime.tool_names import STRUCTURED_RESPONSE_TOOL

log = logging.getLogger(__name__)

COUNT_KEY = "__all__"
"""도구 이름을 가리지 않는 한도의 셈 칸이다(langchain 의 tool_name None)."""

HIDDEN_KEY = "daegam_hidden"
"""AIMessage 의 additional_kwargs 에 이 칸이 참이면 화면에 내지 않는 모델용 글이다."""

LIMIT_NOTICE = (
    "The tool call limit for this turn ({limit} calls) has been reached, so this call did not "
    "run. Do not call any more tools in this turn, except to give your final answer. Answer now "
    "with the results you already have, and say which parts are unfinished because of the limit."
)
"""처음 넘은 호출의 결과다. 모델에게만 가는 글이라 영어로 둔다. 모델은 사람의 말로 답한다.
서브에이전트에서는 읽는 쪽이 메인 에이전트라 "the user" 라 하지 않는다."""

LIMIT_STOP = (
    "The turn stopped here because the tool call limit for this turn ({limit} calls) was reached."
)
"""알린 뒤에도 부른 호출의 결과다. 모델에게만 가는 글이다."""

SUBAGENT_STOP = (
    "The subagent stopped before it finished because its tool call limit ({limit} calls) was "
    "reached. Answer with what you have, or call it again with a narrower task."
)
"""알린 뒤에도 부른 서브에이전트의 task 결과다. 메인 모델에게만 가는 글이다."""


class TurnToolLimit(ToolCallLimitMiddleware):
    """한 실행의 Tool 호출 한도. 처음 넘으면 모델이 답하게 하고, 그 뒤에도 부르면 턴을 닫는다.

    subagent 면 닫을 때 메인 모델이 읽을 task 결과를 남긴다."""

    def __init__(self, limit: int, *, subagent: bool = False) -> None:
        super().__init__(run_limit=limit, exit_behavior="continue")
        self._limit = limit
        self._subagent = subagent

    @property
    def name(self) -> str:
        # 체크포인트의 상태 칸과 미들웨어 목록의 이름은 langchain 의 것을 그대로 둔다
        return ToolCallLimitMiddleware.__name__

    def _matches_tool_filter(self, tool_call: ToolCall) -> bool:
        # 구조화 결과 호출은 답이다. 모델 노드가 결과를 이미 붙였으므로 세지도 막지도 않는다
        return tool_call["name"] != STRUCTURED_RESPONSE_TOOL

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages: list[BaseMessage] = state.get("messages") or []
        at = _last_ai_index(messages)
        if at is None:
            return None
        last = messages[at]
        assert isinstance(last, AIMessage)
        if not last.tool_calls:
            return None
        limit = self._limit
        thread_counts = dict(state.get("thread_tool_call_count") or {})
        run_counts = dict(state.get("run_tool_call_count") or {})
        before_thread = thread_counts.get(COUNT_KEY, 0)
        before_run = run_counts.get(COUNT_KEY, 0)
        allowed, blocked, thread_after, run_after = self._separate_tool_calls(
            last.tool_calls, before_thread, before_run
        )
        # 막힌 호출도 이 실행에서 시도한 것이라 실행 셈에 든다. 처음 넘었는지를 이것으로 가린다
        run_counts[COUNT_KEY] = run_after + len(blocked)
        thread_counts[COUNT_KEY] = thread_after
        counts = {"thread_tool_call_count": thread_counts, "run_tool_call_count": run_counts}
        if not blocked:
            return counts if allowed else None
        if before_run <= limit:
            signals.emit(signals.TOOL_LIMIT, limit=limit)
            notice = LIMIT_NOTICE.format(limit=limit)
            return {**counts, "messages": [_refused(call, notice) for call in blocked]}
        # 알린 뒤에도 불렀다. 이번 묶음은 하나도 돌지 않으므로 스레드 셈에 넣지 않는다. 결과가 이미
        # 붙은 호출(구조화 결과)에는 다시 붙이지 않는다. 같은 tool_call_id 가 둘이면 안 된다
        log.warning("Tool 호출 한도 %d 을 알린 뒤에도 모델이 Tool 을 불러 턴을 닫는다", limit)
        thread_counts[COUNT_KEY] = before_thread
        answered = {m.tool_call_id for m in messages[at + 1 :] if isinstance(m, ToolMessage)}
        stop = LIMIT_STOP.format(limit=limit)
        closing: list[BaseMessage] = [
            _refused(call, stop) for call in last.tool_calls if call["id"] not in answered
        ]
        if self._subagent:
            closing.append(
                AIMessage(
                    content=SUBAGENT_STOP.format(limit=limit),
                    additional_kwargs={HIDDEN_KEY: True},
                )
            )
        return {**counts, "jump_to": "end", "messages": closing}

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


def hidden(message: Any) -> bool:
    """화면에 내지 않는 모델용 글인가(HIDDEN_KEY)."""
    kwargs = getattr(message, "additional_kwargs", None)
    return isinstance(kwargs, dict) and kwargs.get(HIDDEN_KEY) is True


def _last_ai_index(messages: list[BaseMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AIMessage):
            return index
    return None


def _refused(call: ToolCall, content: str) -> ToolMessage:
    return ToolMessage(
        content=content, tool_call_id=call["id"], name=call.get("name"), status="error"
    )
