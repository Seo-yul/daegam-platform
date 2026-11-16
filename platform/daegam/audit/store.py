"""감사 이벤트 저장소.

- `audit_events`: 플랫폼 API 쓰기 요청 한 건당 하나. 인덱스 (tenant, ts),
  TTL 은 Settings.audit_ttl_days
- `mcp_audit_events`: ToolHive 프록시·vMCP 의 audit 이벤트. `_id` = audit_id 라 중복은 그냥 무시된다
- `mcp_dirty_hours`: MCP 메트릭 롤업이 다시 셀 시간. 지난 시간의 호출 이벤트가 새로 들어오면 적는다
  (P51 3절, `audit/mcp_metrics.py`)
- `audit_cursors`: 파드별 pods/log sinceTime 커서
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_ttl_index,
    insert_new_docs,
    new_id,
    ts_range,
    utcnow,
)
from daegam.store.hour_marks import HourMarks, hour_of

logger = logging.getLogger("daegam.audit")

AUDIT_EVENTS = "audit_events"
MCP_AUDIT_EVENTS = "mcp_audit_events"
AUDIT_CURSORS = "audit_cursors"
MCP_DIRTY_HOURS = "mcp_dirty_hours"
# 호출로 세는 MCP 이벤트 유형 (P51 2절). 나머지(initialize·목록·ping·알림)는 프로토콜이다
MCP_TOOL_CALL = "mcp_tool_call"
MCP_RESOURCE_READ = "mcp_resource_read"
MCP_PROMPT_GET = "mcp_prompt_get"
MCP_CALL_TYPES: tuple[str, ...] = (MCP_TOOL_CALL, MCP_RESOURCE_READ, MCP_PROMPT_GET)


class AuditEvent(Document):
    """플랫폼 API 쓰기 요청 기록."""

    id: str = Field(default_factory=new_id)
    ts: UtcDatetime = Field(default_factory=utcnow)
    sub: str | None = None
    subject_type: str = "user"  # user | client. header 모드는 user("header")
    # local 모드 로그인 시도가 적은 사용자 이름 (D40). 실패한 시도는 주체가 없어 sub 가 비는데, 누가
    # 시도했는지는 남아야 한다. sub 에 이름을 섞지 않는다. 성공한 요청의 sub 는 사용자 id 다
    login_username: str | None = None
    # 어느 MCP 클라이언트가 불렀는가 (P33 18절). OAuth 토큰·회수 핸들러가 client_ref 를 둔다.
    # CIMD 면 문서 URL, DCR 이면 `dcr:` 와 해시다
    client_id: str | None = None
    tenant: str | None = None
    action: str  # 라우트 이름 또는 "METHOD path"
    resource: str | None = None  # 라우터 prefix (backend-apis, clusters, ...)
    resource_id: str | None = None
    path_params: dict[str, Any] = Field(default_factory=dict)
    method: str
    path: str
    request_id: str
    status: int
    result: str  # ok | error
    error_code: str | None = None
    duration_ms: int | None = None
    # 누가 대신 불렀는가. 비서는 "assistant/<session_id>" 다. ASGI scope state 로만 채워지므로 외부
    # 요청은 위조하지 못한다 (D31). 사람이 직접 한 요청은 None 이다
    via: str | None = None
    # 한 행이 센 같은 일의 수. 몰려오는 거절을 한 행으로 세는 자리(웹훅, P41 4절)만 쓴다
    count: int | None = None


async def record_system_event(
    audit: Any,
    *,
    sub: str,
    tenant: str | None,
    action: str,
    resource: str,
    path: str,
    resource_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """요청 없이 플랫폼이 스스로 한 일의 감사 행 하나. 주체는 시스템이고 method 는 SYSTEM 이다
    (P32 12절의 회수, P50 8절의 overrides 옮기기). 테넌트가 없는 일(Teams 연결 끊기, P47)은
    None 이다."""
    await audit.record(
        AuditEvent(
            sub=sub,
            subject_type="system",
            tenant=tenant,
            action=action,
            resource=resource,
            resource_id=resource_id,
            method="SYSTEM",
            path=path,
            request_id=new_id(),
            status=200,
            result="ok",
            error_code=error_code,
        )
    )


class MongoAuditStore:
    def __init__(self, db: AsyncDatabase, *, ttl_days: int = 90):
        self.db = db
        self.events = db[AUDIT_EVENTS]
        self.mcp_events = db[MCP_AUDIT_EVENTS]
        self.cursors = db[AUDIT_CURSORS]
        self.mcp_marks = HourMarks(db[MCP_DIRTY_HOURS])
        self.ttl_days = ttl_days

    async def init_schema(self) -> None:
        ttl = int(timedelta(days=self.ttl_days).total_seconds())
        await self.events.create_index(
            [("tenant", ASCENDING), ("ts", DESCENDING)], name="ix_tenant_ts"
        )
        await self.events.create_index([("sub", ASCENDING), ("ts", DESCENDING)], name="ix_sub_ts")
        await self.events.create_index([("via", ASCENDING), ("ts", DESCENDING)], name="ix_via_ts")
        await self.events.create_index(
            [("client_id", ASCENDING), ("ts", DESCENDING)], name="ix_client_ts"
        )
        await ensure_ttl_index(self.events, "ts", ttl, "ttl_ts")
        await self.mcp_events.create_index(
            [("tenant", ASCENDING), ("ts", DESCENDING)], name="ix_tenant_ts"
        )
        await self.mcp_events.create_index(
            [("tool", ASCENDING), ("ts", DESCENDING)], name="ix_tool_ts"
        )
        # MCP 메트릭 롤업이 한 시간의 호출 이벤트만 읽고 세는 데 쓴다 (P51 3절)
        await self.mcp_events.create_index(
            [("type", ASCENDING), ("ts", ASCENDING)], name="ix_type_ts"
        )
        await ensure_ttl_index(self.mcp_events, "ts", ttl, "ttl_ts")

    # -- 플랫폼 API ------------------------------------------------------------------

    async def record(self, event: AuditEvent) -> None:
        await self.events.insert_one(event.to_doc())

    async def count(self, event: AuditEvent) -> None:
        """event 의 id 로 한 행을 두고 셀 때마다 `count` 를 하나 늘린다. 처음이면 event 로
        만든다."""
        doc = event.to_doc()
        # 셀 칸과 거르는 키는 $setOnInsert 에 두지 않는다
        doc.pop("count", None)
        doc.pop("_id", None)
        await self.events.update_one(
            {"_id": event.id}, {"$setOnInsert": doc, "$inc": {"count": 1}}, upsert=True
        )

    async def query(
        self,
        *,
        tenant: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        action: str | None = None,
        sub: str | None = None,
        resource: str | None = None,
        via: str | None = None,
        client_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """via 는 정확 일치다. 값이 "/" 로 끝나면 접두 일치라서 "assistant/" 는 비서가 한 모든
        쓰기다."""
        q: dict[str, Any] = {}
        if tenant is not None:
            q["tenant"] = tenant
        if ts := ts_range(since, until):
            q["ts"] = ts
        if action:
            q["action"] = action
        if sub:
            q["sub"] = sub
        if resource:
            q["resource"] = resource
        if via:
            q["via"] = {"$regex": "^" + re.escape(via)} if via.endswith("/") else via
        if client_id:
            q["client_id"] = client_id
        cursor = self.events.find(q).sort([("ts", DESCENDING), ("_id", DESCENDING)]).limit(limit)
        return [AuditEvent.from_doc(d) async for d in cursor]

    # -- MCP 계층 ------------------------------------------------------------------

    async def add_mcp_events(self, docs: list[dict[str, Any]], *, mark_all: bool = False) -> int:
        """audit_id(_id) 가 이미 있으면 무시한다. 새로 들어간 수를 돌려준다.

        보관 기간보다 오래된 이벤트는 넣지 않는다. 쉬는 파드의 마지막 줄을 TTL 이 지운
        뒤 다시 읽어도 다시 들어가지 않는다. 지난 시간의 호출 이벤트가 새로 들어가면 그
        시간을 MCP 메트릭 롤업이 다시 셀 시간으로 적는다 (P51 3절). 지난 시간은 넣은
        뒤의 지금 시각으로 가린다. 겹쳐 다시 읽은 이벤트는 중복이라 적지 않는다.
        mark_all 은 묶음이 실패한 뒤 하나씩 넣는 자리다. 묶음이 이미 넣은 문서는 거기서
        중복으로 보이므로 다룬 문서를 모두 적는다."""
        horizon = utcnow() - timedelta(days=self.ttl_days)
        fresh = [d for d in docs if d["ts"] >= horizon]
        inserted = await insert_new_docs(self.mcp_events, fresh)
        current = hour_of(utcnow())
        late = {
            hour_of(d["ts"])
            for d in (fresh if mark_all else inserted)
            if d["ts"] < current and d.get("type") in MCP_CALL_TYPES and d.get("instance_id")
        }
        for hour in sorted(late):
            await self.mcp_marks.mark(hour)
        return len(inserted)

    async def query_mcp(
        self,
        *,
        tenant: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        tool: str | None = None,
        sub: str | None = None,
        type_: str | None = None,
        component: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        q: dict[str, Any] = {}
        if tenant is not None:
            q["tenant"] = tenant
        if ts := ts_range(since, until):
            q["ts"] = ts
        if tool:
            q["tool"] = tool
        if sub:
            q["sub"] = sub
        if type_:
            q["type"] = type_
        if component:
            q["component"] = component
        if outcome:
            q["outcome"] = outcome
        cursor = (
            self.mcp_events.find(q).sort([("ts", DESCENDING), ("_id", DESCENDING)]).limit(limit)
        )
        out = []
        async for d in cursor:
            d["audit_id"] = d.pop("_id")
            out.append(d)
        return out

    # -- 커서 ----------------------------------------------------------------------

    async def get_cursor(self, key: str) -> dict[str, Any] | None:
        return await self.cursors.find_one({"_id": key})

    async def set_cursor(self, key: str, *, since: datetime | None, pod_uid: str | None) -> None:
        await self.cursors.update_one(
            {"_id": key},
            {"$set": {"since": since, "pod_uid": pod_uid, "updated_at": utcnow()}},
            upsert=True,
        )

    async def delete_cursors_except(self, prefix: str, keep: set[str]) -> int:
        """사라진 파드의 커서를 지운다. prefix 는 "{cluster_id}/{namespace}/"."""
        res = await self.cursors.delete_many({"_id": {"$regex": "^" + prefix, "$nin": list(keep)}})
        return res.deleted_count
