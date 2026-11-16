"""시간 묶음과 다시 셀 시간의 표시 (P37 10절, P51 3절).

시간 묶음을 만드는 롤업 둘(사용량의 `usage_hourly`, MCP 메트릭의 `mcp_hourly`)이 함께 쓴다.
표시 하나는 `{_id: 시간, marked_at}` 이다. 다시 적으면 `marked_at` 이 새로 바뀌고, 롤업은 다시 센
뒤 `marked_at` 이 그대로인 표시만 지운다. 그래서 롤업 도중 다시 적힌 시간을 잃지 않는다. 묶음의
`_id` 는 (시간, 열쇠 값)의 해시이고, 시간 하나를 다시 센 결과는 그 시간의 묶음을 통째로 바꾼다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DeleteMany, ReplaceOne

from daegam.store.base import utcnow

HOUR = timedelta(hours=1)


def hour_of(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def day_of(tz: str) -> dict[str, Any]:
    """묶음의 시간(`hour`)을 tz 의 날로 자르는 식이다 (Mongo 7 의 `$dateTrunc`)."""
    return {"$dateTrunc": {"date": "$hour", "unit": "day", "timezone": tz}}


def bucket_id(hour: datetime, key: Iterable[Any]) -> str:
    """시간과 열쇠 값으로 만든 묶음 `_id`. sha256 의 앞 32자다."""
    raw = json.dumps([hour.isoformat(), *key], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def replace_hour(coll: Any, hour: datetime, buckets: list[dict[str, Any]]) -> None:
    """그 시간의 묶음을 buckets 로 바꾸고 사라진 열쇠의 묶음은 지운다. 같은 입력이면 몇 번을
    돌아도 결과가 같다."""
    keep = [b["_id"] for b in buckets]
    writes: list[Any] = [ReplaceOne({"_id": b["_id"]}, b, upsert=True) for b in buckets]
    writes.append(DeleteMany({"hour": hour, "_id": {"$nin": keep}}))
    await coll.bulk_write(writes, ordered=False)


class HourMarks:
    """컬렉션 하나에 둔 다시 셀 시간의 표시."""

    def __init__(self, coll: Any):
        self.coll = coll

    async def mark(self, ts: datetime) -> None:
        """ts 의 시간을 다시 셀 시간으로 적는다."""
        await self.coll.update_one(
            {"_id": hour_of(ts)}, {"$set": {"marked_at": utcnow()}}, upsert=True
        )

    async def batch(self, limit: int) -> list[dict[str, Any]]:
        """다시 셀 시간 몇 개. 오래된 것부터다."""
        cursor = self.coll.find().sort("_id", ASCENDING).limit(limit)
        return [d async for d in cursor]

    async def clear(self, hour: datetime, marked_at: datetime) -> None:
        """그 사이 다시 적히지 않은 표시만 지운다."""
        await self.coll.delete_one({"_id": hour, "marked_at": marked_at})
