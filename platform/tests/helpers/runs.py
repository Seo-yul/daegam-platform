"""백그라운드 실행(P41) 시험의 메모리 가짜와 만드는 함수. 스토어와 Agent·사람을 가짜로 바꿔 워커와
서비스를 DB 없이 돌린다. 가짜 스토어는 Mongo 스토어의 조건(상태, 리스 주인, 세션, 이벤트 시각)을
그대로 따른다."""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from daegam.config import Settings
from daegam.services.runs.person import BoundPersonCheck
from daegam.services.runs.service import RunService
from daegam.services.runs.trigger_secrets import TriggerKeys
from daegam.store.agent_runs import (
    OPEN_STATUSES,
    RUN_RETENTION,
    STOP_NOW_STATUSES,
    STOPPED,
    ActsAs,
    AgentRun,
    RunStatus,
    end_fields,
    new_run,
    skip_row_id,
    status_fields,
)
from daegam.store.agent_schedules import AgentSchedule
from daegam.store.agent_triggers import AgentTrigger
from daegam.store.agents import AgentRevision, ToolPin
from daegam.store.base import ItemLease, new_id, utcnow
from daegam.store.hook_secrets import HookSecret
from daegam.store.idp_people import IdpPerson
from daegam.store.memberships import Membership
from daegam.store.users import User
from tests.helpers.agents import template_spec

# naive UTC. 저장 형식과 같다 (store/base.py utcnow)
NOW = datetime(2026, 10, 29, 9, 0)
ALICE = ActsAs(sub="alice")
# local 모드에서 묶인 사람. 묶을 때의 tv 가 있다
BOUND = ActsAs(sub="alice", username="alice", tv=3)
# issuer 없는 oidc 는 header 로 돈다. 사람을 확인하지 않는다
SETTINGS = Settings(mongo_url="mongodb://unused", mongo_db="unused")
LOCAL = Settings(mongo_url="mongodb://unused", mongo_db="unused", auth_mode="local")
# 데이터 플레인 발급자는 기본값(platform)이다. 실행이 묶인 사람을 확인하는 길은 발급자와 상관없다
OIDC = Settings(
    mongo_url="mongodb://unused",
    mongo_db="unused",
    auth_mode="oidc",
    oidc_issuer="https://idp.example",
)


