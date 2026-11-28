"""Agent 의 실행 기록 (P41 1절).

agent_runs {_id, tenant, agent_id, source, source_id?, scheduled_for?, delivery_id?, acts_as,
            message?, input_bytes, input_sha256, session_id?, turn_id?, grant_id?, status,
            reason?, attempts, next_attempt_at?, lease?, last_event_at, event_at?,
            stop_requested_at?, stop_reason?, created_at, started_at?, ended_at?, deadline?,
            expires_at, count?, first_at?, last_at?,
            caller?: {agent_id, session_id?, turn_id?, grant_id?, chain, long_bound, untrusted,
                      timeout_at?, thread_id?}}

실행 하나는 플랫폼이 묶인 사람으로 새 세션에서 여는 턴 하나와 그 뒤의 기록이다. `scheduled_for` 는
스케줄의 울림 시각이고 `delivery_id` 는 웹훅 호출의 id 다. 둘 다 있을 때만 문서에 적어 부분 유일
인덱스가 울림 하나와 호출 하나를 기록 하나로 묶는다. 실행이 되지 않은 울림은 `skipped` 행 하나에
까닭마다 센다(소스 하나·까닭 하나에 행 하나, 처음과 마지막 시각). 입력은 크기와 sha256 만 남긴다.
지금 실행의 본문(`message`)만은 시작 워커가 집을 때까지 둔다. 끝나는 이동이 지우고, 시작 워커가
`running` 으로 옮길 때 지운다(P41 12절). TTL 인덱스가 전사 보존 기간(30일) 뒤에 지운다.

`source` 는 글자 그대로 읽는다. 뒤 판이 더한 출처의 기록도 목록·읽기·이벤트·중지에서 터지지
않는다. 시작과 지켜보기 워커는 이 판이 아는 출처(`RUN_SOURCES`)의 기록만 잡고 나머지는 그 판의
워커에 둔다. 다만 지켜보기 워커는 중지 표시가 남은 조용한 `running` 기록을 출처와 상관없이 잡아 그
까닭으로 끝낸다. 그 판의 워커가 없는 동안 끝을 알리지 않은 턴을 아무도 끝내지 못하기 때문이다
(P46 A0).

출처 `agent` 는 다른 Agent 가 Tool 로 부른 실행이다 (P46 6.3절). `caller` 가 부른 쪽을 적는다. 부른
Agent, 그 세션과 턴의 위임(위임 없는 턴은 파드가 댄 턴 id), 뿌리부터 부른 Agent 까지의 사슬
(`chain`), 뿌리 턴의 한도 종류(`long_bound`), 입력을 믿지 않는 실행인지(`untrusted`), 호출이 답을
기다리는 끝(`timeout_at`)이다. 호출은 스케줄·웹훅·지금 실행의 상한(`RUN_CAP_SOURCES`)과 따로 센다.
부른 턴이 끝나면 아직 첫 턴인 실행을 취소한다. 첫 턴은 `starting` 이나 `running` 이고 마감
(`deadline`)이 한 번도 적히지 않은 기록이다. 승인을 기다린 적이 있는 실행은 마감이 남는다.

맡긴 작업(`caller.thread_id`, P46 6.7절)의 실행은 아무도 기다리지 않는다. 작업 하나의 실행들은 세션
하나를 나눠 쓰므로 세션 하나에 기록이 여럿이고, 세션으로 찾으면 가장 늦게 만든 기록이다. 부른 턴이
끝나도 취소하지 않으므로 `caller.grant_id` 와 `timeout_at` 을 적지 않는다. 그래서 위임의 끝을 쓸어
담는 조건이 이 기록을 고르지 않고, 호출만 아는 판의 쓸기도 고르지 않는다.

`starting` 과 `waiting_approval` 에서 나가는 이동은 모두 조건부 갱신이다. 같은 초에 만난 결정·마감·
중지가 결과 하나만 남긴다. 시작 워커는 `starting` 기록 하나에 리스(`lease`)를 걸고 시작하며, 다시 할
시작은 `next_attempt_at` 에 미룬다. 지켜보기 워커도 `running` 과 `waiting_approval` 기록에 같은
리스를 건다. `event_at` 은 마지막으로 받은 런타임 이벤트 자신의 시각이고 그보다 앞선 이벤트는
버린다. `last_event_at` 은 받은 이벤트와 플랫폼의 이동마다 플랫폼 시계로 찍는다. 중지 표시는
처음 남긴 시각과 까닭(`stop_reason`)이 남는다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    ItemLease,
    UtcDatetime,
    ensure_ttl_index,
    extend_lease,
    free_lease,
    insert_new,
    new_id,
    take_lease,
)

AGENT_RUNS = "agent_runs"
# 전사 보존 기간과 같다 (agent-runtime 의 DAEGAM_TRANSCRIPT_RETENTION_DAYS 기본값)
RUN_RETENTION = timedelta(days=30)

RunSource = Literal["schedule", "webhook", "manual", "agent"]
# 이 판이 시작하고 지켜보는 출처 (P46 A0)
RUN_SOURCES: tuple[RunSource, ...] = get_args(RunSource)
# 다른 Agent 가 Tool 로 부른 실행의 출처 (P46 6.3절)
CALL_SOURCE: RunSource = "agent"
# Agent 하나의 실행 상한이 세는 출처. 호출은 제 상한으로 따로 센다
RUN_CAP_SOURCES: tuple[RunSource, ...] = tuple(s for s in RUN_SOURCES if s != CALL_SOURCE)
RunStatus = Literal[
    "starting",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
    "refused",
    "skipped",
]
# 실행이 아직 움직이는 상태. "앞 실행이 아직 돈다" 판단과 소스를 멈출 때의 중지 표시가 센다
OPEN_STATUSES: tuple[RunStatus, ...] = ("starting", "running", "waiting_approval")
# 상한의 "동시에 도는 실행"은 대기를 빼고 센다 (10절)
MOVING_STATUSES: tuple[RunStatus, ...] = ("starting", "running")
# 중지가 바로 끝내는 상태. 도는 실행은 표시만 남고 런타임의 턴을 멈춰야 끝난다 (7절)
STOP_NOW_STATUSES: tuple[RunStatus, ...] = ("starting", "waiting_approval")
# 사람이 멈춘 실행의 까닭
STOPPED = "stopped"
# 실행이 되지 않은 울림의 까닭 (3절)
SkipReason = Literal["missed", "previous_running", "agent_run_limit"]
# 없을 때 문서에서 빼는 칸. 부분 유일 인덱스가 칸의 있음으로 거른다
_OMIT_WHEN_NONE = ("scheduled_for", "delivery_id", "source_id", "caller")
# 첫 턴인 열린 실행을 고르는 조건이다(모듈 머리)
_FIRST_TURN: dict[str, Any] = {"status": {"$in": MOVING_STATUSES}, "deadline": None}
# 가장 늦게 만든 기록부터 읽는 차례
_LATEST = [("created_at", DESCENDING)]


class ActsAs(BaseModel):
    """실행이 누구로 도는지. `tv` 는 local 모드에서 묶을 때의 `token_version` 이고 시작마다
    사용자 문서와 맞댄다(6절). `kind` 는 사람 또는 Agent 이고 `cause` 는 Agent 로 돈 까닭이다."""

    model_config = ConfigDict(extra="ignore")

    sub: str
    username: str | None = None
    tv: int | None = None
    kind: Literal["person", "agent"] = "person"
    cause: str | None = None


class RunCaller(BaseModel):
    """다른 Agent 가 Tool 로 부른 실행의 부른 쪽이다 (P46 6.3·6.5절). agent_id 는 부른 Agent(미리
    보기는 원본)이고 chain 은 뿌리부터 그 Agent 까지다. grant_id 는 부른 턴의 위임이고, 위임 없는
    턴(header 모드)은 없어 session_id 와 turn_id 가 그 턴을 가린다. thread_id 는 맡긴 작업의
    id 이고(6.7절) 그 실행은 grant_id 와 timeout_at 을 적지 않는다(모듈 머리)."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    session_id: str | None = None
    turn_id: str | None = None
    grant_id: str | None = None
    chain: list[str]
    long_bound: bool = False
    untrusted: bool = True
    timeout_at: UtcDatetime | None = None
    thread_id: str | None = None


