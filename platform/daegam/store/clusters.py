"""Cluster 리소스 (resource-model.md 9절). 접속 비밀은 K8s Secret, Mongo 에는 Secret 이름만 둔다
(docs/guide/clusters.md 4절)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    SEQ_FIELD,
    Document,
    EditClaimStore,
    UtcDatetime,
    naive_utc,
    new_id,
    set_fields_guarded,
    to_millis,
    utcnow,
)
from daegam.store.data_services import ShippedTls
from daegam.store.exposure import TLSMode

CLUSTERS = "clusters"
HOME_CLUSTER_NAME = "home"

ConnectionMode = Literal["in_cluster", "kubecontext", "kubeconfig", "token"]


class ClusterConnection(BaseModel):
    """저장되는 접속 정보. 비밀(kubeconfig, sa_token, ca_bundle)은 `secret_name` Secret 에 있다."""

    model_config = ConfigDict(extra="forbid")

    mode: ConnectionMode
    secret_name: str | None = None  # kubeconfig | token 모드. 플랫폼 네임스페이스의 Secret
    api_url: str | None = None  # token 모드
    kubecontext: str | None = None  # kubecontext 모드 (로컬 개발)


class ClusterImages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_prefix: str | None = None
    pull_secret: str | None = None
    pull_policy: Literal["IfNotPresent", "Always", "Never"] = "IfNotPresent"
    runtime_image: str | None = None
    agent_image: str | None = None


class ClusterNamespaces(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str | None = None  # 없으면 Platform Settings 의 namespace_template
    labels: dict[str, str] = Field(default_factory=dict)
    quota: dict[str, Any] | None = None  # ResourceQuota spec.hard
    limit_range: dict[str, Any] | None = None  # LimitRange spec


class ClusterIngress(BaseModel):
    """노출 기본값과 허용 범위 (resource-model.md 9절, docs/guide/exposure.md 2절, D22). 폼의
    기본값을 채우고 선택지를 제한하는 용도다. 인스턴스는 자기 `exposure` 스냅샷만 보므로 여기를
    바꿔도 실행 중 인스턴스는 안 바뀐다. 예전 문서는 새 필드가 기본값으로 읽힌다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # 이 클러스터에서 노출을 허용하는가
    # header 모드(anonymous)의 무인증 노출 허용. 기본 false, make deploy-local 의 home 만 true
    allow_unauthenticated: bool = False
    class_name: str | None = None  # 기본 IngressClass
    allowed_classes: list[str] = Field(default_factory=list)  # 비어 있으면 클러스터의 전부
    domain: str | None = None  # 기본 도메인 접미사. 폼의 host 기본값 <name>.<tenant>.<domain>
    # 기본 TLS 모드. edge 면 이 클러스터는 Ingress 앞에서 TLS 를 끝내고 노출은 edge 만 받는다(D54)
    tls: TLSMode = "none"
    default_issuer: str | None = None  # cert-manager ClusterIssuer 기본값
    # 모든 Ingress 에 붙는 공통 annotation (nginx 계열이면 proxy-buffering off, read-timeout 등)
    annotations: dict[str, str] | None = None
    controller_namespace: str | None = (
        None  # Ingress 컨트롤러 파드 네임스페이스 (orbstack: traefik)
    )
    controller_ip_blocks: list[str] = Field(default_factory=list)  # hostNetwork 컨트롤러의 CIDR
    # 플랫폼 자신의 Ingress host. 대감 차트가 값 host(로컬은 deploy/dev/values-local.yaml)로 Ingress
    # host 와 DAEGAM_HOME_INGRESS_RESERVED_HOSTS 를 함께 채운다. host_reserved 검사용
    reserved_hosts: list[str] = Field(default_factory=list)


class ClusterScheduling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: dict[str, Any] | None = None
    node_selector: dict[str, str] | None = None
    tolerations: list[dict[str, Any]] | None = None
    priority_class: str | None = None


# 클러스터 기본 Redis 의 TLS CA 를 테넌트 네임스페이스마다 두는 Secret (P61 2절). 키는 Database 의
# CA Secret 과 같은 `ca.crt`(`store/data_services.py` `REDIS_CA_KEY`)다
DEFAULT_REDIS_CA_SECRET = "daegam-redis-ca"


