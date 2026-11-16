"""provider kind 별 BaseChatModel 조립과 스텁 모델 규칙(docs/guide/agents.md 3절 모델 표, D26)."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx2
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from runtime import llm_http
from runtime.config import ConfigError
from runtime.models import (
    BLOCK_BINDING,
    CORRELATION_HOOK_MISSING,
    DEFAULT_CONTEXT_WINDOW,
    THINKING_BINDING_BETA,
    build_chat_model,
    correlation_warnings,
    harness_key,
)
from runtime.stub import (
    NO_TOOL_PREFIX,
    RESULT_PREFIX,
    SLEEP_MAX_SECONDS,
    SLEEP_TEXT,
    SUMMARY_TEXT,
    StubChatModel,
    parse_sleep_fragment,
)
from tests.conftest import load_fixture


def _model(kind: str, name: str, **params) -> dict:
    env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "stub": None}[kind]
    model = copy.deepcopy(load_fixture("minimal.json")["main"]["model"])
    model["provider"] = {"name": f"p-{kind}", "kind": kind, "env_key": env, "base_url": None}
    model["name"] = name
    model["params"].update(params)
    return model


def test_anthropic_mapping(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    spec = _model(
        "anthropic", "claude-opus-5", max_output_tokens=1234, effort="xhigh", temperature=0.5
    )
    spec["provider"]["base_url"] = "http://llm-proxy/anthropic"
    model = build_chat_model(spec)
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-opus-5"
    assert model.max_tokens == 1234
    assert model.thinking == {"type": "adaptive"}
    assert model.output_config == {"effort": "xhigh"}
    assert model.temperature is None
    assert model.anthropic_api_url == "http://llm-proxy/anthropic"
    # SDK 자체 재시도는 끄고 limits.retries 는 그래프의 ModelRetry 미들웨어가 센다
    assert model.max_retries == 0
    assert harness_key(model) == "anthropic:claude-opus-5"


ADAPTIVE_DIRECT = {"type": "adaptive", "block_binding": BLOCK_BINDING}


@pytest.mark.parametrize(
    ("name", "effort", "thinking", "sent_effort", "warns"),
    [
        # 끄지 못하는 모델과 모르는 이름의 off 는 adaptive 의 low 다
        ("claude-opus-5-5", "off", ADAPTIVE_DIRECT, "low", True),
        ("claude-sonnet-5-5", "off", ADAPTIVE_DIRECT, "low", True),
        ("claude-x", "off", ADAPTIVE_DIRECT, "low", True),
        # 끌 수 있는 모델의 off 는 disabled 다. 날짜만 붙은 Opus 5 도 그렇다
        ("claude-opus-5", "off", {"type": "disabled"}, None, False),
        ("claude-opus-5-20260301", "off", {"type": "disabled"}, None, False),
        # Haiku 5.5 는 5.5 세대에서 홀로 끌 수 있다(P43 F0 5번)
        ("claude-haiku-5-5", "off", {"type": "disabled"}, None, False),
        ("claude-haiku-5-5-20261001", "off", {"type": "disabled"}, None, False),
        ("claude-haiku-5-5", "low", ADAPTIVE_DIRECT, "low", False),
        # 4.5 세대는 adaptive 를 받지 않아 off 가 아니면 thinking 도 effort 도 없다
        ("claude-haiku-4-5-20251001", "medium", None, None, True),
        # 4.6 세대는 xhigh 가 없어 high 다
        ("claude-opus-4-6", "xhigh", ADAPTIVE_DIRECT, "high", True),
        # off 가 아닌 모르는 이름은 그대로다
        ("claude-x", "high", ADAPTIVE_DIRECT, "high", False),
    ],
)
def test_anthropic_reasoning_rows(monkeypatch, caplog, name, effort, thinking, sent_effort, warns):
    """7절 표의 행이다. base_url 이 없어 adaptive 에는 block_binding 과 그 베타가 실린다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with caplog.at_level("WARNING", logger="daegam.agent-runtime.models"):
        model = build_chat_model(_model("anthropic", name, effort=effort))
    assert model.thinking == thinking
    assert model.output_config == ({"effort": sent_effort} if sent_effort else None)
    assert model.betas == ([THINKING_BINDING_BETA] if thinking is ADAPTIVE_DIRECT else None)
    # 모르는 이름의 컨텍스트 창 경고(model_profile_unknown)는 이 표의 경고가 아니다
    downgraded = [r for r in caplog.records if r.message.startswith(f"anthropic 모델 {name} ")]
    assert bool(downgraded) is warns


