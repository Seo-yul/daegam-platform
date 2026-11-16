"""OTLP/HTTP JSON 의 모양 (P37 8절·15절). 게이트웨이의 사용량 수집이 읽고 OTLP 내보내기가 쓴다.
뒤쪽은 우리 이벤트를 OTLP span 으로 옮기는 표다(`resource_spans`).

OTLP JSON 은 protobuf 의 JSON 대응이다. 속성은 `[{key, value: AnyValue}]` 이고 AnyValue 는
`stringValue`·`intValue`(64비트라 글자로 온다)·`doubleValue`·`boolValue`·`arrayValue{values}`·
`kvlistValue{values}` 가운데 하나다. 시각은 1970년부터의 나노초를 글자로 싣는다. id 는 16진 글자다.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from daegam.services.telemetry.normalize import (
    BUDGET,
    DECISION,
    GUARDRAIL,
    INTERRUPT,
    MODEL_FALLBACK,
    MODEL_RETRY,
    RATE_LIMITED,
    REJECTED,
    TURN_END,
    TURN_START,
    call_failed,
    run_failed,
    span_kind,
)
from daegam.store.agent_events import BODY

NANOS = 1_000_000_000


def any_value(value: Any) -> Any:
    """AnyValue 하나를 파이썬 값으로. 모르는 모양은 None 이다."""
    if not isinstance(value, Mapping):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [any_value(v) for v in (value["arrayValue"] or {}).get("values") or []]
    if "kvlistValue" in value:
        return attributes((value["kvlistValue"] or {}).get("values"))
    return None


def attributes(items: Any) -> dict[str, Any]:
    """`[{key, value}]` 를 dict 로. 모양이 틀린 항목은 뺀다."""
    out: dict[str, Any] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("key"), str):
            out[item["key"]] = any_value(item.get("value"))
    return out


def encode_value(value: Any) -> dict[str, Any] | None:
    """파이썬 값 하나를 AnyValue 로. bool 은 int 보다 먼저 본다. NaN·Infinity 는 표준 JSON 이 아니라
    싣지 않는다(None). 목록과 사전 안에서도 빠진다."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value} if math.isfinite(value) else None
    if isinstance(value, list | tuple):
        values = [e for e in (encode_value(v) for v in value) if e is not None]
        return {"arrayValue": {"values": values}}
    if isinstance(value, Mapping):
        return {"kvlistValue": {"values": encode_attributes(value)}}
    return {"stringValue": str(value)}