class DefaultRedisTls(BaseModel):
    """클러스터 기본 Redis 의 TLS 끝점 (P61 2절). 호스트는 `address` 의 것이고 포트만 다르다. CA 는
    공개 인증서라 문서에 PEM 그대로 두고, 네임스페이스 스윕이 테넌트 네임스페이스마다
    `daegam-redis-ca` 로 옮긴다. 뒤의 판이 더할 칸이 있어도 읽고 그대로 다시 쓴다."""

    model_config = ConfigDict(extra="allow")

    port: int = Field(ge=1, le=65535)
    ca: str


class DefaultRedis(BaseModel):
    """클러스터 기본 Redis (저장된 문서). 뒤의 판이 더할 칸이 있어도 읽고 그대로 다시 쓴다. 롤아웃
    중의 옛 레플리카와 되돌린 판이 같은 문서를 읽고 저장하기 때문이다(`update` 가 `default_redis` 를
    통째로 쓰므로 버리면 그 칸이 사라진다). API 본문은 모르는 칸을 거절하는 `DefaultRedisInput` 을
    받는다. `tls` 가 있으면 평문 주소와 함께 둘 다 열려 있는 것이다."""

    model_config = ConfigDict(extra="allow")

    address: str
    password_secret: str | None = None
    tls: DefaultRedisTls | None = None
    # 평문 포트가 닫혀 TLS 끝점만 답한다 (P61 5절 5단계). 함께 올린 Redis 에 대해 플랫폼이 기동 때
    # 쓴다(`services/shipped_redis.py`). API 본문의 값은 받지 않는다(`api/clusters.py`)
    plain_closed: bool = False

    def host_port(self) -> tuple[str, int | None]:
        """`address` 의 (호스트, 포트). 포트가 없거나 숫자가 아니면 None 이다."""
        host, _, port = self.address.rpartition(":")
        if not host:
            return self.address, None
        return host, int(port) if port.isdigit() else None

    @property
    def tls_only(self) -> bool:
        """평문 포트가 닫혀 TLS 끝점만 답하는가 (P61 5절 5단계)."""
        return self.tls is not None and self.plain_closed

    def tls_address(self) -> str | None:
        """TLS 끝점의 `host:port`. `tls` 가 없으면 None 이다."""
        if self.tls is None:
            return None
        return f"{self.host_port()[0]}:{self.tls.port}"


class DefaultRedisTlsInput(DefaultRedisTls):
    """API 본문의 TLS 끝점. 모르는 칸을 거절한다."""

    model_config = ConfigDict(extra="forbid")


class DefaultRedisInput(DefaultRedis):
    """API 본문의 클러스터 기본 Redis. 오타를 잡으려고 모르는 칸을 거절한다."""

    model_config = ConfigDict(extra="forbid")

    tls: DefaultRedisTlsInput | None = None


class RbacCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    resource: str
    verb: str
    name: str | None = None  # resourceNames 한정 검사 (clusterroles bind daegam-tls-reader)
    namespace: str | None = None
    allowed: bool


class SecretRightsCheck(BaseModel):
    """플랫폼이 테넌트 Secret 을 어디서 쓸 수 있는가 (P63). 옛 RBAC 파일에서 올라오는 순서는
    docs/guide/clusters.md 의 "Narrowing the platform's Secret rights" 에 있다."""

    model_config = ConfigDict(extra="ignore")

    # 플랫폼 계정이 클러스터 전체에서 Secret 을 읽고 만들고 고치고 지울 수 있는가. 참이면 경고한다.
    # 하나라도 거부면 거짓이고, 거부 없이 묻다가 오류가 났으면 None 이다
    cluster_wide: bool | None = None
    # ClusterRole daegam-tenant-objects 가 Secret 동사(preflight SECRET_VERBS)를 모두 담는가. 읽지
    # 못했으면 None 이다
    role: bool | None = None
    # 어느 길로든 테넌트 Secret 을 쓸 수 있는가. preflight 가 한 번 정하고, 테넌트 Secret 을 쓰는
    # 길의 422 는 이것이 거짓일 때만 낸다. 판정하지 못했으면 None 이다
    writable: bool | None = None
    # 본 관리 네임스페이스의 수. None 이면 네임스페이스 목록을 얻지 못해 바인딩을 보지 않았다
    namespaces: int | None = None
    # 그 가운데 RoleBinding daegam-tenant-objects 가 없거나, 다른 역할을 가리키거나, 이 플랫폼을
    # 묶지 않는 곳의 수와 이름. 이름은 여러 테넌트의 것이라 Cluster 응답은 cluster:write 에게만
    # 싣는다(api/clusters.py)
    unbound: int = 0
    unbound_namespaces: list[str] = Field(default_factory=list)


