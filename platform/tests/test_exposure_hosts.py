"""공개 host 의 선점 (P70, D82).

host 는 플랫폼 전체에서 살아 있는 배포 하나의 것이다. 읽어서 확인하는 것만으로는 함께 도는 두 요청을
가르지 못하므로, 노출을 저장하기 전에 `exposure_hosts` 에 host 를 선점한다. 해제와 삭제의 마지막
단계가 놓고, 리컨실의 한 단계가 남은 선점을 쓸고 기록 없는 노출에 기록을 준다.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from daegam.k8s.client import ClusterUnreachable
from daegam.store.base import utcnow
from tests.helpers.consts import MS
from tests.helpers.docs import seed_server
from tests.helpers.exposure import cluster_with_ingress, prep_ingress
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy

HOST = "calc.example"


def _claims(mongo) -> dict[str, str]:
    return {d["_id"]: d["resource_id"] for d in mongo["exposure_hosts"].find()}


def test_a_host_another_resource_holds_is_a_conflict_and_leaves_no_claim(
    client, fake_clusters, mongo
):
    """읽기 검사를 지나도 다른 리소스가 선점한 host 는 409 host_conflict 다. 진 쪽은 선점을 남기지
    않고, detail 은 상대의 테넌트도 리소스도 밝히지 않는다."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    mongo["exposure_hosts"].insert_one(
        {
            "_id": HOST,
            "kind": "gateway",
            "resource_id": "other",
            "tenant": "beta",
            "nonce": "n0",
            "claimed_at": utcnow(),
        }
    )
    r = clone_and_deploy(client, mp, name="calc-a", exposure={"enabled": True, "host": HOST})
    assert detail_code(r) == (409, "host_conflict")
    detail = r.json()["detail"]
    assert set(detail) == {"code", "detail"} and "beta" not in detail["detail"]
    assert "other" not in detail["detail"]
    assert _claims(mongo) == {HOST: "other"}


def test_a_save_that_fails_after_the_claim_removes_it(client, fake_clusters, mongo, monkeypatch):
    """선점한 뒤 저장이 실패하면(409 state_conflict 와 같은 것) 이 요청이 넣은 선점을 지운다."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    svc = client.app.state.mcp_server_service
    real = type(svc)._transition

    async def lose(self, inst, expect, *, action):
        if action == "deploy":
            raise self._state_conflict(inst, action)
        return await real(self, inst, expect, action=action)

    monkeypatch.setattr(type(svc), "_transition", lose)
    r = clone_and_deploy(client, mp, name="calc-a", exposure={"enabled": True, "host": HOST})
    assert detail_code(r) == (409, "state_conflict")
    assert _claims(mongo) == {}


def test_deploy_claims_and_undeploy_and_delete_release(client, fake_clusters, mongo):
    """배포가 선점하고, 해제와 삭제가 끝나면 다른 문서가 바로 그 host 를 쓸 수 있다. Basic 편집으로
    host 를 바꿔도 옛 선점은 해제까지 남는다. 옛 Ingress 가 아직 그 host 로 답할 수 있고, 옛 사본의
    전이가 옛 host 를 되써도 선점이 그 host 를 지킨다."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    a = clone_and_deploy(client, mp, name="calc-a", exposure={"enabled": True, "host": HOST})
    assert a.status_code == 201, a.text
    aid = a.json()["id"]
    assert _claims(mongo) == {HOST: aid}
    r = clone_and_deploy(client, mp, name="calc-b", exposure={"enabled": True, "host": HOST})
    assert detail_code(r) == (409, "host_conflict")
    r = client.patch(f"{MS}/{aid}", json={"exposure": {"enabled": True, "host": "new.example"}})
    assert r.status_code == 200, r.text
    assert _claims(mongo) == {HOST: aid, "new.example": aid}
    r = clone_and_deploy(client, mp, name="calc-b", exposure={"enabled": True, "host": HOST})
    assert detail_code(r) == (409, "host_conflict")
    assert "held until" in r.json()["detail"]["detail"]
    assert client.post(f"{MS}/{aid}/undeploy").status_code == 200
    assert _claims(mongo) == {}
    b = clone_and_deploy(client, mp, name="calc-b", exposure={"enabled": True, "host": HOST})
    assert b.status_code == 201, b.text
    assert client.delete(f"{MS}/{b.json()['id']}").status_code == 200
    assert _claims(mongo) == {}


