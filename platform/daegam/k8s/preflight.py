"""Cluster preflight.

연결(/version), RBAC(SelfSubjectAccessReview), ToolHive CRD 디스커버리, 오퍼레이터 가용성을 본다.
게이트(k8s/gates.py)가 보는 테넌트 객체 권한과 오퍼레이터의 사용량 지표 env(P50 G1)도 적는다. 둘 다
ok 에 영향을 주지 않는다.

테넌트 Secret 권한은 두 길로 온다 (P63). 클러스터 전체 Secret 권한(옛 ClusterRole)과, 관리
네임스페이스마다 RoleBinding 으로 묶는 ClusterRole daegam-tenant-objects 다. preflight 는 바인딩과
역할을 직접 읽어 본다. 접근 검사는 옛 ClusterRole 을 거쳐 통과해 버리기 때문이다. 쓸 수 있는지는
여기서 한 번 정하고(`secret_writability`), 테넌트 Secret 을 쓰는 길의 422 는 그 판정만 읽는다.
클러스터 전체 권한이 남아 있으면 경고하고, 어느 길로도 쓸 수 없다고 정해졌을 때만 ok 가 거짓이다.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

from daegam.k8s.client import (
    RBAC_API,
    RBAC_GROUP,
    TOOLHIVE_API,
    TOOLHIVE_GROUP,
    ClusterUnreachable,
    KubeClient,
    cluster_down,
    image_tag,
    platform_subject,
)
from daegam.store.base import utcnow
from daegam.store.clusters import Preflight, RbacCheck, SecretRightsCheck

logger = logging.getLogger("daegam.k8s.preflight")

# 이 모듈에서 가져다 쓰는 곳이 있어 이름을 남긴다
__all__ = ["RBAC_API", "RBAC_GROUP", "TOOLHIVE_GV"]

TOOLHIVE_GV = TOOLHIVE_API
REQUIRED_TOOLHIVE_RESOURCES = ("mcpservers", "mcpgroups", "virtualmcpservers", "mcpremoteproxies")
OPERATOR_NAMESPACE = "toolhive-system"
OPERATOR_DEPLOYMENT = "toolhive-operator"

# (group, resource, verb) 또는 이름 한정 (group, resource, verb, name).
# 대감 차트의 규칙 목록(charts/daegam/templates/_rbac.tpl)이 주는 권한과 같아야 한다. 그 목록을
# home(target home)의 daegam-platform 과 원격 클러스터(target remote)의 daegam-deployer 가 함께 쓴다
TLS_READER_CLUSTER_ROLE = "daegam-tls-reader"
# 테넌트 네임스페이스의 MCPAuthzConfig·원격 프록시 ServiceAccount·LocalSubjectAccessReview
# 권한을 담은 바인딩 없는 ClusterRole (P50 2절). 플랫폼이 허용 네임스페이스마다 RoleBinding 으로
# 묶는다
TENANT_OBJECTS_CLUSTER_ROLE = "daegam-tenant-objects"
# 그 ClusterRole 을 테넌트 네임스페이스마다 묶는 RoleBinding 이름 (services/tenancy.py 가 만든다)
TENANT_OBJECTS_BINDING_NAME = "daegam-tenant-objects"
REQUIRED_VERBS: tuple[tuple[str, ...], ...] = (
    (TOOLHIVE_GROUP, "mcpservers", "create"),
    (TOOLHIVE_GROUP, "mcpservers", "patch"),
    (TOOLHIVE_GROUP, "mcpservers", "delete"),
    (TOOLHIVE_GROUP, "mcpservers", "get"),
    (TOOLHIVE_GROUP, "mcpservers", "list"),
    (TOOLHIVE_GROUP, "mcpgroups", "create"),
    (TOOLHIVE_GROUP, "mcpgroups", "patch"),
    (TOOLHIVE_GROUP, "mcpgroups", "get"),
    (TOOLHIVE_GROUP, "mcpoidcconfigs", "create"),
    (TOOLHIVE_GROUP, "mcpoidcconfigs", "patch"),
    (TOOLHIVE_GROUP, "mcpexternalauthconfigs", "create"),
    (TOOLHIVE_GROUP, "mcpexternalauthconfigs", "patch"),
    # Secret 은 여기서 묻지 않는다. 클러스터 전체 권한 없이(P63) 테넌트 네임스페이스마다
    # daegam-tenant-objects 로 받으므로 `check_secret_rights` 가 따로 본다
    ("", "namespaces", "create"),
    ("", "namespaces", "get"),
    ("", "namespaces", "patch"),
    ("", "resourcequotas", "patch"),
    ("", "limitranges", "patch"),
    ("", "pods", "list"),
    ("apps", "deployments", "get"),
    ("apps", "deployments", "list"),
    # F3a: 테넌트 NetworkPolicy + 정책 컨트롤러 탐지
    ("networking.k8s.io", "networkpolicies", "create"),
    ("networking.k8s.io", "networkpolicies", "patch"),
    ("networking.k8s.io", "networkpolicies", "get"),
    ("apps", "daemonsets", "get"),
    ("apps", "daemonsets", "list"),
    ("", "services", "create"),
    ("", "services", "get"),
    ("", "services", "patch"),
    ("", "services", "delete"),
    ("apps", "deployments", "create"),
    ("apps", "deployments", "patch"),
    ("apps", "deployments", "delete"),
    # D26: Agent 의 Deployment·Service·NetworkPolicy 를 daegam.devops.ai.kr/id 라벨로 정리한다
    ("", "services", "list"),
    ("networking.k8s.io", "networkpolicies", "list"),
    # D21: 원격 클러스터의 프록시·에이전트 Service 에 API 서버 services/proxy 로 닿는다
    ("", "services/proxy", "get"),
    ("", "services/proxy", "create"),
    # D41: 내부 MCPServer 와 에이전트 런타임이 Bearer 를 요구하고 services/proxy 는 그 헤더를
    # 지우므로, 직행이 아닌 경로의 tools/list 와 런타임 호출은 pods/portforward 로 간다
    ("", "pods/portforward", "create"),
    # P8 (D22): Ingress 노출, IngressClass·ClusterIssuer 조회, 노출 NetworkPolicy 정리,
    # 허용 네임스페이스의 TLS Secret 목록용 RoleBinding(daegam-tls-reader bind), 자기 identity
    ("networking.k8s.io", "ingresses", "get"),
    ("networking.k8s.io", "ingresses", "create"),
    ("networking.k8s.io", "ingresses", "patch"),
    ("networking.k8s.io", "ingresses", "delete"),
    ("networking.k8s.io", "ingressclasses", "list"),
    ("cert-manager.io", "clusterissuers", "list"),
    ("networking.k8s.io", "networkpolicies", "delete"),
    (RBAC_GROUP, "rolebindings", "get"),
    (RBAC_GROUP, "rolebindings", "create"),
    (RBAC_GROUP, "rolebindings", "patch"),
    (RBAC_GROUP, "rolebindings", "delete"),
    (RBAC_GROUP, "clusterroles", "bind", TLS_READER_CLUSTER_ROLE),
    ("authentication.k8s.io", "selfsubjectreviews", "create"),
)

# 게이트로만 보는 권한 (P50 2절). 같은 SelfSubjectAccessReview 로 묻고 rbac 에 적지만 빠져도 missing
# 에 넣지 않는다. 옛 RBAC 로 등록한 클러스터도 그대로 쓰이고, 이 권한이 필요한 기능만 대체 형태로
# 돈다. 두 rbac.yaml 은 이것도 준다. get 은 ClusterRole 이 있는지 읽으려고, bind 는 그것을 테넌트
# 네임스페이스에 묶으려고(RBAC 승격 검사) 필요하다
TENANT_OBJECT_VERBS: tuple[tuple[str, ...], ...] = (
    (RBAC_GROUP, "clusterroles", "get", TENANT_OBJECTS_CLUSTER_ROLE),
    (RBAC_GROUP, "clusterroles", "bind", TENANT_OBJECTS_CLUSTER_ROLE),
)

# 코드 실행 샌드박스의 권한 (P40 10절). 메모리 규칙과 `shared_nodes` 가 노드를 읽고, 게이트가
# PriorityClass 가 있는지 읽는다. 샌드박스 파드의 create·delete 는 클러스터 전체가 아니라 테넌트
# 네임스페이스마다 daegam-tenant-objects 로 받으므로 묻지 않고 그 역할의 규칙을 읽는다
# (`SANDBOX_POD_VERBS`). 테넌트 객체 권한처럼 게이트로만 보며, 빠지면 샌드박스 게이트만 `rights` 로
# 닫히고 preflight 의 ok 는 그대로다. 이 RBAC 파일을 다시 적용하지 않은 원격 클러스터도 preflight 를
# 통과한다. pods/exec·pods/attach·pods/ephemeralcontainers 는 어디에도 주지 않는다
SANDBOX_PRIORITY_CLASS = "daegam-sandbox"
SANDBOX_VERBS: tuple[tuple[str, ...], ...] = (
    ("", "nodes", "get"),
    ("", "nodes", "list"),
    ("scheduling.k8s.io", "priorityclasses", "get", SANDBOX_PRIORITY_CLASS),
)
SANDBOX_POD_VERBS: tuple[str, ...] = ("create", "delete")

# ToolHive 오퍼레이터가 도구 호출 수를 Stacklok 으로 보내는지 정하는 env (P50 G1). 값이 "false" 일
# 때만 꺼진다
USAGE_METRICS_ENV = "TOOLHIVE_USAGE_METRICS_ENABLED"

# 플랫폼이 테넌트 Secret(Database 자격증명, 서비스 토큰)에 쓰는 동사 (P63). 읽기·만들기·SSA 와 키
# 지우기(patch)·지우기다. update(PUT)를 부르는 곳은 없다. 두 rbac.yaml 의 daegam-tenant-objects 가
# 이것을 주고, list·watch 는 어디에도 없다
SECRET_VERBS: tuple[str, ...] = ("get", "create", "patch", "delete")
# 클러스터 전체 Secret 권한이 남아 있다는 클러스터 응답의 경고 (P63). 이 릴리스의 RBAC 파일을
# 적용하면 사라진다
CLUSTER_SECRET_RIGHTS_WARNING = (
    "cluster_secret_rights: the platform's identity can still read Secrets in every namespace; "
    "apply this release's RBAC file, which grants Secret rights per managed namespace only, "
    "once preflight finds the RoleBinding daegam-tenant-objects in each of them "
    "(the chart's templates/rbac.yaml, P63)"
)
# 클러스터 전체 Secret 권한 없이 바인딩이 빠진 네임스페이스가 있다. 그곳의 Secret 은 읽지도 쓰지도
# 못한다. 배포 길과 auth reapply 의 네임스페이스 쓸기가 바인딩을 만든다
# 함께 올린 Redis 의 TLS 서버 인증서가 이 안에 끝나면 경고한다 (P61 F3)
SHIPPED_REDIS_CERT_WARN = timedelta(days=30)
SHIPPED_REDIS_CERT_WARNING = (
    "shipped_redis_cert_expiring: the TLS server certificate of the Redis this deployment ships "
    "ends within 30 days; renew it with the deploy procedure of this installation. When the CA "
    "itself ends within 60 days the server certificate cannot outlive it: replace the CA first "
    "(P61)"
)
TENANT_SECRET_BINDINGS_WARNING = (
    "tenant_secret_bindings_missing: without cluster-wide Secret rights the platform cannot read "
    "or write Secrets in the managed namespaces that lack the RoleBinding daegam-tenant-objects; "
    "a deploy or the auth reapply's namespace sweep creates it, then run preflight again (P63)"
)

# NetworkPolicy 를 집행하는 CNI/컨트롤러 워크로드 이름(접두어). DaemonSet 또는 Deployment 로 찾는다.
# OrbStack K8s(Flannel) 에는 없어 unknown 이다 (D16 전제, factcheck-orbstack-networkpolicy)
POLICY_CONTROLLERS: tuple[str, ...] = ("calico-node", "cilium", "kube-router", "antrea-agent")

# 사실만 적는다. 집행은 요구 사항이 아니라서 집행하지 않는 것이 실패는 아니고, 격리를
# 약속했다가 못 지킨다는 뜻도 아니다. 무인증 프록시가 클러스터 안에 열려 있는 것은
# 집행 여부와 무관한 D16 의 현재 상태다 (D38)
NETWORK_POLICY_WARNING = (
    "network_policy_unenforced: no NetworkPolicy controller (calico-node/cilium/kube-router) "
    "was detected on this cluster, so the NetworkPolicy objects the platform creates have no "
    "effect here; they are defense in depth and nothing depends on them (D38)"
)


# k3s 가 바이너리 안에서 돌리는 정책 컨트롤러의 이름. 워크로드로 보이지 않아 preflight 가 이
# 이름을 적는다
K3S_POLICY_CONTROLLER = "k3s (embedded kube-router)"


def scan_policy_controller(client: KubeClient) -> tuple[str | None, bool]:
    """정책 컨트롤러 워크로드 `namespace/name` 과 목록을 다 읽었는지. 찾으면 (이름, 참)이다. 찾지
    못했는데 목록 하나라도 읽지 못했으면 없는지 모르므로 (None, 거짓)이다."""
    scanned = True
    for kind in ("DaemonSet", "Deployment"):
        try:
            items = client.list("apps/v1", kind)
        except Exception as e:  # noqa: BLE001
            logger.debug("policy controller scan (%s) failed: %s", kind, e)
            scanned = False
            continue
        for item in items:
            meta = item.get("metadata") or {}
            name = meta.get("name") or ""
            if any(name == c or name.startswith(c + "-") for c in POLICY_CONTROLLERS):
                return f"{meta.get('namespace') or ''}/{name}", True
    return None, scanned


def detect_policy_controller(client: KubeClient) -> str | None:
    """정책 컨트롤러 워크로드 `namespace/name`. 없거나 조회 실패면 None."""
    return scan_policy_controller(client)[0]


def preflight_warnings(preflight: Preflight | None) -> list[str]:
    """Cluster 응답과 Instance 생성 응답에 붙는 경고. preflight 가 없으면 unknown 으로 본다.
    presumed(k3s)는 경고하지 않고 화면이 "추정" 으로 보인다(D54)."""
    if preflight is None or preflight.network_policy == "unknown":
        return [NETWORK_POLICY_WARNING]
    return []


def shipped_redis_cert_expiring(preflight: Preflight | None) -> bool:
    """home 이 함께 올린 Redis 의 TLS 서버 인증서가 30일 안에 끝나는가 (P61 F3)."""
    end = preflight.shipped_redis_cert_expires_at if preflight is not None else None
    if end is None:
        return False
    if end.tzinfo is not None:
        end = end.astimezone(UTC).replace(tzinfo=None)
    return end - utcnow() < SHIPPED_REDIS_CERT_WARN


def cluster_warnings(preflight: Preflight | None) -> list[str]:
    """Cluster 응답에만 붙는 경고. 클러스터 관리자가 할 일이라 Instance 생성 응답에는 싣지 않는다.
    클러스터 전체 Secret 권한이 남아 있거나, 그 권한 없이 바인딩이 빠진 네임스페이스가 있으면 그
    경고를 더한다 (P63). 함께 올린 Redis 의 TLS 서버 인증서가 30일 안에 끝나도 경고한다 (P61)."""
    out = preflight_warnings(preflight)
    if shipped_redis_cert_expiring(preflight):
        out.append(SHIPPED_REDIS_CERT_WARNING)
    rights = preflight.secret_rights if preflight is not None else None
    if rights is None:
        return out
    if rights.cluster_wide is True:
        out.append(CLUSTER_SECRET_RIGHTS_WARNING)
    elif rights.cluster_wide is False and rights.unbound:
        out.append(TENANT_SECRET_BINDINGS_WARNING)
    return out


def is_k3s(k8s_version: str | None) -> bool:
    """k3s 의 gitVersion 은 `v1.36.5+k3s1` 꼴이다."""
    return bool(k8s_version) and "+k3s" in (k8s_version or "")


@dataclass(frozen=True)
class GateInputs:
    """게이트(k8s/gates.py)가 읽는 재료 (P50 2절). None 인 칸은 이번 점검이 판정하지 못했다는
    뜻이라 저장된 값을 그대로 둔다(`ClusterService.preflight`). 연결 오류 한 번으로 한 시간 동안
    게이트가 닫히지 않게 하려는 것이다."""

    # ClusterRole daegam-tenant-objects 가 있고 get·bind 가 허용되는가. 묻다가 오류가 나면 None 이다
    tenant_object_rights: bool | None = None
    # 샌드박스의 권한과 PriorityClass 가 다 있는가 (P40 10절, `sandbox_rights`). 오류면 None 이다
    sandbox_rights: bool | None = None
    # 오퍼레이터 Deployment 를 읽었는가. 못 읽었으면 판(toolhive_version)과 usage_metrics 를 둔다
    operator_read: bool = False
    # 오퍼레이터 env 로 본 사용량 지표. 읽었는데 값이 valueFrom 이라 알 수 없으면 None 이다
    usage_metrics: bool | None = None
    # 정책 컨트롤러를 찾는 워크로드 목록을 다 읽었는가(`scan_policy_controller`). 못 읽었으면 저장된
    # 컨트롤러와 판정을 둔다. 한 번의 실패로 메타데이터 차단(T75)이 풀리지 않게 하려는 것이다
    policy_scanned: bool = False


def connect(client: KubeClient) -> tuple[str | None, Preflight | None]:
    """연결 확인(/version). (Kubernetes 판, 실패를 적은 Preflight). 연결하지 못했으면 판은 None 이고
    게이트 재료는 모두 판정하지 못한 것이다. 예외를 던지지 않는다."""
    try:
        return client.version().get("gitVersion"), None
    except ClusterUnreachable as e:
        return None, Preflight(ok=False, missing=[f"connect: {e}"])
    except Exception as e:  # noqa: BLE001
        return None, Preflight(ok=False, missing=[f"connect: {type(e).__name__}: {e}"])


def check_cluster(
    client: KubeClient, tenant_namespaces: Collection[str] | None = None
) -> tuple[Preflight, GateInputs]:
    """preflight 와 게이트 재료. 예외를 던지지 않는다. 실패는 Preflight(ok=False, missing=[...]) 로
    돌려준다. 연결 확인(`connect`) 뒤의 점검은 `check_connected` 다."""
    k8s_version, failed = connect(client)
    if failed is not None:
        return failed, GateInputs()
    return check_connected(client, k8s_version, tenant_namespaces)


class _StopWhenUnreachable:
    """클러스터가 한 번 연결할 수 없다고 답하면(시한 포함) 그 뒤의 호출은 서버에 묻지 않고 바로
    실패하는 클라이언트 (P65 2절). 점검은 저마다 오류를 적고 넘어가므로, 이것이 없으면 /version 에만
    답한 서버가 남은 점검마다 시한 하나씩을 쓴다."""

    def __init__(self, client: KubeClient):
        self._client = client
        self.unreachable: Exception | None = None

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.unreachable is not None:
                raise ClusterUnreachable(f"skipped after: {self.unreachable}")
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                if cluster_down(e):
                    self.unreachable = e
                raise

        return call


def check_connected(
    client: KubeClient, k8s_version: str | None, tenant_namespaces: Collection[str] | None = None
) -> tuple[Preflight, GateInputs]:
    """연결이 확인된 클러스터의 나머지 점검. 점검 도중 클러스터가 연결할 수 없게 되면 남은 점검을
    건너뛰고 연결 실패로 돌려준다(게이트 재료는 판정하지 못한 것이다)."""
    guarded = _StopWhenUnreachable(client)
    preflight, gates = _check_connected(guarded, k8s_version, tenant_namespaces)  # type: ignore[arg-type]
    if guarded.unreachable is not None:
        return Preflight(
            ok=False,
            k8s_version=k8s_version,
            missing=[f"connect: {type(guarded.unreachable).__name__}: {guarded.unreachable}"],
        ), GateInputs()
    return preflight, gates


def _check_connected(
    client: KubeClient, k8s_version: str | None, tenant_namespaces: Collection[str] | None = None
) -> tuple[Preflight, GateInputs]:
    """연결이 확인된 클러스터의 나머지 점검. 네임스페이스 목록은 연결 확인 뒤에 얻으므로 따로
    받는다.

    tenant_namespaces 는 플랫폼이 테넌트 객체를 두는 네임스페이스다(인증 재적용의 스윕과 같은 곳).
    주면 그곳마다 RoleBinding daegam-tenant-objects 를 읽어 본다. None 이면 보지 않는다 (P63)."""
    missing: list[str] = []
    rbac: list[RbacCheck] = []
    toolhive_version: str | None = None
    operator_read = False
    usage_metrics: bool | None = None

    for check, error in _check_verbs(client, REQUIRED_VERBS):
        rbac.append(check)
        if error:
            missing.append(f"rbac-check: {_verb_label(check)}: {error}")
        if not check.allowed:
            missing.append(f"rbac: {_verb_label(check)}")
    gated = _check_verbs(client, TENANT_OBJECT_VERBS)
    rbac.extend(check for check, _error in gated)
    tenant_object_rights, role = _tenant_object_rights(client, gated)
    sandbox_gated = _check_verbs(client, SANDBOX_VERBS)
    rbac.extend(check for check, _error in sandbox_gated)
    sandbox = sandbox_rights(client, sandbox_gated, tenant_object_rights, role)
    secret_rights, secret_errors = check_secret_rights(
        client, role, tenant_object_rights, tenant_namespaces
    )
    missing.extend(secret_errors)
    if secret_rights.writable is False:
        missing.append(
            f"rbac: core/secrets: neither cluster-wide nor through the ClusterRole "
            f"{TENANT_OBJECTS_CLUSTER_ROLE}"
        )

    try:
        served = set(client.discover(TOOLHIVE_GV))
    except Exception as e:  # noqa: BLE001
        served = set()
        missing.append(f"discovery: {TOOLHIVE_GV}: {e}")
    for r in REQUIRED_TOOLHIVE_RESOURCES:
        if r not in served:
            missing.append(f"crd: {TOOLHIVE_GV}/{r}")

    try:
        deploy = client.get("apps/v1", "Deployment", OPERATOR_DEPLOYMENT, OPERATOR_NAMESPACE)
        # 없다는 답도 읽은 것이다. 오퍼레이터가 없으면 판도 사용량 지표도 없다고 적는다
        operator_read = True
    except Exception as e:  # noqa: BLE001
        deploy = None
        missing.append(f"operator: {OPERATOR_NAMESPACE}/{OPERATOR_DEPLOYMENT}: {e}")
    if deploy is None:
        if not any(m.startswith("operator:") for m in missing):
            missing.append(f"operator: {OPERATOR_NAMESPACE}/{OPERATOR_DEPLOYMENT} not found")
    else:
        status = deploy.get("status") or {}
        available = (status.get("availableReplicas") or 0) > 0
        if not available:
            missing.append(f"operator: {OPERATOR_NAMESPACE}/{OPERATOR_DEPLOYMENT} not available")
        containers = (((deploy.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
            "containers"
        ) or []
        for c in containers:
            tag = image_tag(c.get("image") or "")
            if tag:
                toolhive_version = tag
                break
        usage_metrics = usage_metrics_enabled(containers)

    controller, policy_scanned = scan_policy_controller(client)
    if controller:
        network_policy = "enforced"
    elif is_k3s(k8s_version) and policy_scanned:
        # k3s 는 kube-router 정책 컨트롤러를 k3s 바이너리 안에서 돌려 워크로드로 보이지 않는다.
        # --disable-network-policy 로 띄웠을 수도 있어 추정이다. 실측은 DAEGAM_HOME_NETWORK_POLICY.
        # 목록을 다 읽지 못했으면 다른 컨트롤러가 있는지 모르므로 추정하지 않는다
        network_policy, controller = "presumed", K3S_POLICY_CONTROLLER
    else:
        network_policy = "unknown"
    preflight = Preflight(
        ok=not missing,
        k8s_version=k8s_version,
        toolhive_version=toolhive_version,
        rbac=rbac,
        missing=missing,
        network_policy=network_policy,
        policy_controller=controller,
        secret_rights=secret_rights,
    )
    gates = GateInputs(
        tenant_object_rights=tenant_object_rights,
        sandbox_rights=sandbox,
        operator_read=operator_read,
        usage_metrics=usage_metrics,
        policy_scanned=policy_scanned,
    )
    return preflight, gates


def _verb_label(check: RbacCheck) -> str:
    """`core/secrets:get`, 이름 한정이면 `rbac.authorization.k8s.io/clusterroles:bind[이름]`."""
    label = f"{check.group or 'core'}/{check.resource}:{check.verb}"
    return label + (f"[{check.name}]" if check.name else "")


def _check_verbs(
    client: KubeClient, entries: tuple[tuple[str, ...], ...]
) -> list[tuple[RbacCheck, str | None]]:
    """항목마다 SelfSubjectAccessReview 를 묻는다. 묻다가 난 오류는 거부로 보고 그 글을 함께
    돌려준다."""
    out: list[tuple[RbacCheck, str | None]] = []
    for entry in entries:
        group, resource, verb = entry[:3]
        name = entry[3] if len(entry) > 3 else None
        error: str | None = None
        try:
            allowed = client.can_i(verb, resource, group, name=name)
        except Exception as e:  # noqa: BLE001
            allowed, error = False, str(e)
        check = RbacCheck(group=group, resource=resource, verb=verb, name=name, allowed=allowed)
        out.append((check, error))
    return out


def _tenant_object_rights(
    client: KubeClient, gated: list[tuple[RbacCheck, str | None]]
) -> tuple[bool | None, dict[str, Any] | None]:
    """권한 게이트의 판정과 읽은 ClusterRole. get·bind 가 모두 허용되고 ClusterRole 이 있어야
    참이다. 묻거나 읽다가 오류가 났으면 판정하지 못한 것이라 None 이다."""
    if any(error for _check, error in gated):
        return None, None
    if not all(check.allowed for check, _error in gated):
        return False, None
    try:
        role = client.get(RBAC_API, "ClusterRole", TENANT_OBJECTS_CLUSTER_ROLE)
    except Exception as e:  # noqa: BLE001 - 읽기 오류는 판정하지 못한 것이다
        logger.debug("cluster role %s unreadable: %s", TENANT_OBJECTS_CLUSTER_ROLE, e)
        return None, None
    return role is not None, role


def role_grants(role: dict[str, Any], resource: str, wanted: Collection[str]) -> bool:
    """ClusterRole 이 core 의 resource 에 wanted 동사를 모두 주는가. apiGroups·resources·verbs 의
    `*` 도 센다. 이름 한정 규칙은 세지 않는다."""
    verbs: set[str] = set()
    for rule in role.get("rules") or []:
        if not {"", "*"} & set(rule.get("apiGroups") or []) or rule.get("resourceNames"):
            continue
        if {resource, "*"} & set(rule.get("resources") or []):
            verbs.update(rule.get("verbs") or [])
    return "*" in verbs or set(wanted) <= verbs


def role_grants_secrets(role: dict[str, Any]) -> bool:
    """ClusterRole 이 core 의 secrets 에 `SECRET_VERBS` 를 모두 주는가."""
    return role_grants(role, "secrets", SECRET_VERBS)


def sandbox_rights(
    client: KubeClient,
    gated: list[tuple[RbacCheck, str | None]],
    tenant_object_rights: bool | None,
    role: dict[str, Any] | None,
) -> bool | None:
    """샌드박스 게이트의 `rights` (P40 8절·10절). 노드 get·list 와 PriorityClass get 이 허용되고,
    테넌트 객체 권한이 있으며 그 역할이 파드 create·delete 를 주고, PriorityClass
    `daegam-sandbox` 가 있어야 참이다. 묻거나 읽다가 오류가 났으면 None 이다."""
    if any(error for _check, error in gated) or tenant_object_rights is None:
        return None
    if not all(check.allowed for check, _error in gated) or not tenant_object_rights:
        return False
    if role is None or not role_grants(role, "pods", SANDBOX_POD_VERBS):
        return False
    try:
        found = client.get("scheduling.k8s.io/v1", "PriorityClass", SANDBOX_PRIORITY_CLASS)
    except Exception as e:  # noqa: BLE001 - 읽기 오류는 판정하지 못한 것이다
        logger.debug("priority class %s unreadable: %s", SANDBOX_PRIORITY_CLASS, e)
        return None
    return found is not None


def secret_writability(
    cluster_wide: bool | None, tenant_object_rights: bool | None, role: bool | None
) -> bool | None:
    """테넌트 Secret 을 쓸 수 있는가 (P63 2절). 한 길이라도 열려 있다고 확인되면 참이고, 클러스터
    전체 길이 닫혀 있고 daegam-tenant-objects 길도 닫혀 있다고(권한 게이트나 역할의 Secret 동사가
    거짓) 확인될 때만 거짓이다. 그 밖은 판정하지 못한 것이라 None 이다."""
    if cluster_wide is True or (tenant_object_rights is True and role is True):
        return True
    if cluster_wide is False and (tenant_object_rights is False or role is False):
        return False
    return None


def check_secret_rights(
    client: KubeClient,
    role: dict[str, Any] | None,
    tenant_object_rights: bool | None,
    tenant_namespaces: Collection[str] | None,
) -> tuple[SecretRightsCheck, list[str]]:
    """테넌트 Secret 권한의 두 길 (P63). (검사, missing 에 더할 접근 검사 오류).

    - 클러스터 전체: `SECRET_VERBS` 마다 SelfSubjectAccessReview 를 따로 묻는다(네임스페이스 없이).
      모두 허용이면 참이라 옛 ClusterRole 의 권한이 남아 있고, 하나라도 거부면 거짓이다. 거부 없이
      오류가 난 동사가 있으면 None 이다.
    - 역할: 읽어 둔 ClusterRole daegam-tenant-objects 가 Secret 동사를 담는가. 못 읽었으면 None
      이다. 둘과 권한 게이트로 쓸 수 있는지를 정한다(`secret_writability`).
    - 바인딩: tenant_namespaces 마다 RoleBinding 을 읽어, daegam-tenant-objects 를 가리키고 플랫폼
      자신을 묶는지 본다. 자신을 알 수 없으면 가리키는 역할만 본다. 읽지 못한 곳도 빠진 곳이다."""
    allowed: list[bool] = []
    errors: list[str] = []
    for verb in SECRET_VERBS:
        try:
            allowed.append(client.can_i(verb, "secrets", ""))
        except Exception as e:  # noqa: BLE001 - 묻다가 난 오류는 판정하지 못한 것이다
            errors.append(f"rbac-check: core/secrets:{verb}: {e}")
    cluster_wide = False if not all(allowed) else (None if errors else True)
    role_ok = role_grants_secrets(role) if role is not None else None
    check = SecretRightsCheck(
        cluster_wide=cluster_wide,
        role=role_ok,
        writable=secret_writability(cluster_wide, tenant_object_rights, role_ok),
    )
    if tenant_namespaces is None:
        return check, errors
    subject = platform_subject(client)
    check.namespaces = len(set(tenant_namespaces))
    for ns in sorted(set(tenant_namespaces)):
        try:
            binding = client.get(RBAC_API, "RoleBinding", TENANT_OBJECTS_BINDING_NAME, ns)
        except Exception as e:  # noqa: BLE001 - 읽지 못한 바인딩은 확인하지 못한 것이다
            logger.debug("role binding %s/%s unreadable: %s", ns, TENANT_OBJECTS_BINDING_NAME, e)
            binding = None
        if binding is None or not _binding_grants(binding, subject):
            check.unbound_namespaces.append(ns)
    check.unbound = len(check.unbound_namespaces)
    return check, errors


def _binding_grants(binding: dict[str, Any], subject: dict[str, Any] | None) -> bool:
    """RoleBinding 이 ClusterRole daegam-tenant-objects 를 가리키고 subject 를 묶는가."""
    ref = binding.get("roleRef") or {}
    if ref.get("kind") != "ClusterRole" or ref.get("name") != TENANT_OBJECTS_CLUSTER_ROLE:
        return False
    if subject is None:
        return True
    keys = (
        ("kind", "name", "namespace")
        if subject.get("kind") == "ServiceAccount"
        else ("kind", "name")
    )
    subjects = binding.get("subjects") or []
    return any(all(s.get(k) == subject.get(k) for k in keys) for s in subjects)


def usage_metrics_enabled(containers: list[dict[str, Any]]) -> bool | None:
    """오퍼레이터 컨테이너 env 로 본 사용량 지표 (P50 G1). env 가 없으면 ToolHive 기본값이 켜짐이라
    참이고, 값이 "false" 면 거짓이며, valueFrom 처럼 값을 알 수 없으면 None 이다."""
    for c in containers:
        for env in c.get("env") or []:
            if env.get("name") != USAGE_METRICS_ENV:
                continue
            if "value" not in env:
                return None
            return str(env.get("value")).strip().lower() != "false"
    return True
