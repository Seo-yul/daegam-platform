"""데이터 플레인 발급자 (P53b 6.1·6.2절, D104).

oidc 모드에서 `DAEGAM_DATA_PLANE_ISSUER=platform` 이면 데이터 플레인을 local 모드처럼 렌더한다.
daegam-oidc 가 플랫폼의 사람 발급자를 이름 대고, 렌더의 그룹은 고정 그룹이며, vMCP·노출 앞단이
위임 토큰을 묶고, Agent 는 Gateway 를 플랫폼의 토큰으로 부르고, Tool 규칙이 local 처럼 컴파일된다.
발급자의 종류는 판에 표시로 얹혀 모든 소비자가 다시 렌더되고, idp(기본)와 local 의 판과 렌더는
그대로다. 전환 뒤의 재렌더는 6.2절의 순서로 한 틱의 몫씩 간다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from pydantic import BaseModel

from daegam.auth.cedar import GATEWAY_TARGET, delegation_permit, group_policy
from daegam.auth.rbac import idp_group, role_group
from daegam.config import Settings, settings_from_env
from daegam.k8s.templates.agent import (
    AGENT_CLIENT_SECRET_ENV,
    CONFIG_URL_ENV,
    GATEWAY_AUTH_QUERY,
    GATEWAY_TOKEN_ENV,
    GATEWAY_TOKEN_KEY,
    IdpVerifier,
    idp_verifier_env,
    render_agent_deployment,
)
from daegam.k8s.templates.svc_token import secret_value
from daegam.services import agent_graph
from daegam.services.agent_features import TWO_ISSUERS
from daegam.services.agents import exposure_verifiers
from daegam.services.data_plane_auth import (
    KIND_AGENT,
    KIND_GATEWAY,
    KIND_MCP_SERVER,
    RANK_FOREIGN_GROUP_FRONT,
    RANK_GATEWAY_SOURCE_AGENT,
    RANK_REST,
    RANK_RULED,
    REAPPLY_PER_TICK,
    DataPlaneAuth,
    IdpSide,
    _Target,
    _Tick,
    auth_rev,
    consumer_auth,
    switch_rank,
)
from daegam.services.jwks_heal import platform_config_names
from daegam.services.rule_compile import RuleScope, caller_clauses, roles_claimed
from daegam.services.tenancy import OIDC_CONFIG_NAME, TOOLHIVE_API, oidc_kwargs
from daegam.store.agents import AgentRevision, AgentRuntime, AgentSpec
from daegam.store.auth_reapply import ConsumerAuthState
from daegam.store.base import utcnow
from daegam.store.exposure import AgentExposure
from daegam.store.tenants import TenantSettings, data_plane_group
from daegam.store.tool_rules import ToolCallers
from tests.helpers.agent_runtime import (
    deployed_agent,
    env_map,
    minimal_agent_doc,
    skip_start_check,
)
from tests.helpers.agents import compile_spec, make_ctx, template_spec
from tests.helpers.consts import (
    DEFAULT_NS,
    IMAGE,
    ISSUER,
    OIDC_AUDIENCE,
    OIDC_ISSUER,
    PLATFORM_JWKS,
    PUBLIC_URL,
)
from tests.helpers.docs import TENANT, effective_config
from tests.helpers.exposure import front_and_gateway
from tests.helpers.gateways import cedar_policies, create_gateway, vmcp_of
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.hub import hub_env
from tests.helpers.svc_tokens import claims_of, make_svc_tokens
from tests.helpers.workers import run_data_plane_auth
from tests.test_tool_rules_api import stored_agent

LOCAL = Settings(auth_mode="local", platform_issuer=ISSUER)
IDP = Settings(
    auth_mode="oidc",
    oidc_issuer=OIDC_ISSUER,
    oidc_audience=OIDC_AUDIENCE,
    oidc_client_id="ui",
    platform_issuer=ISSUER,
    data_plane_issuer="idp",
)
PLATFORM = replace(IDP, data_plane_issuer="platform")
HEADER = Settings(auth_mode="header")
# 테넌트 b 의 관리자가 테넌트 a 의 고정 그룹을 mcp_group 에 적었다
STORED = TenantSettings(tenant="b", mcp_group="daegam-a", agent_client_ids=["bot"])


def _doc(*, exposed: bool = False, gateway_source: bool = False) -> SimpleNamespace:
    spec = AgentSpec.model_validate(template_spec(source_id="gw-1"))
    if not gateway_source:
        spec.graph.nodes = [n for n in spec.graph.nodes if n.type != "tools"]
    rev = AgentRevision(revision=1, **spec.model_dump())
    return SimpleNamespace(exposure=SimpleNamespace(enabled=exposed), deployed=rev)


# -- 설정 (순수) -------------------------------------------------------------------------------


def test_the_setting_comes_from_the_environment_and_rejects_other_values(bare_env):
    """기본은 platform 이고 idp 는 되돌아갈 자리다 (P53b 12절 7번, D104)."""
    assert settings_from_env().data_plane_issuer == "platform"
    assert Settings().data_plane_issuer == "platform"
    bare_env.setenv("DAEGAM_DATA_PLANE_ISSUER", " IdP ")
    assert settings_from_env().data_plane_issuer == "idp"
    bare_env.setenv("DAEGAM_DATA_PLANE_ISSUER", "both")
    with pytest.raises(ValueError, match="DAEGAM_DATA_PLANE_ISSUER"):
        settings_from_env()


def test_minting_and_trust_are_two_rules_and_only_oidc_reads_the_setting():
    """플랫폼은 local 과 oidc 에서 사람 토큰을 내고, 데이터 플레인은 local 과 platform 인 oidc 에서
    플랫폼을 믿는다. local 과 header 는 설정을 읽지 않고, 발급자 없는 oidc 는 header 처럼 돈다."""
    no_issuer = replace(PLATFORM, oidc_issuer=None)
    rows = {
        "local": LOCAL,
        "local+platform": replace(LOCAL, data_plane_issuer="platform"),
        "idp": IDP,
        "platform": PLATFORM,
        "header+platform": replace(HEADER, data_plane_issuer="platform"),
        "oidc without issuer": no_issuer,
    }
    got = {
        name: (s.platform_mints_human_tokens, s.data_plane_trusts_platform, s.data_plane_trusts_idp)
        for name, s in rows.items()
    }
    assert got == {
        "local": (True, True, False),
        "local+platform": (True, True, False),
        "idp": (True, False, True),
        "platform": (True, True, False),
        "header+platform": (False, False, False),
        "oidc without issuer": (False, False, False),
    }
    # 데이터 플레인의 발급자와 JWKS 는 믿는 쪽을 따르고, 제어면의 발급자는 그대로 IdP 다
    assert (PLATFORM.data_plane_issuer_url, PLATFORM.human_jwks_url) == (ISSUER, PLATFORM_JWKS)
    assert (IDP.data_plane_issuer_url, IDP.human_jwks_url) == (OIDC_ISSUER, None)
    assert PLATFORM.issuer == IDP.issuer == OIDC_ISSUER
    # 평문 플래그는 믿는 발급자의 것이다. 클러스터 안 플랫폼 주소는 http 다
    assert PLATFORM.insecure_human_http and not IDP.insecure_human_http
    secure = replace(PLATFORM, platform_issuer="https://daegam.example")
    assert not secure.insecure_human_http
    assert replace(IDP, oidc_insecure_http=True).insecure_human_http
    # 공개 주소가 있으면 사람 발급자는 그것이고 두 값에서 같다. 그래서 바꿔도 이미 낸 토큰이 산다
    public = replace(PLATFORM, public_url=PUBLIC_URL)
    assert public.data_plane_issuer_url == PUBLIC_URL
    assert public.platform_human_issuer == replace(IDP, public_url=PUBLIC_URL).platform_human_issuer
    # OAuth 인가 서버는 platform 에서 깨어 있고 idp 에서 잠든다
    assert public.oauth_enabled and not public.oauth_dormant
    assert replace(IDP, public_url=PUBLIC_URL).oauth_dormant


# -- 렌더 입력과 판 (순수) ----------------------------------------------------------------------


def test_platform_takes_local_modes_render_inputs_and_keeps_the_idp_beside_it():
    trusted, local, idp = (consumer_auth(s, STORED) for s in (PLATFORM, LOCAL, IDP))
    for name in (
        "issuer",
        "jwks_url",
        "insecure_http",
        "mcp_audience",
        "tenant_group",
        "groups_claim",
        "client_id_claim",
        "agent_client_ids",
        "scopes",
    ):
        assert getattr(trusted, name) == getattr(local, name), name
    assert trusted.tenant_group == data_plane_group(PLATFORM, STORED) == "daegam-b"
    assert trusted.binds_delegation and trusted.exposure_default_group == "daegam-b"
    assert trusted.idp == IdpSide(
        issuer=OIDC_ISSUER, insecure_http=False, groups_claim=None, client_id_claim="cid"
    )
    # IdP 를 믿는 동안은 저장된 그룹과 IdP 를 그대로 쓴다
    assert (idp.tenant_group, idp.issuer, idp.jwks_url) == ("daegam-a", OIDC_ISSUER, None)
    assert not idp.binds_delegation and idp.exposure_default_group is None and idp.idp is None
    assert local.idp is None
    # IdP 의 클레임 이름은 IdP 검증기에만 간다
    claims = replace(PLATFORM, oidc_groups_claim="roles", oidc_client_id_claim="azp")
    auth = consumer_auth(claims, STORED)
    assert (auth.groups_claim, auth.client_id_claim) == (None, "cid")
    assert auth.idp is not None and (auth.idp.groups_claim, auth.idp.client_id_claim) == (
        "roles",
        "azp",
    )


def _rev_table() -> dict[str, object]:
    """local·idp·header 의 판과 namespace 설정 인자. P53b 앞의 코드가 낸 것(`BEFORE_P53B`)과
    같아야 한다. 발급자 설정이 그 모드의 렌더와 판을 바꾸지 않았다는 증거다."""
    local = replace(LOCAL)
    idp = replace(IDP)
    settings = {
        "local": local,
        "local_public": replace(local, public_url=PUBLIC_URL),
        "idp": idp,
        "idp_public": replace(idp, public_url=PUBLIC_URL),
        "idp_claims": replace(
            idp, oidc_groups_claim="roles", oidc_client_id_claim="azp", oidc_insecure_http=True
        ),
        "header": HEADER,
    }
    tenants = {
        "plain": TenantSettings(tenant="t"),
        "stored": TenantSettings(tenant="t", mcp_group="team-t", agent_client_ids=["bot"]),
    }
    docs = {
        "gateway": (KIND_GATEWAY, None),
        "front": (KIND_MCP_SERVER, _doc(exposed=True)),
        "exposed": (KIND_AGENT, _doc(exposed=True)),
        "source": (KIND_AGENT, _doc(gateway_source=True)),
        "both": (KIND_AGENT, _doc(exposed=True, gateway_source=True)),
    }
    table: dict[str, object] = {
        f"{s}/{t}/{d}": auth_rev(settings[s], tenants[t], kind, doc)
        for s in settings
        for t in tenants
        for d, (kind, doc) in docs.items()
    }
    table.update({f"kwargs/{s}": oidc_kwargs(settings[s]) for s in settings})
    return table


# P53b 앞의 코드가 낸 판과 daegam-oidc 입력. 그 커밋에서 `_rev_table` 로 만들었다
BEFORE_P53B = Path(__file__).parent / "fixtures" / "data-plane-auth-before-p53b.json"


def test_local_idp_and_header_revisions_and_namespace_configs_are_unchanged():
    """idp, local, header 의 소비자 판과 daegam-oidc 입력이 P53b 앞과 같다. 판이 바뀌면 그 문서가
    모두 다시 렌더되고 파드가 굴러가므로 키마다 견준다."""
    before = json.loads(BEFORE_P53B.read_text())
    now = json.loads(json.dumps(_rev_table()))
    assert {k: now.get(k) for k in before} == before and set(now) == set(before)
    # local 은 설정을 읽지 않는다
    ts = TenantSettings(tenant="t")
    moved = replace(LOCAL, data_plane_issuer="platform")
    for kind, doc in ((KIND_GATEWAY, None), (KIND_AGENT, _doc(exposed=True, gateway_source=True))):
        assert auth_rev(moved, ts, kind, doc) == auth_rev(LOCAL, ts, kind, doc)


def test_the_switch_moves_every_consumer_and_only_exposed_agents_follow_the_idp():
    ts = TenantSettings(tenant="t")
    docs = [
        (KIND_GATEWAY, None),
        (KIND_MCP_SERVER, _doc(exposed=True)),
        (KIND_AGENT, _doc(exposed=True)),
        (KIND_AGENT, _doc(gateway_source=True)),
    ]
    for kind, doc in docs:
        assert auth_rev(PLATFORM, ts, kind, doc) not in (
            auth_rev(IDP, ts, kind, doc),
            auth_rev(LOCAL, ts, kind, doc),
        )
    # 노출하지 않은 MCP Server 는 소비자가 아니다
    assert auth_rev(PLATFORM, ts, KIND_MCP_SERVER, _doc()) is None
    # IdP 를 옮기면 그 검증기를 두는 노출 Agent 만 다시 렌더된다
    other = replace(PLATFORM, oidc_issuer="https://idp.other")
    exposed, source = _doc(exposed=True), _doc(gateway_source=True)
    assert auth_rev(other, ts, KIND_AGENT, exposed) != auth_rev(PLATFORM, ts, KIND_AGENT, exposed)
    assert auth_rev(other, ts, KIND_AGENT, source) == auth_rev(PLATFORM, ts, KIND_AGENT, source)
    assert auth_rev(other, ts, KIND_GATEWAY, None) == auth_rev(PLATFORM, ts, KIND_GATEWAY, None)
    # 저장된 mcp_group 은 platform 의 판에 들지 않는다
    stored = ts.model_copy(update={"mcp_group": "team-t"})
    assert auth_rev(PLATFORM, stored, KIND_GATEWAY, None) == auth_rev(
        PLATFORM, ts, KIND_GATEWAY, None
    )
    assert auth_rev(IDP, stored, KIND_GATEWAY, None) != auth_rev(IDP, ts, KIND_GATEWAY, None)


def test_daegam_oidc_names_the_platform_without_a_client_id_where_it_is_trusted():
    kwargs = oidc_kwargs(PLATFORM)
    assert kwargs["oidc_issuer"] == ISSUER and kwargs["oidc_jwks_url"] == PLATFORM_JWKS
    assert kwargs["oidc_client_id"] is None and kwargs["oidc_insecure_http"] is True
    public = oidc_kwargs(replace(PLATFORM, public_url=PUBLIC_URL))
    assert public["oidc_issuer"] == PUBLIC_URL and public["oidc_jwks_url"] == PLATFORM_JWKS
    kwargs = oidc_kwargs(IDP)
    assert (kwargs["oidc_issuer"], kwargs["oidc_jwks_url"], kwargs["oidc_client_id"]) == (
        OIDC_ISSUER,
        None,
        "ui",
    )
    # JWKS 치유는 platform 에서 사람 토큰의 설정도 본다
    assert OIDC_CONFIG_NAME in platform_config_names(PLATFORM)
    assert OIDC_CONFIG_NAME not in platform_config_names(IDP)


def test_tokens_for_human_boundaries_carry_the_fixed_group_in_every_mode():
    async def run():
        for settings in (PLATFORM, IDP, LOCAL):
            tokens = make_svc_tokens(group="daegam-a", settings=settings)
            gateway = await tokens.mint_gateway(name="ai", tenant="b", agent_id="ag-1")
            probe = await tokens.platform_gateway_token("b")
            assert claims_of(gateway)["groups"] == claims_of(probe)["groups"] == ["daegam-b"]
            assert await tokens.gateway_token_current(gateway, "b")
            stale = tokens.issuer.mint_gateway(sub="svc:agent:ai", tenant="b", groups=["daegam-a"])
            assert not await tokens.gateway_token_current(stale, "b")
        # 내부 서버에 가는 토큰은 저장된 그룹을 그대로 싣는다(oidc). 바뀌지 않아 다시 나지 않는다
        for settings in (PLATFORM, IDP):
            tokens = make_svc_tokens(group="daegam-a", settings=settings)
            assert await tokens.groups("b") == ["daegam-a"]
        assert [make_svc_tokens(settings=s).mints_gateway_tokens for s in (PLATFORM, IDP)] == [
            True,
            False,
        ]

    asyncio.run(run())


# -- Agent 의 Gateway 인증과 Tool 규칙 (순수) --------------------------------------------------


def test_agents_call_gateways_with_platform_tokens_where_it_is_trusted():
    spec = AgentSpec.model_validate(template_spec())
    trusted = dataclasses.replace(make_ctx(oauth=False), auth_mode="oidc", trusts_platform=True)
    assert agent_graph.gateway_auth_kind(trusted) == "bearer"
    assert agent_graph.validate(spec, trusted) == []
    (tool,) = compile_spec(template_spec(), trusted)["tools"]
    assert tool["auth"] == {"kind": "bearer", "secret_env": GATEWAY_TOKEN_ENV}
    idp = dataclasses.replace(make_ctx(oauth=False), auth_mode="oidc")
    assert agent_graph.gateway_auth_kind(idp) == "oauth"
    assert {i.code for i in agent_graph.validate(spec, idp)} == {"agent_oauth_missing"}
    # Gateway 소스는 platform 에서 턴 위임 토큰을 받는다
    assert ("gateway_instance", "gi-calc-0001") in agent_graph.turn_token_sources(
        spec, trusts_platform=PLATFORM.data_plane_trusts_platform
    )


def test_rules_compile_as_in_local_mode_with_qualified_idp_groups():
    callers = ToolCallers(roles=["developer"], groups=["eng"], agents=["a1"])
    platform = RuleScope(
        tenant="t", mode="platform", agents={"a1": "agent-a1"}, agent_client_ids=("bot",)
    )
    vmcp = caller_clauses(callers, platform, "vmcp")
    person = vmcp[0]
    assert role_group("t", "developer") in person and idp_group("t", "eng") in person
    assert '"eng"' not in person.replace(idp_group("t", "eng"), "") and "bot" not in person
    assert any("agent-a1" in c for c in vmcp[1:])
    assert roles_claimed([callers], platform)
    # IdP 를 믿는 동안은 매핑된 IdP 그룹과 원래 그룹이고 Agent 절과 역할 클레임이 없다
    idp = RuleScope(tenant="t", mode="oidc", role_groups={"developer": ("devs",)})
    (clause,) = caller_clauses(callers, idp, "vmcp")
    assert "devs" in clause and '"eng"' in clause and not roles_claimed([callers], idp)


# -- 전환의 순서와 속도 (순수) -----------------------------------------------------------------


def _rules(callers: bool) -> SimpleNamespace:
    rule = SimpleNamespace(callers=SimpleNamespace(empty=not callers))
    return SimpleNamespace(backends=[SimpleNamespace(tool_rules={"add": rule})])


def test_switch_rank_orders_the_documents_of_section_6_2():
    plain, stored = TenantSettings(tenant="t"), TenantSettings(tenant="t", mcp_group="team-t")
    fixed = TenantSettings(tenant="t", mcp_group="daegam-t")
    gateway = SimpleNamespace(deployed=_rules(False))
    ruled = SimpleNamespace(deployed=_rules(True))
    front = SimpleNamespace(exposure=SimpleNamespace(enabled=True), deployed=_rules(True))
    hidden = SimpleNamespace(exposure=SimpleNamespace(enabled=False), deployed=_rules(True))
    source = _doc(gateway_source=True)
    assert switch_rank(PLATFORM, stored, KIND_GATEWAY, gateway) == RANK_FOREIGN_GROUP_FRONT
    assert switch_rank(PLATFORM, stored, KIND_GATEWAY, ruled) == RANK_FOREIGN_GROUP_FRONT
    # 그런 테넌트의 노출 앞단도 먼저다. 노출하지 않은 MCP Server 는 앞단이 없다
    assert switch_rank(PLATFORM, stored, KIND_MCP_SERVER, front) == RANK_FOREIGN_GROUP_FRONT
    assert switch_rank(PLATFORM, stored, KIND_MCP_SERVER, hidden) == RANK_REST
    assert switch_rank(PLATFORM, fixed, KIND_GATEWAY, gateway) == RANK_REST
    assert switch_rank(PLATFORM, plain, KIND_AGENT, source) == RANK_GATEWAY_SOURCE_AGENT
    assert switch_rank(PLATFORM, plain, KIND_AGENT, _doc(exposed=True)) == RANK_REST
    assert switch_rank(PLATFORM, plain, KIND_GATEWAY, ruled) == RANK_RULED
    assert switch_rank(PLATFORM, plain, KIND_MCP_SERVER, front) == RANK_RULED
    assert switch_rank(PLATFORM, plain, KIND_MCP_SERVER, hidden) == RANK_REST
    assert switch_rank(PLATFORM, plain, KIND_GATEWAY, gateway) == RANK_REST
    # IdP 로 돌아갈 때는 Gateway 소스를 쓰는 Agent 가 먼저다
    assert switch_rank(IDP, stored, KIND_GATEWAY, gateway) == RANK_REST
    assert switch_rank(IDP, plain, KIND_AGENT, source) == RANK_GATEWAY_SOURCE_AGENT
    # local 과 header 에서는 모두 같은 자리라 순서가 바뀌지 않는다
    for settings in (LOCAL, HEADER):
        assert switch_rank(settings, stored, KIND_GATEWAY, gateway) == 0
        assert switch_rank(settings, plain, KIND_AGENT, source) == 0


class _K8s(BaseModel):
    namespace: str


class _Doc(ConsumerAuthState):
    id: str
    k8s: _K8s
    cluster_id: str = "c1"
    last_spec_change: datetime | None = None


def test_the_switch_rerenders_in_rank_order_at_reapply_per_tick_a_tick():
    """K 개 문서가 판이 바뀌면 틱마다 `REAPPLY_PER_TICK` 개씩, 6.2절의 순서로 다시 렌더된다. 한
    네임스페이스에 몰려도 인증 입력이 바뀐 문서는 네임스페이스의 수에 막히지 않으므로 창은
    ceil(K/10) 틱이다."""
    calls: list[str] = []

    async def reapply_auth(doc):
        calls.append(doc.id)
        return True

    counts = {
        RANK_REST: 9,
        RANK_RULED: 6,
        RANK_GATEWAY_SOURCE_AGENT: 8,
        RANK_FOREIGN_GROUP_FRONT: 4,
    }
    targets = []
    for rank, count in counts.items():
        for i in range(count):
            service = SimpleNamespace(reapply_auth=reapply_auth, auth_kind=KIND_GATEWAY)
            doc = _Doc(id=f"r{rank}-{i}", k8s=_K8s(namespace="ns"), auth_rev="idp")
            targets.append(_Target(service, doc, "platform", 1, rank=rank))
    total = sum(counts.values())
    none = SimpleNamespace()
    kinds = ("gateways", "mcp_servers", "agents", "hub_items", "shared_clones")
    auth = DataPlaneAuth(none, none, **dict.fromkeys(kinds, none))  # type: ignore[arg-type]
    ticks: list[list[str]] = []
    while len(calls) < total:
        before = len(calls)
        tick = _Tick(now=utcnow(), install_id="i", tenant_settings=None)  # type: ignore[arg-type]
        asyncio.run(auth._reapply(targets, tick))
        ticks.append(calls[before:])
    assert [len(t) for t in ticks] == [REAPPLY_PER_TICK, REAPPLY_PER_TICK, total - 20]
    ranks = [int(c.split("-")[0][1:]) for c in calls]
    assert ranks == sorted(ranks)
    assert all(t.current for t in targets)


# -- 렌더 (앱) ----------------------------------------------------------------------------------


def _oidc_app(settings, fake_clusters, fake_secrets, issuer: str):
    return make_app(
        settings,
        fake_clusters,
        fake_secrets,
        auth_mode="oidc",
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=OIDC_AUDIENCE,
        oidc_client_id="ui",
        oidc_insecure_http=True,
        platform_issuer=ISSUER,
        data_plane_issuer=issuer,
    )


def test_switching_renders_gateways_and_fronts_as_local_mode_and_back(
    client, settings, fake_clusters, fake_secrets, mongo
):
    """header 에서 배포한 Gateway 와 노출 앞단을 platform 인 oidc 로 다시 적용하면 daegam-oidc 가
    플랫폼을 이름 대고, vMCP 가 저장된 mcp_group 이 아닌 고정 그룹과 자기에게 묶인 위임 토큰을
    받는다. idp 로 되돌리면 IdP 와 저장된 그룹으로 돌아간다."""
    front, gateway, fake = front_and_gateway(client, fake_clusters)
    mongo["tenant_settings"].insert_one(
        {"_id": "default", "tenant": "default", "mcp_group": "team-other"}
    )
    vmcp = gateway["k8s"]["name"]
    front_vmcp = f"front-{front['k8s']['name']}"
    both = sorted([front["id"], gateway["id"]])
    permit = delegation_permit(GATEWAY_TARGET.format(gateway["id"]), "default")
    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "platform")) as app:
        assert sorted(run_data_plane_auth(app)["reapplied"]) == both
    config = fake.get(TOOLHIVE_API, "MCPOIDCConfig", OIDC_CONFIG_NAME, DEFAULT_NS)
    inline = config["spec"]["inline"]
    assert inline["issuer"] == ISSUER and inline["jwksUrl"] == PLATFORM_JWKS
    assert "clientId" not in inline
    policies = cedar_policies(vmcp_of(fake, vmcp), fake=fake)
    assert group_policy("daegam-default") in policies and permit in policies
    assert not any("team-other" in p for p in policies)
    front_policies = cedar_policies(vmcp_of(fake, front_vmcp), fake=fake)
    assert any("claim_bound_to" in p for p in front_policies)
    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "idp")) as app:
        assert sorted(run_data_plane_auth(app)["reapplied"]) == both
    inline = fake.get(TOOLHIVE_API, "MCPOIDCConfig", OIDC_CONFIG_NAME, DEFAULT_NS)["spec"]["inline"]
    assert inline["issuer"] == OIDC_ISSUER and inline["clientId"] == "ui"
    policies = cedar_policies(vmcp_of(fake, vmcp), fake=fake)
    assert group_policy("team-other") in policies and permit not in policies


def test_switching_moves_an_agents_gateway_source_onto_a_gateway_token_and_back(
    client, settings, fake_clusters, fake_secrets, mongo
):
    """Gateway 소스를 쓰는 Agent 는 platform 에서 gateway_token 으로 부르고 agent_oauth 를 받지
    않는다. 그 토큰은 고정 그룹을 싣는다. idp 로 되돌리면 client credentials 로 돌아간다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    mongo["gateways"].delete_one({"_id": agent.gw["id"]})
    oauth = {"client_id": "bot", "secret_name": "bot", "token_url": "http://idp/t", "scope": "mcp"}
    mongo["tenant_settings"].insert_one(
        {
            "_id": "default",
            "tenant": "default",
            "mcp_group": "team-other",
            "agent_oauth": oauth,
            "agent_client_ids": ["bot"],
        }
    )
    fake, k8s = agent.fake, agent.doc["k8s"]

    def pod_env() -> dict[str, str]:
        dep = fake.get("apps/v1", "Deployment", k8s["name"], k8s["namespace"])
        return env_map(dep["spec"]["template"]["spec"]["containers"][0])

    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "platform")) as app:
        skip_start_check(app.app)
        assert agent.id in run_data_plane_auth(app)["reapplied"]
    env = pod_env()
    assert GATEWAY_TOKEN_ENV in env and AGENT_CLIENT_SECRET_ENV not in env
    secret = fake.get("v1", "Secret", k8s["token_secret"], k8s["namespace"])
    token = secret_value(secret, GATEWAY_TOKEN_KEY)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["groups"] == ["daegam-default"] and claims["iss"] == ISSUER
    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "idp")) as app:
        skip_start_check(app.app)
        assert agent.id in run_data_plane_auth(app)["reapplied"]
    env = pod_env()
    assert GATEWAY_TOKEN_ENV not in env and AGENT_CLIENT_SECRET_ENV in env


