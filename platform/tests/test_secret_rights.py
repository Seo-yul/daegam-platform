"""테넌트 Secret 권한 (P63, D75).

플랫폼은 테넌트 네임스페이스의 Secret 권한을 RoleBinding daegam-tenant-objects 로 받는다.
preflight 는 그 바인딩과 역할을 직접 읽고, 클러스터 전체 Secret 권한이 남아 있으면 클러스터 응답에
경고한다. 어느 길로도 쓸 수 없다고 확인된 클러스터에서는 테넌트 Secret 을 쓰는 길이 아무것도 쓰기
전에 422 다.
"""

from __future__ import annotations

from functools import partial

import pytest

from daegam.k8s.gates import tenant_secret_refusal
from daegam.k8s.preflight import (
    CLUSTER_SECRET_RIGHTS_WARNING,
    RBAC_API,
    REQUIRED_VERBS,
    SECRET_VERBS,
    TENANT_OBJECTS_BINDING_NAME,
    TENANT_OBJECTS_CLUSTER_ROLE,
    TENANT_SECRET_BINDINGS_WARNING,
    check_cluster,
    cluster_warnings,
    preflight_warnings,
    role_grants_secrets,
    secret_writability,
)
from daegam.services.exposure import ExposureInput
from daegam.services.mcp_servers import MCPServerError
from daegam.store.clusters import Preflight, SecretRightsCheck
from tests.helpers.agents import (
    agent_env,
    create_agent,
    deploy_agent,
    deploy_spec,
    template_for,
)
from tests.helpers.clusters import default_cluster, revoke_secret_rights
from tests.helpers.consts import AG, DEFAULT_NS, DS, GW, MS, PLATFORM_SUBJECT
from tests.helpers.data_services import created_database, post_database
from tests.helpers.docs import cluster_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.gateways import create_gateway, deploy_gateway, gateway_up
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

# 지금의 RBAC 파일을 쓰는 클러스터: 플랫폼 계정이 클러스터 전체에서는 Secret 을 다루지 못한다
NO_CLUSTER_SECRETS = {("", "secrets", verb) for verb in SECRET_VERBS}
NEITHER = (
    "rbac: core/secrets: neither cluster-wide nor through the ClusterRole daegam-tenant-objects"
)
OTHER_SUBJECT = {"kind": "ServiceAccount", "name": "other", "namespace": "daegam-system"}
ROLE_KEY = (RBAC_API, "ClusterRole", None, TENANT_OBJECTS_CLUSTER_ROLE)


def _bind(fake, ns, *, role=TENANT_OBJECTS_CLUSTER_ROLE, subject=PLATFORM_SUBJECT) -> None:
    fake.apply(
        {
            "apiVersion": RBAC_API,
            "kind": "RoleBinding",
            "metadata": {"name": TENANT_OBJECTS_BINDING_NAME, "namespace": ns},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": role,
            },
            "subjects": [subject],
        }
    )


def _p50_role(fake) -> FakeClusterClient:
    """Secret 동사가 없는 P50 의 daegam-tenant-objects 로 둔다."""
    fake.objects[ROLE_KEY]["rules"] = []
    return fake


# -- preflight ------------------------------------------------------------------------------


def test_preflight_passes_without_cluster_wide_secret_rights():
    """지금의 RBAC 파일을 쓰는 클러스터는 daegam-tenant-objects 가 Secret 을 주면 preflight 가
    통과하고 경고도 없다."""
    assert [v for v in REQUIRED_VERBS if v[1] == "secrets"] == []
    pf, gates = check_cluster(FakeClusterClient(denied=NO_CLUSTER_SECRETS))
    assert pf.ok and pf.missing == [], pf.missing
    assert gates.tenant_object_rights is True
    assert pf.secret_rights == SecretRightsCheck(cluster_wide=False, role=True, writable=True)
    assert CLUSTER_SECRET_RIGHTS_WARNING not in cluster_warnings(pf)


