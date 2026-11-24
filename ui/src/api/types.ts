/**
 * 응답 타입. 라우터가 타입화된 뒤로는 openapi-typescript 산출물(schema.d.ts)의 alias 다.
 * 서버 계약이 진실이므로 여기서 필드를 덧대지 않는다. 예외는 MCP Server·Gateway·Agent 의 warnings 하나다
 * (배포 응답에만 있고 Out 스키마에 없다).
 * report(verify 리포트) 는 서버에서 자유 dict 라 구조를 여기서 좁힌다.
 *
 * 화면이 아직 쓰지 않는 alias 도 둔다. 여기는 서버 계약을 한눈에 읽는 자리이기도 해서, 응답 스키마의 조각에
 * 이름을 붙여 두면 새 화면을 붙이는 사람이 schema.d.ts(생성물, 2만 줄)를 뒤지지 않아도 된다. 쓰이지 않는다는
 * 이유로 지우면 다음 사람이 같은 줄을 다시 쓰게 된다.
 */
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

// -- version ------------------------------------------------------------------

export type VersionInfo = Schemas["VersionInfo"];

// -- backend apis ---------------------------------------------------------------

export type VerdictLevel = "ok" | "warn" | "error";
export type ItemLevel = "error" | "warn" | "info";
export type CheckStatus = "pass" | "warn" | "fail" | "skip";

export type VerdictItem = Schemas["VerdictItem"];
export type Verdict = Schemas["Verdict"];

export interface CheckResult {
  id: string;
  status: CheckStatus;
  message?: string | null;
  [key: string]: unknown;
}

/** verify 리포트는 서버에서 dict 다. UI 가 쓰는 부분만 좁힌다. */
export interface VerifyReport {
  ok?: boolean;
  complete?: boolean;
  summary?: Record<string, unknown>;
  results?: CheckResult[];
  [key: string]: unknown;
}

export type VerifyResponse = Omit<Schemas["VerifyResponse"], "report"> & { report: VerifyReport | null };
export type ConvertResponse = Omit<Schemas["ConvertResponse"], "report"> & { report: VerifyReport | null };

export type AuthKind = "none" | "basic" | "bearer" | "api_key";

export type BackendAPI = Schemas["BackendAPISummary"];
export type BackendAPIVersion = Omit<Schemas["BackendAPIVersionOut"], "report"> & { report: VerifyReport };
/**
 * 버전 목록과 apply-draft 의 응답. 서버가 report 를 `{ok, summary}` 로 줄여 보낸다(api/backend_apis.py
 * `_version_out`). 검사 결과 전부는 `GET .../versions/{n}` 의 BackendAPIVersion 에만 있다.
 */
export type BackendAPIVersionSummary = Omit<Schemas["BackendAPIVersionOut"], "report"> & { report: Pick<VerifyReport, "ok" | "summary"> };
export type UploadResponse = Schemas["UploadResponse"];
export type BackendAPIToken = Schemas["TokenMeta"];
/** 버전의 변환된 OpenAPI 3.x 문서 그대로. 모양은 lib/openapi.ts 가 푼다. */
export type OpenApiDocument = Record<string, unknown>;
/** 버전에 등록된 원본 문서(올린 그대로). 변환물이 아니다. */
export type VersionSource = Schemas["VersionSourceOut"];
export type IssuedToken = Schemas["IssuedToken"];

// -- 스펙 다듬기 (D25, docs/guide/spec-refine.md 1·4절) ----------------------------------------
// 계약은 docs/guide/spec-refine.md 1·4절이다. 요청 본문은 UI 가 보내는 모양만 좁혀 적는다.

/** minify 응답과 완료된 refine job 의 변경 한 항목. pointer 는 RFC 6901, 채택 단위는 id 다. */
export type SpecChange = Schemas["SpecChange"];
export type SpecChangeKind = SpecChange["kind"];

export const MINIFY_ENRICH = ["errors", "examples"] as const;
export type MinifyEnrich = (typeof MINIFY_ENRICH)[number];

/** POST /backend-apis/{id}/versions/{n}/minify 본문. */
export interface MinifyOptions {
  enrich: MinifyEnrich[];
  max_description?: number;
  drop_value_examples: boolean;
}

export type MinifyResponse = Schemas["MinifyResponse"];

/** 서버 프리셋 이름(refine.py 의 Preset). 아래 두 목록이 satisfies 로 여기에 묶여 서버가 이름을 바꾸면 tsc 가 깨진다. */
type ServerPreset = Schemas["JobRequest"]["presets"][number];

export const REFINE_PRESETS = ["enrich_descriptions", "korean_descriptions", "fill_parameter_docs", "normalize_operation_ids"] as const satisfies readonly ServerPreset[];
export type RefinePreset = (typeof REFINE_PRESETS)[number];

export interface RefineJobRequest {
  mode: "refine";
  source: { backend_profile_id: string; version: number };
  presets: RefinePreset[];
  instruction?: string;
  provider?: string;
}

export type RenamedOperation = Schemas["RenamedOperation"];

export type SpecJobIteration = Schemas["SpecJobIteration"];

/**
 * Spec Generator job. overrides 모드(D37)도 같은 타입을 쓴다. 그쪽은 draft_yaml·renamed 가 없고 changes 만 온다.
 * source 는 refine 원본(Backend API 버전), overrides 원본(MCP Server 리비전), 합치기(D42) 전의 옛 overrides 원본 중 하나다.
 */
export type RefineJob = Schemas["SpecJobDetail"];

/** POST .../apply-draft 본문. job_id(LLM) 또는 minify 옵션(규칙) 중 하나와 채택한 변경 id 목록이다. */
export type ApplyDraftBody = ({ job_id: string } | { minify: MinifyOptions }) & { accepted_ids: string[] };

// -- 도구 오버라이드 제안 (D37) ------------------------------------------------------------------

/** refine 의 프리셋 넷 중 이 모드에 뜻이 있는 둘. 대상이 이름·설명뿐이라 나머지는 여기 없다. */
export const OVERRIDES_PRESETS = ["enrich_descriptions", "korean_descriptions"] as const satisfies readonly ServerPreset[];
export type OverridesPreset = (typeof OVERRIDES_PRESETS)[number];

/** 오버라이드 제안 job 생성 본문. source 가 MCP Server 리비전이라 refine 과 모양이 다르다. */
export interface OverridesJobRequest {
  mode: "overrides";
  source: { mcp_server_id: string; revision: number };
  presets: OverridesPreset[];
  instruction?: string;
  provider?: string;
}

/** POST /mcp-servers/{id}/apply-draft 본문. 채택 대상은 언제나 overrides job 하나다. */
export interface McpApplyDraftBody {
  job_id: string;
  accepted_ids: string[];
}

// -- data services --------------------------------------------------------------

