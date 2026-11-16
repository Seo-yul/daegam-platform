"""사람이 기억한 승인 선택의 목록과 지우기 (P43 5절, 계정 화면).

`/me/approval-choices` 는 부른 사람 자신의 선택만 본다. 테넌트 밖의 경로라 Agent 권한을 보지 않고
모든 테넌트의 것을 Agent 이름과 함께 낸다(`/me/runs-as-me` 와 같다). 남의 선택을 지우려 하면 404 다.
선택은 결정과 함께 만들어지므로(`POST /agents/{id}/sessions/{sid}/decisions` 의 `remember`) 여기서
만들지 않는다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status

from daegam.api._common import audit_target, or_404, with_agent_names
from daegam.api.agents import Agents, Choices
from daegam.api.deps import User
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.approval_choices import ApprovalChoicesOut
from daegam.store.approval_choices import ApprovalChoice

router = APIRouter(
    prefix="/me/approval-choices", tags=["approval-choices"], responses=ERROR_RESPONSES
)


def _out(choice: ApprovalChoice) -> dict[str, Any]:
    return choice.model_dump(exclude={"sub", "digest"})


@router.get("", response_model=ApprovalChoicesOut)
async def list_my_approval_choices(user: User, store: Agents, choices: Choices) -> dict[str, Any]:
    """내가 기억한 선택이다. 만료된 것은 빼고 오래된 것부터다. 지운 Agent 의 선택은 이름이 없다."""
    return {"choices": await with_agent_names(store, await choices.mine(user.sub), _out)}


@router.delete("/{choice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_approval_choice(
    request: Request, user: User, choices: Choices, choice_id: str
) -> None:
    """내 선택 하나를 지운다. 다음 턴부터 다시 묻는다. 남의 것이거나 없으면 404 다."""
    deleted = or_404(await choices.forget_mine(user.sub, choice_id), "approval choice", choice_id)
    audit_target(request, deleted.id, deleted.tenant)
