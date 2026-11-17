"""Agent 응답 (D42). 문서(store Agent)의 정의 쪽을 현재 리비전으로 펼치고 배포 쪽에
status(services.agents.status) 를 붙인 것이다. api/agents.py 의 _out 과 같은 필드다.

노드 `data` 는 API 에서 자유 객체다. 종류별 모양(entry·agent·model·tools·memory·guardrail·
knowledge)은 `services/agent_graph.py` 의 pydantic 모델이 정하고 validate 가 `invalid_node_data` 로
가리킨다. 모델은 `model` 노드이며 에이전트가 `model` 간선으로 붙는다(D28)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from daegam.api.schemas import REQUIRED_DEFAULTS, DeleteResponse
from daegam.api.schemas.mcp_servers import ExposureStatus, InstanceCondition
from daegam.services.agent_sleep import SleepIneligible
from daegam.services.chat_uploads import MAX_ATTACHMENTS_PER_TURN
from daegam.services.exposure import AgentExposureInput
from daegam.store.agent_runs import RunStatus
from daegam.store.agents import (
    AgentK8s,
    AgentRevision,
    AgentState,
    DeployRecord,
    EvaluationGate,
    Graph,
    Point,
    ServiceSettings,
    SleepState,
)
from daegam.store.approval_choices import Choice
from daegam.store.base import UtcDatetime
from daegam.store.exposure import AgentExposure
from daegam.store.tenants import Binding

# 세션 제목과 제목 검색 q 길이의 상한이다. 런타임 turn_record.py 의 SESSION_TITLE_MAX_CHARS 와 같은
# 값이다
SESSION_TITLE_MAX_CHARS = 100
# 정리하기 전 제목 본문의 상한이다. 런타임 turn_record.py 의 SESSION_TITLE_RAW_MAX_CHARS 와 같은
# 값이고, 공백과 제어 문자를 빼면 100자 안에 드는 글까지 받는다
SESSION_TITLE_RAW_MAX_CHARS = 1000
# 첨부 업로드 id 의 길이 상한이다. id 는 32자 hex 이고, 상한 안의 모르는 id 는 unknown_upload 다
UPLOAD_ID_MAX_CHARS = 64


# Tool 효과 (P39b 14절 4부, `services/tool_effects.py`)
ToolEffect = Literal["read", "write", "web"]
# Tool 을 내는 백엔드의 종류 (P43 2절, `services/tool_effects.py`). internal 은 우리 mcp-runtime,
# platform 은 웹 Tool, script 는 code mode Gateway 의 execute_tool_script(P56 6.5절), 나머지는
# 서버가 읽기 전용 여부를 알려 주지 않는 Tool 이다
ToolOrigin = Literal["internal", "platform", "image", "remote", "entry", "script"]


class AgentIssueOut(BaseModel):
    """validate 이슈 하나. node_id·edge_id 로 캔버스가 표식을 단다."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    level: Literal["error", "warning"]
    node_id: str | None
    edge_id: str | None
    # 문장에 끼울 값. `builtin_tool_name` 은 {"clashes": [{"agent": 에이전트 이름, "names": [...]}]}
    data: dict[str, Any] | None = None