export type DataServiceKind = "postgres" | "redis";
export type DataService = Schemas["DataServiceOut"];
export type DataServiceVerifyResult = Schemas["DataServiceVerifyResponse"];
/** TLS 끝점이 있는 Redis 에 아직 평문 포트로 붙는 소비자 (P61 5절 4단계). */
export type PlainRedisConsumer = Schemas["PlainRedisConsumerOut"];

// -- knowledge bases (D32, resource-model 8절) --------------------------------------------

export type KnowledgeBaseState = KnowledgeBase["state"];
export type KnowledgeDocumentState = KnowledgeDocument["state"];
export type KnowledgeEmbedding = Schemas["Embedding"];
export type KnowledgeChunking = Schemas["Chunking"];
export type KnowledgeLimits = Schemas["Limits"];
/** 문서 컬렉션 집계. 목록·상세·생성·수정 응답에 항상 있다. */
export type KnowledgeStatus = Schemas["KnowledgeStatus"];
export type KnowledgeBase = Schemas["KnowledgeBaseOut"];
export type KnowledgeDocumentSource = Schemas["Source"];
/** 문서 오류. detail 은 서버가 만든 코드·클래스 이름뿐이고 provider·libpq 원문은 없다. */
export type KnowledgeDocumentError = Schemas["DocError"];
export type KnowledgeDocument = Schemas["KnowledgeDocumentOut"];
export type KnowledgeBaseCreate = Schemas["KnowledgeBaseCreate"];
/** PATCH 는 display_name·chunking·limits 만 바꾼다. embedding·data_service_id 는 보내면 422 다. */
export type KnowledgeBasePatch = Pick<Schemas["KnowledgeBasePatch"], "display_name" | "chunking" | "limits">;
export type KnowledgeDocumentUrlBody = Schemas["KnowledgeDocumentUrlBody"];
/** deleted 가 거짓이면 테이블이 남았다(warning table_not_dropped, 리소스는 orphaned_table). */
export type KnowledgeBaseDeleteResponse = Schemas["KnowledgeBaseDeleteResponse"];
export type KnowledgeDocumentDeleteResponse = Schemas["KnowledgeDocumentDeleteResponse"];
export type KnowledgeReindexResponse = Schemas["KnowledgeReindexResponse"];

// -- clusters -------------------------------------------------------------------

export type ClusterImages = Schemas["ClusterImages"];
export type ClusterNamespaces = Schemas["ClusterNamespaces"];
export type ClusterIngress = Schemas["ClusterIngress"];
export type ClusterScheduling = Schemas["ClusterScheduling"];
export type DefaultRedis = Schemas["DefaultRedis"];
export type RbacCheck = Schemas["RbacCheckOut"];
export type Preflight = Schemas["PreflightOut"];
export type Cluster = Schemas["ClusterOut"];
/** `GET /clusters/{id}/sandbox`. 물을 때 센 코드 실행 게이트와 용량이다 (P40 7절·8절). */
export type ClusterSandboxStatus = Schemas["ClusterSandboxStatus"];

/** 배포 기본값 편집 폼이 다루는 부분. PATCH /clusters/{id} 본문의 부분집합. */
export interface ClusterDefaults {
  images: ClusterImages;
  namespaces: ClusterNamespaces;
  ingress: ClusterIngress;
  scheduling: ClusterScheduling;
  default_redis: DefaultRedis | null;
}

// -- mcp servers (정의 쪽) ---------------------------------------------------------

export type ToolOverride = Schemas["ToolOverride"];
export type ToolDetail = Schemas["ToolDetail"];
export type ProfileRuntimeSpec = Schemas["ProfileRuntimeSpec"];
export type Binding = Schemas["Binding"];
export type BackendSelection = Schemas["BackendSelectionOut"];
/**
 * 정의와 배포가 한 문서에 산다 (D42). `state` 가 배포 여부이고 `draft` 면 배포 쪽(`cluster_id`·`k8s`·
 * `status`)이 비어 있다. `hub_item` 은 게시 상태이고 배포 응답에만 `warnings` 가 붙는다.
 */
export type MCPServer = Omit<Schemas["MCPServerOut"], "hub_item"> & {
  hub_item: InstanceHubItem | null;
  warnings?: string[];
};
/** PUT 의 답. 규칙 저장의 알림이 붙는다 (P52 1절). */
export type MCPServerSaved = MCPServer & { rules_notice: RulesNotice };
/** 규칙 저장의 알림 (P52 1절). opened 는 모두에게 열린 Tool 의 키, rolling_agents 는 토큰을 다시 받으려고 다시 뜰 수 있는 Agent 다. */
export type RulesNotice = Schemas["RulesNotice"];
export type MCPServerRevision = Schemas["MCPServerRevisionOut"];
export type MCPServerDeployBody = Schemas["daegam__api__schemas__mcp_servers__DeployRequest"];
export type MCPServerCloneBody = Schemas["daegam__api__schemas__mcp_servers__CloneRequest"];

export type Manifest = Record<string, unknown>;
export type PreviewResponse = Schemas["PreviewResponse"];

// -- mcp servers (배포 쪽) ---------------------------------------------------------

export type InstancePhase = "Pending" | "Rolling" | "Ready" | "Failed" | "Missing" | "Deleted";
/** Agent 만 갖는 쉬는 phase (P44 10절). 둘 다 런타임을 읽지 않는다. Sleeping 은 0 레플리카로 잠든 것, Waking 은 다시 띄우는 중이다. */
export type SleepPhase = "Sleeping" | "Waking";

export type InstanceCondition = Schemas["InstanceCondition"];
export type InstanceStatus = Schemas["InstanceStatus"];

/** 허브에 게시돼 있을 때 MCP Server 응답에 붙는 요약 (resource-model 3절, D23). gateway_ids 는 이 항목을 백엔드로 쓰는 Gateway 다. */
export type InstanceHubItem = Schemas["HubItemRef"];

/** 문서 상태 여섯 (D42). CR phase(`InstancePhase`)와 다른 축이다. */
export type ResourceState = "draft" | "deploying" | "deployed" | "failed" | "undeploying" | "deleting";

export type ManifestDocument = Schemas["ManifestDocument"];
export type ManifestResponse = Schemas["ManifestResponse"];
export type OverridesResponse = Schemas["OverridesResponse"];

// -- references (D43) ------------------------------------------------------------

/** `GET /{리소스}/{id}/references` 의 항목. 종류와 무관하게 모양이 하나다. */
export type Reference = Schemas["ReferenceOut"];
export type ReferenceKind = Reference["kind"];
/** 정의가 붙드는지 배포가 핀한 리비전이 붙드는지. */
export type ReferenceRevision = Reference["revision"];

// -- auth ------------------------------------------------------------------------

