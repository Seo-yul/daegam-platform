"""HITL 보류 조회와 결정 검증이다(D26).

보류 상세는 체크포인터 `aget_state(thread).interrupts[0].value` 의 `action_requests` 와
`review_configs` 에서 읽는다. 결정은 개수가 `action_requests` 와 같아야 하고 각 결정의 type 은
대응하는 `review_configs[i].allowed_decisions` 안에 있어야 한다. 검증을 통과한 결정은
`Command(resume={"decisions": [...]})` 로 같은 thread 를 재개한다.

고치기(edit)는 같은 도구의 인자만 바꾼다(D65). `edited_action.name` 은 같은 자리 요청의 이름과
같아야 하고(아니면 `edit_name_mismatch`), 고친 인자는 재개 전에 도구 가드레일의 인자 규칙을
지난다(block 은 `GuardrailBlocked`, flag 는 재개 스트림 앞머리의 guardrail 이벤트). 두 검사는 보류가
메인의 것이든 서브에이전트의 것이든 건다. 실행 때의 도구 가드는 메인에만 있지만 사람이 넣은
인자는 규칙을 지나야 한다. 어느 검사에 걸려도 아무것도 재개하지 않고 세션은 pending 그대로다.
지난 edit 은 재개 턴의 씨앗에 실려(`Pending.seed_with`) 다시 알리는 tool_call 이 고친 인자를 싣는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from runtime.approvals import MAX_CHOICES, ApprovalChoice, PendingCalls
from runtime.compile import CompiledAgent
from runtime.guards import ACTION_BLOCK, GuardrailBlocked, ToolGuard, Violation
from runtime.sessions import RUN_ID_PATTERN
from runtime.stream import (
    DecidedEdit,
    TurnState,
    interrupt_payload,
    snapshot_messages,
    thread_config,
)
from runtime.turn_grants import TurnGrant

DecisionType = Literal["approve", "edit", "reject", "respond"]

APPROVER_HEADER = "x-daegam-approver"
"""플랫폼이 `agent:approve` scope 를 확인한 뒤 붙이는 헤더다. 값이 1 이면 소유자가 아니어도 된다.
인스턴스 토큰 경로에서만 읽고 외부 OIDC 경로에서는 무시한다(runtime/auth.py)."""


class EditedAction(BaseModel):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    type: DecisionType
    edited_action: EditedAction | None = None
    message: str | None = None

    def to_resume(self) -> dict[str, Any]:
        """HumanInTheLoopMiddleware 가 받는 모양으로 바꾼다. 필요 없는 키는 넣지 않는다."""
        out: dict[str, Any] = {"type": self.type}
        if self.type == "edit" and self.edited_action is not None:
            out["edited_action"] = {
                "name": self.edited_action.name,
                "args": self.edited_action.args,
            }
        if self.type in ("reject", "respond") and self.message is not None:
            out["message"] = self.message
        return out


class DecisionsRequest(BaseModel):
    # 프록시 경로의 결정자. 외부 경로는 무시하고 토큰 sub 를 쓴다. 읽는 곳은 runtime/auth.py 뿐이다
    user_sub: str | None = None
    decisions: list[Decision] = Field(min_length=1)
    # 이 턴을 임시로 돌린다(장기 기억 저장소를 안 붙인다). 재개도 같은 표시를 다시 실어야 한다
    ephemeral: bool = False
    # 플랫폼이 실은 턴 위임(P53 6절). 프록시 경로만 받는다(grant_gate.py)
    turn_grant: TurnGrant | None = None
    # 이 턴의 사람이 기억한 선택(P43 5절). 프록시 경로만 받는다(approvals.py)
    approval_choices: list[ApprovalChoice] | None = Field(default=None, max_length=MAX_CHOICES)
    # 백그라운드 실행 세션의 결정이 싣는 그 세션의 run_id 다(P41 9절). 프록시 경로만 받는다
    # (runs.py)
    run_id: str | None = Field(default=None, pattern=RUN_ID_PATTERN)


class HitlError(Exception):
    """결정을 받아들일 수 없을 때 낸다. 라우터가 422 로 옮긴다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Pending:
    action_requests: list[dict[str, Any]]
    review_configs: list[dict[str, Any]]
    seed: TurnState
    """재개 턴의 씨앗이다. 같은 스냅숏에서 만들어 앞 스트림이 낸 사용량·도구 이벤트를 다시 세지
    않는다. 승인을 기다리는 호출은 들어 있지 않아 재개가 그 tool_call 을 다시 알린다."""

    def seed_with(self, decisions: list[Decision]) -> TurnState:
        """edit 결정을 실은 씨앗이다. 재알림이 고친 인자를 싣는다(stream.TurnState.decided_call).
        validate_decisions 가 지난 결정에만 부르므로 자리가 요청과 맞고 고친 이름이 곧 요청의
        이름이다."""
        self.seed.edits = [
            DecidedEdit(request["name"], request.get("args") or {}, decision.edited_action.args)
            for decision, request in zip(decisions, self.action_requests, strict=True)
            if decision.type == "edit" and decision.edited_action is not None
        ]
        return self.seed


