"""스케줄과 실행 기록을 다루는 서비스 (P41 1·3·4·10절). 웹훅 트리거의 관리는 `triggers.py` 다.

라우터(`api/agent_runs.py`)는 권한과 Agent 조회만 하고 규칙은 모두 여기 있다. 스케줄을 만들거나
바꾸거나 다시 켜는 사람이 그 스케줄의 묶인 사람이 된다(6절). 그 테넌트에서 실행에 묶일 role 이 없는
사람(platform-admin 의 `*` 줄만 가진 사람)은 403 이다. 저장할 때 전제 조건(10절)을 보고 맞지
않으면 409 로 거절한다. 입력 길이만은 런타임과 같은 413 `max_input_chars` 다. 지금 실행은
`starting` 기록 하나를 넣고, 시작 워커가 그 기록을 집는다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

from daegam.audit.store import MongoAuditStore, record_system_event
from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.services.agent_graph import Analysis, EntryData, Issue, analyze_structure
from daegam.services.agent_runtime import serves_turns
from daegam.services.runs import clock
from daegam.services.runs.approvals import RunApprovals
from daegam.services.runs.errors import RunError
from daegam.services.runs.events import approval_deadline
from daegam.services.runs.person import LOST_ACCESS, BoundPersonCheck, cause_of, ended_reason
from daegam.services.runs.trigger_rule import check_trigger_rule
from daegam.services.runs.trigger_secrets import TriggerSecrets
from daegam.services.runs.turns import RunTurns
from daegam.services.turn_grants import revision_limits
from daegam.services.turn_person import person_of
from daegam.services.usage.reports import ReportError, check_zone
from daegam.store.agent_runs import (
    CALL_SOURCE,
    MOVING_STATUSES,
    RUN_CAP_SOURCES,
    STOP_NOW_STATUSES,
    STOPPED,
    ActsAs,
    AgentRun,
    MongoAgentRunStore,
    RunStatus,
    input_digest,
    new_run,
)
from daegam.store.agent_schedules import AgentSchedule, MongoAgentScheduleStore
from daegam.store.agent_triggers import MongoAgentTriggerStore
from daegam.store.agents import Agent, AgentRevision, AgentSpec
from daegam.store.base import new_id, utcnow
from daegam.store.personal_tokens import Revocation
from daegam.store.run_sources import MongoRunSourceStore, RunSource, paused_fields

logger = logging.getLogger("daegam.runs")

# 런타임이 받는 입력 길이의 위 끝. 리비전의 max_input_chars 가 이보다 크면 이것이 한도다
RUNTIME_INPUT_CAP = 200_000
# 스케줄 만료 (3절). 바꾸거나 다시 켜면 고른 길이로 새로 센다
EXPIRY_DEFAULT_DAYS = 90
EXPIRY_MAX_DAYS = 365
# Agent 하나의 상한 (10절). 비용을 막는 장치라 레플리카 사이에서는 조금 넘을 수 있다
MAX_SOURCES = 20
MAX_RUNNING = 5
MAX_WAITING = 20
MAX_RUNS_PER_DAY = 500
# 다른 Agent 가 Tool 로 부른 실행의 상한 (P46 6.3절). 호출받는 Agent 마다 스케줄·웹훅·지금 실행과
# 따로 센다
MAX_CALLS_RUNNING = 10
MAX_CALLS_WAITING = 20
MAX_CALLS_PER_DAY = 1000
AGENT_RUN_LIMIT = "agent_run_limit"
AGENT_CALL_BUSY = "agent_call_busy"
# 상한에 걸린 지금 실행의 Retry-After
CAP_RETRY_AFTER = "60"
# Agent 를 내리거나 지우거나 소스를 지워 끝난 실행의 까닭
AGENT_UNDEPLOYED = "agent_undeployed"
AGENT_DELETED = "agent_deleted"
SOURCE_DELETED = "source_deleted"
# 요청 없이 실행 소스를 멈춘 감사 행의 주체
RUNS_SUB = "system:runs"
# 사람이 손으로 멈춘 소스의 까닭
PAUSED_BY_HAND = "paused"
# 배포가 소스를 멈춘 까닭과 그 경고 (3·10절)
AGENT_CHANGED = "agent_changed"
PREREQUISITES_FAILED = "prerequisites_failed"
RUNS_PAUSED_BY_DEPLOY = "runs_paused_by_deploy"


@dataclass(frozen=True)
class SourceKind:
    """실행 소스의 한 종류 (3·4절). 실행 기록의 `source`, 소스를 둔 스토어, 감사 행의 리소스,
    트리거 규칙을 보는지다."""

    source: str
    store: MongoRunSourceStore[Any]
    resource: str
    needs_rule: bool = False


@dataclass(frozen=True)
class RunCaps:
    """한 갈래의 실행이 Agent 하나에서 함께 지키는 상한이다. sources 의 실행만 세고, 걸리면 code 로
    거절한다. noun 은 거절 문장이 그 실행을 부르는 말이다."""

    sources: tuple[str, ...]
    running: int
    waiting: int
    per_day: int
    code: str
    noun: str

    def error(self, running: int, waiting: int, today: int) -> RunError | None:
        detail: str | None = None
        if running >= self.running:
            detail = f"an agent runs at most {self.running} {self.noun} at once"
        elif waiting >= self.waiting:
            detail = f"an agent holds at most {self.waiting} {self.noun} waiting for approval"
        elif today >= self.per_day:
            detail = f"an agent starts at most {self.per_day} {self.noun} a day"
        if detail is None:
            return None
        return RunError(429, self.code, detail, headers={"Retry-After": CAP_RETRY_AFTER})


RUN_CAPS = RunCaps(
    RUN_CAP_SOURCES, MAX_RUNNING, MAX_WAITING, MAX_RUNS_PER_DAY, AGENT_RUN_LIMIT, "runs"
)
CALL_CAPS = RunCaps(
    (CALL_SOURCE,),
    MAX_CALLS_RUNNING,
    MAX_CALLS_WAITING,
    MAX_CALLS_PER_DAY,
    AGENT_CALL_BUSY,
    "calls from other agents",
)


def caps_of(source: str) -> RunCaps:
    """그 출처의 실행이 지키는 상한이다."""
    return CALL_CAPS if source == CALL_SOURCE else RUN_CAPS


@dataclass(frozen=True)
class ScheduleInput:
    cron: str
    time_zone: str
    message: str
    expires_in_days: int


def acts_as_of(user: Principal) -> ActsAs:
    """요청한 사람을 묶인 사람으로 적는다. local 모드면 그때의 `tv` 도 적는다 (6절)."""
    p = person_of(user)
    return ActsAs(sub=p.sub, username=p.username or None, tv=p.tv)


def _entry(a: Analysis) -> EntryData:
    entry = a.typed(a.entry, EntryData) if a.entry is not None else None
    return entry if entry is not None else EntryData()


def input_limit(spec: AgentSpec) -> int:
    """리비전이 받는 입력 길이. 런타임의 위 끝을 넘지 않는다."""
    return min(_entry(analyze_structure(spec)).max_input_chars, RUNTIME_INPUT_CAP)


async def _rule(cron: str, time_zone: str, now: datetime) -> tuple[str, str, datetime]:
    """규칙과 시간대를 읽어 (규칙, 시간대 이름, 다음 울림)을 돌려준다. 1년을 걸어 보는 간격
    검사는 스레드에서 돌려 이벤트 루프를 붙잡지 않는다."""
    try:
        zone = check_zone(time_zone)
    except ReportError as e:
        raise RunError(422, "unknown_time_zone", str(e)) from e
    try:
        rule = clock.parse_rule(cron)
    except clock.CronError as e:
        raise RunError(422, "schedule_invalid_cron", str(e)) from e
    if await asyncio.to_thread(clock.too_frequent, rule, zone, now):
        raise RunError(
            422,
            "schedule_too_frequent",
            "two firings of this rule come closer than 5 minutes within the next year",
        )
    return rule, zone.key, clock.next_fire(rule, zone, now)


def expiry(days: int, now: datetime) -> datetime:
    if not 1 <= days <= EXPIRY_MAX_DAYS:
        raise RunError(
            422,
            "invalid_expiry",
            f"expiry must be 1 to {EXPIRY_MAX_DAYS} days",
            max=EXPIRY_MAX_DAYS,
        )
    return now + timedelta(days=days)


class RunService:
    # 런타임 프록시는 AgentService 가 만들고, AgentService 는 이 서비스의 삭제 훅을 받으므로 만든
    # 뒤에 붙인다 (wiring.py `_check_wiring`)
    late_hooks: ClassVar[frozenset[str]] = frozenset({"turns"})

    def __init__(
        self,
        runs: MongoAgentRunStore,
        schedules: MongoAgentScheduleStore,
        settings: Settings,
        *,
        person: BoundPersonCheck,
        triggers: MongoAgentTriggerStore,
        trigger_secrets: TriggerSecrets,
        audit: MongoAuditStore | None = None,
    ):
        self.runs = runs
        self.schedules = schedules
        self.triggers = triggers
        self.settings = settings
        self.person = person
        # 사람이 접근을 잃어 멈춘 소스를 시스템 주체의 감사 행으로 남긴다
        self.audit = audit
        # 도는 실행의 턴을 런타임에서 멈춘다(late_hooks). 없으면(시험) 중지 표시만 남는다
        self.turns: RunTurns | None = None
        # 실행 세션의 결정과 승인자의 받은 결정함 (7절)
        self.approvals = RunApprovals(self)
        # 웹훅 트리거의 서명 비밀. Agent 를 지울 때 그 Secret 도 지운다 (4절)
        self.trigger_secrets = trigger_secrets
        # 실행 소스의 종류. 소스를 다루는 곳은 모두 이 표를 따른다
        self.schedule_kind = SourceKind("schedule", schedules, "agent-schedules")
        self.trigger_kind = SourceKind("webhook", triggers, "agent-triggers", needs_rule=True)
        self.kinds = (self.schedule_kind, self.trigger_kind)

    def kind_of(self, run: AgentRun) -> SourceKind | None:
        """실행의 소스 종류. 지금 실행과 다른 Agent 가 부른 실행은 소스가 없다."""
        if run.source_id is None:
            return None
        return next((k for k in self.kinds if k.source == run.source), None)

    def untrusted(self, run: AgentRun) -> bool:
        """런타임이 입력을 믿지 않는 실행으로 다룰 것인가. 웹훅 본문은 밖에서 온 글이고(4절), 다른
        Agent 가 부른 실행은 호출이 정해 기록에 둔다(P46 6.6절)."""
        if run.caller is not None:
            return run.caller.untrusted
        return self.kind_of(run) is self.trigger_kind

    # -- 묶인 사람 (6절) ---------------------------------------------------------------

    async def bind(self, user: Principal, tenant: str) -> ActsAs:
        """요청한 사람을 묶는다. 그 테넌트에서 실행에 묶일 role 이 없으면 403 이다."""
        if not await self.person.may_bind(user.sub, tenant):
            raise RunError(
                403,
                "forbidden",
                "runs act as the person who sets them up, and that person needs a role in this "
                "tenant that grants agent:write and agent:chat",
            )
        return acts_as_of(user)

    async def pause_person(
        self, sub: str, cause: str, *, tenant: str | None = None, agent_id: str | None = None
    ) -> int:
        """sub 에게 묶인 켜진 스케줄과 트리거를 멈추고 그 열린 실행을 중지처럼 끝낸다(까닭은
        `ended_reason`). 소스의 종류와 테넌트마다 원인을 실은 감사 행 하나를 남긴다. 멈춘 수다."""
        now = utcnow()
        total = 0
        for kind in self.kinds:
            paused = await kind.store.pause_bound(
                sub, reason=LOST_ACCESS, cause=cause, now=now, tenant=tenant, agent_id=agent_id
            )
            for source_id in paused:
                await self.end_open_for_source(source_id, ended_reason(cause), now)
            total += len(paused)
            if self.audit is None:
                continue
            for name in sorted(set(paused.values())):
                await record_system_event(
                    self.audit,
                    sub=RUNS_SUB,
                    tenant=name,
                    action=LOST_ACCESS,
                    resource=kind.resource,
                    resource_id=sub,
                    path="runs",
                    error_code=cause,
                )
        return total

    async def on_lost_access(self, sub: str, reason: Revocation, tenant: str | None) -> int:
        """접근 회수 훅(`AccessRevoker.on_lost`). 계정 사건이면 모든 테넌트의 것을, 소속에서
        빠졌으면 그 테넌트의 것을 멈춘다."""
        return await self.pause_person(sub, cause_of(reason), tenant=tenant)

    async def end_agent_runs(self, tenant: str, agent_id: str, *, deleting: bool) -> None:
        """Agent 를 내리거나 지우며 그 인스턴스의 턴을 멈추기 전에 부른다. 열린 실행에
        `agent_undeployed`·`agent_deleted` 표시를 남기고 바로 끝낼 수 있는 것은 끝낸다. 멈춘 턴의
        `turn_end` 가 그 까닭으로 실행을 끝낸다. 런타임은 부르지 않는다. 인스턴스의 턴은 해제와
        삭제가 모두 멈춘다(P78 5절)."""
        reason = AGENT_DELETED if deleting else AGENT_UNDEPLOYED
        await self.runs.cancel_open_for_agent(tenant, agent_id, reason, utcnow())
        if deleting:
            return
        # 내린 Agent 의 스케줄과 트리거는 누가 다시 켜기 전까지 울리지 않는다 (3·4절). 열린 실행은
        # 위에서 끝냈고 턴은 해제가 인스턴스째 멈추므로 실행마다 런타임을 부르지 않는다
        for kind in self.kinds:
            await kind.store.pause_for_agent(tenant, agent_id, AGENT_UNDEPLOYED, utcnow())

    # -- 배포 (3·7·10절) -------------------------------------------------------------------

    async def on_deploy(
        self, agent: Agent, deployer: Principal, previous: AgentRevision | None
    ) -> list[Issue]:
        """배포가 새 리비전을 박은 뒤 부른다. 켜진 스케줄과 트리거마다 새 리비전의 전제 조건을 보고,
        맞지 않으면 `prerequisites_failed` 로 멈춘다. 맞으면 리비전이 바뀐 소스를 배포한 사람에게
        묶인 것은 새 리비전으로 옮기고, 다른 사람에게 묶인 것은 `agent_changed` 로 멈춘다.
        같은 리비전의 재배포는 묶음을 바꾸지 않는다. 세션 TTL 이 바뀌면 승인을 기다리는 실행의
        마감을 새 TTL 로 다시 센다. 멈춘 것이 있으면 경고 하나다."""
        revision = agent.deployed
        if revision is None:
            return []
        schedules, broken = await self._redeploy(self.schedule_kind, agent, revision, deployer)
        triggers, broken_triggers = await self._redeploy(
            self.trigger_kind, agent, revision, deployer
        )
        broken += broken_triggers
        if previous is not None:
            await self._redate_deadlines(agent, previous, revision)
        if not (schedules or triggers or broken):
            return []
        message = (
            f"this deploy paused {schedules} schedule(s) and {triggers} trigger(s) that run as "
            f"someone else, and {broken} whose settings no longer hold"
        )
        return [Issue(code=RUNS_PAUSED_BY_DEPLOY, message=message, level="warning")]

    async def _redeploy(
        self, kind: SourceKind, agent: Agent, revision: AgentRevision, deployer: Principal
    ) -> tuple[int, int]:
        """한 종류의 켜진 소스를 새 리비전에 맞춘다. 다른 사람에게 묶여 멈춘 수와 전제 조건이 맞지
        않아 멈춘 수다. 그사이 지워진 소스는 건너뛴다."""
        changed = broken = 0
        for source in await kind.store.list_for_agent(agent.tenant, agent.id):
            if source.paused:
                continue
            try:
                outcome = await self._redeployed(kind, source, agent, revision, deployer)
            except RunError as e:
                if e.status != 404:
                    raise
                continue
            changed += outcome == AGENT_CHANGED
            broken += outcome == PREREQUISITES_FAILED
        return changed, broken

    async def _redeployed(
        self,
        kind: SourceKind,
        source: RunSource,
        agent: Agent,
        revision: AgentRevision,
        deployer: Principal,
    ) -> str | None:
        """켜진 소스 하나를 새 리비전에 맞춘다. 멈췄으면 그 까닭이다."""
        try:
            self.check_source(kind, agent, revision, source.run_message())
        except RunError:
            await self.pause_source(kind.store, source, PREREQUISITES_FAILED)
            return PREREQUISITES_FAILED
        if source.revision == revision.revision:
            return None
        if source.acts_as.sub == deployer.sub:
            fields = {"revision": revision.revision, "updated_at": utcnow()}
            await self.save_source(kind.store, source, fields)
            return None
        await self.pause_source(kind.store, source, AGENT_CHANGED)
        return AGENT_CHANGED

    async def _redate_deadlines(
        self, agent: Agent, previous: AgentRevision, revision: AgentRevision
    ) -> None:
        """세션 TTL 이 바뀐 배포에서 승인을 기다리는 실행의 마감을 새 TTL 로 옮긴다 (7절). 마감은
        마지막 턴에 TTL 을 더한 것이라 두 TTL 의 마감 차이만큼 옮긴다."""
        old = revision_limits(previous).session_ttl_seconds
        new = revision_limits(revision).session_ttl_seconds
        if old == new:
            return
        base = utcnow()
        shift = approval_deadline(base, new) - approval_deadline(base, old)
        await self.runs.shift_deadlines(agent.tenant, agent.id, shift)

    async def forget_agent(self, tenant: str, agent_id: str) -> None:
        """Agent 를 지울 때 그 열린 실행을 끝내고 스케줄과 트리거와 트리거의 Secret 을 지운다
        (3·4절). 런타임은 부르지 않는다."""
        await self.runs.cancel_open_for_agent(tenant, agent_id, AGENT_DELETED, utcnow())
        for kind in self.kinds:
            await kind.store.delete_for_agent(tenant, agent_id)
        try:
            await self.trigger_secrets.drop_agent(agent_id)
        except Exception as e:  # noqa: BLE001 - 남은 Secret 은 리컨실의 쓸기가 걷는다
            logger.warning("agent %s: its webhook secret was not deleted: %s", agent_id, e)

    # -- 중지 (7절) --------------------------------------------------------------------

    async def end_open_for_source(self, source_id: str, reason: str, now: datetime) -> None:
        """소스를 멈추거나 지울 때 그 열린 실행을 중지처럼 끝내고 도는 턴은 런타임에서 멈춘다."""
        await self.stop_turns(await self.runs.cancel_open_for_source(source_id, reason, now))

    async def stop_turns(self, runs: list[AgentRun]) -> None:
        """중지 표시만 남은 도는 실행의 턴을 런타임에서 멈춘다."""
        if self.turns is None:
            return
        for run in runs:
            await self.turns.stop(run)

    async def stop_if_marked(self, run: AgentRun, *, turn_id: str | None = None) -> bool:
        """기록을 다시 읽어 중지 표시가 있으면 그 턴을 런타임에서 멈춘다. 턴을 막 연 시작 워커와
        결정 경로가 쓴다. turn_id 를 주면 그 시도를 멈춘다. 멈추라고 했으면 참이다."""
        current = await self.runs.get(run.tenant, run.agent_id, run.id)
        if current is None or current.stop_requested_at is None:
            return False
        if turn_id is not None:
            current = current.model_copy(update={"turn_id": turn_id})
        await self.stop_turns([current])
        return True

    # -- 전제 조건 (10절) -------------------------------------------------------------

    def ready(self, agent: Agent, message: str | None, kind: SourceKind | None = None) -> int:
        """실행이 쓸 핀된 리비전을 보고 그 번호를 돌려준다. 턴을 받지 않는 Agent 는 409
        not_deployed 이고, 전제 조건이 맞지 않으면 `check_source` 의 거절이다."""
        revision = agent.deployed
        if revision is None or not serves_turns(agent):
            raise RunError(409, "not_deployed", f"agent {agent.name!r} is not deployed")
        self.check_source(kind, agent, revision, message)
        return revision.revision

    def check_trigger(self, agent: Agent) -> int:
        """트리거를 저장하는 전제 조건 (4·10절). `ready` 에 트리거 규칙을 더한 것이다."""
        return self.ready(agent, None, self.trigger_kind)

    def check_source(
        self,
        kind: SourceKind | None,
        agent: Agent,
        revision: AgentRevision,
        message: str | None,
    ) -> None:
        """소스 종류의 전제 조건 (10절). 배포는 아직 턴을 받지 않는 Agent 로 부른다. 트리거는
        트리거 규칙도 지나야 한다(4절)."""
        self.check_revision(revision, message)
        if kind is not None and kind.needs_rule:
            check_trigger_rule(agent)

    def check_revision(self, revision: AgentRevision, message: str | None) -> None:
        """리비전 하나의 전제 조건 (10절). 맞지 않으면 409(입력 길이는 413)다. 메모리 노드가 세션
        색인을 나누고, 세션 예산이 실행의 지출을 막고, 이벤트 수집이 상태를 옮긴다. 배포는 상태와
        상관없이 새 리비전을 이것으로 본다."""
        a = analyze_structure(revision)
        if not a.main_has_memory:
            raise RunError(
                409, "runs_need_memory", "runs need a memory node so sessions are shared"
            )
        if revision.service.budget.session_tokens is None:
            raise RunError(409, "run_budget_required", "runs need a session token budget")
        if not self.settings.telemetry_ingest_enabled:
            raise RunError(409, "runs_need_events", "runs need the event ingest")
        if message is not None:
            limit = min(_entry(a).max_input_chars, RUNTIME_INPUT_CAP)
            if len(message) > limit:
                raise RunError(
                    413,
                    "max_input_chars",
                    f"the message has {len(message)} characters, more than {limit}",
                    limit=limit,
                )

    # -- 스케줄 (3절) ------------------------------------------------------------------

    async def create_schedule(
        self, agent: Agent, user: Principal, data: ScheduleInput
    ) -> AgentSchedule:
        revision = self.ready(agent, data.message)
        acts_as = await self.bind(user, agent.tenant)
        await self.require_room(self.schedules, agent)
        now = utcnow()
        rule, zone, next_run_at = await _rule(data.cron, data.time_zone, now)
        schedule = AgentSchedule(
            id=new_id(),
            tenant=agent.tenant,
            agent_id=agent.id,
            acts_as=acts_as,
            revision=revision,
            cron=rule,
            time_zone=zone,
            message=data.message,
            expires_at=expiry(data.expires_in_days, now),
            expires_in_days=data.expires_in_days,
            next_run_at=next_run_at,
            created_at=now,
            updated_at=now,
        )
        await self.schedules.insert(schedule)
        return schedule

    async def _save(self, schedule: AgentSchedule, fields: dict) -> AgentSchedule:
        return await self.save_source(self.schedules, schedule, fields)

    async def require_room(self, store: MongoRunSourceStore[Any], agent: Agent) -> None:
        """Agent 가 이 종류의 소스를 더 가질 수 있는가 (10절). 스케줄과 트리거를 따로 센다."""
        if await store.count_for_agent(agent.tenant, agent.id) >= MAX_SOURCES:
            raise RunError(
                409,
                "run_source_limit",
                f"an agent holds at most {MAX_SOURCES} schedules and {MAX_SOURCES} triggers",
            )

    async def save_source[S: RunSource](
        self, store: MongoRunSourceStore[S], source: S, fields: dict[str, Any]
    ) -> S:
        """소스의 변경을 적는다. 그사이 지워졌으면 404 다."""
        updated = await store.update(source.id, fields)
        if updated is None:
            raise RunError(404, "not_found", f"{store.model.__name__} {source.id} not found")
        return updated

    async def pause_source[S: RunSource](
        self, store: MongoRunSourceStore[S], source: S, reason: str
    ) -> S:
        """멈추고 그 소스의 열린 실행을 중지처럼 끝낸다 (3절). 묶인 사람은 바꾸지 않는다."""
        now = utcnow()
        updated = await self.save_source(store, source, paused_fields(reason, now))
        await self.end_open_for_source(source.id, reason, now)
        return updated

    async def update_schedule(
        self,
        agent: Agent,
        schedule: AgentSchedule,
        user: Principal,
        *,
        cron: str | None = None,
        time_zone: str | None = None,
        message: str | None = None,
        expires_in_days: int | None = None,
    ) -> AgentSchedule:
        """바꾼 칸만 적는다. 무엇을 바꾸든 바꾼 사람에게 다시 묶고, 만료(고른 길이)와 다음 울림을
        지금부터 센다."""
        new_message = message if message is not None else schedule.message
        revision = self.ready(agent, new_message)
        acts_as = await self.bind(user, agent.tenant)
        now = utcnow()
        rule, zone, next_run_at = await _rule(
            cron if cron is not None else schedule.cron,
            time_zone if time_zone is not None else schedule.time_zone,
            now,
        )
        days = expires_in_days if expires_in_days is not None else schedule.expires_in_days
        return await self._save(
            schedule,
            {
                "cron": rule,
                "time_zone": zone,
                "message": new_message,
                "acts_as": acts_as.model_dump(),
                "revision": revision,
                "expires_at": expiry(days, now),
                "expires_in_days": days,
                "next_run_at": next_run_at,
                "updated_at": now,
            },
        )

    async def pause_schedule(self, schedule: AgentSchedule, reason: str) -> AgentSchedule:
        return await self.pause_source(self.schedules, schedule, reason)

    async def pause_own[S: RunSource](
        self, store: MongoRunSourceStore[S], sub: str, source_id: str
    ) -> S | None:
        """sub 에게 묶인 소스를 손으로 멈춘다(runs-as-me). 묶인 사람이 아니게 됐으면 None 이고
        아무것도 바꾸지 않는다."""
        now = utcnow()
        paused = await store.pause_own(sub, source_id, PAUSED_BY_HAND, now)
        if paused is not None:
            await self.end_open_for_source(paused.id, PAUSED_BY_HAND, now)
        return paused

    async def resume_schedule(
        self, agent: Agent, schedule: AgentSchedule, user: Principal
    ) -> AgentSchedule:
        """다시 켜는 사람에게 묶고 만료와 다음 울림을 지금부터 센다."""
        revision = self.ready(agent, schedule.message)
        acts_as = await self.bind(user, agent.tenant)
        now = utcnow()
        _, _, next_run_at = await _rule(schedule.cron, schedule.time_zone, now)
        return await self._save(
            schedule,
            {
                "paused": False,
                "paused_reason": None,
                "paused_cause": None,
                "acts_as": acts_as.model_dump(),
                "revision": revision,
                "expires_at": expiry(schedule.expires_in_days, now),
                "next_run_at": next_run_at,
                "updated_at": now,
            },
        )

    async def delete_schedule(self, schedule: AgentSchedule) -> None:
        await self.end_open_for_source(schedule.id, SOURCE_DELETED, utcnow())
        await self.schedules.delete(schedule.id)

    # -- 실행 (1절) --------------------------------------------------------------------

    async def run_now(self, agent: Agent, user: Principal, message: str) -> AgentRun:
        """지금 실행. 시작 워커가 집을 `starting` 기록을 넣는다. 입력 본문은 시작할 때까지만
        둔다."""
        self.ready(agent, message)
        acts_as = await self.bind(user, agent.tenant)
        now = utcnow()
        error = await self.cap_error(agent.tenant, agent.id, now)
        if error is not None:
            raise error
        size, digest = input_digest(message)
        run = new_run(
            tenant=agent.tenant,
            agent_id=agent.id,
            source="manual",
            acts_as=acts_as,
            now=now,
            message=message,
            input_bytes=size,
            input_sha256=digest,
        )
        await self.runs.insert(run)
        return run

    async def cap_error(
        self, tenant: str, agent_id: str, now: datetime, caps: RunCaps = RUN_CAPS
    ) -> RunError | None:
        """Agent 하나의 상한에 걸렸으면 그 거절 (10절). 기록을 만들 때 본다. caps 의 출처만 센다.
        스케줄·웹훅·지금 실행과 다른 Agent 가 부른 실행은 서로의 자리를 차지하지 않고, 뒤 판이 더한
        출처의 실행도 세지 않는다 (P46 6.3절, A0)."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return caps.error(
            await self._count(tenant, agent_id, MOVING_STATUSES, caps),
            await self._count(tenant, agent_id, ("waiting_approval",), caps),
            await self.runs.count_created_since(tenant, agent_id, day_start, sources=caps.sources),
        )

    async def start_cap_error(
        self, tenant: str, agent_id: str, caps: RunCaps = RUN_CAPS
    ) -> RunError | None:
        """시작할 때의 상한. `starting` 기록은 만들 때 상한을 지났으므로 도는 것과 기다리는 것만
        센다. 하루 상한도 만들 때 셌다."""
        return caps.error(
            await self._count(tenant, agent_id, ("running",), caps),
            await self._count(tenant, agent_id, ("waiting_approval",), caps),
            0,
        )

    async def _count(
        self, tenant: str, agent_id: str, statuses: tuple[RunStatus, ...], caps: RunCaps
    ) -> int:
        """상한이 세는 실행 수. caps 의 출처의 것이다."""
        return await self.runs.count_in_statuses(tenant, agent_id, statuses, sources=caps.sources)

    async def stop(self, run: AgentRun, reason: str = STOPPED) -> AgentRun:
        """중지 (7절). 먼저 표시를 남기고, 아직 시작하지 않았거나 승인을 기다리는 실행은 바로
        끝낸다. 도는 실행은 런타임에 그 턴을 멈추라고 하고, 턴의 `turn_end` 가 기록을 끝낸다. 시작
        중인 턴은 시작 워커가 표시를 보고 멈춘다. reason 은 사람의 중지가 아닌 까닭(부른 Agent 의
        취소)이다."""
        now = utcnow()
        await self.runs.mark_stop(run, now, reason)
        await self.runs.move(
            run.id, from_status=STOP_NOW_STATUSES, to_status="cancelled", now=now, reason=reason
        )
        updated = await self.runs.get(run.tenant, run.agent_id, run.id)
        assert updated is not None
        if updated.status == "running":
            await self.stop_turns([updated])
        return updated


__all__ = [
    "AGENT_CALL_BUSY",
    "AGENT_CHANGED",
    "AGENT_DELETED",
    "AGENT_RUN_LIMIT",
    "AGENT_UNDEPLOYED",
    "CALL_CAPS",
    "EXPIRY_DEFAULT_DAYS",
    "EXPIRY_MAX_DAYS",
    "MAX_RUNNING",
    "MAX_RUNS_PER_DAY",
    "MAX_SOURCES",
    "MAX_WAITING",
    "PAUSED_BY_HAND",
    "PREREQUISITES_FAILED",
    "RUNS_PAUSED_BY_DEPLOY",
    "RUNTIME_INPUT_CAP",
    "RUN_CAPS",
    "SOURCE_DELETED",
    "RunCaps",
    "RunError",
    "RunService",
    "ScheduleInput",
    "acts_as_of",
    "caps_of",
    "expiry",
    "input_limit",
]