@pytest.mark.parametrize("base_url", [None, "http://llm-proxy/anthropic"])
def test_block_binding_rides_only_on_direct_requests(monkeypatch, base_url) -> None:
    """Claude API 로 바로 가는 요청에만 thinking.block_binding 과 그 베타가 실린다. 프록시가 그
    베타를 받는지 모르기 때문이다. 요청 본문은 네트워크 없이 만든다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    spec = _model("anthropic", "claude-sonnet-5-5", effort="medium")
    spec["provider"]["base_url"] = base_url
    payload = build_chat_model(spec)._get_request_payload([HumanMessage("안녕")])
    assert payload["output_config"] == {"effort": "medium"}
    if base_url is None:
        assert payload["thinking"] == ADAPTIVE_DIRECT
        assert payload["betas"] == [THINKING_BINDING_BETA]
    else:
        assert payload["thinking"] == {"type": "adaptive"}
        assert "betas" not in payload and "block_binding" not in payload["thinking"]


def test_openai_mapping(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    spec = _model("openai", "gpt-4.1", max_output_tokens=777, effort="high", temperature=0.3)
    spec["provider"]["base_url"] = "http://llm-proxy/v1"
    model = build_chat_model(spec)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-4.1"
    assert model.max_tokens == 777
    # gpt-4.1 은 추론 모델이 아니라 effort 가 있어도 reasoning_effort 를 보내지 않는다
    assert model.reasoning_effort is None
    assert model.temperature == 0.3
    assert model.openai_api_base == "http://llm-proxy/v1"
    assert model.use_responses_api is False
    assert model.max_retries == 0
    assert harness_key(model) == "openai:gpt-4.1"


def test_openai_extra_body_rides_only_when_present(monkeypatch) -> None:
    """플랫폼이 실은 extra_body(OpenRouter 업체 선호)는 ChatOpenAI 로 그대로 간다. 없으면 보내지
    않는다."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    routing = {"provider": {"order": ["moonshotai"], "require_parameters": True}}
    spec = _model("openai", "moonshotai/kimi-k3")
    spec["provider"]["base_url"] = "https://openrouter.ai/api/v1"
    spec["provider"]["extra_body"] = routing
    assert build_chat_model(spec).extra_body == routing
    assert build_chat_model(_model("openai", "gpt-4.1")).extra_body is None


def test_openai_effort_off_and_max(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert build_chat_model(_model("openai", "gpt-5.4", effort="off")).reasoning_effort is None
    assert build_chat_model(_model("openai", "gpt-5.4", effort="max")).reasoning_effort == "xhigh"
    # 추론 모델이 아니면 effort 가 있어도 reasoning_effort 를 보내지 않는다(400 이 된다)
    assert (
        build_chat_model(_model("openai", "gpt-4.1-mini", effort="high")).reasoning_effort is None
    )
    assert (
        build_chat_model(_model("openai", "gpt-5-chat-latest", effort="high")).reasoning_effort
        is None
    )
    assert build_chat_model(_model("openai", "o4-mini", effort="low")).reasoning_effort == "low"


@pytest.mark.parametrize(
    ("name", "effort", "sent"),
    [
        # glm-5.3 은 low, high, max 만 받는다. 없는 값은 가장 가까운 값이고 같은 거리면 센 쪽이다
        ("glm-5.3", "low", "low"),
        ("glm-5.3", "medium", "high"),
        ("glm-5.3", "high", "high"),
        ("glm-5.3", "xhigh", "max"),
        ("glm-5.3", "max", "max"),
        # glm-5.3 은 끌 수 없어 off 는 가장 약한 low 다
        ("glm-5.3", "off", "low"),
        # glm-5.2 는 none 부터 max 까지 받는다. off 는 none 이다
        ("glm-5.2", "off", "none"),
        ("glm-5.2", "low", "low"),
        ("glm-5.2", "medium", "medium"),
        ("glm-5.2", "xhigh", "xhigh"),
        ("glm-5.2", "max", "max"),
        # 대소문자는 가리지 않는다
        ("GLM-5.3", "medium", "high"),
        # 값을 모르는 GLM 이름은 보내지 않는다
        ("glm-4.7", "high", None),
        ("glm-5.3-air", "high", None),
    ],
)
def test_glm_effort_is_mapped_per_version(monkeypatch, name, effort, sent) -> None:
    """Z.AI 도 kind openai 로 오므로 모델 이름으로 가른다. 모든 effort 값이 받는 값으로 간다."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    spec = _model("openai", name, effort=effort)
    assert build_chat_model(spec).reasoning_effort == sent


def test_unknown_model_gets_default_context_window(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DAEGAM_MODEL_CONTEXT_WINDOW", "4096")
    model = build_chat_model(_model("openai", "in-house-proxy-model"))
    assert model.profile["max_input_tokens"] == 4096


@pytest.mark.parametrize("raw", ["abc", "128k", "0", "-5"])
def test_bad_context_window_env_falls_back_to_the_default(monkeypatch, raw) -> None:
    """잘못 적은 env 로 기동이 죽지 않는다. 경고하고 기본 창을 쓴다."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DAEGAM_MODEL_CONTEXT_WINDOW", raw)
    model = build_chat_model(_model("openai", "in-house-proxy-model"))
    assert model.profile["max_input_tokens"] == DEFAULT_CONTEXT_WINDOW


