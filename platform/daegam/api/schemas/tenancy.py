"""settings·tenants·members·auth 응답. 대부분 store 모델 그대로다(기본값 필드도 required 로 문서화).
요청 본문과 공유하는 중첩 모델(OidcSettings, LLMProvider, Binding 등)은 건드리지 않는다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.k8s.gates import Feature
from daegam.services.exposure_front import Step
from daegam.store.auth_reapply import AuthSummary, ClusterAuthState, TurnGateHolder
from daegam.store.base import UtcDatetime
from daegam.store.memberships import GroupMapping, Membership
from daegam.store.observability import OtlpSettings
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import TenantSettings


class PlatformSettingsOut(PlatformSettings):
    model_config = REQUIRED_DEFAULTS


LLMProviderCheckCode = Literal[
    "ok", "no_secret", "no_api_key", "auth", "connection", "quota_exhausted", "provider_error"
]


class OtlpSettingsOut(OtlpSettings):
    """OTLP 내보내기 설정 (P37 15절). Platform Settings 와 따로인 문서 `observability` 의 몫이다."""

    model_config = REQUIRED_DEFAULTS


class OtlpStatusOut(BaseModel):
    """OTLP 내보내기의 상태 (P37 15절). 마지막 성공 시각, 실패한 요청과 버린 실행의 누적, 지금
    이어진 실패, 대기열의 실행 수다."""

    model_config = REQUIRED_DEFAULTS

    last_success: UtcDatetime | None
    failures: int
    consecutive_failures: int
    dropped: int
    queued: int


class ApiKeyIn(BaseModel):
    """`PUT /settings/llm-providers/{name}/api-key` 와 `PUT /settings/web-tools/search/api-key` 의
    본문 (D56). 키는 응답·감사·Mongo 어디에도 남지 않고 플랫폼 네임스페이스의 Secret 에만
    들어간다."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=4096)


class LLMProviderKeyOut(BaseModel):
    """키를 넣은 Secret 의 이름. provider 의 `api_key_secret` 이 이것으로 바뀌어 있다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    api_key_secret: str


class WebSearchKeyOut(BaseModel):
    """`PUT /settings/web-tools/search/api-key`. 키를 넣은 Secret 의 이름뿐이다."""

    model_config = ConfigDict(extra="forbid")

    api_key_secret: str


class HubImagesCredentialIn(BaseModel):
    """허브 레지스트리를 읽기만 하는 계정 (P55 6절). 관리 Secret 에만 들어가고 응답·감사·Mongo 에는
    남지 않는다."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=4096)


class HubImagesCredentialOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_set: bool


class HubImagesKeyOut(BaseModel):
    """설정한 공개 키 하나의 지문(`sha256:<DER 의 sha256>`)과 회수했는지."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    revoked: bool


class HubImagesOverlapOut(BaseModel):
    """제품 이미지 접두사가 허브 접두사와 겹치는 클러스터. home 클러스터의 접두사는 기동마다
    환경에서 오므로 저장할 때의 검사를 비켜 갈 수 있다."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    cluster_name: str
    registry_prefix: str


class HubImagesUsageItemOut(BaseModel):
    """거부한 digest 를 든 허브 항목 하나와 그 항목을 백엔드로 쓰는 Gateway 의 수."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant: str
    name: str
    gateways: int


class HubImagesRefusedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: str
    items: list[HubImagesUsageItemOut]


class HubImagesStatusOut(BaseModel):
    """`GET /settings/hub-images/status` (P55 6절). 설정 카드가 읽는 계산한 값이고 platform-admin 만
    부른다. configured 는 허용이 켜졌는가(접두사가 있다)이다."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    keys: list[HubImagesKeyOut]
    overlaps: list[HubImagesOverlapOut]
    refused: list[HubImagesRefusedOut]


WebSearchCheckCode = Literal[
    "ok", "no_api_key", "unauthorized", "quota", "unreachable", "tls_failed", "bad_response"
]


class WebSearchCheckOut(BaseModel):
    """`POST /settings/web-tools/search/check`. 고정 코드와 걸린 시간(ms)이다."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    code: WebSearchCheckCode
    latency_ms: int


class LLMProviderCheckOut(BaseModel):
    """`POST /settings/llm-providers/{name}/check`. 고정 코드뿐이고 provider 의 원문 메시지는 없다.
    no_secret: Secret 이 없다. no_api_key: api_key_secret 이 비었거나 Secret 에 api_key 가 없다.
    auth: provider 가 키를 거부했다(401·403). connection: 연결 실패. provider_error: 그 밖의
    오류."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    code: LLMProviderCheckCode


