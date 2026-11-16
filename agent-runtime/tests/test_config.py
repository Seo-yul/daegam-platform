"""agent.json 로더: fixture 통과, 스키마 위반·참조 오류 거부, 경로 env 덮어쓰기, knowledge 참조,
features[] 의 기동 검사와 채팅 거절(D65)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from runtime import features
from runtime.config import ConfigError, load_config, parse_config
from runtime.main import create_app
from runtime.state import Settings
from tests.app_fixtures import INSTANCE_TOKEN, bearer, long_term_doc, with_subagent
from tests.conftest import FIXTURES, load_fixture, run_app


def test_fixtures_load() -> None:
    support = parse_config(load_fixture("support-agent.json"))
    minimal = parse_config(load_fixture("minimal.json"))
    redis = parse_config(load_fixture("redis-memory.json"))
    assert support.stub is True and minimal.stub is False
    # memory.checkpointer.kind 는 postgres·redis 둘이다(D33). 로더는 그대로 넘기고 저장소가 가른다
    assert support.memory["checkpointer"]["kind"] == "postgres"
    assert redis.memory["checkpointer"]["kind"] == "redis"
    assert redis.memory["checkpointer"]["dsn_env"] == "DAEGAM_DB_0_URL"
    assert support.approvals_for(["tools-calculator"]) == {"Divide": ["approve", "reject"]}
    assert support.timeouts_for(["tools-calculator"]) == {"Divide": 30}
    assert support.warnings == () and minimal.warnings == ()
    # "1" 문서에는 knowledge 키가 없고 접근자는 빈 목록을 준다
    assert support.schema_version == "1" and minimal.schema_version == "1"
    assert support.knowledge == [] and support.main_knowledge_refs() == []


def test_knowledge_fixture_loads() -> None:
    config = parse_config(load_fixture("knowledge-agent.json"))
    assert config.schema_version == "2" and config.warnings == ()
    assert [k["ref"] for k in config.knowledge] == ["kb-faq"]
    assert config.main_knowledge_refs() == ["kb-faq"]
    assert config.subagent_knowledge_refs("summarizer") == []
    assert config.knowledge[0]["tool"]["name"] == "search_faq"
    with pytest.raises(KeyError):
        config.subagent_knowledge_refs("ghost")


def test_provider_extra_body_is_an_optional_object() -> None:
    """provider.extra_body 는 있으면 비지 않은 객체다. 없는 문서는 그대로 읽힌다(T19)."""
    doc = load_fixture("minimal.json")
    doc["main"]["model"]["provider"].update(
        kind="openai",
        env_key="OPENAI_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        extra_body={"provider": {"order": ["moonshotai"]}},
    )
    parse_config(doc)
    for bad in ({}, "x", ["provider"]):
        doc["main"]["model"]["provider"]["extra_body"] = bad
        with pytest.raises(ConfigError) as info:
            parse_config(doc)
        assert info.value.code == "schema"


def test_v3_document_loads_with_long_term() -> None:
    """schema_version "3" 은 knowledge 키 셋과 memory.long_term 을 함께 가진다(D34 의 사다리)."""
    doc = long_term_doc()
    config = parse_config(doc)
    assert config.schema_version == "3"
    assert config.long_term == {"retention_days": 30}
    # 사다리라 knowledge 참조 검사도 그대로 돈다
    assert config.main_knowledge_refs() == ["kb-faq"]


# -- 거절하는 문서 ----------------------------------------------------------------------------


def _duplicate_knowledge() -> dict:
    doc = load_fixture("knowledge-agent.json")
    doc["knowledge"].append(copy.deepcopy(doc["knowledge"][0]))
    return doc


def _duplicate_subagent() -> dict:
    doc = load_fixture("support-agent.json")
    with_subagent(doc)
    doc["subagents"].append(copy.deepcopy(doc["subagents"][0]))
    return doc


def _reserved_subagent() -> dict:
    doc = load_fixture("support-agent.json")
    with_subagent(doc, name="general-purpose")
    return doc


def _document(base: str | Callable[[], dict], changes: dict[str, Any]) -> dict:
    """base(fixture 이름이나 문서를 만드는 함수)의 문서에 changes 를 넣는다. 키는 점으로 이은
    경로이고 숫자 마디는 목록의 자리다."""
    doc = load_fixture(base) if isinstance(base, str) else base()
    for dotted, value in changes.items():
        *parents, last = dotted.split(".")
        target = doc
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[last] = value
    return doc


@pytest.mark.parametrize(
    ("base", "changes", "code", "in_message"),
    [
        pytest.param("minimal.json", {"extra": 1}, "schema", "", id="unknown-key"),
        pytest.param("minimal.json", {"schema_version": "4"}, "schema", "", id="unknown-version"),
        # "1" 에는 knowledge 키가 없다. "2" 로만 올리고 knowledge 키 셋을 안 주어도 스키마가
        # 거부한다
        pytest.param("minimal.json", {"knowledge": []}, "schema", "", id="v1-top-knowledge"),
        pytest.param("minimal.json", {"main.knowledge": []}, "schema", "", id="v1-main-knowledge"),
        pytest.param("minimal.json", {"schema_version": "2"}, "schema", "", id="v2-without-keys"),
        pytest.param(_duplicate_knowledge, {}, "duplicate_knowledge_ref", "", id="dup-knowledge"),
        pytest.param(
            "knowledge-agent.json",
            {"main.knowledge": ["kb-missing"]},
            "unknown_knowledge_ref",
            "main.knowledge",
            id="unknown-knowledge-main",
        ),
        pytest.param(
            "knowledge-agent.json",
            {"subagents.0.knowledge": ["kb-missing"]},
            "unknown_knowledge_ref",
            "subagents[summarizer]",
            id="unknown-knowledge-subagent",
        ),
        # "3" 도 사다리라 knowledge 참조 검사가 그대로 돈다
        pytest.param(
            long_term_doc, {"main.knowledge": ["ghost"]}, "unknown_knowledge_ref", "", id="v3-ref"
        ),
        pytest.param(
            "support-agent.json",
            {"main.tools": ["tools-missing"]},
            "unknown_tool_ref",
            "",
            id="unknown-tool",
        ),
        pytest.param(_duplicate_subagent, {}, "duplicate_subagent", "", id="dup-subagent"),
        pytest.param(_reserved_subagent, {}, "reserved_subagent", "", id="reserved-subagent"),
        pytest.param(
            "support-agent.json",
            {"main.subagents": ["ghost"]},
            "unknown_subagent",
            "",
            id="unknown-subagent",
        ),
        # features 의 이름 꼴은 스키마가 보지만 꼴이 틀린 이름은 모르는 이름이기도 해서 스키마
        # 검사 전에 runtime_image_too_old 로 잡힌다
        pytest.param(
            "minimal.json", {"features": ["Bad-Name"]}, "runtime_image_too_old", "", id="bad-name"
        ),
        pytest.param("minimal.json", {"features": "cancel"}, "schema", "", id="features-string"),
    ],
)
def test_rejected_document_carries_its_code(
    base: str | Callable[[], dict], changes: dict[str, Any], code: str, in_message: str
) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(_document(base, changes))
    assert info.value.code == code
    assert in_message in info.value.message


def test_approval_outside_allow_is_warning() -> None:
    doc = load_fixture("support-agent.json")
    doc["tools"][0]["approval"] = {"Multiply": ["approve"]}
    config = parse_config(doc)
    assert any("Multiply" in w for w in config.warnings)


def test_load_config_env_override(tmp_path, monkeypatch) -> None:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(load_fixture("minimal.json")), encoding="utf-8")
    monkeypatch.setenv("DAEGAM_AGENT_CONFIG", str(path))
    assert load_config().profile["name"] == "minimal"
    assert load_config(FIXTURES / "support-agent.json").profile["name"] == "support"


def test_load_config_missing_file(tmp_path) -> None:
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path / "nope.json")
    assert info.value.code == "unreadable"


# -- features[] (D65) ------------------------------------------------------------------------


def test_features_known_empty_or_absent_pass() -> None:
    """아는 이름·빈 목록·키 없음은 지나고 접근자는 목록이나 빈 목록을 준다."""
    doc = load_fixture("minimal.json")
    assert parse_config(doc).features == []
    doc["features"] = []
    assert parse_config(doc).features == []
    doc["features"] = list(features.KNOWN)
    # 기능 web_guard 가 든 문서는 web_data_tools 를, code_execution 이 든 문서는 code_execution 을
    # 반드시 싣는다(D64, P40)
    doc["web_data_tools"] = False
    doc["code_execution"] = {"approval": []}
    assert parse_config(doc).features == list(features.KNOWN)


def test_code_execution_section_follows_its_feature() -> None:
    """코드 실행 칸은 기능 code_execution 이 있을 때만 받고 그때는 필수다(P40 9절)."""
    doc = load_fixture("minimal.json")
    assert parse_config(doc).code_execution is None
    with pytest.raises(ConfigError, match="does not allow"):
        parse_config({**doc, "code_execution": {"approval": []}})
    with pytest.raises(ConfigError, match="code_execution"):
        parse_config({**doc, "features": ["code_execution"]})
    on = {**doc, "features": ["code_execution"], "code_execution": {"approval": ["approve"]}}
    assert parse_config(on).code_execution == ["approve"]


def test_unknown_feature_is_caught_before_the_schema(monkeypatch) -> None:
    """모르는 이름은 스키마 검사 전에 runtime_image_too_old 다. 새 기능이 더한 새 키가 함께 있어도
    이유가 `schema` 로 가려지지 않는다."""
    doc = load_fixture("minimal.json")
    doc["features"] = ["future_thing"]
    doc["future_thing"] = {"on": True}
    with pytest.raises(ConfigError) as info:
        parse_config(doc)
    assert info.value.code == "runtime_image_too_old"
    assert "future_thing" in info.value.message
    # 이름이 알려지면 같은 문서는 스키마 검사까지 간다(새 키를 모르는 스키마라 schema 다)
    monkeypatch.setattr(features, "KNOWN", frozenset({"future_thing"}))
    with pytest.raises(ConfigError) as info:
        parse_config(doc)
    assert info.value.code == "schema"


async def test_refusing_app_fails_only_the_readiness_probe(minimal_doc) -> None:
    """모르는 기능이면 채팅을 받지 않는다. /healthz 는 200, ?probe=ready 는 503 이고 다른 라우트는
    503 runtime_image_too_old 다."""
    minimal_doc["features"] = ["future_thing"]
    with pytest.raises(ConfigError) as info:
        parse_config(minimal_doc)
    app = create_app(settings=Settings(instance_token=INSTANCE_TOKEN), refusal=info.value)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            live = await client.get("/healthz")
            assert live.status_code == 200
            assert live.json()["status"] == "refusing"
            assert live.json()["error"]["code"] == "runtime_image_too_old"
            assert live.json()["unready"] == live.json()["error"]
            ready = await client.get("/healthz", params={"probe": "ready"})
            assert ready.status_code == 503
            assert ready.json()["code"] == "runtime_image_too_old"
            chat = await client.post(
                "/chat", json={"message": "hi"}, headers=bearer(INSTANCE_TOKEN)
            )
            assert chat.status_code == 503
            assert chat.json()["code"] == "runtime_image_too_old"


async def test_healthz_and_metrics_report_known_features(support_doc) -> None:
    """돌고 있는 런타임은 확인용으로 아는 이름을 싣는다. 준비 검사도 200 이다."""
    async with run_app(support_doc, settings=Settings(instance_token=INSTANCE_TOKEN)) as running:
        headers = bearer(INSTANCE_TOKEN)
        ready = await running.client.get("/healthz", params={"probe": "ready"})
        assert ready.status_code == 200 and ready.json() == {"status": "ok"}
        health = (await running.client.get("/healthz", headers=headers)).json()
        assert health["runtime"] == {"features": sorted(features.KNOWN)}
        metrics = (await running.client.get("/metrics", headers=headers)).json()
        assert metrics["runtime"] == {"features": sorted(features.KNOWN)}
