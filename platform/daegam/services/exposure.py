"""외부 노출 (resource-model.md 11절, D22).

노출 옵션 조회, 생성 본문 `exposure` 의 검증(호스트 정규화, host_reserved·host_conflict,
IngressClass, TLS Secret, cert-manager, 무인증 노출), 앞단 프록시·Ingress·NetworkPolicy 문서 묶음,
노출 상태를 담당한다.
MCP Server 와 Gateway 가 같은 규칙을 쓴다. ToolHive 오퍼레이터에는 ingress·host·TLS
기능이 없으므로(CRD 실물) 플랫폼이 `networking.k8s.io/v1` Ingress 를 렌더링한다.

Agent(D27)는 같은 호스트·IngressClass·TLS 검증을 `require_oidc=True` 로 쓰고(header 모드는
Cluster allow_unauthenticated 와 무관하게 422), 결과를 `agent_exposure_from` 으로 `AgentExposure`
(cors_origins·allowed_groups)로 옮긴다. 문서 묶음은 k8s/templates/agent.py 가 렌더한다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from kubernetes.client.rest import ApiException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from daegam.auth.cedar import (
    FRONT_TARGET,
    delegation_forbid,
    render_authz_inline,
    tenant_policies,
)
from daegam.config.resolve import EffectiveConfig
from daegam.errors import ServiceError
from daegam.k8s.client import AccessDenied, ClusterUnreachable, KubeClient
from daegam.k8s.templates import (
    INGRESS_API,
    MCP_EXPOSE_PATHS,
    PROXY_PORT,
    TOOLHIVE_API,
    VMCP_PORT,
    expose_policy_name,
    expose_proxy_name,
    ingress_tls,
    proxy_service_name,
    proxy_service_url,
    render_expose_policy,
    render_ingress,
    render_remote_proxy,
    vmcp_service_name,
)
from daegam.k8s.templates.authz import (
    AUTHZ_CONFIG_KIND,
    authz_config_name,
    authz_config_ref,
    render_authz_config,
)
from daegam.k8s.templates.svc_token import (
    RESOURCE_EXPOSE_AUTHZ,
    RESOURCE_EXPOSE_SVC_TOKEN,
    SVC_TOKEN_REV_ANNOTATION,
    expose_svc_name,
    render_external_auth_config,
    render_svc_token_secret,
)
from daegam.services.claims import tick_item
from daegam.services.clusters import ClusterService
from daegam.services.data_plane_auth import KIND_AGENT, KIND_GATEWAY, KIND_MCP_SERVER
from daegam.services.tenancy import TenancyError, TenancyService, k8s_name
from daegam.store.agents import Agent
from daegam.store.base import iso_utc
from daegam.store.clusters import Cluster, ClusterIngress
from daegam.store.exposure import (
    AgentExposure,
    Exposure,
    ExposureTLS,
    normalize_cors_origin,
    tls_secret_name_for,
)
from daegam.store.exposure_hosts import HostClaim, MongoExposureHostStore
from daegam.store.gateways import Gateway
from daegam.store.mcp_servers import MCPServer
from daegam.store.name_claims import CLAIM_GRACE, HeldClaim, MongoNameClaimStore

logger = logging.getLogger("daegam.exposure")

# 옛 host 의 선점을 놓지 않는 상태. 재배포가 도는 중이거나 실패한 배포는 새 노출을 아직 적용하지
# 않았을 수 있어 옛 Ingress 가 그 host 로 답한다
_HOLDING_STATES = frozenset({"deploying", "failed"})

CERT_MANAGER_GV = "cert-manager.io/v1"
TLS_SECRET_TYPE = "kubernetes.io/tls"
# 파드 라벨 (실측, orbstack ToolHive v0.51.2. 0.46.0 과 같다)
FRONT_KIND_PROXY = "mcpremoteproxy"
FRONT_KIND_VMCP = "virtualmcpserver"
RESOURCE_EXPOSE_PROXY = "mcp-instance-expose"
RESOURCE_MCP_INGRESS = "mcp-instance-ingress"
RESOURCE_GATEWAY_INGRESS = "gateway-instance-ingress"
RESOURCE_MCP_POLICY = "mcp-instance-expose-policy"
RESOURCE_GATEWAY_POLICY = "gateway-instance-expose-policy"


def unauthenticated_warnings(exposure: Exposure, *, token_auth: bool) -> list[str]:
    """header 모드에서 노출을 켠 인스턴스에 붙는 경고.

    MCP·Gateway 만 쓴다. Agent 는 `validate_exposure(require_oidc=True)` 라 header 모드에서
    노출 자체가 422 이고 이 경고가 나올 자리가 없다."""
    return [UNAUTHENTICATED_WARNING] if exposure.enabled and not token_auth else []


class HasExposure(Protocol):
    """상태 응답의 공통 뼈대가 문서에서 읽는 것. Agent·Gateway·MCP Server 가 다 갖고 있다."""

    exposure: Any
    deploying_since: datetime | None


def status_base(
    doc: HasExposure, observed_at: datetime, *, warnings: list[str], error: str | None
) -> dict[str, Any]:
    """status 응답에서 세 리소스가 공통으로 내는 키들.

    Agent·Gateway·MCP Server 가 같은 아홉 키를 각자 적고 있었다. `warnings` 와 `error` 는 리소스마다
    달라 인자로 받는다. Agent 는 노출이 oidc 전용이라 미인증 경고가 나올 자리가 없어 빈 목록이고,
    상태를 못 읽었을 때 쓰는 `error` 도 Agent 만 문서의 `last_error` 를 처음부터 담는다.

    나머지 키(`url`·`backend_count`·`ready_replicas` 처럼 리소스마다 다른 것)는 호출부가 더한다."""
    return {
        "phase": None,
        "external_url": doc.exposure.external_url,
        "exposure": None,
        "message": None,
        "conditions": [],
        "warnings": warnings,
        "observed_at": iso_utc(observed_at),
        # 화면이 "예상보다 오래 걸립니다" 를 판정하는 기준점 (D43 둘째). 이 필드가 생기기 전부터
        # `deploying` 이던 문서는 null 이고, 그때 화면은 경과 시간 없이 진행 배너만 보인다
        "since": iso_utc(doc.deploying_since) if doc.deploying_since else None,
        "error": error,
    }


UNAUTHENTICATED_WARNING = (
    "unauthenticated_exposure: the platform runs in header mode, so this endpoint is exposed "
    "without authentication (cluster ingress.allow_unauthenticated is true)"
)

_HOST_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class ExposureError(ServiceError):
    pass


class ExposureInput(BaseModel):
    """생성 본문·Basic 폼의 `exposure`. 비어 있는 값은 Cluster ingress 기본값으로 채운다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str | None = None
    ingress_class: str | None = None
    tls: ExposureTLS | None = None