class FakeRuns:
    """`MongoAgentRunStore` 의 메모리 가짜."""

    def __init__(self) -> None:
        self.docs: dict[str, AgentRun] = {}
        self.fail_insert = False
        self.deferred: dict[str, datetime] = {}

    def add(self, run: AgentRun) -> AgentRun:
        self.docs[run.id] = run
        return run

    def slots(self) -> list[AgentRun]:
        return sorted(
            (r for r in self.docs.values() if r.scheduled_for is not None),
            key=lambda r: r.scheduled_for or NOW,
        )

    def skips(self) -> dict[str, int]:
        return {r.reason or "": r.count or 0 for r in self.docs.values() if r.status == "skipped"}

    @staticmethod
    def _set(run: AgentRun, fields: dict[str, Any]) -> None:
        for key, value in fields.items():
            setattr(run, key, value)

    # -- 기록 ---------------------------------------------------------------------------

    async def insert(self, run: AgentRun) -> bool:
        if self.fail_insert:
            raise RuntimeError("store down")
        if run.scheduled_for is not None and await self.find_slot(
            run.source_id or "", run.scheduled_for
        ):
            return False
        if run.delivery_id is not None and await self.find_delivery(
            run.source_id or "", run.delivery_id
        ):
            return False
        self.docs[run.id] = run
        return True

    async def get(self, tenant: str, agent_id: str, run_id: str) -> AgentRun | None:
        r = self.docs.get(run_id)
        return r if r is not None and (r.tenant, r.agent_id) == (tenant, agent_id) else None

    async def for_session(self, tenant: str, agent_id: str, session_id: str) -> AgentRun | None:
        return (await self.for_sessions(tenant, agent_id, [session_id])).get(session_id)

    async def for_sessions(
        self, tenant: str, agent_id: str, session_ids: list[str]
    ) -> dict[str, AgentRun]:
        return {
            r.session_id: r
            for r in self.docs.values()
            if (r.tenant, r.agent_id) == (tenant, agent_id) and r.session_id in session_ids
        }

    async def shift_deadlines(self, tenant: str, agent_id: str, shift: timedelta) -> int:
        moved = 0
        for r in self.docs.values():
            mine = (r.tenant, r.agent_id) == (tenant, agent_id)
            if mine and r.status == "waiting_approval" and r.deadline:
                r.deadline += shift
                moved += 1
        return moved

    async def begin_decision(self, run_id: str, now: datetime) -> bool:
        r = self.docs[run_id]
        due = r.deadline is not None and r.deadline > now
        if r.status != "waiting_approval" or not due or r.stop_requested_at is not None:
            return False
        self._set(r, status_fields("running", now))
        return True

    async def find_delivery(self, source_id: str, delivery_id: str) -> AgentRun | None:
        return next(
            (
                r
                for r in self.docs.values()
                if r.source_id == source_id and r.delivery_id == delivery_id
            ),
            None,
        )

    async def find_slot(self, source_id: str, at: datetime) -> AgentRun | None:
        return next(
            (r for r in self.docs.values() if r.source_id == source_id and r.scheduled_for == at),
            None,
        )

    async def has_open_for_source(self, source_id: str) -> bool:
        return any(
            r.source_id == source_id and r.status in OPEN_STATUSES for r in self.docs.values()
        )

    async def bump_skip(self, *, source_id: str, reason: str, now: datetime, **_: Any) -> None:
        key = skip_row_id(source_id, reason)  # type: ignore[arg-type]
        row = self.docs.get(key)
        if row is None:
            row = run_of(
                id=key,
                source="schedule",
                source_id=source_id,
                status="skipped",
                reason=reason,
                count=0,
                created_at=now,
                last_event_at=now,
            )
            self.docs[key] = row
        row.count = (row.count or 0) + 1

    async def count_in_statuses(
        self, tenant: str, agent_id: str, statuses: tuple, *, sources: Collection[str] | None = None
    ) -> int:
        return sum(
            1
            for r in self.docs.values()
            if r.agent_id == agent_id
            and r.status in statuses
            and (sources is None or r.source in sources)
        )

    async def count_created_since(
        self, tenant: str, agent_id: str, since: datetime, *, sources: Collection[str] | None = None
    ) -> int:
        return sum(
            1
            for r in self.docs.values()
            if r.status != "skipped"
            and r.created_at >= since
            and (sources is None or r.source in sources)
        )

    # -- 이동과 중지 --------------------------------------------------------------------

    async def mark_stop(self, run: AgentRun, now: datetime, reason: str = STOPPED) -> bool:
        r = self.docs.get(run.id)
        if r is None or r.status not in OPEN_STATUSES:
            return False
        r.stop_requested_at = r.stop_requested_at or now
        r.stop_reason = r.stop_reason or reason
        return True

    async def move(
        self,
        run_id: str,
        *,
        from_status: tuple[RunStatus, ...],
        to_status: RunStatus,
        now: datetime,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
        owner: str | None = None,
        unmarked: bool = False,
    ) -> bool:
        r = self.docs.get(run_id)
        if r is None or r.status not in from_status:
            return False
        if owner is not None and (r.lease is None or r.lease.owner != owner):
            return False
        if unmarked and r.stop_requested_at is not None:
            return False
        self._set(r, {**status_fields(to_status, now, reason), **(extra or {})})
        return True

    async def cancel_open_for_source(
        self, source_id: str, reason: str, now: datetime
    ) -> list[AgentRun]:
        return self._cancel_open(lambda r: r.source_id == source_id, reason, now)

    async def cancel_open_for_agent(
        self, tenant: str, agent_id: str, reason: str, now: datetime
    ) -> list[AgentRun]:
        return self._cancel_open(
            lambda r: (r.tenant, r.agent_id) == (tenant, agent_id), reason, now
        )

    def _cancel_open(self, match: Any, reason: str, now: datetime) -> list[AgentRun]:
        for r in self.docs.values():
            if not match(r) or r.status not in OPEN_STATUSES:
                continue
            r.stop_requested_at = r.stop_requested_at or now
            r.stop_reason = r.stop_reason or reason
            if r.status in STOP_NOW_STATUSES:
                self._set(r, end_fields("cancelled", now, reason))
        return [r for r in self.docs.values() if match(r) and r.status == "running"]

    # -- 시작 워커 ----------------------------------------------------------------------

    async def acquire_start(
        self, owner: str, lease_seconds: float, now: datetime, *, busy: Any = ()
    ) -> AgentRun | None:
        for r in sorted(self.docs.values(), key=lambda x: x.next_attempt_at or now):
            free = r.lease is None or r.lease.until < now
            due = r.next_attempt_at is not None and r.next_attempt_at <= now
            if r.status == "starting" and due and free and r.agent_id not in busy:
                r.lease = ItemLease(owner=owner, until=now + timedelta(seconds=lease_seconds))
                return r.model_copy(deep=True)
        return None

    async def renew(self, *_: Any) -> bool:
        return True

    async def defer(self, run_id: str, owner: str, until: datetime) -> None:
        self.deferred[run_id] = until
        r = self.docs[run_id]
        if r.lease is not None and r.lease.owner == owner:
            r.lease = ItemLease(owner=owner, until=until)

    async def retry_start(self, run_id: str, owner: str, *, attempts, at, now) -> bool:
        r = self.docs[run_id]
        if r.status != "starting" or r.lease is None or r.lease.owner != owner:
            return False
        self._set(r, {"attempts": attempts, "next_attempt_at": at, "lease": None})
        r.last_event_at = now
        return True

    async def note_start(
        self, run_id: str, owner: str, *, session_id, turn_id, grant_id, acts_as, now
    ) -> bool:
        r = self.docs[run_id]
        mine = r.lease is not None and r.lease.owner == owner
        released = r.lease is None and r.session_id == session_id and r.started_at is None
        if not (mine or released):
            return False
        self._set(
            r,
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "grant_id": grant_id,
                "acts_as": acts_as,
                "started_at": now,
                "message": None,
            },
        )
        return True

    # -- 이벤트와 지켜보기 --------------------------------------------------------------

    async def apply_event(
        self,
        run_id: str,
        session_id: str,
        *,
        ts: datetime,
        to_status: RunStatus,
        now: datetime,
        reason: str | None = None,
        fields: dict[str, Any] | None = None,
        unmarked: bool = False,
    ) -> bool:
        r = self.docs[run_id]
        if r.status not in OPEN_STATUSES or r.session_id not in (None, session_id):
            return False
        if (r.event_at is not None and r.event_at > ts) or (unmarked and r.stop_requested_at):
            return False
        self._set(r, {**(fields or {}), **status_fields(to_status, now, reason)})
        r.event_at, r.lease = ts, None
        return True

    async def refuse_due(self, run_id: str, now: datetime, *, reason: str) -> bool:
        r = self.docs[run_id]
        if r.status != "waiting_approval" or r.deadline is None or r.deadline > now:
            return False
        self._set(r, {**end_fields("refused", now, reason), "lease": None})
        return True

    async def settle_quiet(self, run, *, to_status, now, reason=None, deadline=None) -> bool:
        r = self.docs[run.id]
        if r.status != "running" or r.last_event_at != run.last_event_at:
            return False
        self._set(r, {**status_fields(to_status, now, reason), "lease": None})
        if deadline is not None:
            r.deadline = deadline
        return True