class TenantSettingsOut(TenantSettings):
    model_config = REQUIRED_DEFAULTS


class AuthSummaryOut(AuthSummary):
    model_config = REQUIRED_DEFAULTS


class ClusterAuthStateOut(ClusterAuthState):
    model_config = REQUIRED_DEFAULTS


# 재적용이 다루는 종류의 이름. `services/data_plane_auth.py` 의 `RENDER_EPOCHS` 열쇠와 같다
ReapplyKindName = Literal["gateway", "mcp_server", "agent", "hub_item", "shared_clone"]


class BlockedConsumerOut(BaseModel):
    """요청 오류로 재적용이 막힌 문서. 기대 판의 두 절반 가운데 하나가 바뀌거나 그 문서의 Advanced
    편집 저장이나 재배포가 있을 때까지 건너뛴다. detail 은 막은 이유(잠긴 경로 등)다."""

    model_config = REQUIRED_DEFAULTS

    kind: ReapplyKindName
    id: str
    tenant: str
    cluster_id: str
    code: str
    detail: str | None


class KindCountsOut(BaseModel):
    """클러스터 하나에서 한 종류의 문서 수다 (P50 1절). pending 은 막히지 않았는데 판이 다른 문서,
    failed 는 그 가운데 백오프 중인 문서, blocked 는 막힌 문서의 수다. 이름은 summary 와 같다."""

    model_config = REQUIRED_DEFAULTS

    pending: int
    failed: int
    blocked: int


class PlainRedisConsumerOut(BaseModel):
    """TLS 끝점을 가진 Redis 에 아직 평문 포트로 붙는 소비자 하나 (P61 5절 4단계).
    data_service_id 가 null 이면 클러스터 기본 Redis 다. reason 은 runtime_image(Agent 의 런타임
    이미지가 redis_tls 를 모른다), binding(바인딩이 TLS 없이 렌더됐다), session_storage(세션
    저장소가 평문으로 렌더됐다), unknown(배포 스냅샷이 없어 렌더를 모른다) 가운데 하나다."""

    model_config = REQUIRED_DEFAULTS

    kind: Literal["agent", "mcp_server"]
    id: str
    name: str
    data_service_id: str | None
    reason: Literal["runtime_image", "binding", "session_storage", "unknown"]


class TurnGateHolderOut(TurnGateHolder):
    """턴 위임 토큰의 Gateway 게이트를 닫은 문서 하나 (P53 3.5·7절). reason 은 no_binding(묶임을
    싣지 않은 렌더로 돌 수 있다), owner_cedar(플랫폼이 짓지 않는 Cedar 로 돈다),
    exposed_toolhive_too_old(노출한 Gateway 의 클러스터가 ToolHive 0.53 아래다),
    exposed_agent_groups(노출 Agent 의 렌더가 allowed_groups 를 채우기 전의 것이다) 가운데 하나다.
    노출한 Gateway 와 노출 Agent 는 모든 클러스터의 게이트를 닫으므로 클러스터마다 다시 나온다."""

    model_config = REQUIRED_DEFAULTS

    kind: ReapplyKindName


class ClusterTurnGatesOut(BaseModel):
    """클러스터 하나의 턴 위임 게이트. version_ok 는 ToolHive 0.53 이상인가(지금 계산),
    gateway_gate_open 은 마지막 틱이 Gateway 게이트를 열었는가(데이터 플레인이 플랫폼을 믿지 않으면
    늘 닫혀 있다), verdict_at 은 그 판정의 때(첫 틱 전이면 null), verdict_fresh 는 그 판정을
    데이터 플레인이 지금 믿는 발급자로 내렸고 리컨실 간격 셋보다 젊은가다. 젊지 않으면 Gateway
    소스를 쓰는 새 턴은 503 이다."""

    model_config = REQUIRED_DEFAULTS

    cluster_id: str
    version_ok: bool
    gateway_gate_open: bool
    verdict_at: UtcDatetime | None
    verdict_fresh: bool
    holders: list[TurnGateHolderOut]


