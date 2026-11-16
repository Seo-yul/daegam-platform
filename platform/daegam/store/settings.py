"""Platform Settings (싱글턴, `_id: "platform"`). 설정 계층의 1단계다."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.pem import MAX_CA_PEM_BYTES
from daegam.store.base import (
    SEQ_FIELD,
    SeqModel,
    UtcDatetime,
    from_stored,
    new_id,
    stored_body,
    upsert_by_seq,
    utcnow,
)

SETTINGS = "settings"
PLATFORM_ID = "platform"
# 이 설치의 id 를 담는 문서 (P32 8절). Platform Settings 문서(PLATFORM_ID)와 다른 문서다
INSTALL_DOC_ID = "install"


class OidcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str
    client_id: str
    audience: str | None = None


ProviderKind = Literal["openai", "anthropic", "openrouter", "zai", "stub"]
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-opus-5",
    "openrouter": "moonshotai/kimi-k3",
    "zai": "glm-5.3",
    "stub": "stub",
}
# kind 별 기본 주소. 여기 없는 kind 는 SDK 의 기본 주소(각 회사의 API)다. zai 는 Z.AI 종량제 API 다.
# GLM Coding Plan 주소(/api/coding/paas/v4)는 Z.AI 가 지원하는 코딩 도구에서만 쓸 수 있으므로
# 기본으로 두지 않는다
DEFAULT_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "zai": "https://api.z.ai/api/paas/v4",
}
# kind 를 부르는 SDK. OpenRouter 와 Z.AI 는 OpenAI 호환 API 라 openai SDK 로 부른다. 여기 없는
# kind 는 제 이름이다
SDK_KINDS: dict[str, str] = {"openrouter": "openai", "zai": "openai"}
# 임베딩 모델의 알려진 차원 (D32). 표에 없는 모델은 Knowledge Base 가 dimensions 를 직접 받는다
EMBEDDING_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "stub": 64,
}
# kind 별 기본 임베딩 모델. 여기 없는 kind 는 임베딩을 쓰지 않는다
DEFAULT_EMBEDDING_MODELS: dict[str, str] = {"openai": "text-embedding-3-small", "stub": "stub"}


def known_dimensions(model: str) -> int | None:
    """알려진 임베딩 모델의 차원. 모르는 모델은 None 이라 호출자가 dimensions 를 요구한다."""
    return EMBEDDING_DIMENSIONS.get(model)


# OpenRouter 가 받는 양자화 이름. 모르는 값은 OpenRouter 가 거부하므로 저장할 때 막는다
Quantization = Literal[
    "int4",
    "int8",
    "fp4",
    "mxfp4",
    "nvfp4",
    "fp6",
    "fp8",
    "mxfp8",
    "fp16",
    "bf16",
    "fp32",
    "unknown",
]
# OpenRouter 업체 slug. `moonshotai` 처럼 업체 전체이거나 `deepinfra/turbo` 처럼 그 한 갈래다
UpstreamSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9._-]{1,64})?$")]


# OpenRouter 는 같은 모델을 여러 업체가 돌리고 요청마다 하나를 고른다. 업체마다 양자화와 도구
# 호출 처리가 달라 응답이 들쭉날쭉하므로, 어느 업체를 쓸지 정해 요청 본문의 `provider` 로 싣는다.
# 기본값인 필드는 보내지 않아 OpenRouter 의 기본을 따른다. 독스트링은 비서 도구 정의에 실리므로
# 짧게 둔다
class ProviderRouting(BaseModel):
    """OpenRouter 업체 선호(T19). 요청 본문 `provider` 로 실린다."""

    model_config = ConfigDict(extra="forbid")

    order: list[UpstreamSlug] = Field(default_factory=list, max_length=20)  # 앞에서부터 시도할 업체
    allow_fallbacks: bool = True  # order 의 업체가 모두 안 되면 다른 업체로 넘어가도 되는가
    require_parameters: bool = False  # 요청 인자(도구 등)를 모두 지원하는 업체만 쓰는가
    quantizations: list[Quantization] = Field(default_factory=list, max_length=12)  # 허용할 양자화

    def body(self) -> dict[str, Any]:
        """요청 본문 `provider` 에 실을 값. 기본값인 필드는 뺀다."""
        out: dict[str, Any] = {}
        if self.order:
            out["order"] = self.order
        if not self.allow_fallbacks:
            out["allow_fallbacks"] = False
        if self.require_parameters:
            out["require_parameters"] = True
        if self.quantizations:
            out["quantizations"] = self.quantizations
        return out


# 키 값은 보관하지 않고 플랫폼 네임스페이스의 K8s Secret 이름(api_key_secret, 키 항목 `api_key`)만
# 둔다. 키 경로는 이것뿐이라 api_key_secret 이 비어 있거나 Secret 에 키가 없으면 저장은 되지만 job
# 은 422 provider_no_key 로 거부된다(D25). 예외는 `kind: stub` 하나다. e2e·로컬 실증용이라 키 없이
# 저장되고 배포본이 DAEGAM_ALLOW_STUB_PROVIDER=true 일 때만 등록할 수 있으며, Spec Generator 는
# 거부하고 Agent validate 는 provider_no_key 를 면제한다.
#
# 임베딩 provider 도 같은 행이다(D32). `embedding_model` 은 Knowledge Base 가 쓰는 임베딩 모델이고
# 비어 있으면 kind 별 기본이다. anthropic·openrouter·zai 는 임베딩 provider 가 아니라 적으면 검증
# 오류다. openrouter 는 OpenAI 호환 게이트웨이 OpenRouter 다. openai SDK 로 부르고(`sdk_kind`),
# base_url 과 모델을 비우면 OpenRouter 주소와 Kimi K3 다. `routing` 은 이 kind 만 받는 업체 선호다.
# zai 는 GLM 을 만든 Z.AI 의 API 를 직접 부르는 kind 다. 역시 openai SDK 로 부르고, base_url 과
# 모델을 비우면 종량제 주소와 glm-5.3 이다.
#
# 독스트링은 OpenAPI 설명이 되어 비서 도구 정의에 실린다. 자세한 규칙은 비서 지식 providers.md 가
# 맡으므로 독스트링은 짧게 둔다
class LLMProvider(BaseModel):
    """LLM 을 쓰는 기능이 이름으로 고르는 provider(D25). 키는 값이 아니라 Secret 이름으로만 둔다."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    kind: ProviderKind
    model: str | None = None  # None 이면 kind 별 기본 모델
    api_key_secret: str | None = None
    base_url: str | None = None  # OpenAI 호환 게이트웨이 등
    embedding_model: str | None = None  # None 이면 kind 별 기본 임베딩 모델 (D32)
    routing: ProviderRouting | None = None  # OpenRouter 업체 선호 (T19)
    # 스트리밍 응답에 사용량을 묻는가 (P37 2절). stream_options 를 거절하는 호환 서버는 끈다
    stream_usage: bool = True

    @model_validator(mode="after")
    def _check_kind_fields(self) -> LLMProvider:
        if self.embedding_model is not None and not self.supports_embedding:
            raise ValueError(f"embedding_model is not supported for kind {self.kind!r}")
        if self.routing is not None and self.kind != "openrouter":
            raise ValueError(f"routing is only for kind 'openrouter', not {self.kind!r}")
        return self

    @property
    def effective_model(self) -> str:
        return self.model or DEFAULT_MODELS[self.kind]

    @property
    def effective_base_url(self) -> str | None:
        """부를 주소. 비어 있으면 kind 기본이고, 그것도 없으면 None 이라 SDK 의 기본 주소다."""
        return self.base_url or DEFAULT_BASE_URLS.get(self.kind)

    @property
    def sdk_kind(self) -> str:
        """부를 SDK 의 계열. agent.json 의 provider kind 도 이것이라 런타임은 openrouter·zai 를
        모른다."""
        return SDK_KINDS.get(self.kind, self.kind)

    @property
    def extra_body(self) -> dict[str, Any] | None:
        """OpenAI SDK 요청 본문에 더 실을 값. 업체 선호가 있을 때만 `{"provider": ...}` 다. 플랫폼의
        호출과 agent.json 이 같은 값을 싣는다."""
        body = self.routing.body() if self.routing is not None else {}
        return {"provider": body} if body else None

    @property
    def is_stub(self) -> bool:
        return self.kind == "stub"

    @property
    def supports_embedding(self) -> bool:
        return self.kind in DEFAULT_EMBEDDING_MODELS

    @property
    def effective_embedding_model(self) -> str | None:
        """임베딩을 지원하지 않는 kind 는 None 이다."""
        if not self.supports_embedding:
            return None
        return self.embedding_model or DEFAULT_EMBEDDING_MODELS[self.kind]


