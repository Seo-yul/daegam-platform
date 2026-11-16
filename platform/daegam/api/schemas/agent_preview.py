"""Agent 미리 보기 API 의 응답 (P44b 6절, D107)."""

from __future__ import annotations

from pydantic import BaseModel

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.store.base import UtcDatetime


class AgentPreviewOut(BaseModel):
    """미리 보기 하나. source_id 는 원본 Agent, owner 는 시작한 사람의 sub 다. revision 은 그 파드가
    도는 리비전이고 phase 는 Deployment 를 한 번 읽은 것이다(뜨기 전이면 null). warnings 는 그
    파드의 init 받기가 남긴 런타임 경고(`code: 설명`, 예 `runtime_feature_dropped`)다. 끝은 둘이다.
    ends_at 은 처음 시작 2시간 뒤이고 옮겨도 그대로다. idle_ends_at 은 마지막 활동(턴, 결정, 다시
    생성, 시작, 옮기기) 30분 뒤이고 ends_at 을 넘지 않는다. idle_minutes 와 max_hours 는 그 두
    길이다. run 은 평가 실행이 띄운 미리 보기의 실행 id 이고 사람의 미리 보기는 null 이다(P45
    9절)."""

    model_config = REQUIRED_DEFAULTS

    id: str
    source_id: str
    owner: str
    revision: int
    state: str
    phase: str | None
    last_error: str | None
    warnings: list[str] = []
    started_at: UtcDatetime
    ends_at: UtcDatetime
    last_activity_at: UtcDatetime
    idle_ends_at: UtcDatetime
    idle_minutes: int
    max_hours: int
    run: str | None = None


class AgentPreviewsOut(BaseModel):
    """그 Agent 에 열린 미리 보기 모두. 처음 시작한 차례다."""

    model_config = REQUIRED_DEFAULTS

    previews: list[AgentPreviewOut]


class AgentPreviewEndOut(BaseModel):
    """끝낸 미리 보기. ended 가 거짓이면 다른 작업이 문서를 쥐고 있어 리컨실이 마저 지운다."""

    model_config = REQUIRED_DEFAULTS

    id: str
    ended: bool
