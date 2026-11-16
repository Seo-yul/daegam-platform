"""설정 계층 합성: Platform Settings < Cluster < Tenant Settings < Profile runtime.

인스턴스 생성 시 한 번 호출해 결과를 `effective_config` 스냅샷으로 저장한다(불변).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from daegam.k8s.resources import effective_resources
from daegam.store.clusters import Cluster, ClusterIngress, ClusterScheduling, DefaultRedis
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import Binding, TenantSettings


class ProfileRuntime(BaseModel):
    """MCP Server / Agent 의 runtime 절 (4단계)."""

    model_config = ConfigDict(extra="forbid")

    image: str | None = None
    replicas: int = 1
    resources: dict[str, Any] | None = None
    bindings: list[Binding] = Field(default_factory=list)


class EffectiveNamespace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    quota: dict[str, Any] | None = None
    limit_range: dict[str, Any] | None = None


class EffectiveImages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_image: str
    agent_image: str
    pull_policy: str = "IfNotPresent"
    pull_secret: str | None = None


class EffectiveConfig(BaseModel):
    """배포 시점의 설정 스냅샷. 배포한 문서마다 저장되고 다시 렌더하기가 읽는다. 뒤의 판이 더할
    칸이 있어도 읽고 그대로 둔다(롤아웃 중의 옛 레플리카와 되돌린 판, P61 1절). API 본문으로는 받지
    않는다."""

    model_config = ConfigDict(extra="allow")

    tenant: str
    cluster_id: str
    cluster_name: str
    platform_internal_url: str
    namespace: EffectiveNamespace
    images: EffectiveImages
    scheduling: ClusterScheduling
    ingress: ClusterIngress
    default_redis: DefaultRedis | None = None
    replicas: int = 1
    resources: dict[str, Any] | None = None
    bindings: list[Binding] = Field(default_factory=list)
    model: str
    # 배포 때의 Platform Settings `network.ca_bundle`(PEM). MCP Server 의 mcp 컨테이너가 기본 루트에
    # 더해 믿고, 설정을 바꿔도 다음 배포까지는 이 값이다 (D88). MCP Server 만 적고
    # (`MCPServerService._effective`) 다른 종류는 비워 둔다. web_fetch 복제본은 이 스냅샷이 아니라
    # 지금 설정을 따른다(`render_fetch_clone` 의 ca_bundle)
    ca_bundle: str | None = None


def image_digest(image: str | None) -> str | None:
    """이미지 참조나 파드의 imageID 에서 `sha256:<hex>` 를 꺼낸다. 없으면 None 이다. 샌드박스의
    판정과 허브의 거부한 digest 가 함께 쓴다."""
    if not image:
        return None
    _, sep, digest = image.rpartition("sha256:")
    return f"sha256:{digest}" if sep and digest else None


def with_registry(image: str, prefix: str | None) -> str:
    """레지스트리가 없는 이미지 참조에만 prefix 를 붙인다 (`a/b:tag`, `ghcr.io/..` 는 그대로)."""
    if not prefix:
        return image
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"{prefix.rstrip('/')}/{image}"


def resolve_effective(
    settings: PlatformSettings,
    cluster: Cluster,
    tenant_settings: TenantSettings,
    profile_runtime: ProfileRuntime | None = None,
) -> EffectiveConfig:
    profile = profile_runtime or ProfileRuntime()
    tenant = tenant_settings.tenant

    # 이미지: Platform < Cluster < Profile
    runtime_image = profile.image or cluster.images.runtime_image or settings.default_runtime_image
    agent_image = cluster.images.agent_image or settings.default_agent_image
    images = EffectiveImages(
        runtime_image=with_registry(runtime_image, cluster.images.registry_prefix),
        agent_image=with_registry(agent_image, cluster.images.registry_prefix),
        pull_policy=cluster.images.pull_policy,
        pull_secret=cluster.images.pull_secret,
    )

    # 네임스페이스: 이름은 Tenant > Cluster template > Platform template
    template = cluster.namespaces.template or settings.namespace_template
    ns_name = (tenant_settings.namespace.name if tenant_settings.namespace else None) or (
        template.format(tenant=tenant)
    )
    labels = dict(cluster.namespaces.labels)
    if tenant_settings.namespace and tenant_settings.namespace.labels:
        labels.update(tenant_settings.namespace.labels)
    namespace = EffectiveNamespace(
        name=ns_name,
        labels=labels,
        quota=tenant_settings.quota
        if tenant_settings.quota is not None
        else cluster.namespaces.quota,
        limit_range=cluster.namespaces.limit_range,
    )

    # 리소스: Profile 이 Cluster.scheduling.resources 를 requests·limits 의 키 단위로 덮는다. 일부만
    # 준 값이 기본값의 나머지 한도를 잃지 않게 한다. 합친 값이 틀리면 422 invalid_resources 다(D54)
    resources = effective_resources(cluster.scheduling.resources, profile.resources)

    # 바인딩: Profile 이 하나라도 있으면 Profile, 없으면 Tenant 기본
    bindings = (
        list(profile.bindings) if profile.bindings else list(tenant_settings.default_bindings or [])
    )

    return EffectiveConfig(
        tenant=tenant,
        cluster_id=cluster.id,
        cluster_name=cluster.name,
        platform_internal_url=cluster.platform_internal_url,
        namespace=namespace,
        images=images,
        scheduling=cluster.scheduling,
        ingress=cluster.ingress,
        default_redis=_snapshot_redis(cluster.default_redis),
        replicas=profile.replicas,
        resources=resources,
        bindings=bindings,
        model=settings.default_model,
    )


def _snapshot_redis(redis: DefaultRedis | None) -> DefaultRedis | None:
    """새 배포 스냅샷에 넣는 클러스터 기본 Redis. 이 판이 아는 칸만 옮긴다(안쪽 모델까지). 클러스터
    기록에 다음 판이 적은 칸이 있어도, 그 칸을 렌더하지 못하는 이 판의 배포 스냅샷에는 들어가지
    않는다."""
    if redis is None:
        return None
    return DefaultRedis.model_validate(known_fields(redis))


def known_fields(model: BaseModel) -> dict[str, Any]:
    """모델이 선언한 칸만 남긴 값. 안쪽 모델도 같은 규칙으로 줄인다(`extra="allow"` 가 남긴 칸을
    뺀다)."""
    out: dict[str, Any] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        out[name] = known_fields(value) if isinstance(value, BaseModel) else value
    return out
