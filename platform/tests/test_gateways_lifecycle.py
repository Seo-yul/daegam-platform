"""Gateway (D42) 를 배포한 뒤의 흐름을 본다.

저장·재배포·Advanced 편집·삭제, 해제와 게시·참조 가드, 해제·삭제 실패와 리컨실의 해제·삭제 갈래,
재배포 네임스페이스, 상태 행, 상태 전이의 경합을 든다.
실패한 배포를 리컨실이 다시 하는 것은 test_reconcile.py 가, 배포된 것을 지우는 데 드는 deploy
scope 는 test_rbac.py 가, 세 인스턴스가 함께 쓰는 배포 쪽 저장의 경합은 test_agents.py 가 본다.
"""

from __future__ import annotations

import yaml

from daegam.k8s.labels import LABEL_PROFILE_REVISION
from daegam.services.gateway_status import vmcp_caught_up
from daegam.services.instance_status import ROLLOUT_WATCH
from daegam.store.gateways import Gateway, GatewaySpec
from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB, TIME_MARGIN
from tests.helpers.deployments import deployment, put_deployment
from tests.helpers.docs import age
from tests.helpers.gateways import create_gateway, deploy_gateway, failure_mode, gateway_up, vmcp_of
from tests.helpers.http import deployed, detail_code
from tests.helpers.hub import hub_env
from tests.helpers.workers import reconciler, stale_tick

# -- 재배포·해제·삭제 ------------------------------------------------------------------------------