class Defaults(BaseModel):
    """용도별 기본 provider 이름. assistant 가 비어 있으면 비서는 spec_generator 를, 그것도 없으면
    첫 provider 를 쓴다(D31)."""

    model_config = ConfigDict(extra="forbid")

    spec_generator: str | None = None
    assistant: str | None = None


# 런타임 models.py 와 같은 추론 강도 어휘 (D26). off 는 thinking disabled / reasoning_effort 없음
Effort = Literal["off", "low", "medium", "high", "xhigh", "max"]
# 비서는 항상 추론한다. off 를 두면 gpt-5 계열이 OpenAI 기본치(medium)로 돌아 더 느리고 비싸진다
AssistantEffort = Literal["low", "medium", "high", "xhigh", "max"]
MAX_TOOL_CALLS_PER_TURN = 60
"""한 턴의 도구 호출 상한. 전체 흐름(verify → backend → MCP → agent → deploy)이 열 번 남짓이고
호출마다 도구 정의 10k 토큰이 실리므로, 그 위는 비용만 늘고 턴 타임아웃(300초)에 먼저 걸린다."""


class AssistantSettings(BaseModel):
    """플랫폼 안의 청지기 (D31, P14). enabled 가 거짓이면 `/api/assistant` 는 config 만 답한다.
    confirm_deploy 가 거짓이면 deploy 등급 Tool 을 승인 없이 실행한다(destructive 는 항상 승인).
    effort 는 모델 인자 표의 입력이고 한도 둘은 턴 루프가 읽는다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    confirm_deploy: bool = True
    effort: AssistantEffort = "low"
    max_tool_calls_per_turn: int = Field(default=24, ge=1, le=MAX_TOOL_CALLS_PER_TURN)
    turn_timeout_seconds: int = Field(default=300, ge=1, le=3600)


class SkillsSettings(BaseModel):
    """테넌트마다의 Skill 할당량 (D64). Skill 개수와 리비전 본문 합계다."""

    model_config = ConfigDict(extra="forbid")

    max_per_tenant: int = Field(default=200, ge=1, le=10_000)
    tenant_quota_mb: int = Field(default=64, ge=1, le=10_240)


class WebFetchSettings(BaseModel):
    """플랫폼 `web_fetch` (P39b 10·15절). 값은 복제본 MCPServer 의 env 로 간다. 상한은
    `web-tools/webtools/settings.py` 가 다시 자르는 값과 같다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    per_minute: int = Field(default=60, ge=1, le=6000)
    per_user_per_minute: int = Field(default=10, ge=1, le=1000)
    per_user_per_day: int = Field(default=200, ge=1, le=100_000)
    max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    max_chars: int = Field(default=20_000, ge=500, le=100_000)
    timeout_seconds: int = Field(default=15, ge=1, le=60)
    max_redirects: int = Field(default=5, ge=0, le=5)
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443], min_length=1, max_length=10)
    blocked_host_suffixes: list[str] = Field(default_factory=list, max_length=100)
    respect_robots: bool = True
    blocked_cidrs: list[str] = Field(default_factory=list, max_length=32)
    user_agent_contact: str | None = None

    @field_validator("allowed_ports")
    @classmethod
    def _ports(cls, ports: list[int]) -> list[int]:
        if any(not 1 <= p <= 65535 for p in ports) or len(set(ports)) != len(ports):
            raise ValueError("allowed_ports are distinct ports from 1 to 65535")
        return ports