class AgentValidateResponse(BaseModel):
    """`POST /agents/validate`. ok 는 오류가 없다는 뜻이고 경고는 있을 수 있다."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    issues: list[AgentIssueOut]
    # tools 노드 id → Tool 이름 → 효과 (P39b 14절 4부). 캔버스가 효과 배지와 바깥 내용 뒤의 승인
    # 표시를 단다
    tool_effects: dict[str, dict[str, ToolEffect]]
    # tools 노드 id → Tool 이름 → 백엔드 종류 (P43 2·4절). 종류를 모르는 이름은 없다. 캔버스가 바깥
    # 표시의 기본값과 읽기 전용 표시가 효과를 내는지를 단다
    tool_origins: dict[str, dict[str, ToolOrigin]] = Field(default_factory=dict)
    # tools 노드 id → 서버나 허브 항목의 읽기 전용 표시가 효과를 정하는 Tool 이름 (P52 4절). 우리
    # mcp-runtime 의 Tool 과 규칙이 있는 image 항목의 Tool 이다. 캔버스의 읽기 전용 표시는 이 Tool
    # 에서 바깥 내용 뒤의 승인 규칙에 세지 않는다
    tool_marked: dict[str, list[str]] = Field(default_factory=dict)
    # 검사가 바깥 내용 뒤의 승인 규칙(P43)으로 봤는가. 늘 참이다. 캔버스가 그 규칙의 표시를 이것으로
    # 가른다
    tool_safety: bool = True


class GraphOut(Graph):
    """저장된 그래프 그대로다."""

    model_config = REQUIRED_DEFAULTS


class ServiceSettingsOut(ServiceSettings):
    model_config = REQUIRED_DEFAULTS


class AgentK8sOut(AgentK8s):
    model_config = REQUIRED_DEFAULTS


class AgentExposureOut(AgentExposure):
    model_config = REQUIRED_DEFAULTS


class AgentRevisionOut(AgentRevision):
    """리비전 하나. spec_digest 는 그 내용의 지문이라(P45 6절) 배포 기록의 줄과 견주어 그 내용이
    언제 배포됐는지 찾는다."""

    model_config = REQUIRED_DEFAULTS

    # 저장 모델의 Graph 는 nodes·edges 가 선택이라 응답에서는 AgentOut 처럼 GraphOut 이다
    graph: GraphOut
    spec_digest: str


class DeployRecordOut(DeployRecord):
    """배포 기록 한 줄 (P45 6절)."""

    model_config = REQUIRED_DEFAULTS


class EvaluationGateOut(EvaluationGate):
    """배포 게이트 (P45 12절)."""

    model_config = REQUIRED_DEFAULTS


class SkipEvaluation(BaseModel):
    """배포 게이트를 건너뛰는 이유 (P45 12절). 배포 기록 줄이 이유와 사람을 남긴다."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class AgentTemplateOut(BaseModel):
    """`GET /agents/templates` 항목. 캔버스가 그대로 불러온다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    description: str
    graph: GraphOut
    layout: dict[str, Point]
    service: ServiceSettingsOut


class AgentRuntimeOut(BaseModel):
    """런타임 이미지가 init 받기 때 스스로 알린 기록이다(D65). 이 파드의 이미지 참조와 그 이미지가
    아는 기능·능력 이름 가운데 플랫폼이 아는 것이다. 화면은 `features` 로 런타임 능력에 기대는
    단추를 가른다."""

    model_config = ConfigDict(extra="forbid")

    image: str
    features: list[str]


class AgentChatLimitsOut(BaseModel):
    """런타임이 받는 첨부의 상한이다(D61). 한 턴의 첨부·이미지 수, 세션의 이미지 수, 이미지 하나와
    세션 합의 바이트다. 세션 합은 세션 저장소에 따라 다르다. `AgentChatOut` 안에 들어 같은 까닭으로
    모르는 칸을 버린다."""

    model_config = ConfigDict(extra="ignore")

    max_attachments_per_turn: int
    max_images_per_turn: int
    max_session_images: int
    max_image_bytes: int
    max_session_bytes: int


class AgentChatOut(BaseModel):
    """런타임 `GET /metrics` 의 `chat` 이다(D61). 문서 첨부는 메인 에이전트의 파일 Tool 이 켜져
    있어야 받고, 이미지는 메인 모델 프로필이 명시적으로 막지 않으면 받는다.

    이 모델은 플랫폼이 모양을 정한 상태(`AgentStatus`, forbid) 안에 든다. 모르는 칸을 거절하면
    런타임이 칸을 더할 때 상태 응답이 500 으로 깨지고, 그대로 넘기면 상태가 정한 모양을 벗어난다.
    그래서 이 모델과 `AgentChatLimitsOut` 만 모르는 칸을 버린다(ignore). 메트릭의 `chat` 도 이
    모델이라 상태와 같은 칸만 보인다."""

    model_config = ConfigDict(extra="ignore")

    documents: bool
    images: bool
    # 그래프에 fetch 가 있고 데이터 Tool 이 없어 첨부가 fetch 를 허용 목록으로 좁히는가(P39b 15절).
    # 채팅 입력이 파일을 붙일 때 알린다. 옛 런타임은 싣지 않는다
    attachments_limit_fetch: bool = False
    limits: AgentChatLimitsOut


class AgentSleepOut(BaseModel):
    """쉬는 상태 (P44 10절, 14절). state 는 awake·draining·asleep·waking 이고 since 는 마지막으로
    바뀐 때, reason 은 그 까닭이다. 잠들기는 idle·request·housekeeping, 깨우기는 turn·run·wake·
    deploy·ineligible·housekeeping 이다. 잠들기를 그만두거나 워커가 awake 로 돌린 까닭은 busy·
    no_answer·stale_drain·stray_pods·wake_timeout 이고, active 는 매일 깨우기의 hold 안에 활동이
    있어 창에 넘긴 것이다. ineligible 은 잠들 수 없는 첫 까닭이고 잠들 수 있으면 null 이다.
    ineligible_all 은 그 까닭을 표의 차례로 모두 둔 것이고 잠들 수 있으면 빈 목록이다. 배포
    대화상자가 자기 입력으로 앞의 까닭을 다시 가를 때 뒤의 까닭을 읽는다. window_seconds 는 이
    설치의 쉬는 창이고 0 이면 스스로 잠들지 않는다."""

    model_config = REQUIRED_DEFAULTS

    state: SleepState
    since: str | None
    reason: str | None
    ineligible: SleepIneligible | None
    ineligible_all: list[SleepIneligible]
    window_seconds: int


class AgentStatus(BaseModel):
    """배포 상태. phase 는 Deployment 조건(Pending·Rolling·Ready·Failed·Missing·Terminating)과 쉬는
    상태의 Sleeping·Waking 이다(P44 10절, 둘 다 런타임을 읽지 않는다). 세션·보류 수는 파드가
    준비됐을 때 런타임 /metrics 에서 읽는다. 그 전에는 null 이고, 잠든 Agent 는 잠들 때 남긴 수다.
    warnings 는 런타임 경고(no_shared_state)와 보류 세션이 있는데 리비전이 바뀐 경우, init 받기가
    남긴 기능 경고다. 노출을 켰으면 external_url(`<scheme>://<host>`, 경로 없음)과
    exposure(Ingress·TLS 준비 여부)가 있다. conditions 는 Deployment 가 낸 것이고 MCP Server 와 같은
    표로 보인다. runtime 은 스냅숏 이미지의 init 이 알린 기록이고 아직 알리지 않았으면 null 이다.
    greeting 은 핀된 리비전 입구의 인사말이라 화면이 첫 말 전의 말풍선으로 보이고, 런타임은 새
    세션의 첫 SSE 이벤트 `greeting` 으로 같은 글을 낸다(D65). chat 은 런타임 /metrics 의 받는
    첨부이고 세션 수와 같이 읽으므로 파드가 준비되기 전과 옛 런타임에서는 null 이다(D61)."""

    model_config = ConfigDict(extra="forbid")

    phase: str | None
    active_sessions: int | None
    pending_approvals: int | None
    chat_url: str | None
    external_url: str | None = None
    exposure: ExposureStatus | None = None
    message: str | None
    conditions: list[InstanceCondition] = []
    ready_replicas: int | None = None
    warnings: list[str] = []
    observed_at: str
    # 배포 쪽을 마지막으로 쓴 시각. 화면이 여기서부터 흐른 시간을 재 진행 배너를 바꾼다 (D43 둘째)
    since: str | None = None
    error: str | None
    # 닫힌 대화의 기록이 남는 날 수 (D46). 플랫폼 설정이 정하고 파드 env 로 내려간 값이다.
    # 화면이 세션 TTL 과 함께 읽어 안내문을 만든다. 0 이면 세션이 닫힐 때 기록도 지운다
    transcript_retention_days: int | None = None
    runtime: AgentRuntimeOut | None = None
    # 핀된 리비전 입구의 인사말 (D65). 정의를 고쳐 저장해도 재배포 전에는 도는 리비전의 값이다
    greeting: str | None = None
    # 핀된 리비전이 장기 기억을 쓰는가와 도구 결과나 답을 가려 인용 원문을 열지 않는가다(P36).
    # 인사말처럼 재배포 전에는 도는 리비전의 값이다
    long_term_memory: bool = False
    sources_masked: bool = False
    chat: AgentChatOut | None = None
    # 쉬는 상태 (P44 10절). 배포 쪽이 있는 문서는 늘 있다
    sleep: AgentSleepOut | None = None


class AgentLlmRouteOut(BaseModel):
    """LLM 호출 경로와 직접 경로의 까닭 (P38 6절). 배포가 정한 값이고 draft 는 direct 다."""

    model_config = REQUIRED_DEFAULTS

    route: Literal["direct", "gateway"]
    reason: str | None


class AgentOut(BaseModel):
    """정의 쪽은 현재 리비전을 펼친 것이고 revisions 는 번호 목록이다. 배포 쪽은 `draft` 에서
    비어 있고 status 도 None 이다. `revision` 과 `deployed_revision` 의 차이가 "r5 저장됨,
    r2 배포됨" 이다."""

    model_config = REQUIRED_DEFAULTS

    id: str
    tenant: str
    name: str
    display_name: str
    state: AgentState
    revision: int
    deployed_revision: int | None
    graph: GraphOut
    service: ServiceSettingsOut
    layout: dict[str, Point]
    revisions: list[int]
    cluster_id: str | None
    k8s: AgentK8sOut | None
    replicas: int
    resources: dict[str, Any] | None
    # 쓰지 않으면 0 으로 줄여도 되는가 (P44 6절). draft 에서도 배포 설정으로 있다
    scale_to_zero: bool
    bindings: list[Binding]
    token_id: str | None
    llm_secrets: list[str]
    llm_route: AgentLlmRouteOut
    exposure: AgentExposureOut
    k8s_uid: str | None
    svc_token_rev: str
    effective_config: dict[str, Any] | None
    overrides: dict[str, Any]
    last_phase: str | None
    last_error: str | None
    last_spec_change: str
    # 배포, 재배포, 되돌리기의 마지막 20줄 (P45 6절). 오래된 것이 앞이다. 해제가 남기고 미리 보기는
    # 비어 있다
    deploy_history: list[DeployRecordOut]
    # 배포 게이트 (P45 12절). 없으면 꺼져 있다
    evaluation_gate: EvaluationGateOut | None
    status: AgentStatus | None
    created_at: str
    updated_at: str
    updated_by: str | None