def test_a_pod_gets_the_gateway_auth_of_its_own_template_from_either_mode(
    client, settings, fake_clusters, fake_secrets, mongo
):
    """믿는 발급자를 바꾸는 동안에는 두 모드의 플랫폼 파드가 함께 init 받기에 답한다. 새 템플릿의
    파드가 옛 모드의 파드에서 문서를 받아도 그 문서의 Gateway 소스 인증은 그 파드의 env 에 있는
    비밀을 가리킨다. 아니면 런타임이 `tool_secret_missing` 으로 뜨지 못하고, init 은 다시 돌지
    않으므로 그 파드는 지워질 때까지 그대로다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    oauth = {"client_id": "bot", "secret_name": "bot", "token_url": "http://idp/t", "scope": "mcp"}
    mongo["tenant_settings"].insert_one(
        {"_id": "default", "tenant": "default", "agent_oauth": oauth, "agent_client_ids": ["bot"]}
    )
    fake, k8s = agent.fake, agent.doc["k8s"]

    def pod() -> tuple[str | None, dict[str, str]]:
        """렌더한 파드의 init 주소가 실은 Gateway 소스 인증과 메인 컨테이너의 env."""
        spec = fake.get("apps/v1", "Deployment", k8s["name"], k8s["namespace"])["spec"]
        template = spec["template"]["spec"]
        query = parse_qs(urlsplit(env_map(template["initContainers"][0])[CONFIG_URL_ENV]).query)
        return query.get(GATEWAY_AUTH_QUERY, [None])[0], env_map(template["containers"][0])

    def secret_envs(app, asked: str | None) -> set[str]:
        """그 플랫폼이 asked 를 실은 init 받기에 내는 문서에서 Gateway 소스의 비밀 env."""
        service, store = app.app.state.agent_service, app.app.state.agents

        async def fetch() -> dict:
            doc = await store.get(agent.id)
            return await service.fetch_config(doc, {}, gateway_auth=asked)

        tools = app.portal.call(fetch)["tools"]
        return {t["auth"]["secret_env"] for t in tools if t["source"]["kind"] == "gateway_instance"}

    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "platform")) as old:
        skip_start_check(old.app)
        assert agent.id in run_data_plane_auth(old)["reapplied"]
        bearer, bearer_env = pod()
        assert bearer == "bearer" and secret_envs(old, bearer) == {GATEWAY_TOKEN_ENV}
        with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "idp")) as new:
            skip_start_check(new.app)
            assert agent.id in run_data_plane_auth(new)["reapplied"]
            asked, env = pod()
            assert asked == "oauth" and AGENT_CLIENT_SECRET_ENV in env
            assert GATEWAY_TOKEN_ENV not in env
            # 새 템플릿의 파드는 어느 모드의 플랫폼 파드에서 받아도 client credentials 다
            assert secret_envs(new, asked) == secret_envs(old, asked) == {AGENT_CLIENT_SECRET_ENV}
            # 아직 남은 옛 템플릿의 파드가 다시 떠도 자기 env 의 토큰을 가리키는 문서를 받는다
            assert secret_envs(new, bearer) == {GATEWAY_TOKEN_ENV} <= set(bearer_env)
            # 종류를 알리지 않는 옛 렌더의 파드는 답하는 플랫폼의 모드를 따른다
            assert secret_envs(new, None) == {AGENT_CLIENT_SECRET_ENV}
            assert secret_envs(old, None) == {GATEWAY_TOKEN_ENV}


def test_a_gateway_rule_names_an_agent_only_once_gateways_trust_the_platform(
    client, fake_clusters, mongo
):
    """Gateway 규칙의 Agent 는 데이터 플레인이 IdP 를 믿는 동안만 422 `rule_agent_oidc` 다. 판정은
    서비스가 `Settings.data_plane_trusts_idp` 를 읽어 낸다."""
    _cid, items, *_ = hub_env(client, fake_clusters)
    calc = items["calculator"]["id"]
    helper = stored_agent(mongo, "helper")
    rule = {"Add": {"callers": {"agents": [helper]}}}
    validator = client.app.state.gateway_service.validator
    validator.agents_refused = lambda: IDP.data_plane_trusts_idp
    r = create_gateway(client, items, backends=[{"hub_item_id": calc, "tool_rules": rule}])
    assert detail_code(r) == (422, "rule_agent_oidc")
    validator.agents_refused = lambda: PLATFORM.data_plane_trusts_idp
    r = create_gateway(client, items, backends=[{"hub_item_id": calc, "tool_rules": rule}])
    assert r.status_code == 201, r.text


# -- 노출 Agent 의 두 발급자 (P53b 6.8절) ----------------------------------------------------------


def _exposed(groups: list[str], *, two_issuers: bool) -> SimpleNamespace:
    return SimpleNamespace(
        tenant="b",
        exposure=AgentExposure(allowed_groups=groups),
        image_reports=lambda name: two_issuers and name == TWO_ISSUERS,
    )


def test_exposed_agents_keep_the_idp_and_add_the_platform_only_on_a_two_issuer_image():
    """F7 의 계약(`runtime.auth.read_issuers`): 두 발급자 이미지는 플랫폼을 첫 발급자(DAEGAM_OIDC_*,
    JWKS 주소)로, IdP 를 곁(DAEGAM_IDP_*)으로 받는다. 옛 이미지는 IdP 를 믿을 때와 같은 입력이다."""
    trusted, idp = consumer_auth(PLATFORM, STORED), consumer_auth(IDP, STORED)
    old = exposure_verifiers(trusted, _exposed(["eng"], two_issuers=False))
    assert old == exposure_verifiers(idp, _exposed(["eng"], two_issuers=False))
    assert old["oidc_issuer"] == OIDC_ISSUER and old["oidc_jwks_url"] is None
    new = exposure_verifiers(trusted, _exposed(["eng", "daegam-ops"], two_issuers=True))
    assert new == {
        "oidc_issuer": ISSUER,
        "oidc_jwks_url": PLATFORM_JWKS,
        "oidc_insecure_http": True,
        "agent_client_ids": [],
        "client_id_claim": "cid",
        "groups_claim": None,
        "default_group": "daegam-b",
        "allowed_groups": (idp_group("b", "eng"), "daegam-ops"),
        "idp_verifier": IdpVerifier(
            IdpSide(OIDC_ISSUER, insecure_http=False, groups_claim=None, client_id_claim="cid"),
            allowed_groups=("eng",),
            agent_client_ids=("bot",),
        ),
    }
    empty = exposure_verifiers(trusted, _exposed([], two_issuers=True))
    assert empty["allowed_groups"] == () and empty["default_group"] == "daegam-b"
    assert empty["idp_verifier"].allowed_groups == ()
    # local 은 오늘과 같다
    local = exposure_verifiers(consumer_auth(LOCAL, STORED), _exposed([], two_issuers=True))
    assert local["oidc_issuer"] == ISSUER and local["default_group"] == "daegam-b"
    assert "idp_verifier" not in local


def _pod_env(settings: Settings, groups: list[str], ts: TenantSettings = STORED) -> dict[str, str]:
    """두 발급자 이미지의 노출 Agent 를 그 설정으로 렌더한 컨테이너 env 다."""
    auth = consumer_auth(settings, ts)
    inst = minimal_agent_doc()
    inst.exposure = AgentExposure(enabled=True, host="a.e2e.local", allowed_groups=groups)
    dep = render_agent_deployment(
        inst,
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        oidc_audience=auth.mcp_audience,
        **exposure_verifiers(auth, _exposed(groups, two_issuers=True)),
    )
    return env_map(dep["spec"]["template"]["spec"]["containers"][0])


def test_an_agent_narrowed_to_platform_groups_gets_no_idp_verifier():
    """빈 DAEGAM_IDP_ALLOWED_GROUPS 는 IdP 가 서명한 모든 토큰을 받는다. 플랫폼
    그룹으로만 좁힌 Agent 는 IdP 묶음이 없어야 하고, 저장된 목록이 빈 Agent 만 빈 목록을 받는다."""
    narrowed = _pod_env(PLATFORM, ["daegam-b"])
    assert not [name for name in narrowed if name.startswith("DAEGAM_IDP_")]
    assert narrowed["DAEGAM_ALLOWED_GROUPS"] == "daegam-b"
    empty = _pod_env(PLATFORM, [])
    assert empty["DAEGAM_IDP_ISSUER"] == OIDC_ISSUER and empty["DAEGAM_IDP_ALLOWED_GROUPS"] == ""
    assert empty["DAEGAM_ALLOWED_GROUPS"] == "daegam-b"
    mixed = _pod_env(PLATFORM, ["eng", "daegam-ops"])
    assert mixed["DAEGAM_IDP_ALLOWED_GROUPS"] == "eng"
    assert mixed["DAEGAM_ALLOWED_GROUPS"] == f"{idp_group('b', 'eng')},daegam-ops"


def test_the_platform_verifier_reads_no_client_id_and_no_idp_claim_name():
    """플랫폼 토큰은 client id 로 들어오지 않고 플랫폼의 클레임 이름으로 읽힌다. IdP 의
    클레임 이름과 client id 는 DAEGAM_IDP_* 에만 있다."""
    claims = replace(PLATFORM, oidc_groups_claim="realm_access.roles", oidc_client_id_claim="azp")
    ts = TenantSettings(tenant="b", agent_client_ids=["svc-1"])
    env = _pod_env(claims, ["eng"], ts)
    assert env["DAEGAM_AGENT_CLIENT_IDS"] == ""
    assert "DAEGAM_OIDC_GROUPS_CLAIM" not in env and "DAEGAM_OIDC_CLIENT_ID_CLAIM" not in env
    assert env["DAEGAM_IDP_GROUPS_CLAIM"] == "realm_access.roles"
    assert env["DAEGAM_IDP_CLIENT_ID_CLAIM"] == "azp"
    assert env["DAEGAM_IDP_AGENT_CLIENT_IDS"] == "svc-1"


@pytest.mark.parametrize("groups", [[], ["eng"], ["daegam-b"], ["eng", "daegam-ops"]])
def test_every_two_issuer_render_carries_the_platform_jwks_address(groups):
    """런타임은 JWKS 주소가 있는 첫 발급자를 플랫폼으로 보아 위임 토큰을 이
    Agent 에 묶는다."""
    env = _pod_env(PLATFORM, groups)
    assert env["DAEGAM_OIDC_ISSUER"] == ISSUER
    assert env["DAEGAM_OIDC_JWKS_URL"] == PLATFORM_JWKS


@pytest.mark.parametrize("idp_issuer", [ISSUER, ISSUER + "/"])
def test_a_platform_issuer_equal_to_the_idp_stops_the_start(idp_issuer):
    """두 묶음에 같은 발급자가 있으면 노출 Agent 마다 기동하지 못하므로, 플랫폼이 먼저
    기동을 막는다. IdP 를 믿는 동안은 두 묶음을 렌더하지 않으므로 막지 않는다."""
    with pytest.raises(ValueError, match="platform issuer other than the IdP"):
        replace(PLATFORM, oidc_issuer=idp_issuer)
    assert replace(IDP, oidc_issuer=idp_issuer).data_plane_trusts_idp


def test_the_idp_verifier_env_carries_the_idp_claim_names_and_lists():
    verifier = IdpVerifier(
        IdpSide(OIDC_ISSUER, insecure_http=True, groups_claim="roles", client_id_claim="azp"),
        allowed_groups=("eng",),
        agent_client_ids=("bot", "cron"),
    )
    assert idp_verifier_env(verifier) == [
        {"name": "DAEGAM_IDP_ISSUER", "value": OIDC_ISSUER},
        {"name": "DAEGAM_IDP_ALLOWED_GROUPS", "value": "eng"},
        {"name": "DAEGAM_IDP_AGENT_CLIENT_IDS", "value": "bot,cron"},
        {"name": "DAEGAM_IDP_GROUPS_CLAIM", "value": "roles"},
        {"name": "DAEGAM_IDP_INSECURE_HTTP", "value": "true"},
        {"name": "DAEGAM_IDP_CLIENT_ID_CLAIM", "value": "azp"},
    ]
    side = IdpSide(OIDC_ISSUER, insecure_http=False, groups_claim=None, client_id_claim="cid")
    plain = replace(verifier, side=side)
    assert [e["name"] for e in idp_verifier_env(plain)] == [
        "DAEGAM_IDP_ISSUER",
        "DAEGAM_IDP_ALLOWED_GROUPS",
        "DAEGAM_IDP_AGENT_CLIENT_IDS",
    ]


def test_a_two_issuer_exposed_agent_renders_both_verifiers(
    client, settings, fake_clusters, fake_secrets, mongo
):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, features=TWO_ISSUERS)
    mongo["gateways"].delete_one({"_id": agent.gw["id"]})
    exposure = {"enabled": True, "host": "a.e2e.local", "allowed_groups": ["eng"]}
    mongo["agents"].update_one(
        {"_id": agent.id}, {"$set": {f"exposure.{k}": v for k, v in exposure.items()}}
    )
    fake, k8s = agent.fake, agent.doc["k8s"]
    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "platform")) as app:
        skip_start_check(app.app)
        assert agent.id in run_data_plane_auth(app)["reapplied"]
    dep = fake.get("apps/v1", "Deployment", k8s["name"], k8s["namespace"])
    env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
    assert env["DAEGAM_OIDC_ISSUER"] == ISSUER and env["DAEGAM_OIDC_JWKS_URL"] == PLATFORM_JWKS
    assert env["DAEGAM_ALLOWED_GROUPS"] == "daegam-group:default:eng"
    assert env["DAEGAM_AGENT_CLIENT_IDS"] == ""
    assert env["DAEGAM_IDP_ISSUER"] == OIDC_ISSUER and env["DAEGAM_IDP_ALLOWED_GROUPS"] == "eng"
    assert "DAEGAM_IDP_JWKS_URL" not in env


def test_a_first_deploy_moves_to_two_issuers_once_the_image_reports_them(
    client, settings, fake_clusters, fake_secrets, mongo
):
    """첫 배포에는 이미지의 기록이 없어 IdP 하나로 렌더된다. 새 파드의 init 이 `two_issuers` 를
    알리면 판의 표시가 사라져 다음 틱이 두 발급자로 다시 렌더한다. 표시가 없으면 그 Agent 는 다시
    배포할 때까지 플랫폼의 토큰을 `issuer mismatch` 로 막는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    mongo["gateways"].delete_one({"_id": agent.gw["id"]})
    exposure = {"enabled": True, "host": "a.e2e.local", "allowed_groups": []}
    mongo["agents"].update_one(
        {"_id": agent.id}, {"$set": {f"exposure.{k}": v for k, v in exposure.items()}}
    )
    fake, k8s = agent.fake, agent.doc["k8s"]

    def env() -> dict[str, str]:
        dep = fake.get("apps/v1", "Deployment", k8s["name"], k8s["namespace"])
        return env_map(dep["spec"]["template"]["spec"]["containers"][0])

    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "platform")) as app:
        skip_start_check(app.app)
        assert agent.id in run_data_plane_auth(app)["reapplied"]
        alone = env()
        assert alone["DAEGAM_OIDC_ISSUER"] == OIDC_ISSUER and "DAEGAM_IDP_ISSUER" not in alone
        assert agent.id not in run_data_plane_auth(app)["reapplied"]
        record = AgentRuntime(image=IMAGE, features=[TWO_ISSUERS])
        assert app.portal.call(app.app.state.agents.record_runtime, agent.id, record)
        assert agent.id in run_data_plane_auth(app)["reapplied"]
        both = env()
        assert both["DAEGAM_OIDC_ISSUER"] == ISSUER and both["DAEGAM_IDP_ISSUER"] == OIDC_ISSUER
        assert agent.id not in run_data_plane_auth(app)["reapplied"]


