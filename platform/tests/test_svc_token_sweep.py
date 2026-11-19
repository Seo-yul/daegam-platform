"""서비스 토큰 스윕 (D41): 만료 임박·kid 불일치·없음이거나 audience 가 그 서버에 원하는 것과
다르면(P50 9절) 갈아 끼우고 소비자를 굴린다. 갈지 않은 소비자에서는 더는 쓰지 않는 토큰을 거둔다."""

from __future__ import annotations

from datetime import timedelta

from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import PlatformSigner
from daegam.k8s.templates.agent import (
    INIT_CONTAINER,
    MCP_TOKEN_TABLE_ENV,
    mcp_token_env,
    mcp_token_table,
)
from daegam.k8s.templates.svc_token import (
    expose_svc_name,
    svc_token_refs,
)
from daegam.services.svc_token_sweep import SvcTokenSweeper
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import agent_deployment, deployed_agent
from tests.helpers.agents import (
    agent_body,
    agent_env,
    deploy_spec,
    seed_tool_server,
    template_for,
    with_tool_server,
)
from tests.helpers.auth import admin_headers, bearer
from tests.helpers.consts import AG, DEFAULT_NS, GW, TENANT_HEADER
from tests.helpers.exposure import front_and_gateway
from tests.helpers.gateways import gateway_up, vmcp_of
from tests.helpers.hub import hub_env
from tests.helpers.svc_tokens import aud_of, claims_of, entry_token_secret, plant_key

FAR = timedelta(days=200)  # 1년 토큰의 만료가 6개월 안으로 들어오는 시점
REV = "daegam.devops.ai.kr/svc-token-rev"


def _sweeper(client) -> SvcTokenSweeper:
    return client.app.state.reconciler.svc_token_sweeper


def _run(client, now=None) -> dict[str, list[str]]:
    # Mongo 클라이언트는 앱의 이벤트 루프에 묶여 있어 TestClient 의 portal 로 돈다
    return client.portal.call(lambda: _sweeper(client).run(now=now))


def test_needs_renewal_rules(client):
    sweeper = _sweeper(client)
    issuer = client.app.state.internal_issuer
    now = utcnow()
    fresh = issuer.mint(sub="svc:vmcp:x", tenant="default", groups=[])
    assert sweeper.needs_renewal(fresh, now) is False
    assert sweeper.needs_renewal(fresh, now + FAR) is True
    assert sweeper.needs_renewal(None, now) and sweeper.needs_renewal("garbage", now)
    # 서명 키가 바뀌면 kid 가 달라 갈아 끼운다
    other = PlatformSigner.generate().sign(
        {"sub": "svc:vmcp:x", "exp": int(now.timestamp()) + 10**7}
    )
    assert sweeper.needs_renewal(other, now) is True
    # 발급자 주소가 바뀌면(DAEGAM_PLATFORM_ISSUER) 옛 iss 의 토큰은 MCPOIDCConfig 와 어긋나
    # 401 이다.
    # kid 도 같고 만료도 멀지만 갈아 끼운다
    moved = issuer.signer.sign({**claims_of(fresh), "iss": "https://old.example"})
    assert sweeper.needs_renewal(moved, now) is True
    # 시각이 aware 여도 같은 판단이다 (KST 개발 기계에서 9시간 어긋나지 않는다)
    from datetime import UTC

    aware = now.replace(tzinfo=UTC)
    assert sweeper.needs_renewal(fresh, aware) is False
    assert sweeper.needs_renewal(fresh, aware + FAR) is True