class AgentRun(Document):
    id: str
    tenant: str
    agent_id: str
    # 읽을 때는 모르는 출처도 받는다 (P46 A0). 만드는 쪽은 `new_run` 의 `RunSource` 다
    source: str
    source_id: str | None = None
    scheduled_for: UtcDatetime | None = None
    delivery_id: str | None = None
    acts_as: ActsAs
    message: str | None = None
    input_bytes: int = 0
    input_sha256: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    grant_id: str | None = None
    status: RunStatus
    reason: str | None = None
    attempts: int = 0
    next_attempt_at: UtcDatetime | None = None
    lease: ItemLease | None = None
    last_event_at: UtcDatetime
    event_at: UtcDatetime | None = None
    stop_requested_at: UtcDatetime | None = None
    stop_reason: str | None = None
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    deadline: UtcDatetime | None = None
    expires_at: UtcDatetime
    # skipped 행만 쓴다
    count: int | None = None
    first_at: UtcDatetime | None = None
    last_at: UtcDatetime | None = None
    # 출처가 `agent` 인 실행만 쓴다
    caller: RunCaller | None = None

    @property
    def long_bound(self) -> bool | None:
        """이 실행의 턴에 여는 위임이 싣는 뿌리 턴의 한도 종류다 (P46 6.4절). 다른 Agent 가 부른
        실행만 싣고 그 밖은 None 이다."""
        return self.caller.long_bound if self.caller is not None else None

    def to_doc(self) -> dict[str, Any]:
        d = super().to_doc()
        for key in _OMIT_WHEN_NONE:
            if d.get(key) is None:
                d.pop(key, None)
        return d