export type AuthConfig = Schemas["AuthConfig"];
export type MeMembership = Schemas["MeMembership"];
export type MeOut = Schemas["MeOut"];
// local 인증 모드 (D40)
export type TokenOut = Schemas["TokenOut"];
export type UserOut = Schemas["UserOut"];
export type UserSummaryOut = Schemas["UserSummaryOut"];
export type UserCreateIn = Schemas["UserCreateIn"];
export type UserPatchIn = Schemas["UserPatchIn"];
export type TenantMembership = Schemas["TenantMembership"];
// 개인 MCP 토큰 (P32). 목록에는 토큰 값이 없고 발급 응답에만 있다
export type PersonalToken = Schemas["PersonalTokenOut"];
export type PersonalTokenStatus = PersonalToken["status"];
export type RevokeReason = NonNullable<PersonalToken["revoke_reason"]>;
export type PersonalTokenIssueIn = Schemas["PersonalTokenIssueIn"];
export type IssuedPersonalToken = Schemas["IssuedPersonalToken"];
// 서비스 어카운트 (P53b 6.7절). 토큰은 개인 토큰과 같은 모양이다
export type ServiceAccounts = Schemas["ServiceAccountsOut"];
export type ServiceAccount = Schemas["ServiceAccountOut"];
export type ServiceAccountIn = Schemas["ServiceAccountIn"];
export type ServiceAccountTokenIn = Schemas["ServiceAccountTokenIn"];
// MCP 클라이언트 연결 동의 (P33). 요청 핸들을 본문으로 되돌려 보내고 내용과 돌아갈 주소를 받는다
export type ConsentView = Schemas["ConsentOut"];
export type ConsentDecision = Schemas["ConsentDecisionOut"];
// MCP 클라이언트의 OAuth 연결 (P33 12절). 토큰 값도 해시도 없다
export type OAuthGrant = Schemas["OAuthGrantOut"];
/** 내가 기억한 승인 선택 하나 (P43 5절). 지운 Agent 의 것은 agent_name 이 null 이다. */
export type ApprovalChoice = Schemas["ApprovalChoiceOut"];
export type OAuthGrantStatus = OAuthGrant["status"];
export type GrantRevokeReason = NonNullable<OAuthGrant["revoked_reason"]>;
export type OAuthClientKind = OAuthGrant["client_kind"];

// -- settings --------------------------------------------------------------------

export type OidcSettings = Schemas["OidcSettings"];
export type ProviderKind = Exclude<LLMProvider["kind"], "stub">;
export type LLMProvider = Schemas["LLMProvider"];
/** OpenRouter 업체 선호 (T19). openrouter provider 만 갖는다. */
export type ProviderRouting = Schemas["ProviderRouting"];
/** `POST /settings/llm-providers/{name}/check`. code 는 고정 6종이고 provider 원문 메시지는 없다. */
export type LLMProviderCheck = Schemas["LLMProviderCheckOut"];
export type PlatformSettings = Schemas["PlatformSettingsOut"];
export type PlatformSettingsPatch = Schemas["SettingsPatch"];
export type DataPlaneAuth = Schemas["DataPlaneAuthOut"];
/** oidc 모드의 데이터 플레인 발급자와 그 전환 (P53b 6.9절). 다른 모드에서는 null 이다. */
export type DataPlaneIssuer = Schemas["DataPlaneIssuerOut"];
/** 플랫폼 웹 Tool 의 설정 (P39b 15절). PATCH 는 절을 통째로 바꾼다. */
export type WebToolsSettings = Schemas["WebToolsSettings"];
export type WebSearchProvider = Schemas["WebSearchSettings"]["provider"];
export type WebSearchCheck = Schemas["WebSearchCheckOut"];
/** tools 노드의 Tool 효과 (P39b 14절 4부). `POST /agents/validate` 의 `tool_effects` 다. */
export type ToolEffect = Schemas["AgentValidateResponse"]["tool_effects"][string][string];
/** tools 노드가 Tool 로 쓰는 게시한 Agent (P46 6.2절). `POST /agents/validate` 의 `agent_calls` 다. */
export type AgentCall = Schemas["AgentCallOut"];
export type TenantSettings = Schemas["TenantSettingsOut"];
export type TenantSettingsPatch = Schemas["TenantSettingsPatch"];

// -- members ---------------------------------------------------------------------

export type MembershipSource = "okta_group" | "manual";
export const TENANT_ROLES = ["tenant-admin", "developer", "viewer"] as const;
export const ALL_ROLES = ["platform-admin", ...TENANT_ROLES] as const;

export type Membership = Schemas["MembershipOut"];
export type GroupMapping = Schemas["GroupMapping"];

// -- audit -----------------------------------------------------------------------

/** `via` 는 비서가 대신 부른 호출의 표식(`assistant/<session_id>`, D31)이다. 비서가 아니면 null 이다. */
export type AuditEvent = Schemas["AuditEventOut"];

export type MCPAuditEvent = Schemas["MCPAuditEventOut"];

export interface AuditFilters {
  tenant?: string;
  since?: string;
  until?: string;
  action?: string;
  sub?: string;
  resource?: string;
  /** `assistant/<session_id>` 로 비서가 한 호출만 본다 (D31). */
  via?: string;
  limit?: number;
}

export interface MCPAuditFilters {
  tenant?: string;
  since?: string;
  until?: string;
  tool?: string;
  sub?: string;
  type?: string;
  component?: string;
  outcome?: string;
  limit?: number;
}

// -- 사용량과 가격표 (P37) -----------------------------------------------------------------
export type UsageReport = Schemas["UsageReportOut"];
export type AdminUsage = Schemas["AdminUsageOut"];
export type UsageRow = Schemas["UsageRow"];
export type UsageMetrics = Schemas["UsageMetrics"];
export type UsageSeriesDay = Schemas["UsageSeriesDay"];
export type Pricing = Schemas["PricingOut"];
/** OTLP 내보내기 설정 (P37 15절). Platform Settings 의 `observability.otlp` 이고 바꾸기는 그 경로 하나다. */
export type OtlpSettings = Schemas["OtlpSettingsOut"];
export type OtlpStatus = Schemas["OtlpStatusOut"];
/** LLM 게이트웨이의 상태 (P38 11절). platform-admin 만 읽는다. */
export type LlmGatewayStatus = Schemas["LlmGatewayStatusOut"];
export type LlmGatewayProviderState = Schemas["LlmGatewayProviderOut"];
/** Platform Settings 의 `llm_gateway.limits` (P38 7절). 분마다의 토큰 한도다. */
export type LlmGatewayLimits = Schemas["LlmGatewayLimits"];
export type PriceRule = Schemas["PriceRule-Output"];
export type DefaultPriceRule = Schemas["DefaultPriceRule"];
export type RepriceStatus = Schemas["RepriceOut"];
/** 원장 행의 호출한 곳 종류. */
export type SourceKind = NonNullable<UsageRow["source_kind"]>;
/** 질의 칸에서 null 을 뺀다. 화면은 칸을 비울 때 undefined 를 두고 `cleanQuery` 가 그 칸을 뺀다. */
type QueryOf<T> = { [K in keyof T]?: Exclude<T[K], null> };
/** GET /usage 의 질의. 묶는 기준은 서버의 타입 그대로다. */
export type TenantUsageFilters = QueryOf<NonNullable<paths["/api/usage"]["get"]["parameters"]["query"]>>;
export type TenantUsageGroup = NonNullable<TenantUsageFilters["group"]>;
/** GET /admin/usage 의 질의. 플랫폼 화면은 tenant·route 기준과 테넌트 거르기가 더해진다. */
export type UsageFilters = QueryOf<NonNullable<paths["/api/admin/usage"]["get"]["parameters"]["query"]>>;
export type UsageGroup = NonNullable<UsageFilters["group"]>;
/** GET /agents/{id}/events 의 질의. */
export type AgentEventFilter = QueryOf<NonNullable<paths["/api/agents/{agent_id}/events"]["get"]["parameters"]["query"]>>;

