"""내부 MCPServer 의 테넌트 울타리와 한 Secret 의 여러 키 (P50 11절).

ToolHive 0.52 부터 클러스터의 응답 필터가 vMCP 뒤에서 Cedar 를 건 서버를 쓸 수 있게 하고,
`spec.secrets` 가 한 Secret 의 여러 키를 받는다. 그 클러스터의 MCP Server, 허브 image 항목,
image 복제본은 그 테넌트의 서비스 토큰과 위임 토큰만 통하는 울타리를 `<CR 이름>-authz` 에 싣고
참조한다. G8 권한이 없으면 인라인이다. 0.52 앞의 클러스터는 울타리 없이 오늘의 꼴이고, 자격증명의
사용자 이름은 mcp 컨테이너 env 로 간다. 상태 API 는 그런 문서를 기능마다 센다. 클러스터의 판정이
바뀌면 렌더 번호가 달라져 재적용이 다시 렌더한다.
"""

from __future__ import annotations

from daegam.auth.cedar import render_authz_inline, tenant_fence_policies
from daegam.k8s.templates.authz import AUTHZ_CONFIG_KIND
from tests.helpers.clusters import downgrade_toolhive, revoke_tenant_rights
from tests.helpers.consts import DEFAULT_NS, GV, MS
from tests.helpers.fake_cluster import FakeClusterClient, operator_image
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import fallbacks, run_data_plane_auth

FENCE = "mcp-calc-1-authz"


def _old_cluster(fake_clusters) -> FakeClusterClient:
    """오퍼레이터가 0.51.2 인 가짜 클러스터를 등록 전에 둔다."""
    fake = FakeClusterClient(operator_image=operator_image("0.51.2"))
    fake_clusters.by_name["edge"] = fake
    return fake


def _server(client, fake_clusters) -> tuple[str, dict, FakeClusterClient]:
    """basic 자격증명 backend 하나와 redis 바인딩의 MCP Server 를 배포한다."""
    cid, _bp, _ds, ms = mcp_server_env(client, backend_auth="basic")
    r = clone_and_deploy(client, ms)
    assert r.status_code == 201, r.text
    return cid, r.json(), fake_clusters.by_name["edge"]


def _spec(fake: FakeClusterClient, name: str = "mcp-calc-1") -> dict:
    return fake.get(GV, "MCPServer", name, DEFAULT_NS)["spec"]


def _fence(fake: FakeClusterClient) -> dict | None:
    return fake.get(GV, AUTHZ_CONFIG_KIND, FENCE, DEFAULT_NS)


def test_the_fence_lets_only_the_tenants_service_and_delegation_tokens_through():
    """바탕 permit 과 forbid … unless 다. 테넌트 이름은 Cedar 글자로 인용한다."""
    permit, fence = tenant_fence_policies('te"am')
    assert permit == "permit(principal, action, resource);"
    assert fence.startswith("forbid(principal, action, resource) unless {")
    assert 'principal.claim_tenant == "te\\"am"' in fence
    assert 'principal.claim_sub like "svc:*"' in fence and "principal has claim_act" in fence


