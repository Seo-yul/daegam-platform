"""MCP Server (D42) 를 배포한 뒤의 흐름을 본다.

CR 상태 매핑, 해제·삭제와 그 실패, 재배포의 핀·토큰, 상태 전이와 저장의 경합, Agent 가 도구
소스로 쓰는 동안의 참조 가드를 든다.

준비와 배포는 tests/helpers/mcp_servers.py 의 `mcp_server_env`·`clone_and_deploy` 다.
"""

from __future__ import annotations

from functools import partial

import pytest

from daegam.services.instance_status import ROLLOUT_WATCH
from daegam.services.mcp_servers import (
    MCPSERVER_GENERATION_ANNOTATION,
    MCPServerError,
    proxy_caught_up,
)
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.consts import BP, DEFAULT_NS, GV, MS, TIME_MARGIN
from tests.helpers.deployments import deployment, put_deployment
from tests.helpers.docs import age
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import deployed, detail_code
from tests.helpers.mcp_servers import clone_and_deploy, create_server, mcp_server_env

# -- 상태 -----------------------------------------------------------------


def test_status_mapping_from_cr(client, fake_clusters, mongo):
    """CR 의 status 를 화면의 phase 로 옮긴다. 이미지를 못 받는 파드는 CR phase 가 Pending 이어도
    Failed 다 (D43 둘째). 화면이 10분 동안 "곧 됩니다" 를 보이던 자리다. `since` 로 흐른 시간도
    같이 준다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    assert inst["status"]["phase"] == "Pending"  # fake CR 에 status 가 없다
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    key = (GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)

    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "mcp-calc-1-abc",
                "namespace": DEFAULT_NS,
                "labels": {"daegam.devops.ai.kr/id": inst["id"]},
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "mcp",
                        "state": {
                            "waiting": {
                                "reason": "ImagePullBackOff",
                                "message": 'Back-off pulling image "ghcr.io/nope:0"',
                            }
                        },
                    }
                ]
            },
        }
    )
    got = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert got["phase"] == "Failed"
    assert got["message"].startswith("ImagePullBackOff: ")
    assert got["since"] is not None

    def set_status(**st):
        fake.set_status(*key[:2], key[2], key[3], st)

    url = "http://mcp-mcp-calc-1-proxy.daegam-default.svc.cluster.local:8080/mcp"
    ready_since = "2026-09-16T01:02:03Z"
    set_status(
        phase="Ready",
        url=url,
        observedGeneration=1,
        readyReplicas=1,
        conditions=[
            {"type": "Ready", "status": "True", "reason": "Ok", "lastTransitionTime": ready_since}
        ],
    )
    got = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert got["phase"] == "Ready" and got["url"] == url and got["ready_replicas"] == 1
    assert got["conditions"][0]["type"] == "Ready" and got["error"] is None
    # 화면의 "떠 있은 지" 는 Ready 조건의 전이 시각에서 나온다 (D44 셋째)
    assert got["conditions"][0]["last_transition"] == ready_since
    assert deployed(client, MS)[0]["status"]["phase"] == "Ready"
    # Ready 가 나면 옛 파드가 그대로 있어도 뒤집지 않는다. 그 파드는 여기서 지운다
    fake.delete("v1", "Pod", "mcp-calc-1-abc", DEFAULT_NS)

    # generation > observedGeneration → Rolling
    fake.apply(
        {
            **fake.get(*key[:2], key[2], key[3]),
            "spec": {**fake.get(*key[:2], key[2], key[3])["spec"], "replicas": 1},
        }
    )
    assert fake.get(*key[:2], key[2], key[3])["metadata"]["generation"] == 2
    set_status(phase="Ready", url=url, observedGeneration=1)
    assert client.get(f"{MS}/{inst['id']}").json()["status"]["phase"] == "Rolling"

    # Failed 인데 last_spec_change 60초 이내 → Rolling. 60초 지나면 Failed + 메시지
    set_status(
        phase="Failed",
        observedGeneration=2,
        conditions=[
            {"type": "Ready", "status": "False", "reason": "Crash", "message": "init failed"}
        ],
    )
    assert client.get(f"{MS}/{inst['id']}").json()["status"]["phase"] == "Rolling"
    age(mongo, "mcp_servers", inst["id"])
    got = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert got["phase"] == "Failed" and got["message"] == "init failed"
    assert got["conditions"][0]["last_transition"] is None  # 전이 시각이 없는 조건

    # CR 이 없어졌으면 Missing, 클러스터 접속 불가면 error
    fake.delete(*key[:2], key[2], key[3])
    assert client.get(f"{MS}/{inst['id']}").json()["status"]["phase"] == "Missing"
    fake.unreachable = True
    got = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert got["phase"] is None and got["error"].startswith("cluster_unreachable")
    # 클러스터 문서가 없으면 error 는 unknown_cluster 줄이다. 화면이 코드표의 문장으로 옮긴다
    # (P71 2.4절)
    fake.unreachable = False
    mongo["clusters"].delete_one({"_id": cid})
    st = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert st["error"] == f"unknown_cluster: cluster {cid} missing"


def _proxy_deployment(generation: int, *, rolled: bool) -> dict:
    """ToolHive 가 MCPServer 이름으로 만드는 프록시 Deployment. 템플릿이 반영한 CR 세대를 싣는다."""
    annotations = {MCPSERVER_GENERATION_ANNOTATION: str(generation)}
    return deployment("mcp-calc-1", annotations, rolled=rolled)


def test_status_waits_for_the_proxy_rollout_after_a_spec_change(client, fake_clusters, mongo):
    """재배포 직후 오퍼레이터는 observedGeneration 을 먼저 올리고 phase 를 옛 파드 기준 Ready 로
    둔다. 프록시 Deployment 가 그 세대로 다 굴러가기 전에는 Rolling 이다 (e2e-gateway STEP 5 의
    backends_not_ready). 유예가 지나면 CR phase 를 그대로 믿는다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    key = (GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)
    gen = fake.get(*key)["metadata"]["generation"]
    fake.set_status(*key, {"phase": "Ready", "observedGeneration": gen, "message": "running"})

    def phase() -> str:
        return client.get(f"{MS}/{inst['id']}").json()["status"]["phase"]

    put_proxy = partial(put_deployment, fake)
    # 오퍼레이터가 아직 프록시 템플릿에 이 세대를 싣지 않았다
    put_proxy(_proxy_deployment(gen - 1, rolled=True))
    got = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert got["phase"] == "Rolling" and "not yet rolled out" in got["message"]
    # 새 템플릿을 실었지만 옛 파드가 남았다
    put_proxy(_proxy_deployment(gen, rolled=False))
    assert phase() == "Rolling"
    # 다 굴러갔다
    put_proxy(_proxy_deployment(gen, rolled=True))
    assert phase() == "Ready"
    # 롤아웃을 지켜보는 시간(ROLLOUT_WATCH)이 지나면 Deployment 를 보지 않는다
    put_proxy(_proxy_deployment(gen, rolled=False))
    age(mongo, "mcp_servers", inst["id"], by=ROLLOUT_WATCH + TIME_MARGIN)
    assert phase() == "Ready"