class AgentExposureInput(ExposureInput):
    """Agent 의 deploy·redeploy 본문 `exposure` (D27). 브라우저 origin 과 인가 그룹이
    더 있다. 형식 검사는 agent_exposure_from 이 한다 (422 invalid_cors_origin)."""

    cors_origins: list[str] = Field(default_factory=list)
    allowed_groups: list[str] = Field(default_factory=list)


def agent_exposure_from(exposure: Exposure, inp: AgentExposureInput | None) -> AgentExposure:
    """validate_exposure 가 돌려준 공용 스냅샷을 AgentExposure 로 옮긴다. origin 은 소문자로
    정규화하고 경로가 있거나 scheme 이 http(s) 가 아니면 422 invalid_cors_origin, 그룹은 공백을 떼고
    빈 값을 버린다. 노출을 껐으면 둘 다 비어 있다."""
    base = exposure.model_dump()
    if not exposure.enabled or inp is None:
        return AgentExposure(**base)
    origins: list[str] = []
    for raw in inp.cors_origins:
        try:
            origin = normalize_cors_origin(raw)
        except ValueError as e:
            raise ExposureError(422, "invalid_cors_origin", str(e)) from e
        if origin not in origins:
            origins.append(origin)
    groups: list[str] = []
    for raw in inp.allowed_groups:
        group = (raw or "").strip()
        if group and group not in groups:
            groups.append(group)
    return AgentExposure(**base, cors_origins=origins, allowed_groups=groups)


def exposure_from_input(inp: ExposureInput | None) -> Exposure:
    """클러스터 검증 없이 입력을 설정 스냅샷으로 옮긴다. 복제가 받는 `exposure` 가 이 모양으로
    draft 에 남고, 배포 때 `_validate_exposure` 가 클러스터를 보고 다시 검증한다."""
    if inp is None or not inp.enabled:
        return Exposure()
    return Exposure(
        enabled=True,
        host=inp.host,
        ingress_class=inp.ingress_class,
        tls=inp.tls if inp.tls is not None else ExposureTLS(),
    )


def exposure_to_input(exposure: Exposure) -> ExposureInput | None:
    """draft 에 남은 설정을 배포 검증의 입력으로 되돌린다. 꺼져 있으면 None."""
    if not exposure.enabled:
        return None
    return ExposureInput(
        enabled=True,
        host=exposure.host,
        ingress_class=exposure.ingress_class,
        tls=exposure.tls,
    )


def agent_exposure_to_input(exposure: AgentExposure) -> AgentExposureInput | None:
    """Agent 문서에 남은 노출 설정을 배포 검증의 입력으로 되돌린다. origin·그룹까지 옮긴다.
    꺼져 있으면 None."""
    if not exposure.enabled:
        return None
    return AgentExposureInput(
        enabled=True,
        host=exposure.host,
        ingress_class=exposure.ingress_class,
        tls=exposure.tls,
        cors_origins=list(exposure.cors_origins),
        allowed_groups=list(exposure.allowed_groups),
    )


# -- 호스트 --------------------------------------------------------------------------


def normalize_host(raw: str) -> str:
    """소문자 DNS-1123 호스트명. 뒤의 점은 뗀다. 아니면 422 invalid_host."""
    host = (raw or "").strip().lower().rstrip(".")
    labels = host.split(".")
    if not host or len(host) > 253 or any(not _HOST_LABEL_RE.fullmatch(x) for x in labels):
        raise ExposureError(
            422, "invalid_host", f"host {raw!r} is not a lowercase DNS-1123 hostname"
        )
    return host


def default_host(tenant: str, name: str, ingress: ClusterIngress) -> str | None:
    """이 인스턴스의 host 기본값. 화면이 받는 `default_host_template` 에 이름을 채운 것이라
    정규화 규칙이 한 곳이다."""
    template = default_host_template(tenant, ingress.domain)
    return template.replace("{name}", k8s_name(name)) if template else None


# 노출을 갖는 리소스의 종류. 인증 재적용의 소비자 종류와 같은 이름이다
_EXPOSED_KINDS: dict[type, str] = {
    MCPServer: KIND_MCP_SERVER,
    Gateway: KIND_GATEWAY,
    Agent: KIND_AGENT,
}


@dataclass(frozen=True)
class LiveExposure:
    """살아 있는 노출 하나. `resource` 는 MCPServer·Gateway·Agent 문서이고 `kind` 는 그 종류다."""

    kind: str
    resource: Any

    @property
    def host(self) -> str:
        return self.resource.exposure.host

    @property
    def external_url(self) -> str:
        return self.resource.exposure.external_url


