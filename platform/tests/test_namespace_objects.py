"""테넌트 객체 (P50 1절·2절·10절).

권한 게이트를 통과한 클러스터의 테넌트 네임스페이스는 ClusterRole daegam-tenant-objects 를 묶은
RoleBinding 과 Role 없는 ServiceAccount daegam-remote-proxy 를 갖는다. 내부 OIDC 설정이 그 둘의 판을
객체 판 밖의 어노테이션으로 실어, 스윕이 그 판으로 지금 있는 네임스페이스에도 둘을 보장한다. 권한이
없는 클러스터에는 만들지 않는다. 테넌트 객체가 실패해도 배포와 설정, 문서의 재렌더는 막히지 않고
그 네임스페이스만 다시 시도를 미룬다.
"""

from __future__ import annotations

from daegam.k8s import client as k8s_client
from daegam.k8s.labels import ANNOTATION_DESIRED_HASH
from daegam.k8s.preflight import RBAC_API
from daegam.services.data_plane_auth import KIND_MCP_SERVER
from daegam.services.tenancy import (
    ANNOTATION_TENANT_OBJECTS,
    INTERNAL_OIDC_CONFIG_NAME,
    REMOTE_PROXY_SERVICE_ACCOUNT,
    TENANT_OBJECTS_BINDING_NAME,
    TOOLHIVE_API,
    ensure_oidc_configs,
    oidc_config_manifests,
    remote_proxy_service_account_manifest,
    tenant_objects_binding_manifest,
)
from tests.helpers.consts import DEFAULT_NS, PLATFORM_SUBJECT
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import raise_epoch, run_data_plane_auth


class _Forbidden(Exception):
    """API 서버의 403 과 같은 모양(`status`)의 예외."""

    status = 403


def _binding(fake, ns=DEFAULT_NS):
    return fake.get(RBAC_API, "RoleBinding", TENANT_OBJECTS_BINDING_NAME, ns)


def _account(fake, ns=DEFAULT_NS):
    return fake.get("v1", "ServiceAccount", REMOTE_PROXY_SERVICE_ACCOUNT, ns)


def _stamp(fake, ns=DEFAULT_NS):
    config = fake.get(TOOLHIVE_API, "MCPOIDCConfig", INTERNAL_OIDC_CONFIG_NAME, ns)
    return (config["metadata"].get("annotations") or {}).get(ANNOTATION_TENANT_OBJECTS)


def _objects(tenant="t", ns="ns"):
    return [
        tenant_objects_binding_manifest(tenant, ns, PLATFORM_SUBJECT),
        remote_proxy_service_account_manifest(tenant, ns),
    ]


def _deployed_without_rights(client, fake_clusters):
    """ClusterRole daegam-tenant-objects 가 없는 클러스터(옛 RBAC 로 등록한 원격 클러스터. Secret 은
    클러스터 전체로 받는다)에 MCP Server 하나를 배포한다. 등록 때의 preflight 가 권한 없음을
    기록한다."""
    fake = fake_clusters.by_name["edge"] = FakeClusterClient(
        tenant_objects_role=False, cluster_wide_secrets=True
    )
    cid, _bp, _ds, mp = mcp_server_env(client)
    r = clone_and_deploy(client, mp)
    assert r.status_code == 201, r.text
    return cid, fake, r.json()


def _grant_rights(mongo, cluster_id: str) -> None:
    """권한이 생긴 뒤 시간마다 도는 preflight 가 남길 기록이다."""
    mongo["clusters"].update_one(
        {"_id": cluster_id}, {"$set": {"gates.tenant_object_rights": True}}
    )