class Preflight(BaseModel):
    # 뒤의 판이 더한 칸을 앞의 판이 읽어도 깨지지 않게 모르는 칸은 버린다. 롤아웃 중의 옛 레플리카와
    # 되돌린 판이 같은 문서를 읽는다
    model_config = ConfigDict(extra="ignore")

    ok: bool
    checked_at: UtcDatetime = Field(default_factory=utcnow)
    k8s_version: str | None = None
    toolhive_version: str | None = None
    rbac: list[RbacCheck] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    # NetworkPolicy 집행 여부 (D16 전제). 정책 컨트롤러(Calico·Cilium·kube-router)가 보이면
    # enforced. k3s 는 kube-router 를 바이너리 안에서 돌려 워크로드로 보이지 않으므로
    # presumed(추정)다 (D54). unknown 이면 네임스페이스 격리가 보장되지 않는다 (경고로 표시, ok 에는
    # 영향 없음)
    network_policy: Literal["enforced", "presumed", "unknown"] = "unknown"
    policy_controller: str | None = None
    # 테넌트 Secret 권한의 바인딩 검사와 클러스터 전체 권한의 거꾸로 검사 (P63)
    secret_rights: SecretRightsCheck | None = None
    # home 이 함께 올린 Redis 의 TLS 서버 인증서가 끝나는 때 (P61 F3, DAEGAM_HOME_REDIS_TLS).
    # home 이 아니거나 그 설정이 없거나 읽지 못하면 None 이다
    shipped_redis_cert_expires_at: UtcDatetime | None = None


class StrictApplyCheck(BaseModel):
    """ToolHive CR 을 Strict 로 적용해도 되는가 (P50 2절). 살아 있는 MCP Server·Gateway 의 최종
    렌더(저장된 overrides 를 얹은 CR)를 fieldValidation=Strict dry run 으로 보내 본 결과다. 문서가
    모두 통과한 kind(`kinds`)만 Strict 로 적용한다(`ClusterClient.strict_kinds`)."""

    model_config = ConfigDict(extra="ignore")

    # 보낸 문서가 하나 이상이고 거부된 것이 없다. 화면이 보이는 요약이다
    ok: bool
    checked_at: UtcDatetime = Field(default_factory=utcnow)
    checked: int = 0  # dry run 으로 보낸 CR 수
    refused: int = 0  # 그 가운데 Strict 만 거부한 수
    # 보낸 문서가 모두 통과한 kind. Strict 적용은 이 kind 에만 든다
    kinds: list[str] = Field(default_factory=list)
    # 거부된 CR 의 `<kind> <namespace>/<name>: <이유>`. 앞의 STRICT_FAILURES_KEPT 개만 남긴다.
    # 여러 테넌트의 이름이 섞이므로 Cluster 응답은 cluster:write 에게만 싣는다(api/clusters.py)
    failures: list[str] = Field(default_factory=list)


# StrictApplyCheck.failures 에 남기는 수. 판정은 하나만 있어도 거짓이고 화면은 앞의 몇 개면 된다
STRICT_FAILURES_KEPT = 20


