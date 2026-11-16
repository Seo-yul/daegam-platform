"""플랫폼이 목록을 볼 수 없는 자리의 관리 Secret 장부 (P86 6.1절, P55 5절).

{_id: 항목 id, written_at, swept_at?, cluster_id?, namespace?}

플랫폼은 Secret 을 이름으로만 읽고 쓰며 목록은 보지 않는다 (`charts/daegam/templates/rbac.yaml`,
`_rbac.tpl` 의 `daegam.rules.tenantObjects`). 그래서 리컨실의 정리가 항목 없이 남은 Secret 을
찾도록, 관리 Secret 에 쓰기 전에 그 항목을 장부에 적는다. `written_at` 은 마지막으로 쓰기를 시작한
시각이고, 정리는 그 뒤 한동안 손대지 않으며 행을 지울 때도 그 값이 그대로일 때만 지운다. `swept_at`
은 정리가 마지막으로 본 시각이다. 정리는 오래전에 본 행부터 한 번에 정한 수만 보고 남긴 행에 그
시각을 적으므로, 살아 있는 행이 많아도 그 뒤의 행에 차례가 온다. 테넌트 네임스페이스의 Secret 은
행이 그 클러스터와 네임스페이스를 든다. `store/hook_secrets.py` 와 같은 꼴이다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import ClassVar

from pydantic import Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime

logger = logging.getLogger("daegam.store")

# 정리가 막 쓰기 시작한 행을 건드리지 않는 동안과 한 틱에 보는 행의 수
WRITE_GRACE = timedelta(minutes=10)
SWEEP_LIMIT = 200


class SecretLedgerRow(Document):
    id: str
    written_at: UtcDatetime
    swept_at: UtcDatetime | None = None
    # 테넌트 네임스페이스의 Secret 이 사는 곳 (P55 5절). 플랫폼 네임스페이스의 것은 없다
    cluster_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    namespace: str | None = Field(default=None, exclude_if=lambda v: v is None)


class MongoSecretLedger:
    """장부 하나. 컬렉션 이름은 하위 클래스가 정한다."""

    collection: ClassVar[str]

    def __init__(self, db: AsyncDatabase):
        self.coll = db[self.collection]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("swept_at", ASCENDING), ("written_at", ASCENDING)], name="ix_swept_written"
        )

    async def note_write(
        self,
        item_id: str,
        now: datetime,
        *,
        cluster_id: str | None = None,
        namespace: str | None = None,
    ) -> None:
        """항목의 관리 Secret 에 쓰기 전에 부른다. 테넌트 네임스페이스의 Secret 이면 그 자리를 함께
        적는다."""
        where = {"cluster_id": cluster_id, "namespace": namespace} if namespace else {}
        await self.coll.update_one(
            {"_id": item_id},
            {"$set": {"written_at": now, **where}, "$setOnInsert": {"swept_at": None}},
            upsert=True,
        )

    async def due(
        self, swept_before: datetime, written_before: datetime, limit: int
    ) -> list[SecretLedgerRow]:
        """볼 때가 된 행. 마지막 쓰기가 written_before 보다 이른 것만, 아직 본 적이 없거나 오래전에
        본 것부터다. 쓰는 중인 행은 오지 않고, 살아 있는 행이 자리를 다 차지하지 못한다."""
        query = {
            "$or": [{"swept_at": None}, {"swept_at": {"$lt": swept_before}}],
            "written_at": {"$lte": written_before},
        }
        cursor = self.coll.find(query).sort("swept_at", ASCENDING).limit(limit)
        return [SecretLedgerRow.from_doc(d) async for d in cursor]

    async def mark_swept(self, item_ids: list[str], now: datetime) -> None:
        """정리가 보고 남긴 행에 그 시각을 적는다."""
        if item_ids:
            await self.coll.update_many({"_id": {"$in": item_ids}}, {"$set": {"swept_at": now}})

    async def forget(self, item_id: str, written_at: datetime | None = None) -> bool:
        """행을 지운다. written_at 을 주면 그사이 누가 쓰기를 시작했을 때(값이 다를 때) 두고
        거짓이다."""
        query: dict = {"_id": item_id}
        if written_at is not None:
            query["written_at"] = written_at
        result = await self.coll.delete_one(query)
        return result.deleted_count == 1


async def sweep_ledger(
    ledger: MongoSecretLedger,
    now: datetime,
    live_ids: Callable[[list[str]], Awaitable[set[str]]],
    delete_row: Callable[[SecretLedgerRow], Awaitable[bool]],
) -> list[str]:
    """장부에 있고 항목이 없는 행의 Secret 을 지운다(모듈 설명의 규칙). 한 틱에 오래전에 본 행부터
    `SWEEP_LIMIT` 개를 보고, 쓰기를 시작한 지 `WRITE_GRACE` 가 안 된 행은 건드리지 않는다.
    delete_row 는 그 행의 Secret 을 이름으로 지우고, 거짓이면 행을 남긴다. 행 하나의 실패는 그 행만
    남기고 id 와 예외의 이름을 적는다. 남긴 행에는 본 시각을 적어 다음 틱이 그 뒤의 행을 본다. 행은
    그사이 쓰기가 다시 시작되지 않았을 때만 지운다. 지운 행의 id 다."""
    rows = await ledger.due(now, now - WRITE_GRACE, SWEEP_LIMIT)
    if not rows:
        return []
    live = await live_ids([r.id for r in rows])
    dropped: list[str] = []
    kept: list[str] = []
    for row in rows:
        if row.id in live:
            kept.append(row.id)
            continue
        try:
            deleted = await delete_row(row)
        except Exception as e:  # noqa: BLE001 - 행 하나가 정리 전체를 멈추지 않는다
            logger.warning(
                "%s row %s kept by the sweep: %s", ledger.collection, row.id, type(e).__name__
            )
            deleted = False
        if not deleted:
            kept.append(row.id)
        elif await ledger.forget(row.id, row.written_at):
            dropped.append(row.id)
    await ledger.mark_swept(kept, now)
    return dropped


__all__ = [
    "SWEEP_LIMIT",
    "WRITE_GRACE",
    "MongoSecretLedger",
    "SecretLedgerRow",
    "sweep_ledger",
]
