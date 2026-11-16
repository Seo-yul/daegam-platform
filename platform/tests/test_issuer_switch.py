"""데이터 플레인 발급자 전환의 상태와 준비 확인 (P53b 6.2·6.9·6.10절).

플랫폼을 믿는 oidc 모드에서 리컨실 단계가 daegam-oidc 를 쓰는 vMCP·노출 앞단과 두 발급자 이미지의
노출 Agent 를 플랫폼 토큰으로 불러 보고, 401 이 아닌 것만 확인된 것으로 센다. 파드가 바뀌면 다시
부르고, 주소가 없는 것은 기다린다. 하나라도 거절하거나, 다시 렌더를 기다리는 문서(막힌 것 포함)가
있거나, 아직 확인하지 못한 클러스터가 있으면 pending 이 0 보다 크다. IdP 로 되돌리면 기록을 비운다.
다른 테넌트의 고정 그룹을 mcp_group 에 둔 테넌트가 있으면 기동이 idp 로 남긴다. 상태는 전환이
바꾸거나 열어 두는 것을 두 값에서 모두 늘어놓고, oidc 의 Gateway 는 소유자 Cedar 가 IdP 이름을 쓰면
경고한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from daegam.api.schemas.tenancy import DataPlaneAuthOut, DataPlaneIssuerOut
from daegam.config import Settings
from daegam.k8s.labels import LABEL_APP_INSTANCE, LABEL_APP_NAME
from daegam.services import issuer_switch as switch_mod
from daegam.services.agent_features import TWO_ISSUERS
from daegam.services.data_plane_auth import Behind
from daegam.services.gateway_status import IDP_NAMES_IN_POLICY_WARNING, GatewayStatusView
from daegam.services.issuer_switch import (
    IssuerSwitch,
    MongoIssuerSwitchStore,
    foreign_platform_groups,
    held_settings,
    names_idp,
    owner_policies,
)
from daegam.services.tenancy import OIDC_CONFIG_NAME, TOOLHIVE_API, oidc_config_manifest
from daegam.store.agents import AgentRevision, AgentSpec
from daegam.store.base import utcnow
from daegam.store.tenants import TenantSettings
from tests.helpers.agents import template_spec
from tests.helpers.consts import ISSUER, OIDC_AUDIENCE, OIDC_ISSUER
from tests.helpers.docs import gateway_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.gateways import backend_snapshot
from tests.helpers.http import api_client, make_app

IDP = Settings(
    auth_mode="oidc",
    oidc_issuer=OIDC_ISSUER,
    oidc_audience=OIDC_AUDIENCE,
    platform_issuer=ISSUER,
    data_plane_issuer="idp",
)
PLATFORM = replace(IDP, data_plane_issuer="platform")
INSTALL = "install-1"
NS = "ns"
VMCP_URL = "http://vmcp-gw.ns.svc.cluster.local:4483/mcp"
PROXY_URL = "http://expose-calc.ns.svc.cluster.local:8080/mcp"
AGENT_URL = "http://agent-a.ns.svc.cluster.local:8080/metrics"


# -- 정책 글 (순수) -----------------------------------------------------------------------------


def _policy(text: str) -> Any:
    return gateway_doc(spec={"authz_policy": text}, state="deployed")


@pytest.mark.parametrize(
    ("when", "groups_claim", "client_id_claim", "named"),
    [
        ('principal in THVGroup::"eng"', "groups", "cid", True),
        ('principal in THVGroup::"daegam-t"', "groups", "cid", False),
        ('principal.claim_groups.contains("eng")', "groups", "cid", True),
        (
            'principal.claim_groups.containsAny(["daegam-t", "daegam-group:t:eng"])',
            "groups",
            "cid",
            False,
        ),
        ('principal.claim_roles.contains("ops")', "roles", "cid", True),
        ('principal.claim_azp == "bot"', "groups", "azp", True),
        ('principal.claim_cid == "bot"', "groups", "azp", True),
        ('principal.claim_cidr == "x"', "groups", "cid", False),
    ],
)
def test_owner_cedar_names_idp_by_group_groups_claim_or_client_id(
    when, groups_claim, client_id_claim, named
):
    """플랫폼 토큰은 IdP 그룹을 테넌트 이름을 붙인 꼴로만 groups 에 싣고 client id 와 IdP 의 그룹
    클레임은 싣지 않는다 (6.6절). 그 밖의 이름으로 가리는 정책은 전환 뒤 맞지 않는다."""
    gw = _policy(f"permit(principal, action, resource) when {{ {when} }};")
    if when.startswith("principal in"):
        gw = _policy(f"permit({when}, action, resource);")
    assert names_idp(gw, groups_claim, client_id_claim) is named


def test_owner_cedar_is_the_authz_policy_and_saved_incoming_auth():
    policy = 'permit(principal in THVGroup::"eng", action, resource);'
    gw = _policy(policy)
    assert owner_policies(gw) == [policy]
    override = 'permit(principal, action, resource) when { principal.claim_cid == "bot" };'
    gw.overrides = {"spec": {"incomingAuth": {"authzConfig": {"inline": {"policies": [override]}}}}}
    assert owner_policies(gw) == [policy, override]


def test_foreign_platform_groups_are_other_tenants_platform_names():
    groups = [
        "eng",
        "daegam-t",
        "daegam-group:t:eng",
        "daegam-role:t:viewer",
        "daegam-other",
        "daegam-group:other:eng",
        "daegam-role:other:viewer",
    ]
    assert foreign_platform_groups("t", groups) == [
        "daegam-other",
        "daegam-group:other:eng",
        "daegam-role:other:viewer",
    ]


# -- 준비 확인 (가짜 클러스터) --------------------------------------------------------------------


class _Coll:
    """`auth_reapply` 컬렉션의 두 호출만 흉내 낸다."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        doc = self.docs.get(query["_id"])
        return None if doc is None else {"_id": query["_id"], **doc}

    async def replace_one(self, query: dict[str, Any], doc: dict[str, Any], upsert: bool) -> None:
        self.docs[query["_id"]] = dict(doc)


