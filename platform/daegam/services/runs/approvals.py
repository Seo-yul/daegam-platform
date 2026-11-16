"""실행 세션의 결정과 승인자의 받은 결정함 (P41 7절).

플랫폼이 결정의 결과를 정한다. 결정 라우트는 실행 세션이면 먼저 결정할 사람인지 보고(묶인 사람이나
`agent:approve`), 그다음에 승인 대기 실행을 `running` 으로 옮긴 뒤 런타임을 부른다. 마감 전이고
중지 표시가 없을 때만 옮기고, 아니면 409 `run_closed` 다.

- 런타임이 오류로 답하거나 런타임을 부르기 전에 실패하면 턴이 열리지 않았으므로 승인 대기로
  되돌린다. 그사이 중지 표시가 남았으면 그 까닭으로 `cancelled` 이고, 런타임이 세션을 이미
  닫았으면(410 `session_closed`) `refused` 다. 연결 실패와 시간 초과는 되돌리지 않는다. 턴이 열렸을
  수 있어 `running` 으로 두고 이벤트나 잃어버린 실행의 확인이 끝낸다.
- 런타임이 받아들였는데 그사이 중지 표시가 남았으면 다시 연 턴을 멈춘다.

받은 결정함은 세션 목록에 `runs=only` 와 `status=pending` 을 준 것이다. 줄마다 그 세션의 실행을
붙이고 승인을 기다리는 실행의 세션만 남긴다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from daegam.services.runs.errors import RunError
from daegam.store.agent_runs import AgentRun, stop_reason_of
from daegam.store.base import utcnow

if TYPE_CHECKING:
    from daegam.services.runs.service import RunService

# 런타임이 결정에 답하는 오류 코드. 세션이 이미 닫혔다(410)
SESSION_CLOSED = "session_closed"


class RunApprovals:
    def __init__(self, service: RunService):
        self.service = service
        self.runs = service.runs

    # -- 결정 ---------------------------------------------------------------------------

    async def begin(
        self, tenant: str, agent_id: str, session_id: str, *, sub: str, approver: bool
    ) -> AgentRun | None:
        """결정을 런타임에 보내기 전에 부른다. 실행 세션이 아니면 None 이다. 묶인 사람도 승인자도
        아니면 아무것도 쓰기 전에 403 이다. 실행이 승인을 기다리고 마감 전이고 중지 표시가 없으면
        `running` 으로 옮기고 그 실행을 돌려준다. 아니면 409 `run_closed` 다."""
        run = await self.runs.for_session(tenant, agent_id, session_id)
        if run is None:
            return None
        if not approver and run.acts_as.sub != sub:
            raise RunError(
                403,
                "forbidden",
                "only the person a run acts as or a holder of agent:approve decides it",
            )
        if not await self.runs.begin_decision(run.id, utcnow()):
            raise RunError(
                409,
                "run_closed",
                "this run is no longer waiting for a decision: it was decided, stopped or refused "
                "at its deadline",
                run_id=run.id,
            )
        return run

    async def refused(self, run: AgentRun, status: int, code: str | None) -> None:
        """런타임이 결정을 오류 상태로 답했다. 런타임의 오류는 턴 리스를 잡기 전이다. 세션이 이미
        닫혔으면 `refused` 이고, 아니면 `not_sent` 다."""
        if status == 410 and code == SESSION_CLOSED:
            await self.runs.move(
                run.id,
                from_status=("running",),
                to_status="refused",
                now=utcnow(),
                reason=SESSION_CLOSED,
            )
            return
        await self.not_sent(run)

    async def not_sent(self, run: AgentRun) -> None:
        """결정이 턴을 열지 못했다. 중지 표시가 없을 때만 같은 갱신에서 승인 대기로 되돌리고,
        표시가 있으면 그 까닭으로 `cancelled` 다."""
        now = utcnow()
        if await self.runs.move(
            run.id,
            from_status=("running",),
            to_status="waiting_approval",
            now=now,
            unmarked=True,
        ):
            return
        current = await self.runs.get(run.tenant, run.agent_id, run.id)
        if current is None or current.status != "running":
            return
        await self.runs.move(
            run.id,
            from_status=("running",),
            to_status="cancelled",
            now=now,
            reason=stop_reason_of(current),
        )

    async def accepted(self, run: AgentRun, turn_id: str | None) -> None:
        """런타임이 결정을 받아 턴을 다시 열었다. 그사이 중지 표시가 남았으면 답이 알린 시도(없으면
        기록의 턴 id)를 멈춘다."""
        await self.service.stop_if_marked(run, turn_id=turn_id)

    # -- 받은 결정함 --------------------------------------------------------------------

    async def mark_sessions(
        self,
        tenant: str,
        agent_id: str,
        sessions: list[dict[str, Any]],
        *,
        runs: Literal["include", "only"],
        status: str | None,
    ) -> list[dict[str, Any]]:
        """런타임 세션 목록의 줄마다 그 세션의 실행(`run`: id, 상태, 마감)을 붙인다. 마감은 승인을
        기다릴 때만 있다. `only` 는 실행 세션만 남긴다. 실행을 모르는 옛 파드가 거르지 않은 목록을
        줘도 같다. 거기에 `status=pending` 이면 승인을 기다리는 실행의 세션만 남긴다. 거절되거나
        중지된 실행의 세션은 런타임이 닫을 때까지 pending 으로 남아 있기 때문이다."""
        only = runs == "only"
        waiting = only and status == "pending"
        found = await self.runs.for_sessions(tenant, agent_id, [s["session_id"] for s in sessions])
        marked = []
        for s in sessions:
            run = found.get(s["session_id"])
            if run is None:
                if not only:
                    marked.append({**s, "run": None})
                continue
            is_waiting = run.status == "waiting_approval"
            if waiting and not is_waiting:
                continue
            deadline = run.deadline if is_waiting else None
            marked.append({**s, "run": {"id": run.id, "status": run.status, "deadline": deadline}})
        return marked


__all__ = ["SESSION_CLOSED", "RunApprovals"]
