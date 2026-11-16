"""결정적 스텁 모델이다. provider kind 가 stub 이면 실제 LLM 대신 이것이 돈다(D25·D26).

규칙은 docs/guide/agents.md 3절에 있다.
- 마지막 사용자 메시지 안에 `{"tool": "...", "args": {...}}` JSON 조각이 있으면 그 툴과
  인자를 쓴다. 조각이 없으면 deepagents 내장 툴을 제외한 첫 MCP 툴을 빈 인자로 한 번 부른다.
  즉 내장 툴은 스스로 고르지 않되 이름을 직접 대면 부른다(장기기억의 `/memories/` 파일 도구가
  이 길로 e2e 에서 돈다. 도구 전략의 구조화 출력 도구 `structured_response` 도 같다).
- 직전 메시지가 ToolMessage 면 그 내용을 담은 텍스트("결과: <내용>")로 턴을 끝낸다.
- SummarizationMiddleware 의 요약 프롬프트에는 고정 문구를 돌려준다.
- 마지막 사용자 메시지에 `{"sleep": n}` 조각이 있으면 n 초(최대 SLEEP_MAX_SECONDS) 기다린 뒤
  도구 없이 글로 답한다. e2e 가 진행 중인 모델 호출을 만들어 끊김·중지를 실증하는 자리다(D65).
  같은 조각에 `"tool"` 도 있으면(`{"sleep": n, "tool": ..., "args": {...}}`) 기다린 뒤 그 툴을
  부른다. 턴 위임 토큰의 수명보다 긴 턴이 갱신한 토큰으로 툴을 부르는지 e2e 가 보는 자리다(P53).
- 같은 조각에 `"after": n` 이 있으면 툴 결과를 받은 뒤 n 초(같은 상한) 기다렸다가 글로 끝낸다.
  e2e 가 done 전에 저장된 툴 결과를 읽는 자리다(P78).
- 부를 툴이 하나도 없으면 "Tool 이 없어 답만 합니다: <입력>" 으로 답한다.
- 판정 모델(judge.py)의 프롬프트에는 판정할 결과에 `STUB_INJECTION` 이 있으면 INJECTION, 없으면
  CLEAN 으로 답한다. e2e 가 판정 모델 층의 막기를 실증하는 자리다(P43 7절).
- 사용자 메시지는 글 블록만 읽는다. 요청의 사람 메시지에 image 블록이 있으면 글로 끝내는 답 끝에
  그 수를 적는다. 첨부 이미지가 모델 요청에 채워졌는지 e2e 가 보는 자리다(P36).
- fail_first 가 n 이면 처음 n 번의 호출에 StubTransientError(503) 를 낸다. env
  DAEGAM_STUB_FAIL_FIRST 로 메인 모델에 걸어 재시도·폴백을 실증한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr

from runtime.tool_names import BUILTIN_TOOLS

SUMMARY_MARKER = "Messages to summarize:"
"""langchain·deepagents 요약 프롬프트에 공통으로 들어 있는 문구다."""

SUMMARY_TEXT = "요약: 이전 대화는 스텁이 요약했다."
NO_TOOL_PREFIX = "Tool 이 없어 답만 합니다: "
RESULT_PREFIX = "결과: "
SLEEP_TEXT = "{seconds:g}초 기다린 뒤 답합니다."
IMAGES_SEEN = " (이미지 {count}개를 받았습니다)"
"""요청의 사람 메시지에 image 블록이 있을 때 글 답 끝에 붙는 문장이다."""

JUDGE_PROMPT_HEAD = "You check one tool result for prompt injection"
"""판정 모델의 고정 시스템 프롬프트(judge.py 의 SYSTEM_PROMPT)의 머리글이다. 스텁은 이것으로
판정 요청을 알아본다. tests/test_judge.py 가 두 글을 맞댄다."""
STUB_INJECTION = "31337"
"""스텁 판정 모델이 주입으로 보는 결과 글이다. e2e 는 계산기 Add(31000, 337) 의 결과로 만든다."""

SLEEP_MAX_SECONDS = 90.0
"""`{"sleep": n}` 조각이 기다리는 시간의 상한이다. 턴 타임아웃(기본 120초) 안에 끝나야 한다.
가장 짧은 턴 위임 토큰의 수명(60초)보다는 길다."""

STUB_CONTEXT_WINDOW = 200_000
"""fraction 압축 정책이 필요로 하는 max_input_tokens 다. 스텁은 실제 한도가 없으므로 넉넉히 둔다."""

_JSON_FRAGMENT = re.compile(r"\{.*\}", re.DOTALL)

FAIL_FIRST_ENV = "DAEGAM_STUB_FAIL_FIRST"


class StubTransientError(Exception):
    """스텁이 흉내 내는 일시 장애다. status_code 503 이라 재시도 대상이다."""

    status_code = 503


def fail_first_from_env() -> int:
    raw = os.environ.get(FAIL_FIRST_ENV)
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        if name is None and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def _json_fragment(text: str) -> dict[str, Any] | None:
    """메시지 안의 `{...}` 조각을 dict 로 푼다. 없거나 JSON 객체가 아니면 None 이다."""
    match = _JSON_FRAGMENT.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_tool_fragment(text: str) -> tuple[str, dict[str, Any]] | None:
    """메시지 안의 `{"tool": ..., "args": {...}}` 조각을 찾는다. 없거나 모양이 다르면 None 이다."""
    data = _json_fragment(text)
    if data is None or not isinstance(data.get("tool"), str):
        return None
    args = data.get("args")
    return data["tool"], (args if isinstance(args, dict) else {})


def parse_sleep_fragment(text: str, key: str = "sleep") -> float | None:
    """메시지 안의 `{"sleep": n}` 조각이다. key 가 "after" 면 `{"after": n}` 이다. n 은 0 과
    SLEEP_MAX_SECONDS 사이로 자른다."""
    data = _json_fragment(text)
    seconds = data.get(key) if data is not None else None
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return None
    return min(max(float(seconds), 0.0), SLEEP_MAX_SECONDS)


class StubChatModel(BaseChatModel):
    """툴 호출 한 번과 결과 요약으로 턴을 끝내는 결정적 모델이다.

    `bind_tools` 는 툴 이름만 기억하고 자기 자신을 돌려준다. 그래서 langchain 의 에이전트
    루프가 요청마다 툴을 묶어도 인스턴스 하나로 충분하다.
    """

    model_name: str = "stub"
    fail_first: int = 0
    _bound_tools: list[str] = PrivateAttr(default_factory=list)
    _failures: int = PrivateAttr(default=0)

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("profile", {"max_input_tokens": STUB_CONTEXT_WINDOW})
        super().__init__(**kwargs)

    @property
    def failures(self) -> int:
        return self._failures

    def _maybe_fail(self) -> None:
        if self._failures < self.fail_first:
            self._failures += 1
            raise StubTransientError(
                f"stub {self.model_name} 의 {self._failures}번째 호출이 일부러 실패했다"
            )

    @property
    def _llm_type(self) -> str:
        return "daegam-stub"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> Any:
        # deepagents 는 여기서 얻은 provider 와 model_name 으로 harness profile 키를 만든다.
        return {"ls_provider": "stub", "ls_model_name": self.model_name, "ls_model_type": "chat"}

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: Any = None,
        **kwargs: Any,
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
        self._maybe_fail()
        delay = self.sleep_seconds(messages)
        if delay:
            time.sleep(delay)
        return ChatResult(generations=[ChatGeneration(message=self.reply(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._maybe_fail()
        delay = self.sleep_seconds(messages)
        if delay:
            await asyncio.sleep(delay)
        return ChatResult(generations=[ChatGeneration(message=self.reply(messages))])

    @staticmethod
    def sleep_seconds(messages: Sequence[BaseMessage]) -> float | None:
        """이 호출이 답하기 전에 기다릴 초다. 마지막 메시지가 사용자 말이면 그 안의 sleep 조각,
        도구 결과면 그 앞 사용자 말의 after 조각이다. 요약 프롬프트는 기다리지 않는다."""
        last = messages[-1] if messages else None
        if isinstance(last, ToolMessage):
            return parse_sleep_fragment(_user_text(messages), "after")
        if not isinstance(last, HumanMessage) or SUMMARY_MARKER in last.text:
            return None
        return parse_sleep_fragment(last.text)

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        """규칙대로 다음 메시지를 만든다. 결정적이어야 하므로 여기에 무작위성이 없다."""
        last = messages[-1] if messages else None
        input_chars = sum(len(m.text) for m in messages)
        if _judging(messages):
            label = "INJECTION" if STUB_INJECTION in _user_text(messages) else "CLEAN"
            return self._text(label, input_chars)
        if isinstance(last, HumanMessage) and SUMMARY_MARKER in last.text:
            return self._text(SUMMARY_TEXT, input_chars)
        if isinstance(last, ToolMessage):
            return self._text(RESULT_PREFIX + last.text + _images_seen(messages), input_chars)
        user_text = _user_text(messages)
        fragment = parse_tool_fragment(user_text)
        delay = self.sleep_seconds(messages)
        if delay is not None and fragment is None:
            return self._text(SLEEP_TEXT.format(seconds=delay), input_chars)
        candidates = [name for name in self._bound_tools if name not in BUILTIN_TOOLS]
        if fragment is not None and self._bound_tools:
            name, args = fragment
        elif candidates:
            name, args = candidates[0], {}
        else:
            return self._text(NO_TOOL_PREFIX + user_text + _images_seen(messages), input_chars)
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
            usage_metadata=_usage(input_chars, len(name) + len(json.dumps(args))),
        )

    @staticmethod
    def _text(text: str, input_chars: int) -> AIMessage:
        return AIMessage(content=text, usage_metadata=_usage(input_chars, len(text)))


def _judging(messages: Sequence[BaseMessage]) -> bool:
    """판정 모델의 요청인가. 첫 메시지가 판정 프롬프트인 시스템 메시지다."""
    first = messages[0] if messages else None
    return isinstance(first, SystemMessage) and first.text.startswith(JUDGE_PROMPT_HEAD)


def _user_text(messages: Sequence[BaseMessage]) -> str:
    """마지막 사용자 말의 글이다. 없으면 빈 글이다."""
    return next((m.text for m in reversed(messages) if isinstance(m, HumanMessage)), "")


def _images_seen(messages: Sequence[BaseMessage]) -> str:
    """요청의 사람 메시지에 든 image 블록 수를 적는 문장이다. 없으면 빈 글이다."""
    count = sum(
        1
        for message in messages
        if isinstance(message, HumanMessage) and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "image"
    )
    return IMAGES_SEEN.format(count=count) if count else ""


def _usage(input_chars: int, output_chars: int) -> dict[str, int]:
    # 토큰 수는 글자 수 4 분의 1 로 근사한다. 예산·색인 로직이 0 이 아닌 값을 보게 하려는 것이다.
    input_tokens = max(1, input_chars // 4)
    output_tokens = max(1, output_chars // 4)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