class ClusterGates(BaseModel):
    """게이트(k8s/gates.py)와 Strict 적용이 읽는 클러스터의 재료 (P50 2절). 앞의 판의 모델은
    `ClusterStatus` 의 모르는 칸을 받지 않으므로 Cluster 의 최상위 칸에 둔다. 저장은 preflight 와
    시간마다의 점검만 하고 문서 전체를 쓰는 `MongoClusterStore.update` 는 이 칸을 쓰지 않는다."""

    model_config = ConfigDict(extra="ignore")

    # 플랫폼이 테넌트 네임스페이스에 ClusterRole daegam-tenant-objects 를 묶을 수 있는가 (권한
    # 게이트). 그 ClusterRole 이 있고 그 이름에 get·bind 가 허용돼야 참이다. 거짓이어도 preflight
    # 의 ok 는 그대로다
    tenant_object_rights: bool = False
    # 코드 실행 샌드박스의 권한과 PriorityClass 가 다 있는가 (P40 10절, preflight `sandbox_rights`).
    # 거짓이면 샌드박스 게이트만 `rights` 로 닫힌다. preflight 의 ok 는 그대로다
    sandbox_rights: bool = False
    # ToolHive 오퍼레이터가 도구 호출 수를 Stacklok 으로 보내는가 (P50 G1). 오퍼레이터 컨테이너
    # env 의 TOOLHIVE_USAGE_METRICS_ENABLED 가 "false" 가 아니면 참이고, 값이 valueFrom 이라 알 수
    # 없으면 None 이다
    usage_metrics: bool | None = None
    strict_apply: StrictApplyCheck | None = None


class ClusterStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    preflight: Preflight | None = None
    # 메타데이터 차단을 바라는데 관리자의 egress 정책이 있어 egress 정책을 두지 않은 테넌트
    # 네임스페이스 (T75). 인증 재적용의 스윕이 적고 Cluster 경고 metadata_block_skipped 가 싣는다
    metadata_skipped: list[str] = Field(default_factory=list)


# -- 코드 실행 샌드박스 (P40 10절) ----------------------------------------------------------------
# Cluster 의 최상위 칸 `sandbox` 하나에 모은다. 앞의 판의 Cluster 는 모르는 최상위 칸을 버리고
# (`Document` 가 extra="ignore"), 안쪽 모델도 모르는 칸을 버린다. 그래서 롤아웃 중의 옛 레플리카와
# 되돌린 판이 같은 문서를 읽는다. `ClusterImages`·`ClusterScheduling` 은 그대로 둔다. 하위 객체는
# null 이 되지 않는다(default_factory). `sandbox.check.passed` 같은 점 경로 쓰기가 create 가 만든
# 문서에도, 이 칸이 생기기 전의 문서에도 듣게 하려는 것이다

# 판정이 무효가 된 까닭 (8절). 샌드박스의 첫 자체 점검이 87 로 끝났거나 플랫폼 프로브가 실패했다
SandboxVoidReason = Literal["self_check", "probe"]


class SandboxScheduling(BaseModel):
    """샌드박스 파드의 배치 (2절). 둘 다 비면 클러스터의 일반 배치(`scheduling`)를 따른다."""

    model_config = ConfigDict(extra="ignore")

    node_selector: dict[str, str] | None = None
    tolerations: list[dict[str, Any]] | None = None

    @property
    def is_set(self) -> bool:
        return self.node_selector is not None or self.tolerations is not None


class SandboxCheck(BaseModel):
    """클러스터의 격리 점검 판정 (8절). 점검이 통째로 바꾸고, 무효·허용·`shared_nodes` 만 칸 단위로
    쓴다. 사람의 저장은 이 칸을 쓰지 않는다(`ClusterSandboxInput` 이 받지 않는다). 판정이 무효로
    읽히는지는 읽을 때마다 정한다(배치·판·정책 컨트롤러의 불일치). 여기 적힌 무효는 87 이나 프로브
    실패가 쓴 것뿐이다."""

    model_config = ConfigDict(extra="ignore")

    passed: bool = False
    userns: bool | None = None
    network: bool | None = None
    # 양성 대조가 닿은 대상과 그 연결 시간
    targets: list[dict[str, Any]] = Field(default_factory=list)
    shared_nodes: bool | None = None
    capacity: int | None = None
    # 점검이 돈 샌드박스 배치의 지문 (`k8s/templates/sandbox.py` `placement_fingerprint`)
    placement: str | None = None
    # 점검이 돌린 이미지. image 는 그때의 실효 참조(`sandbox_image`)이고 image_id 는 점검의 샌드박스
    # 파드가 알린 이미지 id(containerStatuses 의 imageID)다. 판정은 그 이미지에 대한 것이라, 실효
    # 참조가 바뀌거나 열린 파드가 다른 이미지 id 로 뜨면 무효로 읽힌다(`verdict_void_reason`)
    image: str | None = None
    image_id: str | None = None
    # 열린 샌드박스 파드가 알린, image_id 와 다른 이미지 id. 열기나 스윕이 적고 새 점검이 지운다.
    # 이것이 있으면 판정이 무효로 읽혀 게이트가 파드를 만들기 전에 닫힌다
    seen_image_id: str | None = None
    k8s_version: str | None = None
    policy_controller: str | None = None
    # 밀리초까지만 둔다(`to_millis`). Mongo 가 그 아래를 버리므로 허용이 맞대는 값이 되돌아와도 같다
    checked_at: UtcDatetime | None = None
    accepted_by: str | None = None
    accepted_at: UtcDatetime | None = None
    voided_at: UtcDatetime | None = None
    void_reason: SandboxVoidReason | None = None