class _TenantSettings:
    """`MongoTenantSettingsStore` 의 두 조회만 흉내 낸다. 그 조회 자체는 Mongo 테스트가 본다."""

    def __init__(self, rows: dict[str, TenantSettings]) -> None:
        self.rows = rows

    async def foreign_mcp_groups(self) -> dict[str, str]:
        return {
            t: r.mcp_group
            for t, r in self.rows.items()
            if r.mcp_group and r.mcp_group.startswith("daegam-") and r.mcp_group != f"daegam-{t}"
        }

    async def with_agent_client_ids(self) -> list[str]:
        return sorted(t for t, r in self.rows.items() if r.agent_client_ids)


def _agent(name: str, *, two_issuers: bool = True, groups: list[str] | None = None) -> Any:
    return SimpleNamespace(
        id=f"ag-{name}",
        name=name,
        tenant="t",
        cluster_id="c1",
        state="deployed",
        k8s=SimpleNamespace(name=f"agent-{name}", namespace=NS),
        exposure=SimpleNamespace(enabled=True, allowed_groups=groups or []),
        deployed=None,
        image_reports=lambda feature: two_issuers and feature == TWO_ISSUERS,
    )


def _pod(name: str) -> dict:
    """vMCP gw 의 파드. 가짜 클러스터가 uid 를 매긴다."""
    labels = {LABEL_APP_NAME: "virtualmcpserver", LABEL_APP_INSTANCE: "gw"}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NS, "labels": labels},
    }


def _vmcp(url: str | None = "http://vmcp-gw.ns.svc.cluster.local:4483") -> dict:
    ref = {"name": OIDC_CONFIG_NAME, "audience": "daegam-mcp"}
    return {
        "apiVersion": TOOLHIVE_API,
        "kind": "VirtualMCPServer",
        "metadata": {"name": "gw", "namespace": NS, "uid": "u-gw"},
        "spec": {"incomingAuth": {"oidcConfigRef": ref}},
        "status": {"url": url} if url else {},
    }