// -- mcp hub ---------------------------------------------------------------------

export type HubKind = Schemas["HubItemWithStatus"]["kind"];
export type HubTransport = "streamable-http" | "sse";
export type HubSource = Schemas["HubSourceOut"];
export type HubStatus = Schemas["HubStatus"];

/* 접근 정책 (docs/guide/hub-gateway.md 7절, D23). 응답은 AccessOut(세 동작 × 세 목록이 모두 있다), 입력은 Access(생략 가능)다. */
export type HubAction = "view" | "consume" | "manage";
export const HUB_ACTIONS: readonly HubAction[] = ["view", "consume", "manage"] as const;

/** 한 동작의 주체. tenants 는 경계("*" 는 모든 테넌트), roles 와 groups 는 OR 다. */
export type AccessSubjects = Schemas["SubjectsOut"];
export type HubAccess = Schemas["AccessOut"];
/** 저장 본문. 비운 동작·필드는 서버가 빈 목록으로 채운다. */
export type HubAccessInput = Schemas["Access"];
/** 요청 주체를 두고 내린 판정 결과. UI 는 버튼과 선택 가능 여부를 이것으로 그린다. */
export type HubPermissions = Schemas["PermissionsOut"];

export type HubItem = Schemas["HubItemWithStatus"];
export type HubManifestDoc = Schemas["HubManifestDocument"];
/** 도구 하나가 스스로 말하는 것 (D48). 저장된 값이 아니라 도구 탭이 그때 물어본 값이다. */
export type HubTool = Schemas["HubToolOut"];
export type HubTools = Schemas["HubToolsResponse"];
/** image 항목의 규칙 저장의 답 (P52 1절). */
export type HubToolRulesSaved = Schemas["HubToolRulesSaved"];
export type ItemToolRule = Schemas["ItemToolRule"];
/** 이 플랫폼 판과 함께 실린 카탈로그 스냅숏 (P55 4절). `admission` 은 카탈로그 이미지를 등록할 수 있는가다. */
export type HubCatalog = Schemas["HubCatalogResponse"];
export type HubCatalogServer = Schemas["HubCatalogServerOut"];
/** image 항목을 허브 키가 허용한 기록 (P55 6절). */
export type HubAdmission = Schemas["HubAdmissionOut"];
/** Platform Settings 의 허브 이미지 (P55 6절). 읽기 자격 증명은 넣었는지(`credential_set`)만 온다. */
export type HubImagesSettings = Schemas["HubImagesSettings"];
/** 허브 이미지 카드의 계산한 값. platform-admin 만 읽는다. */
export type HubImagesStatus = Schemas["HubImagesStatusOut"];

// -- skills (D64) ------------------------------------------------------------------

/** Skill 하나. 접근 정책과 판정은 허브 항목의 것을 그대로 빌린다. `tenant` 는 소유 테넌트다. */
export type Skill = Schemas["SkillOut"];
/** 가져오기 응답. 건너뛴 zip 항목(`__MACOSX/`·`.DS_Store`)이 `skipped` 에 있다. */
export type SkillImported = Schemas["SkillImportedOut"];
export type SkillFileMeta = Schemas["SkillFileMetaOut"];
export type SkillFile = Schemas["SkillFileOut"];
/** 만들기·새 리비전 본문의 파일 하나. */
export type SkillFileInput = Schemas["SkillFileIn"];
export type SkillFrontmatter = Schemas["SkillFrontmatterOut"];
/** 검사 결과 한 줄. path 가 null 이면 Skill 전체에 걸린 것이다. */
export type SkillIssue = Schemas["SkillIssueOut"];
export type SkillValidation = Schemas["SkillValidateOut"];
export type SkillRevisionMeta = Schemas["SkillRevisionMetaOut"];
export type SkillRevision = Schemas["SkillRevisionOut"];
export type SkillCloneBody = Schemas["SkillClone"];
/** Agent 가 핀한 이 Skill 의 리비전 (참조 행의 `pins`). 붙들지 않는 쪽은 null 이다. */
export type PinnedRevisions = Schemas["PinnedRevisionsOut"];

// -- gateways (정의 쪽) (D22, resource-model 4절) ----------------------------------------

export type GatewayBackend = Schemas["GatewayBackend"];
export type ConflictResolution = "auto" | "priority" | "prefix";
export type FailureMode = "fail" | "best_effort";
export type GatewayRuntime = Schemas["GatewayRuntime"];

/**
 * 정의와 배포가 한 문서에 산다 (D42). 배포 쪽 스냅샷은 정의 쪽과 이름이 겹쳐 `deployed_` 접두가 붙는다
 * (`spec.backends` 는 고른 허브 항목, `deployed_backends` 는 배포 시점에 해석한 Entry·URL).
 */
export type Gateway = Schemas["GatewayOut"] & { warnings?: string[] };
/** PUT 의 답. 검증 경고와 규칙 저장의 알림이 붙는다 (P52 1절). */
export type GatewaySaved = Schemas["GatewaySaved"];
export type GatewayRevision = Schemas["GatewayRevisionOut"];
export type GatewayDeployBody = Schemas["daegam__api__schemas__gateways__DeployRequest"];
export type GatewayCloneBody = Schemas["daegam__api__schemas__gateways__CloneRequest"];
export type GatewayBackendReport = Schemas["GatewayBackendReport"];
export type GatewayPreview = Schemas["GatewayPreviewResponse"];

// -- gateways (배포 쪽) (resource-model 4절) ---------------------------------------------

export type GatewayBackendRow = Schemas["GatewayBackendRow"];
export type GatewayBackendSnapshot = Schemas["GatewayBackendSnapshotOut"];
export type GatewayCondition = Schemas["GatewayCondition"];
export type GatewayStatus = Schemas["GatewayStatus"];
export type Exposure = Schemas["ExposureOut"];

// -- MCP Server·Gateway 메트릭 (P51 4절) ----------------------------------------------------

/**
 * `GET /mcp-servers/{id}/metrics`. 감사 기록의 시간 묶음에서 센 Tool 호출이고 모든 레플리카의 수다. Gateway 의 답
 * (`GatewayMetrics`)은 같은 모양에 백엔드별 줄 `by_backend` 가 붙는다. `source` 가 숫자를 믿어도 되는지 말한다.
 */