SearchProvider = Literal["brave", "tavily", "searxng", "stub"]


class WebSearchSettings(BaseModel):
    """플랫폼 `web_search` (P39b 12·15절). 키는 관리 Secret 의 이름만 둔다 (D56)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: SearchProvider = "brave"
    endpoint: str | None = None
    api_key_secret: str | None = None
    per_minute: int = Field(default=30, ge=1, le=6000)
    per_day: int = Field(default=2000, ge=1, le=1_000_000)
    per_user_per_minute: int = Field(default=10, ge=1, le=1000)
    per_user_per_day: int = Field(default=200, ge=1, le=100_000)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_count: int = Field(default=10, ge=1, le=10)


class WebToolsSettings(BaseModel):
    """플랫폼 웹 Tool 두 개와, 노출한 Gateway(바깥 MCP 클라이언트)에 넣는 것을 허용할지."""

    model_config = ConfigDict(extra="forbid")

    fetch: WebFetchSettings = Field(default_factory=WebFetchSettings)
    search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    external_clients: bool = False


class NetworkSettings(BaseModel):
    """설치 전체의 바깥 연결 (P39b 11절). SSL 을 다시 서명하는 사내 방화벽의 CA 묶음(PEM)이다. 공개
    인증서라 Mongo 에 둔다. 플랫폼은 명시 프록시를 쓰지 않으므로 프록시 칸은 없다."""

    model_config = ConfigDict(extra="forbid")

    ca_bundle: str | None = Field(default=None, max_length=MAX_CA_PEM_BYTES)


# LLM 게이트웨이 토큰 한도의 범위 (P38 7절)
LIMIT_MIN = 1
LIMIT_MAX = 1_000_000_000
TokensPerMinute = Annotated[int, Field(ge=LIMIT_MIN, le=LIMIT_MAX)]


class LlmGatewayLimits(BaseModel):
    """분마다의 토큰 한도. 없으면 한도가 없다. 테넌트 기본값, 테넌트마다의 값, provider 마다의
    값이고 provider 의 값은 그 provider 의 모든 테넌트를 함께 센다."""

    model_config = ConfigDict(extra="forbid")

    tenant_tokens_per_minute: TokensPerMinute | None = None
    tenants: dict[str, TokensPerMinute] = Field(default_factory=dict)
    # provider 이름 → 한도
    providers: dict[str, TokensPerMinute] = Field(default_factory=dict)


class LlmGatewaySettings(BaseModel):
    """Platform Settings 의 `llm_gateway` 절 (P38 7절). platform-admin 이 고친다."""

    model_config = ConfigDict(extra="forbid")

    limits: LlmGatewayLimits = Field(default_factory=LlmGatewayLimits)


# App ID 와 Entra 테넌트 id 의 꼴. Entra 가 내는 GUID 다
GUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
Guid = Annotated[str, Field(pattern=GUID_PATTERN)]


class TeamsSettings(BaseModel):
    """Microsoft Teams 봇 하나 (P47 6.1절). 봇의 Client Secret 은 관리 Secret
    `daegam-channel-teams` 에 있고 여기는 넣었는지(`secret_set`)만 안다. `enabled` 는 Teams 에서
    Agent 를 쓰는 스위치다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    app_id: Guid | None = None
    entra_tenant_id: Guid | None = None
    # Client Secret 을 넣었는가. Secret 을 넣는 경로만 쓴다
    secret_set: bool = False

    @property
    def configured(self) -> bool:
        """App ID, Entra 테넌트 id, Client Secret 이 모두 있는가."""
        return bool(self.app_id and self.entra_tenant_id and self.secret_set)


