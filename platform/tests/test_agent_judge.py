"""판정 모델 규칙 `injection_check` 의 플랫폼 쪽이다(P43 7절, B3).

노드 형식(position tool, 노드 하나에 하나)과 그래프 검사(`unknown_judge_model`, `judge_count`,
붙은 것으로 치는 판정 모델 노드)는 묶음 `tests/fixtures/agent-graph-corpus.json` 도 캔버스와 함께
본다. 여기는 컴파일이 판정 모델 노드를 모델 객체로 풀어 싣는 것, 요구 기능, 붙이는 함수가 판정 모델
자리에도 칸을 싣는 것, 인용 원문이 막히지 않는 것을 본다.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from daegam.services import agent_features
from daegam.services.agent_citations import masks_sources
from daegam.services.agent_features import (
    INJECTION_CHECK,
    LLM_GATEWAY,
    PROVIDER_EXTRA_BODY,
    TOOL_SAFETY,
    AgentFeatures,
    finish_document,
    model_places,
)
from daegam.services.agent_graph import (
    GuardrailData,
    compiled_model_providers,
    uses_injection_check,
    validate,
)
from daegam.store.agents import AgentSpec
from daegam.store.settings import LLMProvider, ProviderRouting
from tests.helpers.agents import attach, compile_spec, make_ctx, template_spec
from tests.helpers.docs import agent_doc


def judge_rule(model: str = "model-judge", **value: Any) -> dict[str, Any]:
    return {"kind": "injection_check", "value": {"model": model, **value}}


def with_judge(
    provider: str = "local-anthropic", *, on_violation: str = "block", **rule: Any
) -> dict[str, Any]:
    """템플릿에 판정용 모델 노드 하나와 그것을 가리키는 Tool 가드레일을 더해 메인에 붙인다."""
    spec = template_spec()
    spec["graph"]["nodes"] += [
        {
            "id": "model-judge",
            "type": "model",
            "data": {"name": "judge", "provider": provider, "model": "claude-haiku-5-5"},
        },
        {
            "id": "guard-judge",
            "type": "guardrail",
            "data": {
                "position": "tool",
                "rules": [judge_rule(**rule)],
                "on_violation": on_violation,
                "message": None,
            },
        },
    ]
    attach(spec, "agent-main", "guard-judge", "guardrail")
    return spec


def judge_rules(document: dict[str, Any]) -> list[dict[str, Any]]:
    """문서의 injection_check 규칙들이다."""
    return [
        rule
        for group in document["guardrails"]
        for rule in group["rules"]
        if rule["kind"] == "injection_check"
    ]


def judge_of(document: dict[str, Any]) -> dict[str, Any]:
    [rule] = judge_rules(document)
    return rule["value"]


def test_the_rule_takes_defaults_and_bounds() -> None:
    data = GuardrailData.model_validate({"position": "tool", "rules": [judge_rule()]})
    assert data.rules[0].value == {"model": "model-judge", "max_chars": 8000, "max_calls": 20}
    assert data.judge is not None and data.judge.model == "model-judge"
    for bad in ({"max_chars": 199}, {"max_calls": 0}, {"max_calls": 1001}, {"extra": 1}):
        with pytest.raises(ValidationError):
            GuardrailData.model_validate({"position": "tool", "rules": [judge_rule(**bad)]})


def test_the_rule_runs_only_at_position_tool_and_once_per_node() -> None:
    with pytest.raises(ValidationError, match="only at position tool"):
        GuardrailData.model_validate({"position": "output", "rules": [judge_rule()]})
    with pytest.raises(ValidationError, match="only one injection_check"):
        GuardrailData.model_validate({"position": "tool", "rules": [judge_rule(), judge_rule()]})


def test_compile_carries_the_judge_model_object() -> None:
    ctx = make_ctx()
    spec = with_judge(max_calls=5)
    assert [i.code for i in validate(AgentSpec.model_validate(spec), ctx)] == []
    # 모델 노드의 추론 강도(기본 medium)와 상관없이 판정은 추론 없이 부른다(F0 5번이 잰 모양)
    assert judge_of(compile_spec(spec, ctx)) == {
        "model": {
            "provider": {
                "name": "local-anthropic",
                "kind": "anthropic",
                "env_key": "DAEGAM_LLM_LOCAL_ANTHROPIC_API_KEY",
                "base_url": None,
            },
            "name": "claude-haiku-5-5",
            "params": {"max_output_tokens": 2048, "effort": "off", "temperature": None},
        },
        "max_chars": 8000,
        "max_calls": 5,
    }


def test_a_judge_requires_injection_check_and_tool_safety() -> None:
    spec = with_judge()
    rev = AgentSpec.model_validate(spec)
    assert uses_injection_check(rev)
    doc = agent_doc(spec=spec)
    wanted = agent_features.agent_features(doc, doc.current, make_ctx().providers)
    assert wanted == AgentFeatures(required=frozenset({INJECTION_CHECK, TOOL_SAFETY}))
    assert not uses_injection_check(AgentSpec.model_validate(template_spec()))


def test_an_unattached_judge_is_not_compiled_or_required() -> None:
    spec = with_judge()
    spec["graph"]["edges"] = [e for e in spec["graph"]["edges"] if e["target"] != "guard-judge"]
    rev = AgentSpec.model_validate(spec)
    assert not uses_injection_check(rev)
    assert compiled_model_providers(rev) == ["local-stub"]
    assert judge_rules(compile_spec(spec, make_ctx())) == []


def test_the_judge_provider_is_a_compiled_model_provider() -> None:
    rev = AgentSpec.model_validate(with_judge("local-openai"))
    assert compiled_model_providers(rev) == ["local-stub", "local-openai"]


def test_attaching_functions_reach_the_judge_model() -> None:
    """업체 선호와 게이트웨이 표식은 모델 자리마다 싣는다. 판정 모델도 모델 자리다."""
    ctx = make_ctx(keyed=("router",))
    ctx.providers["router"] = LLMProvider(
        name="router",
        kind="openrouter",
        api_key_secret="k",
        routing=ProviderRouting(order=["moonshotai"]),
    )
    spec = with_judge("router")
    rev = AgentSpec.model_validate(spec)
    base = compile_spec(spec, ctx)
    assert [m["provider"]["name"] for m in model_places(base)] == ["local-stub", "router"]
    allowed = frozenset({PROVIDER_EXTRA_BODY, LLM_GATEWAY, INJECTION_CHECK, TOOL_SAFETY})
    final = finish_document(base, allowed, ctx, rev)
    provider = judge_of(final)["model"]["provider"]
    assert provider["extra_body"] == {"provider": {"order": ["moonshotai"]}}
    assert provider["gateway"] is True
    assert final["features"] == sorted(allowed)


def test_a_judge_does_not_hide_cited_sources() -> None:
    """판정은 결과를 막을 뿐 가리지 않는다. 막힌 결과는 인용 출처도 비운다."""
    assert not masks_sources(AgentSpec.model_validate(with_judge()))
