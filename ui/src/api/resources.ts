/** 리소스별 API 호출. 화면은 이 함수들만 쓰고 경로 문자열을 직접 쓰지 않는다. */
import { api, chatPath, chatRoute, chatSessionPath, cleanQuery, openSse, postFile, rawJson, unwrap, type ChatBase } from "./client";
import { agentTranscript, normalizePending, normalizeProposal, sessionMessages } from "./runtimeShapes";
import type {
  AdminUsage,
  LlmGatewayStatus,
  OtlpSettings,
  OtlpStatus,
  PriceRule,
  Pricing,
  RepriceStatus,
  Schemas,
  TenantUsageFilters,
  UsageFilters,
  UsageReport,
} from "./types";
import { recentSignInHeaders } from "@/lib/auth";
import type { DeletableResource } from "@/lib/deletion";
import type {
  AgentChatBody,
  ApprovalChoice,
  AgentCloneBody,
  AgentDecisionsBody,
  AgentDeployBody,
  AgentEvent,
  AgentEventFilter,
  AgentGraphBody,
  Agent,
  AgentSummary,
  AgentMetrics,
  MetricsWindow,
  AgentPending,
  AgentBody,
  AgentUpdateBody,
  AgentRevision,
  AgentSaved,
  AgentRedeployBody,
  AgentSession,
  AgentSessionDetail,
  AgentSessionProgress,
  AgentSessionStatus,
  AgentRun,
  RunSchedule,
  RunScheduleBody,
  RunSchedulePatch,
  RunTrigger,
  RunTriggerBody,
  RunTriggerPatch,
  RunTriggerWithSecret,
  RunsAsMe,
  BoundSchedule,
  BoundTrigger,
  AgentTemplate,
  AgentTrace,
  AgentTurn,
  AgentValidateResponse,
  ApplyDraftBody,
  AssistantConfig,
  AssistantProposalView,
  AssistantDecisionsBody,
  AssistantSessionDetail,
  AssistantSessionSummary,
  AssistantTurnBody,
  BackendAPI,
  BackendAPIToken,
  BackendAPIVersion,
  BackendAPIVersionSummary,
  Cluster,
  ClusterSandboxStatus,
  MinifyOptions,
  MinifyResponse,
  RefineJob,
  RefineJobRequest,
  ClusterDefaults,
  ConsentDecision,
  ConsentView,
  ConvertResponse,
  DataService,
  DataServiceVerifyResult,
  ExposureOptions,
  GatewayBasicPatch,
  IssuedToken,
  KnowledgeBase,
  KnowledgeBaseCreate,
  KnowledgeBaseDeleteResponse,
  KnowledgeBasePatch,
  KnowledgeDocument,
  KnowledgeDocumentDeleteResponse,
  KnowledgeDocumentUrlBody,
  KnowledgeReindexResponse,
  ManifestResponse,
  GatewayMetrics,
  McpMetrics,
  McpMetricsWindow,
  McpApplyDraftBody,
  MCPServer,
  MCPServerSaved,
  MCPServerCloneBody,
  MCPServerDeployBody,
  MCPServerRevision,
  OverridesJobRequest,
  OverridesResponse,
  PlainRedisConsumer,
  Preflight,
  PreviewResponse,
  Reference,
  UploadResponse,
  VerifyResponse,
  VersionInfo,
  VersionSource,
  Gateway,
  GatewaySaved,
  GatewayCloneBody,
  GatewayDeployBody,
  GatewayPreview,
  GatewayRevision,
  HubAccessInput,
  HubCatalog,
  HubImagesStatus,
  HubItem,
  HubToolRulesSaved,
  ItemToolRule,
  HubManifestDoc,
  HubTools,
  AuditEvent,
  AuditFilters,
  AuthConfig,
  DataPlaneAuth,
  GroupMapping,
  IssuedPersonalToken,
  LLMProviderCheck,
  MCPAuditEvent,
  MCPAuditFilters,
  Membership,
  OAuthGrant,
  OpenApiDocument,
  PersonalToken,
  PersonalTokenIssueIn,
  PlatformSettings,
  PlatformSettingsPatch,
  ServiceAccount,
  ServiceAccountIn,
  ServiceAccounts,
  ServiceAccountTokenIn,
  WebSearchCheck,
  TenantMembership,
  TenantSettings,
  TenantSettingsPatch,
  UserCreateIn,
  UserOut,
  UserPatchIn,
  UserSummaryOut,
} from "./types";

// -- version ----------------------------------------------------------------------

export async function getVersion(): Promise<VersionInfo> {
  return unwrap(await api.GET("/version"));
}

/**
 * 플랫폼 자신의 OpenAPI 문서(P59). 스키마 밖 라우트(`include_in_schema=False`)라 생성 클라이언트에 없어 rawJson 으로
 * 부른다. 그 사람의 토큰과 테넌트 헤더가 다른 API 호출처럼 붙는다.
 */
export async function getPlatformOpenApi(): Promise<OpenApiDocument> {
  return rawJson<OpenApiDocument>("/openapi.json");
}

// -- references (D43) ---------------------------------------------------------------

/**
 * 이 리소스의 참조. 상세 아홉과 삭제·배포 해제 대화상자가 같은 응답을 본다.
 * 경로는 리소스마다 달라 생성 클라이언트의 경로 타입이 갈리므로 여기서 한 번 가른다.
 */
export async function listReferences(resource: DeletableResource, id: string): Promise<Reference[]> {
  const result = await referenceRequest(resource, id);
  return unwrap(result).used_by;
}

function referenceRequest(resource: DeletableResource, id: string) {
  switch (resource) {
    case "backend-api":
      return api.GET("/backend-apis/{api_id}/references", { params: { path: { api_id: id } } });
    case "mcp-server":
      return api.GET("/mcp-servers/{server_id}/references", { params: { path: { server_id: id } } });
    case "gateway":
      return api.GET("/gateways/{gateway_id}/references", { params: { path: { gateway_id: id } } });
    case "agent":
      return api.GET("/agents/{agent_id}/references", { params: { path: { agent_id: id } } });
    case "data-service":
      return api.GET("/data-services/{ds_id}/references", { params: { path: { ds_id: id } } });
    case "knowledge-base":
      return api.GET("/knowledge-bases/{kb_id}/references", { params: { path: { kb_id: id } } });
    case "cluster":
      return api.GET("/clusters/{cluster_id}/references", { params: { path: { cluster_id: id } } });
    case "hub":
      return api.GET("/hub/{item_id}/references", { params: { path: { item_id: id } } });
    case "skill":
      return api.GET("/skills/{skill_id}/references", { params: { path: { skill_id: id } } });
  }
}

// -- backend apis -------------------------------------------------------------------

export interface SpecInput {
  text?: string;
  file?: File;
  filename?: string;
}

function specBody(input: SpecInput): RequestInit {
  if (input.file) {
    const fd = new FormData();
    fd.append("file", input.file, input.file.name);
    return { method: "POST", body: fd };
  }
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: input.text ?? "", filename: input.filename ?? null }),
  };
}

/*
 * 아래 넷은 multipart(FormData) 나 서버가 Request 로 직접 읽는 본문이라 생성 클라이언트로 부르지 못하고
 * rawJson 으로 부른다. 그 밖의 라우트는 전부 api.GET/POST/PUT/DELETE 다. SSE 라우트만 openSse 다.
 */
export async function verifySpec(input: SpecInput): Promise<VerifyResponse> {
  return rawJson<VerifyResponse>("/backend-apis/verify", specBody(input));
}

export async function convertSpec(input: SpecInput): Promise<ConvertResponse> {
  return rawJson<ConvertResponse>("/backend-apis/convert?format=json", specBody(input));
}

export async function listBackendApis(): Promise<BackendAPI[]> {
  return unwrap(await api.GET("/backend-apis"));
}

export async function getBackendApi(id: string): Promise<BackendAPI> {
  return unwrap(
    await api.GET("/backend-apis/{api_id}", { params: { path: { api_id: id } } }),
  );
}

export interface UploadSpecInput {
  file: File;
  name: string;
  display_name?: string;
  endpoint?: string;
  auth?: string;
  credential_secret?: string;
  soap_headers_secret?: string;
}

export async function uploadSpec(input: UploadSpecInput): Promise<UploadResponse> {
  const fd = new FormData();
  fd.append("file", input.file, input.file.name);
  fd.append("name", input.name);
  if (input.display_name) fd.append("display_name", input.display_name);
  if (input.endpoint) fd.append("endpoint", input.endpoint);
  if (input.auth) fd.append("auth", input.auth);
  if (input.credential_secret) fd.append("credential_secret", input.credential_secret);
  if (input.soap_headers_secret) fd.append("soap_headers_secret", input.soap_headers_secret);
  return rawJson<UploadResponse>("/backend-apis", { method: "POST", body: fd });
}

export type BackendAPIPatch = Schemas["ProfilePatch"];

export async function patchBackendApi(
  id: string,
  patch: BackendAPIPatch,
): Promise<BackendAPI> {
  return unwrap(
    await api.PATCH("/backend-apis/{api_id}", {
      params: { path: { api_id: id } },
      body: patch,
    }),
  );
}

export async function deleteBackendApi(id: string, force = false): Promise<void> {
  unwrap(
    await api.DELETE("/backend-apis/{api_id}", {
      params: { path: { api_id: id }, query: { force } },
    }),
  );
}

export async function listVersions(id: string): Promise<BackendAPIVersionSummary[]> {
  return unwrap(
    await api.GET("/backend-apis/{api_id}/versions", {
      params: { path: { api_id: id } },
    }),
  );
}

export async function getVersionDetail(
  id: string,
  version: number,
): Promise<BackendAPIVersion> {
  return unwrap(
    await api.GET("/backend-apis/{api_id}/versions/{version}", {
      params: { path: { api_id: id, version } },
    }),
  );
}

