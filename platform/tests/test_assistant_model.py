"""P14 F1b: 비서 모델 인자 표.

`agent-runtime/runtime/models.py` 의 `_anthropic`·`_openai` 와 플랫폼의 `anthropic_kwargs`·
`openai_kwargs` 가 같은 params 로 같은 kwargs 를 만든다. 런타임 패키지는 deepagents 등을 끌어와
여기서 import 할 수 없으므로 파일에서 표 함수와 상수만 뽑아 실행한다(D27 이 `oidc.py` 를 바이트
비교로 지키는 것과 같은 취지). Claude 모델의 추론 인자(P35 7절)는 `anthropic_reasoning` 과 이름 표
상수가 두 사본에서 글자 그대로 같고, 모델·effort·direct 격자에서 같은 인자를 낸다. 크레딧 소진
판정(`quota_exhausted`, T20)은 두 이미지가 바이트 그대로 나눠 가지는 llm_http.py 에 있고
(`test_llm_http_copy.py`), 여기서는 실제 SDK 예외에 대한 답을 본다.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import langchain_anthropic
import langchain_openai
import openai
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from daegam import llm_http
from daegam.services.assistant import model as assistant_model
from daegam.services.assistant.model import (
    BLOCK_BINDING,
    DEFAULT_MAX_OUTPUT_TOKENS,
    OPENAI_EFFORT,
    THINKING_BINDING_BETA,
    anthropic_kwargs,
    anthropic_reasoning,
    build_chat_model,
    openai_kwargs,
    prompt_cache,
)
from daegam.services.assistant.stub import AssistantStub
from daegam.services.llm_endpoint import PLACEHOLDER_KEY, Endpoint, direct_endpoint
from daegam.services.llm_providers import ProviderError
from daegam.store.settings import LLMProvider, ProviderRouting
from tests.helpers.consts import PLATFORM_NS
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.source import AGENT_RUNTIME, exec_excerpt

RUNTIME_MODELS = AGENT_RUNTIME / "models.py"
# 런타임 사본이 받는 상관 헤더 클라이언트 자리다
HOOKED_CLIENT = object()
EFFORTS = ("off", "low", "medium", "high", "xhigh", "max")
# 런타임에서 뽑아 실행하는 표 함수다. 아래 상수 표는 이름이 대문자인 모듈 변수 전부다
RUNTIME_FUNCTIONS = (
    "_anthropic",
    "_openai",
    "is_openai_reasoning_model",
    "is_glm_model",
    "glm_reasoning_effort",
    "anthropic_reasoning",
    "_accepts_disabled_thinking",
    "_adaptive_thinking",
)
# 두 사본이 글자 그대로 같아야 하는 Claude 이름 표다
REASONING_TABLE = (
    "DISABLED_THINKING_PREFIXES",
    "DISABLED_THINKING_NAMES",
    "DATE_SUFFIX",
    "NO_REASONING_PREFIXES",
    "NO_XHIGH_PREFIXES",
    "THINKING_BINDING_BETA",
    "BLOCK_BINDING",
    "GLM_EFFORTS",
    "EFFORT_ORDER",
)
# 앞머리 표(P35 7절)가 이름을 가르는 갈래마다 하나다. 이름마다 무엇을 보내는지는
# test_anthropic_reasoning_rows 가 못 박으므로, 격자는 두 사본이 갈래마다 같은 kwargs 를 내는지만
# 본다. 날짜가 붙은 이름은 꼴만 본 것이라 실제 출시일이 아니다
CLAUDE_MODELS = (
    "claude-haiku-4-5-20251001",  # 4.5 세대와 그 이전은 off 가 아니면 추론 인자가 없다
    "claude-opus-4-6",  # 4.6 세대는 xhigh 를 high 로 내린다
    "claude-opus-4-7",  # 끌 수 있는 앞머리의 나머지는 effort 를 그대로 보낸다
    "claude-opus-5",  # 이름 그대로면 끌 수 있다
    "claude-sonnet-5-20260301",  # 이름에 날짜만 붙어도 끌 수 있다
    "claude-opus-5-5",  # 끌 수 있는 이름으로 시작해도 끄지 못한다
    "claude-x",  # 모르는 이름은 끄지 못한다
)
ADAPTIVE_DIRECT = {"type": "adaptive", "block_binding": BLOCK_BINDING}
DISABLED = {"thinking": {"type": "disabled"}}


def _adaptive(effort: str) -> dict[str, Any]:
    """프록시 뒤(direct=False)의 adaptive 인자다."""
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


class Recorder:
    """ChatAnthropic·ChatOpenAI 자리에 서서 생성자 kwargs 만 기억한다."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class HookedRecorder(Recorder):
    """ChatOpenAI 자리. 인자 표 밖의 상관 헤더 클라이언트(P37 2절)는 따로 기억한다."""

    def __init__(self, **kwargs: Any) -> None:
        self.http_async_client = kwargs.pop("http_async_client")
        super().__init__(**kwargs)