def test_sweep_does_not_rotate_an_agent_whose_stored_overrides_are_refused(
    client, fake_clusters, mongo
):
    """Agent 도 같다. 롤아웃을 모는 서비스 토큰 판 어노테이션을 고정한 옛 overrides 는 이제 잠긴
    경로라 적용되지 않고, 인스턴스 토큰 Secret 도 그대로 남는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    pinned = {REV: "pinned"}
    stale = {"spec": {"template": {"metadata": {"annotations": pinned}}}}
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"overrides": stale}})
    secret = fake.get("v1", "Secret", agent.doc["k8s"]["token_secret"], DEFAULT_NS)
    dep = agent_deployment(fake)
    # 이 환경의 도구 소스 Gateway 는 백엔드 없이 심은 문서라 갈 토큰이 없다
    assert _run(client, now=utcnow() + FAR) == {"renewed": [], "failed": [agent.id]}
    assert fake.get("v1", "Secret", agent.doc["k8s"]["token_secret"], DEFAULT_NS) == secret
    assert agent_deployment(fake) == dep


def test_a_token_without_its_server_audience_is_renewed(client, fake_clusters, mongo):
    """노출 앞단과 Entry 의 토큰이 그 서버의 audience 를 싣지 않으면 만료가 멀어도 새로 낸다. 새
    토큰은 테넌트 audience 와 그 서버의 audience 를 함께 싣는다."""
    front, gateway, fake = front_and_gateway(client, fake_clusters)
    tenant_only = client.app.state.internal_issuer.mint(sub="svc:x", tenant="default", groups=[])
    secrets = (expose_svc_name(front["k8s"]["name"]), entry_token_secret(gateway))
    for name in secrets:
        plant_key(fake, name, "token", tenant_only)
    assert sorted(_run(client)["renewed"]) == sorted([front["id"], gateway["id"]])
    wanted = ["daegam-internal:default", f"daegam-internal:default:{front['id']}"]
    for name in secrets:
        assert aud_of(fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]["token"]) == wanted
    # 새 토큰의 판이 노출 앞단(vMCP, P54 5절)과 Gateway vMCP 의 파드 템플릿에 실려 둘 다 굴러간다
    front_vmcp = vmcp_of(fake, f"front-{front['k8s']['name']}")
    consumers = (
        (front_vmcp, "mcp_servers", front),
        (vmcp_of(fake, gateway["k8s"]["name"]), "gateways", gateway),
    )
    for name, (live, collection, doc) in zip(secrets, consumers, strict=True):
        token = fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]["token"]
        rev = live["spec"]["podTemplateSpec"]["metadata"]["annotations"][REV]
        assert rev == claims_of(token)["jti"][:8]
        assert mongo[collection].find_one({"_id": doc["id"]})["svc_token_rev"] == rev
    assert _run(client) == {"renewed": [], "failed": []}


def _vmcp_deployment(fake, name: str, refs: list[str], *, rolled: bool) -> None:
    """오퍼레이터가 만드는 vMCP Deployment 자리. refs 의 Secret 을 secretKeyRef env 로 싣는다.
    rolled 가 아니면 옛 ReplicaSet 의 파드가 하나 남아 있다."""
    env = [
        {"name": f"TOKEN_{i}", "valueFrom": {"secretKeyRef": {"name": ref, "key": "authorization"}}}
        for i, ref in enumerate(refs)
    ]
    template = {"spec": {"containers": [{"name": "vmcp", "env": env}]}}
    meta = {"name": name, "namespace": DEFAULT_NS}
    spec = {"template": template}
    fake.apply({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": spec})
    generation = fake.get("apps/v1", "Deployment", name, DEFAULT_NS)["metadata"]["generation"]
    running = 1 if rolled else 2
    status = {"replicas": running, "updatedReplicas": 1, "availableReplicas": running}
    fake.set_status(
        "apps/v1", "Deployment", name, DEFAULT_NS, {"observedGeneration": generation, **status}
    )


def test_dropped_entry_token_objects_go_once_the_vmcp_has_rolled(client, fake_clusters, mongo):
    """재배포가 뺀 Entry 의 토큰 객체는 렌더가 지우지 않는다. 옛 vMCP 파드가 그 Secret 을
    secretKeyRef env 로 싣고 있을 수 있기 때문이다. 스윕은 vMCP Deployment 가 다 굴러갔고 그
    템플릿이 가리키지 않는 것만 지운다."""
    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="gw-a")
    name = inst["k8s"]["name"]
    kept, dropped = entry_token_secret(inst, 0), entry_token_secret(inst, 1)
    first = inst["deployed_backends"][0]
    body = {"name": "gw-a", "backends": [{"hub_item_id": first["hub_item_id"]}]}
    assert client.put(f"{GW}/{inst['id']}", json=body).status_code == 200
    assert client.post(f"{GW}/{inst['id']}/redeploy").status_code == 200

    def present() -> set[str]:
        return {
            n
            for n in (kept, dropped)
            for api, kind, ref in svc_token_refs(n)
            if fake.get(api, kind, ref, DEFAULT_NS) is not None
        }

    assert present() == {kept, dropped}
    # vMCP Deployment 가 아직 없거나 굴러가는 중이면 손대지 않는다
    assert "settled" not in _run(client)
    _vmcp_deployment(fake, name, [kept, dropped], rolled=False)
    assert "settled" not in _run(client) and present() == {kept, dropped}
    # 다 굴러갔어도 템플릿이 아직 가리키는 Secret 은 둔다
    _vmcp_deployment(fake, name, [kept, dropped], rolled=True)
    assert "settled" not in _run(client) and present() == {kept, dropped}
    _vmcp_deployment(fake, name, [kept], rolled=True)
    assert _run(client)["settled"] == [inst["id"]] and present() == {kept}
    assert _run(client) == {"renewed": [], "failed": []}


def test_agent_tables_wait_for_every_pod_of_their_templates(client, fake_clusters, mongo):
    """Agent 의 MCP Server 소스 토큰도 그 서버의 audience 가 빠지면 새로 낸다. 소스를 거듭 바꿔
    템플릿 셋(A 가 서비스 중, B 는 준비되지 못함, C)이 겹치면 문서는 세 표를, Secret 은 세 표의 키를
    다 둔다. 스윕은 모든 파드가 C 의 지문을 들 때까지 거두지 않고, 그 뒤 앞 표들과 그 표들에만
    있던 키를 거둔다. 스윕이 문서를 읽은 뒤 재배포가 끼어들어도 거두기는 클레임을 쥐며 다시 읽은
    지금 배포로 판단한다 (P50 9절, P66 F3)."""
    cid, ds, gw = agent_env(client, mongo)
    servers = [seed_tool_server(mongo, cluster_id=cid, name=n) for n in ("a", "b", "c")]
    envs = [mcp_token_env(sid) for sid in servers]
    tables = [mcp_token_table([sid]) for sid in servers]
    _p, inst = deploy_spec(client, with_tool_server(template_for(gw, ds), servers[0]))
    fake = fake_clusters.by_name["edge"]
    secret_name = inst["k8s"]["token_secret"]

    def stored() -> dict:
        return fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]

    issuer = client.app.state.internal_issuer
    tenant_only = issuer.mint(sub="svc:agent:agent-support", tenant="default", groups=[])
    plant_key(fake, secret_name, envs[0], tenant_only)
    assert _run(client)["renewed"] == [inst["id"]]
    assert aud_of(stored()[envs[0]]) == [
        "daegam-internal:default",
        f"daegam-internal:default:{servers[0]}",
    ]

    labels = agent_deployment(fake)["spec"]["selector"]["matchLabels"]

    def pod(name: str, digest: str) -> None:
        init = {"name": INIT_CONTAINER, "env": [{"name": MCP_TOKEN_TABLE_ENV, "value": digest}]}
        meta = {"name": name, "namespace": DEFAULT_NS, "labels": labels}
        spec = {"initContainers": [init]}
        fake.apply({"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": spec})

    pod("pod-a", tables[0].digest)
    for i in (1, 2):
        spec = with_tool_server(template_for(gw, ds), servers[i])
        assert client.put(f"{AG}/{inst['id']}", json=agent_body(spec)).status_code == 200
        assert client.post(f"{AG}/{inst['id']}/redeploy").status_code == 200
        pod(f"pod-{'abc'[i]}", tables[i].digest)
    saved = mongo["agents"].find_one({"_id": inst["id"]})
    assert [t["digest"] for t in saved["mcp_token_tables"]] == [t.digest for t in tables[::-1]]
    assert set(envs) <= set(stored())
    for gone in ("pod-b", "pod-a"):
        assert _run(client) == {"renewed": [], "failed": []}
        assert set(envs) <= set(stored())
        fake.delete("v1", "Pod", gone, DEFAULT_NS)
    state = client.app.state
    stale_view = client.portal.call(state.agents.get, inst["id"])
    assert client.post(f"{AG}/{inst['id']}/redeploy").status_code == 200
    assert client.portal.call(state.agent_service.prune_token_tables, stale_view, fake)
    saved = mongo["agents"].find_one({"_id": inst["id"]})
    assert saved["mcp_token_tables"] == [tables[2].model_dump()]
    assert envs[2] in stored() and not {envs[0], envs[1]} & set(stored())
    assert {"token", "mcp_token"} <= set(stored())
    assert _run(client) == {"renewed": [], "failed": []}


def _gateway_token(fake, name: str) -> tuple[str, str]:
    data = fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]
    return data["mcp_token"], data["gateway_token"]


def _stale_gateway_tokens(client) -> list[str]:
    """iss·aud·groups 가운데 하나씩만 지금 원하는 값과 다른 gateway_token 셋 (옛 설정의 흔적)."""
    issuer = client.app.state.internal_issuer
    old = InternalIssuer(
        issuer=issuer.issuer, human_issuer="https://old.example", mcp_audience=issuer.mcp_audience
    )
    old.attach(issuer.signer)
    who = {"sub": "svc:agent:agent-support", "tenant": "default"}
    return [
        issuer.mint_gateway(**who, groups=["ops"]),
        old.mint_gateway(**who, groups=["daegam-default"]),
        issuer.mint(**who, groups=["daegam-default"]),
    ]


def test_local_agent_gateway_token_follows_the_wantedclaims_of(local_client, fake_clusters, mongo):
    """local 에이전트의 gateway_token 은 iss·aud·groups 가운데 하나라도 지금 원하는 값과 다르면
    provision·인증 재적용·서비스 토큰 스윕이 한 비교로 새로 낸다. 셋이 같으면 그대로 둔다. 판이 두
    토큰의 jti 에서 나오므로 새로 내면 파드 템플릿 어노테이션이 바뀐다 (P32 8절)."""
    client = local_client
    client.headers.update({**admin_headers(client, mongo), TENANT_HEADER: "default"})
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    secret = agent.doc["k8s"]["token_secret"]
    service = client.app.state.agent_service
    mcp, gateway = _gateway_token(fake, secret)
    assert claims_of(gateway)["groups"] == ["daegam-default"]

    def rev() -> str:
        dep = agent_deployment(fake)
        return dep["spec"]["template"]["metadata"]["annotations"][REV]

    assert rev() == f"{claims_of(mcp)['jti'][:8]}-{claims_of(gateway)['jti'][:8]}"

    def reapply() -> bool:
        doc = client.portal.call(service.instances.get, agent.id)
        return client.portal.call(service.reapply_auth, doc)

    # 셋이 같으면 재적용이 토큰을 새로 내지 않는다
    assert reapply() and _gateway_token(fake, secret) == (mcp, gateway)
    stale_tokens = _stale_gateway_tokens(client)
    for stale in stale_tokens:
        plant_key(fake, secret, "gateway_token", stale)
        before = rev()
        assert reapply()
        kept, fresh = _gateway_token(fake, secret)
        assert kept == mcp and fresh != stale and claims_of(fresh)["groups"] == ["daegam-default"]
        assert rev() != before
    # 스윕도 같은 비교로 본다. 회전이라 두 토큰을 모두 새로 내고, 살아 있는 인스턴스 토큰은
    # 그대로라 굴러가는 사이에도 플랫폼이 런타임을 부르는 길이 끊기지 않는다
    instance = fake.get("v1", "Secret", secret, DEFAULT_NS)["stringData"]["token"]
    plant_key(fake, secret, "gateway_token", stale_tokens[0])
    assert _run(client)["renewed"] == [agent.id]
    assert _gateway_token(fake, secret)[0] != mcp
    assert fake.get("v1", "Secret", secret, DEFAULT_NS)["stringData"]["token"] == instance
    # provision(재배포)도 같다
    plant_key(fake, secret, "gateway_token", stale_tokens[1])
    assert client.post(f"{agent.url}/redeploy", json={}).status_code == 200
    assert _gateway_token(fake, secret)[1] != stale_tokens[1]


def test_agent_secret_holding_a_stale_instance_token_gets_a_live_one(client, fake_clusters, mongo):
    """Secret 의 인스턴스 토큰이 문서가 가리키는 살아 있는 기록과 맞지 않으면(겹친 provision 이나
    저장과 Secret 쓰기 사이에서 멈춘 배포가 남긴 것) 스윕이 회전해 새 토큰을 낸다 (P66 2.7절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    secret_name = agent.doc["k8s"]["token_secret"]
    secret = fake.get("v1", "Secret", secret_name, DEFAULT_NS)
    secret["stringData"]["token"] = "left-behind-and-revoked"
    fake.apply(secret)
    assert _run(client)["renewed"] == [agent.id]
    token = fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]["token"]
    assert token != "left-behind-and-revoked"
    assert client.get(f"{agent.url}/agent.json", headers=bearer(token)).status_code == 200
    # 맞춘 뒤에는 할 일이 없다
    assert _run(client)["renewed"] == []