/** 버전의 변환된 OpenAPI 문서(JSON). 문서 뷰어 팝업이 열릴 때 받는다. */
export async function getVersionSpec(id: string, version: number): Promise<OpenApiDocument> {
  return unwrap(
    await api.GET("/backend-apis/{api_id}/versions/{version}/spec", {
      params: { path: { api_id: id, version } },
    }),
  ) as OpenApiDocument;
}

/**
 * 버전에 등록된 원본 문서. 편집 화면이 "등록된 문서" 로 보여 준다.
 *
 * `getVersionSpec` 은 변환된 OpenAPI 라 WSDL 로 올린 백엔드에서는 사람이 올린 것과 다른 문서가 나온다.
 */
export async function getVersionSource(id: string, version: number): Promise<VersionSource> {
  return unwrap(
    await api.GET("/backend-apis/{api_id}/versions/{version}/source", {
      params: { path: { api_id: id, version } },
    }),
  ) as VersionSource;
}

export async function listTokens(id: string): Promise<BackendAPIToken[]> {
  return unwrap(
    await api.GET("/backend-apis/{api_id}/tokens", {
      params: { path: { api_id: id } },
    }),
  );
}

/** 서버가 본문을 Request 로 직접 읽어 OpenAPI 에 requestBody 가 없다. 그래서 생성 클라이언트로는 본문을 못 싣는다. */
export async function issueToken(id: string, label: string): Promise<IssuedToken> {
  return rawJson<IssuedToken>(`/backend-apis/${encodeURIComponent(id)}/tokens`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label: label || null }),
  });
}

export async function revokeToken(id: string, tokenId: string): Promise<void> {
  unwrap(
    await api.DELETE("/backend-apis/{api_id}/tokens/{token_id}", {
      params: { path: { api_id: id, token_id: tokenId } },
    }),
  );
}

// -- 스펙 다듬기 (D25) -----------------------------------------------------------------------------

/** 규칙으로 정리. 저장 없이 changes 와 초안 YAML 을 즉시 돌려준다. */
export async function minifyVersion(id: string, version: number, options: MinifyOptions): Promise<MinifyResponse> {
  return unwrap(
    await api.POST("/backend-apis/{api_id}/versions/{version}/minify", { params: { path: { api_id: id, version } }, body: options }),
  );
}

/**
 * LLM 으로 다듬기 job 생성 (202). 422 코드: provider_no_key, no_secret_store, source_not_verified, source_too_large.
 * 진행·결과는 getSpecJob 으로 폴링한다.
 */
export async function createRefineJob(body: RefineJobRequest): Promise<RefineJob> {
  return unwrap(await api.POST("/spec-generator/jobs", { body }));
}

export async function getSpecJob(jobId: string): Promise<RefineJob> {
  return unwrap(
    await api.GET("/spec-generator/jobs/{job_id}", { params: { path: { job_id: jobId } } }),
  );
}

/**
 * 채택한 변경만 심어 v(n+1) 을 만든다. 응답은 만든 버전이고, 무변경이면 200 으로 같은 버전이다.
 * 422 는 필드 오류다(예: 모르는 accepted_ids).
 */
export async function applyDraft(id: string, version: number, body: ApplyDraftBody): Promise<BackendAPIVersionSummary> {
  return unwrap(
    await api.POST("/backend-apis/{api_id}/versions/{version}/apply-draft", { params: { path: { api_id: id, version } }, body }),
  );
}

// -- 도구 오버라이드 제안 (D37) -------------------------------------------------------------------

/**
 * 도구 오버라이드 제안 job 생성 (202). 원본은 MCP Server 리비전이고 서버가 노출 집합의 이름·설명만 뷰로 만든다.
 * 진행·결과는 refine 과 같은 getSpecJob 으로 폴링한다. 결과에는 changes 만 있고 draft_yaml·renamed 는 없다.
 */
export async function createOverridesJob(body: OverridesJobRequest): Promise<RefineJob> {
  return unwrap(await api.POST("/spec-generator/jobs", { body }));
}

/**
 * 채택한 제안만 현재 리비전 위에 심어 새 리비전을 만든다. 무변경이면 200 으로 같은 리비전이다.
 * 422 코드: unknown_change, no_changes, source_mismatch(job 이 본 backends 가 지금과 다르다), surface_changed.
 */
export async function applyMcpServerDraft(id: string, body: McpApplyDraftBody): Promise<MCPServer> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/apply-draft", { params: { path: { server_id: id } }, body }),
  );
}

/** 이 백엔드를 참조하는 MCP Server. 서버에는 이 필터가 없어 전체 목록을 받아 backends 로 거른다. */
export async function listMcpServersUsingBackend(backendProfileId: string): Promise<MCPServer[]> {
  const all = await listMcpServers();
  return all.filter((p) => p.backends?.some((b) => b.backend_profile_id === backendProfileId));
}

// -- data services -------------------------------------------------------------------

export type DataServiceCreate = Schemas["DataServiceCreate"];
export type DataServicePatch = Schemas["DataServicePatch"];

export async function listDataServices(): Promise<DataService[]> {
  return unwrap(await api.GET("/data-services"));
}

export async function getDataService(id: string): Promise<DataService> {
  return unwrap(
    await api.GET("/data-services/{ds_id}", { params: { path: { ds_id: id } } }),
  );
}

export async function createDataService(body: DataServiceCreate): Promise<DataService> {
  return unwrap(await api.POST("/data-services", { body }));
}

export async function patchDataService(id: string, body: DataServicePatch): Promise<DataService> {
  return unwrap(
    await api.PATCH("/data-services/{ds_id}", { params: { path: { ds_id: id } }, body }),
  );
}

export async function deleteDataService(id: string): Promise<void> {
  unwrap(await api.DELETE("/data-services/{ds_id}", { params: { path: { ds_id: id } } }));
}

export async function getDataServicePlainConsumers(id: string): Promise<PlainRedisConsumer[]> {
  const out = unwrap(
    await api.GET("/data-services/{ds_id}/plain-consumers", { params: { path: { ds_id: id } } }),
  );
  return out.consumers;
}

export async function verifyDataService(id: string): Promise<DataServiceVerifyResult> {
  return unwrap(
    await api.POST("/data-services/{ds_id}/verify", { params: { path: { ds_id: id } } }),
  );
}

// -- knowledge bases (D32, resource-model 8절) -----------------------------------------------

function kbPath(kbId: string, rest = ""): string {
  return `/knowledge-bases/${encodeURIComponent(kbId)}${rest}`;
}

export async function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  return unwrap(await api.GET("/knowledge-bases"));
}

export async function getKnowledgeBase(id: string): Promise<KnowledgeBase> {
  return unwrap(await api.GET("/knowledge-bases/{kb_id}", { params: { path: { kb_id: id } } }));
}

/**
 * 생성은 Database 에 접속해 pgvector 확장·벡터 테이블을 만든다. 422 코드: invalid_data_service_kind·unknown_data_service·
 * unknown_provider·provider_no_key·embedding_not_supported·dimensions_required·pgvector_missing·db_unreachable·auth_failed·
 * unknown_database·query_failed. 어느 단계든 실패하면 리소스는 생기지 않는다.
 */
export async function createKnowledgeBase(body: KnowledgeBaseCreate): Promise<KnowledgeBase> {
  return unwrap(await api.POST("/knowledge-bases", { body }));
}

/** display_name·chunking·limits 만. chunking 이 바뀌면 모든 문서가 pending 으로 돌아간다(indexing 문서가 있으면 409 indexing). */
export async function patchKnowledgeBase(id: string, body: KnowledgeBasePatch): Promise<KnowledgeBase> {
  return unwrap(await api.PATCH("/knowledge-bases/{kb_id}", { params: { path: { kb_id: id } }, body }));
}

/** 409 in_use{agent_ids, deployed_agent_ids}·indexing. deleted 가 거짓이면 테이블이 남아 orphaned_table 이고 다시 지울 수 있다. */
export async function deleteKnowledgeBase(id: string): Promise<KnowledgeBaseDeleteResponse> {
  return unwrap(await api.DELETE("/knowledge-bases/{kb_id}", { params: { path: { kb_id: id } } }));
}

export async function listKnowledgeDocuments(kbId: string): Promise<KnowledgeDocument[]> {
  return unwrap(await api.GET("/knowledge-bases/{kb_id}/documents", { params: { path: { kb_id: kbId } } }));
}

/** multipart `file`. 413 too_large(10MB), 422 unsupported_format·too_many_documents, 409 deleting. 즉시 pending 이다. */
export async function uploadKnowledgeDocument(kbId: string, file: File): Promise<KnowledgeDocument> {
  return postFile<KnowledgeDocument>(kbPath(kbId, "/documents"), file);
}

/** 공개 URL 문서. 사설·클러스터 주소는 422 url_* 이고 본문은 워커가 받는다. 이름이 없으면 URL 마지막 조각이다. */
export async function addKnowledgeDocumentUrl(kbId: string, body: KnowledgeDocumentUrlBody): Promise<KnowledgeDocument> {
  return unwrap(await api.POST("/knowledge-bases/{kb_id}/documents/url", { params: { path: { kb_id: kbId } }, body }));
}

/** 모든 문서를 pending 으로 되돌린다(indexing 문서가 있으면 409 indexing). pending 은 되돌린 문서 수다. */
export async function reindexKnowledgeBase(kbId: string): Promise<KnowledgeReindexResponse> {
  return unwrap(await api.POST("/knowledge-bases/{kb_id}/reindex", { params: { path: { kb_id: kbId } } }));
}

