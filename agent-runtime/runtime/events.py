"""관측 이벤트를 stdout 에 JSON 한 줄씩 쓴다. 플랫폼 AuditCollector 가 파드 로그에서 읽는다(D26).

줄 모양은 `{"daegam_event": "<종류>", "id": ..., "ts": ..., "n": ..., "profile": {...}, "instance":
{...}, ...}` 이다. `id` 는 이벤트마다의 uuid4 hex 라 같은 이벤트가 두 길로 와도 한 번만
저장된다(P37). 메시지·툴 인자·결과 본문은 service.observability.record_bodies 가 true 일 때만 싣고,
에이전트의 pii_mask 규칙을 위치와 상관없이 모두 지난다. 직렬화한 줄이 64 KiB 를 넘으면 본문 칸을
빼고 `truncated` 를 단다. 스팬(turn_end·model_call·tool_exec)은 끝날 때 `emit_span` 으로 한 번
나간다(trace.py).
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TextIO

from runtime.config import AgentConfig
from runtime.shipper import event_size

if TYPE_CHECKING:
    from runtime.guards import Rule
    from runtime.shipper import EventShipper

TURN_START = "turn_start"
TURN_END = "turn_end"
# 모델 호출 하나와 도구 실행 하나의 스팬이다(P37). 도구 실행 스팬이 옛 tool_call·tool_result 를
# 갈음한다
MODEL_CALL = "model_call"
TOOL_EXEC = "tool_exec"
INTERRUPT = "interrupt"
DECISION = "decision"
GUARDRAIL = "guardrail"
BUDGET = "budget"
RATE_LIMITED = "rate_limited"
MODEL_FALLBACK = "model_fallback"
MODEL_RETRY = "model_retry"
# 웹 가드가 거절한 호출이다(P39b 16절). code·tool·host 와 url_not_allowed 의 reason 을 싣는다
WEB_GUARD = "web_guard"
# 판정 모델이 판정하지 못하고 바깥 내용으로만 넘긴 결과다(P43 7절). tool·outcome·agent 와
# 거절이면 category, 일부만 보았으면 partial 을 싣는다
INJECTION_CHECK = "injection_check"
# 한 실행의 Tool 호출 한도에 닿았다(T82, tool_limit.py). limit 과 서브에이전트면 agent 를 싣는다
TOOL_LIMIT = "tool_limit"
RUNTIME_START = "runtime_start"
TOOLS_REFRESHED = "tools_refreshed"
SESSION_EXPIRED = "session_expired"
# 소유자가 세션을 지웠다(P36). 플랫폼은 이것을 보고 그 세션의 평가를 지운다
SESSION_DELETED = "session_deleted"
# turn_end 에서 다시 생성이 버린 턴의 id 칸이다(P36). 플랫폼은 이것을 보고 그 턴의 평가에
# superseded 를 단다
REPLACED_TURN_ID = "replaced_turn_id"
# 소유자가 장기 기억 파일 하나를 지웠다(P36). 경로는 싣지 않는다. 플랫폼 프록시 경로로 지웠을 때만
# 플랫폼 감사가 경로 URL 로 남기고 외부 경로로 지우면 주체만 남는다
MEMORY_DELETED = "memory_deleted"
# 이벤트가 없던 입구의 거절이다(P37). 413 max_input_chars, 429 max_turns·max_concurrent_sessions
# 이고 `code` 를 든다
REJECTED = "rejected"

MAX_BODY_CHARS = 8000
# 직렬화한 이벤트 하나의 상한이다. 플랫폼 수집 경로의 이벤트당 상한과 같다
MAX_EVENT_BYTES = 64 * 1024
# record_bodies 일 때만 실리는 본문 칸이다. 상한을 넘은 이벤트는 이 칸을 뺀다
BODY_FIELDS = ("message", "args", "content", "text", "structured_response")
# 사람이 붙인 파일 이름이 드는 칸이다(P36 12절). 본문처럼 record_bodies 일 때만 싣는다
NAME_FIELDS = ("attachment",)


class EventLog:
    def __init__(self, config: AgentConfig, out: TextIO | None = None) -> None:
        self._out = out or sys.stdout
        self._record_bodies = config.record_bodies
        self._base = {
            "profile": {
                "id": config.profile["id"],
                "name": config.profile["name"],
                "revision": config.profile["revision"],
            },
            "instance": {"id": config.instance["id"], "name": config.instance["name"]},
        }
        self.lines = 0
        self.masks: Sequence[Rule] = ()
        """본문에 대는 pii_mask 규칙이다. 컴파일한 가드레일에서 기동이 채운다(boot.py)."""
        self.shipper: EventShipper | None = None
        """플랫폼에 밀어 보내는 큐다(shipper.py). 수집 주소가 없으면 None 이고 stdout 에만 쓴다."""

    @property
    def record_bodies(self) -> bool:
        return self._record_bodies

    def emit(self, event: str, **fields: Any) -> None:
        # `n` 은 이 프로세스가 쓴 줄 번호다. 수집기가 (파드 uid + 줄) 해시로 중복을 지우는데,
        # 같은 밀리초에 같은 내용의 줄이 둘 나오면 하나로 합쳐져 이벤트가 소리 없이 사라진다.
        # 번호가 있으면 줄이 항상 달라지고, 로그를 겹쳐 읽어도 같은 줄은 같은 해시다
        line = {
            "daegam_event": event,
            "id": uuid.uuid4().hex,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "n": self.lines,
            **self._base,
            **{k: v for k, v in fields.items() if v is not None},
        }
        text = json.dumps(line, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) > MAX_EVENT_BYTES:
            line = {**{k: v for k, v in line.items() if k not in BODY_FIELDS}, "truncated": True}
            text = json.dumps(line, ensure_ascii=False, default=str)
        self._out.write(text + "\n")
        self._out.flush()
        self.lines += 1
        if self.shipper is not None:
            self.shipper.offer(json.loads(text), event_size(text))

    def emit_span(self, event: str, *, start: datetime, duration_ms: float, **fields: Any) -> None:
        """끝난 스팬 하나를 낸다. 시작 시각과 길이(밀리초)를 든다."""
        self.emit(
            event,
            start=start.isoformat(timespec="milliseconds"),
            duration_ms=duration_ms,
            **fields,
        )

    def scrub(self, fields: dict[str, Any]) -> dict[str, Any]:
        """칸 가운데 파일 이름(NAME_FIELDS)을 본문 규칙(`body`)으로 거른 사본이다. SSE 로는 이름이
        나가는 위반을 관측 이벤트로 옮길 때 쓴다."""
        return {k: self.body(v) if k in NAME_FIELDS else v for k, v in fields.items()}

    def body(self, value: Any) -> Any:
        """record_bodies 가 꺼져 있으면 None 을 돌려줘 emit 이 키를 빼게 한다. 켜져 있으면 pii_mask
        규칙을 모두 지나 앞 MAX_BODY_CHARS 자만 남긴 글이다. 가리기 전에 두 배로 먼저 잘라 큰
        결과에서도 검출 비용을 묶고, 경계에 걸친 값은 가린 뒤에 자른다."""
        if not self._record_bodies or value is None:
            return None
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except TypeError:
                text = str(value)
        text = text[: 2 * MAX_BODY_CHARS]
        for rule in self.masks:
            text, _ = rule.mask(text)
        return text[:MAX_BODY_CHARS]
