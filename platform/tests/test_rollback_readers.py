"""앞 판으로 되돌릴 때의 읽기 (P39b 15절). 이 판은 뒤 판이 더한 설정 절, 허브 항목 종류, tools 노드
칸을 만나도 터지지 않는다. 그래야 다음 판에서 이 판으로 되돌려도 화면과 컴파일이 산다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from daegam.auth.signer import server_audience
from daegam.services.agent_graph import validate
from daegam.services.platform_tools import _item
from daegam.store.agents import AgentSpec
from daegam.store.auth_reapply import own_audience
from daegam.store.base import utcnow
from daegam.store.settings import PLATFORM_ID, SETTINGS
from daegam.store.shared_clones import SharedClone
from tests.helpers.agents import make_ctx, node_of, template_spec
from tests.helpers.consts import GV, HUB, OTHER, OTHER_NS
from tests.helpers.hub import clone_name_of, other_tenant_gateway, shared_image_env


def test_settings_with_keys_of_another_release_still_read(client, mongo) -> None:
    """뒤 판이 더한 절도, 앞 판이 두었다가 이 판이 지운 칸(`spec_token_ttl`)도 읽을 때 버리고
    저장한 뒤에도 문서에 남긴다. 그래서 그 칸을 아는 판으로 되돌리면 그 판이 값을 다시 읽는다. 뒤
    판의 허브 항목 종류는 목록에서 빠진다."""
    kept = {"future_section": {"on": True}, "spec_token_ttl": 3600}
    mongo[SETTINGS].update_one({"_id": PLATFORM_ID}, {"$set": kept}, upsert=True)
    r = client.get("/settings")
    assert r.status_code == 200, r.text
    assert not kept.keys() & r.json().keys()
    assert client.patch("/settings", json={"default_model": "m"}).status_code == 200
    doc = mongo[SETTINGS].find_one({"_id": PLATFORM_ID})
    assert {k: doc[k] for k in kept} == kept

    later = _item("web-fetch", "http://issuer").to_doc() | {"kind": "later", "name": "later"}
    mongo["hub_items"].insert_one(later)
    r = client.get(HUB)
    assert r.status_code == 200, r.text
    assert "later" not in [i["name"] for i in r.json()]


def test_tools_nodes_ignore_keys_of_a_later_release() -> None:
    """뒤 판의 칸은 버리고 검증한다. 저장하는 사람에게는 경고로 알려 `allow_domain` 같은 오타가
    안전 규칙을 조용히 빼지 않게 한다."""
    spec = template_spec()
    data = node_of(spec, "tools-calculator")["data"]
    data["later_field"] = {"x": 1}
    data["allow_domain"] = ["example.com"]
    issues = validate(AgentSpec.model_validate(spec), make_ctx())
    assert "invalid_node_data" not in {i.code for i in issues}
    [warning] = [i for i in issues if i.code == "unknown_node_keys"]
    assert warning.level == "warning" and warning.node_id == "tools-calculator"
    assert warning.message.endswith("allow_domain, later_field")


def _rc1_clone(audience_switch: dict | None) -> dict:
    return {
        "_id": "clone-1",
        "source_item_id": "item-1",
        "cluster_id": "c1",
        "namespace": OTHER_NS,
        "tenant": OTHER,
        "name": "shared-img",
        "source": {"kind": "image"},
        "audience_switch": audience_switch,
    }


@pytest.mark.parametrize("reason", ["consumers_ready", "seven_days"])
def test_an_audience_switch_written_by_rc1_still_reads(reason) -> None:
    """2.0.0-rc1 은 소비자를 기다려 옮긴 서버의 기록에 consumers_ready·seven_days 를 적고 기다린
    흔적(`pending_since`·`seen_at`·`waiting`)을 남긴다. 이 판은 그 기록을 읽고 흔적을 그대로 둔다.
    읽지 못하면 그 클러스터의 복제본 스윕과 그 원본을 쓰는 Gateway 배포가 멈춘다."""
    now = utcnow()
    waiting = [{"kind": "gateway", "id": "g1"}]
    switch = {
        "pending_since": now - timedelta(days=7),
        "seen_at": now,
        "switched_at": now,
        "reason": reason,
        "waiting": waiting,
    }
    clone = SharedClone.from_doc(_rc1_clone(switch))
    assert own_audience(clone.audience_switch) and clone.audience_switch.reason == reason
    assert clone.to_doc()["audience_switch"]["waiting"] == waiting


@pytest.mark.parametrize("waiting", [False, True], ids=["adopted", "waiting"])
def test_an_image_clone_rc1_left_on_the_tenant_audience_moves_on_the_next_deploy(
    client, fake_clusters, mongo, waiting
) -> None:
    """2.0.0-rc1 이 테넌트 audience 로 입양한 image 기록(기록 없음)과 소비자를 기다리던 기록
    (`switched_at` 없음)은 그 원본을 쓰는 Gateway 가 다시 배포될 때 자기 audience 로 옮긴다. 이
    판의 소비자는 모두 두 audience 를 싣기 때문이다."""
    shared, fake = shared_image_env(client, fake_clusters)
    _profile, r = other_tenant_gateway(client, shared, tools=["Echo"])
    assert r.status_code == 201, r.text
    gateway = r.json()
    now = utcnow()
    rc1 = {"pending_since": now, "seen_at": now, "waiting": [{"kind": "gateway", "id": "g1"}]}
    mongo["shared_clones"].update_one(
        {"source_item_id": shared["id"]}, {"$set": {"audience_switch": rc1 if waiting else None}}
    )
    mongo["gateways"].update_one({"_id": gateway["id"]}, {"$set": {"state": "failed"}})
    portal, state = client.portal, client.app.state
    doc = portal.call(state.gateways.get, gateway["id"])
    assert portal.call(state.gateway_service.provision, doc).state == "deployed"

    record = mongo["shared_clones"].find_one({"source_item_id": shared["id"]})
    switch = record["audience_switch"]
    assert switch["reason"] == "new_server" and switch["switched_at"] is not None
    assert "pending_since" not in switch
    clone = fake.get(GV, "MCPServer", clone_name_of(shared), OTHER_NS)
    assert clone["spec"]["oidcConfigRef"]["audience"] == server_audience(OTHER, record["_id"])
