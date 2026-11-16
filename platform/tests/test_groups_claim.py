"""oidc 모드의 데이터 플레인이 플랫폼과 같은 그룹 클레임을 읽는다.

플랫폼 검증기는 사람 토큰의 그룹을 `DAEGAM_OIDC_GROUPS_CLAIM` 으로 읽는다(Keycloak 은 그룹 매퍼
이름이나 `realm_access.roles`). Gateway 의 vMCP 와 노출한 MCP Server 의 앞단(ToolHive Cedar)은 그
이름을 그룹 클레임으로 받고, 노출 Agent 의 런타임은 env 로 받는다. 기본값(groups)인 설치의 렌더와
판은 그대로다.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from daegam.auth.cedar import group_policy, render_authz_inline
from daegam.config import Settings
from daegam.k8s.labels import REV_LENGTH, stable_hash
from daegam.k8s.templates.agent import exposure_env
from daegam.k8s.templates.authz import authz_config_ref, render_authz_config
from daegam.k8s.templates.vmcp import vmcp_incoming_auth
from daegam.services.data_plane_auth import (
    KIND_AGENT,
    KIND_GATEWAY,
    KIND_MCP_SERVER,
    REV_INPUTS,
    auth_rev,
    consumer_auth,
    marked_rev,
)
from daegam.store.agents import AgentRevision, AgentSpec
from daegam.store.exposure import AgentExposure
from daegam.store.tenants import TenantSettings
from tests.helpers.agents import (
    TOKEN_URL,
    template_spec,
)
from tests.helpers.auth import bearer, map_groups
from tests.helpers.backend_apis import CALC_ENDPOINT, uploaded_backend
from tests.helpers.clusters import default_cluster, revoke_tenant_rights
from tests.helpers.consts import ACME_NS, GV, MS, OIDC_AUDIENCE, OIDC_ISSUER
from tests.helpers.docs import TENANT
from tests.helpers.exposure import INGRESS, cluster_secret, ingress_class
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.mcp_servers import clone_and_deploy
from tests.helpers.workers import run_data_plane_auth

CLAIM = "realm_access.roles"
POLICIES = [group_policy("okta-acme")]
# IdP 를 믿는 oidc. 데이터 플레인이 IdP 의 클레임 이름을 읽는 것은 이때뿐이다 (P53b 6.1절)
OIDC = Settings(
    auth_mode="oidc",
    oidc_issuer=OIDC_ISSUER,
    oidc_audience=OIDC_AUDIENCE,
    data_plane_issuer="idp",
)


# -- 렌더 (순수) ------------------------------------------------------------------------------


def test_the_renderers_name_the_claim_only_when_given():
    """이름이 없으면(기본값인 설치) 고치기 전과 같은 꼴이다. 인라인은 authzConfig 옆의
    groupClaimName, 참조는 MCPAuthzConfig 의 cedarv1 설정 group_claim_name 이다."""
    assert render_authz_inline(POLICIES) == {"type": "inline", "inline": {"policies": POLICIES}}
    assert render_authz_inline(POLICIES, CLAIM)["groupClaimName"] == CLAIM
    args = dict(
        cr_name="gw", namespace=ACME_NS, policies=POLICIES, tenant="acme", resource="r", id="i"
    )
    assert render_authz_config(**args, groups_claim=None)["spec"] == {
        "type": "cedarv1",
        "config": {"policies": POLICIES},
    }
    assert render_authz_config(**args, groups_claim=CLAIM)["spec"]["config"] == {
        "policies": POLICIES,
        "group_claim_name": CLAIM,
    }
    oidc_ref = {"name": "daegam-oidc"}
    inline = vmcp_incoming_auth(oidc_ref=oidc_ref, policies=POLICIES, groups_claim=CLAIM)
    assert inline["authzConfig"]["groupClaimName"] == CLAIM
    # 참조일 때 이름은 참조한 설정에 있다. CRD 는 authzConfig 와 authzConfigRef 를 함께 받지 않는다
    by_ref = vmcp_incoming_auth(
        oidc_ref=oidc_ref, policies=POLICIES, authz_ref=authz_config_ref("gw"), groups_claim=CLAIM
    )
    assert by_ref == {
        "type": "oidc",
        "oidcConfigRef": oidc_ref,
        "authzConfigRef": {"name": "gw-authz"},
    }


def test_the_exposed_agent_env_carries_only_a_given_claim():
    def env(groups_claim: str | None) -> dict[str, str]:
        rows = exposure_env(
            AgentExposure(),
            oidc_issuer=OIDC_ISSUER,
            oidc_audience="daegam-mcp",
            agent_client_ids=[],
            groups_claim=groups_claim,
        )
        return {e["name"]: e["value"] for e in rows}

    assert "DAEGAM_OIDC_GROUPS_CLAIM" not in env(None)
    assert env(CLAIM)["DAEGAM_OIDC_GROUPS_CLAIM"] == CLAIM


# -- 인증 입력과 판 (순수) --------------------------------------------------------------------


def test_only_a_non_default_oidc_claim_reaches_the_data_plane():
    """local 모드는 플랫폼이 낸 토큰의 groups 를 읽고, 발급자 없는 oidc 는 header 로 돈다."""
    ts = TenantSettings(tenant="t")
    assert consumer_auth(OIDC, ts).groups_claim is None
    assert consumer_auth(replace(OIDC, oidc_groups_claim=CLAIM), ts).groups_claim == CLAIM
    local = Settings(auth_mode="local", oidc_groups_claim=CLAIM)
    assert consumer_auth(local, ts).groups_claim is None
    no_issuer = Settings(auth_mode="oidc", oidc_groups_claim=CLAIM)
    assert consumer_auth(no_issuer, ts).groups_claim is None


def _agent(*, exposed: bool, gateway_source: bool) -> SimpleNamespace:
    spec = AgentSpec.model_validate(template_spec(source_id="gw-1"))
    if not gateway_source:
        spec.graph.nodes = [n for n in spec.graph.nodes if n.type != "tools"]
    rev = AgentRevision(revision=1, **spec.model_dump())
    return SimpleNamespace(exposure=SimpleNamespace(enabled=exposed), deployed=rev)


def test_a_non_default_claim_marks_only_the_consumers_that_read_it():
    """기본값인 설치의 판은 그 갈래의 입력만 해시한 값 그대로다. 다른 이름은 ToolHive 앞단과 노출
    Agent 의 판에 groups_claim 표시로 얹히고, Gateway 소스만 쓰는 Agent 의 판은 그대로다. client id
    클레임도 같은 식으로 client_id_claim 표시를 얹고, 둘을 함께 바꾸면 두 표시가 함께 얹힌다."""
    ts = TenantSettings(tenant="t")
    named = replace(OIDC, oidc_groups_claim=CLAIM)
    azp = replace(OIDC, oidc_client_id_claim="azp")
    both = replace(named, oidc_client_id_claim="azp")
    inputs = asdict(consumer_auth(OIDC, ts))

    def plain(part: str) -> str:
        return stable_hash({n: inputs[n] for n in sorted(REV_INPUTS[part])}, REV_LENGTH)

    front = SimpleNamespace(exposure=SimpleNamespace(enabled=True))
    exposed = _agent(exposed=True, gateway_source=False)
    for kind, doc, part in (
        (KIND_GATEWAY, None, "toolhive_front"),
        (KIND_MCP_SERVER, front, "toolhive_front"),
        (KIND_AGENT, exposed, "exposed_agent"),
    ):
        assert auth_rev(OIDC, ts, kind, doc) == plain(part)
        assert auth_rev(named, ts, kind, doc) == marked_rev(plain(part), groups_claim=CLAIM)
        assert auth_rev(azp, ts, kind, doc) == marked_rev(plain(part), client_id_claim="azp")
        assert auth_rev(both, ts, kind, doc) == marked_rev(
            plain(part), groups_claim=CLAIM, client_id_claim="azp"
        )
    source_only = _agent(exposed=False, gateway_source=True)
    for settings in (named, azp, both):
        assert auth_rev(settings, ts, KIND_AGENT, source_only) == auth_rev(
            OIDC, ts, KIND_AGENT, source_only
        )


# -- 기동 검사 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(" groups", id="leading-space"),
        pytest.param("realm access.roles", id="inner-space"),
        pytest.param("realm_access..roles", id="empty-segment"),
        pytest.param("realm_access.roles.", id="trailing-dot"),
        pytest.param("grou\tps", id="control"),
        pytest.param("g" * 254, id="too-long"),
    ],
)
def test_oidc_mode_refuses_a_claim_name_that_looks_like_a_typo(name: str):
    """공백, 제어 문자, 빈 마디는 values 파일의 오타로 본다. 253 자는 ToolHive CRD 의 상한이다."""
    with pytest.raises(ValueError, match="DAEGAM_OIDC_GROUPS_CLAIM"):
        replace(OIDC, oidc_groups_claim=name)


def test_the_names_idps_use_start_and_other_modes_do_not_check():
    for name in ("groups", CLAIM, "cognito:groups", "https://acme.example/groups", "g" * 253):
        replace(OIDC, oidc_groups_claim=name)
    Settings(auth_mode="local", oidc_groups_claim="realm access.roles")


# -- 서비스 렌더 ------------------------------------------------------------------------------


@pytest.fixture
def oidc_settings(oidc_settings: Settings) -> Settings:
    return replace(oidc_settings, oidc_groups_claim=CLAIM)


@pytest.fixture
def h(token, mongo) -> dict[str, str]:
    """플랫폼 관리자 root 의 헤더. 그룹은 설정한 클레임에만 있다."""
    map_groups(mongo, [{"group": "daegam-admins", "tenant": "*", "role": "platform-admin"}])
    roles = {"realm_access": {"roles": ["daegam-admins"]}}
    return bearer(token("root", extra=roles), tenant=TENANT)


def _tenant(client, h) -> None:
    r = client.patch(
        "/tenants/acme/settings",
        json={
            "mcp_group": "okta-acme",
            "agent_client_ids": ["agent-1"],
            "agent_oauth": {
                "client_id": "agent-1",
                "secret_name": "agent-oauth",
                "token_url": TOKEN_URL,
                "scope": "mcp",
            },
        },
        headers=h,
    )
    assert r.status_code == 200, r.text


def _exposure_cluster(client, h, fake_clusters) -> tuple[FakeClusterClient, str]:
    fake = fake_clusters.by_name.setdefault("edge", FakeClusterClient())
    ingress_class(fake, "traefik")
    cluster_secret(fake, ACME_NS, "site-tls", tls=True)
    cid = default_cluster(client, headers=h, ingress={**INGRESS, "allow_unauthenticated": False})
    return fake, cid


def test_the_exposed_server_front_reads_groups_from_the_configured_claim(
    oidc_client, h, mongo, fake_clusters
):
    """MCPAuthzConfig 를 참조하는 앞단은 그 설정에, 권한이 없어 인라인인 앞단은 authzConfig 에
    이름을 싣는다. 권한이 바뀐 것만으로 재적용이 앞단을 다시 렌더한다."""
    fake, cid = _exposure_cluster(oidc_client, h, fake_clusters)
    _tenant(oidc_client, h)
    bp = uploaded_backend(oidc_client, endpoint=CALC_ENDPOINT, headers=h)
    mp = oidc_client.post(
        MS,
        json={"name": "calc-mcp", "backend_profile_id": bp["id"], "spec_version": 1},
        headers=h,
    ).json()
    r = clone_and_deploy(
        oidc_client,
        mp,
        name="calc-1",
        headers=h,
        exposure={"enabled": True, "tls": {"mode": "secret", "secret_name": "site-tls"}},
    )
    assert r.status_code == 201, r.text
    front = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", ACME_NS)
    name = front["spec"]["authzConfigRef"]["name"]
    config = fake.get(GV, "MCPAuthzConfig", name, ACME_NS)
    assert config["spec"]["config"].get("group_claim_name") == CLAIM
    revoke_tenant_rights(mongo, cid)
    assert r.json()["id"] in run_data_plane_auth(oidc_client)["reapplied"]
    spec = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", ACME_NS)["spec"]
    assert "authzConfigRef" not in spec
    assert spec["authzConfig"].get("groupClaimName") == CLAIM
