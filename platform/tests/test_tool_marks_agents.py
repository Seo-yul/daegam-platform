"""P52 F4: Agent 가 쓰는 소스의 읽기 전용 표시와 규칙이 검사, 상태 경고에 닿는다.

Gateway 의 `image` 백엔드는 허브 항목의 표시를 따르고(`ToolEffectLoader` 가 항목을 읽는다), 배포한
Agent 는 소스의 표시가 바뀌면 `tool_marks_changed` 를 다시 배포할 때까지 보인다. 거절된 호출을
견디지 못하는 이미지가 규칙 있는 Tool 을 쓰면 `runtime_tool_refusals` 다.
"""

from __future__ import annotations

from typing import Any

from daegam.store.hub import HubItem, HubK8s
from tests.helpers.agents import agent_env, deploy_spec, node_of, template_for
from tests.helpers.consts import AG
from tests.helpers.gateways import backend_snapshot, seed_deployed_backends

NODE = "tools-calculator"


def _image_item(mongo, rules: dict[str, Any]) -> HubItem:
    item = HubItem(
        tenant="default",
        name="mail",
        kind="image",
        cluster_id="c1",
        k8s=HubK8s(kind="MCPServer", name="mail", namespace="daegam-default"),
        tool_names=["send_mail", "list_mail"],
        tool_rules=rules,
    )
    mongo["hub_items"].insert_one(item.to_doc())
    return item


def _set_rules(mongo, item: HubItem, rules: dict[str, Any]) -> None:
    mongo["hub_items"].update_one({"_id": item.id}, {"$set": {"tool_rules": rules}})


def _deploy(client, mongo, rules: dict[str, Any]) -> tuple[dict, dict, HubItem]:
    """image 백엔드 하나를 둔 Gateway 를 고른 Tool 없이 쓰는 Agent 를 배포한다."""
    _cid, ds, gw = agent_env(client, mongo)
    item = _image_item(mongo, rules)
    snapshot = backend_snapshot("mail", "image", ["send_mail", "list_mail"], hub_item_id=item.id)
    seed_deployed_backends(mongo, gw["id"], [snapshot])
    spec = template_for(gw, ds)
    node_of(spec, NODE)["data"]["allow_tools"] = []
    _p, inst = deploy_spec(client, spec)
    return spec, inst, item


def _warnings(client, agent_id: str, prefix: str) -> list[str]:
    warnings = client.get(f"{AG}/{agent_id}").json()["status"]["warnings"]
    return [w for w in warnings if w.startswith(prefix)]


def test_validate_shows_the_items_mark_and_that_it_stands(client, fake_clusters, mongo):
    spec, _inst, _item = _deploy(client, mongo, {"list_mail": {"read_only": True}})
    r = client.post(f"{AG}/validate", json=spec)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_effects"][NODE] == {"send_mail": "write", "list_mail": "read"}
    assert body["tool_origins"][NODE] == {"send_mail": "image", "list_mail": "image"}
    assert body["tool_marked"][NODE] == ["send_mail", "list_mail"]


def test_an_item_mark_change_shows_until_the_agent_is_redeployed(client, fake_clusters, mongo):
    _spec, inst, item = _deploy(client, mongo, {"list_mail": {"read_only": True}})
    assert _warnings(client, inst["id"], "tool_marks_changed") == []
    _set_rules(mongo, item, {"list_mail": {"read_only": False}})
    assert _warnings(client, inst["id"], "tool_marks_changed") == [f"tool_marks_changed: {NODE}"]
    r = client.post(f"{AG}/{inst['id']}/redeploy", json={})
    assert r.status_code == 200, r.text
    assert _warnings(client, inst["id"], "tool_marks_changed") == []


def test_an_old_image_that_uses_a_ruled_tool_warns(client, fake_clusters, mongo):
    _spec, inst, _item = _deploy(
        client, mongo, {"send_mail": {"callers": {"roles": ["developer"]}}}
    )
    assert _warnings(client, inst["id"], "runtime_tool_refusals") == []  # 기록이 없으면 모른다
    image = mongo["agents"].find_one({"_id": inst["id"]})["effective_config"]["images"]
    record = {"image": image["agent_image"], "features": ["turn_tokens"], "warnings": []}
    mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"runtime": record}})
    assert len(_warnings(client, inst["id"], "runtime_tool_refusals")) == 1
    record["features"] = ["turn_tokens", "tool_refusals"]
    mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"runtime": record}})
    assert _warnings(client, inst["id"], "runtime_tool_refusals") == []
