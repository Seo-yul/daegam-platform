"""Agent (D42). 캔버스 정의와 런타임 배포가 한 문서에 산다.

정의 쪽은 `spec`(graph·service)·`layout`·`revision`·`revisions` 다. 고칠 때마다 리비전이 늘고
이전 리비전은 `revisions[]` 에 불변 스냅샷으로 남는다. `layout` 은 리비전에 들어가지 않으므로
노드를 옮기기만 한 저장은 리비전을 만들지 않는다. 배포 쪽은 `state`·`deployed_revision`·
`cluster_id`·`k8s`·`replicas`·`resources`·`scale_to_zero`·`bindings`·`token_id`·`llm_secrets`·
`llm_env`·`config_digest`·`effective_config`·`runtime`·`overrides`·`last_phase` 다. `draft` 에서는
배포 쪽이 비어 있고 `exposure`·`replicas`·`resources`·`scale_to_zero` 만 늘 있다. 배포 설정이자
복제가 받는 값이기 때문이다. 쉬는 상태(`sleep`, P44), 배포 기록(`deploy_history`, P45)과 배포
게이트(`evaluation_gate`, P45)는 어느 쪽에도 들지 않고 자기 갱신만 쓴다.

옛 인스턴스 스토어의 프로토콜(`live`·`live_on_cluster`·`live_by_k8s_name`·
`live_binding_data_service`·`list`·`get`·`stale`)을 지키고, 그 메서드는 `draft` 를 뺀다. `draft` 는
`k8s`·`cluster_id` 가 없어 그 자리에 나오면 `hosts_in_use` 같은 소비자가 터진다. 화면 목록만
`include_draft=True` 로 전부 본다. 정의 쪽 질의(`referencing_*`)는 draft 도 본다.

미리 보기(P44b, D107)는 `preview` 를 가진 같은 컬렉션의 문서다. 스토어의 읽기는 미리 보기를 빼고,
배포 일을 하는 쪽은 `with_previews()` 가 주는 보기로 미리 보기까지 읽는다. 참조 세기와 쉬기 워커의
질의는 어느 보기든 미리 보기를 빼고, 리컨실·이름 검사·LLM 정리의 질의는 늘 넣는다. 질의마다의
선택은 tests/test_agent_preview_store.py 가 표로 지킨다.

K8s 라벨 `daegam.devops.ai.kr/id`, `agent_events.instance_id`, 장기 기억의 Postgres 접두·Redis 키가
`_id` 라 바뀌면 이미 저장된 기억을 영영 못 찾는다. 토큰의 scope 도 이 id 를 담는다.

모델은 `model` 노드다(D28).
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING

from daegam.store.auth_reapply import AUTH_FIELDS, ConsumerAuthState, DeployRender
from daegam.store.base import Document, UtcDatetime, drop_index_if_present, new_id, utcnow
from daegam.store.exposure import AgentExposure
from daegam.store.instances import DEPLOYED_ONLY, NOT_DRAFT, DeployEnd, MongoInstanceStore
from daegam.store.tenants import Binding

AGENTS = "agents"
# 사람마다 Agent 마다 미리 보기 하나를 지키는 인덱스 (P44b 5절, P45 9절). 열쇠에 평가 실행 id 가
# 들어 사람의 미리 보기(run 이 null)와 평가 미리 보기가 함께 열린다. 옛 이름의 인덱스는 시작마다
# 지운다. 그대로 두면 평가 미리 보기를 사람의 미리 보기 곁에서 거절하기 때문이다 (P45 16절)
PREVIEW_OWNER_INDEX = "uq_preview_owner_run"
OLD_PREVIEW_OWNER_INDEX = "uq_preview_owner"
# 미리 보기가 아닌 문서와 미리 보기만을 고르는 조건 (P44b 5절). `preview` 가 없거나 null 이면 앞의
# 것이다
NOT_PREVIEW: dict[str, Any] = {"preview": None}
PREVIEWS_ONLY: dict[str, Any] = {"preview.source": {"$exists": True}}
# 미리 보기의 자격이 그 끝 뒤로 더 사는 시간 (P44b 8절). 끝내기 워커가 지우는 사이에 도는 턴이
# 끊기지 않게 둔다
PREVIEW_CREDENTIAL_SLACK = timedelta(minutes=10)

AgentState = Literal["draft", "deploying", "deployed", "failed", "undeploying", "deleting"]
K8S_PREFIX = "agent"

NodeType = Literal["entry", "agent", "model", "tools", "memory", "guardrail", "knowledge"]
# 런타임에 종류를 돌아야 하는 곳(agent_graph 의 NODE_DATA_MODELS 완전성 단정)이 쓴다.
# 손으로 두 벌 적으면 갈라지므로 Literal 에서 뽑는다
NODE_TYPES: tuple[NodeType, ...] = get_args(NodeType)
EdgeKind = Literal["entry", "model", "tools", "delegate", "memory", "guardrail", "knowledge"]


def k8s_name_for(agent_name: str) -> str:
    """Deployment·Service 이름. 규칙 안의 name 이라 정규화 없이 접두어만 붙는다 (D24)."""
    return f"{K8S_PREFIX}-{agent_name}"


def token_secret_name_for(agent_id: str) -> str:
    return f"agent-inst-{agent_id}-token"


class GraphNode(BaseModel):
    """`data` 의 모양은 `type` 이 정한다(`services/agent_graph.py` 의 EntryData·AgentData·ModelData·
    ToolsData·MemoryData·GuardrailData·KnowledgeData). 모양이 틀리면 저장 검증이 `invalid_node_data`
    로 그 노드를 가리킨다."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: NodeType
    data: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: EdgeKind


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    def node(self, node_type: NodeType) -> GraphNode | None:
        """그 종류의 첫 노드다. 검증을 지난 그래프에서 entry 처럼 하나뿐인 종류를 읽을 때 쓴다."""
        return next((n for n in self.nodes if n.type == node_type), None)


class Budget(BaseModel):
    """토큰 예산. daily_tokens 는 공유 카운터가 필요하므로 켜면 memory 노드가 필수다."""

    model_config = ConfigDict(extra="forbid")

    session_tokens: int | None = Field(default=None, ge=1)
    daily_tokens: int | None = Field(default=None, ge=1)


class Observability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_bodies: bool = False


