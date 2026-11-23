"""앱 조립. main.py 의 lifespan 은 여기 함수를 순서대로 부르기만 한다.

build_stores 가 Mongo 스토어(`Stores` 의 필드)를, build_services 가 서비스와 그 사이의
훅(빠진 것이 없는지 `_check_wiring` 이 본다), 백그라운드 워커 일곱(reconciler, audit
collector, spec worker, knowledge indexer, usage rollup, MCP metrics rollup, OTLP export)을
만든다. 무엇을 만들고 무엇을 무엇에 붙이는지는 여기서 한눈에 읽힌다. 시작 순서는 D42 이전
데이터 가드, init_schema, bootstrap_home, 함께 올린 Redis 의 TLS 끝점 받기, reconciler, audit
collector, spec worker, knowledge indexer, usage rollup, MCP metrics rollup, OTLP export 순이고
종료는 TLS 끝점 받기, usage rollup 과 MCP metrics rollup 과 OTLP export, audit collector,
reconciler, 비서 턴 태스크, OIDC, spec worker, knowledge indexer, Mongo 순이다. 런타임 이벤트는 수집
경로와 audit collector 가 같은 `TelemetryWriter` 로 넣고, 그 writer 가 원장과 OTLP 대기열과 답변
피드백 훅에 넘긴다.

비서(D31)는 ASGI 앱과 OpenAPI dict 만 받는다. `services` 가 `daegam.api` 를 import 하지 않게
하려는 것이다. 표에 있는데 OpenAPI 에 없는 동작은 `CatalogError` 로 기동을 막는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from pymongo.asynchronous.database import AsyncDatabase
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp

from daegam.audit.mcp_metrics import MongoMcpMetricsStore
from daegam.audit.store import MongoAuditStore
from daegam.auth.oidc import OIDCVerifier
from daegam.auth.pat_keys import PatKeyRing
from daegam.auth.sandbox_key import SandboxKey
from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import PlatformSigner
from daegam.config import Settings
from daegam.k8s.client import set_request_hook
from daegam.secrets import SecretStore
from daegam.services.access_revocation import AccessRevoker, LostAccessHook
from daegam.services.agent_call_runs import AgentCallRuns
from daegam.services.agent_citations import CitationSources
from daegam.services.agent_feedback import AgentFeedbackService
from daegam.services.agent_preview import AgentPreviews, AgentPreviewWorker
from daegam.services.agent_sleep import AgentSleepWorker
from daegam.services.agents import AgentService
from daegam.services.approval_choices import ApprovalChoiceService
from daegam.services.assistant.catalog import CATALOG
from daegam.services.assistant.schema import build_tool_schemas
from daegam.services.assistant.service import AssistantService
from daegam.services.audit_collector import AuditCollector
from daegam.services.backend_apis import BackendAPIService
from daegam.services.channels.bot import TeamsBot
from daegam.services.channels.connector import TeamsConnector
from daegam.services.channels.links import CODE_FAILURES, CODE_LOCK, CODE_WINDOW, ChannelLinks
from daegam.services.channels.voice import TeamsVoice
from daegam.services.chat_uploads import ChatUploadService
from daegam.services.claims import Claims, request_hook
from daegam.services.clusters import ClientFactory, ClusterService
from daegam.services.condition_records import ConditionRecords
from daegam.services.data_plane_auth import DataPlaneAuth
from daegam.services.data_services import DataServiceService
from daegam.services.docparse import DocparseClient
from daegam.services.entry_conversion import EntryConversion
from daegam.services.evaluation.copies import CONSENT_ENDING, EvaluationCopies
from daegam.services.evaluation.gate import DeployGate
from daegam.services.evaluation.grader import EvaluationGrader
from daegam.services.evaluation.runner import EvaluationRunner, EvaluationRunWorker
from daegam.services.evaluation.runs import EvaluationRuns
from daegam.services.evaluation.sets import EvaluationSets
from daegam.services.evaluation.sweep import EvaluationSweep
from daegam.services.gateways import GatewayService
from daegam.services.hub import HubService
from daegam.services.hub_reapply import HubItemReapply
from daegam.services.hub_tools import HubToolNames
from daegam.services.issuer_switch import IssuerSwitch, MongoIssuerSwitchStore
from daegam.services.jwks_heal import JwksHeal
from daegam.services.knowledge.indexer import KnowledgeIndexer
from daegam.services.knowledge.service import KnowledgeService
from daegam.services.lease_worker import LeaseWorker
from daegam.services.llm_endpoint import LlmEndpoints
from daegam.services.llm_gateway import LlmGateway
from daegam.services.llm_tokens import LlmTokens
from daegam.services.local_auth import LocalAuthService
from daegam.services.mcp_metrics.reports import McpMetricsReports
from daegam.services.mcp_metrics.rollup import McpMetricsRollup
from daegam.services.mcp_servers import MCPServerService
from daegam.services.memberships import MembershipService
from daegam.services.named_groups import NamedGroups
from daegam.services.oauth.authorize import OAuthAuthorizer
from daegam.services.oauth.clients import OAuthClients
from daegam.services.oauth.consent import OAuthConsent
from daegam.services.oauth.tokens import OAuthTokens
from daegam.services.personal_tokens import PersonalTokenService
from daegam.services.platform_tools import PlatformTools
from daegam.services.reconcile import Reconciler
from daegam.services.references import ReferenceService
from daegam.services.rule_compile import RuleScopes
from daegam.services.runs.events import RunEvents
from daegam.services.runs.fire import ScheduleFireWorker
from daegam.services.runs.person import BoundPersonCheck
from daegam.services.runs.service import RunService
from daegam.services.runs.start import RunStartWorker
from daegam.services.runs.trigger_secrets import TriggerSecrets
from daegam.services.runs.trigger_sweep import TriggerSecretSweep
from daegam.services.runs.triggers import RunTriggers
from daegam.services.runs.turns import RunTurns
from daegam.services.runs.watch import RunWatchWorker
from daegam.services.runs.webhooks import WebhookReceiver
from daegam.services.sandbox_check import SandboxChecks
from daegam.services.sandbox_sweep import SandboxSweep
from daegam.services.sandboxes import SandboxService
from daegam.services.service_accounts import ServiceAccountService
from daegam.services.shared_clones import SharedCloneReapply, SharedClones
from daegam.services.shipped_redis import adopt_shipped_redis_tls, mark_shipped_plain
from daegam.services.skills import SkillService
from daegam.services.spec_generator.loop import SpecGeneratorRunner
from daegam.services.spec_generator.worker import SpecGeneratorWorker, SpecJobRunner
from daegam.services.svc_token_sweep import SvcTokenSweeper
from daegam.services.svc_tokens import SvcTokens
from daegam.services.telemetry.agent_metrics import AgentMetrics
from daegam.services.telemetry.gateway_usage import GatewayUsageIngest
from daegam.services.telemetry.gateway_usage import provider_names as gateway_provider_names
from daegam.services.telemetry.ingest import CONCURRENCY as INGEST_CONCURRENCY
from daegam.services.telemetry.ingest import TelemetryIngest
from daegam.services.telemetry.otlp_export import OtlpExport
from daegam.services.telemetry.traces import AgentTraces
from daegam.services.telemetry.writer import TelemetryWriter
from daegam.services.tenancy import TenancyService
from daegam.services.tool_effects import ToolEffectLoader
from daegam.services.turn_grants import TurnGrantService
from daegam.services.turn_person import TurnDelegation
from daegam.services.usage.ledger import UsageLedger
from daegam.services.usage.pricing import PriceBook
from daegam.services.usage.reports import UsageReports
from daegam.services.usage.rollup import UsageRollup
from daegam.services.web_search import WebSearch
from daegam.store.agent_events import MongoAgentEventStore
from daegam.store.agent_feedback import MongoAgentFeedbackStore
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore
from daegam.store.agent_previews import MongoAgentPreviewStore
from daegam.store.agent_runs import MongoAgentRunStore
from daegam.store.agent_schedules import MongoAgentScheduleStore
from daegam.store.agent_session_owners import MongoAgentSessionOwnerStore
from daegam.store.agent_triggers import MongoAgentTriggerStore
from daegam.store.agents import MongoAgentStore
from daegam.store.approval_choices import MongoApprovalChoiceStore
from daegam.store.assistant_sessions import MongoAssistantSessionStore
from daegam.store.auth_reapply import MongoAuthReapplyStore
from daegam.store.backend_apis import MongoBackendAPIStore
from daegam.store.base import utcnow
from daegam.store.channels import MongoChannelStore
from daegam.store.chat_uploads import MongoChatUploadStore
from daegam.store.clusters import MongoClusterStore
from daegam.store.data_services import MongoDataServiceStore
from daegam.store.evaluation import MongoEvaluationStore
from daegam.store.evaluation_runs import MongoEvaluationRunStore
from daegam.store.exposure_hosts import MongoExposureHostStore
from daegam.store.files import GridFSSpecFiles
from daegam.store.gateway_entry_names import MongoEntryNameStore
from daegam.store.gateways import MongoGatewayStore
from daegam.store.hook_secrets import MongoHookSecretStore
from daegam.store.hub import MongoHubStore
from daegam.store.hub_env_secrets import MongoHubEnvSecretStore
from daegam.store.idp_people import MongoIdpPeopleStore
from daegam.store.knowledge import KnowledgeFiles, MongoKnowledgeStore
from daegam.store.leases import Lease, init_lease_schema
from daegam.store.llm_gateway import MongoLlmGatewayStore
from daegam.store.llm_revocations import MongoLlmRevocationStore
from daegam.store.login_throttle import MongoLoginThrottle
from daegam.store.mcp_servers import MongoMCPServerStore
from daegam.store.memberships import MongoMembershipStore
from daegam.store.oauth_codes import MongoOAuthCodeStore
from daegam.store.oauth_consent_uses import MongoOAuthConsentUseStore
from daegam.store.oauth_grants import MongoOAuthGrantStore
from daegam.store.observability import MongoObservabilityStore
from daegam.store.otlp import MongoOtlpStore
from daegam.store.outside_header_secrets import MongoOutsideHeaderSecretStore
from daegam.store.pat_keys import MongoPatKeyStore
from daegam.store.personal_tokens import MongoPersonalTokenStore, Revocation
from daegam.store.pricing import MongoPricingStore
from daegam.store.sandboxes import MongoSandboxStore
from daegam.store.service_accounts import MongoServiceAccountStore
from daegam.store.settings import MongoPlatformSettingsStore
from daegam.store.shared_clones import MongoSharedCloneStore
from daegam.store.skills import MongoSkillStore
from daegam.store.spec_jobs import MongoSpecJobStore, SpecJobFiles
from daegam.store.telemetry import MongoAgentTelemetryStore, MongoTelemetryIngestStore
from daegam.store.tenants import MongoTenantSettingsStore
from daegam.store.turn_grants import MongoTurnGrantStore
from daegam.store.usage import MongoUsageStore
from daegam.store.users import MongoUserStore
from daegam.store.web_search import MongoWebSearchCountStore

logger = logging.getLogger("daegam.wiring")


@dataclass
class Stores:
    """Mongo 스토어 묶음. backend_apis 가 클라이언트를 열고 나머지는 그 db 를 나눠 쓴다."""

    backend_apis: MongoBackendAPIStore
    files: GridFSSpecFiles
    clusters: MongoClusterStore
    platform_settings: MongoPlatformSettingsStore
    tenant_settings: MongoTenantSettingsStore
    data_services: MongoDataServiceStore
    mcp_servers: MongoMCPServerStore
    hub_items: MongoHubStore
    gateways: MongoGatewayStore
    agents: MongoAgentStore
    # 미리 보기의 테넌트 자리, 하루 시작 수, 끝난 까닭 (P44b 10절)
    agent_previews: MongoAgentPreviewStore
    agent_instance_tokens: MongoAgentInstanceTokenStore
    spec_jobs: MongoSpecJobStore
    spec_job_files: SpecJobFiles
    memberships: MongoMembershipStore
    audit: MongoAuditStore
    # Gateway·MCP Server 메트릭의 시간 묶음 (P51 3절). 감사 이벤트에서 다시 센다
    mcp_metrics: MongoMcpMetricsStore
    assistant_sessions: MongoAssistantSessionStore
    knowledge: MongoKnowledgeStore
    knowledge_files: KnowledgeFiles
    # local 모드 (D40). 다른 모드에서도 만들어 두되 쓰이지 않는다
    users: MongoUserStore
    # oidc 모드의 사람 기록 (P53b 6.3절). IdP 가 그 사람을 마지막으로 보증한 때와 그룹이다
    idp_people: MongoIdpPeopleStore
    login_throttle: MongoLoginThrottle
    # 인증 재적용의 요약과 클러스터별 상태 (P32 8절)
    auth_reapply: MongoAuthReapplyStore
    # 개인 토큰의 서명 키 슬롯 (P32 4절). 플랫폼이 사람 토큰을 내는 모드(local, oidc)에서만 채운다
    pat_keys: MongoPatKeyStore
    # 개인 MCP 토큰 (P32 5절). 토큰 값은 없고 메타데이터와 발급 때의 tv 다
    personal_tokens: MongoPersonalTokenStore
    # 서비스 어카운트 (P53b 6.7절). 토큰은 personal_tokens 에 sub `sa:<계정 id>` 로 있다
    service_accounts: MongoServiceAccountStore
    # OAuth 요청 핸들의 사용 기록과 인가 코드 (P33 3절·8절). 인가가 성공한 뒤에만 쓴다
    oauth_consent_uses: MongoOAuthConsentUseStore
    oauth_codes: MongoOAuthCodeStore
    # OAuth 연결과 그 갱신 토큰 가족 (P33 11절). 해시만 든다
    oauth_grants: MongoOAuthGrantStore
    # Agent 답변의 피드백 (P36 6절). 턴마다 한 건인 현재 상태다
    agent_feedback: MongoAgentFeedbackStore
    # Agent 의 평가 세트와 사례 (P45 7절)
    evaluation: MongoEvaluationStore
    evaluation_runs: MongoEvaluationRunStore
    # 채팅 첨부의 업로드와 사람마다의 분당 칸 (P36 9절). 1시간 TTL 이고 턴에 실리면 지운다
    chat_uploads: MongoChatUploadStore
    # Skill 과 그 리비전 (D64). 리비전은 본문까지 든 불변 문서다
    skills: MongoSkillStore
    # 공유 복제본의 기록 (P50 1절). 마지막으로 렌더한 원본 정의를 든다
    shared_clones: MongoSharedCloneStore
    # 공개 host 의 선점 기록 (P70, D82)
    exposure_hosts: MongoExposureHostStore
    # Gateway 의 Entry 이름의 선점 기록 (D82)
    entry_names: MongoEntryNameStore
    # Agent 런타임의 관측 이벤트와 파드별 수집 수 (P37 3절·5절)
    agent_events: MongoAgentEventStore
    agent_telemetry: MongoAgentTelemetryStore
    # 게이트웨이 사용량 토큰의 클러스터별 회수 시각 (P37 8절)
    telemetry_revocations: MongoTelemetryIngestStore
    # OTLP 내보내기의 대기열과 상태, 그 설정 문서 (P37 15절)
    otlp: MongoOtlpStore
    observability_settings: MongoObservabilityStore
    # LLM 게이트웨이 동기화의 상태 문서와 회수 목록 (P38 4절·5절)
    llm_gateway: MongoLlmGatewayStore
    llm_revocations: MongoLlmRevocationStore
    # 가격표와 LLM 호출의 원장 (P37 6절·7절)
    pricing: MongoPricingStore
    usage: MongoUsageStore
    # 웹 검색의 테넌트 상한 칸 (P39b 12절)
    web_search_counts: MongoWebSearchCountStore
    # Agent 세션의 소유자 기록 (P53 5.1절). 소유자 자신의 요청이 받아들여졌을 때 쓴다
    agent_session_owners: MongoAgentSessionOwnerStore
    # 턴 위임 (P53 4절). 토큰은 없고 갱신 비밀의 해시와 대상이다
    turn_grants: MongoTurnGrantStore
    # 백그라운드 실행의 기록과 스케줄 (P41 1·3절)
    agent_runs: MongoAgentRunStore
    agent_schedules: MongoAgentScheduleStore
    agent_triggers: MongoAgentTriggerStore
    # 웹훅 트리거의 Secret 을 가진 Agent. 리컨실의 쓸기가 Secret 을 이름으로 찾는다 (P41 4절)
    hook_secrets: MongoHookSecretStore
    outside_header_secrets: MongoOutsideHeaderSecretStore
    # image 항목의 Secret env 값을 둔 테넌트 Secret (P55 5절). 리컨실의 정리가 이름으로 찾는다
    hub_env_secrets: MongoHubEnvSecretStore
    # 사람이 기억한 승인 선택 (P43 5절)
    approval_choices: MongoApprovalChoiceStore
    # 코드 실행 샌드박스의 기록과 슬롯 수, 프로브 (P40 5절·7절)
    sandboxes: MongoSandboxStore
    # Teams 채널의 연결, 연결 코드, 대화, 받은 활동 (P47 6.2절·6.3절)
    channels: MongoChannelStore

    @property
    def db(self) -> AsyncDatabase:
        return self.backend_apis.db

    async def init_schemas(self) -> None:
        """스토어마다 init_schema 를 돈다."""
        # 필드 순서대로 돈다. 새 스토어를 필드로 더하면 여기도 저절로 들어간다
        for f in dataclasses.fields(self):
            await getattr(self, f.name).init_schema()
        # 리스 컬렉션은 스토어가 없으므로 따로 둔다. 멈춘 파드가 남긴 클레임을 TTL 이 걷는다 (P66)
        await init_lease_schema(self.db)

    async def close(self) -> None:
        await self.backend_apis.close()


def oidc_verifier_from_settings(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> OIDCVerifier:
    """플랫폼 Settings 로 검증기를 조립한다. auth/oidc.py 는 agent-runtime 과 바이트 동일한 파일이라
    Settings 를 모르며, 그 결합은 여기 한 곳에만 있다(D27)."""
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise RuntimeError(
            "DAEGAM_AUTH_MODE=oidc requires DAEGAM_OIDC_ISSUER and DAEGAM_OIDC_AUDIENCE "
            "(set DAEGAM_AUTH_MODE=header for local development)"
        )
    return OIDCVerifier(
        settings.oidc_issuer,
        settings.oidc_audience,
        http_client=http_client,
        groups_claim=settings.oidc_groups_claim,
        client_id_claim=settings.oidc_client_id_claim,
        insecure_http=settings.oidc_insecure_http,
    )


def build_stores(settings: Settings) -> Stores:
    """스토어들. 생성만 하고 Mongo 에는 손대지 않는다 (init_schemas 이 한다)."""
    backend_apis = MongoBackendAPIStore(settings.mongo_url, settings.mongo_db)
    db = backend_apis.db
    return Stores(
        backend_apis=backend_apis,
        files=GridFSSpecFiles(db),
        clusters=MongoClusterStore(db),
        platform_settings=MongoPlatformSettingsStore(db),
        tenant_settings=MongoTenantSettingsStore(db),
        data_services=MongoDataServiceStore(db),
        mcp_servers=MongoMCPServerStore(db),
        hub_items=MongoHubStore(db),
        gateways=MongoGatewayStore(db),
        agents=MongoAgentStore(db),
        agent_previews=MongoAgentPreviewStore(db),
        agent_instance_tokens=MongoAgentInstanceTokenStore(db),
        spec_jobs=MongoSpecJobStore(db),
        spec_job_files=SpecJobFiles(db),
        memberships=MongoMembershipStore(db),
        audit=MongoAuditStore(db, ttl_days=settings.audit_ttl_days),
        mcp_metrics=MongoMcpMetricsStore(db, ttl_days=settings.audit_ttl_days),
        assistant_sessions=MongoAssistantSessionStore(db),
        knowledge=MongoKnowledgeStore(db),
        knowledge_files=KnowledgeFiles(db),
        users=MongoUserStore(db),
        idp_people=MongoIdpPeopleStore(db),
        login_throttle=MongoLoginThrottle(db),
        auth_reapply=MongoAuthReapplyStore(db),
        pat_keys=MongoPatKeyStore(db),
        personal_tokens=MongoPersonalTokenStore(db),
        service_accounts=MongoServiceAccountStore(db),
        oauth_consent_uses=MongoOAuthConsentUseStore(db),
        oauth_codes=MongoOAuthCodeStore(db),
        oauth_grants=MongoOAuthGrantStore(db),
        agent_feedback=MongoAgentFeedbackStore(db),
        evaluation=MongoEvaluationStore(db),
        evaluation_runs=MongoEvaluationRunStore(db),
        chat_uploads=MongoChatUploadStore(db),
        skills=MongoSkillStore(db),
        shared_clones=MongoSharedCloneStore(db),
        exposure_hosts=MongoExposureHostStore(db),
        entry_names=MongoEntryNameStore(db),
        agent_events=MongoAgentEventStore(db, ttl_days=settings.agent_events_ttl_days),
        agent_telemetry=MongoAgentTelemetryStore(db),
        telemetry_revocations=MongoTelemetryIngestStore(db),
        otlp=MongoOtlpStore(db),
        observability_settings=MongoObservabilityStore(db),
        llm_gateway=MongoLlmGatewayStore(db),
        llm_revocations=MongoLlmRevocationStore(db),
        pricing=MongoPricingStore(db),
        usage=MongoUsageStore(db, retention_days=settings.usage_retention_days),
        web_search_counts=MongoWebSearchCountStore(db),
        agent_session_owners=MongoAgentSessionOwnerStore(db),
        turn_grants=MongoTurnGrantStore(db),
        agent_runs=MongoAgentRunStore(db),
        agent_schedules=MongoAgentScheduleStore(db),
        agent_triggers=MongoAgentTriggerStore(db),
        hook_secrets=MongoHookSecretStore(db),
        outside_header_secrets=MongoOutsideHeaderSecretStore(db),
        hub_env_secrets=MongoHubEnvSecretStore(db),
        approval_choices=MongoApprovalChoiceStore(db),
        sandboxes=MongoSandboxStore(db),
        channels=MongoChannelStore(db),
    )


@dataclass
class Services:
    """서비스와 백그라운드 워커 묶음. start/stop 이 기동·종료 순서를 정한다."""

    settings: Settings
    oidc: OIDCVerifier | None
    cluster_service: ClusterService
    tenancy: TenancyService
    mcp_server_service: MCPServerService
    hub_service: HubService
    gateway_service: GatewayService
    agent_service: AgentService
    # Agent 답변의 피드백 (P36 6절). Agent·세션 삭제와 수집기 훅이 평가를 지운다
    agent_feedback_service: AgentFeedbackService
    evaluation_sets: EvaluationSets
    evaluation_copies: EvaluationCopies
    # 배포 게이트의 설정과 판정 (P45 12절)
    deploy_gate: DeployGate
    # 사람이 기억한 승인 선택 (P43 5절)
    approval_choices: ApprovalChoiceService
    # 답이 인용한 Knowledge 문서의 원문 (P36 7절)
    citation_sources: CitationSources
    # 채팅 첨부의 업로드와 턴에 싣기 (P36 9절). 문서의 글은 daegam-docparse 가 뽑는다
    chat_uploads: ChatUploadService
    reconciler: Reconciler
    audit_collector: AuditCollector
    # settings.spec_worker_enabled 가 거짓이면 None
    spec_worker: SpecGeneratorWorker | None
    assistant: AssistantService
    knowledge_service: KnowledgeService
    references: ReferenceService
    data_service_service: DataServiceService
    backend_api_service: BackendAPIService
    # Skill (D64). 삭제 가드는 참조 세기를 그대로 쓴다
    skill_service: SkillService
    # settings.knowledge_indexer_enabled 가 거짓이면 None (D32)
    knowledge_indexer: KnowledgeIndexer | None
    # 플랫폼 발급자 (D41). 서명 키는 start() 가 home 클러스터 Secret 에서 붙인다
    internal_issuer: InternalIssuer
    # 인증 재적용 (P32 8절). 리컨실의 한 단계이고 상태 API 가 읽는다
    data_plane_auth: DataPlaneAuth
    # 공유 복제본의 기록 (P50 1절). 스윕이 리컨실의 한 단계다
    shared_clones: SharedClones
    # 플랫폼보다 먼저 떠 JWKS 를 받지 못한 검증기 다시 띄우기 (T24). 리컨실의 한 단계다
    jwks_heal: JwksHeal
    # 플랫폼 JWKS 와 개인 토큰 키 슬롯의 pool (P32 4절). JWKS 라우트와 리컨실이 쓴다
    pat_keys: PatKeyRing
    # 멤버 교체와 빠진 사람의 개인 토큰 회수 (P32 7절)
    membership_service: MembershipService
    # 가격표와 원장에 쓰는 한 곳 (P37 6절·7절)
    price_book: PriceBook
    usage_ledger: UsageLedger
    # 시간 묶음 롤업과 사용량 보고 (P37 10절·13절)
    usage_rollup: UsageRollup
    usage_reports: UsageReports
    # Gateway·MCP Server 메트릭의 시간 묶음 롤업과 그것을 읽는 보고 (P51 3절·4절)
    mcp_metrics_rollup: McpMetricsRollup
    mcp_metrics_reports: McpMetricsReports
    # Agent 런타임 이벤트를 넣는 한 곳과 그 수집 경로 (P37 3절·4절)
    telemetry_writer: TelemetryWriter
    telemetry_ingest: TelemetryIngest
    # LLM 게이트웨이의 사용량 수집 (P37 8절)
    gateway_usage: GatewayUsageIngest
    # Agent 지표와 턴·트레이스. 이벤트 저장소와 원장에서 읽는다 (P37 11절·12절)
    agent_metrics: AgentMetrics
    agent_traces: AgentTraces
    # 저장소에서 실행 단위로 내보내는 OTLP (P37 15절)
    otlp_export: OtlpExport
    # 플랫폼 web_search (P39b 12절). 검색 엔드포인트와 설정의 연결 확인이 쓴다
    web_search: WebSearch
    # 턴 위임과 그 토큰 (P53 4절). 채팅·결정·다시 생성 라우트가 열고 런타임이 갱신한다
    turn_grants: TurnGrantService
    # 다른 Agent 를 Tool 로 부르는 호출 경로의 판단 (P46 6.3절)
    agent_calls: AgentCallRuns
    # 백그라운드 실행의 스케줄과 기록 (P41)
    run_service: RunService
    # 웹훅 트리거의 관리와 공개 경로의 판단 (P41 4절)
    run_triggers: RunTriggers
    webhooks: WebhookReceiver
    # 발사·시작·지켜보기 워커. settings.runs_enabled 와 runs_workers_enabled 가 둘 다 참일 때만 있다
    run_workers: tuple[LeaseWorker[Any], ...]
    # 플랫폼 웹 Tool 의 허브 항목 (P39b 13절). 설정 저장과 리컨실 단계가 맞춘다
    platform_tools: PlatformTools
    # LLM 게이트웨이 (P38 4절). 리컨실 단계와 키 저장 경로와 상태 API 가 쓴다
    llm_gateway: LlmGateway
    # 코드 실행 샌드박스 (P40). 토큰 키는 start() 가 home 클러스터 Secret 에서 붙인다
    sandboxes: SandboxService
    # 샌드박스 스윕 (P40 5절). 리컨실을 켠 배포본에서만 돈다
    sandbox_sweep: SandboxSweep
    # 쓰지 않는 Agent 를 재우고 매일 깨우는 워커 (P44 7절·11절). 리컨실을 켠 배포본에서만 돈다
    agent_sleep_worker: AgentSleepWorker
    # Agent 미리 보기와 그 끝내기 워커 (P44b, D107). 워커는 리컨실을 켠 배포본에서만 돈다
    agent_previews: AgentPreviews
    agent_preview_worker: AgentPreviewWorker
    # 평가 실행기와 그 워커 (P45 9절)
    evaluation_runner: EvaluationRunner
    evaluation_run_worker: EvaluationRunWorker
    evaluation_runs: EvaluationRuns
    # 클러스터의 격리 점검과 허용, 스윕의 shared_nodes·프로브 단계 (P40 8절)
    sandbox_checks: SandboxChecks
    # Teams 채널 (P47). 봇 토큰과 Connector 호출, 연결, 메시지 엔드포인트의 판단이다
    teams_connector: TeamsConnector
    channel_links: ChannelLinks
    teams_bot: TeamsBot
    # 사람 인증은 auth_mode=local 에서만, 개인 토큰은 플랫폼이 사람 토큰을 내는 모드(local,
    # oidc)에서 있다 (D40, P32, P53b 6.5절)
    local_auth: LocalAuthService | None = None
    personal_tokens: PersonalTokenService | None = None
    # 서비스 어카운트와 그 토큰 (P53b 6.7절). 개인 토큰이 있는 모드에 있다
    service_accounts: ServiceAccountService | None = None
    # MCP 클라이언트의 OAuth 인가 서버 (P33). 꺼지지 않았을 때 있다(잠든 서버 포함, P53b 6.5절)
    oauth_clients: OAuthClients | None = None
    oauth_authorizer: OAuthAuthorizer | None = None
    oauth_consent: OAuthConsent | None = None
    oauth_tokens: OAuthTokens | None = None
    # 기동 뒤 뒤에서 도는 Database CA 지문 맞추기 (P61). stop 이 끝나지 않았으면 취소한다
    ca_fingerprints: asyncio.Task[int] | None = field(default=None, init=False)

    async def start(self) -> None:
        await self.cluster_service.bootstrap_home()
        # 서명 키는 home 클러스터의 Secret 에 있어 bootstrap_home 뒤다. 모든 모드에서 붙인다(D41).
        # home 이 없으면 데이터 플레인도 없으므로 발급자 없이 뜬다(JWKS 는 503). local 은 사람
        # 토큰이 여기 달려 있어 그때 죽는다
        store = self.cluster_service.secret_store()
        if store is not None:
            signer = await run_in_threadpool(
                PlatformSigner.load_or_create, store, namespace=self.settings.namespace
            )
            self.internal_issuer.attach(signer)
            # 샌드박스 토큰의 키 (P40 Security). 서명 키와 같은 Secret 저장소에 따로 둔다
            sandbox_key = await run_in_threadpool(
                SandboxKey.load_or_create, store, namespace=self.settings.namespace
            )
            self.sandboxes.attach_key(sandbox_key)
        else:
            logger.warning("no home cluster Secret store: the platform issues no tokens")
        # 배포가 함께 올린 Redis 의 TLS 끝점을 home 의 기록에 더한다 (P61 5절 3단계)
        await adopt_shipped_redis_tls(
            self.settings, self.cluster_service, self.data_service_service
        )
        # 그 Redis 의 평문 포트를 닫았거나 다시 열었으면 기록에 적는다 (P61 5절 5단계)
        await mark_shipped_plain(self.settings, self.cluster_service, self.data_service_service)
        # Database 의 CA 지문을 CA Secret 과 맞춘다. 소비자가 CA 변경을 알아보는 값이다 (P61). 닿지
        # 않는 클러스터의 읽기가 기동을 시한까지 붙잡지 않게 뒤에서 돈다
        self.ca_fingerprints = asyncio.create_task(
            self.data_service_service.sync_ca_fingerprints(), name="ca-fingerprints"
        )
        # 기록 없는 살아 있는 노출에 host 선점을 준다. 기동은 리컨실 리스 밖이라 쓸지는 않는다
        # (P70 2절)
        await self.gateway_service.settle_host_claims(utcnow(), sweep=False)
        # 예약한 LLM 게이트웨이 네임스페이스를 쓰던 테넌트를 알린다 (P38 3절)
        await self.tenancy.warn_reserved_clashes()
        if self.local_auth is not None:
            await self.local_auth.start()
            await self.data_plane_auth.warn_ignored_groups()
        if self.settings.reconcile_enabled:
            self.reconciler.start()
            self.sandbox_sweep.start()
            await self.agent_sleep_worker.start()
            await self.agent_preview_worker.start()
            await self.evaluation_run_worker.start()
        if self.settings.audit_collector_enabled:
            self.audit_collector.start()
        if self.spec_worker is not None:
            await self.spec_worker.start()
        if self.knowledge_indexer is not None:
            await self.knowledge_indexer.start()
        for run_worker in self.run_workers:
            await run_worker.start()
        self.usage_rollup.start()
        self.mcp_metrics_rollup.start()
        self.otlp_export.start()

    async def stop(self) -> None:
        if self.ca_fingerprints is not None:
            self.ca_fingerprints.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.ca_fingerprints
        workers = (
            ("sandbox sweep", self.sandbox_sweep),
            ("usage rollup", self.usage_rollup),
            ("mcp metrics rollup", self.mcp_metrics_rollup),
            ("otlp export", self.otlp_export),
        )
        for name, worker in workers:
            try:
                await worker.stop()
            except Exception:  # noqa: BLE001 - 리스 반납의 실패가 다른 워커의 종료를 막으면 안 된다
                logger.exception("%s did not stop cleanly", name)
        await self.audit_collector.stop()
        await self.reconciler.stop()
        # 도는 격리 점검을 멈춘다. 리스를 놓고, 남은 점검 파드는 스윕과 점검 네임스페이스의 정리가
        # 거둔다
        await self.sandbox_checks.stop()
        # 살아 있는 비서 턴을 취소한다. 태스크의 finally 가 전사를 저장하고 리스를 놓는다 (D31)
        await self.assistant.stop()
        if self.oidc is not None:
            await self.oidc.aclose()
        if self.spec_worker is not None:
            await self.spec_worker.stop()
        if self.knowledge_indexer is not None:
            await self.knowledge_indexer.stop()
        for run_worker in self.run_workers:
            await run_worker.stop()
        await self.agent_sleep_worker.stop()
        await self.agent_preview_worker.stop()
        await self.evaluation_run_worker.stop()
        await self.teams_bot.aclose()


def _lost_access_hook(
    runs: RunService,
    choices: ApprovalChoiceService,
    previews: AgentPreviews,
    copies: EvaluationCopies,
    channels: ChannelLinks,
) -> LostAccessHook:
    """사람이 접근을 잃었을 때의 훅이다. 그 사람에게 묶인 실행 소스를 멈추고(P41 6절) 그 테넌트에서
    기억한 승인 선택을 지우며(P43 5절) 그 사람의 미리 보기를 끝낸다(P44b 10절). 계정을 지웠거나
    소속을 잃었으면 평가 사례로 쓰게 한 동의를 거두고 복사본을 지운다(P45 8절). 막기와 비밀번호
    바꾸기는 동의를 거두지 않는다. 계정 사건이면 Teams 연결을 끊는다(P47 6.3절). 테넌트가
    없으면(막기, 삭제, 비밀번호 바꾸기 같은 계정 사건) 모든 테넌트의 것이다. 단계마다 따로 돌아 앞의
    것이 실패해도 뒤의 것은 하고, 끝에 첫 실패를 다시 올린다."""

    async def hook(sub: str, reason: Revocation, tenant: str | None) -> None:
        steps: list[Callable[[], Awaitable[object]]] = [
            lambda: runs.on_lost_access(sub, reason, tenant),
            lambda: choices.forget_person(sub, tenant),
            lambda: previews.end_for_person(sub, tenant),
            lambda: channels.on_lost_access(sub, reason, tenant),
        ]
        if reason in CONSENT_ENDING:
            steps.append(lambda: copies.withdraw_person(sub, tenant))
        first: BaseException | None = None
        for step in steps:
            try:
                await step()
            except Exception as e:  # noqa: BLE001 - 한 단계의 실패가 뒤 단계를 막지 않는다
                first = first or e
        if first is not None:
            raise first

    return hook


def build_services(
    stores: Stores,
    settings: Settings,
    *,
    client_factory: ClientFactory | None = None,
    secret_store: SecretStore | None = None,
    oidc_http_client: httpx.AsyncClient | None = None,
    spec_runner: SpecJobRunner | None = None,
    asgi_app: ASGIApp,
    openapi: dict[str, Any],
) -> Services:
    """서비스와 그 사이의 훅, 워커 넷. client_factory / secret_store / spec_runner 는 테스트가
    fake 를 넣는 자리다. 운영에서는 비워 둔다. spec_runner 가 없으면 실제 LLM 루프
    (SpecGeneratorRunner)를 쓰고, settings.spec_worker_enabled 가 거짓이면 Spec Generator 워커를,
    settings.knowledge_indexer_enabled 가 거짓이면 Knowledge 인덱서를, settings.runs_enabled 나
    runs_workers_enabled 가 거짓이면 실행의 발사 워커를 만들지 않는다(P41). asgi_app 과
    openapi 는 비서가 플랫폼 API 를 도구로 쓰는 재료다(D31). 표의 동작이 OpenAPI 에 없으면
    CatalogError 로 기동이 멈춘다."""
    cluster_service = ClusterService(
        stores.clusters, settings, client_factory=client_factory, secret_store=secret_store
    )
    # oidc 모드에서만 검증기를 만든다. header 모드(로컬·테스트)는 토큰을 보지 않는다
    oidc = (
        oidc_verifier_from_settings(settings, http_client=oidc_http_client)
        if settings.auth_mode == "oidc"
        else None
    )
    # 플랫폼 발급자 (D41). 모든 모드에서 내부 홉의 서비스 토큰을, local 에서는 사람 발급자로
    # gateway_token 을 낸다 (P32 1절). 키는 start() 가 붙인다
    internal_issuer = InternalIssuer(
        issuer=settings.platform_issuer_url,
        human_issuer=settings.platform_human_issuer,
        mcp_audience=settings.mcp_audience,
    )
    # local 모드는 사람 토큰도 플랫폼이 낸다 (D40)
    local = settings.auth_mode == "local"
    # 기억한 승인 선택 (P43 5절). 턴 라우트와 실행의 시작이 싣고, 배포·삭제·접근을 잃은 사람이
    # 걷는다. 실행 세션은 실행 기록으로, 카드의 선택을 받을 사람은 소유자 기록으로 가린다
    approval_choices = ApprovalChoiceService(
        stores.approval_choices,
        stores.agent_runs,
        stores.agent_session_owners,
        days=settings.approval_choice_days,
    )
    # 백그라운드 실행 (P41). 스토어만 받으므로 회수 훅과 Agent 삭제가 쓰도록 먼저 만든다. 두 훅은
    # 실행을 끈 배포본에서도 붙인다. 켰던 동안 남은 스케줄과 기록을 사람과 Agent 에 맞춰 두기
    # 때문이다. 이벤트 훅(TelemetryIngest 의 on_written)은 도는 실행을 옮기는 것이라 켰을 때만
    # 붙인다
    run_service = RunService(
        stores.agent_runs,
        stores.agent_schedules,
        settings,
        person=BoundPersonCheck(stores.users, stores.memberships, settings, stores.idp_people),
        triggers=stores.agent_triggers,
        # 웹훅 트리거의 비밀은 home 클러스터의 플랫폼 네임스페이스 Secret 에 둔다 (P41 4절)
        trigger_secrets=TriggerSecrets(
            cluster_service.secret_store, namespace=settings.namespace, ledger=stores.hook_secrets
        ),
        audit=stores.audit,
    )
    # Agent 미리 보기 (P44b, D107). 회수 훅과 Agent 서비스가 부르므로 먼저 만들고, 배포와 삭제를
    # 하는 Agent 서비스는 뒤에 붙인다(AgentPreviews.late_hooks)
    agent_previews = AgentPreviews(
        stores.agents, stores.agent_previews, stores.usage, stores.memberships, settings
    )
    # 회수 훅과 접근 정리 (P32 7절, P33 12절). 훅은 계정 사건과 멤버 교체가, 정리는 리컨실이
    # 부른다. 접근 정리는 모드가 바뀐 뒤 남은 토큰·슬롯·OAuth 연결도 치우므로 모든 모드에서 돈다.
    # 훅은 그 사람에게 묶인 실행 소스를 멈추고 기억한 승인 선택을 지우며 미리 보기를 끝내고, 계정
    # 삭제와 소속을 잃은 사람의 평가 사례 동의를 거둔다 (P41 6절, P43 5절, P44b 10절, P45 8절)
    evaluation_copies = EvaluationCopies(stores.evaluation, stores.agent_feedback)
    # Teams 채널 (P47). 연결은 회수 훅이 끊으므로 그보다 먼저 만든다. 봇의 Client Secret 은 home
    # 클러스터의 플랫폼 네임스페이스 Secret 에 있다
    teams_connector = TeamsConnector(cluster_service.secret_store, namespace=settings.namespace)
    teams_voice = TeamsVoice(teams_connector, stores.platform_settings)
    channel_links = ChannelLinks(
        stores.channels,
        stores.users,
        stores.idp_people,
        MongoLoginThrottle(
            stores.db, max_failures=CODE_FAILURES, window=CODE_WINDOW, lock=CODE_LOCK
        ),
        teams_voice,
        stores.platform_settings,
        stores.audit,
        internal_issuer,
        settings,
    )
    access_revoker = AccessRevoker(
        stores.personal_tokens,
        stores.pat_keys,
        stores.oauth_grants,
        stores.users,
        stores.memberships,
        stores.audit,
        internal_issuer,
        settings=settings,
        people=stores.idp_people,
        accounts=stores.service_accounts,
        on_lost=_lost_access_hook(
            run_service, approval_choices, agent_previews, evaluation_copies, channel_links
        ),
        on_role_lowered=agent_previews.end_if_scope_lost,
    )
    # 테넌트의 Tool 규칙과 노출 Agent 가 이름 댄 그룹. oidc 모드의 플랫폼 토큰이 사람의 IdP 그룹
    # 가운데 이것만 싣는다 (P53b 6.6절)
    named_groups = NamedGroups(stores.mcp_servers, stores.gateways, stores.agents)
    local_auth, personal_tokens = _token_services(
        stores, settings, internal_issuer, access_revoker, named_groups
    )
    service_accounts = (
        ServiceAccountService(
            stores.service_accounts,
            personal_tokens,
            access_revoker,
            settings,
            tenant_settings=stores.tenant_settings,
            memberships=stores.memberships,
        )
        if personal_tokens is not None
        else None
    )
    # 멤버와 Group Mapping 의 교체 (P32 7절, P53b 6.3절). 매핑 교체를 마저 하는 리컨실 단계가 있다
    membership_service = MembershipService(stores.memberships, access_revoker, stores.idp_people)
    tenancy = TenancyService(
        stores.clusters, cluster_service, stores.platform_settings, stores.tenant_settings
    )
    # 참조 세기 (D43). 삭제·배포 해제 가드가 모두 이것을 읽는다. 스토어만 받으므로 서비스보다 먼저
    # 만들어 생성자로 넘긴다 (P75 2절)
    references = ReferenceService(
        stores.backend_apis,
        stores.mcp_servers,
        stores.gateways,
        stores.agents,
        stores.hub_items,
        stores.knowledge,
        stores.data_services,
    )

    # Database 와 Backend API 의 쓰기 규칙 (P75 2절)
    data_service_service = DataServiceService(
        stores.data_services, tenancy, cluster_service, references
    )
    backend_api_service = BackendAPIService(
        stores.backend_apis, stores.files, references, settings, stores.db
    )
    # 내부 MCP 홉의 서비스 토큰 발급 창구 (D41). 소비자 넷이 같은 창구를 쓴다
    svc_tokens = SvcTokens(internal_issuer, stores.tenant_settings, settings)
    # P66: 문서 하나의 클러스터 작업을 묶는 클레임. 세 인스턴스 서비스가 나눠 쓰고, 모든 클러스터
    # 요청 앞의 훅이 지금 실행의 클레임을 늘린다. 훅은 서비스 층이 걸므로 K8s 층은 스토어를 모른다
    claims = Claims(stores.db, settings.pod_id)
    set_request_hook(request_hook)
    # 허브·게이트웨이 (D22). D49: internal·published 의 도구 이름은 저장하지 않고 원본에서 계산한다.
    # 해석기는 스토어만 받으므로 먼저 만들어 넘긴다
    hub_service = HubService(
        stores.hub_items,
        tenancy,
        cluster_service,
        references,
        tool_names=HubToolNames(stores.mcp_servers, stores.gateways, stores.backend_apis),
        svc_tokens=svc_tokens,
        header_ledger=stores.outside_header_secrets,
        env_ledger=stores.hub_env_secrets,
    )
    # MCP Server 게시·해제·재배포 동기화·삭제가 허브를 쓴다 (D23). host_conflict 는
    # MCP·Gateway·Agent 세 종류를 함께 본다 (P8·D27, 모든 클러스터, P33 6절)
    mcp_server_service = MCPServerService(
        stores.mcp_servers,
        stores.backend_apis,
        stores.data_services,
        tenancy,
        cluster_service,
        references,
        hub=hub_service,
        gateway_instances=stores.gateways,
        agents=stores.agents,
        svc_tokens=svc_tokens,
        claims=claims,
        exposure_hosts=stores.exposure_hosts,
        # vMCP 앞단의 Entry 이름 선점 (P54 5절). Gateway 의 Entry 이름과 같은 기록이다
        entry_names=stores.entry_names,
    )
    # 허브의 internal 항목 상태는 원본 MCP Server 의 판단이다 (T55, HubService.late_hooks)
    hub_service.server_status = mcp_server_service.item_status
    # Tool 규칙을 컴파일할 테넌트의 사실 (P52 2절). 세 렌더와 Entry 토큰이 같은 것을 읽는다
    rule_scopes = RuleScopes(
        settings,
        stores.tenant_settings,
        stores.memberships,
        stores.agents,
        servers=stores.mcp_servers,
        gateways=stores.gateways,
    )
    mcp_server_service.rule_scopes = rule_scopes
    hub_service.rule_scopes = rule_scopes
    # 공유 복제본 (P50 1절). Gateway 배포가 복제본을 적용하고 리컨실의 스윕이 쓰지 않는 것을 지운다
    shared_clones = SharedClones(
        stores.shared_clones,
        hub_service,
        stores.gateways,
        cluster_service,
        tenancy,
        stores.auth_reapply,
    )
    # P39b 14절 4부: 검증·컴파일 문맥의 Tool 효과 재료를 미리 읽는다. Gateway 배포의 핀 경고도
    # 같은 것으로 게시 항목 아래까지 이름을 푼다
    tool_effects = ToolEffectLoader(
        stores.backend_apis, stores.mcp_servers, stores.gateways, stores.hub_items
    )
    gateway_service = GatewayService(
        stores.gateways,
        hub_service,
        tenancy,
        cluster_service,
        references,
        mcp_instances=stores.mcp_servers,
        agents=stores.agents,
        # 저장된 overrides 를 옮긴 감사 행 (P50 8절)
        audit=stores.audit,
        shared_clones=shared_clones,
        svc_tokens=svc_tokens,
        claims=claims,
        exposure_hosts=stores.exposure_hosts,
        entry_names=stores.entry_names,
        tool_effects=tool_effects,
    )
    gateway_service.rule_scopes = rule_scopes
    # Agent 답변의 피드백 (P36 6절). 바뀔 때마다 agent_events 에 사람 없는 이벤트를 남기고, 평가가
    # 바뀌면 그 평가에서 복사한 평가 사례가 따라 움직인다 (P45 8절)
    agent_feedback_service = AgentFeedbackService(
        stores.agent_feedback,
        stores.agent_events,
        retention_days=settings.feedback_retention_days,
        copies=evaluation_copies,
    )
    # Agent 의 평가 세트와 사례 (P45 7절). Agent 를 지우면 그 세트와 사례도 지운다
    evaluation_sets = EvaluationSets(stores.evaluation, evaluation_copies)
    # 배포, 재배포, 되돌리기가 새 내용에 평가 실행을 묻는다 (P45 12절)
    deploy_gate = DeployGate(stores.evaluation_runs, evaluation_sets, stores.agents, settings)
    # 코드 실행 샌드박스 (P40). 기계 경로가 열고 찾고 닫으며, Agent 의 해제와 삭제가 그 Agent 의
    # 샌드박스를 닫는다. 스토어만 받으므로 AgentService 보다 먼저 만든다
    sandboxes = SandboxService(
        stores.sandboxes,
        agents=stores.agents,
        tokens=stores.agent_instance_tokens,
        clusters=stores.clusters,
        cluster_service=cluster_service,
        owners=stores.agent_session_owners,
        runs=stores.agent_runs,
        events=stores.agent_events,
        platform_settings=stores.platform_settings,
        settings=settings,
    )
    sandbox_checks = SandboxChecks(sandboxes, stores.clusters, stores.db, settings)
    # Agent (D26·D42). 정의와 배포가 한 서비스다. 그래프 검증 문맥은 provider·Tenant Settings·
    # 도구 소스·Database·Knowledge Base·핀한 Skill 을 본다. 삭제가 그 Agent 의 피드백도 지운다
    agent_service = AgentService(
        stores.agents,
        stores.agent_instance_tokens,
        stores.platform_settings,
        stores.tenant_settings,
        stores.gateways,
        stores.mcp_servers,
        stores.data_services,
        stores.knowledge,
        tenancy,
        cluster_service,
        settings,
        svc_tokens=svc_tokens,
        feedback=agent_feedback_service,
        skills=stores.skills,
        claims=claims,
        exposure_hosts=stores.exposure_hosts,
        tool_effects=tool_effects,
        # 게시한 Agent 는 허브의 `agent` 항목이다 (P46 6.1절)
        hub=hub_service,
        run_hooks=run_service,
        choices=approval_choices,
        sandboxes=sandboxes,
        # 잠들기 앞에 열린 실행을 센다 (P44 7절). 실행을 끄면 세지 않는다
        runs=stores.agent_runs if settings.runs_enabled else None,
        # 원본을 지우면 그 미리 보기를 끝낸다 (P44b 10절)
        previews=agent_previews,
        evaluations=evaluation_sets,
        gate=deploy_gate,
    )
    agent_service.rule_scopes = rule_scopes
    # 허브의 agent 항목 상태는 그 Agent 의 판단이다 (P46 6.1절, HubService.late_hooks)
    hub_service.agent_status = agent_service.item_status
    # 미리 보기의 배포와 끝은 Agent 서비스가 한다 (AgentPreviews.late_hooks)
    agent_previews.service = agent_service
    # 중지와 멈춤이 도는 실행의 턴을 런타임에서 멈춘다 (P41 7절, RunService.late_hooks). 프록시는
    # AgentService 가 만든다
    run_service.turns = RunTurns(stores.agents, agent_service.proxy)

    async def _any_instances(cluster_id: str) -> bool:
        if await mcp_server_service.has_instances_on_cluster(cluster_id):
            return True
        if await gateway_service.has_instances_on_cluster(cluster_id):
            return True
        return await agent_service.has_instances_on_cluster(cluster_id)

    # 클러스터 삭제 검사(남은 배포)는 플랫폼 전체를 본다. 스토어 `list()` 에 tenant 가 없고 다른
    # 테넌트의 배포가 남은 클러스터도 막아야 하기 때문이다 (D43). 세 인스턴스 서비스가 있어야
    # 답하므로 만든 뒤에 붙인다(ClusterService.late_hooks). 나머지 참조 가드는 생성자로 받은
    # ReferenceService 가 센다
    cluster_service.instance_checker = _any_instances

    # P53b 6.2·6.9절: 데이터 플레인 발급자 전환의 준비 확인과 상태
    issuer_switch = IssuerSwitch(
        tenancy,
        svc_tokens,
        settings,
        MongoIssuerSwitchStore(stores.auth_reapply.coll),
        agents=stores.agents,
        people=stores.idp_people,
    )
    # P54b 7절: 프록시를 두던 허브 항목(remote 항목, proxy 모드로 게시한 항목)을 Gateway 의 Entry 가
    # 주소를 직접 부르는 꼴로 옮긴다. 리컨실의 한 단계이고 상태 API 가 남은 것을 읽는다
    entry_conversion = EntryConversion(
        hub_service, gateway_service, stores.shared_clones, stores.auth_reapply
    )
    data_plane_auth = DataPlaneAuth(
        tenancy,
        stores.auth_reapply,
        gateways=gateway_service,
        mcp_servers=mcp_server_service,
        agents=agent_service,
        # 자기 CR 을 둔 허브 항목도 렌더 번호로 다시 렌더한다 (P50 1절)
        hub_items=HubItemReapply(hub_service),
        shared_clones=SharedCloneReapply(shared_clones),
        issuer_switch=issuer_switch,
        entry_conversion=entry_conversion,
    )
    # preflight 가 테넌트 Secret 의 바인딩을 볼 네임스페이스는 스윕이 맞추는 곳이다 (P63)
    cluster_service.tenant_namespaces = data_plane_auth.tenant_namespaces
    pat_keys = PatKeyRing(
        internal_issuer,
        stores.pat_keys,
        mints=settings.platform_mints_human_tokens,
        pool_size=settings.pat_key_pool,
    )
    jwks_heal = JwksHeal(tenancy, svc_tokens, settings)
    # P52 4절: 인자 조건이 있는 서버의 파드가 조건을 지키는지 initialize 로 읽어 적는다
    condition_records = ConditionRecords(
        stores.mcp_servers, clients=mcp_server_service.client_for_doc, tokens=svc_tokens
    )
    platform_tools = PlatformTools(
        stores.hub_items,
        stores.platform_settings,
        shared_clones,
        issuer_url=settings.platform_issuer_url,
    )
    # Agent 의 LLM 게이트웨이 토큰 (P38 5절). 토큰 Secret 을 쓰는 자리가 발급을 부르고, 회수는
    # 게이트웨이 설정을 곧바로 맞춘다
    llm_tokens = LlmTokens(
        stores.agents,
        stores.llm_revocations,
        svc_tokens,
        client_for_doc=agent_service.client_for_doc,
    )
    agent_service.agent_tokens.llm = llm_tokens
    llm_tokens.claimed = agent_service.claimed_llm_cleanup
    # Agent 의 경로 결정이 읽는 게이트웨이 상태 (P38 6절)
    agent_service.llm.state = stores.llm_gateway
    llm_gateway = LlmGateway(
        settings,
        stores.platform_settings,
        stores.llm_gateway,
        cluster_service,
        internal_issuer,
        revocations=stores.llm_revocations,
        tokens=llm_tokens,
        telemetry=stores.telemetry_revocations,
    )
    llm_tokens.on_revoked = llm_gateway.sync_config
    # 평가 정리 (P45 8절). 리컨실 틱의 단계로 한 시간에 한 번 돈다
    evaluation_sweep = EvaluationSweep(
        stores.evaluation,
        stores.agent_feedback,
        stores.agents,
        users=stores.users,
        people=stores.idp_people,
        auth_mode=settings.auth_mode,
    )
    reconciler = Reconciler(
        stores.mcp_servers,
        mcp_server_service,
        Lease(stores.db, settings.pod_id),
        interval=settings.reconcile_interval,
        clone_sweep=shared_clones.sweep,
        # D42: Gateway 도 같은 세 갈래로 몬다
        gateways=(stores.gateways, gateway_service),
        # D42: Agent 도 같은 세 갈래로 몬다
        agents=(stores.agents, agent_service),
        # D41: 소비자의 서비스 토큰을 만료 6개월 전에 갈아 끼운다
        svc_token_sweeper=SvcTokenSweeper(
            svc_tokens,
            gateways=(stores.gateways, gateway_service),
            mcp_instances=(stores.mcp_servers, mcp_server_service),
            agents=(stores.agents, agent_service),
        ),
        # P32 8절: 인증 설정이 바뀌면 MCPOIDCConfig 와 소비자를 다시 적용한다
        auth_reapply=data_plane_auth,
        # P50 2절: 한 시간에 한 번 preflight 를 다시 돌리고 ToolHive CR 의 Strict 적용을 판정한다
        cluster_service=cluster_service,
        # P52 4절: 인자 조건의 기록. 인증 재적용보다 먼저 돌아 바뀐 서버를 같은 틱에 다시 렌더한다
        condition_sweep=condition_records.sweep,
        # P54b 7절: 인증 재적용보다 먼저 돌아 스냅샷을 고쳐 쓴 Gateway 를 같은 틱에 다시 렌더한다
        entry_conversion=entry_conversion.run,
        # P32 7절·4절: 접근 정리가 옛 슬롯을 지운 뒤 개인 토큰의 키 pool 을 채운다. T24: 기동 뒤
        # 15분 동안 플랫폼보다 먼저 떠 JWKS 를 받지 못한 검증기를 다시 띄운다
        steps=(
            ("access", access_revoker.sweep),
            # P45 8절: 동의가 끝난 복사본, 없는 Agent 의 세트와 사례를 지운다. 한 시간에 한 번이다
            ("evaluations", evaluation_sweep.run),
            ("pat_pool", pat_keys.fill_pool),
            ("jwks_heal", jwks_heal.sweep),
            # P86 6.1절: 항목 없이 남은 외부 서버 헤더 값의 관리 Secret 을 장부로 찾아 지운다. 앞
            # 판이 항목을 지우면 그 Secret 이 남는다
            ("outside_headers", hub_service.outside_headers.sweep),
            # P55 5절: 항목 없이 남은 image 항목의 Secret env Secret 을 장부로 찾아 지우고, 답하지
            # 않는 stdio 항목의 프록시 파드를 백오프를 두고 다시 만든다
            ("hub_env_secrets", hub_service.secret_envs.sweep),
            ("hub_stdio", hub_service.probe_stdio),
            # P86 6.2절: 재적용이 렌더하지 않는 Gateway(실패, 배포 중, 내리는 중)에서 consume
            # 경계 밖이 되거나 항목이 없어진 헤더 사본을 지운다
            ("outside_header_copies", gateway_service.drop_stray_header_copies),
            # P53b 6.2절: 발급자를 플랫폼으로 바꾼 뒤 vMCP·노출 앞단·노출 Agent 가 플랫폼 토큰을
            # 받는지 불러 본다
            ("issuer_switch", issuer_switch.probe),
            # P53b 6.3절: 긴 한도와 30일이 지난 사람 기록의 그룹과 이메일을 비운다
            (
                "idp_people",
                functools.partial(
                    stores.idp_people.clear_expired, long_bound=settings.idp_confirm_bound
                ),
            ),
            # P53b 6.3절: 매핑 교체의 요청이 끝내지 못한 다시 풀기를 마저 한다
            ("group_mappings", membership_service.finish_mappings),
            # P70 2절: 노출에서 내려온 host 의 선점을 쓸고 기록 없는 노출에 기록을 준다. 리스 안이라
            # 같은 틱의 몰아가기 뒤에 돈다
            ("exposure_hosts", gateway_service.settle_host_claims),
            # D82: 유예가 지난 Entry 이름의 선점 가운데 그 Gateway 가 더는 쓰지 않는 것을 쓴다
            ("entry_names", gateway_service.entries.sweep),
            # P39b 13절: 플랫폼 웹 Tool 항목을 설정에 맞추고, 꺼진 fetch 의 복제본을 지우고 다시 켠
            # fetch 의 복제본을 되살린다
            ("platform_tools", platform_tools.sweep),
            # P38 4절: LLM 게이트웨이를 렌더하고 파드마다 반영된 리비전을 읽는다. 끄면 거둔다
            ("llm_gateway", llm_gateway.sync),
            # P41 4절: 기록이 없는 트리거의 키, 받는 동안이 지난 앞 비밀, 사라진 Agent 의 Secret 을
            # 뺀다. 실행을 끈 배포본에서도 켰던 동안 남은 것을 치운다
            (
                "trigger_secrets",
                TriggerSecretSweep(
                    stores.agents, stores.agent_triggers, run_service.trigger_secrets
                ).sweep,
            ),
            # P47 6.3절: 끝이 지났거나 90일 동안 Teams 턴이 없는 연결을 끊고, 한 시간에 한 번 계정을
            # 잃은 사람과 다른 봇의 연결을 끊는다. 채널을 껐거나 header 모드면 돌지 않는다
            *(
                (("channel_links", channel_links.sweep),)
                if settings.channels_enabled and settings.token_auth
                else ()
            ),
        ),
    )
    # 가격표와 원장에 쓰는 한 곳 (P37 6절·7절). 플랫폼의 세 호출 자리와 런타임 이벤트가 같은 것을
    # 쓴다
    price_book = PriceBook(stores.pricing)
    usage_ledger = UsageLedger(stores.usage, price_book, gateway_url=settings.llm_gateway_url)
    # 플랫폼 호출자(비서, 스펙 생성기, Knowledge 인덱서)가 호출마다 고르는 길 (P38 6절)
    llm_endpoints = LlmEndpoints(
        settings, cluster_service.secret_store, store=stores.llm_gateway, svc_tokens=svc_tokens
    )
    # Agent 런타임 이벤트를 넣는 한 곳 (P37 4절). 수집기와 수집 경로가 같이 쓰고, 외부 경로의 세션
    # 지우기와 다시 생성을 평가에 옮긴다(P36 6절)
    otlp_export = OtlpExport(
        stores.otlp,
        stores.agent_events,
        stores.usage,
        stores.agents,
        stores.observability_settings,
        Lease(stores.db, settings.pod_id),
        secrets=cluster_service.secret_store,
        namespace=settings.namespace,
    )
    # 런타임 이벤트와 게이트웨이 사용량의 수집은 레플리카마다 자리 하나를 나눠 쓴다 (P37 3절·8절)
    ingest_slots = asyncio.Semaphore(INGEST_CONCURRENCY)
    telemetry_writer = TelemetryWriter(
        stores.agent_events,
        usage_ledger,
        on_events=agent_feedback_service.apply_runtime_events,
        otlp=otlp_export.offer,
    )
    audit_collector = AuditCollector(
        stores.audit,
        stores.mcp_servers,
        stores.clusters,
        cluster_service,
        Lease(stores.db, settings.pod_id),
        # D26: Agent 파드의 stdout 관측 이벤트도 같은 폴링이 agent_events 로 넣는다
        agents=stores.agents,
        gateways=stores.gateways,
        hub=stores.hub_items,
        interval=settings.audit_collector_interval,
        telemetry=telemetry_writer,
        pushes=stores.agent_telemetry,
    )
    spec_worker: SpecGeneratorWorker | None = None
    if settings.spec_worker_enabled:
        runner = spec_runner or SpecGeneratorRunner(
            stores.platform_settings,
            cluster_service.secret_store,
            settings,
            usage_ledger=usage_ledger,
            endpoints=llm_endpoints,
        )
        spec_worker = SpecGeneratorWorker(
            stores.spec_jobs,
            stores.spec_job_files,
            runner,
            interval=settings.spec_worker_interval,
            lease_seconds=settings.spec_job_lease_seconds,
            ttl_seconds=settings.spec_job_ttl_seconds,
        )
    # Knowledge Base (D32). 삭제 검사 훅 둘은 최신 리비전에 knowledge 노드가 있는 Agent 와
    # 그것을 핀한 살아 있는 배포의 참조 검사다
    knowledge_service = KnowledgeService(
        stores.knowledge,
        stores.knowledge_files,
        stores.data_services,
        stores.platform_settings,
        tenancy,
        cluster_service,
        settings,
        references,
    )
    # 문서 파싱 서비스(D61)의 클라이언트. Knowledge 인덱서와 채팅 첨부 업로드가 하나를 같이 쓴다
    docparse = DocparseClient.from_settings(settings)
    knowledge_indexer: KnowledgeIndexer | None = None
    if settings.knowledge_indexer_enabled:
        knowledge_indexer = KnowledgeIndexer(
            stores.knowledge,
            stores.knowledge_files,
            tenancy,
            cluster_service,
            stores.platform_settings,
            stores.data_services,
            settings,
            docparse=docparse,
            interval=settings.knowledge_indexer_interval,
            lease_seconds=settings.knowledge_lease_seconds,
            usage_ledger=usage_ledger,
            endpoints=llm_endpoints,
        )
    # 비서 (D31). 도구 표와 OpenAPI 의 어긋남은 여기서 CatalogError 로 기동을 막는다
    assistant = AssistantService(
        stores.assistant_sessions,
        stores.platform_settings,
        settings,
        asgi_app=asgi_app,
        tool_defs=build_tool_schemas(openapi, CATALOG),
        db=stores.db,
        secret_store=cluster_service.secret_store,
        usage_ledger=usage_ledger,
        endpoints=llm_endpoints,
    )
    oauth_clients, oauth_authorizer, oauth_consent, oauth_tokens = _oauth_services(
        stores, settings, internal_issuer, gateway_service, access_revoker, named_groups
    )
    # 턴 위임 (P53). 채팅 라우트와 실행의 시작 워커가 함께 쓴다
    turn_grants = TurnGrantService(
        stores.turn_grants,
        agents=stores.agents,
        instance_tokens=stores.agent_instance_tokens,
        gateways=stores.gateways,
        mcp_servers=stores.mcp_servers,
        clusters=stores.clusters,
        revoker=access_revoker,
        # 인증 재적용의 틱이 클러스터마다 적은 게이트 판정을 읽는다 (P53 3.5절)
        gates=data_plane_auth.turn_gates,
        issuer=internal_issuer,
        settings=settings,
    )
    run_delegation = TurnDelegation(turn_grants, stores.agent_session_owners)

    def run_starter(owner: str | None = None) -> RunStartWorker:
        return RunStartWorker(
            stores.agents,
            run_service,
            agent_service.proxy,
            run_delegation,
            choices=approval_choices,
            owner=owner,
            interval=settings.runs_interval,
            lease_seconds=settings.runs_lease_seconds,
            sandbox_runs=settings.sandbox_runs_per_agent,
            sleep=agent_service.sleep,
        )

    # 다른 Agent 를 Tool 로 부르는 호출 (P46 6.3절). 경로가 시작 워커의 한 걸음을 그 자리에서
    # 돌리므로 틱을 돌지 않는 워커 하나를 따로 쥔다. 위임의 끝이 그 턴의 호출을 끝낸다
    # (TurnGrantService.late_hooks)
    agent_calls = AgentCallRuns(
        agents=stores.agents,
        hub=stores.hub_items,
        grants=stores.turn_grants,
        instance_tokens=stores.agent_instance_tokens,
        run_service=run_service,
        publishing=agent_service.publishing,
        proxy=agent_service.proxy,
        starter=run_starter(f"agent-calls:{settings.pod_id}"),
        issuer=internal_issuer,
        settings=settings,
        audit=stores.audit,
    )
    turn_grants.on_end = agent_calls.caller_ended
    run_workers: tuple[LeaseWorker[Any], ...] = ()
    if settings.runs_enabled and not settings.runs_workers_enabled:
        logger.warning("background runs are enabled but their workers are off: nothing fires")
    if settings.runs_enabled and settings.runs_workers_enabled:
        run_workers = (
            ScheduleFireWorker(
                stores.agents,
                run_service,
                interval=settings.runs_interval,
                lease_seconds=settings.runs_lease_seconds,
            ),
            run_starter(),
            RunWatchWorker(
                stores.agents,
                stores.agent_runs,
                agent_service.proxy,
                interval=settings.runs_interval,
                lease_seconds=settings.runs_lease_seconds,
                caller_sweep=agent_calls.sweep,
            ),
        )
    # 평가 실행은 평가 미리 보기에 턴을 보낸다. 프록시와 쉬기의 게이트는 Agent 서비스의 것이다
    # (P45 9절)
    evaluation_runner = EvaluationRunner(
        stores.evaluation_runs,
        stores.evaluation,
        stores.agents,
        agent_previews,
        agent_service,
        TurnDelegation(turn_grants, stores.agent_session_owners),
        run_service.person,
        stores.memberships,
        stores.usage,
        settings,
        EvaluationGrader(stores.platform_settings, llm_endpoints, usage_ledger, settings),
    )
    services = Services(
        settings=settings,
        oidc=oidc,
        cluster_service=cluster_service,
        tenancy=tenancy,
        mcp_server_service=mcp_server_service,
        hub_service=hub_service,
        gateway_service=gateway_service,
        agent_service=agent_service,
        agent_feedback_service=agent_feedback_service,
        evaluation_sets=evaluation_sets,
        evaluation_copies=evaluation_copies,
        deploy_gate=deploy_gate,
        approval_choices=approval_choices,
        citation_sources=CitationSources(stores.knowledge, stores.knowledge_files),
        chat_uploads=ChatUploadService(
            stores.chat_uploads, docparse, stores.db, pod_id=settings.pod_id
        ),
        reconciler=reconciler,
        audit_collector=audit_collector,
        spec_worker=spec_worker,
        assistant=assistant,
        knowledge_service=knowledge_service,
        references=references,
        data_service_service=data_service_service,
        backend_api_service=backend_api_service,
        skill_service=SkillService(stores.skills, stores.platform_settings, references),
        knowledge_indexer=knowledge_indexer,
        run_service=run_service,
        run_triggers=RunTriggers(run_service),
        webhooks=WebhookReceiver(run_service, stores.agents, stores.audit),
        run_workers=run_workers,
        internal_issuer=internal_issuer,
        data_plane_auth=data_plane_auth,
        shared_clones=shared_clones,
        jwks_heal=jwks_heal,
        pat_keys=pat_keys,
        membership_service=membership_service,
        price_book=price_book,
        usage_ledger=usage_ledger,
        usage_rollup=UsageRollup(
            stores.usage,
            price_book,
            Lease(stores.db, settings.pod_id),
            interval=settings.usage_rollup_seconds,
        ),
        usage_reports=UsageReports(stores.usage, stores.agents, stores.users if local else None),
        mcp_metrics_rollup=McpMetricsRollup(
            stores.mcp_metrics,
            Lease(stores.db, settings.pod_id),
            interval=settings.mcp_metrics_rollup_seconds,
        ),
        mcp_metrics_reports=McpMetricsReports(
            stores.mcp_metrics,
            stores.clusters,
            stores.backend_apis,
            collector_enabled=settings.audit_collector_enabled,
        ),
        telemetry_writer=telemetry_writer,
        telemetry_ingest=TelemetryIngest(
            stores.agent_instance_tokens,
            stores.agents,
            telemetry_writer,
            stores.agent_telemetry,
            slots=ingest_slots,
            # 실행의 상태를 턴 이벤트로 옮긴다 (P41 8절). 실행을 켰을 때만이다
            on_written=(
                RunEvents(stores.agent_runs, stores.agents).apply if settings.runs_enabled else None
            ),
        ),
        gateway_usage=GatewayUsageIngest(
            internal_issuer,
            stores.clusters,
            stores.telemetry_revocations,
            usage_ledger,
            slots=ingest_slots,
            provider_names=gateway_provider_names(stores.platform_settings),
        ),
        agent_metrics=AgentMetrics(stores.agent_events, stores.usage, stores.agent_telemetry),
        agent_traces=AgentTraces(stores.agent_events, stores.usage),
        otlp_export=otlp_export,
        platform_tools=platform_tools,
        llm_gateway=llm_gateway,
        sandboxes=sandboxes,
        sandbox_sweep=SandboxSweep(
            sandboxes,
            stores.clusters,
            stores.agents,
            Lease(stores.db, settings.pod_id),
            interval=settings.sandbox_sweep_seconds,
            extra=(
                sandbox_checks.shared_nodes_step,
                sandbox_checks.probes_step,
                sandbox_checks.helpers_step,
            ),
        ),
        sandbox_checks=sandbox_checks,
        agent_sleep_worker=AgentSleepWorker(
            agent_service.sleep,
            Lease(stores.db, settings.pod_id),
            interval=settings.agent_sleep_interval,
        ),
        agent_previews=agent_previews,
        agent_preview_worker=AgentPreviewWorker(
            agent_previews,
            Lease(stores.db, settings.pod_id),
            interval=settings.agent_preview_interval,
        ),
        evaluation_runner=evaluation_runner,
        evaluation_runs=EvaluationRuns(
            stores.evaluation_runs, evaluation_sets, agent_previews, evaluation_runner, settings
        ),
        evaluation_run_worker=EvaluationRunWorker(
            evaluation_runner,
            Lease(stores.db, settings.pod_id),
            interval=settings.evaluation_interval,
        ),
        turn_grants=turn_grants,
        agent_calls=agent_calls,
        web_search=WebSearch(
            settings_store=stores.platform_settings,
            hub_store=stores.hub_items,
            counts=stores.web_search_counts,
            namespace=settings.namespace,
            secret_store=cluster_service.secret_store,
            gateways=stores.gateways,
            ledger=usage_ledger,
        ),
        teams_connector=teams_connector,
        channel_links=channel_links,
        teams_bot=TeamsBot(
            stores.channels,
            channel_links,
            teams_voice,
            teams_connector,
            stores.platform_settings,
            stores.audit,
        ),
        local_auth=local_auth,
        personal_tokens=personal_tokens,
        service_accounts=service_accounts,
        oauth_clients=oauth_clients,
        oauth_authorizer=oauth_authorizer,
        oauth_consent=oauth_consent,
        oauth_tokens=oauth_tokens,
    )
    _check_wiring(services)
    return services


def _token_services(
    stores: Stores,
    settings: Settings,
    issuer: InternalIssuer,
    revoker: AccessRevoker,
    named: NamedGroups,
) -> tuple[LocalAuthService | None, PersonalTokenService | None]:
    """auth_mode=local 에서만 있는 사람 인증(D40)과, 플랫폼이 사람 토큰을 내는 모드(local, oidc)의
    개인 MCP 토큰(P32, P53b 6.5절). 계정 사건이 회수 훅을 부른다."""
    local_auth = None
    if settings.auth_mode == "local":
        local_auth = LocalAuthService(
            settings,
            stores.users,
            stores.memberships,
            stores.login_throttle,
            issuer,
            stores.tenant_settings,
            revoker,
        )
    if not settings.platform_mints_human_tokens:
        return local_auth, None
    personal_tokens = PersonalTokenService(
        stores.personal_tokens,
        stores.pat_keys,
        stores.memberships,
        issuer,
        revoker,
        settings,
        people=stores.idp_people,
        named=named,
        local_auth=local_auth,
    )
    return local_auth, personal_tokens


def _oauth_services(
    stores: Stores,
    settings: Settings,
    issuer: InternalIssuer,
    gateway_service: GatewayService,
    revoker: AccessRevoker,
    named: NamedGroups,
) -> tuple[OAuthClients | None, OAuthAuthorizer | None, OAuthConsent | None, OAuthTokens | None]:
    """MCP 클라이언트의 OAuth 인가 서버 (P33). 꺼지지 않았을 때(`settings.oauth_off_reason`) 있고,
    잠들었으면(P53b 6.5절) 아무것도 내지 않는다. resource 는 host 유일성과 같은 스토어
    목록(`platform_exposures`)으로 푼다. 갱신은 접근 정리와 같은 규칙(`AccessRevoker.grant_reason`)
    으로 연결을 다시 본다."""
    if settings.oauth_off_reason is not None:
        return None, None, None, None
    clients = OAuthClients(issuer, settings.oauth_cimd_clients)
    authorizer = OAuthAuthorizer(
        clients, gateway_service.platform_exposures, dormant=settings.oauth_dormant
    )
    consent = OAuthConsent(
        authorizer, stores.oauth_consent_uses, stores.oauth_codes, stores.memberships
    )
    tokens = OAuthTokens(
        authorizer, stores.oauth_codes, stores.oauth_grants, revoker, settings, named
    )
    return clients, authorizer, consent, tokens


def _check_wiring(services: Services) -> None:
    """만든 뒤에 붙이는 훅이 전부 채워졌는지 기동 때 본다 (P72 2.4절, D84).

    만들기 전에 있는 의존은 생성자 인자라 빠지면 기동이 TypeError 로 멈춘다. 만든 뒤에 붙여야 하는
    것은 진짜 생성 순환뿐이고(클러스터 삭제 가드가 세 인스턴스 서비스를 보고, 허브가 internal 항목의
    상태를 MCP Server 서비스에 묻는다), 그 클래스가 `late_hooks` 로 적는다. 여기는 손으로 적은
    목록 없이 `Services` 의 모든 값의 선언을 읽는다.
    `build_services` 가 선언하지 않은 속성을 사후 대입하면 테스트(test_wiring.py)가 막는다."""
    missing = [
        f"{name}.{hook}"
        for name, value in vars(services).items()
        for hook in sorted(getattr(value, "late_hooks", ()))
        if getattr(value, hook, None) is None
    ]
    # 비서는 도구 표가 비어 있거나 실행기가 앱을 못 받으면 조용히 "도구 없는 채팅" 이 된다
    assistant = services.assistant
    if not assistant.tool_defs:
        missing.append("assistant.tool_defs")
    if assistant.executor is None or assistant.executor.app is None:
        missing.append("assistant.executor.app")
    if missing:
        raise RuntimeError(f"배선이 빠졌다: {', '.join(missing)}")


def attach(app: FastAPI, stores: Stores, services: Services) -> None:
    """라우터가 request.app.state 에서 읽는 이름으로 붙인다."""
    state = app.state
    state.settings = services.settings
    state.oidc = services.oidc
    state.local_auth = services.local_auth
    state.personal_tokens = services.personal_tokens
    state.service_accounts = services.service_accounts
    state.oauth_clients = services.oauth_clients
    state.oauth_authorizer = services.oauth_authorizer
    state.oauth_consent = services.oauth_consent
    state.oauth_tokens = services.oauth_tokens
    state.membership_service = services.membership_service
    state.internal_issuer = services.internal_issuer
    state.pat_keys = services.pat_keys
    state.audit = stores.audit
    state.backend_apis = stores.backend_apis
    state.backend_api_service = services.backend_api_service
    state.memberships = stores.memberships
    state.idp_people = stores.idp_people
    state.files = stores.files
    state.clusters = stores.clusters
    state.platform_settings = stores.platform_settings
    state.tenant_settings = stores.tenant_settings
    state.cluster_service = services.cluster_service
    state.tenancy = services.tenancy
    state.data_services = stores.data_services
    state.data_service_service = services.data_service_service
    state.mcp_servers = stores.mcp_servers
    state.mcp_server_service = services.mcp_server_service
    state.hub_items = stores.hub_items
    state.hub_service = services.hub_service
    state.gateways = stores.gateways
    state.gateway_service = services.gateway_service
    state.agents = stores.agents
    state.agent_instance_tokens = stores.agent_instance_tokens
    state.agent_session_owners = stores.agent_session_owners
    state.agent_service = services.agent_service
    state.agent_feedback_service = services.agent_feedback_service
    state.evaluation_sets = services.evaluation_sets
    state.deploy_gate = services.deploy_gate
    state.evaluation_copies = services.evaluation_copies
    state.approval_choices = services.approval_choices
    state.spec_jobs = stores.spec_jobs
    state.spec_job_files = stores.spec_job_files
    state.reconciler = services.reconciler
    state.data_plane_auth = services.data_plane_auth
    state.shared_clones = services.shared_clones
    state.jwks_heal = services.jwks_heal
    state.audit_collector = services.audit_collector
    state.spec_worker = services.spec_worker
    state.assistant_sessions = stores.assistant_sessions
    state.assistant = services.assistant
    # Knowledge Base (D32)
    state.knowledge = stores.knowledge
    state.knowledge_files = stores.knowledge_files
    state.knowledge_service = services.knowledge_service
    # 답이 인용한 Knowledge 문서의 원문 (P36 7절)
    state.citation_sources = services.citation_sources
    # 채팅 첨부의 업로드 (P36 9절). 업로드 라우트와 채팅 턴이 쓴다
    state.chat_uploads = services.chat_uploads
    state.knowledge_indexer = services.knowledge_indexer
    # 참조 세기 (D43). 라우터 아홉의 GET /{id}/references 가 읽는다. 가드는 서비스가 생성자로 받았다
    state.references = services.references
    state.skill_service = services.skill_service
    # 가격표와 원장 (P37)
    state.price_book = services.price_book
    state.usage_ledger = services.usage_ledger
    state.usage_rollup = services.usage_rollup
    state.usage_reports = services.usage_reports
    state.mcp_metrics_reports = services.mcp_metrics_reports
    state.agent_events = stores.agent_events
    state.agent_telemetry = stores.agent_telemetry
    state.telemetry_writer = services.telemetry_writer
    state.telemetry_ingest = services.telemetry_ingest
    state.gateway_usage = services.gateway_usage
    state.agent_metrics = services.agent_metrics
    state.agent_traces = services.agent_traces
    state.otlp_export = services.otlp_export
    state.observability_settings = stores.observability_settings
    state.web_search = services.web_search
    state.turn_grants = services.turn_grants
    state.agent_calls = services.agent_calls
    state.run_service = services.run_service
    state.run_triggers = services.run_triggers
    state.webhooks = services.webhooks
    state.run_workers = services.run_workers
    state.platform_tools = services.platform_tools
    state.llm_gateway = services.llm_gateway
    # 코드 실행 샌드박스의 기계 경로 (P40 5절)
    state.sandboxes = services.sandboxes
    state.sandbox_sweep = services.sandbox_sweep
    state.agent_sleep_worker = services.agent_sleep_worker
    state.agent_previews = services.agent_previews
    state.agent_preview_worker = services.agent_preview_worker
    state.evaluation_runner = services.evaluation_runner
    state.evaluation_run_worker = services.evaluation_run_worker
    state.evaluation_runs = services.evaluation_runs
    state.sandbox_checks = services.sandbox_checks
    # Teams 채널 (P47)
    state.teams_connector = services.teams_connector
    state.channel_links = services.channel_links
    state.teams_bot = services.teams_bot
