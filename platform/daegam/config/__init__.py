"""런타임 설정. 환경 변수로 덮어쓰고, 테스트는 create_app(settings)로 직접 주입한다.

플랫폼은 무상태 파드로 돌므로 로컬 디스크 경로가 없다. 메타와 스펙 본문 모두 MongoDB에 둔다.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from daegam.auth.cedar import CLIENT_ID_CLAIM, claim_path
from daegam.auth.signer import (
    CONTROL_PLANE_AUDIENCE,
    INTERNAL_AUDIENCE_PREFIX,
    LLM_AUDIENCE,
    TELEMETRY_AUDIENCE,
    jwks_url,
)
from daegam.documents import TOKEN_ENV as DOCPARSE_TOKEN_ENV
from daegam.urls import ENV_WHITESPACE, is_http_url, origin_of

logger = logging.getLogger("daegam.config")

# DAEGAM_PAT_KEY_POOL 의 범위. 넘거나 모자라면 끝값으로 자른다 (P32 11절)
PAT_KEY_POOL_MIN = 1
PAT_KEY_POOL_MAX = 200
# 자격의 수명을 일로 받는 설정(DAEGAM_PAT_MAX_DAYS, DAEGAM_OAUTH_REFRESH_DAYS)의 범위. 설정 실수
# 하나가 10년짜리 자격을 만들지 않게 상한을 넘으면 상한으로 자르고 경고한다 (P32 11절, P33 17절)
LIFETIME_DAYS_MIN = 1
LIFETIME_DAYS_LIMIT = 365
# oidc 모드의 긴 한도 DAEGAM_IDP_CONFIRM_SECONDS 의 상한(90일)이다. 넘으면 상한으로 자르고
# 경고한다. 하한은 대화형 한도(DAEGAM_TURN_OWNER_CONFIRM_SECONDS)이고 그보다 짧으면 기동하지
# 않는다 (P53b 6.3절)
IDP_CONFIRM_MAX = 90 * 86400
# DAEGAM_SERVICE_ACCOUNTS 의 값. 서비스 어카운트를 누가 만들고 그 토큰을 내는가 (P53b 6.7절)
SERVICE_ACCOUNTS_TENANT_ADMIN = "tenant-admin"
SERVICE_ACCOUNTS_PLATFORM_ADMIN = "platform-admin"
SERVICE_ACCOUNTS_OFF = "off"
SERVICE_ACCOUNTS_MODES = (
    SERVICE_ACCOUNTS_TENANT_ADMIN,
    SERVICE_ACCOUNTS_PLATFORM_ADMIN,
    SERVICE_ACCOUNTS_OFF,
)
# DAEGAM_DATA_PLANE_ISSUER 의 값 (P53b 6.1·6.2절). oidc 모드의 데이터 플레인(Gateway 의 vMCP, 노출
# 앞단, 노출 Agent)이 믿는 사람 토큰의 발급자다. idp 는 IdP 이고 platform 은 플랫폼이다. local 과
# header 모드는 이 값을 읽지 않는다
DATA_PLANE_ISSUER_IDP = "idp"
DATA_PLANE_ISSUER_PLATFORM = "platform"
DATA_PLANE_ISSUERS = (DATA_PLANE_ISSUER_IDP, DATA_PLANE_ISSUER_PLATFORM)
# 턴 위임 토큰의 수명(초) DAEGAM_TURN_TOKEN_TTL_SECONDS 와, oidc 모드의 대화형 턴이 사람의 마지막
# IdP 확인에서 떨어질 수 있는 시간(초) DAEGAM_TURN_OWNER_CONFIRM_SECONDS 의 범위다. 밖이면 끝값으로
# 자른다 (P53 4절, P53b 6.3절)
TURN_TOKEN_TTL_MIN = 60
TURN_TOKEN_TTL_MAX = 900
TURN_OWNER_CONFIRM_MIN = 300
TURN_OWNER_CONFIRM_MAX = 86400
# 쓰지 않는 Agent 를 0 으로 줄이는 설정의 범위 (P44 14절). 창은 0 이거나 이 아래로 내리지 않고,
# 매일 깨우기의 주기(시간)와 클러스터마다 함께 깨우는 수는 밖이면 끝값으로 자른다. 주기의 하한은
# 게이트가 짧은 주기로 매일 깨우기를 보려고 낮게 둔다
AGENT_SLEEP_MIN_SECONDS = 120
HOUSEKEEPING_HOURS_MIN = 0.05
HOUSEKEEPING_HOURS_MAX = 24.0 * 30
HOUSEKEEPING_PARALLEL_MAX = 50
# 매일 깨우기가 Agent 를 깨워 두는 초의 범위. 하한은 게이트가 짧은 hold 로 매일 깨우기를 보려고
# 낮게 두고, 상한은 클러스터의 몫을 한 시간 넘게 쥐지 않게 한다
HOUSEKEEPING_HOLD_MIN_SECONDS = 60
HOUSEKEEPING_HOLD_MAX_SECONDS = 3600
# 테넌트마다 함께 여는 미리 보기 수의 상한 (P44b 10절). 넘으면 상한으로 자른다
PREVIEWS_PER_TENANT_MAX = 50
# DAEGAM_OAUTH_CIMD_CLIENTS 의 기본값. Claude Code, VS Code, Codex CLI 가 client_id 로 쓰는 CIMD
# 문서 URL 이다 (P33 4절). 클라이언트 제작사의 주소라 플랫폼 호스트 글자가 아니다
OAUTH_CIMD_DEFAULT_CLIENTS = (
    "https://claude.ai/oauth/claude-code-client-metadata",
    "https://vscode.dev/oauth/client-metadata.json",
    "https://chatgpt.com/oauth/codex/client.json",
)
# LLM 게이트웨이 (P38 3절·12절). 스위치는 경로 결정만 먹이고, 주소와 네임스페이스는 켜든 끄든 있다
LLM_GATEWAY_OFF = "off"
LLM_GATEWAY_AGENTGATEWAY = "agentgateway"
LLM_GATEWAY_MODES = (LLM_GATEWAY_OFF, LLM_GATEWAY_AGENTGATEWAY)
DEFAULT_LLM_GATEWAY_NAMESPACE = "daegam-llm"
LLM_GATEWAY_SERVICE = "llm-gateway"
LLM_GATEWAY_PORT = 8080
# 게이트웨이 주소는 클러스터 안 Service 이름 하나다. 경로·쿼리가 없다
_LLM_GATEWAY_URL = re.compile(
    r"https?://[a-z0-9]([-a-z0-9]*[a-z0-9])?\.[a-z0-9]([-a-z0-9]*[a-z0-9])?"
    r"\.svc(\.cluster\.local)?(:(?P<port>[0-9]{1,5}))?",
    re.ASCII,
)
# 테넌트 네임스페이스와 같은 DNS 라벨 규칙이다(store/tenants.py NAMESPACE_RE)
_NAMESPACE_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", re.ASCII)


def default_llm_gateway_url(namespace: str) -> str:
    """비운 DAEGAM_LLM_GATEWAY_URL 의 값. 게이트웨이 네임스페이스의 Service 주소다."""
    return f"http://{LLM_GATEWAY_SERVICE}.{namespace}.svc.cluster.local:{LLM_GATEWAY_PORT}"


def check_llm_gateway(settings: Settings) -> None:
    """게이트웨이 설치 값을 본다. 틀리면 기동을 거절한다 (P38 3절). 스위치는 off·agentgateway
    이고, 네임스페이스는 플랫폼 네임스페이스도 `default`·`kube-*` 도 아닌 DNS 라벨이며, 주소는
    `http(s)://<이름>.<네임스페이스>.svc[.cluster.local][:포트]` 꼴이다. 켰으면 이미지가 있어야
    한다."""
    if settings.llm_gateway not in LLM_GATEWAY_MODES:
        raise ValueError(
            f"DAEGAM_LLM_GATEWAY must be off or agentgateway, got {settings.llm_gateway!r}"
        )
    ns = settings.llm_gateway_namespace
    if (
        not _NAMESPACE_LABEL.fullmatch(ns)
        or ns.startswith("kube-")
        or ns in ("default", settings.namespace)
    ):
        raise ValueError(
            f"DAEGAM_LLM_GATEWAY_NAMESPACE {ns!r} must be a DNS label other than the platform "
            "namespace, default and kube-*"
        )
    url = settings.llm_gateway_url or ""
    m = _LLM_GATEWAY_URL.fullmatch(url)
    if m is None or (m["port"] is not None and not 0 < int(m["port"]) < 65536):
        raise ValueError(
            "DAEGAM_LLM_GATEWAY_URL must be "
            "http(s)://<name>.<namespace>.svc[.cluster.local][:port] without a path, "
            f"got {url!r}"
        )
    if settings.llm_gateway_on and not settings.llm_gateway_images:
        raise ValueError("DAEGAM_LLM_GATEWAY=agentgateway needs DAEGAM_LLM_GATEWAY_IMAGES")


# 사람 토큰이 그룹을 싣는 클레임의 기본값. ToolHive 의 Cedar 도 이름을 받지 않으면 이것부터 읽으므로
# (그다음 roles, cognito:groups) 이 이름이면 데이터 플레인 렌더에 클레임 이름을 싣지 않는다
DEFAULT_GROUPS_CLAIM = "groups"
# ToolHive CRD 의 groupClaimName 길이 상한
GROUPS_CLAIM_MAX = 253


def check_groups_claim(name: str) -> None:
    """DAEGAM_OIDC_GROUPS_CLAIM 을 본다. 공백이나 제어 문자가 들었거나 빈 마디가 있는 이름은 설정
    파일의 오타로 보고 거절한다. 플랫폼 검증기와 ToolHive 는 이름을 맨 위 키로 먼저 찾으므로 그런
    키를 그대로 내는 IdP 라면 찾기는 하지만, values 파일에 섞인 공백이나 점이 훨씬 흔하다. 253 자는
    ToolHive CRD 의 groupClaimName 상한이다."""
    if (
        len(name) > GROUPS_CLAIM_MAX
        or not name.isprintable()
        or any(c.isspace() for c in name)
        or "" in name.split(".")
    ):
        raise ValueError(
            f"DAEGAM_OIDC_GROUPS_CLAIM {name!r} must be a claim name of at most "
            f"{GROUPS_CLAIM_MAX} characters without spaces, or several joined by dots for a nested "
            "claim; in oidc mode any other name stops startup"
        )


def check_client_id_claim(name: str) -> None:
    """DAEGAM_OIDC_CLIENT_ID_CLAIM 을 본다. Gateway 와 노출 앞단은 이 이름을 Cedar 정책에 그대로
    적으므로(`claim_path`) 그룹 클레임보다 규칙이 좁다. Cedar 에 적을 수 없는 이름은 거절한다."""
    try:
        claim_path(name)
    except ValueError as e:
        raise ValueError(
            f"DAEGAM_OIDC_CLIENT_ID_CLAIM: {e}; in oidc mode any other name stops startup"
        ) from e


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_number[T: (int, float)](name: str, default: T, parse: Callable[[str], T]) -> T:
    """사람이 ConfigMap 에서 고치는 숫자. 틀린 값으로 기동이 죽지 않게 경고를 남기고 기본값으로
    물러난다. 숫자 설정은 모두 이 규칙 하나를 따른다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return parse(raw.strip())
    except ValueError:
        logger.warning("%s=%r is not a valid number; using the default %r", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return _env_number(name, default, int)


def _env_int_clamped(name: str, default: int, lo: int, hi: int, *, warn_above: bool = False) -> int:
    """범위가 있는 정수 설정. 밖이면 끝값으로 자른다. warn_above 면 상한을 넘은 값에 경고한다."""
    value = _env_int(name, default)
    if warn_above and value > hi:
        logger.warning("%s=%d is above %d; using %d", name, value, hi, hi)
    return min(hi, max(lo, value))


def _lifetime_days(name: str, default: int) -> int:
    """자격의 수명(일) 설정. 1~365 이고 상한을 넘으면 자르고 경고한다."""
    return _env_int_clamped(name, default, LIFETIME_DAYS_MIN, LIFETIME_DAYS_LIMIT, warn_above=True)


def _env_float(name: str, default: float) -> float:
    return _env_number(name, default, float)


def _env_float_clamped(name: str, default: float, lo: float, hi: float) -> float:
    """범위가 있는 실수 설정. 밖이면 끝값으로 자른다."""
    return min(hi, max(lo, _env_float(name, default)))


def _sleep_window_from_env() -> int:
    """DAEGAM_AGENT_SLEEP_AFTER_SECONDS (P44 14절). 0 아래는 0 이고, 0 과 하한 사이는 하한으로
    올리고 경고한다. 활동 쓰기를 창의 4분의 1 동안 건너뛰므로 너무 짧은 창은 턴 사이에 잠든다."""
    name = "DAEGAM_AGENT_SLEEP_AFTER_SECONDS"
    value = max(0, _env_int(name, _DEFAULTS["agent_sleep_after_seconds"]))
    if 0 < value < AGENT_SLEEP_MIN_SECONDS:
        floor = AGENT_SLEEP_MIN_SECONDS
        logger.warning("%s=%d is below %d; using %d", name, value, floor, floor)
        return floor
    return value


@dataclass(frozen=True)
class Settings:
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "daegam"
    tenant_header: str = "X-Daegam-Tenant"
    default_tenant: str = "default"
    max_upload_bytes: int = 20 * 1024 * 1024
    # --- K8s / 홈 클러스터 ---
    namespace: str = "daegam-system"  # 플랫폼 자신의 네임스페이스. cluster-{id} Secret이 여기 산다
    in_cluster: bool = False  # DAEGAM_IN_CLUSTER=true 면 in-cluster config 로 home 등록
    home_kubecontext: str | None = None  # 로컬 개발: kubeconfig 의 이 컨텍스트로 home 등록
    platform_port: int = 9000
    platform_internal_url: str | None = None  # 명시하면 자동 계산값 대신 쓴다
    # --- reconcile (F7b) ---
    reconcile_enabled: bool = True
    reconcile_interval: float = 60.0
    pod_id: str = field(
        default_factory=lambda: os.environ.get("HOSTNAME") or f"local-{os.getpid()}"
    )
    # --- 인증 (P6 F1, D40) ---
    # oidc: IdP 가 사람을 확인한다. local: 플랫폼이 아이디·비밀번호로 확인하고 토큰을 발급한다.
    # header: 아무도 확인하지 않는다(로컬·테스트 전용, 테넌트 헤더를 믿는다)
    auth_mode: str = "oidc"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_client_id: str | None = None  # UI 의 PKCE 클라이언트. 검증에는 안 쓰고 UI 설정에 준다
    oidc_groups_claim: str = DEFAULT_GROUPS_CLAIM
    oidc_client_id_claim: str = CLIENT_ID_CLAIM  # Okta 는 cid, 다른 IdP 는 azp / client_id
    mcp_audience: str = "daegam-mcp"  # MCP 계층(vMCP) 토큰의 aud. 플랫폼 전체 하나 (P6 F4a)
    # 개발 전용: HTTP issuer 허용. MCPOIDCConfig inline 에 insecureAllowHTTP·jwksAllowPrivateIP 를
    # 넣는다 (없으면 Valid=False, docs/archive/reviews/2026-09-02-spike-vmcp-oidc.md 1번)
    oidc_insecure_http: bool = False
    # 부트스트랩: 이 그룹이 토큰 groups 에 있으면 매핑 없이 platform-admin 이 된다.
    # 최초 group_mappings 가 비어 있을 때 첫 관리자를 만들려고 두는 값. 운영에서는 비워 둔다
    bootstrap_admin_group: str | None = None
    # --- 플랫폼 발급자 (D40, D41). 모든 모드에서 서비스 토큰을, local 에서는 사람 토큰도 낸다 ---
    # 서비스 토큰의 iss 이고, 플랫폼이 서명한 토큰의 검증기가 JWKS 를 받는 주소의 접두라 그들이
    # 닿아야 한다. 비우면 홈 클러스터의 platform_internal_url 이다. 여러 클러스터면 모두가 닿는 공개
    # https 주소로 준다. local 의 사람 토큰은 공개 주소가 있으면 그것을 iss 로 쓴다 (P32 1절)
    platform_issuer: str | None = None
    # --- 공개 주소 (D58). 브라우저와 밖의 MCP 클라이언트가 닿는 origin 이다. 설치의 ConfigMap 이
    # 주고(앞단이나 Ingress 가 TLS 를 끝내는 설치는 https, 로컬은 http) 비우면 없다. 꼴 검사는
    # _public_url_from_env 가 한다
    public_url: str | None = None
    # --- local 모드 (D40) ---
    local_token_ttl: int = 12 * 3600
    # users 가 비어 있을 때 한 번 만드는 첫 관리자. 비밀번호는 Secret 에서 env 로 온다
    local_bootstrap_username: str = "admin"
    local_bootstrap_password: str | None = None
    # 미리 JWKS 에 올려 두는 개인 토큰 서명 키 슬롯 수 (P32 4절). local 모드에서만 쓴다
    pat_key_pool: int = 20
    # 개인 토큰 만료의 상한(일) (P32 6절). 사람은 1일부터 이 값까지 선택한다
    pat_max_days: int = LIFETIME_DAYS_LIMIT
    # --- MCP 클라이언트의 OAuth 인가 서버 (D59). 켜지는지는 oauth_enabled 가 넷을 함께 본다 ---
    # DAEGAM_OAUTH_ENABLED. 끄는 스위치이고 켜는 것만으로는 효과가 없다
    oauth_switch: bool = True
    # 평문 http 공개 주소에서도 켠다. orbstack e2e 전용이고 기동 로그가 경고한다
    oauth_allow_http: bool = False
    # CIMD client_id 의 정확한 URL 허용 목록. 비면 CIMD 를 끈다
    oauth_cimd_clients: tuple[str, ...] = OAUTH_CIMD_DEFAULT_CLIENTS
    # OAuth 연결의 절대 수명(일)
    oauth_refresh_days: int = 30
    # --- 턴 위임 (P53 4절). 켜지면 사람이 턴을 시작하거나 이어 갈 때 Tool 대상마다 위임 토큰을
    # 만든다. 기본은 켜짐이다(B3, 8절). 끄면 새 위임을 열지 않고 갱신은 410 이라 열린 위임이
    # 토큰 수명 안에 모두 멈춘다 ---
    turn_tokens_enabled: bool = True
    turn_token_ttl_seconds: int = 300
    # oidc 모드의 대화형 한도(초, P53b 6.3절). 채팅, 결정, 다시 생성의 위임은 그 사람의 마지막 IdP
    # 확인(`idp_people` 의 confirmed_at)에서 이 한도까지 토큰 한 수명이 남아야 열리고 갱신된다. 한
    # 시간짜리 IdP 토큰을 UI 가 만료 60초 전에 갱신하므로 확인의 나이가 3540초까지 간다. 기본값은
    # 그것과 토큰 한 수명, 숨은 탭의 늦은 타이머를 덮는다 (F0 2번)
    turn_owner_confirm_seconds: int = 4500
    # oidc 모드의 긴 한도(초, P53b 6.3절). 개인 토큰, 로그인 연결, 실행은 사람의 마지막 IdP 확인
    # (`idp_people` 의 confirmed_at)에서 이만큼까지만 쓸 수 있다. 기본은 7일이다
    idp_confirm_seconds: int = 7 * 86400
    # 서비스 어카운트를 만들고 그 토큰을 내는 사람 (P53b 6.7절). tenant-admin 이면 테넌트 관리자가,
    # platform-admin 이면 플랫폼 관리자만이다. off 면 아무도 못 내고 접근 정리가 있는 토큰을 모두
    # 회수한다
    service_accounts: str = SERVICE_ACCOUNTS_TENANT_ADMIN
    # oidc 모드의 데이터 플레인이 믿는 사람 토큰의 발급자 (P53b 6.1·6.2절, D104). 기본은 platform
    # 이라 데이터 플레인을 local 모드처럼 렌더한다. idp 면 IdP 를 믿고, 지울 때까지 되돌아갈 자리로
    # 남는다(P53b 12절 5번). 바꾸면 다음 재적용 틱부터 모든 소비자가 다시 렌더되고, 되돌려도
    # 아무것도 회수하지 않는다
    data_plane_issuer: str = DATA_PLANE_ISSUER_PLATFORM
    # env 가 아니라 기동이 정한다(`issuer_switch.held_settings`). platform 을 청했지만 저장된
    # mcp_group 이 다른 테넌트의 고정 그룹이나 그 밖의 `daegam-` 이름인 테넌트들이다. 있으면
    # 데이터 플레인은 idp 로 남는다 (P53b 6.10절)
    data_plane_issuer_held_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.auth_mode not in ("oidc", "local", "header"):
            raise ValueError(f"auth_mode must be oidc, local or header, got {self.auth_mode!r}")
        # 네 audience 가 같으면 한 토큰이 두 경계를 지난다. 사람의 데이터 플레인 토큰이 내부
        # MCPServer 를 열거나 제어면에 들어오거나 사용량을 써 넣는다 (D41, P37 8절)
        # LLM 게이트웨이의 audience 도 갈라 둔다. 그래야 사람·개인·OAuth·다른 서비스 토큰이
        # 게이트웨이에 들지 못하고 LLM 토큰은 다른 어디에도 들지 못한다 (P38 5절)
        if self.mcp_audience in (
            CONTROL_PLANE_AUDIENCE,
            TELEMETRY_AUDIENCE,
            LLM_AUDIENCE,
        ) or self.mcp_audience.startswith(INTERNAL_AUDIENCE_PREFIX):
            raise ValueError(
                f"mcp_audience {self.mcp_audience!r} collides with the control-plane, telemetry, "
                f"LLM or internal audience ({CONTROL_PLANE_AUDIENCE}, {TELEMETRY_AUDIENCE}, "
                f"{LLM_AUDIENCE}, {INTERNAL_AUDIENCE_PREFIX}<tenant>)"
            )
        # 주소는 늘 있다. 비면 게이트웨이 네임스페이스의 Service 다 (P38 3절)
        if not self.llm_gateway_url:
            url = default_llm_gateway_url(self.llm_gateway_namespace)
            object.__setattr__(self, "llm_gateway_url", url)
        check_llm_gateway(self)
        # oidc 모드는 두 클레임 이름을 Gateway·노출 앞단의 ToolHive 와 노출 Agent 의 런타임에도
        # 내린다(`ConsumerAuth`). local 은 DAEGAM_OIDC_* 를 읽지 않는다. 같은 ConfigMap 에 oidc 값이
        # 남아 있어도 기동은 되고 기동 로그가 그것을 말한다(_warn_about_open_settings). 모드 하나만
        # 바꿔 local 로 갈 수 있게 한다
        if self.auth_mode == "oidc":
            check_groups_claim(self.oidc_groups_claim)
            check_client_id_claim(self.oidc_client_id_claim)
        # 긴 한도가 대화형 한도보다 짧으면 실행과 개인 토큰이 채팅보다 먼저 끊긴다 (P53b 6.3절).
        # 두 한도는 oidc 모드에서만 쓰인다
        if self.auth_mode == "oidc" and self.idp_confirm_seconds < self.turn_owner_confirm_seconds:
            raise ValueError(
                f"DAEGAM_IDP_CONFIRM_SECONDS ({self.idp_confirm_seconds}) must not be below "
                f"DAEGAM_TURN_OWNER_CONFIRM_SECONDS ({self.turn_owner_confirm_seconds})"
            )
        if self.service_accounts not in SERVICE_ACCOUNTS_MODES:
            raise ValueError(
                f"DAEGAM_SERVICE_ACCOUNTS must be one of {', '.join(SERVICE_ACCOUNTS_MODES)}, "
                f"got {self.service_accounts!r}"
            )
        if self.data_plane_issuer not in DATA_PLANE_ISSUERS:
            raise ValueError(
                f"DAEGAM_DATA_PLANE_ISSUER must be one of {', '.join(DATA_PLANE_ISSUERS)}, "
                f"got {self.data_plane_issuer!r}"
            )
        # 플랫폼을 믿는 oidc 의 노출 Agent 는 플랫폼과 IdP 를 두 발급자로 받고, 런타임은 두 발급자가
        # 같으면 기동하지 않는다(duplicate_issuer). 그 Agent 들이 이유를 보이지 않고 재시작을
        # 되풀이하기 전에 플랫폼이 기동에서 막는다 (P53b 6.8절)
        if self.oidc_trusts_platform:
            idp = (self.oidc_issuer or "").rstrip("/")
            if self.platform_human_issuer.rstrip("/") == idp:
                raise ValueError(
                    "DAEGAM_DATA_PLANE_ISSUER=platform needs a platform issuer other than the "
                    "IdP's: the platform's (DAEGAM_PUBLIC_URL, else DAEGAM_PLATFORM_ISSUER) and "
                    f"DAEGAM_OIDC_ISSUER are both {idp!r}"
                )

    @property
    def platform_mints_human_tokens(self) -> bool:
        """플랫폼이 사람의 데이터 플레인 토큰(개인 토큰, 로그인 연결의 접근 토큰, 서비스 어카운트의
        토큰)을 내는가 (P53b 6.1절). local 모드와 oidc 모드다. 개인 토큰과 OAuth 인가 서버, 서비스
        어카운트, 접근 정리의 사람 발급자와 슬롯 pool 이 이것을 본다. 데이터 플레인이 그 토큰을
        믿는지는 `data_plane_trusts_platform` 이 따로 정한다. 토큰은 믿기 전에 낼 수 있어야 하기
        때문이다."""
        return self.token_auth

    @property
    def data_plane_trusts_platform(self) -> bool:
        """Gateway 의 vMCP, 노출 앞단, 노출 Agent 가 플랫폼이 낸 사람 토큰을 믿는가 (P53b 6.1절).
        local 모드와, 데이터 플레인 발급자가 platform 인 oidc 모드다. 그런 oidc 설치는 데이터
        플레인을 local 모드처럼 렌더한다. 소비자 렌더의 입력(`ConsumerAuth`), daegam-oidc, JWKS
        치유의 대상, Gateway 게이트, Agent 의 Gateway 인증, Tool 규칙의 컴파일이 이것을 본다."""
        return self.auth_mode == "local" or self.oidc_trusts_platform

    @property
    def idp_login(self) -> bool:
        """사람이 IdP 로 로그인하는 oidc 배포인가. 발급자 없는 oidc 는 header 로 돈다(`token_auth`).
        데이터 플레인 발급자를 고를 수 있는 것은 이 배포뿐이다."""
        return self.auth_mode == "oidc" and self.token_auth

    @property
    def oidc_trusts_platform(self) -> bool:
        """데이터 플레인이 플랫폼을 믿는 oidc 배포인가 (P53b 6.1절). DAEGAM_DATA_PLANE_ISSUER 가
        platform 이고 기동이 그것을 막지 않았다(`data_plane_issuer_held_by`). 노출 Agent 가 플랫폼
        곁에 IdP 를 두고, 전환의 순서와 상태가 이것을 본다."""
        return (
            self.idp_login
            and self.data_plane_issuer == DATA_PLANE_ISSUER_PLATFORM
            and not self.data_plane_issuer_held_by
        )

    @property
    def data_plane_trusts_idp(self) -> bool:
        """데이터 플레인이 IdP 의 토큰만 믿는가. 데이터 플레인 발급자가 idp 인 oidc 모드다. 그동안
        Gateway 규칙은 Agent 를 이름 댈 수 없고(`rule_agent_oidc`), Agent 는 Gateway 를
        `agent_oauth` 의 client credentials 로 부른다."""
        return self.token_auth and not self.data_plane_trusts_platform

    @property
    def data_plane_issuer_url(self) -> str | None:
        """데이터 플레인의 사람 토큰 검증기(daegam-oidc, 노출 Agent)가 믿는 발급자. 플랫폼을 믿으면
        플랫폼의 사람 발급자(`platform_human_issuer`)이고, 아니면 `issuer`(oidc 는 IdP, header 는
        None)다. 제어면은 이것이 아니라 `issuer` 를 본다."""
        if self.data_plane_trusts_platform:
            return self.platform_human_issuer
        return self.issuer

    @property
    def interactive_bound(self) -> timedelta:
        """oidc 모드의 대화형 한도 (P53b 6.3절, `turn_owner_confirm_seconds`)."""
        return timedelta(seconds=self.turn_owner_confirm_seconds)

    @property
    def idp_confirm_bound(self) -> timedelta:
        """oidc 모드의 긴 한도 (P53b 6.3절, `idp_confirm_seconds`)."""
        return timedelta(seconds=self.idp_confirm_seconds)

    @property
    def token_auth(self) -> bool:
        """이 배포본이 발급자 있는 토큰 인증으로 도는가. local 과 issuer 있는 oidc 에서 참이다.

        `auth_mode` 만 보면 안 된다. oidc 인데 issuer 가 없으면 검증기를 만들 수 없어 header 로 도는
        것과 같다(wiring.py 가 그때 RuntimeError 다). 노출 허용·MCPOIDCConfig·Gateway 인증 경계·
        에이전트 env 가 모두 이 하나를 본다."""
        if self.auth_mode == "local":
            return True
        return self.auth_mode == "oidc" and bool(self.oidc_issuer)

    @property
    def issuer(self) -> str | None:
        """사람 토큰의 `iss`. local 은 `platform_human_issuer`, oidc 는 IdP, header 는 None 이다. 이
        주소에서 디스커버리를 읽는 것은 oidc 모드(IdP)뿐이고, local 에서 플랫폼이 서명한 사람 토큰은
        데이터 플레인이 `human_jwks_url` 에서 받은 키로 검증한다 (P32 1절)."""
        if self.auth_mode == "local":
            return self.platform_human_issuer
        return self.oidc_issuer if self.auth_mode == "oidc" else None

    @property
    def platform_human_issuer(self) -> str:
        """플랫폼이 서명하는 사람 토큰의 `iss`. 플랫폼이 사람 토큰을 내는 모드(local, oidc)는 공개
        주소이고 비었으면 서비스 발급자다. header 모드는 서비스 발급자다. 디스커버리 문서의
        issuer 가 이것이다. 서비스 토큰은 공개 주소가 바뀌어도 살아남도록 서비스 발급자를 쓴다
        (P32 1절). oidc 모드도 같은 값이라 데이터 플레인 발급자를 바꾸어도 이미 낸 토큰의 발급자는
        그대로다 (P53b 6.1절)."""
        if self.platform_mints_human_tokens and self.public_url:
            return self.public_url
        return self.platform_issuer_url

    @property
    def platform_issuer_url(self) -> str:
        """플랫폼 자신이 발급자일 때의 서비스 발급자 `iss`. 서비스 토큰(모든 모드)이 쓰고, 공개
        주소가 없는 local 의 사람 토큰도 쓴다."""
        return (self.platform_issuer or self.home_platform_internal_url()).rstrip("/")

    @property
    def cluster_local_platform_issuer(self) -> bool:
        """플랫폼 발급자가 홈 클러스터 안에서만 풀리는 주소(svc·host.internal)인가. 원격 클러스터의
        내부 MCPServer 는 그 주소에서 JWKS 를 못 받는다 (D41)."""
        host = urlsplit(self.platform_issuer_url).hostname or ""
        return host.endswith(".svc") or ".svc." in host or host in ("host.internal", "localhost")

    @property
    def insecure_platform_issuer(self) -> bool:
        """플랫폼 발급자가 평문 http 인가. 두 JWKS 주소가 그 아래에 있으므로 내부 MCPServer 의
        MCPOIDCConfig 두 플래그가 이것 하나를 따른다 (P32 1절)."""
        return self.platform_issuer_url.startswith("http://")

    @property
    def insecure_issuer(self) -> bool:
        """사람 토큰의 발급자가 평문 http 인가. local 은 주소 스킴으로, oidc 는 명시 설정으로 안다.
        검증기에 붙는 평문 플래그는 JWKS 주소까지 보는 `insecure_human_http` 다."""
        if self.auth_mode == "local":
            return (self.issuer or "").startswith("http://")
        return self.auth_mode == "oidc" and self.oidc_insecure_http

    @property
    def insecure_public_url(self) -> bool:
        """공개 주소가 평문 http 인가. 로컬 벌만 그렇고 기동 로그가 경고한다 (D58)."""
        return (self.public_url or "").startswith("http://")

    @property
    def oauth_off_reason(self) -> str | None:
        """OAuth 인가 서버가 꺼진 까닭. 켜졌거나 잠들었으면 None 이다 (P33 1절, P53b 6.5절). OAuth
        2.1 과 MCP 명세가 인가 서버에 TLS 를 요구하므로 공개 주소가 https 여야 하고, 평문은 허용
        플래그로만 연다. 꺼진 서버의 연결은 접근 정리가 `oauth_disabled` 로 회수한다."""
        if not self.platform_mints_human_tokens:
            return f"auth_mode is {self.auth_mode}, where the platform issues no person tokens"
        if not self.oauth_switch:
            return "DAEGAM_OAUTH_ENABLED is false"
        if not self.public_url:
            return "DAEGAM_PUBLIC_URL is not set"
        if self.insecure_public_url and not self.oauth_allow_http:
            return "DAEGAM_PUBLIC_URL is plain http and DAEGAM_OAUTH_ALLOW_HTTP is not true"
        return None

    @property
    def oauth_dormant(self) -> bool:
        """OAuth 인가 서버가 잠들었는가 (P53b 6.5절). 꺼지지 않았지만 데이터 플레인이 플랫폼의
        토큰을 아직 믿지 않는 oidc 모드다. 잠든 서버는 새 연결과 갱신에 아무것도 내지 않고, 접근
        정리는 그 연결을 회수하지 않는다. 데이터 플레인 발급자를 IdP 로 되돌려도 연결이 끊기지 않게
        하려는 것이다."""
        return self.oauth_off_reason is None and not self.data_plane_trusts_platform

    @property
    def oauth_enabled(self) -> bool:
        """인가 서버가 일한다. 플랫폼이 사람 토큰을 내고, 공개 주소가 있고, 그 주소가 https 이거나
        평문 허용 플래그가 참이고, 스위치가 꺼지지 않았고, 잠들지 않았다. 아니면 OAuth 엔드포인트는
        모두 404 다."""
        return self.oauth_off_reason is None and not self.oauth_dormant

    @property
    def platform_jwks_url(self) -> str:
        """플랫폼 JWKS 의 주소. 서비스 발급자 아래라 모든 검증기가 닿는다. 한 클러스터 배치에서는
        클러스터 안 svc 주소다 (P32 1절)."""
        return jwks_url(self.platform_issuer_url)

    @property
    def service_jwks_url(self) -> str:
        """서비스 토큰용 JWKS. 같은 경로에 `?use=service` 이고 daegam-internal-oidc 가 받는다."""
        return jwks_url(self.platform_issuer_url, service=True)

    @property
    def human_jwks_url(self) -> str | None:
        """사람 토큰 검증기(daegam-oidc, 노출 에이전트)가 JWKS 를 받는 주소. 데이터 플레인이
        플랫폼을 믿으면(local, platform 인 oidc) platform_jwks_url 이고, 그 밖에는 없다. IdP 를 믿는
        oidc 는 IdP 디스커버리를 쓴다."""
        return self.platform_jwks_url if self.data_plane_trusts_platform else None

    @property
    def insecure_human_http(self) -> bool:
        """사람 토큰 검증기에 평문 플래그(MCPOIDCConfig 의 두 필드, 런타임의
        DAEGAM_OIDC_INSECURE_HTTP)를 붙이는가. 플랫폼을 믿으면 그 발급자나 JWKS 주소 가운데 하나라도
        http 일 때 붙는다. IdP 를 믿는 oidc 는 JWKS 주소가 없으므로 명시 설정만 본다 (P32 1절)."""
        if self.data_plane_trusts_platform:
            urls = (self.data_plane_issuer_url, self.human_jwks_url)
            return any((u or "").startswith("http://") for u in urls)
        return self.insecure_issuer

    @property
    def ui_client_id(self) -> str | None:
        """MCPOIDCConfig 에 적는 client_id. local 에는 클라이언트 개념이 없다."""
        return self.oidc_client_id if self.auth_mode == "oidc" else None

    # --- LLM provider (D25) ---
    # kind: stub provider 등록 허용. e2e·로컬 실증용 배포본에서만 켠다 (DAEGAM_ALLOW_STUB_PROVIDER)
    allow_stub_provider: bool = False
    # --- Agent 런타임 (D46) ---
    # 닫힌 대화의 기록이 남는 날 수. Agent 파드 env DAEGAM_TRANSCRIPT_RETENTION_DAYS 로 내려간다.
    # 0 이면 세션이 닫힐 때 기록도 같이 지운다
    transcript_retention_days: int = 30
    # 파드가 내려갈 때 Agent 런타임이 도는 턴을 기다리는 초 (P78 5절). provision 이 Agent 문서에
    # 남겨 파드 env DAEGAM_TURN_DRAIN_SECONDS 와 grace 로 렌더한다. 0 이면 기다리지 않는다
    agent_turn_drain_seconds: int = 120
    # --- 쓰지 않는 Agent 를 0 으로 줄이기 (P44 14절, D103). 다섯 다 ConfigMap 으로만 받는다 ---
    # 플랫폼 스위치. 끄면 어떤 Agent 도 잠들지 않고 잠든 Agent 는 다음 틱에 모두 깬다(되돌리기 단계)
    agent_scale_to_zero: bool = True
    # 쉬는 창(초). 이만큼 턴이 없으면 잠든다. 0 이면 스스로 잠들지 않고 `POST /sleep` 과 매일
    # 깨우기만 돈다. 0 이 아니면 적어도 `AGENT_SLEEP_MIN_SECONDS` 다
    agent_sleep_after_seconds: int = 0
    # 이만큼 잠든 Agent 를 매일 깨워 런타임의 보존 삭제와 기억 만료를 돌린다(시간)
    agent_housekeeping_hours: float = 24.0
    # 클러스터마다 함께 매일 깨우는 Agent 수
    agent_housekeeping_parallel: int = 2
    # 매일 깨우기가 파드가 준비된 뒤 Agent 를 깨워 두는 초(hold). 런타임의 첫 정리가 이 안에 끝난다
    agent_housekeeping_hold_seconds: int = 600
    # 쉬기 워커의 주기(초). 환경 변수는 없고 테스트만 바꾼다
    agent_sleep_interval: float = 30.0
    # --- Agent 미리 보기 (P44b 10절·12절, D107) ---
    # 스위치. 끄면 새 미리 보기를 받지 않고 끝내기 워커가 다음 틱에 열린 것을 모두 끝낸다
    # (되돌리기 단계)
    agent_previews_enabled: bool = True
    # 테넌트마다 함께 열 수 있는 미리 보기 수. 0 이면 열 수 없다
    agent_previews_per_tenant: int = 3
    # 끝내기 워커의 주기(초). 환경 변수는 없고 테스트만 바꾼다
    agent_preview_interval: float = 30.0
    # 답변 피드백(P36)이 남는 날 수. 고칠 때마다 다시 잰다. 1 아래로는 내리지 않는다
    feedback_retention_days: int = 180
    # --- Agent 평가 (P45 13절, D109) ---
    # 스위치. 끄면 새 실행을 받지 않고, 도는 실행을 틱 하나 안에 그 미리 보기와 함께 끝낸다
    # (되돌리기 단계)
    evaluations_enabled: bool = True
    # 실행 하나의 원장 상한. 사례는 그 80% 에서 멈추고 나머지는 채점 몫이다
    evaluation_run_tokens: int = 1_000_000
    # 테넌트가 UTC 하루에 시작할 수 있는 실행 수. 미리 보기가 준비된 실행만 센다
    evaluation_runs_per_day: int = 10
    # 실행 결과 문서가 남는 날 수. 전사 보존 기간을 넘지 않고 1 아래로는 내리지 않는다
    evaluation_result_days: int = 30
    # 배포 게이트가 채점 기준을 세는가. 채점 모델의 일치를 잰 F0.4 뒤 B6 이 켠다. 끈 동안 기준의
    # 판정은 보여 주기만 하고 게이트는 확인 항목만 센다 (11절)
    evaluation_gate_criteria: bool = False
    # 실행 워커의 주기(초). 환경 변수는 없고 테스트만 바꾼다
    evaluation_interval: float = 10.0
    # --- Spec Generator (P2 F10) ---
    spec_job_input_max_bytes: int = 256 * 1024
    spec_job_max_active_per_tenant: int = 2
    spec_job_ttl_seconds: int = 7 * 24 * 3600  # done/failed 문서의 TTL
    spec_job_lease_seconds: float = 90.0
    spec_worker_interval: float = 5.0
    spec_worker_enabled: bool = True
    # --- Knowledge 인덱서 (D32). Spec 워커와 같은 스위치다 ---
    knowledge_indexer_enabled: bool = True
    knowledge_indexer_interval: float = 5.0
    knowledge_lease_seconds: float = 90.0
    # --- 백그라운드 실행 (P41, D94). 기본으로 켠다. 끄면 실행·스케줄·트리거 라우트와 웹훅이
    # 404 이고 워커가 돌지 않으며, 화면은 자동 실행 탭과 계정의 목록을 감춘다 ---
    runs_enabled: bool = True
    # 실행 워커의 스위치. runs_enabled 일 때만 뜻이 있다. 환경 변수는 없고 테스트만 끈다
    runs_workers_enabled: bool = True
    runs_interval: float = 15.0
    runs_lease_seconds: float = 60.0
    # --- 바깥 채널 (P47 6.1절). 끄면 Teams 의 메시지 엔드포인트와 모든 채널 라우트가 404 이고 채널
    # 워커가 서며, 연결과 설정은 남는다. header 모드는 켜도 404 다 ---
    channels_enabled: bool = True
    # --- 문서 파싱 서비스 daegam-docparse (D61). 주소를 비우면 services/docparse.py 가 정하고
    # 토큰은 Secret daegam-docparse-token 에서 온다. 토큰이 없으면 텍스트 형식만 뽑는다 ---
    docparse_url: str | None = None
    docparse_token: str | None = field(default=None, repr=False)
    # --- 감사 (P6 F5) ---
    audit_ttl_days: int = 90  # audit_events / mcp_audit_events TTL
    # Agent 런타임 관측 이벤트(agent_events)의 보관 (P37 5절). 스팬 때문에 턴마다 문서가 서너 배라
    # 전사 보관과 같게 두고, 토큰과 금액은 원장에 남는다
    agent_events_ttl_days: int = 30
    # 런타임 이벤트 수집 경로 (P37 17절). 켜져 있으면 Agent 렌더가 `DAEGAM_EVENTS_URL` 을 늘 싣고,
    # 꺼지면 싣지 않으며 수집 경로는 404 다. 그때 다시 배포한 Agent 는 stdout 에만 쓴다
    telemetry_ingest_enabled: bool = True
    audit_collector_enabled: bool = True  # MCP 계층 pods/log 수집 워커
    audit_collector_interval: float = 60.0
    # Gateway·MCP Server 메트릭 롤업이 감사 이벤트를 다시 세는 주기(초) (P51 3절)
    mcp_metrics_rollup_seconds: float = 120.0
    # --- 원장 (P37 10절). 원장 행과 시간 묶음이 남는 날 수. 13개월이라 한 달을 한 해 전 같은 달과
    # 견준다
    usage_retention_days: int = 400
    # 기억한 승인 선택이 남는 날 수 (P43 5절). 1 에서 365 까지이고 넘으면 그 끝으로 붙인다
    approval_choice_days: int = 30
    # 롤업이 지금·앞 시간을 다시 세는 주기(초) (P37 10절)
    usage_rollup_seconds: float = 120.0
    # LLM 게이트웨이 (P38 3절·12절). 스위치 DAEGAM_LLM_GATEWAY 는 off·agentgateway 이고 경로 결정만
    # 먹인다. 네임스페이스는 켜든 끄든 테넌트가 쓰지 못한다(`reserved_namespaces`)
    llm_gateway: str = LLM_GATEWAY_OFF
    llm_gateway_namespace: str = DEFAULT_LLM_GATEWAY_NAMESPACE
    # 게이트웨이의 주소 하나 (P37 17절). 플랫폼 호출은 provider 의 base_url 이 이 아래인지
    # (`under_base`)로 경로를 정한다. 비면 게이트웨이 네임스페이스의 Service 주소다
    # (`default_llm_gateway_url`). 끈 뒤 아직 옮기지 않은 Agent 의 파드가 다시 떠도 게이트웨이
    # base_url 을 받을 수 있게 늘 있다
    llm_gateway_url: str | None = None
    # 게이트웨이 한 벌의 이미지 넷 (store/llm_gateway.py `LlmGatewayImages` 꼴). 켰으면 있어야 한다
    llm_gateway_images: dict[str, Any] = field(default_factory=dict)
    # 게이트웨이의 PodDisruptionBudget(minAvailable 1). 노드가 하나인 클러스터는 끈다 (D54)
    llm_gateway_pdb: bool = False
    # --- home 클러스터의 노출 기본값 (D22). DAEGAM_HOME_INGRESS_* 가 있으면 기동마다 home 문서의
    # ingress 에 덮어쓴다. make deploy-local 은 traefik / 컨트롤러 네임스페이스 traefik / 무인증
    # 노출 허용 / deploy/dev/values-local.yaml 의 host 를 준다. 비어 있으면 화면에서 고친 값이
    # 그대로다
    home_ingress: dict[str, Any] = field(default_factory=dict)
    # --- home 클러스터의 이미지·자원·LimitRange (D54). 같은 규칙으로 기동마다 덮어쓴다. 비어 있으면
    # 화면에서 고친 값이 그대로다. 자원과 LimitRange 는 이미 검증된 값이다(_home_json_from_env)
    home_images: dict[str, Any] = field(default_factory=dict)
    home_resources: dict[str, Any] | None = None
    home_limit_range: dict[str, Any] | None = None
    # home 의 NetworkPolicy 집행 판정을 못 박는다(enforced·unenforced). 비우면 preflight 가 탐지한다
    home_network_policy: str | None = None
    # 배포가 함께 올린 Redis 의 TLS 끝점 (P61 5절 3단계, DAEGAM_HOME_REDIS_TLS). 있으면 기동 때 그
    # Redis 를 가리키는 home 의 기록에 TLS 끝점을 더한다(`services/shipped_redis.py`)
    home_redis_tls: ShippedRedisTls | None = None
    # 플랫폼 web_fetch 복제본의 이미지 (P39b 13절, DAEGAM_WEB_TOOLS_IMAGE). 배포가 digest 로 못
    # 박는다(scripts/resolve-images.sh). 비우면 Platform Settings 의 default_web_tools_image 다.
    # Cluster 문서와 배포 스냅샷에는 칸을 더하지 않는다. 그 모델은 옛 판이 모르는 칸을 거절해
    # 되돌리기가 깨지기 때문이다
    web_tools_image: str | None = None
    # home 클러스터의 샌드박스 이미지 (P40 10절, DAEGAM_HOME_SANDBOX_IMAGE). 배포가 digest 로 못
    # 박고 기동마다 home 의 `sandbox.image` 에 덮어쓴다. `DAEGAM_HOME_IMAGES` 의 키가
    # 아닌 것은 앞의 판이 그 JSON 을 `ClusterImages` 로 엄격히 검증해 모르는 키에 기동을 거절하기
    # 때문이다. 앞의 판은 이 env 를 모르고 지나간다
    home_sandbox_image: str | None = None
    # home 클러스터가 테넌트 파드를 인스턴스 메타데이터 주소에서 막는가 (T75,
    # DAEGAM_HOME_BLOCK_METADATA). 있으면 기동마다 home 의 `block_metadata` 에 덮어쓴다. 비우면
    # 화면에서 고친 값이 그대로다. 노드가 그 주소에서 메타데이터를 내주는 클라우드 VM 의 설치는 켠다
    home_block_metadata: bool | None = None
    # --- 코드 실행 샌드박스 (P40 7절·8절) ---
    # 한 번에 열 수 있는 샌드박스의 수. 사람마다(실행은 빼고), Agent 마다(그 가운데 P41 실행의
    # 몫), 테넌트마다, 클러스터마다다. 클러스터의 수는 메모리 규칙이 정한 용량과 이 값 가운데 작은
    # 것이다
    sandbox_per_person: int = 2
    sandbox_per_agent: int = 5
    sandbox_runs_per_agent: int = 2
    sandbox_per_tenant: int = 10
    sandbox_max: int = 20
    # 노드마다 남기는 메모리(바이트, DAEGAM_SANDBOX_NODE_RESERVE)와 작업 공간의 크기(바이트,
    # DAEGAM_SANDBOX_WORKSPACE). 샌드박스 파드의 메모리 요청과 한도는 작업 공간에 1 GiB 를 더한
    # 값이다
    sandbox_node_reserve: int = 2 * 1024**3
    sandbox_workspace: int = 1024**3
    # 개발용 우회 (P40 8절). 참이면 home 클러스터에서만 점검 없이 샌드박스를 연다. 기록은
    # `unchecked` 이고 자체 점검은 끝나지 않고 알리기만 한다. 로컬 차트 값
    # deploy/dev/values-local.yaml 만 켜고 차트와 릴리스 산출물에는 두지 않는다
    # (test_sandbox_gate.py)
    sandbox_allow_unchecked: bool = False
    # 샌드박스 스윕의 주기(초)
    sandbox_sweep_seconds: float = 60.0

    @property
    def llm_gateway_on(self) -> bool:
        """게이트웨이 스위치가 켜졌는가. 경로 결정만 이것을 본다 (P38 6절)."""
        return self.llm_gateway != LLM_GATEWAY_OFF

    @property
    def reserved_namespaces(self) -> frozenset[str]:
        """이 설치가 테넌트에게 주지 않는 네임스페이스. 플랫폼과 LLM 게이트웨이의 것이다.
        게이트웨이를 켜든 끄든 막는다. 템플릿이 만든 테넌트 네임스페이스(`daegam-llm`)가 모든 키
        옆에 놓이지 않게 한다 (P38 3절)."""
        return frozenset({self.namespace, self.llm_gateway_namespace})

    @property
    def home_mode(self) -> str | None:
        """home 클러스터 접속 방식. in_cluster | kubecontext | None(자동 등록 안 함)."""
        if self.in_cluster:
            return "in_cluster"
        if self.home_kubecontext:
            return "kubecontext"
        return None

    def home_platform_internal_url(self) -> str:
        if self.platform_internal_url:
            return self.platform_internal_url
        if self.in_cluster:
            return f"http://daegam-platform.{self.namespace}.svc:{self.platform_port}"
        return f"http://host.internal:{self.platform_port}"


def _home_ingress_from_env() -> dict[str, Any]:
    """DAEGAM_HOME_INGRESS_{CLASS,CONTROLLER_NAMESPACE,DOMAIN,RESERVED_HOSTS,ALLOW_UNAUTHENTICATED}.
    하나라도 있으면 enabled 도 켠다 (DAEGAM_HOME_INGRESS_ENABLED 로 명시 가능)."""
    out: dict[str, Any] = {}
    if v := os.environ.get("DAEGAM_HOME_INGRESS_CLASS"):
        out["class_name"] = v.strip()
    if v := os.environ.get("DAEGAM_HOME_INGRESS_CONTROLLER_NAMESPACE"):
        out["controller_namespace"] = v.strip()
    if v := os.environ.get("DAEGAM_HOME_INGRESS_DOMAIN"):
        out["domain"] = v.strip()
    if v := os.environ.get("DAEGAM_HOME_INGRESS_RESERVED_HOSTS"):
        out["reserved_hosts"] = [h.strip() for h in v.split(",") if h.strip()]
    if os.environ.get("DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED") is not None:
        out["allow_unauthenticated"] = _env_bool("DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED")
    enabling = dict(out)  # TLS 기본값만 준 배포가 노출을 켜지 않게 tls 는 세지 않는다
    if v := os.environ.get("DAEGAM_HOME_INGRESS_TLS"):
        if v.strip() not in ("none", "secret", "cert-manager", "edge"):
            raise ValueError(
                f"DAEGAM_HOME_INGRESS_TLS must be none, secret, cert-manager or edge, got {v!r}"
            )
        out["tls"] = v.strip()
    if os.environ.get("DAEGAM_HOME_INGRESS_ENABLED") is not None:
        out["enabled"] = _env_bool("DAEGAM_HOME_INGRESS_ENABLED")
    elif enabling:
        out["enabled"] = True
    return out


def _home_json_from_env(name: str) -> Any:
    """JSON 환경 변수. 없거나 비면 None 이고 JSON 이 아니면 기동을 거절한다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{name} is not valid JSON: {e}") from e


def _home_images_from_env() -> dict[str, Any]:
    """DAEGAM_HOME_IMAGES. ClusterImages 의 필드 일부를 담은 JSON 이고, 준 필드만 덮어쓴다."""
    from pydantic import ValidationError

    from daegam.store.clusters import ClusterImages

    raw = _home_json_from_env("DAEGAM_HOME_IMAGES")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("DAEGAM_HOME_IMAGES must be a JSON object")
    try:
        ClusterImages.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"DAEGAM_HOME_IMAGES: {e}") from e
    return raw


def _llm_gateway_images_from_env() -> dict[str, Any]:
    """DAEGAM_LLM_GATEWAY_IMAGES. `LlmGatewayImages` 꼴의 JSON 이고 틀리면 기동을 거절한다."""
    from pydantic import ValidationError

    from daegam.store.llm_gateway import LlmGatewayImages

    raw = _home_json_from_env("DAEGAM_LLM_GATEWAY_IMAGES")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("DAEGAM_LLM_GATEWAY_IMAGES must be a JSON object")
    try:
        LlmGatewayImages.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"DAEGAM_LLM_GATEWAY_IMAGES: {e}") from e
    return raw


def _home_resources_from_env() -> dict[str, Any] | None:
    """DAEGAM_HOME_RESOURCES. 런타임·에이전트 본 컨테이너의 기본 자원(D54). 틀리면 기동을
    거절한다."""
    from daegam.k8s.resources import validate_complete_resources

    raw = _home_json_from_env("DAEGAM_HOME_RESOURCES")
    if raw is None:
        return None
    return validate_complete_resources(raw, where="DAEGAM_HOME_RESOURCES")


def _home_limit_range_from_env() -> dict[str, Any] | None:
    """DAEGAM_HOME_LIMIT_RANGE. 테넌트 네임스페이스에 까는 LimitRange 의 spec(D54)."""
    from daegam.k8s.resources import validate_limit_range

    raw = _home_json_from_env("DAEGAM_HOME_LIMIT_RANGE")
    if raw is None:
        return None
    return validate_limit_range(raw, where="DAEGAM_HOME_LIMIT_RANGE")


def redis_host_key(host: str) -> str:
    """Redis 주소의 호스트를 견줄 꼴. 대소문자와 끝의 점을 가리지 않는다. 같은 Redis 를 ClusterIP 나
    다른 별칭으로 적은 기록은 이것으로도 가리지 못한다."""
    return host.strip().lower().rstrip(".")


@dataclass(frozen=True)
class ShippedRedisTls:
    """DAEGAM_HOME_REDIS_TLS. hosts 는 그 Redis 를 부르는 이름들, port 는 평문 포트, tls_port 는 TLS
    포트, ca_configmap 은 플랫폼 네임스페이스에서 공개 인증서 `ca.crt`·`tls.crt` 를 담은 ConfigMap
    이다. 개인 키가 플랫폼을 지나지 않게 Secret 이 아니라 ConfigMap 을 읽는다. plain 은 평문 포트가
    열려 있는가다 (P61 5절 5단계). 그 Redis 를 두는 배포가 닫고 적는다."""

    hosts: tuple[str, ...]
    port: int
    tls_port: int
    ca_configmap: str
    plain: bool = True

    def serves(self, host: str, port: int | None) -> bool:
        """그 주소가 이 Redis 의 평문 주소인가(`redis_host_key` 로 견준다)."""
        return port == self.port and redis_host_key(host) in self.hosts


def _home_redis_tls_from_env() -> ShippedRedisTls | None:
    raw = _home_json_from_env("DAEGAM_HOME_REDIS_TLS")
    if raw is None:
        return None
    try:
        hosts = tuple(redis_host_key(str(h)) for h in raw["hosts"] if redis_host_key(str(h)))
        plain = raw["plain"]
        if not isinstance(plain, bool):
            raise TypeError("plain must be true or false")
        out = ShippedRedisTls(
            hosts, int(raw["port"]), int(raw["tls_port"]), str(raw["ca_configmap"]), plain
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            "DAEGAM_HOME_REDIS_TLS must be a JSON object with hosts, port, tls_port, ca_configmap "
            "and a boolean plain"
        ) from e
    if not out.hosts or not out.ca_configmap or out.port == out.tls_port:
        raise ValueError(
            "DAEGAM_HOME_REDIS_TLS needs hosts, a ca_configmap and two different ports"
        )
    return out


def _home_block_metadata_from_env() -> bool | None:
    raw = (os.environ.get("DAEGAM_HOME_BLOCK_METADATA") or "").strip().lower()
    if not raw:
        return None
    if raw not in ("true", "false"):
        raise ValueError(f"DAEGAM_HOME_BLOCK_METADATA must be true or false, got {raw!r}")
    return raw == "true"


def _home_network_policy_from_env() -> str | None:
    raw = (os.environ.get("DAEGAM_HOME_NETWORK_POLICY") or "").strip().lower()
    if not raw:
        return None
    if raw not in ("enforced", "unenforced"):
        raise ValueError(f"DAEGAM_HOME_NETWORK_POLICY must be enforced or unenforced, got {raw!r}")
    return raw


def _public_url_from_env() -> str | None:
    """DAEGAM_PUBLIC_URL. 앞뒤 ASCII 공백과 끝의 / 를 지우고 소문자로 맞춘 origin 이다(D58).

    CSP 가 IdP 를 여는 꼴(origin_of)만 받고, 경로·쿼리·조각이 남으면 기동을 거절한다. 이 값은
    토큰의 iss 와 헤더·문서에 글자 그대로 실린다."""
    raw = (os.environ.get("DAEGAM_PUBLIC_URL") or "").strip(ENV_WHITESPACE).rstrip("/")
    if not raw:
        return None
    origin = origin_of(raw)
    if origin is None or origin.lower() != raw.lower():
        raise ValueError(
            "DAEGAM_PUBLIC_URL must be an http(s)://host[:port] origin without path, query or "
            f"fragment, got {raw!r}"
        )
    return origin.lower()


def _cimd_clients_from_env() -> tuple[str, ...]:
    """DAEGAM_OAUTH_CIMD_CLIENTS. 콤마로 이은 정확한 https URL 목록이다 (P33 4절). 없으면 기본
    셋이고 비면 CIMD 를 끈다. https URL 이 아닌 값이 있으면 기동을 거절한다."""
    raw = os.environ.get("DAEGAM_OAUTH_CIMD_CLIENTS")
    if raw is None:
        return OAUTH_CIMD_DEFAULT_CLIENTS
    urls = tuple(u.strip(ENV_WHITESPACE) for u in raw.split(",") if u.strip(ENV_WHITESPACE))
    bad = [u for u in urls if not (u.startswith("https://") and is_http_url(u))]
    if bad:
        raise ValueError(f"DAEGAM_OAUTH_CIMD_CLIENTS must list https URLs, got {bad!r}")
    return urls


# Settings 의 기본값. settings_from_env 는 환경 변수가 없을 때 여기서 읽는다. 기본값을 두 곳에
# 적으면 한쪽만 고쳤을 때 env 없는 배포와 Settings() 를 쓰는 테스트가 서로 다른 값을 쓴다
_DEFAULTS: dict[str, Any] = {
    f.name: f.default for f in dataclasses.fields(Settings) if f.default is not dataclasses.MISSING
}


def _evaluation_result_days(transcript_days: int) -> int:
    """평가 결과 문서가 남는 날 수 (P45 9절). 전사 보존 기간을 넘지 않고 1일 아래로 내리지 않는다.
    전사 보존이 0(세션과 함께 지운다)이어도 결과는 하루 남는다."""
    days = _env_int("DAEGAM_EVALUATION_RESULT_DAYS", _DEFAULTS["evaluation_result_days"])
    return max(1, min(days, transcript_days))


def settings_from_env() -> Settings:
    transcript_days = max(
        0, _env_int("DAEGAM_TRANSCRIPT_RETENTION_DAYS", _DEFAULTS["transcript_retention_days"])
    )
    return Settings(
        mongo_url=os.environ.get("DAEGAM_MONGO_URL", _DEFAULTS["mongo_url"]),
        mongo_db=os.environ.get("DAEGAM_MONGO_DB", _DEFAULTS["mongo_db"]),
        tenant_header=os.environ.get("DAEGAM_TENANT_HEADER", _DEFAULTS["tenant_header"]),
        default_tenant=os.environ.get("DAEGAM_DEFAULT_TENANT", _DEFAULTS["default_tenant"]),
        namespace=os.environ.get("DAEGAM_NAMESPACE", _DEFAULTS["namespace"]),
        in_cluster=_env_bool("DAEGAM_IN_CLUSTER"),
        home_kubecontext=os.environ.get("DAEGAM_HOME_KUBECONTEXT") or None,
        platform_port=_env_int("DAEGAM_PLATFORM_PORT", _DEFAULTS["platform_port"]),
        platform_internal_url=os.environ.get("DAEGAM_PLATFORM_INTERNAL_URL") or None,
        web_tools_image=os.environ.get("DAEGAM_WEB_TOOLS_IMAGE") or None,
        reconcile_enabled=_env_bool("DAEGAM_RECONCILE_ENABLED", _DEFAULTS["reconcile_enabled"]),
        reconcile_interval=_env_float("DAEGAM_RECONCILE_INTERVAL", _DEFAULTS["reconcile_interval"]),
        auth_mode=os.environ.get("DAEGAM_AUTH_MODE", _DEFAULTS["auth_mode"])
        .strip(ENV_WHITESPACE)
        .lower(),
        # 앞뒤 공백도 지운다. UI 의 CSP(ui/docker/daegam-csp.sh)가 공백을 지우고 IdP 를 여는데 OIDC
        # 검증기가 공백 든 주소로 디스커버리를 받는 일이 없게 한다(D52)
        oidc_issuer=(os.environ.get("DAEGAM_OIDC_ISSUER") or "").strip(ENV_WHITESPACE).rstrip("/")
        or None,
        oidc_audience=os.environ.get("DAEGAM_OIDC_AUDIENCE") or None,
        oidc_client_id=os.environ.get("DAEGAM_OIDC_CLIENT_ID") or None,
        oidc_groups_claim=os.environ.get("DAEGAM_OIDC_GROUPS_CLAIM")
        or _DEFAULTS["oidc_groups_claim"],
        oidc_client_id_claim=os.environ.get("DAEGAM_OIDC_CLIENT_ID_CLAIM")
        or _DEFAULTS["oidc_client_id_claim"],
        mcp_audience=os.environ.get("DAEGAM_MCP_AUDIENCE") or _DEFAULTS["mcp_audience"],
        oidc_insecure_http=_env_bool("DAEGAM_OIDC_INSECURE_HTTP"),
        bootstrap_admin_group=os.environ.get("DAEGAM_BOOTSTRAP_ADMIN_GROUP") or None,
        platform_issuer=(os.environ.get("DAEGAM_PLATFORM_ISSUER") or "").rstrip("/") or None,
        public_url=_public_url_from_env(),
        local_token_ttl=_env_int("DAEGAM_LOCAL_TOKEN_TTL", _DEFAULTS["local_token_ttl"]),
        local_bootstrap_username=(
            os.environ.get("DAEGAM_LOCAL_BOOTSTRAP_USERNAME")
            or _DEFAULTS["local_bootstrap_username"]
        )
        .strip()
        .lower(),
        local_bootstrap_password=os.environ.get("DAEGAM_LOCAL_BOOTSTRAP_PASSWORD") or None,
        pat_key_pool=_env_int_clamped(
            "DAEGAM_PAT_KEY_POOL", _DEFAULTS["pat_key_pool"], PAT_KEY_POOL_MIN, PAT_KEY_POOL_MAX
        ),
        pat_max_days=_lifetime_days("DAEGAM_PAT_MAX_DAYS", _DEFAULTS["pat_max_days"]),
        oauth_switch=_env_bool("DAEGAM_OAUTH_ENABLED", _DEFAULTS["oauth_switch"]),
        oauth_allow_http=_env_bool("DAEGAM_OAUTH_ALLOW_HTTP"),
        oauth_cimd_clients=_cimd_clients_from_env(),
        oauth_refresh_days=_lifetime_days(
            "DAEGAM_OAUTH_REFRESH_DAYS", _DEFAULTS["oauth_refresh_days"]
        ),
        turn_tokens_enabled=_env_bool(
            "DAEGAM_TURN_TOKENS_ENABLED", _DEFAULTS["turn_tokens_enabled"]
        ),
        turn_token_ttl_seconds=_env_int_clamped(
            "DAEGAM_TURN_TOKEN_TTL_SECONDS",
            _DEFAULTS["turn_token_ttl_seconds"],
            TURN_TOKEN_TTL_MIN,
            TURN_TOKEN_TTL_MAX,
        ),
        turn_owner_confirm_seconds=_env_int_clamped(
            "DAEGAM_TURN_OWNER_CONFIRM_SECONDS",
            _DEFAULTS["turn_owner_confirm_seconds"],
            TURN_OWNER_CONFIRM_MIN,
            TURN_OWNER_CONFIRM_MAX,
        ),
        idp_confirm_seconds=_env_int_clamped(
            "DAEGAM_IDP_CONFIRM_SECONDS",
            _DEFAULTS["idp_confirm_seconds"],
            TURN_OWNER_CONFIRM_MIN,
            IDP_CONFIRM_MAX,
            warn_above=True,
        ),
        service_accounts=(
            os.environ.get("DAEGAM_SERVICE_ACCOUNTS") or _DEFAULTS["service_accounts"]
        )
        .strip(ENV_WHITESPACE)
        .lower(),
        data_plane_issuer=(
            os.environ.get("DAEGAM_DATA_PLANE_ISSUER") or _DEFAULTS["data_plane_issuer"]
        )
        .strip(ENV_WHITESPACE)
        .lower(),
        allow_stub_provider=_env_bool("DAEGAM_ALLOW_STUB_PROVIDER"),
        transcript_retention_days=transcript_days,
        agent_turn_drain_seconds=max(
            0, _env_int("DAEGAM_AGENT_TURN_DRAIN_SECONDS", _DEFAULTS["agent_turn_drain_seconds"])
        ),
        agent_scale_to_zero=_env_bool(
            "DAEGAM_AGENT_SCALE_TO_ZERO", _DEFAULTS["agent_scale_to_zero"]
        ),
        agent_sleep_after_seconds=_sleep_window_from_env(),
        agent_housekeeping_hours=_env_float_clamped(
            "DAEGAM_AGENT_HOUSEKEEPING_HOURS",
            _DEFAULTS["agent_housekeeping_hours"],
            HOUSEKEEPING_HOURS_MIN,
            HOUSEKEEPING_HOURS_MAX,
        ),
        agent_housekeeping_parallel=_env_int_clamped(
            "DAEGAM_AGENT_HOUSEKEEPING_PARALLEL",
            _DEFAULTS["agent_housekeeping_parallel"],
            1,
            HOUSEKEEPING_PARALLEL_MAX,
        ),
        agent_housekeeping_hold_seconds=_env_int_clamped(
            "DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS",
            _DEFAULTS["agent_housekeeping_hold_seconds"],
            HOUSEKEEPING_HOLD_MIN_SECONDS,
            HOUSEKEEPING_HOLD_MAX_SECONDS,
        ),
        agent_previews_enabled=_env_bool(
            "DAEGAM_AGENT_PREVIEWS_ENABLED", _DEFAULTS["agent_previews_enabled"]
        ),
        agent_previews_per_tenant=_env_int_clamped(
            "DAEGAM_AGENT_PREVIEWS_PER_TENANT",
            _DEFAULTS["agent_previews_per_tenant"],
            0,
            PREVIEWS_PER_TENANT_MAX,
        ),
        feedback_retention_days=max(
            1, _env_int("DAEGAM_FEEDBACK_RETENTION_DAYS", _DEFAULTS["feedback_retention_days"])
        ),
        evaluations_enabled=_env_bool(
            "DAEGAM_EVALUATIONS_ENABLED", _DEFAULTS["evaluations_enabled"]
        ),
        evaluation_run_tokens=max(
            0, _env_int("DAEGAM_EVALUATION_RUN_TOKENS", _DEFAULTS["evaluation_run_tokens"])
        ),
        evaluation_runs_per_day=max(
            0, _env_int("DAEGAM_EVALUATION_RUNS_PER_DAY", _DEFAULTS["evaluation_runs_per_day"])
        ),
        evaluation_result_days=_evaluation_result_days(transcript_days),
        evaluation_gate_criteria=_env_bool(
            "DAEGAM_EVALUATION_GATE_CRITERIA", _DEFAULTS["evaluation_gate_criteria"]
        ),
        spec_worker_enabled=_env_bool(
            "DAEGAM_SPEC_WORKER_ENABLED", _DEFAULTS["spec_worker_enabled"]
        ),
        spec_worker_interval=_env_float(
            "DAEGAM_SPEC_WORKER_INTERVAL", _DEFAULTS["spec_worker_interval"]
        ),
        knowledge_indexer_enabled=_env_bool(
            "DAEGAM_KNOWLEDGE_INDEXER_ENABLED", _DEFAULTS["knowledge_indexer_enabled"]
        ),
        knowledge_indexer_interval=_env_float(
            "DAEGAM_KNOWLEDGE_INDEXER_INTERVAL", _DEFAULTS["knowledge_indexer_interval"]
        ),
        runs_enabled=_env_bool("DAEGAM_RUNS_ENABLED", _DEFAULTS["runs_enabled"]),
        runs_interval=_env_float("DAEGAM_RUNS_INTERVAL", _DEFAULTS["runs_interval"]),
        channels_enabled=_env_bool("DAEGAM_CHANNELS_ENABLED", _DEFAULTS["channels_enabled"]),
        docparse_url=os.environ.get("DAEGAM_DOCPARSE_URL") or None,
        docparse_token=(os.environ.get(DOCPARSE_TOKEN_ENV) or "").strip() or None,
        audit_ttl_days=_env_int("DAEGAM_AUDIT_TTL_DAYS", _DEFAULTS["audit_ttl_days"]),
        agent_events_ttl_days=max(
            1, _env_int("DAEGAM_AGENT_EVENTS_TTL_DAYS", _DEFAULTS["agent_events_ttl_days"])
        ),
        telemetry_ingest_enabled=_env_bool(
            "DAEGAM_TELEMETRY_INGEST_ENABLED", _DEFAULTS["telemetry_ingest_enabled"]
        ),
        audit_collector_enabled=_env_bool(
            "DAEGAM_AUDIT_COLLECTOR_ENABLED", _DEFAULTS["audit_collector_enabled"]
        ),
        audit_collector_interval=_env_float(
            "DAEGAM_AUDIT_COLLECTOR_INTERVAL", _DEFAULTS["audit_collector_interval"]
        ),
        mcp_metrics_rollup_seconds=max(
            5.0,
            _env_float(
                "DAEGAM_MCP_METRICS_ROLLUP_SECONDS", _DEFAULTS["mcp_metrics_rollup_seconds"]
            ),
        ),
        usage_retention_days=max(
            1, _env_int("DAEGAM_USAGE_RETENTION_DAYS", _DEFAULTS["usage_retention_days"])
        ),
        approval_choice_days=_lifetime_days(
            "DAEGAM_APPROVAL_CHOICE_DAYS", _DEFAULTS["approval_choice_days"]
        ),
        usage_rollup_seconds=max(
            5.0, _env_float("DAEGAM_USAGE_ROLLUP_SECONDS", _DEFAULTS["usage_rollup_seconds"])
        ),
        llm_gateway=(os.environ.get("DAEGAM_LLM_GATEWAY") or LLM_GATEWAY_OFF)
        .strip(ENV_WHITESPACE)
        .lower(),
        llm_gateway_namespace=(os.environ.get("DAEGAM_LLM_GATEWAY_NAMESPACE") or "").strip(
            ENV_WHITESPACE
        )
        or DEFAULT_LLM_GATEWAY_NAMESPACE,
        llm_gateway_url=(os.environ.get("DAEGAM_LLM_GATEWAY_URL") or "")
        .strip(ENV_WHITESPACE)
        .rstrip("/")
        or None,
        llm_gateway_images=_llm_gateway_images_from_env(),
        llm_gateway_pdb=_env_bool("DAEGAM_LLM_GATEWAY_PDB"),
        home_ingress=_home_ingress_from_env(),
        home_images=_home_images_from_env(),
        home_resources=_home_resources_from_env(),
        home_limit_range=_home_limit_range_from_env(),
        home_network_policy=_home_network_policy_from_env(),
        home_redis_tls=_home_redis_tls_from_env(),
        home_sandbox_image=(os.environ.get("DAEGAM_HOME_SANDBOX_IMAGE") or "").strip() or None,
        home_block_metadata=_home_block_metadata_from_env(),
        sandbox_per_person=_sandbox_cap("DAEGAM_SANDBOX_PER_PERSON", "sandbox_per_person"),
        sandbox_per_agent=_sandbox_cap("DAEGAM_SANDBOX_PER_AGENT", "sandbox_per_agent"),
        sandbox_runs_per_agent=_sandbox_cap(
            "DAEGAM_SANDBOX_RUNS_PER_AGENT", "sandbox_runs_per_agent"
        ),
        sandbox_per_tenant=_sandbox_cap("DAEGAM_SANDBOX_PER_TENANT", "sandbox_per_tenant"),
        sandbox_max=_sandbox_cap("DAEGAM_SANDBOX_MAX", "sandbox_max"),
        sandbox_node_reserve=_env_bytes(
            "DAEGAM_SANDBOX_NODE_RESERVE", _DEFAULTS["sandbox_node_reserve"], 0, 64 * 1024**3
        ),
        sandbox_workspace=_env_bytes(
            "DAEGAM_SANDBOX_WORKSPACE",
            _DEFAULTS["sandbox_workspace"],
            SANDBOX_WORKSPACE_MIN,
            SANDBOX_WORKSPACE_MAX,
        ),
        sandbox_allow_unchecked=_env_bool("DAEGAM_SANDBOX_ALLOW_UNCHECKED"),
        sandbox_sweep_seconds=max(
            10.0, _env_float("DAEGAM_SANDBOX_SWEEP_SECONDS", _DEFAULTS["sandbox_sweep_seconds"])
        ),
    )


# 샌드박스 상한 설정의 범위. 0 이면 그 상한에서 하나도 열지 못한다
SANDBOX_CAP_MAX = 1000
# 작업 공간 크기의 범위. 메모리 emptyDir 이라 이 크기가 곧 메모리다
SANDBOX_WORKSPACE_MIN = 64 * 1024**2
SANDBOX_WORKSPACE_MAX = 16 * 1024**3


def _sandbox_cap(name: str, field_name: str) -> int:
    return _env_int_clamped(name, _DEFAULTS[field_name], 0, SANDBOX_CAP_MAX, warn_above=True)


def _env_bytes(name: str, default: int, lo: int, hi: int) -> int:
    """메모리 크기 설정. 쿠버네티스 quantity(`2Gi`, `512Mi`)나 바이트 수다. 범위 밖이면 끝값으로
    자른다."""
    from kubernetes.utils import parse_quantity

    value = _env_number(name, default, lambda raw: int(parse_quantity(raw)))
    return min(hi, max(lo, value))
