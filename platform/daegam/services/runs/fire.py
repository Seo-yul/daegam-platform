"""발사 워커 (P41 3·5절).

울릴 때가 된 스케줄 하나에 리스를 걸고, 그 울림을 실행으로 만들지 건너뛸지 정한다. 건너뛰는 울림은
기록을 만들지 않고 소스의 skipped 행에 까닭마다 센다. 실행으로 만드는 울림은 먼저 그 울림의 기록을
넣고 그다음에 `next_run_at` 을 옮긴다. 둘 사이에 죽으면 다음 차례가 남은 기록을 찾아 옮기기만 한다
(12절의 어긋남 3).

건너뛰는 까닭은 셋이다.
- `missed`: 울림이 `min(간격, 1시간)` 보다 오래 지났다. 그사이의 울림은 하나로 세고,
  그 창 안에 든 가장 늦은 울림이 있으면 그것은 한 번 친다.
- `previous_running`: 그 스케줄의 앞 실행이 아직 열려 있다.
- `agent_run_limit`: Agent 의 상한에 닿았다.

5분 바닥은 울림 시각끼리 견준다(`last_slot_at`). 벽시계로 견주면 틱이 늦은 만큼 다음 울림이
바닥에 걸린다. 처리하다 실패한 스케줄은 리스를 늘려 물러난다. 같은 스케줄이 한 틱을 붙잡지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from daegam.services.lease_worker import LeaseWorker
from daegam.services.runs import clock
from daegam.services.runs.service import AGENT_UNDEPLOYED, RunService
from daegam.store.agent_runs import SkipReason, new_run
from daegam.store.agent_schedules import AgentSchedule
from daegam.store.agents import MongoAgentStore
from daegam.store.base import utcnow

logger = logging.getLogger("daegam.runs.fire")

# 늦은 울림을 놓친 것으로 보는 한도의 위 끝 (3절)
MISSED_CAP = timedelta(hours=1)
# Agent 가 이 상태면 스케줄을 멈춘다. 배포 중(deploying)은 지나가는 상태라 다음 틱에 다시 본다
GONE_STATES = frozenset({"draft", "undeploying", "deleting"})
# 실패한 스케줄이 물러나는 시간의 위 끝
MAX_BACKOFF = timedelta(hours=1)


class ScheduleFireWorker(LeaseWorker[AgentSchedule]):
    task_prefix = "run-fire"
    tick_label = "schedule fire tick"
    item_label = "schedule"
    log = logger

    def __init__(
        self,
        agents: MongoAgentStore,
        service: RunService,
        *,
        owner: str | None = None,
        interval: float,
        lease_seconds: float,
    ):
        super().__init__(owner=owner, interval=interval, lease_seconds=lease_seconds)
        self.agents = agents
        self.service = service
        self.runs = service.runs
        self.schedules = service.schedules

    async def acquire(self) -> AgentSchedule | None:
        return await self.schedules.acquire_due(self.owner, self.lease_seconds, utcnow())

    async def renew(self, item_id: str) -> bool:
        return await self.schedules.renew(item_id, self.owner, self.lease_seconds, utcnow())

    async def process(self, item: AgentSchedule) -> None:
        try:
            async with self.heartbeating(item.id):
                await self.fire(item, utcnow())
        except Exception:  # noqa: BLE001 - 스케줄 하나의 실패가 다음 스케줄을 막지 않는다
            self.log.exception("schedule %s: firing failed", item.id)
            await self.schedules.back_off(item.id, self.owner, utcnow() + self.backoff(item))

    def wait(self) -> timedelta:
        """스케줄을 다시 보기까지 쉬는 바닥. 틱 간격이고 적어도 1분이다."""
        return timedelta(seconds=max(self.interval, 60.0))

    def backoff(self, schedule: AgentSchedule) -> timedelta:
        """실패한 스케줄이 쉬는 시간. `wait` 에서 시작해 실패마다 두 배가 된다."""
        return min(self.wait() * (2 ** min(schedule.fire_failures, 10)), MAX_BACKOFF)

    async def fire(self, schedule: AgentSchedule, now: datetime) -> str | None:
        """울림 하나를 처리하고 만든 실행 id 를 돌려준다. 건너뛰거나 멈췄으면 None 이다."""
        agent = await self.agents.get(schedule.agent_id, tenant=schedule.tenant)
        if agent is None or agent.state in GONE_STATES:
            # Agent 를 지웠거나 내렸다. 다시 켜기 전까지 울리지 않는다 (3절). 지운 Agent 의
            # 스케줄을 지우는 것은 Agent 삭제의 훅이다(B1a 둘째 묶음)
            await self._pause(schedule, AGENT_UNDEPLOYED, now)
            return None
        if agent.state == "deploying":
            # 지나가는 상태다. 리스를 놓으면 같은 틱이 이 스케줄을 다시 잡으므로 미뤄 둔다
            await self.schedules.defer(schedule.id, self.owner, now + self.wait())
            return None
        if schedule.expires_at <= now:
            await self._pause(schedule, "expired", now)
            return None
        zone = ZoneInfo(schedule.time_zone)
        slot = schedule.next_run_at
        if await self._resume_recorded(schedule, zone, slot):
            return None
        if now - slot > self._window(schedule.cron, zone, slot):
            latest = clock.latest_fire(schedule.cron, zone, max(slot, now - MISSED_CAP), now)
            if latest is not None and await self._resume_recorded(schedule, zone, latest):
                return None
            await self._count_skip(schedule, "missed", now)
            if latest is None or now - latest > self._window(schedule.cron, zone, latest):
                await self.schedules.advance(
                    schedule, self.owner, next_run_at=clock.next_fire(schedule.cron, zone, now)
                )
                return None
            # 창 안에 든 가장 늦은 울림은 한 번 친다
            slot = latest
        following = clock.next_fire(schedule.cron, zone, slot)
        if schedule.last_slot_at is not None and slot - schedule.last_slot_at < clock.MIN_GAP:
            # 바닥 (목표 5). 저장할 때 규칙을 걸렀지만 시간대 규칙 변경이나 옛 기록도 막는다
            floor = schedule.last_slot_at + clock.MIN_GAP - timedelta(microseconds=1)
            await self.schedules.advance(
                schedule,
                self.owner,
                next_run_at=clock.next_fire(schedule.cron, zone, max(slot, floor)),
            )
            return None
        if await self.runs.has_open_for_source(schedule.id):
            await self._skip(schedule, "previous_running", now, following)
            return None
        if await self.service.cap_error(schedule.tenant, schedule.agent_id, now) is not None:
            await self._skip(schedule, "agent_run_limit", now, following)
            return None
        run = new_run(
            tenant=schedule.tenant,
            agent_id=schedule.agent_id,
            source="schedule",
            acts_as=schedule.acts_as,
            now=now,
            source_id=schedule.id,
            scheduled_for=slot,
        )
        if not await self.runs.insert(run):
            # 다른 레플리카가 같은 울림을 방금 넣었다. 다음 차례가 남은 기록으로 옮긴다
            self.log.info("schedule %s: slot %s already recorded", schedule.id, slot.isoformat())
            await self.schedules.release(schedule.id, self.owner)
            return None
        await self.schedules.advance(
            schedule, self.owner, next_run_at=following, slot=slot, fired_at=now, run_id=run.id
        )
        return run.id

    async def _resume_recorded(
        self, schedule: AgentSchedule, zone: ZoneInfo, slot: datetime
    ) -> bool:
        """앞 차례가 이 울림의 기록을 넣고 옮기기 전에 죽었으면, 기록은 그대로 두고 그 울림으로
        옮기기만 한다. 그랬으면 참이다."""
        recorded = await self.runs.find_slot(schedule.id, slot)
        if recorded is None:
            return False
        await self.schedules.advance(
            schedule,
            self.owner,
            next_run_at=clock.next_fire(schedule.cron, zone, slot),
            slot=slot,
            fired_at=recorded.created_at,
            run_id=recorded.id,
        )
        return True

    @staticmethod
    def _window(rule: str, zone: ZoneInfo, slot: datetime) -> timedelta:
        """울림이 이보다 늦으면 놓친 것이다. 다음 울림까지의 간격과 한 시간 가운데 짧은 것이다."""
        return min(clock.next_fire(rule, zone, slot) - slot, MISSED_CAP)

    async def _count_skip(self, schedule: AgentSchedule, reason: SkipReason, now: datetime) -> None:
        await self.runs.bump_skip(
            tenant=schedule.tenant,
            agent_id=schedule.agent_id,
            source="schedule",
            source_id=schedule.id,
            reason=reason,
            acts_as=schedule.acts_as,
            now=now,
        )

    async def _skip(
        self, schedule: AgentSchedule, reason: SkipReason, now: datetime, next_run_at: datetime
    ) -> None:
        await self._count_skip(schedule, reason, now)
        await self.schedules.advance(schedule, self.owner, next_run_at=next_run_at)

    async def _pause(self, schedule: AgentSchedule, reason: str, now: datetime) -> None:
        """워커가 멈춘다. 사람이 멈출 때처럼 소스의 열린 실행도 멈춘다 (3절)."""
        if await self.schedules.pause_held(schedule, self.owner, reason):
            await self.service.end_open_for_source(schedule.id, reason, now)


__all__ = ["GONE_STATES", "MISSED_CAP", "ScheduleFireWorker"]