@pytest.fixture(scope="module")
def runtime() -> dict[str, Any]:
    """표 함수와 이름이 대문자인 모듈 변수다."""
    scope = {
        "Any": Any,
        "BaseChatModel": object,
        "log": logging.getLogger("runtime-excerpt"),
        "_with_context_window": lambda m: m,
        # 전송 설정(P37 2절)은 인자 표 밖이다. 상관 헤더 클라이언트는 HookedRecorder 가 따로 든다
        "llm_http": SimpleNamespace(
            HookedChatAnthropic=Recorder,
            hooked_async_client=lambda: HOOKED_CLIENT,
            GATEWAY_CONNECT_SECONDS=llm_http.GATEWAY_CONNECT_SECONDS,
            gateway_timeout=llm_http.gateway_timeout,
        ),
    }
    return exec_excerpt(RUNTIME_MODELS, scope, functions=RUNTIME_FUNCTIONS, constants=str.isupper)


def _params(effort: str) -> dict[str, Any]:
    return {"max_output_tokens": 4096, "effort": effort, "temperature": None}


def test_reasoning_table_is_the_same_in_both_copies(runtime):
    """이름 표는 두 사본이 글자 그대로 같다. 한쪽만 고치면 여기서 잡힌다."""
    for name in REASONING_TABLE:
        assert getattr(assistant_model, name) == runtime[name], name
    assert runtime["OPENAI_EFFORT"] == OPENAI_EFFORT


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize(
    ("base_url", "gateway"),
    [
        (None, False),
        ("https://gw.example.com/v1", False),
        ("http://llm-gateway.daegam-llm.svc.cluster.local:8080/p/claude", True),
    ],
)
def test_anthropic_kwargs_match_runtime(runtime, monkeypatch, model, effort, base_url, gateway):
    """모델·effort·경로 격자에서 런타임 `_anthropic` 과 비서 `anthropic_kwargs` 가 같다. 게이트웨이
    경로는 block_binding 을 그대로 넘기므로 Claude API 로 바로 가는 것과 같고 연결 제한이 붙는다
    (P38 F5). 키 자리도 같다. 런타임은 게이트웨이 경로에서 env 의 토큰을 api_key 로 준다."""
    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", Recorder)
    provider = {
        "name": "p",
        "kind": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": base_url,
        **({"gateway": True} if gateway else {}),
    }
    theirs = runtime["_anthropic"](model, _params(effort), provider, "sk-a").kwargs
    ours = anthropic_kwargs(
        model,
        max_output_tokens=4096,
        effort=effort,
        api_key="sk-a",
        base_url=base_url,
        gateway=gateway,
    )
    assert ours == theirs
    direct = gateway or base_url is None
    assert ours == {
        "model": model,
        "max_tokens": 4096,
        "api_key": "sk-a",
        "max_retries": 0,
        **anthropic_reasoning(model, effort, direct=direct),
        **({"base_url": base_url} if base_url else {}),
        **({"connect_timeout": llm_http.GATEWAY_CONNECT_SECONDS} if gateway else {}),
    }
    assert "temperature" not in ours
    # block_binding 과 그 베타는 adaptive 를 Claude API 나 게이트웨이로 보낼 때만 실린다
    thinking = ours.get("thinking")
    direct_adaptive = direct and thinking is not None and thinking["type"] == "adaptive"
    assert ("betas" in ours) is direct_adaptive
    assert (thinking == ADAPTIVE_DIRECT) is direct_adaptive
    if direct_adaptive:
        assert ours["betas"] == [THINKING_BINDING_BETA]


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        # 끌 수 있는 모델의 off 는 disabled 다
        ("claude-opus-5", "off", DISABLED),
        ("claude-sonnet-5-20260301", "off", DISABLED),
        ("claude-opus-4-6", "off", DISABLED),
        ("claude-haiku-4-5-20251001", "off", DISABLED),
        ("claude-3-7-sonnet-latest", "off", DISABLED),
        ("claude-haiku-5-5", "off", DISABLED),
        ("claude-haiku-5-5-20261001", "off", DISABLED),
        # 끄지 못하는 모델과 모르는 이름의 off 는 adaptive 의 low 다
        ("claude-opus-5-5", "off", _adaptive("low")),
        ("claude-sonnet-5-5", "off", _adaptive("low")),
        ("claude-fable-5-1", "off", _adaptive("low")),
        ("claude-x", "off", _adaptive("low")),
        # 4.5 세대와 그 이전은 off 가 아니면 thinking 도 effort 도 없다
        ("claude-haiku-4-5-20251001", "medium", {}),
        ("claude-sonnet-4-5", "high", {}),
        ("claude-opus-4-5", "max", {}),
        ("claude-opus-4-1", "low", {}),
        ("claude-opus-4-20250514", "low", {}),
        ("claude-sonnet-4-0", "low", {}),
        ("claude-3-7-sonnet-latest", "low", {}),
        # 4.6 세대는 xhigh 가 없어 high 다. max 는 그대로다
        ("claude-opus-4-6", "xhigh", _adaptive("high")),
        ("claude-sonnet-4-6", "xhigh", _adaptive("high")),
        ("claude-sonnet-4-6", "max", _adaptive("max")),
        # 그 밖(모르는 이름 포함)은 adaptive 와 그 effort 다
        ("claude-opus-4-7", "xhigh", _adaptive("xhigh")),
        ("claude-opus-5-5", "max", _adaptive("max")),
        ("claude-x", "high", _adaptive("high")),
    ],
)
def test_anthropic_reasoning_rows(model, effort, expected):
    """7절 표의 행이다. 직행(direct=True)은 adaptive 에 block_binding 과 베타를 더한다."""
    assert anthropic_reasoning(model, effort, direct=False) == expected
    direct = anthropic_reasoning(model, effort, direct=True)
    if expected.get("thinking", {}).get("type") == "adaptive":
        assert direct == {**expected, "thinking": ADAPTIVE_DIRECT, "betas": [THINKING_BINDING_BETA]}
    else:
        assert direct == expected


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("base_url", [None, "https://gw.example.com/v1"])
@pytest.mark.parametrize("extra_body", [None, {"provider": {"order": ["moonshotai"]}}])
@pytest.mark.parametrize("gateway", [False, True])
def test_openai_kwargs_match_runtime(runtime, monkeypatch, effort, base_url, extra_body, gateway):
    """게이트웨이 경로(P38 F5)는 두 사본 모두 연결 제한 5초를 싣는다."""
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", HookedRecorder)
    provider = {"name": "p", "kind": "openai", "env_key": "OPENAI_API_KEY", "base_url": base_url}
    if extra_body:
        provider["extra_body"] = extra_body
    if gateway:
        provider["gateway"] = True
    theirs = runtime["_openai"]("gpt-5-mini", _params(effort), provider, "sk-o").kwargs
    ours = openai_kwargs(
        "gpt-5-mini",
        max_output_tokens=4096,
        effort=effort,
        api_key="sk-o",
        base_url=base_url,
        extra_body=extra_body,
        gateway=gateway,
    )
    assert ours == theirs
    assert ours.get("timeout") == (llm_http.gateway_timeout() if gateway else None)
    if effort == "off":
        assert "reasoning_effort" not in ours
    else:
        assert ours["reasoning_effort"] == OPENAI_EFFORT[effort]
    # 추론 모델이 아니면 두 표 모두 reasoning_effort 를 보내지 않는다
    plain_theirs = runtime["_openai"]("gpt-4.1-mini", _params(effort), provider, "sk-o").kwargs
    plain_ours = openai_kwargs(
        "gpt-4.1-mini",
        max_output_tokens=4096,
        effort=effort,
        api_key="sk-o",
        base_url=base_url,
        extra_body=extra_body,
        gateway=gateway,
    )
    assert plain_ours == plain_theirs and "reasoning_effort" not in plain_ours
    assert ours["use_responses_api"] is False and ours["max_retries"] == 0
    assert "temperature" not in ours


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize(
    "model", ["glm-5.3", "glm-5.2", "GLM-5.3", "glm-4.7", "glm-5.3-air", "z-ai/glm-5.3"]
)
def test_glm_openai_kwargs_match_runtime(runtime, monkeypatch, model, effort):
    """GLM 이름이면 두 사본이 같은 reasoning_effort 를 낸다. 표에 없는 이름(다른 판, 날짜나 -air 가
    붙은 이름, OpenRouter 의 z-ai/ 이름)은 보내지 않는다."""
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", HookedRecorder)
    provider = {"name": "p", "kind": "openai", "env_key": "OPENAI_API_KEY", "base_url": None}
    theirs = runtime["_openai"](model, _params(effort), provider, "sk-o").kwargs
    ours = openai_kwargs(
        model, max_output_tokens=4096, effort=effort, api_key="sk-o", base_url=None
    )
    assert ours == theirs
    known = model.lower() in ("glm-5.3", "glm-5.2")
    assert ("reasoning_effort" in ours) is known
    if model.lower() == "glm-5.3" and effort == "off":
        # glm-5.3 은 끌 수 없어 off 는 가장 약한 low 다 (Z.AI 는 보내지 않으면 max 로 돈다)
        assert ours["reasoning_effort"] == "low"


