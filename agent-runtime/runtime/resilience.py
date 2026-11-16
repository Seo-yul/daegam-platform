"""모델 호출 재시도와 폴백, 서브에이전트의 턴 제한이다(D26).

- 재시도: limits.retries 만큼 지수 백오프(초기 1초, 배수 2, 상한 30초, 지터)로 다시 부른다. 대상은
  llm_http 의 다시 보내기 규칙(연결 오류, 타임아웃, 408, 409, 429, 5xx)이다. 크레딧
  소진(`llm_http.quota_exhausted`, T20)과 게이트웨이의 한도·토큰 거절(P38 7절)은 429 여도 기다려서
  풀리지 않으므로 다시 부르지 않고, 폴백이 있으면 곧바로 폴백으로 간다. 다 소진하면 예외를 그대로
  올린다(on_failure=error).
- 폴백: main.fallback 이 있으면 ModelFallbackMiddleware 가 재시도 바깥에서 마지막에 그 모델로
  한 번 더 간다(폴백 모델도 같은 재시도를 거친다). 전환은 custom 신호 model_fallback{from, to} 다.
- 서브에이전트 턴 제한: 메인의 SubagentTimeout 이 task 호출을 그 서브의 limits.turn_timeout_seconds
  로 감싼다(asyncio.timeout). 넘으면 status="error" 인 도구 결과가 메인에게 간다. 메인의 턴 제한
  (stream.py)이 여전히 바깥 한도다.
provider SDK 자체 재시도(max_retries)는 0 으로 두어 여기서만 센다.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

import httpx
from langchain.agents.middleware import ModelFallbackMiddleware, ModelRetryMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.exceptions import ModelError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolCall, ToolMessage
from langgraph.types import Command

from runtime import llm_http, signals
from runtime.guards import effective_call, error_result
from runtime.tool_names import SUBAGENT_TOOL, SUBAGENT_TYPE_ARG
from runtime.trace import SUBAGENT_TIMEOUT, signal

RETRY_INITIAL_DELAY_ENV = "DAEGAM_RETRY_INITIAL_DELAY_SECONDS"
RETRY_INITIAL_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_BACKOFF_FACTOR = 2.0

SUBAGENT_TIMEOUT_TEXT = "서브에이전트 {name} 의 턴이 {seconds:g}초를 넘겨 끊었다"
"""서브의 턴 제한을 넘긴 task 호출의 도구 결과 문구다. 메인 모델이 읽는다."""

TRANSIENT_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
)


def is_transient(exc: Exception, *, gateway: bool = False) -> bool:
    """다시 보내도 되는 실패인가. 규칙은 llm_http 의 `is_retryable` 하나이고(408·409·429·5xx 와 연결
    오류, 크레딧 소진과 게이트웨이의 거절은 아니다), langchain 이 감싼 ModelError 와 이름으로만 아는
    예외를 더한다. langchain 은 429 를 다시 보내도 되는 ModelError 로 감싸므로 게이트웨이의 한도
    (P38 7절)를 그보다 먼저 본다. 게이트웨이의 표시는 게이트웨이 경로(gateway)에서만 본다. 직접
    경로 provider 의 빈 429 는 다시 보낸다."""
    if llm_http.not_retried(exc, gateway=gateway):
        return False
    if isinstance(exc, ModelError):
        return bool(exc.is_retryable)
    if llm_http.is_retryable(exc, gateway=gateway):
        return True
    if isinstance(exc, ConnectionError | TimeoutError | httpx.TransportError):
        return True
    return type(exc).__name__ in TRANSIENT_NAMES


def initial_delay() -> float:
    raw = os.environ.get(RETRY_INITIAL_DELAY_ENV)
    try:
        return float(raw) if raw else RETRY_INITIAL_DELAY_SECONDS
    except ValueError:
        return RETRY_INITIAL_DELAY_SECONDS


def model_label(model: Any) -> str:
    for attr in ("model", "model_name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


class ModelRetry(ModelRetryMiddleware):
    """재시도마다 model_retry 신호를 낸다."""

    def __init__(self, retries: int, *, delay: float | None = None, gateway: bool = False) -> None:
        super().__init__(
            max_retries=int(retries),
            retry_on=partial(is_transient, gateway=gateway),
            on_failure="error",
            initial_delay=initial_delay() if delay is None else delay,
            backoff_factor=RETRY_BACKOFF_FACTOR,
            max_delay=RETRY_MAX_DELAY_SECONDS,
            jitter=True,
        )

    @property
    def name(self) -> str:
        return "DaegamModelRetry"

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> Any:
        attempts = 0

        def spy(req: ModelRequest[Any]) -> ModelResponse[Any]:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                self._signal(attempts - 1, req.model)
            return handler(req)

        return super().wrap_model_call(request, spy)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> Any:
        attempts = 0

        async def spy(req: ModelRequest[Any]) -> ModelResponse[Any]:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                self._signal(attempts - 1, req.model)
            return await handler(req)

        return await super().awrap_model_call(request, spy)

    @staticmethod
    def _signal(attempt: int, model: Any) -> None:
        signals.emit(signals.MODEL_RETRY, attempt=attempt, model=model_label(model))


class ModelFallback(ModelFallbackMiddleware):
    """폴백 모델로 넘어갈 때 model_fallback 신호를 낸다."""

    def __init__(self, fallback: BaseChatModel) -> None:
        super().__init__(fallback)

    @property
    def name(self) -> str:
        return "DaegamModelFallback"

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> Any:
        primary = request.model

        def spy(req: ModelRequest[Any]) -> ModelResponse[Any]:
            if req.model is not primary:
                self._signal(primary, req.model)
            return handler(req)

        return super().wrap_model_call(request, spy)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> Any:
        primary = request.model

        async def spy(req: ModelRequest[Any]) -> ModelResponse[Any]:
            if req.model is not primary:
                self._signal(primary, req.model)
            return await handler(req)

        return await super().awrap_model_call(request, spy)

    @staticmethod
    def _signal(primary: Any, fallback: Any) -> None:
        signals.emit(
            signals.MODEL_FALLBACK,
            **{"from": model_label(primary), "to": model_label(fallback)},
        )


class SubagentTimeout(AgentMiddleware):
    """메인의 task 호출에 그 서브에이전트의 limits.turn_timeout_seconds 를 건다(P35 10절).

    서브에이전트는 메인의 도구 호출 하나로 도므로 메인의 턴 제한이 바깥 한도이고, 이 미들웨어가
    안쪽에서 서브의 한도를 따로 센다. 넘으면 서브의 그래프 실행이 취소되고 메인은 status="error"
    인 도구 결과를 받아 턴을 이어 간다. 런타임은 astream 만 쓰므로 동기 훅은 두지 않는다.
    """

    def __init__(self, timeouts: Mapping[str, float]) -> None:
        """timeouts 는 서브에이전트 이름마다의 턴 제한(초)이다."""
        super().__init__()
        self.timeouts = {name: float(seconds) for name, seconds in timeouts.items()}

    @property
    def name(self) -> str:
        return "DaegamSubagentTimeout"

    def _limit_for(self, call: ToolCall) -> tuple[str, float] | None:
        """이 호출이 아는 서브에이전트로 가는 task 면 (이름, 제한)이다. 모르는 이름은 deepagents 가
        거절 문구로 답하므로 여기서는 제한을 걸지 않는다."""
        if call["name"] != SUBAGENT_TOOL:
            return None
        sub = (call.get("args") or {}).get(SUBAGENT_TYPE_ARG)
        if not isinstance(sub, str) or sub not in self.timeouts:
            return None
        return sub, self.timeouts[sub]

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = effective_call(request)
        limit = self._limit_for(call)
        if limit is None:
            return await handler(request)
        sub, seconds = limit
        try:
            async with asyncio.timeout(seconds):
                return await handler(request)
        except TimeoutError:
            # 끊긴 task 의 끝 훅은 오지 않는다. 트레이서가 그 스팬을 timeout 으로 닫는다(P37)
            await signal(SUBAGENT_TIMEOUT, {"tool_call_id": call["id"]})
            return error_result(call, SUBAGENT_TIMEOUT_TEXT.format(name=sub, seconds=seconds))