async def live_exposures(stores: list[Any], *, host: str | None = None) -> list[LiveExposure]:
    """모든 클러스터의 살아 있는 노출. stores 는 MCP·Gateway·Agent 스토어(`live`·`live_on_host`)
    세 종류다. host 유일성, OAuth 의 resource 풀기, 기동 때의 중복 로그가 이 반복 하나를 쓴다
    (D27, P33 6절). host 를 주면 그 host 의 노출만 인덱스로 읽는다. 인증 없는 인가 요청이 문서
    전체를 읽게 하지 않으려는 것이다.

    None 이 섞이면 그 종류가 통째로 빠져 두 인스턴스가 같은 host 를 가져간다. 조용히 건너뛰지
    않고 터뜨린다. 배선 누락이 조용한 불변식 손실이 되면 안 된다."""
    found: list[LiveExposure] = []
    for store in stores:
        if store is None:
            raise RuntimeError(
                "host 유일성 검사에 배선되지 않은 인스턴스 스토어가 있다. 이대로면 그 종류의 "
                "host 를 못 보고 중복 노출을 허용한다 (wiring.build_services 를 본다)"
            )
        for inst in await (store.live() if host is None else store.live_on_host(host)):
            if inst.exposure.enabled and inst.exposure.host:
                found.append(LiveExposure(_EXPOSED_KINDS[type(inst)], inst))
    return found


async def hosts_in_use(stores: list[Any], *, exclude_id: str | None = None) -> set[str]:
    """살아 있는 인스턴스가 테넌트와 클러스터를 넘어 쓰는 host. 노출 host 는 플랫폼 전체에서
    유일하다. OAuth 의 resource 가 테넌트 하나로 풀리는 전제다 (P33 6절)."""
    return {e.host for e in await live_exposures(stores) if e.resource.id != exclude_id}


# -- 검증 ----------------------------------------------------------------------------


def _exists(client: KubeClient, api_version: str, kind: str, name: str) -> bool:
    """클러스터 스코프 리소스의 존재 확인. get 이 아니라 list(metadata) + 이름 필터다."""
    items = client.list_metadata(api_version, kind, field_selector=f"metadata.name={name}")
    return any((i.get("metadata") or {}).get("name") == name for i in items)


def _check_auth_mode(cluster: Cluster, *, token_auth: bool, require_oidc: bool) -> list[str]:
    """무인증 외부 경로를 열어도 되는가. 열어도 되면 경고만 돌려준다.

    require_oidc 는 Agent 규칙(D27)이다. header 모드면 Cluster allow_unauthenticated 와
    무관하게 422 다. 런타임에 검증기가 없어 외부 요청이 전부 401 이 되므로 무인증 외부 경로
    자체를 두지 않는다."""
    if token_auth:
        return []
    if require_oidc:
        raise ExposureError(
            422, "unauthenticated_exposure", "Agent exposure requires oidc or local auth mode"
        )
    if not cluster.ingress.allow_unauthenticated:
        raise ExposureError(
            422,
            "unauthenticated_exposure",
            "the platform runs in header mode (no OIDC), so the exposed endpoint would be "
            f"unauthenticated; cluster {cluster.name} does not allow that "
            "(ingress.allow_unauthenticated)",
        )
    return [UNAUTHENTICATED_WARNING]


def _resolve_host(
    inp: ExposureInput, *, tenant: str, name: str, ing: ClusterIngress, taken: set[str]
) -> str:
    """쓸 호스트. 안 주면 클러스터 도메인으로 만든다. 플랫폼이 쓰는 예약 이름이면 422
    host_reserved, 다른 배포가 쓰고 있으면 409 host_conflict 다 (P74 2.3절)."""
    raw_host = inp.host or default_host(tenant, name, ing)
    if not raw_host:
        raise ExposureError(
            422, "invalid_host", "exposure.host is required (cluster has no ingress.domain)"
        )
    host = normalize_host(raw_host)
    if host in {_reserved_form(h) for h in ing.reserved_hosts}:
        raise ExposureError(422, "host_reserved", f"host {host} is reserved for the platform")
    if host in taken:
        raise ExposureError(
            409, "host_conflict", f"host {host} is already exposed on this platform"
        )
    return host


def _reserved_form(raw: str) -> str:
    """예약 호스트를 요청 호스트와 같은 꼴(소문자, 앞뒤 공백·끝 점 없음)로 맞춘다. 검증은 하지
    않는다. 잘못 적힌 항목은 어떤 호스트와도 맞지 않을 뿐 노출 배포를 모두 막지 않는다."""
    return raw.strip().lower().rstrip(".")


def _resolve_ingress_class(inp: ExposureInput, *, ing: ClusterIngress, client: KubeClient) -> str:
    """쓸 IngressClass. 클러스터 허용 목록 안이어야 하고 실제로 있어야 한다."""
    ingress_class = inp.ingress_class or ing.class_name
    if not ingress_class:
        raise ExposureError(
            422, "invalid_value", "exposure.ingress_class is required (cluster has no default)"
        )
    if ing.allowed_classes and ingress_class not in ing.allowed_classes:
        raise ExposureError(
            422,
            "ingress_class_not_allowed",
            f"IngressClass {ingress_class} is not in the cluster's allowed list",
            allowed=list(ing.allowed_classes),
        )
    if not _exists(client, INGRESS_API, "IngressClass", ingress_class):
        raise ExposureError(
            422, "unknown_ingress_class", f"IngressClass {ingress_class} does not exist"
        )
    return ingress_class