class FakeSources:
    """`MongoRunSourceStore` 의 메모리 가짜. 하위 클래스의 model 은 바꾸기가 칸을 검사할 모델이고
    on_change 는 사람의 변경이 함께 적는 칸이다."""

    model: Any
    on_change: Mapping[str, Any] = {}

    def __init__(self, *sources: Any) -> None:
        self.docs = {s.id: s for s in sources}

    async def insert(self, source: Any) -> None:
        self.docs[source.id] = source

    async def get(self, tenant: str, agent_id: str, source_id: str) -> Any:
        return self.docs.get(source_id)

    async def list_for_agent(self, tenant: str, agent_id: str) -> list[Any]:
        return [s for s in self.docs.values() if (s.tenant, s.agent_id) == (tenant, agent_id)]

    async def list_bound(self, sub: str) -> list[Any]:
        return [s for s in self.docs.values() if s.acts_as.sub == sub]

    async def count_for_agent(self, tenant: str, agent_id: str) -> int:
        return sum(1 for s in self.docs.values() if s.agent_id == agent_id)

    async def update(
        self, source_id: str, fields: Mapping[str, Any], *, match: Mapping[str, Any] | None = None
    ) -> Any:
        """사람의 변경. 저장된 것 그대로를 바꿔 시험이 쥔 소스에도 보인다."""
        stored = self.docs.get(source_id)
        if stored is None or any(getattr(stored, k) != v for k, v in (match or {}).items()):
            return None
        changed = {**fields, **self.on_change}
        checked = self.model(**{**stored.model_dump(), **changed})
        for key in changed:
            setattr(stored, key, getattr(checked, key))
        return stored

    async def delete(self, source_id: str) -> bool:
        return self.docs.pop(source_id, None) is not None

    async def delete_for_agent(self, tenant: str, agent_id: str) -> int:
        gone = [k for k, s in self.docs.items() if (s.tenant, s.agent_id) == (tenant, agent_id)]
        for k in gone:
            del self.docs[k]
        return len(gone)

    async def pause_bound(
        self,
        sub: str,
        *,
        reason: str,
        cause: str,
        now: datetime,
        tenant: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, str]:
        paused = {}
        for s in self.docs.values():
            if s.acts_as.sub != sub or s.paused:
                continue
            if tenant is not None and s.tenant != tenant:
                continue
            if agent_id is not None and s.agent_id != agent_id:
                continue
            s.paused, s.paused_reason, s.paused_cause, s.updated_at = True, reason, cause, now
            paused[s.id] = s.tenant
        return paused

    async def pause_for_agent(self, tenant: str, agent_id: str, reason: str, now: datetime) -> int:
        paused = 0
        for s in await self.list_for_agent(tenant, agent_id):
            if not s.paused:
                s.paused, s.paused_reason, s.paused_cause, s.updated_at = True, reason, None, now
                paused += 1
        return paused

    async def pause_own(self, sub: str, source_id: str, reason: str, now: datetime) -> Any:
        s = self.docs.get(source_id)
        if s is None or s.acts_as.sub != sub:
            return None
        s.paused, s.paused_reason, s.paused_cause, s.updated_at = True, reason, None, now
        return s


