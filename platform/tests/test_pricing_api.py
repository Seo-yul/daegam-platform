"""가격표 API (P37 6절): 읽기, 판을 맞춘 바꾸기, 겹친 규칙, 감사와 비서 묶음."""

from __future__ import annotations

from daegam.services.assistant.catalog import CATALOG, group_of
from tests.helpers.http import detail_code

PRICING = "/settings/pricing"
RULE = {
    "provider_kind": "openai",
    "model": "echo-*",
    "input": 1,
    "output": 2,
    "effective_from": "2026-01-01",
}


def test_put_replaces_the_table_under_the_read_version(client, mongo) -> None:
    r = client.put(PRICING, json={"expected_version": 0, "rules": [RULE]})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["version"] == 1 and saved["rules"][0]["id"]
    # 같은 판으로 다시 쓰면 충돌이다
    stale = client.put(PRICING, json={"expected_version": 0, "rules": []})
    assert detail_code(stale) == (409, "revision_conflict")
    # 서버가 준 id 를 그대로 돌려보내면 유지된다
    keep = client.put(PRICING, json={"expected_version": 1, "rules": saved["rules"]})
    assert keep.json()["rules"][0]["id"] == saved["rules"][0]["id"]
    doc = mongo["settings"].find_one({"_id": "pricing"})
    assert doc["seq"] == 2 and doc["rules"][0]["effective_from"] == "2026-01-01"
    assert client.get(PRICING).json()["version"] == 2
    assert (
        mongo["audit_events"].count_documents({"path": "/api/settings/pricing", "method": "PUT"})
        == 3
    )


def test_duplicate_rules_ids_and_bad_patterns_are_refused(client) -> None:
    r = client.put(PRICING, json={"expected_version": 0, "rules": [RULE, RULE]})
    assert detail_code(r) == (422, "invalid_value")
    twins = [{**RULE, "id": "r1"}, {**RULE, "id": "r1", "model": "echo-2*"}]
    assert detail_code(client.put(PRICING, json={"expected_version": 0, "rules": twins})) == (
        422,
        "invalid_value",
    )
    # 기본 가격의 id 앞머리는 관리자 규칙이 쓰지 못한다
    taken = [{**RULE, "id": "default:mine"}]
    assert detail_code(client.put(PRICING, json={"expected_version": 0, "rules": taken})) == (
        422,
        "invalid_value",
    )
    bad = {**RULE, "model": "gpt*5"}
    assert client.put(PRICING, json={"expected_version": 0, "rules": [bad]}).status_code == 422


def test_an_admin_rule_keeps_its_tier_through_save_and_read(client, mongo) -> None:
    tier = {
        "above_input_tokens": 100_000,
        "input": 0.5,
        "output": 2.5,
        "cache_read": 0.05,
        "cache_write": 0.625,
    }
    rule = {**RULE, "provider_kind": "anthropic", "model": "claude-haiku-5-5*", "tier": tier}
    saved = client.put(PRICING, json={"expected_version": 0, "rules": [rule]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["rules"][0]["tier"] == tier
    assert client.get(PRICING).json()["rules"][0]["tier"] == tier
    assert mongo["settings"].find_one({"_id": "pricing"})["rules"][0]["tier"] == tier
    # 기본 가격의 tier 도 읽기에 실린다
    haiku = next(
        d for d in client.get(PRICING).json()["defaults"] if d["model"] == "claude-haiku-5-5*"
    )
    assert haiku["tier"]["above_input_tokens"] == 100_000
    # tier 칸도 규칙처럼 검증한다
    bad = {**rule, "tier": {**tier, "above_input_tokens": 0}}
    assert client.put(PRICING, json={"expected_version": 1, "rules": [bad]}).status_code == 422


def test_the_price_read_sits_in_the_usage_group() -> None:
    tool = next(t for t in CATALOG.tools if t.path == "/api/settings/pricing")
    assert group_of(tool) == "usage"
    assert ("PUT", "/api/settings/pricing") in CATALOG.excluded
