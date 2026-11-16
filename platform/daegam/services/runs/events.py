"""런타임 이벤트로 옮기는 실행의 상태 (P41 7·8절).

이벤트 수집이 저장한 `turn_start` 와 `turn_end` 가운데 `run_id` 를 실은 것만 본다. 그 실행이
이벤트를 보낸 인스턴스 토큰의 Agent 것이고 세션이 실행의 세션(아직 없으면 그 세션으로 묶는다)일
때만 받는다.
이벤트 자신의 시각으로 줄을 세워 그보다 앞선 이벤트는 버리고(`event_at`), 끝난 상태는 다시 옮기지
않는다. 받은 이벤트마다 `last_event_at` 을 찍는다.

- `turn_start` 는 실행을 `running` 으로 옮긴다. 시작 워커가 헤더를 읽기 전에 죽었어도 세션이 묶이고,
  결정 경로가 놓친 재개도 `waiting_approval` 에서 옮긴다(7절의 뒷받침).
- `turn_end` 는 `done` 이면 `succeeded`, 인터럽트면 새 마감과 함께 `waiting_approval`(중지 표시가
  있으면 `cancelled`), 중지면 `cancelled`, 사람이 접근을 잃었으면 `failed`(`turn_access_lost`),
  나머지 오류 코드면 그 코드로 `failed` 다. `cancelled` 의 까닭은 중지 표시의 까닭이고 없으면
  `stopped` 다.

마감은 `turn_end` 의 시각에 핀된 리비전의 세션 TTL 을 더하고 `min(5분, TTL / 2)` 를 뺀 때다. 세션이
닫히기 전에 플랫폼이 먼저 거절한다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from daegam.services.telemetry.normalize import TURN_END, TURN_START
from daegam.services.turn_grants import turn_limits
from daegam.store.agent_runs import OPEN_STATUSES, STOPPED, AgentRun, MongoAgentRunStore, RunStatus
from daegam.store.agents import MongoAgentStore
from daegam.store.base import utcnow

logger = logging.getLogger("daegam.runs.events")

# 세션이 닫히기 전 마감의 여유 위 끝 (7절)
DEADLINE_MARGIN = timedelta(minutes=5)
# 런타임의 turn_end status. 인터럽트는 런타임이 pending 이라 적는다
DONE = "done"
INTERRUPTED = frozenset({"pending", "interrupt"})
CANCELLED = "cancelled"
TURN_ACCESS_LOST = "turn_access_lost"


def approval_deadline(last_turn_at: datetime, session_ttl_seconds: int) -> datetime:
    """승인을 기다리는 실행의 마감. 세션이 TTL 로 닫히기 전이다."""
    ttl = timedelta(seconds=session_ttl_seconds)
    return last_turn_at + ttl - min(DEADLINE_MARGIN, ttl / 2)


def turn_end_outcome(status: Any, code: Any) -> tuple[RunStatus, str | None]:
    """turn_end 의 status 와 오류 코드가 정하는 (상태, 까닭). 인터럽트는 `waiting_approval` 이다."""
    if status == DONE:
        return "succeeded", None
    if status in INTERRUPTED:
        return "waiting_approval", None
    if status == CANCELLED:
        if code == TURN_ACCESS_LOST:
            return "failed", TURN_ACCESS_LOST
        return "cancelled", STOPPED
    return "failed", str(status or "turn_failed")


def _run_events(docs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """실행의 턴 이벤트를 이벤트 시각 순으로."""
    picked = [
        d
        for d in docs
        if d.get("event") in (TURN_START, TURN_END)
        and isinstance((d.get("data") or {}).get("run_id"), str)
        and isinstance(d.get("session_id"), str)
    ]
    return sorted(picked, key=lambda d: d["ts"])


class RunEvents:
    def __init__(self, runs: MongoAgentRunStore, agents: MongoAgentStore):
        self.runs = runs
        self.agents = agents

    async def apply(self, agent_id: str, tenant: str, docs: list[dict[str, Any]]) -> None:
        """이벤트 수집이 저장한 뒤 부른다. 실패는 로그만 남긴다. 잃어버린 실행의 확인이 끝을
        맞춘다."""
        for doc in _run_events(docs):
            try:
                await self._apply(agent_id, tenant, doc)
            except Exception as e:  # noqa: BLE001 - 이벤트는 이미 저장됐다
                logger.warning("run event %s not applied: %s", doc.get("_id"), e)

    async def _apply(self, agent_id: str, tenant: str, doc: dict[str, Any]) -> None:
        data = doc.get("data") or {}
        run = await self.runs.get(tenant, agent_id, data["run_id"])
        if run is None or run.status not in OPEN_STATUSES:
            return
        session_id = doc["session_id"]
        if run.session_id is not None and run.session_id != session_id:
            return
        ts, now = doc["ts"], utcnow()
        fields: dict[str, Any] = {"session_id": session_id, "message": None}
        if isinstance(doc.get("turn_id"), str):
            fields["turn_id"] = doc["turn_id"]
        if doc["event"] == TURN_START:
            await self.runs.apply_event(
                run.id, session_id, ts=ts, to_status="running", now=now, fields=fields
            )
            return
        to_status, reason = turn_end_outcome(doc.get("status"), data.get("code"))
        if to_status == "cancelled":
            # 중지 표시의 까닭(멈춤, 접근을 잃음)이 런타임의 중지보다 많이 말한다
            reason = await self._mark_reason(run, reason)
        if to_status != "waiting_approval":
            await self.runs.apply_event(
                run.id,
                session_id,
                ts=ts,
                to_status=to_status,
                now=now,
                reason=reason,
                fields=fields,
            )
            return
        # 중지 표시가 없을 때만 승인을 기다린다. 읽은 뒤 표시가 남았어도 갱신이 놓치고 멈춘다
        waiting = {**fields, "deadline": await self._deadline(run.agent_id, tenant, ts)}
        if run.stop_requested_at is None and await self.runs.apply_event(
            run.id,
            session_id,
            ts=ts,
            to_status="waiting_approval",
            now=now,
            fields=waiting,
            unmarked=True,
        ):
            return
        await self.runs.apply_event(
            run.id,
            session_id,
            ts=ts,
            to_status="cancelled",
            now=now,
            reason=await self._mark_reason(run, STOPPED),
            fields=fields,
        )

    async def _mark_reason(self, run: AgentRun, default: str | None) -> str | None:
        """중지 표시의 까닭. 앞서 읽은 뒤에 표시가 남았을 수 있어 다시 읽는다. 표시가 없으면
        default 다."""
        fresh = await self.runs.get(run.tenant, run.agent_id, run.id)
        return (fresh.stop_reason if fresh is not None else None) or default

    async def _deadline(self, agent_id: str, tenant: str, last_turn_at: datetime) -> datetime:
        agent = await self.agents.get(agent_id, tenant=tenant)
        ttl = turn_limits(agent).session_ttl_seconds if agent is not None else 0
        return approval_deadline(last_turn_at, ttl)


__all__ = ["DEADLINE_MARGIN", "RunEvents", "approval_deadline", "turn_end_outcome"]