class SandboxCheckRun(BaseModel):
    """격리 점검의 마지막 실행 (8절). 점검은 뒤에서 돌고 화면이 이것을 읽는다. error 는 점검을
    하지 못한 까닭이고(권한, 당기기, 용량, 시한, 양성 대조의 실패), 그때 판정(`check`)은 앞의 것
    그대로다. 점검이 끝나면 판정을 통째로 바꾸고 error 는 None 이다."""

    model_config = ConfigDict(extra="ignore")

    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    by: str | None = None
    error: str | None = None


class ClusterSandbox(BaseModel):
    """`Cluster.sandbox`. image 는 샌드박스 이미지(digest)이고 비면 플랫폼 기본값이다
    (`k8s/templates/sandbox.py` `sandbox_image`)."""

    model_config = ConfigDict(extra="ignore")

    image: str | None = None
    scheduling: SandboxScheduling = Field(default_factory=SandboxScheduling)
    check: SandboxCheck = Field(default_factory=SandboxCheck)
    run: SandboxCheckRun = Field(default_factory=SandboxCheckRun)


class SandboxSchedulingInput(SandboxScheduling):
    """API 본문의 샌드박스 배치. 오타를 잡으려고 모르는 칸을 거절한다."""

    model_config = ConfigDict(extra="forbid")


class ClusterSandboxInput(BaseModel):
    """사람의 저장이 받는 `sandbox` (10절). 보낸 칸만 점 경로로 쓴다. `check` 는 받지 않는다."""

    model_config = ConfigDict(extra="forbid")

    image: str | None = None
    scheduling: SandboxSchedulingInput | None = None


# 사람의 저장이 쓰는 `sandbox` 의 칸. 점 경로로 따로 쓰므로 `sandbox.check` 는 덮지 않는다
SANDBOX_HUMAN_FIELDS = ("image", "scheduling")


class Cluster(Document):
    id: str = Field(default_factory=new_id)
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    connection: ClusterConnection
    platform_internal_url: str
    images: ClusterImages = Field(default_factory=ClusterImages)
    namespaces: ClusterNamespaces = Field(default_factory=ClusterNamespaces)
    ingress: ClusterIngress = Field(default_factory=ClusterIngress)
    scheduling: ClusterScheduling = Field(default_factory=ClusterScheduling)
    default_redis: DefaultRedis | None = None
    # 기본 Redis 가 함께 올린 Redis 의 TLS 끝점을 받은 기록 (P61, `services/shipped_redis.py`)
    shipped_redis_tls: ShippedTls | None = None
    # 관리자가 적어 둔 ToolHive 판 (P50 2절). 오퍼레이터 이미지를 digest 로만 핀하면 preflight 가
    # 판을 읽지 못하므로 관리자가 적는다. 게이트(k8s/gates.py)는 preflight 가 판을 읽지 못했을
    # 때만 이것을 본다
    recorded_toolhive_version: str | None = None
    gates: ClusterGates = Field(default_factory=ClusterGates)
    status: ClusterStatus = Field(default_factory=ClusterStatus)
    # 코드 실행 샌드박스의 이미지·배치·격리 판정 (P40 10절)
    sandbox: ClusterSandbox = Field(default_factory=ClusterSandbox)
    # 테넌트 파드가 인스턴스 메타데이터 주소에 닿지 못하게 하는가 (T75). 켜면 정책 컨트롤러가 확인된
    # 것일 때 테넌트 네임스페이스마다 egress 정책을 둔다(`k8s/templates/tenant_policies.py`
    # `egress_policy_wanted`). 최상위 칸이라 앞의 판은 모르고 지나간다
    block_metadata: bool = False
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None  # 마지막으로 만들거나 고친 주체의 sub (감사)

    @property
    def is_home(self) -> bool:
        return self.name == HOME_CLUSTER_NAME


