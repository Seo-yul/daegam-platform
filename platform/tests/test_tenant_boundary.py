"""P32 F3: 토큰의 경계는 테넌트다 (P32 2절, D58).

local 모드에서 테넌트의 MCP 그룹은 저장값과 무관하게 daegam-<tenant> 이고(`human_mcp_group`),
`mcp_group` PATCH 는 역할과 무관하게 422 다. oidc 모드는 IdP 그룹 매핑이라 그대로 PATCH 된다. local
노출 에이전트의 빈 allowed_groups 는 그 테넌트의 그룹으로 채워 내리고 oidc 는 비워 둔다(D27). local
로 뜰 때 쓰이지 않는 저장값과 그 이름을 THVGroup 으로 적은 authz_policy 를 기동 경고가 알린다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import jwt
import pytest

from daegam.auth.cedar import group_policy
from daegam.config import Settings
from daegam.k8s.templates.agent import GATEWAY_TOKEN_KEY, exposure_env
from daegam.k8s.templates.svc_token import secret_value
from daegam.services.data_plane_auth import KIND_GATEWAY, auth_rev, consumer_auth
from daegam.store.exposure import AgentExposure
from daegam.store.tenants import TenantSettings, human_mcp_group
from tests.helpers.agent_runtime import deployed_agent, env_map, skip_start_check
from tests.helpers.auth import admin_headers, bearer, login
from tests.helpers.consts import ISSUER, OIDC_AUDIENCE, OIDC_ISSUER, PLATFORM_JWKS
from tests.helpers.docs import gateway_doc
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.oauth import make_user, password_of
from tests.helpers.svc_tokens import make_svc_tokens
from tests.helpers.workers import run_data_plane_auth

LOCAL = Settings(auth_mode="local", platform_issuer=ISSUER)
# IdP 를 믿는 oidc. 저장된 mcp_group 과 IdP 의 클레임을 읽는 것은 이때뿐이다 (P53b 6.1절)
OIDC = Settings(
    auth_mode="oidc",
    oidc_issuer=OIDC_ISSUER,
    oidc_audience=OIDC_AUDIENCE,
    data_plane_issuer="idp",
)
HEADER = Settings(auth_mode="header")
STORED = TenantSettings(tenant="b", mcp_group="team-b")


def _groups(token: str) -> list[str]:
    return jwt.decode(token, options={"verify_signature": False})["groups"]


# -- 그룹 한 벌 (순수) -----------------------------------------------------------------------


def test_local_group_is_fixed_regardless_of_the_stored_value():
    assert human_mcp_group(LOCAL, STORED) == "daegam-b"
    assert human_mcp_group(LOCAL, TenantSettings(tenant="b")) == "daegam-b"
    for settings in (OIDC, HEADER):
        assert human_mcp_group(settings, STORED) == "team-b"
        assert human_mcp_group(settings, TenantSettings(tenant="b")) == "daegam-b"
    # 렌더와 소비자 판이 같은 값을 받는다. 노출 에이전트의 빈 그룹 채우기는 local 만이다
    assert consumer_auth(LOCAL, STORED).tenant_group == "daegam-b"
    assert consumer_auth(LOCAL, STORED).exposure_default_group == "daegam-b"
    assert consumer_auth(OIDC, STORED).tenant_group == "team-b"
    assert consumer_auth(OIDC, STORED).exposure_default_group is None
    assert consumer_auth(HEADER, STORED).exposure_default_group is None


def test_service_tokens_carry_the_same_group():
    async def run():
        local = make_svc_tokens(group="team-b", settings=LOCAL)
        assert await local.groups("b") == ["daegam-b"]
        assert _groups(await local.mint_gateway(name="ai", tenant="b")) == ["daegam-b"]
        assert await make_svc_tokens(group="team-b", settings=OIDC).groups("b") == ["team-b"]

    asyncio.run(run())


def test_exposure_env_fills_only_an_empty_allowed_groups():
    def groups(exposure: AgentExposure, default_group: str | None) -> str:
        env = exposure_env(
            exposure,
            oidc_issuer=ISSUER,
            oidc_audience="daegam-mcp",
            agent_client_ids=[],
            default_group=default_group,
        )
        return next(e["value"] for e in env if e["name"] == "DAEGAM_ALLOWED_GROUPS")

    assert groups(AgentExposure(), "daegam-b") == "daegam-b"
    assert groups(AgentExposure(), None) == ""
    # 주인이 적은 그룹은 그대로다. 다른 테넌트 그룹을 적어 넓히는 것도 주인의 몫이다
    assert groups(AgentExposure(allowed_groups=["daegam-a", "ops"]), "daegam-b") == "daegam-a,ops"


def test_oidc_mode_reads_the_client_id_from_the_idp_claim():
    """Gateway 와 노출 앞단의 Agent permit, 노출 Agent 의 검증기가 플랫폼 검증기와 같은 클레임을
    읽는다. local 은 플랫폼이 내는 cid 이고, 기본값인 설치는 판도 env 도 그대로다."""
    azp = replace(OIDC, oidc_client_id_claim="azp")
    assert consumer_auth(OIDC, STORED).client_id_claim == "cid"
    assert consumer_auth(azp, STORED).client_id_claim == "azp"
    local_azp = replace(LOCAL, oidc_client_id_claim="azp")
    assert consumer_auth(local_azp, STORED).client_id_claim == "cid"
    # 기본값이면 판이 바뀌지 않고, 다른 클레임이면 그 클레임을 읽는 문서의 판만 바뀐다
    assert auth_rev(OIDC, STORED, KIND_GATEWAY, None) == auth_rev(
        replace(OIDC, oidc_client_id_claim="cid"), STORED, KIND_GATEWAY, None
    )
    assert auth_rev(OIDC, STORED, KIND_GATEWAY, None) != auth_rev(azp, STORED, KIND_GATEWAY, None)

    def env(claim: str) -> dict[str, str]:
        rendered = exposure_env(
            AgentExposure(),
            oidc_issuer=OIDC_ISSUER,
            oidc_audience=OIDC_AUDIENCE,
            agent_client_ids=["svc-1"],
            client_id_claim=claim,
        )
        return {e["name"]: e["value"] for e in rendered}

    assert "DAEGAM_OIDC_CLIENT_ID_CLAIM" not in env("cid")
    assert env("azp")["DAEGAM_OIDC_CLIENT_ID_CLAIM"] == "azp"


def test_an_oidc_client_id_claim_that_cedar_cannot_name_stops_startup():
    with pytest.raises(ValueError, match="DAEGAM_OIDC_CLIENT_ID_CLAIM"):
        replace(OIDC, oidc_client_id_claim="client-id")
    # local 과 header 는 그 값을 쓰지 않는다
    replace(LOCAL, oidc_client_id_claim="client-id")


# -- mcp_group PATCH ----------------------------------------------------------------------------


def test_local_mcp_group_patch_is_422_for_every_role(local_client, mongo):
    """테넌트 B 의 관리자가 B 의 그룹을 daegam-a 로 바꿔 A 의 Gateway 를 열려 해도 422 이고, B 의
    토큰 groups 는 daegam-b 그대로다. platform-admin 도 같은 422 다."""
    h = admin_headers(local_client, mongo)
    r = local_client.patch("/tenants/b/settings", json={"mcp_group": "team-b"}, headers=h)
    assert detail_code(r) == (422, "invalid_value")
    assert "daegam-<tenant>" in r.json()["detail"]["detail"]
    bob = make_user(local_client, h, "bob", mongo)
    r = local_client.put(
        "/tenants/b/members", json=[{"sub": bob["id"], "role": "tenant-admin"}], headers=h
    )
    assert r.status_code == 200, r.text
    bh = bearer(login(local_client, "bob", password_of("bob")).json()["access_token"])
    for body in ({"mcp_group": "daegam-a"}, {"mcp_group": None}):
        r = local_client.patch("/tenants/b/settings", json=body, headers=bh)
        assert detail_code(r) == (422, "invalid_value")
    # 막는 것은 mcp_group 하나다. tenant-admin 의 다른 칸은 그대로 저장된다
    r = local_client.patch("/tenants/b/settings", json={"agent_client_ids": ["x"]}, headers=bh)
    assert r.status_code == 200 and r.json()["mcp_group"] is None
    dp = local_client.post("/auth/token", headers=bh).json()["access_token"]
    assert _groups(dp) == ["daegam-b"]


def test_agent_oauth_without_a_scope_is_422_and_stores_nothing(client):
    """에이전트 런타임의 client credentials 는 scope 를 요구한다(Okta 의 custom authorization
    server). 빠지거나 비면 422 이고 저장하지 않는다."""
    oauth = {"client_id": "bot", "secret_name": "bot", "token_url": "http://idp/t"}
    for body in (oauth, {**oauth, "scope": ""}):
        r = client.patch("/tenants/acme/settings", json={"agent_oauth": body})
        assert r.status_code == 422, (body, r.text)
    assert client.get("/tenants/acme/settings").json()["agent_oauth"] is None
    r = client.patch("/tenants/acme/settings", json={"agent_oauth": {**oauth, "scope": "mcp"}})
    assert r.status_code == 200 and r.json()["agent_oauth"]["scope"] == "mcp"


# -- 노출 에이전트의 빈 allowed_groups ---------------------------------------------------------


def test_exposed_agent_gets_the_tenant_group_only_in_local_mode(
    client, settings, fake_clusters, fake_secrets, mongo, local_app
):
    """header 에서 배포된 Agent 를 노출한 것처럼 두고 local, oidc 로 차례로 다시 적용한다. local 은
    저장된 mcp_group 과 무관하게 env 와 gateway_token 이 daegam-default 이고, oidc 는 env 를 비운다.
    저장된 노출 설정은 두 모드 모두 빈 목록 그대로다. 이 판 전의 local 이 저장된 그룹으로 낸
    gateway_token 은 groups 가 달라 재적용이 새로 낸다 (P32 F3). local 의 런타임은 플랫폼 JWKS 를
    클러스터 안 주소에서 받고 oidc 는 IdP 디스커버리로 돌아간다 (P32 F4a)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    mongo["gateways"].delete_one({"_id": agent.gw["id"]})  # effective_config 가 빈 가짜 문서라 뺀다
    mongo["agents"].update_one(
        {"_id": agent.id}, {"$set": {"exposure.enabled": True, "exposure.host": "a.e2e.local"}}
    )
    oauth = {"client_id": "bot", "secret_name": "bot", "token_url": "http://idp/t", "scope": "mcp"}
    mongo["tenant_settings"].insert_one(
        {"_id": "default", "tenant": "default", "mcp_group": "team-x", "agent_oauth": oauth}
    )
    fake = agent.fake
    k8s = agent.doc["k8s"]

    def pod_env() -> dict[str, str]:
        dep = fake.get("apps/v1", "Deployment", k8s["name"], k8s["namespace"])
        return env_map(dep["spec"]["template"]["spec"]["containers"][0])

    secret = fake.get("v1", "Secret", k8s["token_secret"], k8s["namespace"])
    with api_client(local_app) as local:
        skip_start_check(local.app)
        issuer = local.app.state.internal_issuer
        stale = issuer.mint_gateway(sub="svc:agent:x", tenant="default", groups=["team-x"])
        secret["stringData"][GATEWAY_TOKEN_KEY] = stale
        fake.apply(secret)
        assert agent.id in run_data_plane_auth(local)["reapplied"]
    env = pod_env()
    assert env["DAEGAM_ALLOWED_GROUPS"] == "daegam-default"
    assert env["DAEGAM_OIDC_JWKS_URL"] == PLATFORM_JWKS
    assert env["DAEGAM_OIDC_INSECURE_HTTP"] == "true"
    secret = fake.get("v1", "Secret", k8s["token_secret"], k8s["namespace"])
    assert _groups(secret_value(secret, GATEWAY_TOKEN_KEY)) == ["daegam-default"]
    oidc = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        auth_mode="oidc",
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=OIDC_AUDIENCE,
        oidc_insecure_http=True,
        data_plane_issuer="idp",
    )
    with api_client(oidc) as other:
        skip_start_check(other.app)
        assert agent.id in run_data_plane_auth(other)["reapplied"]
    env = pod_env()
    assert env["DAEGAM_ALLOWED_GROUPS"] == "" and "DAEGAM_OIDC_JWKS_URL" not in env
    assert mongo["agents"].find_one({"_id": agent.id})["exposure"]["allowed_groups"] == []