def test_undeploy_keeps_a_claim_taken_again_after_the_draft(
    client, fake_clusters, mongo, monkeypatch
):
    """해제는 마지막 전이 앞에서 읽은 nonce 로 지운다. 전이 바로 뒤 다시 선점한 기록은 남는다."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    a = clone_and_deploy(client, mp, name="calc-a", exposure={"enabled": True, "host": HOST})
    aid = a.json()["id"]
    svc = client.app.state.mcp_server_service
    real = type(svc)._transition

    async def reclaim_after_draft(self, inst, expect, *, action):
        await real(self, inst, expect, action=action)
        if action == "finish undeploy":
            mongo["exposure_hosts"].update_one(
                {"_id": HOST}, {"$set": {"nonce": "fresh", "claimed_at": utcnow()}}
            )

    monkeypatch.setattr(type(svc), "_transition", reclaim_after_draft)
    assert client.post(f"{MS}/{aid}/undeploy").status_code == 200
    assert mongo["exposure_hosts"].find_one({"_id": HOST})["nonce"] == "fresh"


def _old_claim(mongo, host: str, resource_id: str, nonce: str = "n0", age: int = 10) -> None:
    mongo["exposure_hosts"].insert_one(
        {
            "_id": host,
            "kind": "mcp_server",
            "resource_id": resource_id,
            "tenant": "default",
            "nonce": nonce,
            "claimed_at": utcnow() - timedelta(minutes=age),
        }
    )


def test_the_sweep_frees_unheld_hosts_and_adoption_claims_the_rest(client, mongo):
    """쓸기는 유예보다 오래된 선점 가운데 리소스가 없거나 draft 이거나, 그 host 를 이름에 두지
    않으면서 deploying·failed 가 아닌 것을 지운다. 다음에 기록 없는 살아 있는 노출에 기록을 준다."""
    seed_server(mongo, "live", "live.example")
    seed_server(mongo, "moved", "moved-new.example")
    seed_server(mongo, "failing", "failing-new.example")
    mongo["mcp_servers"].update_one({"_id": "failing"}, {"$set": {"state": "failed"}})
    _old_claim(mongo, "gone.example", "nobody")
    _old_claim(mongo, "moved-old.example", "moved")
    _old_claim(mongo, "failing-old.example", "failing")
    _old_claim(mongo, "young.example", "nobody", age=1)
    svc = client.app.state.mcp_server_service
    result = client.portal.call(svc.settle_host_claims, utcnow())
    assert sorted(result["released"]) == ["gone.example", "moved-old.example"]
    claims = _claims(mongo)
    assert claims["failing-old.example"] == "failing" and claims["young.example"] == "nobody"
    for host, rid in (
        ("live.example", "live"),
        ("moved-new.example", "moved"),
        ("failing-new.example", "failing"),
    ):
        assert claims[host] == rid


def test_the_sweep_keeps_a_claim_refreshed_after_it_read(client, mongo, monkeypatch):
    """쓸기는 읽은 nonce 로만 지운다. 읽은 뒤 배포가 다시 선점한 기록은 남는다."""
    _old_claim(mongo, "gone.example", "nobody")
    store = client.app.state.mcp_server_service.exposure_hosts
    real_all = type(store).all

    async def read_then_refresh(self):
        claims = await real_all(self)
        mongo["exposure_hosts"].update_one({"_id": "gone.example"}, {"$set": {"nonce": "fresh"}})
        return claims

    monkeypatch.setattr(type(store), "all", read_then_refresh)
    svc = client.app.state.mcp_server_service
    assert client.portal.call(svc.settle_host_claims, utcnow())["released"] == []
    assert mongo["exposure_hosts"].find_one({"_id": "gone.example"})["nonce"] == "fresh"


def _mcp_exposed(client, fake_clusters, host):
    """host 로 노출한 MCP Server. (서비스, 문서 경로, 컬렉션, id)."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    r = clone_and_deploy(client, mp, name="calc-a", exposure={"enabled": True, "host": host})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    return client.app.state.mcp_server_service, f"{MS}/{doc_id}", "mcp_servers", doc_id


