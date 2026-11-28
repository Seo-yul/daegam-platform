"""세션의 턴과 실행 하나의 트레이스 (P37 12절). 이벤트 저장소와 원장에서 만든다.

- 턴: 턴 시도(`turn_id`)마다 그 실행들(`trace_id`)이다. 승인 뒤 재개는 같은 턴의 다음 실행이다.
  토큰은 실행들의 `turn_end.usage` 를 더하고 금액은 원장 행을 `turn_id` 로 더한다
- 트레이스: 실행 하나의 span(실행, 모델, Tool, 서브에이전트, 임베딩)과 점 이벤트다. 모델 span 은 그
  호출의 원장 행 금액을 받는다. 게이트웨이 경로의 행은 게이트웨이의 토큰과 우리 가격이다. 실행의
  `turn_start`·`turn_end` 는 따로 읽어 늘 들고, 나머지 이벤트는 3,000개, span 은 1,000개까지다.
  넘으면 `truncated_by` 가 넘은 상한이다. 본문은 기록됐을 때만 있다
- 금액: 세션 목록의 `cost_usd` 는 원장 행을 (Agent, 세션)으로 더한다. 세션·턴·트레이스의 호출이
  모두 가격 규칙이 없으면 0 이 아니라 null 이다
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from daegam.services.telemetry.normalize import (
    AGENT_EVENT_FIELDS,
    BODY_KEYS,
    PREVIEW_ID_KEY,
    TURN_END,
    TURN_START,
    call_failed,
    run_failed,
    span_kind,
)
from daegam.services.usage.costs import priced_sums
from daegam.store.agent_events import BODY, MongoAgentEventStore
from daegam.store.agent_runs import AgentRun
from daegam.store.agents import Agent
from daegam.store.usage import MongoUsageStore, UsageRecord, agent_records

MAX_SPANS = 1000
# 한 트레이스에서 읽는 turn_start·turn_end 밖의 이벤트. span 과 점 이벤트를 함께 세므로 span
# 상한보다 넉넉하다
MAX_TRACE_EVENTS = 3000
# 한 트레이스의 turn_start·turn_end. 실행마다 하나씩이고 남는 것은 겹쳐 온 사본이다
MAX_RUN_EVENTS = 10
# 턴 목록이 읽는 이벤트. 실행마다 turn_start·turn_end 둘이다
MAX_TURN_EVENTS = 2000
RUN_EVENTS = (TURN_START, TURN_END)
RUNNING = "running"
TRUNCATED_SPANS = "spans"
TRUNCATED_EVENTS = "events"
# span 과 점 이벤트의 attributes 에서 빼는 칸. 문서의 제 칸으로 이미 있다
_PLACED = frozenset(AGENT_EVENT_FIELDS) | frozenset(BODY_KEYS)


def usage_row(sums: Mapping[str, Any]) -> dict[str, Any]:
    """원장의 합 한 줄을 화면이 읽는 `usage` 와 `cost_usd` 로 바꾼다."""
    return {
        "usage": {"input_tokens": sums["input_tokens"], "output_tokens": sums["output_tokens"]},
        "cost_usd": sums["cost_usd"],
    }


class AgentTraces:
    def __init__(self, events: MongoAgentEventStore, usage: MongoUsageStore):
        self.events = events
        self.usage = usage

    async def owner(self, doc: Agent, session_id: str) -> str | None:
        return await self.events.session_owner(doc.tenant, doc.id, session_id)

    async def trace_owner(self, doc: Agent, trace_id: str) -> str | None:
        return await self.events.trace_owner(doc.tenant, doc.id, trace_id)

    async def turns(self, doc: Agent, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        """최신 턴 시도부터 limit 개. 실행은 차례대로다."""
        rows = await self.events.query(
            tenant=doc.tenant,
            instance_id=doc.id,
            session_id=session_id,
            events=RUN_EVENTS,
            limit=MAX_TURN_EVENTS,
        )
        by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in reversed(rows):
            if isinstance(row.get("turn_id"), str):
                by_turn[row["turn_id"]].append(row)
        newest = sorted(by_turn, key=lambda t: by_turn[t][-1]["ts"], reverse=True)[:limit]
        costs = await self._costs(doc, "turn_id", newest)
        return [_turn(turn_id, by_turn[turn_id], costs.get(turn_id)) for turn_id in newest]

    async def rows(self, doc: Agent, trace_id: str) -> tuple[list[dict[str, Any]], bool]:
        """실행 하나의 이벤트를 (ts, seq) 차례로. turn_start·turn_end 는 따로 읽어 늘 들고, 나머지는
        MAX_TRACE_EVENTS 까지다. 둘째 값은 나머지가 상한을 넘었는가다."""
        where = {"tenant": doc.tenant, "instance_id": doc.id, "trace_id": trace_id}
        runs = await self.events.query(
            **where, events=RUN_EVENTS, limit=MAX_RUN_EVENTS, oldest_first=True
        )
        rest = await self.events.query(
            **where, hide_events=RUN_EVENTS, limit=MAX_TRACE_EVENTS + 1, oldest_first=True
        )
        cut = len(rest) > MAX_TRACE_EVENTS
        return sorted(runs + rest[:MAX_TRACE_EVENTS], key=_event_order), cut

    async def trace(self, doc: Agent, trace_id: str) -> dict[str, Any] | None:
        """실행 하나. 이벤트가 없으면 None 이다(옛 런타임의 턴, 지난 보관)."""
        rows, cut = await self.rows(doc, trace_id)
        if not rows:
            return None
        ledger = {r.id: r for r in await self.usage.trace_records(doc.tenant, trace_id, MAX_SPANS)}
        start = next((r for r in rows if r["event"] == TURN_START), None)
        runs: list[dict[str, Any]] = []
        spans: list[dict[str, Any]] = []
        points: list[dict[str, Any]] = []
        for row in rows:
            if (kind := span_kind(row)) is not None and isinstance(row.get("span_id"), str):
                (runs if kind == "run" else spans).append(_span(row, kind, ledger, trace_id))
            elif row["event"] != TURN_START:
                points.append(_point(row))
        if start is not None and not runs:
            runs.append(_running(start))
        # run span 은 상한에 잘리지 않는다
        truncated_by = (
            TRUNCATED_SPANS
            if len(runs) + len(spans) > MAX_SPANS
            else TRUNCATED_EVENTS
            if cut
            else None
        )
        root = runs[0] if runs else None
        return {
            "trace_id": trace_id,
            "session_id": (start or rows[0]).get("session_id"),
            "turn_id": (start or rows[0]).get("turn_id"),
            "user_sub": (start or {}).get("user_sub"),
            "preview_id": (start or rows[0]).get(PREVIEW_ID_KEY),
            "root": root,
            "spans": runs + spans[: max(0, MAX_SPANS - len(runs))],
            "points": points,
            "totals": _trace_totals(root, ledger.values()),
            "truncated": truncated_by is not None,
            "truncated_by": truncated_by,
            "limit": MAX_TRACE_EVENTS if truncated_by == TRUNCATED_EVENTS else MAX_SPANS,
        }

    async def session_costs(self, doc: Agent, session_ids: list[str]) -> dict[str, float | None]:
        """세션마다 원장 금액의 합. 원장에 행이 없는 세션은 없다."""
        return {k: v["cost_usd"] for k, v in (await self.session_usage(doc, session_ids)).items()}

    async def session_usage(self, doc: Agent, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """세션마다 원장의 입력·출력 토큰(`usage`)과 금액(`cost_usd`)의 합. 백그라운드 실행 기록이
        자기 세션의 몫을 붙인다(P41 11절). 원장에 행이 없는 세션은 없다."""
        sums = await self._costs(doc, "ref", session_ids)
        return {k: usage_row(v) for k, v in sums.items()}

    async def call_usage(self, tenant: str, runs: list[AgentRun]) -> dict[str, dict[str, Any]]:
        """부른 실행(id)마다 그 실행의 턴이 쓴 원장 토큰(`usage`)과 금액(`cost_usd`)이다. 호출받은
        Agent 의 원장 행을 턴으로 더한다. 맡긴 작업은 한 세션에 실행이 여럿이라 세션이 아니라 턴으로
        가른다. 아직 턴이 없거나 원장에 행이 없는 실행은 없다."""
        by_agent: dict[str, list[AgentRun]] = defaultdict(list)
        for run in runs:
            if run.turn_id:
                by_agent[run.agent_id].append(run)
        out: dict[str, dict[str, Any]] = {}
        for agent_id, called in by_agent.items():
            sums = await priced_sums(
                self.usage, agent_records(tenant, agent_id), "turn_id", [r.turn_id for r in called]
            )
            for run in called:
                if (v := sums.get(run.turn_id)) is not None:
                    out[run.id] = usage_row(v)
        return out

    async def _costs(self, doc: Agent, key: str, values: list[str]) -> dict[str, dict[str, Any]]:
        """key 의 값마다 원장 행의 합. 호출이 모두 가격 규칙이 없으면 `cost_usd` 가 null 이다."""
        return await priced_sums(self.usage, agent_records(doc.tenant, doc.id), key, values)


def visible_event(row: dict[str, Any], *, bodies: bool) -> dict[str, Any]:
    """이벤트 목록의 한 건. 본문은 bodies 일 때만 둔다."""
    if bodies:
        return row
    return {k: v for k, v in row.items() if k != BODY}


def _turn(turn_id: str, rows: list[dict[str, Any]], cost: dict[str, Any] | None) -> dict[str, Any]:
    starts = [r for r in rows if r["event"] == TURN_START and isinstance(r.get("trace_id"), str)]
    ends = [r for r in rows if r["event"] == TURN_END]
    by_trace = {e["trace_id"]: e for e in ends if isinstance(e.get("trace_id"), str)}
    runs = [_run(s, by_trace.get(s["trace_id"])) for s in starts]
    # turn_start 를 잃은 실행은 turn_end 하나가 실행 하나다
    paired = {s["trace_id"] for s in starts}
    runs += [_run(e, e) for e in ends if e.get("trace_id") not in paired]
    runs.sort(key=lambda r: r["start"])
    usage: dict[str, int] = defaultdict(int)
    for end in ends:
        for k, v in ((end.get("data") or {}).get("usage") or {}).items():
            if isinstance(v, int):
                usage[k] += v
    first = next((r for r in rows if r["event"] == TURN_START), rows[0])
    return {
        "turn_id": turn_id,
        "turn": first.get("turn"),
        "regenerated": bool((first.get("data") or {}).get("regenerated")),
        "runs": runs,
        "usage": dict(usage),
        "cost_usd": None if cost is None else cost["cost_usd"],
    }


def _run(start: dict[str, Any], end: dict[str, Any] | None) -> dict[str, Any]:
    data = start.get("data") or {}
    return {
        "trace_id": start.get("trace_id"),
        "status": RUNNING if end is None else end.get("status"),
        "start": (end or {}).get("start") or start["ts"],
        "duration_ms": None if end is None else end.get("duration_ms"),
        "resumed": bool(data.get("resumed")),
    }


def _event_order(row: dict[str, Any]) -> tuple[Any, int, str]:
    """같은 시각의 이벤트는 런타임이 매긴 차례(`seq`)를 따른다."""
    seq = row.get("seq")
    return row["ts"], seq if isinstance(seq, int) else -1, str(row.get("id"))


def _span(
    row: dict[str, Any], kind: str, ledger: dict[str, UsageRecord], trace_id: str
) -> dict[str, Any]:
    data = row.get("data") or {}
    if kind == "run":
        name = f"turn {row.get('turn')}"
    elif kind in ("model", "embeddings"):
        name = data.get("model") or "model"
    else:
        name = row.get("tool") or "tool"
    span = _span_base(row, kind, name, row.get("status"))
    span["start"] = row.get("start") or row["ts"]
    span["duration_ms"] = row.get("duration_ms")
    if (call := ledger.get(f"{trace_id}-{row['span_id']}")) is not None:
        span["cost_usd"] = call.cost_usd
        span["ledger"] = {
            "writer": call.writer,
            "route": call.route,
            "usage": call.usage.model_dump(),
            "estimated": call.estimated,
        }
    if row.get(BODY):
        span[BODY] = row[BODY]
    return span


def _running(start: dict[str, Any]) -> dict[str, Any]:
    """turn_end 가 아직 없는 실행의 span. 도는 중이거나 파드가 마감하지 못하고 죽었다."""
    span = _span_base(start, "run", f"turn {start.get('turn')}", RUNNING)
    span["parent_span_id"] = None
    return span


def _span_base(row: dict[str, Any], kind: str, name: str, status: Any) -> dict[str, Any]:
    """span 의 공통 칸. 오류는 실행과 호출의 규칙(normalize)을 따르고 도는 중은 오류가 아니다."""
    failed = run_failed(status) if kind == "run" else call_failed(status)
    return {
        "span_id": row.get("span_id"),
        "parent_span_id": row.get("parent_span_id"),
        "kind": kind,
        "name": name,
        "start": row["ts"],
        "duration_ms": None,
        "status": status,
        "error": status != RUNNING and failed,
        "attributes": _attributes(row.get("data") or {}),
        "cost_usd": None,
    }


def _point(row: dict[str, Any]) -> dict[str, Any]:
    point = {
        "event": row["event"],
        "ts": row["ts"],
        "span_id": row.get("span_id") or row.get("parent_span_id"),
        "attributes": _attributes(row.get("data") or {}),
    }
    if row.get(BODY):
        point[BODY] = row[BODY]
    return point


def _attributes(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _PLACED}


def _trace_totals(root: dict[str, Any] | None, rows: Iterable[UsageRecord]) -> dict[str, Any]:
    """실행의 시간과 토큰(turn_end 의 합), 원장 금액의 합. 값이 없는 행(가격 없음)은 금액에
    들지 않는다."""
    calls = list(rows)
    priced = [r.cost_usd for r in calls if r.cost_usd is not None]
    return {
        "duration_ms": None if root is None else root["duration_ms"],
        "usage": {} if root is None else root["attributes"].get("usage") or {},
        "cost_usd": sum(priced) if priced else None,
        "calls": len(calls),
        "unpriced_calls": len(calls) - len(priced),
    }
