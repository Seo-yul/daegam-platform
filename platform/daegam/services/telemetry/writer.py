"""관측 이벤트를 넣는 한 곳 (P37 3절·4절·5절·7절, D62). 런타임이 밀어 보낸 것과 수집기가 읽은 것이
같은 길을 지난다.

- 지운 세션(`session_deleted`, D61)은 표를 남기고 그 세션의 본문을 지운다. 표가 있는 세션의 이벤트는
  늦게 와도 본문 없이 들어간다. 두 길이 겹치고 차례가 없기 때문이다
- 이벤트는 `<인스턴스>:<이벤트 id>` 를 `_id` 로 한 번만 들어간다. 다시 온 이벤트는 새로 들어가지
  않는다
- `model_call` 은 원장에 간다. 직접 경로의 끝난 호출은 런타임이 쓰는 행이고, 게이트웨이 경로의
  호출은 게이트웨이 기록을 기다리는 잇기 문서다(7절). 같은 호출의 행은 호출 id 로 한 번만 들어간다.
  원장에 넣지 못한 문서는 그것만 로그에 남는다. 미리 보기의 이벤트는 원본의 것이고 그 행의 부른
  쪽(`source.preview`)에 미리 보기의 id 가 남는다 (P44b 9절)
- 저장한 이벤트는 훅(답변 피드백의 세션 지우기와 다시 생성, D61)과 OTLP 내보내기의 대기열에도
  넘긴다. 다시 와도 결과가 같다
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from daegam.services.telemetry.normalize import (
    BODY_KEYS,
    MODEL_CALL,
    PREVIEW_ID_KEY,
    SESSION_DELETED,
)
from daegam.services.usage.ledger import UsageLedger
from daegam.store.agent_events import BODY, MongoAgentEventStore
from daegam.store.base import error_kind
from daegam.store.usage import (
    USAGE_FIELDS,
    TokenUsage,
    UsageJoin,
    UsageProvider,
    UsageRecord,
    UsageSource,
)

logger = logging.getLogger("daegam.telemetry")

# 저장한 Agent 이벤트를 받는 훅. 겹쳐 읽은 이벤트도 다시 오므로 같은 이벤트를 두 번 받아도 결과가
# 같아야 한다
AgentEventsHook = Callable[[list[dict[str, Any]]], Awaitable[None]]


class TelemetryWriter:
    def __init__(
        self,
        events: MongoAgentEventStore,
        ledger: UsageLedger,
        *,
        on_events: AgentEventsHook | None = None,
        otlp: AgentEventsHook | None = None,
    ):
        self.events = events
        self.ledger = ledger
        self.on_events = on_events
        # 표본에 든 실행을 OTLP 대기열에 넣는다 (P37 15절). 내보내기가 실패해도 넣기는 맞는다
        self.otlp = otlp

    async def write(self, docs: list[dict[str, Any]]) -> int:
        """문서들을 넣고 새로 들어간 수를 돌려준다. 문서는 테넌트와 인스턴스가 찍혀 있다(토큰이나
        파드 라벨이 정한 것). `_id` 는 `<인스턴스>:<이벤트 id>` 로 넣는다. 다른 Agent 가 같은 이벤트
        id 를 대어 이 Agent 의 이벤트를 먼저 차지하지 못한다. 받은 문서는 바꾸지 않는다."""
        docs = [{**doc, "_id": f"{doc['instance_id']}:{doc['_id']}"} for doc in docs]
        sessions = await self._apply_tombstones(docs)
        inserted = await self.events.add(docs)
        await self._scrub_late(docs, sessions)
        for doc in docs:
            await self._to_ledger(doc)
        if self.on_events is not None:
            await self.on_events(docs)
        if self.otlp is not None:
            try:
                await self.otlp(docs)
            except Exception as e:  # noqa: BLE001 - 내보내기 대기열의 실패가 이벤트 넣기를 막으면 안 된다
                logger.warning("telemetry: could not queue the OTLP export: %s", error_kind(e))
        return inserted

    async def _apply_tombstones(self, docs: list[dict[str, Any]]) -> dict[str, set[str]]:
        """지운 세션의 문서는 본문 없이 넣는다. 묶음의 (인스턴스, 세션들)을 돌려준다."""
        sessions: dict[str, set[str]] = defaultdict(set)
        for doc in docs:
            instance_id, session_id = doc.get("instance_id"), doc.get("session_id")
            if not (isinstance(instance_id, str) and isinstance(session_id, str)):
                continue
            if doc.get("event") == SESSION_DELETED:
                await self.events.tombstone(instance_id, session_id)
                await self.events.scrub(instance_id, session_id, BODY_KEYS)
            sessions[instance_id].add(session_id)
        for instance_id, session_ids in sessions.items():
            gone = await self.events.tombstoned(instance_id, session_ids)
            for doc in docs:
                if doc.get("instance_id") == instance_id and _text_in(doc.get("session_id"), gone):
                    doc.pop(BODY, None)
        return sessions

    async def _scrub_late(self, docs: list[dict[str, Any]], sessions: dict[str, set[str]]) -> None:
        """넣는 사이에 지워진 세션의 본문을 지운다. 표 읽기와 넣기 사이에 다른 레플리카가 표를
        남겼을 수 있다. 본문이 든 문서의 세션만 다시 본다."""
        with_body: dict[str, set[str]] = defaultdict(set)
        for d in docs:
            instance_id, session_id = d.get("instance_id"), d.get("session_id")
            if d.get(BODY) and _text_in(session_id, sessions.get(instance_id, set())):
                with_body[instance_id].add(session_id)
        for instance_id, session_ids in with_body.items():
            for session_id in await self.events.tombstoned(instance_id, session_ids):
                await self.events.scrub(instance_id, session_id, BODY_KEYS)

    async def _to_ledger(self, doc: dict[str, Any]) -> None:
        """문서 하나의 원장 몫. 틀린 칸이나 원장의 실패는 그 문서만이다. 로그에는 id 와 예외의
        종류만 남긴다(문서의 값이 로그로 새지 않게)."""
        try:
            entry = ledger_entry(doc)
            if isinstance(entry, UsageJoin):
                await self.ledger.join(entry)
            elif entry is not None:
                await self.ledger.write(entry)
        except Exception as e:  # noqa: BLE001 - 깨진 이벤트 하나가 묶음의 나머지를 막으면 안 된다
            logger.warning(
                "telemetry: model_call %s did not reach the ledger: %s",
                doc.get("_id"),
                error_kind(e),
            )


def ledger_entry(doc: dict[str, Any]) -> UsageRecord | UsageJoin | None:
    """`model_call` 하나의 원장 몫. 직접 경로의 끝난 호출은 행, 게이트웨이 경로는 잇기 문서이고 그
    밖은 None 이다. 호출 id 는 `<trace_id>-<span_id>` 다. 끊긴 호출은 토큰이 없어 행이 없다."""
    if doc.get("event") != MODEL_CALL:
        return None
    tenant, instance_id = doc.get("tenant"), doc.get("instance_id")
    trace_id, span_id = doc.get("trace_id"), doc.get("span_id")
    if not (tenant and instance_id and trace_id and span_id):
        return None
    data = _dict(doc.get("data"))
    instance = _dict(doc.get("instance"))
    where = {
        "id": f"{trace_id}-{span_id}",
        "tenant": tenant,
        "source": UsageSource(
            kind="agent", id=instance_id, name=instance.get("name"), preview=doc.get(PREVIEW_ID_KEY)
        ),
        "ref": doc.get("session_id"),
        "turn_id": doc.get("turn_id"),
        "user_sub": doc.get("user_sub"),
        "trace_id": trace_id,
    }
    status = data.get("status")
    if data.get("route") == "gateway":
        return UsageJoin(
            **where, ts=doc["ts"], cancelled=status == "cancelled", failed=status == "error"
        )
    if status != "ok":
        return None
    provider = _dict(data.get("provider"))
    usage = _dict(data.get("usage"))
    return UsageRecord(
        **where,
        ts=doc["ts"],
        writer="runtime",
        route="direct",
        provider=UsageProvider(kind=provider.get("kind") or "unknown", name=provider.get("name")),
        model=data.get("model") or data.get("response_model") or "unknown",
        response_model=data.get("response_model"),
        operation=data.get("operation") or "chat",
        usage=TokenUsage(**{k: int(usage.get(k) or 0) for k in USAGE_FIELDS}),
        estimated=bool(data.get("estimated")),
        latency_ms=doc.get("duration_ms"),
        record_id=doc["_id"],
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_in(value: Any, names: set[str]) -> bool:
    """글자이면서 names 의 하나인가. 파드 로그라 밖의 글이 섞여 세션 id 가 글자가 아닐 수 있다."""
    return isinstance(value, str) and value in names