def test_real_classes_accept_the_kwargs():
    """표의 이름이 실제 langchain 필드다. 네트워크는 쓰지 않는다."""
    a = ChatAnthropic(
        **anthropic_kwargs(
            "claude-opus-5", max_output_tokens=10, effort="high", api_key="k", base_url="http://a"
        )
    )
    assert a.thinking == {"type": "adaptive"} and a.output_config == {"effort": "high"}
    assert a.betas is None
    assert a.max_tokens == 10 and a.max_retries == 0 and a.anthropic_api_url == "http://a"
    a = ChatAnthropic(
        **anthropic_kwargs(
            "claude-opus-5", max_output_tokens=10, effort="high", api_key="k", base_url=None
        )
    )
    assert a.thinking == ADAPTIVE_DIRECT and a.betas == [THINKING_BINDING_BETA]
    a = ChatAnthropic(
        **anthropic_kwargs(
            "claude-opus-5", max_output_tokens=10, effort="off", api_key="k", base_url=None
        )
    )
    assert a.thinking == {"type": "disabled"} and a.output_config is None and a.betas is None
    a = ChatAnthropic(
        **anthropic_kwargs(
            "claude-haiku-4-5", max_output_tokens=10, effort="high", api_key="k", base_url=None
        )
    )
    assert a.thinking is None and a.output_config is None and a.betas is None
    o = ChatOpenAI(
        **openai_kwargs(
            "gpt-5-mini", max_output_tokens=10, effort="max", api_key="k", base_url="http://o"
        )
    )
    assert o.max_tokens == 10 and o.reasoning_effort == "xhigh" and o.use_responses_api is False
    assert o.max_retries == 0 and o.openai_api_base == "http://o" and o.temperature is None
    o = ChatOpenAI(
        **openai_kwargs("gpt-x", max_output_tokens=10, effort="off", api_key="k", base_url=None)
    )
    assert o.reasoning_effort is None and o.extra_body is None
    body = {"provider": {"order": ["moonshotai"]}}
    o = ChatOpenAI(
        **openai_kwargs(
            "gpt-x", max_output_tokens=10, effort="off", api_key="k", base_url=None, extra_body=body
        )
    )
    assert o.extra_body == body