class AgentSaved(AgentOut):
    """POST·PUT·clone 응답. issues 는 저장을 막지 않은 경고이고 revision_created 는 리비전이
    늘었는가다."""

    issues: list[AgentIssueOut]
    revision_created: bool


class AgentDeployed(AgentOut):
    """deploy·redeploy 응답. warnings 는 그래프 경고, 클러스터 preflight 경고, 재배포 때의 보류
    세션 경고다."""

    warnings: list[str]


class AgentSummary(BaseModel):
    """고르개 목록의 항목 하나다(D35 F2). 클러스터·런타임을 왕복하지 않는다.

    `GET /agents` 는 배포된 것마다 Deployment 조회와 런타임 `/metrics` 를 왕복하므로 첫 화면의 첫
    요청으로 쓸 수 없다. 여기 `phase` 는 지금 상태가 아니라 상태 조회가 마지막으로 본 값이고, 한
    번도 조회한 적이 없으면 null 이다. 배포된 것만 든다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    display_name: str
    phase: str | None


class AgentDeleteResponse(DeleteResponse):
    """삭제 응답. draft 였으면 k8s 는 None 이고 회수한 토큰도 없다."""

    state_before: AgentState
    revoked_tokens: int
    k8s: AgentK8sOut | None


class AgentMemoriesDeleteResponse(BaseModel):
    """`DELETE /agents/{id}/memories/{user_sub}` 응답 (D34).

    `deleted` 는 지운 항목 수다. 장기 기억이 꺼져 있거나 남긴 것이 없으면 0 이고 그래도 200 이다
    (지우려던 상태가 됐기 때문이다)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    deleted: int