def _cluster_fake(issuer: str) -> FakeClusterClient:
    fake = FakeClusterClient()
    fake.apply(oidc_config_manifest("t", NS, issuer, None, install_id=INSTALL))
    ref = {"name": OIDC_CONFIG_NAME, "audience": "daegam-mcp"}
    fake.apply(_vmcp())
    fake.apply(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPRemoteProxy",
            "metadata": {"name": "expose-calc", "namespace": NS, "uid": "u-px"},
            "spec": {"oidcConfigRef": ref},
            "status": {"url": PROXY_URL},
        }
    )
    return fake


def _switch(
    settings: Settings,
    fake: FakeClusterClient,
    coll: _Coll,
    agents: list[Any],
    tenants: dict[str, TenantSettings] | None = None,
) -> IssuerSwitch:
    cluster = SimpleNamespace(id="c1", name="edge", is_home=True)

    async def install_id() -> str:
        return INSTALL

    async def list_clusters() -> list[Any]:
        return [cluster]

    async def live_on_cluster(_cid: str) -> list[Any]:
        return agents

    async def gateway_token(tenant: str) -> str:
        return f"platform-{tenant}"

    tenancy = SimpleNamespace(
        platform_settings=SimpleNamespace(install_id=install_id),
        clusters=SimpleNamespace(list=list_clusters),
        cluster_service=SimpleNamespace(client_for=lambda _c: fake),
        tenant_settings=_TenantSettings(
            tenants or {"t": TenantSettings(tenant="t", agent_client_ids=["bot"])}
        ),
    )
    tokens = SimpleNamespace(available=True, platform_gateway_token=gateway_token)
    return IssuerSwitch(
        tenancy,  # type: ignore[arg-type]
        tokens,  # type: ignore[arg-type]
        settings,
        MongoIssuerSwitchStore(coll),
        agents=SimpleNamespace(live_on_cluster=live_on_cluster),
    )


@pytest.fixture
def answers(monkeypatch) -> dict[str, Any]:
    """주소마다의 답(상태 코드). 없는 주소는 부르지 못한 것(None)이다. 부른 주소와 토큰을 남긴다.
    `jwks` 가 거짓이면 플랫폼 JWKS 를 받지 못하는 틱이다."""
    state: dict[str, Any] = {"calls": [], "jwks": True}

    async def probe(_client, url: str, bearer: str):
        state["calls"].append((url, bearer))
        status = state.get(url)
        return None if status is None else (status, "")

    async def served(_url: str) -> bool:
        return state["jwks"]

    monkeypatch.setattr(switch_mod, "probe_verifier", probe)
    monkeypatch.setattr(switch_mod, "jwks_served", served)
    return state


def _status(switch: IssuerSwitch, now, behind: list[Behind] | None = None) -> dict[str, Any]:
    clusters = [SimpleNamespace(id="c1", name="edge", is_home=True)]
    out = asyncio.run(switch.status(clusters, {"c1": []}, {"c1": behind or []}, now))  # type: ignore[arg-type]
    assert out is None or DataPlaneIssuerOut.model_validate(out)
    return out


