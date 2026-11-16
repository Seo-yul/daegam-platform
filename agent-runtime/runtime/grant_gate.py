"""턴 위임의 입구 검사다(P53 6절). `/chat`, 결정 재개, 다시 생성이 턴을 열기 전에 부른다.

외부 경로는 위임과 기억한 승인 선택(P43 5절)을 받지 않는다(422). 위임의 `sub` 는 세션
소유자이거나(새 세션이면 부른 사람), 결정 재개에서 플랫폼이 승인자로 표시한 결정자다. 결정자가
소유자가 아닌데 결정이 그의 입력(고친 인자, 보탠 글)을 실으면 소유자의 위임은 받지 않는다. 어긋나면
409 `turn_grant_mismatch` 다. 위임 토큰을 쓰고 갱신하는 일은 turn_grants.py 에 있다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from runtime.auth import Caller
from runtime.hitl import Decision
from runtime.turn_gate import error
from runtime.turn_grants import TurnGrant
from runtime.validation import body_field_error, validation_error

GRANT_MISMATCH = "turn_grant_mismatch"
# 결정이 결정자의 입력을 싣지 않는 꼴. 이것 밖은 모두 입력이라 새 결정 종류도 닫힌 쪽이다
_NO_INPUT_TYPES = frozenset({"approve", "reject"})


def holds_input(decisions: Sequence[Decision]) -> bool:
    """결정이 결정자 자신의 입력을 싣는가. 승인과 글 없는(`message is None`) 거절만 아니다. 빈 글도
    대화에 실리므로 입력이다 (P53 5.3절)."""
    return any(
        d.type not in _NO_INPUT_TYPES or (d.type == "reject" and d.message is not None)
        for d in decisions
    )


def refuse_external_fields(caller: Caller, **fields: Any) -> None:
    """외부 경로는 플랫폼만 싣는 칸을 받지 않는다. 밖의 호출자가 고른 토큰을 Agent 에 건네거나
    사람이 기억하지 않은 승인을 들이지 못하게 한다. fields 는 칸 이름과 받은 값이고 None 은 없는
    것이다."""
    if not caller.external:
        return
    errors = [
        body_field_error(name, f"{name} is accepted on the platform's path only", "extra_forbidden")
        for name, value in fields.items()
        if value is not None
    ]
    if errors:
        raise validation_error(errors)


def check_grant(
    caller: Caller,
    grant: TurnGrant | None,
    *,
    owner: str,
    decisions: Sequence[Decision] | None = None,
) -> TurnGrant | None:
    """위임이 이 턴의 사람의 것인가. 아니면 409 `turn_grant_mismatch` 다. decisions 는 결정 재개일
    때만 준다. 외부 경로의 위임은 입구가 먼저 `refuse_external_fields` 로 막았다."""
    if grant is None:
        return None
    resume = decisions is not None
    if grant.sub == owner:
        if resume and caller.sub != owner and holds_input(decisions or ()):
            raise _mismatch()
        return grant
    if resume and caller.approver and grant.sub == caller.sub:
        return grant
    raise _mismatch()


def _mismatch() -> Exception:
    return error(409, GRANT_MISMATCH, "위임이 이 턴의 사람의 것이 아니다")
