"""P8 F3: 허용 네임스페이스(Tenant Settings allowed_namespaces), namespace_claimed,
RoleBinding daegam-tls-reader(SelfSubjectReview subject), exposure-options(Secret 이름만),
RBAC 표. 허용 목록에서 빠진 네임스페이스로의 재배포 409 는 test_mcp_servers_lifecycle.py 와
test_gateways_lifecycle.py 가 본다."""

from __future__ import annotations

import asyncio

import pytest

from daegam.config import Settings
from daegam.config.resolve import EffectiveNamespace
from daegam.k8s.preflight import (
    REQUIRED_VERBS,
    SANDBOX_POD_VERBS,
    SANDBOX_VERBS,
    TENANT_OBJECT_VERBS,
    TENANT_OBJECTS_CLUSTER_ROLE,
    role_grants,
    role_grants_secrets,
)
from daegam.services.tenancy import TenancyError, check_namespace_name
from tests.helpers.auth import bearer
from tests.helpers.chart import render_template
from tests.helpers.consts import GV, NP_API, PLATFORM_SUBJECT, T2
from tests.helpers.exposure import cluster_secret, ingress_class
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

RBAC_API = "rbac.authorization.k8s.io/v1"


# -- allowed_namespaces --------------------------------------------------------------------


def test_allowed_namespaces_ensure_objects_and_claim(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    r = client.patch(
        "/tenants/default/settings", json={"allowed_namespaces": ["team-a", "team-a", "team-b"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowed_namespaces"] == ["team-a", "team-b"]
    for ns in ("team-a", "team-b"):
        n = fake.get("v1", "Namespace", ns)
        assert n["metadata"]["labels"]["daegam.devops.ai.kr/tenant"] == "default"
        np = fake.get(NP_API, "NetworkPolicy", "daegam-tenant-ingress", ns)
        assert {
            "namespaceSelector": {"matchLabels": {"daegam.devops.ai.kr/tenant": "default"}}
        } in np["spec"]["ingress"][0]["from"]
        assert fake.get(GV, "MCPGroup", "default", ns) is not None
        rb = fake.get(RBAC_API, "RoleBinding", "daegam-tls-reader", ns)
        assert rb["roleRef"]["name"] == "daegam-tls-reader"
        assert rb["subjects"] == [PLATFORM_SUBJECT]
    # 기본 네임스페이스에도 RoleBinding 이 붙는다 (인스턴스 생성 경로)
    assert clone_and_deploy(client, mp).status_code == 201
    rb = fake.get(RBAC_API, "RoleBinding", "daegam-tls-reader", "daegam-default")
    assert rb["subjects"][0]["name"] == "daegam-platform"
    # 다른 테넌트가 같은 네임스페이스를 넣으면 422 namespace_claimed
    r = client.patch(
        "/tenants/other/settings",
        json={"allowed_namespaces": ["team-a"]},
        headers=T2,
    )
    assert detail_code(r) == (422, "namespace_claimed")
    assert client.get("/tenants/other/settings").json()["allowed_namespaces"] == []
    # 라벨 없는 기존 네임스페이스는 점유할 수 있다 (platform-admin 만 여기 온다)
    fake.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "legacy-ns"}})
    r = client.patch("/tenants/other/settings", json={"allowed_namespaces": ["legacy-ns"]})
    assert r.status_code == 200, r.text
    assert fake.get("v1", "Namespace", "legacy-ns")["metadata"]["labels"][
        "daegam.devops.ai.kr/tenant"
    ] == ("other")
    # 이름 검증
    r = client.patch("/tenants/default/settings", json={"allowed_namespaces": ["Bad_Name"]})
    assert detail_code(r) == (422, "invalid_value")
    # 비우기
    r = client.patch("/tenants/default/settings", json={"allowed_namespaces": []})
    assert r.status_code == 200 and r.json()["allowed_namespaces"] == []


def test_allowed_namespaces_need_settings_write(oidc, token):
    ta = bearer(token("ta", "daegam-a-admin"))
    # tenant-admin 의 members:write 로는 다른 필드는 되지만 allowed_namespaces 는 403
    r = oidc.patch("/tenants/a/settings", json={"quota": {"pods": "1"}}, headers=ta)
    assert r.status_code == 200, r.text
    r = oidc.patch("/tenants/a/settings", json={"allowed_namespaces": ["x"]}, headers=ta)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["scope"] == "settings:write"
    # 네임스페이스를 옮기는 것도 같은 급이다. 라벨 없는 기존 네임스페이스를 점유하는 길이기 때문이다
    r = oidc.patch("/tenants/a/settings", json={"namespace": {"name": "x"}}, headers=ta)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "settings:write"
    # platform-admin 은 되지만 클러스터가 없으면 422 no_cluster
    admin = bearer(token("root", "daegam-admins"))
    r = oidc.patch("/tenants/a/settings", json={"allowed_namespaces": ["x"]}, headers=admin)
    assert detail_code(r) == (422, "no_cluster")


RESERVED = Settings().reserved_namespaces


def test_check_namespace_name():
    assert check_namespace_name("team-a", reserved=RESERVED) == "team-a"
    for bad in ("daegam-system", "kube-system", "kube-anything", "default", "toolhive-system"):
        with pytest.raises(TenancyError) as e:
            check_namespace_name(bad, reserved=RESERVED)
        assert e.value.code == "namespace_reserved", bad
    with pytest.raises(TenancyError) as e:
        check_namespace_name("Bad_Name", reserved=RESERVED)
    assert e.value.code == "invalid_value"


def test_a_platform_in_another_namespace_still_refuses_the_live_platform_namespace():
    """설치본에 붙여 띄운 로컬 플랫폼은 daegam-dev-system 을 쓴다. 테넌트 system 이 라이브의
    daegam-system 으로 풀려 테넌트 라벨·RoleBinding 이 붙지 않게 한다."""
    reserved = Settings(namespace="daegam-dev-system").reserved_namespaces
    for bad in ("daegam-system", "daegam-dev-system"):
        with pytest.raises(TenancyError) as e:
            check_namespace_name(bad, reserved=reserved)
        assert e.value.code == "namespace_reserved", bad


@pytest.mark.parametrize("gateway_ns", ["daegam-llm", "llm-keys"])
def test_the_llm_gateway_namespace_is_reserved_with_the_gateway_off(gateway_ns):
    """게이트웨이를 켜든 끄든 그 네임스페이스는 테넌트가 쓰지 못한다. 템플릿 daegam-{tenant} 는
    테넌트 llm 에게 daegam-llm 을 주므로 모든 provider 키 옆에 테넌트 워크로드가 놓일 것이다
    (P38 3절)."""
    reserved = Settings(llm_gateway_namespace=gateway_ns).reserved_namespaces
    assert Settings(llm_gateway_namespace=gateway_ns).llm_gateway_on is False
    with pytest.raises(TenancyError) as e:
        check_namespace_name(gateway_ns, reserved=reserved)
    assert e.value.code == "namespace_reserved"


def test_every_name_source_refuses_the_gateway_namespace(client):
    """Tenant Settings 의 namespace·allowed_namespaces 두 입구와, 템플릿이 만든 이름까지 지나는
    ensure_namespace 가 모두 422 namespace_reserved 다 (P38 3절)."""
    gw = client.app.state.settings.llm_gateway_namespace
    r = client.patch("/tenants/default/settings", json={"namespace": {"name": gw}})
    assert detail_code(r) == (422, "namespace_reserved")
    r = client.patch("/tenants/default/settings", json={"allowed_namespaces": [gw]})
    assert detail_code(r) == (422, "namespace_reserved")
    # ensure_namespace 는 클러스터에 닿기 전에 이름부터 본다. 템플릿이 만든 이름(테넌트 llm 의
    # daegam-llm)도 여기를 지난다
    tenancy = client.app.state.tenancy
    with pytest.raises(TenancyError) as e:
        asyncio.run(tenancy.ensure_namespace("llm", None, EffectiveNamespace(name=gw)))
    assert e.value.code == "namespace_reserved"


def test_tenant_namespace_cannot_be_the_platform_or_k8s_namespace(client):
    """Tenant Settings 의 namespace.name 은 검사 없이 resolve 로 흘러 들어갔다. `daegam-system` 을
    적으면 ensure_namespace 가 거기에 테넌트 라벨을 붙이고 RoleBinding 을 만들어 cluster-* Secret 이
    사는 네임스페이스에 테넌트 워크로드가 들어갔다. 두 입구(namespace·allowed_namespaces) 모두
    422 namespace_reserved 다. 막는 이름마다의 판정은 test_check_namespace_name 이 본다."""
    name = "default"
    r = client.patch("/tenants/default/settings", json={"namespace": {"name": name}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "namespace_reserved"
    r = client.patch("/tenants/default/settings", json={"allowed_namespaces": [name]})
    assert detail_code(r) == (422, "namespace_reserved")
    assert client.get("/tenants/default/settings").json()["namespace"] is None
    # 모양이 틀린 이름은 모델이 먼저 막는다
    r = client.patch("/tenants/default/settings", json={"namespace": {"name": "Bad_Name"}})
    assert r.status_code == 422, r.text


# -- redeploy: 인스턴스 자신의 네임스페이스가 허용 목록에서 빠지면 409 -------------------------


# -- exposure-options -----------------------------------------------------------------------


def test_exposure_options_lists_names_only(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(
        client, cluster_extra={"ingress": {"enabled": True, "domain": "mcp.example.com"}}
    )
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    ingress_class(fake, "traefik")
    ingress_class(fake, "nginx")
    cluster_secret(fake, "daegam-default", "site-tls", tls=True)
    cluster_secret(fake, "daegam-default", "creds", tls=False)
    cluster_secret(fake, "elsewhere", "other-tls", tls=True)
    r = client.get(f"/clusters/{cid}/exposure-options")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True and body["namespace"] == "daegam-default"
    assert body["namespaces"] == ["daegam-default"]
    assert body["ingress_classes"] == ["nginx", "traefik"]
    assert body["tls_secrets"] == ["site-tls"]
    assert body["cluster_issuers"] == [] and body["allow_unauthenticated"] is False
    assert body["defaults"] == {
        "host": "{name}.default.mcp.example.com",
        "ingress_class": None,
        "tls_mode": "none",
        "issuer": None,
    }
    # Secret data 는 응답 어디에도 없다
    assert "PRIVATE-KEY-MATERIAL" not in r.text and "CERT" not in r.text
    # 허용 네임스페이스는 선택할 수 있고 그 네임스페이스의 Secret 만 나온다
    assert (
        client.patch(
            "/tenants/default/settings", json={"allowed_namespaces": ["elsewhere"]}
        ).status_code
        == 200
    )
    r = client.get(f"/clusters/{cid}/exposure-options", params={"namespace": "elsewhere"})
    assert r.status_code == 200, r.text
    assert r.json()["namespaces"] == ["daegam-default", "elsewhere"]
    assert r.json()["tls_secrets"] == ["other-tls"]
    r = client.get(f"/clusters/{cid}/exposure-options", params={"namespace": "nope"})
    assert detail_code(r) == (422, "namespace_not_allowed")
    # cert-manager CRD 가 있으면 ClusterIssuer 이름이 나온다
    fake.extra_discovery["cert-manager.io/v1"] = ["clusterissuers", "certificates"]
    fake.apply(
        {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "le"}}
    )
    body = client.get(f"/clusters/{cid}/exposure-options").json()
    assert body["cluster_issuers"] == ["le"]
    # 404
    assert client.get("/clusters/nope/exposure-options").status_code == 404


def test_exposure_options_forbidden_reports_missing_verb(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    fake.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "daegam-default"}})
    fake.denied.add(("", "secrets", "list"))
    r = client.get(f"/clusters/{cid}/exposure-options")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "forbidden"
    assert r.json()["detail"]["missing"] == ["secrets:list@daegam-default"]
    fake.unreachable = True
    r = client.get(f"/clusters/{cid}/exposure-options")
    assert detail_code(r) == (502, "cluster_unreachable")


def test_exposure_options_before_first_deploy_skips_secret_list(client, fake_clusters):
    """첫 배포 전에는 네임스페이스도, 그 안의 Secret 을 읽는 RoleBinding 도 아직 없다.
    목록을 물으면 403 이 나 "rbac.yaml 을 다시 적용하라" 는 틀린 안내가 되므로 묻지 않고
    빈 목록으로 답한다."""
    # Data Service 는 비밀번호 Secret 을 두려고 네임스페이스를 먼저 깐다. 여기서는 만들지 않는다
    cid, bp, ds, mp = mcp_server_env(client, ds=False)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    assert fake.get("v1", "Namespace", "daegam-default") is None
    fake.denied.add(("", "secrets", "list"))
    r = client.get(f"/clusters/{cid}/exposure-options")
    assert r.status_code == 200, r.text
    assert r.json()["tls_secrets"] == []
    # 네임스페이스를 볼 권한이 없으면 전처럼 목록을 묻고, 그 403 을 그대로 알린다
    fake.denied.add(("", "namespaces", "get"))
    r = client.get(f"/clusters/{cid}/exposure-options")
    assert r.status_code == 403 and r.json()["detail"]["missing"] == ["secrets:list@daegam-default"]


def test_exposure_options_defaults_pick_configured_class(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(
        client,
        cluster_extra={
            "ingress": {
                "class_name": "traefik",
                "allowed_classes": ["traefik", "nginx"],
                "allow_unauthenticated": True,
                "tls": "cert-manager",
                "default_issuer": "le",
            }
        },
    )
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    ingress_class(fake, "traefik")
    ingress_class(fake, "haproxy")  # 허용 목록 밖
    body = client.get(f"/clusters/{cid}/exposure-options").json()
    assert body["enabled"] is False and body["allow_unauthenticated"] is True
    assert body["ingress_classes"] == ["traefik"]
    assert body["defaults"]["ingress_class"] == "traefik" and body["defaults"]["host"] is None
    # cert-manager 가 없으면 기본 TLS 모드는 none 으로 내려가고 issuer 도 없다
    assert body["defaults"]["tls_mode"] == "none" and body["defaults"]["issuer"] is None
    fake.extra_discovery["cert-manager.io/v1"] = ["clusterissuers"]
    fake.apply(
        {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "le"}}
    )
    body = client.get(f"/clusters/{cid}/exposure-options").json()
    assert body["defaults"]["tls_mode"] == "cert-manager" and body["defaults"]["issuer"] == "le"


def test_kubecontext_identity_binds_a_user(client, fake_clusters):
    fake_clusters.by_name["edge"] = FakeClusterClient(identity="kubernetes-admin")
    cid, bp, ds, mp = mcp_server_env(client)
    assert clone_and_deploy(client, mp).status_code == 201
    rb = fake_clusters.by_name["edge"].get(
        RBAC_API, "RoleBinding", "daegam-tls-reader", "daegam-default"
    )
    assert rb["subjects"] == [
        {"kind": "User", "name": "kubernetes-admin", "apiGroup": "rbac.authorization.k8s.io"}
    ]


# -- RBAC 매니페스트와 REQUIRED_VERBS 가 같다 ----------------------------------------------------


# home 과 원격 클러스터의 RBAC 는 대감 차트의 templates/rbac.yaml 이 같은 규칙 목록으로 렌더한다
# (P85 8절). 원격 클러스터는 target: remote 다
HOME_RBAC = "home"
REMOTE_RBAC = "remote"


def _docs(target: str) -> list[dict]:
    return render_template("rbac.yaml", sets={"target": target})


def _rules(source: str, role: str) -> list[dict]:
    for d in _docs(source):
        if d["kind"] == "ClusterRole" and d["metadata"]["name"] == role:
            return d["rules"]
    raise AssertionError(f"ClusterRole {role} missing in {source}")


def _covers(rules: list[dict], group: str, resource: str, verb: str, name: str | None) -> bool:
    for rule in rules:
        if group not in rule.get("apiGroups", []):
            continue
        if resource not in rule.get("resources", []):
            continue
        if verb not in rule.get("verbs", []):
            continue
        names = rule.get("resourceNames")
        if names and name not in names:
            continue
        return True
    return False


def test_rbac_manifests_grant_every_required_verb():
    for source, role in (
        (HOME_RBAC, "daegam-platform"),
        (REMOTE_RBAC, "daegam-deployer"),
    ):
        rules = _rules(source, role)
        missing = [
            e for e in REQUIRED_VERBS if not _covers(rules, *e[:3], e[3] if len(e) > 3 else None)
        ]
        assert not missing, (source, missing)
        # secrets list 는 클러스터 전체에 없고 바인딩 없는 daegam-tls-reader 에만 있다
        assert not _covers(rules, "", "secrets", "list", None)
        reader = _rules(source, "daegam-tls-reader")
        assert reader == [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]}]
        docs = _docs(source)
        assert not any(
            d["kind"] == "ClusterRoleBinding" and d["roleRef"]["name"] == "daegam-tls-reader"
            for d in docs
        )


