"""P13 F2: Agent Instance 외부 노출 렌더·검증·상태 (D27).

fake 클러스터 렌더 스냅샷(Ingress `/chat`·`/sessions` → agent-<name>:8080, owner Deployment,
NetworkPolicy podSelector 에이전트 라벨, 파드 env 5종), header 모드 422(allow_unauthenticated
클러스터에서도), oidc 모드 통과, cors_origins 검증 422, 호스트 충돌 여섯 방향(MCP↔Gateway↔Agent),
redeploy 본문 exposure, status·external_url, 삭제 정리를 든다. 기존 MCP·Gateway 노출은
test_exposure.py 가 그대로 본다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daegam.k8s.templates import (
    RenderError,
    render_agent_deployment,
    render_agent_expose_docs,
    render_expose_policy,
    render_ingress,
)
from daegam.services.exposure import (
    UNAUTHENTICATED_WARNING,
    AgentExposureInput,
    ExposureError,
    ExposureInput,
    agent_exposure_from,
)
from daegam.store.clusters import ClusterIngress
from daegam.store.exposure import AgentExposure, Exposure, normalize_cors_origin
from tests.helpers.agent_runtime import agent_deployment, env_map, minimal_agent_doc
from tests.helpers.agents import (
    TOKEN_URL,
    agent_body,
    agent_env,
    deploy_agent,
    seed_tool_gateway,
    template_for,
)
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import ACME_NS, AG, DEFAULT_NS, NP_API, OIDC_ISSUER
from tests.helpers.data_services import created_database
from tests.helpers.docs import effective_config
from tests.helpers.exposure import INGRESS, cluster_secret, ingress_class
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import deployed, detail_code

OAUTH = {
    "client_id": "daegam-agent",
    "secret_name": "agent-oauth",
    "token_url": TOKEN_URL,
    "scope": "mcp",
}


# -- 단위: 모델·origin 정규화·렌더 ------------------------------------------------------------


def test_agent_exposure_models_and_cors_normalization():
    assert AgentExposure().external_url is None
    e = AgentExposure(enabled=True, host="bot.acme.example", ingress_class="traefik")
    assert e.external_url == "http://bot.acme.example"
    assert e.cors_origins == [] and e.allowed_groups == []
    e = AgentExposure(
        enabled=True, host="bot.acme.example", tls={"mode": "secret", "secret_name": "s"}
    )
    assert e.external_url == "https://bot.acme.example"
    # 공용 모델(MCP·Gateway 본문)에는 에이전트 전용 필드가 새지 않는다
    with pytest.raises(ValidationError):
        Exposure(enabled=True, host="h", cors_origins=["https://a.io"])
    with pytest.raises(ValidationError):
        ExposureInput(enabled=True, cors_origins=["https://a.io"])
    assert AgentExposureInput(enabled=True, cors_origins=["https://a.io"]).cors_origins == [
        "https://a.io"
    ]
    # origin: 소문자 정규화, 경로·다른 scheme 은 거부
    assert normalize_cors_origin(" HTTPS://App.Example.com:8443 ") == "https://app.example.com:8443"
    for bad in ("app.example.com", "https://app.example.com/", "https://a.io/chat", "ftp://x", ""):
        with pytest.raises(ValueError):
            normalize_cors_origin(bad)
    snap = Exposure(enabled=True, host="h.local", ingress_class="traefik")
    out = agent_exposure_from(
        snap,
        AgentExposureInput(
            enabled=True,
            cors_origins=["HTTPS://A.io", "https://a.io"],
            allowed_groups=[" g1 ", "", "g1", "g2"],
        ),
    )
    assert isinstance(out, AgentExposure) and out.host == "h.local"
    assert out.cors_origins == ["https://a.io"] and out.allowed_groups == ["g1", "g2"]
    with pytest.raises(ExposureError) as err:
        agent_exposure_from(snap, AgentExposureInput(enabled=True, cors_origins=["a.io"]))
    assert err.value.status == 422 and err.value.code == "invalid_cors_origin"
    # 껐으면 둘 다 비어 있다
    assert (
        agent_exposure_from(Exposure(), AgentExposureInput(cors_origins=["x"])) == AgentExposure()
    )


def test_render_ingress_paths_and_expose_policy_pod_selector():
    common = dict(tenant="t", instance_id="i", resource="r", namespace="ns")
    ing = render_ingress(
        **common,
        name="agent-bot",
        host="h",
        ingress_class="traefik",
        service_name="agent-bot",
        port=8080,
        paths=("/chat", "/sessions"),
    )
    paths = ing["spec"]["rules"][0]["http"]["paths"]
    assert [p["path"] for p in paths] == ["/chat", "/sessions"]
    assert {p["pathType"] for p in paths} == {"Prefix"}
    assert paths[0]["backend"] == paths[1]["backend"]
    selector = {"daegam.devops.ai.kr/resource": "agent-instance", "daegam.devops.ai.kr/id": "i"}
    np = render_expose_policy(
        **common,
        name="daegam-expose-agent-bot",
        controller_namespace="traefik",
        pod_selector=selector,
    )
    assert np["spec"]["podSelector"] == {"matchLabels": selector}
    # 없으면 앞단 라벨 (기존 호출부)
    np = render_expose_policy(
        **common,
        name="p",
        fronts=[("expose-mcp-x", "mcpremoteproxy")],
        controller_namespace="traefik",
    )
    assert np["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/instance": "expose-mcp-x",
        "app.kubernetes.io/name": "mcpremoteproxy",
    }
    with pytest.raises(ValueError):
        render_expose_policy(**common, name="p", controller_namespace="traefik")


def test_render_agent_expose_docs_and_deployment_env():
    inst = minimal_agent_doc(
        exposure=AgentExposure(
            enabled=True,
            host="bot.acme.example",
            ingress_class="nginx",
            tls={"mode": "cert-manager", "issuer": "le"},
            cors_origins=["https://app.example.com"],
            allowed_groups=["g1", "g2"],
        ),
        k8s_uid="uid-7",
    )
    ingress = ClusterIngress(
        enabled=True,
        controller_namespace="ingress-nginx",
        controller_ip_blocks=["10.0.0.0/24"],
        annotations={"nginx.ingress.kubernetes.io/proxy-buffering": "off"},
    )
    ing, np = render_agent_expose_docs(inst, inst.exposure, ingress=ingress, owner_uid="uid-7")
    owner = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "agent-bot",
        "uid": "uid-7",
        "blockOwnerDeletion": False,
        "controller": False,
    }
    assert ing["metadata"]["name"] == "agent-bot" and ing["metadata"]["namespace"] == ACME_NS
    assert ing["metadata"]["ownerReferences"] == [owner]
    assert ing["metadata"]["annotations"] == {
        "nginx.ingress.kubernetes.io/proxy-buffering": "off",
        "cert-manager.io/cluster-issuer": "le",
    }
    assert ing["spec"]["tls"] == [{"hosts": ["bot.acme.example"], "secretName": "agent-bot-tls"}]
    assert ing["spec"]["rules"][0]["http"]["paths"][1]["backend"] == {
        "service": {"name": "agent-bot", "port": {"number": 8080}}
    }
    assert np["metadata"]["name"] == "daegam-expose-agent-bot"
    assert np["metadata"]["ownerReferences"] == [owner]
    assert np["spec"]["podSelector"] == {
        "matchLabels": {
            "daegam.devops.ai.kr/resource": "agent-instance",
            "daegam.devops.ai.kr/id": "ag-1",
        }
    }
    assert np["spec"]["ingress"][0]["from"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}},
        {"ipBlock": {"cidr": "10.0.0.0/24"}},
    ]
    # uid 를 모르면 owner 없이 렌더한다
    ing, np = render_agent_expose_docs(inst, inst.exposure, ingress=ingress, owner_uid=None)
    assert "ownerReferences" not in ing["metadata"] and "ownerReferences" not in np["metadata"]
    # env 5종. audience 는 데이터 플레인 것이다
    dep = render_agent_deployment(
        inst,
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        oidc_issuer="http://idp",
        oidc_audience="daegam-mcp",
        agent_client_ids=["daegam-agent", "partner"],
        client_id_claim="cid",
        groups_claim=None,
    )
    env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
    assert env["DAEGAM_OIDC_ISSUER"] == "http://idp"
    assert env["DAEGAM_OIDC_AUDIENCE"] == "daegam-mcp"
    assert env["DAEGAM_CORS_ORIGINS"] == "https://app.example.com"
    assert env["DAEGAM_ALLOWED_GROUPS"] == "g1,g2"
    assert env["DAEGAM_AGENT_CLIENT_IDS"] == "daegam-agent,partner"
    # 노출 스냅샷이 있는데 검증기 설정이 없으면(header 모드) 렌더가 막는다
    with pytest.raises(RenderError) as err:
        render_agent_deployment(
            inst,
            effective_config(),
            providers=[],
            data_services={},
            agent_oauth=None,
            client_id_claim="cid",
            groups_claim=None,
        )
    assert err.value.code == "unauthenticated_exposure"
    # 노출을 끈 인스턴스에는 env 가 없다
    dep = render_agent_deployment(
        minimal_agent_doc(),
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        oidc_issuer="http://idp",
        oidc_audience="daegam-mcp",
        client_id_claim="cid",
        groups_claim=None,
    )
    assert "DAEGAM_OIDC_ISSUER" not in env_map(dep["spec"]["template"]["spec"]["containers"][0])


# -- header 모드: Cluster allow_unauthenticated 와 무관하게 422 ----------------------------------


def test_header_mode_rejects_agent_exposure_even_when_cluster_allows_unauthenticated(
    client, fake_clusters, mongo
):
    cid, ds, gw = agent_env(client, mongo)
    # home 과 같은 allow_unauthenticated=true 클러스터. MCP·Gateway 는 경고와 함께 열리지만 Agent 는
    # 막는다
    assert client.patch(f"/clusters/{cid}", json={"ingress": INGRESS}).status_code == 200
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    ingress_class(fake, "traefik")
    p = client.post(AG, json=agent_body(template_for(gw, ds))).json()
    r = deploy_agent(client, p["id"], exposure={"enabled": True, "host": "bot.e2e.local"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "code": "unauthenticated_exposure",
        "detail": "Agent exposure requires oidc or local auth mode",
    }
    # 정의는 이미 저장돼 있으므로 draft 로 남고 배포된 것만 없다 (D42)
    assert deployed(client, AG) == []
    assert fake.get(NP_API, "Ingress", "agent-support", DEFAULT_NS) is None
    # 껐으면 header 모드에서도 된다. 문서·상태의 노출 필드는 비어 있고 uid 가 기록된다
    r = deploy_agent(client, p["id"], exposure={"enabled": False, "cors_origins": ["https://a.io"]})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["exposure"] == {
        "enabled": False,
        "host": None,
        "ingress_class": None,
        "tls": {"mode": "none", "secret_name": None, "issuer": None},
        "cors_origins": [],
        "allowed_groups": [],
    }
    assert inst["status"]["external_url"] is None and inst["status"]["exposure"] is None
    assert inst["k8s_uid"] == agent_deployment(fake)["metadata"]["uid"]
    assert UNAUTHENTICATED_WARNING not in inst["warnings"]
    # redeploy 본문으로 켜는 것도 같은 422 이고 스냅샷은 그대로다
    r = client.post(
        f"{AG}/{inst['id']}/redeploy", json={"exposure": {"enabled": True, "host": "bot.e2e.local"}}
    )
    assert detail_code(r) == (422, "unauthenticated_exposure")
    assert client.get(f"{AG}/{inst['id']}").json()["exposure"]["enabled"] is False
    # 본문 없는 redeploy 와 빈 본문은 그대로 된다. 모르는 키는 422
    assert client.post(f"{AG}/{inst['id']}/redeploy").status_code == 200
    assert client.post(f"{AG}/{inst['id']}/redeploy", json={}).status_code == 200
    assert client.post(f"{AG}/{inst['id']}/redeploy", json={"nope": 1}).status_code == 422
    # 노출 없는 삭제도 Ingress·노출 정책 이름을 지나가며 실패하지 않는다
    assert client.delete(f"{AG}/{inst['id']}").status_code == 200
    assert agent_deployment(fake) is None


# -- oidc 모드 --------------------------------------------------------------------------------


def _oidc_env(oidc_client, h, mongo, fake_clusters):
    """oidc 모드 테넌트 acme: 노출 허용 클러스터(allow_unauthenticated=false) + stub provider +
    Database + Gateway Instance 문서(도구 소스) + agent_oauth·agent_client_ids + Agent Profile.
    h 는 플랫폼 관리자 헤더(oidc_headers)다. (admin 헤더, fake, cluster id, profile)."""
    fake = fake_clusters.by_name.setdefault("edge", FakeClusterClient())
    ingress_class(fake, "traefik")
    cluster_secret(fake, ACME_NS, "site-tls", tls=True)
    cid = default_cluster(
        oidc_client, headers=h, ingress={**INGRESS, "allow_unauthenticated": False}
    )
    r = oidc_client.patch(
        "/settings", json={"llm_providers": [{"name": "local-stub", "kind": "stub"}]}, headers=h
    )
    assert r.status_code == 200, r.text
    r = oidc_client.patch(
        "/tenants/acme/settings",
        json={"agent_oauth": OAUTH, "agent_client_ids": ["daegam-agent", "partner-app"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    ds = created_database(oidc_client, headers=h)
    gw = seed_tool_gateway(mongo, tenant="acme", cluster_id=cid, namespace=ACME_NS)
    spec = template_for(gw, ds)
    # 배포는 이 정의를 복제해 이름을 붙인다. 밑틀 이름을 "bot" 으로 두면 첫 복제부터 부딪힌다 (D42)
    r = oidc_client.post(AG, json=agent_body(spec, "base"), headers=h)
    assert r.status_code == 201, r.text
    return h, fake, cid, r.json()


def _deploy_exposed(oidc_client, h, pid: str, name: str, exposure: dict):
    """정의를 복제해 새 draft 를 만들고 그것을 노출과 함께 배포한다. 합치기 전에는 한 프로필에서
    이름만 달리해 인스턴스를 여럿 띄웠지만 이제 문서 하나는 한 번 배포된다 (D42). 옆의 Gateway
    도우미도 복제한 뒤 배포하는 같은 모양을 쓴다."""
    c = oidc_client.post(f"{AG}/{pid}/clone", json={"name": name}, headers=h)
    if c.status_code != 201:
        return c
    return oidc_client.post(f"{AG}/{c.json()['id']}/deploy", json={"exposure": exposure}, headers=h)


def test_clone_with_exposure_deploys_without_resending_it(
    oidc_client, oidc_headers, mongo, fake_clusters
):
    """복제가 받은 노출은 draft 에 남고 배포가 그것을 그대로 쓴다. 배포 본문에 exposure 를 다시
    넣지 않아도 ingress 가 선다. 노출용 둘째를 복제 한 번으로 만드는 자리다 (D42)."""
    h, fake, cid, p = _oidc_env(oidc_client, oidc_headers, mongo, fake_clusters)
    c = oidc_client.post(
        f"{AG}/{p['id']}/clone",
        json={
            "name": "bot",
            "exposure": {
                "enabled": True,
                "host": "bot.acme.example",
                "cors_origins": ["HTTPS://App.Example.com"],
                "allowed_groups": ["support-team"],
            },
        },
        headers=h,
    )
    assert c.status_code == 201, c.text
    draft = c.json()
    assert draft["state"] == "draft" and draft["exposure"]["enabled"] is True
    assert draft["exposure"]["host"] == "bot.acme.example"
    assert draft["exposure"]["cors_origins"] == ["https://app.example.com"]
    assert draft["exposure"]["allowed_groups"] == ["support-team"]
    # 배포 본문은 비어 있다. 문서에 남은 설정을 되살려 쓴다
    r = oidc_client.post(f"{AG}/{draft['id']}/deploy", json={}, headers=h)
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["exposure"]["host"] == "bot.acme.example"
    assert inst["exposure"]["ingress_class"] == "traefik"  # 클러스터 기본값은 배포 때 채운다
    ing = fake.get(NP_API, "Ingress", "agent-bot", ACME_NS)
    assert ing["spec"]["rules"][0]["host"] == "bot.acme.example"
    agent = agent_deployment(fake, "agent-bot", ACME_NS)["spec"]["template"]["spec"]["containers"][
        0
    ]
    env = env_map(agent)
    assert env["DAEGAM_CORS_ORIGINS"] == "https://app.example.com"
    assert env["DAEGAM_ALLOWED_GROUPS"] == "support-team"


def test_oidc_mode_renders_ingress_policy_env_status_redeploy_and_cleanup(
    oidc_client, oidc_headers, mongo, fake_clusters
):
    h, fake, cid, p = _oidc_env(oidc_client, oidc_headers, mongo, fake_clusters)
    r = _deploy_exposed(
        oidc_client,
        h,
        p["id"],
        "bot",
        {
            "enabled": True,
            "host": "Bot.ACME.Example",
            "tls": {"mode": "secret", "secret_name": "site-tls"},
            "cors_origins": ["HTTPS://App.Example.com", "http://localhost:5173"],
            "allowed_groups": ["support-team"],
        },
    )
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["exposure"] == {
        "enabled": True,
        "host": "bot.acme.example",
        "ingress_class": "traefik",
        "tls": {"mode": "secret", "secret_name": "site-tls", "issuer": None},
        "cors_origins": ["https://app.example.com", "http://localhost:5173"],
        "allowed_groups": ["support-team"],
    }
    # external_url 은 경로 없는 베이스 URL 이고 chat_url(플랫폼 프록시)은 그대로 둘 다 있다
    assert inst["status"]["external_url"] == "https://bot.acme.example"
    assert inst["status"]["chat_url"] == f"/api/agents/{inst['id']}/chat"
    assert inst["status"]["exposure"] == {
        "front_phase": None,
        "address_assigned": False,
        "tls_secret_present": True,
        "certificate_ready": None,
    }
    assert UNAUTHENTICATED_WARNING not in inst["warnings"]
    dep = agent_deployment(fake, "agent-bot", ACME_NS)
    assert inst["k8s_uid"] == dep["metadata"]["uid"]
    owner = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "agent-bot",
        "uid": inst["k8s_uid"],
        "blockOwnerDeletion": False,
        "controller": False,
    }
    # 파드 env: 검증기(issuer·데이터 플레인 audience)·CORS·인가 그룹·agent_client_ids
    agent = dep["spec"]["template"]["spec"]["containers"][0]
    env = env_map(agent)
    assert env["DAEGAM_OIDC_ISSUER"] == OIDC_ISSUER
    assert env["DAEGAM_OIDC_AUDIENCE"] == "daegam-mcp"  # 제어면 daegam-api 가 아니다
    assert env["DAEGAM_CORS_ORIGINS"] == "https://app.example.com,http://localhost:5173"
    assert env["DAEGAM_ALLOWED_GROUPS"] == "support-team"
    assert env["DAEGAM_AGENT_CLIENT_IDS"] == "daegam-agent,partner-app"
    assert [e["name"] for e in agent["env"]][:10] == [
        "DAEGAM_AGENT_CONFIG",
        "DAEGAM_TENANT",
        "DAEGAM_TRANSCRIPT_RETENTION_DAYS",
        "DAEGAM_INSTANCE_TOKEN",
        "DAEGAM_MCP_TOKEN",
        "DAEGAM_OIDC_ISSUER",
        "DAEGAM_OIDC_AUDIENCE",
        "DAEGAM_CORS_ORIGINS",
        "DAEGAM_ALLOWED_GROUPS",
        "DAEGAM_AGENT_CLIENT_IDS",
    ]
    # Ingress: /chat·/sessions·/memories 만 agent-bot:8080 으로, TLS Secret, owner Deployment
    ing = fake.get(NP_API, "Ingress", "agent-bot", ACME_NS)
    backend = {"service": {"name": "agent-bot", "port": {"number": 8080}}}
    assert ing["spec"] == {
        "ingressClassName": "traefik",
        "rules": [
            {
                "host": "bot.acme.example",
                "http": {
                    "paths": [
                        {"path": "/chat", "pathType": "Prefix", "backend": backend},
                        {"path": "/sessions", "pathType": "Prefix", "backend": backend},
                        {"path": "/memories", "pathType": "Prefix", "backend": backend},
                    ]
                },
            }
        ],
        "tls": [{"hosts": ["bot.acme.example"], "secretName": "site-tls"}],
    }
    assert ing["metadata"]["labels"] == {
        "daegam.devops.ai.kr/tenant": "acme",
        "daegam.devops.ai.kr/resource": "agent-instance-ingress",
        "daegam.devops.ai.kr/id": inst["id"],
        "app.kubernetes.io/managed-by": "daegam",
    }
    assert ing["metadata"]["annotations"] == INGRESS["annotations"]
    assert ing["metadata"]["ownerReferences"] == [owner]
    # NetworkPolicy: podSelector 는 에이전트 파드 라벨, from 은 컨트롤러 네임스페이스
    np = fake.get(NP_API, "NetworkPolicy", "daegam-expose-agent-bot", ACME_NS)
    assert np["spec"] == {
        "podSelector": {
            "matchLabels": {
                "daegam.devops.ai.kr/resource": "agent-instance",
                "daegam.devops.ai.kr/id": inst["id"],
            }
        },
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "traefik"}
                        }
                    }
                ]
            }
        ],
    }
    assert np["metadata"]["ownerReferences"] == [owner]
    assert (
        np["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "agent-instance-expose-policy"
    )
    assert fake.get(NP_API, "NetworkPolicy", "daegam-agent-bot", ACME_NS) is not None
    # 적용 순서: Deployment → Service → 인스턴스 정책 → Ingress → 노출 정책
    applied = [(o["kind"], o["metadata"]["name"]) for o in fake.applied]
    assert applied[-5:] == [
        ("Deployment", "agent-bot"),
        ("Service", "agent-bot"),
        ("NetworkPolicy", "daegam-agent-bot"),
        ("Ingress", "agent-bot"),
        ("NetworkPolicy", "daegam-expose-agent-bot"),
    ]
    doc = mongo["agents"].find_one({"_id": inst["id"]})
    assert doc["exposure"]["cors_origins"] == ["https://app.example.com", "http://localhost:5173"]
    assert doc["k8s_uid"] == inst["k8s_uid"]
    # 상태: LB 주소가 잡히면 address_assigned. 목록도 같다
    fake.set_status(
        NP_API, "Ingress", "agent-bot", ACME_NS, {"loadBalancer": {"ingress": [{"ip": "10.0.0.9"}]}}
    )
    st = oidc_client.get(f"{AG}/{inst['id']}", headers=h).json()["status"]
    assert st["exposure"]["address_assigned"] is True
    assert st["external_url"] == "https://bot.acme.example"
    listed = {a["id"]: a for a in oidc_client.get(AG, headers=h).json()}
    assert listed[inst["id"]]["status"]["external_url"] == "https://bot.acme.example"
    # 문서 묶음에 노출 객체가 잠긴 채 들어간다
    docs = oidc_client.get(f"{AG}/{inst['id']}/manifest", headers=h).json()["documents"]
    assert [d["kind"] for d in docs] == [
        "Deployment",
        "Service",
        "NetworkPolicy",
        "Ingress",
        "NetworkPolicy",
    ]
    assert docs[3]["name"] == "agent-bot" and docs[3]["locked"] == ["*"] and docs[3]["live"]
    assert docs[4]["name"] == "daegam-expose-agent-bot" and docs[4]["locked"] == ["*"]
    # 검증기 env 는 Advanced 로 못 바꾼다
    r = oidc_client.put(
        f"{AG}/{inst['id']}/overrides",
        headers=h,
        json={
            "patch": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "agent",
                                    "env": [{"name": "DAEGAM_ALLOWED_GROUPS", "value": ""}],
                                }
                            ]
                        }
                    }
                }
            }
        },
    )
    assert detail_code(r) == (422, "locked_field")

    # redeploy 본문 exposure: origin·그룹·TLS 를 바꾼다. 자기 host 는 충돌이 아니다
    r = oidc_client.post(
        f"{AG}/{inst['id']}/redeploy",
        headers=h,
        json={
            "exposure": {
                "enabled": True,
                "host": "bot.acme.example",
                "cors_origins": ["https://other.example"],
                "allowed_groups": [],
            }
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["exposure"]["cors_origins"] == ["https://other.example"]
    assert out["exposure"]["allowed_groups"] == [] and out["exposure"]["tls"]["mode"] == "none"
    assert out["status"]["external_url"] == "http://bot.acme.example"
    env = env_map(
        agent_deployment(fake, "agent-bot", ACME_NS)["spec"]["template"]["spec"]["containers"][0]
    )
    assert env["DAEGAM_CORS_ORIGINS"] == "https://other.example"
    assert env["DAEGAM_ALLOWED_GROUPS"] == ""
    assert "tls" not in fake.get(NP_API, "Ingress", "agent-bot", ACME_NS)["spec"]
    # 본문 없는 redeploy 는 스냅샷을 유지한다
    r = oidc_client.post(f"{AG}/{inst['id']}/redeploy", headers=h)
    assert r.status_code == 200 and r.json()["exposure"]["cors_origins"] == [
        "https://other.example"
    ]
    assert fake.get(NP_API, "Ingress", "agent-bot", ACME_NS) is not None
    # cors_origins 검증 422 는 redeploy 도 같고 실패하면 스냅샷이 그대로다
    r = oidc_client.post(
        f"{AG}/{inst['id']}/redeploy",
        headers=h,
        json={"exposure": {"enabled": True, "cors_origins": ["app.example.com"]}},
    )
    assert detail_code(r) == (422, "invalid_cors_origin")
    got = oidc_client.get(f"{AG}/{inst['id']}", headers=h).json()
    assert (
        got["exposure"]["cors_origins"] == ["https://other.example"] and got["state"] == "deployed"
    )
    # 끄면 Ingress·노출 정책이 사라지고 env 도 빠진다. 인스턴스 정책은 남는다
    r = oidc_client.post(
        f"{AG}/{inst['id']}/redeploy", headers=h, json={"exposure": {"enabled": False}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"]["external_url"] is None and r.json()["status"]["exposure"] is None
    assert fake.get(NP_API, "Ingress", "agent-bot", ACME_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-expose-agent-bot", ACME_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-agent-bot", ACME_NS) is not None
    env = env_map(
        agent_deployment(fake, "agent-bot", ACME_NS)["spec"]["template"]["spec"]["containers"][0]
    )
    assert "DAEGAM_OIDC_ISSUER" not in env and "DAEGAM_CORS_ORIGINS" not in env
    docs = oidc_client.get(f"{AG}/{inst['id']}/manifest", headers=h).json()["documents"]
    assert [d["kind"] for d in docs] == ["Deployment", "Service", "NetworkPolicy"]
    # 다시 켜고 삭제하면 노출 객체까지 정리되고 host 가 풀린다
    r = oidc_client.post(
        f"{AG}/{inst['id']}/redeploy",
        headers=h,
        json={"exposure": {"enabled": True, "host": "bot.acme.example"}},
    )
    assert r.status_code == 200 and fake.get(NP_API, "Ingress", "agent-bot", ACME_NS) is not None
    r = oidc_client.delete(f"{AG}/{inst['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert fake.get(NP_API, "Ingress", "agent-bot", ACME_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-expose-agent-bot", ACME_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-agent-bot", ACME_NS) is None
    assert agent_deployment(fake, "agent-bot", ACME_NS) is None
    r = _deploy_exposed(
        oidc_client, h, p["id"], "bot2", {"enabled": True, "host": "bot.acme.example"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"]["external_url"] == "http://bot.acme.example"
