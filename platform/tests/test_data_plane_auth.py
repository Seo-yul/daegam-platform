"""인증 재적용 (P32 8절). 객체 판과 소비자 판, 실물과 맞대는 드리프트 스윕, 설치 표시와 입양,
백오프와 막힘, 발급자를 옮긴 재렌더, 요약 문서·로그 한 줄·상태 API 를 본다.

리컨실 틱은 부르지 않고 `DataPlaneAuth.run` 을 직접 부른다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import jwt

from daegam.config import Settings
from daegam.k8s.labels import LABEL_PLATFORM_ID
from daegam.k8s.templates.agent import GATEWAY_TOKEN_KEY, MCP_TOKEN_KEY
from daegam.k8s.templates.svc_token import secret_value
from daegam.services.data_plane_auth import KIND_AGENT, KIND_GATEWAY, KIND_MCP_SERVER, auth_rev
from daegam.services.tenancy import (
    INTERNAL_OIDC_CONFIG_NAME,
    OIDC_CONFIG_NAME,
    TOOLHIVE_API,
    oidc_config_manifest,
)
from daegam.store.agents import AgentRevision, AgentSpec
from daegam.store.auth_reapply import next_retry
from daegam.store.base import utcnow
from daegam.store.hub import HubItem, HubK8s
from daegam.store.settings import MongoPlatformSettingsStore
from daegam.store.tenants import TenantSettings
from tests.helpers.agent_runtime import deployed_agent, env_map, skip_start_check
from tests.helpers.agents import template_spec
from tests.helpers.consts import (
    DEFAULT_NS,
    GW,
    ISSUER,
    PLATFORM_JWKS,
    PUBLIC_URL,
)
from tests.helpers.exposure import front_and_gateway
from tests.helpers.gateways import vmcp_of
from tests.helpers.http import api_client, make_app
from tests.helpers.svc_tokens import entry_token_secret
from tests.helpers.workers import run_data_plane_auth


def _oidc_config(fake, name=INTERNAL_OIDC_CONFIG_NAME, ns=DEFAULT_NS):
    return fake.get(TOOLHIVE_API, "MCPOIDCConfig", name, ns)


def _install_id(client) -> str:
    return client.portal.call(client.app.state.platform_settings.install_id)


# -- 두 판 (순수) -----------------------------------------------------------------------------


def _doc(*, exposed=False, gateway_source=False):
    spec = AgentSpec.model_validate(template_spec(source_id="gw-1"))
    if not gateway_source:
        spec.graph.nodes = [n for n in spec.graph.nodes if n.type != "tools"]
    rev = AgentRevision(revision=1, **spec.model_dump())
    return SimpleNamespace(exposure=SimpleNamespace(enabled=exposed), deployed=rev)


def test_a_non_default_client_id_claim_reaches_only_the_parts_that_read_it():
    """oidc 의 client id 클레임은 Gateway·노출 앞단(Agent permit)과 노출 Agent(검증기)만 읽는다.
    cid 가 아니면 그 판만 바뀌고, Gateway 소스만 쓰는 Agent 와 노출하지 않은 MCP Server 는
    그대로다."""
    idp = Settings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        platform_issuer=ISSUER,
        data_plane_issuer="idp",
    )
    azp = replace(idp, oidc_client_id_claim="azp")
    ts = TenantSettings(tenant="t")
    exposed, gateway_source = _doc(exposed=True), _doc(gateway_source=True)
    front = _doc(exposed=True)
    assert auth_rev(idp, ts, KIND_GATEWAY, None) != auth_rev(azp, ts, KIND_GATEWAY, None)
    assert auth_rev(idp, ts, KIND_MCP_SERVER, front) != auth_rev(azp, ts, KIND_MCP_SERVER, front)
    assert auth_rev(idp, ts, KIND_AGENT, exposed) != auth_rev(azp, ts, KIND_AGENT, exposed)
    assert auth_rev(idp, ts, KIND_AGENT, gateway_source) == auth_rev(
        azp, ts, KIND_AGENT, gateway_source
    )
    assert auth_rev(azp, ts, KIND_MCP_SERVER, _doc()) is None


def test_consumer_rev_reacts_only_to_its_inputs():
    """각 갈래는 P32 8절 표의 입력에만 반응한다. 그룹은 유효값이고, 발급자는 Agent 에만 들며,
    소비자가 아닌 문서는 판이 없다."""
    local = Settings(auth_mode="local", platform_issuer=ISSUER)
    other_issuer = replace(local, platform_issuer="https://daegam.example")
    # https 공개 주소는 OAuth 인가 서버를 켠다. 발급자만 옮기려고 그 스위치는 끈다
    public = replace(local, public_url="https://daegam.example", oauth_switch=False)
    ts = TenantSettings(tenant="t")
    exposed, gateway_source, both = (
        _doc(exposed=True),
        _doc(gateway_source=True),
        _doc(exposed=True, gateway_source=True),
    )
    front = _doc(exposed=True)
    # 그룹은 유효값이다. 비어 있을 때와 daegam-<t> 를 적었을 때가 같다
    assert auth_rev(local, ts, KIND_GATEWAY, None) == auth_rev(
        local, ts.model_copy(update={"mcp_group": "daegam-t"}), KIND_GATEWAY, None
    )
    # 발급자는 Agent 에만 든다. ToolHive 앞단은 오퍼레이터가 MCPOIDCConfig 변경으로 굴린다. 공개
    # 주소가 바꾸는 local 의 사람 발급자도 같다
    for moved in (other_issuer, public):
        assert auth_rev(local, ts, KIND_GATEWAY, None) == auth_rev(moved, ts, KIND_GATEWAY, None)
        assert auth_rev(local, ts, KIND_MCP_SERVER, front) == auth_rev(
            moved, ts, KIND_MCP_SERVER, front
        )
        for doc in (exposed, gateway_source, both):
            assert auth_rev(local, ts, KIND_AGENT, doc) != auth_rev(moved, ts, KIND_AGENT, doc)
    # oidc 의 노출 에이전트는 플랫폼 JWKS 를 받지 않으므로 플랫폼 발급자가 판에 들지 않는다
    idp = Settings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        platform_issuer=ISSUER,
        data_plane_issuer="idp",
    )
    moved = replace(idp, platform_issuer="https://daegam.example")
    for doc in (exposed, both):
        assert auth_rev(idp, ts, KIND_AGENT, doc) == auth_rev(moved, ts, KIND_AGENT, doc)
    # OAuth 의 scope 는 ToolHive 앞단의 참조에만 있어 Gateway·노출 MCP Server 의 판에만 든다
    oauth = replace(public, oauth_switch=True)
    assert auth_rev(public, ts, KIND_GATEWAY, None) != auth_rev(oauth, ts, KIND_GATEWAY, None)
    assert auth_rev(public, ts, KIND_MCP_SERVER, front) != auth_rev(
        oauth, ts, KIND_MCP_SERVER, front
    )
    for doc in (exposed, gateway_source, both):
        assert auth_rev(public, ts, KIND_AGENT, doc) == auth_rev(oauth, ts, KIND_AGENT, doc)
    # agent_client_ids 는 Gateway 소스만 쓰는 Agent 에 들지 않는다
    ids = ts.model_copy(update={"agent_client_ids": ["cid"]})
    assert auth_rev(local, ts, KIND_GATEWAY, None) != auth_rev(local, ids, KIND_GATEWAY, None)
    assert auth_rev(local, ts, KIND_AGENT, exposed) != auth_rev(local, ids, KIND_AGENT, exposed)
    assert auth_rev(local, ts, KIND_AGENT, gateway_source) == auth_rev(
        local, ids, KIND_AGENT, gateway_source
    )
    # 인증 모드는 모든 갈래에 든다
    header = Settings(auth_mode="header")
    assert auth_rev(local, ts, KIND_GATEWAY, None) != auth_rev(header, ts, KIND_GATEWAY, None)
    # 두 갈래에 드는 Agent 는 합집합이다. Gateway 소스의 입력이 노출의 입력 안에 있어 노출과 같다
    assert auth_rev(local, ts, KIND_AGENT, both) == auth_rev(local, ts, KIND_AGENT, exposed)
    assert auth_rev(local, ts, KIND_AGENT, both) != auth_rev(local, ts, KIND_AGENT, gateway_source)
    # 노출하지 않은 MCP Server, 노출도 Gateway 소스도 없는 Agent 는 소비자가 아니다
    assert auth_rev(local, ts, KIND_MCP_SERVER, _doc()) is None
    assert auth_rev(local, ts, KIND_AGENT, _doc()) is None
    assert len(auth_rev(local, ts, KIND_GATEWAY, None)) == 12


def test_backoff_doubles_from_five_minutes_up_to_an_hour():
    now = utcnow()
    assert [next_retry(now, n)[0] - now for n in (0, 1, 2, 3, 4, 40)] == [
        timedelta(minutes=m) for m in (5, 10, 20, 40, 60, 60)
    ]
    assert next_retry(now, 3)[1] == 4


# -- 드리프트 스윕과 설치 표시 ------------------------------------------------------------------


def _plant(fake, tenant: str, ns: str, install_id: str | None) -> dict:
    manifest = oidc_config_manifest(
        tenant, ns, "http://elsewhere", None, name=INTERNAL_OIDC_CONFIG_NAME, install_id=install_id
    )
    fake.apply(manifest)
    return _oidc_config(fake, ns=ns)


def test_only_this_installs_objects_are_rewritten(client, fake_clusters, mongo):
    """다른 설치의 라벨은 건드리지 않는다. 라벨 없는 객체는 이 설치의 문서(허브 항목 포함)나 Tenant
    Settings 가 가리키는 네임스페이스에서만 입양하고 나머지는 unadopted 로 센다. 드리프트는 적어
    둔 값이 아니라 실물과 맞댄다. 그래서 입양한 객체가 어노테이션도 jwksUrl 도 없는 옛 렌더여도
    지금의 렌더로 고치고, 객체가 없어진 소비자 네임스페이스도 다시 보장한다."""
    _front, _gateway, fake = front_and_gateway(client, fake_clusters)
    install = _install_id(client)
    foreign = _plant(fake, "x", "daegam-x", "another-install")
    stray = _plant(fake, "y", "daegam-y", None)
    _plant(fake, "z", "daegam-z", None)
    _plant(fake, "h", "hub-only", None)
    assert client.patch("/tenants/z/settings", json={"agent_client_ids": []}).status_code == 200
    cid = mongo["clusters"].find_one({"name": "edge"})["_id"]
    item = HubItem(
        tenant="h",
        name="img",
        kind="image",
        cluster_id=cid,
        k8s=HubK8s(kind="MCPServer", name="img", namespace="hub-only"),
    )
    mongo["hub_items"].insert_one(item.to_doc())
    result = run_data_plane_auth(client)
    assert sorted(result["ensured"]) == ["daegam-z", "hub-only"]
    for ns in ("hub-only", "daegam-z"):
        assert _oidc_config(fake, ns=ns)["metadata"]["labels"][LABEL_PLATFORM_ID] == install
        jwks_url = _oidc_config(fake, ns=ns)["spec"]["inline"]["jwksUrl"]
        assert jwks_url.endswith("/jwks.json?use=service")
    assert _oidc_config(fake, ns="daegam-x") == foreign
    assert _oidc_config(fake, ns="daegam-y") == stray
    state = mongo["auth_reapply"].find_one({"_id": f"cluster:{cid}"})
    assert state["unadopted"] == ["daegam-x", "daegam-y"] and state["last_error"] is None
    fake.delete(TOOLHIVE_API, "MCPOIDCConfig", INTERNAL_OIDC_CONFIG_NAME, DEFAULT_NS)
    assert run_data_plane_auth(client)["ensured"] == [DEFAULT_NS] and _oidc_config(fake) is not None


def test_install_id_is_one_when_replicas_start_together(client):
    db = client.app.state.platform_settings.coll.database

    async def both() -> list[str]:
        stores = [MongoPlatformSettingsStore(db) for _ in range(4)]
        return list(await asyncio.gather(*(s.install_id() for s in stores)))

    ids = client.portal.call(both)
    assert len(set(ids)) == 1 and ids[0] == _install_id(client)


# -- 소비자 재적용 ---------------------------------------------------------------------------


def test_public_url_moves_the_human_issuer_and_keeps_service_tokens(
    client, fake_clusters, fake_secrets, local_settings, local_app, mongo
):
    """공개 주소가 local 의 사람 발급자가 되면 재적용이 daegam-oidc 의 issuer 를 바꾸고(jwksUrl 은
    클러스터 안 그대로) 노출·Gateway 소스 Agent 를 새 iss 의 gateway_token 과 env 로 다시 렌더한다.
    서비스 토큰과 daegam-internal-oidc 는 서비스 발급자 그대로다 (P32 8절 "발급자를 바꿀 때")."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    mongo["gateways"].delete_one({"_id": agent.gw["id"]})  # effective_config 가 빈 가짜 문서라 뺀다
    mongo["agents"].update_one(
        {"_id": agent.id}, {"$set": {"exposure.enabled": True, "exposure.host": "a.e2e.local"}}
    )
    fake, k8s = agent.fake, agent.doc["k8s"]
    ns, jwks = k8s["namespace"], PLATFORM_JWKS

    def tokens() -> tuple[str | None, str | None]:
        secret = fake.get("v1", "Secret", k8s["token_secret"], ns)
        return secret_value(secret, MCP_TOKEN_KEY), secret_value(secret, GATEWAY_TOKEN_KEY)

    with api_client(local_app) as local:
        skip_start_check(local.app)
        assert agent.id in run_data_plane_auth(local)["reapplied"]
    mcp, gateway = tokens()
    moved = make_app(local_settings, fake_clusters, fake_secrets, public_url=PUBLIC_URL)
    with api_client(moved) as p:
        skip_start_check(p.app)
        assert run_data_plane_auth(p) == {
            "ensured": [ns],
            "reapplied": [agent.id],
            "failed": [],
            "blocked": [],
        }
        assert run_data_plane_auth(p)["reapplied"] == []
    human = _oidc_config(fake, OIDC_CONFIG_NAME, ns)["spec"]["inline"]
    assert (human["issuer"], human["jwksUrl"]) == (PUBLIC_URL, jwks)
    assert _oidc_config(fake, ns=ns)["spec"]["inline"]["issuer"] == ISSUER
    kept, fresh = tokens()
    assert kept == mcp and fresh != gateway and fresh is not None
    assert jwt.decode(fresh, options={"verify_signature": False})["iss"] == PUBLIC_URL
    dep = fake.get("apps/v1", "Deployment", k8s["name"], ns)
    env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
    assert (env["DAEGAM_OIDC_ISSUER"], env["DAEGAM_OIDC_JWKS_URL"]) == (PUBLIC_URL, jwks)
    assert env["DAEGAM_OIDC_INSECURE_HTTP"] == "true"  # JWKS 가 클러스터 안 평문 주소다


