"""agent.json 의 model 객체를 provider kind 별 BaseChatModel 로 조립한다
(docs/guide/agents.md 3절 모델 표, D26).

anthropic (ChatAnthropic)
  name -> model, max_output_tokens -> max_tokens, effort -> anthropic_reasoning 의 이름 표
  (thinking·output_config·betas. 기본은 thinking={"type":"adaptive"} 와 output_config={"effort"},
  off 는 끌 수 있는 모델에서만 thinking={"type":"disabled"}), temperature 는 무시(SDK 2.x 가 보내는
  API 판 2026-10-21 이 모든 모델에서 400 으로 거부), base_url -> base_url.
openai (ChatOpenAI)
  name -> model, max_output_tokens -> max_completion_tokens, effort -> reasoning_effort(off 는 생략,
  max 는 xhigh), temperature -> temperature, base_url -> base_url, use_responses_api=False.
stub
  StubChatModel. 키가 없다.
키는 provider.env_key 가 가리키는 파드 env 에서만 읽는다. 게이트웨이 경로(`gateway: true`,
P38 5절)는 같은 env 가 90일 토큰을 가리킨다. 두 SDK 모두 그 값을 api_key 로 받아
`Authorization: Bearer` 로 보낸다(anthropic SDK 2.x 도 X-Api-Key 를 싣지 않는다). 그 경로의 연결
제한은 5초다(P38 10절). SDK 자체 재시도(max_retries)는 0 이다.
limits.retries 는 그래프의 ModelRetry 미들웨어가 센다(resilience.py). 호출 id 하나가 HTTP 요청
하나여야 하기 때문이기도 하다(P37 2절).

SDK 클라이언트는 상관 헤더 훅을 단 공유 클라이언트다(llm_http.py). openai 는 `http_async_client` 로
넘기고 anthropic 은 그 자리를 바꾼 `HookedChatAnthropic` 이다. 훅 단 클라이언트를 주면
langchain-openai 가 스트림 사용량을 스스로 켜지 않으므로 provider 의 `stream_usage`(없으면 켬)를
직접 준다. 모델의 metadata 에는 provider 이름·경로(`gateway` 표식)·역할을 실어 트레이서가
`model_call` 에 옮긴다(trace.py). 기동 검사 `correlation_hook_missing` 은 훅이 빠진 모델을 경고로
알리고 기동을 막지 않는다. 수는 그대로 맞고 잇기만 잃기 때문이다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

from langchain_core.language_models import BaseChatModel

from runtime import llm_http
from runtime.config import ConfigError
from runtime.stub import StubChatModel
from runtime.trace import PROVIDER_KEY, ROLE_KEY, ROLE_MAIN, ROUTE_DIRECT, ROUTE_GATEWAY, ROUTE_KEY

log = logging.getLogger("daegam.agent-runtime.models")

DEFAULT_CONTEXT_WINDOW_ENV = "DAEGAM_MODEL_CONTEXT_WINDOW"
DEFAULT_CONTEXT_WINDOW = 200_000
"""모델 프로필에 max_input_tokens 가 없을 때 fraction 압축 정책이 쓰는 값이다."""

OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
"""openai reasoning_effort 값이다. max 는 OpenAI 에 없어 xhigh 로 내린다."""

# GLM 모델이 reasoning_effort 로 받는 값이다(docs/guide/llm-providers.md "Z.AI and GLM").
# 키는 Z.AI 의 모델 이름 그대로이고, 값은 약한 쪽부터 센 쪽으로 늘어놓았다. 캔버스의 effort 가
# 이 값에 없으면 가장 가까운 값으로 보낸다(glm_reasoning_effort). 이 표에 없는 GLM 이름은 모른다
GLM_EFFORTS = {
    "glm-5.3": ("low", "high", "max"),
    "glm-5.2": ("none", "low", "medium", "high", "xhigh", "max"),
}
# 캔버스 effort 의 강도 순서다. off 는 순서 밖이고 따로 다룬다
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

# Claude 모델 이름의 앞머리 표다(P35 7절). 아래 상수와 anthropic_reasoning 은 비서 사본
# (platform/daegam/services/assistant/model.py)과 글자 그대로 같고
# platform/tests/test_assistant_model.py 가 맞댄다. langchain-anthropic 의 모델 프로필은 쓰지
# 않는다. 프로필마다 칸이 달라 칸이 없는 것과 모르는 모델을 가리지 못하고, 두 사본이 다른 판을
# 잠가 다른 프로필을 보기 때문이다.
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
    뺀다(플랫폼 assistant/model.py 와 같다)."""
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


