"""Agent 답변 피드백의 본문과 응답 (P36 6절, D61). 평가 한 건은 store 모델 그대로다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.store.agent_feedback import (
    MAX_COMMENT_CHARS,
    MAX_TAGS,
    AgentFeedback,
    FeedbackTag,
    Rating,
)


class AgentFeedbackBody(BaseModel):
    """`PUT .../turns/{turn_id}/feedback`. 이유는 다섯 가운데 고른 것이고 겹치면 하나로 센다.
    의견은 앞뒤 공백을 떼고 비면 없는 것이다. `ephemeral` 은 테스트 채팅의 평가라는 표시다.
    `evaluation_use` 를 끄면 이 평가에서 복사한 평가 사례가 지워진다."""

    model_config = ConfigDict(extra="forbid")

    rating: Rating
    tags: list[FeedbackTag] = Field(default_factory=list, max_length=MAX_TAGS)
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    ephemeral: bool = False
    # 이 질문과 답을 평가 사례로 복사해도 된다는 동의 (P45 8절). 보내지 않거나 null 이면 동의하지
    # 않은 것이다. 화면은 켜져 있을 때만 싣는다
    evaluation_use: bool | None = None

    @field_validator("tags")
    @classmethod
    def _unique_tags(cls, tags: list[FeedbackTag]) -> list[FeedbackTag]:
        return list(dict.fromkeys(tags))

    @field_validator("comment")
    @classmethod
    def _blank_comment(cls, comment: str | None) -> str | None:
        return (comment or "").strip() or None


class AgentFeedbackOut(AgentFeedback):
    """평가 한 건. 소유자가 다시 연 세션의 단추 상태와 승인자 목록이 같은 모양이다."""

    model_config = REQUIRED_DEFAULTS


class AgentSessionFeedbackOut(BaseModel):
    """`GET .../sessions/{sid}/feedback`. 그 세션의 내 평가와 이 설치의 평가 보존 기간이다.
    `retention_days` 는 `DAEGAM_FEEDBACK_RETENTION_DAYS` 이고, 평가 사례로 쓰는 동의의 안내가
    세션에 아직 평가가 없어도 이 날 수를 적는다(P45 8절)."""

    model_config = REQUIRED_DEFAULTS

    items: list[AgentFeedbackOut]
    retention_days: int


class AgentFeedbackCountsOut(BaseModel):
    """평가별 수다."""

    model_config = REQUIRED_DEFAULTS

    up: int
    down: int


class AgentFeedbackListOut(BaseModel):
    """`GET /agents/{id}/feedback`. 새것부터 한 쪽이고 `next_before` 를 다음 쪽의 `before` 로
    그대로 준다. 마지막 쪽이면 null 이다. 값은 마지막 행의 만든 시각과 id 를 이은 표지라 같은
    시각의 행이 쪽 경계에 걸려도 빠지지 않는다. `last_7_days` 는 거르개와 상관없이 지난 7일의
    수다."""

    model_config = REQUIRED_DEFAULTS

    items: list[AgentFeedbackOut]
    next_before: str | None = None
    last_7_days: AgentFeedbackCountsOut


__all__ = [
    "AgentFeedbackBody",
    "AgentFeedbackCountsOut",
    "AgentFeedbackListOut",
    "AgentFeedbackOut",
    "AgentSessionFeedbackOut",
]