def secret_name_for(cluster_id: str) -> str:
    return f"cluster-{cluster_id}"


def home_env_fields(
    platform_internal_url: str,
    *,
    ingress_overrides: dict[str, Any] | None = None,
    images_overrides: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    limit_range: dict[str, Any] | None = None,
    sandbox_image: str | None = None,
    block_metadata: bool | None = None,
) -> dict[str, Any]:
    """환경(DAEGAM_HOME_*)이 정하는 home 필드. 키는 Mongo 경로, 값은 저장할 값이다.

    `upsert_home` 이 기동마다 이 값으로 덮어쓰고 `ClusterService.env_managed` 가 이 경로를 화면과
    PATCH 에서 잠근다. 두 곳이 목록을 따로 만들면 새 환경 변수를 한쪽에만 더했을 때 화면에서 고친
    값이 기동마다 조용히 되돌아가므로 여기 하나만 둔다. 샌드박스 이미지는 `DAEGAM_HOME_IMAGES` 의
    키가 아니라 따로 오고(`DAEGAM_HOME_SANDBOX_IMAGE`) `sandbox.image` 로 간다. 앞의 판은 그 env 를
    모르고, `DAEGAM_HOME_IMAGES` 는 기동 때 `ClusterImages` 로 엄격히 검증하기 때문이다
    (P40 10절)."""
    fields: dict[str, Any] = {"platform_internal_url": platform_internal_url}
    if sandbox_image:
        fields["sandbox.image"] = sandbox_image
    if block_metadata is not None:
        fields["block_metadata"] = block_metadata
    if ingress_overrides:
        ingress = ClusterIngress.model_validate(ingress_overrides).model_dump()
        fields.update({f"ingress.{k}": ingress[k] for k in ingress_overrides})
    if images_overrides:
        images = ClusterImages.model_validate(images_overrides).model_dump()
        fields.update({f"images.{k}": images[k] for k in images_overrides})
    if resources is not None:
        fields["scheduling.resources"] = resources
    if limit_range is not None:
        fields["namespaces.limit_range"] = limit_range
    return fields


