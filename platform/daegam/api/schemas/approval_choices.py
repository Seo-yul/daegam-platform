"""기억한 승인 선택의 응답 모양 (P43 5절)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from daegam.store.approval_choices import Choice
from daegam.store.base import UtcDatetime


class ApprovalChoiceOut(BaseModel):
    """기억한 선택 하나다. `agent_name` 은 지운 Agent 면 null 이다."""

    id: str
    tenant: str
    agent_id: str
    agent_name: str | None = None
    tool: str
    choice: Choice
    created_at: UtcDatetime
    expires_at: UtcDatetime


class ApprovalChoicesOut(BaseModel):
    choices: list[ApprovalChoiceOut] = Field(default_factory=list)