def test_pending_stays_above_zero_until_every_verifier_takes_a_platform_token(answers):
    fake, coll = _cluster_fake(ISSUER), _Coll()
    agents = [_agent("a"), _agent("old", two_issuers=False)]
    switch = _switch(PLATFORM, fake, coll, agents)
    now = utcnow()
    # 아직 확인하지 못한 클러스터도 pending 이다
    assert _status(switch, now)["pending"] == 1
    answers.update({VMCP_URL: 401, PROXY_URL: 200, AGENT_URL: 401})
    result = asyncio.run(switch.probe(now))
    assert sorted(result["refused"]) == [
        f"edge/{NS}/Agent/agent-a",
        f"edge/{NS}/VirtualMCPServer/gw",
    ]
    assert result["passed"] == [f"edge/{NS}/MCPRemoteProxy/expose-calc"]
    # 두 발급자를 모르는 이미지의 노출 Agent 는 부르지 않는다. 토큰은 테넌트의 플랫폼 토큰이다
    assert {url for url, _ in answers["calls"]} == {VMCP_URL, PROXY_URL, AGENT_URL}
    assert {bearer for _, bearer in answers["calls"]} == {"platform-t"}
    status = _status(switch, now)
    assert status["setting"] == "platform" and status["pending"] == 2
    (row,) = status["clusters"]
    assert row["platform_namespaces"] == [NS] and row["idp_namespaces"] == []
    waiting = {w["name"]: w for w in row["waiting_verifiers"]}
    assert set(waiting) == {"gw", "agent-a"} and waiting["gw"]["refused"]
    # 확인된 검증기는 파드가 그대로인 동안 다시 부르지 않는다. 모두 지나면 0 이다
    answers["calls"].clear()
    answers.update({VMCP_URL: 200, AGENT_URL: 403})
    asyncio.run(switch.probe(now + timedelta(minutes=1)))
    assert {url for url, _ in answers["calls"]} == {VMCP_URL, AGENT_URL}
    status = _status(switch, now)
    assert status["pending"] == 0
    verified = {(v["kind"], v["name"]) for v in status["clusters"][0]["verified_verifiers"]}
    assert verified == {
        ("VirtualMCPServer", "gw"),
        ("MCPRemoteProxy", "expose-calc"),
        ("Agent", "agent-a"),
    }


def test_documents_waiting_for_their_re_render_keep_the_switch_pending_even_when_blocked(answers):
    """요청 오류로 막힌 소비자도 옛 렌더로 돌므로 전환은 끝나지 않았다."""
    fake, coll = _cluster_fake(ISSUER), _Coll()
    switch = _switch(PLATFORM, fake, coll, [])
    answers.update({VMCP_URL: 200, PROXY_URL: 200})
    asyncio.run(switch.probe(utcnow()))
    assert _status(switch, utcnow())["pending"] == 0
    doc = SimpleNamespace(id="g1", name="g", tenant="t", cluster_id="c1")
    out = _status(switch, utcnow(), [Behind("gateway", doc), Behind("gateway", doc, blocked=True)])
    assert out["pending"] == 2
    assert [d["blocked"] for d in out["clusters"][0]["pending_documents"]] == [False, True]


def test_a_restarted_verifier_is_probed_again(answers):
    """확인은 그때의 파드에 대한 것이다. 파드가 바뀌면 다시 부를 때까지 확인된 것에서 빠진다."""
    fake, coll = _cluster_fake(ISSUER), _Coll()
    fake.apply(_pod("gw-0"))
    switch = _switch(PLATFORM, fake, coll, [])
    answers.update({VMCP_URL: 200, PROXY_URL: 200})
    asyncio.run(switch.probe(utcnow()))
    answers["calls"].clear()
    asyncio.run(switch.probe(utcnow()))
    assert answers["calls"] == []
    fake.apply(_pod("gw-1"))
    fake.delete("v1", "Pod", "gw-0", NS)
    answers[VMCP_URL] = 401
    asyncio.run(switch.probe(utcnow()))
    assert [url for url, _ in answers["calls"]] == [VMCP_URL]
    (row,) = _status(switch, utcnow())["clusters"]
    assert [v["name"] for v in row["verified_verifiers"]] == ["expose-calc"]
    assert [w["name"] for w in row["waiting_verifiers"]] == ["gw"]


