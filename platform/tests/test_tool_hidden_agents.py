"""P52 F5: 규칙이 Agent 가 고른 Tool 을 숨기면 Agent 상태가 `agent_tool_hidden` 을 원인마다 낸다.

토큰 인증 모드에서만 낸다. header 모드는 누가 부를지의 규칙을 집행하지 않는다(2절). 규칙이 이
Agent 를 이름 대도 그 Agent 의 다음 렌더가 자기 id 를 실은 토큰을 아직 내지 않았으면 원인이 토큰의
재발급이다(3절).
"""

from __future__ import annotations

from typing import Any

from tests.helpers.consts import HUB
from tests.helpers.workers import run_data_plane_auth
from tests.test_tool_marks_agents import _deploy, _set_rules, _warnings

DEV = {"callers": {"roles": ["developer"]}}
HIDDEN = "agent_tool_hidden"


def _line(cause: str, tools: str) -> str:
    return f"{HIDDEN}: a Tool rule hides Tools this Agent selected (cause: {cause}; tools: {tools})"


def _gateway_rules(mongo, gateway_id: str, item_id: str, rules: dict[str, Any]) -> None:
    """Gateway 의 모든 리비전에 그 백엔드의 규칙을 둔다. 서비스를 거치지 않는다."""
    doc = mongo["gateways"].find_one({"_id": gateway_id})
    for rev in doc["revisions"]:
        rev["backends"] = [{"hub_item_id": item_id, "tool_rules": rules}]
    mongo["gateways"].replace_one({"_id": gateway_id}, doc)


def test_an_item_rule_hides_the_tool_and_says_so(local_admin, fake_clusters, mongo):
    _spec, inst, item = _deploy(local_admin, mongo, {"send_mail": DEV})
    assert _warnings(local_admin, inst["id"], HIDDEN) == [_line("item", "send_mail")]
    _set_rules(mongo, item, {})
    assert _warnings(local_admin, inst["id"], HIDDEN) == []


def test_header_mode_enforces_no_caller_rule_and_warns_nothing(client, fake_clusters, mongo):
    _spec, inst, item = _deploy(client, mongo, {"send_mail": DEV})
    # 규칙은 읽혔다. 항목은 header 모드라 집행하지 않는다고 알리고(견본 항목의 클러스터는 판을 몰라
    # rules_toolhive_old 도 낸다), Agent 는 숨는 Tool 이 없다
    r = client.get(f"{HUB}/{item.id}")
    assert r.status_code == 200, r.text
    assert "rules_not_enforced" in [w.split(":")[0] for w in r.json()["status"]["warnings"]]
    assert _warnings(client, inst["id"], HIDDEN) == []


def test_a_gateway_rule_hides_until_it_names_the_agent_and_its_tokens_carry_the_id(
    local_admin, fake_clusters, mongo
):
    spec, inst, item = _deploy(local_admin, mongo, {})
    gateway_id = next(
        n["data"]["source"]["id"] for n in spec["graph"]["nodes"] if n["type"] == "tools"
    )
    _gateway_rules(mongo, gateway_id, item.id, {"list_mail": DEV})
    assert _warnings(local_admin, inst["id"], HIDDEN) == [_line("not_named", "list_mail")]
    _gateway_rules(mongo, gateway_id, item.id, {"list_mail": {"callers": {"agents": [inst["id"]]}}})
    local_admin.app.state.agent_service.rule_scopes.forget("default")
    # 이름 댄 Agent 의 다음 재적용이 자기 id 를 실은 토큰을 낼 때까지 숨는다
    assert _warnings(local_admin, inst["id"], HIDDEN) == [_line("reissue", "list_mail")]
    run_data_plane_auth(local_admin)
    assert _warnings(local_admin, inst["id"], HIDDEN) == []