def test_missing_key_env_rejected(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError) as info:
        build_chat_model(_model("anthropic", "claude-opus-5"))
    assert info.value.code == "provider_key_missing"


def test_stub_model_rules() -> None:
    model = build_chat_model(_model("stub", "stub"))
    assert isinstance(model, StubChatModel)
    assert harness_key(model) == "stub:stub"
    assert model.profile["max_input_tokens"] > 0

    # 툴이 없으면 답만 한다
    reply = model.reply([HumanMessage("안녕")])
    assert reply.content == NO_TOOL_PREFIX + "안녕" and not reply.tool_calls

    # 내장 툴은 건너뛰고 첫 MCP 툴을 빈 인자로 부른다
    assert model.bind_tools([{"name": "ls"}, {"name": "task"}, {"name": "Add"}]) is model
    reply = model.reply([HumanMessage("더해 줘")])
    assert reply.tool_calls[0]["name"] == "Add" and reply.tool_calls[0]["args"] == {}
    assert reply.usage_metadata["input_tokens"] >= 1

    # JSON 조각이 있으면 그 툴과 인자다
    reply = model.reply([HumanMessage('계산: {"tool": "Divide", "args": {"a": 6, "b": 2}}')])
    assert reply.tool_calls[0]["name"] == "Divide"
    assert reply.tool_calls[0]["args"] == {"a": 6, "b": 2}

    # 내장 툴만 묶여 있어도 이름을 직접 대면 부른다(장기기억 e2e 가 이 길로 파일을 쓴다)
    assert model.bind_tools([{"name": "write_file"}, {"name": "read_file"}]) is model
    reply = model.reply([HumanMessage('저장 {"tool": "write_file", "args": {"file_path": "/x"}}')])
    assert reply.tool_calls[0]["name"] == "write_file"
    assert reply.tool_calls[0]["args"] == {"file_path": "/x"}

    # 그래도 스스로 고르지는 않는다
    reply = model.reply([HumanMessage("아무거나 해 줘")])
    assert reply.content == NO_TOOL_PREFIX + "아무거나 해 줘" and not reply.tool_calls

    # 직전이 ToolMessage 면 결과 텍스트로 끝난다
    reply = model.reply([HumanMessage("x"), ToolMessage(content="7", tool_call_id="c1")])
    assert reply.content == RESULT_PREFIX + "7" and not reply.tool_calls

    # 요약 프롬프트에는 고정 문구다
    reply = model.reply([HumanMessage("<messages>\nMessages to summarize:\n...</messages>")])
    assert reply.content == SUMMARY_TEXT


async def test_stub_sleep_fragment_waits_then_answers_without_tools() -> None:
    """`{"sleep": n}` 조각은 n 초 기다린 뒤 도구 없이 글로 답한다(D65). e2e 가 진행 중인 모델 호출을
    만드는 자리다. 상한이 있고, 요약 프롬프트와 도구 결과 차례에는 기다리지 않는다."""
    assert parse_sleep_fragment('잠깐 {"sleep": 2}') == 2.0
    assert parse_sleep_fragment('{"sleep": 999}') == SLEEP_MAX_SECONDS
    assert parse_sleep_fragment('{"sleep": -1}') == 0.0
    assert parse_sleep_fragment('{"sleep": true}') is None
    assert parse_sleep_fragment('{"tool": "Add"}') is None

    model = StubChatModel()
    model.bind_tools([{"name": "Add"}])
    messages = [HumanMessage('{"sleep": 0.05}')]
    assert model.sleep_seconds(messages) == 0.05
    reply = model.reply(messages)
    assert reply.content == SLEEP_TEXT.format(seconds=0.05) and not reply.tool_calls
    after_tool = [HumanMessage("x"), ToolMessage(content="7", tool_call_id="c")]
    assert model.sleep_seconds(after_tool) is None
    assert model.sleep_seconds([HumanMessage('Messages to summarize:\n{"sleep": 5}')]) is None

    started = asyncio.get_running_loop().time()
    result = await model.ainvoke(messages)
    assert asyncio.get_running_loop().time() - started >= 0.05
    assert result.content == SLEEP_TEXT.format(seconds=0.05)