async def read_pending(agent: CompiledAgent, session_id: str) -> Pending | None:
    """보류 중인 인터럽트를 읽는다. 없으면 None 이다. `next` 가 아니라 인터럽트를 본다. 결정 수가
    맞지 않아 실패한 재개는 인터럽트를 남긴 채 `next` 를 비우기 때문이다(P43 F0).

    재개 턴의 씨앗은 기록을 훑은 바깥 여부와 대기 요청을 든다. 판정 지도가 없는 옛 메시지를 대기
    요청에 든 호출만 물어 재개하고, 재개하는 task 호출을 알아보기 때문이다(approvals.py)."""
    snapshot = await agent.graph.aget_state(thread_config(session_id))
    if not snapshot.interrupts:
        return None
    payload = interrupt_payload(snapshot.interrupts)
    messages = snapshot_messages(snapshot)
    action_requests = [dict(req) for req in payload["action_requests"]]
    seed = TurnState.seeded(messages, resumed=True, history_outside=agent.history_outside(messages))
    seed.pending_calls = PendingCalls.of(action_requests, unanswered_calls(messages))
    return Pending(
        action_requests=action_requests,
        review_configs=[dict(cfg) for cfg in payload["review_configs"]],
        seed=seed,
    )


def unanswered_calls(messages: list[BaseMessage]) -> frozenset[str]:
    """메시지의 도구 호출 가운데 아직 결과가 없는 것의 id 다. 재개하면 다시 도는 호출이다."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return frozenset(
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call.get("id") and call["id"] not in answered
    )


def validate_decisions(decisions: list[Decision], pending: Pending) -> list[dict[str, Any]]:
    """개수, 허용 type, edit 의 도구 이름을 검사하고 resume 페이로드를 돌려준다."""
    expected = len(pending.action_requests)
    if len(decisions) != expected:
        raise HitlError(
            "decision_count_mismatch",
            f"결정은 {expected}개여야 하는데 {len(decisions)}개다",
        )
    out: list[dict[str, Any]] = []
    for index, decision in enumerate(decisions):
        request = pending.action_requests[index]
        config = pending.review_configs[index] if index < len(pending.review_configs) else {}
        allowed = list(config.get("allowed_decisions") or [])
        if decision.type not in allowed:
            raise HitlError(
                "decision_not_allowed",
                f"{request.get('name')} 에는 {allowed} 만 허용되는데 {decision.type} 이다",
            )
        if decision.type == "edit":
            _check_edit_name(decision, request)
        if decision.type == "respond" and decision.message is None:
            raise HitlError("decision_invalid", "respond 에는 message 가 필요하다")
        out.append(decision.to_resume())
    return out


def _check_edit_name(decision: Decision, request: dict[str, Any]) -> None:
    """edit 은 같은 도구의 인자만 고친다(D65). 이름이 다르면 재개하지 않는다."""
    if decision.edited_action is None:
        raise HitlError("decision_invalid", "edit 에는 edited_action{name, args} 가 필요하다")
    expected = request.get("name")
    if decision.edited_action.name != expected:
        raise HitlError(
            "edit_name_mismatch",
            f"edit 은 {expected} 의 인자만 고칠 수 있는데 {decision.edited_action.name} 이다",
        )


def check_edited_args(decisions: list[Decision], guard: ToolGuard) -> list[Violation]:
    """edit 결정의 고친 인자에 도구 가드레일의 인자 규칙을 댄다. flag 위반 목록을 돌려주고 block
    위반이 하나라도 있으면 GuardrailBlocked 다(입력 검사와 같은 꼴). validate_decisions 가 지난
    결정에만 부르므로 고친 이름이 곧 요청의 도구다."""
    flagged: list[Violation] = []
    for decision in decisions:
        if decision.type != "edit" or decision.edited_action is None:
            continue
        action = decision.edited_action
        for violation in guard.args_violations(action.name, action.args):
            if violation.action == ACTION_BLOCK:
                raise GuardrailBlocked(violation)
            flagged.append(violation)
    return flagged


def edited_args(decisions: list[dict[str, Any]]) -> list[dict[str, Any] | None] | None:
    """resume 페이로드의 자리마다 고친 인자다(edit 이 아니면 None). edit 이 하나도 없으면 None 이라
    결정 이벤트가 칸을 싣지 않는다."""
    out = [
        decision["edited_action"]["args"] if decision.get("edited_action") else None
        for decision in decisions
    ]
    return out if any(args is not None for args in out) else None


def resume_command(decisions: list[dict[str, Any]]) -> Command:
    return Command(resume={"decisions": decisions})