class TurnTokenDocOut(BaseModel):
    """턴 위임 토큰의 상태가 이름을 대는 문서 하나."""

    model_config = REQUIRED_DEFAULTS

    id: str
    name: str
    tenant: str
    cluster_id: str | None


class TurnTokensOut(BaseModel):
    """턴 위임 토큰의 상태 (P53 7절). authz_forbid_gateways 는 Authz policy 에 forbid 문장이 있는
    Gateway 다(글자 검사). 턴 위임 토큰에는 groups 가 없어 그 정책이 그룹으로 가리면 Agent 의 호출이
    거절되므로 소유자가 정책을 읽는다. agents_without_turn_tokens 는 토큰 인증 모드에서 Tool 소스를
    쓰는데 런타임 기록에 turn_tokens 가 없는 Agent 다. 그런 Agent 는 다시 배포할 때까지 Agent 의
    이름으로 부른다."""

    model_config = REQUIRED_DEFAULTS

    clusters: list[ClusterTurnGatesOut]
    authz_forbid_gateways: list[TurnTokenDocOut]
    agents_without_turn_tokens: list[TurnTokenDocOut]


class SwitchDocOut(TurnTokenDocOut):
    """발급자 전환의 상태가 이름을 대는 문서 하나. kind 는 재적용의 종류 이름이다."""

    kind: ReapplyKindName


class PendingDocOut(SwitchDocOut):
    """다시 렌더를 기다리는 문서. blocked 는 요청 오류로 막혀 옛 렌더로 도는가다."""

    blocked: bool


class ForeignGroupsAgentOut(SwitchDocOut):
    """allowed_groups 가 다른 테넌트의 플랫폼 그룹을 이름 대는 노출 Agent 와 그 그룹들."""

    groups: list[str]


class WaitingVerifierOut(BaseModel):
    """아직 플랫폼 토큰을 받지 못한 검증기. kind 는 VirtualMCPServer·MCPRemoteProxy·Agent 다.
    refused 는 마지막으로 불렀을 때 401 이었는가이고, 거짓이면 아직 부르지 못했다. since 는 처음
    기다린 때다."""

    model_config = REQUIRED_DEFAULTS

    kind: str
    name: str
    namespace: str
    tenant: str
    since: UtcDatetime
    refused: bool
    last_at: UtcDatetime | None


class VerifiedVerifierOut(BaseModel):
    """플랫폼 토큰을 받은 검증기와 그때. 그 파드가 바뀌면 다시 불러 보기 전까지 빠진다."""

    model_config = REQUIRED_DEFAULTS

    kind: str
    name: str
    namespace: str
    at: UtcDatetime


class ClusterSwitchOut(BaseModel):
    """클러스터 하나의 전환. platform_namespaces 는 daegam-oidc 가 플랫폼을 이름 대는 네임스페이스,
    idp_namespaces 는 아직 아닌 것이다. pending_documents 는 다시 렌더를 기다리는 문서(막힌 것
    포함)이고 waiting_verifiers 는 플랫폼 토큰을 아직 받지 못한 검증기(주소가 아직 없는 것 포함),
    verified_verifiers 는 받은 검증기다. probed_at 이 null 이면 아직 이 클러스터를 확인하지
    못했다."""

    model_config = REQUIRED_DEFAULTS

    cluster_id: str
    probed_at: UtcDatetime | None
    error: str | None
    platform_namespaces: list[str]
    idp_namespaces: list[str]
    pending_documents: list[PendingDocOut]
    waiting_verifiers: list[WaitingVerifierOut]
    verified_verifiers: list[VerifiedVerifierOut]


class IdpTokenLifeOut(BaseModel):
    """지난 하루에 확인된 사람 기록의 가장 긴 IdP 액세스 토큰 수명(초)과 그것이 넘지 말아야 할
    값(대화형 한도에서 위임 토큰 한 수명을 뺀 것, P53b 6.3절). 넘으면 warn 이다."""

    model_config = REQUIRED_DEFAULTS

    largest_seconds: int | None
    limit_seconds: int
    warn: bool


class ForeignMcpGroupOut(BaseModel):
    """저장된 mcp_group 이 다른 테넌트의 고정 그룹이나 그 밖의 `daegam-` 이름인 테넌트."""

    model_config = REQUIRED_DEFAULTS

    tenant: str
    mcp_group: str