def test_delete_redeploy_and_edit(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    # 저장은 리비전만 올리고 핀은 그대로다 ("r2 저장됨, r1 배포됨")
    r = client.put(
        f"{GW}/{inst['id']}",
        json={
            "name": "a",
            "failure_mode": "fail",
            "runtime": {"replicas": 3},
            "backends": [{"hub_item_id": items["calculator"]["id"], "tools": ["Divide"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["deployed_revision"] == 1
    assert r.json()["state"] == "deployed" and r.json()["status"] is not None
    assert failure_mode(vmcp_of(fake, "gw-a")) == "best_effort"
    # redeploy: 핀이 r2 로 (failure_mode fail, Divide 만, replicas 3)
    r = client.post(f"{GW}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert r.json()["deployed_revision"] == 2 and r.json()["warnings"] == []
    cr = vmcp_of(fake, "gw-a")
    assert failure_mode(cr) == "fail" and cr["spec"]["replicas"] == 3
    assert cr["metadata"]["labels"]["daegam.devops.ai.kr/profile-revision"] == "2"
    assert cr["spec"]["config"]["aggregation"]["tools"] == [
        {"workload": "a-calculator", "filter": ["Divide"]}
    ]
    # 더 이상 안 쓰는 orders Entry 는 지워졌다
    assert sorted(e["metadata"]["name"] for e in fake.find("MCPServerEntry", DEFAULT_NS)) == [
        "a-calculator"
    ]
    # 편집: replicas 오버라이드는 허용, aggregation.tools·groupRef 는 잠김
    r = client.put(f"{GW}/{inst['id']}/overrides", json={"patch": {"spec": {"replicas": 2}}})
    assert r.status_code == 200 and r.json()["validation"] == "schema_only"
    assert vmcp_of(fake, "gw-a")["spec"]["replicas"] == 2
    r = client.put(
        f"{GW}/{inst['id']}/overrides",
        json={"patch": {"spec": {"config": {"aggregation": {"tools": []}}}}},
    )
    assert detail_code(r) == (422, "locked_field")
    r = client.put(
        f"{GW}/{inst['id']}/overrides", json={"patch": {"spec": {"groupRef": {"name": "x"}}}}
    )
    assert detail_code(r) == (422, "locked_field")
    full = client.get(f"{GW}/{inst['id']}/manifest").json()["documents"][-1]["final"]
    full["spec"]["replicas"] = 4
    r = client.put(f"{GW}/{inst['id']}/overrides", json={"yaml": yaml.safe_dump(full)})
    assert r.status_code == 200, r.text
    assert vmcp_of(fake, "gw-a")["spec"]["replicas"] == 4
    docs = client.get(f"{GW}/{inst['id']}/manifest").json()["documents"]
    assert [d["kind"] for d in docs] == [
        "MCPGroup",
        "MCPExternalAuthConfig",
        "MCPServerEntry",
        "MCPAuthzConfig",
        "VirtualMCPServer",
    ]
    m = docs[-1]
    assert m["overrides"]["spec"]["replicas"] == 4 and m["drift"] == []
    assert docs[0]["drift"] == [] and docs[0]["overrides"] == {} and docs[0]["locked"] == ["*"]
    # 드리프트 → reset / absorb
    fake.edit_live(
        GV, "VirtualMCPServer", "gw-a", DEFAULT_NS, lambda o: o["spec"].__setitem__("replicas", 5)
    )
    assert client.get(f"{GW}/{inst['id']}/manifest").json()["documents"][-1]["drift"] == [
        "spec.replicas"
    ]
    assert client.post(f"{GW}/{inst['id']}/drift/reset").json()["documents"][-1]["drift"] == []
    assert vmcp_of(fake, "gw-a")["spec"]["replicas"] == 4
    fake.edit_live(
        GV, "VirtualMCPServer", "gw-a", DEFAULT_NS, lambda o: o["spec"].__setitem__("replicas", 6)
    )
    r = client.post(f"{GW}/{inst['id']}/drift/absorb")
    assert r.status_code == 200 and r.json()["overrides"]["spec"]["replicas"] == 6
    # 삭제: 정의와 클러스터의 것을 모두 지운다
    r = client.delete(f"{GW}/{inst['id']}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert r.json()["state_before"] == "deployed" and r.json()["k8s"]["name"] == "gw-a"
    assert vmcp_of(fake, "gw-a") is None
    assert fake.find("MCPServerEntry", DEFAULT_NS) == []
    assert fake.get(GV, "MCPGroup", "gw-a", DEFAULT_NS) is None
    assert fake.get(GV, "MCPGroup", "default", DEFAULT_NS) is not None  # 테넌트 그룹은 남는다
    assert fake.get(GV, "MCPExternalAuthConfig", "svc-gw-a", DEFAULT_NS) is None
    assert fake.get("v1", "Secret", "svc-gw-a", DEFAULT_NS) is None
    assert client.get(f"{GW}/{inst['id']}").status_code == 404
    assert client.get(GW).json() == []


def test_undeploy_refuses_while_published_or_referenced(client, fake_clusters, mongo):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    pub = client.post(f"{GW}/{inst['id']}/publish", json={}).json()
    for action in ("undeploy", ""):
        r = (
            client.post(f"{GW}/{inst['id']}/undeploy")
            if action
            else client.delete(f"{GW}/{inst['id']}")
        )
        assert detail_code(r) == (409, "published")
        assert r.json()["detail"]["hub_item_ids"] == [pub["id"]]
    assert client.delete(f"{HUB}/{pub['id']}").status_code == 200
    # Agent 그래프가 도구 소스로 참조하면 해제·삭제 둘 다 409 in_use (정의 쪽 참조라 draft 도 본다)
    mongo["agents"].insert_one(
        {
            "_id": "ag-1",
            "tenant": "default",
            "name": "bot",
            "display_name": "bot",
            "spec": {
                "graph": {
                    "nodes": [
                        {
                            "id": "t1",
                            "type": "tools",
                            "data": {"source": {"kind": "gateway_instance", "id": inst["id"]}},
                        }
                    ],
                    "edges": [],
                },
                "service": {},
            },
            "revision": 1,
            "revisions": [],
        }
    )
    for r in (client.post(f"{GW}/{inst['id']}/undeploy"), client.delete(f"{GW}/{inst['id']}")):
        assert detail_code(r) == (409, "in_use")
        assert r.json()["detail"]["agent_ids"] == ["ag-1"]
    assert client.get(f"{GW}/{inst['id']}").json()["state"] == "deployed"
    mongo["agents"].delete_one({"_id": "ag-1"})
    assert client.post(f"{GW}/{inst['id']}/undeploy").status_code == 200


def test_undeploy_and_delete_failures_keep_their_state(client, fake_clusters, caplog):
    """해제·삭제 도중의 실패는 failed 가 아니라 undeploying·deleting + last_error 다. 리컨실이 그
    갈래를 다시 몬다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    a = gateway_up(client, items, name="a")
    b = gateway_up(client, items, name="b")
    fake.unreachable = True
    r = client.post(f"{GW}/{a['id']}/undeploy")
    assert r.status_code == 502, r.text
    r = client.delete(f"{GW}/{b['id']}")
    assert r.status_code == 502, r.text
    states = {d["name"]: d for d in deployed(client, GW)}
    assert states["a"]["state"] == "undeploying" and states["a"]["last_error"]
    assert states["b"]["state"] == "deleting" and states["b"]["last_error"]
    # 진행 중에는 다른 동작이 409 in_progress
    r = client.post(f"{GW}/{a['id']}/redeploy")
    assert detail_code(r) == (409, "in_progress")
    fake.unreachable = False
    app = client.app
    rec = reconciler(client, gateways=(app.state.gateways, app.state.gateway_service))
    result = stale_tick(client, rec)
    assert result["undeployed"] == [a["id"]] and result["deleted"] == [b["id"]], (
        result,
        caplog.text,
    )
    assert result["failed"] == [], (result, caplog.text)
    assert client.get(f"{GW}/{a['id']}").json()["state"] == "draft"
    assert client.get(f"{GW}/{b['id']}").status_code == 404
    assert vmcp_of(fake, "gw-a") is None and vmcp_of(fake, "gw-b") is None


def test_redeploy_keeps_gateway_namespace(client, fake_clusters):
    """재배포는 문서 자신의 k8s.namespace 를 쓴다. 테넌트 기본 네임스페이스가 바뀌어도 409 가 아니고
    CR 은 원래 네임스페이스에 다시 적용된다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    assert inst["k8s"]["namespace"] == DEFAULT_NS
    r = client.patch("/tenants/default/settings", json={"namespace": {"name": "daegam-moved"}})
    assert r.status_code == 200, r.text
    # 옛 네임스페이스가 허용 목록에서 빠졌으면 409, 넣으면 그 네임스페이스에 그대로 재배포 (F3)
    r = client.post(f"{GW}/{inst['id']}/redeploy")
    assert detail_code(r) == (409, "namespace_no_longer_allowed")
    r = client.patch("/tenants/default/settings", json={"allowed_namespaces": [DEFAULT_NS]})
    assert r.status_code == 200, r.text
    r = client.post(f"{GW}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert r.json()["k8s"]["namespace"] == DEFAULT_NS
    assert r.json()["effective_config"]["namespace"]["name"] == DEFAULT_NS
    assert vmcp_of(fake, "gw-a") is not None and fake.find("VirtualMCPServer", "daegam-moved") == []
    m = client.get(f"{GW}/{inst['id']}/manifest").json()["documents"]
    assert {d["namespace"] for d in m} == {DEFAULT_NS}


# -- 상태 ------------------------------------------------------------------------------------------


def test_status_rows_and_generation_trust(client, fake_clusters, mongo):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    fake.set_status(
        GV, "VirtualMCPServer", "gw-a", DEFAULT_NS, {"phase": "Ready", "backendCount": 2}
    )
    assert client.get(f"{GW}/{inst['id']}").json()["status"]["phase"] == "Rolling"
    fake.set_status(GV, "MCPServerEntry", "a-calculator", DEFAULT_NS, {"phase": "Valid"})
    fake.set_status(
        GV,
        "MCPServerEntry",
        "a-orders",
        DEFAULT_NS,
        {"phase": "Failed", "conditions": [{"type": "Valid", "status": "False", "message": "bad"}]},
    )
    fake.set_status(
        GV,
        "VirtualMCPServer",
        "gw-a",
        DEFAULT_NS,
        {
            "phase": "Degraded",
            "observedGeneration": 1,
            "backendCount": 2,
            "discoveredBackends": [{"name": "a-calculator", "status": "ready", "url": "u"}],
        },
    )
    age(mongo, "gateways", inst["id"])
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    rows = {r["workload"]: r for r in st["backends"]}
    assert st["phase"] == "Degraded" and st["badge"] == "일부 백엔드 불가"
    assert (
        rows["a-calculator"]["status"] == "ready" and rows["a-calculator"]["entry_phase"] == "Valid"
    )
    assert rows["a-orders"]["status"] == "invalid" and rows["a-orders"]["message"] == "bad"
    fake.set_status(GV, "MCPServerEntry", "a-orders", DEFAULT_NS, {"phase": "Valid"})
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert {r["workload"]: r["status"] for r in st["backends"]}["a-orders"] == "discovering"


def test_status_waits_for_the_vmcp_rollout_after_a_spec_change(client, fake_clusters, mongo):
    """재배포 직후 오퍼레이터는 observedGeneration 을 먼저 올리고 phase 를 옛 파드 기준 Ready 로
    둔다. vMCP Deployment 가 그 리비전으로 다 굴러가기 전에는 Rolling 이다 (e2e-mcp-auth 를 다시
    돌린 STEP 4 가 옛 파드에 붙었다). 지켜보는 시간이 지나면 CR phase 를 그대로 믿는다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    gen = vmcp_of(fake, "gw-a")["metadata"]["generation"]
    fake.set_status(
        GV,
        "VirtualMCPServer",
        "gw-a",
        DEFAULT_NS,
        {
            "phase": "Ready",
            "observedGeneration": gen,
            "backendCount": len(inst["deployed_backends"]),
        },
    )
    rev = inst["deployed_revision"]

    def put_vmcp(revision: int, *, rolled: bool) -> None:
        labels = {LABEL_PROFILE_REVISION: str(revision)}
        put_deployment(fake, deployment("gw-a", {}, labels=labels, rolled=rolled))

    def status() -> dict:
        return client.get(f"{GW}/{inst['id']}").json()["status"]

    # 오퍼레이터가 아직 Deployment 템플릿에 이 리비전을 싣지 않았다
    put_vmcp(rev - 1, rolled=True)
    st = status()
    assert st["phase"] == "Rolling" and "not yet rolled out" in st["message"]
    # Deployment 를 읽지 못하면(권한이 모자란 클러스터) CR phase 를 그대로 믿는다
    denied = ("apps", "deployments", "get")
    fake.denied.add(denied)
    assert status()["phase"] == "Ready"
    fake.denied.discard(denied)
    # 새 템플릿을 실었지만 옛 파드가 남았다
    put_vmcp(rev, rolled=False)
    assert status()["phase"] == "Rolling"
    # 다 굴러갔다
    put_vmcp(rev, rolled=True)
    assert status()["phase"] == "Ready"
    # 롤아웃을 지켜보는 시간(ROLLOUT_WATCH)이 지나면 Deployment 를 보지 않는다
    put_vmcp(rev, rolled=False)
    age(mongo, "gateways", inst["id"], by=ROLLOUT_WATCH + TIME_MARGIN)
    assert status()["phase"] == "Ready"


def test_vmcp_caught_up_without_the_revision_label():
    """라벨이 없는 판과 리비전이 그대로인 렌더(인증 재적용)는 롤아웃만 본다."""
    dep = deployment("gw-a", {})
    assert vmcp_caught_up(dep, 5)
    dep["status"]["replicas"] = 2
    assert not vmcp_caught_up(dep, 5)


# -- 경합: 상태 전이는 조건부 갱신이고 저장은 자기 쪽 필드만 쓴다 ----------------------------------


def _root():
    from daegam.auth.principal import header_principal

    return header_principal()


def test_state_transitions_are_conditional(client, fake_clusters, mongo):
    """같은 문서에 요청이 겹치면 하나만 지나간다 (스토어의 조건부 갱신)."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    p = create_gateway(client, items, name="a").json()
    svc = client.app.state.gateway_service
    store = client.app.state.gateways

    async def race_deploy():
        doc = await store.get(p["id"])
        other = await store.get(p["id"])
        first, _ = await svc.deploy(doc, cluster_id=None, principal=_root())
        try:
            await svc.deploy(other, cluster_id=None, principal=_root())
        except Exception as e:  # noqa: BLE001
            return first, e
        return first, None

    first, err = client.portal.call(race_deploy)
    assert first.state == "deploying" and err is not None
    assert getattr(err, "code", None) in ("state_conflict", "already_deployed")

    # 그 사이 draft 삭제도 지나가지 않는다
    async def stale_draft_delete():
        stale = Gateway.model_validate({**first.model_dump(), "state": "draft"})
        try:
            await svc.delete(stale)
        except Exception as e:  # noqa: BLE001
            return e
        return None

    err = client.portal.call(stale_draft_delete)
    assert getattr(err, "code", None) == "state_conflict"
    client.portal.call(svc.provision, first)
    assert client.get(f"{GW}/{p['id']}").json()["state"] == "deployed"

    # deployed 에 undeploy 와 redeploy 가 겹치면 둘째가 409
    async def race_undeploy_redeploy():
        a = await store.get(p["id"])
        b = await store.get(p["id"])
        a.state = "undeploying"
        assert await store.transition(a, expect={"deployed", "failed"})
        try:
            await svc.redeploy(b, principal=_root())
        except Exception as e:  # noqa: BLE001
            return e
        return None

    err = client.portal.call(race_undeploy_redeploy)
    assert getattr(err, "code", None) == "state_conflict"


def test_definition_and_deployment_saves_do_not_overwrite_each_other(client, fake_clusters, mongo):
    """PUT 은 정의 쪽만, 배포는 배포 쪽만 쓴다. 배포 전에 읽은 문서로 저장한 r2 가 배포의 저장에
    지워지지 않고, 그 저장이 deploying 을 draft 로 되돌리지도 않는다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    p = create_gateway(client, items, name="a").json()
    svc = client.app.state.gateway_service
    store = client.app.state.gateways
    new_spec = GatewaySpec(
        backends=[{"hub_item_id": items["calculator"]["id"]}], failure_mode="fail"
    )

    async def race():
        stale = await store.get(p["id"])  # PUT 이 읽은 문서 (draft r1)
        doc, _ = await svc.deploy(await store.get(p["id"]), cluster_id=None, principal=_root())
        saved, _ = await svc.save(
            stale, display_name="a", spec=new_spec, principal=_root(), user_sub="u2"
        )
        after = await store.get(p["id"])
        await svc.provision(doc)
        return saved, after, await store.get(p["id"])

    saved, after, final = client.portal.call(race)
    assert saved.revision == 2 and after.revision == 2
    assert after.state == "deploying" and after.deployed_revision == 1
    assert after.spec.failure_mode == "fail" and after.updated_by == "u2"
    assert final.state == "deployed" and final.revision == 2 and final.deployed_revision == 1
    assert final.spec.failure_mode == "fail"  # 배포 끝의 저장이 정의 쪽을 덮지 않았다

    # 같은 옛 리비전으로 또 저장하면 409 revision_conflict
    async def again():
        stale = Gateway.model_validate({**final.model_dump(), "revision": 1})
        try:
            await svc.save(stale, display_name="a", spec=new_spec, principal=_root(), user_sub="u3")
        except Exception as e:  # noqa: BLE001
            return e
        return None

    err = client.portal.call(again)
    assert getattr(err, "code", None) == "revision_conflict"
    assert client.get(f"{GW}/{p['id']}").json()["revision"] == 2


def test_deployment_side_saves_lose_to_an_undeploy_in_between(client, fake_clusters, mongo):
    """스윕의 토큰 회전·PATCH 노출·overrides 저장이 해제와 겹치면 해제가 이긴다. 클러스터를 만지기
    전에 문서를 다시 읽으므로 해제된 vMCP 를 되살리지 않고, DB 저장도 상태 조건에 걸린다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    svc = client.app.state.gateway_service
    store = client.app.state.gateways

    async def race():
        stale = await store.get(inst["id"])
        await svc.undeploy(await store.get(inst["id"]))
        errors: dict[str, str] = {}
        attempts = (
            ("rotate", lambda: svc.rotate_svc_token(stale)),
            ("exposure", lambda: svc.set_exposure(stale, None, principal=_root())),
            ("overrides", lambda: svc.set_overrides(stale, patch={"spec": {"replicas": 2}})),
            ("drift", lambda: svc.drift_reset(stale)),
            (
                "save",
                lambda: svc._save_deployment(
                    stale, {"svc_token_rev"}, expect_state={"deployed"}, action="x"
                ),
            ),
        )
        for name, call in attempts:
            try:
                await call()
            except Exception as e:  # noqa: BLE001
                errors[name] = getattr(e, "code", type(e).__name__)
        return errors, await store.get(inst["id"])

    errors, final = client.portal.call(race)
    assert errors == {
        "rotate": "not_deployed",
        "exposure": "not_deployed",
        "overrides": "not_deployed",
        "drift": "not_deployed",
        "save": "state_conflict",
    }
    assert final.state == "draft" and final.cluster_id is None and final.svc_token_rev == "0"
    assert (
        vmcp_of(fake, "gw-a") is None and fake.get("v1", "Secret", "svc-gw-a", DEFAULT_NS) is None
    )


def test_end_transitions_are_conditional(client, fake_clusters, mongo):
    """배포 절차 도중 삭제가 들어오면 provision 의 끝 전이가 409 state_conflict 다. 해제가 시작된
    뒤 옛 문서로 온 삭제도 같다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    p = create_gateway(client, items, name="a").json()
    svc = client.app.state.gateway_service
    store = client.app.state.gateways

    async def deploy_then_delete():
        doc, _ = await svc.deploy(await store.get(p["id"]), cluster_id=None, principal=_root())
        await svc.delete(await store.get(p["id"]))  # deploying 에서 삭제는 된다
        try:
            await svc.provision(doc)
        except Exception as e:  # noqa: BLE001
            return e, await store.get(p["id"])
        return None, await store.get(p["id"])

    err, final = client.portal.call(deploy_then_delete)
    assert getattr(err, "code", None) == "state_conflict" and final is None

    inst = gateway_up(client, items, name="b")

    async def undeploy_then_stale_delete():
        stale = await store.get(inst["id"])
        live = await store.get(inst["id"])
        live.state = "undeploying"
        assert await store.transition(live, expect={"deployed"})
        try:
            await svc.delete(stale)
        except Exception as e:  # noqa: BLE001
            return e
        return None

    err = client.portal.call(undeploy_then_stale_delete)
    assert getattr(err, "code", None) == "state_conflict"
    assert client.get(f"{GW}/{inst['id']}").json()["state"] == "undeploying"


def test_status_names_a_missing_vmcp_and_a_missing_cluster(client, fake_clusters, mongo):
    """vMCP CR 이 없으면 phase Missing 과 그 이름이고, 클러스터 문서가 없으면 unknown_cluster
    줄이다. 세 종류가 같은 틀이다 (P71 2.4절)."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    name = inst["k8s"]["name"]
    fake.delete(GV, "VirtualMCPServer", name, DEFAULT_NS)
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["phase"] == "Missing"
    assert st["message"] == f"VirtualMCPServer {DEFAULT_NS}/{name} not found"
    mongo["clusters"].delete_one({"_id": cid})
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["error"] == f"unknown_cluster: cluster {cid} missing"


def test_a_token_name_collision_in_provision_is_422(client, fake_clusters, monkeypatch):
    """provision 의 토큰 객체 이름 충돌은 스펙 탓이라 422 svc_token_name_collision 이다. 실패 기록이
    종류의 오류만이 아니라 모든 서비스 오류를 코드 그대로 지나보낸다 (P71 2.2절). 전에는 502
    k8s_error 였다."""
    from daegam.k8s.templates.svc_token import TokenNameCollision

    def collide(k8s_name, backends):
        raise TokenNameCollision("a-calculator", "a-orders", f"{k8s_name}-svc")

    # 토큰 발급(`GatewayTokens.issue`)과 렌더·정리가 따로 부른다
    monkeypatch.setattr("daegam.services.gateways.entry_token_objects", collide)
    monkeypatch.setattr("daegam.services.gateway_tokens.entry_token_objects", collide)
    _cid, items, *_rest = hub_env(client, fake_clusters)
    r = create_gateway(client, items, name="a")
    d = deploy_gateway(client, r.json()["id"])
    assert detail_code(d) == (422, "svc_token_name_collision"), d.text
    doc = client.get(f"{GW}/{r.json()['id']}").json()
    assert doc["state"] == "failed"
    assert doc["last_error"].startswith("svc_token_name_collision: ")
