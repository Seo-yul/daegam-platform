"""배포 때의 Tool 핀 (P39b 14절 5부). 배포는 tools 노드마다 그때 쓰는 Tool 의 이름·효과·웹 종류를
핀하고, 컴파일은 핀한 이름 가운데 소스에 지금 있는 것을 allow 로 싣는다. 소스가 바뀌면 상태가
`tools_behind`·`tools_missing` 을 알리고 재배포가 새 이름을 받는다. 핀 없는 Agent 가 쓰는 Gateway 에
웹 백엔드를 더하는 배포는 409 다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.errors import ServiceError
from daegam.services.agent_features import WEB_GUARD
from daegam.services.platform_tools import _item
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    WEB_FETCH,
    SourceTools,
)
from daegam.services.tool_pins import apply_pins, pinned_tools
from daegam.store.agents import AgentSpec, ToolPin
from daegam.store.hub import HubItem, HubK8s, HubSource
from tests.helpers.agents import (
    agent_env,
    deploy_spec,
    node_of,
    template_for,
)
from tests.helpers.consts import AG
from tests.helpers.docs import gateway_doc
from tests.helpers.gateways import WEB_BACKENDS, backend_snapshot, seed_deployed_backends

NODE = "tools-calculator"


def _advertise(mongo, gateway_id: str, names: list[str]) -> None:
    """Gateway 배포 스냅샷의 백엔드 하나가 names 를 광고하게 한다."""
    seed_deployed_backends(mongo, gateway_id, [backend_snapshot("spike", "remote", names)])


def _deploy_open(client, mongo, names: list[str] | None = None) -> tuple[dict, dict]:
    """고른 Tool 이 없는 tools 노드로 Gateway 를 쓰는 Agent 를 배포한다. (Gateway, Agent)."""
    _cid, ds, gw = agent_env(client, mongo)
    _advertise(mongo, gw["id"], names or ["Add", "Divide"])
    spec = template_for(gw, ds)
    node_of(spec, NODE)["data"]["allow_tools"] = []
    _p, inst = deploy_spec(client, spec)
    return gw, inst


def _compile(client, agent_id: str) -> dict:
    service = client.app.state.agent_service

    async def compile_doc() -> dict:
        doc = await service.agents.get(agent_id)
        return await service.compile(doc, token_envs=None)

    return client.portal.call(compile_doc)


def _stored(mongo, agent_id: str) -> dict[str, Any]:
    return mongo["agents"].find_one({"_id": agent_id})


def _warnings(client, agent_id: str) -> list[str]:
    return client.get(f"{AG}/{agent_id}").json()["status"]["warnings"]


def _spec(*sources: tuple[str, list[str]]) -> AgentSpec:
    nodes: list[dict[str, Any]] = [{"id": "agent-main", "type": "agent", "data": {"name": "main"}}]
    for node_id, allow in sources:
        source = {"kind": "gateway_instance", "id": f"gw-{node_id}"}
        data = {"source": source, "allow_tools": allow}
        nodes.append({"id": node_id, "type": "tools", "data": data})
    return AgentSpec.model_validate({"graph": {"nodes": nodes, "edges": []}})


def test_allow_is_the_pins_still_offered_and_an_empty_source_drops_out() -> None:
    """핀 ∩ 지금 이름이다. 지금 이름을 모르면 핀 그대로, 남는 이름이 없으면 소스와 그 참조를 뺀다.
    핀이 없는 소스와 이름을 모를 때 핀한 소스는 손대지 않는다."""
    spec = _spec(("a", []), ("b", []), ("c", []), ("d", []), ("e", ["X"]))
    doc = {
        "tools": [{"ref": ref, "allow": ["X"] if ref == "e" else []} for ref in "abcde"],
        "main": {"tools": list("abcde")},
        "subagents": [{"tools": ["c", "d"]}],
    }
    pins = {
        "a": ToolPin(names=["Add", "Gone"]),
        "b": ToolPin(names=["Add"]),
        "c": ToolPin(names=["Gone"]),
        "d": ToolPin(names=None),
    }
    sources = {
        "gw-a": SourceTools(names=("Add", "New")),
        "gw-b": SourceTools(names=None),
        "gw-c": SourceTools(names=("Add",)),
    }
    out = apply_pins(doc, spec, pins, sources)
    assert {t["ref"]: t["allow"] for t in out["tools"]} == {
        "a": ["Add"],
        "b": ["Add"],
        "d": [],
        "e": ["X"],
    }
    assert out["main"]["tools"] == ["a", "b", "d", "e"] and out["subagents"] == [{"tools": ["d"]}]
    assert doc["tools"][0]["allow"] == [], "받은 문서는 바꾸지 않는다"


def test_pins_keep_the_stronger_effect() -> None:
    """지금 소스가 더 약한 효과를 말해도 배포 때의 센 효과와 웹 종류를 쓴다."""
    pin = ToolPin(names=["Add"], effects={"Add": EFFECT_WEB}, web={"Add": WEB_FETCH})
    source = SourceTools(names=("Add",), effects={"Add": EFFECT_READ})
    names, tools = pinned_tools([], source, pin)
    assert names == ["Add"] and tools.effect("Add") == EFFECT_WEB
    assert tools.web == {"Add": WEB_FETCH}


def test_status_reports_drift_until_the_redeploy(client, fake_clusters, mongo) -> None:
    """새 Tool 은 재배포 때 들어오고, 사라진 이름은 경고로 보이고 호출되지 않는다."""
    gw, inst = _deploy_open(client, mongo)
    assert not [w for w in _warnings(client, inst["id"]) if w.startswith("tools_")]
    _advertise(mongo, gw["id"], ["Add", "Multiply"])
    drift = [w for w in _warnings(client, inst["id"]) if w.startswith("tools_")]
    assert drift == [f"tools_behind: {NODE}", f"tools_missing: {NODE}"]
    assert _compile(client, inst["id"])["tools"][0]["allow"] == ["Add"]
    r = client.post(f"{AG}/{inst['id']}/redeploy", json={})
    assert r.status_code == 200, r.text
    assert _stored(mongo, inst["id"])["tool_pins"][NODE]["names"] == ["Add", "Multiply"]
    assert not [w for w in _warnings(client, inst["id"]) if w.startswith("tools_")]


def test_a_web_backend_removed_and_added_again_keeps_the_guard(client, fake_clusters, mongo):
    """핀에 웹 Tool 이 있으면 Gateway 가 그 백엔드를 뺐다가 다시 넣어도 문서는 `web_guard` 를
    요구한다. 뺀 동안에는 allow 에 없어서 다시 읽기로 들어오지 않는다."""
    _cid, ds, gw = agent_env(client, mongo)
    seed_deployed_backends(mongo, gw["id"], WEB_BACKENDS)
    spec = template_for(gw, ds)
    node_of(spec, NODE)["data"].update({"allow_tools": [], "approval": {}, "timeouts": {}})
    _p, inst = deploy_spec(client, spec)
    assert _stored(mongo, inst["id"])["tool_pins"][NODE]["web"] == {"web_fetch": WEB_FETCH}
    _advertise(mongo, gw["id"], ["Add"])
    removed = _compile(client, inst["id"])
    assert WEB_GUARD in removed["features"] and removed["tools"][0]["allow"] == ["Add"]
    seed_deployed_backends(mongo, gw["id"], WEB_BACKENDS)
    [source] = _compile(client, inst["id"])["tools"]
    assert source["allow"] == ["Add", "web_fetch"] and source["web"]["fetch"] == "web_fetch"


def test_an_unpinned_agent_whose_source_gained_web_tools_must_redeploy(
    client, fake_clusters, mongo
) -> None:
    """핀이 없으면 웹 Tool 이 생긴 소스를 이름으로 정할 수 없다. 스키마 오류가 아니라 409
    `redeploy_required` 다."""
    gw, inst = _deploy_open(client, mongo, ["Add"])
    seed_deployed_backends(mongo, gw["id"], WEB_BACKENDS)
    mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"tool_pins": {}}})
    with pytest.raises(ServiceError) as e:
        _compile(client, inst["id"])
    assert (e.value.status, e.value.code) == (409, "redeploy_required")
    assert e.value.extra["nodes"] == [NODE]


def _check_web_pins(client, gateway_id: str, item: HubItem | None = None) -> list[str]:
    """Gateway 에 플랫폼 web-fetch 를 더하는 배포의 핀 검사다."""
    service = client.app.state.gateway_service
    item = item or _item("web-fetch", "http://issuer")

    async def run() -> list[str]:
        doc = await service.gateways.get(gateway_id)
        return await service.validator._check_web_pins([item], doc)

    return client.portal.call(run)


def test_adding_a_web_backend_waits_for_unpinned_agents(client, fake_clusters, mongo) -> None:
    """핀 없는 Agent(이 기능 전에 배포된 것)가 쓰는 Gateway 에 웹 백엔드를 더하면 409 다. 이름을
    모를 때 핀한 Agent 는 재배포로 풀리지 않아 다른 409 다. 모두 핀한 Agent 면 다음 재배포에
    받는다는 경고다."""
    gw, inst = _deploy_open(client, mongo)
    [warning] = _check_web_pins(client, gw["id"])
    assert warning.startswith("web_backend_pinned_agents: 1 agent(s)") and inst["id"] in warning
    for pins, code in (
        ({}, "web_backend_unpinned_agents"),
        ({NODE: {"names": None}}, "web_backend_unknown_pins"),
    ):
        mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"tool_pins": pins}})
        with pytest.raises(ServiceError) as e:
            _check_web_pins(client, gw["id"])
        assert (e.value.status, e.value.code) == (409, code)
        assert e.value.extra["agent_ids"] == [inst["id"]]


def test_a_consumer_gateway_with_unknown_names_blocks_the_web_gain(client, fake_clusters, mongo):
    """아래 Gateway 가 웹 Tool 을 얻으면 그 게시 항목을 배포한 위 Gateway 도 닿는다. 위 Gateway 의
    배포 스냅샷에 이름을 모르는 백엔드가 있으면 아래 Gateway 의 배포가 422 다. 위 Gateway 를 쓰는
    Agent 가 이름 없는 웹 소스를 받지 않게 하려는 것이다(probe_holes2 의 두 단 사슬). 이름을 다
    알아도 위 Gateway 를 핀 없이 쓰는 Agent 가 있으면 막는다."""
    upper, inst = _deploy_open(client, mongo)
    lower = gateway_doc("gi-lower", state="deployed")
    pub = HubItem(
        tenant="default",
        name="lower",
        kind="published",
        cluster_id="c1",
        source=HubSource(gateway_instance_id=lower.id),
        k8s=HubK8s(kind="VirtualMCPServer", name="gw-lower", namespace="daegam-default"),
    )
    mongo["gateways"].insert_one(lower.to_doc())
    mongo["hub_items"].insert_one(pub.to_doc())
    snapshots = [
        backend_snapshot("spike", "remote", []),
        backend_snapshot("lower", "published", ["Add"], hub_item_id=pub.id),
    ]
    seed_deployed_backends(mongo, upper["id"], snapshots)
    with pytest.raises(ServiceError) as e:
        _check_web_pins(client, lower.id)
    assert (e.value.status, e.value.code) == (422, "web_gateway_unknown_names")
    name = mongo["gateways"].find_one({"_id": upper["id"]})["name"]
    assert e.value.extra["items"] == [f"{name}/spike"]

    seed_deployed_backends(mongo, upper["id"], snapshots[1:])
    mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"tool_pins": {}}})
    with pytest.raises(ServiceError) as e:
        _check_web_pins(client, lower.id)
    assert e.value.code == "web_backend_unpinned_agents"
    assert e.value.extra["agent_ids"] == [inst["id"]]


def test_a_gateway_deploy_warns_when_pinned_agents_keep_other_names(client, fake_clusters, mongo):
    """배포할 스냅샷의 이름이 Agent 의 핀과 다르면 그 Agent 는 재배포까지 핀한 Tool 을 쓴다는
    경고다. 같으면 경고가 없다."""
    gw, inst = _deploy_open(client, mongo)
    service = client.app.state.gateway_service

    def warn(names: list[str]) -> list[str]:
        _advertise(mongo, gw["id"], names)

        async def run() -> list[str]:
            return await service.validator.pinned_names_warning(
                await service.gateways.get(gw["id"])
            )

        return client.portal.call(run)

    assert warn(["Add", "Divide"]) == []
    [warning] = warn(["Add"])
    assert warning == (
        "pinned_agents_keep_tools: 1 agent(s) keep the tools pinned at their deploy until they "
        f"are redeployed: {inst['id']}"
    )
