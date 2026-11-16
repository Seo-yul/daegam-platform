"""비서 API 요청·응답 (D31, P14). SSE 본문은 JSON 모델이 없고 이벤트 계약은
docs/guide/assistant.md 9절(API)이 정본이다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from daegam.api.schemas.agents import GraphOut, ServiceSettingsOut


class AssistantLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls_per_turn: int
    turn_timeout_seconds: int


class AssistantProviderOut(BaseModel):
    """홈에서 고를 수 있는 LLM provider 하나 (D45). 이름·종류·모델만 싣는다. 키 값도 Secret 이름도
    싣지 않으므로 설정 읽기 권한이 없는 사용자도 이 목록으로 모델을 고를 수 있다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    model: str


class AssistantConfigOut(BaseModel):
    """`GET /assistant/config`. enabled 는 `settings.assistant.enabled` 와 provider 해소 성공의
    논리곱이다. provider 는 해소된 기본 이름이고 없으면 null. providers 는 대화에 붙일 수 있는
    provider 목록이다(D45)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider: str | None
    providers: list[AssistantProviderOut] = Field(default_factory=list)
    confirm_deploy: bool
    limits: AssistantLimits


class AssistantSessionSummary(BaseModel):
    """provider 는 이 대화에 붙은 provider 이름이다. 고르지 않은 대화는 null 이고 설정의 기본으로
    돈다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    state: Literal["idle", "running", "pending"]
    provider: str | None = None
    updated_at: str


class AssistantSessionListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AssistantSessionSummary]


class AssistantSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)


class AssistantSessionCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str


class AssistantPendingCallOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    args: dict[str, Any]
    risk: str


class AssistantPendingOut(BaseModel):
    """승인 대기. `confirm` 의 calls[i].id 가 결정의 id 다. `question` 은 ask_user 하나이고 답은 새
    메시지로 온다."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirm", "question"]
    calls: list[AssistantPendingCallOut]


class AssistantProposalOut(BaseModel):
    """`GET /assistant/sessions/{id}/proposal` (P48 6.1절). 기다리는 제안을 저장해 둔 기준
    리비전(만들기는 없다) 위에 짜 맞춘 정의(graph·service)와 카드의 덩어리(proposal)다.
    latest_revision 은 Agent 의 지금 최신 리비전이라 기준과 다르면 그 제안은 저장되지 않는다.
    덩어리의 모양은 docs/guide/assistant.md 5절이 정본이다."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    kind: Literal["create", "update"]
    agent_id: str | None = None
    name: str
    display_name: str | None = None
    base_revision: int | None = None
    latest_revision: int | None = None
    graph: GraphOut
    service: ServiceSettingsOut
    proposal: dict[str, Any]


class AssistantUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    total_tokens: int
    # input_tokens 가운데 provider 가 캐시에서 읽은 몫. 단가가 낮아 비용을 셈할 때 따로 본다
    cached_input_tokens: int = 0


class AssistantSessionOut(BaseModel):
    """`GET /assistant/sessions/{id}`. transcript 항목은 UI ChatItem 과 같은 `{kind, ...}` 다.
    종류는 릴리스마다 늘 수 있어 자유 객체로 두고 UI 가 접는다 (P73 2절)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    state: Literal["idle", "running", "pending"]
    provider: str | None = None
    pending: AssistantPendingOut | None
    transcript: list[dict[str, Any]]
    turns: int
    usage: AssistantUsage
    updated_at: str


class AssistantUiResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=64)
    id: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=200)


class AssistantUiContext(BaseModel):
    """화면이 턴마다 보내는 문맥. 프롬프트에 "사용자는 지금 ... 을 보고 있다" 로 들어간다.
    `unsaved_edits` 는 Agent 캔버스에 저장하지 않은 변경이 있다는 표시다. 청지기는 캔버스의 편집을
    읽지 않으므로 제안하기 전에 그 변경을 저장하거나 버려 달라고 먼저 말한다 (P48 6.5절)."""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(max_length=512)
    resource: AssistantUiResource | None = None
    unsaved_edits: bool = False


class AssistantTurnBody(BaseModel):
    """provider 는 이 대화에 붙일 LLM provider 이름이다(D45). 첫 턴에 오면 세션이 기억하고 다음
    턴은 보내지 않아도 같은 provider 로 간다. 아무도 고르지 않으면 설정의 기본이다."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=200_000)
    provider: str | None = Field(default=None, max_length=64)
    context: AssistantUiContext | None = None


class AssistantDecision(BaseModel):
    """`proposal_digest` 는 Agent 정의 제안의 결정만 싣는다(P48 6.1절). 기다리는 호출의 인자
    `_proposal.digest` 를 되돌려 보내고, 없거나 다르면 409 proposal_changed 다.
    `canvas_saved` 는 사람이 캔버스에서 제 판을 저장한 뒤 그 제안 호출을 닫는 거절의 표시다(6.5절).
    카드의 거절이 아니므로 한 구간의 카드 거절 수에 들지 않고 모델에게 그렇게 말한다. 거절에만,
    지문과 함께만 실린다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["approve", "reject"]
    message: str | None = Field(default=None, max_length=2000)
    proposal_digest: str | None = Field(default=None, max_length=128)
    canvas_saved: bool | None = None

    @model_validator(mode="after")
    def _canvas_saved_is_a_proposal_rejection(self) -> AssistantDecision:
        if self.canvas_saved and (self.type != "reject" or self.proposal_digest is None):
            raise ValueError("canvas_saved closes a proposal call: type reject with its digest")
        return self


class AssistantDecisionsBody(BaseModel):
    """decisions 개수는 `pending.calls` 개수와 같아야 한다(422 decision_count_mismatch)."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[AssistantDecision] = Field(min_length=1)


__all__ = [
    "AssistantConfigOut",
    "AssistantDecision",
    "AssistantDecisionsBody",
    "AssistantLimits",
    "AssistantPendingCallOut",
    "AssistantPendingOut",
    "AssistantProposalOut",
    "AssistantProviderOut",
    "AssistantSessionCreate",
    "AssistantSessionCreated",
    "AssistantSessionListOut",
    "AssistantSessionOut",
    "AssistantSessionSummary",
    "AssistantTurnBody",
    "AssistantUiContext",
    "AssistantUiResource",
    "AssistantUsage",
]