def _resolve_tls(
    inp: ExposureInput, *, ing: ClusterIngress, client: KubeClient, namespace: str
) -> ExposureTLS:
    """TLS 네 갈래. 안 주면 클러스터 기본값에서 시작하고, 갈래마다 필요한 것이 실제로 있는지
    본다. 돌려주는 것은 그 갈래에 필요한 필드만 담은 새 값이다.

    edge 는 Ingress 앞이 TLS 를 끝낸다는 클러스터의 선언이다(D54). 플랫폼은 앞단이 있는지 스스로 알
    수 없으므로 클러스터 기본값이 edge 일 때만 받고, 그 클러스터에서는 edge 만 받는다. 그러지 않으면
    앞단이 없는 곳에 https 주소가 나가거나, 앞단이 https 로 돌려보내는 곳에 http 주소가 나간다."""
    tls = inp.tls or ExposureTLS(mode=ing.tls, issuer=ing.default_issuer)
    if (ing.tls == "edge") != (tls.mode == "edge"):
        allowed = ["edge"] if ing.tls == "edge" else ["none", "secret", "cert-manager"]
        raise ExposureError(
            422,
            "tls_mode_not_allowed",
            f"exposure.tls.mode {tls.mode} is not allowed on this cluster "
            f"(cluster default TLS is {ing.tls})",
            allowed=allowed,
        )
    if tls.mode == "edge":
        return ExposureTLS(mode="edge")
    if tls.mode == "secret":
        if not tls.secret_name:
            raise ExposureError(422, "tls_secret_invalid", "exposure.tls.secret_name is required")
        found = client.list_metadata(
            "v1",
            "Secret",
            namespace,
            field_selector=f"metadata.name={tls.secret_name},type={TLS_SECRET_TYPE}",
        )
        if not found:
            raise ExposureError(
                422,
                "tls_secret_invalid",
                f"Secret {namespace}/{tls.secret_name} is missing or not type {TLS_SECRET_TYPE}",
            )
        return ExposureTLS(mode="secret", secret_name=tls.secret_name)
    if tls.mode == "cert-manager":
        if not cert_manager_available(client):
            raise ExposureError(
                422,
                "cert_manager_unavailable",
                "cert-manager (ClusterIssuer CRD) is not installed on this cluster",
            )
        issuer = tls.issuer or ing.default_issuer
        if not issuer:
            raise ExposureError(
                422, "invalid_value", "exposure.tls.issuer is required for cert-manager"
            )
        if not _exists(client, CERT_MANAGER_GV, "ClusterIssuer", issuer):
            raise ExposureError(422, "unknown_issuer", f"ClusterIssuer {issuer} does not exist")
        return ExposureTLS(mode="cert-manager", issuer=issuer)
    return ExposureTLS()


def validate_exposure(
    inp: ExposureInput | None,
    *,
    tenant: str,
    name: str,
    cluster: Cluster,
    namespace: str,
    client: KubeClient,
    token_auth: bool,
    taken: set[str],
    require_oidc: bool = False,
) -> tuple[Exposure, list[str]]:
    """(스냅샷, warnings). 동기 함수라 호출부가 threadpool 에서 돌린다. 오류는 ExposureError 422.

    네 규칙을 순서대로 본다. 인증 모드 게이트, 호스트 결정·유일성, IngressClass, TLS 다.
    뒤의 둘은 클러스터를 읽으므로 RBAC 이 모자라면 403 forbidden 으로 옮긴다."""
    if inp is None or not inp.enabled:
        return Exposure(), []
    ing = cluster.ingress
    if not ing.enabled:
        raise ExposureError(
            422, "ingress_disabled", f"cluster {cluster.name} does not allow exposure"
        )
    warnings = _check_auth_mode(cluster, token_auth=token_auth, require_oidc=require_oidc)
    host = _resolve_host(inp, tenant=tenant, name=name, ing=ing, taken=taken)
    try:
        # 존재 확인은 list(metadata, 이름 필터)로 한다. RBAC 은 ingressclasses·clusterissuers 에
        # list 만 주고 get 은 주지 않는다 (preflight REQUIRED_VERBS 와 같다)
        ingress_class = _resolve_ingress_class(inp, ing=ing, client=client)
        tls = _resolve_tls(inp, ing=ing, client=client, namespace=namespace)
    except AccessDenied as e:
        raise ExposureError(
            403, "forbidden", f"the cluster client may not {e.missing}", missing=[e.missing]
        ) from e
    return Exposure(enabled=True, host=host, ingress_class=ingress_class, tls=tls), warnings


# -- 문서 묶음 -------------------------------------------------------------------------