class AgentMemoryFileRow(BaseModel):
    """런타임 `GET /memories` 의 파일 한 줄이다 (P36 8절). `path` 는 에이전트가 보는 경로
    (`/memories/prefs.md`)이고 `size` 는 내용의 UTF-8 바이트다. 시각은 ISO 8601 이다."""

    model_config = ConfigDict(extra="allow")

    path: str
    size: int
    created_at: str
    modified_at: str


class AgentMemoriesOut(BaseModel):
    """런타임 `GET /memories` 응답 그대로. 호출자 자신의 장기 기억 파일이고 경로순으로 1000개까지다.
    `retention_days` 가 null 이면 만료가 없다."""

    model_config = ConfigDict(extra="allow")

    enabled: bool
    retention_days: int | None = None
    files: list[AgentMemoryFileRow]


class AgentMemoryFileOut(BaseModel):
    """런타임 `GET /memories/files/{key}` 응답 그대로. `encoding` 은 `utf-8` 이거나 `base64` 다."""

    model_config = ConfigDict(extra="allow")

    path: str
    encoding: str
    content: str


class AgentMemoryFileDeleteOut(BaseModel):
    """런타임 `DELETE /memories/files/{key}` 응답 그대로. 그 파일 하나만 지워졌다."""

    model_config = ConfigDict(extra="allow")

    path: str
    deleted: bool


