"""Agent 런타임의 관측 이벤트 `agent_events` 와 지운 세션의 표 `agent_event_tombstones` (P37 5절,
D62).

이벤트는 런타임이 밀어 보낸 것(`source: push`), 수집기가 파드 로그에서 읽은 것(`log`), 플랫폼이 쓴
것 (`platform`, 답변 피드백)이다. 문서 모양은 `services/telemetry/normalize.py` 가 정한다. `_id` 는
이벤트의 `id` 라 두 길로 온 같은 이벤트는 한 번만 들어간다. 본문은 `body` 칸 하나에 있다.

보관은 `DAEGAM_AGENT_EVENTS_TTL_DAYS`(기본 30일)다. 스팬 때문에 턴마다 문서가 서너 배라 전사 보관과
같게 두고, 토큰과 금액은 400일 원장에 남는다. 지운 세션(D61)은 표를 남기고 그 세션의 본문을 지운다.
표는 이벤트 보관 동안 남아, 늦게 온 그 세션의 이벤트도 본문 없이 들어간다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    ensure_partial_index,
    ensure_ttl_index,
    insert_new,
    ts_range,
    utcnow,
)
from daegam.store.base import facets as store_facets

AGENT_EVENTS = "agent_events"
AGENT_EVENT_TOMBSTONES = "agent_event_tombstones"
# 본문이 드는 칸 하나다. 옛 문서는 같은 이름들을 `data` 아래에 두었다
BODY = "body"


class MongoAgentEventStore:
    def __init__(self, db: AsyncDatabase, *, ttl_days: int = 30):
        self.events = db[AGENT_EVENTS]
        self.tombstones = db[AGENT_EVENT_TOMBSTONES]
        self.ttl_days = ttl_days

    async def init_schema(self) -> None:
        ttl = int(timedelta(days=self.ttl_days).total_seconds())
        e = self.events
        await e.create_index(
            [("instance_id", ASCENDING), ("ts", DESCENDING)], name="ix_instance_ts"
        )
        await e.create_index([("tenant", ASCENDING), ("ts", DESCENDING)], name="ix_tenant_ts")
        # 지표의 창과 이벤트 거르기, 세션의 턴, 트레이스가 읽는다 (P37 5절)
        await e.create_index(
            [("instance_id", ASCENDING), ("event", ASCENDING), ("ts", DESCENDING)],
            name="ix_instance_event_ts",
        )
        await e.create_index(
            [("instance_id", ASCENDING), ("session_id", ASCENDING), ("ts", DESCENDING)],
            name="ix_instance_session_ts",
        )
        await ensure_partial_index(e, "trace_id", ("ts", ASCENDING), name="ix_trace_ts_set")
        await ensure_ttl_index(e, "ts", ttl, "ttl_ts")
        await ensure_ttl_index(self.tombstones, "ts", ttl, "ttl_ts")

    async def add(self, docs: list[dict[str, Any]]) -> int:
        """`_id` 가 이미 있으면 무시한다. 새로 들어간 수를 돌려준다."""
        return await insert_new(self.events, docs)

    async def query(
        self,
        *,
        tenant: str,
        instance_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        events: tuple[str, ...] = (),
        session_id: str | None = None,
        trace_id: str | None = None,
        hide_events: tuple[str, ...] = (),
        limit: int = 100,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """기본은 최신순이고 같은 시각은 런타임이 매긴 차례(`seq`)를 따른다. `events` 는 고를
        이벤트 종류이고, `hide_events` 는 이 호출자에게 보이지 않는 종류라 골라도 빠진다."""
        q: dict[str, Any] = {"tenant": tenant, "instance_id": instance_id}
        if ts := ts_range(since, until):
            q["ts"] = ts
        kind: dict[str, Any] = {}
        if events:
            kind["$in"] = list(events)
        if hide_events:
            kind["$nin"] = list(hide_events)
        if kind:
            q["event"] = kind
        if session_id:
            q["session_id"] = session_id
        if trace_id:
            q["trace_id"] = trace_id
        order = ASCENDING if oldest_first else DESCENDING
        cursor = self.events.find(q).sort([("ts", order), ("seq", order), ("_id", order)])
        cursor = cursor.limit(limit)
        out = []
        async for d in cursor:
            d["id"] = d.pop("_id")
            out.append(d)
        return out

    async def facets(
        self, match: dict[str, Any], facets: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[dict[str, Any]]]:
        """match 에 맞는 이벤트를 facet 마다 모은다. 지표가 한 번 읽기로 여러 수를 센다."""
        return await store_facets(self.events, match, facets)

    async def session_owner(self, tenant: str, instance_id: str, session_id: str) -> str | None:
        """세션의 `turn_start` 가 실은 소유자다. 이벤트가 없으면 None 이다."""
        doc = await self.events.find_one(
            {
                "tenant": tenant,
                "instance_id": instance_id,
                "session_id": session_id,
                "event": "turn_start",
            },
            {"user_sub": 1},
            sort=[("ts", DESCENDING)],
        )
        owner = (doc or {}).get("user_sub")
        return owner if isinstance(owner, str) else None

    async def trace_owner(self, tenant: str, instance_id: str, trace_id: str) -> str | None:
        """실행의 소유자. 그 실행의 `turn_start` 가 실은 `user_sub` 이고, 그것이 없으면(잃은 줄)
        실행이 든 세션의 소유자다. 이벤트가 없으면 None 이다."""
        where = {"tenant": tenant, "instance_id": instance_id, "trace_id": trace_id}
        start = await self.events.find_one({**where, "event": "turn_start"}, {"user_sub": 1})
        if isinstance(owner := (start or {}).get("user_sub"), str):
            return owner
        doc = await self.events.find_one(
            {**where, "session_id": {"$type": "string"}}, {"session_id": 1}
        )
        if doc is None:
            return None
        return await self.session_owner(tenant, instance_id, doc["session_id"])

    # -- 지운 세션 (D61) ---------------------------------------------------------------------

    async def tombstone(self, instance_id: str, session_id: str) -> None:
        """세션의 표를 남긴다. 다시 와도 처음 시각을 둔다."""
        await self.tombstones.update_one(
            {"_id": _tombstone_id(instance_id, session_id)},
            {
                "$setOnInsert": {
                    "instance_id": instance_id,
                    "session_id": session_id,
                    "ts": utcnow(),
                }
            },
            upsert=True,
        )

    async def tombstoned(self, instance_id: str, session_ids: Iterable[str]) -> set[str]:
        """표가 있는 세션 id."""
        ids = [_tombstone_id(instance_id, s) for s in set(session_ids)]
        if not ids:
            return set()
        cursor = self.tombstones.find({"_id": {"$in": ids}}, {"session_id": 1})
        return {d["session_id"] async for d in cursor}

    async def scrub(self, instance_id: str, session_id: str, data_keys: Iterable[str]) -> None:
        """세션의 본문을 지운다. 이 판 전의 문서는 본문 칸이 `data` 아래에 있어 그것도 지우고,
        첨부 이름(`data.attachments[].name`)은 첨부가 목록인 문서에서만 지운다."""
        q = {"instance_id": instance_id, "session_id": session_id}
        unset = {BODY: "", **{f"data.{k}": "" for k in data_keys}}
        await self.events.update_many(q, {"$unset": unset})
        await self.events.update_many(
            {**q, "data.attachments": {"$type": "array"}},
            {"$unset": {"data.attachments.$[].name": ""}},
        )


def _tombstone_id(instance_id: str, session_id: str) -> str:
    return f"{instance_id}:{session_id}"