def test_rbac_manifests_carry_the_tenant_objects_role():
    """P50 2절: 게이트로만 보는 권한(get·bind daegam-tenant-objects)도 두 target 의 렌더가
    주고, 그 ClusterRole 은 바인딩 없이 네 가지만 담는다(P63 이 테넌트 Secret 을 더했다). 이름
    한정이라 다른 ClusterRole 은 묶지 못한다."""
    for source, role in (
        (HOME_RBAC, "daegam-platform"),
        (REMOTE_RBAC, "daegam-deployer"),
    ):
        rules = _rules(source, role)
        missing = [e for e in TENANT_OBJECT_VERBS if not _covers(rules, *e[:3], e[3])]
        assert not missing, (source, missing)
        assert not _covers(rules, "rbac.authorization.k8s.io", "clusterroles", "bind", "admin")
        objects = _rules(source, TENANT_OBJECTS_CLUSTER_ROLE)
        assert objects == [
            {
                "apiGroups": ["toolhive.stacklok.dev"],
                "resources": ["mcpauthzconfigs"],
                "verbs": ["get", "list", "create", "patch", "delete"],
            },
            {
                "apiGroups": [""],
                "resources": ["serviceaccounts"],
                "verbs": ["get", "create", "patch"],
            },
            {
                "apiGroups": ["authorization.k8s.io"],
                "resources": ["localsubjectaccessreviews"],
                "verbs": ["create"],
            },
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["get", "create", "update", "patch", "delete"],
            },
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "delete"]},
        ]
        # 플랫폼 자신의 ClusterRole 은 클러스터 전체에 MCPAuthzConfig·ServiceAccount 를 쓰지 않는다
        assert not _covers(rules, "toolhive.stacklok.dev", "mcpauthzconfigs", "create", None)
        assert not _covers(rules, "", "serviceaccounts", "create", None)
        docs = _docs(source)
        assert not any(
            d["kind"] == "ClusterRoleBinding"
            and d["roleRef"]["name"] == TENANT_OBJECTS_CLUSTER_ROLE
            for d in docs
        )