def test_proxy_caught_up_without_the_generation_annotation():
    """어노테이션이 없는 오퍼레이터 판은 롤아웃만 본다."""
    dep = _proxy_deployment(1, rolled=True)
    del dep["spec"]["template"]["metadata"]["annotations"]
    assert proxy_caught_up(dep, 5)
    dep["status"]["replicas"] = 2
    assert not proxy_caught_up(dep, 5)


# -- 해제·삭제 ---------------------------------------------------------------


def test_delete_failure_stays_deleting(client, fake_clusters):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    fake_clusters.by_name["edge"].unreachable = True
    r = client.delete(f"{MS}/{inst['id']}")
    assert detail_code(r) == (502, "cluster_unreachable")
    got = client.get(f"{MS}/{inst['id']}").json()
    # failed 가 아니라 deleting 이다. failed 면 리컨실이 지우던 CR 을 다시 배포한다
    assert got["state"] == "deleting"
    assert got["last_error"].startswith("cluster_unreachable")
    # 토큰은 이미 회수됐다 (삭제 순서의 1단계)
    assert client.get(f"{BP}/{bp['id']}/tokens").json()[0]["active"] is False
    # 지우는 중에는 다른 동작이 409 deleting 이다 (P74 2.3절)
    r = client.post(f"{MS}/{inst['id']}/undeploy")
    assert detail_code(r) == (409, "deleting")
    # 클러스터가 돌아오면 다시 DELETE 로 마무리
    fake_clusters.by_name["edge"].unreachable = False
    assert client.delete(f"{MS}/{inst['id']}").status_code == 200
    assert deployed(client, MS) == []