class MongoClusterStore(EditClaimStore):
    def __init__(self, db: AsyncDatabase):
        self.coll = db[CLUSTERS]

    async def init_schema(self) -> None:
        await self.coll.create_index([("name", ASCENDING)], unique=True, name="uq_name")

    async def create(self, cluster: Cluster) -> Cluster:
        await self.coll.insert_one(cluster.to_doc())
        return cluster

    async def get(self, cluster_id: str) -> Cluster | None:
        doc = await self.coll.find_one({"_id": cluster_id})
        return Cluster.from_doc(doc) if doc else None

    async def get_by_name(self, name: str) -> Cluster | None:
        doc = await self.coll.find_one({"name": name})
        return Cluster.from_doc(doc) if doc else None

    async def list(self) -> list[Cluster]:
        cursor = self.coll.find({}).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return [Cluster.from_doc(d) for d in await cursor.to_list()]

    async def _set_fact(
        self, cluster_id: str, sets: dict[str, Any], expect_seq: int | None
    ) -> bool:
        """점검이 알아낸 것을 쓴다. expect_seq 를 주면 그 `seq` 이고 클레임이 없을 때만이다.
        점검 결과는 그때의 접속 정보에 대한 것이므로 그사이 사람의 저장이 지나갔으면 남기지
        않는다 (P68 2절)."""
        fields = {**sets, "updated_at": utcnow()}
        if expect_seq is None:
            await self.coll.update_one({"_id": cluster_id}, {"$set": fields})
            return True
        return await set_fields_guarded(
            self.coll, cluster_id, fields, expect_seq=expect_seq, unclaimed=True
        )

    async def set_preflight(
        self, cluster_id: str, preflight: Preflight, *, expect_seq: int | None = None
    ) -> bool:
        return await self._set_fact(
            cluster_id, {"status.preflight": preflight.model_dump()}, expect_seq
        )

    async def set_metadata_skipped(self, cluster_id: str, namespaces: list[str]) -> None:
        """메타데이터 차단을 두지 않은 네임스페이스의 목록을 쓴다 (T75, `ClusterStatus`)."""
        await self._set_fact(cluster_id, {"status.metadata_skipped": namespaces}, None)

    async def set_gates(
        self, cluster_id: str, fields: dict[str, Any], *, expect_seq: int | None = None
    ) -> bool:
        """`gates` 의 칸 가운데 fields 에 든 것만 쓴다. 키는 `ClusterGates` 의 칸 이름이다."""
        if not fields:
            return True
        unknown = set(fields) - set(ClusterGates.model_fields)
        if unknown:
            raise ValueError(f"unknown gate fields: {sorted(unknown)}")
        sets: dict[str, Any] = {f"gates.{k}": v for k, v in fields.items()}
        return await self._set_fact(cluster_id, sets, expect_seq)

    async def set_strict_apply(
        self, cluster_id: str, check: StrictApplyCheck, *, expect_seq: int | None = None
    ) -> bool:
        return await self.set_gates(
            cluster_id, {"strict_apply": check.model_dump()}, expect_seq=expect_seq
        )

    # -- 샌드박스 판정의 칸 쓰기 (P40 8절·10절) --------------------------------------------------
    # 모두 seq 를 올리지 않는 칸 쓰기다. 사람의 저장(`save_edit`)은 `sandbox.image`·
    # `sandbox.scheduling` 만 점 경로로 쓰므로, 무효 전에 문서를 읽은 저장이 판정을 되살리지 않는다

    async def set_sandbox_check(self, cluster_id: str, check: SandboxCheck) -> None:
        """격리 점검의 판정으로 `sandbox.check` 를 통째로 바꾼다. 앞의 허용과 무효도 함께 지운다."""
        if check.checked_at is not None:
            check = check.model_copy(update={"checked_at": to_millis(check.checked_at)})
        await self._set_fact(cluster_id, {"sandbox.check": check.model_dump()}, None)

    async def void_sandbox_check(self, cluster_id: str, reason: SandboxVoidReason) -> bool:
        """통과한 판정을 무효로 적는다. passed 를 거짓으로 두고 그 때와 까닭을 남기며 허용을 지운다.
        이미 무효면 처음 까닭을 둔다. 쓴 것이 있으면 참이다. 87 로 끝난 샌드박스와 실패한 플랫폼
        프로브만 부른다(8절)."""
        res = await self.coll.update_one(
            {"_id": cluster_id, "sandbox.check.passed": True, "sandbox.check.voided_at": None},
            {
                "$set": {
                    "sandbox.check.passed": False,
                    "sandbox.check.voided_at": utcnow(),
                    "sandbox.check.void_reason": reason,
                    "sandbox.check.accepted_by": None,
                    "sandbox.check.accepted_at": None,
                    "updated_at": utcnow(),
                }
            },
        )
        return res.modified_count == 1

    async def accept_sandbox(self, cluster_id: str, by: str, checked_at: datetime) -> bool:
        """노드를 함께 쓰는 위험의 허용 (P40 8절). 판정이 통과했고 무효가 아니며 그
        `checked_at` 이 관리자가 본 값일 때만 쓴다. 맞으면 참이다."""
        now = utcnow()
        res = await self.coll.update_one(
            {
                "_id": cluster_id,
                "sandbox.check.passed": True,
                "sandbox.check.voided_at": None,
                "sandbox.check.checked_at": to_millis(naive_utc(checked_at)),
            },
            {
                "$set": {
                    "sandbox.check.accepted_by": by,
                    "sandbox.check.accepted_at": now,
                    "updated_at": now,
                }
            },
        )
        return res.matched_count == 1

    async def note_sandbox_image(
        self, cluster_id: str, checked_at: datetime, image_id: str
    ) -> bool:
        """열린 파드가 판정의 것과 다른 이미지 id 를 알렸다 (8절). 그 판정(checked_at)에만 적는다.
        passed 는 그대로이고 판정은 읽을 때 무효로 읽힌다. 적었으면 참이다."""
        res = await self.coll.update_one(
            {
                "_id": cluster_id,
                "sandbox.check.checked_at": to_millis(naive_utc(checked_at)),
                "sandbox.check.seen_image_id": None,
            },
            {"$set": {"sandbox.check.seen_image_id": image_id, "updated_at": utcnow()}},
        )
        return res.modified_count == 1

    async def set_sandbox_run(self, cluster_id: str, run: SandboxCheckRun) -> None:
        """격리 점검의 실행 기록을 통째로 쓴다. 판정은 건드리지 않는다."""
        await self._set_fact(cluster_id, {"sandbox.run": run.model_dump()}, None)

    async def set_sandbox_shared_nodes(self, cluster_id: str, shared: bool) -> None:
        """스윕이 살아 있는 파드로 다시 센 `shared_nodes` (8절). 허용은 건드리지 않는다. 참으로
        바뀌었는데 허용이 없으면 게이트가 `unaccepted` 로 닫힌다. 무효가 아니다."""
        await self._set_fact(cluster_id, {"sandbox.check.shared_nodes": shared}, None)

    async def delete(self, cluster_id: str) -> bool:
        res = await self.coll.delete_one({"_id": cluster_id})
        return res.deleted_count == 1

    async def upsert_home(
        self, connection: ClusterConnection, env_fields: dict[str, Any]
    ) -> Cluster:
        """home 클러스터를 이름 기준으로 upsert. 레플리카가 동시에 떠도 유일 인덱스가 하나로 모은다.
        env_fields(`home_env_fields` 의 경로 → 값)는 환경(DAEGAM_HOME_*)이 정한 필드라 기동마다
        덮어쓴다(D22, D54). 환경이 정하지 않은 필드는 화면에서 고친 값이 남는다.

        `$set` 하는 경로의 최상위 키는 `$setOnInsert` 에서 뺀다. 둘이 같은 경로를 건드리면
        MongoDB 가 경로 충돌로 거절한다. ingress 는 하위 키를 하나도 쓰지 않을 때도 뺀다. 빠진
        최상위 키는 읽을 때 모델 기본값이 채운다."""
        set_fields: dict[str, Any] = {
            **env_fields,
            "connection": connection.model_dump(),
            "updated_at": utcnow(),
        }
        candidate = Cluster(
            name=HOME_CLUSTER_NAME,
            connection=connection,
            platform_internal_url=env_fields["platform_internal_url"],
        )
        on_insert = candidate.to_doc()
        for top in {path.partition(".")[0] for path in set_fields} | {"ingress"}:
            on_insert.pop(top, None)
        update: dict[str, Any] = {"$set": set_fields, "$setOnInsert": on_insert}
        current = await self.coll.find_one({"name": HOME_CLUSTER_NAME})
        if current is not None and _changes_any(
            current, {**env_fields, "connection": connection.model_dump()}
        ):
            # 화면은 카드 전체를 보내므로, 기동 전에 읽은 PATCH 가 바뀐 환경 값을 옛 값으로
            # 되돌리지 않게 `seq` 를 올린다. 바뀐 값이 없으면 두어 PATCH 를 헛되이 막지 않는다
            # (P68 2절)
            update["$inc"] = {SEQ_FIELD: 1}
        doc = await self.coll.find_one_and_update(
            {"name": HOME_CLUSTER_NAME},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return Cluster.from_doc(doc)


def _changes_any(doc: dict[str, Any], paths: dict[str, Any]) -> bool:
    """paths(점 경로 → 값) 가운데 doc 의 값과 다른 것이 있는가."""
    for path, value in paths.items():
        current: Any = doc
        for part in path.split("."):
            current = current.get(part) if isinstance(current, dict) else None
        if current != value:
            return True
    return False
