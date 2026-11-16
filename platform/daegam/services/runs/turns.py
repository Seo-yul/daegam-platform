"""도는 실행의 턴을 런타임에서 멈춘다 (P41 7절).

중지, 소스의 멈춤과 지움, 묶인 사람이 접근을 잃은 것은 `running` 실행에 중지 표시만 남긴다. 그 턴은
런타임이 멈춰야 끝나므로 세션과 턴 id 로 런타임의 중지(`POST /sessions/{sid}/cancel`)를 부른다.
승인자 헤더로 부르므로 묶인 사람이 아니어도 멈춘다. 턴이 이미 끝났으면 런타임이 409
`no_turn_in_progress` 로 답하고 그것도 멈춘 것이다. 런타임에 닿지 못하면 로그만 남긴다. 중지 표시가
남아 있으므로 그 턴의 `turn_end` 가 실행을 끝내고, 이벤트가 없으면 지켜보기 워커가 끝낸다.
"""

from __future__ import annotations

import logging

from daegam.services.agent_runtime import AgentRuntimeProxy, serves_turns
from daegam.store.agent_runs import AgentRun
from daegam.store.agents import MongoAgentStore

logger = logging.getLogger("daegam.runs.turns")

# 런타임이 멈춘 턴과 이미 끝난 턴에 답하는 상태
_STOPPED_STATUSES = frozenset({202, 409})


class RunTurns:
    def __init__(self, agents: MongoAgentStore, proxy: AgentRuntimeProxy):
        self.agents = agents
        self.proxy = proxy

    async def stop(self, run: AgentRun) -> None:
        """run 의 턴을 멈춘다. 세션이나 턴 id 가 없거나 Agent 가 턴을 받지 않으면 할 것이 없다."""
        if run.session_id is None or run.turn_id is None:
            return
        agent = await self.agents.get(run.agent_id, tenant=run.tenant)
        if agent is None or not serves_turns(agent):
            return
        try:
            status, _ = await self.proxy.cancel(
                agent, run.session_id, user_sub=run.acts_as.sub, approver=True, turn_id=run.turn_id
            )
        except Exception as e:  # noqa: BLE001 - 중지 표시와 지켜보기 워커가 끝낸다
            logger.warning("run %s: its turn was not stopped: %s", run.id, e)
            return
        if status not in _STOPPED_STATUSES:
            logger.warning("run %s: the runtime answered %s to the stop", run.id, status)


__all__ = ["RunTurns"]
