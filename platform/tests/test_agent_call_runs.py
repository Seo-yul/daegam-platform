"""다른 Agent 를 Tool 로 부르는 호출 경로의 판단 (P46 6.3~6.6절, `services/agent_call_runs.py`).

위임과 실행의 저장소는 Mongo 이고 Agent, 허브 항목, 인스턴스 토큰, 런타임 프록시는 그 자리의 가짜다.
시작은 진짜 시작 워커의 한 걸음이다. 토큰은 턴 위임이 내는 것과 같은 클레임으로 이 시험이 서명한다.
위임이 그 토큰을 내는 것은 `tests/test_turn_grants.py` 가, 시작 워커의 규칙은
`tests/test_runs_start.py` 가 본다.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from daegam.auth.local import LocalVerifier
from daegam.auth.oidc import AuthError
from daegam.auth.service_token import (
    AGENT_CALL_TARGET,
    InternalIssuer,
    service_sub,
    turn_token_claims,
    verify_agent_call_token,
)
from daegam.auth.signer import (
    CONTROL_PLANE_AUDIENCE,
    LLM_AUDIENCE,
    TELEMETRY_AUDIENCE,
    PlatformSigner,
    agent_call_audience,
    audience_server_id,
    internal_audience,
    server_audience,
)
from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.services import agent_call_runs
from daegam.services.agent_call_runs import (
    CALLER_ENDED,
    CALLER_TIMEOUT,
    MAX_CALLS_PER_TURN,
    AgentCallRuns,
    CallCredentials,
)
from daegam.services.runs.events import RunEvents
from daegam.services.runs.service import (
    CALL_CAPS,
    MAX_CALLS_RUNNING,
    MAX_RUNNING,
    RUN_CAPS,
    RunService,
)
from daegam.services.runs.start import RunStartWorker
from daegam.services.runs.watch import RunWatchWorker
from daegam.services.turn_person import TurnDelegation
from daegam.store.agent_runs import ActsAs, AgentRun, MongoAgentRunStore, RunCaller, new_run
from daegam.store.agents import AgentCallPin, ToolPin
from daegam.store.base import ItemLease, hash_token, utcnow
from daegam.store.turn_grants import PURGE_AFTER, GrantTarget, MongoTurnGrantStore, TurnGrant
from tests.helpers.agents import node_of, template_spec
from tests.helpers.aio import run_stores
from tests.helpers.consts import ISSUER
from tests.helpers.runs import (
    FakeAgent,
    FakeAudit,
    FakeOwners,
    FakeTurns,
    FakeUsers,
    alice,
    person_check,
    service_of,
)
from tests.helpers.turns import FakeGrants
from tests.test_runs_start import RUN_HEADERS, FakeProxy

SIGNER = PlatformSigner.generate()
TENANT = "default"
ROOT, CALLING, CALLED, THIRD = "ag-root", "ag-support", "ag-billing", "ag-third"
INSTANCE_TOKEN = "instance-token-calling"
MESSAGE = "what is my balance?"
ANSWER = "Your balance is 42."
STORES = (MongoTurnGrantStore, MongoAgentRunStore)


@dataclasses.dataclass
class CallAgent(FakeAgent):
    """호출 경로가 읽는 칸을 더한 가짜 Agent 다. 미리 보기가 아니다."""

    preview: Any = None
    deployed_revision: int | None = 3

    @property
    def acting_id(self) -> str:
        return self.id


def _item(agent_id: str) -> str:
    """그 Agent 를 게시한 허브 항목의 id 다."""
    return f"hub-{agent_id}"


def _uses(*called: str) -> dict[str, Any]:
    """called 를 Tool 로 붙여 배포한 Agent 의 칸이다. 리비전의 tools 노드가 그 항목을 대고 핀이
    호출받는 Agent 를 댄다."""
    spec = template_spec()
    nodes = spec["graph"]["nodes"]
    first = node_of(spec, "tools-calculator")
    pins: dict[str, ToolPin] = {}
    for n, agent_id in enumerate(called):
        node = first if n == 0 else copy.deepcopy(first) | {"id": f"tools-call-{n}"}
        node["data"]["source"] = {"kind": "agent", "id": _item(agent_id)}
        if n:
            nodes.append(node)
        pin = AgentCallPin(
            agent_id=agent_id, agent_name=agent_id, description="d", timeout_seconds=630
        )
        pins[node["id"]] = ToolPin(names=["ask_x"], call=pin)
    return {"spec": spec, "tool_pins": pins}


class Agents:
    def __init__(self, *agents: CallAgent) -> None:
        self.by_id = {a.id: a for a in agents}

    def with_previews(self) -> Agents:
        return self

    async def get(self, agent_id: str, tenant: str | None = None) -> CallAgent | None:
        doc = self.by_id.get(agent_id)
        return doc if doc is not None and tenant in (None, doc.tenant) else None


class Hub:
    """게시한 Agent 의 id → 호출을 믿는가. 없는 id 는 게시하지 않은 Agent 다."""

    def __init__(self, **trusted: bool) -> None:
        self.trusted = trusted
        # 게시를 거뒀다가 다시 게시한 Agent 의 새 항목 id
        self.republished: dict[str, str] = {}

    async def get_by_agent(self, agent_id: str) -> Any:
        if agent_id not in self.trusted:
            return None
        return SimpleNamespace(
            id=self.republished.get(agent_id, _item(agent_id)),
            kind="agent",
            tenant=TENANT,
            calls_trusted=self.trusted[agent_id],
        )


class InstanceTokens:
    async def get_by_hash(self, digest: str) -> Any:
        if digest != hash_token(INSTANCE_TOKEN):
            return None
        return SimpleNamespace(agent_instance_id=CALLING, active=True)


class Publishing:
    def call_refusal(self, doc: Any) -> None:
        return None


class Runtime(FakeProxy):
    """시작을 받아들이고 실행 세션의 전사로 답하는 런타임이다."""

    def __init__(self) -> None:
        super().__init__()
        self.transcript_reads = 0

    async def chat(self, agent: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        headers = {**RUN_HEADERS, "x-daegam-run-id": kwargs["run_id"]}
        if kwargs.get("turn_grant"):
            headers["x-daegam-turn-grant"] = kwargs["turn_grant"]["id"]
        return self.answer(200, headers)

    async def session(self, agent: Any, session_id: str, **_: Any) -> tuple[int, Any]:
        self.transcript_reads += 1
        items = [
            {"kind": "user", "text": MESSAGE},
            {"kind": "assistant", "text": ANSWER},
            {"kind": "done", "turn_id": "t1"},
        ]
        return 200, {"session": {"running": False}, "transcript": {"items": items}}


def _local(settings: Settings) -> Settings:
    return dataclasses.replace(settings, auth_mode="local", platform_issuer=ISSUER)


class Rig:
    """호출 서비스 하나와 그 가짜들이다. 같은 저장소 위에 여럿을 세우면 레플리카다."""

    def __init__(
        self,
        settings: Settings,
        grants: MongoTurnGrantStore,
        runs: MongoAgentRunStore,
        *,
        role: str = "viewer",
        owner: str = "calls-1",
        **published: bool,
    ) -> None:
        self.settings, self.grants, self.runs = settings, grants, runs
        self.agents = Agents(
            CallAgent(id=ROOT, name="root", **_uses(CALLING)),
            CallAgent(id=CALLING, name="support", **_uses(CALLED)),
            CallAgent(id=CALLED, name="billing", **_uses(THIRD, ROOT)),
            CallAgent(id=THIRD, name="third"),
        )
        self.hub = Hub(**(published or {CALLED: True, THIRD: True, ROOT: True}))
        self.proxy = Runtime()
        self.turns = FakeTurns()
        self.audit = FakeAudit()
        person = person_check(settings, FakeUsers(alice()), ("alice", TENANT, role))
        self.service: RunService = service_of(runs, None, settings, person=person, turns=self.turns)
        self.opened = FakeGrants()
        self.starter = RunStartWorker(
            self.agents,  # type: ignore[arg-type]
            self.service,
            self.proxy,  # type: ignore[arg-type]
            TurnDelegation(self.opened, FakeOwners()),  # type: ignore[arg-type]
            owner=owner,
            interval=15,
            lease_seconds=60,
        )
        issuer = InternalIssuer(issuer=ISSUER, human_issuer=ISSUER, mcp_audience="daegam-mcp")
        issuer.attach(SIGNER)
        self.calls = AgentCallRuns(
            agents=self.agents,  # type: ignore[arg-type]
            hub=self.hub,  # type: ignore[arg-type]
            grants=grants,
            instance_tokens=InstanceTokens(),  # type: ignore[arg-type]
            run_service=self.service,
            publishing=Publishing(),  # type: ignore[arg-type]
            proxy=self.proxy,  # type: ignore[arg-type]
            starter=self.starter,
            issuer=issuer,
            settings=settings,
            audit=self.audit,  # type: ignore[arg-type]
        )

    async def grant(self, grant_id: str = "g1", **fields: Any) -> TurnGrant:
        """부르는 Agent 의 채팅 턴에 연, 호출받는 Agent 를 대상으로 둔 위임이다."""
        now = utcnow()
        base: dict[str, Any] = dict(
            id=grant_id,
            secret_sha256="x",
            agent_id=CALLING,
            tenant=TENANT,
            sub="alice",
            username="alice",
            tv=3,
            trigger="chat",
            session_id="s-chat",
            targets=[GrantTarget(kind="agent", id=CALLED)],
            opened_at=now,
            deadline=now + timedelta(minutes=10),
            purge_at=now + PURGE_AFTER,
        )
        grant = TurnGrant(**{**base, **fields})
        await self.grants.insert(grant)
        return grant

    async def start(self, grant: TurnGrant, called: str = CALLED, **creds: Any) -> dict[str, Any]:
        return await self.calls.start(
            called, CallCredentials(token(grant, called), **creds), MESSAGE
        )

    async def stored(self, run_id: str, agent_id: str = CALLED) -> AgentRun:
        run = await self.runs.get(TENANT, agent_id, run_id)
        assert run is not None
        return run

    async def end_turn(self, run_id: str) -> None:
        """런타임이 그 실행의 턴을 마쳤다고 알린 이벤트다."""
        event = {
            "_id": f"ev-{run_id}",
            "event": "turn_end",
            "status": "done",
            "session_id": "s1",
            "ts": utcnow(),
            "data": {"run_id": run_id},
        }
        await RunEvents(self.runs, self.agents).apply(CALLED, TENANT, [event])  # type: ignore[arg-type]


def token(grant: TurnGrant, called: str = CALLED, **claims: Any) -> str:
    """턴 위임이 그 Agent 를 대상으로 내는 토큰이다(`TurnGrantService._mint`)."""
    base = turn_token_claims(
        issuer=ISSUER,
        audience=agent_call_audience(grant.tenant, called),
        sub=grant.sub,
        ttl=300,
        actor=service_sub("agent", "support"),
        bound_to=AGENT_CALL_TARGET.format(called),
        tenant=grant.tenant,
        username=grant.username,
        agent_id=grant.agent_id,
        grant_id=grant.id,
    )
    return SIGNER.sign({**base, **claims})


def _calling_run(agent_id: str = CALLING, **fields: Any) -> AgentRun:
    """부르는 세션이 실행의 세션일 때 그 실행 기록이다."""
    run = new_run(
        tenant=TENANT, agent_id=agent_id, source="manual", acts_as=ActsAs(sub="alice"), now=utcnow()
    )
    return run.model_copy(update={"status": "running", "session_id": "s-run", **fields})


def _call_run(grant_id: str | None, status: str = "running", **fields: Any) -> AgentRun:
    """다른 Agent 가 부른 실행 기록이다."""
    now = utcnow()
    run = new_run(
        tenant=TENANT,
        agent_id=CALLED,
        source="agent",
        acts_as=ActsAs(sub="alice", username="alice", tv=3),
        now=now,
        session_id="s1",
        turn_id="t1",
        caller=RunCaller(agent_id=CALLING, grant_id=grant_id, chain=[CALLING], timeout_at=now),
    )
    return run.model_copy(update={"status": status, **fields})


def _go(settings: Settings, body: Any) -> Any:
    return run_stores(_local(settings), STORES, body)


# 거절은 `ServiceError` 이고 401 만은 앱의 다른 경로처럼 `HTTPException` 이다
REFUSALS = (ServiceError, HTTPException)


def _refused(error: pytest.ExceptionInfo[Any]) -> tuple[int, str]:
    e = error.value
    if isinstance(e, HTTPException):
        return e.status_code, e.detail["code"]  # type: ignore[index]
    return e.status, e.code


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """요청이 끝나지 않은 실행을 기다리지 않고 답한다."""
    monkeypatch.setattr(agent_call_runs, "REQUEST_WAIT_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def no_session_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """세션이 아직 붙지 않은 위임을 다시 읽기 전에 기다리지 않는다."""
    monkeypatch.setattr(agent_call_runs, "SESSION_ATTACH_WAIT_SECONDS", 0.0)


# -- 토큰은 호출 경로만 연다 (7절) ----------------------------------------


def test_the_call_token_passes_no_other_boundary():
    """호출 토큰의 audience 는 제어면, 데이터 플레인, 내부 홉, 사용량 수집, LLM 게이트웨이의 어느
    것도 아니고 서버 audience 로도 읽히지 않는다. `bound_to` 는 노출된 런타임이 받는 꼴이 아니다."""
    grant = SimpleNamespace(id="g1", tenant=TENANT, sub="alice", username="alice", agent_id=CALLING)
    call_token = token(grant)  # type: ignore[arg-type]
    audience = agent_call_audience(TENANT, CALLED)
    others = (
        CONTROL_PLANE_AUDIENCE,
        "daegam-mcp",
        internal_audience(TENANT),
        server_audience(TENANT, CALLED),
        TELEMETRY_AUDIENCE,
        LLM_AUDIENCE,
        agent_call_audience(TENANT, THIRD),
    )
    for other in others:
        with pytest.raises(AuthError):
            LocalVerifier(SIGNER, issuer=ISSUER, audience=other, required=("exp",)).verify(
                call_token
            )
    assert audience_server_id(TENANT, audience) is None
    claims = InternalIssuer.peek(call_token)
    assert claims is not None and claims.bound_to == f"agent-call:{CALLED}"
    assert claims.bound_to != f"agent:{CALLED}" and not claims.has_groups


def test_only_a_token_minted_for_this_agents_calls_verifies():
    grant = SimpleNamespace(id="g1", tenant=TENANT, sub="alice", username="alice", agent_id=CALLING)
    good = verify_agent_call_token(token(grant), SIGNER, issuer=ISSUER, agent_id=CALLED)  # type: ignore[arg-type]
    assert (good.sub, good.tenant, good.grant_id, good.actor_agent_id) == (
        "alice",
        TENANT,
        "g1",
        CALLING,
    )
    server = SIGNER.sign(
        turn_token_claims(
            issuer=ISSUER,
            audience=server_audience(TENANT, CALLED),
            sub="alice",
            ttl=300,
            actor=service_sub("agent", "support"),
            bound_to=f"mcp:{CALLED}",
            tenant=TENANT,
            username="alice",
            agent_id=CALLING,
        )
    )
    refused = [
        server,
        token(grant, THIRD),  # type: ignore[arg-type]
        token(grant, bound_to=f"agent:{CALLED}"),  # type: ignore[arg-type]
        token(grant, sub="svc:agent:support"),  # type: ignore[arg-type]
        token(grant, grant_id=None),  # type: ignore[arg-type]
        token(grant, iss="https://idp.example"),  # type: ignore[arg-type]
        PlatformSigner.generate().sign(
            turn_token_claims.__call__(  # 다른 키로 서명한 같은 클레임
                issuer=ISSUER,
                audience=agent_call_audience(TENANT, CALLED),
                sub="alice",
                ttl=300,
                actor="svc:agent:support",
                bound_to=f"agent-call:{CALLED}",
                tenant=TENANT,
                username="alice",
                agent_id=CALLING,
                grant_id="g1",
            )
        ),
    ]
    for bad in refused:
        with pytest.raises(AuthError):
            verify_agent_call_token(bad, SIGNER, issuer=ISSUER, agent_id=CALLED)


def test_the_call_audience_prefix_is_reserved(offline_settings):
    for name in ("mcp_audience", "oidc_audience"):
        with pytest.raises(ValueError, match="agent call audience prefix"):
            dataclasses.replace(offline_settings, **{name: "daegam-agent-call:x"})


def test_a_token_route_takes_no_other_credential(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        cases: list[tuple[str, int, str]] = [
            # 인스턴스 토큰만 온 요청은 토큰 인증 모드에서 받지 않는다
            (INSTANCE_TOKEN, 403, "acts_as_unavailable"),
            (token(grant, THIRD), 401, "unauthorized"),
            (token(grant, sub="mallory"), 401, "unauthorized"),
            (token(grant, grant_id="g-none"), 401, "unauthorized"),
            (token(grant, act={"sub": "svc:agent:x", "agent_id": THIRD}), 401, "unauthorized"),
        ]
        for bearer, status, code in cases:
            with pytest.raises(REFUSALS) as e:
                await rig.calls.start(CALLED, CallCredentials(bearer, "s", "t", "alice"), MESSAGE)
            assert _refused(e) == (status, code), bearer
        assert await runs.count_in_statuses(TENANT, CALLED, ("starting", "running")) == 0
        # 다른 Agent 를 대상으로 둔 위임의 토큰은 그 Agent 의 경로만 연다
        other = await rig.grant("g2", targets=[GrantTarget(kind="agent", id=THIRD)])
        with pytest.raises(REFUSALS) as e:
            await rig.calls.start(CALLED, CallCredentials(token(other, CALLED)), MESSAGE)
        assert _refused(e) == (401, "unauthorized")

    _go(settings, body)


def test_an_ended_or_due_grant_and_a_person_without_chat_are_refused(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        ended = await rig.grant("g-ended", ended_at=utcnow(), end_reason="done")
        due = await rig.grant("g-due", deadline=utcnow() - timedelta(seconds=1))
        for grant in (ended, due):
            with pytest.raises(REFUSALS) as e:
                await rig.start(grant)
            assert _refused(e) == (410, "turn_grant_ended")
        # 비밀번호를 바꾼 사람(tv 가 다르다)과 그 테넌트에서 대화할 수 없는 사람이다
        stale = await rig.grant("g-tv", tv=2)
        with pytest.raises(REFUSALS) as e:
            await rig.start(stale)
        assert _refused(e) == (403, "acts_as_unavailable")
        nobody = Rig(_local(settings), grants, runs, role="operator")
        with pytest.raises(REFUSALS) as e:
            await nobody.start(await rig.grant("g-role"))
        assert _refused(e) == (403, "acts_as_unavailable")
        assert e.value.extra["reason"] == "acts_as_lost_access"

    _go(settings, body)


# -- 호출은 실행이다 (6.3절) ----------------------------------------


def test_a_viewers_call_starts_a_run_as_that_person_and_answers(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        first = await rig.start(grant, outside=False)
        assert first["status"] == "running" and "answer" not in first
        run = await rig.stored(first["run_id"])
        assert (run.source, run.source_id, run.acts_as.sub, run.acts_as.tv) == (
            "agent",
            None,
            "alice",
            3,
        )
        caller = run.caller
        assert caller is not None and (caller.agent_id, caller.session_id, caller.grant_id) == (
            CALLING,
            "s-chat",
            "g1",
        )
        assert (caller.chain, caller.long_bound, caller.untrusted) == ([CALLING], False, False)
        # 시작 워커의 한 걸음이 그 사람으로 계기 agent 의 위임을 열었다
        opened = rig.opened.opened[0]
        assert (opened["trigger"], opened["long_bound"], opened["run_id"]) == (
            "agent",
            False,
            run.id,
        )
        assert (run.acts_as.kind, run.grant_id) == ("person", "g1")
        sent = rig.proxy.calls[0]
        assert (sent["message"], sent["user_sub"], sent["untrusted"]) == (MESSAGE, "alice", False)
        await rig.end_turn(run.id)
        done = await rig.calls.poll(CALLED, run.id, CallCredentials(token(grant)), 0)
        assert done == {"run_id": run.id, "status": "succeeded", "answer": ANSWER}
        rows = [(e.action, e.sub, e.status, e.resource_id, e.via) for e in rig.audit.events]
        assert rows == [
            ("agent_call_start", "alice", 200, run.id, f"agent/{CALLING}"),
            ("agent_call_poll", "alice", 200, run.id, f"agent/{CALLING}"),
        ]
        assert rig.audit.events[0].path_params["agent_id"] == CALLED

    _go(settings, body)


@pytest.mark.parametrize(
    ("status", "reason", "want"),
    [
        ("waiting_approval", None, {"status": "waiting_approval"}),
        ("failed", "acts_as_unavailable", {"status": "failed", "code": "acts_as_unavailable"}),
        ("refused", "approval_deadline", {"status": "refused", "code": "approval_deadline"}),
        ("cancelled", "caller_ended", {"status": "cancelled", "code": "caller_ended"}),
    ],
)
def test_a_settled_run_answers_at_once_with_its_code(settings, status, reason, want):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        run = _call_run("g1", status, reason=reason)
        await runs.insert(run)
        began = time.monotonic()
        answer = await rig.calls.poll(CALLED, run.id, CallCredentials(token(grant)), 25)
        assert answer == {"run_id": run.id, **want}
        assert time.monotonic() - began < 2

    _go(settings, body)


def test_a_republished_agent_refuses_the_revisions_that_attached_its_old_item(settings, quick):
    """게시를 거뒀다가 다시 게시한 Agent 는 새 항목이다. 옛 항목을 붙여 배포한 리비전은 새 항목의
    사용 권한을 보는 저장과 배포를 거치지 않았으므로 부르지 못한다. header 모드도 같다."""

    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        assert (await rig.start(grant))["status"] == "running"
        del rig.hub.trusted[CALLED]
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (409, "agent_source_unpublished")
        rig.hub.trusted[CALLED] = True
        rig.hub.republished[CALLED] = "hub-billing-again"
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (409, "agent_source_unpublished")
        header = Rig(settings, grants, runs)
        header.hub.republished[CALLED] = "hub-billing-again"
        with pytest.raises(REFUSALS) as e:
            await header.calls.start(CALLED, CallCredentials(INSTANCE_TOKEN, "s", "t"), MESSAGE)
        assert _refused(e) == (409, "agent_source_unpublished")
        # 새 항목을 붙여 다시 배포한 리비전은 부른다
        source = node_of(rig.agents.by_id[CALLING].spec, "tools-calculator")["data"]["source"]
        source["id"] = "hub-billing-again"
        assert (await rig.start(grant))["status"] == "running"

    _go(settings, body)


def test_a_called_revision_that_lost_its_prerequisites_answers_the_call_code(settings, quick):
    """memory 노드나 세션 예산을 잃은 리비전은 게시와 저장이 답하는 코드로 거절한다. 실행의 코드가
    먼저 나가지 않는다."""

    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        unmet = ServiceError(409, "agent_source_runs_unmet", "needs a memory node and a budget")
        rig.calls.publishing.call_refusal = lambda doc: unmet  # type: ignore[method-assign]

        def ready(agent: Any, message: str | None) -> int:
            raise ServiceError(409, "runs_need_memory", "no memory node")

        rig.service.ready = ready  # type: ignore[method-assign,assignment]
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (409, "agent_source_runs_unmet")
        # 턴을 받지 않는 Agent 는 실행의 코드 그대로다
        rig.agents.by_id[CALLED].state = "draft"
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (409, "runs_need_memory")

    _go(settings, body)


def test_a_request_without_a_bearer_and_an_unexpected_error_leave_an_audit_row(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        with pytest.raises(REFUSALS) as e:
            await rig.calls.start(CALLED, CallCredentials(None), MESSAGE)
        assert _refused(e)[0] == 401
        grant = await rig.grant()

        async def down(agent_id: str) -> Any:
            raise RuntimeError("the hub store is down")

        rig.hub.get_by_agent = down  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await rig.start(grant)
        rows = [(e.action, e.sub, e.status, e.result, e.error_code) for e in rig.audit.events]
        assert rows == [
            ("agent_call_start", None, 401, "error", "unauthorized"),
            ("agent_call_start", "alice", 500, "error", None),
        ]

    _go(settings, body)


def test_a_first_turn_call_records_the_session_once_the_grant_holds_it(settings, quick):
    """새 세션의 첫 턴은 위임에 세션이 붙기 전에 호출이 올 수 있다. 부르는 실행의 세션을 읽고,
    없으면 위임을 한 번 다시 읽는다."""

    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        calling = _calling_run()
        await runs.insert(calling)
        of_run = await rig.grant("g-run", trigger="run", session_id=None, run_id=calling.id)
        run = await rig.stored((await rig.start(of_run))["run_id"])
        assert run.caller is not None and run.caller.session_id == "s-run"

        chat = await rig.grant("g-chat", session_id=None)
        read = rig.grants.get

        async def attached(grant_id: str) -> Any:
            # 런타임이 턴을 받아들여 세션이 붙었다
            await rig.grants.coll.update_one({"_id": grant_id}, {"$set": {"session_id": "s-new"}})
            return await read(grant_id)

        first = CallCredentials(token(chat))
        caller = await rig.calls._caller(CALLED, first)
        rig.grants.get = attached  # type: ignore[method-assign]
        assert await rig.calls._caller_session(caller) == "s-new"

    _go(settings, body)


def test_a_run_of_another_turn_is_not_found(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        mine, theirs = await rig.grant("g1"), await rig.grant("g2")
        run = _call_run("g2")
        await runs.insert(run)
        for call in (
            rig.calls.poll(CALLED, run.id, CallCredentials(token(mine)), 0),
            rig.calls.cancel(CALLED, run.id, CallCredentials(token(mine)), CALLER_TIMEOUT),
            rig.calls.poll(CALLED, "r-none", CallCredentials(token(theirs)), 0),
        ):
            with pytest.raises(REFUSALS) as e:
                await call
            assert _refused(e) == (404, "not_found")
        assert (await rig.stored(run.id)).stop_requested_at is None

    _go(settings, body)


def test_refusals_before_a_run_record_nothing(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs, **{THIRD: True})
        grant = await rig.grant()
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (409, "agent_source_unpublished")
        rig.hub.trusted[CALLED] = True
        rig.agents.by_id[CALLED].state = "deploying"
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (429, "agent_call_busy")
        rig.agents.by_id[CALLED].state = "deployed"
        node_of(rig.agents.by_id[CALLED].spec, "entry")["data"]["max_input_chars"] = 5
        with pytest.raises(REFUSALS) as e:
            await rig.start(grant)
        assert _refused(e) == (413, "max_input_chars") and e.value.extra["limit"] == 5
        off = Rig(dataclasses.replace(rig.settings, agent_calls_enabled=False), grants, runs)
        with pytest.raises(REFUSALS) as e:
            await off.start(grant)
        assert _refused(e) == (409, "agent_calls_disabled")
        assert await runs.count_created_since(TENANT, CALLED, utcnow() - timedelta(days=1)) == 0
        assert (await grants.get("g1")).calls == 0  # type: ignore[union-attr]
        assert rig.audit.events[0].error_code == "agent_source_unpublished"

    _go(settings, body)


def test_the_persons_rate_is_the_called_agents_entry_limit(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        entry = node_of(rig.agents.by_id[CALLED].spec, "entry")["data"]
        entry["rate_limit"] = {"per_user_per_minute": 2}
        first, second = await rig.grant("g1"), await rig.grant("g2")
        await rig.start(first)
        await rig.start(second)
        with pytest.raises(REFUSALS) as e:
            await rig.start(await rig.grant("g3"))
        assert _refused(e) == (429, "agent_call_rate_limited")

    _go(settings, body)


# -- 사슬 (6.5절) ----------------------------------------


def test_the_chain_comes_from_the_calling_sessions_run_record(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        # 뿌리(ROOT)가 부른 실행의 세션에서 CALLING 이 CALLED 를 부른다. 사슬은 셋이다
        caller = RunCaller(agent_id=ROOT, grant_id="g-root", chain=[ROOT], long_bound=True)
        calling = _calling_run(source="agent", caller=caller)
        await runs.insert(calling)
        grant = await rig.grant(trigger="agent", session_id=None, run_id=calling.id)
        answer = await rig.start(grant)
        run = await rig.stored(answer["run_id"])
        assert run.caller is not None and run.caller.chain == [ROOT, CALLING]
        assert run.caller.long_bound is True
        assert rig.opened.opened[0]["long_bound"] is True
        # 그 실행(CALLED)의 세션이 넷째를 부르면 사슬이 셋을 넘는다
        deep = await rig.grant(
            "g-deep",
            agent_id=CALLED,
            trigger="agent",
            session_id=None,
            run_id=run.id,
            targets=[GrantTarget(kind="agent", id=THIRD)],
        )
        with pytest.raises(REFUSALS) as e:
            await rig.calls.start(THIRD, CallCredentials(token(deep, THIRD)), MESSAGE)
        assert _refused(e) == (409, "agent_call_too_deep")
        # 사슬에 있는 Agent 를 다시 부르면 순환이다
        back = await rig.grant(
            "g-back",
            agent_id=CALLED,
            trigger="agent",
            session_id=None,
            run_id=run.id,
            targets=[GrantTarget(kind="agent", id=ROOT)],
        )
        with pytest.raises(REFUSALS) as e:
            await rig.calls.start(ROOT, CallCredentials(token(back, ROOT)), MESSAGE)
        assert _refused(e) == (409, "agent_call_cycle")

    _go(settings, body)


def test_an_approval_resume_reads_the_same_record_and_starts_no_new_chain(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        caller = RunCaller(agent_id=ROOT, grant_id="g-root", chain=[ROOT, THIRD], long_bound=False)
        calling = _calling_run(source="agent", caller=caller, deadline=utcnow())
        await runs.insert(calling)
        # 결정으로 재개한 턴의 위임은 실행이 아니라 세션을 댄다
        resumed = await rig.grant(trigger="decide", session_id="s-run", long_bound=False)
        with pytest.raises(REFUSALS) as e:
            await rig.start(resumed)
        assert _refused(e) == (409, "agent_call_too_deep")

    _go(settings, body)


def test_a_scheduled_runs_call_has_the_long_bound_and_a_chats_call_the_interactive_one(
    settings, quick
):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        calling = _calling_run()
        await runs.insert(calling)
        from_run = await rig.grant("g-run", trigger="run", session_id=None, run_id=calling.id)
        from_chat = await rig.grant("g-chat")
        bounds = []
        for grant in (from_run, from_chat):
            run = await rig.stored((await rig.start(grant))["run_id"])
            assert run.caller is not None
            bounds.append((run.caller.chain, run.caller.long_bound))
        assert bounds == [([CALLING], True), ([CALLING], False)]

    _go(settings, body)


# -- 한 턴의 호출 수와 상한 (6.3·6.5절) ----------------------------------------


def test_one_turn_starts_five_calls_across_replicas(settings, quick):
    async def body(grants, runs) -> None:
        replicas = [Rig(_local(settings), grants, runs, owner=f"calls-{i}") for i in range(2)]
        grant = await replicas[0].grant()

        async def one(i: int) -> str:
            try:
                await replicas[i % 2].start(grant)
            except ServiceError as e:
                return e.code
            return "started"

        outcomes = await asyncio.gather(*(one(i) for i in range(MAX_CALLS_PER_TURN + 3)))
        assert sorted(outcomes) == ["agent_call_fanout"] * 3 + ["started"] * MAX_CALLS_PER_TURN
        assert (await grants.get("g1")).calls == MAX_CALLS_PER_TURN  # type: ignore[union-attr]
        # 다른 턴의 위임은 제 몫을 따로 센다
        assert (await replicas[1].start(await replicas[0].grant("g2")))["status"] == "running"

    _go(settings, body)


def test_calls_are_capped_apart_from_schedules_webhooks_and_run_now(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        for _ in range(MAX_RUNNING):
            await runs.insert(_calling_run(CALLED, session_id=None))
        now = utcnow()
        assert (await rig.service.cap_error(TENANT, CALLED, now)).code == "agent_run_limit"  # type: ignore[union-attr]
        # 스케줄과 지금 실행이 제 상한에 닿아도 호출은 시작한다
        assert (await rig.start(await rig.grant("g0")))["status"] == "running"
        for i in range(MAX_CALLS_RUNNING - 1):
            await runs.insert(_call_run(f"g-other-{i}"))
        with pytest.raises(REFUSALS) as e:
            await rig.start(await rig.grant("g1"))
        assert _refused(e) == (429, "agent_call_busy")
        # 호출이 제 상한에 닿아도 그 수는 스케줄과 지금 실행의 상한에 들지 않는다
        assert await rig.service._count(TENANT, CALLED, ("running",), RUN_CAPS) == MAX_RUNNING
        assert await rig.service._count(TENANT, CALLED, ("running",), CALL_CAPS) == (
            MAX_CALLS_RUNNING
        )

    _go(settings, body)


# -- 믿지 않는 입력 (6.6절) ----------------------------------------


@pytest.mark.parametrize(
    ("trusted", "outside", "marks_off", "untrusted"),
    [
        (True, False, False, False),
        (False, False, False, True),
        (True, True, False, True),
        (True, False, True, True),
    ],
)
def test_a_call_is_untrusted_unless_everything_says_otherwise(
    settings, quick, trusted, outside, marks_off, untrusted
):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs, **{CALLED: trusted})
        if marks_off:
            tools = node_of(rig.agents.by_id[CALLING].spec, "tools-calculator")["data"]
            tools["outside"] = {"Add": False}
        answer = await rig.start(await rig.grant(), outside=outside)
        run = await rig.stored(answer["run_id"])
        assert run.caller is not None and run.caller.untrusted is untrusted
        assert rig.proxy.calls[0]["untrusted"] is untrusted

    _go(settings, body)


# -- header 모드 (6.4절) ----------------------------------------


def test_header_mode_takes_the_pods_word_and_runs_as_the_agent(settings, quick):
    async def body(grants, runs) -> None:
        rig = Rig(settings, grants, runs)
        assert not settings.token_auth
        creds = CallCredentials(INSTANCE_TOKEN, "s-9", "t-9", "alice%20kim", outside=False)
        answer = await rig.calls.start(CALLED, creds, MESSAGE)
        run = await rig.stored(answer["run_id"])
        assert (run.acts_as.sub, run.acts_as.kind, run.acts_as.cause) == (
            "alice kim",
            "agent",
            "header_mode",
        )
        caller = run.caller
        assert caller is not None and (caller.grant_id, caller.session_id, caller.turn_id) == (
            None,
            "s-9",
            "t-9",
        )
        # 파드의 말을 믿는 모드의 호출은 늘 믿지 않는 입력이다
        assert caller.untrusted is True and rig.opened.opened == []
        # 그 턴의 다섯째까지만 시작하고, 다른 턴은 따로 센다
        for _ in range(MAX_CALLS_PER_TURN - 1):
            await rig.calls.start(CALLED, creds, MESSAGE)
        with pytest.raises(REFUSALS) as e:
            await rig.calls.start(CALLED, creds, MESSAGE)
        assert _refused(e) == (429, "agent_call_fanout")
        other_turn = dataclasses.replace(creds, turn_id="t-10")
        polled = await rig.calls.poll(CALLED, run.id, creds, 0)
        assert polled["run_id"] == run.id
        with pytest.raises(REFUSALS) as e:
            await rig.calls.poll(CALLED, run.id, other_turn, 0)
        assert _refused(e) == (404, "not_found")

    run_stores(settings, STORES, body)


def test_header_mode_checks_the_token_the_turn_and_that_the_revision_uses_the_agent(
    settings, quick
):
    async def body(grants, runs) -> None:
        rig = Rig(settings, grants, runs)
        cases = [
            (CALLED, CallCredentials("not-a-token", "s", "t"), (401, "unauthorized")),
            (CALLED, CallCredentials(INSTANCE_TOKEN), (401, "unauthorized")),
            # 부르는 리비전이 Tool 로 쓰지 않는 Agent 다
            (THIRD, CallCredentials(INSTANCE_TOKEN, "s", "t"), (403, "forbidden")),
        ]
        for called, creds, want in cases:
            with pytest.raises(REFUSALS) as e:
                await rig.calls.start(called, creds, MESSAGE)
            assert _refused(e) == want
        # 다른 테넌트의 Agent 는 게시하지 않은 Agent 와 같이 답한다
        rig.agents.by_id[CALLED].tenant = "other"
        with pytest.raises(REFUSALS) as e:
            await rig.calls.start(CALLED, CallCredentials(INSTANCE_TOKEN, "s", "t"), MESSAGE)
        assert _refused(e) == (409, "agent_source_unpublished")

    run_stores(settings, STORES, body)


# -- 부른 턴의 끝과 쓸기 (6.5절) ----------------------------------------


def test_the_sweep_cancels_first_turn_calls_of_closed_grants_only(settings):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        await rig.grant("g-open")
        await rig.grant("g-ended", ended_at=utcnow(), end_reason="done")
        await rig.grant("g-due", deadline=utcnow() - timedelta(seconds=1))
        docs = {
            "open": _call_run("g-open"),
            "ended-running": _call_run("g-ended"),
            "ended-starting": _call_run("g-ended", "starting", session_id=None, turn_id=None),
            "due": _call_run("g-due"),
            "purged": _call_run("g-purged"),
            # 승인을 기다리는 실행과 승인 뒤 다시 도는 실행은 제 마감에 둔다
            "waiting": _call_run("g-ended", "waiting_approval", deadline=utcnow()),
            "resumed": _call_run("g-ended", deadline=utcnow() + timedelta(hours=1)),
        }
        for run in docs.values():
            await runs.insert(run)
        assert await rig.calls.sweep() == 3
        after = {name: await rig.stored(run.id) for name, run in docs.items()}
        marked = {name for name, run in after.items() if run.stop_reason == CALLER_ENDED}
        assert marked == {"ended-running", "ended-starting", "due", "purged"}
        assert (after["ended-starting"].status, after["ended-starting"].reason) == (
            "cancelled",
            CALLER_ENDED,
        )
        # 도는 턴은 런타임에서 멈추고 그 `turn_end` 가 기록을 끝낸다
        assert {r.id for r in rig.turns.stopped} == {
            docs[name].id for name in ("ended-running", "due", "purged")
        }
        assert after["ended-running"].status == "running"
        # 끝난 위임은 틱마다 다시 온다. 이미 표시한 실행은 건드리지도 다시 멈추지도 않는다
        seen_at = after["ended-running"].last_event_at
        assert await rig.calls.sweep() == 0 and len(rig.turns.stopped) == 3
        assert (await rig.stored(docs["ended-running"].id)).last_event_at == seen_at

    _go(settings, body)


def test_a_marked_call_whose_turn_never_ends_is_settled_by_the_watch(settings):
    """멈추라고 했지만 `turn_end` 가 오지 않는 호출(파드가 없어졌다)이다. 쓸기가 틱마다 그 기록을
    다시 건드리지 않아야 조용한 시간이 지난 뒤 지켜보기가 `caller_ended` 로 끝낸다."""

    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        await rig.grant("g-ended", ended_at=utcnow(), end_reason="done")
        run = _call_run("g-ended")
        await runs.insert(run)
        worker = RunWatchWorker(
            rig.agents,  # type: ignore[arg-type]
            runs,
            rig.proxy,  # type: ignore[arg-type]
            owner="w1",
            interval=15,
            lease_seconds=60,
            caller_sweep=rig.calls.sweep,
        )
        for _ in range(3):
            await worker.tick()
        marked = await rig.stored(run.id)
        assert (marked.status, marked.stop_reason) == ("running", CALLER_ENDED)
        assert [r.id for r in rig.turns.stopped] == [run.id]
        # 표시를 남긴 뒤로 조용한 시간이 지났다
        await runs.coll.update_one(
            {"_id": run.id}, {"$set": {"last_event_at": utcnow() - timedelta(hours=2)}}
        )
        for _ in range(3):
            await worker.tick()
        settled = await rig.stored(run.id)
        assert (settled.status, settled.reason) == ("cancelled", CALLER_ENDED)
        assert [r.id for r in rig.turns.stopped] == [run.id]

    _go(settings, body)


def test_a_grants_end_cancels_its_calls_at_once(settings):
    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        mine, other = _call_run("g1"), _call_run("g2")
        await runs.insert(mine)
        await runs.insert(other)
        await rig.calls.caller_ended("g1")
        assert (await rig.stored(mine.id)).stop_reason == CALLER_ENDED
        assert (await rig.stored(other.id)).stop_requested_at is None
        assert [r.id for r in rig.turns.stopped] == [mine.id]

    _go(settings, body)


# -- 재어 본 것: 끝에서 답까지, 묻는 사이의 재시작, 취소 ----------------------------------------


def test_a_poll_answers_within_two_seconds_of_the_runs_end(settings, capsys):
    """실행의 끝 이벤트부터 전사를 읽어 답하기까지다."""

    async def body(grants, runs) -> float:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        run = _call_run("g1")
        await runs.insert(run)
        poll = asyncio.create_task(
            rig.calls.poll(CALLED, run.id, CallCredentials(token(grant)), 25)
        )
        await asyncio.sleep(1.2)
        assert not poll.done()
        ended = time.monotonic()
        await rig.end_turn(run.id)
        answer = await poll
        took = time.monotonic() - ended
        assert answer == {"run_id": run.id, "status": "succeeded", "answer": ANSWER}
        assert rig.proxy.transcript_reads == 1
        return took

    took = _go(settings, body)
    with capsys.disabled():
        print(f"\nrun end -> poll answer: {took:.3f}s")
    assert took < 2.0


def test_a_restart_during_a_poll_costs_that_poll_only(settings, capsys):
    """묻는 사이에 플랫폼이 내려가면 그 묻기만 잃는다. 다시 뜬 플랫폼이 다음 묻기에 같은 실행을
    답하고, 시작하다 내려간 플랫폼이 남긴 기록은 리스가 끝난 뒤 시작 워커가 잡는다."""

    async def body(grants, runs) -> float:
        before = Rig(_local(settings), grants, runs, owner="calls-before")
        grant = await before.grant()
        run = _call_run("g1")
        await runs.insert(run)
        poll = asyncio.create_task(
            before.calls.poll(CALLED, run.id, CallCredentials(token(grant)), 25)
        )
        await asyncio.sleep(0.3)
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
        await before.end_turn(run.id)
        after = Rig(_local(settings), grants, runs, owner="calls-after")
        began = time.monotonic()
        answer = await after.calls.poll(CALLED, run.id, CallCredentials(token(grant)), 25)
        took = time.monotonic() - began
        assert answer["status"] == "succeeded" and answer["answer"] == ANSWER
        # 리스를 쥔 채 `starting` 으로 남은 기록이다
        lease = ItemLease(owner="calls-before", until=utcnow() - timedelta(seconds=1))
        left = _call_run(
            "g1", "starting", session_id=None, turn_id=None, message=MESSAGE, lease=lease
        )
        await runs.insert(left)
        held = await after.starter.acquire()
        assert held is not None and held.id == left.id
        await after.starter.process(held)
        assert (await after.stored(left.id)).status == "running"
        return took

    took = _go(settings, body)
    with capsys.disabled():
        print(f"\nfirst poll after a restart: {took:.3f}s")
    assert took < 2.0


def test_a_cancel_reaches_the_run_and_its_turn(settings, capsys):
    async def body(grants, runs) -> float:
        rig = Rig(_local(settings), grants, runs)
        grant = await rig.grant()
        running, starting = _call_run("g1"), _call_run("g1", "starting", session_id=None)
        await runs.insert(running)
        await runs.insert(starting)
        creds = CallCredentials(token(grant))
        began = time.monotonic()
        first = await rig.calls.cancel(CALLED, running.id, creds, CALLER_TIMEOUT)
        took = time.monotonic() - began
        # 도는 턴은 런타임에서 멈추라고 하고 표시의 까닭으로 끝난다
        assert first == {"run_id": running.id, "status": "running", "cancelled": False}
        assert [(r.id, r.session_id, r.turn_id) for r in rig.turns.stopped] == [
            (running.id, "s1", "t1")
        ]
        assert (await rig.stored(running.id)).stop_reason == CALLER_TIMEOUT
        second = await rig.calls.cancel(CALLED, starting.id, creds, CALLER_ENDED)
        assert second == {"run_id": starting.id, "status": "cancelled", "cancelled": True}
        assert (await rig.stored(starting.id)).reason == CALLER_ENDED
        # 끝난 실행의 취소는 아무것도 바꾸지 않는다
        again = await rig.calls.cancel(CALLED, starting.id, creds, CALLER_TIMEOUT)
        assert again["status"] == "cancelled"
        assert (await rig.stored(starting.id)).reason == CALLER_ENDED
        return took

    took = _go(settings, body)
    with capsys.disabled():
        print(f"\ncancel -> stop sent to the run's turn: {took:.3f}s")
    assert took < 2.0


def test_a_waking_agent_is_started_again_within_the_request(settings, monkeypatch):
    """깨우는 중인 Agent 의 시작은 틱을 기다리지 않고 미룬 때에 이 요청이 다시 한다."""

    class Waking:
        def __init__(self) -> None:
            self.asked = 0

        async def admit(self, doc: Any, reason: str) -> None:
            self.asked += 1
            if self.asked == 1:
                raise ServiceError(503, "agent_waking", "waking")

    monkeypatch.setattr("daegam.services.runs.start.WAKE_DEFER", timedelta(seconds=0.2))
    monkeypatch.setattr(agent_call_runs, "REQUEST_WAIT_SECONDS", 1.0)

    async def body(grants, runs) -> None:
        rig = Rig(_local(settings), grants, runs)
        rig.starter.sleep = Waking()
        answer = await rig.start(await rig.grant())
        assert rig.starter.sleep.asked == 2 and answer["status"] == "running"
        assert (await rig.stored(answer["run_id"])).attempts == 0

    _go(settings, body)