class FakeTriggers(FakeSources):
    """`MongoAgentTriggerStore` 의 메모리 가짜. lookups 는 id 로 찾은 수다."""

    model = AgentTrigger

    def __init__(self, *sources: Any) -> None:
        super().__init__(*sources)
        self.lookups = 0

    async def get_by_id(self, trigger_id: str) -> AgentTrigger | None:
        self.lookups += 1
        return self.docs.get(trigger_id)


class FakeTriggerSecrets:
    """`TriggerSecrets` 의 메모리 가짜. (Agent id, 트리거 id) → (지금, 앞) 비밀이다. reads 는 읽은
    수이고 broken 이면 읽기와 쓰기가 실패한다."""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], TriggerKeys] = {}
        self.reads = 0
        self.broken = False
        self.dropped_agents: list[str] = []

    def _check(self) -> None:
        if self.broken:
            raise RuntimeError("secret store down")

    async def put(self, agent_id: str, trigger_id: str, current: str, previous: str | None) -> None:
        self._check()
        self.keys[(agent_id, trigger_id)] = TriggerKeys(current, previous)

    async def read(self, agent_id: str, trigger_id: str) -> TriggerKeys:
        self._check()
        self.reads += 1
        return self.keys.get((agent_id, trigger_id), TriggerKeys(None, None))

    async def drop(self, agent_id: str, trigger_id: str) -> None:
        self._check()
        self.keys.pop((agent_id, trigger_id), None)

    async def restore(
        self, agent_id: str, trigger_id: str, written: TriggerKeys, back: TriggerKeys
    ) -> bool:
        self._check()
        if self.keys.get((agent_id, trigger_id)) != written:
            return False
        self.keys[(agent_id, trigger_id)] = back
        return True

    async def drop_agent(self, agent_id: str) -> None:
        self._check()
        self.dropped_agents.append(agent_id)
        for key in [k for k in self.keys if k[0] == agent_id]:
            del self.keys[key]


