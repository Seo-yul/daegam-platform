"""OTLP 내보내기의 대기열 `otlp_outbox` 와 그 상태 `telemetry_state` (P37 15절).

대기열 문서 하나가 내보낼 실행(trace) 하나다. `_id` 는 `<instance_id>:<trace_id>` 라 같은 `turn_end`
가 두 길로 와도 한 번이고, 다른 Agent 가 같은 trace id 를 대어 이 실행의 자리를 차지하지 못한다. 칸
`trace_id` 가 그 trace id 다. `ready_at` 은 받은 때에서 10초 뒤다. 늦게 오는 span 을 기다린다.
실패하면 `attempts` 를 올리고 `ready_at` 을 물린다. 보낸 문서는 지우지 않고 `sent_at` 을 둔 채
TTL(하루)까지 남는다. 그래야 늦게 온 같은 `turn_end` 가 같은 실행을 다시 대기열에 넣지 못한다. 상태
문서는 마지막 성공 시각과 실패·버림의 누적이다. 설정 카드가 읽는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import ensure_ttl_index, insert_new

OTLP_OUTBOX = "otlp_outbox"
TELEMETRY_STATE = "telemetry_state"
OTLP_STATE_ID = "otlp"
# 아직 보내지 않은 문서의 표시. 부분 인덱스는 $exists:false 를 받지 않아 참인 칸을 둔다
PENDING = "pending"
# 대기열의 안전망. 내보내기는 한 시간 안에 보내거나 버리므로 하루 지난 문서는 남은 것이다 (P37 16절)
OUTBOX_RETENTION = timedelta(days=1)


class MongoOtlpStore:
    def __init__(self, db: AsyncDatabase):
        self.outbox = db[OTLP_OUTBOX]
        self.state = db[TELEMETRY_STATE]

    async def init_schema(self) -> None:
        await self.outbox.create_index(
            [("ready_at", ASCENDING)],
            name="ix_ready_pending",
            partialFilterExpression={PENDING: True},
        )
        await ensure_ttl_index(
            self.outbox, "received_at", int(OUTBOX_RETENTION.total_seconds()), "ttl_received"
        )

    async def enqueue(self, docs: list[dict[str, Any]]) -> int:
        """내보낼 실행을 넣는다. 이미 있는 trace(보낸 것 포함)는 그대로 둔다."""
        return await insert_new(self.outbox, [{**d, PENDING: True} for d in docs])

    async def ready(self, now: datetime, limit: int) -> list[dict[str, Any]]:
        """아직 보내지 않았고 기다림이 끝난 실행."""
        query = {PENDING: True, "ready_at": {"$lte": now}}
        cursor = self.outbox.find(query).sort("ready_at", ASCENDING)
        return [d async for d in cursor.limit(limit)]

    async def mark_sent(self, trace_ids: list[str], at: datetime) -> None:
        if trace_ids:
            await self.outbox.update_many(
                {"_id": {"$in": trace_ids}}, {"$set": {"sent_at": at}, "$unset": {PENDING: ""}}
            )

    async def remove(self, trace_ids: list[str]) -> None:
        if trace_ids:
            await self.outbox.delete_many({"_id": {"$in": trace_ids}})

    async def clear(self) -> int:
        """보내지 않은 실행을 버린다(내보내기를 껐다). 보낸 것은 TTL 까지 남는다."""
        result = await self.outbox.delete_many({PENDING: True})
        return result.deleted_count

    async def retry(self, trace_id: str, attempts: int, ready_at: datetime) -> None:
        await self.outbox.update_one(
            {"_id": trace_id}, {"$set": {"attempts": attempts, "ready_at": ready_at}}
        )

    async def record(
        self, *, success_at: datetime | None = None, failures: int = 0, dropped: int = 0
    ) -> None:
        """상태를 더한다. 성공이면 그 시각을 두고 연속 실패를 0 으로 한다."""
        update: dict[str, Any] = {"$inc": {"failures": failures, "dropped": dropped}}
        if success_at is not None:
            update["$set"] = {"last_success": success_at, "consecutive_failures": 0}
        elif failures:
            update["$inc"]["consecutive_failures"] = failures
        await self.state.update_one({"_id": OTLP_STATE_ID}, update, upsert=True)

    async def status(self) -> dict[str, Any]:
        doc = await self.state.find_one({"_id": OTLP_STATE_ID}) or {}
        return {
            "last_success": doc.get("last_success"),
            "failures": doc.get("failures", 0),
            "consecutive_failures": doc.get("consecutive_failures", 0),
            "dropped": doc.get("dropped", 0),
            "queued": await self.outbox.count_documents({PENDING: True}),
        }