class DataPlaneIssuerOut(BaseModel):
    """oidc 모드의 데이터 플레인 발급자 (P53b 6.9·6.10절). setting 은 DAEGAM_DATA_PLANE_ISSUER 이고
    trusted 는 데이터 플레인이 실제로 믿는 발급자다. 둘이 다르면 error 가 까닭이다:
    foreign_mcp_group 은 tenants_foreign_mcp_group 의 테넌트가 있어 기동이 idp 로 남겼다는
    것이고, 그 mcp_group 을 고치고 플랫폼을 다시 띄우면 바뀐다. 그 목록은 전환 전에 고치라고 두
    값에서 모두 낸다. trusted 가 platform 이면 clusters 가 클러스터마다의 전환이고 pending 은
    다시 렌더를 기다리는 문서, 플랫폼 토큰을 아직 받지 못한 검증기, 아직 확인하지 못한 클러스터의
    수다. 0 이 되면 전환이 끝났다. jwks_unreachable_clusters 는 플랫폼 발급자가 클러스터 안
    주소라 플랫폼 JWKS 에 닿지 못할 원격 클러스터다. 나머지 목록은 전환이 바꾸거나 열어 두는 것이고
    두 값에서 모두 낸다: gateways_idp_names(소유자 Cedar 가 IdP 그룹, IdP 의 그룹 클레임, client
    id 로 가린다), gateways_permit(소유자 Cedar 에 permit 이 있다),
    exposed_agents_foreign_groups(allowed_groups 가 다른 테넌트의 플랫폼 그룹을 이름 댄다),
    exposed_agents_open(allowed_groups 가 비어 audience 의 모든 IdP 토큰을 받는다),
    exposed_agents_one_issuer(두 발급자를 모르는 이미지라 IdP 하나다),
    tenants_agent_client_ids(IdP 클라이언트를 적은 테넌트, 전환 뒤 vMCP 와 앞단이 거절한다),
    agents_gateway_sources(Gateway 소스를 써 전환 때 다시 뜬다),
    gateways_published_refused(IdP 를 믿는 동안 게시된 백엔드가 거절하는 Gateway, P57 6.1절.
    플랫폼을 믿으면 비어 있다)."""

    model_config = REQUIRED_DEFAULTS

    setting: Literal["idp", "platform"]
    trusted: Literal["idp", "platform"]
    error: Literal["foreign_mcp_group"] | None
    pending: int
    clusters: list[ClusterSwitchOut]
    idp_token_life: IdpTokenLifeOut
    jwks_unreachable_clusters: list[str]
    gateways_idp_names: list[SwitchDocOut]
    gateways_permit: list[SwitchDocOut]
    exposed_agents_foreign_groups: list[ForeignGroupsAgentOut]
    exposed_agents_open: list[SwitchDocOut]
    exposed_agents_one_issuer: list[SwitchDocOut]
    tenants_agent_client_ids: list[str]
    agents_gateway_sources: list[SwitchDocOut]
    gateways_published_refused: list[SwitchDocOut]
    tenants_foreign_mcp_group: list[ForeignMcpGroupOut]