class ChannelsSettings(BaseModel):
    """Agent 를 부르는 바깥 채널 (P47). 저장된 문서는 이 판이 모르는 칸을 버리고 읽는다
    (`_platform_from_doc`). 뒤 판이 더한 채널이 있어도 이 판이 설정을 읽는다."""

    model_config = ConfigDict(extra="forbid")

    teams: TeamsSettings = Field(default_factory=TeamsSettings)


# 허브 image 항목의 서명 확인 (P55 6절). digest 와 키 지문은 같은 꼴이다
SHA256_REF = r"^sha256:[0-9a-f]{64}$"
Sha256Ref = Annotated[str, Field(pattern=SHA256_REF)]
# 공개 키 하나(PEM)의 상한과 목록의 상한. 키는 바꾸는 동안 옛 키와 새 키 둘이면 되고, 거부한
# digest 는 사람이 하나씩 적는 목록이다
MAX_HUB_KEY_PEM = 4096
MAX_HUB_KEYS = 8
MAX_HUB_REVOKED_KEYS = 64
MAX_HUB_REFUSED_DIGESTS = 500


class HubImagesSettings(BaseModel):
    """허브의 image 항목이 쓸 수 있는 이미지 (P55 6절). 레지스트리 접두사 하나와 허브 키의 공개 키
    (PEM, P-256), 회수한 키의 지문, 거부한 digest 다. 접두사가 있으면 허용이 켜진다(`configured`).
    접두사는 공개 키 없이 저장되지 않으므로 허용이 조용히 꺼지는 일이 없다. 읽기 자격 증명은
    플랫폼 네임스페이스의 관리 Secret 에 있고 여기는 넣었는지(`credential_set`)만 안다. 값의
    뜻(접두사의 꼴, 키의 곡선, 제품 이미지 경로와 겹침)은 `services/hub_images.py`
    `check_hub_images` 가 본다."""

    model_config = ConfigDict(extra="forbid")

    prefix: str | None = Field(default=None, max_length=512)
    public_keys: list[Annotated[str, Field(max_length=MAX_HUB_KEY_PEM)]] = Field(
        default_factory=list, max_length=MAX_HUB_KEYS
    )
    revoked_keys: list[Sha256Ref] = Field(default_factory=list, max_length=MAX_HUB_REVOKED_KEYS)
    refused_digests: list[Sha256Ref] = Field(
        default_factory=list, max_length=MAX_HUB_REFUSED_DIGESTS
    )
    # 읽기 자격 증명을 넣었는가. 자격 증명을 넣는 경로만 쓴다
    credential_set: bool = False

    @property
    def configured(self) -> bool:
        """허용이 켜졌는가. 접두사가 있다. 쓸 수 있는 키가 없으면 모든 이미지가 허용되지 않는다."""
        return bool(self.prefix)