class AgentChatBody(BaseModel):
    """`POST /agents/{id}/chat`. 세션 소유자는 호출자다(user_sub 는 플랫폼이 넣는다).

    `ephemeral` 은 이 턴에 장기 기억 저장소를 안 붙이라는 표시다(D34). 만드는 사람이 던져 보는
    테스트 채팅이 켜고, 장기 기억이 꺼진 정의에는 아무 뜻이 없다.

    `attachments` 는 `POST /agents/{id}/uploads` 가 돌려준 업로드 id 다(P36 9절). 한 턴에
    다섯까지이고 겹치면 422 다. 플랫폼이 업로드를 런타임 모양으로 바꿔 싣는다.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    message: str = Field(min_length=1)
    ephemeral: bool = False
    attachments: list[Annotated[str, Field(min_length=1, max_length=UPLOAD_ID_MAX_CHARS)]] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS_PER_TURN
    )

    @field_validator("attachments")
    @classmethod
    def _distinct_attachments(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("the same upload is attached twice")
        return ids


class AgentRegenerateBody(BaseModel):
    """`POST /agents/{id}/sessions/{sid}/regenerate`. 마지막 턴을 다시 만든다. `message` 가 있으면
    마지막 질문을 그 글로 바꿔 보낸다(고쳐 보내기). `ephemeral` 은 `/chat` 과 같다."""

    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, min_length=1)
    ephemeral: bool = False


class AgentDecisionsBody(BaseModel):
    """`POST /agents/{id}/sessions/{sid}/decisions`. decisions 개수는 인터럽트의 action_requests
    개수와 같아야 한다(런타임이 검사한다). `ephemeral` 은 `/chat` 과 같다. 결정마다
    `remember`(`approve` 또는 `reject`)를 두면 세션 소유자가 그 Tool 의 선택을 기억한다(P43 5절).
    approve 는 approve 결정과만, reject 는 reject 결정과만 짝이고 런타임에는 보내지 않는다."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[dict[str, Any]] = Field(min_length=1)
    ephemeral: bool = False


class AgentSessionUsageOut(BaseModel):
    """세션 한 줄의 누적 토큰이다. 합은 화면이 더한다."""

    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    output_tokens: int = 0