export async function reindexKnowledgeDocument(kbId: string, docId: string): Promise<KnowledgeDocument> {
  return unwrap(await api.POST("/knowledge-bases/{kb_id}/documents/{doc_id}/reindex", { params: { path: { kb_id: kbId, doc_id: docId } } }));
}

/** 상태를 deleting 으로 바꿀 뿐이고 행·본문·문서는 워커가 지운다. 실패한 문서에 다시 보내는 "다시 삭제" 도 같은 호출이다. */
export async function deleteKnowledgeDocument(kbId: string, docId: string): Promise<KnowledgeDocumentDeleteResponse> {
  return unwrap(await api.DELETE("/knowledge-bases/{kb_id}/documents/{doc_id}", { params: { path: { kb_id: kbId, doc_id: docId } } }));
}

// -- clusters -------------------------------------------------------------------------

export type ClusterCreate = Schemas["ClusterCreate"];
export type ClusterPatch = Schemas["ClusterPatch"];

export async function listClusters(): Promise<Cluster[]> {
  return unwrap(await api.GET("/clusters"));
}

export async function getCluster(id: string): Promise<Cluster> {
  return unwrap(
    await api.GET("/clusters/{cluster_id}", { params: { path: { cluster_id: id } } }),
  );
}

export async function createCluster(body: ClusterCreate): Promise<Cluster> {
  return unwrap(await api.POST("/clusters", { body }));
}

export async function patchCluster(id: string, body: ClusterPatch): Promise<Cluster> {
  return unwrap(
    await api.PATCH("/clusters/{cluster_id}", { params: { path: { cluster_id: id } }, body }),
  );
}

export async function patchClusterDefaults(
  id: string,
  defaults: Partial<ClusterDefaults>,
): Promise<Cluster> {
  return patchCluster(id, defaults as ClusterPatch);
}

export async function deleteCluster(id: string): Promise<void> {
  unwrap(await api.DELETE("/clusters/{cluster_id}", { params: { path: { cluster_id: id } } }));
}

export async function runPreflight(
  id: string,
): Promise<{ id: string; name: string; preflight: Preflight }> {
  return unwrap(
    await api.POST("/clusters/{cluster_id}/preflight", { params: { path: { cluster_id: id } } }),
  );
}

/** 코드 실행 게이트와 지금 센 용량 (P40 7절·8절). 클러스터의 Cluster 카드와 Agent 캔버스가 읽는다. */
export async function getClusterSandbox(id: string): Promise<ClusterSandboxStatus> {
  return unwrap(await api.GET("/clusters/{cluster_id}/sandbox", { params: { path: { cluster_id: id } } }));
}

/** 격리 점검을 뒤에서 시작한다(202). 이미 돌면 409 sandbox_check_running 이다. */
export async function checkClusterSandbox(id: string): Promise<void> {
  unwrap(await api.POST("/clusters/{cluster_id}/sandbox/check", { params: { path: { cluster_id: id } } }));
}

/** 노드를 함께 쓰는 위험을 허용한다. checkedAt 은 화면이 본 판정의 시각이고 그사이 바뀌었으면 409 sandbox_check_stale 이다. */
export async function acceptClusterSandbox(id: string, checkedAt: string): Promise<Cluster> {
  return unwrap(
    await api.POST("/clusters/{cluster_id}/sandbox/accept", { params: { path: { cluster_id: id } }, body: { checked_at: checkedAt } }),
  );
}

/**
 * 생성 폼의 노출 섹션이 클러스터·네임스페이스를 고른 뒤 부른다 (docs/guide/exposure.md 2절). TLS Secret 은
 * 이름만 온다. 권한이 없으면 403 `forbidden` 과 `missing[]`(예: secrets:list@<ns>) 이 온다.
 */
export async function getExposureOptions(clusterId: string, namespace?: string | null): Promise<ExposureOptions> {
  return unwrap(
    await api.GET("/clusters/{cluster_id}/exposure-options", {
      params: { path: { cluster_id: clusterId }, query: { namespace: namespace || null } },
    }),
  );
}

// -- mcp servers (D42: 정의와 배포가 한 리소스) ------------------------------------------------

export type MCPServerBody = Schemas["MCPServerBody"];

/** 목록은 draft 를 포함한다. 화면이 상태 열로 가른다. */
export async function listMcpServers(): Promise<MCPServer[]> {
  return unwrap(await api.GET("/mcp-servers"));
}

export async function getMcpServer(id: string): Promise<MCPServer> {
  return unwrap(
    await api.GET("/mcp-servers/{server_id}", { params: { path: { server_id: id } } }),
  );
}

/** 만들기는 정의만 받는다. 저장된 문서는 `draft` 이고 클러스터에는 아무것도 없다 (D42). */
export async function createMcpServer(body: MCPServerBody): Promise<MCPServer> {
  return unwrap(await api.POST("/mcp-servers", { body }));
}

/** PUT 은 전체 교체이고 새 리비전을 만든다. 배포 쪽은 건드리지 않는다. 답에는 규칙 저장의 알림이 붙는다 (P52 1절). */
export async function putMcpServer(id: string, body: MCPServerBody): Promise<MCPServerSaved> {
  return unwrap(
    await api.PUT("/mcp-servers/{server_id}", { params: { path: { server_id: id } }, body }),
  );
}

/** 삭제의 답. 다른 작업이 문서의 클레임을 쥐고 있으면 deleted 가 거짓이고 그 작업이 이어서 지운다 (P66). */
export async function deleteMcpServer(id: string): Promise<{ deleted: boolean }> {
  return unwrap(await api.DELETE("/mcp-servers/{server_id}", { params: { path: { server_id: id } } }));
}

export async function listMcpRevisions(id: string): Promise<MCPServerRevision[]> {
  return unwrap(
    await api.GET("/mcp-servers/{server_id}/revisions", { params: { path: { server_id: id } } }),
  );
}

export async function previewRevision(
  id: string,
  n: number,
  clusterId?: string | null,
): Promise<PreviewResponse> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/revisions/{n}/preview", {
      params: { path: { server_id: id, n }, query: { cluster_id: clusterId || null } },
    }),
  );
}

// -- mcp servers: 배포 쪽 동작 (D42) -----------------------------------------------------

export type BasicPatch = Schemas["BasicPatch"];

/**
 * 배포. `draft` 문서를 클러스터에 올린다. 본문은 클러스터·네임스페이스·노출이고 이름은 받지 않는다
 * (이름은 이미 문서의 것이다). 이미 배포돼 있으면 409 `already_deployed`.
 */
export async function deployMcpServer(id: string, body: MCPServerDeployBody = {}): Promise<MCPServer> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/deploy", { params: { path: { server_id: id } }, body }),
  );
}

/** 재배포. 핀을 현재 리비전으로 옮기고 다시 적용한다. */
export async function redeployMcpServer(id: string): Promise<MCPServer> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/redeploy", { params: { path: { server_id: id } } }),
  );
}

/** 배포 해제. 클러스터에서 내리고 정의는 남긴다(`draft` 로 돌아간다). 게시돼 있으면 409 `published`. */
export async function undeployMcpServer(id: string): Promise<MCPServer> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/undeploy", { params: { path: { server_id: id } } }),
  );
}

/** 복제. 정의를 베낀 `draft` 를 만든다. 노출 설정을 함께 주면 그것도 베낀다. */
export async function cloneMcpServer(id: string, body: MCPServerCloneBody): Promise<MCPServer> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/clone", { params: { path: { server_id: id } }, body }),
  );
}

/** Basic 편집: 배포 쪽 값(노출·replicas·리소스)을 바꾸고 재배포한다. */
export async function patchMcpServer(id: string, body: BasicPatch): Promise<MCPServer> {
  return unwrap(
    await api.PATCH("/mcp-servers/{server_id}", { params: { path: { server_id: id } }, body }),
  );
}

export async function getMcpServerManifest(id: string): Promise<ManifestResponse> {
  return unwrap(
    await api.GET("/mcp-servers/{server_id}/manifest", { params: { path: { server_id: id } } }),
  );
}

/** 메트릭 (P51 4절). tz 는 7일·30일 기간의 날을 자를 IANA 시간대다. */
export async function getMcpServerMetrics(id: string, window: McpMetricsWindow, tz: string): Promise<McpMetrics> {
  return unwrap(await api.GET("/mcp-servers/{server_id}/metrics", { params: { path: { server_id: id }, query: { window, tz } } }));
}

/** Advanced 저장: 편집한 CR 전체 YAML. 서버가 rendered 와 diff 해 overrides 로 저장하고 dry-run 뒤 apply 한다. */
export async function putOverridesYaml(id: string, yaml: string): Promise<OverridesResponse> {
  return unwrap(
    await api.PUT("/mcp-servers/{server_id}/overrides", {
      params: { path: { server_id: id } },
      body: { yaml },
    }),
  );
}

export async function driftAction(id: string, action: "reset" | "absorb"): Promise<ManifestResponse> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/drift/{action}", {
      params: { path: { server_id: id, action } },
    }),
  );
}

/** 허브 게시 본문 (docs/guide/hub-gateway.md 6절). 이름 기본값은 인스턴스 이름, access 를 생략하면 빈 정책이다. */
export type InstancePublishBody = Schemas["daegam__api__mcp_servers__PublishBody"];

/**
 * 허브에 게시 (docs/guide/hub-gateway.md 6절, D23). kind internal 항목이 생기고 이미 게시돼 있으면 200 으로 기존 항목이다.
 * 이름 충돌은 409 `name_conflict`, 다른 테넌트를 넣으면 422 `cross_tenant_unsupported`.
 */
export async function publishMcpServer(id: string, body: InstancePublishBody = {}): Promise<HubItem> {
  return unwrap(
    await api.POST("/mcp-servers/{server_id}/publish", { params: { path: { server_id: id } }, body }),
  );
}