def input_digest(message: str) -> tuple[int, str]:
    """입력의 바이트 수와 sha256. 기록은 입력 본문 대신 이것을 남긴다."""
    data = message.encode()
    return len(data), hashlib.sha256(data).hexdigest()


def new_run(
    *, tenant: str, agent_id: str, source: RunSource, acts_as: ActsAs, now: datetime, **fields: Any
) -> AgentRun:
    """시작 워커가 집을 `starting` 기록. 스케줄 울림·웹훅 호출·지금 실행이 같은 꼴로 만든다. 기록의
    `source` 는 모르는 출처도 읽으므로 만들 때 이 판의 출처인지 본다."""
    if source not in RUN_SOURCES:
        raise ValueError(f"unknown run source {source!r}")
    return AgentRun(
        id=new_id(),
        tenant=tenant,
        agent_id=agent_id,
        source=source,
        acts_as=acts_as,
        status="starting",
        next_attempt_at=now,
        last_event_at=now,
        created_at=now,
        expires_at=now + RUN_RETENTION,
        **fields,
    )


def skip_row_id(source_id: str, reason: SkipReason) -> str:
    """소스 하나·까닭 하나의 skipped 행 id."""
    return f"skip:{source_id}:{reason}"


def end_fields(status: RunStatus, now: datetime, reason: str | None) -> dict[str, Any]:
    """끝나는 이동이 함께 적는 칸. 끝난 시각을 적고 남은 입력 본문을 지운다."""
    fields: dict[str, Any] = {
        "status": status,
        "last_event_at": now,
        "ended_at": now,
        "message": None,
    }
    if reason is not None:
        fields["reason"] = reason
    return fields


def status_fields(status: RunStatus, now: datetime, reason: str | None = None) -> dict[str, Any]:
    """상태를 옮기는 갱신의 칸. 열린 상태면 상태와 `last_event_at` 이고, 끝나는 상태면
    `end_fields` 다."""
    if status not in OPEN_STATUSES:
        return end_fields(status, now, reason)
    fields: dict[str, Any] = {"status": status, "last_event_at": now}
    if reason is not None:
        fields["reason"] = reason
    return fields


def stop_reason_of(run: AgentRun) -> str:
    """중지 표시가 남은 실행이 끝날 까닭. 표시의 까닭이고 없으면 `stopped` 다."""
    return run.stop_reason or STOPPED


def _source_in(sources: Collection[str]) -> dict[str, Any]:
    """sources 가운데 하나의 출처인 기록을 고르는 조건."""
    return {"source": {"$in": list(sources)}}


def _first_stop(now: datetime, reason: str) -> list[dict[str, Any]]:
    """중지 표시를 남기는 갱신. 처음 남긴 시각과 까닭이 남는다."""
    return [
        {
            "$set": {
                "stop_requested_at": {"$ifNull": ["$stop_requested_at", now]},
                "stop_reason": {"$ifNull": ["$stop_reason", reason]},
                "last_event_at": now,
            }
        }
    ]


class MongoAgentRunStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_RUNS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("source_id", ASCENDING), ("scheduled_for", ASCENDING)],
            name="uq_schedule_slot",
            unique=True,
            partialFilterExpression={"scheduled_for": {"$exists": True}},
        )
        await self.coll.create_index(
            [("source_id", ASCENDING), ("delivery_id", ASCENDING)],
            name="uq_webhook_delivery",
            unique=True,
            partialFilterExpression={"delivery_id": {"$exists": True}},
        )
        # 목록과 하루 상한이 쓴다
        await self.coll.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING), ("last_event_at", DESCENDING)],
            name="ix_agent_last_event",
        )
        await self.coll.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING), ("created_at", ASCENDING)],
            name="ix_agent_created",
        )
        # 동시 실행과 승인 대기 상한이 쓴다
        await self.coll.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING), ("status", ASCENDING)],
            name="ix_agent_status",
        )
        # "앞 실행이 아직 돈다"와 소스를 멈출 때의 중지가 쓴다
        await self.coll.create_index(
            [("source_id", ASCENDING), ("status", ASCENDING)], name="ix_source_status"
        )
        await self.coll.create_index(
            [("status", ASCENDING), ("next_attempt_at", ASCENDING)], name="ix_status_next_attempt"
        )
        # 지켜보기 워커가 마감 지난 것과 조용한 것을 찾는다
        await self.coll.create_index(
            [("status", ASCENDING), ("deadline", ASCENDING)], name="ix_status_deadline"
        )
        await self.coll.create_index(
            [("status", ASCENDING), ("last_event_at", ASCENDING)], name="ix_status_last_event"
        )
        # 결정 경로와 승인자 목록이 세션으로 실행을 찾는다
        await self.coll.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING), ("session_id", ASCENDING)],
            name="ix_agent_session",
        )
        # 부른 턴의 위임이 끝났을 때 그 턴이 시작한 호출을 찾는다 (P46 6.5절)
        await self.coll.create_index(
            [("caller.grant_id", ASCENDING), ("status", ASCENDING)],
            name="ix_caller_grant",
            partialFilterExpression={"caller.grant_id": {"$exists": True}},
        )
        # 맡긴 작업 하나의 실행들을 찾는다 (P46 6.7절)
        await self.coll.create_index(
            [("caller.thread_id", ASCENDING), ("status", ASCENDING)],
            name="ix_caller_thread",
            partialFilterExpression={"caller.thread_id": {"$exists": True}},
        )
        # 부른 세션의 트레이스 창이 그 세션의 턴들이 부른 실행을 찾는다 (P46 6.5절)
        await self.coll.create_index(
            [("caller.session_id", ASCENDING), ("caller.agent_id", ASCENDING)],
            name="ix_caller_session",
            partialFilterExpression={"caller.session_id": {"$exists": True}},
        )
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def insert(self, run: AgentRun) -> bool:
        """새 기록을 넣는다. 같은 울림이나 같은 호출의 기록이 이미 있으면 거짓이다."""
        return await insert_new(self.coll, [run.to_doc()]) == 1

    async def get(self, tenant: str, agent_id: str, run_id: str) -> AgentRun | None:
        doc = await self.coll.find_one({"_id": run_id, "tenant": tenant, "agent_id": agent_id})
        return AgentRun.from_doc(doc) if doc else None

    async def for_session(self, tenant: str, agent_id: str, session_id: str) -> AgentRun | None:
        """그 세션의 실행. 실행 세션이 아니면 None 이다. 맡긴 작업의 세션은 실행이 여럿이라 가장
        늦게 만든 것이다."""
        doc = await self.coll.find_one(
            {"tenant": tenant, "agent_id": agent_id, "session_id": session_id}, sort=_LATEST
        )
        return AgentRun.from_doc(doc) if doc else None

    async def for_sessions(
        self, tenant: str, agent_id: str, session_ids: list[str]
    ) -> dict[str, AgentRun]:
        """세션 id → 그 세션의 실행. 실행 세션이 아닌 것은 빠진다. 실행이 여럿인 세션은
        `for_session` 처럼 가장 늦게 만든 것이다."""
        if not session_ids:
            return {}
        # 만든 차례로 읽어 같은 세션의 늦은 기록이 앞의 것을 덮는다
        cursor = self.coll.find(
            {"tenant": tenant, "agent_id": agent_id, "session_id": {"$in": session_ids}}
        ).sort("created_at", ASCENDING)
        return {d["session_id"]: AgentRun.from_doc(d) async for d in cursor}

    async def called_from_session(
        self, tenant: str, caller_agent_id: str, session_id: str, *, limit: int
    ) -> list[AgentRun]:
        """그 Agent 의 세션 하나에서 턴들이 부른 실행이다(호출과 맡긴 작업). 가장 늦게 만든 것부터
        limit 개까지다."""
        cursor = self.coll.find(
            {"tenant": tenant, "caller.session_id": session_id, "caller.agent_id": caller_agent_id},
            sort=_LATEST,
        ).limit(limit)
        return [AgentRun.from_doc(d) async for d in cursor]

    # -- 맡긴 작업 (P46 6.7절) ---------------------------------------------------------------

    async def for_thread(self, tenant: str, agent_id: str, thread_id: str) -> list[AgentRun]:
        """작업 하나의 실행들이다. 가장 늦게 만든 것부터다."""
        cursor = self.coll.find(
            {"tenant": tenant, "agent_id": agent_id, "caller.thread_id": thread_id}, sort=_LATEST
        )
        return [AgentRun.from_doc(d) async for d in cursor]

    async def thread_turn_running(self, thread_id: str, *, besides: str) -> bool:
        """그 작업에서 besides 가 아닌 실행이 아직 도는가. 그 턴이 끝나야 세션이 다음 턴을
        받는다."""
        doc = await self.coll.find_one(
            {"caller.thread_id": thread_id, "status": "running", "_id": {"$ne": besides}},
            projection={"_id": 1},
        )
        return doc is not None

    async def find_delivery(self, source_id: str, delivery_id: str) -> AgentRun | None:
        """웹훅 호출 하나의 기록. 같은 delivery id 로 다시 온 호출이 같은 실행을 받는다."""
        doc = await self.coll.find_one({"source_id": source_id, "delivery_id": delivery_id})
        return AgentRun.from_doc(doc) if doc else None

    async def find_slot(self, source_id: str, scheduled_for: datetime) -> AgentRun | None:
        """스케줄 울림 하나의 기록. 없으면 None 이다."""
        doc = await self.coll.find_one({"source_id": source_id, "scheduled_for": scheduled_for})
        return AgentRun.from_doc(doc) if doc else None

    async def list_for_agent(
        self, tenant: str, agent_id: str, *, limit: int, status: RunStatus | None = None
    ) -> list[AgentRun]:
        """마지막으로 움직인 것부터다. 건너뛴 울림을 세는 행도 셀 때마다 앞으로 온다."""
        query: dict[str, Any] = {"tenant": tenant, "agent_id": agent_id}
        if status is not None:
            query["status"] = status
        cursor = self.coll.find(query).sort("last_event_at", DESCENDING).limit(limit)
        return [AgentRun.from_doc(d) async for d in cursor]

    async def count_in_statuses(
        self,
        tenant: str,
        agent_id: str,
        statuses: tuple[RunStatus, ...],
        *,
        sources: Collection[str] | None = None,
    ) -> int:
        """statuses 의 실행 수. sources 를 주면 그 출처의 것만 센다. 상한은 이 판이 아는 출처만
        센다(`RUN_SOURCES`, P46 A0)."""
        query: dict[str, Any] = {
            "tenant": tenant,
            "agent_id": agent_id,
            "status": {"$in": statuses},
        }
        if sources is not None:
            query.update(_source_in(sources))
        return await self.coll.count_documents(query)

    async def count_created_since(
        self, tenant: str, agent_id: str, since: datetime, *, sources: Collection[str] | None = None
    ) -> int:
        """since 뒤에 만든 실행 수. skipped 행은 세지 않는다 (하루 상한, 10절). sources 를 주면 그
        출처의 것만 센다. 하루 상한은 이 판이 아는 출처만 센다(`RUN_SOURCES`, P46 A0)."""
        query: dict[str, Any] = {
            "tenant": tenant,
            "agent_id": agent_id,
            "created_at": {"$gte": since},
            "status": {"$ne": "skipped"},
        }
        if sources is not None:
            query.update(_source_in(sources))
        return await self.coll.count_documents(query)

    async def has_open_for_source(self, source_id: str) -> bool:
        doc = await self.coll.find_one(
            {"source_id": source_id, "status": {"$in": OPEN_STATUSES}}, projection={"_id": 1}
        )
        return doc is not None

    async def bump_skip(
        self,
        *,
        tenant: str,
        agent_id: str,
        source: RunSource,
        source_id: str,
        reason: SkipReason,
        acts_as: ActsAs,
        now: datetime,
    ) -> None:
        """실행이 되지 않은 울림을 소스와 까닭의 skipped 행에 하나 센다."""
        await self.coll.update_one(
            {"_id": skip_row_id(source_id, reason)},
            {
                "$inc": {"count": 1},
                "$set": {"last_at": now, "last_event_at": now, "expires_at": now + RUN_RETENTION},
                "$setOnInsert": {
                    "tenant": tenant,
                    "agent_id": agent_id,
                    "source": source,
                    "source_id": source_id,
                    "acts_as": acts_as.model_dump(),
                    "status": "skipped",
                    "reason": reason,
                    "attempts": 0,
                    "input_bytes": 0,
                    "created_at": now,
                    "first_at": now,
                },
            },
            upsert=True,
        )

    # -- 시작 워커의 리스 (5절) -------------------------------------------------------------

    async def acquire_start(
        self, owner: str, lease_seconds: float, now: datetime, *, busy: Collection[str] = ()
    ) -> AgentRun | None:
        """시작할 때가 된 `starting` 기록 하나에 리스를 건다. 가장 오래 기다린 것부터이고, busy 의
        Agent 는 건너뛴다(Agent 하나는 한 번에 하나). 이 판이 아는 출처의 기록만 잡는다. 뒤 판의
        기록은 리스를 걸지 않고 그 판의 워커에 둔다 (P46 A0)."""
        query: dict[str, Any] = {
            "status": "starting",
            **_source_in(RUN_SOURCES),
            "next_attempt_at": {"$lte": now},
            **free_lease(now),
        }
        if busy:
            query["agent_id"] = {"$nin": list(busy)}
        doc = await self.coll.find_one_and_update(
            query,
            {"$set": take_lease(owner, lease_seconds, now)},
            sort=[("next_attempt_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return AgentRun.from_doc(doc) if doc else None

    async def renew(self, run_id: str, owner: str, lease_seconds: float, now: datetime) -> bool:
        return await extend_lease(self.coll, run_id, owner, now + timedelta(seconds=lease_seconds))

    async def defer(self, run_id: str, owner: str, until: datetime) -> None:
        """지금 다룰 수 없는 기록을 until 까지 미룬다. 시도로 세지 않는다. 시작과 지켜보기 워커가
        쓴다."""
        await extend_lease(self.coll, run_id, owner, until)

    async def retry_start(
        self, run_id: str, owner: str, *, attempts: int, at: datetime, now: datetime
    ) -> bool:
        """시작을 at 에 다시 한다. 시도 수를 적고 리스를 놓는다. 다시 할 때까지의 조용함이 잃어버린
        실행으로 보이지 않게 `last_event_at` 을 찍는다."""
        result = await self.coll.update_one(
            {"_id": run_id, "status": "starting", "lease.owner": owner},
            {
                "$set": {
                    "attempts": attempts,
                    "next_attempt_at": at,
                    "lease": None,
                    "last_event_at": now,
                }
            },
        )
        return result.modified_count == 1

    async def note_start(
        self,
        run_id: str,
        owner: str,
        *,
        session_id: str,
        turn_id: str,
        grant_id: str | None,
        acts_as: ActsAs,
        now: datetime,
    ) -> bool:
        """런타임이 받은 턴의 세션·턴·위임과 실행 이름을 적고 입력 본문을 지운다. 리스를 쥔 시작
        워커만 적고 상태는 옮기지 않는다. 이벤트가 먼저 상태를 옮겼어도 이 칸들은 시작 워커만 안다
        (5절). 그 이벤트가 리스를 놓았으면 같은 세션이고 아직 아무도 적지 않았을 때 적는다. 리스를
        다른 워커가 가져갔으면 거짓이다."""
        result = await self.coll.update_one(
            {
                "_id": run_id,
                "$or": [
                    {"lease.owner": owner},
                    {"lease": None, "session_id": session_id, "started_at": None},
                ],
            },
            {
                "$set": {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "grant_id": grant_id,
                    "acts_as": acts_as.model_dump(),
                    "started_at": now,
                    "message": None,
                }
            },
        )
        return result.matched_count == 1

    # -- 이벤트와 지켜보기 (7·8절) ----------------------------------------------------------

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
        """런타임 이벤트 하나로 열린 실행을 옮긴다. 세션이 실행의 것(없으면 묶는다)이고 이벤트가
        마지막으로 받은 것보다 앞서지 않을 때만이다. unmarked 면 중지 표시가 없을 때만이다."""
        query: dict[str, Any] = {
            "_id": run_id,
            "status": {"$in": OPEN_STATUSES},
            "session_id": {"$in": [None, session_id]},
            "$or": [{"event_at": None}, {"event_at": {"$lte": ts}}],
        }
        if unmarked:
            query["stop_requested_at"] = None
        moved = status_fields(to_status, now, reason)
        result = await self.coll.update_one(
            query, {"$set": {**(fields or {}), **moved, "event_at": ts, "lease": None}}
        )
        return result.modified_count == 1

    async def acquire_watch(
        self, owner: str, lease_seconds: float, now: datetime, *, quiet_since: datetime
    ) -> AgentRun | None:
        """지켜볼 기록 하나에 리스를 건다. 마감이 지난 `waiting_approval` 과 quiet_since 뒤로
        움직임이 없는 `running` 이다. 시작 워커처럼 이 판이 아는 출처의 기록만 잡는다. 중지 표시가
        남은 조용한 `running` 은 출처와 상관없이 잡는다. 지켜보기는 그 기록을 표시의 까닭으로
        끝낸다(`RunWatchWorker._settle`). 출처의 뜻을 보지 않는 이동이라 뒤 판의 워커도 같이 한다
        (P46 A0)."""
        quiet = {"status": "running", "last_event_at": {"$lte": quiet_since}}
        due = {
            "$or": [
                {
                    **_source_in(RUN_SOURCES),
                    "status": "waiting_approval",
                    "deadline": {"$lte": now},
                },
                {**_source_in(RUN_SOURCES), **quiet},
                {**quiet, "stop_requested_at": {"$ne": None}},
            ]
        }
        doc = await self.coll.find_one_and_update(
            {"$and": [due, free_lease(now)]},
            {"$set": take_lease(owner, lease_seconds, now)},
            sort=[("last_event_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return AgentRun.from_doc(doc) if doc else None

    async def refuse_due(self, run_id: str, now: datetime, *, reason: str) -> bool:
        """마감이 지난 `waiting_approval` 실행을 `refused` 로 끝낸다. 결정이 먼저 옮겼거나 마감이
        새로 늦춰졌으면 거짓이다 (7절)."""
        result = await self.coll.update_one(
            {"_id": run_id, "status": "waiting_approval", "deadline": {"$lte": now}},
            {"$set": {**end_fields("refused", now, reason), "lease": None}},
        )
        return result.modified_count == 1

    async def settle_quiet(
        self,
        run: AgentRun,
        *,
        to_status: RunStatus,
        now: datetime,
        reason: str | None = None,
        deadline: datetime | None = None,
    ) -> bool:
        """조용한 `running` 실행을 런타임의 세션대로 맞춘다. 읽은 뒤에 이벤트나 플랫폼의 이동이
        있었으면(`last_event_at` 이 바뀌었으면) 거짓이다 (8절)."""
        fields = status_fields(to_status, now, reason)
        if deadline is not None:
            fields["deadline"] = deadline
        result = await self.coll.update_one(
            {"_id": run.id, "status": "running", "last_event_at": run.last_event_at},
            {"$set": {**fields, "lease": None}},
        )
        return result.modified_count == 1

    async def shift_deadlines(self, tenant: str, agent_id: str, shift: timedelta) -> int:
        """Agent 의 승인을 기다리는 실행의 마감을 shift 만큼 옮긴다. 세션 TTL 이 바뀐 배포가
        부른다 (7절). 옮긴 수다."""
        result = await self.coll.update_many(
            {"tenant": tenant, "agent_id": agent_id, "status": "waiting_approval"},
            [{"$set": {"deadline": {"$add": ["$deadline", shift.total_seconds() * 1000]}}}],
        )
        return result.modified_count

    # -- 이동과 중지 (7절) ---------------------------------------------------------------

    async def begin_decision(self, run_id: str, now: datetime) -> bool:
        """결정을 런타임에 보내기 전에 승인 대기 실행을 `running` 으로 옮긴다. 마감 전이고 중지
        표시가 없을 때만이다. 같은 초의 마감·중지와 한쪽만 남는다."""
        result = await self.coll.update_one(
            {
                "_id": run_id,
                "status": "waiting_approval",
                "deadline": {"$gt": now},
                "stop_requested_at": None,
            },
            {"$set": status_fields("running", now)},
        )
        return result.modified_count == 1

    async def mark_stop(self, run: AgentRun, now: datetime, reason: str = STOPPED) -> bool:
        """아직 열린 실행에 중지 표시를 남긴다. 처음 남긴 시각과 까닭이 남는다. 열린 실행이 아니면
        거짓이다."""
        result = await self.coll.update_one(
            {"_id": run.id, "tenant": run.tenant, "status": {"$in": OPEN_STATUSES}},
            _first_stop(now, reason),
        )
        return result.matched_count == 1

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
        """from_status 의 실행을 to_status 로 옮기는 조건부 갱신. 끝나는 상태면 끝난 시각을 적고
        입력 본문을 지운다. owner 를 주면 그 워커가 리스를 쥔 기록만, unmarked 면 중지 표시가 없는
        기록만 옮긴다."""
        query: dict[str, Any] = {"_id": run_id, "status": {"$in": from_status}}
        if owner is not None:
            query["lease.owner"] = owner
        if unmarked:
            query["stop_requested_at"] = None
        result = await self.coll.update_one(
            query, {"$set": {**status_fields(to_status, now, reason), **(extra or {})}}
        )
        return result.modified_count == 1

    async def cancel_open_for_source(
        self, source_id: str, reason: str, now: datetime
    ) -> list[AgentRun]:
        """소스가 멈추거나 지워질 때 그 소스의 열린 실행에 중지 표시를 남기고, 바로 끝낼 수 있는
        실행은 끝낸다(7절의 중지). 표시만 남은 도는 실행을 돌려준다. 그 턴은 런타임에서 멈춰야
        끝난다."""
        return await self._cancel_open({"source_id": source_id}, reason, now)

    async def cancel_open_for_agent(
        self, tenant: str, agent_id: str, reason: str, now: datetime
    ) -> list[AgentRun]:
        """Agent 를 지울 때 그 Agent 의 열린 실행을 소스와 상관없이 중지처럼 끝낸다."""
        return await self._cancel_open({"tenant": tenant, "agent_id": agent_id}, reason, now)

    async def first_turn_caller_grants(self) -> list[str]:
        """아직 첫 턴인 호출을 시작한 위임의 id 다. 지켜보기가 그 위임이 끝났는지 본다."""
        return await self.coll.distinct(
            "caller.grant_id", {"caller.grant_id": {"$type": "string"}, **_FIRST_TURN}
        )

    async def cancel_first_turn_for_grants(
        self, grant_ids: Collection[str], reason: str, now: datetime
    ) -> list[AgentRun]:
        """grant_ids 의 위임이 시작한 호출 가운데 아직 첫 턴이고 중지 표시가 없는 것을 중지처럼
        끝낸다. 이번에 표시를 남긴 도는 실행만 돌려준다. 이미 표시가 남은 실행은 건드리지 않는다.
        끝난 위임은 틱마다 다시 오므로, 건드리면 `last_event_at` 이 새로 적혀 지켜보기가 그 실행을
        조용하다고 보지 못한다. 승인을 기다리거나 기다린 적이 있는 실행은 두고, 그 실행은 제 마감이
        끝낸다 (P46 6.5절)."""
        if not grant_ids:
            return []
        query = {"caller.grant_id": {"$in": list(grant_ids)}, **_FIRST_TURN}
        unmarked = {**query, "stop_requested_at": None}
        ids = [d["_id"] async for d in self.coll.find(unmarked, {"_id": 1})]
        marked = [
            run_id
            for run_id in ids
            if await self._mark_unmarked({"_id": run_id, **unmarked}, reason, now)
        ]
        await self.coll.update_many(
            {**query, "status": "starting"}, {"$set": end_fields("cancelled", now, reason)}
        )
        # 표시를 남긴 뒤에 읽는다. 그사이 시작돼 running 이 된 실행도 돌려준다
        cursor = self.coll.find({"_id": {"$in": marked}, "status": "running"})
        return [AgentRun.from_doc(d) async for d in cursor]

    async def _mark_unmarked(self, query: dict[str, Any], reason: str, now: datetime) -> bool:
        result = await self.coll.update_one(query, _first_stop(now, reason))
        return result.modified_count == 1

    async def _cancel_open(
        self, query: dict[str, Any], reason: str, now: datetime
    ) -> list[AgentRun]:
        await self.coll.update_many(
            {**query, "status": {"$in": OPEN_STATUSES}}, _first_stop(now, reason)
        )
        await self.coll.update_many(
            {**query, "status": {"$in": STOP_NOW_STATUSES}},
            {"$set": end_fields("cancelled", now, reason)},
        )
        # 표시를 남긴 뒤에 읽는다. 그사이 시작돼 running 이 된 실행도 돌려준다
        cursor = self.coll.find({**query, "status": "running"})
        return [AgentRun.from_doc(d) async for d in cursor]


__all__ = [
    "AGENT_RUNS",
    "CALL_SOURCE",
    "MOVING_STATUSES",
    "OPEN_STATUSES",
    "RUN_CAP_SOURCES",
    "RUN_RETENTION",
    "RUN_SOURCES",
    "STOPPED",
    "STOP_NOW_STATUSES",
    "ActsAs",
    "AgentRun",
    "MongoAgentRunStore",
    "RunCaller",
    "RunSource",
    "RunStatus",
    "SkipReason",
    "end_fields",
    "input_digest",
    "new_run",
    "skip_row_id",
    "status_fields",
    "stop_reason_of",
]