def test_the_cluster_wide_path_needs_every_secret_verb():
    """클러스터 전체 길은 get 만이 아니라 쓰는 동사(create, patch, delete)까지 따로 묻는다. 하나라도
    거부되면 그 길로는 쓸 수 없고 경고도 하지 않는다. 역할의 길이 열려 있으면 쓸 수 있다."""
    assert SECRET_VERBS == ("get", "create", "patch", "delete")
    fake = FakeClusterClient(denied={("", "secrets", "create")})
    pf, _ = check_cluster(fake)
    assert (pf.secret_rights.cluster_wide, pf.secret_rights.writable) == (False, True)
    assert CLUSTER_SECRET_RIGHTS_WARNING not in cluster_warnings(pf)
    _p50_role(fake)
    pf, _ = check_cluster(fake)
    assert pf.secret_rights.writable is False and pf.missing == [NEITHER]


def test_cluster_wide_secret_rights_warn_on_cluster_responses_only():
    """클러스터 전체 Secret 권한이 남은 클러스터는 클러스터 관리자가 할 일이라 Cluster 응답에만
    경고하고 Instance 생성 응답의 경고(preflight_warnings)에는 없다."""
    pf, _ = check_cluster(FakeClusterClient(cluster_wide_secrets=True))
    assert pf.ok and pf.secret_rights.cluster_wide is True
    assert CLUSTER_SECRET_RIGHTS_WARNING in cluster_warnings(pf)
    assert CLUSTER_SECRET_RIGHTS_WARNING not in preflight_warnings(pf)
    assert cluster_warnings(None) == preflight_warnings(None)


@pytest.mark.parametrize("role", ["absent", "without_secrets"])
def test_preflight_fails_when_no_path_writes_tenant_secrets(role):
    """클러스터 전체 권한이 없고 daegam-tenant-objects 로도 받지 못하면 ok 가 거짓이다. 옛 RBAC
    파일의 ClusterRole 이 남은 클러스터는 통과한다."""
    fake = FakeClusterClient(denied=set(NO_CLUSTER_SECRETS), tenant_objects_role=role != "absent")
    if role == "without_secrets":
        _p50_role(fake)
    pf, _ = check_cluster(fake)
    assert not pf.ok and pf.missing == [NEITHER] and pf.secret_rights.writable is False
    fake.denied = set()
    fake.cluster_wide_secrets = True
    pf, _ = check_cluster(fake)
    assert pf.ok and pf.missing == []
    assert (pf.secret_rights.cluster_wide, pf.secret_rights.writable) == (True, True)


class _FlakyGate(FakeClusterClient):
    """권한 게이트의 접근 검사(ClusterRole daegam-tenant-objects 의 get·bind)가 오류로 끝나는
    클러스터."""

    def can_i(self, verb, resource, group="", namespace=None, name=None):
        if name == TENANT_OBJECTS_CLUSTER_ROLE:
            raise RuntimeError("connection reset")
        return super().can_i(verb, resource, group, namespace, name)


def test_a_transient_gate_error_decides_nothing():
    """처음 점검에서 권한 게이트의 접근 검사가 오류로 끝나면 게이트도 역할도 판정하지 못한 것이다.
    클러스터 전체 길이 닫혀 있어도 쓸 수 없다고 정하지 않는다."""
    pf, gates = check_cluster(_FlakyGate(denied=set(NO_CLUSTER_SECRETS)))
    assert gates.tenant_object_rights is None
    rights = pf.secret_rights
    assert (rights.cluster_wide, rights.role, rights.writable) == (False, None, None)
    assert NEITHER not in pf.missing


def test_the_binding_check_reads_each_managed_namespace():
    """접근 검사는 옛 ClusterRole 을 거쳐 통과해 버리므로 바인딩을 직접 읽는다. 없거나, 다른 역할을
    가리키거나, 플랫폼을 묶지 않는 바인딩은 묶이지 않은 곳이다."""
    fake = FakeClusterClient()
    _bind(fake, "ns-a")
    _bind(fake, "ns-c", role="admin")
    _bind(fake, "ns-d", subject=OTHER_SUBJECT)
    pf, _ = check_cluster(fake, ["ns-d", "ns-a", "ns-b", "ns-c", "ns-a"])
    rights = pf.secret_rights
    assert (rights.namespaces, rights.unbound) == (4, 3)
    assert rights.unbound_namespaces == ["ns-b", "ns-c", "ns-d"]
    # 묶이지 않은 곳은 ok 를 바꾸지 않는다. 빠진 바인딩은 배포 길과 쓸기가 만든다
    assert pf.ok
    pf, _ = check_cluster(fake, ["ns-a"])
    assert (pf.secret_rights.namespaces, pf.secret_rights.unbound) == (1, 0)
    # 목록을 받지 못한 점검은 바인딩을 보지 않는다
    pf, _ = check_cluster(fake)
    assert pf.secret_rights.namespaces is None and pf.secret_rights.unbound_namespaces == []