/**
 * 게시 해제. DELETE /hub/{item} 과 같고 참조 중인 Gateway 가 있으면 409 `in_use`(`gateway_ids`),
 * manage 가 없으면 403, 게시돼 있지 않으면 404 `not_published`.
 */
export async function unpublishMcpServer(id: string): Promise<void> {
  unwrap(await api.DELETE("/mcp-servers/{server_id}/publish", { params: { path: { server_id: id } } }));
}

// -- auth / settings / members / audit (전부 스키마 타입화됨) ---------------------------

export async function getAuthConfig(): Promise<AuthConfig> {
  return unwrap(await api.GET("/auth/config"));
}

export async function listMyTenants(): Promise<TenantMembership[]> {
  return unwrap(await api.GET("/tenants"));
}

export async function getPlatformSettings(): Promise<PlatformSettings> {
  return unwrap(await api.GET("/settings"));
}

export async function patchPlatformSettings(patch: PlatformSettingsPatch): Promise<PlatformSettings> {
  return unwrap(await api.PATCH("/settings", { body: patch }));
}

/** 인증 재적용의 진행과 다시 배포할 소비자 수(P32 8절). platform-admin 만 부른다. */
export async function getDataPlaneAuth(): Promise<DataPlaneAuth> {
  return unwrap(await api.GET("/settings/data-plane-auth"));
}

/** 저장된 LLM provider 의 키 경로(Secret)와 연결(models.list)을 확인한다. 응답은 고정 코드뿐이다. */
export async function checkLlmProvider(name: string): Promise<LLMProviderCheck> {
  return unwrap(await api.POST("/settings/llm-providers/{name}/check", { params: { path: { name } } }));
}

/**
 * provider 의 API 키를 넣는다 (D56). 플랫폼이 플랫폼 네임스페이스에 관리 Secret 을 만들어 키를 넣고
 * provider 의 api_key_secret 을 그 이름으로 맞춘다. 키는 응답에 돌아오지 않는다.
 */
export async function setLlmProviderKey(name: string, apiKey: string): Promise<{ name: string; api_key_secret: string }> {
  return unwrap(await api.PUT("/settings/llm-providers/{name}/api-key", { params: { path: { name } }, body: { api_key: apiKey } }));
}

/** 웹 검색 provider 의 키를 넣는다 (D56, P39b 15절). 관리 Secret 의 이름만 돌아온다. */
export async function setWebSearchKey(apiKey: string): Promise<{ api_key_secret: string }> {
  return unwrap(await api.PUT("/settings/web-tools/search/api-key", { body: { api_key: apiKey } }));
}

/** 저장한 검색 설정으로 질의 하나를 보내 본다. 고정 코드와 걸린 시간만 돌아온다. */
export async function checkWebSearch(): Promise<WebSearchCheck> {
  return unwrap(await api.POST("/settings/web-tools/search/check"));
}

export async function listGroupMappings(): Promise<GroupMapping[]> {
  return unwrap(await api.GET("/group-mappings"));
}

export async function putGroupMappings(items: GroupMapping[]): Promise<GroupMapping[]> {
  return unwrap(await api.PUT("/group-mappings", { body: items }));
}

// -- local 인증 모드의 사용자 관리 (D40). oidc·header 모드에서는 404 다 --------------------------

/** 전체 목록. platform-admin(users:write)만. */
export async function listUsers(): Promise<UserOut[]> {
  return unwrap(await api.GET("/users"));
}

/** 사람 고르기 목록(id·username·display_name). 어느 테넌트든 members:write 가 있으면 된다. */
export async function listUserDirectory(): Promise<UserSummaryOut[]> {
  return unwrap(await api.GET("/users/directory"));
}

export async function createUser(body: UserCreateIn): Promise<UserOut> {
  return unwrap(await api.POST("/users", { body }));
}

export async function patchUser(userId: string, body: UserPatchIn): Promise<UserOut> {
  return unwrap(await api.PATCH("/users/{user_id}", { params: { path: { user_id: userId } }, body }));
}

export async function resetUserPassword(userId: string, password: string): Promise<UserOut> {
  return unwrap(
    await api.POST("/users/{user_id}/password", { params: { path: { user_id: userId } }, body: { password } }),
  );
}

export async function deleteUser(userId: string): Promise<void> {
  unwrap(await api.DELETE("/users/{user_id}", { params: { path: { user_id: userId } } }));
}

// -- 개인 MCP 토큰 (P32 6절, P53b 6.5절). local·oidc 모드에 있고 header 모드에서는 404 다 ------------

export async function listMyTokens(): Promise<PersonalToken[]> {
  return unwrap(await api.GET("/me/tokens"));
}

/**
 * 발급. 평문 토큰은 이 응답에만 있다. local 은 현재 비밀번호가 틀리면 403 invalid_credentials 이고(401 이 아니라 세션이
 * 지워지지 않는다), 로그인과 같은 잠금에 걸리면 429 login_locked 다. oidc 는 ID 토큰을 함께 싣고 로그인이 10분보다
 * 오래면 403 reauth_required 다.
 */
export async function issueMyToken(body: PersonalTokenIssueIn): Promise<IssuedPersonalToken> {
  return unwrap(await api.POST("/me/tokens", { body, headers: await recentSignInHeaders() }));
}

export async function revokeMyToken(tokenId: string): Promise<void> {
  unwrap(await api.DELETE("/me/tokens/{token_id}", { params: { path: { token_id: tokenId } } }));
}

/** 관리자(users:write)가 그 사람의 토큰을 본다. */
export async function listUserTokens(userId: string): Promise<PersonalToken[]> {
  return unwrap(await api.GET("/users/{user_id}/tokens", { params: { path: { user_id: userId } } }));
}

export async function revokeUserToken(userId: string, tokenId: string): Promise<void> {
  unwrap(
    await api.DELETE("/users/{user_id}/tokens/{token_id}", { params: { path: { user_id: userId, token_id: tokenId } } }),
  );
}

// -- 서비스 어카운트 (P53b 6.7절). local·oidc 모드에 있고 그 테넌트의 members:write 가 필요하다 --------------

/** 테넌트의 계정과 그 토큰. mode 는 DAEGAM_SERVICE_ACCOUNTS 이고 can_manage 는 이 사람이 만들고 토큰을 낼 수 있는가다. */
export async function listServiceAccounts(tenant: string): Promise<ServiceAccounts> {
  return unwrap(await api.GET("/tenants/{tenant}/service-accounts", { params: { path: { tenant } } }));
}

/** 만든다. 같은 이름이 있으면 409 name_conflict, 설정이 막으면 409 service_accounts_off 나 403 이다. */
export async function createServiceAccount(tenant: string, body: ServiceAccountIn): Promise<ServiceAccount> {
  return unwrap(await api.POST("/tenants/{tenant}/service-accounts", { params: { path: { tenant } }, body }));
}

/** 지운다. 그 계정의 토큰이 모두 회수된다. */
export async function deleteServiceAccount(tenant: string, accountId: string): Promise<void> {
  unwrap(
    await api.DELETE("/tenants/{tenant}/service-accounts/{account_id}", {
      params: { path: { tenant, account_id: accountId } },
    }),
  );
}

/** 계정의 토큰. 평문은 이 응답에만 있다. local 은 내는 사람의 비밀번호를, oidc 는 ID 토큰을 싣는다. */
export async function issueServiceAccountToken(tenant: string, accountId: string, body: ServiceAccountTokenIn): Promise<IssuedPersonalToken> {
  return unwrap(
    await api.POST("/tenants/{tenant}/service-accounts/{account_id}/tokens", {
      params: { path: { tenant, account_id: accountId } },
      body,
      headers: await recentSignInHeaders(),
    }),
  );
}

export async function revokeServiceAccountToken(tenant: string, accountId: string, tokenId: string): Promise<void> {
  unwrap(
    await api.DELETE("/tenants/{tenant}/service-accounts/{account_id}/tokens/{token_id}", {
      params: { path: { tenant, account_id: accountId, token_id: tokenId } },
    }),
  );
}

// -- 기억한 승인 선택 (P43 5절). 모든 인증 모드에서 부른 사람 자신의 것만이다 ---------------------------------

export async function listMyApprovalChoices(): Promise<ApprovalChoice[]> {
  return unwrap(await api.GET("/me/approval-choices")).choices ?? [];
}

/** 지운다. 다음 턴부터 그 Tool 을 다시 묻는다. 남의 것이거나 없으면 404 다. */
export async function deleteMyApprovalChoice(choiceId: string): Promise<void> {
  unwrap(await api.DELETE("/me/approval-choices/{choice_id}", { params: { path: { choice_id: choiceId } } }));
}

// -- MCP 클라이언트 연결 동의 (P33 7절). OAuth 가 켜진 local 모드에만 있고 다른 때는 404 다 --------------

/** 동의 화면의 내용. 소속이 아니면 대상의 공개 주소만 온다. 서명이 틀리면 404, 만료면 410, 쓰였으면 409 다. */
export async function inspectConsent(request: string): Promise<ConsentView> {
  return unwrap(await api.POST("/oauth/consent/inspect", { body: { request } }));
}

/**
 * 허용. 로그인한 지 10분이 지났으면 403 reauth_required, 소속이 아니면 403 not_a_member, 상한이면 429 다. oidc 는 ID 토큰을
 * 함께 싣는다.
 */
export async function approveConsent(request: string): Promise<ConsentDecision> {
  return unwrap(await api.POST("/oauth/consent/approve", { body: { request }, headers: await recentSignInHeaders() }));
}

export async function denyConsent(request: string): Promise<ConsentDecision> {
  return unwrap(await api.POST("/oauth/consent/deny", { body: { request } }));
}

// -- MCP 클라이언트 연결 (P33 12절). OAuth 가 켜진 local 모드에만 있고 다른 때는 404 다 ---------------------