def test_rbac_grants_tenant_secrets_per_namespace_only():
    """P63: 두 target 의 daegam-tenant-objects 가 preflight 가 보는 Secret 동사를 모두 주고,
    플랫폼의 ClusterRole 에는 Secret 동사가 하나도 없다. Secret 의 list·watch 는 어디에도 없다."""
    for source, role in (
        (HOME_RBAC, "daegam-platform"),
        (REMOTE_RBAC, "daegam-deployer"),
    ):
        objects = _rules(source, TENANT_OBJECTS_CLUSTER_ROLE)
        assert role_grants_secrets({"rules": objects}), source
        # 와일드카드(`*`)로 Secret 을 주는 규칙도 잡는다
        rules = _rules(source, role)
        assert not any(
            {"", "*"} & set(r.get("apiGroups", []))
            and {"secrets", "*"} & set(r.get("resources", []))
            for r in rules
        ), source
        for verb in ("list", "watch"):
            assert not _covers(objects, "", "secrets", verb, None), (source, verb)


def test_rbac_manifests_carry_the_sandbox_rights_and_objects():
    """P40 10절: 두 target 의 렌더가 샌드박스의 게이트 권한(노드 get·list, PriorityClass get)과
    daegam-tenant-objects 의 파드 create·delete 를 주고, PriorityClass daegam-sandbox 와
    네임스페이스 daegam-probe 및 그곳의 RoleBinding 을 싣는다. exec·attach·ephemeral container 는
    어디에도 없다."""
    for source, role, account in (
        (HOME_RBAC, "daegam-platform", "daegam-platform"),
        (REMOTE_RBAC, "daegam-deployer", "daegam-deployer"),
    ):
        rules = _rules(source, role)
        missing = [
            e for e in SANDBOX_VERBS if not _covers(rules, *e[:3], e[3] if len(e) > 3 else None)
        ]
        assert not missing, (source, missing)
        # 파드 쓰기는 클러스터 전체에 없고 네임스페이스마다 묶는 역할에만 있다
        assert not any(_covers(rules, "", "pods", verb, None) for verb in SANDBOX_POD_VERBS)
        assert role_grants(
            {"rules": _rules(source, TENANT_OBJECTS_CLUSTER_ROLE)}, "pods", SANDBOX_POD_VERBS
        )
        docs = _docs(source)
        granted = {
            res
            for d in docs
            if d["kind"] in ("ClusterRole", "Role")
            for rule in d["rules"]
            for res in rule.get("resources", [])
        }
        assert not granted & {"pods/exec", "pods/attach", "pods/ephemeralcontainers"}, source
        [priority] = [d for d in docs if d["kind"] == "PriorityClass"]
        assert priority["metadata"]["name"] == "daegam-sandbox"
        assert (priority["value"], priority["preemptionPolicy"], priority["globalDefault"]) == (
            -10,
            "Never",
            False,
        )
        [probe] = [
            d for d in docs if d["kind"] == "Namespace" and d["metadata"]["name"] == "daegam-probe"
        ]
        # 그 네임스페이스의 파드는 restricted 아래에서만 뜬다
        assert probe["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
        [binding] = [
            d
            for d in docs
            if d["kind"] == "RoleBinding" and d["metadata"].get("namespace") == "daegam-probe"
        ]
        assert binding["roleRef"]["name"] == TENANT_OBJECTS_CLUSTER_ROLE
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": account, "namespace": "daegam-system"}
        ]
        # 네임스페이스가 그 RoleBinding 보다 먼저 적용된다
        names = [(d["kind"], d["metadata"]["name"]) for d in docs]
        assert names.index(("Namespace", "daegam-probe")) < names.index(
            ("RoleBinding", TENANT_OBJECTS_CLUSTER_ROLE)
        )


