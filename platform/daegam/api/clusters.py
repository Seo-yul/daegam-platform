"""Cluster API. 플랫폼 수준 리소스라 테넌트 헤더를 요구하지 않는다.
scope 는 cluster:read / cluster:write (쓰기는 platform-admin 만)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.api._common import error, not_null, or_404, patched_fields, references_out
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.clusters import (
    ClusterDeleteResponse,
    ClusterOut,
    ClusterPreflightResponse,
    ClusterSandboxStatus,
    ExposureOptions,
    SandboxCheckStarted,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.auth.principal import Principal
from daegam.error_codes import status_of
from daegam.errors import ServiceError, revision_conflict
from daegam.k8s.gates import is_release_version
from daegam.k8s.kubeconfig import KubeconfigRejected, load_kubeconfig
from daegam.k8s.preflight import cluster_warnings
from daegam.k8s.sandbox_capacity import Capacity
from daegam.k8s.templates.sandbox import verdict_void_reason
from daegam.k8s.templates.tenant_policies import metadata_block_warnings
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.pem import is_ca_pem
from daegam.services.clusters import ClusterService, NoHomeCluster
from daegam.services.exposure import exposure_options
from daegam.services.hub_images import check_cluster_prefix
from daegam.services.sandbox_gate import (
    GateReason,
    SandboxGateClosed,
    gate_reason,
    override_active,
)
from daegam.store.base import save_with_edit_claim, utcnow
from daegam.store.clusters import (
    HOME_CLUSTER_NAME,
    Cluster,
    ClusterConnection,
    ClusterImages,
    ClusterIngress,
    ClusterNamespaces,
    ClusterSandbox,
    ClusterSandboxInput,
    ClusterScheduling,
    DefaultRedisInput,
    MongoClusterStore,
    SandboxScheduling,
    secret_name_for,
)
from daegam.urls import is_http_url

logger = logging.getLogger("daegam.clusters")

router = APIRouter(responses=ERROR_RESPONSES, prefix="/clusters", tags=["clusters"])


def get_cluster_store(request: Request) -> MongoClusterStore:
    return request.app.state.clusters


def get_cluster_service(request: Request) -> ClusterService:
    return request.app.state.cluster_service


Clusters = Annotated[MongoClusterStore, Depends(get_cluster_store)]
Service = Annotated[ClusterService, Depends(get_cluster_service)]


# -- 입력 모델 -----------------------------------------------------------------


class KubeconfigConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kubeconfig: str


class TokenConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str
    # 필수다. 없으면 검증을 끄는 길밖에 없고, 그러면 API 서버 자리에 선 누구에게나 토큰을 준다
    ca_bundle: str
    sa_token: str


ConnectionInput = KubeconfigConnection | TokenConnection


class ClusterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None  # 없으면 name (D24)
    connection: ConnectionInput
    platform_internal_url: str
    images: ClusterImages = ClusterImages()
    namespaces: ClusterNamespaces = ClusterNamespaces()
    ingress: ClusterIngress = ClusterIngress()
    scheduling: ClusterScheduling = ClusterScheduling()
    default_redis: DefaultRedisInput | None = None
    # 오퍼레이터 이미지를 digest 로만 핀한 클러스터의 ToolHive 판 (P50 2절). 게이트는 preflight 가
    # 판을 읽지 못했을 때 이것을 본다
    recorded_toolhive_version: str | None = None
    # 샌드박스 이미지와 배치 (P40 10절). 격리 판정(`check`)은 받지 않는다
    sandbox: ClusterSandboxInput | None = None
    # 테넌트 파드를 인스턴스 메타데이터 주소에서 막는가 (T75). 없으면 끈다. 화면은 켤 때만 보낸다.
    # 이 칸을 모르는 옛 레플리카가 롤아웃 중에 받아도 거절하지 않게 하려는 것이다
    block_metadata: bool | None = None


class ClusterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None  # 저장값과 같아야 한다 (422 name_immutable, D24)
    display_name: str | None = None
    connection: ConnectionInput | None = None
    platform_internal_url: str | None = None
    images: ClusterImages | None = None
    namespaces: ClusterNamespaces | None = None
    ingress: ClusterIngress | None = None
    scheduling: ClusterScheduling | None = None
    default_redis: DefaultRedisInput | None = None
    recorded_toolhive_version: str | None = None  # null 이면 지운다
    # 보낸 칸(image·scheduling)만 점 경로로 쓴다. 화면은 이 칸을 자기 카드에서만 보낸다. 다른 카드의
    # 저장이 롤아웃 중의 옛 레플리카에 닿아도 거절되지 않게 하려는 것이다 (P40 10절)
    sandbox: ClusterSandboxInput | None = None
    # 켜면 다음 재적용 틱이 테넌트 네임스페이스마다 egress 정책을 두고, 끄면 지운다 (T75)
    block_metadata: bool | None = None


def _validate_default_redis(
    redis: DefaultRedisInput | None, *, closed: bool = False
) -> DefaultRedisInput | None:
    """기본 Redis 의 TLS CA 는 PEM 인증서만 든 값이어야 한다 (P61 2절). 공개 인증서라 문서에 그대로
    두고 테넌트 네임스페이스마다 옮기므로 개인 키가 섞이면 거절한다.

    평문 포트가 닫힌 표시(`plain_closed`, 5절 5단계)는 플랫폼이 정한다. 본문의 값은 받지 않고
    closed(`ClusterService.plain_closed_at`)를 적는다. 닫힌 주소에서 TLS 끝점이 없으면 422 다."""
    if redis is not None:
        redis.plain_closed = closed
        if closed and redis.tls is None:
            raise error(
                422,
                "invalid_value",
                "default_redis serves only its TLS endpoint, so default_redis.tls is required",
            )
    if redis is not None and redis.tls is not None:
        ca = is_ca_pem(redis.tls.ca)
        if ca is None:
            raise error(
                422,
                "invalid_value",
                "default_redis.tls.ca must hold PEM certificates only, without a private key",
            )
        redis.tls.ca = ca
    return redis


async def _plain_closed(service: Service, cluster_id: str, redis: DefaultRedisInput | None) -> bool:
    """보낸 기본 Redis 가 평문 포트를 닫은 함께 올린 Redis 를 가리키는가 (P61 5절 5단계)."""
    return redis is not None and await service.plain_closed_at(cluster_id, *redis.host_port())


def _sandbox_values(body: ClusterSandboxInput) -> dict[str, Any]:
    """보낸 `sandbox` 의 칸과 저장할 값. image 는 앞뒤 공백을 지우고 비면 null(플랫폼
    기본값)이다. scheduling 의 null 은 빈 배치(클러스터의 일반 배치를 따른다)다. 하위 객체는 null 로
    두지 않는다."""
    values: dict[str, Any] = {}
    if "image" in body.model_fields_set:
        values["image"] = (body.image or "").strip() or None
    if "scheduling" in body.model_fields_set:
        sched = body.scheduling
        values["scheduling"] = SandboxScheduling.model_validate(
            sched.model_dump() if sched is not None else {}
        )
    return values


def _validate_url(url: str, field: str) -> str:
    url = url.strip()
    if not is_http_url(url):
        raise error(422, "invalid_value", f"{field} must be an http(s) URL")
    return url


def _validate_toolhive_version(value: str | None) -> str | None:
    """`0.53.1`·`0.53.1-rc1` 꼴만 받는다(`is_release_version`). 앞의 v 는 떼고 저장한다. 빈 값은
    지우기다."""
    value = (value or "").strip()
    if not value:
        return None
    if not is_release_version(value):
        raise error(
            422,
            "invalid_value",
            "recorded_toolhive_version must be a ToolHive release such as 0.53.1",
        )
    return value.removeprefix("v")


def _out(c: Cluster, service: ClusterService, *, full: bool = True) -> dict[str, Any]:
    """full 이 아니면 접속 정보(API 서버 주소·kubecontext·Secret 이름)와 Strict dry run 이 거부한
    CR 의 목록, RoleBinding daegam-tenant-objects 가 없는 네임스페이스의 이름(P63)을 비운다.

    cluster:read 는 viewer 도 가진 scope 라 인스턴스를 어느 클러스터에 둘지 고르는 데는 이름과
    상태만 있으면 되고, 제어면의 주소는 클러스터를 고치는 사람만 본다. 두 목록은 여러 테넌트의
    네임스페이스와 이름을 담으므로 수만 남긴다."""
    d = c.model_dump(mode="json")
    d["home"] = c.is_home
    d["env_managed"] = service.env_managed(c)
    d["warnings"] = [*cluster_warnings(c.status.preflight), *metadata_block_warnings(c)]
    # 판정이 무효로 읽히는 까닭은 읽을 때마다 정한다 (P40 8절)
    d["sandbox"]["void_reason"] = verdict_void_reason(c)
    d["sandbox"]["override"] = override_active(service.settings, c)
    if not full:
        conn = d.get("connection") or {}
        for k in ("api_url", "kubecontext", "secret_name"):
            if k in conn:
                conn[k] = None
        strict = (d.get("gates") or {}).get("strict_apply")
        if strict:
            strict["failures"] = []
        secret_rights = ((d.get("status") or {}).get("preflight") or {}).get("secret_rights")
        if secret_rights:
            secret_rights["unbound_namespaces"] = []
    return d


def _full(principal: Principal) -> bool:
    return "cluster:write" in principal.all_scopes


async def _check_hub_prefix(
    request: Request, registry_prefix: str | None, cluster: str, *, before: str | None
) -> None:
    """제품 이미지 접두사를 바꿔 허브 이미지 접두사와 겹치면 422 hub_prefix_overlap 이다 (P55
    6절). 바꾸지 않은 접두사의 겹침은 pull Secret 같은 다른 저장을 막지 않고 설정 카드의 경고가
    보인다."""
    if registry_prefix == before:
        return
    hub = (await request.app.state.platform_settings.get()).hub_images
    check_cluster_prefix(registry_prefix, hub, cluster=cluster, error_cls=ServiceError)


async def _get_or_404(store: MongoClusterStore, cluster_id: str) -> Cluster:
    return or_404(await store.get(cluster_id), "cluster", cluster_id)


def _check_kubeconfig(text: str) -> None:
    """클라이언트와 같은 `load_kubeconfig` 로 열어 본다. 열지 못하면 422 `invalid_yaml` 에 줄과
    칸만 싣고, 명령·파일 참조가 있으면 422 `invalid_value` 에 그 경로를 싣는다."""
    try:
        load_kubeconfig(text)
    except KubeconfigRejected as e:
        extra = {"paths": e.paths} if e.paths else {}
        raise error(status_of(e.code, 422), e.code, e.detail, **extra) from None


def _credentials(conn: ConnectionInput) -> tuple[str, dict[str, str], str | None]:
    """(mode, secret data, api_url)"""
    if isinstance(conn, KubeconfigConnection):
        if not conn.kubeconfig.strip():
            raise error(422, "invalid_value", "kubeconfig is empty")
        _check_kubeconfig(conn.kubeconfig)
        return "kubeconfig", {"kubeconfig": conn.kubeconfig}, None
    if not conn.ca_bundle.strip():
        raise error(422, "invalid_value", "connection.ca_bundle is empty")
    data = {"token": conn.sa_token, "ca_bundle": conn.ca_bundle}
    return "token", data, _validate_url(conn.api_url, "connection.api_url")


# -- 핸들러 ------------------------------------------------------------------


@router.post(
    "",
    response_model=ClusterOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("cluster:write"))],
)
async def create_cluster(
    store: Clusters, service: Service, body: ClusterCreate, user: User, request: Request
) -> dict[str, Any]:
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    if name == HOME_CLUSTER_NAME:
        raise error(422, "reserved_name", "'home' is the auto-registered cluster")
    await _check_hub_prefix(request, body.images.registry_prefix, name, before=None)
    if await store.get_by_name(name) is not None:
        raise error(409, "name_conflict", f"cluster {name!r} already exists")
    mode, data, api_url = _credentials(body.connection)
    cluster = Cluster(
        updated_by=user.sub,
        name=name,
        display_name=display_name,
        connection=ClusterConnection(mode=mode, api_url=api_url),
        platform_internal_url=_validate_url(body.platform_internal_url, "platform_internal_url"),
        images=body.images,
        namespaces=body.namespaces,
        ingress=body.ingress,
        scheduling=body.scheduling,
        default_redis=_validate_default_redis(body.default_redis),
        recorded_toolhive_version=_validate_toolhive_version(body.recorded_toolhive_version),
        sandbox=ClusterSandbox(**_sandbox_values(body.sandbox or ClusterSandboxInput())),
        block_metadata=bool(body.block_metadata),
    )
    cluster.connection.secret_name = await _store_credentials(service, cluster.id, data)
    try:
        await store.create(cluster)
    except DuplicateKeyError as e:
        await run_in_threadpool(service.delete_credentials, cluster)
        raise error(409, "name_conflict", f"cluster {name!r} already exists") from e
    cluster.status.preflight = await service.preflight(cluster)
    if service.settings.cluster_local_platform_issuer:
        # 그 클러스터의 내부 MCPServer 는 플랫폼 발급자에서 JWKS 를 받아야 한다 (D41)
        logger.warning(
            "cluster %s registered while the platform issuer %s is only reachable inside the "
            "home cluster; set DAEGAM_PLATFORM_ISSUER to an address every cluster can reach",
            name,
            service.settings.platform_issuer_url,
        )
    return _out(cluster, service)


@router.get(
    "", response_model=list[ClusterOut], dependencies=[Depends(require_scope("cluster:read"))]
)
async def list_clusters(store: Clusters, service: Service, principal: User) -> list[dict[str, Any]]:
    full = _full(principal)
    return [_out(c, service, full=full) for c in await store.list()]


@router.get(
    "/{cluster_id}",
    response_model=ClusterOut,
    dependencies=[Depends(require_scope("cluster:read"))],
)
async def get_cluster(
    store: Clusters, service: Service, cluster_id: str, principal: User
) -> dict[str, Any]:
    return _out(await _get_or_404(store, cluster_id), service, full=_full(principal))


@router.get(
    "/{cluster_id}/references",
    response_model=ReferencesResponse,
    dependencies=[Depends(require_scope("cluster:read"))],
)
async def cluster_references(
    store: Clusters, cluster_id: str, tenant: Tenant, request: Request
) -> dict[str, Any]:
    """이 클러스터에 배포된 것 (D43). 클러스터는 플랫폼 리소스지만 이 응답만은 요청 테넌트의
    것만 담는다. 삭제 가드(`cluster_service.instance_checker`)는 그대로 플랫폼 전체를 본다."""
    cluster = await _get_or_404(store, cluster_id)
    return await references_out(request, "cluster", cluster.id, tenant=tenant)


@router.patch(
    "/{cluster_id}",
    response_model=ClusterOut,
    dependencies=[Depends(require_scope("cluster:write"))],
)
async def patch_cluster(
    store: Clusters,
    service: Service,
    cluster_id: str,
    patch: ClusterPatch,
    user: User,
    request: Request,
) -> dict[str, Any]:
    cluster = await _get_or_404(store, cluster_id)
    sent = patched_fields(patch)
    _check_env_managed(cluster, patch, sent, service.env_managed(cluster))

    if "name" in sent:
        # name 은 생성 뒤 바꿀 수 없다 (D24). 같은 값이면 통과, 다르면 422 name_immutable
        check_name_unchanged(not_null(patch.name, "name", code="invalid_name"), cluster.name)
    names = {"updated_at", "updated_by"}
    if "display_name" in sent:
        cluster.display_name = validate_display_name(
            not_null(patch.display_name, "display_name", code="invalid_display_name"),
            default=cluster.name,
        )
        names.add("display_name")
    if "platform_internal_url" in sent:
        cluster.platform_internal_url = _validate_url(
            not_null(patch.platform_internal_url, "platform_internal_url"),
            "platform_internal_url",
        )
        names.add("platform_internal_url")
    if "images" in sent:
        cluster_images = not_null(patch.images, "images")
        await _check_hub_prefix(
            request,
            cluster_images.registry_prefix,
            cluster.name,
            before=cluster.images.registry_prefix,
        )
        cluster.images = cluster_images
        names.add("images")
    for field in ("namespaces", "ingress", "scheduling"):
        if field in sent:
            setattr(cluster, field, not_null(getattr(patch, field), field))
            names.add(field)
    if "default_redis" in sent:
        closed = await _plain_closed(service, cluster.id, patch.default_redis)
        cluster.default_redis = _validate_default_redis(patch.default_redis, closed=closed)
        names.add("default_redis")
    if "recorded_toolhive_version" in sent:
        cluster.recorded_toolhive_version = _validate_toolhive_version(
            patch.recorded_toolhive_version
        )
        names.add("recorded_toolhive_version")
    if "sandbox" in sent:
        for field, value in _sandbox_values(not_null(patch.sandbox, "sandbox")).items():
            setattr(cluster.sandbox, field, value)
            names.add(f"sandbox.{field}")
    if "block_metadata" in sent:
        cluster.block_metadata = not_null(patch.block_metadata, "block_metadata")
        names.add("block_metadata")

    cluster.updated_at = utcnow()
    cluster.updated_by = user.sub
    # 보낸 칸만, 서버가 읽은 seq 를 조건으로 쓴다. status·gates·sandbox.check 는 점검이 쓴다
    # (P68 2절)
    connection_changed = "connection" in sent
    if connection_changed:
        connection = not_null(patch.connection, "connection")
        await _save_connection(store, service, cluster, connection, names)
    elif not await store.save_edit(cluster, names):
        raise revision_conflict(error, "cluster", cluster.name)
    # 접속 정보가 아니어도 캐시를 비운다 (PATCH 가 무효화 지점이다)
    service.invalidate(cluster.id)
    if connection_changed:
        # 마지막 저장이 cluster 의 seq 를 올렸으므로 이 점검의 결과는 저장된다
        cluster.status.preflight = await service.preflight(cluster)
    return _out(cluster, service)


async def _save_connection(
    store: Clusters,
    service: ClusterService,
    cluster: Cluster,
    connection: ConnectionInput,
    names: set[str],
) -> None:
    """접속 정보를 바꾸는 저장이다. names 는 함께 저장할 칸이다. 접속 Secret 을 먼저 쓰므로 편집
    클레임을 잡는다. 두 요청이 하나의 Secret 에 서로 다른 접속 정보를 쓰지 않게 한다 (P68 2절)."""
    if cluster.is_home:
        raise error(422, "reserved_name", "home cluster connection is managed by the platform")
    mode, data, api_url = _credentials(connection)

    async def prepare() -> None:
        # Secret 을 쓰기 전에 실패하면 편집 클레임을 푼다
        if service.secret_store() is None:
            raise error(503, "no_home_cluster", "no home cluster to store credentials in")

    async def write(_: None) -> None:
        secret_name = await _store_credentials(service, cluster.id, data)
        cluster.connection = ClusterConnection(mode=mode, api_url=api_url, secret_name=secret_name)

    await save_with_edit_claim(
        store,
        cluster,
        names | {"connection"},
        prepare=prepare,
        write=write,
        conflict=lambda: revision_conflict(error, "cluster", cluster.name),
    )


async def _store_credentials(service: ClusterService, cluster_id: str, data: dict[str, str]) -> str:
    """접속 비밀을 home 의 Secret 에 쓰고 그 이름을 돌려준다. home 이 없으면 503 이다. Secret 쓰기는
    동기 K8s 호출이라 스레드풀에서 한다 (P69)."""
    try:
        return await run_in_threadpool(service.store_credentials, cluster_id, data)
    except NoHomeCluster as e:
        raise error(503, "no_home_cluster", "no home cluster to store credentials in") from e


def _check_env_managed(
    cluster: Cluster, patch: ClusterPatch, sent: set[str], managed: list[str]
) -> None:
    """환경(DAEGAM_HOME_*)이 정한 필드를 다른 값으로 바꾸려 하면 409 다(D54). 다음 기동에 조용히
    되돌아가기 때문이다. 화면은 카드 전체를 보내므로 지금과 같은 값이 실려 오는 것은 받는다."""
    changed = []
    for path in managed:
        top, _, sub = path.partition(".")
        if top not in sent:
            continue
        new_top = getattr(patch, top)
        if new_top is None:
            continue
        if top == "sandbox":
            # sandbox 는 보낸 칸만 쓰므로 보내지 않은 칸은 바꾸지 않는다
            values = _sandbox_values(new_top)
            if sub not in values:
                continue
            new = values[sub]
        else:
            new = getattr(new_top, sub) if sub else new_top
        old = getattr(getattr(cluster, top), sub) if sub else getattr(cluster, top)
        if new != old:
            changed.append(path)
    if changed:
        raise error(
            409,
            "env_managed",
            "these home cluster fields are set by the platform environment (DAEGAM_HOME_*) and "
            "are rewritten on every start; change them in the deployment's ConfigMap",
            fields=changed,
        )


@router.delete(
    "/{cluster_id}",
    response_model=ClusterDeleteResponse,
    dependencies=[Depends(require_scope("cluster:write"))],
)
async def delete_cluster(store: Clusters, service: Service, cluster_id: str) -> dict[str, Any]:
    cluster = await _get_or_404(store, cluster_id)
    if cluster.is_home:
        raise error(409, "home_cluster", "the home cluster cannot be deleted")
    if await service.has_instances(cluster.id):
        raise error(409, "instances_exist", "cluster still has MCP instances")
    await run_in_threadpool(service.delete_credentials, cluster)
    service.invalidate(cluster.id)
    await store.delete(cluster.id)
    return {"id": cluster.id, "deleted": True, "secret_name": secret_name_for(cluster.id)}


@router.post(
    "/{cluster_id}/preflight",
    response_model=ClusterPreflightResponse,
    dependencies=[Depends(require_scope("cluster:write"))],
)
async def preflight_cluster(store: Clusters, service: Service, cluster_id: str) -> dict[str, Any]:
    cluster = await _get_or_404(store, cluster_id)
    result = await service.preflight(cluster)
    cluster.status.preflight = result
    return {
        "id": cluster.id,
        "name": cluster.name,
        "preflight": result.model_dump(mode="json"),
        "warnings": [*cluster_warnings(result), *metadata_block_warnings(cluster)],
    }


@router.get(
    "/{cluster_id}/exposure-options",
    response_model=ExposureOptions,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def exposure_options_for(
    request: Request,
    store: Clusters,
    service: Service,
    cluster_id: str,
    tenant: Tenant,
    namespace: str | None = None,
) -> dict[str, Any]:
    """생성 폼의 노출 섹션 (D22). 테넌트 허용 네임스페이스, IngressClass, 그 네임스페이스의 TLS
    Secret 이름(data 없음), ClusterIssuer, 기본값. 권한이 없으면 403 과 빠진 verb 를 돌려준다."""
    cluster = await _get_or_404(store, cluster_id)
    return await exposure_options(request.app.state.tenancy, service, cluster, tenant, namespace)


# -- 코드 실행 샌드박스 (P40 8절) ---------------------------------------------------------


class SandboxAcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 관리자가 본 판정의 checked_at. 그사이 새 점검이나 무효가 지나갔으면 허용하지 않는다
    checked_at: datetime


@router.get(
    "/{cluster_id}/sandbox",
    response_model=ClusterSandboxStatus,
    dependencies=[Depends(require_scope("cluster:read"))],
)
async def cluster_sandbox_status(
    request: Request, store: Clusters, cluster_id: str, principal: User
) -> dict[str, Any]:
    """코드 실행 게이트와 지금 센 용량 (P40 7절·8절). 클러스터의 노드·파드 목록을 읽으므로(60초
    캐시) 닿지 않는 클러스터는 502 cluster_unreachable 이다. 권한이 없거나 목록 읽기가 403 이면
    목록 없이 게이트가 `rights` 로 닫혔다고 답한다."""
    cluster = await _get_or_404(store, cluster_id)
    state = request.app.state
    sandboxes = state.sandboxes
    override = override_active(state.settings, cluster)
    capacity = Capacity(nodes=(), total=0)
    reason: GateReason | None = "rights"
    if cluster.gates.sandbox_rights:
        try:
            capacity = await sandboxes.capacity(cluster)
            reason = gate_reason(cluster, capacity=capacity.total, override=override)
        except SandboxGateClosed as e:
            reason = e.reason
    used = await sandboxes.store.node_counts(cluster.id, [n.name for n in capacity.nodes])
    return {
        "gate": {"open": reason is None, "reason": reason},
        "capacity": capacity.total,
        "nodes": [
            {
                "name": n.name,
                "allocatable": n.allocatable,
                "others": n.others,
                "cap": n.cap,
                "used": used.get(n.name, 0),
            }
            for n in capacity.nodes
        ],
        "assumed": len(capacity.assumed),
        "assumed_pods": list(capacity.assumed) if _full(principal) else [],
        "shared_nodes": cluster.sandbox.check.shared_nodes,
        "void_reason": verdict_void_reason(cluster),
        "override": override,
        "check_running": await state.sandbox_checks.running(cluster.id),
        "open_sandboxes": len(await sandboxes.store.live(cluster_id=cluster.id)),
        "run": cluster.sandbox.run.model_dump(mode="json"),
    }


@router.post(
    "/{cluster_id}/sandbox/check",
    response_model=SandboxCheckStarted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scope("cluster:write"))],
)
async def check_cluster_sandbox(
    request: Request, store: Clusters, cluster_id: str, user: User
) -> dict[str, Any]:
    """격리 점검을 뒤에서 시작한다 (P40 8절). 결과는 Cluster 의 `sandbox.check` 와 `sandbox.run`
    이다. 이미 돌면 409 sandbox_check_running 이다."""
    cluster = await _get_or_404(store, cluster_id)
    await request.app.state.sandbox_checks.start(cluster, user.sub)
    return {"started": True}


@router.post(
    "/{cluster_id}/sandbox/accept",
    response_model=ClusterOut,
    dependencies=[Depends(require_scope("cluster:write"))],
)
async def accept_cluster_sandbox(
    request: Request,
    store: Clusters,
    service: Service,
    cluster_id: str,
    body: SandboxAcceptBody,
    user: User,
) -> dict[str, Any]:
    """샌드박스가 다른 워크로드와 노드를 함께 쓰는 위험을 허용한다 (P40 8절). 판정이
    통과했고 무효로 읽히지 않으며 checked_at 이 지금의 것일 때만이다. 아니면 409
    sandbox_check_stale 이다. HTTP 감사가 누가 언제 허용했는지 남긴다."""
    cluster = await _get_or_404(store, cluster_id)
    await request.app.state.sandbox_checks.accept(cluster, user.sub, body.checked_at)
    return _out(await _get_or_404(store, cluster_id), service)