export async function listMyOAuthGrants(): Promise<OAuthGrant[]> {
  return unwrap(await api.GET("/me/oauth-grants"));
}

/** 끊는다. 그 연결의 갱신은 그 자리에서 막히고 이미 나간 접근 토큰은 15분 안에 만료한다. */
export async function revokeMyOAuthGrant(grantId: string): Promise<void> {
  unwrap(await api.DELETE("/me/oauth-grants/{grant_id}", { params: { path: { grant_id: grantId } } }));
}

/** 관리자(users:write)가 그 사람의 연결을 본다. */
export async function listUserOAuthGrants(userId: string): Promise<OAuthGrant[]> {
  return unwrap(await api.GET("/users/{user_id}/oauth-grants", { params: { path: { user_id: userId } } }));
}

export async function revokeUserOAuthGrant(userId: string, grantId: string): Promise<void> {
  unwrap(
    await api.DELETE("/users/{user_id}/oauth-grants/{grant_id}", { params: { path: { user_id: userId, grant_id: grantId } } }),
  );
}

export async function getTenantSettings(tenant: string): Promise<TenantSettings> {
  return unwrap(await api.GET("/tenants/{tenant}/settings", { params: { path: { tenant } } }));
}

export async function patchTenantSettings(tenant: string, patch: TenantSettingsPatch): Promise<TenantSettings> {
  return unwrap(
    await api.PATCH("/tenants/{tenant}/settings", { params: { path: { tenant } }, body: patch }),
  );
}

export async function listMembers(tenant: string): Promise<Membership[]> {
  return unwrap(await api.GET("/tenants/{tenant}/members", { params: { path: { tenant } } }));
}

/** manual 소속 전체 교체. okta_group 소속은 서버가 그룹에서 동기화하므로 보내지 않는다. */
export async function putMembers(tenant: string, items: { sub: string; role: string }[]): Promise<Membership[]> {
  return unwrap(
    await api.PUT("/tenants/{tenant}/members", { params: { path: { tenant } }, body: items }),
  );
}

export async function listAudit(filters: AuditFilters): Promise<AuditEvent[]> {
  return unwrap(await api.GET("/audit", { params: { query: cleanQuery({ ...filters }) } }));
}

export async function listMcpAudit(filters: MCPAuditFilters): Promise<MCPAuditEvent[]> {
  return unwrap(await api.GET("/audit/mcp", { params: { query: cleanQuery({ ...filters }) } }));
}

// -- 사용량과 가격표 (P37) -------------------------------------------------------------------

export async function getUsage(filters: TenantUsageFilters): Promise<UsageReport> {
  return unwrap(await api.GET("/usage", { params: { query: cleanQuery({ ...filters }) } }));
}

export async function getPlatformUsage(filters: UsageFilters): Promise<AdminUsage> {
  return unwrap(await api.GET("/admin/usage", { params: { query: cleanQuery({ ...filters }) } }));
}

export async function getPricing(): Promise<Pricing> {
  return unwrap(await api.GET("/settings/pricing"));
}

/** 가격표 전체를 바꾼다. 읽은 판이 아니면 409 revision_conflict 다. */
export async function putPricing(expectedVersion: number, rules: PriceRule[]): Promise<Pricing> {
  return unwrap(await api.PUT("/settings/pricing", { body: { expected_version: expectedVersion, rules } }));
}

/** OTLP 내보내기 설정. Platform Settings 와 따로인 문서이고 저장한 적이 없으면 서버의 기본값(꺼짐)이다. */
export async function getOtlpSettings(): Promise<OtlpSettings> {
  return unwrap(await api.GET("/settings/observability/otlp"));
}

/** OTLP 내보내기 설정을 통째로 바꾼다. 비서의 설정 도구로는 바꾸지 못하는 값이다. */
export async function putOtlpSettings(otlp: OtlpSettings): Promise<OtlpSettings> {
  return unwrap(await api.PUT("/settings/observability/otlp", { body: otlp }));
}

export async function getOtlpStatus(): Promise<OtlpStatus> {
  return unwrap(await api.GET("/settings/observability/otlp/status"));
}

/** 허브 이미지 카드의 계산한 값 (P55 6절). 모든 테넌트의 항목을 실으므로 platform-admin 만 부른다. */
export async function getHubImagesStatus(): Promise<HubImagesStatus> {
  return unwrap(await api.GET("/settings/hub-images/status"));
}

/** 허브 레지스트리의 읽기 자격 증명을 관리 Secret 에 넣는다 (P55 6절, D56). 값은 응답에 없다. */
export async function putHubImagesCredential(username: string, password: string): Promise<void> {
  unwrap(await api.PUT("/settings/hub-images/credential", { body: { username, password } }));
}

/** LLM 게이트웨이의 상태 (P38 11절). 테넌트를 가로질러 세므로 platform-admin 만 부른다. */
export async function getLlmGateway(): Promise<LlmGatewayStatus> {
  return unwrap(await api.GET("/settings/llm-gateway"));
}

/**
 * Agent 의 LLM 게이트웨이 토큰을 새로 내고 파드를 굴린다 (P38 5절). 옛 토큰은 옛 파드가 다 내려간 뒤에 회수하고,
 * immediate 면 곧바로 회수한다. 게이트웨이 경로가 아닌 Agent 는 409 llm_token_missing 이다.
 */
export async function rotateLlmToken(agentId: string, immediate: boolean): Promise<{ agent_id: string; immediate: boolean }> {
  return unwrap(await api.POST("/agents/{agent_id}/llm-token/rotate", { params: { path: { agent_id: agentId } }, body: { immediate } }));
}

/** 기간의 비용을 다시 계산한다. until 은 들지 않는다. 끝나지 않은 다시 계산이 있으면 409 reprice_running 이다. */
export async function startReprice(since: string, until: string): Promise<RepriceStatus> {
  return unwrap(await api.POST("/usage/reprice", { body: { since, until } }));
}

export async function getReprice(): Promise<RepriceStatus> {
  return unwrap(await api.GET("/usage/reprice"));
}

// -- mcp hub ---------------------------------------------------------------------------

export type HubCreate = Schemas["HubCreate"];

export async function listHubItems(): Promise<HubItem[]> {
  return unwrap(await api.GET("/hub"));
}

export async function getHubItem(id: string): Promise<HubItem> {
  return unwrap(await api.GET("/hub/{item_id}", { params: { path: { item_id: id } } }));
}

export async function createHubItem(body: HubCreate): Promise<HubItem> {
  return unwrap(await api.POST("/hub", { body }));
}

/** 카탈로그 스냅숏 (P55 4절). 허브 이미지 설정 전에는 `admission` 이 false 이고 `mirrored_image` 가 null 이다. */
export async function getHubCatalog(): Promise<HubCatalog> {
  return unwrap(await api.GET("/hub/catalog"));
}

export async function deleteHubItem(id: string): Promise<void> {
  unwrap(await api.DELETE("/hub/{item_id}", { params: { path: { item_id: id } } }));
}

/**
 * header 항목의 헤더 값을 바꾼다 (P86 6.1절). 만든 사람과 tenant-admin 만 되고(403 `forbidden`), 값을 실어 불러 본 답이
 * `auth.check` 로 함께 온다. 값은 응답에 없다.
 */
export async function setHubHeaderValue(id: string, value: string): Promise<HubItem> {
  return unwrap(await api.PUT("/hub/{item_id}/auth/header", { params: { path: { item_id: id } }, body: { value } }));
}

/**
 * image 항목의 Secret env 값을 바꾼다 (P55 5절). 보낸 이름만 바뀌고, 만든 사람과 tenant-admin 만 된다(`permissions.replace_secret_env`).
 * 값은 응답에 없다.
 */
export async function replaceHubSecretEnv(id: string, values: Record<string, string>): Promise<HubItem> {
  return unwrap(await api.PUT("/hub/{item_id}/secret-env", { params: { path: { item_id: id } }, body: { values } }));
}

export async function refreshHubTools(id: string): Promise<HubItem> {
  return unwrap(
    await api.POST("/hub/{item_id}/refresh-tools", { params: { path: { item_id: id } } }),
  );
}

/** 도구 탭 (D48). 서버에 못 물어봐도 200 이고 `listed` 가 false 다. */
export async function getHubTools(id: string): Promise<HubTools> {
  return unwrap(
    await api.GET("/hub/{item_id}/tools", { params: { path: { item_id: id } } }),
  );
}

/**
 * image 항목의 Tool 규칙을 통째로 저장한다 (P52 1절). manage 가 없으면 403, 역할 밖의 대상은 422 `invalid_value`, 항목이 내놓지
 * 않는 Tool 은 422 `rule_names_unknown_tool`, 0.53 아래 클러스터는 422 `rules_toolhive_old` 다.
 */
export async function setHubToolRules(id: string, rules: Record<string, ItemToolRule>): Promise<HubToolRulesSaved> {
  return unwrap(await api.PUT("/hub/{item_id}/tool-rules", { params: { path: { item_id: id } }, body: { rules } }));
}

export async function getHubManifest(id: string): Promise<{ documents: HubManifestDoc[] }> {
  return unwrap(
    await api.GET("/hub/{item_id}/manifest", { params: { path: { item_id: id } } }),
  );
}

/**
 * 접근 정책 저장 (docs/guide/hub-gateway.md 7절, D23). manage 가 없으면 403 `forbidden`(`action: manage`),
 * 경계를 넓히는데 platform-admin 이 아니면 403 `forbidden`(`scope: hub:share`, `actions[]`), internal·published 에
 * 다른 테넌트를 넣으면 422 `cross_tenant_unsupported`(`action`, `kind`), 모르는 role 은 422
 * `invalid_role`(`action`, `roles`).
 */