class DataPlaneAuthOut(BaseModel):
    """`GET /settings/data-plane-auth` (P32 8절, P50 1·2절). summary 는 첫 틱 전이면 null 이다.
    render_epochs 는 이 코드가 종류마다 쓰는 렌더 번호이고, kinds 는 클러스터 id 마다 종류별 문서
    수다. fallbacks 는 클러스터 id 마다, 게이트를 지나지 못해 그 기능을 뺀 대체 형태로 도는 살아
    있는 문서의 수를 기능 이름마다 센 것이다. 그런 문서가 없는 클러스터와 기능은 빠진다.
    handovers 는 클러스터 id 마다, 노출 앞단을 vMCP 로 넘겨받는 중인 MCP Server 를 넘겨받기의
    단계마다 센 것이다(P54 11절). 그런 서버가 없는 클러스터와 단계는 빠진다.
    redis_plain_consumers 는 테넌트마다, TLS 끝점을 가진 Redis 에 아직 평문 포트로 붙는 소비자다(P61
    5절 4단계). shipped_redis_cert_expires_at 은 home 이 함께 올린 Redis 의 TLS 서버 인증서가 끝나는
    때이고, expiring 은 그것이 30일 안인가다(P61 F3). turn_tokens 는 턴 위임 토큰의 게이트와 그것을
    막는 문서다(P53 7절). data_plane_issuer 는 oidc 모드의 데이터 플레인 발급자와 그 전환이고 다른
    모드에서는 null 이다(P53b 6.9절)."""

    model_config = REQUIRED_DEFAULTS

    install_id: str
    summary: AuthSummaryOut | None
    clusters: list[ClusterAuthStateOut]
    render_epochs: dict[ReapplyKindName, int]
    kinds: dict[str, dict[ReapplyKindName, KindCountsOut]]
    fallbacks: dict[str, dict[Feature, int]]
    handovers: dict[str, dict[Step, int]]
    redis_plain_consumers: dict[str, list[PlainRedisConsumerOut]]
    shipped_redis_cert_expires_at: UtcDatetime | None
    shipped_redis_cert_expiring: bool
    blocked: list[BlockedConsumerOut]
    exposed_agents_stale_runtime: int
    mcp_servers_stale_ca: int
    turn_tokens: TurnTokensOut
    data_plane_issuer: DataPlaneIssuerOut | None


class MembershipOut(Membership):
    model_config = REQUIRED_DEFAULTS


class TenantMembership(BaseModel):
    """`GET /tenants` 항목. platform-admin 은 tenant "*"."""

    model_config = ConfigDict(extra="forbid")

    tenant: str
    role: str


class PersonalTokenConfig(BaseModel):
    """개인 MCP 토큰 발급 폼의 재료 (P32 6절). 만료는 1일부터 max_days 까지 선택한다."""

    model_config = ConfigDict(extra="forbid")

    max_days: int


class OAuthServerConfig(BaseModel):
    """MCP 클라이언트의 OAuth 인가 서버가 켜졌는가 (P33 17절). 연결 카드가 "로그인으로 연결" 을
    보일지 이것으로 정한다. dormant 는 oidc 모드에서 데이터 플레인이 아직 IdP 만 믿어 서버가
    잠들었다는 뜻이다(P53b 6.5절). 그때 enabled 는 거짓이다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    dormant: bool = False


class AuthConfig(BaseModel):
    """`GET /auth/config`. header 모드면 auth_mode 만 온다. local 은 issuer·mcp_audience·token_ttl·
    personal_tokens·oauth, oidc 는 IdP 설정과 personal_tokens·oauth 에 더해 P53b 의 값이다.
    data_plane_issuer 는 Gateway·노출 앞단이 받는 사람 토큰의 발급자(`idp`·`platform`),
    idp_confirm_seconds 는 긴 한도, recent_sign_in_seconds 는 오래 가는 자격을 만들 때 ID 토큰의
    `auth_time` 이 들어야 하는 시간이다."""

    model_config = ConfigDict(extra="forbid")

    auth_mode: str
    issuer: str | None = None
    client_id: str | None = None
    audience: str | None = None
    scopes: list[str] | None = None
    groups_claim: str | None = None
    mcp_audience: str | None = None
    token_ttl: int | None = None
    personal_tokens: PersonalTokenConfig | None = None
    oauth: OAuthServerConfig | None = None
    data_plane_issuer: Literal["idp", "platform"] | None = None
    idp_confirm_seconds: int | None = None
    recent_sign_in_seconds: int | None = None


class MeMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    role: str
    source: str


class MeOut(BaseModel):
    """`GET /me`. name 은 화면이 사람을 부를 때 쓰는 이름이고 없으면 null 이다. `sub` 는 local 이
    uuid, header 가 "header" 라 인사에 쓰지 못한다(D45)."""

    model_config = ConfigDict(extra="forbid")

    sub: str
    name: str | None = None
    email: str | None
    groups: list[str]
    client_id: str | None
    subject_type: str
    memberships: list[MeMembership]
    is_platform_admin: bool
    scopes_by_tenant: dict[str, list[str]]


__all__ = [
    "AuthConfig",
    "GroupMapping",
    "LLMProviderCheckOut",
    "MeMembership",
    "MeOut",
    "MembershipOut",
    "OAuthServerConfig",
    "PersonalTokenConfig",
    "PlatformSettingsOut",
    "TenantMembership",
    "TenantSettingsOut",
]