class ServiceSettings(BaseModel):
    """리소스 수준 서비스 설정. evaluation 은 예약이라 저장만 하고 compile 이 읽지 않는다."""

    model_config = ConfigDict(extra="forbid")

    budget: Budget = Field(default_factory=Budget)
    observability: Observability = Field(default_factory=Observability)
    evaluation: dict[str, Any] | None = None


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class AgentSpec(BaseModel):
    """리비전마다 스냅샷되는 부분. layout 은 여기 없다."""

    model_config = ConfigDict(extra="forbid")

    graph: Graph = Field(default_factory=Graph)
    service: ServiceSettings = Field(default_factory=ServiceSettings)


class AgentRevision(AgentSpec):
    revision: int
    created_at: UtcDatetime = Field(default_factory=utcnow)


class AgentK8s(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str  # agent-<name>. Deployment·Service 이름이고 Service 는 agent-<name>:8080
    namespace: str
    token_secret: str  # 인스턴스 토큰 Secret (provision 이 만든다)


class AgentRuntime(BaseModel):
    """런타임 이미지가 init 받기 때 스스로 알린 기록이다(D65).

    `image` 는 알린 이미지 참조, `features` 는 그 이미지가 아는 이름 가운데 플랫폼이 아는 것,
    `warnings` 는 그 받기가 남긴 상태 경고(`runtime_features_invalid` 등)다. 기록은 에이전트마다
    따로이고 스냅숏 이미지와 같은 이미지의 것만 쓴다."""

    model_config = ConfigDict(extra="forbid")

    image: str
    features: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reported_at: UtcDatetime = Field(default_factory=utcnow)


class McpTokenTable(BaseModel):
    """파드 템플릿 하나의 MCP Server 토큰 env 표 (P50 9절). envs 는 MCP Server id → env 이름이고
    env 이름은 그 Secret 키이기도 하다. digest 는 env 이름들의 지문이라 그 템플릿의 init 이 같은
    값을 보낸다(`k8s/templates/agent.py` `MCP_TOKEN_TABLE_ENV`)."""

    model_config = ConfigDict(extra="forbid")

    digest: str
    envs: dict[str, str] = Field(default_factory=dict)


class LlmTokenRecord(BaseModel):
    """이 Agent 에게 낸 LLM 게이트웨이 토큰 하나 (P38 5절). Secret 에 쓰기 전에 남기므로 겹친 발급도
    Secret 에 닿은 토큰을 모두 회수할 수 있다."""

    model_config = ConfigDict(extra="ignore")

    jti: str
    iat: UtcDatetime
    exp: UtcDatetime


class LlmCleanup(BaseModel):
    """롤아웃이 끝난 뒤에 할 일 (P38 6절). 옛 템플릿의 파드가 다시 떠도 옛 env 가 가리키는 Secret 을
    읽으므로 기다린다. revoke 는 회수할 jti, drop_keys 는 토큰 Secret 에서 뺄 키다. generation 은
    그 일을 만든 Deployment 의 세대이고 그 뒤의 롤아웃이 끝나야 한다. id 로 하나씩 빼므로 겹친
    쓰기가 남의 일을 지우지 않는다."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)

    revoke: list[str] = Field(default_factory=list)
    drop_keys: list[str] = Field(default_factory=list)
    # 게이트웨이 경로로 옮긴 뒤 지울 키 복사본 Secret 이름이다. 정리할 때 다시 직접 경로면 두고,
    # drop_keys 는 다시 게이트웨이 경로면 둔다. 옛 파드 대신 지금 템플릿이 그것을 읽기 때문이다
    drop_secrets: list[str] = Field(default_factory=list)
    generation: int | None = None
    since: UtcDatetime = Field(default_factory=utcnow)


LlmRouteName = Literal["direct", "gateway"]


class LlmRoute(BaseModel):
    """Agent 의 LLM 호출 경로 (P38 6절). provision 과 재적용이 결정을 적고 렌더는 이것만 읽는다.
    reason 은 직접 경로의 까닭이고, providers 는 게이트웨이 경로의 토큰이 여는 provider slug 다."""

    model_config = ConfigDict(extra="ignore")

    route: LlmRouteName = "direct"
    reason: str | None = None
    providers: list[str] = Field(default_factory=list)

    @property
    def gateway(self) -> bool:
        return self.route == "gateway"


class ToolPin(BaseModel):
    """배포 때 tools 노드 하나가 쓴 Tool 이다 (P39b 14절 5부, `services/tool_pins.py`). names 는
    그때의 이름이고 모르면 None 이다. effects 와 web 은 그 이름마다의 효과(`read`·`write`·`web`)와
    웹 종류(`fetch`·`search`)다. 뒤 판이 칸을 더해도 읽히게 모르는 칸은 버린다."""

    model_config = ConfigDict(extra="ignore")

    names: list[str] | None = None
    effects: dict[str, str] = Field(default_factory=dict)
    web: dict[str, str] = Field(default_factory=dict)


SleepState = Literal["awake", "draining", "asleep", "waking"]
# 0 으로 줄이기의 기록 (P44 9절). asleep 을 적을 때 pending 이고, 패치가 0 을 준 뒤 applied 다
ScaleMark = Literal["pending", "applied"]


class AgentSleep(BaseModel):
    """쉬는 Agent 의 상태 (P44 7절, D103). 자기 조건부 갱신만 쓰고(`MongoAgentStore.move_sleep`)
    배포 쪽 저장은 쓰지 않는다. 그래서 바뀌기 전에 읽은 문서로 하는 provision 이 이것을 되돌리지
    못한다. 칸이 없는 문서(2.0.0-rc1 과 그 전)는 awake 로 읽는다.

    since 는 마지막으로 바뀐 때이고 reason 은 그 까닭이다. activity 는 마지막 활동(턴, 결정, 다시
    생성, 실행 시작, 깨우기)이고 앞으로만 간다. 두 수는 잠들 때 런타임 `/metrics` 에서 읽어 둔
    것이라 쉬는 동안 상태와 목록이 런타임 없이 보인다. chat 은 같은 `/metrics` 의 받는
    첨부(D61)이고 잠들 때 읽지 못하면 앞 잠들기의 값을 둔다. 쉬는 동안과 깨우는 동안 상태가 이것을
    내어 화면이 첨부를 계속 받는다. scaled 와 generation 은 asleep 의 0 으로 줄이기가 적용됐는지와
    그 패치가 만든 Deployment 세대다 (9절)."""

    model_config = ConfigDict(extra="ignore")

    state: SleepState = "awake"
    since: UtcDatetime | None = None
    reason: str | None = None
    activity: UtcDatetime | None = None
    active_sessions: int | None = None
    pending_approvals: int | None = None
    chat: dict[str, Any] | None = None
    scaled: ScaleMark | None = None
    generation: int | None = None
    # 마지막 잠들기가 런타임에 세운 드레인 표시의 id (7절). 플랫폼의 `DELETE /drain` 이 이 id 를
    # 실어 그 표시만 지우므로, 늦게 닿은 지우기가 그 뒤 잠들기의 새 표시를 지우지 못한다
    mark: str | None = None


class AgentPreview(BaseModel):
    """미리 보기 문서의 표시 (P44b 5절, D107). source 는 원본 Agent 의 id 이고 source_name 은 그
    이름이다. 이름은 바꿀 수 없으므로(D24) 토큰이 싣는 원본의 서비스 주체를 여기서 정한다. owner 는
    시작한 사람의 sub, started_at 은 처음 시작한 때, ends_at 은 그 뒤 2시간이고 옮기기가 바꾸지
    않는다. run 은 평가 미리 보기의 평가 실행 id 이고 사람의 미리 보기는 None 이다(P45 9절). 만들
    때만 쓰고 저장 단위 어디에도 들지 않는다."""

    model_config = ConfigDict(extra="ignore")

    source: str
    source_name: str
    owner: str
    started_at: UtcDatetime
    ends_at: UtcDatetime
    run: str | None = None

    @property
    def credentials_end(self) -> datetime:
        """미리 보기가 받은 자격이 모두 끝나는 때 (P44b 8절). 서비스·Gateway·LLM 토큰이 이때
        만료하고 인스턴스 토큰은 이때부터 거절된다."""
        return self.ends_at + PREVIEW_CREDENTIAL_SLACK


# 배포 기록이 남기는 동작과 결과 (P45 6절). pending 은 아직 끝나지 않은 배포이고, 뒤 배포가 앞지른
# pending 줄은 배포되지 않은 것으로 센다. 끝난 값은 `store/instances.py` `DeployEnd` 다. 쓰는 쪽만
# 이 타입을 쓰고 저장된 줄은 글자로 읽는다. 뒤 판이 값을 더해도 이 판이 Agent 를 읽게 하려는 것이다
DeployAction = Literal["deploy", "redeploy", "rollback"]
DEPLOY_PENDING = "pending"
DEPLOYED: DeployEnd = "deployed"
DEPLOY_HISTORY = "deploy_history"
# 배포 기록이 남기는 마지막 줄 수
DEPLOY_HISTORY_LIMIT = 20


class DeployRecord(BaseModel):
    """배포 기록 한 줄 (P45 6절). 배포, 재배포, 되돌리기가 `deploying` 전이와 같은 조건부 갱신으로
    넣고(`Agent.push_deploy_record`), 그 배포가 끝나는 자리가 id 로 outcome 을 적는다. revision 은
    핀한 리비전, restored_from 은 되돌리기가 내용을 가져온 리비전, spec_digest 는 그 내용의 지문,
    image 는 스냅숏의 런타임 이미지다. evaluation_run 과 skipped_reason 은 배포 게이트가 채운다
    (12절). 뒤 판이 칸을 더해도 읽히게 모르는 칸은 버리고, action 과 outcome 은 뒤 판의 새 값도
    읽히게 글자로 둔다."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    action: str
    revision: int
    restored_from: int | None = None
    spec_digest: str
    image: str | None = None
    at: UtcDatetime = Field(default_factory=utcnow)
    by: str | None = None
    evaluation_run: str | None = None
    skipped_reason: str | None = None
    outcome: str = DEPLOY_PENDING


class EvaluationGate(BaseModel):
    """배포 게이트 (P45 12절). 리비전 밖의 설정이라 리비전이 끌 수 없다. set_id 는 게이트가 읽는
    평가 세트, min_pass_rate 는 지나야 할 비율, no_worse_cases 는 기준선보다 나빠진 사례를 막는가,
    max_age_days 는 실행과 기준선의 나이 한도(일)다. 모르는 칸은 버리고 상한은 보지 않는다. 상한은
    입력(`services/evaluation/inputs.py` `EvaluationGateIn`)이 본다. 뒤 판이 넓힌 값도 이 판이 Agent
    를 읽게 하려는 것이다."""

    model_config = ConfigDict(extra="ignore")

    set_id: str
    min_pass_rate: float
    no_worse_cases: bool = False
    max_age_days: int
    updated_by: str | None = None
    updated_at: UtcDatetime = Field(default_factory=utcnow)


EVALUATION_GATE = "evaluation_gate"


class Agent(Document, ConsumerAuthState):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    # 정의 쪽
    spec: AgentSpec
    layout: dict[str, Point] = Field(default_factory=dict)
    revision: int = 1
    revisions: list[AgentRevision] = Field(default_factory=list)
    # 배포 쪽. draft 에서는 비어 있다
    state: AgentState = "draft"
    deployed_revision: int | None = None
    cluster_id: str | None = None
    k8s: AgentK8s | None = None
    replicas: int = Field(default=1, ge=1, le=10)
    resources: dict[str, Any] | None = None
    # 쓰지 않으면 0 으로 줄여도 되는가 (P44 6절). replicas 처럼 배포 설정이라 해제가 남기고 복제가
    # 받는다. 끄지 않으면 켜져 있다
    scale_to_zero: bool = True
    # 쉬는 상태 (P44 7절). 자기 조건부 갱신만 쓰므로 배포 쪽 필드가 아니다
    sleep: AgentSleep = Field(default_factory=AgentSleep)
    # memory·knowledge 노드의 Database 바인딩. env_prefix DAEGAM_DB_<i> 로 파드에 들어간다
    bindings: list[Binding] = Field(default_factory=list)
    token_id: str | None = None  # agent_instance_tokens 문서 id
    llm_secrets: list[str] = Field(default_factory=list)  # provision 이 복사한 provider Secret 이름
    # provider 이름 → 파드의 키 env 이름 (P35 12절). provision 이 적용 바로 전에 남기고 렌더와 init
    # 때의 컴파일이 같은 표를 읽는다. API 응답에는 싣지 않는다
    llm_env: dict[str, str] = Field(default_factory=dict)
    # 외부 노출 설정 (D27, docs/design/resource-model.md 11절 파생). draft 에서도 늘 있다
    exposure: AgentExposure = Field(default_factory=AgentExposure)
    k8s_uid: str | None = None  # Deployment uid (Ingress·노출 NetworkPolicy 의 ownerReference)
    # 서비스 토큰 판. 파드 템플릿 어노테이션이 이 값을 담아 토큰이 바뀌면 파드가 굴러간다 (D41)
    svc_token_rev: str = "0"
    # 적용한 파드 템플릿의 토큰이 자기 audience 를 싣는 MCP Server 의 id (P50 9절)
    svc_token_servers: list[str] = Field(default_factory=list)
    # MCP Server 소스마다의 토큰 env 표 (P50 9절). 지금 템플릿의 표가 앞이고, 파드가 남았을 수
    # 있는 앞 템플릿들의 표가 뒤에 있을 수 있다. 파드의 init 이 자기 표의 지문을 보내면 init 때의
    # 컴파일이 그 표로 소스마다의 env 를 고른다. 비어 있으면 표가 생기기 전 문서라 모든 소스가 옛
    # 단일 토큰을 쓴다. API 응답에는 싣지 않는다
    mcp_token_tables: list[McpTokenTable] = Field(default_factory=list)
    # 이 Agent 에게 낸 LLM 게이트웨이 토큰과 롤아웃 뒤의 정리 (P38 5절·6절). API 응답에는 싣지
    # 않는다
    llm_tokens: list[LlmTokenRecord] = Field(default_factory=list)
    llm_cleanup: list[LlmCleanup] = Field(default_factory=list)
    # LLM 호출 경로와 그 까닭 (P38 6절). 렌더와 init 이 없는 컴파일이 읽는다
    llm_route: LlmRoute = Field(default_factory=LlmRoute)
    # 배포 때의 Tool 핀 (P39b 14절 5부, `services/tool_pins.py`). tools 노드 id 마다 그때 쓴 Tool
    # 이름과 효과다. tool_pins_revision 은 핀을 박은 리비전이다. 이 판은 읽지 않지만 2.0.0-rc1 이
    # deployed_revision 과 맞춰 보므로 그 판으로 되돌려도 핀이 남도록 계속 쓴다. API 응답에는 싣지
    # 않는다
    tool_pins: dict[str, ToolPin] = Field(default_factory=dict)
    tool_pins_revision: int | None = None
    # 파드가 뜰 때 읽는 입력(컴파일한 agent.json 과 복사한 provider 키)의 키 없는 지문 (P35 13절).
    # provision 이 남기고, 파드 템플릿 어노테이션은 이것을 인스턴스 토큰으로 건 판이라 같은
    # 리비전 재배포도 입력이 바뀌었으면 파드가 굴러간다. API 응답에는 싣지 않는다
    config_digest: str = ""
    effective_config: dict[str, Any] | None = None  # 배포 시점의 resolve_effective 스냅샷 (불변)
    # 런타임 이미지가 init 받기 때 알린 기능 기록 (D65). 배포 전 검사와 프록시 라우트가 읽는다
    runtime: AgentRuntime | None = None
    # 마지막 렌더가 TLS 끝점으로 붙인 Redis Database 의 id (P61). 평문 소비자 세기가 읽는다.
    # None 이면 이 칸 전에 렌더한 문서라 모두 평문이다
    redis_tls_bindings: list[str] | None = None
    # 마지막 렌더가 파드에 단 Redis CA 의 지문(`templates/common.py` `REDIS_CA_ANNOTATION`, P61).
    # 재적용이 CA 만 바뀐 판을 천천히 다시 렌더하려고 읽는다
    redis_ca: str | None = None
    # provision 이 남긴 턴 drain 결정(초, P78 5절). 렌더가 파드 env `DAEGAM_TURN_DRAIN_SECONDS` 와
    # grace 로 싣는다. None 이면 provision 전(draft)이라 둘 다 싣지 않는다. API 응답에는 싣지 않는다
    turn_drain_seconds: int | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    # 마지막으로 관찰한 Deployment phase (D35). status() 가 값이 바뀔 때만 적는다. 고르개 목록이
    # 클러스터를 왕복하지 않고 읽는 값이라 "지금" 이 아니라 "마지막으로 알려진" 상태다
    last_phase: str | None = None
    last_error: str | None = None
    last_spec_change: UtcDatetime = Field(default_factory=utcnow)
    # `deploying` 으로 들어간 시각. 화면이 여기서부터 흐른 시간을 재 진행 배너를 바꾼다 (D43 둘째).
    # `updated_at` 은 리컨실이 재시도마다 밀어 올려 경과 시간이 0 으로 돌아가므로 쓸 수 없다
    deploying_since: UtcDatetime | None = None
    # 배포가 도는 동안 돌 수 있는 렌더의 번호 (P53 3.5절). 턴 위임 게이트가 deploying·failed 문서를
    # 이것으로 읽는다. 해제가 비운다
    deploy_render: DeployRender | None = None
    # 미리 보기면 그 표시다 (P44b 5절). 만들 때만 쓰고, 미리 보기가 아닌 문서는 칸이 없다
    preview: AgentPreview | None = None
    # 배포 기록 (P45 6절). 마지막 20줄이고 자기 갱신(`$push`, 결과 적기)만 쓴다. 배포 쪽 필드가
    # 아니라서 해제가 남기고, 미리 보기에는 쌓이지 않는다
    deploy_history: list[DeployRecord] = Field(default_factory=list)
    # 배포 게이트 (P45 12절). 자기 갱신(`set_evaluation_gate`)만 쓴다. 미리 보기와 복제에는 없다
    evaluation_gate: EvaluationGate | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None  # 마지막으로 만들거나 고친 주체의 sub (감사)

    def to_doc(self) -> dict[str, Any]:
        """미리 보기가 아닌 문서에는 `preview` 칸을 두지 않는다. 그래서 "`preview` 를 가진 문서" 가
        곧 미리 보기다 (P44b 12절의 되돌리기)."""
        doc = super().to_doc()
        if doc.get("preview") is None:
            doc.pop("preview", None)
        return doc

    @property
    def evaluation_run(self) -> str | None:
        """평가 미리 보기면 그 평가 실행의 id 다 (P45 9절). 아니면 None 이다."""
        return self.preview.run if self.preview is not None else None

    @property
    def acting_id(self) -> str:
        """규칙과 토큰이 쓰는 id (P44b 5절). 미리 보기는 원본의 것이고 나머지는 자기 id 다. 토큰의
        `agent_id`, Tool 규칙의 Agent 절, 규칙이 숨기는 Tool 의 판정이 이것을 본다."""
        return self.preview.source if self.preview is not None else self.id

    @property
    def acting_k8s_name(self) -> str:
        """토큰의 서비스 주체 `svc:agent:<이름>` 이 싣는 이름 (P44b 9절). 미리 보기는 원본의
        Deployment 이름이다. Tool 규칙의 Agent 절이 주체와 id 를 함께 보기 때문이다."""
        if self.preview is not None:
            return k8s_name_for(self.preview.source_name)
        return self.k8s.name if self.k8s is not None else k8s_name_for(self.name)

    def get_revision(self, n: int) -> AgentRevision | None:
        for r in self.revisions:
            if r.revision == n:
                return r
        return None

    @property
    def current(self) -> AgentRevision:
        rev = self.get_revision(self.revision)
        assert rev is not None
        return rev

    @property
    def current_token_table(self) -> McpTokenTable | None:
        """지금 파드 템플릿의 토큰 env 표 (P50 9절). 표가 생기기 전 문서는 None 이다."""
        return self.mcp_token_tables[0] if self.mcp_token_tables else None

    @property
    def deployed(self) -> AgentRevision | None:
        """핀된 리비전. draft 면 None."""
        if self.deployed_revision is None:
            return None
        return self.get_revision(self.deployed_revision)

    @property
    def is_deployed(self) -> bool:
        """draft 가 아니다. 클러스터에 무엇인가 있거나 있으려 한다."""
        return self.state != "draft"

    @property
    def snapshot_image(self) -> str | None:
        """배포 스냅숏의 런타임 이미지 참조다. draft 면 None 이다."""
        images = (self.effective_config or {}).get("images") or {}
        return images.get("agent_image")

    def runtime_for(self, image: str | None) -> AgentRuntime | None:
        """그 이미지의 기록이다(D65). 기록이 없거나 다른 이미지의 것이면 None 이다. 같은 이미지를
        가르는 규칙은 여기 한 곳이다."""
        if self.runtime is None or self.runtime.image != image:
            return None
        return self.runtime

    @property
    def runtime_for_image(self) -> AgentRuntime | None:
        """스냅숏 이미지의 기록이다. 이미지를 바꾼 재배포 뒤의 옛 기록은 None 이다."""
        return self.runtime_for(self.snapshot_image)

    def push_deploy_record(self, record: DeployRecord) -> dict[str, Any]:
        """기록 한 줄을 메모리의 기록에 더하고 같은 줄을 저장할 `$push` 를 돌려준다. 상태 전이가 이
        `$push` 를 같은 조건부 갱신에 싣는다 (P45 6절)."""
        self.deploy_history = [*self.deploy_history, record][-DEPLOY_HISTORY_LIMIT:]
        return {DEPLOY_HISTORY: {"$each": [record.model_dump()], "$slice": -DEPLOY_HISTORY_LIMIT}}

    @property
    def open_deploy_record(self) -> DeployRecord | None:
        """결과를 기다리는 마지막 줄. 실패한 배포를 리컨실이 다시 몰면 그 줄이 다시 결과를 받는다.
        이미 배포된 줄이나 기록이 없는 문서(이 기능 전의 배포)는 None 이다."""
        last = self.deploy_history[-1] if self.deploy_history else None
        return last if last is not None and last.outcome != DEPLOYED else None

    def image_reports(self, name: str) -> bool:
        """스냅숏 이미지가 그 기능 이름을 알렸는가 (D65). 기록이 없으면 모르는 것이다."""
        record = self.runtime_for_image
        return record is not None and name in record.features


# 저장 단위. D42 가 문서를 정의 쪽과 배포 쪽으로 가른 것이 그대로 저장 단위다. 정의 저장은 정의 쪽
# 필드만, 배포 쪽 저장(상태 전이 포함)은 배포 쪽 필드만 $set 한다. 그래야 낡은 스냅샷을 든 저장이
# 남의 쪽을 되돌리지 못한다(PUT 이 도는 중인 배포를 draft 로, 스윕이 끝난 해제를 deployed 로).
# 공통 필드는 만들 때만 쓴다. updated_at 은 어느 쪽 저장이든 찍는다
# 미리 보기의 표시(`preview`, P44b 5절)도 만들 때만 쓴다. 그래서 배포 쪽 저장과 상태 전이가 쓰지
# 않고, 이 판을 모르는 옛 판의 저장도 지우지 못한다
SHARED_FIELDS: frozenset[str] = frozenset({"_id", "tenant", "name", "created_at", "preview"})
DEFINITION_FIELDS: frozenset[str] = frozenset(
    {"display_name", "spec", "layout", "revision", "revisions", "updated_by", "updated_at"}
)
DEPLOYMENT_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "deployed_revision",
        "cluster_id",
        "k8s",
        "replicas",
        "resources",
        "scale_to_zero",
        "bindings",
        "token_id",
        "llm_secrets",
        "llm_env",
        "exposure",
        "k8s_uid",
        "svc_token_rev",
        "svc_token_servers",
        "mcp_token_tables",
        "llm_tokens",
        "llm_cleanup",
        "llm_route",
        "tool_pins",
        "tool_pins_revision",
        "config_digest",
        "effective_config",
        "runtime",
        "redis_tls_bindings",
        "redis_ca",
        "turn_drain_seconds",
        "overrides",
        "last_phase",
        "last_error",
        "last_spec_change",
        "deploying_since",
        "deploy_render",
        "updated_at",
        *AUTH_FIELDS,  # 인증 재적용의 칸 (P32 8절)
    }
)
# init 받기가 알린 기록(D65). 배포 쪽 칸이지만 상태 전이는 쓰지 않는다. 요청이 시작할 때 읽은 문서로
# 전이하면 그 사이 새 파드의 init 이 적은 기록을 옛 값으로 덮기 때문이다. 쓰는 곳은 `record_runtime`
# (조건부)과 해제뿐이다
RUNTIME_RECORD = "runtime"
# LLM 게이트웨이 토큰의 기록과 정리 (P38 5절·6절). 상태 전이가 읽어 둔 문서로 통째로 쓰면 그 사이
# 낸 토큰의 기록이나 남은 정리를 지우므로, 이 둘은 jti·id 로 한 항목씩 `$push`·`$pull` 한다
LLM_TOKEN_FIELDS = frozenset({"llm_tokens", "llm_cleanup"})
TRANSITION_FIELDS: frozenset[str] = DEPLOYMENT_FIELDS - {RUNTIME_RECORD} - LLM_TOKEN_FIELDS
# 어느 저장 단위에도 들지 않고 자기 갱신만 쓰는 칸. 쉬는 상태(P44 7절), 배포 기록(P45 6절), 배포
# 게이트(P45 12절)다. 2.0.0-rc1 은 이 칸을 모르고 읽어 버리며, 배포 쪽 저장이 쓰지 않으므로 그 판의
# 저장도 되돌리지 못한다
OWN_UPDATE_FIELDS: frozenset[str] = frozenset({"sleep", DEPLOY_HISTORY, EVALUATION_GATE})
# 쉬는 상태를 옮길 수 있는 배포 상태 (P44 7절). draft 와 해제·삭제 중인 문서는 해제가 상태를
# 비우므로 옮기지 않는다
SLEEP_DOC_STATES: tuple[str, ...] = ("deploying", "deployed", "failed")
# 매일 깨우기의 까닭 (P44 11절). 이 까닭으로 깨어 있는 Agent 가 클러스터의 상한에 든다
HOUSEKEEPING = "housekeeping"
# 매일 깨우기가 상한에 드는 상태. 다시 잠들면 빠진다
HOUSEKEEPING_HELD: tuple[SleepState, ...] = ("waking", "awake", "draining")


