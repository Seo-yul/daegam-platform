"""미리 보기의 테넌트 자리, 하루 시작 수, 끝난 까닭 (P44b 7절·10절, D107).

미리 보기 자체는 `agents` 컬렉션의 문서다(`store/agents.py` `AgentPreview`). 여기는 그 둘레의 세
컬렉션이다.

- **자리.** 테넌트마다 `DAEGAM_AGENT_PREVIEWS_PER_TENANT` 개의 문서 `<테넌트>:<n>` 이다. 시작은 빈
  자리 하나를 넣기로 잡고 끝은 그 자리를 지운다. 넣기가 고유 `_id` 에 걸리면 남이 쥔 자리라 다음
  자리를 본다. 그래서 플랫폼 레플리카가 함께 시작해도 상한을 넘지 못한다. 시작이 실패했거나 플랫폼이
  끝 전에 멈춰 남은 자리는 끝내기 워커가 놓는다.
- **시작 수.** 사람과 테넌트마다 UTC 날 칸의 수다(`count_in_window`). 시작과 옮기기가 하나씩 센다.
- **끝.** (테넌트, 원본, 사람)마다 마지막으로 끝난 까닭과 때다. 미리 보기 문서는 끝과 함께
  지워지므로 서랍이 왜 끝났는지 여기서 읽는다. 하루 뒤 TTL 이 걷는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime, count_in_window, ensure_ttl_index

AGENT_PREVIEW_SLOTS = "agent_preview_slots"
AGENT_PREVIEW_STARTS = "agent_preview_starts"
AGENT_PREVIEW_ENDS = "agent_preview_ends"
# 시작 수의 칸과 그 칸을 남기는 시간, 끝의 기록을 남기는 시간
START_WINDOW = timedelta(days=1)
STARTS_KEEP = timedelta(days=1)
ENDS_KEEP = timedelta(days=1)

# 미리 보기가 끝난 까닭 (P44b 10절). idle 은 마지막 활동 30분 뒤, max_age 는 처음 시작 2시간 뒤,
# ended 는 시작한 사람이, ended_by_other 는 `agent:deploy` 를 가진 다른 사람이 끝낸 것이다.
# access_lost 는 시작한 사람이 그 테넌트의 `agent:deploy` 나 계정을 잃은 것, source_deleted 는
# 원본을 지운 것, disabled 는 `DAEGAM_AGENT_PREVIEWS_ENABLED` 를 끈 것이다
PreviewEndReason = Literal[
    "idle", "max_age", "ended", "ended_by_other", "access_lost", "source_deleted", "disabled"
]


class PreviewSlot(Document):
    """테넌트의 자리 하나. id 는 `<테넌트>:<n>` 이다."""

    id: str
    tenant: str
    n: int
    preview_id: str
    taken_at: UtcDatetime


class PreviewEnd(Document):
    """끝난 미리 보기 하나의 기록. id 는 `<테넌트>:<원본>:<사람>` 이라 다음 끝이 덮는다."""

    id: str
    tenant: str
    source: str
    owner: str
    preview_id: str
    reason: PreviewEndReason
    by: str | None = None
    at: UtcDatetime
    expires_at: UtcDatetime


def end_id(tenant: str, source: str, owner: str) -> str:
    return f"{tenant}:{source}:{owner}"


class MongoAgentPreviewStore:
    def __init__(self, db: AsyncDatabase):
        self.slots = db[AGENT_PREVIEW_SLOTS]
        self.starts = db[AGENT_PREVIEW_STARTS]
        self.ends = db[AGENT_PREVIEW_ENDS]

    async def init_schema(self) -> None:
        await self.slots.create_index([("preview_id", ASCENDING)], name="ix_preview")
        await ensure_ttl_index(self.starts, "expires_at", 0, "ttl_expires_at")
        await ensure_ttl_index(self.ends, "expires_at", 0, "ttl_expires_at")

    # -- 자리 ---------------------------------------------------------------------------------

    async def take_slot(
        self, tenant: str, preview_id: str, count: int, now: datetime
    ) -> int | None:
        """그 테넌트의 빈 자리 하나를 preview_id 로 잡는다. 잡은 자리 번호이고 자리가 없으면
        None 이다."""
        for n in range(count):
            slot = PreviewSlot(
                id=f"{tenant}:{n}", tenant=tenant, n=n, preview_id=preview_id, taken_at=now
            )
            try:
                await self.slots.insert_one(slot.to_doc())
            except DuplicateKeyError:
                continue
            return n
        return None

    async def release_slot(self, preview_id: str) -> int:
        """그 미리 보기가 쥔 자리를 놓는다. 놓은 수다."""
        res = await self.slots.delete_many({"preview_id": preview_id})
        return res.deleted_count

    async def all_slots(self) -> list[PreviewSlot]:
        cursor = self.slots.find({}).sort("_id", ASCENDING)
        return [PreviewSlot.from_doc(d) for d in await cursor.to_list()]

    # -- 시작 수 -------------------------------------------------------------------------------

    async def count_start(self, tenant: str, sub: str, now: datetime) -> tuple[int, datetime]:
        """그 사람의 그 테넌트 오늘(UTC) 칸에 하나를 더한다. (더한 뒤의 수, 칸이 끝나는 때)."""
        return await count_in_window(self.starts, f"{tenant}:{sub}", now, START_WINDOW, STARTS_KEEP)

    # -- 끝 ---------------------------------------------------------------------------------

    async def record_end(
        self,
        *,
        tenant: str,
        source: str,
        owner: str,
        preview_id: str,
        reason: PreviewEndReason,
        by: str | None,
        now: datetime,
    ) -> None:
        end = PreviewEnd(
            id=end_id(tenant, source, owner),
            tenant=tenant,
            source=source,
            owner=owner,
            preview_id=preview_id,
            reason=reason,
            by=by,
            at=now,
            expires_at=now + ENDS_KEEP,
        )
        await self.ends.replace_one({"_id": end.id}, end.to_doc(), upsert=True)

    async def end_of(self, tenant: str, source: str, owner: str) -> PreviewEnd | None:
        doc = await self.ends.find_one({"_id": end_id(tenant, source, owner)})
        return PreviewEnd.from_doc(doc) if doc else None

    async def forget_end(self, tenant: str, source: str, owner: str) -> None:
        """새 미리 보기를 시작하면 앞의 끝을 지운다."""
        await self.ends.delete_one({"_id": end_id(tenant, source, owner)})
