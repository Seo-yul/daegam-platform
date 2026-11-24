"""Agent 호출과 맡긴 작업의 기계 경로가 요청마다 쓰는 감사 행 (P46 6.8절).

감사 미들웨어는 이 경로들을 뺀다(`audit/middleware.py` 의 `UNAUDITED_PATHS`). 미들웨어는 사람의
토큰이 아닌 요청의 사람과 두 Agent 를 알지 못하기 때문이다. 경로의 서비스가 요청을 다루며 알게 된
것(`Seen`)을 실어 행 하나를 쓴다. 사람, 부르는 Agent 와 호출받는 Agent, 실행, 결과다. Bearer 가 없는
요청과 뜻밖의 오류(500)도 행을 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from daegam.audit.store import AuditEvent, MongoAuditStore
from daegam.errors import ServiceError
from daegam.store.base import new_id
from daegam.urls import AGENT_CALL_PATH, API_PREFIX

if TYPE_CHECKING:
    from daegam.services.agent_call_runs import CallingTurn

logger = logging.getLogger("daegam.agent_calls")

# 감사 행의 리소스
AUDIT_RESOURCE = "agent-calls"


@dataclass(frozen=True)
class AuditedRoute:
    """감사 행이 적는 경로 하나다. path 는 호출받는 Agent 의 id 를 받는 틀이고 ok_status 는 받아들인
    요청의 상태다."""

    action: str
    method: str = "POST"
    path: str = AGENT_CALL_PATH
    ok_status: int = 200


@dataclass
class Seen:
    """감사 행이 적을, 요청을 다루며 알게 된 것이다."""

    caller: CallingTurn | None = None
    run_id: str | None = None
    status: str | None = None
    thread_id: str | None = None


class CallAudit:
    def __init__(self, store: MongoAuditStore | None):
        # 없으면(시험) 행을 쓰지 않는다
        self.store = store

    async def audited(
        self, route: AuditedRoute, called_id: str, seen: Seen, work: Any
    ) -> dict[str, Any]:
        """work 를 돌리고 그 요청의 감사 행을 쓴다. work 가 seen 에 실행의 상태를 적지 않았으면
        답의 `status` 가 그 상태다."""
        try:
            answer = await work()
        except ServiceError as e:
            await self._audit(route, called_id, seen, e.status, e.code)
            raise
        except HTTPException as e:
            # 401 은 앱의 다른 경로처럼 HTTPException 이다(`errors.unauthorized`)
            detail = e.detail if isinstance(e.detail, dict) else {}
            await self._audit(route, called_id, seen, e.status_code, detail.get("code"))
            raise
        except Exception:
            # 뜻밖의 오류도 요청 하나의 행을 남긴다. 답은 앱의 500 이고 코드는 없다
            await self._audit(route, called_id, seen, 500, None)
            raise
        seen.status = seen.status or answer.get("status")
        await self._audit(route, called_id, seen, route.ok_status, answer.get("code"))
        return answer

    async def _audit(
        self, route: AuditedRoute, called_id: str, seen: Seen, status: int, code: str | None
    ) -> None:
        """요청 하나의 감사 행이다. 막혀도 답은 같으므로 경고만 남긴다."""
        if self.store is None:
            return
        caller = seen.caller
        params = {
            "agent_id": called_id,
            "caller_agent_id": caller.agent.acting_id if caller is not None else None,
            "run_status": seen.status,
        }
        if seen.thread_id is not None:
            params["thread_id"] = seen.thread_id
        event = AuditEvent(
            sub=caller.acts_as.sub if caller is not None else None,
            subject_type="user" if caller is not None else "system",
            tenant=caller.agent.tenant if caller is not None else None,
            action=route.action,
            resource=AUDIT_RESOURCE,
            resource_id=seen.run_id,
            path_params=params,
            method=route.method,
            path=API_PREFIX + route.path.format(agent_id=called_id),
            request_id=new_id(),
            status=status,
            result="ok" if status < 400 else "error",
            error_code=code,
            via=f"agent/{caller.agent.acting_id}" if caller is not None else None,
        )
        try:
            await self.store.record(event)
        except Exception as e:  # noqa: BLE001 - 감사가 막혀도 답은 같다
            logger.warning("agent call audit row %s not written: %s", route.action, e)


__all__ = ["AUDIT_RESOURCE", "AuditedRoute", "CallAudit", "Seen"]