def _direct(provider: LLMProvider, ss: FakeSecretStore | None) -> Endpoint:
    return direct_endpoint(provider, ss, namespace=PLATFORM_NS)


def test_build_chat_model_reads_the_secret_and_routes_by_kind(monkeypatch):
    ss = FakeSecretStore()
    ss.put("llm-keys", {"api_key": "sk-secret"}, namespace=PLATFORM_NS)
    monkeypatch.setattr(llm_http, "HookedChatAnthropic", Recorder)
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", HookedRecorder)
    anthropic = LLMProvider(name="a", kind="anthropic", api_key_secret="llm-keys")
    made = build_chat_model(anthropic, effort="low", endpoint=_direct(anthropic, ss))
    assert made.kwargs.pop("bearer") is None
    assert made.kwargs == anthropic_kwargs(
        "claude-opus-5",
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        effort="low",
        api_key="sk-secret",
        base_url=None,
    )
    openai = LLMProvider(
        name="o", kind="openai", model="gpt-5", api_key_secret="llm-keys", base_url="http://gw/v1"
    )
    made = build_chat_model(
        openai, effort="off", endpoint=_direct(openai, ss), max_output_tokens=100
    )
    assert made.kwargs == openai_kwargs(
        "gpt-5", max_output_tokens=100, effort="off", api_key="sk-secret", base_url="http://gw/v1"
    )
    # 스트리밍 usage 와 상관 헤더 클라이언트는 표 밖에서 준다
    assert made.stream_usage is True
    assert llm_http.has_correlation_hook(made.http_async_client)
    # stream_options 를 거절하는 호환 서버는 provider 의 stream_usage 를 끈다 (P37 2절)
    quiet = openai.model_copy(update={"stream_usage": False})
    made = build_chat_model(quiet, effort="off", endpoint=_direct(quiet, ss))
    assert made.stream_usage is False
    # openrouter 는 ChatOpenAI 로 만들고 주소와 모델은 kind 기본이다
    router = LLMProvider(name="r", kind="openrouter", api_key_secret="llm-keys")
    made = build_chat_model(router, effort="low", endpoint=_direct(router, ss))
    assert made.kwargs == openai_kwargs(
        "moonshotai/kimi-k3",
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        effort="low",
        api_key="sk-secret",
        base_url="https://openrouter.ai/api/v1",
    )
    assert made.stream_usage is True
    # 업체 선호가 있으면 같은 본문을 싣는다 (T19)
    routed = router.model_copy(update={"routing": ProviderRouting(require_parameters=True)})
    made = build_chat_model(routed, effort="low", endpoint=_direct(routed, ss))
    assert made.kwargs["extra_body"] == {"provider": {"require_parameters": True}}
    # stub 은 키 없이 AssistantStub
    stub = LLMProvider(name="s", kind="stub")
    made = build_chat_model(stub, effort="off", endpoint=_direct(stub, None))
    assert isinstance(made, AssistantStub)
    # 키 경로는 Secret 뿐이다 (D25)
    with pytest.raises(ProviderError) as ei:
        _direct(LLMProvider(name="n", kind="openai"), ss)
    assert ei.value.code == "no_api_key"
    with pytest.raises(ProviderError) as ei:
        _direct(LLMProvider(name="n", kind="openai", api_key_secret="nope"), ss)
    assert ei.value.code == "no_secret"