export type McpMetrics = Schemas["McpMetricsOut"];
export type GatewayMetrics = Schemas["GatewayMetricsOut"];
export type McpMetricsWindow = McpMetrics["window"];
/** 결과 갈래 여섯의 수. 오류는 success 가 아닌 다섯이다. */
export type McpOutcomes = Schemas["OutcomeCountsOut"];
export type McpMetricsBackend = Schemas["McpMetricsBackendOut"];

// -- 외부 노출 (resource-model 11절, D22) ----------------------------------------------------

export type ExposureInput = Schemas["ExposureInput"];
export type ExposureTLS = Schemas["ExposureTLS"];
export type ExposureTLSMode = ExposureTLS["mode"];
export type ExposureStatus = Schemas["ExposureStatus"];
export type ExposureOptions = Schemas["ExposureOptions"];
export type GatewayBasicPatch = Schemas["GatewayBasicPatch"];


// -- agents (D26, resource-model 5절) ------------------------------------------------------------

/** D28 의 노드 종류 `model` 과 간선 종류 `model` 도 생성 타입에 있다. */
export type AgentGraphNode = Schemas["GraphNode"];
export type AgentGraphEdge = Schemas["GraphEdge"];
export type AgentGraphOut = Schemas["GraphOut"];
export type AgentLayout = Record<string, Schemas["Point"]>;
export type AgentServiceSettings = Schemas["ServiceSettingsOut"];
export type AgentIssue = Schemas["AgentIssueOut"];
export type AgentValidateResponse = Schemas["AgentValidateResponse"];
export type AgentTemplate = Schemas["AgentTemplateOut"];
/** 정의(그래프)와 배포가 한 문서에 산다 (D42). */
export type Agent = Schemas["AgentOut"] & { warnings?: string[] };
export type AgentSaved = Schemas["AgentSaved"];
export type AgentRevision = Schemas["AgentRevisionOut"];
export type AgentBody = Schemas["AgentBody"];
/** PUT 본문. `expect_revision` 을 주면 그 리비전이 최신일 때만 저장되고 아니면 409 revision_conflict 다 (P48 6.7절). */
export type AgentUpdateBody = Schemas["AgentUpdateBody"];
export type AgentGraphBody = Schemas["AgentGraphBody"];
export type AgentDeployBody = Schemas["daegam__api__schemas__agents__DeployRequest"];
export type AgentCloneBody = Schemas["daegam__api__schemas__agents__CloneRequest"];
export type AgentStatus = Schemas["AgentStatus"];
/** 쉬는 상태 (P44 10절, 14절). ineligible 은 잠들 수 없는 첫 까닭이고 window_seconds 는 설치의 쉬는 창(0 이면 스스로 잠들지 않는다)이다. */
export type AgentSleep = Schemas["AgentSleepOut"];
/** 잠들 수 없는 까닭 (P44 6절). 표의 차례다. */
export type SleepIneligible = NonNullable<AgentSleep["ineligible"]>;
/** 런타임이 받는 첨부의 상한 (D61). 상태 `chat.limits` 이고 런타임 `/metrics` 의 값 그대로다. */
export type AgentChatLimits = Schemas["AgentChatLimitsOut"];
/** Agent 의 노출 스냅샷·입력 (D27). 11절 exposure 에 cors_origins·allowed_groups 가 더 있다. */
export type AgentExposure = Schemas["AgentExposureOut"];
export type AgentExposureInput = Schemas["AgentExposureInput"];
export type AgentRedeployBody = Schemas["RedeployRequest"];
/** 상태 왕복 없는 가벼운 목록 한 줄 (D35 F2). `phase` 는 마지막으로 알려진 값이다. */
export type AgentSummary = Schemas["AgentSummary"];
/**
 * Agent 의 미리 보기 하나 (P44b 6절). owner 는 시작한 사람의 sub, revision 은 그 파드가 도는 리비전이고 phase 는 Deployment 를
 * 한 번 읽은 값이다(뜨기 전이면 null). ends_at 은 처음 시작 2시간 뒤, idle_ends_at 은 마지막 활동 30분 뒤(ends_at 을 넘지 않는다)다.
 */
export type AgentPreview = Schemas["AgentPreviewOut"];
/** 미리 보기가 끝난 까닭. 플랫폼 `store/agent_previews.py` 의 `PreviewEndReason` 이다. */
export type PreviewEndReason = "idle" | "max_age" | "ended" | "ended_by_other" | "access_lost" | "source_deleted" | "disabled";
/** 하루 안에 끝난 내 미리 보기. `GET /agents/{id}/preview` 의 404 `preview_not_found` 가 `ended` 로 싣는다. by 는 끝낸 사람의 sub 다. */
export interface PreviewEnded {
  reason: PreviewEndReason;
  at: string;
  by: string | null;
}

/*
 * 아래는 채팅·세션·승인·메트릭 프록시 라우트의 계약이다. 런타임이 쓰고 플랫폼이 그대로 건네는 모양(보류 승인 요청,
 * 메시지 요약, 전사)은 서버 스키마에서 자유 객체라 여기서 손으로 적고 `api/runtimeShapes.ts` 가 좁힌다 (P73 2절).
 * 근거는 resource-model 5절 API 표와 agent-runtime/README.md 의 /sessions·/metrics·SSE 표다.
 */


export interface AgentSessionUsage {
  input_tokens: number;
  output_tokens: number;
  /** 세션 색인 행에는 없다. 합이 필요하면 `lib/usage.tokensOf` 로 더한다. */
  total_tokens?: number;
  /** input_tokens 가운데 provider 가 캐시에서 읽은 몫. 턴의 done·전사에만 있다. 단가가 낮다. */
  cached_input_tokens?: number;
}

/** 세션 색인 한 줄 (`GET /agents/{id}/sessions`). `title` 은 첫 메시지 첫 줄이나 바꾼 이름이고 P36 앞 세션은 없다. */
export type AgentSession = Schemas["AgentSessionRow"];
export type AgentSessionStatus = AgentSession["status"];

// -- 자동 실행 (P41). 실행 기록, 일정, 웹훅 트리거, 나를 대신하는 소스다. 트리거의 서명 비밀은 만들 때와 새로 만들 때의 답에만 있다
/** 실행 기록 한 줄. `usage`·`cost_usd` 는 그 세션의 원장 몫이고 세션이나 원장 행이 없으면 null 이다. */
export type AgentRun = Schemas["AgentRunOut"];
export type AgentRunStatus = AgentRun["status"];
/** 실행의 출처. `schedule`·`webhook`·`manual` 과 P46 의 `agent` 가 사전에 있고, 뒤 판이 더한 출처도 글자 그대로 온다. */
export type RunSourceKind = AgentRun["source"];
/** 다른 Agent 가 Tool 로 부른 실행의 부른 쪽 (P46 6.5절). 출처가 `agent` 인 실행에만 있다. */
export type RunCaller = NonNullable<AgentRun["caller"]>;
export type RunSchedule = Schemas["ScheduleOut"];
export type RunScheduleBody = Schemas["ScheduleIn"];
export type RunSchedulePatch = Schemas["SchedulePatch"];
export type RunTrigger = Schemas["TriggerOut"];
export type RunTriggerBody = Schemas["TriggerIn"];
export type RunTriggerPatch = Schemas["TriggerPatch"];
/** 트리거를 만들거나 비밀을 새로 만든 답. `secret` 은 이 답에서 한 번만 보인다. */
export type RunTriggerWithSecret = Schemas["TriggerSecretOut"];
/** 나에게 묶인 소스 (`GET /me/runs-as-me`). 테넌트를 가로지르므로 줄마다 테넌트와 Agent 이름이 있다. */
export type RunsAsMe = Schemas["RunsAsMeOut"];
export type BoundSchedule = Schemas["BoundScheduleOut"];
export type BoundTrigger = Schemas["BoundTriggerOut"];