def sleep_state_in(states: Collection[SleepState]) -> dict[str, Any]:
    """`sleep.state` 가 states 안인 조건. 칸이 없는 문서는 awake 로 읽는다."""
    values: list[str | None] = list(states)
    if "awake" in states:
        values.append(None)
    return {"sleep.state": {"$in": values}}


class MongoAgentStore(MongoInstanceStore[Agent]):
    """Agent 의 스토어. 세 종류가 함께 쓰는 질의는 `MongoInstanceStore` 에 있고 여기는 이 종류의
    질의와 인덱스만 둔다 (P71 2.3절).

    기본 보기는 미리 보기를 뺀다(P44b 5절). 화면, 참조, 실행, 규칙이 이 보기를 쥔다. 배포 일(클레임,
    다시 읽기, 재적용, 인스턴스 토큰의 콜백, 샌드박스, 이벤트 수집)은 `with_previews()` 의 보기를
    쥔다. 쓰기는 부르는 쪽이 이미 쥔 문서의 id 로 하므로 보기와 상관없다."""

    model = Agent
    collection = AGENTS
    transition_fields = TRANSITION_FIELDS

    hidden = NOT_PREVIEW

    def with_previews(self) -> MongoAgentStore:
        """같은 컬렉션을 읽되 미리 보기도 넣는 보기. 배포 일을 하는 쪽이 생성자에서 이것으로
        바꿔 쥔다."""
        if self.shows_hidden:
            return self
        view = copy.copy(self)
        view.shows_hidden = True
        return view

    async def init_schema(self) -> None:
        """세 종류가 같은 인덱스에 미리 보기의 고유 인덱스 하나를 더한다. 사람 하나가 Agent 하나에
        미리 보기 하나만 열고, 평가 실행 하나가 평가 미리 보기 하나를 연다(부분 고유 인덱스, 열쇠에
        실행 id). 미리 보기가 아닌 문서는 `preview.source` 가 없어 들지 않는다 (P44b 5절, 10절).
        새 인덱스를 만든 뒤 옛 이름의 인덱스를 지운다. 그 사이에도 고유 인덱스 하나가 늘 있다 (P45
        16절)."""
        await super().init_schema()
        await self.coll.create_index(
            [(f"preview.{key}", ASCENDING) for key in ("source", "owner", "run")],
            unique=True,
            name=PREVIEW_OWNER_INDEX,
            partialFilterExpression=PREVIEWS_ONLY,
        )
        await drop_index_if_present(self.coll, OLD_PREVIEW_OWNER_INDEX)

    async def add_llm_token(
        self, agent_id: str, record: LlmTokenRecord, *, now: datetime, states: Collection[str]
    ) -> bool:
        """LLM 토큰의 기록 하나를 더한다(P38 5절). 만료된 기록을 먼저 뺀다. 상태가 states 밖이면
        False 다."""
        await self.coll.update_one(
            {"_id": agent_id}, {"$pull": {"llm_tokens": {"exp": {"$lte": now}}}}
        )
        res = await self.coll.update_one(
            {"_id": agent_id, "state": {"$in": list(states)}},
            {"$push": {"llm_tokens": record.model_dump()}},
        )
        return res.modified_count == 1

    async def drop_llm_tokens(self, agent_id: str, jtis: Collection[str]) -> None:
        await self.coll.update_one(
            {"_id": agent_id}, {"$pull": {"llm_tokens": {"jti": {"$in": list(jtis)}}}}
        )

    async def add_llm_cleanup(
        self, agent_id: str, cleanup: LlmCleanup, *, states: Collection[str]
    ) -> bool:
        res = await self.coll.update_one(
            {"_id": agent_id, "state": {"$in": list(states)}},
            {"$push": {"llm_cleanup": cleanup.model_dump()}},
        )
        return res.modified_count == 1

    async def drop_llm_cleanup(self, agent_id: str, ids: Collection[str]) -> None:
        await self.coll.update_one(
            {"_id": agent_id}, {"$pull": {"llm_cleanup": {"id": {"$in": list(ids)}}}}
        )

    async def drop_llm_secrets(self, agent_id: str, names: Collection[str]) -> None:
        """정리가 지운 키 복사본의 이름을 뺀다 (P38 6절)."""
        await self.coll.update_one(
            {"_id": agent_id}, {"$pull": {"llm_secrets": {"$in": list(names)}}}
        )

    async def with_llm_cleanup(self) -> list[Agent]:
        """롤아웃 뒤의 정리가 남은 배포 쪽 문서 (P38 6절). 옮긴 미리 보기의 옛 토큰도 회수해야
        하므로 보기와 상관없이 모든 문서를 본다."""
        return await self._many({"llm_cleanup.0": {"$exists": True}, **NOT_DRAFT})

    async def set_evaluation_gate(
        self, tenant: str, agent_id: str, gate: EvaluationGate | None
    ) -> bool:
        """배포 게이트를 바꾸거나(None 이면) 지운다 (P45 12절). 미리 보기에는 쓰지 않는다. 그
        Agent 가 있으면 True 다."""
        query = {"_id": agent_id, "tenant": tenant, **NOT_PREVIEW}
        update = (
            {"$unset": {EVALUATION_GATE: ""}}
            if gate is None
            else {"$set": {EVALUATION_GATE: gate.model_dump()}}
        )
        res = await self.coll.update_one(query, update)
        return res.matched_count == 1

    async def end_deploy_record(self, agent_id: str, record_id: str, outcome: DeployEnd) -> bool:
        """배포 기록의 그 줄에 결과를 적는다 (P45 6절). 이미 deployed 인 줄은 두고, 줄이 20줄 밖으로
        밀려났으면 쓰지 않는다. 그 줄이 있고 deployed 가 아니었으면 True 다."""
        res = await self.coll.update_one(
            {
                "_id": agent_id,
                DEPLOY_HISTORY: {"$elemMatch": {"id": record_id, "outcome": {"$ne": DEPLOYED}}},
            },
            {"$set": {f"{DEPLOY_HISTORY}.$.outcome": outcome}},
        )
        return res.matched_count == 1

    async def record_runtime(self, agent_id: str, record: AgentRuntime) -> bool:
        """init 받기가 알린 기록을 적는다(D65). 스냅숏 이미지가 기록의 이미지와 같을 때만 쓰는
        조건부 갱신이라 롤링 중 옛 파드의 init 이나 그 사이 바뀐 스냅숏이 새 기록을 흐리지 않는다.
        쓰지 않았으면 False 다."""
        res = await self.coll.update_one(
            {"_id": agent_id, "effective_config.images.agent_image": record.image},
            {"$set": {"runtime": record.model_dump()}},
        )
        return res.matched_count == 1

    # -- 쉬는 상태 (P44 7절). 배포 쪽 저장과 따로 자기 조건부 갱신만 쓴다 ---------------------

    async def move_sleep(
        self,
        agent_id: str,
        expect: Collection[SleepState],
        fields: Mapping[str, Any],
        *,
        also: Mapping[str, Any] | None = None,
        touch: datetime | None = None,
    ) -> bool:
        """쉬는 상태를 옮긴다. 문서가 배포 쪽 상태이고(`SLEEP_DOC_STATES`) `sleep.state` 가 expect
        안이며 also 의 칸(sleep 의 칸 이름 → 읽은 값)이 그대로일 때만 fields(sleep 의 칸)를 쓴다.
        touch 는 같은 갱신에서 앞으로만 가는 활동이다(`$max`). 레플리카가 여럿이어도 한 쪽만 옮기고,
        맞지 않으면 False 다."""
        query: dict[str, Any] = {
            "_id": agent_id,
            "state": {"$in": list(SLEEP_DOC_STATES)},
            **sleep_state_in(expect),
            **{f"sleep.{name}": value for name, value in (also or {}).items()},
        }
        update: dict[str, Any] = {"$set": {f"sleep.{k}": v for k, v in fields.items()}}
        if touch is not None:
            update["$max"] = {"sleep.activity": touch}
        res = await self.coll.update_one(query, update)
        # 같은 값을 다시 써도 조건이 맞았으면 옮긴 것이다
        return res.matched_count == 1

    async def mark_activity(self, agent_id: str, at: datetime) -> None:
        """활동을 적는다. 앞으로만 가므로 늦게 닿은 쓰기가 새 활동을 되돌리지 않는다."""
        await self.coll.update_one({"_id": agent_id}, {"$max": {"sleep.activity": at}})

    async def start_window(self, agent_id: str, at: datetime) -> bool:
        """활동이 적힌 적 없는 문서의 창을 at 에서 시작한다 (P44 6절). 이미 있으면 False 다."""
        res = await self.coll.update_one(
            {"_id": agent_id, "sleep.activity": None}, {"$set": {"sleep.activity": at}}
        )
        return res.matched_count == 1

    async def clear_sleep(self, agent_id: str) -> None:
        """쉬는 상태를 지운다. 해제가 배포 쪽을 비울 때다 (P44 12절)."""
        await self.coll.update_one({"_id": agent_id}, {"$unset": {"sleep": ""}})

    async def sleep_of(self, agent_id: str) -> AgentSleep | None:
        """지금 쉬는 상태 하나만 읽는다. 문서가 없으면 None 이다."""
        doc = await self.coll.find_one(self._scoped({"_id": agent_id}), {"sleep": 1})
        if doc is None:
            return None
        return AgentSleep.model_validate(doc.get("sleep") or {})

    async def sleep_candidates(
        self, *, idle_before: datetime | None, stale_before: datetime, capability: str
    ) -> list[Agent]:
        """쉬기 워커가 볼 `deployed` 문서 (P44 7절·11절). 잠들었거나 깨는 중인 것 모두, 표시의
        기한이 지난 draining, 매일 깨우기로 깨어 있는 awake 모두다. 매일 깨우기의 awake 는 hold
        안에서도 잠들 수 없게 되면 까닭을 바꿔 클러스터의 몫을 놓아야 하므로 늘 든다.
        idle_before 가 있으면(창이 켜졌다) 스위치를 끄지 않고 노출하지 않았으며 런타임 기록이
        capability 를 알렸고 overrides 가 `spec.replicas` 를 정하지 않은 awake 가운데 활동이 그보다
        오래됐거나 없는 것도 든다. 다시 배포하기 전의 Agent 처럼 잠들 수 없는 것이 틱마다 들지
        않는다. 나머지 조건(memory 노드, 기록이 스냅숏 이미지의 것인가)은 워커가 문서로 본다.
        미리 보기는 잠들지 않으므로(P44b 7절) 보기와 상관없이 빠진다."""
        ors: list[dict[str, Any]] = [
            sleep_state_in(("asleep", "waking")),
            {"sleep.state": "draining", "sleep.since": {"$lt": stale_before}},
            {"sleep.state": "awake", "sleep.reason": HOUSEKEEPING},
        ]
        if idle_before is not None:
            ors.append(
                {
                    **sleep_state_in(("awake",)),
                    "scale_to_zero": {"$ne": False},
                    "exposure.enabled": {"$ne": True},
                    "runtime.features": capability,
                    "overrides.spec.replicas": {"$exists": False},
                    "$or": [{"sleep.activity": None}, {"sleep.activity": {"$lt": idle_before}}],
                }
            )
        return await self._many({"state": "deployed", "$or": ors, **NOT_PREVIEW})

    async def housekeeping_wakes(self, cluster_id: str, *, since_after: datetime) -> int:
        """그 클러스터에서 매일 깨우기로 깨어 있는 Agent 수 (P44 11절). since_after 뒤에 그 상태가
        된 것만 센다. 매일 깨우기 하나가 그보다 오래 이어질 수 없으므로, 까닭이 남은 문서가 몫을
        계속 쥐지 않는다. 미리 보기는 잠들지 않으므로 세지 않는다."""
        return await self.coll.count_documents(
            {
                "cluster_id": cluster_id,
                "sleep.reason": HOUSEKEEPING,
                "sleep.state": {"$in": list(HOUSEKEEPING_HELD)},
                "sleep.since": {"$gt": since_after},
                **NOT_PREVIEW,
            }
        )

    async def set_phase(self, agent_id: str, phase: str | None) -> None:
        """마지막으로 관찰한 phase 만 고친다(D35). 상태 조회처럼 읽은 지 오래된 객체로 불러도 그
        사이의 다른 쓰기를 덮지 않게 필드 하나만 `$set` 한다."""
        await self.coll.update_one({"_id": agent_id}, {"$set": {"last_phase": phase}})

    async def live_binding_data_service(
        self, ds_id: str, *, tenant: str | None = None, deployed_only: bool = False
    ) -> list[Agent]:
        """memory·knowledge 바인딩이 이 Data Service 를 가리키는 배포 쪽 문서. deployed_only 면
        리비전 이력과 overrides 를 읽지 않는다(`DEPLOYED_ONLY`)."""
        projection = DEPLOYED_ONLY if deployed_only else None
        query = self._bound_to("bindings.data_service_id", ds_id, tenant)
        return await self._many(query, projection=projection)

    async def _referencing(self, query: dict[str, Any]) -> list[Agent]:
        """참조 세기(D87)의 질의. 미리 보기는 다른 리소스의 삭제를 막지 않으므로 보기와 상관없이
        뺀다 (P44b 5절). 미리 보기는 쓰던 것이 사라지면 끝날 때까지 그 호출이 실패한다."""
        return await self._many({**query, **NOT_PREVIEW})

    async def referencing_data_service(self, ds_id: str) -> list[Agent]:
        """현재 리비전의 memory 노드가 이 Data Service 를 가리키는 문서. draft 도 든다."""
        return await self._referencing(
            {
                "spec.graph.nodes": {
                    "$elemMatch": {"type": "memory", "data.checkpointer.data_service_id": ds_id}
                }
            }
        )

    async def referencing_tool_source(self, kind: str, source_id: str) -> list[Agent]:
        """현재 리비전의 tools 노드가 이 도구 소스(`mcp_instance`·`gateway_instance`)를 가리키는
        문서. MCP Server·Gateway 의 해제·삭제 가드가 쓴다 (D42). draft 도 든다."""
        return await self._referencing(
            {
                "spec.graph.nodes": {
                    "$elemMatch": {
                        "type": "tools",
                        "data.source.kind": kind,
                        "data.source.id": source_id,
                    }
                }
            }
        )

    async def revisions_referencing_tool_source(self, kind: str, source_id: str) -> list[Agent]:
        """현재 리비전이나 어느 리비전이든 tools 노드가 이 도구 소스를 가리키는 문서. 배포가 핀한
        옛 리비전이 쓰는 소스를 찾을 때 쓴다. 어느 리비전이 붙드는지는 부르는 쪽이 가린다."""
        node = {
            "$elemMatch": {"type": "tools", "data.source.kind": kind, "data.source.id": source_id}
        }
        revision = {"$elemMatch": {"graph.nodes": node}}
        return await self._referencing(
            {"$or": [{"spec.graph.nodes": node}, {"revisions": revision}]}
        )

    async def referencing_knowledge_base(self, kb_id: str) -> list[Agent]:
        """현재 리비전의 knowledge 노드가 이 Knowledge Base 를 가리키는 문서 (D32 삭제 가드)."""
        return await self._referencing(
            {
                "spec.graph.nodes": {
                    "$elemMatch": {"type": "knowledge", "data.knowledge_base_id": kb_id}
                }
            }
        )

    async def revisions_referencing_knowledge_base(self, kb_id: str) -> list[Agent]:
        """어느 리비전이든 knowledge 노드가 이 Knowledge Base 를 가리키는 문서. 배포가 핀한 옛
        리비전을 찾을 때 쓴다."""
        return await self._referencing(
            {
                "revisions": {
                    "$elemMatch": {
                        "graph.nodes": {
                            "$elemMatch": {"type": "knowledge", "data.knowledge_base_id": kb_id}
                        }
                    }
                }
            }
        )

    async def referencing_skill(self, skill_id: str) -> list[Agent]:
        """어느 리비전이든 에이전트 노드가 이 Skill 을 붙인 문서 (D64). 현재 리비전과 배포가 핀한
        리비전 가운데 어느 쪽이 붙드는지는 부르는 쪽이 가린다."""
        node = {"$elemMatch": {"type": "agent", "data.skills.id": skill_id}}
        revision = {"$elemMatch": {"graph.nodes": node}}
        return await self._referencing(
            {"$or": [{"spec.graph.nodes": node}, {"revisions": revision}]}
        )

    # -- 미리 보기 (P44b). 보기와 상관없이 미리 보기만 읽는다 -------------------------------

    async def preview_of(self, tenant: str, source_id: str, owner: str) -> Agent | None:
        """그 사람이 그 Agent 에 연 미리 보기. 없으면 None 이다. 고유 인덱스가 하나만 두게 한다.
        평가 미리 보기는 찾지 않는다. 그래서 서랍과 미리 보기 라우트가 그것에 닿지 않는다 (P45
        9절)."""
        query = {
            "tenant": tenant,
            "preview.source": source_id,
            "preview.owner": owner,
            "preview.run": None,
        }
        return self._one(await self.coll.find_one(query))

    async def preview_of_run(self, tenant: str, source_id: str, run: str) -> Agent | None:
        """그 Agent 에 평가 실행 run 이 연 미리 보기 (P45 9절). 없으면 None 이다."""
        query = {"tenant": tenant, "preview.source": source_id, "preview.run": run}
        return self._one(await self.coll.find_one(query))

    async def previews_of(self, tenant: str, source_id: str) -> list[Agent]:
        """그 Agent 에 열린 미리 보기 모두. 처음 시작한 차례다."""
        return await self._many(
            {"tenant": tenant, "preview.source": source_id},
            [("preview.started_at", ASCENDING), ("_id", ASCENDING)],
        )

    async def previews_of_owner(self, owner: str, *, tenant: str | None = None) -> list[Agent]:
        """그 사람이 연 미리 보기. tenant 를 주면 그 테넌트의 것만이다."""
        query: dict[str, Any] = {"preview.owner": owner}
        if tenant is not None:
            query["tenant"] = tenant
        return await self._many(query)

    async def all_previews(self) -> list[Agent]:
        """모든 테넌트의 미리 보기. 끝내기 워커가 틱마다 읽는다. 테넌트마다 몇 개뿐이다."""
        return await self._many(dict(PREVIEWS_ONLY), [("_id", ASCENDING)])