def test_without_its_own_identity_the_check_reads_only_the_role_reference():
    fake = FakeClusterClient(denied={("authentication.k8s.io", "selfsubjectreviews", "create")})
    _bind(fake, "ns-c", role="admin")
    _bind(fake, "ns-d", subject=OTHER_SUBJECT)
    pf, _ = check_cluster(fake, ["ns-c", "ns-d"])
    assert pf.secret_rights.unbound_namespaces == ["ns-c"]


def test_an_unreadable_binding_counts_as_unbound():
    class FlakyBinding(FakeClusterClient):
        def get(self, api_version, kind, name, namespace=None):
            if kind == "RoleBinding" and namespace == "ns-b":
                raise RuntimeError("connection reset")
            return super().get(api_version, kind, name, namespace)

    fake = FlakyBinding()
    _bind(fake, "ns-a")
    _bind(fake, "ns-b")
    pf, _ = check_cluster(fake, ["ns-a", "ns-b"])
    assert pf.secret_rights.unbound_namespaces == ["ns-b"]


def test_an_error_in_the_inverse_check_is_undecided():
    """클러스터 전체 Secret 권한을 묻다가 오류가 나면 판정하지 못한 것이다. 경고하지 않고, 다른 접근
    검사의 오류처럼 동사마다 missing 에 남는다. 역할의 길이 열려 있으면 쓸 수 있다."""

    class FlakyReview(FakeClusterClient):
        def can_i(self, verb, resource, group="", namespace=None, name=None):
            if resource == "secrets":
                raise RuntimeError("connection reset")
            return super().can_i(verb, resource, group, namespace, name)

    pf, _ = check_cluster(FlakyReview())
    assert (pf.secret_rights.cluster_wide, pf.secret_rights.writable) == (None, True)
    assert pf.missing == [f"rbac-check: core/secrets:{v}: connection reset" for v in SECRET_VERBS]
    assert CLUSTER_SECRET_RIGHTS_WARNING not in cluster_warnings(pf)


@pytest.mark.parametrize(
    ("cluster_wide", "gate", "role", "writable"),
    [
        (True, None, None, True),
        (None, True, True, True),
        (False, False, None, False),
        (False, True, False, False),
        # 판정하지 못한 재료가 남으면 정하지 않는다
        (False, None, None, None),
        (False, True, None, None),
        (None, False, None, None),
    ],
)
def test_secret_writability(cluster_wide, gate, role, writable):
    assert secret_writability(cluster_wide, gate, role) is writable