def test_the_stamp_rides_outside_the_internal_configs_desired_hash():
    """테넌트 객체의 판은 내부 OIDC 설정에 실리지만 객체 판(desired-hash)을 바꾸지 않는다. 사람
    토큰용 설정에는 싣지 않는다."""
    kw = {"internal_issuer": "http://issuer", "oidc_issuer": "http://idp"}
    human, internal = oidc_config_manifests("t", "ns", "i", tenant_objects_stamp="abc", **kw)
    bare = oidc_config_manifests("t", "ns", "i", **kw)[1]
    assert ANNOTATION_TENANT_OBJECTS not in human["metadata"]["annotations"]
    assert internal["metadata"]["annotations"][ANNOTATION_TENANT_OBJECTS] == "abc"
    assert ANNOTATION_TENANT_OBJECTS not in bare["metadata"]["annotations"]
    assert (
        internal["metadata"]["annotations"][ANNOTATION_DESIRED_HASH]
        == bare["metadata"]["annotations"][ANNOTATION_DESIRED_HASH]
    )


def test_failed_tenant_objects_hold_back_neither_the_configs_nor_the_documents(
    client, fake_clusters, mongo, monkeypatch
):
    """ServiceAccount 를 쓸 수 없으면 그 네임스페이스는 `objects_pending` 에 들고 판은 옛 값으로
    남지만, 그 네임스페이스의 문서는 다시 렌더된다. 다시 시도는 그 네임스페이스만 백오프로
    미루고, 클러스터 단위 백오프는 걸지 않는다."""
    cid, fake, server = _deployed_without_rights(client, fake_clusters)
    _grant_rights(mongo, cid)
    before = _stamp(fake)
    apply = fake.apply

    def refusing(obj, *args, **kw):
        if obj.get("kind") == "ServiceAccount":
            raise RuntimeError("serviceaccounts is forbidden by policy")
        return apply(obj, *args, **kw)

    monkeypatch.setattr(fake, "apply", refusing)
    raise_epoch(monkeypatch, KIND_MCP_SERVER)
    result = run_data_plane_auth(client)
    assert server["id"] in result["reapplied"]
    state = client.portal.call(client.app.state.data_plane_auth.store.cluster, cid)
    assert state.objects_pending == [DEFAULT_NS] and state.drifted == []
    assert not state.unreachable and state.retry_at is None
    assert state.namespace_retries[DEFAULT_NS].failures == 1
    assert _stamp(fake) == before and _binding(fake) is not None and _account(fake) is None
    # 미뤄 둔 동안에는 그 네임스페이스를 다시 보장하지 않는다
    assert run_data_plane_auth(client)["ensured"] == []


def test_a_write_waits_for_the_binding_it_relies_on(monkeypatch):
    """막 만든 RoleBinding 의 권한이 RBAC 캐시에 들기 전의 403 은 잠깐 기다렸다 다시 한다."""
    monkeypatch.setattr(k8s_client.time, "sleep", lambda _seconds: None)
    applied: list[str] = []
    denials = {"ServiceAccount": 2}

    class Client:
        def apply(self, obj, **_kw):
            if denials.get(obj["kind"]):
                denials[obj["kind"]] -= 1
                raise _Forbidden("serviceaccounts is forbidden")
            applied.append(obj["kind"])
            return obj

    ensured = ensure_oidc_configs(
        Client(), "t", "ns", "i", tenant_objects=_objects(), internal_issuer="http://issuer"
    )
    assert ensured and applied == ["RoleBinding", "ServiceAccount", "MCPOIDCConfig"]


def test_an_unknown_platform_identity_leaves_the_stamp_alone(client, fake_clusters, monkeypatch):
    """SelfSubjectReview 가 한 번 실패해도 테넌트 객체와 그 판을 건드리지 않아, 판이 틱마다 오가지
    않는다."""
    _cid, _bp, _ds, mp = mcp_server_env(client)
    assert clone_and_deploy(client, mp).status_code == 201
    fake = fake_clusters.by_name["edge"]
    stamp = _stamp(fake)

    def no_identity():
        raise RuntimeError("selfsubjectreviews unavailable")

    monkeypatch.setattr(fake, "whoami", no_identity)
    assert run_data_plane_auth(client)["ensured"] == []
    assert _stamp(fake) == stamp and _binding(fake) is not None