def test_build_chat_model_on_the_gateway_route(monkeypatch):
    """게이트웨이 경로(P38 F5·F6)는 주소가 게이트웨이 경로이고 SDK 에는 자리표시 키를 준다. 토큰은
    요청마다 받는 bearer 훅이 싣는다. anthropic 은 block_binding 을 싣고 둘 다 연결 제한이 5초다."""
    monkeypatch.setattr(llm_http, "HookedChatAnthropic", Recorder)
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", HookedRecorder)
    gw = "http://llm-gateway.daegam-llm.svc.cluster.local:8080"

    def bearer() -> str:
        return "per-request"

    anthropic = LLMProvider(name="a", kind="anthropic", api_key_secret="llm-keys")
    endpoint = Endpoint(f"{gw}/p/a", PLACEHOLDER_KEY, bearer, "gateway")
    made = build_chat_model(anthropic, effort="low", endpoint=endpoint)
    assert made.kwargs.pop("bearer") is bearer
    assert made.kwargs["api_key"] == PLACEHOLDER_KEY and made.kwargs["base_url"] == f"{gw}/p/a"
    assert made.kwargs["thinking"] == ADAPTIVE_DIRECT
    assert made.kwargs["connect_timeout"] == llm_http.GATEWAY_CONNECT_SECONDS
    openai = LLMProvider(name="o", kind="openai", model="gpt-5", api_key_secret="llm-keys")
    endpoint = Endpoint(f"{gw}/p/o/v1", PLACEHOLDER_KEY, bearer, "gateway")
    made = build_chat_model(openai, effort="low", endpoint=endpoint)
    assert made.kwargs["timeout"] == llm_http.gateway_timeout()
    assert made.kwargs["api_key"] == PLACEHOLDER_KEY
    hooks = made.http_async_client.event_hooks["request"]
    assert any(isinstance(h, llm_http.BearerHook) and h.bearer is bearer for h in hooks)


def test_reasoning_effort_only_goes_to_openai_reasoning_models():
    """gpt-4.1 계열은 reasoning_effort 를 받으면 400 이다. gpt-5 계열과 o 시리즈에만 보낸다."""
    common = {"max_output_tokens": 10, "effort": "low", "api_key": "k", "base_url": None}
    assert openai_kwargs("gpt-5-mini", **common)["reasoning_effort"] == "low"
    assert openai_kwargs("o4-mini", **common)["reasoning_effort"] == "low"
    assert "reasoning_effort" not in openai_kwargs("gpt-4.1-mini", **common)
    assert "reasoning_effort" not in openai_kwargs("gpt-5-chat-latest", **common)