def mcp_expose_docs(
    *,
    tenant: str,
    instance_id: str,
    k8s_name: str,
    namespace: str,
    exposure: Exposure,
    effective: EffectiveConfig,
    owner: dict[str, Any] | None,
    oidc_ref: dict[str, Any] | None = None,
    tenant_group: str | None = None,
    agent_client_ids: list[str] | None = None,
    client_id_claim: str,
    groups_claim: str | None,
    svc_token: str | None = None,
    svc_token_rev: str = "0",
    service_account: str | None = None,
    authz_by_ref: bool = False,
    binds_delegation: bool = False,
    rules: Sequence[str] = (),
    roles_claim: str | None = None,
) -> list[dict[str, Any]]:
    """MCP Server 노출 묶음이다. (서비스 토큰 Secret,) MCPExternalAuthConfig, 앞단
    MCPRemoteProxy expose-<k8s.name>, Ingress, NetworkPolicy 순이다. oidc_ref(resourceUrl 이
    external_url 인 사람 참조)가 있으면(발급자 있는 모드) 앞단에 그것과 Cedar 를 건다. 앞단은 모든
    모드에서 안쪽 MCPServer 에 서비스 토큰을 낸다(D41). svc_token 이 있으면 Secret 문서도
    낸다(배포 경로). 미리보기는 토큰 없이 렌더한다. binds_delegation(local 모드)이면 앞단에 묶이지
    않은 위임 토큰을 막는 forbid 를 더한다. 앞단에 묶인 토큰은 아무도 내지 않으므로 모든 턴 토큰이
    거절된다 (P53 3.2절). groups_claim 은 앞단의 ToolHive 가 그룹을 읽을 클레임이다
    (`ConsumerAuth.groups_claim`). rules 는 서버의 Tool 규칙 forbid 이고 roles_claim 은 역할 절이
    있을 때의 role_claim_name 이다 (P52 2·3절)."""
    front = expose_proxy_name(k8s_name)
    svc_name = expose_svc_name(k8s_name)
    authz = authz_ref = None
    docs: list[dict[str, Any]] = []
    if oidc_ref:
        policies = tenant_policies(tenant_group or "", agent_client_ids or [], client_id_claim)
        if binds_delegation:
            policies.append(delegation_forbid(FRONT_TARGET.format(instance_id), tenant))
        policies += rules
        if authz_by_ref:
            # G8 게이트를 지난 클러스터는 정책을 앞단의 MCPAuthzConfig 에 싣고 참조한다 (P50 8절)
            docs.append(
                render_authz_config(
                    cr_name=front,
                    namespace=namespace,
                    policies=policies,
                    tenant=tenant,
                    resource=RESOURCE_EXPOSE_AUTHZ,
                    id=instance_id,
                    groups_claim=groups_claim,
                    owner=owner,
                    roles_claim=roles_claim,
                )
            )
            authz_ref = authz_config_ref(front)
        else:
            authz = render_authz_inline(policies, groups_claim, roles_claim)
    ing = effective.ingress
    tls_secret, issuer = ingress_tls(exposure, k8s_name)
    if svc_token:
        docs.append(
            render_svc_token_secret(
                name=svc_name,
                namespace=namespace,
                token=svc_token,
                tenant=tenant,
                resource=RESOURCE_EXPOSE_SVC_TOKEN,
                id=instance_id,
                owner=owner,
            )
        )
    docs.append(
        render_external_auth_config(
            name=svc_name,
            namespace=namespace,
            secret_name=svc_name,
            mode="bearer",
            tenant=tenant,
            resource=RESOURCE_EXPOSE_SVC_TOKEN,
            id=instance_id,
            owner=owner,
        )
    )
    return docs + [
        render_remote_proxy(
            tenant=tenant,
            item_id=instance_id,
            name=front,
            namespace=namespace,
            url=proxy_service_url(k8s_name, namespace),
            transport="streamable-http",
            resource=RESOURCE_EXPOSE_PROXY,
            oidc_config_ref=oidc_ref,
            authz_config=authz,
            authz_config_ref=authz_ref,
            trust_proxy_headers=True,
            audit=True,
            owner=owner,
            external_auth_config=svc_name,
            pod_annotations={SVC_TOKEN_REV_ANNOTATION: svc_token_rev},
            service_account=service_account,
        ),
        render_ingress(
            tenant=tenant,
            instance_id=instance_id,
            resource=RESOURCE_MCP_INGRESS,
            name=k8s_name,
            namespace=namespace,
            host=exposure.host or "",
            ingress_class=exposure.ingress_class or "",
            service_name=proxy_service_name(front, remote=True),
            port=PROXY_PORT,
            tls_secret=tls_secret,
            cluster_issuer=issuer,
            annotations=ing.annotations,
            owner=owner,
            paths=MCP_EXPOSE_PATHS,
        ),
        render_expose_policy(
            tenant=tenant,
            instance_id=instance_id,
            resource=RESOURCE_MCP_POLICY,
            name=expose_policy_name(k8s_name),
            namespace=namespace,
            front_name=front,
            front_kind=FRONT_KIND_PROXY,
            controller_namespace=ing.controller_namespace,
            controller_ip_blocks=ing.controller_ip_blocks,
            owner=owner,
        ),
    ]


