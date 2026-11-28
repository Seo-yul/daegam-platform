"""렌더러들이 함께 쓰는 것. ToolHive API 버전·프록시 포트, RenderError, ownerReferences 헬퍼,
허브 항목 라벨, 사설 엔드포인트 판정, OIDC 참조, ToolHive 가 만드는 Service 이름·주소 규칙,
MCPServer 백엔드 파드의 라벨, 파드 배치 정책, 설정을 curl 로 받는 파드, Opaque Secret, secretKeyRef
env, 관리자 CA 묶음 env, 테넌트 ingress 출처와 Database 바인딩 env."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from daegam.auth.signer import internal_audience, server_audience
from daegam.config.resolve import EffectiveConfig
from daegam.k8s.client import TOOLHIVE_API
from daegam.k8s.labels import LABEL_MCP_BACKEND, LABEL_TENANT, daegam_labels, ensure_labels
from daegam.store.data_services import REDIS_CA_KEY, DataService, RedisTls

# TOOLHIVE_API 의 정본은 k8s/client.py 다. 렌더들은 이 모듈에서 가져다 쓴다
NETWORK_POLICY_API = "networking.k8s.io/v1"
PROXY_PORT = 8080
# 테넌트 네임스페이스의 MCPOIDCConfig 이름. services/tenancy.py 가 이 이름으로 만든다
OIDC_CONFIG_NAME = "daegam-oidc"
# 내부 MCP 홉의 서비스 토큰을 검증하는 MCPOIDCConfig (D41). 발급자가 플랫폼이라 사람 토큰용과 다른
# 객체다
INTERNAL_OIDC_CONFIG_NAME = "daegam-internal-oidc"
# 설정을 받는 init 컨테이너 이미지와 그 설정 볼륨이 붙는 자리 (MCPServer·Agent 가 같다)
CURL_IMAGE = "curlimages/curl:8.14.1"
CONFIG_MOUNT = "/config"

# 우리가 만든 이미지(MCP 런타임·에이전트·curl init)는 모두 uid 1000 으로 돈다. PodSecurity
# `restricted` 가 요구하는 것과 같다. 루트 파일시스템 읽기 전용은 런타임이 /tmp 밖에 쓰지 않는지
# 확인한 뒤 켠다. 허브의 외부 이미지는 uid 를 알 수 없어 여기에 넣지 않는다
CONTAINER_SECURITY: dict[str, Any] = {
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "runAsNonRoot": True,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
    # 우리 이미지는 루트에 쓰지 않는다. 바이트코드는 PYTHONDONTWRITEBYTECODE, 라이브러리 캐시는
    # HOME=/tmp 로 emptyDir 에 떨어진다. 쓰는 자리는 마운트한 /tmp 하나다
    "readOnlyRootFilesystem": True,
}
POD_SECURITY: dict[str, Any] = {
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "fsGroup": 1000,
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}
# 우리가 만들지 않은 이미지(허브의 외부 MCP 이미지, ToolHive 의 vMCP)는 uid 를 알 수 없어 사용자를
# 정하지 않는다. 권한 상승·capability·seccomp 는 이미지와 무관하게 닫는다. 루트 읽기 전용은 그
# 이미지가 어디에 쓰는지 모르므로 켜지 않는다
FOREIGN_CONTAINER_SECURITY: dict[str, Any] = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}
FOREIGN_POD_SECURITY: dict[str, Any] = {"seccompProfile": {"type": "RuntimeDefault"}}


# 우리 설정이 아니라 고정값으로 두는 컨테이너 자원 (D54). 모든 컨테이너에 requests/limits 를
# 요구하는 클러스터에서 LimitRange 가 덮지 못하는 경우(Deployment·StatefulSet 수준의 강제)까지
# 통과하려고 CR 에 직접 적는다. ToolHive 는 MCPServer·MCPRemoteProxy 의 spec.resources 를 프록시
# 컨테이너에 넣고, vMCP 는 spec.resources 를 읽지 않아 podTemplateSpec 의 vmcp 컨테이너로만
# 바뀐다(ToolHive 0.51.2). 실측은 0.53.0 에서 잰 프록시 CPU 3m·메모리 103MiB, vMCP CPU 2m·메모리
# 107MiB 다. 메모리 요청이 실제보다 작으면 노드 메모리가 모자랄 때 이 파드가 먼저 쫓겨나므로
# 요청을 실제보다 크게 둔다. 호출마다 새 dict 를 준다(매니페스트끼리 공유하지 않는다)
def proxy_resources() -> dict[str, Any]:
    """MCPServer·MCPRemoteProxy 의 프록시와 기본 vMCP 컨테이너."""
    return {
        "requests": {"cpu": "10m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
    }


def init_resources() -> dict[str, Any]:
    """스펙·설정을 받는 init 컨테이너(curl, Agent 는 런타임 이미지의 표준 라이브러리 urllib).
    받아 쓰고 끝난다."""
    return {
        "requests": {"cpu": "10m", "memory": "16Mi"},
        "limits": {"cpu": "200m", "memory": "64Mi"},
    }


class RenderError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


def pod_placement(effective: EffectiveConfig) -> dict[str, Any]:
    """클러스터 스케줄링 정책을 파드 스펙 조각으로. 네 렌더러(MCPServer·Deployment·vMCP 둘)가
    같은 규칙을 쓰므로 여기 하나만 고치면 넷에 다 붙는다. 값이 없는 키는 넣지 않는다."""
    pod: dict[str, Any] = {}
    if effective.images.pull_secret:
        pod["imagePullSecrets"] = [{"name": effective.images.pull_secret}]
    sched = effective.scheduling
    if sched.node_selector:
        pod["nodeSelector"] = dict(sched.node_selector)
    if sched.tolerations:
        pod["tolerations"] = list(sched.tolerations)
    if sched.priority_class:
        pod["priorityClassName"] = sched.priority_class
    return pod


def curl_fetch(token_env: str, url: str, out_file: str) -> str:
    """설정을 받는 init 스크립트의 한 줄. env `token_env` 의 토큰을 Bearer 로 url 을 받아 설정
    볼륨의 out_file 에 쓴다."""
    return f'curl -fsSL -H "Authorization: Bearer ${token_env}" {url} -o {CONFIG_MOUNT}/{out_file}'


def config_fetch_pod(
    *,
    init_name: str,
    volume: str,
    init_env: list[dict[str, Any]],
    script: str,
    main: dict[str, Any],
    effective: EffectiveConfig,
    init_image: str = CURL_IMAGE,
    init_command: tuple[str, ...] = ("sh", "-c"),
    init_pull_policy: str | None = None,
    extra_volumes: tuple[tuple[dict[str, Any], dict[str, Any]], ...] = (),
) -> dict[str, Any]:
    """설정을 받아 쓰는 파드 스펙 (MCPServer 의 podTemplateSpec, Agent 의 Deployment).
    init 컨테이너가 설정 볼륨(emptyDir)에 받아 두고 메인 컨테이너는 그것을 읽기 전용으로 붙인다.
    두 워크로드의 보안 배치가 여기 한 곳에서 정해진다. init_name·volume 은 merge.py 의 잠금 표가
    이름으로 가리키므로 바꾸면 그쪽도 함께 바꾼다. init 은 기본이 curl 이미지의 `sh -c` 이고
    Agent 는 자기 런타임 이미지의 `python -c` 로 받는다(D65). init_pull_policy 는 그때 메인과
    같은 정책을 준다. extra_volumes 는 메인 컨테이너에만 붙이는 (볼륨, 마운트) 쌍이다(Redis 의
    CA, `binding_ca_volume`)."""
    container = {
        **main,
        "securityContext": CONTAINER_SECURITY,
        "volumeMounts": [
            {"name": volume, "mountPath": CONFIG_MOUNT, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
            *(mount for _, mount in extra_volumes),
        ],
    }
    init: dict[str, Any] = {
        "name": init_name,
        "image": init_image,
        "securityContext": CONTAINER_SECURITY,
        "resources": init_resources(),
        "env": init_env,
        "command": list(init_command),
        "args": [script],
        "volumeMounts": [
            {"name": volume, "mountPath": CONFIG_MOUNT},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
    }
    if init_pull_policy:
        init["imagePullPolicy"] = init_pull_policy
    pod: dict[str, Any] = {
        "securityContext": POD_SECURITY,
        "volumes": [
            {"name": volume, "emptyDir": {}},
            {"name": "tmp", "emptyDir": {}},
            *(vol for vol, _ in extra_volumes),
        ],
        "initContainers": [init],
        "containers": [container],
    }
    pod.update(pod_placement(effective))
    return pod


def rolled_out(deployment: dict[str, Any] | None) -> bool:
    """Deployment 의 롤아웃이 끝났는가. 지금 세대를 관찰했고, 모든 파드가 지금 템플릿이며 준비돼
    `replicas = updatedReplicas = availableReplicas` 가 원하는 수다. 옛 ReplicaSet 의 파드가
    남았거나 Deployment 가 없으면 거짓이다."""
    if not deployment or not generation_observed(deployment):
        return False
    st = deployment.get("status") or {}
    counts = [int(st.get(k) or 0) for k in ("replicas", "updatedReplicas", "availableReplicas")]
    return counts == [_wanted(deployment)] * 3


def current_template_ready(deployment: Mapping[str, Any] | None) -> bool:
    """모든 파드가 지금 템플릿이고 하나는 준비됐는가. 지금 세대를 관찰했고 `updatedReplicas` 가
    `replicas` 에 닿았으며 준비된 파드가 있다. 원하는 수가 모두 준비되기 전에도 참이다 (P44 8절,
    `api/_agent_proxy.py` `require_rolled_out`)."""
    if not deployment or not generation_observed(deployment):
        return False
    st = deployment.get("status") or {}
    ready = int(st.get("readyReplicas") or 0)
    return ready >= 1 and int(st.get("updatedReplicas") or 0) >= int(st.get("replicas") or 0)


def deployment_counts(deployment: Mapping[str, Any] | None) -> tuple[int, int]:
    """Deployment 의 (원하는 수, 준비된 파드 수). 없으면 (0, 0) 이다."""
    if not deployment:
        return 0, 0
    st = deployment.get("status") or {}
    return _wanted(deployment), int(st.get("readyReplicas") or 0)


def generation_observed(obj: Mapping[str, Any]) -> bool:
    """컨트롤러가 객체(Deployment, ToolHive CR)의 지금 세대를 관찰했는가."""
    meta = obj.get("metadata") or {}
    st = obj.get("status") or {}
    return st.get("observedGeneration") == meta.get("generation")


def _wanted(deployment: Mapping[str, Any]) -> int:
    """Deployment 가 원하는 레플리카 수. 칸이 없으면 쿠버네티스의 기본 1 이다."""
    spec = deployment.get("spec") or {}
    return int(spec["replicas"]) if spec.get("replicas") is not None else 1


def _template_metadata(deployment: dict[str, Any] | None, field: str) -> dict[str, Any]:
    """Deployment 파드 템플릿 metadata 의 field(annotations·labels). 없으면 빈 dict 다."""
    template = ((deployment or {}).get("spec") or {}).get("template") or {}
    return (template.get("metadata") or {}).get(field) or {}


def template_annotation(deployment: dict[str, Any] | None, key: str) -> str | None:
    """Deployment 파드 템플릿의 어노테이션 key. Deployment 나 그 어노테이션이 없으면 None 이다."""
    return _template_metadata(deployment, "annotations").get(key)


def template_label(deployment: dict[str, Any] | None, key: str) -> str | None:
    """Deployment 파드 템플릿의 라벨 key. Deployment 나 그 라벨이 없으면 None 이다."""
    return _template_metadata(deployment, "labels").get(key)


def named_item(items: Any, name: str) -> dict[str, Any]:
    """containers 나 containerStatuses 같은 목록에서 이름이 같은 항목이다. 없으면 빈 dict 다."""
    return next((c for c in items or [] if isinstance(c, dict) and c.get("name") == name), {})


def secret_refs(pod_template: dict[str, Any] | None) -> set[str]:
    """파드 템플릿이 가리키는 Secret 이름. env 의 secretKeyRef, envFrom 의 secretRef, secret
    볼륨이다."""
    spec = (pod_template or {}).get("spec") or {}
    names: set[str] = set()
    for c in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
        for e in c.get("env") or []:
            names.add(((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") or "")
        for e in c.get("envFrom") or []:
            names.add((e.get("secretRef") or {}).get("name") or "")
    for v in spec.get("volumes") or []:
        names.add((v.get("secret") or {}).get("secretName") or "")
    names.discard("")
    return names


def secret_env(name: str, secret: str, key: str) -> dict[str, Any]:
    """Secret 키 하나를 값으로 받는 컨테이너 env (secretKeyRef)."""
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def ca_env(name: str, pem: str) -> dict[str, str]:
    """관리자가 준 CA 묶음(`network.ca_bundle`, PEM)을 base64 로 싣는 env. 공개 인증서라 Secret 이
    필요 없고, ToolHive 는 줄바꿈이 든 env 값을 거절한다."""
    return {"name": name, "value": base64.b64encode(pem.encode()).decode()}


def opaque_secret(
    name: str,
    namespace: str,
    string_data: dict[str, str],
    *,
    tenant: str,
    resource: str,
    id: str,
    owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """플랫폼이 값을 정해 두는 Opaque Secret. 값은 stringData 로 준다."""
    doc = ensure_labels(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "stringData": string_data,
        },
        tenant=tenant,
        resource=resource,
        id=id,
    )
    return with_owner(doc, owner)


def dns_egress() -> dict[str, Any]:
    """클러스터 DNS(kube-system 의 kube-dns, 53)로 나가는 NetworkPolicy 규칙. 나가는 길을 좁히는
    정책은 모두 이것을 연다. 이름 풀기가 실패하면 기능이 조용히 꺼지는 곳(게이트웨이의 전역 한도)이
    있기 때문이다."""
    return {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }


def tenant_ingress_from(tenant: str, platform_namespace: str) -> list[dict[str, Any]]:
    """테넌트 격리 규칙의 ingress 출처. 같은 네임스페이스, 같은 테넌트 라벨이 붙은 네임스페이스,
    플랫폼 네임스페이스(프록시)다. 테넌트 NetworkPolicy 와 Agent 파드의 NetworkPolicy 가 이 목록을
    함께 쓴다. NetworkPolicy 는 합집합이라 한쪽만 좁히면 다른 쪽으로 옛 출처가 열린 채 남는다."""
    return [
        {"podSelector": {}},
        {"namespaceSelector": {"matchLabels": {LABEL_TENANT: tenant}}},
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": platform_namespace}}},
    ]


def binding_url(ds: DataService, auth: str = "", tls: RedisTls | None = None) -> str:
    """Database 바인딩의 `<PREFIX>_URL`. auth 를 주면 `auth@` 를 호스트 앞에 붙인다. tls 를 주면
    Redis 의 그 TLS 끝점을 `rediss://` 로 가리킨다(`tls_endpoint`)."""
    at = f"{auth}@" if auth else ""
    if ds.kind == "redis":
        if tls is not None:
            return f"rediss://{at}{ds.host}:{tls.port}/{ds.db or 0}"
        return f"redis://{at}{ds.host}:{ds.port}/{ds.db or 0}"
    return f"postgresql://{at}{ds.host}:{ds.port}/{ds.database or ''}"


def tls_endpoint(ds: DataService, allowed: bool) -> RedisTls | None:
    """이 렌더가 쓸 Database 의 TLS 끝점. Database 가 TLS 끝점을 가진 Redis 이고, 부르는 쪽이 쓸 수
    있다고 정했거나(allowed) 평문 포트가 닫혔을 때다(`plain_closed`, P61 5절 5단계). 닫힌 평문
    포트로는 붙을 수 없으므로 TLS 를 모르는 소비자는 배포가 먼저 막는다. 쓰기로 하면 평문으로
    물러서지 않는다 (P61 3절)."""
    if ds.kind == "redis" and (allowed or ds.tls_only):
        return ds.tls
    return None


# 바인딩 env 이름의 꼬리. `<env_prefix>_<꼬리>` 로 붙는다(`binding_env_name`). MCP Server 의 배포
# 스냅샷을 살아 있는 MCPServer 에서 채우는 쪽(`services/mcp_inputs.py`)도 이 이름으로 읽는다
BINDING_HOST = "HOST"
BINDING_PORT = "PORT"
BINDING_DB = "DB"
BINDING_DATABASE = "DATABASE"
BINDING_URL = "URL"
BINDING_USER = "USER"
BINDING_PASSWORD = "PASSWORD"
# Redis 의 TLS 끝점을 쓰는 바인딩만 낸다 (P61 3절). TLS 는 "1", CA_FILE 은 붙인 CA 의 경로다
BINDING_TLS = "TLS"
BINDING_CA_FILE = "CA_FILE"
# 바인딩의 CA Secret 을 붙이는 자리. 접두어마다 아래에 하나다
REDIS_CA_MOUNT = "/var/run/daegam/redis-ca"


def binding_env_name(prefix: str, tail: str) -> str:
    """바인딩 env 이름 `<prefix>_<tail>`. tail 은 위의 `BINDING_*` 다."""
    return f"{prefix}_{tail}"


def binding_env_and_secrets(
    prefix: str, ds: DataService, *, allow_tls: bool = False
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(평문 env, spec.secrets 참조). URL 은 비밀 없이 host:port/db 형태다.
    username 은 Mongo 에도 평문으로 있으므로 평문 env 로 준다. TLS 끝점을 쓰면(`tls_endpoint`) PORT
    가 TLS 포트이고 URL 이 `rediss://` 이며 `<PREFIX>_TLS=1` 과 `<PREFIX>_CA_FILE` 이 더해진다. CA
    는 `binding_ca_volume` 이 그 경로에 붙인다."""

    def name(tail: str) -> str:
        return binding_env_name(prefix, tail)

    tls = tls_endpoint(ds, allow_tls)
    env = [
        {"name": name(BINDING_HOST), "value": ds.host},
        {"name": name(BINDING_PORT), "value": str(tls.port if tls is not None else ds.port)},
    ]
    if ds.kind == "redis":
        env.append({"name": name(BINDING_DB), "value": str(ds.db or 0)})
    else:
        env.append({"name": name(BINDING_DATABASE), "value": ds.database or ""})
    env.append({"name": name(BINDING_URL), "value": binding_url(ds, tls=tls)})
    if ds.username:
        env.append({"name": name(BINDING_USER), "value": ds.username})
    if tls is not None:
        env.append({"name": name(BINDING_TLS), "value": "1"})
        env.append({"name": name(BINDING_CA_FILE), "value": binding_ca_file(prefix)})
    secrets: list[dict[str, str]] = []
    if ds.has_password:
        secrets.append(
            {"name": ds.secret_name, "key": "password", "targetEnvName": name(BINDING_PASSWORD)}
        )
    return env, secrets


