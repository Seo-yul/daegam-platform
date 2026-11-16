"""비서용 결정적 스텁 모델 (D31, P14 F1b). provider `kind: stub` 이 받는다.

상태가 없다. 마지막 HumanMessage 의 `{"steps": [{"tool": ..., "args": {...}}, ..., {"say": "..."}]}`
를 읽고 그 뒤에 붙은 ToolMessage 수로 다음 step 을 정하므로 결정 재개가 다른 파드에 가도 된다.

- step 은 `{"tool", "args"}` 하나이거나 `{"tools": [...]}` 로 한 AIMessage 에 여럿이다. 도구 step 은
  부른 수만큼 ToolMessage 를 소비하고 `say` 는 소비하지 않는다.
- args 안 문자열 `"$<n>.<json-path>"`(예 `"$2.id"`, `"$0.items[0].id"`)는 마지막 HumanMessage 뒤
  n 번째 ToolMessage(0부터. 루프가 붙인 순서라 바로 실행, deferred, 사용자의 답 순이다)의 JSON
  에서 뽑은 값으로 바뀐다. 문자열 전체가 참조면 값의 형(型)을 지키고, 긴 문장 안에 섞여 있으면
  문자열로 끼워 넣는다. JSON 이 아닌 결과는 문자열 그대로이고 없는 참조는 null 이다. `say` 문장에도
  같은 규칙이 적용된다.
- `say` 는 텍스트 답으로 턴을 끝낸다. 마지막 도구 결과가 error 면 그 code 를 문장 뒤에 붙인다
  (완료 조건 2: viewer 의 403 을 청지기가 말한다).
- steps 가 없으면 "Tool 이 없어 답만 합니다: " + 입력. steps 를 다 썼는데 say 가 없으면 고정 문구.
- 요약 프롬프트(`Messages to summarize:`)에는 고정 문구를 돌려준다.
- `bind_tools` 는 이름만 기억하고 자기 자신을 돌려준다. usage 는 글자 수 / 4 근사다.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr

from daegam.services.assistant.prompt import SUMMARY_MARKER

SUMMARY_TEXT = "요약: 이전 대화는 스텁이 요약했다."
# 사용자에게 그대로 보이는 답이라 합니다체다. 런타임 스텁(agent-runtime/runtime/stub.py)과 같은 문구
NO_TOOL_PREFIX = "Tool 이 없어 답만 합니다: "
STEPS_DONE_TEXT = "모든 단계를 마쳤다."
LAST_ERROR_FORMAT = " (마지막 Tool 결과: error {code})"

# 참조 경로는 이름·점·대괄호 색인만이다. `$2`, `$2.id`, `$0.items[0].id`
_PATH = r"[A-Za-z0-9_]+(?:\[\d+\])*(?:\.[A-Za-z0-9_]+(?:\[\d+\])*)*"
# 문자열 전체가 참조 하나인가
_REF_FULL = re.compile(rf"^\$(\d+)(?:\.({_PATH}))?$")
# 문장 안에 섞인 참조
_REF_INLINE = re.compile(rf"\$(\d+)(?:\.({_PATH}))?")
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        if name is None and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def parse_steps(text: str) -> list[dict[str, Any]] | None:
    """메시지 안의 `{"steps": [...]}` 를 찾는다. 메시지 전체가 JSON 이거나, 그 조각이 어디엔가
    있으면 된다. 없거나 모양이 다르면 None."""
    candidates = [text.strip()]
    start = text.find('{"steps"')
    if start >= 0:
        candidates.append(text[start:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            data, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(steps, list) and all(isinstance(s, dict) for s in steps):
            return steps
    return None


def json_path(value: Any, path: str | None) -> Any:
    """`items[0].id` 꼴 경로를 따라간다. 없으면 None."""
    if not path:
        return value
    for key, index in _PATH_TOKEN.findall(path):
        if index:
            if isinstance(value, list) and int(index) < len(value):
                value = value[int(index)]
            else:
                return None
        elif isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def parse_result(message: ToolMessage) -> Any:
    """ToolMessage 본문을 JSON 으로. JSON 이 아니면 문자열 그대로."""
    text = message.text
    try:
        return json.loads(text)
    except ValueError:
        return text


def substitute(value: Any, results: Sequence[Any]) -> Any:
    """args 안의 `$<n>.<path>` 참조를 결과로 바꾼다. dict·list 는 재귀다."""
    if isinstance(value, dict):
        return {k: substitute(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, results) for v in value]
    if not isinstance(value, str):
        return value
    full = _REF_FULL.fullmatch(value)
    if full is not None:
        return _lookup(results, int(full.group(1)), full.group(2))

    def inline(m: re.Match[str]) -> str:
        found = _lookup(results, int(m.group(1)), m.group(2))
        return found if isinstance(found, str) else json.dumps(found, ensure_ascii=False)

    return _REF_INLINE.sub(inline, value)


def _lookup(results: Sequence[Any], n: int, path: str | None) -> Any:
    if n >= len(results):
        return None
    return json_path(results[n], path)


def step_calls(step: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """step 이 부르는 도구들. `{"tool", "args"}` 는 하나, `{"tools": [...]}` 는 한 AIMessage 에
    여럿이다. say 는 빈 목록."""
    if isinstance(step.get("tool"), str):
        return [(step["tool"], step.get("args") or {})]
    tools = step.get("tools")
    if isinstance(tools, list):
        return [(t["tool"], t.get("args") or {}) for t in tools if isinstance(t.get("tool"), str)]
    return []


def current_step(steps: Sequence[dict[str, Any]], answered: int) -> dict[str, Any] | None:
    """마지막 HumanMessage 뒤에 붙은 ToolMessage 수로 다음 step 을 고른다. 도구 step 은 부른 수만큼
    ToolMessage 를 소비하고 say 는 소비하지 않는다. 다 썼으면 None."""
    consumed = 0
    for step in steps:
        width = len(step_calls(step))
        if width and answered >= consumed + width:
            consumed += width
            continue
        return step
    return None


def _last_error_code(results: Sequence[Any], messages: Sequence[ToolMessage]) -> str | None:
    if not messages:
        return None
    last, value = messages[-1], results[-1]
    is_error = last.status == "error" or (
        isinstance(value, dict) and value.get("status") == "error"
    )
    if not is_error:
        return None
    code = value.get("code") if isinstance(value, dict) else None
    return str(code) if code else "error"


class AssistantStub(BaseChatModel):
    """steps 를 순서대로 tool_call 로 바꾸고 say 로 턴을 끝내는 결정적 모델이다."""

    model_name: str = "stub"
    _bound_tools: list[str] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "daegam-assistant-stub"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> Any:
        return {"ls_provider": "stub", "ls_model_name": self.model_name, "ls_model_type": "chat"}

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        self._bound_tools = [name for name in (_tool_name(t) for t in tools) if name]
        return self

    @property
    def bound_tools(self) -> list[str]:
        return list(self._bound_tools)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.reply(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.reply(messages))])

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        """규칙대로 다음 메시지를 만든다. 무작위성은 tool_call id 뿐이다."""
        input_chars = sum(len(m.text) for m in messages)
        last = messages[-1] if messages else None
        if isinstance(last, HumanMessage) and SUMMARY_MARKER in last.text:
            return self._text(SUMMARY_TEXT, input_chars)
        human_index = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if human_index is None:
            return self._text(NO_TOOL_PREFIX, input_chars)
        user_text = messages[human_index].text
        steps = parse_steps(user_text)
        if steps is None:
            return self._text(NO_TOOL_PREFIX + user_text, input_chars)
        tool_messages = [m for m in messages[human_index + 1 :] if isinstance(m, ToolMessage)]
        results = [parse_result(m) for m in tool_messages]
        suffix = ""
        code = _last_error_code(results, tool_messages)
        if code is not None:
            suffix = LAST_ERROR_FORMAT.format(code=code)
        step = current_step(steps, len(tool_messages))
        if step is None:
            return self._text(STEPS_DONE_TEXT + suffix, input_chars)
        calls = step_calls(step)
        if calls:
            tool_calls = []
            for name, raw_args in calls:
                args = substitute(raw_args, results)
                tool_calls.append(
                    {
                        "name": name,
                        "args": args if isinstance(args, dict) else {},
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "type": "tool_call",
                    }
                )
            output = sum(len(c["name"]) + len(json.dumps(c["args"])) for c in tool_calls)
            return AIMessage(
                content="",
                tool_calls=tool_calls,
                usage_metadata=_usage(input_chars, output),
            )
        say = step.get("say")
        text = (
            substitute(say, results)
            if isinstance(say, str)
            else json.dumps(step, ensure_ascii=False)
        )
        return self._text(str(text) + suffix, input_chars)

    @staticmethod
    def _text(text: str, input_chars: int) -> AIMessage:
        return AIMessage(content=text, usage_metadata=_usage(input_chars, len(text)))


def _usage(input_chars: int, output_chars: int) -> dict[str, int]:
    # 토큰 수는 글자 수 4 분의 1 로 근사한다. usage 를 세는 쪽이 0 이 아닌 값을 보게 하려는 것이다
    input_tokens = max(1, input_chars // 4)
    output_tokens = max(1, output_chars // 4)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


__all__ = [
    "LAST_ERROR_FORMAT",
    "NO_TOOL_PREFIX",
    "STEPS_DONE_TEXT",
    "SUMMARY_MARKER",
    "SUMMARY_TEXT",
    "AssistantStub",
    "current_step",
    "json_path",
    "parse_steps",
    "substitute",
]
