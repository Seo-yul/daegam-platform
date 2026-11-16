"""기억한 승인 선택, 옛 메시지를 재개할 대기 요청, 대기 이유 줄이다(P43 4·5절, D96).

- **기억한 선택.** 플랫폼이 `/chat`, 결정 재개, 다시 생성의 본문 `approval_choices` 로 그 턴의
  사람이 기억한 선택(`approve` 나 `reject`)을 Tool 이름마다 싣는다. 런타임은 턴 문맥에만 두고
  저장하지 않는다. 외부 경로는 그 칸을 받지 않는다(grant_gate.py 의 `refuse_external_fields`). 같은
  이름이 둘이면 거절이 이긴다. 거절은 막기만 하기 때문이다. 어느 이름이 기억할 수 있는 규칙인지와
  규칙이 바뀌어 무효가 된 선택을 거르는 것은 플랫폼의 몫이다(P43 5절). 런타임은 정적 규칙이 있는
  Tool 에만 승인을 쓴다(tool_safety.py). 백그라운드 실행의 세션에서는 승인을 버리고 거절만
  쓴다(P41 9절, runs.py).
- **대기 요청(`PendingCalls`).** 판정 지도가 없는 옛 메시지를 새 판으로 재개할 때 그 대기 요청에 든
  호출만 묻는다. 결정 라우트가 읽은 대기 요청을 턴 문맥에 싣고, `when` 이 메시지의 호출을 차례로
  이름과 인자로 맞춰 하나씩 꺼낸다(action request 에는 호출 id 가 없다). 재개하는 길은 모두 이것을
  실어야 한다. 없으면 모든 호출을 묻고 결정 수가 맞지 않아 세션이 대기에 남는다(의도한 실패다).
  재개하는 메시지에서 아직 답이 없는 호출 id 도 함께 든다. 메인의 바깥 표시 층이 재개된 task 호출을
  알아보는 데 쓴다(tool_safety.py).
- **대기 이유.** 승인 항목의 설명 첫 줄은 `reason: rule` 이나 `reason: outside` 다. 런타임의 대기
  읽기와 스트림의 `interrupt` 이벤트가 플랫폼에 보내기 전에 그 줄을 항목의 `reason` 칸으로 옮긴다
  (stream.py 의 `interrupt_payload`).

이 모듈은 pydantic 과 표준 라이브러리만 쓴다. 턴 문맥(long_term.py)이 이것을 싣기 때문이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

APPROVE = "approve"
REJECT = "reject"
MAX_CHOICES = 1000
"""본문 하나가 실을 수 있는 선택의 상한이다. 한 Agent 의 Tool 수보다 넉넉하다."""

REASON_RULE = "rule"
REASON_OUTSIDE = "outside"
REASON_PREFIX = "reason: "
REASON_FIELD = "reason"
REASONS = frozenset({REASON_RULE, REASON_OUTSIDE})


class ApprovalChoice(BaseModel):
    """기억한 선택 하나다. tool 은 런타임이 부르는 Tool 이름이다."""

    tool: str = Field(min_length=1, max_length=256)
    choice: Literal["approve", "reject"]


def choices_of(
    items: Sequence[ApprovalChoice] | None, *, rejects_only: bool = False
) -> dict[str, str]:
    """본문의 선택을 Tool 이름마다 하나로 모은다. 같은 이름이면 거절이 이긴다. rejects_only 면
    승인을 버린다. 백그라운드 실행의 세션이 그렇다(P41 9절, P43 5절). 실행은 스스로 승인을 결정하지
    않으므로 플랫폼이 승인을 실었더라도 쓰지 않는다."""
    out: dict[str, str] = {}
    for item in items or ():
        if rejects_only and item.choice != REJECT:
            continue
        if out.get(item.tool) != REJECT:
            out[item.tool] = item.choice
    return out


@dataclass
class PendingCalls:
    """재개할 대기 요청이다. calls 는 그 요청에 든 호출의 (이름, 인자)이고 `take` 가 차례로 하나씩
    꺼낸다. call_ids 는 재개하는 메시지에서 아직 답이 없는 호출 id 다."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    call_ids: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls, action_requests: Sequence[Mapping[str, Any]], call_ids: frozenset[str] = frozenset()
    ) -> PendingCalls:
        calls = [(str(r.get("name")), dict(r.get("args") or {})) for r in action_requests]
        return cls(calls, call_ids)

    def take(self, name: str, args: Mapping[str, Any]) -> bool:
        """이 호출이 대기 요청에 있었으면 꺼내고 참이다."""
        for index, (pending_name, pending_args) in enumerate(self.calls):
            if pending_name == name and pending_args == dict(args):
                del self.calls[index]
                return True
        return False


def reason_line(reason: str) -> str:
    return f"{REASON_PREFIX}{reason}"


def with_reason(action: Mapping[str, Any]) -> dict[str, Any]:
    """승인 항목 하나의 설명 첫 줄에 이유가 있으면 그것을 `reason` 칸으로 옮긴 사본이다. 없으면
    그대로의 사본이다(옛 판과 기능이 없는 문서의 항목)."""
    out = dict(action)
    description = out.get("description")
    if not isinstance(description, str):
        return out
    first, _, rest = description.partition("\n")
    reason = first[len(REASON_PREFIX) :] if first.startswith(REASON_PREFIX) else None
    if reason in REASONS:
        out[REASON_FIELD] = reason
        out["description"] = rest
    return out
