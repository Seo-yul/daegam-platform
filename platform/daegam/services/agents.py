"""Agent 서비스 (D42). 캔버스 정의(저장·리비전·복제·검증)와 런타임 배포(배포·재배포·해제·삭제·
상태·편집)가 한 문서 위에서 돈다. 배포된 런타임을 대신 부르는 프록시는 `agent_runtime.py` 다.

배포는 네임스페이스, LLM 경로(P38 6절, `agent_llm.py`), 인스턴스 토큰 Secret, 장기 기억 스키마를
차례로 거쳐 Deployment·Service·NetworkPolicy 를 SSA 로 적용한다. 모든 단계가 결정적 이름이라
재실행이 안전하고, 중간 상태에 멈춘 문서는 리컨실이 다시 몰아간다. 상태 여섯 가운데 동작이 되는
상태는 플랜의 상태 표를 따른다.

상태를 바꾸는 저장은 스토어의 조건부 갱신(`transition`)이라 두 요청이 겹치면 하나는 409
state_conflict 다. 상태를 바꾸지 않는 저장은 쓸 필드를 인자로 받는다(`_save_deployment`). 배포 쪽을
통째로 쓰면 상태가 같은 채 겹치는 저장끼리 서로를 되돌린다(스윕의 토큰 회전이 방금 켠 노출을,
노출 저장이 방금 저장한 overrides 를 덮는 식이다). `record_failure` 가 `failed` 로 옮기는 것은 배포
경로(`deploying`)에서만이고, 해제·삭제 도중의 실패는 그 상태에 `last_error` 로 남는다.

클러스터를 만지는 동작(토큰 회전·노출 편집·overrides·drift·기억 삭제)은 시작에서 문서를 다시 읽고
(`_fresh`) 상태를 본다. 조건부 저장은 문서를 지키지만 CR·Deployment 는 이미 한 번 적용된 뒤이기
때문이다.

장기 기억(D34)은 플랫폼이 저장소에 직접 붙는 유일한 자리다. 배포가 Postgres 전용 스키마
`daegam_store` 를 파드보다 먼저 만들고, 삭제의 마지막 단계가 그 문서의 기억을 지우며(실패는 경고),
`delete_memories` 가 사용자 한 사람 것을 지운다. **배포 해제는 기억을 남긴다.** 같은 id 로 다시
배포되기 때문이다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import anyio
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.config.resolve import EffectiveConfig, ProfileRuntime, resolve_effective
from daegam.error_codes import status_of
from daegam.errors import ServiceError, revision_conflict
from daegam.k8s.client import KubeClient
from daegam.k8s.merge import AGENT_LOCKED_PATHS
from daegam.k8s.templates import (
    RenderError,
    policy_name_for,
    render_agent_deployment,
    render_agent_expose_docs,
    render_agent_policy,
    render_agent_service,
)
from daegam.k8s.templates.agent import (
    MCP_TOKEN_TABLE_HEADER,
    TOKEN_KEY,
    IdpVerifier,
    config_digest,
    db_env_prefix,
    fit_turn_drain,
    idp_allowed_groups,
    llm_env_for,
    platform_allowed_groups,
)
from daegam.k8s.templates.common import (
    NETWORK_POLICY_API,
    REDIS_CA_ANNOTATION,
    redis_ca_mark,
    template_annotation,
    tls_endpoint,
)
from daegam.k8s.templates.svc_token import secret_value
from daegam.services import (
    agent_calls,
    agent_features,
    agent_graph,
    agent_tool_safety,
    agent_web,
)
from daegam.services.advanced_edit import AdvancedEditor
from daegam.services.agent_code_mode import attach_code_mode, code_mode_issues
from daegam.services.agent_features import (
    DETACHED_TURNS,
    REDIS_TLS,
    RUNTIME_FEATURE_MISSING,
    RUNTIME_IMAGE_TOO_OLD,
    TWO_ISSUERS,
)
from daegam.services.agent_feedback import AgentFeedbackService
from daegam.services.agent_graph import (
    AGENT_SOURCE,
    Analysis,
    CompileError,
    GraphContext,
    GraphError,
    Issue,
    MemoryData,
    ToolsData,
    analyze_structure,
    errors_of,
    issue_lines,
)
from daegam.services.agent_llm import AgentLlm
from daegam.services.agent_memory import AgentMemories
from daegam.services.agent_pod_inputs import POD_INPUT_RECORDS, AgentPodInputs
from daegam.services.agent_publish import CALLS_BROKEN_BY_DEPLOY, AgentPublishing
from daegam.services.agent_rollback import (
    AgentRollbackMixin,
    deploy_record_push,
    record_deploy_end,
)
from daegam.services.agent_runtime import TURN_STOP_SUB, AgentRuntimeProxy
from daegam.services.agent_skills import AgentSkillStore
from daegam.services.agent_sleep import AgentSleepControl
from daegam.services.agent_status import (
    HIDDEN_REISSUE,
    RUNTIME_TOOL_REFUSALS_WARNING,
    AgentStatusView,
    hidden_tool_warnings,
    hidden_tools,
    refusals_unknown,
    uses_ruled_tools,
)
from daegam.services.agent_tokens import AgentTokens, asking_envs, current_envs
from daegam.services.approval_choices import ApprovalChoiceService
from daegam.services.claims import Claims, checkpoint
from daegam.services.clusters import ClusterService
from daegam.services.data_plane_auth import (
    KIND_AGENT,
    ConsumerAuth,
    idp_alone_rev,
    llm_rev,
    named_rev,
    redis_tls_rev,
)
from daegam.services.data_services import (
    ClusterMismatch,
    check_same_cluster,
)
from daegam.services.evaluation.holds import EVALUATION_TOOLS_UNKNOWN, ToolsUnknown, hold_writes
from daegam.services.evaluation.outcome import GateOutcome
from daegam.services.exposure import (
    AgentExposureInput,
    Exposure,
    ExposureInput,
    ExposureValidationMixin,
    agent_exposure_from,
    agent_exposure_to_input,
    delete_expose_objects,
    expose_object_refs,
    exposure_from_input,
)
from daegam.services.hub import HubService
from daegam.services.instance_base import (
    EDIT_STATES,
    IN_PROGRESS,
    BaseInstanceService,
    delete_labeled,
)
from daegam.services.redis_tls_consumers import (
    RecentReads,
    stored_effective,
    tls_redis_bindings,
)
from daegam.services.sandboxes import SandboxService
from daegam.services.svc_tokens import SvcTokens
from daegam.services.tenancy import TenancyService
from daegam.services.tool_effects import (
    GraphTools,
    SourceTools,
    ToolEffectLoader,
    graph_tools,
    source_ids,
)
from daegam.services.tool_pins import (
    REDEPLOY_REQUIRED,
    ToolPins,
    apply_pins,
    builtin_name_issues,
    marks_drift,
    pin_drift,
    pins_of,
    unpinned_sources,
    valid_pins,
)
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, scope_for
from daegam.store.agent_runs import MongoAgentRunStore
from daegam.store.agents import (
    DEFINITION_FIELDS,
    DEPLOYMENT_FIELDS,
    RUNTIME_RECORD,
    Agent,
    AgentK8s,
    AgentRevision,
    AgentSleep,
    AgentSpec,
    MongoAgentStore,
    Point,
    k8s_name_for,
    token_secret_name_for,
)
from daegam.store.base import to_millis, utcnow
from daegam.store.clusters import Cluster
from daegam.store.data_services import DataService, MongoDataServiceStore
from daegam.store.exposure import AgentExposure
from daegam.store.exposure_hosts import MongoExposureHostStore
from daegam.store.gateways import MongoGatewayStore
from daegam.store.hub import HubItem
from daegam.store.instances import DeployEnd
from daegam.store.knowledge import KnowledgeBase, MongoKnowledgeStore
from daegam.store.mcp_servers import MongoMCPServerStore
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore
from daegam.store.skills import MongoSkillStore
from daegam.store.tenants import AgentOAuth, Binding, MongoTenantSettingsStore

logger = logging.getLogger("daegam.agents")

# 배포 해제와 삭제의 API 경로가 남은 턴이 없어질 때까지 다시 묻는 간격과 상한(초)이다(P78 5절).
# 런타임에 실어 보내는 주체는 `agent_runtime.TURN_STOP_SUB` 다
TURN_STOP_POLL_SECONDS = 1.0
TURN_STOP_WAIT_SECONDS = 15.0


class AgentRunHooks(Protocol):
    """배포·해제·삭제가 부르는 백그라운드 실행의 일 (P41, `services/runs/service.py`
    `RunService`)."""

    async def on_deploy(
        self, agent: Agent, deployer: Principal, previous: AgentRevision | None
    ) -> list[Issue]: ...

    async def end_agent_runs(self, tenant: str, agent_id: str, *, deleting: bool) -> object: ...

    async def forget_agent(self, tenant: str, agent_id: str) -> object: ...

    def check_revision(self, revision: AgentRevision, message: str | None) -> None: ...


class AgentEvaluationHooks(Protocol):
    """Agent 를 지울 때 그 평가 세트와 사례를 지운다 (P45 7절)."""

    async def forget_agent(self, tenant: str, agent_id: str) -> object: ...


class AgentGateHooks(Protocol):
    """배포 게이트의 판정 (P45 12절, `services/evaluation/gate.py` `DeployGate`)."""

    async def judge(
        self, agent: Agent, content: AgentSpec, *, skip: str | None = None
    ) -> GateOutcome: ...


class AgentPreviewHooks(Protocol):
    """원본을 지울 때 그 미리 보기를 끝내는 일 (P44b 10절, `services/agent_preview.py`
    `AgentPreviews`)."""

    async def end_for_source(self, source: Agent) -> object: ...


class AgentError(ServiceError):
    """Agent 서비스 오류. 앱의 전역 핸들러가 응답으로 낸다."""

    @classmethod
    def from_graph(cls, e: GraphError) -> AgentError:
        d = e.to_dict()
        return cls(e.status, d.pop("code"), d.pop("detail"), **d)


@dataclass(frozen=True)
class GraphReview:
    """저장 없는 검사(`POST /validate`)의 결과다. tools 는 tools 노드마다 Tool 이름의 효과와 백엔드
    종류다. 캔버스가 이것으로 효과 배지, 승인 표시, 바깥 표시를 단다. calls 는 게시한 Agent 를
    Tool 로 쓰는 tools 노드마다 그 Agent 다(`agent_calls.AgentSource.view`, P46 6.2절)."""

    issues: list[Issue]
    tools: GraphTools
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _Context:
    """렌더·배포가 보는 문맥."""

    cluster: Cluster
    rev: AgentRevision
    effective: EffectiveConfig
    providers: list[LLMProvider]  # 그래프가 쓰는 provider (메인·폴백·서브·임베딩 순, 중복 없음)
    # 노출 검증기와 Gateway 소스의 인증 입력. 소비자 판과 같은 함수에서 온다 (P32 8절)
    auth: ConsumerAuth
    # agent.json knowledge[] 의 (순서 n, 임베딩 provider). 항목마다 DAEGAM_KNOWLEDGE_<n>_API_KEY 다
    knowledge_providers: list[tuple[int, LLMProvider]] = field(default_factory=list)
    data_services: dict[str, DataService] = field(default_factory=dict)
    agent_oauth: AgentOAuth | None = None
    gateway_token: bool = False  # local 모드에서 Gateway 소스를 쓴다 (플랫폼이 낸 토큰, D41)
    # 설정 지문을 거는 인스턴스 토큰 평문 (P35 13절). 적용하는 렌더(provision·재적용·Advanced
    # 편집)만 채우고 확인용 렌더는 비워 둬 지문이 "0" 이다
    instance_token: str | None = None


async def _best_effort(doc: Agent, what: str, step: Awaitable[object]) -> None:
    """문서를 이미 바꾼 뒤의 정리 단계다. 실패를 올리면 화면은 500 을 받고 다시 해도 같은 자리에
    오지 못하므로 경고로 남기고 지나간다. 남은 것은 각자의 기한이 걷는다."""
    try:
        await step
    except PyMongoError as e:
        logger.warning("agent %s: %s: %s", doc.id, what, e)


async def _analyze(
    spec: AgentSpec, ctx: GraphContext, *, calls_enabled: bool = True
) -> tuple[Analysis, list[Issue]]:
    """그래프 검증에 웹 Tool 의 규칙(`agent_web.web_issues`), 바깥 표시의 경고
    (`agent_tool_safety.safety_issues`), 내장 Tool 과 이름이 같은 Tool 의 경고
    (`tool_pins.builtin_name_issues`), code mode 스크립트의 선택과 중복
    (`agent_code_mode.code_mode_issues`), 게시한 Agent 를 Tool 로 쓰는 노드의 규칙
    (`agent_calls.call_issues`)을 더한 (분석, 이슈)다. 저장 앞의 검증과 배포·재배포가 같은 것을
    본다. calls_enabled 는 이 플랫폼에서 Agent 호출이 켜져 있는가다."""
    analysis = await run_in_threadpool(agent_graph.analyze, spec, ctx)
    web = await run_in_threadpool(agent_web.web_issues, spec, ctx)
    safety = await run_in_threadpool(agent_tool_safety.safety_issues, spec, ctx)
    builtin = builtin_name_issues(spec, ctx)
    code_mode = await run_in_threadpool(code_mode_issues, spec, ctx)
    calls = agent_calls.call_issues(spec, ctx, enabled=calls_enabled)
    return analysis, [*analysis.issues, *web, *safety, *builtin, *code_mode, *calls]


class AgentService(ExposureValidationMixin, AgentRollbackMixin, BaseInstanceService[Agent]):
    # Tool 규칙이 이름 댄 Agent 를 묻는 곳 (P52 3절). wiring 이 만든 뒤에 단다. 없으면 아무도
    # 이름 대지 않는다
    rule_scopes: Any = None
    late_hooks: ClassVar[frozenset[str]] = frozenset({"rule_scopes"})
    error_cls = AgentError
    profile_kind = "agent"
    claim_kind = "agent"
    auth_kind = KIND_AGENT
    # 소비자를 다시 렌더하며 인스턴스 토큰을 다시 낼 수 있다
    # 다시 렌더하기가 새로 낼 수 있는 인스턴스 토큰과, 토큰 env 표(P50 9절), TLS 로 붙인
    # Redis(P61)를 함께 저장한다
    consumer_fields = frozenset(
        {"token_id", "mcp_token_tables", "redis_tls_bindings", "redis_ca", "llm_route"}
    )
    definition_fields = DEFINITION_FIELDS
    deployment_fields = DEPLOYMENT_FIELDS
    k8s_api = "apps/v1"
    # 내리거나 지우는 중이면 클러스터를 읽지 않고 Terminating 이다
    terminating_states = frozenset({"deleting", "undeploying"})
    k8s_kind = "Deployment"
    kept_settings = frozenset({"exposure", "replicas", "resources", "scale_to_zero"})
    records_deploy_render = True
    edit_outside = "not_deployed"  # 배포된 것만 받는 동작이 대부분이라 밖의 409 코드가 다르다
    require_oidc_exposure = True  # header 모드 노출은 422 다 (D27)

    def __init__(
        self,
        agents: MongoAgentStore,
        tokens: MongoAgentInstanceTokenStore,
        platform_settings: MongoPlatformSettingsStore,
        tenant_settings: MongoTenantSettingsStore,
        gateways: MongoGatewayStore,
        mcp_servers: MongoMCPServerStore,
        data_services: MongoDataServiceStore,
        knowledge: MongoKnowledgeStore,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        settings: Settings,
        *,
        svc_tokens: SvcTokens,
        feedback: AgentFeedbackService,
        skills: MongoSkillStore,
        claims: Claims,
        exposure_hosts: MongoExposureHostStore,
        tool_effects: ToolEffectLoader,
        hub: HubService,
        run_hooks: AgentRunHooks | None = None,
        choices: ApprovalChoiceService | None = None,
        sandboxes: SandboxService | None = None,
        runs: MongoAgentRunStore | None = None,
        previews: AgentPreviewHooks | None = None,
        evaluations: AgentEvaluationHooks | None = None,
        gate: AgentGateHooks | None = None,
    ):
        # 배포 일을 하므로 미리 보기까지 읽는 보기를 쥔다 (P44b 5절). 클레임, 다시 읽기, 재적용,
        # 이름 검사가 미리 보기를 보아야 그 문서를 배포하고 지운다. 아래 도우미도 이 보기를 받는다.
        # 화면이나 테넌트 목록을 읽는 메서드를 여기 더하면 미리 보기가 섞이므로 그 읽기는 라우트가
        # 쥔 기본 보기(`app.state.agents`)로 한다
        agents = agents.with_previews()
        self.agents = agents
        # BaseInstanceService 가 쓰는 이름. 합친 스토어가 옛 인스턴스 스토어 프로토콜을 지킨다
        self.instances = agents
        self.tokens = tokens
        self.platform_settings = platform_settings
        self.tenant_settings = tenant_settings
        self.gateway_instances = gateways
        self.mcp_instances = mcp_servers
        self.data_services = data_services
        # 재적용 틱이 Agent 마다 바인딩한 Database 를 본다. `_redis_ca` 가 쓴다
        self._recent = RecentReads()
        self.knowledge = knowledge
        # 그래프가 핀한 Skill 리비전(D64). 검증 문맥, init 의 Skill 받기, 상태 경고가 읽는다
        self.skills = AgentSkillStore(skills)
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        self.settings = settings
        # 런타임이 MCP Server·Gateway 를 부를 서비스 토큰의 발급 창구 (D41). wiring 이 준다
        self.svc_tokens = svc_tokens
        # 답변 피드백 (P36 6절). 삭제의 마지막 단계가 그 Agent 의 평가를 지운다
        self.feedback = feedback
        # 문서 하나의 클러스터 작업을 묶는 클레임 (P66)
        self.claims = claims
        # 공개 host 의 선점 기록 (P70). 노출을 저장하기 전에 host 를 잡는다
        self.exposure_hosts = exposure_hosts
        # 도구 소스의 Tool 과 효과 (P39b 14절 4부). 검증·컴파일 문맥이 미리 읽는다
        self.tool_effects = tool_effects
        # 해제와 삭제가 그 Agent 의 열린 실행을 끝내고, 삭제는 스케줄도 지운다 (P41 3·7절)
        self.run_hooks = run_hooks
        # 사람이 기억한 승인 선택 (P43 5절). 배포가 바꾼 규칙의 선택을 걷고 삭제가 모두 지운다
        self.choices = choices
        # 코드 실행 샌드박스 (P40 5절). 해제와 삭제가 그 Agent 의 샌드박스를 닫는다
        self.sandboxes = sandboxes
        # 원본을 지우면 그 미리 보기를 끝낸다 (P44b 10절)
        self.previews = previews
        # 삭제가 그 Agent 의 평가 세트와 사례를 지운다 (P45 7절)
        self.evaluations = evaluations
        # 배포, 재배포, 되돌리기가 새 내용을 판정받는다 (P45 12절). 없으면 게이트가 없다
        self.gate = gate
        # Advanced 편집 (F7a 공통 모듈). 편집 대상은 Deployment 하나이고 Service·NetworkPolicy·
        # 노출 객체는 전부 잠긴다. 오퍼레이터가 덮어쓰는 경로는 없다
        self.editor: AdvancedEditor[Agent] = AdvancedEditor(
            kind="Deployment",
            render=self.render,
            client=self.client_for_doc,
            save=self._save_overrides,
            manifest=self.manifest,
            locked=lambda _doc: AGENT_LOCKED_PATHS,
            ignored=(),
            error_cls=AgentError,
            finish=fit_turn_drain,
        )
        # 런타임 프록시 (D21). 상태 가드·클러스터 클라이언트·토큰 Secret 읽기는 이 서비스의 것이다
        self.proxy = AgentRuntimeProxy(
            client=self.client_for_doc,
            read_token=lambda doc, client: self.agent_tokens.secret_token(doc, client),
            require=self._require,
            k8s=self._k8s,
            error_cls=AgentError,
        )
        # 쓰지 않는 Agent 의 잠들기와 깨우기 (P44, D103). 프록시가 잠든 Agent 의 읽기와 드레인
        # 거절에서, 상태 조회가 phase 와 남긴 수에서 부른다. runs 는 잠들기 앞에 열린 실행을 센다
        self.sleep = AgentSleepControl(
            agents=agents,
            settings=settings,
            proxy=self.proxy,
            claimed=self.claimed_scale,
            error_cls=AgentError,
            runs=runs,
        )
        self.proxy.sleep = self.sleep
        # 허브 게시 (P46 6.1절). 게시한 Agent 는 다른 Agent 의 Tool 이 되고, 호출은 실행이라
        # 전제 조건을 실행 서비스가 본다
        self.publishing = AgentPublishing(
            hub=hub,
            settings=settings,
            check_revision=run_hooks.check_revision if run_hooks is not None else None,
            require_serving=lambda doc: self.proxy.require_deployed(doc, action="publish"),
            error_cls=AgentError,
        )
        # 게시한 Agent 를 Tool 로 쓰는 tools 노드가 가리키는 항목을 읽는다 (P46 6.2절)
        self.agent_sources = agent_calls.AgentSourceLoader(
            hub=hub, agents=agents, publishing=self.publishing
        )
        # 인스턴스 토큰과 그 Secret, 파드 입력 (P72 2.1절)
        self.agent_tokens = AgentTokens(
            tokens=tokens,
            agents=agents,
            svc_tokens=svc_tokens,
            k8s=self._k8s,
            pinned=self._pinned,
            client_for_doc=self.client_for_doc,
            save_deployment=self._save_deployment,
            forget_token=self.proxy.forget_token,
            live_states=self.live_states,
            error_cls=AgentError,
            named=self.named_by_rules,
        )
        self.pod_inputs = AgentPodInputs(
            cluster_service=cluster_service,
            settings=settings,
            k8s=self._k8s,
            save_deployment=self._save_deployment,
            error_cls=AgentError,
        )
        # provider 해소와 LLM 경로 (P38 6절). 게이트웨이 상태 문서는 배선이 붙인다
        self.llm = AgentLlm(
            settings=settings,
            platform_settings=platform_settings,
            agents=agents,
            cluster_service=cluster_service,
            svc_tokens=svc_tokens,
            pod_inputs=self.pod_inputs,
            knowledge_bases=self.knowledge_bases,
            error_cls=AgentError,
        )
        self.agent_tokens.llm_wanted = self.llm.wanted
        # 장기 기억 저장소 (D34, P72 2.1절)
        self.memories = AgentMemories(data_services, tenancy, cluster_service, error_cls=AgentError)
        # 상태 훅 (P72 2.1절). 틀은 InstanceStatusMixin 이다
        self.status_view = AgentStatusView(
            settings=settings,
            skills=self.skills,
            agents=agents,
            proxy=self.proxy,
            client_for_doc=self.client_for_doc,
            with_pod_waiting=self._with_pod_waiting,
            pin_warnings=self._tool_warnings,
            sandbox_warnings=self._sandbox_warnings,
            error_cls=AgentError,
            sleep=self.sleep,
        )

    async def _sandbox_warnings(self, doc: Agent) -> list[str]:
        """코드 실행을 켠 Agent 의 게이트와 개발용 우회 경고다 (P40 8절)."""
        return await self.sandboxes.gate_warnings(doc) if self.sandboxes is not None else []

    @property
    def token_auth(self) -> bool:
        return self.settings.token_auth

    def _memory_revision(self, doc: Agent) -> AgentRevision | None:
        """장기 기억이 사는 Database 를 정한 리비전. 배포가 핀한 것이 있으면 그것이고, 핀이 풀렸으면
        (해제를 지난 문서) 현재 리비전이다. 핀이 가리키는 리비전이 사라졌으면 None 이다. 그때 현재
        리비전으로 떨어지면 memory 노드가 그 사이 다른 Database 를 가리키게 됐을 때 남의 기억을
        지운다."""
        if doc.deployed_revision is None:
            return doc.current
        return doc.get_revision(doc.deployed_revision)

    # -- 그래프 문맥·검증 -------------------------------------------------------------

    async def context(
        self,
        tenant: str,
        spec: AgentSpec,
        *,
        principal: Principal | None = None,
        in_pod_state: bool = False,
        agent_id: str | None = None,
    ) -> GraphContext:
        """테넌트의 검증·컴파일 문맥. provider 키 유무는 has_key 가 부를 때 Secret 을 읽는다(동기
        K8s 호출이라 validate·compile 은 스레드풀에서 돈다). Skill 은 spec 이 핀한 리비전의 메타만,
        도구 소스는 지금 광고하는 Tool 과 효과를 미리 읽는다(`AgentSkillStore.pinned`,
        `ToolEffectLoader`). in_pod_state 는 상태를 파드 안에 두는 미리 보기의 문맥이다 (P44b
        7절). agent_id 는 그 정의를 가진 Agent 이고, Tool 로 쓰는 게시한 Agent 가 그것 자신이거나
        그것을 다시 부르는지를 가린다 (`AgentSourceLoader.load`). 새 Agent 는 None 이다."""
        ps = await self.platform_settings.get()
        ts = await self.tenant_settings.get(tenant)
        # draft 도 사전에 넣는다. 빠지면 "모르는 소스" 라는 틀린 진단이 나온다. 배포 안 된 것은
        # 검증이 source_not_deployed 로 가른다 (D42). Tool 효과는 spec 이 가리키는 소스만 읽는다
        gateways = {i.id: i for i in await self.gateway_instances.list(tenant, include_draft=True)}
        servers = {i.id: i for i in await self.mcp_instances.list(tenant, include_draft=True)}
        ids = source_ids(spec)
        called = await self.agent_sources.load(tenant, spec, principal=principal, agent_id=agent_id)
        source_tools = await self.tool_effects.load(
            {k: v for k, v in gateways.items() if k in ids},
            {k: v for k, v in servers.items() if k in ids},
        )
        return GraphContext(
            providers=ps.providers_by_name,
            has_key=self.llm.has_key_checker(),
            token_auth=self.token_auth,
            auth_mode=self.settings.auth_mode,
            trusts_platform=self.settings.data_plane_trusts_platform,
            tenant_settings=ts,
            gateway_instances=gateways,
            mcp_instances=servers,
            data_services={d.id: d for d in await self.data_services.list(tenant)},
            knowledge_bases=await self.knowledge_bases(tenant),
            skill_revisions=await self.skills.pinned(tenant, spec, principal),
            source_tools={**source_tools, **agent_calls.source_tools(called)},
            agent_sources=called,
            web_tools=ps.web_tools,
            in_pod_state=in_pod_state,
        )

    async def _doc_context(
        self, doc: Agent, rev: AgentRevision, *, principal: Principal | None = None
    ) -> GraphContext:
        """문서 하나를 배포하거나 컴파일하는 문맥이다. 미리 보기면 상태를 파드 안에 두고
        (`in_pod_state`), 그 리비전이 원본에서 메인에 memory 노드를 가졌으면 파드 안에서 승인을
        묻는다(`in_pod_approvals`, P44b 7절)."""
        ctx = await self.context(
            doc.tenant,
            rev,
            principal=principal,
            in_pod_state=doc.preview is not None,
            agent_id=doc.acting_id,
        )
        ctx.in_pod_approvals = await self._preview_asks(doc, rev.revision)
        return ctx

    async def _preview_asks(self, doc: Agent, revision: int) -> bool:
        """미리 보기가 파드 안에서 승인을 묻는가 (P44b 7절). 미리 보기의 정의에서는 memory 노드가
        빠지므로 원본의 같은 번호 리비전을 본다. 그 메인에 memory 노드가 있으면 배포한 원본이 묻는
        자리이고, 없으면 거절하는 자리라 미리 보기도 거절한다. 원본이나 그 리비전이 없으면 묻지
        않는다. 미리 보기가 아니면 거짓이다. 평가 미리 보기는 늘 묻는다. 잡은 쓰기 Tool 을 승인에서
        `respond` 로 답하기 때문이다 (P45 10절)."""
        if doc.preview is None:
            return False
        if doc.evaluation_run is not None:
            return True
        source = await self.agents.get(doc.preview.source)
        original = source.get_revision(revision) if source is not None else None
        return original is not None and analyze_structure(original).main_has_memory

    async def _pin_warnings(self, doc: Agent) -> list[str]:
        """핀과 소스의 지금 이름이 갈린 tools 노드(P39b 14절 5부)와 배포 뒤 소스의 읽기 전용 표시가
        바뀐 tools 노드(P52 4절)의 상태 경고다. 런타임 이미지가 거절된 Tool 호출을 견디지 못하는데
        규칙이 있는 Tool 을 쓰면 그 경고도 낸다(P52 5절). 토큰 인증 모드면 규칙이 이 Agent 에게
        숨기는 Tool 의 경고도 낸다(P52 7절). 셋 다 볼 것이 없으면 소스를 읽지 않는다."""
        rev = doc.deployed
        pins = valid_pins(doc)
        refusals = refusals_unknown(doc)
        if rev is None or not (pins or refusals or self.token_auth):
            return []
        sources = await self.source_tools_for(rev)
        out = pin_drift(rev, pins, sources) + marks_drift(rev, pins, sources) if pins else []
        if refusals and uses_ruled_tools(rev, sources):
            out.append(RUNTIME_TOOL_REFUSALS_WARNING)
        if self.token_auth:
            out.extend(await self._hidden_tool_warnings(doc, rev, sources))
        return out

    async def _tool_warnings(self, doc: Agent) -> list[str]:
        """Tool 핀의 상태 경고에 Tool 로 쓰는 게시한 Agent 의 경고를 더한 것이다 (P46 6.8절,
        `agent_calls.status_warnings`)."""
        return await self._pin_warnings(doc) + await self.agent_sources.status_warnings(doc)

    async def _hidden_tool_warnings(
        self, doc: Agent, rev: AgentSpec, sources: Mapping[str, SourceTools]
    ) -> list[str]:
        """규칙이 이 Agent 가 고른 Tool 을 숨기는 원인마다의 경고다(`agent_tool_hidden`, P52 7절).
        모든 규칙이 이 Agent 를 받아도 그 Agent 의 다음 렌더가 자기 id 를 실은 토큰을 아직 내지
        않았으면 그 Tool 도 숨는다(3절). 마지막 렌더가 이름 표시 없는 판이면
        그렇다고 본다. 다른 까닭(Redis CA, 렌더 번호)으로 판이 다른 것은 토큰 재발급이 아니므로
        세지 않는다."""
        hidden, admitted = hidden_tools(
            doc.acting_id, rev, sources, trusts_idp=self.settings.data_plane_trusts_idp
        )
        if admitted and await self.named_by_rules(doc):
            ts = await self.tenant_settings.get(doc.tenant)
            raw = await self._auth_base(doc, ts)
            unnamed = redis_tls_rev(named_rev(raw, False), await self._redis_ca(doc))
            if doc.auth_rev == unnamed:
                hidden.setdefault(HIDDEN_REISSUE, []).extend(admitted)
        return hidden_tool_warnings(hidden)

    async def source_tools_for(self, rev: AgentSpec) -> dict[str, SourceTools]:
        """리비전의 tools 노드가 가리키는 소스만의 Tool 과 효과다(`ToolEffectLoader`)."""
        ids = source_ids(rev)
        gateways = await self.gateway_instances.get_many(ids)
        servers = await self.mcp_instances.get_many(ids - set(gateways))
        return await self.tool_effects.load(gateways, servers)

    async def knowledge_bases(
        self, tenant: str, *, status: bool = True
    ) -> dict[str, KnowledgeBase]:
        """테넌트의 Knowledge Base (id → 문서). status 가 참이면 문서 집계를 채운다(검증의
        `knowledge_base_empty`·`knowledge_base_failed` 경고가 읽는다). 배포처럼 임베딩 provider 만
        필요하면 끈다."""
        kbs = await self.knowledge.list(tenant)
        if status:
            found = await self.knowledge.statuses([kb.id for kb in kbs])
            for kb in kbs:
                kb.status = found[kb.id]
        return {kb.id: kb for kb in kbs}

    async def validate(
        self, tenant: str, spec: AgentSpec, *, principal: Principal, agent_id: str | None = None
    ) -> list[Issue]:
        return (await self.review(tenant, spec, principal=principal, agent_id=agent_id)).issues

    async def review(
        self, tenant: str, spec: AgentSpec, *, principal: Principal, agent_id: str | None = None
    ) -> GraphReview:
        """검증 이슈와 tools 노드마다의 Tool 효과와 백엔드 종류, Tool 로 쓰는 게시한 Agent 다
        (`POST /validate`). 효과는 바깥 내용 뒤의 승인 규칙이 쓰는 것이다. agent_id 는 그 정의를
        가진 Agent 다(`context`)."""
        ctx = await self.context(tenant, spec, principal=principal, agent_id=agent_id)
        _analysis, issues = await _analyze(spec, ctx, calls_enabled=self.agent_sources.enabled)
        tools = graph_tools(spec, ctx.source_tools, tool_safety=True)
        calls = agent_calls.node_views(spec, ctx.agent_sources)
        return GraphReview(issues=issues, tools=tools, calls=calls)

    async def check(
        self, tenant: str, spec: AgentSpec, *, principal: Principal, agent_id: str | None = None
    ) -> list[Issue]:
        """저장·배포 앞의 검증. 오류가 있으면 AgentError(422 invalid_graph), 아니면 경고 목록."""
        issues = await self.validate(tenant, spec, principal=principal, agent_id=agent_id)
        if errors_of(issues):
            raise AgentError.from_graph(GraphError(issues))
        return issues

    # -- 정의: 저장·리비전·복제 ---------------------------------------------------------

    async def create(
        self,
        tenant: str,
        *,
        name: str,
        display_name: str,
        spec: AgentSpec,
        layout: dict[str, Point],
        principal: Principal,
        exposure: AgentExposureInput | None = None,
        replicas: int = 1,
        resources: dict[str, Any] | None = None,
        scale_to_zero: bool = True,
    ) -> tuple[Agent, list[Issue]]:
        """정의만 받아 draft r1 을 만든다. 클러스터는 건드리지 않는다. (문서, 검증 경고)."""
        if await self.agents.get_by_name(tenant, name) is not None:
            raise AgentError(409, "name_conflict", f"agent {name!r} already exists")
        issues = await self.check(tenant, spec, principal=principal)
        doc = Agent(
            tenant=tenant,
            name=name,
            display_name=display_name,
            spec=spec,
            layout=dict(layout),
            revision=1,
            revisions=[AgentRevision(revision=1, **spec.model_dump())],
            replicas=replicas,
            resources=resources,
            scale_to_zero=scale_to_zero,
            # 공용 스냅샷을 먼저 만들어 넘긴다. agent_exposure_from 은 첫 인자가 꺼져 있으면
            # 입력을 통째로 버리므로 빈 Exposure() 를 넘기면 켜서 보낸 노출도 같이 사라진다
            exposure=agent_exposure_from(exposure_from_input(exposure), exposure),
            updated_by=principal.sub,
        )
        await self.agents.create(doc)
        return doc, issues

    async def save(
        self,
        doc: Agent,
        *,
        display_name: str,
        spec: AgentSpec,
        layout: dict[str, Point] | None,
        principal: Principal,
        expect_revision: int | None = None,
    ) -> tuple[Agent, list[Issue], bool]:
        """(문서, 경고, 리비전을 만들었는가). graph·service 가 현재 리비전과 같으면 layout 과
        display_name 만 바꾸고 검증도 리비전도 없다. 배치만 옮긴 저장이 provider 삭제 같은 문맥
        변화로 막히지 않게 하려는 것이다. 배포 핀과 상태는 그대로다.

        `expect_revision` 을 주면 최신 리비전이 그 번호일 때만 저장하고, 아니면 409
        `revision_conflict` 에 지금 번호를 싣는다. 조건부 저장도 그 번호에 조건을 건다. layout 이
        None 이면 새 그래프에 남는 노드의 저장된 배치를 지키고 빠진 노드의 것만 버린다. 배치만 옮긴
        저장은 리비전을 만들지 않으므로 그 배치를 이 저장이 읽은 문서에서 가져와야 되돌리지 않는다
        (P48 6.7절)."""
        if doc.state in IN_PROGRESS:
            self._require(doc, frozenset(), action="save")
        if expect_revision is not None and expect_revision != doc.revision:
            raise revision_conflict(self._error, self.profile_kind, doc.name, revision=doc.revision)
        if layout is None:
            kept = {n.id for n in spec.graph.nodes}
            layout = {node_id: p for node_id, p in doc.layout.items() if node_id in kept}
        changed = spec.model_dump() != doc.spec.model_dump()
        issues: list[Issue] = []
        before = doc.revision
        if changed:
            issues = await self.check(doc.tenant, spec, principal=principal, agent_id=doc.id)
            doc.spec = spec
            doc.revision += 1
            doc.revisions.append(AgentRevision(revision=doc.revision, **spec.model_dump()))
        doc.display_name = display_name
        doc.layout = dict(layout)
        doc.updated_by = principal.sub
        await self._save_definition(doc, expect_revision=before, action="save")
        return doc, issues, changed

    async def clone(
        self,
        doc: Agent,
        *,
        name: str,
        display_name: str,
        exposure: AgentExposureInput | None,
        principal: Principal,
    ) -> tuple[Agent, list[Issue]]:
        """현재 리비전의 정의를 베낀 draft r1. `exposure` 를 주면 그 값이 배포 설정으로 남는다.
        한 정의로 둘을 띄우는 자리를 대신한다 (D42)."""
        rev = doc.current
        return await self.create(
            doc.tenant,
            name=name,
            display_name=display_name,
            spec=AgentSpec(graph=rev.graph, service=rev.service),
            layout=dict(doc.layout),
            principal=principal,
            exposure=exposure,
            replicas=doc.replicas,
            resources=doc.resources,
            scale_to_zero=doc.scale_to_zero,
        )

    # -- 공통 (렌더 재료) --------------------------------------------------------------

    async def want_rev(self, doc: Agent, ts: Any) -> str | None:
        """재적용이 보는 판의 첫 절반. Redis 의 TLS 끝점으로 붙을 Agent 는 그 CA 들의 지문을
        표시로 더한다 (`redis_tls_rev`, P61 3절). 그래서 Database 가 TLS 끝점을 얻거나 CA 가 바뀌면
        다음 틱에 다시 렌더되어 파드가 새 CA 로 뜬다. Tool 규칙이 이름 댄 Agent 는 표시를 하나 더
        얹어, 이 기능 전에 낸 토큰을 다음 재적용이 자기 id 를 실은 것으로 새로 낸다 (P52 3절)."""
        base = named_rev(await self._auth_base(doc, ts), await self.named_by_rules(doc))
        return redis_tls_rev(base, await self._redis_ca(doc))

    async def _auth_base(self, doc: Agent, ts: Any) -> str | None:
        """판의 인증 절반에 LLM 경로의 몫을 얹은 것 (P38 6절, `AgentLlm.rev_inputs`). 플랫폼을
        믿는 oidc 모드에서 IdP 하나로 렌더되는 노출 Agent 는 표시를 하나 더 얹는다
        (`idp_alone_rev`, `exposure_verifiers` 와 같은 조건). 그래서 첫 배포의 파드가 `two_issuers`
        를 알리면 다음 틱이 두 발급자로 다시 렌더한다."""
        base = llm_rev(await super().want_rev(doc, ts), await self.llm.rev_inputs(doc))
        alone = (
            self.cluster_service.settings.oidc_trusts_platform
            and doc.exposure.enabled
            and not doc.image_reports(TWO_ISSUERS)
        )
        return idp_alone_rev(base, alone)

    async def paced_revs(self, doc: Agent, ts: Any) -> frozenset[str | None]:
        """Redis 표시와 규칙의 이름 표시만 다른 판. 둘 다 회수가 아니라서 네임스페이스마다의 속도로
        간다. 이름 표시로 다시 받는 토큰은 Agent 마다 한 번 굴러간다."""
        raw = await self._auth_base(doc, ts)
        bases = {raw, named_rev(raw, True)}
        return frozenset(r for b in bases for r in (b, redis_tls_rev(b, doc.redis_ca)))

    async def named_by_rules(self, doc: Agent) -> bool:
        """Agent 절을 짓는 Tool 규칙이 이 Agent 를 이름 대는가 (P52 3절). 미리 보기는 원본을 이름
        대는가다 (P44b 5절)."""
        if self.rule_scopes is None:
            return False
        return doc.acting_id in await self.rule_scopes.named_agents(doc.tenant)

    async def _redis_ca(self, doc: Agent) -> str | None:
        """이 Agent 의 렌더가 TLS 끝점으로 붙을 Redis 들의 CA 지문(`redis_ca_mark`). 렌더와 같은
        규칙(`tls_endpoint`)으로 고른다. 이미지가 `redis_tls` 를 알렸으면 TLS 끝점을 가진 Redis
        전부이고, 아니면 평문 포트가 닫힌 Redis 만이다. 지워진 Database 는 보지 않는다(렌더가 그때
        422 로 멈춘다). 읽지 못하는 스냅샷은 붙지 않는 것으로 본다. 그 문서 하나 때문에 틱이 멈추지
        않게 하려는 것이다."""
        effective = stored_effective(doc)
        if effective is None:
            return None
        bound = await tls_redis_bindings(
            effective, lambda i: self._recent.database(self.data_services, i, doc.tenant)
        )
        reports = doc.image_reports(REDIS_TLS)
        return redis_ca_mark(
            {ds.id: tls.ca_sha256 for ds in bound if (tls := tls_endpoint(ds, reports)) is not None}
        )

    async def _binds_closed_redis(
        self,
        tenant: str,
        effective: EffectiveConfig,
        services: Mapping[str, DataService] | None = None,
    ) -> bool:
        """바인딩한 Database 가운데 평문 포트가 닫힌 Redis 가 있는가 (P61 5절 5단계). services 는
        부르는 쪽이 이미 읽은 바인딩 Database 이고, 없으면 재적용과 같은 `RecentReads` 로 읽는다.
        지워진 Database 는 보지 않는다(렌더가 422 로 멈춘다)."""

        async def lookup(ds_id: str) -> DataService | None:
            if services is not None:
                return services.get(ds_id)
            return await self._recent.database(self.data_services, ds_id, tenant)

        return any(ds.tls_only for ds in await tls_redis_bindings(effective, lookup))

    async def _bound_data_services(
        self, tenant: str, effective: EffectiveConfig
    ) -> dict[str, DataService]:
        services: dict[str, DataService] = {}
        for b in effective.bindings:
            ds = await self.data_services.get(b.data_service_id, tenant=tenant)
            if ds is None:
                raise AgentError(
                    422, "unknown_data_service", f"data service {b.data_service_id} not found"
                )
            services[ds.id] = ds
        return services

    async def _agent_oauth(self, tenant: str, rev: AgentRevision) -> AgentOAuth | None:
        """데이터 플레인이 IdP 를 믿는 oidc 모드에서 Gateway 도구 소스를 쓸 때만 client credentials
        env 가 필요하다. 플랫폼을 믿는 모드(local, platform 인 oidc)는 플랫폼이 낸 토큰을 쓴다 (D41,
        P53b 6.1절)."""
        if not (self.settings.data_plane_trusts_idp and agent_graph.uses_gateway_source(rev)):
            return None
        ts = await self.tenant_settings.get(tenant)
        if ts.agent_oauth is None:
            raise AgentError(
                422,
                "agent_oauth_missing",
                "Tenant Settings agent_oauth is required for a Gateway tool source",
            )
        return ts.agent_oauth

    async def _context(self, doc: Agent) -> _Context:
        cluster = await self._cluster_of(doc)
        rev = self._pinned(doc)
        effective = EffectiveConfig.model_validate(doc.effective_config)
        auth = await self._consumer_auth(doc.tenant)
        providers, knowledge_providers = await self.llm.providers(doc.tenant, rev)
        return _Context(
            cluster=cluster,
            rev=rev,
            effective=effective,
            providers=providers,
            knowledge_providers=knowledge_providers,
            data_services=await self._bound_data_services(doc.tenant, effective),
            agent_oauth=await self._agent_oauth(doc.tenant, rev),
            auth=auth,
            gateway_token=auth.trusts_platform and agent_graph.uses_gateway_source(rev),
        )

    def _render_deployment(self, doc: Agent, ctx: _Context) -> dict[str, Any]:
        """노출을 켠 문서는 검증기 env 가 들어간다. issuer 는 발급자 있는 모드에만 있으므로 header
        모드로 바뀐 플랫폼에서 노출 스냅샷을 다시 렌더하면 422 unauthenticated_exposure 다.

        이 렌더가 TLS 끝점으로 붙인 Redis 와 그 CA 의 지문을 문서에 적는다(`redis_tls_bindings`,
        `redis_ca`). 배포 쪽 저장이 함께 남긴다."""
        redis_tls = doc.image_reports(REDIS_TLS)
        doc.redis_tls_bindings = sorted(
            ds_id
            for ds_id, ds in ctx.data_services.items()
            if tls_endpoint(ds, redis_tls) is not None
        )
        try:
            deployment = render_agent_deployment(
                doc,
                ctx.effective,
                providers=ctx.providers,
                data_services=ctx.data_services,
                agent_oauth=ctx.agent_oauth,
                oidc_audience=ctx.auth.mcp_audience,
                **exposure_verifiers(ctx.auth, doc),
                knowledge_providers=ctx.knowledge_providers,
                gateway_token=ctx.gateway_token,
                transcript_retention_days=self.settings.transcript_retention_days,
                instance_token=ctx.instance_token,
                redis_tls=redis_tls,
                telemetry_ingest=self.settings.telemetry_ingest_enabled,
                sandbox=doc.deployed is not None
                and agent_graph.code_execution_of(doc.deployed) is not None,
            )
        except RenderError as e:
            raise AgentError(status_of(e.code, 422), e.code, e.detail) from e
        # 재적용이 CA 만 바뀐 판을 천천히 다시 렌더하려고 렌더한 CA 지문을 적는다(`paced_revs`)
        doc.redis_ca = template_annotation(deployment, REDIS_CA_ANNOTATION)
        return deployment

    def _render_side_docs(self, doc: Agent) -> list[dict[str, Any]]:
        """Service·NetworkPolicy, 노출을 켰으면 Ingress·노출 NetworkPolicy (owner 는 Deployment)."""
        docs = [
            render_agent_service(doc),
            render_agent_policy(doc, self.settings.namespace),
        ]
        if doc.exposure.enabled:
            effective = EffectiveConfig.model_validate(doc.effective_config)
            docs.extend(
                render_agent_expose_docs(
                    doc, doc.exposure, ingress=effective.ingress, owner_uid=doc.k8s_uid
                )
            )
        return docs

    def _sibling_stores(self) -> list[Any]:
        return [self.agents, self.mcp_instances, self.gateway_instances]

    def _empty_exposure(self) -> AgentExposure:
        return AgentExposure()

    def _wrap_exposure(self, exposure: Exposure, inp: ExposureInput | None) -> AgentExposure:
        return agent_exposure_from(exposure, inp)

    async def _analyze_for_cluster(
        self, tenant: str, rev: AgentRevision, cluster: Cluster, ctx: GraphContext
    ) -> tuple[Analysis, list[Binding], list[Issue]]:
        """그래프 검증 + 배포 클러스터 검사. 도구 소스와 memory·knowledge 의 Database 는 배포
        클러스터에 있어야 파드가 Service 주소로 닿는다. (분석, Database 바인딩, 경고). 바인딩은
        memory 가 `DAEGAM_DB_0`, 그 뒤 knowledge 노드 순서로 `DAEGAM_DB_<i>` 이고 같은 Database 는
        한 번이다(compile 이 `templates/agent.py` 의 `dsn_env` 로 Database id 를 찾는다). 웹 Tool 의
        규칙도 저장 앞의 검증처럼 다시 본다. 저장 뒤에 소스가 웹 Tool 을 얻었을 수 있다."""
        analysis, issues = await _analyze(rev, ctx, calls_enabled=self.agent_sources.enabled)
        if errors_of(issues):
            raise AgentError.from_graph(GraphError(issues))
        for node_id, data in analysis.nodes_of("tools", ToolsData):
            if data.source.kind == AGENT_SOURCE:
                continue  # 호출은 플랫폼을 거치므로 호출받는 Agent 의 클러스터를 묻지 않는다
            source = ctx.source_pool(data.source.kind)[data.source.id]
            if source.cluster_id != cluster.id:
                raise AgentError(
                    422,
                    "cluster_mismatch",
                    f"tools node {node_id!r}: {data.source.kind} {source.name!r} is on cluster "
                    f"{source.cluster_id}, expected {cluster.id}",
                    node_id=node_id,
                )
        bindings: list[Binding] = []
        assert analysis.main is not None

        def bind(ds: DataService, node_id: str) -> None:
            if any(b.data_service_id == ds.id for b in bindings):
                return
            try:
                check_same_cluster([ds], tenant=tenant, cluster_id=cluster.id)
            except ClusterMismatch as e:
                raise AgentError(422, "cluster_mismatch", e.detail, node_id=node_id) from e
            bindings.append(Binding(data_service_id=ds.id, env_prefix=db_env_prefix(len(bindings))))

        for memory_id, mdata in analysis.typed_targets(analysis.main, "memory", MemoryData)[:1]:
            bind(ctx.data_services[mdata.checkpointer.data_service_id], memory_id)
        for node_id, kdata in analysis.attached_knowledge_nodes():
            kb = ctx.knowledge_bases[kdata.knowledge_base_id]
            ds = ctx.data_services.get(kb.data_service_id)
            if ds is None:
                # Knowledge Base 가 가리키는 Database 가 사라졌다. Database 삭제 가드가 막는
                # 조합이라 정상 경로에서는 오지 않는다
                raise AgentError(
                    422,
                    "unknown_data_service",
                    f"knowledge node {node_id!r}: data service {kb.data_service_id!r} of "
                    f"knowledge base {kb.name!r} not found",
                    node_id=node_id,
                )
            bind(ds, node_id)
        warnings = [i for i in issues if i.level == "warning"]
        return analysis, bindings, warnings

    async def _effective(
        self,
        tenant: str,
        cluster: Cluster,
        *,
        replicas: int,
        resources: dict[str, Any] | None,
        bindings: list[Binding],
        namespace: str,
    ) -> EffectiveConfig:
        effective = resolve_effective(
            await self.platform_settings.get(),
            cluster,
            await self.tenant_settings.get(tenant),
            ProfileRuntime(image=None, replicas=replicas, resources=resources, bindings=bindings),
        )
        effective.namespace.name = namespace
        return effective

    # -- 배포 --------------------------------------------------------------------------

    async def deploy(
        self,
        doc: Agent,
        *,
        cluster_id: str | None,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, Any] | None = None,
        resources_sent: bool = False,
        exposure: AgentExposureInput | None = None,
        exposure_sent: bool = False,
        scale_to_zero: bool | None = None,
        principal: Principal,
        skip_evaluation: str | None = None,
    ) -> tuple[Agent, list[Issue]]:
        """검증, 노출 검증(oidc 전용, host 충돌은 리소스 셋 전역), 바인딩·effective_config 스냅샷,
        렌더 확인을 거쳐 배포 쪽을 채워 `deploying` 으로 저장한다. 적용은 `provision` 이다.
        (문서, 경고). `exposure`·`replicas`·`resources`·`scale_to_zero` 를 보내지 않으면 문서에 남은
        설정을 쓴다. 배포 게이트는 메모리에 든 현재 리비전을 판정한다. 전이가 `draft` 를 조건으로
        걸어 그 사이 바뀐 문서에는 쓰지 않기 때문이다 (P45 12절). skip_evaluation 은 게이트를
        건너뛰는 이유다."""
        self._require(doc, {"draft"}, action="deploy")
        tenant = doc.tenant
        rev = doc.current
        cluster, ns_name = await self._resolve_target(tenant, cluster_id, namespace)
        k8s_name = k8s_name_for(doc.name)
        await self._require_name_free(doc, cluster.id, ns_name, k8s_name)
        inp = exposure if exposure_sent else agent_exposure_to_input(doc.exposure)
        exposure_snapshot, _expose_warnings = await self._validate_exposure(
            inp, tenant=tenant, name=doc.name, cluster=cluster, namespace=ns_name
        )
        ctx = await self._doc_context(doc, rev, principal=principal)
        _analysis, bindings, warnings = await self._analyze_for_cluster(tenant, rev, cluster, ctx)
        await self._sandbox_check(rev, cluster, ns_name)
        want_replicas, want_resources, effective = await self._sized_effective(
            doc, cluster, bindings, ns_name, replicas, resources, resources_sent
        )
        # 배포 때 소스가 광고하는 이름을 박는다 (P39b 14절 5부)
        pins = pins_of(rev, ctx.source_tools, ctx.agent_sources)
        warnings += await self._feature_check(doc, rev, effective, ctx, pins)
        gate = await self._judge_gate(doc, rev, skip_evaluation)
        warnings += gate.warnings
        doc.replicas = want_replicas
        doc.resources = want_resources
        if scale_to_zero is not None:
            doc.scale_to_zero = scale_to_zero
        doc.bindings = bindings
        doc.tool_pins = pins
        doc.tool_pins_revision = rev.revision
        self._stamp_deploying(
            doc,
            cluster=cluster,
            k8s=AgentK8s(
                name=k8s_name, namespace=ns_name, token_secret=token_secret_name_for(doc.id)
            ),
            effective=effective,
            exposure=exposure_snapshot,
            revision=rev.revision,
        )
        # 쉬는 상태는 새 배포에서 깨어 있음으로 시작하고 창도 배포에서 시작한다. 같은 저장이
        # 비우므로 남은 상태가 새 배포로 넘어가지 않는다 (P44 12절)
        doc.sleep = AgentSleep(activity=to_millis(utcnow()))
        # 렌더가 되는지(바인딩 Database 존재, agent_oauth 등) 저장 전에 확인한다
        self._render_deployment(doc, await self._context(doc))
        history = deploy_record_push(doc, "deploy", principal, gate=gate)
        async with self._host_claimed(doc):
            await self._transition(doc, {"draft"}, action="deploy", extra={"sleep"}, push=history)
        warnings += await self._runs_deployed(doc, principal, previous=None)
        warnings += await self._calls_deployed(doc)
        await self._prune_choices(doc)
        return doc, warnings

    def _name_stores(self) -> list[tuple[Any, str]]:
        """Agent 의 Deployment 이름은 Gateway 의 것과도 겹치면 안 된다. Gateway 를 먼저 본다."""
        return [(self.gateway_instances, "gateway"), (self.agents, self.profile_kind)]

    async def provision(self, doc: Agent, *, token: str | None = None) -> Agent:
        """네임스페이스, 토큰 Secret, provider 키 Secret 복사, 장기 기억 스키마,
        Deployment·Service·NetworkPolicy 순으로 적용하고 `deployed` 다(골격은 기반의 것). token 은
        방금 발급한 평문이다(없으면 Secret 이 있고 토큰이 살아 있을 때만 건너뛰고, 아니면 새로
        발급한다)."""
        return await super().provision(doc, token=token)

    async def _provision(self, doc: Agent, token: str | None = None) -> None:
        ctx = await self._context(doc)
        k8s = self._k8s(doc)
        # 파드가 뜰 때 읽는 입력의 기록(키 env 표, 토큰 env 표, 설정 지문)은 Deployment 를 적용하기
        # 바로 전에 남긴다. 바뀌었는지는 처음 값과 견준다
        before = {name: getattr(doc, name) for name in POD_INPUT_RECORDS}
        # 저장된 overrides 가 지금 잠긴 경로를 건드리면 토큰·Secret 을 만들기 전에 멈춘다
        await self.editor.check_stored_rendered(doc)
        # 1. 네임스페이스 (스냅샷의 네임스페이스 규칙과 같다)
        await self.tenancy.ensure_namespace(doc.tenant, ctx.cluster, ctx.effective.namespace)
        client: KubeClient = await run_in_threadpool(self.cluster_service.client_for, ctx.cluster)

        # 2. LLM 경로 (P38 6절). 직접 경로면 provider 키를 테넌트 네임스페이스로 복사한다. 렌더가
        # 참조하는 Secret 이라 렌더보다 먼저고, 지문에 드는 키 sha256 도 여기서 온다
        llm_before = self.llm.before(doc)
        keys = await self.llm.bind(doc, ctx, client)

        # 3. 인스턴스 토큰 Secret. MCP Server 소스마다의 서비스 토큰이 이 템플릿의 표를 따르고(P50
        # 9절), 게이트웨이 경로면 llm_token 이 든다. 설정 지문을 걸 토큰이 돌아온다 (P35 13절)
        doc.mcp_token_tables = await self.agent_tokens.tables(doc)
        ctx.instance_token = await self.agent_tokens.ensure_secret(doc, ctx, client, token)

        # 4. 토큰 판과 키 env 이름의 표(P35 12절), 설정 지문(13절)이 정해졌으니 렌더한다 (맨 앞에서
        # 검사했다). 지문의 컴파일이 실패하면 여기서 422 라 파드가 크래시 루프에 들지 않는다. 턴
        # drain 은 provision 마다 지금 플랫폼 설정으로 정한다 (P78 5절)
        doc.llm_env = llm_env_for(ctx.providers)
        doc.turn_drain_seconds = self.settings.agent_turn_drain_seconds
        doc.config_digest = await self._config_digest(doc, keys)
        deployment = self.editor.stored_final(doc, self._render_deployment(doc, ctx))

        # 5. 장기 기억의 전용 스키마. 파드가 뜨기 전에 있어야 한다 (D34). Postgres 만 부르므로
        # 클러스터 요청의 훅이 보지 못해 앞에서 클레임을 확인한다 (P66 2.4절)
        await checkpoint()
        await self.memories.ensure_schema(doc, ctx.rev, ctx.data_services)

        # 6. 표와 지문을 남기고(적용 바로 전) Deployment(+overrides) → Service·NetworkPolicy →
        # 노출 객체(Ingress·노출 정책, owner 는 방금 적용한 Deployment). 노출을 껐으면 이름으로
        # 정리한다
        await self.pod_inputs.record(doc, before)
        applied = await run_in_threadpool(client.apply, deployment)
        doc.k8s_uid = (applied.get("metadata") or {}).get("uid") or doc.k8s_uid
        await self.llm.after_apply(doc, llm_before, applied)
        for rendered in self._render_side_docs(doc):
            await run_in_threadpool(client.apply, rendered)
        if not doc.exposure.enabled:
            await run_in_threadpool(
                delete_expose_objects, client, k8s.name, k8s.namespace, front_proxy=False
            )

    async def _check_start(
        self, doc: Agent, services: Mapping[str, DataService] | None = None
    ) -> None:
        """다시 렌더한 Deployment 의 새 파드가 뜰 때 받을 검사를 먼저 돌린다 (P50 1절). 새 파드의
        init 은 현재 provider·이미지 기능으로 agent.json 을 컴파일하므로(`fetch_config`), 그
        컴파일이 어긋나거나 핀한 이미지의 기록에 요구 기능이 없으면 굴리지 않고 422 로 멈춘다.
        재적용은 그 문서를 막아 재배포를 기다리는 것으로 센다. 옛 파드는 그대로 돈다. services 는
        렌더 문맥이 읽은 바인딩 Database 다."""
        wanted = await self._wanted_features(doc, services=services)
        record = doc.runtime_for_image
        if record is not None:
            check = agent_features.check_features(wanted, frozenset(record.features))
            if check.missing:
                raise _image_too_old(check.missing)
        await self._start_document(doc, wanted)

    async def _wanted_features(
        self,
        doc: Agent,
        ctx: GraphContext | None = None,
        *,
        rev: AgentRevision | None = None,
        pins: ToolPins | None = None,
        effective: EffectiveConfig | None = None,
        services: Mapping[str, DataService] | None = None,
        gateway: bool | None = None,
    ) -> agent_features.AgentFeatures:
        """리비전이 provider 표와 Tool 핀으로 요구하는 기능. 배포 전 검사, 롤 전 검사, init
        받기, 컴파일이 이것 하나를 쓴다. 바인딩한 Redis 가운데 평문 포트가 닫힌 것이 있으면
        `redis_tls` 도 요구한다 (P61 5절 5단계, `agent_features.requiring_redis_tls`). 주지 않은
        rev·pins·effective 는 핀한 리비전, 문서의 Tool 핀, 저장된 스냅샷이고, 읽지 못하는 스냅샷은
        닫힌 Redis 가 없는 것으로 본다. 문맥(ctx)이 없으면 provider 표와 리비전이 가리키는 소스만
        읽는다. services 는 이미 읽은 바인딩 Database 이고, 주지 않으면 문맥의 것이나
        `RecentReads` 로 읽은 것이다. 게이트웨이 경로로 컴파일하면(gateway, 주지 않으면 저장된 경로)
        `llm_gateway` 도 요구한다 (P38 6절). 파드 안에서 승인을 묻는 미리 보기에는 `in_pod_state` 를
        붙인다(문맥의 `in_pod_approvals`, 문맥이 없으면 `_preview_asks`, P44b 7절). 평가 미리
        보기는 그것을 요구한다. 그 이름을 모르는 이미지는 init 받기에서 `runtime_image_too_old` 다
        (P45 9절)."""
        rev = rev or self._pinned(doc)
        if ctx is None:
            providers = await self._providers_by_name()
            sources = await self.source_tools_for(rev)
            asks = await self._preview_asks(doc, rev.revision)
        else:
            providers, sources = ctx.providers, ctx.source_tools
            services = ctx.data_services if services is None else services
            asks = ctx.in_pod_approvals
        pins = valid_pins(doc) if pins is None else pins
        web = agent_web.uses_web(rev, pins, sources)
        wanted = agent_features.agent_features(doc, rev, providers, web=web, in_pod_approvals=asks)
        if effective is None:
            effective = stored_effective(doc)
        if effective is not None and await self._binds_closed_redis(
            doc.tenant, effective, services
        ):
            wanted = agent_features.requiring_redis_tls(wanted)
        if doc.llm_route.gateway if gateway is None else gateway:
            wanted = agent_features.requiring_llm_gateway(wanted)
        if doc.evaluation_run is not None:
            wanted = agent_features.requiring_in_pod_state(wanted)
        return wanted

    async def _start_document(
        self, doc: Agent, wanted: agent_features.AgentFeatures
    ) -> dict[str, Any]:
        """새 파드의 init 이 받을 agent.json 이다. 스냅숏 이미지의 기록으로 허락된 기능과 지금
        템플릿의 토큰 env 표로 컴파일한다. 설정 지문과 롤 전 검사가 함께 쓴다."""
        return await self.compile(
            doc,
            allowed=agent_features.snapshot_allowed(doc, wanted),
            token_envs=current_envs(doc),
        )

    async def prune_token_tables(self, doc: Agent, client: KubeClient) -> bool:
        """앞 템플릿들의 토큰 env 표를 그 파드가 다 없어졌으면 거둔다 (`AgentTokens.prune_tables`).
        서비스 토큰 스윕이 부른다. 문서의 클레임을 쥐고 돌고, 남이 쥐고 있으면 건너뛴다 (P66 F3).
        client 는 스윕이 목록의 사본으로 만든 것이라, 쥐며 다시 읽은 문서가 다른 클러스터로 옮겨
        갔으면 건너뛴다."""
        listed = doc.cluster_id

        async def work() -> bool:
            if doc.cluster_id != listed:
                return False
            return await self.agent_tokens.prune_tables(doc, client)

        return await self._claimed_or_skip(
            doc, self.live_states, action="prune token tables", work=work
        )

    async def claimed_scale(self, doc: Agent, work: Callable[[], Awaitable[bool]]) -> bool:
        """레플리카 패치(`AgentSleepControl.apply_replicas`)를 문서의 클레임 안에서 돌린다 (P44
        9절). provision·재적용과 차례를 클레임이 정한다. 남이 쥐고 있으면 건너뛰고 False 다."""
        return await self._claimed_or_skip(
            doc, self.live_states, action="scale replicas", work=work
        )

    async def claimed_llm_cleanup(self, doc: Agent, work: Callable[[], Awaitable[bool]]) -> bool:
        """롤아웃 뒤의 LLM 정리(`LlmTokens.settle`)를 문서의 클레임 안에서 돌린다 (P66 F3)."""
        return await self._claimed_or_skip(
            doc, self.live_states, action="settle llm cleanup", work=work
        )

    async def instance_token_current(self, doc: Agent, token: str | None) -> bool:
        """Secret 의 인스턴스 토큰을 다시 써도 되는가 (`AgentTokens.instance_current`). 서비스 토큰
        스윕이 부른다."""
        return await self.agent_tokens.instance_current(doc, token)

    async def _config_digest(self, doc: Agent, keys: Mapping[str, str]) -> str:
        """파드가 뜰 때 읽는 입력의 키 없는 지문 (P35 13절). agent.json 은 스냅숏 이미지의 기록으로
        허락된 기능으로 컴파일해 init 이 받을 문서와 같다. 그래프가 지금 상태에서 어긋나면 422
        invalid_graph 라 init 이 받지 못해 크래시 루프에 드는 대신 provision 이 실패한다."""
        document = await self._start_document(doc, await self._wanted_features(doc))
        return config_digest(document, keys)

    async def _apply_consumer(self, doc: Agent, client: KubeClient, *, rotate: bool) -> None:
        """LLM 경로를 다시 정하고(P38 6절) 토큰 Secret 을 통째로 다시 써서(`rewrite_secret`)
        Deployment 를 다시 적용한다. 인스턴스 토큰 평문은 발급 때만 있으므로 살아 있지 않을 때만
        다시 발급한다. 살아 있으면 그대로 두어 굴러가는 사이에도 플랫폼이 런타임을 부르는 호출이
        끊기지 않는다."""
        ctx = await self._context(doc)
        # 렌더가 요청 오류(header 로 바뀐 배포본의 노출 등)로 막히거나 저장된 overrides 가 거부되면
        # Secret 과 토큰을 건드리기 전에 멈춘다. deploy 가 저장 전에 하는 검증 렌더와 같다
        self.editor.check_stored(doc, self._render_deployment(doc, ctx))
        # LLM 경로를 다시 정한다. 옮길 때의 복사본과 롤아웃 뒤의 정리가 여기서 온다 (P38 6절)
        llm_before = self.llm.before(doc)
        await self.llm.bind(doc, ctx, client, provision=False, expect_state=self.live_states)
        # 토큰 env 표는 그것을 싣는 Deployment 보다 먼저 남긴다. 새 템플릿의 파드가 init 때 이 표를
        # 찾는다 (P50 9절)
        before = {"mcp_token_tables": doc.mcp_token_tables}
        doc.mcp_token_tables = await self.agent_tokens.tables(doc)
        await self.pod_inputs.record(doc, before, expect_state=self.live_states)
        await self._check_start(doc, ctx.data_services)
        existing = await self.agent_tokens.token_secret(doc, client)
        held = secret_value(existing, TOKEN_KEY)
        token = held if await self.agent_tokens.instance_current(doc, held) else None
        if token is None:
            token = await self.agent_tokens.issue(doc, ctx.rev.revision)
        await self.agent_tokens.rewrite_secret(doc, ctx, client, token, existing, rotate=rotate)
        # 설정 지문은 다시 계산하지 않고 남긴 digest 를 지금 토큰으로 다시 건다 (P35 13절). 컴파일은
        # `_check_start` 가 검사로만 한다. 토큰을 다시 냈으면 값이 바뀌어 파드가 새 토큰을 받고, 그
        # 사이 바뀐 플랫폼 상태는 다음 재배포가 싣는다
        ctx.instance_token = token
        applied = await run_in_threadpool(
            client.apply, self.editor.stored_final(doc, self._render_deployment(doc, ctx))
        )
        await self.llm.after_apply(doc, llm_before, applied)

    # -- 장기 기억 (D34). 저장소에 붙는 일은 agent_memory.AgentMemories 가 한다 ----------------

    async def delete_memories(self, doc: Agent, *, user_sub: str | None) -> int:
        """그 문서의 장기 기억을 지운다. `user_sub` 가 있으면 그 사람 것만이다(D34). 장기 기억이
        꺼져 있으면 지울 것이 없으므로 0 이다."""
        doc = await self._fresh(doc)
        # 해제로 draft 가 된 문서에도 사람의 기억이 남아 있다. 지울 길이 없으면 "내 기억을 지워
        # 달라" 에 답할 수 없으므로 draft 도 받는다. 진행 중인 상태는 _require 가 막는다
        self._require(doc, EDIT_STATES | {"draft"}, action="delete memories")
        # 핀이 풀린 문서는 정의가 여전히 memory 노드와 그 Database 를 말한다 (D42)
        rev = self._memory_revision(doc)
        if rev is None:
            raise AgentError(
                409,
                "unknown_revision",
                f"agent {doc.name} pins revision {doc.deployed_revision}, which is gone; "
                "cannot tell where the memories live",
            )
        return await self.memories.delete(doc, rev, user_sub=user_sub)

    # -- 재배포 ----------------------------------------------------------------

    async def adopt_revision(
        self, doc: Agent, revision: AgentRevision, *, display_name: str
    ) -> None:
        """저장한 리비전 하나를 그 번호 그대로 이 문서의 지금 정의로 들인다. 미리 보기의 옮기기가
        원본의 최신 리비전으로 쓰고 뒤이어 재배포한다 (P44b 7절). 정의 쪽만 쓰는 저장이라 읽은
        리비전이 그대로일 때만 지나가고 배포 쪽은 손대지 않는다."""
        before = doc.revision
        doc.spec = AgentSpec(graph=revision.graph, service=revision.service)
        doc.revision = revision.revision
        if doc.get_revision(revision.revision) is None:
            doc.revisions.append(revision)
        doc.display_name = display_name
        await self._save_definition(doc, expect_revision=before, action="adopt a revision")

    async def redeploy(
        self,
        doc: Agent,
        *,
        exposure: AgentExposureInput | None = None,
        exposure_sent: bool = False,
        replicas: int | None = None,
        resources: dict[str, Any] | None = None,
        resources_sent: bool = False,
        scale_to_zero: bool | None = None,
        principal: Principal,
        restored_from: int | None = None,
        skip_evaluation: str | None = None,
        gate: GateOutcome | None = None,
    ) -> tuple[Agent, list[str]]:
        """핀을 현재 리비전으로 옮기고 effective_config 를 다시 계산해 다시 적용한다 (provider 키
        재복사·SSA 재적용). 리비전이 바뀌면 같은 토큰에 새 리비전 scope 를 더한다. 보류 세션이
        있는데 리비전이 바뀌면 경고한다. (문서, 경고). 문서의 클레임을 쥐고 돈다. 남이 쥐고 있으면
        409 in_progress 다 (P66 F3). 잠든 Agent 는 새 리비전의 파드가 뜨게 먼저 깨운다 (P44
        12절). restored_from 은 되돌리기가 내용을 가져온 리비전이고, 배포 기록에 rollback 으로
        남는다 (P45 6절). 배포 게이트는 클레임이 다시 읽은 현재 리비전을 판정한다 (12절). gate 는
        되돌리기가 저장 앞에 받은 판정이다."""
        self._require(doc, EDIT_STATES, action="redeploy")
        async with self._claim_scope(doc, EDIT_STATES, action="redeploy"):
            tenant = doc.tenant
            rev = doc.current
            if gate is None:
                gate = await self._judge_gate(doc, rev, skip_evaluation)
            k8s = self._k8s(doc)
            cluster = await self._writable_cluster_of(doc)
            await self._require_namespace_still_allowed(tenant, cluster, k8s.namespace)
            ctx = await self._doc_context(doc, rev, principal=principal)
            _analysis, bindings, issues = await self._analyze_for_cluster(tenant, rev, cluster, ctx)
            await self._sandbox_check(rev, cluster, k8s.namespace)
            revision_changed = rev.revision != doc.deployed_revision
            if revision_changed:
                issues += await self._pending_moved(doc, rev.revision)
            new_exposure = await self._redeploy_exposure(
                doc, exposure, exposure_sent, cluster, k8s.namespace
            )
            want_replicas, want_resources, effective = await self._sized_effective(
                doc, cluster, bindings, k8s.namespace, replicas, resources, resources_sent
            )
            pins = pins_of(rev, ctx.source_tools, ctx.agent_sources)
            issues += await self._feature_check(doc, rev, effective, ctx, pins)
            issues += gate.warnings
            previous = doc.deployed
            await self._apply_snapshot(
                doc,
                revision=rev.revision,
                effective=effective,
                bindings=bindings,
                exposure=new_exposure,
                replicas=want_replicas,
                resources=want_resources,
                tool_pins=pins,
            )
            if revision_changed and doc.token_id:
                await self.tokens.add_scope(doc.token_id, scope_for(doc.id, rev.revision))
            if scale_to_zero is not None:
                doc.scale_to_zero = scale_to_zero
            await self.sleep.wake_for_deploy(doc)
            self._start_deployment(doc)
            action = "rollback" if restored_from is not None else "redeploy"
            history = deploy_record_push(
                doc, action, principal, restored_from=restored_from, gate=gate
            )
            # host 를 바꿀 때만 선점한다. 그대로인 host 는 배포 때의 선점이나 기록 주기가 지킨다
            async with self._host_claimed(doc, claim=exposure_sent):
                await self._transition(doc, EDIT_STATES, action="redeploy", push=history)
            issues += await self._runs_deployed(doc, principal, previous=previous)
            issues += await self._calls_deployed(doc)
            await self._prune_choices(doc)
            self._place_claim(doc)
            return await self.provision(doc), issue_lines(issues)

    async def _judge_gate(self, doc: Agent, content: AgentSpec, skip: str | None) -> GateOutcome:
        """배포 게이트의 판정 (P45 12절). 게이트가 없거나 걸리지 않으면 빈 판정이다."""
        if self.gate is None:
            return GateOutcome()
        return await self.gate.judge(doc, content, skip=skip)

    async def _sandbox_check(self, rev: AgentSpec, cluster: Cluster, namespace: str) -> None:
        """코드 실행을 켠 리비전이면 그 클러스터의 게이트와 네임스페이스 LimitRange 를 본다 (P40
        7절·8절). 닫혔으면 409 `sandbox_gate_closed` 와 까닭, 담지 못하면 409
        `sandbox_limit_range` 다."""
        if self.sandboxes is None or agent_graph.code_execution_of(rev) is None:
            return
        await self.sandboxes.deploy_check(cluster, namespace)

    async def _sized_effective(
        self,
        doc: Agent,
        cluster: Cluster,
        bindings: list[Binding],
        namespace: str,
        replicas: int | None,
        resources: dict[str, Any] | None,
        resources_sent: bool,
    ) -> tuple[int, dict[str, Any] | None, EffectiveConfig]:
        """배포·재배포가 쓸 (복제 수, 자원, effective_config). 보내지 않은 복제 수·자원은 문서의
        것이다. 자원은 null 을 보낼 수 있어 보냈는지(resources_sent)로 가른다."""
        want_replicas = replicas if replicas is not None else doc.replicas
        want_resources = resources if resources_sent else doc.resources
        effective = await self._effective(
            doc.tenant,
            cluster,
            replicas=want_replicas,
            resources=want_resources,
            bindings=bindings,
            namespace=namespace,
        )
        return want_replicas, want_resources, effective

    async def _redeploy_exposure(
        self,
        doc: Agent,
        exposure: AgentExposureInput | None,
        exposure_sent: bool,
        cluster: Cluster,
        namespace: str,
    ) -> AgentExposure:
        """재배포가 쓸 노출. 보낸 노출은 검증하고(null 이면 끈 노출이다), 보내지 않았으면 문서의
        것이다."""
        if not exposure_sent:
            return doc.exposure
        checked, _warnings = await self._validate_exposure(
            exposure,
            tenant=doc.tenant,
            name=doc.name,
            cluster=cluster,
            namespace=namespace,
            exclude_id=doc.id,
        )
        return checked

    async def _pending_moved(self, doc: Agent, revision: int) -> list[Issue]:
        """리비전을 옮기는 재배포에 보류 세션이 있으면 그 경고다. 세션은 새 리비전 그래프에서
        이어진다."""
        pending = await self.status_view.pending_count(doc)
        if not pending:
            return []
        message = (
            f"{pending} session(s) await approval on revision {doc.deployed_revision}; they "
            f"resume on revision {revision}"
        )
        return [Issue(code="pending_sessions_revision_changed", message=message, level="warning")]

    async def _apply_snapshot(
        self,
        doc: Agent,
        *,
        revision: int,
        effective: EffectiveConfig,
        bindings: list[Binding],
        exposure: AgentExposure,
        replicas: int,
        resources: dict[str, Any] | None,
        tool_pins: ToolPins,
    ) -> None:
        """재배포가 문서에 얹는 새 스냅샷. overrides 가 있으면 그것으로 렌더해 dry-run 하고,
        실패하면 진입 시점으로 되돌린 뒤 오류를 그대로 올린다. 새 렌더에 안 맞으면 409
        overrides_invalid 이고, 저장된 overrides 가 지금 잠긴 경로를 건드리면 422 locked_field 다.

        아직 저장 전이라 되돌려야 다음 재배포가 옛 스냅샷에서 시작한다. 되돌릴 값은 `fields`
        에서 그대로 뜨므로 스냅샷 필드를 하나 더해도 롤백이 따라온다."""
        fields: dict[str, Any] = {
            "deployed_revision": revision,
            "effective_config": effective.model_dump(mode="json"),
            "bindings": bindings,
            "exposure": exposure,
            "replicas": replicas,
            "resources": resources,
            "tool_pins": tool_pins,
            "tool_pins_revision": revision,
        }
        before = {name: getattr(doc, name) for name in fields}
        for name, value in fields.items():
            setattr(doc, name, value)
        if not doc.overrides:
            return
        try:
            ctx = await self._context(doc)
            await self._dry_run_overrides(doc, self._render_deployment(doc, ctx))
        except AgentError:
            for name, value in before.items():
                setattr(doc, name, value)
            raise

    # -- 해제·삭제 ------------------------------------------------------------------

    async def _guard_undeploy(self, doc: Agent) -> None:
        """허브에 게시돼 있으면 409 published 다 (P46 6.1절). 다시 몰아가는 해제에도 본다."""
        await self.publishing.refuse_undeploy(doc)

    async def _after_cluster_removed(self, doc: Agent) -> None:
        """게시한 허브 항목을 정리한다. 삭제는 게시가 막지 않는다."""
        await self.publishing.forget(doc)

    async def _delete_steps(self, doc: Agent, *, forget_memories: bool = False) -> None:
        """Deployment·Service·NetworkPolicy·노출 객체·Secret 을 지운다. `forget_memories` 면
        마지막에 장기 기억도 지운다(삭제만. 해제는 남긴다)."""
        k8s = self._k8s(doc)
        client = await self._cleanup_client(doc)
        if client is None:
            return
        ns = k8s.namespace
        # 이름으로 지운다. 노출 객체(Ingress·daegam-expose-<name>)는 Deployment 의 ownerReference 로
        # GC 되지만 한 번 더 지운다. 노출 스냅샷과 무관하게 항상 지운다 (예전에 켰다 끈 잔재)
        refs = [
            ("apps/v1", "Deployment", k8s.name),
            ("v1", "Service", k8s.name),
            (NETWORK_POLICY_API, "NetworkPolicy", policy_name_for(k8s.name)),
            *expose_object_refs(k8s.name, front_proxy=False),
        ]
        for api_version, kind, name in refs:
            await run_in_threadpool(client.delete, api_version, kind, name, ns)
        # 이름이 바뀌었거나 남은 것을 daegam.devops.ai.kr/id 라벨로 한 번 더 지운다 (폴백).
        # Ingress 는 RBAC 에 list 가 없어 이름으로만 지운다
        await delete_labeled(
            client,
            (("apps/v1", "Deployment"), ("v1", "Service"), (NETWORK_POLICY_API, "NetworkPolicy")),
            ns,
            doc.id,
        )
        # Secret 은 목록 권한이 없으므로 이름으로만 지운다 (토큰, provider 키 복사본)
        for name in [k8s.token_secret, *doc.llm_secrets]:
            await run_in_threadpool(client.delete, "v1", "Secret", name, ns)
        if forget_memories:
            # 장기 기억은 맨 나중이다. 파드가 아직 돌 때 지우면 그 사이의 쓰기가 다시 남는다 (D34)
            await self.memories.forget(doc)

    async def _teardown(self, doc: Agent, *, deleting: bool) -> None:
        """K8s 를 정리한다. 샌드박스를 먼저 닫는다. 삭제면 장기 기억, 답변 피드백, 실행 기록과
        기억한 선택도 지운다.
        실패하면 문서가 deleting 으로 남아 리컨실이 다시 지운다. **해제는 장기 기억을 남긴다.** 같은
        id 로 다시 배포되기 때문이다."""
        if self.sandboxes is not None:
            # 샌드박스를 Deployment 보다 먼저 닫는다. 파드는 ownerReference 로도 GC 되지만 기록과
            # 슬롯은 플랫폼이 닫아야 한다 (P40 5절)
            await self.sandboxes.close_agent(doc, "deleted" if deleting else "undeployed")
        if self.agent_tokens.llm is not None:
            # LLM 게이트웨이 토큰은 곧바로 회수한다. 파드도 내려간다 (P38 5절)
            await self.agent_tokens.llm.revoke_all(doc)
        await self._delete_steps(doc, forget_memories=deleting)
        if deleting:
            await self.feedback.forget_agent(doc)
            if self.run_hooks is not None:
                await self.run_hooks.forget_agent(doc.tenant, doc.id)
            if self.choices is not None:
                await self.choices.forget_agent(doc.id)
            await self._forget_evaluations(doc)

    async def _stop_running(self, doc: Agent, *, wait: bool) -> None:
        """배포 해제와 삭제가 인스턴스 토큰을 회수하기 전에 도는 턴을 멈춘다(P78 5절). 일부러
        내리는 Agent 라 턴을 기다려 주지 않고, 회수한 토큰은 기다린 턴을 실패시키기 때문이다. API
        경로(wait)는 남은 턴이 없어질 때까지 `TURN_STOP_WAIT_SECONDS` 안에서 다시 부르고, 리컨실은
        멈추라고만 한다. 기록에 `detached_turns` 가 없는 이미지에는 그 경로가 없으므로 부르지
        않는다. 클러스터나 런타임에 닿지 못하면 로그만 남기고 내리기를 이어 간다. 공유 색인이
        없으면 표시가 닿은 파드의 턴만 멈추고, 나머지 파드의 턴은 SIGTERM 과 drain 이 끝낸다. 턴을
        멈추기 전에 백그라운드 실행에 해제나 삭제의 까닭을 남긴다(P41 7절)."""
        await self._end_runs(doc)
        if doc.k8s is None or not doc.image_reports(DETACHED_TURNS):
            return
        if await self.sleep.podless(doc):
            # 드레인 표시가 어떤 턴도 시작하지 못하게 했고 남은 파드도 없다 (P44 8절)
            return
        give_up = anyio.current_time() + TURN_STOP_WAIT_SECONDS
        try:
            running = await self.proxy.stop_turns(doc, user_sub=TURN_STOP_SUB)
            if not wait:
                return
            while running:
                if anyio.current_time() >= give_up:
                    logger.warning("Agent %s 의 턴이 멈추기 전에 토큰을 회수한다", doc.id)
                    return
                await anyio.sleep(TURN_STOP_POLL_SECONDS)
                running = await self.proxy.stop_turns(doc, user_sub=TURN_STOP_SUB)
        except Exception as e:  # noqa: BLE001 - 멈추지 못한 턴은 회수한 토큰과 drain 이 끝낸다
            logger.warning("Agent %s 의 도는 턴을 멈추지 못했다: %s", doc.id, e)

    async def _runs_deployed(
        self, doc: Agent, principal: Principal, *, previous: AgentRevision | None
    ) -> list[Issue]:
        """배포가 새 리비전을 박은 뒤 백그라운드 실행의 소스를 맞춘다(P41 3·10절). 멈춘 소스가
        있으면 그 경고다. 배포는 이미 저장됐으므로 실패는 경고 로그만 남기고 소스는 시작 워커가
        시작마다 다시 본다. 미리 보기에는 실행이 없다 (P44b 7절)."""
        if self.run_hooks is None or doc.preview is not None:
            return []
        try:
            return await self.run_hooks.on_deploy(doc, principal, previous)
        except Exception as e:  # noqa: BLE001 - 저장된 배포를 실패로 돌리지 않는다
            logger.warning("agent %s: run sources not checked at deploy: %s", doc.id, e)
            return []

    async def _calls_deployed(self, doc: Agent) -> list[Issue]:
        """게시한 Agent 의 새 배포가 호출의 전제 조건을 잃었으면 그 경고다 (P46 6.1절). 배포는 이미
        저장됐으므로 실패는 경고 로그만 남긴다."""
        try:
            message = await self.publishing.deploy_warning(doc)
        except Exception as e:  # noqa: BLE001 - 저장된 배포를 실패로 돌리지 않는다
            logger.warning("agent %s: its hub item not checked at deploy: %s", doc.id, e)
            return []
        if message is None:
            return []
        return [Issue(code=CALLS_BROKEN_BY_DEPLOY, message=message, level="warning")]

    async def item_status(self, item: HubItem) -> dict[str, Any] | None:
        """agent 허브 항목의 상태. 그 Agent 의 상태라 허브 화면이 Agent 화면과 같은 phase 를 본다
        (P46 6.1절). Agent 가 없거나 draft 면 None 이다. 허브 상태는 phase 만 쓰므로 런타임 수치와
        노출 상태는 읽지 않는다."""
        agent_id = item.source.agent_id
        doc = await self.agents.get(agent_id, item.tenant) if agent_id else None
        return await self.status(doc, listing=True, with_exposure=False) if doc else None

    async def _end_runs(self, doc: Agent) -> None:
        """도는 턴을 멈추기 전에 이 Agent 의 열린 실행에 해제나 삭제의 까닭을 남긴다. 멈춘 턴의
        `turn_end` 가 그 까닭으로 실행을 끝낸다. 실패는 경고로 남긴다. 그때 실행은 `stopped` 로
        끝난다."""
        if self.run_hooks is None:
            return
        try:
            await self.run_hooks.end_agent_runs(
                doc.tenant, doc.id, deleting=doc.state == "deleting"
            )
        except Exception as e:  # noqa: BLE001 - 표시가 실패해도 해제와 삭제는 이어 간다
            logger.warning("agent %s: its runs were not marked before stopping: %s", doc.id, e)

    async def _deploy_ended(self, doc: Agent, outcome: DeployEnd) -> None:
        """그 배포의 기록 줄에 결과를 적는다 (P45 6절)."""
        await record_deploy_end(self.agents, doc, outcome)

    async def _revoke_access(self, doc: Agent) -> int:
        """인스턴스 토큰을 회수하고 프록시가 들고 있던 것을 잊는다. 회수한 수다."""
        revoked = await self.tokens.revoke_for_instance(doc.id)
        self.proxy.forget_token(doc.id)
        return revoked

    async def _after_cleared(self, doc: Agent) -> None:
        """상태 전이는 init 의 기록과 쉬는 상태를 쓰지 않으므로 따로 비운다. 파드가 사라진 뒤라 새
        기록이 들지 않는다. 쉬는 상태는 다음 배포가 awake 에서 시작한다 (P44 12절)."""
        await self._save_deployment(
            doc, {RUNTIME_RECORD}, expect_state={"undeploying"}, action="finish undeploy"
        )
        await self.agents.clear_sleep(doc.id)
        doc.sleep = AgentSleep()

    def _override_fields(self, inst: Agent) -> frozenset[str]:
        """`spec.replicas` 를 정하는 overrides 는 잠든 문서를 잠들 수 없게 한다. 같은 저장에서
        awake 로 옮긴다 (P44 6절). overrides 가 그 수로 파드를 띄운다."""
        return self.sleep.awake_after_override(inst)

    async def delete(self, doc: Agent) -> tuple[Agent, int | None]:  # type: ignore[override]
        """정의와 클러스터의 것을 모두 지운다(골격은 기반의 것). (문서, 회수한 토큰 수). 수가 None
        이면 남이 쥔 클레임에 일을 남겼다 (P66 2.6절). 미리 보기가 아닌 문서면 그 뒤 클레임 밖에서
        그 미리 보기를 모두 끝낸다(P44b 10절). 끝내지 못한 것은 끝내기 워커가 원본이 없는 것을 보고
        끝낸다."""
        revoked = await self._delete(doc)
        if doc.preview is None and self.previews is not None:
            try:
                await self.previews.end_for_source(doc)
            except Exception as e:  # noqa: BLE001 - 원본은 이미 지웠다. 워커가 잇는다
                logger.warning("agent %s: its previews were not ended: %s", doc.id, e)
        return doc, revoked

    async def _active_access(self, doc: Agent) -> list[str]:
        return [tok.id for tok in await self.tokens.list(doc.id, active_only=True)]

    async def _count_revoked(self, ids: list[str]) -> int:
        return sum((await self.tokens.revoked_flags(ids)).values())

    async def _revoke_issued(self, ids: list[str]) -> None:
        for tok_id in ids:
            await self.tokens.revoke(tok_id)

    async def _after_draft_deleted(self, doc: Agent) -> None:
        """조건부 삭제가 먼저다. 같은 draft 에 배포가 겹치면 그 삭제가 409 로 지는데, 그때 기억을
        이미 지웠으면 도는 에이전트의 사람 기억이 사라진 채 되돌릴 길이 없다. 해제를 지나 draft 로
        돌아온 문서에도 장기 기억이 남아 있다. 클러스터에 아무것도 없어도 사람의 기억은 문서보다
        오래 남으면 안 되므로 이 단계는 지난다 (D34)."""
        await self.memories.forget(doc)
        await _best_effort(doc, "hub item not deleted", self.publishing.forget(doc))
        await self._forget_feedback_of_draft(doc)
        await self._forget_runs_of_draft(doc)
        await self._forget_choices_of_draft(doc)
        await self._forget_evaluations(doc)

    async def _prune_choices(self, doc: Agent) -> None:
        """배포·재배포 뒤에 바뀐 규칙과 없어진 Tool 의 기억한 선택을 걷는다(P43 5절). 걷지 못한
        선택은 요약이 맞지 않아 전달되지 않는다."""
        if self.choices is not None:
            await _best_effort(doc, "stale approval choices not pruned", self.choices.prune(doc))

    async def _forget_choices_of_draft(self, doc: Agent) -> None:
        """draft 삭제의 마지막 단계다. 남은 선택은 만료가 걷는다."""
        if self.choices is not None:
            await _best_effort(
                doc, "approval choices not deleted", self.choices.forget_agent(doc.id)
            )

    async def _forget_feedback_of_draft(self, doc: Agent) -> None:
        """draft 삭제의 마지막 단계다. 남은 평가는 보존 기한(`expires_at`)이 지나면 TTL 이
        걷는다."""
        await _best_effort(doc, "answer feedback not deleted", self.feedback.forget_agent(doc))

    async def _forget_evaluations(self, doc: Agent) -> None:
        """삭제의 마지막 단계다. 남은 세트와 사례는 평가 정리가 Agent 가 없는 것을 보고 지운다
        (P45 8절)."""
        if self.evaluations is not None:
            forget = self.evaluations.forget_agent(doc.tenant, doc.id)
            await _best_effort(doc, "evaluation sets not deleted", forget)

    async def _forget_runs_of_draft(self, doc: Agent) -> None:
        """draft 삭제의 마지막 단계다. 남은 스케줄은 발사 워커가 Agent 가 없는 것을 보고 멈추고,
        남은 기록은 보존 기한이 지나면 TTL 이 걷는다."""
        if self.run_hooks is not None:
            forget = self.run_hooks.forget_agent(doc.tenant, doc.id)
            await _best_effort(doc, "runs and schedules not deleted", forget)

    # -- 렌더·Advanced (F7a 공통 모듈) ---------------------------------------------------

    async def render(self, doc: Agent) -> dict[str, Any]:
        """저장된 스냅샷으로 다시 렌더링한 Deployment (overrides 전). 설정 지문은 남긴 digest 를
        Secret 의 인스턴스 토큰으로 다시 걸어 provision 이 적용한 값과 같다. 그래서 Advanced 편집과
        드리프트 비교가 파드를 굴리지 않는다 (P35 13절)."""
        ctx = await self._context(doc)
        ctx.instance_token = await self.agent_tokens.known_token(doc)
        return self._render_deployment(doc, ctx)

    async def manifest(self, doc: Agent) -> dict[str, Any]:
        """{documents: [Deployment(편집 대상), Service, NetworkPolicy, 노출을 켰으면 Ingress·노출
        NetworkPolicy (전부 잠김)]}. draft 면 409 not_deployed."""
        self._require(doc, EDIT_STATES, action="read the manifest", outside="not_deployed")
        docs = [await self.editor.document(doc, await self.render(doc))]
        for rendered in self._render_side_docs(doc):
            docs.append(await self.editor.document(doc, rendered, drift=False))
        return {"documents": docs}

    # Advanced 편집 셋(`set_overrides`·`drift_reset`·`drift_absorb`)은 기반이 맡는다. 이 도메인은
    # `edit_outside` 가 not_deployed 인 것만 다르다

    # -- agent.json ----------------------------------------------------------------

    async def compile(
        self,
        doc: Agent,
        *,
        allowed: frozenset[str] | None = None,
        token_envs: dict[str, str] | None,
        gateway: bool | None = None,
        gateway_auth: str | None = None,
    ) -> dict[str, Any]:
        """핀된 리비전을 지금의 플랫폼 상태로 펼친다. 그래프가 지금 상태에서 어긋나면(provider 삭제
        등) 422 invalid_graph 다. `allowed` 는 이미지가 아는 이름으로 허락된 문서 기능이고(D65)
        없으면 요구 기능 전부다. `token_envs` 는 컴파일을 받는 파드의 MCP Server 토큰 env 표이고
        None 이면 옛 단일 토큰이다 (P50 9절). gateway 는 받는 파드의 경로이고 없으면 저장된 경로다
        (P38 6절). gateway_auth 는 받는 파드의 템플릿이 실은 Gateway 소스 인증이고 없으면 모드가
        정한다. 기능 칸을 얹은 최종 문서를 여기서 한 번 검사한다. 평가 미리 보기는 핀 뒤에 쓰기
        Tool 을 잡는다. 이름을 모르는 소스가 있으면 409 `evaluation_tools_unknown` 이다 (P45
        10절)."""
        rev = self._pinned(doc)
        ctx = await self._doc_context(doc, rev)
        ctx.mcp_token_envs = token_envs
        ctx.gateway_auth = gateway_auth
        ctx.tool_pins = valid_pins(doc)
        ctx.agent_sources = agent_calls.addressed(ctx.agent_sources, self._platform_url(doc))
        gateway = doc.llm_route.gateway if gateway is None else gateway
        ctx.llm_gateway_url = self.settings.llm_gateway_url if gateway else None
        if allowed is None:
            allowed = (await self._wanted_features(doc, ctx, gateway=gateway)).wanted
        try:
            document = await run_in_threadpool(agent_graph.compile, doc, rev, ctx)
            document = apply_pins(document, rev, ctx.tool_pins, ctx.source_tools)
            document = attach_code_mode(document, rev, ctx)
            if doc.evaluation_run is not None:
                document = _hold_evaluation_writes(document, rev, ctx)
            if agent_features.WEB_GUARD in allowed and (refs := unpinned_sources(document)):
                raise _redeploy_required(refs)
            return agent_features.finish_document(document, allowed, ctx, rev)
        except GraphError as e:
            raise AgentError.from_graph(e) from e
        except CompileError as e:
            logger.error("agent.json for %s violates the schema: %s", doc.id, e.messages)
            raise AgentError(e.status, e.code, str(e), errors=e.messages) from e

    def _platform_url(self, doc: Agent) -> str:
        """그 Agent 의 파드가 플랫폼을 부르는 주소다. 배포 스냅샷의 것이고, 스냅샷을 읽지 못하면 홈
        클러스터의 것이다."""
        effective = stored_effective(doc)
        if effective is not None:
            return effective.platform_internal_url
        return self.settings.home_platform_internal_url()

    async def fetch_config(
        self,
        doc: Agent,
        headers: Mapping[str, str],
        *,
        gateway: bool = False,
        gateway_auth: str | None = None,
    ) -> dict[str, Any]:
        """init 받기다(D65). 머리의 이름을 기록에 먼저 적고 그다음 `required` 를 판정하므로 409 로
        끝나도 그 파드가 알린 기록은 남는다. `attached` 가 빠지면 빼고 기록의 경고로 남긴다. MCP
        Server 소스의 토큰 env 는 그 파드가 보낸 표 지문의 표로 고른다 (P50 9절,
        `agent_tokens.asking_envs`). gateway 는 그 파드의 템플릿이 실은 `?llm=gateway` 이고,
        gateway_auth 는 그 템플릿이 env 로 실은 Gateway 소스 인증이다. 믿는 발급자를 바꾸는 동안
        다른 모드의 플랫폼 파드가 답해도 그 파드의 env 에 맞는 문서를 낸다."""
        reported = agent_features.parse_reported(headers)
        wanted = await self._wanted_features(doc, gateway=gateway)
        check = agent_features.check_features(wanted, reported.names)
        record = agent_features.runtime_record(reported, check)
        if record is not None:
            await self.agents.record_runtime(doc.id, record)
        if check.missing:
            detail = agent_features.missing_detail(check.missing)
            await self._record_error(doc, f"{RUNTIME_IMAGE_TOO_OLD}: {detail}")
            raise AgentError(409, RUNTIME_IMAGE_TOO_OLD, detail, missing=list(check.missing))
        token_envs = asking_envs(doc, headers.get(MCP_TOKEN_TABLE_HEADER))
        try:
            return await self.compile(
                doc,
                allowed=check.allowed,
                token_envs=token_envs,
                gateway=gateway,
                gateway_auth=gateway_auth,
            )
        except AgentError as e:
            if e.code in (REDEPLOY_REQUIRED, EVALUATION_TOOLS_UNKNOWN):
                await self._record_error(doc, f"{e.code}: {e.detail}")
            raise

    async def skill_bundle(self, doc: Agent, name: str) -> dict[str, Any]:
        """init 의 Skill 받기다(D64 7절, `AgentSkillStore.bundle`)."""
        return await self.skills.bundle(self._pinned(doc), name)

    async def _providers_by_name(self) -> dict[str, LLMProvider]:
        """문서 기능을 셀 때 보는 provider 표다. 검증·컴파일 문맥(`context`)의 것과 같다."""
        return (await self.platform_settings.get()).providers_by_name

    async def _feature_check(
        self,
        doc: Agent,
        rev: AgentRevision,
        effective: EffectiveConfig,
        ctx: GraphContext,
        pins: ToolPins,
    ) -> list[Issue]:
        """배포·재배포 전 검사다(D65). 배포할 이미지의 기록이 있을 때만 `required` 가 빠지면 409
        이고 그 밖은 경고다. rev·effective 는 이 배포가 핀할 리비전과 스냅샷이고, ctx 는 그 요청이
        읽은 검증 문맥이다. 웹 Tool 은 이번 배포가 박을 핀(pins)으로 세고, 바인딩 Database 는 그
        문맥이 막 읽은 것을 쓴다. 게시한 Agent 를 Tool 로 쓰는데 이미지가 `agent_tools` 를, 작업을
        맡기는데 `agent_tasks` 를 모르면 409 `runtime_feature_missing` 이다 (P46 6.2·6.7절)."""
        wanted = await self._wanted_features(doc, ctx, rev=rev, pins=pins, effective=effective)
        check, warnings = agent_features.deploy_check(doc, effective.images.agent_image, wanted)
        for feature in (agent_features.AGENT_TOOLS, agent_features.AGENT_TASKS):
            if check is not None and feature in check.missing:
                raise _call_feature_missing(feature)
        if check is not None and check.missing:
            raise _image_too_old(check.missing)
        return warnings


def _hold_evaluation_writes(
    document: dict[str, Any], rev: AgentRevision, ctx: GraphContext
) -> dict[str, Any]:
    """평가 미리 보기의 쓰기 Tool 을 잡는다 (P45 10절, `evaluation.holds.hold_writes`). 이름을
    모르는 소스가 있으면 409 `evaluation_tools_unknown` 이다."""
    try:
        return hold_writes(document, rev, ctx)
    except ToolsUnknown as e:
        raise _tools_unknown(e.refs) from e


def _tools_unknown(refs: list[str]) -> AgentError:
    """평가 미리 보기에 이름을 모르는 tools 노드가 있을 때의 409 다 (P45 10절). 무엇이 쓰기인지
    가를 수 없어 쓰기 Tool 을 잡을 수 없다."""
    return AgentError(
        409,
        EVALUATION_TOOLS_UNKNOWN,
        "evaluation needs the tool names of every tools node, to hold its write tools: "
        + ", ".join(refs),
        nodes=refs,
    )


def _redeploy_required(refs: list[str]) -> AgentError:
    """웹 Tool 이 생긴 그래프에 배포 때 이름을 박지 않은 tools 노드가 있을 때의 409 다(P39b 14절
    5부). 빈 allow 는 소스의 모든 Tool 을 다시 읽으므로 웹 가드가 이름을 정할 수 없다. 재배포가
    핀을 박는다."""
    return AgentError(
        409,
        REDEPLOY_REQUIRED,
        "the graph now has web tools, and these tools nodes were deployed without tool pins; "
        "redeploy the agent: " + ", ".join(refs),
        nodes=refs,
    )


def _call_feature_missing(feature: str) -> AgentError:
    """다른 Agent 를 Tool 로 부르거나 작업을 맡기지 못하는 이미지에 그런 그래프를 배포할 때의 409
    다 (P46 6.2·6.7절)."""
    return AgentError(
        409,
        RUNTIME_FEATURE_MISSING,
        f"the runtime image has not reported the {feature!r} feature that this graph's agent "
        "sources need; redeploy on a newer image",
        feature=feature,
    )


def _image_too_old(missing: tuple[str, ...]) -> AgentError:
    """핀한 이미지가 요구 기능을 모를 때의 409 다. 요청은 맞고 클러스터의 런타임 이미지가 막는
    상태다 (P74 2.3절). 배포 전 검사와 롤 전 검사가 함께 쓰고, 재적용은 이 코드에서 문서를 막는다
    (`daegam.error_codes.holds_reapply`)."""
    return AgentError(
        409, RUNTIME_IMAGE_TOO_OLD, agent_features.missing_detail(missing), missing=list(missing)
    )


def _verifier_args(
    issuer: str | None,
    jwks_url: str | None,
    insecure_http: bool,
    client_ids: Iterable[str],
    client_id_claim: str,
    groups_claim: str | None,
    **groups: Any,
) -> dict[str, Any]:
    """`render_agent_deployment` 가 DAEGAM_OIDC_* 로 내리는 발급자 하나의 인자."""
    return {
        "oidc_issuer": issuer,
        "oidc_jwks_url": jwks_url,
        "oidc_insecure_http": insecure_http,
        "agent_client_ids": list(client_ids),
        "client_id_claim": client_id_claim,
        "groups_claim": groups_claim,
        **groups,
    }


def exposure_verifiers(auth: ConsumerAuth, doc: Agent) -> dict[str, Any]:
    """노출 Agent 런타임이 사람 토큰을 검증할 입력 (D27, P53b 6.8절). `render_agent_deployment` 의
    인자다. 데이터 플레인이 플랫폼을 믿는 oidc 모드(`ConsumerAuth.idp`)에서 이미지가 두 발급자를
    알리면(`TWO_ISSUERS`) 런타임의 계약(`auth.read_issuers`)대로 플랫폼을 첫 발급자(DAEGAM_OIDC_*)로
    local 모드처럼 둔다. 클러스터 안 JWKS 주소가 있고, IdP 그룹은 테넌트 이름을 붙인 꼴이며 빈
    목록은 고정 그룹이 채운다. client id 는 비우고, 클레임 이름은 플랫폼 토큰의 것이다(그때
    `consumer_auth` 가 IdP 의 이름을 싣지 않는다). IdP 는 그 곁(DAEGAM_IDP_*)에 IdP 를 믿을 때의
    클레임 이름과 client id 로 두되, 플랫폼 그룹으로만 좁힌 Agent 에는 두지 않는다
    (`idp_allowed_groups`). 옛 이미지는 IdP 하나를 IdP 를 믿을 때와 같은 입력으로 둔다. 두 발급자가
    같은 설치는 `Settings` 가 기동에서 막는다. 그 밖의 모드는 소비자 입력 그대로다."""
    trusted = _verifier_args(
        auth.issuer,
        auth.jwks_url,
        auth.insecure_http,
        auth.agent_client_ids,
        auth.client_id_claim,
        auth.groups_claim,
        default_group=auth.exposure_default_group,
    )
    idp = auth.idp
    if idp is None:
        return trusted
    if not doc.image_reports(TWO_ISSUERS):
        return _verifier_args(
            idp.issuer,
            None,
            idp.insecure_http,
            auth.agent_client_ids,
            idp.client_id_claim,
            idp.groups_claim,
            default_group=None,
        )
    stored = list(doc.exposure.allowed_groups)
    trusted.update(agent_client_ids=[], allowed_groups=platform_allowed_groups(doc.tenant, stored))
    idp_groups = idp_allowed_groups(stored)
    if idp_groups is not None:
        trusted["idp_verifier"] = IdpVerifier(idp, idp_groups, auth.agent_client_ids)
    return trusted