def encode_attributes(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """dict 를 `[{key, value}]` 로. None 이거나 실을 수 없는 값은 싣지 않는다."""
    out = []
    for k, v in values.items():
        if v is not None and (encoded := encode_value(v)) is not None:
            out.append({"key": k, "value": encoded})
    return out


def nanos(value: Any) -> datetime | None:
    """나노초 글자를 naive UTC 시각으로. 틀리면 None 이다."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n / NANOS, UTC).replace(tzinfo=None)


def to_nanos(ts: datetime) -> str:
    """naive UTC 시각을 나노초 글자로."""
    return str(int(ts.replace(tzinfo=UTC).timestamp() * NANOS))


def spans(body: Any) -> Iterator[dict[str, Any]]:
    """`ExportTraceServiceRequest` 의 span 들. 각 span 에 `_attrs`(span 속성)를 붙여 준다."""
    for resource in _list(body, "resourceSpans"):
        for scope in _list(resource, "scopeSpans"):
            for span in _list(scope, "spans"):
                yield {**span, "_attrs": attributes(span.get("attributes"))}


def log_records(body: Any) -> Iterator[dict[str, Any]]:
    """`ExportLogsServiceRequest` 의 log record 들. 각 record 에 `_attrs` 를 붙여 준다."""
    for resource in _list(body, "resourceLogs"):
        for scope in _list(resource, "scopeLogs"):
            for record in _list(scope, "logRecords"):
                yield {**record, "_attrs": attributes(record.get("attributes"))}


def _list(value: Any, key: str) -> list[dict[str, Any]]:
    items = value.get(key) if isinstance(value, Mapping) else None
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


# -- 우리 이벤트를 OTLP span 으로 (15절) ---------------------------------------------------------
# GenAI 관례는 아직 Development 상태라 그 이름은 이 표에만 둔다

# span 길이의 상한(하루, ms)
MAX_SPAN_MS = 86_400_000.0
SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_OK = 1
STATUS_ERROR = 2
# Tool 소스 → gen_ai.tool.type
TOOL_TYPES = {
    "mcp_instance": "extension",
    "gateway_instance": "extension",
    "knowledge": "datastore",
    "builtin": "function",
}
STUB_PROVIDER = "daegam.stub"
# 점 이벤트마다 내보내는 칸. 표에 없는 칸(user_sub·decided_by 같은 사람, 본문)은 내보내지 않는다
POINT_FIELDS: dict[str, tuple[str, ...]] = {
    MODEL_RETRY: ("attempt", "model", "error_type", "delay_seconds"),
    MODEL_FALLBACK: ("from", "to", "agent"),
    GUARDRAIL: ("position", "rule", "action", "pii_type", "stage", "tool", "agent", "partial"),
    INTERRUPT: ("tools",),
    DECISION: ("decisions", "tools", "approver"),
    BUDGET: ("scope", "action", "used", "limit"),
    REJECTED: ("code",),
    RATE_LIMITED: ("limit", "count", "window"),
}
TRACE_ID = re.compile(r"[0-9a-f]{32}")
SPAN_ID = re.compile(r"[0-9a-f]{16}")


def is_trace_id(value: Any) -> bool:
    return isinstance(value, str) and TRACE_ID.fullmatch(value) is not None


def is_span_id(value: Any) -> bool:
    return isinstance(value, str) and SPAN_ID.fullmatch(value) is not None


def resource_spans(
    rows: list[dict[str, Any]],
    *,
    agent: Mapping[str, Any],
    costs: Mapping[str, float | None],
    capture: bool,
    links: list[tuple[str, str]],
) -> dict[str, Any] | None:
    """실행 하나의 이벤트(시각 순)를 `resourceSpans` 하나로. `turn_end` 나 그 run span 이 없으면
    None 이다. 문서의 칸은 모양을 믿지 않고 하나씩 확인한다. id 가 16진 글자가 아닌 span 은 뺀다.
    costs 는 호출 id(`<trace>-<span>`)마다의 원장 금액이다. capture 면 기록된 본문을 싣는다.
    links 는 같은 턴의 앞 실행 root 들이다."""
    end = next((r for r in rows if r.get("event") == TURN_END), None)
    if end is None or not is_trace_id(end.get("trace_id")):
        return None
    trace_id = end["trace_id"]
    start = next((r for r in rows if r.get("event") == TURN_START), None)
    spans: dict[str, dict[str, Any]] = {}
    for row in rows:
        span = _span(row, agent=agent, costs=costs, capture=capture, trace_id=trace_id)
        if span is not None:
            spans[span["spanId"]] = span
    root = spans.get(str(end.get("span_id")))
    if root is None:
        return None
    if capture and start is not None and (message := _body(start, "message")) is not None:
        root["attributes"] += encode_attributes({"gen_ai.input.messages": message})
    priced = [c for c in costs.values() if isinstance(c, int | float)]
    root["attributes"] += encode_attributes({"daegam.cost.usd": sum(priced) if priced else None})
    root["links"] = [
        {"traceId": t, "spanId": s} for t, s in links if is_trace_id(t) and is_span_id(s)
    ]
    # span 이 아닌 이벤트(turn_start 빼고)는 부모 span 의 span event 다
    for row in rows:
        if span_kind(row) is not None or row.get("event") == TURN_START:
            continue
        if not isinstance(row.get("ts"), datetime):
            continue
        parent = spans.get(str(row.get("span_id") or row.get("parent_span_id"))) or root
        parent.setdefault("events", []).append(_point(row))
    resource = {
        "service.name": _dict(agent.get("k8s")).get("name") or f"agent-{agent.get('name')}",
        "service.namespace": _text(end.get("tenant")),
        "service.version": str(_dict(end.get("profile")).get("revision") or ""),
        "service.instance.id": _text(end.get("pod")),
        "k8s.namespace.name": _text(end.get("namespace")),
        "k8s.pod.name": _text(end.get("pod")),
        "daegam.agent.id": agent.get("id"),
        "daegam.cluster": _text(end.get("cluster_id")),
    }
    return {
        "resource": {"attributes": encode_attributes(resource)},
        "scopeSpans": [{"scope": {"name": "daegam.agent"}, "spans": list(spans.values())}],
    }


def _span(
    row: dict[str, Any],
    *,
    agent: Mapping[str, Any],
    costs: Mapping[str, float | None],
    capture: bool,
    trace_id: str,
) -> dict[str, Any] | None:
    kind = span_kind(row)
    span_id = row.get("span_id")
    if kind is None or not is_span_id(span_id):
        return None
    data = _dict(row.get("data"))
    start = row.get("start") if isinstance(row.get("start"), datetime) else row.get("ts")
    if not isinstance(start, datetime):
        return None
    # 실행 하나는 하루를 넘지 않는다. 그 밖의 값이 끝 시각을 넘치게 하지 않게 자른다
    duration = min(_number(row.get("duration_ms")), MAX_SPAN_MS)
    status = _text(row.get("status"))
    if kind == "run":
        name, span_kind_code, attrs = _run(row, data, agent)
        failed = run_failed(status)
        if capture and (text := _body(row, "text")) is not None:
            attrs["gen_ai.output.messages"] = text
    elif kind in ("model", "embeddings"):
        cost = costs.get(f"{trace_id}-{span_id}")
        name, span_kind_code, attrs = _model(data, cost if isinstance(cost, int | float) else None)
        failed = call_failed(status)
    else:
        name, span_kind_code, attrs = _tool(row, data)
        failed = call_failed(status)
        if capture:
            attrs["gen_ai.tool.call.arguments"] = _body(row, "args")
            attrs["gen_ai.tool.call.result"] = _body(row, "content")
    if failed:
        attrs["error.type"] = _text(data.get("error_type")) or status or "error"
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": span_kind_code,
        "startTimeUnixNano": to_nanos(start),
        "endTimeUnixNano": to_nanos(start + timedelta(milliseconds=duration)),
        "attributes": encode_attributes(attrs),
        "status": {"code": STATUS_ERROR if failed else STATUS_OK},
    }
    if is_span_id(row.get("parent_span_id")):
        span["parentSpanId"] = row["parent_span_id"]
    return span


def _run(
    row: dict[str, Any], data: dict[str, Any], agent: Mapping[str, Any]
) -> tuple[str, int, dict[str, Any]]:
    usage = _dict(data.get("usage"))
    name = agent.get("name") or "agent"
    return (
        f"invoke_agent {name}",
        SPAN_KIND_INTERNAL,
        {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.id": agent.get("id"),
            "gen_ai.agent.name": name,
            "gen_ai.agent.version": _count(_dict(row.get("profile")).get("revision")),
            "gen_ai.conversation.id": _text(row.get("session_id")),
            "daegam.turn": _count(row.get("turn")),
            "daegam.turn_id": _text(row.get("turn_id")),
            "daegam.status": _text(row.get("status")),
            "gen_ai.usage.input_tokens": _count(usage.get("input_tokens")),
            "gen_ai.usage.output_tokens": _count(usage.get("output_tokens")),
        },
    )


def _model(data: dict[str, Any], cost: float | None) -> tuple[str, int, dict[str, Any]]:
    operation = "embeddings" if data.get("operation") == "embeddings" else "chat"
    provider = _text(_dict(data.get("provider")).get("kind"))
    usage = _dict(data.get("usage"))
    ttft = data.get("ttft_ms")
    finish = _text(data.get("finish_reason"))
    model = _text(data.get("model"))
    return (
        f"{operation} {model or 'model'}",
        SPAN_KIND_CLIENT,
        {
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": STUB_PROVIDER if provider == "stub" else provider,
            "gen_ai.request.model": model,
            "gen_ai.response.model": _text(data.get("response_model")),
            "gen_ai.usage.input_tokens": _count(usage.get("input_tokens")),
            "gen_ai.usage.output_tokens": _count(usage.get("output_tokens")),
            "gen_ai.usage.cache_read.input_tokens": _count(usage.get("cache_read_tokens")),
            "gen_ai.usage.cache_write.input_tokens": _count(usage.get("cache_write_tokens")),
            "gen_ai.usage.reasoning.output_tokens": _count(usage.get("reasoning_tokens")),
            "gen_ai.response.finish_reasons": [finish] if finish else None,
            "gen_ai.response.time_to_first_chunk": (
                _number(ttft) / 1000 if isinstance(ttft, int | float) else None
            ),
            "daegam.route": _text(data.get("route")),
            "daegam.attempt": _count(data.get("attempt")),
            "daegam.fallback": data.get("fallback")
            if isinstance(data.get("fallback"), bool)
            else None,
            "daegam.purpose": _text(data.get("purpose")),
            "daegam.cost.usd": cost,
        },
    )


def _tool(row: dict[str, Any], data: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    source = _dict(data.get("source"))
    if source.get("kind") == "subagent":
        sub = _text(data.get("subagent")) or _text(row.get("tool")) or "subagent"
        return (
            f"invoke_agent {sub}",
            SPAN_KIND_INTERNAL,
            {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": sub},
        )
    tool = _text(row.get("tool")) or "tool"
    kind = _text(source.get("kind"))
    return (
        f"execute_tool {tool}",
        SPAN_KIND_INTERNAL,
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.id": _text(data.get("call_id")),
            "gen_ai.tool.type": TOOL_TYPES.get(kind or "", "function"),
            "daegam.tool.source.kind": kind,
            "daegam.tool.source.id": _text(source.get("id")),
            "daegam.tool.source.name": _text(source.get("name")),
        },
    )


def _point(row: dict[str, Any]) -> dict[str, Any]:
    """점 이벤트 하나를 부모 span 의 span event 로. 이벤트마다 정한 칸(`POINT_FIELDS`)만 싣고
    값은 글·수·참거짓과 그 목록만이다."""
    event = str(row.get("event"))
    data = _dict(row.get("data"))
    fields = {k: data[k] for k in POINT_FIELDS.get(event, ()) if _plain(data.get(k))}
    return {
        "timeUnixNano": to_nanos(row["ts"]),
        "name": event,
        "attributes": encode_attributes(fields),
    }


def _plain(value: Any) -> bool:
    if isinstance(value, list):
        return all(isinstance(v, str | int | float | bool) for v in value)
    return isinstance(value, str | int | float | bool)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _number(value: Any) -> float:
    """0 이상의 유한한 수. 아니면 0 이다."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number > 0 else 0.0


def _body(row: dict[str, Any], key: str) -> str | None:
    """기록된 본문 칸 하나를 글로. 없으면 None 이다."""
    value = _dict(row.get(BODY)).get(key)
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