def test_a_verifier_without_an_address_waits_and_no_tick_probes_while_the_jwks_is_not_served(
    answers,
):
    fake, coll = _cluster_fake(ISSUER), _Coll()
    fake.apply(_vmcp(url=None))
    switch = _switch(PLATFORM, fake, coll, [])
    answers.update({PROXY_URL: 200, "jwks": False})
    assert asyncio.run(switch.probe(utcnow()))["skipped"] == ["jwks"]
    assert answers["calls"] == [] and coll.docs == {}
    answers["jwks"] = True
    asyncio.run(switch.probe(utcnow()))
    assert [url for url, _ in answers["calls"]] == [PROXY_URL]
    status = _status(switch, utcnow())
    (waiting,) = status["clusters"][0]["waiting_verifiers"]
    assert waiting["name"] == "gw" and not waiting["refused"] and status["pending"] == 1


def test_a_verifier_that_cannot_be_reached_waits_without_a_refusal(answers):
    fake, coll = _cluster_fake(ISSUER), _Coll()
    switch = _switch(PLATFORM, fake, coll, [])
    answers.update({PROXY_URL: 200})
    asyncio.run(switch.probe(utcnow()))
    (row,) = _status(switch, utcnow())["clusters"]
    (waiting,) = row["waiting_verifiers"]
    assert waiting["name"] == "gw" and not waiting["refused"] and waiting["last_at"] is None


def test_switching_back_clears_the_record_and_a_new_switch_probes_again(answers):
    fake, coll = _cluster_fake(ISSUER), _Coll()
    answers.update({VMCP_URL: 200, PROXY_URL: 200})
    asyncio.run(_switch(PLATFORM, fake, coll, []).probe(utcnow()))
    assert _status(_switch(PLATFORM, fake, coll, []), utcnow())["pending"] == 0
    idp = _switch(IDP, fake, coll, [])
    assert asyncio.run(idp.probe(utcnow())) == {}
    status = _status(idp, utcnow())
    assert status["setting"] == "idp" and status["trusted"] == "idp" and status["error"] is None
    assert status["pending"] == 0 and status["clusters"] == []
    assert coll.docs["issuer_switch"] == {"issuer": "idp", "clusters": {}}
    answers["calls"].clear()
    again = _switch(PLATFORM, fake, coll, [])
    assert _status(again, utcnow())["pending"] == 1
    asyncio.run(again.probe(utcnow()))
    assert {url for url, _ in answers["calls"]} == {VMCP_URL, PROXY_URL}


def test_namespaces_still_on_the_idp_are_listed(answers):
    fake, coll = _cluster_fake(OIDC_ISSUER), _Coll()
    switch = _switch(PLATFORM, fake, coll, [])
    asyncio.run(switch.probe(utcnow()))
    (row,) = _status(switch, utcnow())["clusters"]
    assert row["idp_namespaces"] == [NS] and row["platform_namespaces"] == []


def test_outside_oidc_mode_there_is_no_switch(answers):
    local = Settings(auth_mode="local", platform_issuer=ISSUER, data_plane_issuer="platform")
    switch = _switch(local, _cluster_fake(ISSUER), _Coll(), [])
    assert asyncio.run(switch.probe(utcnow())) == {}
    assert _status(switch, utcnow()) is None and answers["calls"] == []


