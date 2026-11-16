"""Agent 의 스케줄 (P41 3절).

agent_schedules {_id, tenant, agent_id, acts_as, revision, cron, time_zone, message, expires_at,
                 expires_in_days, paused, paused_reason?, paused_cause?, next_run_at, last_slot_at?,
                 last_fired_at?, last_run_id?, fire_failures, lease?, created_at, updated_at}

소스가 함께 쓰는 칸과 저장은 `store/run_sources.py` 에 있다. `next_run_at` 은 다음 울림의 UTC
시각이고 바뀔 때와 다시 켤 때 지금부터 다시 센다. `last_slot_at` 은 마지막으로 실행이 된 울림
시각이고 5분 바닥이 이것과 견준다. `last_fired_at` 은 그 울림을 처리한 벽시계 시각이다(화면용).

발사 워커는 울릴 때가 된 스케줄 하나에 리스를 걸고(`lease`) 울림의 실행 기록을 넣은 뒤 그 리스를
가진 채로, 그리고 그사이 사람이 바꾸지 않았을 때만(`updated_at`) 다음 울림을 옮긴다. 처리하다
실패하면 리스를 늘려 물러나고(`fire_failures` 로 두 배씩) 다른 스케줄이 먼저 돈다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument

from daegam.store.base import ItemLease, UtcDatetime, extend_lease, free_lease, take_lease
from daegam.store.run_sources import MongoRunSourceStore, RunSource

AGENT_SCHEDULES = "agent_schedules"


class AgentSchedule(RunSource):
    cron: str
    time_zone: str = "UTC"
    message: str
    next_run_at: UtcDatetime
    last_slot_at: UtcDatetime | None = None
    last_fired_at: UtcDatetime | None = None
    last_run_id: str | None = None
    fire_failures: int = 0
    lease: ItemLease | None = None

    def run_message(self) -> str | None:
        return self.message


class MongoAgentScheduleStore(MongoRunSourceStore[AgentSchedule]):
    collection = AGENT_SCHEDULES
    model = AgentSchedule
    # 사람의 변경은 실패 횟수를 지워 물러나 있던 스케줄도 바로 다시 돈다
    on_change = {"fire_failures": 0}

    async def init_schema(self) -> None:
        await super().init_schema()
        await self.coll.create_index(
            [("paused", ASCENDING), ("next_run_at", ASCENDING)], name="ix_paused_next_run"
        )

    async def acquire_due(
        self, owner: str, lease_seconds: float, now: datetime
    ) -> AgentSchedule | None:
        """울릴 때가 된 켜진 스케줄 하나에 리스를 건다. 가장 오래 기다린 것부터다."""
        doc = await self.coll.find_one_and_update(
            {"paused": False, "next_run_at": {"$lte": now}, **free_lease(now)},
            {"$set": take_lease(owner, lease_seconds, now)},
            sort=[("next_run_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return AgentSchedule.from_doc(doc) if doc else None

    async def renew(
        self, schedule_id: str, owner: str, lease_seconds: float, now: datetime
    ) -> bool:
        return await extend_lease(
            self.coll, schedule_id, owner, now + timedelta(seconds=lease_seconds)
        )

    def _held(self, schedule: AgentSchedule, owner: str) -> dict[str, Any]:
        """리스를 가진 채이고, 그사이 사람이 바꾸지 않았다. 바꿨으면 사람의 변경이 이기고 리스는
        저절로 끝난다."""
        return {"_id": schedule.id, "lease.owner": owner, "updated_at": schedule.updated_at}

    async def advance(
        self,
        schedule: AgentSchedule,
        owner: str,
        *,
        next_run_at: datetime,
        slot: datetime | None = None,
        fired_at: datetime | None = None,
        run_id: str | None = None,
    ) -> bool:
        """다음 울림을 옮기고 리스를 놓는다. slot 이 있으면 그 울림이 실행이 된 것이고 그 울림
        시각과 처리한 시각, 실행 id 를 적는다."""
        fields: dict[str, Any] = {"next_run_at": next_run_at, "lease": None, "fire_failures": 0}
        if slot is not None:
            fields.update(last_slot_at=slot, last_fired_at=fired_at, last_run_id=run_id)
        result = await self.coll.update_one(self._held(schedule, owner), {"$set": fields})
        return result.modified_count == 1

    async def pause_held(self, schedule: AgentSchedule, owner: str, reason: str) -> bool:
        """워커가 멈춘다(만료, Agent 내림). 다음 울림은 그대로 두고 리스를 놓는다."""
        result = await self.coll.update_one(
            self._held(schedule, owner),
            {"$set": {"paused": True, "paused_reason": reason, "lease": None}},
        )
        return result.modified_count == 1

    async def defer(self, schedule_id: str, owner: str, until: datetime) -> None:
        """지금 칠 수 없는 스케줄(배포 중인 Agent)을 until 까지 미룬다. 실패로 세지 않는다."""
        await extend_lease(self.coll, schedule_id, owner, until)

    async def back_off(self, schedule_id: str, owner: str, until: datetime) -> None:
        """처리에 실패한 스케줄을 until 까지 쉬게 한다. 실패 횟수를 센다."""
        await self.coll.update_one(
            {"_id": schedule_id, "lease.owner": owner},
            {"$set": {"lease.until": until}, "$inc": {"fire_failures": 1}},
        )

    async def release(self, schedule_id: str, owner: str) -> None:
        await self.coll.update_one(
            {"_id": schedule_id, "lease.owner": owner}, {"$set": {"lease": None}}
        )


__all__ = ["AGENT_SCHEDULES", "AgentSchedule", "MongoAgentScheduleStore"]