def test_a_host_change_whose_redeploy_never_starts_saves_nothing(
    client, fake_clusters, mongo, monkeypatch
):
    """Basic 편집의 새 노출은 재배포가 deploying 으로 옮기는 저장이 함께 쓴다. 재배포가 그 앞에서
    멈추면 문서는 옛 host 그대로이고 새 host 를 선점하지 않는다 (P70)."""
    svc, url, coll, doc_id = _mcp_exposed(client, fake_clusters, HOST)

    async def refuse(self, tenant, cluster, ns):
        raise self._error(422, "namespace_not_allowed", "gone")

    monkeypatch.setattr(type(svc), "_require_namespace_still_allowed", refuse)
    r = client.patch(url, json={"exposure": {"enabled": True, "host": "new.example"}})
    assert detail_code(r) == (422, "namespace_not_allowed")
    doc = mongo[coll].find_one({"_id": doc_id})
    assert doc["exposure"]["host"] == HOST and doc["state"] == "deployed"
    assert _claims(mongo) == {HOST: doc_id}


def test_a_host_change_whose_provision_fails_keeps_the_old_host(
    client, fake_clusters, mongo, monkeypatch
):
    """재배포가 시작한 뒤 적용이 실패하면 문서는 새 host 로 failed 다. 옛 Ingress 가 남았을 수 있어
    쓸기는 옛 host 의 선점을 놓지 않는다."""
    svc, url, coll, doc_id = _mcp_exposed(client, fake_clusters, HOST)

    async def down(self, inst, **_steps):
        raise ClusterUnreachable("down")

    monkeypatch.setattr(type(svc), "_provision", down)
    r = client.patch(url, json={"exposure": {"enabled": True, "host": "new.example"}})
    assert r.status_code >= 500, r.text
    doc = mongo[coll].find_one({"_id": doc_id})
    assert doc["exposure"]["host"] == "new.example" and doc["state"] == "failed"
    assert _claims(mongo) == {HOST: doc_id, "new.example": doc_id}
    old = utcnow() - timedelta(minutes=10)
    mongo["exposure_hosts"].update_many({}, {"$set": {"claimed_at": old}})
    assert client.portal.call(svc.settle_host_claims, utcnow())["released"] == []
    assert _claims(mongo) == {HOST: doc_id, "new.example": doc_id}


def test_a_stored_host_that_does_not_normalize_is_skipped(client, mongo, caplog):
    """정규화 규칙보다 먼저 저장된 host 는 기동과 리컨실을 멈추지 않는다. 경고하고 건너뛰며, 그
    host 의 선점은 이름에 두지 않은 것으로 본다 (P70)."""
    seed_server(mongo, "odd", "bad_host!")
    seed_server(mongo, "fine", "fine.example")
    _old_claim(mongo, "bad-host.example", "odd")
    svc = client.app.state.mcp_server_service
    with caplog.at_level(logging.WARNING, logger="daegam.exposure"):
        result = client.portal.call(svc.settle_host_claims, utcnow())
    assert result == {"released": ["bad-host.example"], "adopted": ["fine.example"]}
    assert any("does not normalize" in m for m in caplog.messages)
