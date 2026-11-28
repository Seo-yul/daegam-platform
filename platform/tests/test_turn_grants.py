"""턴 위임과 그 토큰 (P53 1·2·4절, `services/turn_grants.py`).

위임은 규칙이 허락하는 Tool 대상마다 사람(`sub`)과 Agent(`act`)를 싣고 그 대상 하나에 묶인 토큰을
만든다. 서비스 시험은 위임 저장소만 Mongo 이고 나머지 저장소와 게이트는 그 자리의 가짜다. 기계 경로
시험은 header 모드 플랫폼에 배포한 Agent 의 인스턴스 토큰으로 부른다.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import jwt
import pytest
from pymongo.errors import PyMongoError

from daegam.auth.oidc import AuthError
from daegam.auth.rbac import ALL_TENANTS
from daegam.auth.service_token import InternalIssuer, verify_agent_call_token
from daegam.auth.signer import PlatformSigner, agent_call_audience, server_audience
from daegam.config import Settings, settings_from_env
from daegam.errors import ServiceError
from daegam.k8s.gates import pack_epoch
from daegam.services.access_revocation import AccessRevoker
from daegam.services.agent_features import TURN_TOKENS
from daegam.services.assistant.catalog import CATALOG
from daegam.services.data_plane_auth import KIND_GATEWAY, RENDER_EPOCHS
from daegam.services.turn_gates import TurnGates
from daegam.services.turn_grants import (
    CONFIRM_BOUND,
    END_REFUSED,
    REQUEST_BURST,
    GrantPerson,
    TurnGrantError,
    TurnGrantService,
    TurnLimits,
    TurnTokensUnavailable,
    turn_limits,
)
from daegam.store.agent_instance_tokens import AgentInstanceToken, scope_for
from daegam.store.agents import AgentCallPin, AgentRuntime, ToolPin
from daegam.store.auth_reapply import AudienceSwitch, ClusterTurnGates
from daegam.store.base import hash_token, utcnow
from daegam.store.memberships import Membership
from daegam.store.turn_grants import TURN_GRANTS, GrantTarget, MongoTurnGrantStore
from daegam.store.users import User
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import node_of, template_spec
from tests.helpers.aio import run_store
from tests.helpers.auth import bearer
from tests.helpers.consts import ISSUER
from tests.helpers.docs import (
    TENANT,
    agent_doc,
    cluster_doc,
    effective_config,
    gateway_doc,
    mcp_server_doc,
)
from tests.helpers.runs import FakePeople, idp_person

AGENT_ID = "ag-1"
INSTANCE_TOKEN = "instance-token-1"
PERSON = "user-alice"
PUBLIC = "https://daegam.example"
AGENT_IMAGE = effective_config().images.agent_image
SPLIT = "turn-grants"


# -- 가짜 저장소와 게이트 ------------------------------------------------------------------------


class _Docs:
    """id → 문서. tenant 를 주면 그 테넌트의 문서만 찾는다(`MongoInstanceStore.get`)."""

    def __init__(self, *docs: Any):
        self.docs = {d.id: d for d in docs}

    def with_previews(self) -> _Docs:
        """`MongoAgentStore.with_previews` 의 자리다. 이 가짜는 미리 보기를 가르지 않는다."""
        return self

    async def get(self, doc_id: str, tenant: str | None = None) -> Any:
        doc = self.docs.get(doc_id)
        if doc is None or (tenant is not None and doc.tenant != tenant):
            return None
        return doc


class _Users:
    def __init__(self, *users: User):
        self.users = {u.id: u for u in users}

    async def get(self, user_id: str) -> User | None:
        return self.users.get(user_id)


class _Memberships:
    def __init__(self, rows: dict[str, list[str]]):
        self.rows = rows

    async def list_for_sub(self, sub: str) -> list[Membership]:
        return [Membership(sub=sub, tenant=t, role="developer") for t in self.rows.get(sub, [])]


class _InstanceTokens:
    """Agent 마다 살아 있는 인스턴스 토큰 하나. scope 는 배포된 리비전 1 이다."""

    def __init__(self, *agent_ids: str):
        self.agent_ids = agent_ids

    async def list(self, instance_id: str, *, active_only: bool = False) -> list:
        if instance_id not in self.agent_ids:
            return []
        token = AgentInstanceToken(
            agent_instance_id=instance_id,
            agent_profile_id=instance_id,
            token_sha256=hash_token(f"{INSTANCE_TOKEN}:{instance_id}"),
            scope=[scope_for(instance_id, 1)],
        )
        return [token]


class _Gates(TurnGates):
    """재적용 틱이 적은 판정 대신 시험이 준 판정이다. 신선도는 진짜 규칙이다."""

    def __init__(self, verdict: ClusterTurnGates | None, settings: Settings):
        super().__init__(None, settings)  # type: ignore[arg-type]
        self.verdict = verdict

    async def cluster(self, cluster_id: str) -> ClusterTurnGates | None:
        return self.verdict if self.verdict and self.verdict.cluster_id == cluster_id else None


def _verdict(
    *, open_: bool = True, age: timedelta = timedelta(0), local: bool = True
) -> ClusterTurnGates:
    return ClusterTurnGates(
        cluster_id="c1",
        version_ok=True,
        gateway_gate_open=open_,
        verdict_at=utcnow() - age,
        local=local,
        holders=[],
    )


def _spec(*, turn_timeout: int = 600, session_ttl: int = 7200) -> dict[str, Any]:
    """Gateway gw-1 과 MCP Server mcp-1 을 Tool 대상으로 둔 템플릿 spec."""
    spec = template_spec(source_id="gw-1")
    node_of(spec, "agent-main")["data"]["turn_timeout_seconds"] = turn_timeout
    node_of(spec, "entry")["data"]["session_ttl_seconds"] = session_ttl
    graph = spec["graph"]
    graph["nodes"].append(
        {
            "id": "tools-mcp",
            "type": "tools",
            "data": {"source": {"kind": "mcp_instance", "id": "mcp-1"}, "allow_tools": ["x"]},
        }
    )
    graph["edges"].append(
        {"id": "e-mcp", "source": "agent-main", "target": "tools-mcp", "kind": "tools"}
    )
    return spec


def _agent(aid: str = AGENT_ID, *, features: tuple[str, ...] = (TURN_TOKENS,), **kw: Any):
    return agent_doc(
        aid,
        spec=kw.pop("spec", None) or _spec(),
        state=kw.pop("state", "deployed"),
        effective_config=effective_config().model_dump(),
        runtime=AgentRuntime(image=AGENT_IMAGE, features=list(features)),
        **kw,
    )


# 묶임을 싣는 렌더의 번호. local 모드의 Gateway 번호 3 에 묶임의 표시를 단 것이다
BOUND_EPOCH = pack_epoch(RENDER_EPOCHS[KIND_GATEWAY], bound=True)


def _gateway(**kw: Any):
    """묶임을 싣는 렌더로 배포된 Gateway 다."""
    kw.setdefault("render_epoch", BOUND_EPOCH)
    return gateway_doc("gw-1", state="deployed", **kw)


def _server(*, applied: bool = True, **kw: Any):
    now = utcnow()
    switch = AudienceSwitch(switched_at=now, applied_at=now if applied else None)
    return mcp_server_doc("mcp-1", state="deployed", audience_switch=switch, **kw)


def _user(tv: int = 3, **kw: Any) -> User:
    return User(id=PERSON, username="alice", password_hash="x", token_version=tv, **kw)


@dataclasses.dataclass
class World:
    """서비스 하나를 짓는 재료. 시험이 고친 뒤 `build` 한다."""

    settings: Settings
    agents: list[Any] = dataclasses.field(default_factory=lambda: [_agent()])
    gateways: list[Any] = dataclasses.field(default_factory=lambda: [_gateway()])
    servers: list[Any] = dataclasses.field(default_factory=lambda: [_server()])
    clusters: list[Any] = dataclasses.field(
        default_factory=lambda: [cluster_doc("edge", id="c1", recorded_toolhive_version="0.53.0")]
    )
    users: list[User] = dataclasses.field(default_factory=lambda: [_user()])
    # oidc 모드의 사람 기록. 기본은 방금 IdP 가 보증한 사람이다
    people: list[Any] = dataclasses.field(default_factory=lambda: [idp_person(PERSON)])
    rows: dict[str, list[str]] = dataclasses.field(default_factory=lambda: {PERSON: [TENANT]})
    verdict: ClusterTurnGates | None = dataclasses.field(default_factory=_verdict)
    signed: bool = True
    clock: Callable[[], float] = lambda: 1000.0

    def build(
        self, store: MongoTurnGrantStore, issuer: InternalIssuer | None = None
    ) -> TurnGrantService:
        if issuer is None:
            issuer = _issuer(self.settings)
            if self.signed:
                issuer.attach(SIGNER)
        # 사람의 판정은 접근 정리의 것이다. 이 시험이 쓰는 것은 사람과 소속을 읽는 저장소뿐이다
        revoker = AccessRevoker(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            _Users(*self.users),  # type: ignore[arg-type]
            _Memberships(self.rows),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            issuer,
            settings=self.settings,
            people=FakePeople(*self.people),  # type: ignore[arg-type]
        )
        return TurnGrantService(
            store,
            agents=_Docs(*self.agents),  # type: ignore[arg-type]
            instance_tokens=_InstanceTokens(*(a.id for a in self.agents)),  # type: ignore[arg-type]
            gateways=_Docs(*self.gateways),  # type: ignore[arg-type]
            mcp_servers=_Docs(*self.servers),  # type: ignore[arg-type]
            clusters=_Docs(*self.clusters),  # type: ignore[arg-type]
            revoker=revoker,
            gates=_Gates(self.verdict, self.settings),
            issuer=issuer,
            settings=self.settings,
            clock=self.clock,
        )


SIGNER = PlatformSigner.generate()


def _issuer(settings: Settings, *, human: str = PUBLIC) -> InternalIssuer:
    return InternalIssuer(issuer=ISSUER, human_issuer=human, mcp_audience=settings.mcp_audience)


@pytest.fixture
def turn_settings(settings: Settings) -> Settings:
    return dataclasses.replace(
        settings, auth_mode="local", platform_issuer=ISSUER, turn_tokens_enabled=True
    )


@pytest.fixture
def world(turn_settings) -> World:
    return World(turn_settings)


def _run[R](settings: Settings, fn: Callable[[MongoTurnGrantStore], Awaitable[R]]) -> R:
    """스키마를 맞춘 위임 저장소로 fn(store) 를 돈다."""
    return run_store(settings, MongoTurnGrantStore, fn)


def _person(**kw: Any) -> GrantPerson:
    return GrantPerson(**{"sub": PERSON, "username": "alice", "tv": 3, **kw})


async def _open(service: TurnGrantService, agent_id: str = AGENT_ID, **kw: Any):
    agent = await service.agents.get(agent_id)
    return await service.open(
        agent=agent,
        person=kw.pop("person", None) or _person(),
        trigger=kw.pop("trigger", "chat"),
        session_id=kw.pop("session_id", "s1"),
        **kw,
    )


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def _tokens(turn_grant: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(t["kind"], t["id"]): _claims(t["token"]) for t in turn_grant["tokens"]}


def _targets(world: World) -> set[tuple[str, str]]:
    """그 세계에서 연 위임이 토큰을 준 대상. 위임이 없으면 빈 집합이다."""

    async def go(store: MongoTurnGrantStore) -> set[tuple[str, str]]:
        outcome = await _open(world.build(store))
        return set(_tokens(outcome.grant.turn_grant)) if outcome.grant else set()

    return _run(world.settings, go)


ALL = {("gateway_instance", "gw-1"), ("mcp_instance", "mcp-1")}
# 소유자 Cedar 하나. 플랫폼의 forbid 를 갈음하는 ConfigMap 참조다 (3.1절)
CONFIGMAP_AUTHZ = {"type": "configMap", "configMap": {"name": "owner-cedar"}}


# -- 토큰의 클레임 (1절) -------------------------------------------------------------------------


def test_each_token_names_the_person_the_agent_and_one_target_and_no_groups(world):
    async def go(store: MongoTurnGrantStore) -> dict[str, Any]:
        outcome = await _open(world.build(store))
        assert outcome.grant is not None and outcome.refused is None
        return outcome.grant.turn_grant

    grant = _run(world.settings, go)
    assert set(grant) == {"id", "secret", "sub", "tokens", "expires_in", "renew_url"}
    assert grant["sub"] == PERSON and grant["expires_in"] == 300
    assert grant["renew_url"] == f"{ISSUER}/api/agents/{AGENT_ID}/turn-grants/{grant['id']}"
    tokens = _tokens(grant)
    assert set(tokens) == ALL
    gw, mcp = tokens["gateway_instance", "gw-1"], tokens["mcp_instance", "mcp-1"]
    assert (gw["iss"], gw["aud"], gw["bound_to"]) == (PUBLIC, "daegam-mcp", "gateway:gw-1")
    assert (mcp["iss"], mcp["aud"], mcp["bound_to"]) == (
        ISSUER,
        server_audience(TENANT, "mcp-1"),
        "mcp-server:mcp-1",
    )
    for claims in (gw, mcp):
        assert claims["sub"] == PERSON and claims["tenant"] == TENANT
        # act 는 Agent 의 id 도 싣는다 (P52 3절)
        assert claims["act"] == {"sub": "svc:agent:agent-ag-1", "agent_id": AGENT_ID}
        assert claims["preferred_username"] == "alice"
        assert claims["exp"] - claims["iat"] == 300
        assert not {"groups", "cid", "tv", "scope"} & set(claims)


def test_turn_limits_read_the_pinned_revision():
    assert turn_limits(_agent(spec=_spec(turn_timeout=45, session_ttl=900))) == TurnLimits(45, 900)
    assert turn_limits(agent_doc("draft")) == TurnLimits(120, 3600)


# -- 어느 대상이 토큰을 받는가 (2절) ---------------------------------------------------------------


def test_a_server_that_has_not_applied_its_own_audience_keeps_the_agent_credential(world):
    world.servers = [_server(applied=False)]
    assert _targets(world) == {("gateway_instance", "gw-1")}


@pytest.mark.parametrize(
    "gateway",
    [
        pytest.param(
            {"overrides": {"spec": {"incomingAuth": {"authzConfig": CONFIGMAP_AUTHZ}}}},
            id="owner-cedar",
        ),
        # 다른 모드에서 렌더한 번호 3 은 묶임의 표시가 없다
        pytest.param({"render_epoch": RENDER_EPOCHS[KIND_GATEWAY]}, id="rendered-in-another-mode"),
        pytest.param({"tenant": "other"}, id="other-tenant"),
    ],
)
def test_a_gateway_that_cannot_hold_the_binding_keeps_the_agent_credential(world, gateway):
    world.gateways = [_gateway(**gateway)]
    assert _targets(world) == {("mcp_instance", "mcp-1")}


def test_a_closed_gateway_gate_keeps_gateway_sources_on_the_agent_credential(world):
    world.verdict = _verdict(open_=False)
    assert _targets(world) == {("mcp_instance", "mcp-1")}


def test_a_missing_or_stale_gate_verdict_answers_503_instead_of_running_as_the_agent(world):
    """오래됐거나 다른 모드에서 내린 판정도 없는 판정과 같다. 그 신선도는 test_turn_gates.py 가
    본다."""
    world.verdict = None

    async def go(store: MongoTurnGrantStore) -> None:
        await _open(world.build(store))

    with pytest.raises(TurnTokensUnavailable) as e:
        _run(world.settings, go)
    assert (e.value.status, e.value.code) == (503, "turn_tokens_unavailable")


def test_a_target_on_a_cluster_below_0_53_keeps_the_agent_credential(world):
    world.clusters = [cluster_doc("edge", id="c1", recorded_toolhive_version="0.52.0")]
    assert _targets(world) == set()


def test_without_the_capability_or_a_signing_key_the_turn_runs_as_today(world):
    """`turn_tokens` 를 알리지 않는 옛 런타임의 Agent 는 지금처럼 Agent 의 자격으로 돈다. 서명
    키가 없는 플랫폼, 설정을 끈 플랫폼, 토큰을 만들지 않는 header 모드도 같은 판정(`_mints_for`)으로
    위임을 열지 않는다."""
    base = world.settings
    world.agents = [_agent(features=())]
    assert _targets(world) == set()
    world.agents = [_agent()]
    world.signed = False
    assert _targets(world) == set()
    world.signed = True
    for off in ({"turn_tokens_enabled": False}, {"auth_mode": "header"}):
        world.settings = dataclasses.replace(base, **off)
        assert _targets(world) == set(), off


def test_a_platform_admin_without_a_membership_row_gets_no_grant(world):
    world.rows = {PERSON: [ALL_TENANTS]}

    async def go(store: MongoTurnGrantStore):
        return await _open(world.build(store))

    outcome = _run(world.settings, go)
    assert outcome.grant is None and outcome.refused == "membership"


def test_a_person_who_fails_the_checks_runs_as_the_agent_without_a_gate_verdict(world):
    """사람을 먼저 본다. 확인을 지나지 못한 사람은 이름이 대는 까닭이 있어 Agent 의 이름으로 돌고,
    게이트의 판정이 없다고 503 을 받지 않는다 (2절). local 모드에서 `tv` 를 싣지 않은 사람은
    비밀번호 판을 지나지 못한다. 서비스 주체는 사람이 아니므로 위임을 열지 않는다."""
    world.verdict = None
    world.users = [_user(disabled=True)]

    def opened(person: GrantPerson):
        return _run(world.settings, lambda store: _open(world.build(store), person=person))

    outcome = opened(_person())
    assert outcome.grant is None and outcome.refused == "disabled"
    world.users = [_user()]
    outcome = opened(_person(tv=None))
    assert outcome.grant is None and outcome.refused == "password"
    outcome = opened(_person(sub="svc:agent:x"))
    assert outcome.grant is None and outcome.refused is None


# -- 열기 (4절) ----------------------------------------------------------------------------------


def _oidc(settings: Settings) -> Settings:
    """IdP 를 믿는 oidc 모드. Gateway 대상은 Agent 의 자격 그대로이고 MCP Server 만 토큰을
    받는다."""
    return dataclasses.replace(
        settings,
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        oidc_audience="api",
        data_plane_issuer="idp",
    )


def test_in_oidc_mode_an_owner_resume_needs_the_owners_recent_idp_confirmation(world):
    """승인자가 소유자 대신 재개하면 소유자는 없으므로, 소유자의 사람 기록이 마지막 IdP 확인에서
    대화형 한도까지 토큰 한 수명을 남겨야 연다(P53b 6.3절). 기록이 없거나 비웠으면 `unconfirmed`
    다. oidc 모드의 vMCP 는 IdP 만 믿으므로 Gateway 대상은 Agent 의 자격 그대로이고 MCP Server 만
    토큰을 받는다."""
    world.settings = _oidc(world.settings)
    bound = timedelta(seconds=world.settings.turn_owner_confirm_seconds)
    ttl = timedelta(seconds=world.settings.turn_token_ttl_seconds)

    def resumed(*people: Any):
        world.people = list(people)
        return _run(
            world.settings,
            lambda store: _open(world.build(store), trigger="decide", decided_by="carol"),
        )

    fresh = resumed(idp_person(PERSON, age=bound - ttl - timedelta(seconds=30)))
    assert set(_tokens(fresh.grant.turn_grant)) == {("mcp_instance", "mcp-1")}
    near = resumed(idp_person(PERSON, age=bound - ttl + timedelta(seconds=5)))
    assert near.grant is None and near.refused == CONFIRM_BOUND
    assert resumed().refused == "unconfirmed"
    assert resumed(idp_person(PERSON, cleared_at=utcnow())).refused == "unconfirmed"


def test_in_oidc_mode_a_persons_own_turn_opens_as_them_and_its_renewal_ends_past_the_bound(
    world,
):
    """자기 턴은 그 요청이 유효한 IdP 토큰으로 사람이 있음을 보였으므로, 마지막 IdP 확인이 대화형
    한도에 닿았어도 그 사람으로 연다. Agent 의 자격으로 내려가지 않는다. 한도는 갱신이 보고, 넘긴
    갱신은 410 이라 턴은 이어 가고 Tool 만 꺼진다."""
    world.settings = _oidc(world.settings)
    bound = timedelta(seconds=world.settings.turn_owner_confirm_seconds)
    world.people = [idp_person(PERSON, age=bound)]
    for trigger in ("chat", "regenerate", "decide"):

        async def own(store: MongoTurnGrantStore, t: str = trigger) -> Any:
            return await _open(world.build(store), trigger=t)

        outcome = _run(world.settings, own)
        assert outcome.refused is None and outcome.grant is not None, trigger
        assert outcome.grant.turn_grant["sub"] == PERSON
    result = _renew_after(world)
    assert _status(result) == (410, "turn_grant_ended")
    assert isinstance(result, TurnGrantError) and result.extra == {"reason": CONFIRM_BOUND}
    # 접근을 잃은 사람은 자기 턴도 열지 않는다
    world.people = []
    assert _run(world.settings, lambda store: _open(world.build(store))).refused == "unconfirmed"


def test_in_oidc_mode_a_run_checks_the_long_bound_and_the_deadline_is_the_turns(world, mongo):
    """실행은 대화형 한도가 아니라 긴 한도(7일)로 사람을 본다. 마감은 local 처럼 턴 시한과 여유이고
    더는 한도로 자르지 않는다. 긴 턴의 Tool 은 갱신마다 사람의 확인으로 지킨다."""
    world.settings = _oidc(world.settings)
    world.agents = [_agent(spec=_spec(turn_timeout=4 * 3600))]
    long_bound = timedelta(seconds=world.settings.idp_confirm_seconds)
    ttl = timedelta(seconds=world.settings.turn_token_ttl_seconds)

    def opened(age: timedelta):
        world.people = [idp_person(PERSON, age=age)]
        outcome = _run(world.settings, lambda store: _open(world.build(store), trigger="run"))
        row = mongo[TURN_GRANTS].find_one({"_id": outcome.grant.id}) if outcome.grant else None
        return outcome, row

    _outcome, row = opened(timedelta(hours=3))
    assert row["deadline"] - row["opened_at"] == timedelta(hours=4, seconds=60)
    stale, _row = opened(long_bound - ttl + timedelta(seconds=5))
    assert stale.grant is None and stale.refused == "unconfirmed"


def test_in_oidc_mode_every_renewal_checks_the_bound_of_its_trigger(world):
    """갱신마다 계기의 한도로 사람을 다시 본다. 채팅은 대화형 한도, 실행은 긴 한도다. 대화형 한도를
    넘긴 사람은 접근을 잃지 않았으므로 410 이다. 런타임은 턴을 멈추지 않고 Tool 호출만 오류로
    답한다. 긴 한도를 넘긴 사람은 접근을 잃었으므로 403 이고 턴이 멈춘다."""
    world.settings = _oidc(world.settings)
    bound = timedelta(seconds=world.settings.turn_owner_confirm_seconds)

    def aged(by: timedelta) -> Callable[[World], None]:
        return lambda w: setattr(w, "people", [idp_person(PERSON, age=by)])

    result = _renew_after(world, aged(bound))
    assert _status(result) == (410, "turn_grant_ended")
    assert isinstance(result, TurnGrantError) and result.extra == {"reason": CONFIRM_BOUND}
    assert set(_tokens(_renew_after(world, aged(bound), trigger="run"))) == {
        ("mcp_instance", "mcp-1")
    }
    result = _renew_after(world, aged(timedelta(days=8)), trigger="run")
    assert _status(result) == (403, "turn_grant_refused")
    assert isinstance(result, TurnGrantError) and result.extra == {"reason": "unconfirmed"}


def test_the_grant_record_keeps_who_delegated_and_no_secret(world, mongo):
    async def go(store: MongoTurnGrantStore) -> Any:
        outcome = await _open(
            world.build(store), trigger="decide", decided_by="user-bob", session_id=None
        )
        await world.build(store).attach_session(outcome.grant.id, "s-new")
        return outcome.grant

    opened = _run(world.settings, go)
    grant = opened.turn_grant
    # repr 에도 비밀과 토큰이 없다
    text = repr(opened)
    assert grant["secret"] not in text and all(t["token"] not in text for t in grant["tokens"])
    stored = mongo[TURN_GRANTS].find_one({"_id": grant["id"]})
    assert stored["secret_sha256"] == hash_token(grant["secret"])
    assert grant["secret"] not in str(stored)
    assert (stored["sub"], stored["trigger"], stored["decided_by"]) == (
        PERSON,
        "decide",
        "user-bob",
    )
    assert (stored["session_id"], stored["tv"], stored["username"]) == ("s-new", 3, "alice")
    assert {(t["kind"], t["id"]) for t in stored["targets"]} == ALL
    assert stored["purge_at"] - stored["deadline"] == timedelta(days=7)


def test_opening_answers_503_when_signing_or_the_write_fails(world, monkeypatch):
    def broken_sign(_claims: dict[str, Any]) -> str:
        raise ValueError("no key")

    async def sign_fails(store: MongoTurnGrantStore) -> None:
        signer = PlatformSigner.generate()
        monkeypatch.setattr(signer, "sign", broken_sign)
        issuer = _issuer(world.settings)
        issuer.attach(signer)
        await _open(world.build(store, issuer))

    async def write_fails(store: MongoTurnGrantStore) -> None:
        service = world.build(store)

        async def insert(_grant: Any) -> None:
            raise PyMongoError("down")

        monkeypatch.setattr(service.grants, "insert", insert)
        await _open(service)

    for fn in (sign_fails, write_fails):
        with pytest.raises(TurnTokensUnavailable):
            _run(world.settings, fn)


# -- 갱신과 끝 (4절) -----------------------------------------------------------------------------


def _renew_after(
    world: World,
    change: Callable[[World], None] | None = None,
    *,
    agent_id: str = AGENT_ID,
    presented_by: str | None = None,
    secret: str | None = None,
    end: str | None = None,
    trigger: str = "chat",
    stored: dict[str, Any] | None = None,
) -> Any:
    """위임을 연 뒤 change 로 세계를 바꾸고 agent_id 의 경로로 갱신한다. presented_by 는 인스턴스
    토큰의 주인이고 기본은 agent_id 다. trigger 는 위임을 연 계기다. stored 는 연 뒤 기록에 바로
    거는 갱신이다. 뒤 판이 쓴 칸을 흉내 낸다. 거절이면 그 예외를 돌려준다."""

    async def go(store: MongoTurnGrantStore) -> Any:
        grant = (await _open(world.build(store), trigger=trigger)).grant.turn_grant
        if stored is not None:
            await store.coll.update_one({"_id": grant["id"]}, stored)
        if end is not None:
            await world.build(store).end(grant["id"], end)
        if change is not None:
            change(world)
        try:
            return await world.build(store).renew(
                agent_id,
                grant["id"],
                secret=secret or grant["secret"],
                presented=f"{INSTANCE_TOKEN}:{presented_by or agent_id}",
            )
        except Exception as e:  # noqa: BLE001 - 시험이 거절의 상태와 코드를 본다
            return e

    return _run(world.settings, go)


def _status(result: Any) -> tuple[int, str]:
    if isinstance(result, ServiceError):
        return result.status, result.code
    detail = getattr(result, "detail", {})
    return result.status_code, detail.get("code")


def _delete(w: World) -> None:
    w.users = []


def test_a_renewal_needs_this_agents_instance_token_its_grant_and_the_secret(world):
    """다른 Agent 의 인스턴스 토큰은 401, 다른 Agent 의 경로로 낸 이 위임은 404, 틀린 비밀은
    401 이다."""
    world.agents.append(_agent("ag-2"))
    assert _status(_renew_after(world, presented_by="ag-2")) == (401, "unauthorized")
    assert _status(_renew_after(world, agent_id="ag-2")) == (404, "turn_grant_not_found")
    assert _status(_renew_after(world, secret="wrong")) == (401, "unauthorized")


def test_a_renewal_checks_the_person_again(world):
    """local 모드는 사용자 문서를, oidc 모드는 사람 기록과 소속 줄을 다시 읽는다."""
    result = _renew_after(world, _delete)
    assert _status(result) == (403, "turn_grant_refused")
    assert isinstance(result, TurnGrantError) and result.extra == {"reason": "deleted"}
    world.settings = _oidc(world.settings)
    result = _renew_after(world, lambda w: setattr(w, "rows", {}))
    assert _status(result) == (403, "turn_grant_refused")
    assert isinstance(result, TurnGrantError) and result.extra == {"reason": "membership"}


def test_a_target_that_fails_its_check_gets_no_token_and_leaves_the_grant(world, mongo):
    """위임을 연 뒤 Gateway 가 묶임 없는 렌더로 돌아갔다. 갱신은 열기와 같은 대상 규칙을 다시
    본다."""
    unbound = _gateway(render_epoch=RENDER_EPOCHS[KIND_GATEWAY])
    renewed = _renew_after(world, lambda w: setattr(w, "gateways", [unbound]))
    assert set(_tokens(renewed)) == {("mcp_instance", "mcp-1")}
    stored = mongo[TURN_GRANTS].find_one({})
    assert [t["id"] for t in stored["targets"]] == ["mcp-1"] and stored["renewals"] == 1


def test_a_renewal_never_adds_a_target_and_ignores_the_cluster_gates(world):
    """연 뒤 열린 대상도 더하지 않고, 연 뒤 닫힌 게이트도 열린 위임의 토큰을 빼지 않는다."""
    world.servers = [_server(applied=False)]

    def later(w: World) -> None:
        w.servers = [_server()]
        w.verdict = _verdict(open_=False, age=timedelta(hours=1))
        w.clusters = [cluster_doc("edge", id="c1", recorded_toolhive_version="0.50.0")]

    assert set(_tokens(_renew_after(world, later))) == {("gateway_instance", "gw-1")}


def test_a_renewal_is_capped_by_the_deadline_and_refused_past_it(world, mongo):
    """갱신한 토큰은 마감을 넘지 않는다. 마감이 지났거나 플랫폼이 끝냈거나 연 뒤 토큰을 만들지 않게
    된 위임의 갱신은 410 이다."""
    world.agents = [_agent(spec=_spec(turn_timeout=30))]
    renewed = _renew_after(world)
    assert set(renewed) == {"tokens", "expires_in"} and renewed["expires_in"] <= 90
    tokens = _tokens(renewed)
    assert set(tokens) == ALL and tokens["gateway_instance", "gw-1"]["sub"] == PERSON

    def past(_w: World) -> None:
        mongo[TURN_GRANTS].update_many({}, {"$set": {"deadline": utcnow() - timedelta(seconds=1)}})

    assert _status(_renew_after(world, past)) == (410, "turn_grant_ended")
    # 플랫폼이 끝낸 위임도 마감 전에 410 이다
    assert _status(_renew_after(world, end=END_REFUSED)) == (410, "turn_grant_ended")
    # 연 뒤 설정을 끄거나 header 모드로 옮긴 플랫폼은 토큰을 만들지 않으므로 갱신도 410 이다
    base = world.settings
    for off in ({"turn_tokens_enabled": False}, {"auth_mode": "header"}):
        world.settings = base

        def switch(w: World, off: dict[str, Any] = off) -> None:
            w.settings = dataclasses.replace(base, **off)

        assert _status(_renew_after(world, switch)) == (410, "turn_grant_ended"), off


def test_concurrent_renewals_never_bring_back_a_dropped_target(world, mongo):
    """한 갱신이 Gateway 를 뺀 뒤, 그 전에 위임을 읽은 다른 갱신이 아무것도 빼지 않고 적어도 Gateway
    는 돌아오지 않는다."""

    async def go(store: MongoTurnGrantStore) -> str:
        grant = (await _open(world.build(store))).grant
        gateway = GrantTarget(kind="gateway_instance", id="gw-1")
        now = utcnow()
        assert await store.record_renewal(grant.id, dropped=[gateway], now=now)
        assert await store.record_renewal(grant.id, dropped=[], now=now)
        return grant.id

    stored = mongo[TURN_GRANTS].find_one({"_id": _run(world.settings, go)})
    assert [t["id"] for t in stored["targets"]] == ["mcp-1"] and stored["renewals"] == 2


# 뒤 판이 더할 대상 종류. 이름은 이 시험의 것이다. P46 의 `agent`(토큰의 `bound_to` 는
# `agent-call:<id>`)가 그런 종류이고, 이 판이 `agent` 를 알게 된 뒤에도 이 시험은 다음 판의 종류를
# 본다
LATER_TARGET = {"kind": "later_kind", "id": "ag-billing"}


def test_a_renewal_keeps_a_target_of_a_later_release_and_mints_no_token_for_it(world, mongo):
    """롤링 중에 이 판의 레플리카가 뒤 판이 연 위임을 갱신한다 (P46 A0). 모르는 종류의 대상은
    토큰을 받지 못하지만 기록에 남아, 뒤 판의 다음 갱신이 다시 낸다. 아는 대상은 지금처럼 본다."""
    renewed = _renew_after(world, stored={"$push": {"targets": LATER_TARGET}})
    assert set(_tokens(renewed)) == ALL
    stored = mongo[TURN_GRANTS].find_one({})
    assert LATER_TARGET in stored["targets"] and stored["renewals"] == 1


def test_a_renewal_takes_the_stored_bound_class_over_the_trigger(world):
    """뒤 판이 부른 Agent 의 위임은 계기가 `agent` 이고 뿌리 턴의 한도 종류를 `long_bound` 로 든다
    (P46 6.4절). 이 판의 갱신은 계기 대신 그것으로 사람을 본다. 실행이 뿌리면 대화형 한도를 넘긴
    사람도 갱신하고, 채팅이 뿌리면 410 이다. 사람이 지금 보낸 채팅의 위임에 참이 적혀 있어도 그
    참은 믿지 않는다."""
    world.settings = _oidc(world.settings)
    bound = timedelta(seconds=world.settings.turn_owner_confirm_seconds)

    def aged(w: World) -> None:
        w.people = [idp_person(PERSON, age=bound)]

    run_root = {"$set": {"trigger": "agent", "long_bound": True}}
    renewed = _renew_after(world, aged, trigger="run", stored=run_root)
    assert set(_tokens(renewed)) == {("mcp_instance", "mcp-1")}
    for trigger, stored in (
        ("run", {"$set": {"trigger": "agent", "long_bound": False}}),
        ("chat", {"$set": {"long_bound": True}}),
    ):
        result = _renew_after(world, aged, trigger=trigger, stored=stored)
        assert _status(result) == (410, "turn_grant_ended"), stored
        assert isinstance(result, TurnGrantError) and result.extra == {"reason": CONFIRM_BOUND}


def test_a_grant_opened_outside_an_agent_call_holds_no_bound_class(world, mongo):
    """Agent 호출 밖에서 연 위임에는 `long_bound` 칸이 없다. 비어 있으면 저장하지 않는다."""
    _run(world.settings, lambda store: _open(world.build(store)))
    assert "long_bound" not in mongo[TURN_GRANTS].find_one({})


# -- 다른 Agent 를 Tool 로 부르는 대상 (P46 6.4절) ----------------------

CALLED = "ag-billing"
CALL = ("agent", CALLED)


def _calling(world: World, **kw: Any) -> None:
    """ag-1 의 배포된 리비전이 게시한 Agent `ag-billing` 을 Tool 로 쓴다."""
    pin = AgentCallPin(agent_id=CALLED, agent_name="billing", description="d", timeout_seconds=630)
    pins = {"tools-billing": ToolPin(names=["ask_billing"], call=pin)}
    world.agents = [_agent(tool_pins=pins, **kw), _agent(CALLED)]


def test_an_agent_source_gets_a_token_only_the_call_routes_take(world, mongo):
    _calling(world)
    grant = _run(world.settings, lambda store: _open(world.build(store))).grant.turn_grant
    tokens = _tokens(grant)
    assert set(tokens) == ALL | {CALL}
    call = tokens[CALL]
    assert (call["iss"], call["aud"]) == (ISSUER, agent_call_audience(TENANT, CALLED))
    assert (call["bound_to"], call["grant_id"]) == (f"agent-call:{CALLED}", grant["id"])
    assert (call["sub"], call["act"]["agent_id"]) == (PERSON, AGENT_ID)
    assert not {"groups", "cid", "tv", "scope"} & set(call)
    # 다른 대상의 토큰은 위임 id 를 싣지 않고 호출 경로의 검증을 지나지 못한다
    raw = {(t["kind"], t["id"]): t["token"] for t in grant["tokens"]}
    for target, token in raw.items():
        if target == CALL:
            claims = verify_agent_call_token(token, SIGNER, issuer=ISSUER, agent_id=CALLED)
            assert claims.grant_id == grant["id"]
            continue
        assert "grant_id" not in tokens[target]
        with pytest.raises(AuthError):
            verify_agent_call_token(token, SIGNER, issuer=ISSUER, agent_id=CALLED)
    assert {"kind": "agent", "id": CALLED} in mongo[TURN_GRANTS].find_one({})["targets"]


def test_the_agent_target_does_not_depend_on_the_data_plane_issuer_or_the_gate(world):
    """IdP 를 믿는 oidc 모드와 판정이 없는 게이트에서도 호출 토큰은 나간다. 플랫폼이 검증한다."""
    world.settings = _oidc(world.settings)
    world.servers, world.gateways, world.verdict = [], [], None
    _calling(world)
    assert _targets(world) == {CALL}


@pytest.mark.parametrize("change", ["calls off", "runs off", "agent gone", "other tenant"])
def test_an_agent_target_needs_calls_on_and_the_called_agent_in_the_tenant(world, change):
    _calling(world)
    if change == "calls off":
        world.settings = dataclasses.replace(world.settings, agent_calls_enabled=False)
    elif change == "runs off":
        world.settings = dataclasses.replace(world.settings, runs_enabled=False)
    elif change == "agent gone":
        world.agents = world.agents[:1]
    else:
        world.agents[1] = _agent(CALLED, tenant="other")
    assert _targets(world) == ALL


def test_a_renewal_mints_the_agent_target_again_and_drops_it_once_the_agent_is_gone(world, mongo):
    _calling(world)
    assert set(_tokens(_renew_after(world))) == ALL | {CALL}
    mongo[TURN_GRANTS].delete_many({})
    gone = _renew_after(world, lambda w: setattr(w, "agents", w.agents[:1]))
    assert set(_tokens(gone)) == ALL
    assert {"kind": "agent", "id": CALLED} not in mongo[TURN_GRANTS].find_one({})["targets"]


def test_an_agent_only_grant_reports_no_targets_apart_from_a_refusal(world):
    """대상이 없는 Agent 와 위임을 열 수 없는 플랫폼, 확인을 지나지 못한 사람을 가른다. 다른
    Agent 가 부른 실행은 첫째만 위임 없이 돈다."""
    world.servers, world.gateways = [], []
    opened = _run(world.settings, lambda store: _open(world.build(store)))
    assert (opened.grant, opened.refused, opened.no_targets) == (None, None, True)
    world.users = []
    lost = _run(world.settings, lambda store: _open(world.build(store)))
    assert lost.refused == "deleted" and lost.no_targets is False
    world.settings = dataclasses.replace(world.settings, turn_tokens_enabled=False)
    off = _run(world.settings, lambda store: _open(world.build(store)))
    assert (off.grant, off.refused, off.no_targets) == (None, None, False)


def test_a_call_grant_stores_the_roots_bound_and_its_run(world, mongo):
    _run(
        world.settings,
        lambda store: _open(
            world.build(store), trigger="agent", session_id=None, long_bound=True, run_id="r1"
        ),
    )
    stored = mongo[TURN_GRANTS].find_one({})
    assert (stored["trigger"], stored["long_bound"], stored["run_id"]) == ("agent", True, "r1")
    assert "calls" not in stored


def test_in_oidc_mode_a_call_opens_and_renews_with_the_roots_bound(world):
    """채팅이 뿌리인 호출은 사람이 그 요청에 없으므로 열 때도 대화형 한도를 본다. 실행이 뿌리인
    호출과 그 실행을 재개하는 결정은 긴 한도다. 갱신도 계기가 아니라 적힌 한도 종류를 따른다."""
    world.settings = _oidc(world.settings)
    bound = timedelta(seconds=world.settings.turn_owner_confirm_seconds)
    world.people = [idp_person(PERSON, age=bound)]

    def opened(**kw: Any) -> Any:
        return _run(world.settings, lambda store: _open(world.build(store), **kw))

    assert opened(trigger="agent", long_bound=False).refused == CONFIRM_BOUND
    assert opened(trigger="agent", long_bound=True).grant is not None
    # 자기 결정은 그 요청이 사람을 보였으므로 열 때 보지 않고, 승인자의 재개는 적힌 한도를 본다
    assert opened(trigger="decide", long_bound=False).grant is not None
    assert opened(trigger="decide", long_bound=False, decided_by="bob").refused == CONFIRM_BOUND
    assert opened(trigger="decide", long_bound=True, decided_by="bob").grant is not None

    def aged(w: World) -> None:
        w.people = [idp_person(PERSON, age=bound)]

    world.people = [idp_person(PERSON)]
    kept = _renew_after(world, aged, trigger="agent", stored={"$set": {"long_bound": True}})
    assert set(_tokens(kept)) == {("mcp_instance", "mcp-1")}
    world.people = [idp_person(PERSON)]
    late = _renew_after(world, aged, trigger="agent", stored={"$set": {"long_bound": False}})
    assert _status(late) == (410, "turn_grant_ended")


def test_the_first_end_of_a_grant_tells_the_call_hook_once(world):
    async def go(store: MongoTurnGrantStore) -> list[str]:
        service = world.build(store)
        told: list[str] = []

        async def on_end(grant_id: str) -> None:
            told.append(grant_id)
            raise RuntimeError("the hook failed")

        service.on_end = on_end
        grant = (await _open(service)).grant.turn_grant
        presented = f"{INSTANCE_TOKEN}:{AGENT_ID}"
        for _ in range(2):
            await service.end_by_runtime(
                AGENT_ID, grant["id"], secret=grant["secret"], presented=presented, reason="done"
            )
        await service.end(grant["id"], END_REFUSED)
        return [g for g in told if g == grant["id"]]

    assert len(_run(world.settings, go)) == 1


def test_a_grant_counts_five_calls_and_no_more_once_it_ended(world):
    async def go(store: MongoTurnGrantStore) -> tuple[list[bool], bool, list[bool]]:
        service = world.build(store)
        grant_id = (await _open(service)).grant.id
        now = utcnow()
        counted = [await store.count_call(grant_id, limit=5, now=now) for _ in range(6)]
        other = (await _open(service)).grant.id
        await service.end(other, END_REFUSED)
        ended = await store.count_call(other, limit=5, now=now)
        turns = [await store.count_turn_call("ag:s:t", limit=2, now=now) for _ in range(3)]
        return counted, ended, turns

    counted, ended, turns = _run(world.settings, go)
    assert counted == [True] * 5 + [False] and ended is False
    assert turns == [True, True, False]


def test_the_first_end_recorded_stays(world, mongo):
    async def go(store: MongoTurnGrantStore) -> tuple[bool, bool]:
        service = world.build(store)
        grant = (await _open(service)).grant.turn_grant
        await service.end(grant["id"], END_REFUSED)
        args = (AGENT_ID, grant["id"])
        presented = f"{INSTANCE_TOKEN}:{AGENT_ID}"
        late = await service.end_by_runtime(
            *args, secret=grant["secret"], presented=presented, reason="done"
        )
        stored = await service.grants.get(grant["id"])
        return late["ended"], stored.end_reason == END_REFUSED

    assert _run(world.settings, go) == (False, True)


def test_the_routes_have_a_bucket_per_agent(world):
    world.agents.append(_agent("ag-2"))

    async def go(store: MongoTurnGrantStore) -> None:
        service = world.build(store)
        for _ in range(int(REQUEST_BURST)):
            service._admit(AGENT_ID)
        with pytest.raises(TurnGrantError) as e:
            service._admit(AGENT_ID)
        assert e.value.code == "turn_grant_rate_limited"
        assert e.value.headers == {"Retry-After": "1"}
        # 다른 Agent 의 버킷은 따로다
        service._admit("ag-2")

    _run(world.settings, go)


# -- 기계 경로 ------------------------------------------------------------------------------------


def test_the_routes_stay_out_of_openapi_the_catalog_and_the_http_audit(
    client, fake_clusters, mongo, openapi_spec
):
    assert not [p for p in openapi_spec["paths"] if SPLIT in p]
    assert not [t for t in CATALOG.api_tools if t.path and SPLIT in t.path]
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    base = f"{agent.url}/turn-grants/nope"
    r = client.post(f"{base}/renew", json={"secret": "s"}, headers=agent.auth)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "turn_grant_not_found"
    r = client.post(f"{base}/end", json={"secret": "s", "reason": "done"}, headers=agent.auth)
    assert r.status_code == 404
    assert mongo["audit_events"].count_documents({"path": {"$regex": SPLIT}}) == 0


def test_the_routes_take_the_instance_token_and_the_grant_secret(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    secret = "grant-secret"
    now = utcnow()
    mongo[TURN_GRANTS].insert_one(
        {
            "_id": "g1",
            "secret_sha256": hash_token(secret),
            "agent_id": agent.id,
            "tenant": "default",
            "sub": PERSON,
            "trigger": "chat",
            "targets": [],
            "opened_at": now,
            "deadline": now + timedelta(minutes=5),
            "purge_at": now + timedelta(days=7),
        }
    )
    renew = f"{agent.url}/turn-grants/g1/renew"
    assert client.post(renew, json={"secret": secret}).status_code == 401
    assert client.post(renew, json={"secret": secret}, headers=bearer("x")).status_code == 401
    assert client.post(renew, json={"secret": "wrong"}, headers=agent.auth).status_code == 401
    # header 모드는 토큰을 만들지 않으므로 설정이 꺼진 것과 같다
    r = client.post(renew, json={"secret": secret}, headers=agent.auth)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "turn_grant_ended"
    end = f"{agent.url}/turn-grants/g1/end"
    r = client.post(end, json={"secret": secret, "reason": "nope"}, headers=agent.auth)
    assert r.status_code == 422
    r = client.post(end, json={"secret": secret, "reason": "done"}, headers=agent.auth)
    assert r.status_code == 200 and r.json() == {"ended": True}
    assert mongo[TURN_GRANTS].find_one({"_id": "g1"})["end_reason"] == "done"
    big = {"secret": "s" * 5000}
    assert client.post(renew, json=big, headers=agent.auth).status_code == 413


# -- 설정 -----------------------------------------------------------------------------------------


def test_the_settings_default_on_and_clamp_to_their_ranges(bare_env):
    defaults = settings_from_env()
    assert defaults.turn_tokens_enabled is True
    # 대화형 한도는 한 시간짜리 IdP 토큰과 UI 의 60초 앞 갱신을 덮는다 (P53b F0 2번)
    assert (defaults.turn_token_ttl_seconds, defaults.turn_owner_confirm_seconds) == (300, 4500)
    bare_env.setenv("DAEGAM_TURN_TOKENS_ENABLED", "false")
    bare_env.setenv("DAEGAM_TURN_TOKEN_TTL_SECONDS", "5")
    bare_env.setenv("DAEGAM_TURN_OWNER_CONFIRM_SECONDS", "999999")
    off = settings_from_env()
    assert off.turn_tokens_enabled is False
    assert (off.turn_token_ttl_seconds, off.turn_owner_confirm_seconds) == (60, 86400)
    bare_env.setenv("DAEGAM_TURN_TOKEN_TTL_SECONDS", "5000")
    assert settings_from_env().turn_token_ttl_seconds == 900
