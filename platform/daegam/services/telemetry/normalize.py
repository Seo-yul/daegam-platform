"""런타임 이벤트 하나를 `agent_events` 문서로 바꾸는 규칙 (P37 4절·5절, D62).

런타임이 밀어 보낸 이벤트와 수집기가 파드 로그에서 읽은 줄이 같은 규칙을 지난다. 이벤트의 `id` 가
`_id` 라 두 길로 온 같은 이벤트는 한 번만 들어간다. `id` 가 없는 옛 줄은 파드 uid 와 줄 원문의
해시다.

- 거르기와 색인이 읽는 칸(`AGENT_EVENT_FIELDS`)은 최상위로 끌어올리고, 나머지는 `data` 아래에 둔다
- 본문 칸(`BODY_KEYS`)과 첨부 이름은 `data` 에서 `body` 하나로 옮긴다. 목록은 이 파일에만 있다
- 입구의 거절은 `rejected_code` 한 칸으로 센다
- 플랫폼만 쓰는 이벤트(`PLATFORM_EVENTS`)는 런타임에서 오면 받지 않는다. 인스턴스 토큰으로 평가를
  지어내지 못하게 한다
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from daegam.store.agent_events import BODY
from daegam.store.agents import Agent
from daegam.store.base import parse_iso_utc, utcnow

AGENT_EVENT_KEY = "daegam_event"
# 이벤트 문서의 최상위로 끌어올리는 런타임 칸. 나머지는 data 아래에 그대로 둔다
AGENT_EVENT_FIELDS = (
    "session_id",
    "user_sub",
    "turn",
    "tool",
    "status",
    "decided_by",
    "turn_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "start",
    "duration_ms",
)
# 본문 칸이다. record_bodies 일 때만 오고, 들어갈 때 data 에서 body 로 옮긴다
BODY_KEYS = ("message", "args", "content", "text", "structured_response", "attachment")
# 첨부 요약에 붙은 사람이 지은 이름(turn_start.attachments[].name)이 body 에서 드는 칸이다
ATTACHMENT_NAMES = "attachment_names"
# 이벤트의 봉투 칸이다. 문서의 제 칸으로 가고 data 에는 들지 않는다
ENVELOPE = (AGENT_EVENT_KEY, "id", "ts", "n", "profile", "instance")
# 미리 보기가 낸 이벤트의 표시 (P44b 9절). 플랫폼이 수집할 때 원본의 문서에 단다. 런타임이 같은
# 이름의 칸을 보내도 최상위로 오지 않고 data 에 든다
PREVIEW_ID_KEY = "preview_id"


def preview_mark(agent: Agent) -> dict[str, Any]:
    """미리 보기의 이벤트 문서가 덮어쓰는 칸 (P44b 9절). 인스턴스는 원본의 id 와 이름이고 미리
    보기의 id 를 따로 단다. 미리 보기가 아닌 Agent 는 빈 사전이다. 수집 경로와 수집기가 같이
    쓴다."""
    preview = agent.preview
    if preview is None:
        return {}
    return {
        "instance_id": preview.source,
        "instance": {"id": preview.source, "name": preview.source_name},
        PREVIEW_ID_KEY: agent.id,
    }


FEEDBACK_EVENT = "feedback"
# 플랫폼이 읽는 런타임 이벤트의 이름이다. 원본은 agent-runtime/runtime/events.py 이고 이 파일이
# 플랫폼의 한 자리다(test_chat_parity 가 맞춰 본다)
TURN_START = "turn_start"
TURN_END = "turn_end"
MODEL_CALL = "model_call"
TOOL_EXEC = "tool_exec"
INTERRUPT = "interrupt"
DECISION = "decision"
GUARDRAIL = "guardrail"
BUDGET = "budget"
RATE_LIMITED = "rate_limited"
MODEL_FALLBACK = "model_fallback"
MODEL_RETRY = "model_retry"
REJECTED = "rejected"
# 소유자가 세션을 지웠다(D61). 그 세션의 본문을 지우고 표를 남긴다
SESSION_DELETED = "session_deleted"
# 다시 생성한 턴이 대신한 턴의 id 다(turn_end 의 칸)
REPLACED_TURN_ID = "replaced_turn_id"
# 런타임 큐가 넘쳐도 마지막까지 지키는 비용 이벤트다(shipper.COST_EVENTS). 원장과 턴의 토큰이 읽는다
COST_EVENTS = frozenset({MODEL_CALL, TURN_END})
# 거절로 세는 budget·guardrail 이벤트의 어휘다(런타임 budget.py·guards.py)
BUDGET_REJECTED = "rejected"
BUDGET_DAILY = "daily"
GUARDRAIL_INPUT = "input"
GUARDRAIL_BLOCK = "block"
RUNTIME_EVENT_NAMES = (
    "TURN_START",
    "TURN_END",
    "MODEL_CALL",
    "TOOL_EXEC",
    "INTERRUPT",
    "DECISION",
    "GUARDRAIL",
    "BUDGET",
    "RATE_LIMITED",
    "MODEL_FALLBACK",
    "MODEL_RETRY",
    "REJECTED",
    "SESSION_DELETED",
    "REPLACED_TURN_ID",
)
# 코드 실행 샌드박스가 열리고 닫혔다 (P40 5절). 세션과 사람을 싣는다
SANDBOX_OPENED = "sandbox_opened"
SANDBOX_CLOSED = "sandbox_closed"
# 플랫폼만 쓰는 이벤트. 수집기는 버리고 수집 경로는 거절한다
PLATFORM_EVENTS = frozenset({FEEDBACK_EVENT, SANDBOX_OPENED, SANDBOX_CLOSED})

# 실패가 아닌 실행(turn_end)의 결과다. 이 밖의 결과(turn_failed 같은 실패 이유와 모르는 값)는 실패다
RUN_OK_STATUSES = frozenset({"done", "pending", "cancelled"})
# 실패가 아닌 호출의 결과다. model_call 은 ok, tool_exec 는 Tool 결과의 success 다
CALL_OK_STATUSES = frozenset({"ok", "success"})

# Mongo 가 받는 정수의 범위다
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

SOURCE_PUSH = "push"
SOURCE_LOG = "log"
SOURCE_PLATFORM = "platform"


def run_failed(status: Any) -> bool:
    """실행이 실패로 끝났다. 결과가 없으면 실패가 아니다. 지표, OTLP 표본과 span, 트레이스가
    같이 쓴다."""
    return status is not None and status not in RUN_OK_STATUSES


def call_failed(status: Any) -> bool:
    """모델·Tool 호출이 실패했다. 결과가 없으면 실패가 아니다."""
    return status is not None and status not in CALL_OK_STATUSES


def call_failed_expr(field: str = "$status") -> dict[str, Any]:
    """`call_failed` 의 Mongo 집계 식이다. 지표가 같은 규칙으로 센다."""
    return {
        "$let": {
            "vars": {"s": {"$ifNull": [field, None]}},
            "in": {
                "$and": [
                    {"$ne": ["$$s", None]},
                    {"$not": [{"$in": ["$$s", sorted(CALL_OK_STATUSES)]}]},
                ]
            },
        }
    }


def span_kind(row: dict[str, Any]) -> str | None:
    """이벤트 문서가 되는 span 의 종류다. span 이 아닌 이벤트는 None 이다. 트레이스 화면과 OTLP
    내보내기가 같이 쓴다."""
    event = row.get("event")
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    if event == TURN_END:
        return "run"
    if event == MODEL_CALL:
        return "embeddings" if data.get("operation") == "embeddings" else "model"
    if event == TOOL_EXEC:
        source = data.get("source")
        return (
            "subagent" if isinstance(source, dict) and source.get("kind") == "subagent" else "tool"
        )
    return None


def numbers_fit(value: Any) -> bool:
    """모든 수가 저장되고 내보내진다. 정수는 64비트 안이고(Mongo) 실수는 유한하다. `json.loads` 는
    NaN·Infinity 와 `1e400` 을 받지만 표준 JSON 이 아니라 OTLP 내보내기가 그 묶음을 보내지 못한다.
    수집 경로와 수집기가 이 규칙으로 이벤트 하나씩 거절한다."""
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return INT64_MIN <= value <= INT64_MAX
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(numbers_fit(v) for v in value.values())
    if isinstance(value, list):
        return all(numbers_fit(v) for v in value)
    return True


def json_object(line: str) -> dict[str, Any] | None:
    """줄에서 첫 `{` 부터 JSON 객체로 읽는다 (kubectl --timestamps 접두어 허용)."""
    start = line.find("{")
    if start < 0:
        return None
    try:
        d = json.loads(line[start:])
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def rejected_code(event: str, fields: dict[str, Any]) -> str | None:
    """입구의 거절이면 그 코드다. 지표가 이 칸 하나로 거절을 센다 (P37 5절)."""
    if event == RATE_LIMITED:
        return RATE_LIMITED
    if event == BUDGET and fields.get("action") == BUDGET_REJECTED:
        daily = fields.get("scope") == BUDGET_DAILY
        return "daily_budget_exceeded" if daily else "session_budget_exceeded"
    input_block = (fields.get("position"), fields.get("action")) == (
        GUARDRAIL_INPUT,
        GUARDRAIL_BLOCK,
    )
    if event == GUARDRAIL and input_block:
        return "guardrail_blocked"
    if event == REJECTED and isinstance(fields.get("code"), str):
        return fields["code"]
    return None


def split_body(fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(data, body). 본문 칸과 첨부 이름을 body 로 옮긴다."""
    data = {k: v for k, v in fields.items() if k not in BODY_KEYS}
    body = {k: fields[k] for k in BODY_KEYS if fields.get(k) is not None}
    attachments = data.get("attachments")
    if isinstance(attachments, list):
        names = [a.get("name") if isinstance(a, dict) else None for a in attachments]
        if any(n is not None for n in names):
            body[ATTACHMENT_NAMES] = names
            data["attachments"] = [
                {k: v for k, v in a.items() if k != "name"} if isinstance(a, dict) else a
                for a in attachments
            ]
    return data, body


