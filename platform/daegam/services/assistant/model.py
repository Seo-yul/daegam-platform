"""provider 설정으로 비서의 채팅 모델을 만든다 (D31, P14 F1b).

인자 표는 `agent-runtime/runtime/models.py` 의 `_anthropic`·`_openai` 를 글자 그대로 옮긴 것이다.
두 파일은 lock 이 달라 함수를 공유하지 못하므로 `tests/test_assistant_model.py` 가 같은 params
로 두 쪽의 kwargs 가 같음을 확인한다(D27 이 `oidc.py` 를 바이트 비교로 지키는 것과 같은 방식).

anthropic (ChatAnthropic)
  model, max_tokens=max_output_tokens, api_key, max_retries=0. 추론 인자는 `anthropic_reasoning`
  의 이름 표다(기본은 thinking adaptive 와 output_config={"effort"}, 끌 수 있는 모델의 off 만
  thinking disabled, base_url 이 없거나 게이트웨이 경로면 block_binding 과 그 베타). base_url 은
  있을 때만.
openai (ChatOpenAI)
  model, max_completion_tokens=max_output_tokens, api_key, use_responses_api=False, max_retries=0.
  effort off 면 reasoning_effort 없음, 아니면 OPENAI_EFFORT 대응값(max 는 xhigh). GLM 모델
  (glm-5.3, glm-5.2)은 `glm_reasoning_effort` 가 판마다 받는 값으로 바꾼다. base_url 은 있을 때만.
temperature 는 두지 않는다. 주소와 키는 호출자가 고른 `Endpoint`(`services/llm_endpoint.py`)다.
프롬프트 캐시 표지(`prompt_cache`)는 인자 표 밖이다. 턴 루프가 Claude 호출에만 싣는다 (T80).
직접 경로의 키는 플랫폼 네임스페이스 Secret 에서만 읽고(D25), 게이트웨이 경로는 자리표시 키와
요청마다 토큰을 싣는 훅이다(P38 5절). `kind: stub` 은 상태 없는 `AssistantStub` 이다.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from daegam import llm_http
from daegam.services.assistant.stub import AssistantStub
from daegam.services.llm_endpoint import Endpoint
from daegam.services.llm_providers import ProviderError
from daegam.store.settings import Effort, LLMProvider

log = logging.getLogger("daegam.assistant.model")

DEFAULT_MAX_OUTPUT_TOKENS = 8192

OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
"""openai reasoning_effort 값이다. max 는 OpenAI 에 없어 xhigh 로 내린다."""

# GLM 모델이 reasoning_effort 로 받는 값이다. 아래 표와 두 함수는 런타임 원본과 글자 그대로 같고
# tests/test_assistant_model.py 가 맞댄다 (docs/guide/llm-providers.md "Z.AI and GLM")
GLM_EFFORTS = {
    "glm-5.3": ("low", "high", "max"),
    "glm-5.2": ("none", "low", "medium", "high", "xhigh", "max"),
}
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

# Claude 모델 이름의 앞머리 표다(P35 7절). 아래 상수와 anthropic_reasoning 은 런타임 원본
# (agent-runtime/runtime/models.py)과 글자 그대로 같고 tests/test_assistant_model.py 가 맞댄다.
# 비서는 effort 를 low 아래로 두지 않으므로(store/settings.py 의 AssistantEffort) off 행은 안 탄다.
DISABLED_THINKING_PREFIXES = ("claude-3", "claude-haiku-4", "claude-sonnet-4", "claude-opus-4")
"""thinking={"type": "disabled"} 를 받는 모델의 앞머리다. 닫힌 목록이라 모르는 이름은 못 끈다."""
DISABLED_THINKING_NAMES = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5")
"""이 이름 그대로이거나 날짜만 붙은 이름도 disabled 를 받는다. Opus 5.5·Sonnet 5.5 는 아니고
Haiku 5.5 는 받는다(P43 F0 5번 기록)."""
DATE_SUFFIX = "-20"
"""`claude-opus-5-20260301` 처럼 날짜가 붙은 이름에서 날짜 꼬리가 시작하는 글자다."""
NO_REASONING_PREFIXES = (
    "claude-3",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2025",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4-0",
    "claude-opus-4-2025",
)
"""adaptive thinking 을 받지 않는 4.5 세대와 그 이전의 앞머리다. thinking 도 effort 도 안 보낸다."""
NO_XHIGH_PREFIXES = ("claude-opus-4-6", "claude-sonnet-4-6")
"""xhigh 가 없는 4.6 세대의 앞머리다. xhigh 는 high 로 내린다."""
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
"""block_binding 을 여는 베타다. adaptive 를 Claude API 로 바로 보낼 때만 싣는다."""
BLOCK_BINDING = {"prefix_mismatch_behavior": "drop_block"}
"""만든 대화와 어긋난 thinking 블록과 그 뒤를 버리고 요청을 받게 한다. 압축·재배포로 이력이
바뀐 세션이 400 대신 살고 앞 추론의 이음만 잃는다."""


def _accepts_disabled_thinking(name: str) -> bool:
    """thinking 을 끌 수 있는 모델인가. 소문자 이름을 받는다.

    닫힌 목록이다. 새 세대(Opus 5.5, Sonnet 5.5, Fable)는 끄지 못하므로 모르는 이름은 False 다.
    Haiku 5.5 는 끌 수 있다."""
    if name.startswith(DISABLED_THINKING_PREFIXES):
        return True
    return any(
        name == base or name.startswith(base + DATE_SUFFIX) for base in DISABLED_THINKING_NAMES
    )


def anthropic_reasoning(name: str, effort: str, *, direct: bool) -> dict[str, Any]:
    """Claude 모델의 추론 인자다. 모델 이름의 앞머리 표로 thinking·output_config·betas 를 정한다.

    사람이 적은 이름이라 어느 모델이든 온다. 끄지 못하는 모델의 off 는 adaptive 의 low 로,
    4.5 세대와 그 이전의 off 아닌 effort 는 안 보내는 것으로, 4.6 세대의 xhigh 는 high 로
    내리고 경고한다. direct 는 요청이 Claude API 로 바로 가는지다. adaptive 를 바로 보낼 때만
    block_binding 과 그 베타를 싣는다. 프록시가 그 베타를 받는지 모르기 때문이다. 비서의
    anthropic_kwargs 와 규칙이 같다.
    """
    lower = name.lower()
    if effort == "off":
        if _accepts_disabled_thinking(lower):
            return {"thinking": {"type": "disabled"}}
        log.warning("anthropic 모델 %s 는 thinking 을 못 꺼 off 를 adaptive 의 low 로 보낸다", name)
        return _adaptive_thinking("low", direct=direct)
    if lower.startswith(NO_REASONING_PREFIXES):
        log.warning("anthropic 모델 %s 는 adaptive 가 없어 effort %s 를 안 보낸다", name, effort)
        return {}
    if effort == "xhigh" and lower.startswith(NO_XHIGH_PREFIXES):
        log.warning("anthropic 모델 %s 는 xhigh 가 없어 high 로 보낸다", name)
        return _adaptive_thinking("high", direct=direct)
    return _adaptive_thinking(effort, direct=direct)


def _adaptive_thinking(effort: str, *, direct: bool) -> dict[str, Any]:
    thinking: dict[str, Any] = {"type": "adaptive"}
    kwargs: dict[str, Any] = {"thinking": thinking, "output_config": {"effort": effort}}
    if direct:
        thinking["block_binding"] = dict(BLOCK_BINDING)
        kwargs["betas"] = [THINKING_BINDING_BETA]
    return kwargs


def is_openai_reasoning_model(name: str) -> bool:
    """reasoning_effort 를 받는 OpenAI 모델인가. gpt-5 계열(gpt-5-chat 제외)과 o 시리즈다.

    gpt-4.1 같은 모델에 보내면 400 unsupported_parameter 인데 langchain 이 걸러 주지 않아 여기서
    뺀다(런타임 models.py 와 같다)."""
    lower = name.lower()
    if lower.startswith("gpt-5-chat"):
        return False
    return lower.startswith(("gpt-5", "o1", "o3", "o4"))


def glm_reasoning_effort(name: str, effort: str) -> str | None:
    """GLM 모델에 보낼 reasoning_effort 다. 모르는 GLM 이름이거나 보낼 값이 없으면 None 이다.

    effort off 는 끌 수 있는 모델(glm-5.2)에서 none 이고, 끌 수 없는 모델(glm-5.3)에서는
    가장 약한 값(low)이다. Z.AI 는 thinking 을 끄던 호출에 low 를 권하고, 보내지 않으면
    기본값 max 로 돈다. 그 밖의 값이 모델이 받는 값에 없으면 강도가 가장 가까운 값이다.
    같은 거리에 둘이 있으면 센 쪽이다(medium 은 high, xhigh 는 max)."""
    supported = GLM_EFFORTS.get(name.lower())
    if supported is None:
        return None
    levels = [e for e in supported if e != "none"]
    if effort == "off":
        return "none" if "none" in supported else levels[0]
    rank = EFFORT_ORDER.index(effort)
    return min(levels, key=lambda e: (abs(EFFORT_ORDER.index(e) - rank), -EFFORT_ORDER.index(e)))


def is_glm_model(name: str) -> bool:
    """reasoning_effort 값을 아는 GLM 모델인가. Z.AI 의 이름 그대로여야 한다."""
    return name.lower() in GLM_EFFORTS


PROMPT_CACHE: dict[str, str] = {"type": "ephemeral"}
"""Claude 프롬프트 캐시 표지다 (T80). 5분 TTL 이다. 한 턴의 호출은 몇 초 간격이라 읽을 때마다
수명이 다시 늘어난다. 쓰기 값이 두 배인 1시간 TTL 은 턴 사이가 5분을 넘을 때만 이득이다."""


def prompt_cache(model: BaseChatModel) -> dict[str, str] | None:
    """턴 루프가 이 모델의 호출에 실을 캐시 표지다. Claude 만 표지를 받는다. 직접 경로와 게이트웨이
    경로가 같고(게이트웨이는 본문을 그대로 넘긴다), base_url 을 둔 provider(프록시)도 같은 표지를
    받는다. 런타임의 캐시 미들웨어가 같은 provider 로 같은 필드를 보내므로, 그 필드를 거절하는
    프록시는 두 쪽 모두 실패한다. 다른 provider 는 None 이다. OpenAI 계열은 표지 없이 스스로
    캐시하고, 스텁은 캐시가 없다."""
    return dict(PROMPT_CACHE) if isinstance(model, ChatAnthropic) else None


def anthropic_kwargs(
    name: str,
    *,
    max_output_tokens: int,
    effort: str,
    api_key: str,
    base_url: str | None,
    gateway: bool = False,
) -> dict[str, Any]:
    """gateway 는 게이트웨이 경로인가다 (P38 F5). 게이트웨이는 block_binding 과 그 베타를 그대로
    넘기므로 Claude API 로 바로 가는 것과 같고, 연결 제한은 5초다."""
    kwargs: dict[str, Any] = {
        "model": name,
        "max_tokens": max_output_tokens,
        "api_key": api_key,
        "max_retries": 0,
        **anthropic_reasoning(name, effort, direct=gateway or not base_url),
    }
    if base_url:
        kwargs["base_url"] = base_url
    if gateway:
        kwargs["connect_timeout"] = llm_http.GATEWAY_CONNECT_SECONDS
    return kwargs


def openai_kwargs(
    name: str,
    *,
    max_output_tokens: int,
    effort: str,
    api_key: str,
    base_url: str | None,
    extra_body: dict[str, Any] | None = None,
    gateway: bool = False,
) -> dict[str, Any]:
    """gateway 는 게이트웨이 경로인가다. 그 경로의 연결 제한은 5초다 (P38 10절)."""
    kwargs: dict[str, Any] = {
        "model": name,
        "max_completion_tokens": max_output_tokens,
        "api_key": api_key,
        "use_responses_api": False,
        "max_retries": 0,
    }
    if is_glm_model(name):
        glm_mapped = glm_reasoning_effort(name, effort)
        if glm_mapped is not None:
            if glm_mapped != effort and effort != "off":
                log.warning("glm 모델 %s 의 effort %s 는 %s 로 보낸다", name, effort, glm_mapped)
            kwargs["reasoning_effort"] = glm_mapped
    elif effort != "off" and is_openai_reasoning_model(name):
        mapped = OPENAI_EFFORT[effort]
        if mapped != effort:
            log.warning("openai 모델 %s 의 effort %s 는 %s 로 보낸다", name, effort, mapped)
        kwargs["reasoning_effort"] = mapped
    if base_url:
        kwargs["base_url"] = base_url
    if gateway:
        kwargs["timeout"] = llm_http.gateway_timeout()
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def build_chat_model(
    provider: LLMProvider,
    *,
    effort: Effort,
    endpoint: Endpoint,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> BaseChatModel:
    """설정 한 줄과 그 턴의 길(`LlmEndpoints.resolve_endpoint`)에서 채팅 모델을 만든다. 요청마다
    토큰을 싣는 훅(`bearer`)은 인자 표 밖이다. 런타임은 토큰을 env 에서 읽기 때문이다."""
    if provider.is_stub:
        return AssistantStub()
    name = provider.effective_model
    table = {
        "max_output_tokens": max_output_tokens,
        "effort": effort,
        "api_key": endpoint.api_key,
        "base_url": endpoint.base_url,
        "gateway": endpoint.gateway,
    }
    if provider.sdk_kind == "anthropic":
        # 상관 헤더 훅을 단 클라이언트를 쓰는 하위 클래스다 (P37 2절)
        return llm_http.HookedChatAnthropic(
            **anthropic_kwargs(name, **table), bearer=endpoint.bearer
        )
    if provider.sdk_kind == "openai":
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            **openai_kwargs(name, extra_body=provider.extra_body, **table),
            # 상관 헤더 훅 (P37 2절). 훅 단 클라이언트를 주면 langchain-openai 가 stream_usage 를
            # 스스로 켜지 않는다. 게이트웨이 경로는 요청마다 토큰을 싣는 훅도 단다 (P38 5절)
            http_async_client=llm_http.hooked_async_client(
                base_url=endpoint.base_url if endpoint.bearer else None, bearer=endpoint.bearer
            ),
        )
        # 인자 표 밖의 스트리밍 설정이다. 이것이 없으면 `astream` 의 마지막 청크에 usage 가 실리지
        # 않아 세션 usage 가 0 으로 센다. stream_options 를 거절하는 호환 서버는 provider 의
        # stream_usage 를 끈다 (P37 2절). 표와 따로 두어 kwargs 동일성 검사에 섞이지 않게 한다
        model.stream_usage = provider.stream_usage
        return model
    raise ProviderError("unknown_kind", f"unsupported provider kind {provider.kind!r}")


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "OPENAI_EFFORT",
    "PROMPT_CACHE",
    "anthropic_kwargs",
    "anthropic_reasoning",
    "build_chat_model",
    "openai_kwargs",
    "prompt_cache",
]