@pytest.mark.parametrize(
    ("rules", "grants"),
    [
        ([{"apiGroups": [""], "resources": ["secrets"], "verbs": list(SECRET_VERBS)}], True),
        # 동사가 여러 규칙에 나뉘어도 모으면 된다
        (
            [
                {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create"]},
                {"apiGroups": [""], "resources": ["secrets", "configmaps"], "verbs": ["*"]},
            ],
            True,
        ),
        ([{"apiGroups": [""], "resources": ["*"], "verbs": list(SECRET_VERBS)}], True),
        ([{"apiGroups": ["*"], "resources": ["secrets"], "verbs": list(SECRET_VERBS)}], True),
        (
            [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create", "patch"]}],
            False,
        ),
        # 이름 한정 규칙은 테넌트 Secret 을 주지 못한다
        (
            [
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "resourceNames": ["x"],
                    "verbs": list(SECRET_VERBS),
                }
            ],
            False,
        ),
        ([{"apiGroups": ["apps"], "resources": ["secrets"], "verbs": list(SECRET_VERBS)}], False),
        ([], False),
    ],
)
def test_role_grants_secrets(rules, grants):
    assert role_grants_secrets({"rules": rules}) is grants


# -- 422 의 규칙 -------------------------------------------------------------------------------


def _cluster(preflight: Preflight | None):
    """마지막 preflight 가 preflight 인 클러스터. 저장된 권한 게이트는 기본값(거짓)이다."""
    c = cluster_doc()
    c.status.preflight = preflight
    assert c.gates.tenant_object_rights is False
    return c


def _checked(writable, role=None) -> Preflight:
    """쓸 수 있는지를 writable 로 정한 preflight."""
    rights = SecretRightsCheck(cluster_wide=False, role=role, writable=writable)
    return Preflight(ok=True, secret_rights=rights)


@pytest.mark.parametrize(
    ("preflight", "found"),
    [
        (_checked(True), None),
        # 판정하지 못한 점검은 저장된 게이트가 기본값으로 닫혀 있어도 막지 않는다
        (_checked(None), None),
        # preflight 를 아직 돌리지 않은 클러스터와 Secret 권한을 적지 않은 preflight 도 막지 않는다
        (None, None),
        (Preflight(ok=True), None),
        (_checked(False), "no cluster-wide Secret rights and no daegam-tenant-objects rights"),
        (
            _checked(False, False),
            "no cluster-wide Secret rights and daegam-tenant-objects without the Secret verbs",
        ),
    ],
)
def test_only_a_cluster_that_cannot_write_tenant_secrets_is_refused(preflight, found):
    refusal = tenant_secret_refusal(_cluster(preflight))
    if found is None:
        assert refusal is None
        return
    assert refusal is not None
    assert (refusal["code"], refusal["feature"], refusal["found"]) == (
        "cluster_rights_missing",
        "tenant_secrets",
        found,
    )


# -- 테넌트 Secret 을 쓰는 길 ----------------------------------------------------------------


def _refused(r) -> None:
    assert detail_code(r) == (422, "cluster_rights_missing"), r.text
    assert r.json()["detail"]["feature"] == "tenant_secrets"


def test_databases_are_refused_before_anything_is_written(client, fake_clusters, mongo):
    """preflight 가 본 그대로다. 클러스터 전체 권한이 없고 daegam-tenant-objects 도 없는 클러스터는
    네임스페이스도 Secret 도 만들지 않고 422 다."""
    fake = FakeClusterClient(denied=set(NO_CLUSTER_SECRETS), tenant_objects_role=False)
    fake_clusters.by_name["edge"] = fake
    cid = default_cluster(client)
    preflight = client.get(f"/clusters/{cid}").json()["status"]["preflight"]
    assert preflight["secret_rights"]["writable"] is False
    _refused(post_database(client))
    assert fake.find("Secret") == [] and fake.find("Namespace") == []
    assert client.get(DS).json() == []
    # 옛 RBAC 파일의 ClusterRole 이 남은 클러스터는 권한 게이트가 닫혀 있어도 만든다
    fake.denied = set()
    fake.cluster_wide_secrets = True
    assert client.post(f"/clusters/{cid}/preflight").status_code == 200
    assert created_database(client)["name"] == "pg"


def test_database_credentials_are_not_touched_on_such_a_cluster(client, fake_clusters, mongo):
    cid = default_cluster(client)
    ds = created_database(client)
    fake = fake_clusters.by_name["edge"]
    secret = fake.get("v1", "Secret", f"ds-{ds['id']}", DEFAULT_NS)
    revoke_secret_rights(mongo, cid)
    _refused(client.patch(f"{DS}/{ds['id']}", json={"password": "new"}))
    _refused(client.delete(f"{DS}/{ds['id']}"))
    assert fake.get("v1", "Secret", f"ds-{ds['id']}", DEFAULT_NS) == secret
    assert client.get(f"{DS}/{ds['id']}").status_code == 200
    # Secret 을 건드리지 않는 수정은 그대로다
    r = client.patch(f"{DS}/{ds['id']}", json={"display_name": "PG"})
    assert r.status_code == 200, r.text


def test_mcp_server_deploys_edits_and_rotation_are_refused(client, fake_clusters, mongo):
    cid, _bp, _ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    fake = fake_clusters.by_name["edge"]
    revoke_secret_rights(mongo, cid)
    applied = len(fake.applied)
    _refused(clone_and_deploy(client, ms, name="calc-2"))
    draft = next(d for d in client.get(MS).json() if d["name"] == "calc-2")
    assert draft["state"] == "draft"
    _refused(client.post(f"{MS}/{inst['id']}/redeploy"))
    _refused(client.patch(f"{MS}/{inst['id']}", json={"exposure": {"enabled": False}}))
    _refused(client.patch(f"{MS}/{inst['id']}", json={"replicas": 2}))
    service = client.app.state.mcp_server_service
    doc = client.portal.call(client.app.state.mcp_servers.get, inst["id"])
    with pytest.raises(MCPServerError) as ei:
        client.portal.call(service.rotate_svc_token, doc)
    assert ei.value.code == "cluster_rights_missing"
    with pytest.raises(MCPServerError):
        client.portal.call(
            partial(service.basic_patch, doc, exposure=ExposureInput(), sent={"exposure"})
        )
    assert len(fake.applied) == applied
    stored = mongo["mcp_servers"].find_one({"_id": inst["id"]})
    assert stored["svc_token_rev"] == doc.svc_token_rev and stored["state"] == "deployed"


def test_gateway_deploys_and_exposure_are_refused(client, fake_clusters, mongo):
    cid, items, *_rest, fake = hub_env(client, fake_clusters)
    gw = gateway_up(client, items)
    revoke_secret_rights(mongo, cid)
    applied = len(fake.applied)
    r = create_gateway(client, items, name="b")
    assert r.status_code == 201, r.text
    _refused(deploy_gateway(client, r.json()["id"]))
    _refused(client.post(f"{GW}/{gw['id']}/redeploy"))
    _refused(client.patch(f"{GW}/{gw['id']}", json={"exposure": {"enabled": False}}))
    assert len(fake.applied) == applied
    assert client.get(f"{GW}/{r.json()['id']}").json()["state"] == "draft"


def test_agent_deploys_are_refused(client, fake_clusters, mongo):
    cid, ds, gw = agent_env(client, mongo)
    _profile, agent = deploy_spec(client, template_for(gw, ds))
    revoke_secret_rights(mongo, cid)
    fake = fake_clusters.by_name["edge"]
    applied = len(fake.applied)
    draft = create_agent(client, template_for(gw, ds), "second").json()
    _refused(deploy_agent(client, draft["id"]))
    _refused(client.post(f"{AG}/{agent['id']}/redeploy", json={}))
    assert len(fake.applied) == applied


# -- 관리 네임스페이스를 받는 preflight -------------------------------------------------------


def test_preflight_checks_the_binding_where_the_platform_keeps_tenant_objects(
    client, fake_clusters, mongo
):
    """preflight 는 인증 재적용의 스윕이 맞추는 네임스페이스마다 바인딩을 읽는다. 배포가 깐 바인딩이
    있으면 빠진 곳이 없고, 지워지면 그 네임스페이스가 묶이지 않은 곳이다. 이름은 cluster:write 의
    응답에만 싣는다."""
    cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    r = client.post(f"/clusters/{cid}/preflight")
    assert r.status_code == 200, r.text
    assert r.json()["preflight"]["secret_rights"] == {
        "cluster_wide": False,
        "role": True,
        "writable": True,
        "namespaces": 1,
        "unbound": 0,
        "unbound_namespaces": [],
    }
    assert TENANT_SECRET_BINDINGS_WARNING not in r.json()["warnings"]

    # 클러스터 전체 권한 없이 바인딩이 빠지면 그곳의 Secret 은 다룰 수 없으므로 경고한다
    fake = fake_clusters.by_name["edge"]
    del fake.objects[(RBAC_API, "RoleBinding", DEFAULT_NS, TENANT_OBJECTS_BINDING_NAME)]
    r = client.post(f"/clusters/{cid}/preflight")
    rights = r.json()["preflight"]["secret_rights"]
    assert (rights["unbound"], rights["unbound_namespaces"]) == (1, [DEFAULT_NS])
    assert TENANT_SECRET_BINDINGS_WARNING in r.json()["warnings"]
    assert TENANT_SECRET_BINDINGS_WARNING in client.get(f"/clusters/{cid}").json()["warnings"]


# -- 밖에서 지운 네임스페이스 ------------------------------------------------------------------