class FakeHookSecrets:
    """`MongoHookSecretStore` 의 메모리 가짜. writes 는 쓰기를 적은 Agent 의 차례다."""

    def __init__(self, *rows: HookSecret) -> None:
        self.rows = {r.id: r for r in rows}
        self.writes: list[str] = []

    async def note_write(self, agent_id: str, now: datetime) -> None:
        self.writes.append(agent_id)
        row = self.rows.get(agent_id)
        if row is None:
            self.rows[agent_id] = HookSecret(id=agent_id, written_at=now)
        else:
            row.written_at = now

    async def get(self, agent_id: str) -> HookSecret | None:
        row = self.rows.get(agent_id)
        return row.model_copy() if row is not None else None

    async def due(
        self, swept_before: datetime, written_before: datetime, limit: int
    ) -> list[HookSecret]:
        rows = [
            r
            for r in self.rows.values()
            if (r.swept_at is None or r.swept_at < swept_before) and r.written_at <= written_before
        ]
        rows.sort(key=lambda r: (r.swept_at is not None, r.swept_at or datetime.min))
        return [r.model_copy() for r in rows[:limit]]

    async def mark_swept(self, agent_id: str, now: datetime) -> None:
        if agent_id in self.rows:
            self.rows[agent_id].swept_at = now

    async def forget(self, agent_id: str, written_at: datetime) -> bool:
        row = self.rows.get(agent_id)
        if row is None or row.written_at != written_at:
            return False
        del self.rows[agent_id]
        return True


class FakeSchedules(FakeSources):
    """`MongoAgentScheduleStore` 의 메모리 가짜. 사람이 바꾸면(`updated_at`) 워커의 옮기기가
    진다."""

    model = AgentSchedule
    on_change = {"fire_failures": 0}
    # 한 틱이 같은 스케줄을 되풀이해 잡으면 멈추지 않고 실패하게 하는 위 끝
    MAX_ACQUIRED = 50

    def __init__(self, *schedules: AgentSchedule) -> None:
        super().__init__(*schedules)
        self.acquired = 0

    def _held(self, schedule: AgentSchedule, owner: str) -> AgentSchedule | None:
        s = self.docs.get(schedule.id)
        ok = s is not None and s.lease is not None and s.lease.owner == owner
        return s if ok and s.updated_at == schedule.updated_at else None

    async def acquire_due(
        self, owner: str, lease_seconds: float, now: datetime
    ) -> AgentSchedule | None:
        for s in sorted(self.docs.values(), key=lambda x: x.next_run_at):
            free = s.lease is None or s.lease.until < now
            if not s.paused and s.next_run_at <= now and free:
                s.lease = ItemLease(owner=owner, until=now + timedelta(seconds=lease_seconds))
                self.acquired += 1
                if self.acquired > self.MAX_ACQUIRED:
                    raise AssertionError("one tick keeps acquiring the same schedule")
                return s.model_copy(deep=True)
        return None

    async def renew(self, *_: Any) -> bool:
        return True

    async def advance(
        self, schedule, owner, *, next_run_at, slot=None, fired_at=None, run_id=None
    ) -> bool:
        s = self._held(schedule, owner)
        if s is None:
            return False
        s.next_run_at, s.lease, s.fire_failures = next_run_at, None, 0
        if slot is not None:
            s.last_slot_at, s.last_fired_at, s.last_run_id = slot, fired_at, run_id
        return True

    async def pause_held(self, schedule, owner, reason) -> bool:
        s = self._held(schedule, owner)
        if s is None:
            return False
        s.paused, s.paused_reason, s.lease = True, reason, None
        return True

    async def defer(self, schedule_id: str, owner: str, until: datetime) -> None:
        s = self.docs[schedule_id]
        if s.lease is not None and s.lease.owner == owner:
            s.lease = ItemLease(owner=owner, until=until)

    async def back_off(self, schedule_id: str, owner: str, until: datetime) -> None:
        s = self.docs[schedule_id]
        if s.lease is not None and s.lease.owner == owner:
            s.lease = ItemLease(owner=owner, until=until)
            s.fire_failures += 1

    async def release(self, schedule_id: str, owner: str) -> None:
        s = self.docs[schedule_id]
        if s.lease is not None and s.lease.owner == owner:
            s.lease = None