class PlatformSettings(SeqModel):
    """`GET/PATCH /settings` 본문. 값이 없는 필드는 코드 기본값이 적용된다."""

    model_config = ConfigDict(extra="forbid")

    default_runtime_image: str = "daegam-mcp-runtime:0.1.0"
    # effective_config.images.agent_image 로 실려 가고 Agent 렌더가 쓴다 (D26)
    default_agent_image: str = "daegam-agent-runtime:0.1.0"
    default_model: str = "claude-opus-5"
    default_cluster_id: str | None = None
    namespace_template: str = "daegam-{tenant}"
    oidc: OidcSettings | None = None
    llm_providers: list[LLMProvider] = Field(default_factory=list)
    defaults: Defaults = Field(default_factory=Defaults)
    assistant: AssistantSettings = Field(default_factory=AssistantSettings)
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    web_tools: WebToolsSettings = Field(default_factory=WebToolsSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    # LLM 게이트웨이의 토큰 한도 (P38 7절)
    llm_gateway: LlmGatewaySettings = Field(default_factory=LlmGatewaySettings)
    # Agent 를 부르는 바깥 채널 (P47)
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    # 허브 image 항목의 서명 확인 (P55 6절)
    hub_images: HubImagesSettings = Field(default_factory=HubImagesSettings)
    # 플랫폼 web_fetch 복제본의 이미지. Cluster 가 덮는다 (P39b 13절)
    default_web_tools_image: str = "daegam-web-tools:0.1.0"
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None

    @model_validator(mode="after")
    def _check_providers(self) -> PlatformSettings:
        names = [p.name for p in self.llm_providers]
        if len(names) != len(set(names)):
            raise ValueError("llm_providers names must be unique")
        for purpose in ("spec_generator", "assistant"):
            wanted = getattr(self.defaults, purpose)
            if wanted is not None and wanted not in names:
                raise ValueError(f"defaults.{purpose} {wanted!r} is not in llm_providers")
        return self

    @property
    def providers_by_name(self) -> dict[str, LLMProvider]:
        return {p.name: p for p in self.llm_providers}

    def provider(self, name: str | None = None) -> LLMProvider | None:
        """이름으로, 이름이 없으면 defaults.spec_generator 로, 그것도 없으면 첫 provider."""
        wanted = name or self.defaults.spec_generator
        if wanted is None:
            return self.llm_providers[0] if self.llm_providers else None
        for p in self.llm_providers:
            if p.name == wanted:
                return p
        return None

    def assistant_provider(self) -> LLMProvider | None:
        """비서가 쓰는 provider. defaults.assistant, 없으면 defaults.spec_generator, 그것도
        없으면 첫 provider 다 (D31)."""
        return self.provider(self.defaults.assistant)


# 칸마다 따로 쓰는 절 (P47 6.1절, P55 6절). 저장이 그 절을 통째로 바꾸지 않으므로 이 판이 모르는
# 안쪽 칸이 문서에 남는다
MERGED_SECTIONS = ("channels", "hub_images")


def _known(model: type[BaseModel], data: Any) -> Any:
    """model 이 아는 칸만 남긴다. 안쪽 모델 칸도 같이 거른다."""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name not in data:
            continue
        inner = info.annotation
        known = isinstance(inner, type) and issubclass(inner, BaseModel)
        out[name] = _known(inner, data[name]) if known else data[name]
    return out


def _platform_from_doc(doc: dict[str, Any]) -> PlatformSettings:
    """저장된 문서를 읽는다. `PlatformSettings` 의 필드만 읽으므로 이 판이 모르는 칸은 버린다. 뒤
    판이 더한 절과 앞 판이 두었던 칸(따로인 문서로 옮긴 `observability`(P37 15절), 읽는 곳이 없어
    지운 `spec_token_ttl`)이 그렇다. 앞 판으로 되돌려도 설정을 읽을 수 있고, 저장은 보낸 절만
    쓰므로 모르는 칸은 문서에 남는다. `channels` 는 안쪽의 모르는 칸까지 버리고 읽고(P47 6.1절)
    칸마다 따로 쓴다(`MERGED_SECTIONS`). `hub_images` 도 그렇다 (P55 6절)."""
    body = {k: v for k, v in stored_body(doc).items() if k in PlatformSettings.model_fields}
    if "channels" in body:
        body["channels"] = _known(ChannelsSettings, body["channels"])
    if "hub_images" in body:
        body["hub_images"] = _known(HubImagesSettings, body["hub_images"])
    return from_stored(PlatformSettings, doc, body)


class MongoPlatformSettingsStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SETTINGS]
        self._install_id: str | None = None

    async def init_schema(self) -> None:
        return None

    async def get(self) -> PlatformSettings:
        doc = await self.coll.find_one({"_id": PLATFORM_ID})
        if doc is None:
            return PlatformSettings()
        return _platform_from_doc(doc)

    async def update(
        self,
        changes: dict[str, Any],
        *,
        updated_by: str | None = None,
        base: PlatformSettings | None = None,
    ) -> PlatformSettings | None:
        """사람의 저장 (P68 2절). base(없으면 지금 읽은 값) 위에 changes 를 얹어 문서 전체를
        검증하고, 보낸 절과 updated_at·updated_by 만 쓴다. base 를 읽은 `seq` 일 때만 맞고 `seq` 를
        올린다. 그사이 다른 저장이 지나갔으면 None 이다."""
        current = base if base is not None else await self.get()
        doc = await upsert_by_seq(
            self.coll, PLATFORM_ID, current, changes, updated_by, merged=MERGED_SECTIONS
        )
        return None if doc is None else _platform_from_doc(doc)

    async def set_provider_key_secret(
        self, name: str, secret: str, *, updated_by: str | None = None
    ) -> PlatformSettings | None:
        """provider name 의 `api_key_secret` 만 그 자리에서 바꾼다 (P68 2절). 앞서 읽은 목록을
        통째로 쓰지 않으므로 그사이 더해진 provider 를 지우지 않는다. 그 provider 가 그사이
        지워졌으면 None 이다."""
        doc = await self.coll.find_one_and_update(
            {"_id": PLATFORM_ID, "llm_providers.name": name},
            {
                "$set": {
                    "llm_providers.$[p].api_key_secret": secret,
                    "updated_at": utcnow(),
                    "updated_by": updated_by,
                },
                # 사람의 저장이라 `seq` 를 올린다. 키를 넣기 전에 읽은 저장이 목록을 되돌려
                # 쓰지 못한다
                "$inc": {SEQ_FIELD: 1},
            },
            array_filters=[{"p.name": name}],
            return_document=ReturnDocument.AFTER,
        )
        return None if doc is None else _platform_from_doc(doc)

    async def mark_teams_secret(self, *, updated_by: str | None = None) -> PlatformSettings:
        """Teams 봇의 Client Secret 을 넣었다고 적는다 (P47 6.1절)."""
        return await self._mark_set("channels.teams.secret_set", updated_by)

    async def mark_hub_images_credential(
        self, *, updated_by: str | None = None
    ) -> PlatformSettings:
        """허브 이미지의 읽기 자격 증명을 넣었다고 적는다 (P55 6절)."""
        return await self._mark_set("hub_images.credential_set", updated_by)

    async def _mark_set(self, field: str, updated_by: str | None) -> PlatformSettings:
        """관리 Secret 에 값을 넣었다는 표시 하나를 켠다. 그 칸만 그 자리에서 쓰므로 그사이 다른
        저장과 부딪히지 않는다. 사람의 저장이라 `seq` 를 올린다."""
        doc = await self.coll.find_one_and_update(
            {"_id": PLATFORM_ID},
            {
                "$set": {field: True, "updated_at": utcnow(), "updated_by": updated_by},
                "$inc": {SEQ_FIELD: 1},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return _platform_from_doc(doc)

    async def install_id(self) -> str:
        """이 설치의 id (P32 8절). 처음 부를 때 `install` 문서에 한 번 만들고 다시 바꾸지 않는다.
        레플리카 둘이 함께 떠도 `$setOnInsert` upsert 라 값은 하나다. 두 upsert 가 부딪혀 한쪽이
        중복 키로 지면 이긴 쪽의 값을 읽는다. 바뀌지 않는 값이라 스토어가 기억한다."""
        if self._install_id is None:
            query = {"_id": INSTALL_DOC_ID}
            try:
                doc = await self.coll.find_one_and_update(
                    query,
                    {"$setOnInsert": {"install_id": new_id()}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                doc = await self.coll.find_one(query)
            self._install_id = str(doc["install_id"])
        return self._install_id
