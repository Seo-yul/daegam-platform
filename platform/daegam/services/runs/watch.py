"""지켜보기 워커 (P41 5·7·8절).

두 가지를 본다. 런타임은 마감에 부르지 않는다. 시작 워커처럼 이 판이 아는 출처의 기록만 잡고,
중지 표시가 남은 조용한 `running` 기록만은 출처와 상관없이 잡아 표시의 까닭으로 끝낸다
(`MongoAgentRunStore.acquire_watch`, P46 A0).

- **마감.** 마감이 지난 `waiting_approval` 실행을 조건부 갱신 하나로 `refused` 로 끝낸다. 세션은
  다시 열지 않고 TTL 쓸기가 여유 안에 닫는다. 결정이 같은 초에 오면 한쪽만 남는다.
- **잃어버린 실행.** 마지막 움직임(`last_event_at`) 뒤로 `turn_timeout + 60초` 동안 조용한 `running`
  실행은 런타임의 가벼운 세션 읽기(`view=progress`)로 맞춘다. 세션이 아직 돌면 그대로 두고,
  아니면 전사의 마지막 항목이 정한다. `done` 이면 `succeeded`, `error{code}` 면 그 코드로 `failed`,
  열린 인터럽트면 `waiting_approval`(세션이 이미 닫혔으면 결정되지 않은 것이라 `refused`)이고, 끝
  항목이나 세션이 없으면 `run_lost` 다. 런타임에 닿지 못하면 다음 차례에 다시 본다. 맞추는 사이
  이벤트가 오면 이벤트가 이긴다(`last_event_at` 조건). 중지 표시가 남은 실행은 그 까닭으로, Agent 가
  없거나 내려간 실행은 `agent_deleted`·`agent_undeployed` 로 `cancelled` 다. 배포 중인 Agent 는
  롤아웃이 끝난 뒤에 본다.

틱마다 먼저, 부른 턴이 끝난 Agent 호출을 취소한다(`caller_sweep`, P46 6.5절). 위임의 끝이 호출을 그
자리에서 끝내지만, 이벤트 없이 지나는 위임의 마감과 그 끝을 받은 레플리카가 호출을 끝내지 못한
때를 이 쓸기가 덮는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from daegam.services.agent_runtime import (
    AgentRuntimeProxy,
    ends_undecided,
    serves_turns,
    transcript_items,
)
from daegam.services.lease_worker import LeaseWorker
from daegam.services.runs.events import approval_deadline
from daegam.services.runs.fire import GONE_STATES
from daegam.services.runs.service import AGENT_DELETED, AGENT_UNDEPLOYED
from daegam.services.runs.start import RUN_LOST
from daegam.services.turn_grants import DEADLINE_SLACK, turn_limits
from daegam.store.agent_runs import AgentRun, MongoAgentRunStore, RunStatus, stop_reason_of
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import parse_iso_utc, utcnow

logger = logging.getLogger("daegam.runs.watch")

# 마감으로 거절한 실행의 까닭
DEADLINE_PASSED = "approval_deadline"
# 런타임 전사의 항목 종류 (agent-runtime/runtime/transcript.py)
KIND_DONE = "done"
KIND_ERROR = "error"
CLOSED_SESSION_STATUS = "closed"


class RunWatchWorker(LeaseWorker[AgentRun]):
    task_prefix = "run-watch"
    tick_label = "run watch tick"
    item_label = "run"
    log = logger

    def __init__(
        self,
        agents: MongoAgentStore,
        runs: MongoAgentRunStore,
        proxy: AgentRuntimeProxy,
        *,
        owner: str | None = None,
        interval: float,
        lease_seconds: float,
        caller_sweep: Callable[[], Awaitable[int]] | None = None,
    ):
        super().__init__(owner=owner, interval=interval, lease_seconds=lease_seconds)
        self.agents = agents
        self.runs = runs
        self.proxy = proxy
        # 부른 턴이 끝난 Agent 호출을 끝낸다. 없으면(호출을 끈 플랫폼, 시험) 건너뛴다
        self.caller_sweep = caller_sweep

    async def tick(self) -> int:
        if self.caller_sweep is not None:
            try:
                await self.caller_sweep()
            except Exception:  # noqa: BLE001 - 쓸기의 실패가 지켜보기를 막지 않는다
                self.log.exception("run watch: the caller sweep failed")
        return await super().tick()

    async def acquire(self) -> AgentRun | None:
        now = utcnow()
        return await self.runs.acquire_watch(
            self.owner, self.lease_seconds, now, quiet_since=now - DEADLINE_SLACK
        )

    async def renew(self, item_id: str) -> bool:
        return await self.runs.renew(item_id, self.owner, self.lease_seconds, utcnow())

    async def process(self, item: AgentRun) -> None:
        try:
            async with self.heartbeating(item.id):
                await self.watch(item, utcnow())
        except Exception:  # noqa: BLE001 - 기록 하나의 실패가 다른 기록을 막지 않는다
            self.log.exception("run %s: watch failed", item.id)
            await self.runs.defer(item.id, self.owner, utcnow() + self.wait())

    def wait(self) -> timedelta:
        """다시 보기까지 쉬는 바닥. 틱 간격이고 적어도 1분이다."""
        return timedelta(seconds=max(self.interval, 60.0))

    async def watch(self, run: AgentRun, now: datetime) -> None:
        if run.status == "waiting_approval":
            if await self.runs.refuse_due(run.id, now, reason=DEADLINE_PASSED):
                self.log.info("run %s: refused at its approval deadline", run.id)
            return
        agent = await self.agents.get(run.agent_id, tenant=run.tenant)
        if agent is not None and agent.state == "deploying":
            # 지나가는 상태다. 롤아웃이 끝난 뒤에 본다
            await self.runs.defer(run.id, self.owner, now + self.wait())
            return
        quiet = self._quiet(agent)
        if now - run.last_event_at < quiet:
            await self.runs.defer(run.id, self.owner, run.last_event_at + quiet)
            return
        await self._settle(run, agent, now, quiet)

    @staticmethod
    def _quiet(agent: Agent | None) -> timedelta:
        """잃어버린 것으로 보기 전의 조용함. 핀된 리비전의 턴 시한과 60초다."""
        if agent is None:
            return DEADLINE_SLACK
        return timedelta(seconds=turn_limits(agent).turn_timeout_seconds) + DEADLINE_SLACK

    async def _settle(
        self, run: AgentRun, agent: Agent | None, now: datetime, quiet: timedelta
    ) -> None:
        if run.stop_requested_at is not None:
            # 멈추라고 한 실행이다. 턴의 끝을 기다리지 않고 표시의 까닭으로 끝낸다
            await self._move(run, "cancelled", stop_reason_of(run), now)
            return
        if agent is None:
            await self._move(run, "cancelled", AGENT_DELETED, now)
            return
        if agent.state in GONE_STATES or not serves_turns(agent):
            await self._move(run, "cancelled", AGENT_UNDEPLOYED, now)
            return
        if run.session_id is None:
            await self._move(run, "failed", RUN_LOST, now)
            return
        try:
            status, body = await self.proxy.session(
                agent, run.session_id, user_sub=run.acts_as.sub, approver=True, view="progress"
            )
        except Exception as e:  # noqa: BLE001 - 런타임에 닿지 못하면 다음 차례에 다시 본다
            self.log.warning("run %s: session not read: %s", run.id, e)
            await self.runs.defer(run.id, self.owner, now + self.wait())
            return
        if status == 404:
            await self._move(run, "failed", RUN_LOST, now)
            return
        if status != 200 or not isinstance(body, dict):
            await self.runs.defer(run.id, self.owner, now + self.wait())
            return
        session = body.get("session") if isinstance(body.get("session"), dict) else {}
        if session.get("running"):
            await self.runs.defer(run.id, self.owner, now + quiet)
            return
        await self._from_transcript(run, agent, session, transcript_items(body), now)

    async def _from_transcript(
        self,
        run: AgentRun,
        agent: Agent,
        session: dict[str, Any],
        items: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        last = items[-1] if items else {}
        kind = last.get("kind")
        if kind == KIND_DONE:
            await self._move(run, "succeeded", None, now)
        elif kind == KIND_ERROR:
            await self._move(run, "failed", str(last.get("code") or "turn_failed"), now)
        elif ends_undecided(items):
            if session.get("status") == CLOSED_SESSION_STATUS:
                await self._move(run, "refused", DEADLINE_PASSED, now)
                return
            last_turn_at = parse_iso_utc(session.get("last_turn_at")) or now
            deadline = approval_deadline(last_turn_at, turn_limits(agent).session_ttl_seconds)
            await self._move(run, "waiting_approval", None, now, deadline=deadline)
        else:
            await self._move(run, "failed", RUN_LOST, now)

    async def _move(
        self,
        run: AgentRun,
        to_status: RunStatus,
        reason: str | None,
        now: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None:
        moved = await self.runs.settle_quiet(
            run, to_status=to_status, now=now, reason=reason, deadline=deadline
        )
        if moved:
            self.log.info(
                "run %s: settled as %s (%s) after a quiet spell", run.id, to_status, reason
            )


__all__ = ["DEADLINE_PASSED", "RunWatchWorker"]