def test_the_status_lists_what_the_switch_changes_or_leaves_open_under_both_values(answers):
    policy = 'permit(principal in THVGroup::"eng", action, resource);'
    gw = gateway_doc(spec={"authz_policy": policy}, state="deployed", tenant="t")
    # 플랫폼을 믿을 때 배포한 게시된 백엔드는 IdP 를 믿는 동안 이 Gateway 를 거절한다 (P57 6.1절)
    gw.deployed_backends = [backend_snapshot("pub", "published", ["Add"])]
    spec = AgentSpec.model_validate(template_spec(source_id="gw-1"))
    source = _agent("src")
    source.exposure = SimpleNamespace(enabled=False, allowed_groups=[])
    source.deployed = AgentRevision(revision=1, **spec.model_dump())
    docs = {
        "c1": [
            ("gateway", gw),
            ("agent", _agent("open")),
            ("agent", _agent("foreign", groups=["daegam-other", "eng"])),
            ("agent", _agent("old", two_issuers=False, groups=["eng"])),
            ("agent", source),
        ]
    }
    clusters = [SimpleNamespace(id="c1", name="edge", is_home=True)]
    for settings in (IDP, PLATFORM):
        switch = _switch(settings, _cluster_fake(ISSUER), _Coll(), [])
        out = asyncio.run(switch.status(clusters, docs, {"c1": []}, utcnow()))  # type: ignore[arg-type]
        assert [r["id"] for r in out["gateways_idp_names"]] == [gw.id]
        assert [r["id"] for r in out["gateways_permit"]] == [gw.id]
        assert [r["name"] for r in out["exposed_agents_open"]] == ["open"]
        (foreign,) = out["exposed_agents_foreign_groups"]
        assert foreign["name"] == "foreign" and foreign["groups"] == ["daegam-other"]
        assert [r["name"] for r in out["exposed_agents_one_issuer"]] == ["old"]
        assert out["tenants_agent_client_ids"] == ["t"]
        assert out["jwks_unreachable_clusters"] == []
        assert [r["name"] for r in out["agents_gateway_sources"]] == ["src"]
        refused = [r["id"] for r in out["gateways_published_refused"]]
        assert refused == ([gw.id] if settings is IDP else [])


def test_remote_clusters_cannot_reach_a_cluster_local_platform_issuer(answers):
    switch = _switch(PLATFORM, _cluster_fake(ISSUER), _Coll(), [])
    clusters = [
        SimpleNamespace(id="c1", name="edge", is_home=True),
        SimpleNamespace(id="c2", name="far", is_home=False),
    ]
    out = asyncio.run(switch.status(clusters, {}, {}, utcnow()))  # type: ignore[arg-type]
    assert out["jwks_unreachable_clusters"] == ["c2"]
    public = _switch(
        replace(PLATFORM, platform_issuer="https://daegam.example"),
        _cluster_fake(ISSUER),
        _Coll(),
        [],
    )
    assert asyncio.run(public.status(clusters, {}, {}, utcnow()))["jwks_unreachable_clusters"] == []  # type: ignore[arg-type]


def test_the_idp_token_life_warns_when_it_reaches_the_interactive_bound_minus_a_turn_token():
    class People:
        def __init__(self, life: int | None) -> None:
            self.life = life

        async def largest_token_life(self, since):
            return self.life

    switch = _switch(PLATFORM, _cluster_fake(ISSUER), _Coll(), [])
    limit = PLATFORM.turn_owner_confirm_seconds - PLATFORM.turn_token_ttl_seconds
    for life, warn in ((None, False), (limit - 1, False), (limit, True)):
        switch.people = People(life)  # type: ignore[assignment]
        got = _status(switch, utcnow())["idp_token_life"]
        assert got == {"largest_seconds": life, "limit_seconds": limit, "warn": warn}


# -- Gateway 경고 -------------------------------------------------------------------------------


def test_an_oidc_gateway_whose_policy_names_idp_names_warns():
    policy = 'permit(principal in THVGroup::"eng", action, resource);'
    doc = gateway_doc(spec={"authz_policy": policy}, state="deployed")

    async def no_warnings(_doc):
        return []

    async def get_many_unscoped(_ids):
        return {}

    async def none_refused():
        return frozenset()

    def view(idp_names) -> GatewayStatusView:
        return GatewayStatusView(
            hub_items=SimpleNamespace(get_many_unscoped=get_many_unscoped),  # type: ignore[arg-type]
            token_auth=lambda: True,
            turn_tokens_on=lambda: False,
            pinned=lambda d: d.deployed,
            k8s=lambda d: d.k8s,
            with_pod_waiting=None,  # type: ignore[arg-type]
            web_warnings=no_warnings,
            refused_digests=none_refused,
            idp_names=idp_names,
        )

    got = asyncio.run(view(lambda d: names_idp(d, "groups", "cid")).fields(doc, utcnow()))
    assert IDP_NAMES_IN_POLICY_WARNING in got["warnings"]
    got = asyncio.run(view(lambda _d: False).fields(doc, utcnow()))
    assert IDP_NAMES_IN_POLICY_WARNING not in got["warnings"]