async def test_stub_sleep_fragment_with_a_tool_waits_then_calls_it() -> None:
    """`{"sleep": n, "tool": ...}` 조각은 n 초 기다린 뒤 그 툴을 부른다(P53). 턴 위임 토큰의
    수명보다 긴 턴을 e2e 가 만드는 자리다. 툴 결과 차례에는 기다리지 않고 글로 끝낸다."""
    model = StubChatModel()
    model.bind_tools([{"name": "Add"}])
    messages = [HumanMessage('{"sleep": 0.05, "tool": "Add", "args": {"a": 5, "b": 6}}')]
    started = asyncio.get_running_loop().time()
    result = await model.ainvoke(messages)
    assert asyncio.get_running_loop().time() - started >= 0.05
    assert [(c["name"], c["args"]) for c in result.tool_calls] == [("Add", {"a": 5, "b": 6})]
    call_id = result.tool_calls[0]["id"]
    after_tool = [*messages, result, ToolMessage(content="11", tool_call_id=call_id)]
    assert model.sleep_seconds(after_tool) is None
    assert model.reply(after_tool).content == "결과: 11"


async def test_stub_after_fragment_waits_after_the_tool_result() -> None:
    """`"after": n` 이 있으면 툴 결과를 받은 뒤 n 초 기다렸다가 글로 끝낸다(P78). e2e 가 done 전에
    저장된 툴 결과를 읽는 자리다. 사용자 말 차례에는 기다리지 않는다."""
    assert parse_sleep_fragment('{"after": 999}', "after") == SLEEP_MAX_SECONDS
    model = StubChatModel()
    model.bind_tools([{"name": "Add"}])
    messages = [HumanMessage('{"tool": "Add", "args": {"a": 5, "b": 6}, "after": 0.05}')]
    assert model.sleep_seconds(messages) is None
    call = await model.ainvoke(messages)
    after_tool = [*messages, call, ToolMessage(content="11", tool_call_id=call.tool_calls[0]["id"])]
    assert model.sleep_seconds(after_tool) == 0.05
    started = asyncio.get_running_loop().time()
    result = await model.ainvoke(after_tool)
    assert asyncio.get_running_loop().time() - started >= 0.05
    assert result.content == "결과: 11"