def test_undeploy_failure_stays_undeploying(client, fake_clusters):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    fake_clusters.by_name["edge"].unreachable = True
    r = client.post(f"{MS}/{inst['id']}/undeploy")
    assert detail_code(r) == (502, "cluster_unreachable")
    got = client.get(f"{MS}/{inst['id']}").json()
    assert got["state"] == "undeploying" and got["last_error"].startswith("cluster_unreachable")
    assert got["k8s"]["name"] == "mcp-calc-1"  # 배포 쪽은 아직 있다
    for r in (
        client.post(f"{MS}/{inst['id']}/redeploy"),
        client.delete(f"{MS}/{inst['id']}"),
        client.post(f"{MS}/{inst['id']}/deploy", json={}),
        client.put(
            f"{MS}/{inst['id']}",
            json={"name": "calc-1", "backend_profile_id": bp["id"], "spec_version": 1},
        ),
    ):
        assert detail_code(r) == (409, "in_progress"), r.text
    fake_clusters.by_name["edge"].unreachable = False
    r = client.post(f"{MS}/{inst['id']}/undeploy")
    assert r.status_code == 200 and r.json()["state"] == "draft"


# -- 재배포 ---------------------------------------------------------------


def test_redeploy_moves_pin_and_recomputes_effective(client, fake_clusters):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    assert inst["deployed_revision"] == 1
    # 리비전 2 (툴 추가, replicas 유지) + 클러스터 이미지 변경
    client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    client.patch(f"/clusters/{cid}", json={"images": {"runtime_image": "mcp-runtime:2"}})
    before = client.get(f"{MS}/{inst['id']}").json()
    assert (before["revision"], before["deployed_revision"]) == (2, 1)
    assert before["effective_config"]["images"]["runtime_image"] == "daegam-mcp-runtime:0.1.0"
    applied_before = len(fake.applied)

    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["deployed_revision"] == 2 and after["state"] == "deployed"
    assert after["effective_config"]["images"]["runtime_image"] == "mcp-runtime:2"
    assert after["spec_token_id"] == inst["spec_token_id"]  # 토큰 유지
    assert after["last_spec_change"] > before["last_spec_change"]
    cr = fake.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)
    assert cr["spec"]["image"] == "mcp-runtime:2"
    assert (
        f"/mcp-servers/{inst['id']}/revisions/2/profile.json"
        in cr["spec"]["podTemplateSpec"]["spec"]["initContainers"][0]["args"][0]
    )
    assert cr["metadata"]["labels"]["daegam.devops.ai.kr/profile-revision"] == "2"
    # 토큰 Secret 은 다시 만들지 않았다 (Secret 이 있고 토큰이 살아 있음)
    assert [o["kind"] for o in fake.applied[applied_before:]] == [
        "Namespace",
        "NetworkPolicy",  # 샌드박스 격리 정책 daegam-sandbox-isolation (P40 4절)
        "NetworkPolicy",  # MCP 백엔드 정책 daegam-mcp-backend (T67)
        "NetworkPolicy",
        # 네임스페이스 객체 (P50 1절). daegam-tenant-objects 와 원격 프록시의 ServiceAccount 다
        "RoleBinding",
        "ServiceAccount",
        "MCPOIDCConfig",  # 서비스 토큰용 daegam-internal-oidc, 모든 모드 (D41)
        "MCPGroup",
        "RoleBinding",
        "MCPAuthzConfig",  # 테넌트 울타리가 서버보다 먼저다 (P50 11절)
        "MCPServer",
    ]
    assert len(client.get(f"{BP}/{bp['id']}/tokens").json()) == 1

    # 테넌트 네임스페이스가 바뀌어 옛 네임스페이스가 허용 목록 밖이면 redeploy 는 409 (F3)
    client.patch("/tenants/default/settings", json={"namespace": {"name": "moved-ns"}})
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert detail_code(r) == (409, "namespace_no_longer_allowed")


def test_redeploy_validation(client, fake_clusters):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    # 최신 리비전이 replicas 2 인데 Redis 가 없으면 422 redis_required, 문서는 그대로
    client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "runtime": {"replicas": 2},
        },
    )
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert detail_code(r) == (422, "redis_required")
    got = client.get(f"{MS}/{inst['id']}").json()
    assert got["deployed_revision"] == 1 and got["state"] == "deployed"
    assert client.post(f"{MS}/nope/redeploy").status_code == 404


