"""공유 복제본의 기록 (P50 1절, D23).

Gateway 의 배포가 복제본마다 기록을 만들고, 재적용이 원본의 변경과 렌더 번호로 복제본을 다시
렌더한다. 경계 밖 테넌트의 재배포는 기록의 원본으로 렌더한다. 스윕은 이 설치의 라벨이 있는 버려진
복제본만 지우고 이 설치의 것이 아닌 복제본은 건드리지 않는다. 복제본을 두는 것은 image 항목이다.
전에 만든 remote 항목의 복제본(MCPRemoteProxy)은 다시 렌더하지 않고 스윕이 지우기만 한다.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from daegam.k8s.labels import LABEL_PLATFORM_ID, LABEL_SOURCE_ID
from daegam.services import shared_clones
from daegam.services.budget import resumed
from daegam.services.gateways import GatewayError
from daegam.store.auth_reapply import ClusterAuthState
from daegam.store.base import utcnow
from tests.helpers.clusters import register_cluster
from tests.helpers.consts import GV, GW, HUB, OTHER_NS, T2
from tests.helpers.hub import (
    clone_name_of,
    gateway_on_remote_clone,
    other_tenant_gateway,
    shared_env,
    shared_image_env,
)
from tests.helpers.workers import OneItemBudget, run_clone_sweep, run_data_plane_auth

IMAGE = "ghcr.io/acme/tools:1"
NEW_IMAGE = "ghcr.io/acme/tools:2"


def _record(mongo, item_id: str) -> dict:
    return mongo["shared_clones"].find_one({"source_item_id": item_id})


def _deployed(client, fake_clusters):
    """다른 테넌트가 shared image 항목 하나를 쓰는 Gateway 를 배포한다. (항목, 배포, 가짜)."""
    shared, fake = shared_image_env(client, fake_clusters)
    _profile, r = other_tenant_gateway(client, shared, tools=["Echo"])
    assert r.status_code == 201, r.text
    return shared, r.json(), fake


def test_a_retried_deploy_outside_the_boundary_renders_the_recorded_source(
    client, fake_clusters, mongo
):
    """경계 밖 테넌트의 Gateway 를 다시 provision 하면 복제본을 기록의 원본으로 렌더한다. 기록이
    없으면 렌더할 원본이 없어 422 hub_item_not_consumable 이고 복제본을 적용하지 않는다."""
    shared, gateway, fake = _deployed(client, fake_clusters)
    assert client.put(f"{HUB}/{shared['id']}/access", json={}).status_code == 200
    mongo["hub_items"].update_one({"_id": shared["id"]}, {"$set": {"source.image": NEW_IMAGE}})
    portal, state = client.portal, client.app.state

    def retry():
        mongo["gateways"].update_one({"_id": gateway["id"]}, {"$set": {"state": "failed"}})
        doc = portal.call(state.gateways.get, gateway["id"])
        return portal.call(state.gateway_service.provision, doc)

    name = clone_name_of(shared)
    assert retry().state == "deployed"
    assert fake.get(GV, "MCPServer", name, OTHER_NS)["spec"]["image"] == IMAGE
    assert _record(mongo, shared["id"])["frozen"] is True

    mongo["shared_clones"].delete_many({})
    fake.delete(GV, "MCPServer", name, OTHER_NS)
    with pytest.raises(GatewayError) as e:
        retry()
    assert e.value.code == "hub_item_not_consumable"
    assert fake.get(GV, "MCPServer", name, OTHER_NS) is None


def test_the_sweep_skips_clusters_in_backoff_and_starts_with_the_one_left_waiting(
    client, fake_clusters, monkeypatch
):
    """인증 재적용이 연결하지 못해 미뤄 둔 클러스터는 건너뛴다. 시간이 모자라 미룬 클러스터는 다음
    틱이 먼저 본다. 한 클러스터의 실패는 다른 클러스터를 막지 않는다."""
    _deployed(client, fake_clusters)
    (edge,) = [c["id"] for c in client.get("/clusters").json()]
    far = register_cluster(client, "far")
    seen: list[str] = []

    async def run(sweep):
        seen.append(sweep.cluster.id)

    # 시계로 재면 바쁜 기계에서 첫 클러스터 전에 몫을 넘긴다
    monkeypatch.setattr(shared_clones, "Budget", OneItemBudget)
    monkeypatch.setattr(shared_clones._ClusterSweep, "run", run)
    assert run_clone_sweep(client)["deferred"] == [f"cluster:{far}"]
    assert run_clone_sweep(client)["deferred"] == [f"cluster:{edge}"]
    assert seen == [edge, far]

    states = client.app.state.data_plane_auth.store
    later = utcnow() + timedelta(hours=1)
    blocked = ClusterAuthState(cluster_id=edge, unreachable=True, retry_at=later)
    client.portal.call(states.put_cluster, blocked)

    async def fail(sweep):
        seen.append(sweep.cluster.id)
        raise RuntimeError("boom")

    monkeypatch.setattr(shared_clones._ClusterSweep, "run", fail)
    swept = run_clone_sweep(client)
    assert swept["unreachable"] == [f"cluster:{edge}"]
    assert swept["failed"] == [f"cluster:{far}"] and seen[-1] == far


def test_a_long_record_list_resumes_after_the_record_where_the_last_tick_stopped():
    records = [SimpleNamespace(id=i) for i in ("a", "b", "c", "d")]

    def by_id(r):
        return r.id

    assert [r.id for r in resumed(records, "b", key=by_id, wrap=True)] == ["c", "d", "a", "b"]
    assert resumed(records, None, key=by_id, wrap=True) == records
    # 한 시간에 한 번 끝까지 도는 스윕은 감지 않는다
    assert [r.id for r in resumed(records, "b", key=by_id, wrap=False)] == ["c", "d"]


def test_clones_of_another_installation_are_left_alone(client, fake_clusters, mongo):
    """다른 설치의 라벨이 있는 복제본과 라벨이 없는 복제본은 지우지 않는다."""
    shared, _gateway, fake = _deployed(client, fake_clusters)
    ours = fake.get(GV, "MCPServer", clone_name_of(shared), OTHER_NS)
    labelled = {**ours["metadata"]["labels"], LABEL_PLATFORM_ID: "another-install"}
    unlabelled = {k: v for k, v in ours["metadata"]["labels"].items() if k != LABEL_PLATFORM_ID}
    unlabelled[LABEL_SOURCE_ID] = "item-of-another-install"
    for name, labels in (("shared-labelled", labelled), ("shared-unlabelled", unlabelled)):
        foreign = copy.deepcopy(ours)
        foreign["metadata"] = {"name": name, "namespace": OTHER_NS, "labels": labels}
        fake.apply(foreign)
    assert run_clone_sweep(client) == {}
    assert fake.get(GV, "MCPServer", "shared-labelled", OTHER_NS) is not None
    assert fake.get(GV, "MCPServer", "shared-unlabelled", OTHER_NS) is not None
    assert mongo["shared_clones"].count_documents({}) == 1


def test_an_unused_clone_is_deleted_only_with_this_installations_label(client, fake_clusters):
    """기록도 Gateway 도 없는 복제본은 이 설치의 라벨이 있을 때만 지운다. 라벨이 없으면 남긴다."""
    shared, _gateway, fake = _deployed(client, fake_clusters)
    ours = fake.get(GV, "MCPServer", clone_name_of(shared), OTHER_NS)
    unlabelled = {k: v for k, v in ours["metadata"]["labels"].items() if k != LABEL_PLATFORM_ID}
    strays = (
        ("team-a", "shared-stray", unlabelled),
        ("elsewhere", "shared-labelled", ours["metadata"]["labels"]),
    )
    for namespace, name, labels in strays:
        stray = copy.deepcopy(ours)
        stray["metadata"] = {"name": name, "namespace": namespace, "labels": labels}
        fake.apply(stray)
    assert run_clone_sweep(client)["deleted"] == ["elsewhere/shared-labelled"]
    assert fake.get(GV, "MCPServer", "shared-stray", "team-a") is not None
    assert fake.get(GV, "MCPServer", clone_name_of(shared), OTHER_NS) is not None


def test_a_remote_clone_left_by_an_earlier_release_is_swept_but_never_rendered_again(
    client, fake_clusters, mongo
):
    """전에 만든 remote 항목의 복제본은 쓰는 Gateway 가 있는 동안 적용된 그대로 남는다. 손으로
    지워도 다시 만들라는 표시를 하지 않고(렌더할 것이 없다), 재적용도 그 CR 을 건드리지 않는다.
    기록도 Gateway 도 없이 남은 이 설치의 복제본과, 쓰는 Gateway 가 없어진 복제본은 스윕이
    지운다."""
    _cid, _items, shared, fake = shared_env(client, fake_clusters)
    gateway = gateway_on_remote_clone(client, shared)
    name = clone_name_of(shared)
    before = fake.get(GV, "MCPRemoteProxy", name, OTHER_NS)
    mongo["shared_clones"].update_many({}, {"$set": {"render_epoch": 0}})
    assert _record(mongo, shared["id"])["_id"] in run_data_plane_auth(client)["reapplied"]
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) == before

    stray = copy.deepcopy(before)
    stray["metadata"] = {**before["metadata"], "name": "shared-stray", "namespace": "elsewhere"}
    fake.apply(stray)
    assert run_clone_sweep(client) == {"deleted": ["elsewhere/shared-stray"]}

    fake.delete(GV, "MCPRemoteProxy", name, OTHER_NS)
    assert run_clone_sweep(client) == {}
    assert run_data_plane_auth(client)["reapplied"] == []
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is None

    fake.apply(before)
    assert client.delete(f"{GW}/{gateway['id']}", headers=T2).status_code == 200
    assert run_clone_sweep(client) == {"deleted": [f"{OTHER_NS}/{name}"]}
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is None
    assert mongo["shared_clones"].count_documents({}) == 0