def test_the_gateway_service_warns_in_oidc_mode_under_both_values(client, monkeypatch):
    service = client.app.state.gateway_service
    policy = 'permit(principal in THVGroup::"eng", action, resource);'
    doc = gateway_doc(spec={"authz_policy": policy}, state="deployed")

    def warns() -> bool:
        got = asyncio.run(service.status_view.fields(doc, utcnow()))
        return IDP_NAMES_IN_POLICY_WARNING in got["warnings"]

    assert not warns()  # header 모드
    for settings in (IDP, PLATFORM):
        monkeypatch.setattr(service.cluster_service, "settings", settings)
        assert warns()


def test_the_status_api_answers_the_switch_in_oidc_mode_only(
    client, settings, fake_clusters, fake_secrets, mongo
):
    """`GET /settings/data-plane-auth` 의 data_plane_issuer 는 oidc 모드에서만 있고 응답 모델을
    지난다. header 모드는 null 이다."""
    assert client.portal.call(client.app.state.data_plane_auth.status)["data_plane_issuer"] is None
    app = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        auth_mode="oidc",
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=OIDC_AUDIENCE,
        oidc_insecure_http=True,
        platform_issuer=ISSUER,
        data_plane_issuer="platform",
    )
    with api_client(app) as oidc:
        body = oidc.portal.call(oidc.app.state.data_plane_auth.status)
    out = DataPlaneAuthOut.model_validate(body).data_plane_issuer
    assert out is not None and out.setting == "platform"
    assert out.idp_token_life.limit_seconds == 4500 - 300


# -- 기동의 막음 (6.10절) -----------------------------------------------------------------------


FOREIGN = {"t": TenantSettings(tenant="t", mcp_group="daegam-other")}


@pytest.mark.parametrize(
    ("group", "held"),
    [("daegam-other", True), ("daegam-x-y", True), ("daegam-t", False), ("team-t", False)],
)
def test_a_stored_daegam_mcp_group_holds_the_data_plane_at_idp(group, held):
    tenants = _TenantSettings({"t": TenantSettings(tenant="t", mcp_group=group)})
    running = asyncio.run(held_settings(PLATFORM, tenants))  # type: ignore[arg-type]
    assert running.data_plane_issuer == "platform"
    assert running.oidc_trusts_platform is not held and running.data_plane_trusts_idp is held
    assert running.data_plane_issuer_held_by == (("t",) if held else ())
    # idp 와 local 은 보지 않는다
    for settings in (IDP, Settings(auth_mode="local", platform_issuer=ISSUER)):
        assert asyncio.run(held_settings(settings, tenants)) is settings  # type: ignore[arg-type]


def test_the_status_names_the_tenants_that_hold_the_switch(answers):
    running = asyncio.run(held_settings(PLATFORM, _TenantSettings(FOREIGN)))  # type: ignore[arg-type]
    switch = _switch(running, _cluster_fake(ISSUER), _Coll(), [], FOREIGN)
    assert asyncio.run(switch.probe(utcnow())) == {} and answers["calls"] == []
    status = _status(switch, utcnow())
    assert (status["setting"], status["trusted"], status["error"]) == (
        "platform",
        "idp",
        "foreign_mcp_group",
    )
    assert status["clusters"] == [] and status["pending"] == 0
    assert status["tenants_foreign_mcp_group"] == [{"tenant": "t", "mcp_group": "daegam-other"}]
    # IdP 를 믿는 동안에도 전환 전에 고치라고 늘어놓는다
    idp = _status(_switch(IDP, _cluster_fake(ISSUER), _Coll(), [], FOREIGN), utcnow())
    assert idp["error"] is None and idp["tenants_foreign_mcp_group"] == [
        {"tenant": "t", "mcp_group": "daegam-other"}
    ]