# -- 전환을 막는 mcp_group (6.10절) ----------------------------------------------------------------


def test_a_stored_daegam_mcp_group_keeps_the_data_plane_on_the_idp(
    client, settings, fake_clusters, fake_secrets, mongo
):
    """다른 테넌트의 고정 그룹을 mcp_group 에 둔 테넌트가 있으면 platform 을 청해도 기동이
    데이터 플레인을 idp 로 남긴다. 렌더는 IdP 를 믿을 때 그대로이고, 화면과 상태가 그것을 말한다."""
    front, gateway, fake = front_and_gateway(client, fake_clusters)
    mongo["tenant_settings"].insert_one(
        {"_id": "default", "tenant": "default", "mcp_group": "daegam-other"}
    )
    with api_client(_oidc_app(settings, fake_clusters, fake_secrets, "platform")) as app:
        run_data_plane_auth(app)
        assert app.get("/auth/config").json()["data_plane_issuer"] == "idp"
        issuer = app.portal.call(app.app.state.data_plane_auth.status)["data_plane_issuer"]
    inline = fake.get(TOOLHIVE_API, "MCPOIDCConfig", OIDC_CONFIG_NAME, DEFAULT_NS)["spec"]["inline"]
    assert inline["issuer"] == OIDC_ISSUER
    policies = cedar_policies(vmcp_of(fake, gateway["k8s"]["name"]), fake=fake)
    assert group_policy("daegam-other") in policies
    assert (issuer["setting"], issuer["trusted"], issuer["error"]) == (
        "platform",
        "idp",
        "foreign_mcp_group",
    )
    assert issuer["tenants_foreign_mcp_group"] == [
        {"tenant": "default", "mcp_group": "daegam-other"}
    ]


def test_while_the_platform_is_trusted_mcp_group_cannot_take_a_daegam_name(
    oidc_client, oidc_headers, monkeypatch
):
    """저장하면 다음 기동이 데이터 플레인을 idp 로 남기므로 플랫폼을 믿는 동안은 받지 않는다. 자기
    고정 그룹과 `daegam-` 가 아닌 이름은 받는다. 라우트는 요청 때의 설정을 읽는다."""

    def patch(group: str) -> int:
        body = {"mcp_group": group}
        r = oidc_client.patch(f"/tenants/{TENANT}/settings", json=body, headers=oidc_headers)
        return r.status_code

    assert patch("daegam-other") == 200
    trusted = replace(oidc_client.app.state.settings, data_plane_issuer="platform")
    monkeypatch.setattr(oidc_client.app.state, "settings", trusted)
    assert [patch(g) for g in ("daegam-other", f"daegam-{TENANT}", "team-x")] == [422, 200, 200]