def test_a_cluster_moving_to_0_53_fences_its_servers(client, fake_clusters, mongo):
    """0.51.2 에서 배포해 오늘의 꼴로 렌더한 서버는 클러스터가 0.53 으로 오르면 렌더 번호가 달라져
    다음 재적용이 울타리를 걸고 여러 키를 spec.secrets 로 옮긴다. 상태 API 의 대체 형태도
    사라진다. Advanced 저장과 드리프트 되돌리기도 참조할 울타리 설정을 서버보다 먼저 적용한다."""
    _old_cluster(fake_clusters)
    cid, inst, fake = _server(client, fake_clusters)
    spec = _spec(fake)
    assert "authzConfigRef" not in spec and "authzConfig" not in spec
    assert fallbacks(client, cid) == {"tenant_fence": 1, "multi_key_secrets": 1}
    assert run_data_plane_auth(client)["reapplied"] == []

    downgrade_toolhive(mongo, cid, "0.53.0")
    start = len(fake.applied)
    assert inst["id"] in run_data_plane_auth(client)["reapplied"]
    spec = _spec(fake)
    assert spec["authzConfigRef"] == {"name": FENCE}
    # 울타리는 이 테넌트의 것이고 서버보다 먼저 적용된다
    assert _fence(fake)["spec"] == {
        "type": "cedarv1",
        "config": {"policies": tenant_fence_policies("default")},
    }
    kinds = [o["kind"] for o in fake.applied[start:]]
    assert kinds.index(AUTHZ_CONFIG_KIND) < kinds.index("MCPServer")
    assert ("SPEC2OPENAPI_USERNAME", "username") in {
        (s["targetEnvName"], s["key"]) for s in spec["secrets"]
    }
    assert "env" not in spec["podTemplateSpec"]["spec"]["containers"][0]
    assert fallbacks(client, cid) == {}
    assert inst["id"] not in run_data_plane_auth(client)["reapplied"]
    # Advanced 편집은 울타리 설정을 읽기만 한다
    docs = client.get(f"{MS}/{inst['id']}/manifest").json()["documents"]
    assert [d["kind"] for d in docs] == ["MCPServer", AUTHZ_CONFIG_KIND]
    assert docs[1]["locked"] == ["*"]
    for request in (
        lambda: client.put(f"{MS}/{inst['id']}/overrides", json={"patch": {}}),
        lambda: client.post(f"{MS}/{inst['id']}/drift/reset"),
    ):
        fake.delete(GV, AUTHZ_CONFIG_KIND, FENCE, DEFAULT_NS)
        start = len(fake.applied)
        r = request()
        assert r.status_code == 200, r.text
        assert [o["kind"] for o in fake.applied[start:]] == [AUTHZ_CONFIG_KIND, "MCPServer"]
        assert _spec(fake)["authzConfigRef"] == {"name": FENCE} and _fence(fake) is not None


def test_without_the_g8_rights_the_fence_is_inline(client, fake_clusters, mongo):
    """울타리 판(0.52)은 지나도 MCPAuthzConfig 를 쓸 권한이 없으면 인라인이다. 권한이 바뀐 것만으로
    다시 렌더되고, 그 서버는 Cedar 참조를 빼고 도는 것으로 센다."""
    cid, inst, fake = _server(client, fake_clusters)
    revoke_tenant_rights(mongo, cid)
    assert inst["id"] in run_data_plane_auth(client)["reapplied"]
    spec = _spec(fake)
    assert spec["authzConfig"] == render_authz_inline(tenant_fence_policies("default"))
    assert "authzConfigRef" not in spec
    assert fallbacks(client, cid)["authz_ref"] == 1


def test_a_refused_fence_stops_the_deploy_before_the_server(client, fake_clusters):
    fake = FakeClusterClient()
    fake.authz_invalid = "policy 1: unexpected token"
    fake_clusters.by_name["edge"] = fake
    _cid, _bp, _ds, ms = mcp_server_env(client, backend_auth="basic")
    r = clone_and_deploy(client, ms)
    assert detail_code(r) == (422, "authz_invalid"), r.text
    assert "unexpected token" in r.json()["detail"]["detail"]
    assert fake.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS) is None


def test_a_second_binding_of_one_database_reads_its_password_from_pod_env(client, fake_clusters):
    """한 Database 를 두 접두어로 묶으면 둘째 바인딩의 비밀번호는 같은 (Secret, 키)라 mcp 컨테이너
    env 로 간다."""
    _cid, bp, ds, ms = mcp_server_env(client)
    body = {
        "name": "calc-mcp",
        "backend_profile_id": bp["id"],
        "spec_version": 1,
        "tools": ["Add"],
        "bindings": [
            {"data_service_id": ds["id"], "env_prefix": "CACHE"},
            {"data_service_id": ds["id"], "env_prefix": "SPARE"},
        ],
    }
    assert client.put(f"{MS}/{ms['id']}", json=body).status_code == 200
    r = clone_and_deploy(client, ms)
    assert r.status_code == 201, r.text
    spec = _spec(fake_clusters.by_name["edge"])
    assert [s["targetEnvName"] for s in spec["secrets"]] == ["CACHE_PASSWORD"]
    (env,) = spec["podTemplateSpec"]["spec"]["containers"][0]["env"]
    assert env == {
        "name": "SPARE_PASSWORD",
        "valueFrom": {"secretKeyRef": {"name": ds["secret_name"], "key": "password"}},
    }