class SessionRunOut(BaseModel):
    """세션 목록 줄에 붙는 그 세션의 백그라운드 실행 (P41 7절). 마감은 승인을 기다릴 때만 있다."""

    id: str
    status: RunStatus
    deadline: UtcDatetime | None = None


class AgentSessionRow(BaseModel):
    """런타임 세션 색인 한 줄(`SessionRecord.to_dict`)이다. 목록·상세·이름 바꾸기가 같은 모양이다.

    `title` 은 새 세션의 첫 메시지 첫 줄이거나 소유자가 바꾼 이름이고, 옛 세션은 null 이다(P36
    3절). 시각은 ISO 8601 이다."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    user_sub: str
    tenant: str | None = None
    instance_id: str | None = None
    title: str | None = None
    created_at: str
    last_turn_at: str
    turns: int = 0
    usage: AgentSessionUsageOut
    status: Literal["active", "pending", "closed"]
    pending_since: str | None = None
    # 원장의 금액 합 (P37 12절). 목록에서만 붙고 원장에 행이 없으면 null 이다
    cost_usd: float | None = None
    # 목록에 `runs` 를 주면 붙는 그 세션의 실행. 실행 세션이 아니면 null 이다 (P41 7절)
    run: SessionRunOut | None = None
    # 턴이 도는가(살아 있는 리스, P78 2절)와 그 시도의 id. 중지가 그 시도를 이름으로 멈춘다.
    # `detached_turns` 를 모르는 옛 런타임은 보내지 않으므로 null 이고, 돌지 않는 것과 같다
    running: bool | None = None
    turn_id: str | None = None


class AgentSessionsOut(BaseModel):
    """런타임 `GET /sessions` 응답 그대로. `q` 는 런타임이 받은 제목 검색어를 되돌려준 값이고
    검색이 없으면 없다."""

    model_config = ConfigDict(extra="allow")

    sessions: list[AgentSessionRow]
    q: str | None = None


class AgentSessionOut(BaseModel):
    """런타임 `GET /sessions/{sid}` 응답 그대로 (색인 한 줄, 메시지 요약, 보류 여부). 가벼운
    읽기(`view=progress`, P78 2절)는 메시지 요약이 비고, 보류는 턴이 돌지 않을 때만 참이다."""

    model_config = ConfigDict(extra="allow")

    session: AgentSessionRow
    messages: list[dict[str, Any]] = []
    pending: bool = False
    # 화면용 전사 `{items, limit}` (D35 F1). 런타임이 쓰는 모양이라 자유 객체로 두고
    # UI 가 접는다 (P73 2절). 오래된 런타임은 보내지 않는다.
    transcript: dict[str, Any] | None = None
    # 가벼운 읽기에서만 있다. 화면이 진행 중 턴을 다시 읽는 간격(초)이고 런타임에 바로 닿으면 3,
    # API 서버의 터널을 거치면 10 이다 (P78 4절)
    poll_seconds: int | None = None


class AgentPendingOut(BaseModel):
    """런타임 `GET /sessions/{sid}/pending` 응답 그대로 (보류 승인 요청). `remember_options` 와
    `remember_days` 는 플랫폼이 더한다. 요청마다(action_requests 와 같은 순서) 세션 소유자가
    기억할 수 있는 선택과 그 선택이 남는 날 수이고, 소유자가 아니거나 이미지가 모르면 null
    이다(P43 5절)."""

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    action_requests: list[dict[str, Any]] = []
    review_configs: list[dict[str, Any]] = []
    remember_options: list[list[Choice]] | None = None
    remember_days: int | None = None


class AgentSessionRenameBody(BaseModel):
    """`PATCH /agents/{id}/sessions/{sid}`. 제목 규칙(공백·제어 문자를 뺀 1~100자)은 런타임이
    본다(422 invalid_title)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=SESSION_TITLE_RAW_MAX_CHARS)


class AgentSessionPatchOut(BaseModel):
    """런타임 `PATCH /sessions/{sid}` 응답 그대로. 이름을 바꾼 세션 한 줄이다."""

    model_config = ConfigDict(extra="allow")

    session: AgentSessionRow


