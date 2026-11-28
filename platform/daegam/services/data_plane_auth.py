"""데이터 플레인 인증 재적용 (P32 8절, D58)과 렌더 번호로 다시 렌더하기 (P50 1절).

인증 설정(모드, 발급자, audience, 테넌트 그룹, agent_client_ids)이 바뀌거나 릴리스가 종류의 렌더를
바꾸면 플랫폼이 스스로 데이터 플레인에 다시 적용한다. 판은 둘이다. 네임스페이스 객체의 판과 문서의
판이고, 문서의 판은 인증 절반(`auth_rev`)과 렌더 번호(`render_epoch`)로 나뉜다.

- 네임스페이스 객체의 판: MCPOIDCConfig 하나의 기대 렌더 해시다. `oidc_config_manifest` 가
  어노테이션 `daegam.devops.ai.kr/desired-hash` 로 싣고, 리컨실이 틱마다 클러스터의 실물과
  맞댄다(드리프트 스윕). 적어 둔 값이 아니라 실물과 맞대므로 롤링 중에 옛 레플리카가 옛 렌더를
  적용해도 다음 틱이 잡는다. 내부 설정은 테넌트 객체(RoleBinding·ServiceAccount)의 판도 객체 판 밖에
  싣는다.
- 인증 절반 `auth_rev`: 문서(Gateway, 노출한 MCP Server, 노출했거나 Gateway 소스를 쓰는 Agent)가
  렌더에 쓰는 인증 입력의 해시다. 렌더와 판이 `consumer_auth` 하나에서 입력을 받으므로 둘이 갈라지지
  않는다. provision·rotate_svc_token·reapply_auth 가 적는다. 공유 복제본 기록의 첫 절반은 인증
  입력이 아니라 렌더할 원본 정의의 판이다(`services/shared_clones.py`).
- 렌더 번호 `render_epoch`: 종류(MCP Server·Gateway·Agent·허브 항목·공유 복제본 기록)가 렌더하는
  것을 바꾸는 커밋이 `RENDER_EPOCHS` 의 그 종류 번호를 올리면 그 종류의 살아 있는 문서가 모두
  제자리에서 다시 렌더된다. 그래서 살아 있는 문서는 인증 입력을 쓰지 않아도 모두 이 단계가 본다.

리컨실은 모든 클러스터를 먼저 스윕하고, 기대 판과 다른 문서를 인증 절반이 다른 것부터 한 틱의 몫과
시간 안에서 다시 렌더한다. MCPOIDCConfig 를 이번 틱에 맞추지 못한 네임스페이스의 문서는 고르지
않는다. 그 설정이 먼저 바뀌고 문서가 뒤에 렌더되게 하려는 것이다. 다른 네임스페이스의 문서는
기다리지 않는다. 한 클러스터를 두 설치가 함께 쓸 수 있으므로 스윕은 설치 라벨
`daegam.devops.ai.kr/platform-id` 가 자기 값인 객체와, 라벨이 없더라도 자기가 관리하는
네임스페이스의 객체만 다시 쓴다. 틱의 결과는 `auth_reapply` 컬렉션에 남고
`GET /api/settings/data-plane-auth` 가 보인다.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any, Protocol

from starlette.concurrency import run_in_threadpool

from daegam.auth.cedar import CLIENT_ID_CLAIM, holds_forbid, names_group
from daegam.auth.signer import SCOPE_MCP
from daegam.config import DATA_PLANE_ISSUER_PLATFORM, DEFAULT_GROUPS_CLAIM, Settings
from daegam.config.resolve import EffectiveConfig, resolve_effective
from daegam.error_codes import holds_reapply
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient, platform_subject
from daegam.k8s.gates import (
    VMCP_GATES,
    Feature,
    pack_epoch,
    passing_features,
    tenant_object_rights,
    unpack_epoch,
)
from daegam.k8s.labels import (
    ANNOTATION_DESIRED_HASH,
    LABEL_PLATFORM_ID,
    LABEL_RESOURCE,
    LABEL_TENANT,
    REV_LENGTH,
    stable_hash,
)
from daegam.k8s.preflight import shipped_redis_cert_expiring
from daegam.k8s.templates.agent import IdpSide
from daegam.k8s.templates.common import (
    TOOLHIVE_API,
    RenderError,
    oidc_config_ref,
)
from daegam.k8s.templates.tenant_policies import egress_policy_wanted
from daegam.services import agent_graph
from daegam.services.advanced_edit import api_error_message
from daegam.services.agent_features import TURN_TOKENS
from daegam.services.budget import Budget
from daegam.services.claims import tick_item
from daegam.services.proxy_accounts import proxy_account_can_read
from daegam.services.redis_tls_consumers import (
    by_tenant,
    plain_redis_consumers,
    plain_row,
    shipped_at,
)
from daegam.services.tenancy import (
    RESOURCE_INTERNAL_OIDC_CONFIG,
    RESOURCE_OIDC_CONFIG,
    TenancyError,
    TenancyService,
    check_namespace_name,
    ensure_namespace_policies,
    ensure_oidc_configs,
    live_namespace_policies,
    namespace_egress,
    namespace_policies,
    namespace_policies_stamp,
    oidc_config_manifests,
    oidc_kwargs,
    policy_stamp_of,
    tenant_objects_for,
    tenant_objects_names,
    tenant_objects_stamp,
    tenant_objects_stamp_of,
)
from daegam.services.turn_gates import TurnGates, doc_row, judge, version_ok
from daegam.store.auth_reapply import (
    AUTH_FIELDS,
    KIND_AGENT,
    KIND_CLONE,
    KIND_GATEWAY,
    KIND_HUB_ITEM,
    KIND_MCP_SERVER,
    AudienceSwitch,
    AuthBlocked,
    AuthSummary,
    ClusterAuthState,
    MongoAuthReapplyStore,
    own_audience,
)
from daegam.store.base import fields_of, utcnow
from daegam.store.clusters import Cluster
from daegam.store.exposure import exposure_enabled
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import (
    MongoTenantSettingsStore,
    TenantSettings,
    data_plane_group,
    default_mcp_group,
)
from daegam.store.tool_rules import has_caller_rule

if TYPE_CHECKING:
    from daegam.services.entry_conversion import EntryConversion
    from daegam.services.issuer_switch import IssuerSwitch

logger = logging.getLogger("daegam.data_plane_auth")

# 한 틱에 다시 렌더하는 소비자 수. MCPOIDCConfig 가 그대로인 재적용이 이만큼씩 나뉘어 굴러간다.
# 실패한 클러스터·소비자의 재시도 간격은 store/auth_reapply.py 의 next_retry 다
REAPPLY_PER_TICK = 10
# 한 틱의 재렌더가 쓰는 시간(초). 리컨실 리스(`store/leases.py` 의 `LEASE_TTL`) 안에서 다른 단계의
# 몫을 남긴다. 시계는 모든 클러스터의 스윕이 끝난 뒤에 돈다
REAPPLY_SECONDS_PER_TICK = 30.0
# 렌더 번호만 다른 문서는 한 틱에 한 네임스페이스에서 이 수까지만 다시 렌더한다. 인증 절반이 다른
# 문서도 이 수에 세지만 그 문서는 이 수에 막히지 않는다. 렌더 번호가 오른 릴리스에서 한
# 네임스페이스의 파드가 한꺼번에 굴러가지 않게 한다
REAPPLY_PER_NAMESPACE = 3
# 런타임 이미지를 세는 노출 Agent 의 상태. 다시 배포하면 지금 이미지로 뜨는 문서다
EXPOSED_AGENT_STATES = frozenset({"deploying", "deployed", "failed"})

# 종류마다의 렌더 번호 (P50 1절). 그 종류가 렌더하는 것을 바꾸는 커밋이 같은 커밋에서 번호를 올린다.
# render_epoch 가 없는 문서는 0 으로 읽으므로 번호가 0 인 종류는 아무 문서도 다시 렌더하지 않는다.
# tests/test_render_epochs.py 가 종류마다 고정 문서의 렌더를 지켜, 렌더가 바뀌었는데 번호가 그대로면
# 실패한다
RENDER_EPOCHS: dict[str, int] = {
    # 1: 노출 Ingress 가 /mcp 와 보호 자원 메타데이터 경로만 연다, 앞단의 Cedar 참조와 역할 없는
    # ServiceAccount, 앞단 토큰이 그 서버의 audience 를 싣는다 (P50 5·8·9·10절)
    # 2: 0.52 클러스터에서 테넌트 울타리와 한 Secret 의 여러 키 (P50 11절)
    # 3: 여러 backend 의 배포 때 endpoint·auth env
    # 4: local 모드 노출 앞단이 묶이지 않은 위임 토큰을 막는 forbid (P53 3.2절)
    # 5: 백엔드 파드의 라벨 (T67)
    # 6: 노출 앞단이 백엔드 하나의 vMCP 이고, 옛 앞단이 도는 서버는 vMCP 앞단이 그 자리를
    #    넘겨받는다. 노출 NetworkPolicy 가 두 종류의 앞단 파드를 고른다 (P54 5·6절)
    # 7: 그 리비전이 아는 Tool 이름 밖의 것을 닫는 forbid 를 모든 내부 서버가 싣는다 (P52 2절)
    # 8: 프록시와 노출 앞단의 자원 요청이 실측을 따른다 (10m, 128Mi)
    KIND_MCP_SERVER: 8,
    # 1: 감사, 이름 바꾼 툴의 필터, resources·prompts 표면, 백엔드마다의 호출 타임아웃, Cedar 참조,
    # 정적 백엔드 인증, Entry 마다의 토큰 (P50 4·6·7·8·9·12·13절)
    # 2: authz_policy 를 문장마다 정책 원소 하나로 (T63)
    # 3: local 모드에서 이 Gateway 에 묶인 위임 토큰의 permit 과 그 밖의 위임 토큰의 forbid,
    #    ConfigMap authzConfig 의 잠금 (P53 3.1절)
    # 4: vMCP 의 기본 자원 요청이 실측을 따른다 (10m, 128Mi)
    KIND_GATEWAY: 4,
    # 1: MCP Server 소스마다의 토큰 env 와 init 의 토큰 표 지문 (P50 9절)
    # 2: 이벤트 수집 주소와 파드 이름·uid env (P37 3절)
    # 3: overrides 가 파드 템플릿에 단 app.kubernetes.io/name: mcpserver 를 뺀다 (T67)
    # 4: 준비 검사가 첫 1초부터 2초마다이고 실패 문턱이 15다. 깨운 파드가 곧 Service 에 든다
    #    (P44 8절)
    # 5: Gateway 소스가 있는 Agent 의 init 주소가 파드가 실은 Gateway 인증 방식을 싣는다
    KIND_AGENT: 5,
    # 1: 원격 프록시의 역할 없는 ServiceAccount (P50 10절)
    # 2: 0.52 클러스터에서 image 항목의 테넌트 울타리 (P50 11절)
    # 3: image 항목의 백엔드 파드의 라벨 (T67)
    # 4: image 항목의 프록시 자원 요청이 실측을 따른다 (10m, 128Mi)
    KIND_HUB_ITEM: 4,
    # 1: 원격 프록시 복제본의 역할 없는 ServiceAccount (P50 10절)
    # 2: 0.52 클러스터에서 image 복제본의 테넌트 울타리 (P50 11절)
    # 3: image 복제본과 플랫폼 fetch 복제본의 백엔드 파드의 라벨 (T67)
    # 4: 복제본의 프록시 자원 요청이 실측을 따른다 (10m, 128Mi)
    KIND_CLONE: 4,
}
# 종류마다 렌더가 따르는 클러스터 게이트 (P50 2절). 그 클러스터에서 이 기능의 판정이 바뀌면 그
# 종류의 기대 렌더 번호가 달라져(`render_epoch`) 살아 있는 문서가 재적용의 속도로 다시 렌더된다.
# 렌더가 새 게이트를 따르게 되면 여기에 더하고 그 종류의 `RENDER_EPOCHS` 번호도 올린다
RENDER_GATES: dict[str, frozenset[Feature]] = {
    # vMCP 의 게이트는 노출 앞단이 vMCP 인지를 가른다 (P54 5절)
    KIND_MCP_SERVER: frozenset(
        {*VMCP_GATES, Feature.ROLELESS_SA, Feature.TENANT_FENCE, Feature.MULTI_KEY_SECRETS}
    ),
    KIND_GATEWAY: frozenset(VMCP_GATES),
    KIND_AGENT: frozenset(),
    KIND_HUB_ITEM: frozenset({Feature.AUTHZ_REF, Feature.ROLELESS_SA, Feature.TENANT_FENCE}),
    KIND_CLONE: frozenset({Feature.AUTHZ_REF, Feature.ROLELESS_SA, Feature.TENANT_FENCE}),
}


def render_epoch(kind: str, passing: frozenset[Feature], *, binds_delegation: bool = False) -> int:
    """그 종류의 문서가 그 클러스터에서 가져야 할 렌더 번호 (판의 둘째 절반). passing 은 그
    클러스터가 지나는 기능이다(`passing_features`). 종류가 따르는 게이트(`RENDER_GATES`)를 모두
    지나면 `RENDER_EPOCHS` 의 번호 그대로이고, 하나라도 못 지나면 번호와 못 지난 게이트의
    조합마다 다른 음수다(`k8s/gates.py` `pack_epoch`). 그래서 판정이 바뀌면(오퍼레이터를 올렸거나
    권한이 바뀌었다) 문서에 적힌 번호와 달라져 다시 렌더된다. binds_delegation(데이터 플레인이
    플랫폼을 믿는 모드, `ConsumerAuth.binds_delegation`)이면 Gateway 의 번호에 위임 묶임의 표시를
    단다. 그 렌더만 permit 과 forbid 를 싣기 때문이다 (P53 3.5절). 모드가 바뀌면 번호도 달라져 다시
    렌더된다."""
    bound = kind == KIND_GATEWAY and binds_delegation
    return pack_epoch(RENDER_EPOCHS[kind], RENDER_GATES[kind] - passing, bound=bound)


def missed_gates(kind: str, epoch: int) -> frozenset[Feature] | None:
    """문서에 적힌 렌더 번호(`render_epoch`)에서 그 렌더가 지나지 못한 게이트를 읽는다
    (`unpack_epoch`). 게이트를 빠뜨린 번호는 어느 판의 것이든 그 게이트를 싣고 있으므로 판이 올라
    다시 렌더되기 전에도 읽는다. 지금 판의 번호는 빈 집합이고, 그 밖(0, STALE_EPOCH, 옛 판의
    양수)은 None 이다. 메트릭이 Gateway 의 적용한 렌더에 감사가 있었는지 보는 데 쓴다 (P51 4절)."""
    stamp = unpack_epoch(epoch)
    if stamp is None or (not stamp.missed and stamp.base != RENDER_EPOCHS[kind]):
        return None
    return stamp.missed


# 게이트를 지나지 못한 클러스터에서 그 기능을 뺀 대체 형태로 렌더하는 종류 (P50 2절). 기능을 렌더에
# 들이는 배치가 여기에 더하고, 상태 API 가 기능마다 그런 문서를 센다
FALLBACK_KINDS: dict[Feature, frozenset[str]] = {
    # 노출한 MCP Server 는 vMCP 앞단의 게이트 넷 가운데 하나라도 지나지 못하면 옛 앞단으로
    # 돈다(authz_ref 는 아래 줄에 이미 든다) (P54 5절)
    Feature.AUDIT: frozenset({KIND_GATEWAY, KIND_MCP_SERVER}),
    Feature.TIMEOUTS: frozenset({KIND_GATEWAY, KIND_MCP_SERVER}),
    Feature.STATIC_BACKENDS: frozenset({KIND_GATEWAY, KIND_MCP_SERVER}),
    Feature.AUTHZ_REF: frozenset({KIND_GATEWAY, KIND_MCP_SERVER, KIND_HUB_ITEM, KIND_CLONE}),
    Feature.ROLELESS_SA: frozenset({KIND_MCP_SERVER, KIND_HUB_ITEM, KIND_CLONE}),
    Feature.TENANT_FENCE: frozenset({KIND_MCP_SERVER, KIND_HUB_ITEM, KIND_CLONE}),
    Feature.MULTI_KEY_SECRETS: frozenset({KIND_MCP_SERVER}),
    Feature.SESSION_TLS: frozenset({KIND_MCP_SERVER}),
}


def _remote_proxy(kind: str, doc: Any) -> bool:
    """원격 프록시를 띄우는 문서인가. 노출한 MCP Server 의 앞단, remote·proxy 모드 게시 허브 항목,
    remote 원본의 복제본이다."""
    if kind == KIND_MCP_SERVER:
        return exposure_enabled(doc)
    if kind == KIND_HUB_ITEM:
        return getattr(getattr(doc, "k8s", None), "kind", None) == "MCPRemoteProxy"
    if kind == KIND_CLONE:
        return (getattr(doc, "source", None) or {}).get("kind") == "remote"
    return False


def _vmcp(kind: str, doc: Any) -> bool:
    """vMCP 를 렌더하는 문서인가. Gateway 와 노출한 MCP Server 의 앞단이다 (P54 5절)."""
    return kind == KIND_GATEWAY or (kind == KIND_MCP_SERVER and exposure_enabled(doc))


def _cedar_by_ref(kind: str, doc: Any, passing: frozenset[Feature]) -> bool:
    """Cedar 를 MCPAuthzConfig 로 참조하는 문서인가. Gateway 의 vMCP, 노출한 MCP Server 의 앞단,
    울타리를 거는 클러스터의 우리 MCPServer 다 (P50 8·11절)."""
    if kind == KIND_GATEWAY or (kind == KIND_MCP_SERVER and exposure_enabled(doc)):
        return True
    return Feature.TENANT_FENCE in passing and internal_server(kind, doc)


def _has_backend_secrets(doc: Any) -> bool:
    """배포 스냅샷에, 한 Secret 의 여러 키를 받는지에 따라 자리가 바뀌는 Secret 키가
    있는가(`_SecretRefs`). 자격증명을 실은 backend 와, backend 가 여럿인 서버의 soap:header 값
    Secret(T60)이다. backend 하나의 soap:header 값은 자격증명과 같은 Secret 일 때만 자리가 바뀌고
    그때는 자격증명으로 센다. 스냅샷이 없는 문서는 모르므로 세지 않는다."""
    inputs = getattr(doc, "deployed_inputs", None)
    backends = getattr(inputs, "backends", [])
    if any(b.has_credentials for b in backends):
        return True
    return len(backends) > 1 and bool(getattr(inputs, "soap_headers_secrets", None))


# 기능마다, 그 기능을 렌더하는 문서인가 (종류, 문서, 그 클러스터가 지나는 기능). 없는 기능은
# `FALLBACK_KINDS` 의 종류 모두가 렌더한다
_RENDERS: dict[Feature, Callable[[str, Any, frozenset[Feature]], bool]] = {
    Feature.AUDIT: lambda kind, doc, _passing: _vmcp(kind, doc),
    Feature.TIMEOUTS: lambda kind, doc, _passing: _vmcp(kind, doc),
    Feature.STATIC_BACKENDS: lambda kind, doc, _passing: _vmcp(kind, doc),
    Feature.AUTHZ_REF: _cedar_by_ref,
    Feature.ROLELESS_SA: lambda kind, doc, _passing: _remote_proxy(kind, doc),
    Feature.TENANT_FENCE: lambda kind, doc, _passing: internal_server(kind, doc),
    Feature.MULTI_KEY_SECRETS: lambda _kind, doc, _passing: _has_backend_secrets(doc),
    Feature.SESSION_TLS: lambda _kind, doc, _passing: _session_redis_has_tls(doc),
}


def _renders_feature(feature: Feature, kind: str, doc: Any, passing: frozenset[Feature]) -> bool:
    """그 종류의 문서 가운데 기능을 렌더하는 것인가 (`_RENDERS`)."""
    judge = _RENDERS.get(feature)
    return judge is None or judge(kind, doc, passing)


def _session_redis_has_tls(doc: Any) -> bool:
    """세션 저장소를 쓰는 MCP Server(replicas 2 이상)의 그 Redis 가 TLS 끝점을 가졌는가. 바인딩한
    Redis 가 먼저이고 없으면 배포 시점의 클러스터 기본 Redis 다 (P61 3절)."""
    effective = getattr(doc, "effective_config", None) or {}
    if int(effective.get("replicas") or 1) < 2:
        return False
    inputs = getattr(doc, "deployed_inputs", None)
    for binding in getattr(inputs, "bindings", None) or []:
        if binding.kind == "redis":
            return binding.tls is not None
    return bool((effective.get("default_redis") or {}).get("tls"))


# 소비자 갈래마다 판에 드는 입력 (P32 8절 표). 두 갈래에 드는 Agent 는 합집합이다. 발급자와 JWKS
# 주소는 ToolHive 앞단(vMCP·노출 앞단)의 판에 들지 않는다. MCPOIDCConfig 에만 있고 오퍼레이터가 그
# 변경을 보고 스스로 굴리기 때문이다. scope 는 앞단의 참조에만 있어 Agent 의 판에 들지 않는다 (P33
# 14절)
REV_INPUTS: dict[str, tuple[str, ...]] = {
    "toolhive_front": ("mode", "mcp_audience", "tenant_group", "agent_client_ids", "scopes"),
    "exposed_agent": (
        "mode",
        "issuer",
        "jwks_url",
        "insecure_http",
        "mcp_audience",
        "tenant_group",
        "agent_client_ids",
    ),
    "gateway_source": ("mode", "issuer", "mcp_audience", "tenant_group"),
}
# IdP 클레임 이름(`ConsumerAuth.groups_claim`, `client_id_claim`)을 읽는 갈래. 두 이름은 기본값이
# 아닐 때만 판에 표시로 얹으므로(`auth_rev`) 기본값인 설치는 판이 바뀌지 않는다
IDP_CLAIM_PARTS = frozenset({"toolhive_front", "exposed_agent"})


def idp_side(settings: Settings) -> IdpSide | None:
    """플랫폼을 믿는 oidc 모드의 IdP 검증기 입력. 그 밖의 모드에서는 None 이다."""
    if not settings.oidc_trusts_platform:
        return None
    assert settings.oidc_issuer is not None  # 플랫폼을 믿는 oidc 는 발급자가 있다(token_auth)
    groups_claim = settings.oidc_groups_claim
    return IdpSide(
        issuer=settings.oidc_issuer,
        insecure_http=settings.insecure_issuer,
        groups_claim=None if groups_claim == DEFAULT_GROUPS_CLAIM else groups_claim,
        client_id_claim=settings.oidc_client_id_claim,
    )


@dataclass(frozen=True)
class ConsumerAuth:
    """소비자 렌더가 인증에 쓰는 입력. 세 서비스의 렌더와 소비자 판이 이것 하나에서 나온다."""

    mode: str  # header·local·oidc. 발급자 없는 oidc 는 header 로 돈다 (Settings.token_auth)
    # 데이터 플레인이 믿는 사람 토큰의 발급자(Settings.data_plane_issuer_url). header 에서 None
    issuer: str | None
    # 노출 에이전트가 JWKS 를 받는 클러스터 안 주소. 플랫폼을 믿을 때만 있다
    # (Settings.human_jwks_url)
    jwks_url: str | None
    insecure_http: bool  # 검증기의 평문 플래그. 발급자나 JWKS 주소가 http 면 참이다
    mcp_audience: str
    tenant_group: str
    # ToolHive 앞단과 노출 Agent 가 사람 토큰의 그룹을 읽을 클레임. IdP 를 믿는 oidc 모드에서
    # DAEGAM_OIDC_GROUPS_CLAIM 이 기본값(groups)이 아닐 때만 있다. 없으면 렌더가 이름을 싣지 않고
    # 앞단은 ToolHive 의 기본(groups, roles, cognito:groups), 런타임은 groups 를 읽는다
    groups_claim: str | None
    # Agent client credentials 토큰이 client id 를 싣는 클레임. IdP 를 믿는 oidc 모드는 플랫폼
    # 검증기와 같은 DAEGAM_OIDC_CLIENT_ID_CLAIM 이고(Keycloak 은 azp), 그 밖에는 플랫폼이 내는
    # 토큰의 cid 다
    client_id_claim: str
    agent_client_ids: tuple[str, ...]
    # ToolHive 앞단의 참조가 광고하는 scope. OAuth 인가 서버가 켜졌을 때만 mcp 다 (P33 14절)
    scopes: tuple[str, ...]
    # 데이터 플레인이 플랫폼의 사람 토큰을 믿는가 (`Settings.data_plane_trusts_platform`, P53b
    # 6.1절). 판의 입력(`REV_INPUTS`)이 아니라 local 과 IdP 를 믿는 oidc 의 판은 그대로이고,
    # 플랫폼을 믿는 oidc 는 표시로 판에 얹는다(`auth_rev`). 그때 위의 칸은 local 모드의
    # 값이다(발급자는 플랫폼, JWKS 는 클러스터 안 주소, 그룹은 daegam-<tenant>, 클레임은 플랫폼
    # 토큰의 이름)
    trusts_platform: bool = False
    # 플랫폼을 믿는 oidc 모드에서 노출 Agent 가 플랫폼 곁에 두는 IdP 검증기. 그 밖에는 None 이다
    idp: IdpSide | None = None

    @property
    def token_auth(self) -> bool:
        return self.mode != "header"

    @property
    def binds_delegation(self) -> bool:
        """사람 경계가 위임 토큰을 자기 대상에 묶는가 (P53 3.1·3.2절). 데이터 플레인이 플랫폼을 믿는
        모드(local, platform 인 oidc)뿐이다. IdP 를 믿는 경계에는 플랫폼 토큰이 닿지 않고, 그곳의
        forbid 는 IdP 가 위임으로 낸 토큰(`act`)만 막게 된다. 믿는 발급자가 바뀌면 판이 달라져 다시
        렌더된다."""
        return self.trusts_platform

    @property
    def exposure_default_group(self) -> str | None:
        """노출 Agent 의 빈 allowed_groups 를 채울 그룹. 데이터 플레인이 플랫폼을 믿을 때만이고,
        그때 이 그룹은 플랫폼 검증기의 것이다. 비워 두면 다른 테넌트의 토큰도 들어오기 때문이다
        (P32 2절). IdP 검증기는 D27 대로 IdP 가 인증한 주체 모두다 (P53b 6.8절)."""
        return self.tenant_group if self.trusts_platform else None

    def oidc_ref(self, resource_url: str | None) -> dict[str, Any]:
        """ToolHive 앞단(Gateway 의 vMCP, 노출한 MCP Server 의 앞단)이 사람 토큰을 검증하는 참조.
        노출했으면 resourceUrl 이 external_url 이다 (D22)."""
        return oidc_config_ref(self.mcp_audience, resource_url, self.scopes)


def consumer_auth(settings: Settings, ts: TenantSettings) -> ConsumerAuth:
    """소비자 렌더와 판의 입력. 데이터 플레인이 IdP 를 믿는 oidc 모드만 IdP 의 클레임 이름과 저장된
    mcp_group 을 쓰고, 플랫폼을 믿으면(local, platform 인 oidc) 플랫폼 토큰의 이름과 고정 그룹을
    쓴다 (P53b 6.1절)."""
    token = settings.token_auth
    idp = settings.data_plane_trusts_idp
    groups_claim = settings.oidc_groups_claim
    return ConsumerAuth(
        mode=settings.auth_mode if token else "header",
        issuer=settings.data_plane_issuer_url if token else None,
        jwks_url=settings.human_jwks_url if token else None,
        insecure_http=token and settings.insecure_human_http,
        mcp_audience=settings.mcp_audience,
        tenant_group=data_plane_group(settings, ts),
        groups_claim=groups_claim if idp and groups_claim != DEFAULT_GROUPS_CLAIM else None,
        client_id_claim=settings.oidc_client_id_claim if idp else CLIENT_ID_CLAIM,
        agent_client_ids=tuple(ts.agent_client_ids),
        scopes=(SCOPE_MCP,) if settings.oauth_enabled else (),
        trusts_platform=settings.data_plane_trusts_platform,
        idp=idp_side(settings),
    )


def consumer_parts(kind: str, doc: Any) -> tuple[str, ...]:
    """문서가 인증 소비자로 드는 갈래. 비면 인증 소비자가 아니다. 그런 문서도 렌더 번호로는 다시
    렌더된다. Agent 가 Gateway 소스를 쓰는지는 배포가 핀한 리비전으로 본다."""
    if kind == KIND_GATEWAY:
        return ("toolhive_front",)
    exposed = bool(doc.exposure.enabled)
    if kind == KIND_MCP_SERVER:
        return ("toolhive_front",) if exposed else ()
    pinned = doc.deployed
    gateway = pinned is not None and agent_graph.uses_gateway_source(pinned)
    return tuple(p for p, on in (("exposed_agent", exposed), ("gateway_source", gateway)) if on)


def auth_rev(settings: Settings, ts: TenantSettings, kind: str, doc: Any) -> str | None:
    """소비자 판. 그 갈래의 입력만 정렬한 JSON 으로 해시한다. 소비자가 아니면 None 이다. 그룹
    클레임이나 client id 클레임이 기본값이 아니면 그것을 읽는 갈래의 판에 표시로 얹는다
    (`marked_rev`). 그래서 클레임을 바꾸면 그 문서들만 다시 렌더되고, 기본값인 설치의 판은 그대로다.
    표시는 클레임마다 따로 얹으므로 한쪽만 바꾼 설치의 판은 그 클레임 하나를 얹은 판과 같다.

    데이터 플레인이 플랫폼을 믿는 oidc 모드는 발급자의 종류를 표시로 얹는다 (P53b 6.1절). 그래서
    발급자를 바꾸면 모든 소비자가 다시 렌더되고, IdP 를 믿는 oidc 와 local 의 판은 그대로다. 노출
    Agent 는 곁에 두는 IdP 검증기의 입력(`IdpSide`)도 얹어 IdP 의 설정이 바뀌면 다시 렌더된다."""
    parts = consumer_parts(kind, doc)
    if not parts:
        return None
    auth = consumer_auth(settings, ts)
    inputs = asdict(auth)
    names = sorted({name for part in parts for name in REV_INPUTS[part]})
    rev = stable_hash({name: inputs[name] for name in names}, REV_LENGTH)
    mark: dict[str, str] = {}
    if IDP_CLAIM_PARTS.intersection(parts):
        if auth.groups_claim is not None:
            mark["groups_claim"] = auth.groups_claim
        if auth.client_id_claim != CLIENT_ID_CLAIM:
            mark["client_id_claim"] = auth.client_id_claim
    # 곁의 IdP 는 플랫폼을 믿는 oidc 모드에만 있다(`idp_side`)
    if auth.idp is not None:
        mark["issuer"] = DATA_PLANE_ISSUER_PLATFORM
        if "exposed_agent" in parts:
            mark["idp"] = stable_hash(asdict(auth.idp), REV_LENGTH)
    return marked_rev(rev, **mark)


def marked_rev(base: str | None, **mark: str) -> str | None:
    """판의 첫 절반에 표시를 얹는다. 표시가 없으면 base 그대로이고, 있으면 base(인증 입력을 쓰지
    않는 문서는 None)와 표시를 함께 해시한 값이다. 그래서 표시가 생기거나 사라진 문서는 다음 틱에
    다시 렌더되고, 그 표시를 모르는 이전 릴리스는 판이 달라 표시 전의 렌더로 되돌린다. 판을 적는
    자리와 기대 판이 모두 같은 함수를 거친다."""
    if not mark:
        return base
    return stable_hash({"auth": base, **mark}, REV_LENGTH)


def own_audience_rev(base: str | None, switch: AudienceSwitch | None) -> str | None:
    """자기 audience 로 옮겨 간 서버(P50 9절, G9 2단계)의 판 첫 절반(`marked_rev`). 판을 적는
    자리(`_stamp_auth`, 허브 항목의 `_stamp_epoch`, 복제본 기록의 `_rendered`)와 기대 판이 모두
    이것을 거친다."""
    return marked_rev(base, audience="own") if own_audience(switch) else base


def rules_rev(base: str | None, mark: str | None) -> str | None:
    """Tool 규칙을 든 객체(P52 2절)의 판 첫 절반(`marked_rev`). mark 는 그 객체가 지금 컴파일할
    규칙의 지문이다. 규칙을 컴파일할 판인지(0.53), 이름 댄 Agent, Group Mapping 의 역할 그룹, 백엔드
    서버의 규칙 지문, 돌고 있는 파드의 앞 이름이 모두 그 글에 들어 있다. 규칙이 없으면 None 이라
    base 그대로이므로 규칙 없는 객체의 판은 오늘과 같다."""
    return marked_rev(base, rules=mark) if mark else base


def front_rev(base: str | None, mark: str | None) -> str | None:
    """노출 앞단을 넘겨받는 MCP Server(P54 6절)의 판 첫 절반(`marked_rev`). mark 는 넘겨받기의
    단계(`services/exposure_front.py` `Step`)이고 기대 판에는 클러스터가 보이는 단계가, 적힌 판에는
    렌더가 닿은 단계가 실린다. 그래서 vMCP 앞단이 `Pending` 에 머문 서버는 기대 판과 같아 틱마다
    다시 렌더되지 않는다. 넘겨받을 것이 없으면 None 이라 base 그대로다."""
    return marked_rev(base, front=mark) if mark else base


def secret_env_rev(base: str | None, rev: int | None) -> str | None:
    """Secret env 를 든 image 항목(P55 5절)의 판 첫 절반(`marked_rev`). 표시는 값을 바꿀 때마다
    오르는 판이다. 값을 바꾼 뒤 적용이 실패해도 기대 판이 달라 재적용이 다시 렌더한다. 그 칸이 없는
    항목은 base 그대로다."""
    return marked_rev(base, secret_env=str(rev)) if rev is not None else base


def newer_rev(base: str | None, newer: Sequence[str]) -> str | None:
    """뒤 판이 쓴 칸을 든 문서(P55 9절, 허브 항목과 복제본 기록)의 판 첫 절반(`marked_rev`). 표시는
    그 칸 이름이다. 그래서 이 판이 그 문서에 둔 `hub_item_newer` 막힘은 문서가 이 판이 모르는 칸을
    드는 동안만 맞는다. 그 칸을 아는 판은 표시를 얹지 않으므로 기대 판이 막힘의 판과 달라, 따로
    풀지 않아도 다시 렌더한다. 그런 칸이 없으면 base 그대로다."""
    return marked_rev(base, newer=",".join(newer)) if newer else base


def named_rev(base: str | None, named: bool) -> str | None:
    """Tool 규칙이 이름 댄 Agent(P52 3절)의 판 첫 절반(`marked_rev`). 표시가 생기면 다음 재적용이
    그 Agent 를 다시 렌더하고, 그 렌더가 자기 id 를 싣지 않은 서비스 토큰을 새로 낸다."""
    return marked_rev(base, agent_id="named") if named else base


def idp_alone_rev(base: str | None, alone: bool) -> str | None:
    """플랫폼을 믿는 oidc 모드에서 IdP 하나만 렌더된 노출 Agent(P53b 6.8절)의 판 첫
    절반(`marked_rev`). 그 이미지가 `two_issuers` 를 알린 기록이 아직 없을 때다(첫 배포, 이미지를
    바꾼 배포, 옛 이미지). 새 파드의 init 이 그 이름을 알리면 표시가 사라져 다음 틱이 두 발급자로
    다시 렌더한다. 표시가 없으면 그 Agent 는 다시 배포할 때까지 플랫폼의 토큰을 받지 못한다.
    두 발급자로 렌더된 Agent 의 판은 base 그대로다."""
    return marked_rev(base, exposure="idp_alone") if alone else base


def redis_tls_rev(base: str | None, ca_mark: str | None) -> str | None:
    """Redis 의 TLS 끝점으로 붙는 소비자(P61 3절)의 판 첫 절반(`marked_rev`). ca_mark 는 그
    소비자가 믿는 CA 들의 지문이고(`templates/common.py` `redis_ca_mark`), 없으면 base 그대로다.
    인증 소비자가 아니라 base 가 None 인 문서도 표시를 얹으므로, 런타임 이미지가 `redis_tls` 를
    알리거나 Database 가 TLS 끝점을 얻거나 그 CA 가 바뀌면 다음 틱에 다시 렌더된다."""
    return marked_rev(base, redis=ca_mark) if ca_mark else base


def llm_rev(base: str | None, inputs: dict[str, Any] | None) -> str | None:
    """재적용의 llm 몫(P38 6절)을 얹은 Agent 의 판 첫 절반(`marked_rev`). inputs 는 결정이
    게이트웨이인 Agent 의 경로 입력이고(`AgentLlm.rev_inputs`), 아니면 None 이라 base 그대로다.
    그래서 끈 게이트웨이에서는 어느 Agent 도 다시 렌더되지 않고, 결정이 바뀐 Agent 만 다시 렌더된다.
    표시를 단 판이 표시 없는 판으로 바뀌는 것이 끈 뒤 직접 경로로 돌아가는 재적용이다."""
    return marked_rev(base, llm=stable_hash(inputs, REV_LENGTH)) if inputs else base


def request_error_code(e: Exception) -> str | None:
    """렌더가 요청 오류로 끝났으면 그 코드다. 같은 판에서는 다시 해도 같은 오류가 난다. 상태가
    아니라 오류 코드표의 표시(`holds_reapply`)로 가른다 (P74 2.5절). 클러스터의 사실에 기대는
    거절(cluster_rights_missing)은 표시가 없어 백오프로 미룬다."""
    if isinstance(e, RenderError):
        return e.code
    if isinstance(e, ServiceError) and holds_reapply(e.code):
        return e.code
    return None


def request_error_detail(e: Exception) -> str | None:
    """막힘을 상태 API 에 보일 이유. 잠긴 overrides 는 그 경로가 이유다."""
    if isinstance(e, ServiceError):
        path = e.extra.get("path")
        return f"{path}: {e.detail}" if path else e.detail
    return str(e) or None


# -- 드리프트 스윕의 순수 함수 -----------------------------------------------------------------


def live_oidc_configs(client: KubeClient) -> dict[str, list[dict[str, Any]]]:
    """클러스터의 두 MCPOIDCConfig 를 라벨로 찾아 네임스페이스마다 metadata 로 묶는다. 모든
    네임스페이스에 list_metadata 두 번이다."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resource in (RESOURCE_OIDC_CONFIG, RESOURCE_INTERNAL_OIDC_CONFIG):
        for item in client.list_metadata(
            TOOLHIVE_API, "MCPOIDCConfig", labels={LABEL_RESOURCE: resource}
        ):
            meta = item["metadata"]
            out[str(meta.get("namespace") or "")].append(meta)
    return dict(out)