def binding_ca_file(prefix: str) -> str:
    """바인딩의 CA 파일 경로 `<REDIS_CA_MOUNT>/<PREFIX>/ca.crt` 다."""
    return f"{REDIS_CA_MOUNT}/{prefix}/{REDIS_CA_KEY}"


def binding_ca_volume(
    prefix: str, ds: DataService, *, allow_tls: bool
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """TLS 끝점을 쓰는 바인딩의 (파드 볼륨, 컨테이너 마운트). 쓰지 않으면 None 이다. 볼륨은
    Database 의 CA Secret 에서 `ca.crt` 하나만 담는다. 볼륨 이름은 접두어의 지문이라 길이가 접두어와
    상관없이 63 자 안이다."""
    tls = tls_endpoint(ds, allow_tls)
    if tls is None:
        return None
    digest = hashlib.sha256(prefix.encode()).hexdigest()[:12]
    volume_name = f"redis-ca-{digest}"
    volume = {
        "name": volume_name,
        "secret": {
            "secretName": tls.ca_secret,
            "items": [{"key": REDIS_CA_KEY, "path": REDIS_CA_KEY}],
        },
    }
    mount = {"name": volume_name, "mountPath": f"{REDIS_CA_MOUNT}/{prefix}", "readOnly": True}
    return volume, mount


# 파드가 믿는 Redis CA 의 지문 (P61). redis-py 와 ToolHive 프록시는 연결을 만들 때 읽은 CA 를
# 쥐고 있어서 kubelet 이 마운트한 CA 파일을 바꿔도 새 CA 를 믿지 않는다. 그래서 CA 가 바뀌면 이
# 값이 바뀌어 파드가 다시 뜬다. TLS 로 붙는 Redis 가 없는 파드에는 없다
REDIS_CA_ANNOTATION = "daegam.devops.ai.kr/redis-ca"
REDIS_CA_SHA_LENGTH = 16
# 지문을 적기 전에 저장한 CA (`RedisTls.ca_sha256` 가 None)
UNKNOWN_CA = "unknown"
# 세션 저장소가 클러스터 기본 Redis 의 TLS 로 붙을 때 그 CA 의 출처 이름. 다른 출처는 Database id 다
DEFAULT_REDIS_SOURCE = "default_redis"


def redis_ca_mark(fingerprints: Mapping[str, str | None]) -> str | None:
    """`REDIS_CA_ANNOTATION` 의 값. 출처(Database id 나 `DEFAULT_REDIS_SOURCE`)마다
    `<출처>=<CA 지문 앞 16자>` 를 출처 순으로 쉼표로 잇는다. 모르는 지문은 `UNKNOWN_CA` 다. 믿는
    CA 가 없으면 None 이다. 재적용은 이 값에서 출처를 다시 읽고(`redis_ca_sources`), 함께 올린
    Redis 를 두는 배포는 CA 를 바꾸는 2 단계 전에 이 값으로 옛 CA 를 믿는 파드를 찾는다."""
    parts = (
        f"{source}={(fp or UNKNOWN_CA)[:REDIS_CA_SHA_LENGTH]}"
        for source, fp in sorted(fingerprints.items())
    )
    return ",".join(parts) or None


def redis_ca_fingerprints(mark: str | None) -> dict[str, str]:
    """`redis_ca_mark` 값을 출처마다의 지문 앞 16자(또는 `UNKNOWN_CA`)로 되돌린다."""
    pairs = (part.partition("=") for part in mark.split(",")) if mark else ()
    return {source: fingerprint for source, _, fingerprint in pairs}


def redis_ca_sources(mark: str | None) -> list[str]:
    """`redis_ca_mark` 값의 출처들."""
    return list(redis_ca_fingerprints(mark))


def with_owner(doc: dict[str, Any], owner: dict[str, Any] | None) -> dict[str, Any]:
    if owner:
        doc["metadata"]["ownerReferences"] = [owner]
    return doc


def owned_by(
    obj: Mapping[str, Any] | None, kind: str, name: str, *, controller: bool = False
) -> bool:
    """obj 의 ownerReferences 에 그 종류와 이름의 소유자가 있는가. controller 면 그 소유자가
    컨트롤러여야 한다. 읽은 객체가 플랫폼이 만든 것의 것인지 지우기 전에 본다 (원격 프록시의
    RoleBinding, stdio 항목의 프록시 파드와 Deployment)."""
    owners = ((obj or {}).get("metadata") or {}).get("ownerReferences") or []
    return any(
        o.get("kind") == kind and o.get("name") == name and (o.get("controller") or not controller)
        for o in owners
    )


def owner_ref(kind: str, name: str, uid: str, *, api_version: str = TOOLHIVE_API) -> dict[str, Any]:
    """렌더한 객체를 소유자에 종속시켜 소유자가 지워지면 함께 GC 되게 한다. 소유자는 기본이
    ToolHive CR(MCPServer | VirtualMCPServer)이고, Agent 의 노출 객체는 apps/v1 Deployment 다."""
    return {
        "apiVersion": api_version,
        "kind": kind,
        "name": name,
        "uid": uid,
        "blockOwnerDeletion": False,
        "controller": False,
    }


# 허브 항목이 두는 CR(원본과 공유 복제본)의 `daegam.devops.ai.kr/resource` 라벨 값
RESOURCE_HUB_ITEM = "hub-item"


def hub_labels(tenant: str, item_id: str, kind: str) -> dict[str, str]:
    labels = daegam_labels(tenant=tenant, resource=RESOURCE_HUB_ITEM, id=item_id)
    labels["daegam.devops.ai.kr/hub-kind"] = kind
    return labels


def is_private_endpoint(url: str) -> bool:
    """RFC1918/loopback IP, `.cluster.local`, `.svc`(로 끝나거나 `.svc.` 포함) 면 True."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    return (
        host.endswith(".cluster.local")
        or host.endswith(".svc")
        or ".svc." in host
        or host == "localhost"
    )


def oidc_config_ref(
    mcp_audience: str, resource_url: str | None = None, scopes: tuple[str, ...] = ()
) -> dict[str, Any]:
    """MCPOIDCConfig 참조. resourceUrl 은 노출했을 때의 external_url (RFC 9728 보호 리소스
    메타데이터로 MCP 클라이언트의 OAuth 디스커버리가 외부 URL 을 보게 한다). scopes 는 PRM 의
    `scopes_supported` 와 401 의 `scope` 로 광고만 한다. 비우면 ToolHive 가 `openid` 를 광고한다
    (P33 ToolHive 표 3번)."""
    ref: dict[str, Any] = {"name": OIDC_CONFIG_NAME, "audience": mcp_audience}
    if resource_url:
        ref["resourceUrl"] = resource_url
    if scopes:
        ref["scopes"] = list(scopes)
    return ref


def internal_oidc_config_ref(
    tenant: str,
    resource_url: str,
    server_id: str | None = None,
    *,
    audience: str | None = None,
) -> dict[str, Any]:
    """내부 MCPServer 의 `spec.oidcConfigRef` (D41). audience 가 테넌트마다 달라 그것이 테넌트
    격리다. server_id 를 주면 그 서버 하나만 받는 audience 다 (P50 9절, G9 2단계). audience 를 주면
    그 값이다(web_fetch 복제본의 fetch audience, P39b 13절). resourceUrl 은 프록시 Service 의 실제
    주소다. 비우면 401 챌린지가 CR 이름 호스트를 광고해 닿지 않는 주소가 된다."""
    if audience is None:
        audience = server_audience(tenant, server_id) if server_id else internal_audience(tenant)
    return {
        "name": INTERNAL_OIDC_CONFIG_NAME,
        "audience": audience,
        "resourceUrl": resource_url,
    }


def proxy_service_name(cr_name: str, *, remote: bool = False) -> str:
    """ToolHive 가 만드는 프록시 Service 이름 규칙.
    MCPServer: mcp-<name>-proxy, MCPRemoteProxy: mcp-<name>-remote-proxy."""
    return f"mcp-{cr_name}-{'remote-proxy' if remote else 'proxy'}"


def proxy_service_url(cr_name: str, namespace: str, *, remote: bool = False) -> str:
    """ToolHive 가 만드는 프록시 Service 의 클러스터 안 MCP 주소."""
    service = proxy_service_name(cr_name, remote=remote)
    return f"http://{service}.{namespace}.svc.cluster.local:{PROXY_PORT}/mcp"


def mark_mcp_pods(spec: dict[str, Any], cr_name: str) -> dict[str, Any]:
    """MCPServer spec 에 백엔드 파드의 라벨 `LABEL_MCP_BACKEND` 를 단다(제자리 수정 후 같은 dict
    반환, T67). podTemplateSpec 의 metadata 에 두면 프록시가 그 템플릿을 StatefulSet 파드에
    덮으므로 백엔드 파드에만 간다(0.53.0 실측, P39 스파이크). 프록시 파드는 오퍼레이터의
    `LABEL_APP_NAME` 으로 가린다. Advanced 편집이 두 라벨을 모두 잠근다(`k8s/merge.py`)."""
    template = spec.setdefault("podTemplateSpec", {}).setdefault("metadata", {})
    template.setdefault("labels", {})[LABEL_MCP_BACKEND] = cr_name
    return spec


def proxy_mcp_url(status_url: str, transport: str) -> str:
    """MCPRemoteProxy/MCPServer status.url 에 transport 별 경로를 보장한다."""
    base = status_url.rstrip("/")
    path = "/sse" if transport == "sse" else "/mcp"
    return base if base.endswith(path) else f"{base}{path}"