class AgentSessionDeleteOut(BaseModel):
    """런타임 `DELETE /sessions/{sid}` 응답 그대로. 체크포인트·전사·색인 행이 지워졌다."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    deleted: bool


class AgentSessionFileRow(BaseModel):
    """런타임 `GET /sessions/{sid}/files` 의 파일 한 줄이다 (P36 10절). `origin` 은 경로가 가른다.
    `upload` 는 `/uploads/` 아래의 업로드한 파일, `workspace` 는 코드 실행 Agent 의 작업 공간
    `/workspace/` 아래 파일(P40 6절), `agent` 는 에이전트가 파일 Tool 로 쓴 파일이다. `size` 는
    바이트이고 `modified_at` 은 ISO 8601 이며 시각이 없는 에이전트 파일은 null 이다."""

    model_config = ConfigDict(extra="allow")

    path: str
    size: int
    modified_at: str | None = None
    origin: Literal["upload", "agent", "workspace"]


class AgentSessionFilesOut(BaseModel):
    """런타임 `GET /sessions/{sid}/files` 응답 그대로. 업로드한 파일을 넣은 차례로, 에이전트 파일을
    경로순으로 500개까지 싣는다."""

    model_config = ConfigDict(extra="allow")

    files: list[AgentSessionFileRow]


class AgentCancelBody(BaseModel):
    """`POST /agents/{id}/sessions/{sid}/cancel` 의 본문이다. 없어도 된다. turn_id 를 주면 그 시도일
    때만 멈춘다(P78 2절). 묵은 화면에서 누른 중단이 다음 턴을 멈추지 않게 한다."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentCancelOut(BaseModel):
    """런타임 `POST /sessions/{sid}/cancel` 의 202 응답 그대로(D65). 멈춘 턴은 스트림이
    `error{code: turn_cancelled}` 로 끝난다."""

    model_config = ConfigDict(extra="allow")

    session_id: str


class DeployRequest(BaseModel):
    """`POST /agents/{id}/deploy`. 정의는 문서에 있으므로 클러스터 쪽만 받는다. 보내지 않은 값은
    문서에 남은 설정(복제가 받은 것)을 쓴다."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str | None = None
    namespace: str | None = None  # 배포 네임스페이스 (허용 목록 안). 기본은 테넌트 기본 (D22)
    replicas: int | None = Field(default=None, ge=1, le=10)
    resources: dict[str, Any] | None = None
    exposure: AgentExposureInput | None = None  # 외부 노출 (D27, oidc 모드 전용)
    # 쓰지 않으면 0 으로 줄여도 되는가 (P44 6절). 보내지 않으면 문서의 설정이고 처음에는 켜져 있다
    scale_to_zero: bool | None = None
    # 배포 게이트를 건너뛰는 이유 (P45 12절)
    skip_evaluation: SkipEvaluation | None = None


class RedeployRequest(BaseModel):
    """`POST /agents/{id}/redeploy`. 보내지 않은 값은 저장된 스냅샷을 유지한다."""

    model_config = ConfigDict(extra="forbid")

    replicas: int | None = Field(default=None, ge=1, le=10)
    resources: dict[str, Any] | None = None
    exposure: AgentExposureInput | None = None
    # 쓰지 않으면 0 으로 줄여도 되는가 (P44 6절). 보내지 않으면 저장된 설정을 유지한다
    scale_to_zero: bool | None = None
    skip_evaluation: SkipEvaluation | None = None


class RollbackRequest(BaseModel):
    """`POST /agents/{id}/rollback` (P45 6절). revision 은 내용을 가져올 리비전이다."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    skip_evaluation: SkipEvaluation | None = None


class CloneRequest(BaseModel):
    """`POST /agents/{id}/clone`. 정의를 베낀 draft 를 만든다. exposure 를 주면 배포 설정으로
    남아 노출용 둘째가 한 호출로 만들어진다 (D42)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    exposure: AgentExposureInput | None = None


AgentJson = dict[str, Any]