export async function setHubAccess(id: string, access: HubAccessInput): Promise<HubItem> {
  return unwrap(
    await api.PUT("/hub/{item_id}/access", { params: { path: { item_id: id } }, body: access }),
  );
}

// -- gateways (P8 F2) --------------------------------------------------------------------

export type GatewayBody = Schemas["GatewayBody"];

export async function listGateways(): Promise<Gateway[]> {
  return unwrap(await api.GET("/gateways"));
}

export async function getGateway(id: string): Promise<Gateway> {
  return unwrap(
    await api.GET("/gateways/{gateway_id}", { params: { path: { gateway_id: id } } }),
  );
}

export async function createGateway(body: GatewayBody): Promise<Gateway> {
  return unwrap(await api.POST("/gateways", { body }));
}

/** PUT 은 전체 교체이고 새 리비전을 만든다. 답에는 검증 경고와 규칙 저장의 알림이 붙는다 (P52 1절). */
export async function putGateway(id: string, body: GatewayBody): Promise<GatewaySaved> {
  return unwrap(
    await api.PUT("/gateways/{gateway_id}", { params: { path: { gateway_id: id } }, body }),
  );
}

export async function deleteGateway(id: string): Promise<{ deleted: boolean }> {
  return unwrap(await api.DELETE("/gateways/{gateway_id}", { params: { path: { gateway_id: id } } }));
}

export async function listGatewayRevisions(id: string): Promise<GatewayRevision[]> {
  return unwrap(
    await api.GET("/gateways/{gateway_id}/revisions", { params: { path: { gateway_id: id } } }),
  );
}

/** 인스턴스를 만들면 생길 문서 묶음(MCPGroup, Entry 들, VirtualMCPServer)을 apply 없이 렌더링한다. */
export async function previewGatewayRevision(
  id: string,
  n: number,
  clusterId?: string | null,
): Promise<GatewayPreview> {
  return unwrap(
    await api.GET("/gateways/{gateway_id}/revisions/{n}/preview", {
      params: { path: { gateway_id: id, n }, query: { cluster_id: clusterId || null } },
    }),
  );
}

// -- gateways: 배포 쪽 동작 (D42) ----------------------------------------------------------

/** 게시 방식은 entry 하나라 부르는 쪽이 고르지 않는다. publishGateway 가 싣는다. */
export type PublishBody = Omit<Schemas["daegam__api__gateways__PublishBody"], "mode">;

/** 배포. `draft` 문서의 vMCP 를 올린다. 백엔드 허브 항목이 준비돼 있어야 한다(409 `backends_not_ready`). */
export async function deployGateway(id: string, body: GatewayDeployBody = {}): Promise<Gateway> {
  return unwrap(
    await api.POST("/gateways/{gateway_id}/deploy", { params: { path: { gateway_id: id } }, body }),
  );
}

/** 배포 해제. vMCP·Entry·그룹을 지우고 정의는 남긴다. 공유 복제본은 그대로 둔다 (D42). */
export async function undeployGateway(id: string): Promise<Gateway> {
  return unwrap(
    await api.POST("/gateways/{gateway_id}/undeploy", { params: { path: { gateway_id: id } } }),
  );
}

/** 복제. 정의를 베낀 `draft` 를 만든다. */
export async function cloneGateway(id: string, body: GatewayCloneBody): Promise<Gateway> {
  return unwrap(
    await api.POST("/gateways/{gateway_id}/clone", { params: { path: { gateway_id: id } }, body }),
  );
}

/** Basic 편집 (docs/design/resource-model.md 13절): 노출 설정 수정. 서버가 Ingress·앞단을 다시 렌더링하고 재배포한다. */
export async function patchGateway(id: string, body: GatewayBasicPatch): Promise<Gateway> {
  return unwrap(
    await api.PATCH("/gateways/{gateway_id}", { params: { path: { gateway_id: id } }, body }),
  );
}

/** 프로필 최신 리비전으로 핀을 옮기고 스냅샷을 다시 만든다. 응답에 검증 경고가 붙는다. */
export async function redeployGateway(id: string): Promise<Gateway> {
  return unwrap(
    await api.POST("/gateways/{gateway_id}/redeploy", { params: { path: { gateway_id: id } } }),
  );
}

export async function getGatewayManifest(id: string): Promise<ManifestResponse> {
  return unwrap(
    await api.GET("/gateways/{gateway_id}/manifest", { params: { path: { gateway_id: id } } }),
  );
}

/** 메트릭 (P51 4절). MCP Server 의 것에 백엔드별 줄이 붙는다. */
export async function getGatewayMetrics(id: string, window: McpMetricsWindow, tz: string): Promise<GatewayMetrics> {
  return unwrap(await api.GET("/gateways/{gateway_id}/metrics", { params: { path: { gateway_id: id }, query: { window, tz } } }));
}

/** Advanced 저장: overrides 는 VirtualMCPServer 문서에만 얹는다. */
export async function putGatewayOverridesYaml(id: string, yaml: string): Promise<OverridesResponse> {
  return unwrap(
    await api.PUT("/gateways/{gateway_id}/overrides", {
      params: { path: { gateway_id: id } },
      body: { yaml },
    }),
  );
}

export async function gatewayDriftAction(id: string, action: "reset" | "absorb"): Promise<ManifestResponse> {
  return unwrap(
    await api.POST("/gateways/{gateway_id}/drift/{action}", {
      params: { path: { gateway_id: id, action } },
    }),
  );
}

/** 허브 게시 (docs/guide/hub-gateway.md 6절). 결과는 kind published 허브 항목이고, 해제는 DELETE /hub/{id} 다. */
export async function publishGateway(id: string, body: PublishBody): Promise<HubItem> {
  return unwrap(
    await api.POST("/gateways/{gateway_id}/publish", {
      params: { path: { gateway_id: id } },
      body: { ...body, mode: "entry" },
    }),
  );
}

// -- agents (D26, resource-model 5절) ---------------------------------------------------------

export async function listAgentTemplates(): Promise<AgentTemplate[]> {
  return unwrap(await api.GET("/agents/templates"));
}

/** 저장 없이 그래프를 검사한다. 저장·Deploy 도 같은 규칙이라 캔버스의 검증 배지가 이 결과를 보인다. 고치는 중인
 *  Agent 의 id 를 주면 그 Agent 의 런타임 이미지가 아는 기능으로 검사한다(P43 8절). */
export async function validateAgentGraph(body: AgentGraphBody, agentId?: string): Promise<AgentValidateResponse> {
  // 그래프 본문은 D28 의 model 노드·간선을 담는 얇은 타입이라 생성 타입으로 되돌려 보낸다 (types.ts 참고)
  const params = agentId ? { query: { agent_id: agentId } } : undefined;
  return unwrap(await api.POST("/agents/validate", { body: body, params }));
}

export type AgentPublishBody = Schemas["AgentPublishBody"];

/**
 * Agent 를 허브에 게시한다 (P46 6.1절). kind agent 항목이 생기고 이미 게시돼 있으면 200 으로 그 항목이다. 턴을 받지 않으면 409
 * `not_deployed`, memory 노드나 세션 예산이 없으면 409 `agent_source_runs_unmet`, Agent 호출이 꺼졌으면 409 `agent_calls_disabled`,
 * 이름 충돌은 409 `name_conflict` 다.
 */
export async function publishAgent(id: string, body: AgentPublishBody): Promise<HubItem> {
  return unwrap(await api.POST("/agents/{agent_id}/publish", { params: { path: { agent_id: id } }, body }));
}

/** 게시 해제. DELETE /hub/{item} 과 같고 이 항목을 Tool 로 쓰는 Agent 가 있어도 해제된다. manage 가 없으면 403 이다. */
export async function unpublishAgent(id: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/publish", { params: { path: { agent_id: id } } }));
}

export async function listAgents(): Promise<Agent[]> {
  return unwrap(await api.GET("/agents"));
}

export async function getAgent(id: string): Promise<Agent> {
  return unwrap(
    await api.GET("/agents/{agent_id}", { params: { path: { agent_id: id } } }),
  );
}

/** 422 invalid_graph 면 ApiError.extra("issues") 에 이슈 목록이 있다. */
export async function createAgent(body: AgentBody): Promise<AgentSaved> {
  return unwrap(await api.POST("/agents", { body: body }));
}

/**
 * PUT 은 전체 교체. graph·service 가 바뀌면 새 리비전이고 layout 만 바뀌면 리비전 없이 저장된다(revision_created).
 * `expect_revision` 을 주면 그 리비전이 최신일 때만 저장되고 아니면 409 revision_conflict 다(P48 6.7절).
 */
export async function putAgent(id: string, body: AgentUpdateBody): Promise<AgentSaved> {
  return unwrap(
    await api.PUT("/agents/{agent_id}", { params: { path: { agent_id: id } }, body: body }),
  );
}

export async function deleteAgent(id: string): Promise<{ deleted: boolean }> {
  return unwrap(await api.DELETE("/agents/{agent_id}", { params: { path: { agent_id: id } } }));
}

export async function getAgentRevision(id: string, n: number): Promise<AgentRevision> {
  return unwrap(
    await api.GET("/agents/{agent_id}/revisions/{n}", { params: { path: { agent_id: id, n } } }),
  );
}

/** 배포 (docs/guide/agents.md 2절). 응답의 warnings 는 그래프 경고와 클러스터 preflight 경고다. */
export async function deployAgent(id: string, body: AgentDeployBody): Promise<Agent> {
  return unwrap(
    await api.POST("/agents/{agent_id}/deploy", { params: { path: { agent_id: id } }, body }),
  );
}

// -- agents: 배포 쪽 동작 (D42) -------------------------------------------------------------