@dataclass
class FakeAgent:
    state: str = "deployed"
    name: str = "support"
    id: str = "ag-1"
    tenant: str = "default"
    spec: dict[str, Any] = field(default_factory=template_spec)
    # 배포 때의 Tool 핀 (P39b 14절 5부). 트리거 규칙이 읽는다
    tool_pins: dict[str, ToolPin] = field(default_factory=dict)
    tool_pins_revision: int | None = 3

    @property
    def is_deployed(self) -> bool:
        return self.state != "draft"

    @property
    def deployed(self) -> AgentRevision | None:
        return None if self.state == "draft" else AgentRevision(revision=3, **self.spec)


class FakeAgents:
    """agent 를 주면 모든 기록의 Agent 이고, by_id 가 있으면 그 id 의 Agent 다. gone 이면 없다."""

    def __init__(
        self,
        agent: FakeAgent | None = None,
        by_id: dict[str, FakeAgent] | None = None,
        *,
        gone: bool = False,
    ) -> None:
        self.agent = agent if agent is not None else FakeAgent()
        self.by_id = by_id or {}
        self.gone = gone

    async def get(self, agent_id: str, *, tenant: str) -> FakeAgent | None:
        return None if self.gone else self.by_id.get(agent_id, self.agent)

    async def get_many(self, ids: Any) -> dict[str, FakeAgent]:
        """있는 Agent 만이다. by_id 에 없으면 agent 의 id 일 때만 있다."""
        out = {i: self.by_id[i] for i in ids if i in self.by_id}
        if not self.gone and self.agent.id in ids:
            out.setdefault(self.agent.id, self.agent)
        return out


class FakeUsers:
    def __init__(self, *users: User) -> None:
        self.docs = {u.id: u for u in users}

    async def get(self, user_id: str) -> User | None:
        return self.docs.get(user_id)


class FakeMemberships:
    def __init__(self, *rows: tuple[str, str, str]) -> None:
        self.rows = [Membership(sub=s, tenant=t, role=r) for s, t, r in rows]

    async def list_for_sub(self, sub: str) -> list[Membership]:
        return [m for m in self.rows if m.sub == sub]


def idp_person(sub: str, *, age: timedelta = timedelta(0), **fields: Any) -> IdpPerson:
    """oidc 모드의 사람 기록. age 는 마지막 IdP 확인이 지금에서 떨어진 시간이다."""
    confirmed = utcnow() - age
    base = {
        "id": sub,
        "username": sub,
        "groups": [],
        "confirmed_at": confirmed,
        "token_life": 300,
        "updated_at": confirmed,
    }
    return IdpPerson(**{**base, **fields})


class FakePeople:
    """`MongoIdpPeopleStore` 의 읽기 가짜."""

    def __init__(self, *people: IdpPerson) -> None:
        self.docs = {p.id: p for p in people}

    async def get(self, sub: str) -> IdpPerson | None:
        return self.docs.get(sub)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.counted: dict[str, Any] = {}

    async def record(self, event: Any) -> None:
        self.events.append(event)

    async def count(self, event: Any) -> None:
        row = self.counted.setdefault(event.id, event.model_copy(update={"count": 0}))
        row.count += 1


class FakeOwners:
    """`MongoAgentSessionOwnerStore` 가운데 턴 위임의 `send` 가 쓰는 것."""

    def __init__(self) -> None:
        self.confirmed: list[Any] = []

    async def confirm(self, owner: Any) -> bool:
        self.confirmed.append(owner)
        return True


class FakeTurns:
    """`RunTurns` 의 가짜. 멈추라고 한 실행을 적는다."""

    def __init__(self) -> None:
        self.stopped: list[AgentRun] = []

    async def stop(self, run: AgentRun) -> None:
        self.stopped.append(run)


def alice(**fields: Any) -> User:
    base = {"id": "alice", "username": "alice", "password_hash": "x", "token_version": 3}
    return User(**{**base, **fields})


def person_check(
    settings: Settings = SETTINGS,
    users: FakeUsers | None = None,
    *rows: tuple[str, str, str],
    people: FakePeople | None = None,
) -> BoundPersonCheck:
    return BoundPersonCheck(
        users,  # type: ignore[arg-type]
        FakeMemberships(*rows),  # type: ignore[arg-type]
        settings,
        people or FakePeople(),  # type: ignore[arg-type]
    )