# -- LLM 게이트웨이 네임스페이스의 Role (P38 3절) -------------------------------------------


def _gateway_rbac() -> list[dict]:
    """켠 게이트웨이의 차트 렌더에서 네임스페이스를 뺀 Role 과 RoleBinding."""
    docs = render_template(
        "llm-gateway.yaml", sets={"llmGateway.mode": "agentgateway", "llmGateway.namespace": "gw-x"}
    )
    return [d for d in docs if d["kind"] != "Namespace"]


def test_the_gateway_role_is_namespaced_and_matches_what_the_sync_calls():
    """Role 은 게이트웨이 네임스페이스 하나에만 묶이고, Secret 에 list·watch 가 없으며, 동사는
    동기화가 부르는 것(`NAMESPACE_VERBS`)과 같다. 남는 동사(update)도 빠진 동사도 없다."""
    from daegam.services.llm_gateway import NAMESPACE_VERBS

    role, binding = _gateway_rbac()
    assert (role["kind"], binding["kind"]) == ("Role", "RoleBinding")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "gw-x"
    assert binding["roleRef"] == {
        "apiGroup": RBAC_API.split("/")[0],
        "kind": "Role",
        "name": role["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "daegam-platform", "namespace": "daegam-system"}
    ]
    granted: dict[tuple[str, str], set[str]] = {}
    for rule in role["rules"]:
        assert "resourceNames" not in rule
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                granted.setdefault((group, resource), set()).update(rule["verbs"])
    assert granted == {k: set(v) for k, v in NAMESPACE_VERBS.items()}
    assert not granted[("", "secrets")] & {"list", "watch", "update"}