def _label(meta: dict[str, Any], key: str) -> str | None:
    return (meta.get("labels") or {}).get(key)


def classify(
    live: dict[str, list[dict[str, Any]]], install_id: str
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """네임스페이스를 셋으로 가른다. 이 설치의 라벨이 있는 곳과 라벨이 없는 곳(각각 → 테넌트), 다른
    설치의 라벨이 있는 곳이다. 한 객체라도 다른 설치의 것이면 그 네임스페이스는 다른 설치의
    것이다."""
    ours: dict[str, str] = {}
    unlabeled: dict[str, str] = {}
    foreign: set[str] = set()
    for ns, metas in live.items():
        installs = {_label(m, LABEL_PLATFORM_ID) for m in metas} - {None}
        tenant = next((t for m in metas if (t := _label(m, LABEL_TENANT))), "")
        if installs - {install_id}:
            foreign.add(ns)
        elif installs:
            ours[ns] = tenant
        else:
            unlabeled[ns] = tenant
    return ours, unlabeled, foreign


def in_sync(desired: list[dict[str, Any]], metas: list[dict[str, Any]]) -> bool:
    """네임스페이스의 실물이 기대 렌더와 같은가. 객체마다 객체 판 어노테이션을 맞댄다. 기대하지 않는
    객체(header 모드에 남은 daegam-oidc)는 보지 않는다."""
    live = {m.get("name"): (m.get("annotations") or {}).get(ANNOTATION_DESIRED_HASH) for m in metas}
    return all(
        live.get(d["metadata"]["name"]) == d["metadata"]["annotations"][ANNOTATION_DESIRED_HASH]
        for d in desired
    )


# -- 한 틱 ---------------------------------------------------------------------------------


class ReapplyKind(Protocol):
    """재적용이 다루는 종류 하나의 모양. 세 인스턴스 서비스(`BaseInstanceService`), 허브 항목
    재적용(`services/hub_reapply.py` `HubItemReapply`), 공유 복제본(`services/shared_clones.py`
    `SharedCloneReapply`)이 맞춘다. `want_rev` 는 판의 첫 절반이고, 복제본에서는 인증 입력이 아니라
    렌더할 원본 정의의 판이다. 자기 audience 로 옮겨 간 서버는 그 표시를 함께 싣는다
    (`own_audience_rev`). `paced_revs` 는 기대 판과 표시 하나만큼만 다른 첫 절반들이고, 문서의 판이
    그 가운데 하나면 회수가 아니라서 렌더 번호처럼 천천히 다시 렌더한다(`_Target.urgent`)."""

    auth_kind: str
    profile_kind: str
    live_states: frozenset[str]
    instances: Any  # live_on_cluster(cluster_id) 와 set_fields(id, fields, *, expect_state)

    async def want_rev(self, doc: Any, ts: TenantSettings) -> str | None: ...

    async def paced_revs(self, doc: Any, ts: TenantSettings) -> frozenset[str | None]: ...

    async def reapply_auth(self, doc: Any) -> bool: ...


@dataclass
class _Target:
    """한 틱이 보는 살아 있는 문서 하나. want 는 지금 설정의 인증 절반(인증 입력을 쓰지 않으면
    None, 공유 복제본 기록이면 렌더할 원본 정의의 판), epoch 은 그 종류의 지금 렌더 번호다."""

    service: ReapplyKind
    doc: Any
    want: str | None
    epoch: int
    # 기대 판과 표시 하나만큼만 다른 첫 절반들 (`ReapplyKind.paced_revs`)
    paced: frozenset[str | None] = frozenset()
    # 인증 입력이 바뀐 문서 사이의 순서 (`switch_rank`). 데이터 플레인이 플랫폼을 믿는 oidc
    # 모드에서만 0 이 아닐 수 있다
    rank: int = 0

    @property
    def current(self) -> bool:
        return self.doc.auth_rev == self.want and self.doc.render_epoch == self.epoch

    @property
    def auth_behind(self) -> bool:
        """판의 첫 절반이 다르다."""
        return self.doc.auth_rev != self.want

    @property
    def urgent(self) -> bool:
        """인증 입력이 바뀌었다(회수 등). 먼저 다시 렌더하고 네임스페이스마다의 수에 막히지 않는다.
        공유 복제본의 첫 절반은 원본 정의의 판이라 그 변경은 렌더 번호처럼 천천히 간다. 한
        네임스페이스의 서버 프록시가 한꺼번에 굴러가지 않게 하려는 것이다."""
        return (
            self.auth_behind
            and self.service.auth_kind != KIND_CLONE
            and self.doc.auth_rev not in self.paced
        )

    @property
    def blocked(self) -> bool:
        """요청 오류로 막혔다. 막은 뒤 판의 두 절반도 문서도 그대로일 때만이다(`AuthBlocked`)."""
        b = self.doc.auth_blocked
        return (
            b is not None
            and (b.rev or None) == self.want
            and b.epoch == self.epoch
            and b.spec_change == self.doc.last_spec_change
        )

    @property
    def namespace(self) -> str:
        return self.doc.k8s.namespace

    def waiting(self, now: datetime) -> bool:
        return self.doc.waiting(now)

    def due(self, now: datetime) -> bool:
        return not (self.current or self.blocked or self.waiting(now))

    def order(self) -> tuple[bool, int, bool, bool, datetime]:
        """인증 입력이 바뀐 것이 먼저이고, 그 안에서는 발급자 전환의 순서(`switch_rank`)다. 그다음
        판의 첫 절반이 다른 것(공유 복제본의 원본 변경)이다. 그 안에서는 재시도 기록이 없는 것,
        그다음 재시도 시각 순이다."""
        retry = self.doc.auth_retry_at
        rank = self.rank if self.urgent else 0
        return (
            not self.urgent,
            rank,
            not self.auth_behind,
            retry is not None,
            retry or datetime.min,
        )


# 발급자 전환의 순서 (P53b 6.2절). 데이터 플레인이 믿는 발급자가 바뀌면 모든 소비자가 다시
# 렌더되고, 인증 입력이 바뀐 문서는 이 순서로 한 틱의 몫(`REAPPLY_PER_TICK`)씩 간다
RANK_FOREIGN_GROUP_FRONT = 0  # 저장된 mcp_group 이 고정 그룹과 다른 테넌트의 Gateway 와 노출 앞단
RANK_GATEWAY_SOURCE_AGENT = 1  # Gateway 소스를 쓰는 Agent
RANK_RULED = 2  # 누가 부를지의 Tool 규칙이 있는 Gateway 와 노출한 MCP Server
RANK_REST = 3


def switch_rank(settings: Settings, ts: TenantSettings, kind: str, doc: Any) -> int:
    """데이터 플레인이 믿는 발급자를 바꾼 뒤 인증 입력이 바뀐 문서를 다시 렌더하는 순서 (P53b
    6.2절). 플랫폼으로 갈 때 옛 Cedar 의 vMCP 는 테넌트가 mcp_group 을 정하지 않았으면 고정 그룹을
    받으므로, Agent 의 gateway_token 은 vMCP 가 굴러가자마자 통한다. 새 Cedar 가 필요한 Gateway 와
    노출 앞단(저장된 mcp_group 이 고정 그룹과 다른 테넌트의 것)이 먼저이고, 그다음 Gateway 소스를
    쓰는 Agent, 그다음 옛 Cedar 가 역할 절을 매핑된 IdP 그룹으로 컴파일한 규칙 있는 Gateway 와
    노출한 MCP Server, 그다음 나머지다. IdP 로 돌아갈 때는 Gateway 소스를 쓰는 Agent 가 먼저다.
    vMCP 가 IdP 로 굴러가면 그 Agent 의 gateway_token 이 통하지 않기 때문이다. local 과 header 는
    모두 같은 자리라 순서가 오늘과 같다."""
    if not settings.idp_login:
        return 0
    if kind == KIND_AGENT and "gateway_source" in consumer_parts(kind, doc):
        return RANK_GATEWAY_SOURCE_AGENT
    if not settings.oidc_trusts_platform:
        return RANK_REST
    fronts = kind == KIND_GATEWAY or (kind == KIND_MCP_SERVER and exposure_enabled(doc))
    if fronts and ts.mcp_group and ts.mcp_group != default_mcp_group(ts.tenant):
        return RANK_FOREIGN_GROUP_FRONT
    if fronts and _names_callers(doc):
        return RANK_RULED
    return RANK_REST


def _names_callers(doc: Any) -> bool:
    """배포한 리비전에 누가 부를지의 Tool 규칙이 있는가 (Gateway, MCP Server)."""
    rev = getattr(doc, "deployed", None)
    return rev is not None and any(
        has_caller_rule(rule) for b in rev.backends for rule in b.tool_rules.values()
    )


@dataclass(frozen=True)
class Behind:
    """다시 렌더를 기다리는 소비자 하나. blocked 는 요청 오류로 막혀 옛 렌더로 도는가다."""

    kind: str
    doc: Any
    blocked: bool = False


@dataclass
class _Scan:
    """클러스터 하나의 살아 있는 문서(서비스, 문서)와 그 가운데 소비자."""

    docs: list[tuple[Any, Any]]
    consumers: list[_Target]

    @property
    def namespaces(self) -> set[str]:
        return {doc.k8s.namespace for _, doc in self.docs if doc.k8s is not None}


def _live_docs(scans: dict[str, _Scan]) -> dict[str, list[tuple[str, Any]]]:
    """클러스터 id 마다 살아 있는 문서의 (종류, 문서)."""
    return {cid: [(svc.auth_kind, doc) for svc, doc in scan.docs] for cid, scan in scans.items()}


@dataclass
class _Tick:
    """한 틱의 재료와 결과. Tenant Settings 는 테넌트마다 한 번 읽는다."""

    now: datetime
    install_id: str
    tenant_settings: MongoTenantSettingsStore
    budget: int = REAPPLY_PER_TICK
    # 재렌더를 멈출 단조 시계 시각(`start_clock` 이 스윕 뒤에 맞춘다)과 (클러스터, 네임스페이스)마다
    # 이 틱에 다시 렌더한 수
    clock: Budget = field(default_factory=lambda: Budget(REAPPLY_SECONDS_PER_TICK))
    per_namespace: Counter[tuple[str, str]] = field(default_factory=Counter)
    clusters: list[ClusterAuthState] = field(default_factory=list)
    consumers: list[_Target] = field(default_factory=list)
    result: dict[str, list[str]] = field(
        default_factory=lambda: {"ensured": [], "reapplied": [], "failed": [], "blocked": []}
    )
    _settings: dict[str, TenantSettings] = field(default_factory=dict)

    async def settings_of(self, tenant: str) -> TenantSettings:
        if tenant not in self._settings:
            self._settings[tenant] = await self.tenant_settings.get(tenant)
        return self._settings[tenant]

    def start_clock(self) -> None:
        """재렌더의 시간을 지금부터 센다. 스윕이 쓴 시간은 재렌더의 몫에서 빼지 않는다."""
        self.clock.restart()

    def out_of_time(self) -> bool:
        return self.clock.out_of_time()

    def summary(self) -> AuthSummary:
        """pending 은 드리프트가 남은 클러스터와 판이 다른 소비자에서 막힌 것을 뺀 수, failed 는 그
        가운데 백오프 중인 수, blocked 는 막힌 소비자 수다."""
        behind = [c for c in self.consumers if not c.current and not c.blocked]
        return AuthSummary(
            pending=sum(s.last_error is not None for s in self.clusters) + len(behind),
            failed=sum(s.waiting(self.now) for s in self.clusters)
            + sum(s.namespace_waiting(ns, self.now) for s in self.clusters for ns in s.drifted)
            + sum(c.waiting(self.now) for c in behind),
            blocked=sum(c.blocked for c in self.consumers),
            tick_at=self.now,
        )


class DataPlaneAuth:
    """리컨실의 인증 재적용 단계와 그 상태 API."""

    def __init__(
        self,
        tenancy: TenancyService,
        store: MongoAuthReapplyStore,
        *,
        gateways: Any,
        mcp_servers: Any,
        agents: Any,
        hub_items: ReapplyKind,
        shared_clones: ReapplyKind,
        issuer_switch: IssuerSwitch | None = None,
        entry_conversion: EntryConversion | None = None,
    ):
        self.tenancy = tenancy
        self.store = store
        self.gateways = gateways
        self.mcp_servers = mcp_servers
        self.agents = agents
        # 재적용이 다루는 다섯 종류. 스토어는 각 종류의 `instances` 다
        self.services: tuple[ReapplyKind, ...] = (
            gateways,
            mcp_servers,
            agents,
            hub_items,
            shared_clones,
        )
        # 로그 한 줄을 남길지 가르는 앞 틱의 숫자. None 이면 기동 뒤 첫 틱이다
        self._last_counts: tuple[int, ...] | None = None
        # 데이터 플레인 발급자 전환의 상태 (`services/issuer_switch.py` `IssuerSwitch`, P53b 6.9절).
        # 없으면 상태의 data_plane_issuer 가 null 이다
        self.issuer_switch = issuer_switch
        # 프록시를 두던 허브 항목 옮기기의 상태 (`services/entry_conversion.py` `EntryConversion`,
        # P54b 7절). 없으면 상태의 entry_conversions 가 비어 있다
        self.entry_conversion = entry_conversion

    @property
    def settings(self) -> Settings:
        return self.tenancy.cluster_service.settings

    @cached_property
    def turn_gates(self) -> TurnGates:
        """틱이 적은 턴 위임 게이트 판정을 읽는다. 위임 발급(`services/turn_grants.py`)도 이것을
        쓴다."""
        return TurnGates(self.store, self.settings)

    async def run(self, now: datetime | None = None) -> dict[str, list[str]]:
        """리컨실 틱의 한 단계(리스 안). 모든 클러스터의 드리프트를 먼저 고친 뒤, MCPOIDCConfig 가
        맞춰진 네임스페이스의 문서를 모아 클러스터와 상관없이 순서대로 한 틱의 몫만큼 다시 렌더하고
        요약을 적는다. 그래서 한 클러스터의 렌더 번호 일거리가 다른 클러스터의 회수를 밀어내지
        않는다. 다시 렌더한 뒤에는 자기 audience 를 처음 적용한 서버에 그 때를 적고, 클러스터마다의
        턴 위임 게이트를 판정한다 (P53 2·3.5절)."""
        tick = await self._new_tick(now)
        clusters = await self.tenancy.clusters.list()
        # 게이트 판정의 때는 이 단계가 문서를 읽기 시작한 벽시계다. 틱의 시작(`tick.now`)이 아니다.
        # 틱의 앞 단계(멈춘 문서, 클러스터 점검)가 자기 예산을 다 쓰면 판정이 그만큼 늙은 채로 적혀,
        # 건강한 local 설치도 위임이 503 이 된다 (P53 3.5절)
        read_at = utcnow()
        admitted: list[_Target] = []
        scans: dict[str, _Scan] = {}
        for cluster in clusters:
            # 틱의 리스를 늘리고 잃었으면 멈춘다 (P67 2절)
            await tick_item()
            scan = scans[cluster.id] = await self._scan(cluster, tick)
            state = await self._sweep(cluster, scan, tick)
            admitted.extend(c for c in scan.consumers if state.admits(c.namespace))
            tick.clusters.append(state)
            tick.consumers.extend(scan.consumers)
        tick.start_clock()
        await self._reapply(admitted, tick)
        await self._record_applied_audiences(tick)
        await self._judge_turn_gates(clusters, scans, read_at)
        await self.store.retain_clusters([c.id for c in clusters])
        await self._report(tick)
        return tick.result

    async def _new_tick(self, now: datetime | None) -> _Tick:
        return _Tick(
            now=now or utcnow(),
            install_id=await self.tenancy.platform_settings.install_id(),
            tenant_settings=self.tenancy.tenant_settings,
        )

    async def _scan(self, cluster: Cluster, tick: _Tick) -> _Scan:
        """이 클러스터의 살아 있는 문서를 읽고 살아 있는 상태인 문서마다 기대 판의 두 절반을
        계산한다. 인증 소비자가 아닌 문서도 렌더 번호로 다시 렌더되므로 모두 든다 (P50 1절). 노출
        앞단을 넘겨받는 MCP Server 의 기대 판이 읽을 클러스터의 옛 앞단 목록을 먼저 읽는다 (P54
        6절, `_observe_fronts`)."""
        docs: list[tuple[Any, Any]] = [
            (service, doc)
            for service in self.services
            for doc in await service.instances.live_on_cluster(cluster.id)
        ]
        await self._observe_fronts(cluster, docs, tick)
        consumers: list[_Target] = []
        passing = passing_features(cluster)
        for service, doc in docs:
            if doc.state in service.live_states:
                ts = await tick.settings_of(doc.tenant)
                want = await service.want_rev(doc, ts)
                binds = consumer_auth(self.settings, ts).binds_delegation
                epoch = render_epoch(service.auth_kind, passing, binds_delegation=binds)
                paced = await service.paced_revs(doc, ts)
                rank = switch_rank(self.settings, ts, service.auth_kind, doc)
                consumers.append(_Target(service, doc, want, epoch, paced, rank))
        return _Scan(docs, consumers)

    async def _observe_fronts(
        self, cluster: Cluster, docs: list[tuple[Any, Any]], tick: _Tick
    ) -> None:
        """노출한 MCP Server 가 있는 클러스터에서만 옛 앞단 목록을 읽는다
        (`MCPServerService.observe_fronts`). 연결할 수 없어 미뤄 둔 클러스터는 읽지 않고, 그
        서버들은 적힌 넘겨받기 단계를 그대로 기대한다."""
        exposed = any(svc is self.mcp_servers and exposure_enabled(doc) for svc, doc in docs)
        state = await self.store.cluster(cluster.id) if exposed else None
        if state is None or state.backing_off(tick.now):
            self.mcp_servers.fronts.forget(cluster.id)
            return
        await self.mcp_servers.observe_fronts(cluster, tick.now)

    # -- 드리프트 스윕 -----------------------------------------------------------------------

    async def _sweep(self, cluster: Cluster, scan: _Scan, tick: _Tick) -> ClusterAuthState:
        """클러스터 하나의 네임스페이스 객체(NetworkPolicy 묶음, 두 MCPOIDCConfig 와 테넌트 객체)를
        기대 렌더와 맞추고 그 상태를 적는다. 연결할 수 없어 미뤄 둔 클러스터는 그동안 보지 않는다.
        네임스페이스 하나의 실패는 그 네임스페이스만 미루므로 다른 네임스페이스는 틱마다 다시
        본다."""
        state = await self.store.cluster(cluster.id)
        if state.backing_off(tick.now):
            return state
        try:
            client = await run_in_threadpool(self.tenancy.cluster_service.client_for, cluster)
            live = await run_in_threadpool(live_oidc_configs, client)
            targets, state.unadopted = await self._plan(cluster, live, scan, tick.install_id)
        except Exception as e:  # noqa: BLE001 - 연결할 수 없는 클러스터는 백오프로 미룬다
            state.unreachable = True
            return await self._record_sweep(state, api_error_message(e), tick)
        state.unreachable = False
        held, policy_error = await self._ensure_policies(cluster, client, targets, state, tick)
        error = await self._ensure(cluster, client, targets, live, state, tick, held)
        return await self._record_sweep(state, error or policy_error, tick)

    async def _ensure_policies(
        self,
        cluster: Cluster,
        client: KubeClient,
        targets: dict[str, str],
        state: ClusterAuthState,
        tick: _Tick,
    ) -> tuple[set[str], str | None]:
        """대상 네임스페이스마다 테넌트 ingress 정책이 실은 NetworkPolicy 묶음의 판을 기대 렌더의
        판과 맞대고(`namespace_policies`), 다르거나 정책이 없으면 묶음을 다시 둔다. 그
        네임스페이스에 배포가 없어도 묶음을 바꾼 판이 옛 네임스페이스에 닿는다. egress 정책은
        클러스터가 바라고(`egress_policy_wanted`) 그 네임스페이스에 관리자의 egress 정책이 없을
        때만 기대 렌더에 든다(`namespace_egress`). 스위치를 켜고 끄면 판이 바뀌어 다음 틱이
        맞춘다. 예약된 네임스페이스(`check_namespace_name`)는 보지 않는다.

        관리자의 egress 정책 때문에 egress 정책을 두지 않은 네임스페이스는 Cluster 에 적는다
        (`ClusterStatus.metadata_skipped`). 실패한 네임스페이스는 그 네임스페이스만 백오프로
        미루고, 백오프 중인 네임스페이스는 건너뛴다. 묶음을 맞추지 못한 네임스페이스(실패했거나
        백오프 중)와 마지막 오류를 돌려준다. `_ensure` 는 그 네임스페이스를 맞춘 것으로 적지
        않는다. 적으면 백오프가 지워진다."""
        wanted = egress_policy_wanted(cluster)
        platform_ns = self.settings.namespace
        try:
            live = await run_in_threadpool(live_namespace_policies, client)
        except Exception as e:  # noqa: BLE001 - 정책을 읽지 못한 것이 설정의 스윕을 막지 않는다
            return set(targets), f"namespace policies: {api_error_message(e)}"
        held: set[str] = set()
        skipped: list[str] = []
        error: str | None = None
        for ns, tenant in sorted(targets.items()):
            try:
                check_namespace_name(ns, reserved=self.settings.reserved_namespaces)
            except TenancyError:
                continue
            policies = live.get(ns, [])
            egress = namespace_egress(policies, wanted=wanted)
            if wanted and not egress:
                skipped.append(ns)
            want = namespace_policies(tenant, ns, platform_ns, egress=egress)
            if policy_stamp_of(policies) == namespace_policies_stamp(want):
                continue
            if state.namespace_waiting(ns, tick.now):
                held.add(ns)
                continue
            try:
                await run_in_threadpool(
                    ensure_namespace_policies,
                    client,
                    tenant,
                    ns,
                    platform_ns,
                    egress=wanted,
                    live=policies,
                )
            except Exception as e:  # noqa: BLE001 - 한 네임스페이스의 실패가 다른 곳을 막지 않는다
                state.namespace_backoff(ns, tick.now)
                held.add(ns)
                error = f"{ns}: namespace policies not ensured: {api_error_message(e)}"
                continue
            logger.info("namespace policies ensured in %s", ns)
        if skipped != cluster.status.metadata_skipped:
            await self.tenancy.clusters.set_metadata_skipped(cluster.id, skipped)
        return held, error

    async def _plan(
        self,
        cluster: Cluster,
        live: dict[str, list[dict[str, Any]]],
        scan: _Scan,
        install_id: str,
    ) -> tuple[dict[str, str], list[str]]:
        """다시 보장할 네임스페이스(→ 테넌트)와 입양하지 않은 네임스페이스. 라벨 없는 객체는 이
        설치가 관리하는 네임스페이스에서만 입양하고, 소비자 문서의 네임스페이스는 객체가 없어도
        대상이다. 다른 설치의 네임스페이스는 어느 쪽으로도 대상이 되지 않는다."""
        ours, unlabeled, foreign = classify(live, install_id)
        managed = await self._managed(cluster, scan.namespaces) if unlabeled else set()
        targets = {**ours, **{ns: t for ns, t in unlabeled.items() if ns in managed}}
        for c in scan.consumers:
            if c.doc.k8s.namespace not in foreign:
                targets.setdefault(c.doc.k8s.namespace, c.doc.tenant)
        return targets, sorted(foreign | (unlabeled.keys() - managed))

    async def tenant_namespaces(self, cluster: Cluster, client: KubeClient) -> list[str]:
        """이 클러스터에서 테넌트 객체(RoleBinding daegam-tenant-objects 와 원격 프록시 계정)를 두는
        네임스페이스 (P63 preflight 의 바인딩 검사). 스윕이 맞추는 곳과 같다: 이 설치의 OIDC 설정이
        있는 곳, 이 설치가 관리하는 곳의 라벨 없는 설정, 살아 있는 문서의 네임스페이스. 다른 설치의
        네임스페이스는 들지 않는다. 클러스터에서 읽다가 난 오류는 그대로 올라간다."""
        tick = await self._new_tick(None)
        scan = await self._scan(cluster, tick)
        live = await run_in_threadpool(live_oidc_configs, client)
        targets, _unadopted = await self._plan(cluster, live, scan, tick.install_id)
        return sorted(targets)

    async def _managed(self, cluster: Cluster, namespaces: set[str]) -> set[str]:
        """이 설치가 관리하는 네임스페이스. 살아 있는 문서와 CR 을 가진 허브 항목, 공유 복제본
        기록의 네임스페이스(`_Scan.namespaces`, 둘도 한 종류다)와 Tenant Settings 가 있는
        테넌트마다의 허용 네임스페이스다."""
        managed = set(namespaces)
        for tenant in await self.tenancy.tenant_settings.list_tenants():
            managed.update(await self.tenancy.allowed_namespaces(tenant, cluster))
        return managed

    async def _ensure(
        self,
        cluster: Cluster,
        client: KubeClient,
        targets: dict[str, str],
        live: dict[str, list[dict[str, Any]]],
        state: ClusterAuthState,
        tick: _Tick,
        held: set[str],
    ) -> str | None:
        """대상 네임스페이스마다 두 MCPOIDCConfig 를 기대 렌더와, 테넌트 객체(RoleBinding·
        ServiceAccount, P50 1절)를 내부 설정이 실은 판과 맞댄다. 다르면 테넌트 객체와 설정을 다시
        보장한다. 설정을 맞추지 못한 네임스페이스는 `state.drifted` 에 들어 그 문서가 기다리고,
        테넌트 객체만 맞추지 못한 네임스페이스는 `state.objects_pending` 에 들어 문서를 막지 않는다.
        실패한 네임스페이스는 그 네임스페이스만 백오프로 미루고 마지막 오류를 돌려준다.

        플랫폼 자신의 identity 를 얻지 못했으면(테넌트 객체가 None) 이번 틱에는 테넌트 객체와 그
        판을 건드리지 않는다. 판이 틱마다 오가지 않게 하려는 것이다. held 는 NetworkPolicy 묶음을
        맞추지 못한 네임스페이스(`_ensure_policies`)이고, 그 백오프를 지우지 않는다."""
        kwargs = oidc_kwargs(self.settings)
        subject = None
        if tenant_object_rights(cluster):
            subject = await run_in_threadpool(platform_subject, client)
        state.drifted, state.objects_pending = [], []
        error: str | None = None
        for ns, tenant in sorted(targets.items()):
            metas = live.get(ns, [])
            objects = tenant_objects_for(cluster, tenant, ns, subject)
            live_stamp = tenant_objects_stamp_of(metas)
            want_stamp = live_stamp if objects is None else tenant_objects_stamp(objects)
            desired = oidc_config_manifests(
                tenant, ns, tick.install_id, tenant_objects_stamp=want_stamp, **kwargs
            )
            configs_in_sync = in_sync(desired, metas)
            if configs_in_sync and live_stamp == want_stamp:
                # NetworkPolicy 묶음이 백오프 중인 네임스페이스는 그 백오프를 남긴다
                if ns not in held:
                    state.namespace_settled(ns)
                continue
            if state.namespace_waiting(ns, tick.now):
                if not configs_in_sync:
                    state.drifted.append(ns)
                if live_stamp != want_stamp:
                    state.objects_pending.append(ns)
                continue
            try:
                ensured = await run_in_threadpool(
                    ensure_oidc_configs,
                    client,
                    tenant,
                    ns,
                    tick.install_id,
                    tenant_objects=objects,
                    live_stamp=live_stamp,
                    **kwargs,
                )
            except Exception as e:  # noqa: BLE001 - 한 네임스페이스의 실패가 다른 곳을 막지 않는다
                state.drifted.append(ns)
                state.namespace_backoff(ns, tick.now)
                error = f"{ns}: {api_error_message(e)}"
                continue
            tick.result["ensured"].append(ns)
            if objects is not None and not ensured:
                state.objects_pending.append(ns)
                state.namespace_backoff(ns, tick.now)
                error = f"{ns}: tenant objects not ensured ({tenant_objects_names(objects)})"
            elif ns not in held:
                state.namespace_settled(ns)
            if objects and ensured:
                await self._check_proxy_account(client, ns, state)
        return error

    @staticmethod
    async def _check_proxy_account(
        client: KubeClient, namespace: str, state: ClusterAuthState
    ) -> None:
        """막 맞춘 원격 프록시 계정이 그 네임스페이스의 Secret·ConfigMap 을 읽지 못하는지 본다 (P50
        10절). 읽을 수 있으면 `proxy_account_reads` 에 적고 경고를 남긴다. 묻다가 실패하면 모른 채로
        둔다."""
        try:
            reads = await run_in_threadpool(proxy_account_can_read, client, namespace)
        except Exception as e:  # noqa: BLE001 - 확인을 못 한 것이 스윕을 막지 않는다
            logger.warning("proxy account check in %s: %s", namespace, api_error_message(e))
            return
        listed = set(state.proxy_account_reads) - {namespace}
        if reads:
            logger.warning("proxy account in %s can read Secrets or ConfigMaps", namespace)
            listed.add(namespace)
        state.proxy_account_reads = sorted(listed)

    async def _record_sweep(
        self, state: ClusterAuthState, error: str | None, tick: _Tick
    ) -> ClusterAuthState:
        """스윕의 결과를 적는다. 클러스터 단위 백오프는 클러스터에 연결할 수 없을 때만 건다.
        네임스페이스의 실패는 `_ensure` 가 그 네임스페이스에만 걸었다."""
        if error is None:
            state.retry_at, state.failures, state.last_error = None, 0, None
        else:
            logger.warning("reapply_auth cluster %s: %s", state.cluster_id, error)
            if state.unreachable:
                state.backoff(tick.now)
            state.last_error = error
            tick.result["failed"].append(f"cluster:{state.cluster_id}")
        await self.store.put_cluster(state)
        return state

    # -- 소비자 재적용 -----------------------------------------------------------------------

    async def _reapply(self, consumers: list[_Target], tick: _Tick) -> None:
        """판이 다른 문서를 인증 절반이 다른 것부터 다시 렌더한다. 이 틱에 남은 몫과 시간까지다.
        렌더 번호만 다른 문서는, 그 네임스페이스에서 이 틱에 다시 렌더한 문서가(인증 절반이 다른
        문서도 센다) `REAPPLY_PER_NAMESPACE` 개에 이르면 다음 틱으로 미룬다. 인증 절반이 다른
        문서(회수 등)는 그 수에 막히지 않는다."""
        due = sorted((c for c in consumers if c.due(tick.now)), key=_Target.order)
        for c in due:
            await tick_item()
            if tick.budget <= 0 or tick.out_of_time():
                return
            key = (c.doc.cluster_id, c.namespace)
            if not c.urgent and tick.per_namespace[key] >= REAPPLY_PER_NAMESPACE:
                continue
            tick.budget -= 1
            tick.per_namespace[key] += 1
            await self._reapply_one(c, tick)

    async def _reapply_one(self, c: _Target, tick: _Tick) -> None:
        try:
            done = await c.service.reapply_auth(c.doc)
        except Exception as e:  # noqa: BLE001 - 한 소비자의 실패가 틱을 멈추면 안 된다
            logger.warning("reapply_auth %s %s: %s", c.service.profile_kind, c.doc.id, e)
            await self._hold(c, e, tick)
            return
        if done:
            c.doc.mark_rendered(c.want, c.epoch)
            tick.result["reapplied"].append(c.doc.id)

    # -- 턴 위임 토큰의 재료 (P53 2·3.5절) -------------------------------------------------------

    async def _record_applied_audiences(self, tick: _Tick) -> None:
        """자기 audience 로 옮겨 간 MCP Server 가운데 그 audience 로 검증하는 렌더가 처음 보인
        서버에 그 때(`AudienceSwitch.applied_at`)를 적는다. 옮겨 간 서버의 기대 판은 모두 자기
        audience 표시를 실으므로(`own_audience_rev`), 적힌 판의 첫 절반이 기대 판이거나 표시 하나만
        다른 판이면 그 렌더가 표시를 실은 것이다. 이 틱에 다시 렌더한 문서도 든다. 한 번 적으면
        다른 인증 입력이 바뀌어도 두고, 그 사이 문서가 살아 있는 상태를 벗어났으면 적지 않는다."""
        for c in tick.consumers:
            switch = getattr(c.doc, "audience_switch", None)
            if c.service.auth_kind != KIND_MCP_SERVER or not own_audience(switch):
                continue
            assert switch is not None
            if switch.applied_at is not None or c.doc.auth_rev not in {c.want, *c.paced}:
                continue
            c.doc.audience_switch = switch.model_copy(update={"applied_at": tick.now})
            saved = await c.service.instances.set_fields(
                c.doc.id, fields_of(c.doc, {"audience_switch"}), expect_state=c.service.live_states
            )
            if not saved:
                c.doc.audience_switch = switch

    async def _judge_turn_gates(
        self, clusters: list[Cluster], scans: dict[str, _Scan], read_at: datetime
    ) -> None:
        """클러스터마다의 턴 위임 게이트 판정을 적는다. 이 틱이 읽은 배포 상태 문서로 세고, 이 틱에
        다시 렌더한 문서는 그 렌더의 번호로 센다. 노출 Agent 는 데이터 플레인이 플랫폼을 믿는
        모드에서만 센다. 판정의 때는 문서를 읽기 시작한 read_at 이다."""
        trusts_platform = self.settings.data_plane_trusts_platform
        verdicts = judge(clusters, _live_docs(scans), trusts_platform=trusts_platform, now=read_at)
        await self.store.put_turn_gates(verdicts)

    async def _hold(self, c: _Target, e: Exception, tick: _Tick) -> None:
        """실패를 문서에 적는다. 요청 오류는 기대 판이나 문서가 바뀔 때까지 막고(`AuthBlocked`), 그
        밖의 실패는 백오프로 미룬다. 그 사이 문서가 그 종류의 살아 있는 상태(`live_states`)를
        벗어났으면 적지 않는다."""
        code = request_error_code(e)
        if code is not None:
            c.doc.auth_blocked = AuthBlocked(
                rev=c.want or "",
                epoch=c.epoch,
                code=code,
                spec_change=c.doc.last_spec_change,
                detail=request_error_detail(e),
            )
        else:
            c.doc.backoff(tick.now)
        saved = await c.service.instances.set_fields(
            c.doc.id, fields_of(c.doc, AUTH_FIELDS), expect_state=c.service.live_states
        )
        if saved:
            tick.result["blocked" if code is not None else "failed"].append(c.doc.id)

    async def _report(self, tick: _Tick) -> None:
        """요약 문서를 적는다. 로그 한 줄은 기동 뒤 첫 틱, 무언가를 처리한 틱, 숫자가 바뀐 틱에만
        남긴다."""
        summary = tick.summary()
        await self.store.put_summary(summary)
        counts = (len(tick.result["reapplied"]), summary.failed, summary.blocked, summary.pending)
        if self._last_counts is None or any(tick.result.values()) or counts != self._last_counts:
            logger.info("reapply_auth reapplied=%d failed=%d blocked=%d pending=%d", *counts)
        self._last_counts = counts

    # -- local 모드의 기동 경고 ---------------------------------------------------------------

    async def warn_ignored_groups(self) -> None:
        """local 모드 기동 때 한 번. 저장된 mcp_group 은 이 모드에서 쓰이지 않는다(P32 2절).
        oidc 로 되돌리면 다시 쓰이므로 값은 지우지 않는다. 그런 테넌트의 소비자는 판이 바뀌어
        다음 리컨실에서 daegam-<tenant> 로 다시 렌더된다."""
        custom = await self.tenancy.tenant_settings.custom_mcp_groups()
        for tenant, group in sorted(custom.items()):
            logger.warning(
                "tenant %s mcp_group %r is ignored in local auth mode; its MCP group is %s",
                tenant,
                group,
                default_mcp_group(tenant),
            )
        if custom:
            await self._warn_lost_permits(set(custom.values()))

    async def _warn_lost_permits(self, custom: set[str]) -> None:
        """사용자 정의 그룹을 authz_policy 에 THVGroup 으로 적은 Gateway 는 그 허용이 조용히
        사라지므로 이름과 함께 알린다. 다른 테넌트의 고정 그룹(daegam-<tenant>)은 그 테넌트의 토큰이
        여전히 실으므로 뺀다. 테넌트는 설정 문서나 배포된 Gateway 가 있는 곳이다."""
        gateways = await self.gateways.instances.live()
        tenants = set(await self.tenancy.tenant_settings.list_tenants())
        tenants.update(gw.tenant for gw in gateways)
        groups = sorted(custom - {default_mcp_group(t) for t in tenants})
        for gw in gateways:
            policy = gw.deployed.authz_policy if gw.deployed else None
            named = [g for g in groups if policy and names_group(policy, g)]
            if named:
                logger.warning(
                    "gateway %s/%s authz_policy permits THVGroup %s, which no token carries in "
                    "local auth mode",
                    gw.tenant,
                    gw.name,
                    ", ".join(named),
                )

    # -- 상태 API -----------------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """`GET /api/settings/data-plane-auth` 의 본문. 요약과 클러스터별 상태는 마지막 틱이 적은
        문서이고, 막힌 소비자와 옛 런타임의 노출 Agent 수는 지금 계산한다."""
        tick = await self._new_tick(None)
        platform = await self.tenancy.platform_settings.get()
        blocked: list[dict[str, str | None]] = []
        kinds: dict[str, dict[str, dict[str, int]]] = {}
        fallbacks: dict[str, dict[str, int]] = {}
        handovers: dict[str, dict[str, int]] = {}
        stale = 0
        stale_ca = 0
        cert_expires_at = None
        cert_expiring = False
        home: Cluster | None = None
        clusters = await self.tenancy.clusters.list()
        scans: dict[str, _Scan] = {}
        for cluster in clusters:
            if cluster.is_home:
                home = cluster
                cert_expires_at = (
                    cluster.status.preflight.shipped_redis_cert_expires_at
                    if cluster.status.preflight is not None
                    else None
                )
                cert_expiring = shipped_redis_cert_expiring(cluster.status.preflight)
            scan = scans[cluster.id] = await self._scan(cluster, tick)
            blocked.extend(_blocked_row(c) for c in scan.consumers if c.blocked)
            kinds[cluster.id] = _kind_counts(scan.consumers, tick.now)
            if counts := _fallback_counts(cluster, scan.consumers):
                fallbacks[cluster.id] = counts
            if counts := self._handover_counts(scan.consumers):
                handovers[cluster.id] = counts
            stale += await self._stale_runtimes(cluster, platform, scan, tick)
            stale_ca += self._stale_ca(platform, scan)
        summary = await self.store.summary()
        plain = await plain_redis_consumers(
            self.agents.instances,
            self.mcp_servers.instances,
            self.agents.data_services,
            self.tenancy.clusters,
            shipped=shipped_at(self.settings.home_redis_tls, home),
        )
        return {
            "install_id": tick.install_id,
            "summary": summary.model_dump(mode="json") if summary else None,
            "clusters": [s.model_dump(mode="json") for s in await self.store.clusters()],
            "render_epochs": dict(RENDER_EPOCHS),
            "kinds": kinds,
            "fallbacks": fallbacks,
            "handovers": handovers,
            "entry_conversions": await self._conversion_status(clusters, tick.now),
            "redis_plain_consumers": {
                tenant: [plain_row(r) for r in rows] for tenant, rows in by_tenant(plain).items()
            },
            "shipped_redis_cert_expires_at": cert_expires_at,
            "shipped_redis_cert_expiring": cert_expiring,
            "blocked": blocked,
            "exposed_agents_stale_runtime": stale,
            "mcp_servers_stale_ca": stale_ca,
            "turn_tokens": await self._turn_tokens_status(clusters, scans, tick.now),
            "data_plane_issuer": await self._issuer_status(clusters, scans, tick.now),
        }

    def _handover_counts(self, consumers: list[_Target]) -> dict[str, int]:
        """클러스터 하나에서 노출 앞단을 넘겨받는 중인 MCP Server 를 단계마다 센다 (P54 11절,
        `MCPServerService.handover_step`). 그런 서버가 없는 단계는 뺀다."""
        steps = (
            self.mcp_servers.handover_step(c.doc)
            for c in consumers
            if c.service is self.mcp_servers
        )
        return dict(Counter(str(step) for step in steps if step is not None))

    async def _conversion_status(
        self, clusters: list[Cluster], now: datetime
    ) -> dict[str, dict[str, Any]]:
        """클러스터마다 프록시를 둔 채 남은 허브 항목과 remote 복제본, 옮기지 않고 둔 항목과 그
        까닭 (P54b 7절, `EntryConversion.status`). 남은 것이 없는 클러스터는 빠진다."""
        if self.entry_conversion is None:
            return {}
        return await self.entry_conversion.status(clusters, now)

    async def _issuer_status(
        self, clusters: list[Cluster], scans: dict[str, _Scan], now: datetime
    ) -> dict[str, Any] | None:
        """발급자 전환의 상태 (P53b 6.9절). 클러스터마다 살아 있는 문서와, 그 가운데 판이 달라
        다시 렌더를 기다리는 소비자를 넘긴다. 막힌 소비자(`AuthBlocked`)도 넘긴다. 옛 렌더로 도는
        동안은 전환이 끝나지 않았기 때문이다."""
        if self.issuer_switch is None:
            return None
        behind = {
            cid: [
                Behind(c.service.auth_kind, c.doc, blocked=c.blocked)
                for c in scan.consumers
                if not c.current
            ]
            for cid, scan in scans.items()
        }
        return await self.issuer_switch.status(clusters, _live_docs(scans), behind, now)

    async def _turn_tokens_status(
        self, clusters: list[Cluster], scans: dict[str, _Scan], now: datetime
    ) -> dict[str, Any]:
        """턴 위임 토큰의 상태 (P53 7절). 클러스터마다 판 검사(지금 계산), 마지막 틱이 적은 Gateway
        게이트와 그 판정이 젊은지, 게이트를 닫은 문서와 그 까닭이다. 그리고 Authz policy 에 forbid
        문장이 있는 Gateway(글자 검사, 소유자가 정책을 읽는다)와, 토큰 인증 모드에서 Tool 소스를
        쓰는데 런타임 기록에 `turn_tokens` 가 없는 Agent 다."""
        stored = {g.cluster_id: g for g in await self.store.turn_gates()}
        gates = []
        for cluster in clusters:
            verdict = stored.get(cluster.id)
            gates.append(
                {
                    "cluster_id": cluster.id,
                    "version_ok": version_ok(cluster),
                    "gateway_gate_open": verdict is not None and verdict.gateway_gate_open,
                    "verdict_at": verdict.verdict_at if verdict else None,
                    "verdict_fresh": self.turn_gates.fresh(verdict, now),
                    "holders": [h.model_dump() for h in verdict.holders] if verdict else [],
                }
            )
        forbid: list[dict[str, str | None]] = []
        unsupported: list[dict[str, str | None]] = []
        trusts_platform = self.settings.data_plane_trusts_platform
        for scan in scans.values():
            for service, doc in scan.docs:
                pinned = doc.deployed if service.auth_kind in (KIND_GATEWAY, KIND_AGENT) else None
                if pinned is None:
                    continue
                if service.auth_kind == KIND_GATEWAY and holds_forbid(pinned.authz_policy):
                    forbid.append(doc_row(doc))
                elif (
                    service.auth_kind == KIND_AGENT
                    and self.settings.token_auth
                    and agent_graph.turn_token_sources(pinned, trusts_platform=trusts_platform)
                    and not doc.image_reports(TURN_TOKENS)
                ):
                    unsupported.append(doc_row(doc))
        return {
            "clusters": gates,
            "authz_forbid_gateways": forbid,
            "agents_without_turn_tokens": unsupported,
        }

    async def _stale_runtimes(
        self, cluster: Cluster, platform: PlatformSettings, scan: _Scan, tick: _Tick
    ) -> int:
        """배포 때 고정한 에이전트 이미지가 지금 설정으로 다시 계산한 것과 다른 노출 Agent 수.
        재적용은 런타임 이미지를 바꾸지 않으므로(D26) 사람이 다시 배포할 몫이다."""
        count = 0
        for service, doc in scan.docs:
            if service is not self.agents or not doc.exposure.enabled:
                continue
            if doc.state not in EXPOSED_AGENT_STATES:
                continue
            ts = await tick.settings_of(doc.tenant)
            want = resolve_effective(platform, cluster, ts).images.agent_image
            count += EffectiveConfig.model_validate(doc.effective_config).images.agent_image != want
        return count

    def _stale_ca(self, platform: PlatformSettings, scan: _Scan) -> int:
        """배포가 적은 CA 묶음(`effective_config.ca_bundle`)이 지금 `network.ca_bundle` 과 다른 MCP
        Server 수. 재적용은 스냅샷으로 렌더하므로 뺀 CA 도 다시 배포할 때까지 믿는다 (D88)."""
        want = platform.network.ca_bundle
        return sum(
            1
            for service, doc in scan.docs
            if service is self.mcp_servers
            and doc.state in service.live_states
            and (doc.effective_config or {}).get("ca_bundle") != want
        )


def internal_server(kind: str, doc: Any) -> bool:
    """서비스 토큰을 검증하는 우리 MCPServer 인가 (P50 9절). MCP Server, 허브 `image` 항목,
    `image` 원본의 공유 복제본 기록이다."""
    if kind == KIND_MCP_SERVER:
        return True
    if kind == KIND_HUB_ITEM:
        return getattr(doc, "kind", None) == "image"
    if kind == KIND_CLONE:
        return (getattr(doc, "source", None) or {}).get("kind") == "image"
    return False


def _blocked_row(c: _Target) -> dict[str, str | None]:
    return {
        "kind": c.service.auth_kind,
        "id": c.doc.id,
        "tenant": c.doc.tenant,
        "cluster_id": c.doc.cluster_id,
        "code": c.doc.auth_blocked.code,
        "detail": c.doc.auth_blocked.detail,
    }


def _fallback_counts(cluster: Cluster, consumers: list[_Target]) -> dict[str, int]:
    """클러스터 하나에서 게이트를 지나지 못해 기능을 뺀 대체 형태로 렌더하는 살아 있는 문서 수를
    기능마다 센다 (P50 2절). 그런 문서가 없는 기능은 뺀다."""
    out: dict[str, int] = {}
    passing = passing_features(cluster)
    for feature, kinds in FALLBACK_KINDS.items():
        if feature in passing:
            continue
        count = sum(
            c.service.auth_kind in kinds
            and _renders_feature(feature, c.service.auth_kind, c.doc, passing)
            for c in consumers
        )
        if count:
            out[str(feature)] = count
    return out


def _kind_counts(consumers: list[_Target], now: datetime) -> dict[str, dict[str, int]]:
    """클러스터 하나의 종류마다 막히지 않았는데 판이 다른 문서(pending), 그 가운데 백오프 중인
    문서(failed), 막힌 문서(blocked)의 수. 판이 맞은 문서만 있는 종류는 세 수가 모두 0 이다. 이름은
    요약(`AuthSummary`)과 같다."""
    out: dict[str, dict[str, int]] = {}
    for c in consumers:
        row = out.setdefault(c.service.auth_kind, {"pending": 0, "failed": 0, "blocked": 0})
        if c.blocked:
            row["blocked"] += 1
        elif not c.current:
            row["pending"] += 1
            row["failed"] += c.waiting(now)
    return out