def service_of(
    runs: Any = None,
    schedules: Any = None,
    settings: Settings = SETTINGS,
    *,
    person: BoundPersonCheck | None = None,
    audit: Any = None,
    turns: Any = None,
    triggers: Any = None,
    secrets: Any = None,
) -> RunService:
    """가짜 스토어 위의 서비스. person 이 없으면 settings 의 모드로 아무 소속도 없는 확인이다."""
    service = RunService(
        runs if runs is not None else FakeRuns(),
        schedules if schedules is not None else FakeSchedules(),
        settings,
        person=person or person_check(settings),
        triggers=triggers if triggers is not None else FakeTriggers(),
        trigger_secrets=secrets if secrets is not None else FakeTriggerSecrets(),
        audit=audit,
    )
    service.turns = turns
    return service


def schedule_of(**fields: Any) -> AgentSchedule:
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant="default",
        agent_id="ag-1",
        acts_as=ALICE,
        revision=3,
        cron="*/15 * * * *",
        time_zone="UTC",
        message="report",
        expires_at=NOW + timedelta(days=90),
        next_run_at=NOW,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    return AgentSchedule(**{**base, **fields})


def run_of(**fields: Any) -> AgentRun:
    """NOW 에 만든 지금 실행 기록. `expires_at` 만은 실제 시계를 따른다. API 시험이 이것을 Mongo 에
    넣으면 TTL 인덱스가 그 시각에 지우기 때문이다."""
    base = new_run(tenant="default", agent_id="ag-1", source="manual", acts_as=ALICE, now=NOW)
    return base.model_copy(update={"expires_at": utcnow() + RUN_RETENTION, **fields})


def waiting_run_of(**fields: Any) -> AgentRun:
    """세션 s1 의 턴 t1 에서 5분 뒤 마감까지 승인을 기다리는 실행. 지금 시각 기준이라 API 시험도
    쓴다."""
    base = {
        "status": "waiting_approval",
        "session_id": "s1",
        "turn_id": "t1",
        "deadline": utcnow() + timedelta(minutes=5),
    }
    return run_of(**{**base, **fields})


def with_budget(spec: dict[str, Any], tokens: int | None) -> dict[str, Any]:
    spec = copy.deepcopy(spec)
    spec["service"].setdefault("budget", {})["session_tokens"] = tokens
    return spec


def with_ttl(spec: dict[str, Any], seconds: int) -> dict[str, Any]:
    """entry 노드의 세션 TTL 을 바꾼 spec."""
    spec = copy.deepcopy(spec)
    for node in spec["graph"]["nodes"]:
        if node["type"] == "entry":
            node.setdefault("data", {})["session_ttl_seconds"] = seconds
    return spec


def with_approval(
    spec: dict[str, Any], node_id: str, approval: dict[str, list[str]]
) -> dict[str, Any]:
    """tools 노드 하나에 승인 규칙을 단 spec."""
    spec = copy.deepcopy(spec)
    for node in spec["graph"]["nodes"]:
        if node["id"] == node_id:
            node["data"]["approval"] = approval
    return spec


# 템플릿의 tools 노드는 Add 와 Divide 를 고르고 Divide 에만 승인 규칙이 있다. 둘 다 쓰는 핀이다
WRITES = {
    "tools-calculator": ToolPin(
        names=["Add", "Divide"], effects={"Add": "write", "Divide": "write"}
    )
}
APPROVE = ["approve", "reject"]


def guarded(**fields: Any) -> FakeAgent:
    """쓰는 Tool 마다 승인 규칙이 있어 트리거 규칙을 지나는 Agent."""
    approval = {"Add": APPROVE, "Divide": APPROVE}
    spec = with_approval(template_spec(), "tools-calculator", approval)
    return FakeAgent(spec=spec, tool_pins=WRITES, **fields)


def trigger_of(**fields: Any) -> AgentTrigger:
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant="default",
        agent_id="ag-1",
        acts_as=ALICE,
        revision=3,
        name="deploys",
        template="{{payload}}",
        secret_fp="fp",
        expires_at=utcnow() + timedelta(days=90),
        created_at=NOW,
        updated_at=NOW,
    )
    return AgentTrigger(**{**base, **fields})