def test_consumers_rerender_for_their_own_inputs_and_wait_out_their_clusters_backoff(
    client, fake_clusters, mongo, local_app
):
    """header 에서 local 로, 다시 header 로 바꾸면 vMCP·노출 앞단의 incomingAuth 가 anonymous 와
    oidcConfigRef 사이를 오가고, 사람 토큰용 MCPOIDCConfig 가 먼저 생긴다. 다른 테넌트의 설정
    변경은 이 테넌트의 소비자를 다시 렌더하지 않는다. 드리프트를 못 고친 클러스터의 소비자는 고르지
    않고, 연결할 수 없는 클러스터는 5분부터 물러난다. 재적용은 서비스 토큰을 새로 내지 않는다."""
    front, gateway, fake = front_and_gateway(client, fake_clusters)
    secret = entry_token_secret(gateway)
    token = fake.get("v1", "Secret", secret, DEFAULT_NS)["stringData"]["token"]
    vmcp, front_vmcp = gateway["k8s"]["name"], f"front-{front['k8s']['name']}"
    both = sorted([front["id"], gateway["id"]])
    with api_client(local_app) as local:
        result = run_data_plane_auth(local)
        assert result["ensured"] == [DEFAULT_NS] and sorted(result["reapplied"]) == both
        assert _oidc_config(fake, OIDC_CONFIG_NAME)["spec"]["inline"]["issuer"] == ISSUER
        assert "oidcConfigRef" in vmcp_of(fake, vmcp)["spec"]["incomingAuth"]
        assert "oidcConfigRef" in vmcp_of(fake, front_vmcp)["spec"]["incomingAuth"]
        assert run_data_plane_auth(local)["reapplied"] == []
    assert sorted(run_data_plane_auth(client)["reapplied"]) == both
    assert vmcp_of(fake, vmcp)["spec"]["incomingAuth"]["type"] == "anonymous"
    assert vmcp_of(fake, front_vmcp)["spec"]["incomingAuth"]["type"] == "anonymous"
    # 다른 테넌트의 설정 변경은 이 테넌트의 소비자를 다시 렌더하지 않는다
    assert (
        client.patch("/tenants/other/settings", json={"agent_client_ids": ["x"]}).status_code == 200
    )
    assert run_data_plane_auth(client)["reapplied"] == []
    assert (
        client.patch("/tenants/default/settings", json={"agent_client_ids": ["x"]}).status_code
        == 200
    )
    fake.unreachable = True
    now = utcnow()
    result = run_data_plane_auth(client, now=now)
    cid = gateway["cluster_id"]
    assert result["failed"] == [f"cluster:{cid}"] and result["reapplied"] == []
    state = mongo["auth_reapply"].find_one({"_id": f"cluster:{cid}"})
    assert state["failures"] == 1 and state["last_error"]
    summary = mongo["auth_reapply"].find_one({"_id": "summary"})
    assert summary["pending"] == 3 and summary["failed"] == 1  # 클러스터 하나 + 소비자 둘
    fake.unreachable = False
    assert (
        run_data_plane_auth(client, now=now + timedelta(minutes=4))["reapplied"] == []
    )  # 아직 백오프 중
    assert len(run_data_plane_auth(client, now=now + timedelta(minutes=6))["reapplied"]) == 2
    summary = mongo["auth_reapply"].find_one({"_id": "summary"})
    assert (summary["pending"], summary["failed"]) == (0, 0)
    # 재적용은 서비스 토큰을 새로 내지 않는다
    assert fake.get("v1", "Secret", secret, DEFAULT_NS)["stringData"]["token"] == token