def agent_event_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """이벤트의 칸을 agent_events 문서의 칸으로 나눈다. 플랫폼이 직접 쓰는 이벤트(답변 피드백)도
    이 모양이다."""
    data, body = split_body(fields)
    out: dict[str, Any] = {**{key: data.get(key) for key in AGENT_EVENT_FIELDS}, "data": data}
    out["start"] = parse_iso_utc(out["start"]) if isinstance(out["start"], str) else None
    if body:
        out[BODY] = body
    return out


def event_document(
    event: dict[str, Any], *, source: str, fallback_id: str | None = None
) -> dict[str, Any] | None:
    """이벤트 하나를 문서로. 이름이 없거나 id 도 대신할 id 도 없으면 None 이다."""
    name = event.get(AGENT_EVENT_KEY)
    event_id = event.get("id") if isinstance(event.get("id"), str) else fallback_id
    if not isinstance(name, str) or not name or not event_id:
        return None
    fields = {k: v for k, v in event.items() if k not in ENVELOPE}
    seq = event.get("n")
    return {
        "_id": event_id,
        "ts": parse_iso_utc(event.get("ts")) or utcnow(),
        "event": name,
        "profile": _dict_or_none(event.get("profile")),
        "instance": _dict_or_none(event.get("instance")),
        "seq": seq if isinstance(seq, int) else None,
        "source": source,
        "rejected_code": rejected_code(name, fields),
        **agent_event_fields(fields),
    }


def parse_agent_line(line: str, *, salt: str = "") -> dict[str, Any] | None:
    """런타임 관측 줄 하나 → 문서. `daegam_event` 가 없거나 깨졌거나 플랫폼만 쓰는 이벤트거나
    저장·내보내지 못하는 수(`numbers_fit`)가 있으면 None. 이벤트의 `id` 가 없는 옛 줄의 `_id` 는
    salt(파드 uid)와 JSON 원문의 sha256 앞 32자라 같은 파드에서 겹쳐 읽은 줄은 같은 id 다."""
    if AGENT_EVENT_KEY not in line:
        return None
    d = json_object(line)
    if d is None or d.get(AGENT_EVENT_KEY) in PLATFORM_EVENTS or not numbers_fit(d):
        return None
    raw = line[line.find("{") :].strip()
    digest = hashlib.sha256(f"{salt}\n{raw}".encode()).hexdigest()[:32]
    return event_document(d, source=SOURCE_LOG, fallback_id=digest)


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None