/** 답변 피드백 한 건 (P36 6절). 소유자가 다시 연 세션의 엄지 상태와 피드백 탭의 승인자 목록이 같은 모양이다. */
export type AgentFeedback = Schemas["AgentFeedbackOut"];
export type AgentFeedbackBody = Schemas["AgentFeedbackBody"];
/** 세션의 내 평가와 설치의 평가 보존 기간(일). 의견 창의 동의 안내가 보존 기간을 읽는다(P45 8절). */
export type AgentSessionFeedback = Schemas["AgentSessionFeedbackOut"];
/** 피드백 탭이 보는 승인자 목록의 한 쪽. `next_before` 가 다음 쪽의 `before` 다. */
export type AgentFeedbackList = Schemas["AgentFeedbackListOut"];
export type FeedbackRating = AgentFeedback["rating"];
export type FeedbackTag = AgentFeedback["tags"][number];

// -- 되돌리기와 평가 (P45, D109) ----------------------------------------------------------------

/** 배포 기록 한 줄 (P45 6절). action 과 outcome 은 뒤 판의 새 값도 읽히게 글자다. */
export type DeployRecord = Schemas["DeployRecordOut"];
/** Agent 의 배포 게이트 (P45 12절). 리비전 밖의 설정이고 꺼져 있으면 Agent 의 `evaluation_gate` 가 null 이다. */
export type EvaluationGate = Schemas["EvaluationGateOut"];
export type EvaluationGateBody = Schemas["EvaluationGateBody"];
/** `POST /agents/{id}/rollback` 본문. `skip_evaluation` 은 배포와 재배포 본문에도 있다. */
export type AgentRollbackBody = Schemas["RollbackRequest"];
export type SkipEvaluation = Schemas["SkipEvaluation"];
/** 평가 세트 하나와 그 지문 (P45 7절). */
export type EvaluationSet = Schemas["EvaluationSetOut"];
export type EvaluationSetBody = Schemas["EvaluationSetBody"];
export type EvaluationGrader = Schemas["Grader"];
/** 사례 하나. origin 이 있으면 대화에서 복사한 사례다 (8절). */
export type EvaluationCase = Schemas["EvaluationCaseOut"];
export type EvaluationCaseList = Schemas["EvaluationCasesOut"];
export type EvaluationCaseBody = Schemas["EvaluationCaseBody"];
export type EvaluationCaseContent = Schemas["CaseContent"];
export type EvaluationCaseContentIn = Schemas["CaseContentIn"];
export type EvaluationCheckIn = Schemas["CheckIn"];
export type EvaluationCheckKind = EvaluationCheckIn["kind"];
export type EvaluationCaseOrigin = Schemas["CaseOrigin"];
/** `from-turn` 이 돌려준 저장하기 전의 복사본. replaced 는 자리표로 바꾼 개인정보의 종류마다의 수다. */
export type EvaluationCaseDraft = Schemas["EvaluationCaseDraftOut"];
/** 실행 하나 (9절). 판정과 합계는 승인자 모두가, 결과의 내용은 시작한 사람만 본다. */
export type EvaluationRun = Schemas["EvaluationRunOut"];
export type EvaluationRunDetail = Schemas["EvaluationRunDetailOut"];
/** 세트의 실행 목록과 부른 사람이 시작하기 전에 확인할 사례. */
export type EvaluationRunList = Schemas["EvaluationRunsOut"];
export type EvaluationReview = Schemas["EvaluationReviewOut"];
export type EvaluationVerdict = Schemas["CaseVerdict"];
export type EvaluationResult = Schemas["EvaluationResultOut"];
export type EvaluationToolCall = Schemas["ToolCallRecord"];

/** 이 에이전트가 기억하는 내 파일 목록 (P36 8절). `path` 는 에이전트가 보는 `/memories/…` 경로이고 `size` 는 UTF-8 바이트다. */
export type AgentMemories = Schemas["AgentMemoriesOut"];
export type AgentMemoryFileRow = Schemas["AgentMemoryFileRow"];
/** 기억 파일 하나의 내용. `encoding` 은 `utf-8` 이거나 `base64` 다. */
export type AgentMemoryFile = Schemas["AgentMemoryFileOut"];

/** 채팅에 붙일 파일의 업로드 한 건 (P36 9절). 보낼 때는 `id` 만 싣고, 문서면 뽑은 글의 글자 수와 쪽·시트 수가 있다. */
export type AgentUpload = Schemas["AgentUploadOut"];

/**
 * 세션 파일 목록 (P36 10절). `origin` 이 `upload` 면 사용자가 `/uploads/` 에 업로드한 파일이고 `agent` 면 에이전트가 파일 도구로 쓴
 * 텍스트 파일이다. `size` 는 바이트다.
 */
export type AgentSessionFiles = Schemas["AgentSessionFilesOut"];
export type AgentSessionFileRow = Schemas["AgentSessionFileRow"];

/** 승인 대기 중인 도구 호출 하나. deepagents `action_requests` 항목 그대로다. */
export interface AgentActionRequest {
  name: string;
  args: Record<string, unknown>;
  description?: string | null;
  /** 비서(D31)가 이 호출이 참조하는 fetch_url 파일과 그 출처를 싣는다. Agent 채팅에는 오지 않는다. */
  files?: { name: string; url?: string | null }[] | null;
  /** 왜 묻는가 (P43 4절). `rule` 은 정한 승인 규칙, `outside` 는 대화에 들어온 외부 내용이다. 옛 런타임은 싣지 않는다. */
  reason?: ApprovalReason;
  /** 비서가 Agent 정의를 제안하는 호출이면 카드가 그리는 제안 덩어리다(P48 6.4절). Agent 채팅에는 오지 않는다. */
  proposal?: AgentProposal;
}

/*
 * Agent 정의 제안 (P48 6.4절). 플랫폼이 기준 리비전과 짜 맞춘 정의를 한 번 비교한 결과이고 카드·캔버스는 그것을 그리기만
 * 한다. 서버 스키마에서는 자유 객체라 `api/runtimeShapes.ts` `normalizeProposal` 이 좁힌다. 계약은 docs/guide/assistant.md 5절이다.
 */