def test_redeploy_after_backend_swap_revokes_the_old_tokens(client, fake_clusters):
    """리비전이 바뀌어 backends 목록이 달라져도 옛 스펙 토큰이 회수된다.

    토큰 회수를 (새 backend, 옛 토큰 id) 로 짝지어 부르면 두 조건이 안 맞아 조용히 실패하고,
    그 직후 spec_token_ids 가 덮여 옛 id 가 문서에서 사라진다. 그러면 그 토큰으로 계속
    GET /backend-apis/{id}/spec 을 통과할 수 있다.
    """
    _cid, bp, _ds, ms = mcp_server_env(client, ds=False, backend_auth="basic")
    r = clone_and_deploy(client, ms, name="swap-1")
    assert r.status_code == 201, r.text
    inst = r.json()
    old_ids = inst["spec_token_ids"]
    assert len(old_ids) == 1

    # backend 를 다른 것으로 갈아 새 리비전을 만든다
    bp2 = uploaded_backend(client, "pets", "legacy-swagger.json", endpoint="https://api.pets/v1")
    r = client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "swap-1",
            "backends": [{"backend_profile_id": bp2["id"], "spec_version": 1, "tools": ["getPet"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert client.post(f"{MS}/{inst['id']}/redeploy").status_code == 200

    after = client.get(f"{MS}/{inst['id']}").json()
    assert after["spec_token_ids"] != old_ids, "새 backend 로 토큰을 다시 발급해야 한다"
    # 옛 토큰은 옛 backend profile 아래에 남아 있고, 살아 있으면 안 된다
    tokens = client.get(f"{BP}/{bp['id']}/tokens").json()
    stale = [t for t in tokens if t["token_id"] in old_ids]
    assert stale, "옛 토큰 문서가 사라졌다. 테스트 전제가 틀렸다"
    assert not any(t["active"] for t in stale), f"회수되지 않은 스펙 토큰이 남았다: {stale}"


# -- 경합: 상태 전이는 조건부 갱신이다 ---------------------------------------------------------


def _service(client):
    return client.app.state.mcp_server_service


def _snapshot(client, server_id):
    """다른 요청이 같은 문서를 읽은 시점의 사본. 서비스 메서드를 직접 부르면 검사는 이 사본으로
    지나가고 저장이 DB 의 상태를 본다."""
    return client.portal.call(client.app.state.mcp_servers.get, server_id)


def test_delete_and_deploy_of_the_same_draft_leave_no_cluster_objects(client, fake_clusters):
    """삭제가 먼저 지나가면 stale draft 의 배포는 문서 없는 CR·Secret·토큰을 만들지 않고 409 다.
    반대로 배포가 먼저면 stale draft 의 삭제가 막힌다."""
    cid, bp, ds, ms = mcp_server_env(client)
    svc = _service(client)
    stale = _snapshot(client, ms["id"])
    assert client.delete(f"{MS}/{ms['id']}").status_code == 200
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(partial(svc.deploy, stale, cluster_id=None))
    assert ei.value.code == "state_conflict"
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    assert fake.get(GV, "MCPServer", "mcp-calc-mcp", DEFAULT_NS) is None
    assert client.get(f"{BP}/{bp['id']}/tokens").json() == []
    assert client.get(f"{MS}/{ms['id']}").status_code == 404

    draft = create_server(client, bp, name="calc-b").json()
    stale = _snapshot(client, draft["id"])
    assert clone_and_deploy(client, draft, name="calc-b").status_code == 201
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(svc.delete, stale)
    assert ei.value.code == "state_conflict"
    assert client.get(f"{MS}/{draft['id']}").json()["state"] == "deployed"
    assert fake.get(GV, "MCPServer", "mcp-calc-b", DEFAULT_NS) is not None


# -- 경합: 저장은 자기 쪽 필드만 쓴다 ---------------------------------------------------------


def _store(client):
    return client.app.state.mcp_servers


def test_put_and_deploy_of_the_same_document_keep_both_sides(client, fake_clusters):
    """PUT 은 정의 쪽만, 배포는 배포 쪽만 쓴다. 어느 순서로 겹쳐도 r2 와 deploying 이 함께 남고,
    이어지는 provision 은 revisions 를 지우지 않는다."""
    cid, bp, ds, ms = mcp_server_env(client)
    svc = _service(client)
    deployer, editor = _snapshot(client, ms["id"]), _snapshot(client, ms["id"])
    new_spec = editor.spec.model_copy(
        update={"runtime": editor.spec.runtime.model_copy(update={"replicas": 2})}
    )
    # 배포가 먼저 지나간다 (deploying). 낡은 draft 사본으로 PUT: 진행 중 검사는 사본이라 지나가고
    # 정의 저장은 revision 조건(1)에 맞아 r2 가 들어간다. 배포 쪽은 손대지 않는다
    doc, _w = client.portal.call(partial(svc.deploy, deployer, cluster_id=None))
    client.portal.call(
        partial(svc.save, editor, display_name="calc-mcp", spec=new_spec, user_sub="u")
    )
    body = client.get(f"{MS}/{ms['id']}").json()
    assert body["state"] == "deploying" and body["revision"] == 2 and body["revisions"] == [1, 2]
    # provision 의 끝 전이는 배포 쪽만 쓰므로 r2 가 남는다
    client.portal.call(svc.provision, doc)
    body = client.get(f"{MS}/{ms['id']}").json()
    assert body["state"] == "deployed" and body["deployed_revision"] == 1
    assert (
        body["revision"] == 2 and body["revisions"] == [1, 2] and body["runtime"]["replicas"] == 2
    )

    # 거꾸로: PUT 이 먼저(r2), 낡은 draft 사본(r1)으로 배포. 전이는 정의를 덮지 않는다
    draft = create_server(client, bp, name="calc-b").json()
    stale, stale_put = _snapshot(client, draft["id"]), _snapshot(client, draft["id"])
    r = client.put(
        f"{MS}/{draft['id']}",
        json={
            "name": "calc-b",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
        },
    )
    assert r.status_code == 200 and r.json()["revision"] == 2
    client.portal.call(partial(svc.deploy, stale, cluster_id=None))
    body = client.get(f"{MS}/{draft['id']}").json()
    assert body["state"] == "deploying" and body["revision"] == 2 and body["revisions"] == [1, 2]
    # r1 을 본 낡은 PUT 은 revision 조건(1)에서 0건이라 409 revision_conflict 다. r2 는 그대로다
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(
            partial(svc.save, stale_put, display_name="calc-b", spec=stale_put.spec, user_sub="u")
        )
    assert ei.value.code == "revision_conflict"
    body = client.get(f"{MS}/{draft['id']}").json()
    assert body["state"] == "deploying" and body["revision"] == 2


class _StubSvcTokens:
    async def mint(self, **_kw) -> str:
        return "not-a-jwt"


def test_stale_deployment_writes_do_not_resurrect_an_undeployed_document(
    client, fake_clusters, monkeypatch
):
    """해제가 끝난 뒤 낡은 deployed 사본으로 들어오는 재배포는 409 state_conflict 다. 스윕의 토큰
    회전·PATCH 노출·overrides 저장은 클러스터를 만지기 전에 문서를 다시 읽어 409 not_deployed 이고,
    문서를 deployed 로 되살리지 못한다. 사본을 어떻게 고쳐 와도 쓰이는 것은 저장된 문서다."""
    from daegam.services.exposure import ExposureInput

    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    svc = _service(client)
    stale = _snapshot(client, inst["id"])
    assert client.post(f"{MS}/{inst['id']}/undeploy").status_code == 200
    # 0. 해제가 먼저 지나간 stale deployed 의 재배포는 409 다. draft 인데 CR 이 살아 있는 상태가
    # 생기지 않는다
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(svc.redeploy, stale)
    assert ei.value.code == "state_conflict"
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    assert fake.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS) is None
    # 1. 스윕의 토큰 회전. 사본의 노출을 켜도 다시 읽은 문서가 deployed 가 아니라 409 다. 모든 MCP
    # Server 가 재렌더 대상이므로(P50 1절) 노출 여부와 상관없이 상태가 먼저 막는다
    stale.exposure.enabled = True
    monkeypatch.setattr(svc, "svc_tokens", _StubSvcTokens())
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(svc.rotate_svc_token, stale)
    assert ei.value.code == "not_deployed"
    assert client.get(f"{MS}/{inst['id']}").json()["state"] == "draft"
    # 2. PATCH 노출
    stale.exposure.enabled = False
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(
            partial(svc.basic_patch, stale, exposure=ExposureInput(), sent={"exposure"})
        )
    assert ei.value.code == "not_deployed"
    # 3. overrides 저장 (AdvancedEditor 의 save 콜백)
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(partial(svc.set_overrides, stale, patch={}))
    assert ei.value.code == "not_deployed"
    body = client.get(f"{MS}/{inst['id']}").json()
    assert body["state"] == "draft" and body["cluster_id"] is None and body["k8s"] is None


def test_document_removed_during_undeploy_is_not_resurrected(client, fake_clusters, monkeypatch):
    """해제 도중 문서가 사라지면 끝 전이가 0건이라 409 이고, 조건부 저장은 upsert 가 아니라 문서를
    되살리지 않는다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    svc = _service(client)
    real = svc._delete_steps

    async def steps_then_vanish(d):
        await real(d)
        await _store(client).delete(d.id)

    monkeypatch.setattr(svc, "_delete_steps", steps_then_vanish)
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(svc.undeploy, _snapshot(client, inst["id"]))
    assert ei.value.code == "state_conflict"
    assert client.get(f"{MS}/{inst['id']}").status_code == 404