# 상관 헤더 훅이 빠진 모델의 기동 경고다(P37 2절)
CORRELATION_HOOK_MISSING = "correlation_hook_missing"


def build_chat_model(
    model: dict[str, Any], *, role: str = ROLE_MAIN, stub_fail_first: int = 0
) -> BaseChatModel:
    """model 객체 하나를 BaseChatModel 로 만든다. 키는 env 에서만 읽고 값은 로그에 남기지 않는다.
    metadata 에 provider 이름·경로·역할(role: main, fallback, subagent)을 싣는다.

    stub_fail_first 는 스텁이 처음 n 번 실패하게 하는 실증용 훅이다. 메인 모델에만 건다.
    """
    chat = _build(model, stub_fail_first)
    provider = model["provider"]
    chat.metadata = {
        **(chat.metadata or {}),
        PROVIDER_KEY: provider["name"],
        ROUTE_KEY: ROUTE_GATEWAY if provider.get("gateway") else ROUTE_DIRECT,
        ROLE_KEY: role,
    }
    return chat


def _build(model: dict[str, Any], stub_fail_first: int) -> BaseChatModel:
    provider = model["provider"]
    kind = provider["kind"]
    params = model["params"]
    name = model["name"]

    if kind == "stub":
        return StubChatModel(model_name=name, fail_first=stub_fail_first)

    env_key = provider["env_key"]
    api_key = os.environ.get(env_key or "")
    if not api_key:
        raise ConfigError(
            "provider_key_missing",
            f"provider {provider['name']!r} 의 키 env {env_key!r} 가 비어 있다",
        )

    if kind == "anthropic":
        return _anthropic(name, params, provider, api_key)
    if kind == "openai":
        return _openai(name, params, provider, api_key)
    raise ConfigError("unknown_provider_kind", f"provider kind {kind!r} 는 모른다")


def _anthropic(
    name: str, params: dict[str, Any], provider: dict[str, Any], api_key: str
) -> BaseChatModel:
    gateway = bool(provider.get("gateway"))
    kwargs: dict[str, Any] = {
        "model": name,
        "max_tokens": params["max_output_tokens"],
        "api_key": api_key,
        "max_retries": 0,
        # 게이트웨이는 block_binding 과 그 베타를 그대로 넘기므로 Claude API 로 바로 가는 것과 같다
        **anthropic_reasoning(name, params["effort"], direct=gateway or not provider["base_url"]),
    }
    if params["temperature"] is not None:
        log.warning("anthropic 모델 %s 는 temperature 를 받지 않아 무시한다", name)
    if provider["base_url"]:
        kwargs["base_url"] = provider["base_url"]
    if gateway:
        kwargs["connect_timeout"] = llm_http.GATEWAY_CONNECT_SECONDS
    return _with_context_window(llm_http.HookedChatAnthropic(**kwargs))