# -- 기동 경고 ------------------------------------------------------------------------------------


def _insert_gateway(mongo, name: str, policy: str, *, state: str = "deployed") -> None:
    doc = gateway_doc(name, tenant="a", spec={"authz_policy": policy}, state=state)
    mongo["gateways"].insert_one(doc.to_doc())


def test_local_startup_warns_about_ignored_groups(local_app, mongo, caplog):
    """저장된 mcp_group 이 기본값과 다른 테넌트마다 한 줄, 그 이름을 THVGroup 으로 적은 배포된
    Gateway 마다 한 줄이다. 저장값이 다른 테넌트의 고정 그룹이면 그 테넌트의 토큰이 싣고 있으므로
    Gateway 경고가 없다(e 는 Gateway 만 있는 a 의 것, f 는 설정 문서만 있는 d 의 것). 저장값은
    지우지 않는다."""
    mongo["tenant_settings"].insert_many(
        [
            {"_id": "b", "tenant": "b", "mcp_group": "team-b"},
            {"_id": "c", "tenant": "c", "mcp_group": "daegam-c"},
            {"_id": "d", "tenant": "d"},
            {"_id": "e", "tenant": "e", "mcp_group": "daegam-a"},
            {"_id": "f", "tenant": "f", "mcp_group": "daegam-d"},
        ]
    )
    _insert_gateway(mongo, "wide", group_policy("team-b"))
    _insert_gateway(mongo, "own", group_policy("daegam-a"))
    _insert_gateway(mongo, "peer", group_policy("daegam-d"))
    _insert_gateway(mongo, "draft", group_policy("team-b"), state="draft")
    with caplog.at_level(logging.WARNING, logger="daegam.data_plane_auth"):
        with api_client(local_app):
            pass
    warnings = [m for m in caplog.messages if "local auth mode" in m]
    assert warnings == [
        "tenant b mcp_group 'team-b' is ignored in local auth mode; its MCP group is daegam-b",
        "tenant e mcp_group 'daegam-a' is ignored in local auth mode; its MCP group is daegam-e",
        "tenant f mcp_group 'daegam-d' is ignored in local auth mode; its MCP group is daegam-f",
        "gateway a/wide authz_policy permits THVGroup team-b, which no token carries in local "
        "auth mode",
    ]
    assert mongo["tenant_settings"].find_one({"_id": "b"})["mcp_group"] == "team-b"
