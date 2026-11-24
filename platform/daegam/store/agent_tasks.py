"""다른 Agent 에게 맡긴 작업 (P46 6.7절).

agent_task_threads {_id, tenant, agent_id, caller_agent_id, sub, created_at, expires_at}

작업 하나는 Agent Protocol 의 thread 하나이고 호출받는 Agent 의 세션 하나다. `threads.create` 가 이
기록을 만들며 넷에 묶는다. 테넌트, 호출받는 Agent(`agent_id`), 부르는 Agent(`caller_agent_id`, 미리
보기는 원본), 그 사람(`sub`)이다. 넷이 모두 맞는 요청만 작업을 읽고 잇고 취소한다(`get_bound`). 그
밖에는 없는 작업과 같다. 작업의 실행들은 실행 기록이 `caller.thread_id` 로 가리키고
(`store/agent_runs.py`), 세션은 그 실행들이 적는다. 실행을 시작할 때마다 남는 기간을 새로 세고, TTL
인덱스가 실행 기록과 같은 기간 뒤에 지운다. 지운 Agent 가 받은 작업도 그때까지 남고, 그 작업의
요청은 호출받는 Agent 가 없어 거절된다.
"""

from __future__ import annotations

from datetime import datetime

from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.agent_runs import RUN_RETENTION
from daegam.store.base import Document, UtcDatetime, ensure_ttl_index, new_id

AGENT_TASK_THREADS = "agent_task_threads"


class AgentTaskThread(Document):
    id: str
    tenant: str
    agent_id: str
    caller_agent_id: str
    sub: str
    created_at: UtcDatetime
    expires_at: UtcDatetime


class MongoAgentTaskThreadStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_TASK_THREADS]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def create(
        self, *, tenant: str, agent_id: str, caller_agent_id: str, sub: str, now: datetime
    ) -> AgentTaskThread:
        thread = AgentTaskThread(
            id=new_id(),
            tenant=tenant,
            agent_id=agent_id,
            caller_agent_id=caller_agent_id,
            sub=sub,
            created_at=now,
            expires_at=now + RUN_RETENTION,
        )
        await self.coll.insert_one(thread.to_doc())
        return thread

    async def get_bound(
        self, thread_id: str, *, tenant: str, agent_id: str, caller_agent_id: str, sub: str
    ) -> AgentTaskThread | None:
        """넷이 모두 맞는 작업이다. 하나라도 다르면 없는 작업과 같이 None 이다."""
        doc = await self.coll.find_one(
            {
                "_id": thread_id,
                "tenant": tenant,
                "agent_id": agent_id,
                "caller_agent_id": caller_agent_id,
                "sub": sub,
            }
        )
        return AgentTaskThread.from_doc(doc) if doc else None

    async def touch(self, thread_id: str, now: datetime) -> None:
        """실행을 시작한 작업이 남는 기간을 지금부터 다시 센다."""
        await self.coll.update_one(
            {"_id": thread_id}, {"$set": {"expires_at": now + RUN_RETENTION}}
        )


__all__ = ["AGENT_TASK_THREADS", "AgentTaskThread", "MongoAgentTaskThreadStore"]
