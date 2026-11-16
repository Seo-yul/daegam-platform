"""테넌트 ↔ 클러스터 ↔ 네임스페이스. Data Service(F4)와 MCP Server(F6)가 함께 쓴다."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.config.resolve import EffectiveNamespace, resolve_effective
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient, platform_subject, retry_forbidden
from daegam.k8s.gates import tenant_object_rights, tenant_secret_refusal
from daegam.k8s.labels import (
    LABEL_ID,
    LABEL_MANAGED_BY,
    LABEL_PLATFORM_ID,
    LABEL_RESOURCE,
    LABEL_TENANT,
    MANAGED_BY,
    REV_LENGTH,
    ensure_labels,
    stable_hash,
    stamp_desired_hash,
)
from daegam.k8s.preflight import (
    RBAC_API,
    RBAC_GROUP,
    TENANT_OBJECTS_BINDING_NAME,
    TENANT_OBJECTS_CLUSTER_ROLE,
    TLS_READER_CLUSTER_ROLE,
)
from daegam.k8s.templates.common import (
    INTERNAL_OIDC_CONFIG_NAME,
    NETWORK_POLICY_API,
    OIDC_CONFIG_NAME,
    TOOLHIVE_API,
    opaque_secret,
    tenant_ingress_from,
)
from daegam.k8s.templates.sandbox import PROBE_NAMESPACE, render_isolation_policy
from daegam.k8s.templates.tenant_policies import (
    EGRESS_POLICY_NAME,
    egress_policy_wanted,
    render_backend_policy,
    render_egress_policy,
    tenant_ingress_selector,
)
from daegam.names import group_name_for, k8s_name
from daegam.services.clusters import ClusterService
from daegam.store.clusters import DEFAULT_REDIS_CA_SECRET, Cluster, MongoClusterStore
from daegam.store.data_services import REDIS_CA_KEY
from daegam.store.settings import MongoPlatformSettingsStore
from daegam.store.tenants import NAMESPACE_RE, MongoTenantSettingsStore

logger = logging.getLogger("daegam.tenancy")

NETWORK_POLICY_NAME = "daegam-tenant-ingress"
RESOURCE_NETWORK_POLICY = "tenant-network-policy"
# 네임스페이스 정책 묶음(`namespace_policies`)의 판. 테넌트 ingress 정책이 싣고, 인증 재적용의
# 스윕이 이 판이 다른 네임스페이스에 묶음을 다시 둔다. 렌더를 바꾼 판이 옛 네임스페이스에도 닿는다
ANNOTATION_NAMESPACE_POLICIES = "daegam.devops.ai.kr/namespace-policies-hash"
# 허용 네임스페이스마다 두는 RoleBinding (ClusterRole daegam-tls-reader → 클러스터 클라이언트 자신)
ROLE_BINDING_NAME = "daegam-tls-reader"
# 테넌트 객체 (P50 2절·10절). 플랫폼이 테넌트 네임스페이스에 MCPAuthzConfig·ServiceAccount·Secret
# 을 쓰는 권한의 RoleBinding(`TENANT_OBJECTS_BINDING_NAME`, preflight 가 읽는다)과, 원격 프록시가
# 쓰는 Role 없는 ServiceAccount 다
REMOTE_PROXY_SERVICE_ACCOUNT = "daegam-remote-proxy"
# 내부 OIDC 설정에 싣는 테넌트 객체의 판. 객체 판(`daegam.devops.ai.kr/desired-hash`)에는 들지
# 않으므로 이 판이 달라도 설정의 동기화와 문서의 재렌더는 막히지 않는다. 스윕은 이 판으로 두 객체가
# 맞춰졌는지 본다 (P50 1절)
ANNOTATION_TENANT_OBJECTS = "daegam.devops.ai.kr/tenant-objects-hash"
# 두 MCPOIDCConfig 의 daegam.devops.ai.kr/resource 값. 인증 재적용이 이 라벨로 클러스터의
# 객체를 찾는다
RESOURCE_OIDC_CONFIG = "tenant-oidc-config"
RESOURCE_INTERNAL_OIDC_CONFIG = "tenant-internal-oidc-config"


class TenancyError(ServiceError):
    pass


# 테넌트가 자기 네임스페이스로 삼을 수 없는 이름. k8s 자신의 것과 오퍼레이터의 것, 그리고
# 배포된 플랫폼의 기본 네임스페이스다. 이 프로세스의 네임스페이스는 설정에 있어
# `check_namespace_name` 이 따로 받는다. 설치본에 붙여 띄운 로컬 플랫폼이 다른 네임스페이스(예:
# daegam-dev-system)를 써도 라이브의 daegam-system 을 테넌트로 잡지 못하게 늘 막는다. 격리
# 점검의 네임스페이스(P40 8절)도 테넌트가 잡으면 그 테넌트의 파드가 점검 곁에 서므로 막는다
RESERVED_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "toolhive-system",
        "daegam-system",
        PROBE_NAMESPACE,
    }
)


def check_namespace_name(name: str, *, reserved: Collection[str]) -> str:
    """테넌트가 쓸 네임스페이스 이름 검사. DNS 라벨이어야 하고 플랫폼·k8s·오퍼레이터의
    네임스페이스는 못 쓴다. reserved 는 이 설치의 것(`Settings.reserved_namespaces`: 플랫폼과 LLM
    게이트웨이의 네임스페이스)이다.

    `ensure_namespace` 가 그 네임스페이스에 테넌트 라벨을 SSA 로 붙이고 RoleBinding 을 만들기
    때문이다. `daegam-system` 을 자기 네임스페이스로 잡으면 cluster-* Secret 이 사는 곳에 테넌트
    워크로드가 들어가고, 게이트웨이 네임스페이스(템플릿이 테넌트 `llm` 에게 주는 `daegam-llm`)를
    잡으면 모든 provider 키 옆에 들어간다 (P38 3절). 아니면 TenancyError(422 invalid_value /
    namespace_reserved)."""
    if not NAMESPACE_RE.fullmatch(name):
        raise TenancyError(422, "invalid_value", f"invalid namespace name {name!r}")
    if name in reserved or name in RESERVED_NAMESPACES or name.startswith("kube-"):
        raise TenancyError(
            422, "namespace_reserved", f"namespace {name} belongs to the platform or Kubernetes"
        )
    return name


class TenancyService:
    def __init__(
        self,
        clusters: MongoClusterStore,
        cluster_service: ClusterService,
        platform_settings: MongoPlatformSettingsStore,
        tenant_settings: MongoTenantSettingsStore,
    ):
        self.clusters = clusters
        self.cluster_service = cluster_service
        self.platform_settings = platform_settings
        self.tenant_settings = tenant_settings

    # -- 클러스터 선택 -------------------------------------------------------

    async def resolve_cluster(self, tenant: str, cluster_id: str | None) -> Cluster:
        """명시 cluster_id > 테넌트 기본 > 플랫폼 기본 > home. 테넌트 허용 목록을 검사한다."""
        ts = await self.tenant_settings.get(tenant)
        if cluster_id is None:
            cluster_id = ts.default_cluster_id
        if cluster_id is None:
            cluster_id = (await self.platform_settings.get()).default_cluster_id
        if cluster_id is None:
            home = await self.cluster_service.home()
            if home is None:
                raise TenancyError(422, "no_cluster", "no cluster_id given and no default cluster")
            cluster = home
        else:
            cluster = await self.clusters.get(cluster_id)
            if cluster is None:
                raise TenancyError(422, "unknown_cluster", f"cluster {cluster_id} not found")
        if ts.allowed_cluster_ids is not None and cluster.id not in ts.allowed_cluster_ids:
            raise TenancyError(
                422, "cluster_not_allowed", f"cluster {cluster.name} not allowed for {tenant}"
            )
        return cluster

    # -- 네임스페이스 ----------------------------------------------------------

    async def namespace_for(self, tenant: str, cluster: Cluster) -> EffectiveNamespace:
        eff = resolve_effective(
            await self.platform_settings.get(), cluster, await self.tenant_settings.get(tenant)
        )
        return eff.namespace

    async def tenants_on_namespace(self, name: str) -> list[str]:
        """home 클러스터에서 name 을 자기 네임스페이스나 허용 네임스페이스로 삼는 테넌트(설정
        문서가 있는 테넌트, 정렬). 템플릿이 만든 이름도 센다. 예약이 생기기 전부터 그 이름을 쓰던
        테넌트는 업그레이드 뒤 네임스페이스 작업이 422 라, 기동 경고와 게이트웨이 상태가 이것을
        알린다 (P38 3절). home 이 없으면 비어 있다."""
        home = await self.cluster_service.home()
        if home is None:
            return []
        settings = await self.platform_settings.get()
        clashing = []
        for tenant in await self.tenant_settings.list_tenants():
            ts = await self.tenant_settings.get(tenant)
            own = resolve_effective(settings, home, ts).namespace.name
            if name == own or name in ts.allowed_namespaces:
                clashing.append(tenant)
        return clashing

    async def warn_reserved_clashes(self) -> None:
        """기동 때 한 번. 예약한 게이트웨이 네임스페이스를 쓰던 테넌트를 알린다 (P38 3절)."""
        name = self.cluster_service.settings.llm_gateway_namespace
        for tenant in await self.tenants_on_namespace(name):
            logger.warning(
                "tenant %s uses namespace %s, which this install reserves for the LLM gateway; its "
                "namespace operations now fail with namespace_reserved until "
                "DAEGAM_LLM_GATEWAY_NAMESPACE or the tenant's namespace changes",
                tenant,
                name,
            )

    async def allowed_namespaces(self, tenant: str, cluster: Cluster) -> list[str]:
        """인스턴스를 둘 수 있는 네임스페이스. 첫 항목이 기본(테넌트) 네임스페이스이고 나머지는
        Tenant Settings allowed_namespaces 다 (D5)."""
        default = (await self.namespace_for(tenant, cluster)).name
        ts = await self.tenant_settings.get(tenant)
        return [default] + [n for n in ts.allowed_namespaces if n != default]

    async def check_namespace(self, tenant: str, cluster: Cluster, namespace: str | None) -> str:
        """None 이면 기본 네임스페이스, 아니면 허용 목록 안이어야 한다
        (422 namespace_not_allowed)."""
        allowed = await self.allowed_namespaces(tenant, cluster)
        if namespace is None:
            return allowed[0]
        if namespace not in allowed:
            raise TenancyError(
                422,
                "namespace_not_allowed",
                f"namespace {namespace} is not allowed for tenant {tenant} "
                f"(allowed: {', '.join(allowed)})",
            )
        return namespace

    async def ensure_tenant_namespace(self, tenant: str, cluster: Cluster) -> str:
        """테넌트 기본 네임스페이스를 보장하고 이름을 돌려준다."""
        ns = await self.namespace_for(tenant, cluster)
        return await self.ensure_namespace(tenant, cluster, ns)

    async def ensure_namespace(
        self, tenant: str, cluster: Cluster, ns: EffectiveNamespace | str
    ) -> str:
        """Namespace(+라벨), NetworkPolicy 묶음, MCPOIDCConfig(oidc), ResourceQuota/LimitRange
        (있으면), MCPGroup, RoleBinding daegam-tls-reader 를 SSA 로 보장한다. 이름만 주면 테넌트
        기본 네임스페이스의 라벨·쿼터 규칙에 그 이름을 얹는다. 다른 테넌트의
        `daegam.devops.ai.kr/tenant` 라벨이 이미 붙어 있으면 422 namespace_claimed 다 (SSA 가
        라벨을 조용히 덮어 격리를 깨기 때문)."""
        if isinstance(ns, str):
            base = await self.namespace_for(tenant, cluster)
            ns = base.model_copy(update={"name": ns})
        # 이름이 어디서 왔든(Tenant Settings·템플릿·allowed_namespaces) 여기서 한 번 더 본다.
        # 라벨을 붙이고 RoleBinding 을 만드는 자리라 마지막 문이다
        check_namespace_name(ns.name, reserved=self.cluster_service.settings.reserved_namespaces)
        client = await run_in_threadpool(self.cluster_service.client_for, cluster)
        await run_in_threadpool(check_namespace_claim, client, tenant, ns.name)
        subject = await run_in_threadpool(platform_subject, client)
        await run_in_threadpool(
            ensure_namespace_objects,
            client,
            tenant,
            ns,
            platform_namespace=self.cluster_service.settings.namespace,
            install_id=await self.platform_settings.install_id(),
            tls_reader_subject=subject,
            tenant_objects=tenant_objects_for(cluster, tenant, ns.name, subject),
            egress=egress_policy_wanted(cluster),
            **oidc_kwargs(self.cluster_service.settings),
        )
        return ns.name


def check_namespace_claim(client: KubeClient, tenant: str, name: str) -> None:
    """네임스페이스에 다른 테넌트의 daegam.devops.ai.kr/tenant 라벨이 있으면
    TenancyError(422 namespace_claimed)."""
    existing = client.get("v1", "Namespace", name)
    if existing is None:
        return
    owner = ((existing.get("metadata") or {}).get("labels") or {}).get(LABEL_TENANT)
    if owner and owner != tenant:
        raise TenancyError(
            422,
            "namespace_claimed",
            f"namespace {name} is labeled {LABEL_TENANT}={owner}; it belongs to another tenant",
        )


def ensure_namespace_objects(
    client: KubeClient,
    tenant: str,
    ns: EffectiveNamespace,
    *,
    platform_namespace: str = "daegam-system",
    install_id: str | None = None,
    tls_reader_subject: dict[str, Any] | None = None,
    tenant_objects: list[dict[str, Any]] | None = None,
    egress: bool = False,
    **oidc: Any,
) -> None:
    """Namespace, NetworkPolicy 묶음(`ensure_namespace_policies`), 테넌트 객체와 두 MCPOIDCConfig
    (`ensure_oidc_configs`), Quota/LimitRange, MCPGroup, RoleBinding daegam-tls-reader(subject 가
    있을 때) 순으로 SSA 한다. tenant_objects 는 `tenant_objects_for` 가 낸 목록이고, 그 적용이
    실패해도 배포는 계속된다 (P50 1절). egress 는 egress 정책을 바라는지다(`egress_policy_wanted`).
    oidc 는 `oidc_kwargs` 가 내는 인자다."""
    labels = {**ns.labels}
    client.apply(
        ensure_labels(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": ns.name, "labels": labels},
            },
            tenant=tenant,
            resource="tenant-namespace",
            id=tenant,
        )
    )
    ensure_namespace_policies(client, tenant, ns.name, platform_namespace, egress=egress)
    ensure_oidc_configs(client, tenant, ns.name, install_id, tenant_objects=tenant_objects, **oidc)
    if ns.quota:
        client.apply(
            ensure_labels(
                {
                    "apiVersion": "v1",
                    "kind": "ResourceQuota",
                    "metadata": {"name": "daegam-tenant-quota", "namespace": ns.name},
                    "spec": {"hard": ns.quota},
                },
                tenant=tenant,
                resource="tenant-quota",
                id=tenant,
            )
        )
    if ns.limit_range:
        client.apply(
            ensure_labels(
                {
                    "apiVersion": "v1",
                    "kind": "LimitRange",
                    "metadata": {"name": "daegam-tenant-limits", "namespace": ns.name},
                    "spec": ns.limit_range,
                },
                tenant=tenant,
                resource="tenant-limits",
                id=tenant,
            )
        )
    client.apply(mcp_group_manifest(tenant, ns.name))
    if tls_reader_subject is not None:
        client.apply(tls_reader_binding_manifest(tenant, ns.name, tls_reader_subject))


def _namespace_role_binding(
    tenant: str, namespace: str, subject: dict[str, Any], *, name: str, role: str, resource: str
) -> dict[str, Any]:
    """바인딩 없는 ClusterRole role 을 이 네임스페이스에서만 플랫폼 자신(subject)에게 묶는
    RoleBinding name. 플랫폼에는 그 이름 한정 clusterroles bind 와 rolebindings 쓰기만 있어
    escalate 없이 만들 수 있다."""
    return ensure_labels(
        {
            "apiVersion": RBAC_API,
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace},
            "subjects": [dict(subject)],
            "roleRef": {"apiGroup": RBAC_GROUP, "kind": "ClusterRole", "name": role},
        },
        tenant=tenant,
        resource=resource,
        id=tenant,
    )


def tls_reader_binding_manifest(
    tenant: str, namespace: str, subject: dict[str, Any]
) -> dict[str, Any]:
    """허용 네임스페이스 한정 `secrets list` (D22 RBAC). ClusterRole daegam-tls-reader 를 이
    네임스페이스에만 묶는다."""
    return _namespace_role_binding(
        tenant,
        namespace,
        subject,
        name=ROLE_BINDING_NAME,
        role=TLS_READER_CLUSTER_ROLE,
        resource="tenant-tls-reader",
    )


def tenant_objects_binding_manifest(
    tenant: str, namespace: str, subject: dict[str, Any]
) -> dict[str, Any]:
    """MCPAuthzConfig·ServiceAccount 쓰기와 LocalSubjectAccessReview (P50 2절). ClusterRole
    daegam-tenant-objects 를 이 네임스페이스에만 묶는다."""
    return _namespace_role_binding(
        tenant,
        namespace,
        subject,
        name=TENANT_OBJECTS_BINDING_NAME,
        role=TENANT_OBJECTS_CLUSTER_ROLE,
        resource="tenant-objects-binding",
    )


def remote_proxy_service_account_manifest(tenant: str, namespace: str) -> dict[str, Any]:
    """원격 프록시가 쓰는 ServiceAccount (P50 10절). Role 을 묶지 않는다. 프록시가 뜰 때
    Kubernetes 클라이언트를 만들므로 토큰은 마운트한 채 둔다(F0)."""
    return ensure_labels(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": REMOTE_PROXY_SERVICE_ACCOUNT, "namespace": namespace},
        },
        tenant=tenant,
        resource="remote-proxy-account",
        id=tenant,
    )


def tenant_objects_for(
    cluster: Cluster, tenant: str, namespace: str, subject: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """한 네임스페이스에 둘 테넌트 객체(RoleBinding, ServiceAccount)다 (P50 1절·2절). 권한 게이트를
    통과하지 못한 클러스터(`tenant_object_rights`)면 빈 목록을 돌려주어 객체를 만들지 않는다. 권한은
    있는데 플랫폼 자신의 identity 를 얻지 못했으면 None 이다. 그때는 무엇을 둘지 모르므로 객체도 그
    판도 건드리지 않는다. ServiceAccount 는 RoleBinding 이 준 권한으로 만들므로 RoleBinding 이
    먼저다.

    클러스터 기본 Redis 가 TLS 끝점을 가지면 그 CA 의 Secret `daegam-redis-ca` 가 끝에 더해진다
    (P61 2절, `default_redis_ca_ready`). 객체 판에 들므로 CA 가 바뀌면 스윕이 네임스페이스마다 다시
    쓴다."""
    if not tenant_object_rights(cluster):
        return []
    if subject is None:
        return None
    objects = [
        tenant_objects_binding_manifest(tenant, namespace, subject),
        remote_proxy_service_account_manifest(tenant, namespace),
    ]
    if default_redis_ca_ready(cluster):
        objects.append(default_redis_ca_secret(cluster, tenant, namespace))
    return objects


def default_redis_ca_ready(cluster: Cluster) -> bool:
    """클러스터 기본 Redis 의 CA 가 테넌트 네임스페이스에 놓이는가. TLS 끝점이 있고, 테넌트 객체를
    두는 클러스터(`tenant_object_rights`)이며, 테넌트 Secret 을 쓸 수 있다고 확인된 클러스터
    (`tenant_secret_refusal` 이 없음)일 때다. 아니면 세션 저장소는 평문 포트로 렌더한다."""
    redis = cluster.default_redis
    return (
        redis is not None
        and redis.tls is not None
        and tenant_object_rights(cluster)
        and tenant_secret_refusal(cluster) is None
    )


def default_redis_ca_secret(cluster: Cluster, tenant: str, namespace: str) -> dict[str, Any]:
    """클러스터 기본 Redis 의 TLS CA 를 담은 Secret. MCP Server 세션 저장소의
    `sessionStorage.tls.caCertSecretRef` 가 가리킨다. `default_redis_ca_ready` 일 때만 부른다."""
    redis = cluster.default_redis
    if redis is None or redis.tls is None:
        raise ValueError(f"cluster {cluster.id} has no default Redis TLS endpoint")
    return opaque_secret(
        DEFAULT_REDIS_CA_SECRET,
        namespace,
        {REDIS_CA_KEY: redis.tls.ca},
        tenant=tenant,
        resource="redis-ca",
        id=tenant,
    )


def tenant_objects_names(tenant_objects: list[dict[str, Any]]) -> str:
    """테넌트 객체의 종류와 이름. 보장하지 못했을 때의 오류와 경고에 싣는다."""
    return ", ".join(f"{o['kind']} {o['metadata']['name']}" for o in tenant_objects)


def tenant_objects_stamp(tenant_objects: list[dict[str, Any]]) -> str:
    """내부 OIDC 설정이 싣는 테넌트 객체의 판(`ANNOTATION_TENANT_OBJECTS`)."""
    return stable_hash(tenant_objects, REV_LENGTH)


def tenant_objects_stamp_of(metas: list[dict[str, Any]]) -> str | None:
    """한 네임스페이스의 MCPOIDCConfig metadata 가운데 내부 설정이 실은 테넌트 객체의 판. 없으면
    None 이다."""
    for meta in metas:
        if meta.get("name") == INTERNAL_OIDC_CONFIG_NAME:
            return (meta.get("annotations") or {}).get(ANNOTATION_TENANT_OBJECTS)
    return None


def _apply_after_grant(client: KubeClient, manifest: dict[str, Any]) -> None:
    """막 만든 RoleBinding 이 준 권한에 기대는 쓰기다. 403 이면 API 서버의 인가가 그 권한을 받을
    때까지 잠깐씩 다시 한다(`retry_forbidden`)."""
    retry_forbidden(lambda: client.apply(manifest))


def mcp_group_manifest(tenant: str, namespace: str) -> dict[str, Any]:
    return ensure_labels(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPGroup",
            "metadata": {"name": group_name_for(tenant), "namespace": namespace},
            "spec": {"description": f"daegam tenant {tenant}"},
        },
        tenant=tenant,
        resource="tenant-group",
        id=tenant,
    )


def oidc_kwargs(settings: Settings) -> dict[str, Any]:
    """ensure_namespace_objects 에 넘길 OIDC 인자.

    서비스 토큰의 발급자(플랫폼 자신, D41)는 모든 모드에 있다. 사람 토큰의 발급자는 발급자 있는
    토큰 인증(oidc·local)에만 있고, 데이터 플레인이 믿는 발급자다(`Settings.data_plane_issuer_url`).
    플랫폼이 서명한 토큰의 검증기는 JWKS 를 클러스터 안 주소에서 받는다. 서비스 토큰은 모든
    모드에서, 사람 토큰은 데이터 플레인이 플랫폼을 믿을 때(local, platform 인 oidc)다. 그때는
    client id 가 없다. IdP 를 믿는 oidc 의 사람 토큰은 IdP 디스커버리와 UI 의 client id 를 쓴다
    (P32 1절, P53b 6.1절). 발급자나 JWKS 주소가 평문 http(클러스터 안 svc 주소)면
    insecureAllowHTTP 가 붙는다."""
    out: dict[str, Any] = {
        "internal_issuer": settings.platform_issuer_url,
        "internal_jwks_url": settings.service_jwks_url,
        "internal_insecure_http": settings.insecure_platform_issuer,
    }
    issuer = settings.data_plane_issuer_url
    if settings.token_auth and issuer:
        out.update(
            oidc_issuer=issuer,
            oidc_jwks_url=settings.human_jwks_url,
            oidc_client_id=None if settings.data_plane_trusts_platform else settings.ui_client_id,
            oidc_insecure_http=settings.insecure_human_http,
        )
    return out


def oidc_config_manifests(
    tenant: str,
    namespace: str,
    install_id: str | None = None,
    *,
    oidc_issuer: str | None = None,
    oidc_jwks_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_insecure_http: bool = False,
    internal_issuer: str | None = None,
    internal_jwks_url: str | None = None,
    internal_insecure_http: bool = False,
    tenant_objects_stamp: str | None = None,
) -> list[dict[str, Any]]:
    """한 네임스페이스에 둘 MCPOIDCConfig. 사람 토큰용 daegam-oidc 는 oidc_issuer 가 있을
    때만(header 모드는 없다), 서비스 토큰용 daegam-internal-oidc(D41) 는 internal_issuer 가 있을
    때다. 인자는 `oidc_kwargs` 가 낸다. 인증 재적용은 이 목록을 클러스터의 실물과 맞댄다
    (P32 8절). 내부 OIDC 설정은 테넌트 객체의 판(tenant_objects_stamp)을 어노테이션으로 싣되 객체
    판 밖에 둔다. 어노테이션만 바뀐 설정은 run configuration 이 그대로인 MCPServer 를 굴리지
    않는다(P50 F0, 0.51.2 실측)."""
    out: list[dict[str, Any]] = []
    if oidc_issuer:
        out.append(
            oidc_config_manifest(
                tenant,
                namespace,
                oidc_issuer,
                oidc_client_id,
                jwks_url=oidc_jwks_url,
                insecure_http=oidc_insecure_http,
                install_id=install_id,
            )
        )
    if internal_issuer:
        out.append(
            oidc_config_manifest(
                tenant,
                namespace,
                internal_issuer,
                None,
                jwks_url=internal_jwks_url,
                insecure_http=internal_insecure_http,
                name=INTERNAL_OIDC_CONFIG_NAME,
                resource=RESOURCE_INTERNAL_OIDC_CONFIG,
                install_id=install_id,
                tenant_objects_stamp=tenant_objects_stamp,
            )
        )
    return out


def ensure_oidc_configs(
    client: KubeClient,
    tenant: str,
    namespace: str,
    install_id: str | None,
    *,
    tenant_objects: list[dict[str, Any]] | None = None,
    live_stamp: str | None = None,
    **oidc: Any,
) -> bool:
    """테넌트 객체를 먼저, 두 MCPOIDCConfig 를 뒤에 SSA 한다. 네임스페이스 보장과 인증 재적용이
    함께 쓴다. 테넌트 객체가 맞춰졌는지를 돌려준다.

    테넌트 객체의 실패는 설정을 막지 않는다. 내부 설정은 맞춰진 객체의 판을 싣고, 실패했으면
    live_stamp(지금 실물이 실은 판, 모르면 없음)를 그대로 실어 다음 스윕이 다시 보장한다.
    tenant_objects 가 None 이면(무엇을 둘지 모른다) 객체를 건드리지 않고 live_stamp 를 싣는다."""
    stamp, ensured = live_stamp, False
    if tenant_objects is not None:
        try:
            for manifest in tenant_objects:
                _apply_after_grant(client, manifest)
            stamp, ensured = tenant_objects_stamp(tenant_objects), True
        except Exception as e:  # noqa: BLE001 - 테넌트 객체를 쓰는 기능만 그 객체 없이 멈춘다
            logger.warning(
                "tenant objects in %s not ensured (%s): %s",
                namespace,
                tenant_objects_names(tenant_objects),
                e,
            )
    manifests = oidc_config_manifests(
        tenant, namespace, install_id, tenant_objects_stamp=stamp, **oidc
    )
    for manifest in manifests:
        client.apply(manifest)
    return ensured


def oidc_config_manifest(
    tenant: str,
    namespace: str,
    issuer: str,
    client_id: str | None,
    *,
    jwks_url: str | None = None,
    insecure_http: bool = False,
    name: str = OIDC_CONFIG_NAME,
    resource: str = RESOURCE_OIDC_CONFIG,
    install_id: str | None = None,
    tenant_objects_stamp: str | None = None,
) -> dict[str, Any]:
    """테넌트 네임스페이스의 MCPOIDCConfig (D16, P6 F4a). vMCP incomingAuth.oidcConfigRef 가 참조.
    oidcConfigRef 는 같은 네임스페이스만 가리키므로 내용이 같은 복제본을 테넌트마다 둔다.
    audience 는 여기가 아니라 참조 쪽(oidcConfigRef.audience) 에 있다. 서비스 토큰용
    `daegam-internal-oidc`(D41) 도 같은 꼴이고 이름·라벨만 다르다. jwks_url 이 있으면 ToolHive 는
    디스커버리 없이 그 주소에서 키를 받는다 (P32 1절). install_id 가 있으면 설치 라벨
    daegam.devops.ai.kr/platform-id 를 달고, 마지막에 그 렌더의 객체 판을 어노테이션으로 싣는다 (P32
    8절)."""
    inline: dict[str, Any] = {"issuer": issuer}
    if jwks_url:
        inline["jwksUrl"] = jwks_url
    if client_id:
        inline["clientId"] = client_id
    if insecure_http:
        # HTTP issuer 나 jwksUrl 은 이 두 필드 없이는 오퍼레이터가 Valid=False 로 거부한다
        # (docs/archive/reviews/2026-09-02-spike-vmcp-oidc.md 1번,
        # docs/archive/reviews/2026-09-27-spike-p32-jwks.md 1절)
        inline["insecureAllowHTTP"] = True
        inline["jwksAllowPrivateIP"] = True
    manifest = ensure_labels(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPOIDCConfig",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"type": "inline", "inline": inline},
        },
        tenant=tenant,
        resource=resource,
        id=tenant,
    )
    if install_id:
        manifest["metadata"]["labels"][LABEL_PLATFORM_ID] = install_id
    manifest = stamp_desired_hash(manifest)
    if tenant_objects_stamp is not None:
        # 객체 판을 매긴 뒤에 얹어 객체 판에 들지 않게 한다
        manifest["metadata"]["annotations"][ANNOTATION_TENANT_OBJECTS] = tenant_objects_stamp
    return manifest


def network_policy_manifest(tenant: str, namespace: str, platform_namespace: str) -> dict[str, Any]:
    """테넌트 네임스페이스 ingress 정책 (D16·D5). 같은 네임스페이스, 같은 테넌트 라벨
    (`daegam.devops.ai.kr/tenant`)이 붙은 다른 허용 네임스페이스, 플랫폼 네임스페이스에서만
    들어온다. 집행은 CNI 가 NetworkPolicy 를 지원할 때만 된다(preflight `network_policy`). 출처
    목록은 `tenant_ingress_from` 에서 오고, Agent 파드의 정책(`render_agent_policy`)도 같은 목록을
    쓴다. 샌드박스 파드(P40 4절)와 MCP 백엔드 파드(T67)는 고르지 않는다(`tenant_ingress_selector`).
    앞의 것은 격리 정책과 Agent 의 샌드박스 정책만, 뒤의 것은 백엔드 정책만 받는다."""
    return ensure_labels(
        {
            "apiVersion": NETWORK_POLICY_API,
            "kind": "NetworkPolicy",
            "metadata": {"name": NETWORK_POLICY_NAME, "namespace": namespace},
            "spec": {
                "podSelector": tenant_ingress_selector(),
                "policyTypes": ["Ingress"],
                "ingress": [{"from": tenant_ingress_from(tenant, platform_namespace)}],
            },
        },
        tenant=tenant,
        resource=RESOURCE_NETWORK_POLICY,
        id=tenant,
    )


def namespace_policies(
    tenant: str, namespace: str, platform_namespace: str, *, egress: bool = False
) -> list[dict[str, Any]]:
    """테넌트 네임스페이스의 NetworkPolicy 묶음을 적용할 순서대로 낸다. 샌드박스 격리
    (`render_isolation_policy`), MCP 백엔드(`render_backend_policy`), 테넌트 ingress, 그리고 egress
    가 참이면 메타데이터 주소를 막는 egress 정책(`render_egress_policy`, T75)이다.

    좁은 정책이 넓은 정책보다 먼저다. 테넌트 ingress 정책은 샌드박스와 백엔드 파드를 빼므로, 그
    정책이 먼저 바뀌고 뒤의 적용이 실패하면 그 파드는 어느 ingress 정책도 고르지 않아 다 열린다.
    테넌트 ingress 정책이 묶음 전체의 판(`ANNOTATION_NAMESPACE_POLICIES`)을 싣는다."""
    tenant_policy = network_policy_manifest(tenant, namespace, platform_namespace)
    policies = [
        render_isolation_policy(tenant, namespace),
        render_backend_policy(tenant, namespace),
        tenant_policy,
    ]
    if egress:
        policies.append(render_egress_policy(tenant, namespace))
    stamp = stable_hash(policies, REV_LENGTH)
    tenant_policy["metadata"]["annotations"] = {ANNOTATION_NAMESPACE_POLICIES: stamp}
    return policies


def namespace_policies_stamp(policies: list[dict[str, Any]]) -> str:
    """`namespace_policies` 가 테넌트 ingress 정책에 실은 묶음의 판."""
    tenant_policy = next(p for p in policies if p["metadata"]["name"] == NETWORK_POLICY_NAME)
    return tenant_policy["metadata"]["annotations"][ANNOTATION_NAMESPACE_POLICIES]


def foreign_egress_policies(policies: list[dict[str, Any]]) -> list[str]:
    """한 네임스페이스의 NetworkPolicy 가운데 플랫폼이 만들지 않았고 나가는 길을 좁히는 것의 이름.
    egress 규칙이 있거나 policyTypes 에 Egress 가 있으면 좁힌다. 플랫폼이 만든 것은
    `app.kubernetes.io/managed-by: daegam` 라벨을 단다."""
    names = []
    for policy in policies:
        meta = policy.get("metadata") or {}
        if (meta.get("labels") or {}).get(LABEL_MANAGED_BY) == MANAGED_BY:
            continue
        spec = policy.get("spec") or {}
        if "Egress" in (spec.get("policyTypes") or []) or "egress" in spec:
            names.append(str(meta.get("name") or ""))
    return sorted(names)


def namespace_egress(policies: list[dict[str, Any]], *, wanted: bool) -> bool:
    """그 네임스페이스에 egress 정책을 두는가. 바라고(wanted, `egress_policy_wanted`) 그곳에
    관리자가 둔 egress 정책이 없을 때다. NetworkPolicy 는 허용을 더하기만 하므로, 메타데이터 주소만
    빼고 다 여는 정책이 관리자의 egress 제한을 지운다."""
    return wanted and not foreign_egress_policies(policies)


def ensure_namespace_policies(
    client: KubeClient,
    tenant: str,
    namespace: str,
    platform_namespace: str,
    *,
    egress: bool = False,
    live: list[dict[str, Any]] | None = None,
) -> bool:
    """NetworkPolicy 묶음(`namespace_policies`)을 그 순서대로 SSA 하고, egress 정책을 두었는지
    돌려준다. egress 는 egress 정책을 바라는지다. 바라면 그 네임스페이스의 정책(live, 없으면
    읽는다)에 관리자의 egress 정책이 있는지 보고, 있으면 두지 않고 로그를 남긴다
    (`namespace_egress`). 두지 않으면 남은 egress 정책을 지운다. 스위치를 끄거나 컨트롤러가 바뀐
    클러스터에 앞서 둔 것이 남았을 수 있다. 테넌트 네임스페이스를 보장하는 길과 인증 재적용의
    스윕이 함께 쓴다."""
    if egress:
        if live is None:
            live = client.list(NETWORK_POLICY_API, "NetworkPolicy", namespace)
        foreign = foreign_egress_policies(live)
        if foreign:
            logger.warning(
                "%s left out of %s: NetworkPolicies %s narrow egress there",
                EGRESS_POLICY_NAME,
                namespace,
                ", ".join(foreign),
            )
        egress = not foreign
    for policy in namespace_policies(tenant, namespace, platform_namespace, egress=egress):
        client.apply(policy)
    if not egress:
        client.delete(NETWORK_POLICY_API, "NetworkPolicy", EGRESS_POLICY_NAME, namespace)
    return egress


def live_namespace_policies(client: KubeClient) -> dict[str, list[dict[str, Any]]]:
    """클러스터의 NetworkPolicy 를 네임스페이스마다 묶는다. 모든 네임스페이스에 list 한 번이다.
    스윕이 묶음의 판(`policy_stamp_of`)과 관리자의 egress 정책(`foreign_egress_policies`)을 함께
    본다."""
    out: dict[str, list[dict[str, Any]]] = {}
    for item in client.list(NETWORK_POLICY_API, "NetworkPolicy"):
        namespace = str((item.get("metadata") or {}).get("namespace") or "")
        out.setdefault(namespace, []).append(item)
    return out


def policy_stamp_of(policies: list[dict[str, Any]]) -> str | None:
    """한 네임스페이스의 NetworkPolicy 가운데 테넌트 ingress 정책이 실은 묶음의 판. 그 정책이 없거나
    판이 없으면(이 판 전의 렌더) None 이다."""
    for policy in policies:
        meta = policy.get("metadata") or {}
        labels = meta.get("labels") or {}
        if meta.get("name") == NETWORK_POLICY_NAME and labels.get(LABEL_RESOURCE) == (
            RESOURCE_NETWORK_POLICY
        ):
            return (meta.get("annotations") or {}).get(ANNOTATION_NAMESPACE_POLICIES)
    return None


# 이름 규칙은 daegam/names.py 가 정본이다. 이 모듈을 쓰던 호출자가 있어 재수출한다
__all__ = [
    "LABEL_ID",
    "LABEL_RESOURCE",
    "LABEL_TENANT",
    "NAMESPACE_RE",
    "NETWORK_POLICY_NAME",
    "INTERNAL_OIDC_CONFIG_NAME",
    "OIDC_CONFIG_NAME",
    "RESOURCE_INTERNAL_OIDC_CONFIG",
    "RESOURCE_OIDC_CONFIG",
    "REMOTE_PROXY_SERVICE_ACCOUNT",
    "ROLE_BINDING_NAME",
    "TENANT_OBJECTS_BINDING_NAME",
    "TOOLHIVE_API",
    "TenancyError",
    "TenancyService",
    "check_namespace_claim",
    "ensure_namespace_objects",
    "ensure_namespace_policies",
    "ensure_oidc_configs",
    "group_name_for",
    "k8s_name",
    "foreign_egress_policies",
    "live_namespace_policies",
    "mcp_group_manifest",
    "namespace_policies",
    "namespace_egress",
    "namespace_policies_stamp",
    "network_policy_manifest",
    "oidc_config_manifest",
    "oidc_config_manifests",
    "oidc_kwargs",
    "policy_stamp_of",
    "remote_proxy_service_account_manifest",
    "tenant_objects_binding_manifest",
    "tenant_objects_for",
    "tenant_objects_stamp",
    "tenant_objects_stamp_of",
    "tls_reader_binding_manifest",
]