/**
 * 상태 왕복 없는 가벼운 목록 (D35 F2). 홈 챗의 고르개가 쓴다. 배포된 것만 온다. `phase` 는 상태
 * 조회가 마지막으로 본 값이라 지금 사실이 아닐 수 있고, 고른 뒤에 그 하나만 `getAgent` 로 다시 읽는다.
 */
export async function listAgentSummaries(): Promise<AgentSummary[]> {
  return unwrap(await api.GET("/agents/summary"));
}

/** 배포 해제. 파드·Deployment 를 내리고 정의는 남긴다. 장기 기억은 남는다 (같은 id 로 다시 배포된다). */
export async function undeployAgent(id: string): Promise<Agent> {
  return unwrap(
    await api.POST("/agents/{agent_id}/undeploy", { params: { path: { agent_id: id } } }),
  );
}

/** 복제. 정의(그래프·layout)를 베낀 `draft` 를 만든다. 둘째를 띄우려면 이 길뿐이다 (D42). */
export async function cloneAgent(id: string, body: AgentCloneBody): Promise<Agent> {
  return unwrap(
    await api.POST("/agents/{agent_id}/clone", { params: { path: { agent_id: id } }, body }),
  );
}

/**
 * 프로필 최신 리비전으로 핀을 옮기고 provider Secret 을 다시 복사한다. 보류 세션이 있으면 status.warnings 에 경고가 붙는다.
 * 본문 `exposure` 를 주면 노출 설정을 바꿔 재배포하고, 없으면 저장된 노출 스냅샷을 유지한다 (D27).
 */
export async function redeployAgent(id: string, body: AgentRedeployBody = {}): Promise<Agent> {
  return unwrap(
    await api.POST("/agents/{agent_id}/redeploy", { params: { path: { agent_id: id } }, body }),
  );
}

/**
 * 쓰지 않는 Agent 를 지금 0 으로 줄인다 (P44 7절, agent:deploy). 쉬는 창과 상관없다. 턴이 리스를 쥐고 있으면 409 agent_busy,
 * 잠들 수 없으면 409 agent_sleep_ineligible, 남이 문서를 쥐고 있으면 409 in_progress 다(0 으로 줄이기는 워커가 잇는다).
 */
export async function sleepAgent(id: string): Promise<Agent> {
  return unwrap(await api.POST("/agents/{agent_id}/sleep", { params: { path: { agent_id: id } } }));
}

/** 잠든 Agent 의 파드를 다시 띄운다 (P44 8절, agent:chat). 파드가 뜨기를 기다리지 않고 상태로 답한다. 깨어 있으면 활동만 적힌다. */
export async function wakeAgent(id: string): Promise<Agent> {
  return unwrap(await api.POST("/agents/{agent_id}/wake", { params: { path: { agent_id: id } } }));
}

/*
 * 채팅과 결정은 `text/event-stream` 이라 생성 클라이언트로 부르지 않는다. openSse 가 Response 를 그대로
 * 돌려주고 lib/chatStream.ts 가 읽는다. 그 밖의 라우트는 다른 곳과 같이 api.GET/POST/PUT 이다.
 */

/**
 * 채팅 한 턴. `text/event-stream` 응답의 Response 를 돌려주고 lib/chatStream.readChatStream 이 읽는다(중단도 거기서
 * reader 를 취소한다). 오류(413 max_input_chars, 429 max_turns·max_concurrent_sessions·예산, 409 session_pending·
 * turn_in_progress 등)는 ApiError 로 던진다.
 */
export async function openAgentChat(chat: ChatBase, body: AgentChatBody): Promise<Response> {
  return openSse(chatPath(chat, "/chat"), body);
}

/** 보류 승인을 결정해 턴을 재개한다. 응답은 /chat 과 같은 SSE 다. 결정 개수는 action_requests 개수와 같아야 한다(422). */
export async function openAgentDecisions(chat: ChatBase, sessionId: string, body: AgentDecisionsBody): Promise<Response> {
  return openSse(chatSessionPath(chat, sessionId, "/decisions"), body);
}

/**
 * 진행 중 턴을 멈춘다 (D65, P78 2절). 202 면 열려 있던 스트림이 `error{code: turn_cancelled}` 로 끝나고 세션 읽기도 그 마감을
 * 보인다. 진행 중 턴이 없으면 409 no_turn_in_progress, 배포된 이미지가 `cancel` 을 알리지 않았으면 런타임을 부르지 않는 409
 * runtime_feature_missing 이다. `turnId` 를 주면 그 시도만 멈춰서, 낡은 화면의 중단이 다음 턴을 멈추지 않는다. 부르는 쪽은
 * `status.runtime.features` 에 `cancel` 이 있거나 응답이 연결과 떨어져 도는 턴이라고 알렸을 때만 부른다(lib/runtimeFeatures.ts).
 */
export async function cancelAgentTurn(chat: ChatBase, sessionId: string, turnId?: string | null): Promise<void> {
  unwrap(
    await api.POST(chatRoute(chat, "/agents/{agent_id}/sessions/{session_id}/cancel"), {
      params: { path: { agent_id: chat.agentId, session_id: sessionId } },
      body: { turn_id: turnId ?? null },
    }),
  );
}

/**
 * 세션 색인 목록. 줄 모양은 생성된 스키마(`AgentSessionRow`)다. `q` 는 제목 검색어이고 런타임이 `session_admin` 을 알릴 때만
 * 준다. 없는 기록에 주면 409 runtime_feature_missing 이다(P36).
 */
export async function listAgentSessions(
  id: string,
  status?: AgentSessionStatus,
  q?: string,
  runs?: { runs: "include" | "only"; allUsers?: boolean },
): Promise<AgentSession[]> {
  // runs 는 자동 실행의 세션을 넣거나 그것만 본다(P41 7절). 승인자의 받은 결정함이 all_users 와 함께 쓴다
  const query = { status, q: q || undefined, runs: runs?.runs, all_users: runs?.allUsers || undefined };
  const body = unwrap(await api.GET("/agents/{agent_id}/sessions", { params: { path: { agent_id: id }, query } }));
  return body.sessions;
}

/** 세션 상세. 봉투 `{session, messages, pending}` 도 스키마가 지킨다. */
export async function getAgentSession(chat: ChatBase, sessionId: string): Promise<AgentSessionDetail> {
  const body = unwrap(
    await api.GET(chatRoute(chat, "/agents/{agent_id}/sessions/{session_id}"), { params: { path: { agent_id: chat.agentId, session_id: sessionId } } }),
  );
  return {
    session: body.session,
    messages: sessionMessages(body.messages),
    pending: body.pending,
    transcript: agentTranscript(body.transcript),
  };
}

/**
 * 도는 턴을 따라가는 가벼운 읽기 (P78 2절). 체크포인트와 메시지 요약 없이 색인 줄과 지금까지 저장된 전사만 오고, 플랫폼이 다시
 * 읽을 간격을 싣는다(Agent 의 Service 로 바로 닿으면 3초, API 서버를 거치면 10초). `pending` 은 턴이 돌지 않을 때만 참이라
 * 화면은 턴이 끝난 뒤의 전체 읽기에서 승인 카드를 세운다.
 */
export async function getAgentSessionProgress(chat: ChatBase, sessionId: string): Promise<AgentSessionProgress> {
  const body = unwrap(
    await api.GET(chatRoute(chat, "/agents/{agent_id}/sessions/{session_id}"), {
      params: { path: { agent_id: chat.agentId, session_id: sessionId }, query: { view: "progress" } },
    }),
  );
  // 스키마는 전체 읽기와 같은 모양이라 선택이지만, 플랫폼은 view=progress 의 200 응답에 간격을 늘 싣는다
  return { session: body.session, transcript: agentTranscript(body.transcript), pollSeconds: body.poll_seconds as number };
}

/** 보류 승인 상세. 보류가 없으면 404 not_pending 이다. */
export async function getAgentPending(chat: ChatBase, sessionId: string): Promise<AgentPending> {
  // 런타임이 쓴 모양이라 서버 스키마에서는 자유 객체다. SSE `interrupt` 와 같은 접기로 좁힌다 (P73 2절)
  const path = chatRoute(chat, "/agents/{agent_id}/sessions/{session_id}/pending");
  return normalizePending(unwrap(await api.GET(path, { params: { path: { agent_id: chat.agentId, session_id: sessionId } } })));
}

/** 지표. tz 는 날마다 비용을 자를 IANA 시간대다. */
export async function getAgentMetrics(id: string, window: MetricsWindow, tz: string): Promise<AgentMetrics> {
  return unwrap(await api.GET("/agents/{agent_id}/metrics", { params: { path: { agent_id: id }, query: { window, tz } } }));
}

/** 이벤트 목록의 거르기. 빈 값은 싣지 않고 한 번에 100건이 기본이다. */
export async function listAgentEvents(id: string, filter: AgentEventFilter = {}): Promise<AgentEvent[]> {
  return unwrap(await api.GET("/agents/{agent_id}/events", { params: { path: { agent_id: id }, query: cleanQuery({ limit: 100, ...filter }) } }));
}

/** 세션의 턴 시도(최신부터). 소유자나 agent:approve 만 본다. */
export async function listAgentTurns(id: string, sessionId: string): Promise<AgentTurn[]> {
  const body = unwrap(await api.GET("/agents/{agent_id}/sessions/{session_id}/turns", { params: { path: { agent_id: id, session_id: sessionId } } }));
  return body.turns;
}

export async function getAgentTrace(id: string, traceId: string): Promise<AgentTrace> {
  return unwrap(await api.GET("/agents/{agent_id}/traces/{trace_id}", { params: { path: { agent_id: id, trace_id: traceId } } }));
}

/** Advanced 탭: Deployment·Service·NetworkPolicy 문서 묶음. MCP Server 의 manifest 와 같은 계약이다. */
export async function getAgentManifest(id: string): Promise<ManifestResponse> {
  return unwrap(await api.GET("/agents/{agent_id}/manifest", { params: { path: { agent_id: id } } }));
}