def gateway_expose_docs(
    *,
    tenant: str,
    instance_id: str,
    k8s_name: str,
    namespace: str,
    exposure: Exposure,
    effective: EffectiveConfig,
    owner: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Gateway 노출: Ingress(대상 vmcp-<k8s.name>:4483)와 NetworkPolicy. 인증은 vMCP 자신의
    incomingAuth(oidcConfigRef.resourceUrl)가 맡는다."""
    ing = effective.ingress
    tls_secret, issuer = ingress_tls(exposure, k8s_name)
    return [
        render_ingress(
            tenant=tenant,
            instance_id=instance_id,
            resource=RESOURCE_GATEWAY_INGRESS,
            name=k8s_name,
            namespace=namespace,
            host=exposure.host or "",
            ingress_class=exposure.ingress_class or "",
            service_name=vmcp_service_name(k8s_name),
            port=VMCP_PORT,
            tls_secret=tls_secret,
            cluster_issuer=issuer,
            annotations=ing.annotations,
            owner=owner,
            paths=MCP_EXPOSE_PATHS,
        ),
        render_expose_policy(
            tenant=tenant,
            instance_id=instance_id,
            resource=RESOURCE_GATEWAY_POLICY,
            name=expose_policy_name(k8s_name),
            namespace=namespace,
            front_name=k8s_name,
            front_kind=FRONT_KIND_VMCP,
            controller_namespace=ing.controller_namespace,
            controller_ip_blocks=ing.controller_ip_blocks,
            owner=owner,
        ),
    ]


def expose_object_refs(k8s_name: str, *, front_proxy: bool) -> list[tuple[str, str, str]]:
    """삭제 단계에서 이름으로 지울 (apiVersion, kind, name). ownerReference 로 GC 되지만 한 번 더
    지운다."""
    refs = [
        (INGRESS_API, "Ingress", k8s_name),
        (INGRESS_API, "NetworkPolicy", expose_policy_name(k8s_name)),
    ]
    if front_proxy:
        front = expose_proxy_name(k8s_name)
        refs.append((TOOLHIVE_API, "MCPRemoteProxy", front))
        refs.append((TOOLHIVE_API, AUTHZ_CONFIG_KIND, authz_config_name(front)))
        refs.append((TOOLHIVE_API, "MCPExternalAuthConfig", expose_svc_name(k8s_name)))
        refs.append(("v1", "Secret", expose_svc_name(k8s_name)))
    return refs


def delete_expose_objects(
    client: KubeClient, k8s_name: str, namespace: str, *, front_proxy: bool
) -> None:
    for api_version, kind, name in expose_object_refs(k8s_name, front_proxy=front_proxy):
        client.delete(api_version, kind, name, namespace)


# -- 상태 --------------------------------------------------------------------------------


def exposure_status(
    client: KubeClient,
    *,
    k8s_name: str,
    namespace: str,
    exposure: Exposure,
    front_proxy: bool,
) -> dict[str, Any] | None:
    """{front_phase, address_assigned, tls_secret_present, certificate_ready}. 노출하지 않았으면
    None. 조회 실패는 필드를 None 으로 둔다 (예외 없음)."""
    if not exposure.enabled:
        return None
    out: dict[str, Any] = {
        "front_phase": None,
        "address_assigned": None,
        "tls_secret_present": None,
        "certificate_ready": None,
    }
    try:
        if front_proxy:
            cr = client.get(TOOLHIVE_API, "MCPRemoteProxy", expose_proxy_name(k8s_name), namespace)
            out["front_phase"] = ((cr or {}).get("status") or {}).get("phase") if cr else "Missing"
        ingress = client.get(INGRESS_API, "Ingress", k8s_name, namespace)
        if exposure.tls.mode == "edge":
            # 앞단이 공개하는 클러스터는 LoadBalancer 가 없어 Ingress 상태에 주소가 쓰이지 않는다.
            # 판정하지 않는다(None). 화면은 "앞단이 공개" 로 보인다
            out["address_assigned"] = None
        elif ingress is not None:
            lb = ((ingress.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
            out["address_assigned"] = bool(lb)
        else:
            out["address_assigned"] = False
        secret_name = None
        if exposure.tls.mode == "secret":
            secret_name = exposure.tls.secret_name
        elif exposure.tls.mode == "cert-manager":
            secret_name = tls_secret_name_for(k8s_name)
        if secret_name:
            found = client.list_metadata(
                "v1",
                "Secret",
                namespace,
                field_selector=f"metadata.name={secret_name},type={TLS_SECRET_TYPE}",
            )
            out["tls_secret_present"] = bool(found)
        if exposure.tls.mode == "cert-manager":
            cert = client.get(
                CERT_MANAGER_GV, "Certificate", tls_secret_name_for(k8s_name), namespace
            )
            conds = ((cert or {}).get("status") or {}).get("conditions") or []
            out["certificate_ready"] = any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in conds
            )
    except (AccessDenied, ClusterUnreachable):
        pass
    except Exception:  # noqa: BLE001 - cert-manager CRD 없음 등. 상태 조회가 인스턴스 조회를 막지 않는다
        pass
    return out


# -- 옵션 조회 ---------------------------------------------------------------------------


def ingress_class_names(client: KubeClient, cluster: Cluster) -> list[str]:
    """클러스터의 IngressClass 이름. Cluster ingress.allowed_classes 가 있으면 그 안으로 거른다."""
    names = [
        (i.get("metadata") or {}).get("name") or ""
        for i in client.list_metadata(INGRESS_API, "IngressClass")
    ]
    names = sorted(n for n in names if n)
    allowed = cluster.ingress.allowed_classes
    if allowed:
        names = [n for n in names if n in allowed]
    return names


def tls_secret_names(client: KubeClient, namespace: str) -> list[str]:
    """그 네임스페이스의 type=kubernetes.io/tls Secret 이름. metadata 만 받는다 (data 없음).

    네임스페이스가 아직 없으면(그 테넌트의 첫 배포 전) 안에 Secret 도 없으므로 묻지 않고
    빈 목록이다. 목록을 읽는 RoleBinding(daegam-tls-reader)은 첫 배포가 네임스페이스와 함께
    깔기 때문에, 그 전에 물으면 403 이 나고 폼이 "rbac.yaml 을 다시 적용하라" 는 틀린 안내를
    보인다. 네임스페이스를 볼 권한이 없으면 전처럼 목록을 묻는다."""
    try:
        if client.get("v1", "Namespace", namespace) is None:
            return []
    except AccessDenied:
        pass
    items = client.list_metadata(
        "v1", "Secret", namespace, field_selector=f"type={TLS_SECRET_TYPE}"
    )
    return sorted(n for n in ((i.get("metadata") or {}).get("name") for i in items) if n)


def cert_manager_available(client: KubeClient) -> bool:
    try:
        return "clusterissuers" in client.discover(CERT_MANAGER_GV)
    except (ClusterUnreachable, ApiException):
        # 연결하지 못했거나 그 그룹을 볼 수 없다(권한, 프록시의 4xx). 둘 다 없는 것으로 본다
        return False


def cluster_issuer_names(client: KubeClient) -> list[str]:
    """cert-manager ClusterIssuer 이름. CRD 가 없으면 빈 목록."""
    if not cert_manager_available(client):
        return []
    items = client.list_metadata(CERT_MANAGER_GV, "ClusterIssuer")
    return sorted(n for n in ((i.get("metadata") or {}).get("name") for i in items) if n)


def default_host_template(tenant: str, domain: str | None) -> str | None:
    """폼의 host 기본값. `{name}` 자리에 인스턴스 이름(DNS-1123 정규화)이 들어간다.

    테넌트·도메인 정규화가 `default_host` 와 같아야 한다. 다르면 화면이 채워 준 기본값을
    서버가 422 invalid_host 로 거부한다(테넌트 이름에는 대문자·밑줄이 올 수 있다)."""
    if not domain:
        return None
    return f"{{name}}.{k8s_name(tenant)}.{domain.strip('.').lower()}"


async def exposure_options(
    tenancy: TenancyService,
    cluster_service: ClusterService,
    cluster: Cluster,
    tenant: str,
    namespace: str | None,
) -> dict[str, Any]:
    """생성 폼이 클러스터·네임스페이스를 고른 뒤 부르는 노출 옵션. Secret 은 이름만 나간다."""
    allowed = await tenancy.allowed_namespaces(tenant, cluster)
    try:
        ns = await tenancy.check_namespace(tenant, cluster, namespace)
    except TenancyError as e:
        raise ExposureError(e.status, e.code, e.detail) from e
    ing = cluster.ingress
    try:
        client = await run_in_threadpool(cluster_service.client_for, cluster)
        classes = await run_in_threadpool(ingress_class_names, client, cluster)
        secrets = await run_in_threadpool(tls_secret_names, client, ns)
        issuers = await run_in_threadpool(cluster_issuer_names, client)
    except AccessDenied as e:
        raise ExposureError(
            403,
            "forbidden",
            f"the cluster client may not {e.missing}; apply deploy rbac.yaml "
            "(daegam-tls-reader RoleBinding for this namespace) and retry",
            missing=[e.missing],
        ) from e
    except ClusterUnreachable as e:
        raise ExposureError(502, "cluster_unreachable", str(e)) from e
    default_class = ing.class_name if ing.class_name in classes else None
    if default_class is None and len(classes) == 1:
        default_class = classes[0]
    return {
        "enabled": ing.enabled,
        "namespaces": allowed,
        "namespace": ns,
        "ingress_classes": classes,
        "tls_secrets": secrets,
        "cluster_issuers": issuers,
        "defaults": {
            "host": default_host_template(tenant, ing.domain),
            "ingress_class": default_class,
            "tls_mode": ing.tls if ing.tls != "cert-manager" or issuers else "none",
            "issuer": ing.default_issuer if ing.default_issuer in issuers else None,
        },
        "allow_unauthenticated": ing.allow_unauthenticated,
    }


class ExposureValidationMixin:
    """노출 검증의 공통 골격. 인스턴스 서비스 셋이 섞어 쓴다.

    규칙 자체가 노출 도메인의 것이라 여기 산다(`advanced_edit` 는 편집 흐름만 안다).
    리소스마다 다른 것은 아래 훅 넷뿐이고, 나머지 절차는 셋이 같다."""

    require_oidc_exposure: bool = False  # Agent 는 header 모드 노출을 아예 막는다 (D27)
    exposure_hosts: MongoExposureHostStore  # 공개 host 의 선점 기록 (P70). 생성자가 채운다

    def _sibling_stores(self) -> list[Any]:
        """host 유일성을 함께 보는 다른 인스턴스 스토어. 배선이 채운다 (D27)."""
        raise NotImplementedError

    async def platform_exposures(self, host: str | None = None) -> list[LiveExposure]:
        """플랫폼 전체의 살아 있는 노출. host 를 주면 그 host 의 것만이다. host 유일성과 같은 스토어
        목록을 본다. OAuth 의 resource 풀기와 기동 때의 중복 로그가 부른다 (P33 6절)."""
        return await live_exposures(self._sibling_stores(), host=host)

    def _wrap_exposure(self, exposure: Exposure, inp: ExposureInput | None) -> Any:
        """저장 형식이 리소스마다 다르면 여기서 옮긴다. 기본은 그대로."""
        return exposure

    def _empty_exposure(self) -> Any:
        """노출을 껐을 때의 빈 스냅샷."""
        return Exposure()

    def _claim_stores(self) -> list[MongoNameClaimStore[Any]]:
        """해제와 삭제가 놓는 이 종류의 선점 기록. 셋 모두 공개 host 이고 Gateway 는 Entry 이름을
        더한다."""
        return [self.exposure_hosts]

    async def _held_claims(self, inst: Any) -> list[HeldClaim]:
        """해제의 마지막 저장 앞에서 읽어 두는 그 리소스의 선점 (P70 2절). 저장 뒤 읽은 nonce 로
        지우므로 draft 로 바뀐 바로 뒤 다시 선점한 배포의 기록은 남는다."""
        return [
            (store, claim)
            for store in self._claim_stores()
            for claim in await store.of_resource(inst.id)
        ]

    async def _release_claims(self, held: list[HeldClaim]) -> None:
        for store, claim in held:
            await store.remove(claim.id, claim.nonce)

    async def _release_all_claims(self, inst: Any) -> None:
        """문서를 지운 뒤 그 리소스의 선점을 모두 지운다. 지운 문서 뒤에 올 배포가 없다."""
        for store in self._claim_stores():
            await store.remove_resource(inst.id)

    @asynccontextmanager
    async def _host_claimed(self, inst: Any, *, claim: bool = True) -> AsyncIterator[None]:
        """inst 의 새 노출을 저장하는 동안 그 host 를 선점한다 (P70 2절). 다른 리소스가 쥐고 있으면
        409 host_conflict 다. 블록 안에서 무엇이든 실패하면(저장의 409 포함) 이 요청이 새로 넣은
        기록만 지운다. 자기 host 를 다시 선점한 기록은 그대로 둔다.

        읽기 검사(`hosts_in_use`)는 지금 저장된 노출만 본다. host 를 막 옮긴 배포의 옛 host 는
        읽기 검사로는 비었어도 쓸어 가기 전까지 선점이 남아 있다. 그래서 detail 은 상대를 밝히지
        않고 그 경우까지 함께 말한다."""
        exposure = inst.exposure
        if not claim or not exposure.enabled or not exposure.host:
            yield
            return
        host = normalize_host(exposure.host)
        got = await self.exposure_hosts.claim(
            host, kind=_EXPOSED_KINDS[type(inst)], resource_id=inst.id, tenant=inst.tenant
        )
        if got is None:
            raise self._error(
                409,
                "host_conflict",
                f"host {host} is in use, or held until the deployment that used it finishes "
                "moving off it",
            )
        claim, inserted = got
        try:
            yield
        except BaseException:
            if inserted:
                await self.exposure_hosts.remove(host, claim.nonce)
            raise

    async def settle_host_claims(
        self, now: datetime, *, sweep: bool = True
    ) -> dict[str, list[str]]:
        """선점 기록을 살아 있는 노출에 맞춘다 (P70 2절). 먼저 쓸고(sweep) 다음에 기록 없는 노출에
        기록을 준다(adopt). 쓸기는 리컨실 리스 안에서만 돈다. 기동은 리스 밖이라 기록을 주기만
        한다. 쓰는 것은 모두 조건부라 둘이 함께 돌아도 해가 없다.

        살아 있는 노출은 스토어마다 한 번 읽는다. 맵에 없는 리소스는 지워졌거나 draft 다. 정규화되지
        않는 옛 host 는 경고하고 건너뛴다(그 host 를 이름에 두지 않은 것으로 본다)."""
        live = {doc.id: doc for store in self._sibling_stores() for doc in await store.live()}
        removed: list[str] = []
        if sweep:
            for claim in await self.exposure_hosts.all():
                # 리컨실 리스를 항목마다 늘린다 (P67)
                await tick_item()
                if now - claim.claimed_at < CLAIM_GRACE:
                    continue
                if _claim_unheld(live.get(claim.resource_id), claim) and (
                    await self.exposure_hosts.remove(claim.id, claim.nonce)
                ):
                    removed.append(claim.id)
        adopted: list[str] = []
        for doc in live.values():
            host = _stored_host(doc)
            if host is None:
                continue
            if await self.exposure_hosts.adopt(
                host,
                kind=_EXPOSED_KINDS[type(doc)],
                resource_id=doc.id,
                tenant=doc.tenant,
                now=now,
            ):
                adopted.append(host)
        return {"released": removed, "adopted": adopted}

    async def _validate_exposure(
        self,
        inp: ExposureInput | None,
        *,
        tenant: str,
        name: str,
        cluster: Cluster,
        namespace: str,
        exclude_id: str | None = None,
    ) -> tuple[Any, list[str]]:
        """(Exposure 스냅샷, warnings). 클러스터 조회가 필요한 검증(IngressClass·Secret·
        cert-manager)은 threadpool 에서 돈다. 노출을 끄면 클러스터에 닿지 않는다."""
        if inp is None or not inp.enabled:
            return self._empty_exposure(), []
        taken = await hosts_in_use(self._sibling_stores(), exclude_id=exclude_id)
        try:
            client = await run_in_threadpool(self.cluster_service.client_for, cluster)
            exposure, warnings = await run_in_threadpool(
                validate_exposure,
                inp,
                tenant=tenant,
                name=name,
                cluster=cluster,
                namespace=namespace,
                client=client,
                token_auth=self.token_auth,
                taken=taken,
                require_oidc=self.require_oidc_exposure,
            )
            # 옮기는 것도 검증이다. Agent 의 cors_origins 는 여기서 422 invalid_cors_origin 이 된다
            return self._wrap_exposure(exposure, inp), warnings
        except ExposureError as e:
            raise self._error(e.status, e.code, e.detail, **e.extra) from e
        except ClusterUnreachable as e:
            raise self._error(502, "cluster_unreachable", str(e)) from e


def _stored_host(doc: Any) -> str | None:
    """살아 있는 문서가 쓰는 노출 host 의 정규형. 노출이 꺼졌거나 host 가 없으면 None 이다. 정규화
    규칙보다 먼저 저장된 host 가 규칙에 맞지 않으면 경고하고 None 이다. 기동과 리컨실의 한 틱이
    문서 하나 때문에 멈추면 안 된다."""
    exposure = doc.exposure
    if not exposure.enabled or not exposure.host:
        return None
    try:
        return normalize_host(exposure.host)
    except ExposureError:
        logger.warning(
            "exposure host %r of %s does not normalize; no claim for it", exposure.host, doc.id
        )
        return None


def _claim_unheld(doc: Any, claim: HostClaim) -> bool:
    """claim 의 리소스가 그 host 를 더는 쥐지 않는가. doc 은 살아 있는 문서 맵에서 찾은 것이다.
    없으면(지워졌거나 draft) 참이고, 그 host 를 이름에 두지 않으면서 deploying·failed 가 아니면
    참이다."""
    if doc is None:
        return True
    return _stored_host(doc) != claim.id and doc.state not in _HOLDING_STATES