/** 바뀐 노드 하나. name 은 노드 data 의 name 이 있을 때만 온다. */
export interface ProposalNode {
  id: string;
  type: string;
  name?: string;
  /** 바뀐 노드의 바뀐 data 칸 이름. 종류가 바뀌었으면 `type` 하나다. */
  fields?: string[];
}

export interface ProposalEdge {
  source: string;
  target: string;
  kind: string;
}

/** 글 칸 하나의 줄 비교. 바뀐 줄만 앞(`-`)·뒤(`+`)로 오고, 줄 수나 줄 길이 상한에 걸렸으면 cut 이다. */
export interface ProposalTextDiff {
  node_id: string;
  field: string;
  lines: { op: "-" | "+"; text: string }[];
  cut: boolean;
}

export interface ProposalChanges {
  nodes: { added: ProposalNode[]; changed: ProposalNode[]; removed: ProposalNode[] };
  edges: { added: ProposalEdge[]; removed: ProposalEdge[] };
  /** 바뀐 service 칸 이름. */
  service: string[];
  texts: ProposalTextDiff[];
  /** 표시 이름이 바뀔 때만 온다. */
  display_name?: { before: string | null; after: string | null };
}

/** 먼저 볼 것이 가리키는 소스·Knowledge Base·Skill. 이름은 플랫폼이 그 문서에서 읽은 것이고 못 읽었으면 없다. */
export interface ProposalRef {
  id: string;
  kind?: string;
  name?: string;
  revision?: number | null;
}

/** 저장하기 전에 먼저 볼 변경 하나. code 마다 쓰는 칸이 다르다(P48 6.4절 표). */
export interface ProposalAttention {
  code: string;
  node_id?: string;
  agent?: string;
  tool?: string;
  position?: string;
  provider?: string;
  model?: string | null;
  source?: ProposalRef;
  knowledge?: ProposalRef;
  skill?: ProposalRef;
  fields?: string[];
}

export interface AgentProposal {
  proposal_id: string;
  kind: "create" | "update";
  agent: { id: string | null; name: string; display_name: string | null };
  /** 바꾸기의 기준 리비전. 만들기는 null 이다. */
  base_revision: number | null;
  /** 배포된 리비전. 배포 전이거나 만들기면 null 이다. */
  deployed_revision: number | null;
  changes: ProposalChanges;
  attention: ProposalAttention[];
  /** 검증 경고. 캔버스의 이슈 표로 문장을 만든다. */
  warnings: { code: string; node_id: string | null; message: string | null }[];
}

/**
 * `GET /assistant/sessions/{id}/proposal`. 기다리는 제안을 저장해 둔 기준 리비전 위에 짜 맞춘 정의와 그 덩어리다.
 * latest_revision 이 base_revision 과 다르면 그 사이 새 리비전이 저장된 것이다.
 */
export type AssistantProposalView = Omit<Schemas["AssistantProposalOut"], "proposal"> & { proposal: AgentProposal };

export type ApprovalReason = "rule" | "outside";

/** 세션 소유자가 결정과 함께 기억하는 선택 (P43 5절). */
export type RememberChoice = "approve" | "reject";

export interface AgentReviewConfig {
  action_name: string;
  allowed_decisions: AgentDecisionType[];
}

/** `GET /sessions/{sid}/pending` 과 SSE `interrupt` 이벤트의 data. */
export interface AgentPending {
  session_id: string;
  action_requests: AgentActionRequest[];
  review_configs: AgentReviewConfig[];
  /**
   * 요청마다(action_requests 와 같은 순서) 기억할 수 있는 선택과 그 선택이 남는 날 수다. 플랫폼의 `GET /pending` 이 부른
   * 사람이 세션 소유자일 때만 싣는다. SSE `interrupt` 와 옛 플랫폼에는 없고, 없으면 카드가 기억하기를 보이지 않는다.
   */
  remember_options?: RememberChoice[][];
  remember_days?: number;
}

/**
 * 세션 상세의 메시지 요약 한 줄. 런타임 `summarize_messages`(agent-runtime/runtime/stream.py)가 체크포인트 메시지의
 * 앞 200자만 싣는다. role 은 langchain 메시지 종류라 human 은 사용자, ai 는 에이전트, tool 은 도구 결과다.
 */
export interface AgentSessionMessage {
  role: "human" | "ai" | "tool" | "system" | string;
  text: string;
  /** ai 가 이 메시지에서 부른 도구 이름. */
  tool_calls?: string[];
  /** tool 의 도구 이름과 결과 상태. */
  tool?: string;
  status?: "success" | "error" | string;
}

/** 세션 상세: 색인 한 줄 + 체크포인트의 메시지 요약 + 보류 여부 + 화면용 전사.
 *
 * 서버는 색인 줄을 `session` 으로 감싸 보내고 `pending` 은 참·거짓이다. 승인 요청 내용은
 * 별도 `GET /sessions/{sid}/pending`(`getAgentPending`)이다. 세션이 닫히면 체크포인트가 지워져
 * 요약은 비고 전사는 보존 기간 동안 남는다 (D46). */
export interface AgentSessionDetail {
  session: AgentSession;
  messages: AgentSessionMessage[];
  pending: boolean;
  /** 화면용 전사 (D35 F1). `limit` 을 넘으면 앞에서부터 버린다. */
  transcript?: { items: AssistantTranscriptItem[]; limit?: number };
}

/**
 * 도는 턴을 따라가는 가벼운 읽기 (P78 2절, `getAgentSessionProgress`). 색인 줄의 `running`·`turn_id` 와 지금까지 저장된 전사,
 * 플랫폼이 정한 다시 읽을 간격(초)이다.
 */
export interface AgentSessionProgress {
  session: AgentSession;
  transcript: AgentSessionDetail["transcript"];
  pollSeconds: number;
}

export type AgentDecisionType = "approve" | "edit" | "reject" | "respond";

/** `POST /sessions/{sid}/decisions` 본문의 결정 하나. edit 는 edited_action, respond 는 message 를 갖는다. */
export type AgentDecision =
  | { type: "approve"; remember?: "approve" }
  | { type: "edit"; edited_action: { name: string; args: Record<string, unknown> } }
  | { type: "reject"; message?: string; remember?: "reject" }
  | { type: "respond"; message: string };

/**
 * `POST /sessions/{sid}/decisions` 본문. 서버 스키마는 결정 하나를 자유 객체로 받아 런타임에 그대로 건넨다(런타임이
 * 결정의 모양을 본다). UI 는 보내는 쪽이라 런타임이 받는 네 갈래로 좁혀 둔다. 생성 타입보다 좁아 그대로 넘길 수 있다.
 */
export interface AgentDecisionsBody {
  decisions: AgentDecision[];
  /** 이 턴에 장기 기억 저장소를 안 붙인다(D34). `/chat` 과 같다. */
  ephemeral?: boolean;
}

