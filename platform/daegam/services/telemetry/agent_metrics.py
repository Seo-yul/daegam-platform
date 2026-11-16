"""Agent 지표 (P37 11절). 런타임의 지금 상태와 저장소가 센 창 안의 수를 합친다.

- 런타임 몫: 런타임 `/metrics` 의 `sessions`·`totals`·`daily_tokens`·`tools`·`warnings`·`runtime`·
  `chat` 이다. 공유 저장소의 값이라 어느 레플리카가 답해도 같다. 런타임에 연결할 수 없으면 이
  몫만 비고 `runtime_error` 가 그 까닭이다
- 저장소 몫: 창(1시간, 24시간) 안의 모든 레플리카의 이벤트와 원장이다. 턴의 결과, 입구의 거절,
  지연, 모델 호출과 Tool 실행, 사용량과 금액, 30일의 날마다 금액, 이벤트 수집의 상태다. 중지된
  턴은 지연과 오류율에 들지 않는다
- 응답은 (Agent, 창, 시간대)마다 15초 기억한다
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from daegam.errors import ServiceError
from daegam.services.agent_features import TELEMETRY
from daegam.services.telemetry.normalize import (
    MODEL_CALL,
    MODEL_FALLBACK,
    MODEL_RETRY,
    TOOL_EXEC,
    TURN_END,
    call_failed_expr,
    run_failed,
)
from daegam.store.agent_events import MongoAgentEventStore
from daegam.store.agents import Agent
from daegam.store.base import utcnow
from daegam.store.telemetry import PUSH_FRESHNESS, MongoAgentTelemetryStore
from daegam.store.usage import USAGE_FIELDS, MongoUsageStore, agent_buckets, agent_records
from daegam.ttl_cache import TTLCache

WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24)}
CACHE_SECONDS = 15.0
CACHE_ENTRIES = 1000
TOP_TOOLS = 20
COST_DAYS = 30
# 따로 세는 실패한 턴의 결과. 이 밖의 실패(normalize.run_failed)는 turn_failed 로 센다. pending 은
# 승인 대기로 끝난 턴이다
FAILED = ("turn_failed", "turn_timeout", "quota_exhausted", "budget_exceeded")
CANCELLED = "cancelled"
TURN_RESULTS = ("done", "pending", *FAILED, CANCELLED)
# 런타임 `/metrics` 에서 가져오는 칸. 옛 런타임의 파드 카운터는 버린다. 런타임의 `telemetry`(이벤트
# 보내기의 수)도 Service 뒤 파드 하나의 값이라 싣지 않는다. 모든 파드의 수는 `telemetry.pods` 가
# 수집 쪽(agent_telemetry 문서)에서 센다
RUNTIME_KEYS = ("sessions", "totals", "tools", "warnings", "runtime", "chat")
DAILY_TOKEN_KEYS = ("used", "limit", "window")

# turn_end 의 지연이다. 이 칸이 없는 turn_end 는 지연 표본에 들지 않는다
TURN_DURATION = "$duration_ms"

RuntimeMetrics = Callable[[Agent], Awaitable[tuple[int, Any]]]
# 채팅 모델 호출과 Tool 실행이다
MODEL_CHAT = {"$match": {"event": MODEL_CALL, "data.operation": {"$ne": "embeddings"}}}
TOOL_CALLS = {"$match": {"event": TOOL_EXEC}}


class AgentMetrics:
    def __init__(
        self,
        events: MongoAgentEventStore,
        usage: MongoUsageStore,
        pods: MongoAgentTelemetryStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.events = events
        self.usage = usage
        self.pods = pods
        self._cache: TTLCache[tuple[str, str, str], dict[str, Any]] = TTLCache(
            CACHE_SECONDS, clock=clock, max_entries=CACHE_ENTRIES
        )

    async def read(
        self, doc: Agent, *, window: str, tz: str, runtime: RuntimeMetrics
    ) -> dict[str, Any]:
        key = (doc.id, window, tz)
        if (hit := self._cache.get(key)) is not None:
            return hit
        now = utcnow()
        since = now - WINDOWS[window]
        events = await self._events_part(doc.id, since)
        logged = events.pop("_logged")
        out = {
            "window": window,
            **await _runtime_part(doc, runtime),
            **events,
            **await self._usage_part(doc, since, tz, now),
            "telemetry": {
                **await self._telemetry(doc.id, since, now, logged=logged),
                # 도는 이미지가 이벤트를 밀어 보낼 줄 아는가(18절). 모르면(기록 전) None 이다
                "image_pushes": None if doc.runtime is None else TELEMETRY in doc.runtime.features,
            },
        }
        self._cache.put(key, out)
        return out

    async def _events_part(self, agent_id: str, since: datetime) -> dict[str, Any]:
        """턴·거절·지연·모델·Tool 의 수. 한 번 읽기다."""
        match = {"instance_id": agent_id, "ts": {"$gte": since}}
        got = await self.events.facets(
            match,
            {
                "turns": [
                    {"$match": {"event": TURN_END}},
                    {"$group": {"_id": "$status", "n": {"$sum": 1}}},
                ],
                "latency": [
                    {"$match": {"event": TURN_END, "status": {"$ne": CANCELLED}}},
                    {"$project": {"ms": TURN_DURATION}},
                    {"$match": {"ms": {"$type": "number"}}},
                    _latency_group("$ms"),
                ],
                "rejected": [
                    {"$match": {"rejected_code": {"$type": "string"}}},
                    {"$group": {"_id": "$rejected_code", "n": {"$sum": 1}}},
                ],
                "model": [MODEL_CHAT, *_by("$data.model", "$duration_ms")],
                "model_p95": [MODEL_CHAT, _overall_p95("$duration_ms")],
                "model_points": [
                    {"$match": {"event": {"$in": [MODEL_RETRY, MODEL_FALLBACK]}}},
                    {"$group": {"_id": "$event", "n": {"$sum": 1}}},
                ],
                "tools": [TOOL_CALLS, *_by("$tool", "$duration_ms")],
                "tools_p95": [TOOL_CALLS, _overall_p95("$duration_ms")],
                "logged": [{"$match": {"source": "log"}}, {"$limit": 1}],
            },
        )
        turns = _counts(got["turns"])
        failed = sum(n for status, n in turns.items() if run_failed(status))
        results = {r: turns.pop(r, 0) for r in TURN_RESULTS}
        results["turn_failed"] += sum(n for status, n in turns.items() if run_failed(status))
        counted = sum(results.values()) - results[CANCELLED]
        rejected = _counts(got["rejected"])
        points = _counts(got["model_points"])
        return {
            "turns": results,
            "rejected": {"total": sum(rejected.values()), "by_code": rejected},
            "errors": {"turns": failed, "error_rate": round(failed / counted, 4) if counted else 0},
            "latency_ms": _latency(got["latency"]),
            "model": {
                **_totals(got["model"], got["model_p95"]),
                "retries": points.get(MODEL_RETRY, 0),
                "fallbacks": points.get(MODEL_FALLBACK, 0),
                "by_model": [_row("model", g) for g in got["model"]],
            },
            "tool_calls": {
                **_totals(got["tools"], got["tools_p95"]),
                "by_tool": [_row("tool", g) for g in got["tools"][:TOP_TOOLS]],
            },
            "_logged": bool(got["logged"]),
        }

    async def _usage_part(
        self, doc: Agent, since: datetime, tz: str, now: datetime
    ) -> dict[str, Any]:
        """창 안의 사용량과 금액은 원장 행에서, 30일의 날마다 금액은 시간 묶음에서 읽는다."""
        rows = {**agent_records(doc.tenant, doc.id), "ts": {"$gte": since}}
        (total,) = await self.usage.record_sums(rows) or [{}]
        days = await self.usage.bucket_groups(
            {
                **agent_buckets(doc.tenant, doc.id),
                "hour": {"$gte": now - timedelta(days=COST_DAYS)},
            },
            {},
            day_tz=tz,
        )
        return {
            "usage": {f: total.get(f, 0) for f in USAGE_FIELDS},
            "cost": {
                "usd": total.get("cost_usd", 0),
                "unpriced_calls": total.get("unpriced_calls", 0),
            },
            "cost_daily": [
                {
                    "day": g["_id"]["day"],
                    "cost_usd": g["cost_usd"],
                    "calls": g["calls"],
                    "input_tokens": g["input_tokens"],
                    "output_tokens": g["output_tokens"],
                }
                for g in sorted(days, key=lambda g: g["_id"]["day"])
            ],
        }

    async def _telemetry(
        self, agent_id: str, since: datetime, now: datetime, *, logged: bool
    ) -> dict[str, Any]:
        """이벤트가 어떻게 오는가. 최근 5분 안에 밀어 보낸 파드가 있으면 push, 창 안에 로그에서 읽은
        이벤트가 있으면 log 다. 둘 다 아니어도 창 안에 밀어 보낸 파드가 있으면(표에 그 수가 보인다)
        push 이고, 아무것도 없을 때만 none 이다. 파드는 창 안에 보낸 것만 보인다."""
        pods = await self.pods.pods(agent_id, since=since)
        rows = [_pod(p) for p in pods]
        dropped = {
            "cost": sum(r["dropped"]["cost"] for r in rows),
            "other": sum(r["dropped"]["other"] for r in rows),
        }
        if any(p["last_at"] >= now - PUSH_FRESHNESS for p in pods):
            mode = "push"
        elif logged:
            mode = "log"
        else:
            mode = "push" if pods else "none"
        return {"mode": mode, "pods": rows, "dropped": dropped}


async def _runtime_part(doc: Agent, runtime: RuntimeMetrics) -> dict[str, Any]:
    """런타임의 지금 상태. 연결할 수 없거나 오류면 비고 `runtime_error` 가 그 까닭이다."""
    empty: dict[str, Any] = {k: None for k in (*RUNTIME_KEYS, "daily_tokens")}
    try:
        status, body = await runtime(doc)
    except ServiceError as e:
        return {**empty, "runtime_error": {"code": e.code, "message": e.detail}}
    if status != 200 or not isinstance(body, dict):
        detail = body if isinstance(body, dict) else {}
        code = detail.get("code") if isinstance(detail.get("code"), str) else "runtime_error"
        message = detail.get("message") or f"the runtime answered {status}"
        return {**empty, "runtime_error": {"code": code, "message": str(message)}}
    daily = body.get("daily_tokens")
    return {
        **{k: body.get(k) for k in RUNTIME_KEYS},
        "daily_tokens": (
            {k: daily.get(k) for k in DAILY_TOKEN_KEYS} if isinstance(daily, dict) else None
        ),
        "runtime_error": None,
    }


def _latency_group(field: str) -> dict[str, Any]:
    return {
        "$group": {
            "_id": None,
            "avg": {"$avg": field},
            "max": {"$max": field},
            "pct": {"$percentile": {"input": field, "p": [0.5, 0.95], "method": "approximate"}},
        }
    }


def _by(key: str, duration: str) -> list[dict[str, Any]]:
    """key 마다 호출 수·오류 수·p95 를 세고 많은 순으로 둔다. 오류는 normalize 의 호출 규칙이다."""
    return [
        {
            "$group": {
                "_id": key,
                "calls": {"$sum": 1},
                "errors": {"$sum": {"$cond": [call_failed_expr(), 1, 0]}},
                "p95": _p95_of(duration),
            }
        },
        {"$sort": {"calls": -1, "_id": 1}},
    ]


def _overall_p95(duration: str) -> dict[str, Any]:
    """모든 호출의 p95 하나다. 묶음들의 p95 로는 전체의 p95 를 낼 수 없다."""
    return {"$group": {"_id": None, "p95": _p95_of(duration)}}


def _p95_of(duration: str) -> dict[str, Any]:
    return {"$percentile": {"input": duration, "p": [0.95], "method": "approximate"}}


def _counts(groups: list[dict[str, Any]]) -> dict[Any, int]:
    return {g["_id"]: g["n"] for g in groups}


def _latency(groups: list[dict[str, Any]]) -> dict[str, float]:
    if not groups:
        return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    g = groups[0]
    p50, p95 = g["pct"]
    return {
        "avg": round(g["avg"], 1),
        "p50": round(p50, 1),
        "p95": round(p95, 1),
        "max": round(g["max"], 1),
    }


def _totals(groups: list[dict[str, Any]], overall: list[dict[str, Any]]) -> dict[str, Any]:
    """by 묶음의 합과 모든 호출의 p95 다."""
    return {
        "calls": sum(g["calls"] for g in groups),
        "errors": sum(g["errors"] for g in groups),
        "p95_ms": _p95(overall[0]) if overall else None,
    }


def _row(name: str, g: dict[str, Any]) -> dict[str, Any]:
    return {name: g["_id"], "calls": g["calls"], "errors": g["errors"], "p95_ms": _p95(g)}


def _p95(g: dict[str, Any]) -> float | None:
    value = (g.get("p95") or [None])[0]
    return round(value, 1) if isinstance(value, int | float) else None


def _pod(doc: dict[str, Any]) -> dict[str, Any]:
    """파드 하나의 수집 수. 버린 수는 부트마다 가장 큰 값을 더한다."""
    boots = (doc.get("boots") or {}).values()
    return {
        "name": doc.get("pod_name"),
        "uid": doc.get("pod_uid"),
        "last_at": doc["last_at"],
        "received": doc.get("received", 0),
        "duplicates": doc.get("duplicates", 0),
        "refused": doc.get("refused", 0),
        "dropped": {
            "cost": sum(b.get("dropped_cost", 0) for b in boots),
            "other": sum(b.get("dropped_other", 0) for b in boots),
        },
    }