export async function putAgentOverridesYaml(id: string, yaml: string): Promise<OverridesResponse> {
  return unwrap(
    await api.PUT("/agents/{agent_id}/overrides", { params: { path: { agent_id: id } }, body: { yaml } }),
  );
}

export async function agentDriftAction(id: string, action: "reset" | "absorb"): Promise<ManifestResponse> {
  return unwrap(
    await api.POST("/agents/{agent_id}/drift/{action}", { params: { path: { agent_id: id, action } } }),
  );
}

// -- 플랫폼 비서 (D31, phase-14) -------------------------------------------------------------------------

/*
 * 턴과 결정은 `text/event-stream` 이라 openSse 로 부르고 Response 를 그대로 돌려준다(Agent 세션 계열과 같다).
 * 오류(409 turn_in_progress·tenant_mismatch·not_pending, 422 decision_count_mismatch,
 * 404 session_not_found·assistant_disabled)는 ApiError 다.
 */

function assistantPath(rest: string): string {
  return `/assistant${rest}`;
}

/** `{enabled, provider, confirm_deploy, limits}`. 오류면 부르는 쪽이 disabled 로 본다. */
export async function getAssistantConfig(): Promise<AssistantConfig> {
  return unwrap(await api.GET("/assistant/config"));
}

/** 내 세션 목록. 응답은 `{items: [...]}` 다. */
export async function listAssistantSessions(): Promise<AssistantSessionSummary[]> {
  return unwrap(await api.GET("/assistant/sessions")).items;
}

export async function createAssistantSession(title?: string | null): Promise<{ id: string }> {
  return unwrap(await api.POST("/assistant/sessions", { body: title ? { title } : {} }));
}

/** 복원용 상세: `{id, title, state, pending, transcript, turns, usage, updated_at}`. */
export async function getAssistantSession(id: string): Promise<AssistantSessionDetail> {
  return unwrap(await api.GET("/assistant/sessions/{session_id}", { params: { path: { session_id: id } } }));
}

export async function deleteAssistantSession(id: string): Promise<void> {
  unwrap(await api.DELETE("/assistant/sessions/{session_id}", { params: { path: { session_id: id } } }));
}

/** 한 턴. `{message, context?}` → SSE. */
export async function openAssistantTurn(id: string, body: AssistantTurnBody): Promise<Response> {
  return openSse(assistantPath(`/sessions/${encodeURIComponent(id)}/turns`), body);
}

/** 보류 승인 결정. `{decisions: [{id, type, message?}]}` → SSE(재개). 개수는 pending.calls 와 같아야 한다(422). */
export async function openAssistantDecisions(id: string, body: AssistantDecisionsBody): Promise<Response> {
  return openSse(assistantPath(`/sessions/${encodeURIComponent(id)}/decisions`), body);
}

/**
 * 기다리는 Agent 정의 제안을 저장해 둔 기준 리비전 위에 짜 맞춘 것 (P48 6.1절). 기다리는 제안이 없으면 404 proposal_not_found,
 * 대상 Agent 를 읽지 못하면 그 404·403, 기준 위에 더는 짜 맞춰지지 않으면 409 proposal_changed 다.
 */
export async function getAssistantProposal(id: string): Promise<AssistantProposalView> {
  const raw = unwrap(await api.GET("/assistant/sessions/{session_id}/proposal", { params: { path: { session_id: id } } }));
  const proposal = normalizeProposal(raw.proposal);
  // 플랫폼은 늘 덩어리를 싣는다. 없으면 계약이 어긋난 개발자의 잘못이라 영어 그대로 던진다
  if (!proposal) throw new Error("The proposal route answered without a proposal block.");
  return { ...raw, proposal };
}

// -- 자동 실행 (P41). 실행을 켜기 전에는 모든 라우트가 404 다 -----------------------------------------------------

/** 실행 기록 하나. 목록의 한 줄과 같은 모양이다. */
export async function getAgentRun(id: string, runId: string): Promise<AgentRun> {
  return unwrap(await api.GET("/agents/{agent_id}/runs/{run_id}", { params: { path: { agent_id: id, run_id: runId } } }));
}

/** 실행 기록. 마지막으로 움직인 것부터이고, 건너뛴 울림은 소스와 까닭마다 `skipped` 줄 하나다. */
export async function listAgentRuns(id: string): Promise<AgentRun[]> {
  return unwrap(await api.GET("/agents/{agent_id}/runs", { params: { path: { agent_id: id } } })).runs;
}

/** 지금 실행. 부른 사람으로 도는 기록을 `starting` 으로 넣고 바로 답한다. 상한이면 429 agent_run_limit 이다. */
export async function runAgentNow(id: string, message: string): Promise<AgentRun> {
  return unwrap(await api.POST("/agents/{agent_id}/runs", { params: { path: { agent_id: id } }, body: { message } }));
}

/** 실행을 멈춘다. 묶인 사람과 agent:approve 만이고, 이미 끝난 실행은 그 기록을 그대로 답한다. */
export async function cancelAgentRun(id: string, runId: string): Promise<AgentRun> {
  return unwrap(await api.POST("/agents/{agent_id}/runs/{run_id}/cancel", { params: { path: { agent_id: id, run_id: runId } } }));
}

export async function listRunSchedules(id: string): Promise<RunSchedule[]> {
  return unwrap(await api.GET("/agents/{agent_id}/schedules", { params: { path: { agent_id: id } } })).schedules;
}

/** 일정을 만든다. 만든 사람이 묶인 사람이 된다. */
export async function createRunSchedule(id: string, body: RunScheduleBody): Promise<RunSchedule> {
  return unwrap(await api.POST("/agents/{agent_id}/schedules", { params: { path: { agent_id: id } }, body }));
}

/** 보낸 칸만 바꾸고 바꾼 사람에게 다시 묶는다. */
export async function updateRunSchedule(id: string, scheduleId: string, body: RunSchedulePatch): Promise<RunSchedule> {
  return unwrap(
    await api.PATCH("/agents/{agent_id}/schedules/{schedule_id}", { params: { path: { agent_id: id, schedule_id: scheduleId } }, body }),
  );
}

export async function deleteRunSchedule(id: string, scheduleId: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/schedules/{schedule_id}", { params: { path: { agent_id: id, schedule_id: scheduleId } } }));
}

/** 멈춘다(`pause`) 또는 다시 켠다(`resume`). 다시 켜면 다시 켠 사람에게 묶인다. */
export async function setRunSchedulePaused(id: string, scheduleId: string, paused: boolean): Promise<RunSchedule> {
  const params = { params: { path: { agent_id: id, schedule_id: scheduleId } } };
  return unwrap(
    paused
      ? await api.POST("/agents/{agent_id}/schedules/{schedule_id}/pause", params)
      : await api.POST("/agents/{agent_id}/schedules/{schedule_id}/resume", params),
  );
}

export async function listRunTriggers(id: string): Promise<RunTrigger[]> {
  return unwrap(await api.GET("/agents/{agent_id}/triggers", { params: { path: { agent_id: id } } })).triggers;
}

/** 트리거를 만든다. 서명 비밀은 이 답에만 있다. */
export async function createRunTrigger(id: string, body: RunTriggerBody): Promise<RunTriggerWithSecret> {
  return unwrap(await api.POST("/agents/{agent_id}/triggers", { params: { path: { agent_id: id } }, body }));
}

/** 보낸 칸만 바꾼다. 이름만 바꾸면 묶음이 그대로이고 그 밖의 변경은 바꾼 사람에게 다시 묶는다. */
export async function updateRunTrigger(id: string, triggerId: string, body: RunTriggerPatch): Promise<RunTrigger> {
  return unwrap(
    await api.PATCH("/agents/{agent_id}/triggers/{trigger_id}", { params: { path: { agent_id: id, trigger_id: triggerId } }, body }),
  );
}

export async function deleteRunTrigger(id: string, triggerId: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/triggers/{trigger_id}", { params: { path: { agent_id: id, trigger_id: triggerId } } }));
}

/** 서명 비밀을 새로 만든다. 새 비밀은 이 답에만 있고 앞 비밀은 24시간 더 받는다. */
export async function rotateRunTriggerSecret(id: string, triggerId: string): Promise<RunTriggerWithSecret> {
  return unwrap(
    await api.POST("/agents/{agent_id}/triggers/{trigger_id}/rotate-secret", { params: { path: { agent_id: id, trigger_id: triggerId } } }),
  );
}

export async function setRunTriggerPaused(id: string, triggerId: string, paused: boolean): Promise<RunTrigger> {
  const params = { params: { path: { agent_id: id, trigger_id: triggerId } } };
  return unwrap(
    paused
      ? await api.POST("/agents/{agent_id}/triggers/{trigger_id}/pause", params)
      : await api.POST("/agents/{agent_id}/triggers/{trigger_id}/resume", params),
  );
}

/** 나에게 묶인 일정과 트리거 (P41 6절). 실행이 꺼져 있으면 404 다. */
export async function listRunsAsMe(): Promise<RunsAsMe> {
  return unwrap(await api.GET("/me/runs-as-me"));
}

/** 나에게 묶인 일정을 멈춘다. 그사이 다른 사람에게 묶였으면 404 다. */
export async function pauseMySchedule(scheduleId: string): Promise<BoundSchedule> {
  return unwrap(await api.POST("/me/runs-as-me/schedules/{schedule_id}/pause", { params: { path: { schedule_id: scheduleId } } }));
}

export async function pauseMyTrigger(triggerId: string): Promise<BoundTrigger> {
  return unwrap(await api.POST("/me/runs-as-me/triggers/{trigger_id}/pause", { params: { path: { trigger_id: triggerId } } }));
}