def test_status_api_log_line_and_blocked_exposed_agent(client, fake_clusters, mongo, caplog):
    """header 모드로 바뀐 배포본의 노출 Agent 는 재적용이 422 unauthenticated_exposure 로 막힌다.
    막힌 재적용은 토큰 Secret 과 토큰 저장소를 건드리지 않는다. 상태 API 는 요약, 클러스터 상태,
    막힌 소비자, 옛 런타임의 노출 Agent 수를 보인다. 로그 한 줄은 첫 틱, 무언가를 처리한 틱, 숫자가
    바뀐 틱에만 남는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    mongo["gateways"].delete_one({"_id": agent.gw["id"]})  # effective_config 가 빈 가짜 문서라 뺀다
    # 다른 모드에서 노출해 배포된 것처럼 둔다
    mongo["agents"].update_one(
        {"_id": agent.id}, {"$set": {"exposure.enabled": True, "exposure.host": "a.e2e.local"}}
    )
    fake = agent.fake
    secret = agent.doc["k8s"]["token_secret"]

    def secret_writes() -> int:
        return sum(o["kind"] == "Secret" and o["metadata"]["name"] == secret for o in fake.applied)

    writes, tokens = secret_writes(), list(mongo["agent_instance_tokens"].find())
    caplog.set_level(logging.INFO, logger="daegam.data_plane_auth")
    assert run_data_plane_auth(client)["blocked"] == [agent.id]
    assert secret_writes() == writes
    assert list(mongo["agent_instance_tokens"].find()) == tokens
    assert any(
        "reapply_auth reapplied=0 failed=0 blocked=1 pending=0" in m for m in caplog.messages
    )
    caplog.clear()
    assert run_data_plane_auth(client) == {
        "ensured": [],
        "reapplied": [],
        "failed": [],
        "blocked": [],
    }
    assert not [m for m in caplog.messages if m.startswith("reapply_auth reapplied")]
    # 에이전트 이미지를 바꾸면 이미 뜬 노출 Agent 는 옛 런타임이다 (재적용은 이미지를 바꾸지 않는다)
    assert client.patch("/settings", json={"default_agent_image": "agent:new"}).status_code == 200
    body = client.get("/settings/data-plane-auth").json()
    assert body["install_id"] == _install_id(client)
    assert set(body["summary"]) == {"pending", "failed", "blocked", "tick_at"}
    assert body["summary"]["blocked"] == 1 and body["summary"]["pending"] == 0
    assert body["blocked"] == [
        {
            "kind": "agent",
            "id": agent.id,
            "tenant": "default",
            "cluster_id": agent.cluster_id,
            "code": "unauthenticated_exposure",
            "detail": "Agent exposure requires oidc mode",
        }
    ]
    assert body["clusters"][0]["drifted"] == [] and body["clusters"][0]["retry_at"] is None
    assert body["exposed_agents_stale_runtime"] == 1


def test_a_locked_override_blocks_the_reapply_until_a_save_and_a_late_block_does_not_hold(
    client, fake_clusters, mongo
):
    """저장된 overrides 가 지금 잠긴 경로를 건드리면 재적용은 `locked_field` 로 막히고 vMCP 는
    그대로 남는다. 막힘은 기대 판이 바뀔 때만 풀리고, 사용자가 고쳐 저장하면 그 저장이 막힘을
    비운다. 틱이 옛 overrides 로 실패하는 사이에 저장이 끼면 그 틱의 막힘 기록이 저장 뒤에 적힌다.
    막힘은 막은 시도가 본 `last_spec_change` 와 함께 적히고 문서의 값이 그와 같을 때만 막으므로
    다음 틱이 한 번 더 시도해 판을 적는다."""
    _front, gateway, fake = front_and_gateway(client, fake_clusters)
    gid = gateway["id"]
    mongo["gateways"].update_one(
        {"_id": gid},
        {"$set": {"auth_rev": "stale", "overrides": {"spec": {"serviceType": "NodePort"}}}},
    )
    before = vmcp_of(fake, gateway["k8s"]["name"])
    assert run_data_plane_auth(client)["blocked"] == [gid]
    assert vmcp_of(fake, gateway["k8s"]["name"]) == before
    late = mongo["gateways"].find_one({"_id": gid})["auth_blocked"]
    assert late["code"] == "locked_field" and late["spec_change"] is not None
    blocked = client.get("/settings/data-plane-auth").json()["blocked"]
    assert [b["code"] for b in blocked] == ["locked_field"]
    assert run_data_plane_auth(client)["reapplied"] == []  # 판이 그대로면 막힌 채다
    r = client.put(f"{GW}/{gid}/overrides", json={"patch": {"spec": {"replicas": 2}}})
    assert r.status_code == 200, r.text
    assert mongo["gateways"].find_one({"_id": gid})["auth_blocked"] is None
    assert client.get("/settings/data-plane-auth").json()["blocked"] == []
    # 저장보다 늦게 도착한 그 틱의 막힘 기록
    mongo["gateways"].update_one({"_id": gid}, {"$set": {"auth_blocked": late}})
    assert run_data_plane_auth(client)["reapplied"] == [gid]
    stored = mongo["gateways"].find_one({"_id": gid})
    assert stored["auth_rev"] not in (None, "stale") and stored["auth_blocked"] is None
    assert vmcp_of(fake, gateway["k8s"]["name"])["spec"]["replicas"] == 2
    assert run_data_plane_auth(client)["reapplied"] == []