def _sdk_errors() -> list[tuple[Exception, bool]]:
    """업체가 실제로 주는 꼴의 SDK 예외와 크레딧 소진인지. OpenAI 클라이언트는 body 의 error 를
    벗기고, Anthropic 은 body 를 그대로 둔다."""
    req = httpx.Request("POST", "http://provider/v1/chat")

    def openai_error(cls: Any, status: int, body: dict[str, Any]) -> Exception:
        return cls(body["message"], response=httpx.Response(status, request=req), body=body)

    def anthropic_error(cls: Any, status: int, error: dict[str, Any]) -> Exception:
        body = {"type": "error", "error": error}
        return cls(error["message"], response=httpx.Response(status, request=req), body=body)

    low = "Your credit balance is too low to access the Anthropic API."
    usage = (
        "You have reached your specified API usage limits. You will regain access on 2026-11-01."
    )
    spend = {
        "message": "Spend limit",
        "type": "insufficient_quota",
        "code": "project_spend_limit_exceeded",
    }
    # Z.AI 는 잔액 부족을 429 와 숫자 문자열 코드 1113 으로 알린다. 같은 429 라도 다른 숫자 코드는
    # 기다리면 풀린다
    zai_balance = {"message": "Insufficient balance or no resource package.", "code": "1113"}
    zai_busy = {"message": "Rate limit reached for requests", "code": "1302"}
    return [
        (openai_error(openai.RateLimitError, 429, zai_balance), True),
        (openai_error(openai.RateLimitError, 429, zai_busy), False),
        (
            openai_error(
                openai.RateLimitError, 429, {"message": "quota", "code": "insufficient_quota"}
            ),
            True,
        ),
        (openai_error(openai.RateLimitError, 429, spend), True),
        (
            openai_error(
                openai.RateLimitError, 429, {"message": "slow down", "code": "rate_limit_exceeded"}
            ),
            False,
        ),
        (
            openai_error(
                openai.PermissionDeniedError,
                403,
                {"message": "Key limit exceeded (monthly limit)", "code": 403},
            ),
            True,
        ),
        (
            openai_error(
                openai.APIStatusError, 402, {"message": "Insufficient credits", "code": 402}
            ),
            True,
        ),
        (
            openai_error(
                openai.AuthenticationError,
                401,
                {"message": "Incorrect API key", "code": "invalid_api_key"},
            ),
            False,
        ),
        (
            anthropic_error(
                anthropic.BadRequestError, 400, {"type": "invalid_request_error", "message": low}
            ),
            True,
        ),
        (
            anthropic_error(
                anthropic.BadRequestError, 400, {"type": "invalid_request_error", "message": usage}
            ),
            True,
        ),
        (
            anthropic_error(
                anthropic.APIStatusError, 402, {"type": "billing_error", "message": "billing"}
            ),
            True,
        ),
        (
            anthropic_error(
                anthropic.RateLimitError, 429, {"type": "rate_limit_error", "message": "slow"}
            ),
            False,
        ),
        (
            openai_error(openai.APIStatusError, 400, {"message": "bad", "code": {"nested": "x"}}),
            False,
        ),
        (ValueError("x"), False),
    ]


def test_quota_judgement_reads_real_sdk_errors():
    """크레딧 소진 판정은 런타임(턴·임베딩)과 플랫폼(비서·스펙·인덱서·연결 확인)이 나눠 가지는
    llm_http.py 하나이고 실제 SDK 예외에 맞는 답을 낸다."""
    for error, expected in _sdk_errors():
        assert llm_http.quota_exhausted(error) is expected, error


def test_only_claude_calls_carry_the_cache_marker() -> None:
    """Steward 의 Claude 호출만 캐시 표지를 받는다 (T80). 게이트웨이 경로도 같은 클래스라 같고,
    OpenAI 는 표지 없이 스스로 캐시하며 스텁은 캐시가 없다. 표지는 부를 때마다 새 사본이다."""
    claude = llm_http.HookedChatAnthropic(model="claude-sonnet-5-5", api_key="k")
    gateway = llm_http.HookedChatAnthropic(
        model="claude-sonnet-5-5", api_key=PLACEHOLDER_KEY, base_url="http://gw/anthropic"
    )
    assert prompt_cache(claude) == prompt_cache(gateway) == {"type": "ephemeral"}
    assert prompt_cache(claude) is not prompt_cache(claude)
    assert prompt_cache(ChatOpenAI(model="gpt-5-mini", api_key="k")) is None
    assert prompt_cache(AssistantStub()) is None