def _openai(
    name: str, params: dict[str, Any], provider: dict[str, Any], api_key: str
) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": name,
        "max_completion_tokens": params["max_output_tokens"],
        "api_key": api_key,
        "use_responses_api": False,
        "max_retries": 0,
    }
    effort = params["effort"]
    if is_glm_model(name):
        # Z.AI 도 openai SDK 로 부르므로 agent.json 에는 kind openai 로 온다. 모델 이름으로 가른다
        mapped = glm_reasoning_effort(name, effort)
        if mapped is not None:
            if mapped != effort and effort != "off":
                log.warning("glm 모델 %s 의 effort %s 는 %s 로 보낸다", name, effort, mapped)
            kwargs["reasoning_effort"] = mapped
    elif effort != "off" and is_openai_reasoning_model(name):
        mapped = OPENAI_EFFORT[effort]
        if mapped != effort:
            log.warning("openai 모델 %s 의 effort %s 는 %s 로 보낸다", name, effort, mapped)
        kwargs["reasoning_effort"] = mapped
    elif effort != "off":
        log.warning(
            "openai 모델 %s 는 reasoning_effort 를 받지 않아 effort %s 를 무시한다", name, effort
        )
    if params["temperature"] is not None:
        kwargs["temperature"] = params["temperature"]
    if provider["base_url"]:
        kwargs["base_url"] = provider["base_url"]
    if provider.get("gateway"):
        kwargs["timeout"] = llm_http.gateway_timeout()
    # 게이트웨이에 실을 본문(OpenRouter 의 업체 선호 등). 플랫폼이 정하고 런타임은 그대로 넘긴다
    if provider.get("extra_body"):
        kwargs["extra_body"] = provider["extra_body"]
    # 인자 표 밖의 전송 설정이다. 표는 비서 사본과 맞대므로 섞지 않는다
    chat = ChatOpenAI(**kwargs, http_async_client=llm_http.hooked_async_client())
    chat.stream_usage = provider.get("stream_usage", True)
    return _with_context_window(chat)


def correlation_warnings(models: Iterable[BaseChatModel | None]) -> list[str]:
    """SDK 클라이언트에 상관 헤더 훅이 없는 모델의 기동 경고다. 스텁처럼 HTTP 를 쓰지 않는 모델은
    보지 않는다."""
    missing = [model_identifier(m) for m in models if llm_http.lacks_correlation_hook(m)]
    return [f"{CORRELATION_HOOK_MISSING}: {', '.join(missing)}"] if missing else []


def _with_context_window(model: BaseChatModel) -> BaseChatModel:
    """fraction 압축 정책은 model.profile.max_input_tokens 가 있어야 한다.

    langchain 이 모르는 모델 이름이면 프로필이 비어 있으므로 기본 창을 넣고 경고한다.
    """
    profile = model.profile
    if isinstance(profile, dict) and isinstance(profile.get("max_input_tokens"), int):
        return model
    window = _default_context_window(os.environ.get(DEFAULT_CONTEXT_WINDOW_ENV))
    log.warning(
        "model_profile_unknown: %s 의 컨텍스트 창을 모른다. %d 토큰으로 가정한다",
        model_identifier(model),
        window,
    )
    merged = dict(profile) if isinstance(profile, dict) else {}
    merged["max_input_tokens"] = window
    model.profile = merged
    return model


def _default_context_window(raw: str | None) -> int:
    """env 값을 기본 컨텍스트 창으로 읽는다. 비었거나 1 이상의 정수가 아니면 기본값이다.

    잘못 적은 값(`128k` 등)으로 compile_agent 가 맨 ValueError 를 내며 기동이 죽지 않게 한다.
    """
    if not raw:
        return DEFAULT_CONTEXT_WINDOW
    try:
        window = int(raw)
    except ValueError:
        window = 0
    if window >= 1:
        return window
    log.warning("%s=%r 가 1 이상의 정수가 아니라 기본값을 쓴다", DEFAULT_CONTEXT_WINDOW_ENV, raw)
    return DEFAULT_CONTEXT_WINDOW


def model_identifier(model: BaseChatModel) -> str:
    from deepagents._models import get_model_identifier

    return get_model_identifier(model) or type(model).__name__


def harness_key(model: BaseChatModel) -> str:
    """deepagents 가 pre-built 모델에서 harness profile 을 찾는 `provider:model` 키다.

    첫 `:` 만 구분자다. 모델 쪽의 `:`(`ft:gpt-4o-mini:…` 같은 파인튜닝 이름)는 키에 그대로 들어가고
    deepagents 는 그 키를 글자 그대로 맞춘다(0.7.20 `profiles/_keys.py`).
    """
    from deepagents._models import get_model_identifier, get_model_provider

    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if not provider or not identifier:
        raise ConfigError(
            "harness_key",
            f"{type(model).__name__} 의 provider·model 을 알 수 없어 harness profile 을 못 건다",
        )
    return f"{provider}:{identifier}"