/** `POST /agents/{id}/chat` 본문. `ephemeral` 은 이 턴에 장기 기억 저장소를 안 붙이는 표시이고(D34) `attachments` 는 업로드 id 다(P36 9절). */
export type AgentChatBody = Schemas["AgentChatBody"];

/**
 * `GET /agents/{id}/metrics` (P37 11절). 런타임 몫(세션·누적·일당 예산·Tool 적재·경고)은 지금 값이고 런타임에 연결할 수
 * 없으면 null 이며 `runtime_error` 가 까닭이다. 나머지는 고른 기간 안의 모든 레플리카를 플랫폼이 센 값이다.
 */
export type AgentMetrics = Schemas["AgentMetricsOut"];
export type MetricsWindow = AgentMetrics["window"];
/** 세션의 턴 시도 하나와 그 실행들 (P37 12절). 승인 뒤 재개는 같은 턴의 다음 실행이다. */
export type AgentTurn = Schemas["AgentTurnOut"];
export type AgentTurnRun = Schemas["TurnRunOut"];
/** 실행 하나의 트레이스. span 과 점 이벤트, 원장 금액이다. */
export type AgentTrace = Schemas["AgentTraceOut"];
export type TraceSpan = Schemas["TraceSpanOut"];

/**
 * `GET /agents/{id}/events`. `agent_events` 한 줄이다(플랫폼 `AgentEventOut`). 런타임이 밀어 보냈거나(`source: push`) 수집기가
 * 파드 로그에서 읽은 것(`log`)이다. 갈래는 `event`, 주체는 `user_sub` 이고 턴 본문(`usage`·`tool_calls`)은 `data` 안이다.
 * 본문(`body`)은 record_bodies 가 켜졌을 때만 있고 `agent:approve` 에게만 온다.
 */
export type AgentEvent = Schemas["AgentEventOut"];

// -- 플랫폼 비서 (D31, phase-14) -------------------------------------------------------------------------

/*
 * 라우트의 응답·본문은 생성 타입의 별칭이다. SSE 이벤트의 data(승인 요청, 질문, 리소스 카드)와 전사 항목은 서버
 * 스키마가 없거나 자유 객체라 여기서 손으로 적는다. 계약은 docs/guide/assistant.md 9절이다.
 */

/** 비서는 항상 추론한다. off 는 없다(gpt-5 계열이 OpenAI 기본치로 돌아 더 느리고 비싸진다). */
export const ASSISTANT_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const satisfies readonly Schemas["AssistantSettings"]["effort"][];
/** 한 턴의 도구 호출 상한. 서버 규칙(MAX_TOOL_CALLS_PER_TURN)과 같다. */
export const ASSISTANT_MAX_TOOL_CALLS = 60;
export type AssistantEffort = (typeof ASSISTANT_EFFORTS)[number];

/** Settings `assistant{...}`. 기본값은 스펙의 것(effort low, 턴당 도구 24회, 턴 300초)이다. */
export type AssistantSettings = Schemas["AssistantSettings"];

export const ASSISTANT_SETTINGS_DEFAULTS: AssistantSettings = { enabled: false, confirm_deploy: true, effort: "low", max_tool_calls_per_turn: 24, turn_timeout_seconds: 300 };

/** 홈에서 고를 수 있는 provider 한 줄 (D45). 키 값도 Secret 이름도 오지 않는다. */
export type AssistantProvider = Schemas["AssistantProviderOut"];

/** `GET /assistant/config`. enabled 는 settings.assistant.enabled 와 provider 해소 성공의 논리곱이다.
 * providers 는 대화에 붙일 수 있는 목록이고 비서가 꺼져 있으면 비어 있다 (D45). */
export type AssistantConfig = Schemas["AssistantConfigOut"];

/** 세션 목록 한 줄 (`GET /assistant/sessions` 의 items[]). provider 는 이 대화에 붙은 것이고 고르지
 * 않았으면 null 이다 (D45). */
export type AssistantSessionSummary = Schemas["AssistantSessionSummary"];

/** 세션 문서의 보류. 호출마다 `id` 가 결정 본문의 id 다. */
export type AssistantPendingState = Schemas["AssistantPendingOut"];

/** `ask_user` 의 질문. 선택지가 있으면 버튼으로, 언제나 자유 입력으로도 답한다. */
export interface AssistantQuestion {
  text: string;
  options?: string[] | null;
  field?: string | null;
}

/** interrupt data 의 action_requests 항목. id 는 선택이고 없으면 세션의 pending.calls[i].id 를 쓴다. */
export type AssistantActionRequest = AgentActionRequest & { id?: string };

/** SSE `interrupt` 의 data. AgentPending 모양 위에 kind·question 이 얹혀 승인 카드(ApprovalCard)를 그대로 쓴다. */
export interface AssistantPending extends AgentPending {
  action_requests: AssistantActionRequest[];
  kind?: "confirm" | "question";
  question?: AssistantQuestion | null;
}

/**
 * 저장된 전사 항목 하나. UI ChatItem 과 같은 `{kind, ...}` 이고 비서와 Agent 런타임이 같은 어휘로 쓴다. 서버 스키마에서는
 * 자유 객체라 `api/runtimeShapes.ts` 가 좁히고 `lib/assistant/transcript.ts` 가 접는다. 모르는 종류는 건너뛴다.
 */
export interface AssistantTranscriptItem {
  kind: string;
  [key: string]: unknown;
}

/** `GET /assistant/sessions/{id}`. 복원용 전사와 살아 있는 보류. 전사 항목은 자유 객체라 접을 때 좁힌다. */
export type AssistantSessionDetail = Schemas["AssistantSessionOut"];

/**
 * 턴마다 보내는 화면 문맥. 프롬프트는 "사용자는 지금 mcp-server petstore 상세를 보고 있다" 로 넣는다. `unsaved_edits` 는 기본값이
 * 있어 생성 타입이 필수로 두지만 참일 때만 보낸다(lib/assistant/context.ts `buildContext`).
 */
export type AssistantContext = Omit<Schemas["AssistantUiContext"], "unsaved_edits"> & { unsaved_edits?: boolean };

/** 턴 본문. provider 는 이 대화에 붙일 것이고(D45) 첫 턴에 보내면 세션이 기억한다. */
export type AssistantTurnBody = Omit<Schemas["AssistantTurnBody"], "context"> & { context?: AssistantContext | null };

export type AssistantDecision = Schemas["AssistantDecision"];
export type AssistantDecisionsBody = Schemas["AssistantDecisionsBody"];

/** SSE `resource` 의 data. 패널이 링크 카드로 그린다. url 은 앱 안 경로다. */
export interface AssistantResource {
  /** 리소스 종류. 보통은 `lib/deletion.ts` 의 `DeletableResource` 아홉 중 하나이고, 화면 이름은 `resourceLabel` 이 옮긴다. */
  kind: string;
  id: string;
  name: string;
  /** 표시 이름이 K8s 이름과 다를 때만 온다. */
  display_name?: string;
  url: string;
}
