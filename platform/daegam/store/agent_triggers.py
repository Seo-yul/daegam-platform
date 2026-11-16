"""Agent 의 웹훅 트리거 (P41 4절).

agent_triggers {_id, tenant, agent_id, acts_as, revision, name, template, expires_at,
                expires_in_days, paused, paused_reason?, paused_cause?, secret_fp, previous_fp?,
                previous_until?, created_at, updated_at}

소스가 함께 쓰는 칸과 저장은 `store/run_sources.py` 에 있다. 서명 비밀의 값은 여기 두지 않는다.
Agent 마다 플랫폼이 관리하는 Secret 하나에 트리거 id 로 두고(`services/runs/trigger_secrets.py`),
여기는 지금 비밀과 앞 비밀의 지문(`secret_fp`, `previous_fp`)과 앞 비밀이 받아지는 끝
(`previous_until`, 바꾼 지 24시간)만 둔다. `template` 은 받은 본문으로 메시지를 만드는 틀이다.
"""

from __future__ import annotations

from daegam.store.base import UtcDatetime
from daegam.store.run_sources import MongoRunSourceStore, RunSource

AGENT_TRIGGERS = "agent_triggers"


class AgentTrigger(RunSource):
    name: str
    template: str
    secret_fp: str
    previous_fp: str | None = None
    previous_until: UtcDatetime | None = None


class MongoAgentTriggerStore(MongoRunSourceStore[AgentTrigger]):
    collection = AGENT_TRIGGERS
    model = AgentTrigger

    async def get_by_id(self, trigger_id: str) -> AgentTrigger | None:
        """웹훅 경로의 찾기. 호출은 트리거 id 만 대므로 테넌트를 모른다."""
        return self._of(await self.coll.find_one({"_id": trigger_id}))


__all__ = ["AGENT_TRIGGERS", "AgentTrigger", "MongoAgentTriggerStore"]