@pytest.mark.parametrize(
    ("kind", "attr"), [("anthropic", "anthropic_api_key"), ("openai", "openai_api_key")]
)
def test_two_providers_of_one_kind_read_their_own_key_env(monkeypatch, kind, attr) -> None:
    """같은 kind 의 provider 둘은 env 이름이 다르고(플랫폼이 provider 이름에서 짓는다, P35 12절)
    각자 제 env 의 키로 모델을 만든다. kind 별 옛 env 는 읽지 않는다."""
    monkeypatch.delenv({"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[kind], False)
    monkeypatch.setenv("DAEGAM_LLM_MAIN_API_KEY", "k-main")
    monkeypatch.setenv("DAEGAM_LLM_BACKUP_API_KEY", "k-backup")
    keys = []
    for name in ("main", "backup"):
        spec = _model(kind, {"anthropic": "claude-opus-5", "openai": "gpt-4.1"}[kind])
        spec["provider"].update(name=name, env_key=f"DAEGAM_LLM_{name.upper()}_API_KEY")
        keys.append(getattr(build_chat_model(spec), attr).get_secret_value())
    assert keys == ["k-main", "k-backup"]


def test_models_carry_provider_route_and_role_and_hooked_clients(monkeypatch) -> None:
    """모델 metadata 에 provider 이름·경로·역할이 실리고 SDK 클라이언트는 상관 헤더 훅을 단다.
    스트림 사용량은 provider 가 끄지 않는 한 켠다(P37 2절)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    gateway = _model("openai", "gpt-4.1")
    gateway["provider"].update(gateway=True, stream_usage=False)
    openai = build_chat_model(gateway, role="fallback")
    anthropic = build_chat_model(_model("anthropic", "claude-opus-5"), role="subagent")
    stub = build_chat_model(_model("stub", "stub"))
    assert {k: v for k, v in openai.metadata.items() if k.startswith("daegam_")} == {
        "daegam_provider": "p-openai",
        "daegam_route": "gateway",
        "daegam_role": "fallback",
    }
    assert anthropic.metadata["daegam_route"] == "direct"
    assert anthropic.metadata["daegam_role"] == "subagent"
    assert stub.metadata["daegam_role"] == "main"
    assert openai.stream_usage is False and openai.max_retries == 0
    assert build_chat_model(_model("openai", "gpt-4.1")).stream_usage is True
    assert llm_http.has_correlation_hook(openai.http_async_client)
    assert isinstance(anthropic, llm_http.HookedChatAnthropic)
    assert correlation_warnings([openai, anthropic, stub, None]) == []


def test_a_model_without_the_hook_is_a_startup_warning() -> None:
    plain = ChatOpenAI(model="gpt-4.1", api_key="k")
    (warning,) = correlation_warnings([plain])
    assert warning.startswith(f"{CORRELATION_HOOK_MISSING}: ")


GATEWAY_URL = "http://llm-gateway.daegam-llm.svc.cluster.local:8080/p/main"


def _gateway(kind: str, name: str, **params) -> dict:
    """게이트웨이 경로의 모델 객체다(P38 6절). base_url 은 게이트웨이 경로이고 키 env 는
    토큰이다."""
    spec = _model(kind, name, **params)
    suffix = "" if kind == "anthropic" else "/v1"
    spec["provider"].update(gateway=True, base_url=GATEWAY_URL + suffix)
    return spec


def test_the_gateway_route_sends_the_token_as_bearer_with_a_connect_limit(monkeypatch) -> None:
    """게이트웨이 경로(P38 F5)는 env 의 토큰을 두 SDK 에 api_key 로 준다. 둘 다 Bearer 로 싣고
    anthropic 도 X-Api-Key 를 싣지 않는다. 두 SDK 의 연결 제한은 5초다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "llm-token")
    monkeypatch.setenv("OPENAI_API_KEY", "llm-token")
    openai = build_chat_model(_gateway("openai", "gpt-5-mini"))
    assert openai.openai_api_key.get_secret_value() == "llm-token"
    assert openai.request_timeout == llm_http.gateway_timeout()
    assert openai.request_timeout.connect == llm_http.GATEWAY_CONNECT_SECONDS
    anthropic = build_chat_model(_gateway("anthropic", "claude-opus-5"))
    client = anthropic._async_client
    assert client.auth_headers == {"Authorization": "Bearer llm-token"}
    assert client.timeout.connect == llm_http.GATEWAY_CONNECT_SECONDS
    assert anthropic.metadata["daegam_route"] == "gateway"
    # 직접 경로도 키를 Bearer 로 보내고 연결 제한을 따로 두지 않는다
    direct = build_chat_model(_model("anthropic", "claude-opus-5"))._async_client
    assert direct.auth_headers == {"Authorization": "Bearer llm-token"}
    assert build_chat_model(_model("openai", "gpt-5-mini")).request_timeout is None


MESSAGE = {
    "id": "m",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.mark.parametrize("gateway", [False, True])
def test_the_anthropic_request_on_the_wire(monkeypatch, gateway) -> None:
    """anthropic SDK 2.x 의 요청이다(API 판 2026-10-21). 키는 `Authorization: Bearer` 하나이고
    `x-api-key` 는 없다. 그 판은 temperature·top_p·top_k 를 400 으로 거부하므로 params 에
    temperature 가 있어도 본문에 하나도 없다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "llm-key")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=MESSAGE)

    spec = (_gateway if gateway else _model)("anthropic", "claude-opus-5", temperature=0.5)
    model = build_chat_model(spec)
    model.http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    asyncio.run(model.ainvoke([HumanMessage("hi")]))
    (request,) = seen
    assert request.headers["authorization"] == "Bearer llm-key"
    assert "x-api-key" not in request.headers
    assert request.headers["anthropic-version"] == "2026-10-21"
    assert request.headers["anthropic-beta"] == THINKING_BINDING_BETA
    assert not {"temperature", "top_p", "top_k"} & json.loads(request.content).keys()


def test_block_binding_rides_on_the_gateway_route(monkeypatch) -> None:
    """게이트웨이는 block_binding 과 그 베타를 그대로 넘기므로(F0 17번) base_url 이 있어도
    게이트웨이 경로면 싣는다. 게이트웨이가 아닌 base_url 은 싣지 않는다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    spec = _gateway("anthropic", "claude-sonnet-5-5", effort="medium")
    payload = build_chat_model(spec)._get_request_payload([HumanMessage("안녕")])
    assert payload["thinking"] == ADAPTIVE_DIRECT
    assert payload["betas"] == [THINKING_BINDING_BETA]
