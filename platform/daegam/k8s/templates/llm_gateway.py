"""LLM 게이트웨이 한 벌의 객체 렌더 (P38 4절·7절·10절).

게이트웨이 네임스페이스(`Settings.llm_gateway_namespace`)에 게이트웨이 `llm-gateway`(2 레플리카),
한도 서비스 `llm-ratelimit` 과 그 계수기 `llm-redis`, 파드마다의 NetworkPolicy 를 둔다. 수집기
`llm-otel` 은 8절이다. 모든 객체는 라벨 `daegam.devops.ai.kr/resource` 와 이 설치의
`daegam.devops.ai.kr/platform-id` 를 단다. 동기화(`services/llm_gateway.py`)가 이 라벨로 자기 객체만
지운다.

남의 이미지 넷은 사용자를 숫자로 정한다. PodSecurity restricted 를 집행하고 `FOREIGN_POD_SECURITY`
는 사용자를 정하지 않으며, 한도 서비스와 Redis 의 원본 이미지는 root 로 뜬다 (F0 1번). Secret 은
`data` 로 쓴다. SSA 는 stringData 로 넣은 키를 다음 적용에서 빼도 남기므로, 지운 provider 의 키
파일이 남지 않게 한다.
"""

from __future__ import annotations

from typing import Any

import yaml

from daegam.k8s.client import encode_secret_data
from daegam.k8s.labels import LABEL_MANAGED_BY, LABEL_PLATFORM_ID, LABEL_RESOURCE, MANAGED_BY
from daegam.k8s.templates.common import (
    FOREIGN_CONTAINER_SECURITY,
    FOREIGN_POD_SECURITY,
    NETWORK_POLICY_API,
    dns_egress,
)
from daegam.k8s.templates.llm_gateway_config import (
    CONFIG_DIR,
    CONFIG_FILE,
    GATEWAY_PORT,
    PRESTOP_SECONDS,
    RATELIMIT_DOMAIN,
    RATELIMIT_PORT,
    READY_PORT,
    REV_PORT,
    TERMINATION_GRACE_SECONDS,
)
from daegam.k8s.templates.web_tools import GLOBAL_V6_BLOCK
from daegam.store.llm_gateway import LlmGatewayImages
from daegam.store.settings import LlmGatewayLimits

GATEWAY = "llm-gateway"
CONFIG_SECRET = "llm-gateway-config"
# 마지막으로 반영된 리비전의 사본. 붙이지 않는다. 파드가 버린 설정을 되돌릴 때 읽는다 (4절)
APPLIED_SECRET = "llm-gateway-config-applied"
RATELIMIT = "llm-ratelimit"
REDIS = "llm-redis"
# 수집기와 그 수집 토큰 파일 (8절)
COLLECTOR = "llm-otel"
COLLECTOR_TOKEN_SECRET = "llm-otel-token"
COLLECTOR_TOKEN_KEY = "token"
OTLP_GRPC_PORT = 4317
COLLECTOR_HEALTH_PORT = 13133
COLLECTOR_UID = 10001
COLLECTOR_REPLICAS = 2
COLLECTOR_RESOURCES = {
    "requests": {"cpu": "20m", "memory": "64Mi"},
    "limits": {"cpu": "500m", "memory": "256Mi"},
}
REDIS_PORT = 6379
REDIS_PASSWORD_KEY = "password"
INTERNAL_UPSTREAMS = "llm-internal-upstreams"
APP_LABEL = "app.kubernetes.io/name"
# 그 파드가 게이트웨이 경로인 Agent 의 것이다. 게이트웨이 정책과 거두기 점검이 읽는다 (P38 6절)
LLM_ROUTE_LABEL = "daegam.devops.ai.kr/llm-route"
# 시작할 때만 읽는 설정(`config` 절)의 판. 바뀌면 파드가 다시 뜬다 (4절)
CONFIG_REV_ANNOTATION = "daegam.devops.ai.kr/config-rev"
TENANT_LABEL = "daegam.devops.ai.kr/tenant"
GATEWAY_REPLICAS = 2
GATEWAY_UID = 65532
RATELIMIT_UID = 65532
REDIS_UID = 999
# 게이트웨이 셋의 자원 (D54). 요청의 합은 F0 12번의 상한(200m, 384Mi) 안이고, 게이트웨이의 메모리
# 한도는 11 MiB 요청 넷이 한꺼번에 와도 견딘 값이다
GATEWAY_RESOURCES = {
    "requests": {"cpu": "50m", "memory": "64Mi"},
    "limits": {"cpu": "1000m", "memory": "512Mi"},
}
SMALL_RESOURCES = {
    "requests": {"cpu": "20m", "memory": "32Mi"},
    "limits": {"cpu": "500m", "memory": "128Mi"},
}
# 공인 주소로만 나간다. 사설·CGNAT·링크로컬·루프백을 뺀다 (10절)
PRIVATE_V4 = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "127.0.0.0/8",
]
PUBLIC_V6_EXCEPT = ["fc00::/7", "fe80::/10"]


def gateway_labels(resource: str, install_id: str) -> dict[str, str]:
    return {LABEL_RESOURCE: resource, LABEL_PLATFORM_ID: install_id, LABEL_MANAGED_BY: MANAGED_BY}


def _meta(name: str, namespace: str, resource: str, install_id: str) -> dict[str, Any]:
    return {"name": name, "namespace": namespace, "labels": gateway_labels(resource, install_id)}


def render_secret(
    name: str, namespace: str, files: dict[str, str], *, install_id: str
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": _meta(name, namespace, name, install_id),
        "data": encode_secret_data(files),
    }


def _pod_security(uid: int) -> dict[str, Any]:
    return {**FOREIGN_POD_SECURITY, "runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid}


def _container_security() -> dict[str, Any]:
    return {**FOREIGN_CONTAINER_SECURITY, "runAsNonRoot": True, "readOnlyRootFilesystem": True}


def _pod(
    images: LlmGatewayImages, uid: int, containers: list[dict[str, Any]], **spec: Any
) -> dict[str, Any]:
    pod: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "securityContext": _pod_security(uid),
        "containers": containers,
        **spec,
    }
    if images.pull_secret:
        pod["imagePullSecrets"] = [{"name": images.pull_secret}]
    return pod


def _deployment(
    name: str,
    namespace: str,
    install_id: str,
    pod: dict[str, Any],
    *,
    replicas: int = 1,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    """모든 롤링은 `maxUnavailable: 0` 이다. 게이트웨이와 수집기는 레플리카 둘이 함께 내려가지
    않는다."""
    template_meta: dict[str, Any] = {"labels": {APP_LABEL: name}}
    if annotations:
        template_meta["annotations"] = annotations
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(name, namespace, name, install_id),
        "spec": {
            "replicas": replicas,
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0}},
            "selector": {"matchLabels": {APP_LABEL: name}},
            "template": {"metadata": template_meta, "spec": pod},
        },
    }


def render_service(name: str, namespace: str, port: int, *, install_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(name, namespace, name, install_id),
        "spec": {
            "type": "ClusterIP",
            "selector": {APP_LABEL: name},
            "ports": [{"name": "tcp", "port": port, "targetPort": port}],
        },
    }


def render_gateway(
    images: LlmGatewayImages, *, namespace: str, install_id: str, config_rev: str
) -> dict[str, Any]:
    """게이트웨이 Deployment. 설정 Secret 하나를 파일로 붙여 설정과 키가 함께 바뀐다. 지워지는
    파드는 preStop 동안 열린 연결의 요청을 끝내고, SIGTERM 은 그 뒤에 온다 (10절)."""
    container = {
        "name": "agentgateway",
        "image": images.ref(images.gateway_image),
        "imagePullPolicy": images.pull_policy,
        "args": ["-f", f"{CONFIG_DIR}/{CONFIG_FILE}"],
        "ports": [
            {"name": "http", "containerPort": GATEWAY_PORT},
            {"name": "rev", "containerPort": REV_PORT},
        ],
        "readinessProbe": {"httpGet": {"path": "/healthz/ready", "port": READY_PORT}},
        "lifecycle": {"preStop": {"sleep": {"seconds": PRESTOP_SECONDS}}},
        "resources": GATEWAY_RESOURCES,
        "securityContext": _container_security(),
        "volumeMounts": [{"name": "config", "mountPath": CONFIG_DIR, "readOnly": True}],
    }
    anti_affinity = {
        "podAntiAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "podAffinityTerm": {
                        "topologyKey": "kubernetes.io/hostname",
                        "labelSelector": {"matchLabels": {APP_LABEL: GATEWAY}},
                    },
                }
            ]
        }
    }
    pod = _pod(
        images,
        GATEWAY_UID,
        [container],
        terminationGracePeriodSeconds=TERMINATION_GRACE_SECONDS,
        affinity=anti_affinity,
        volumes=[{"name": "config", "secret": {"secretName": CONFIG_SECRET}}],
    )
    return _deployment(
        GATEWAY,
        namespace,
        install_id,
        pod,
        replicas=GATEWAY_REPLICAS,
        annotations={CONFIG_REV_ANNOTATION: config_rev},
    )


def render_pdb(namespace: str, *, install_id: str) -> dict[str, Any]:
    """LLM_GATEWAY_PDB=true 일 때만이다. 노드가 하나인 클러스터는 drain 이 멈춘다 (D54)."""
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": _meta(GATEWAY, namespace, GATEWAY, install_id),
        "spec": {"minAvailable": 1, "selector": {"matchLabels": {APP_LABEL: GATEWAY}}},
    }


def ratelimit_config(limits: LlmGatewayLimits, slugs: dict[str, str]) -> str:
    """한도 서비스의 설정 파일. 테넌트 기본값, 테넌트마다의 값, provider 마다의 값이다. slugs 는
    provider 이름 → slug 이고 렌더하지 않은 provider 의 한도는 뺀다 (7절)."""

    def entry(key: str, n: int, value: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"key": key}
        if value is not None:
            out["value"] = value
        out["rate_limit"] = {"unit": "minute", "requests_per_unit": n}
        return out

    descriptors = []
    if limits.tenant_tokens_per_minute is not None:
        descriptors.append(entry("tenant", limits.tenant_tokens_per_minute))
    descriptors += [entry("tenant", n, t) for t, n in sorted(limits.tenants.items())]
    descriptors += [
        entry("provider", n, slugs[p]) for p, n in sorted(limits.providers.items()) if p in slugs
    ]
    return yaml.safe_dump({"domain": RATELIMIT_DOMAIN, "descriptors": descriptors}, sort_keys=False)


def render_ratelimit(
    images: LlmGatewayImages, *, namespace: str, install_id: str, config: str
) -> list[dict[str, Any]]:
    """ConfigMap, Deployment, Service. ConfigMap 의 symlink 가 바뀌면 디렉터리를 다시 읽는다
    (`RUNTIME_WATCH_ROOT=false`, F0 8번)."""
    env = {
        "USE_STATSD": "false",
        "LOG_LEVEL": "info",
        "REDIS_SOCKET_TYPE": "tcp",
        "REDIS_URL": f"{REDIS}:{REDIS_PORT}",
        "RUNTIME_ROOT": "/data",
        "RUNTIME_SUBDIRECTORY": "ratelimit",
        "RUNTIME_WATCH_ROOT": "false",
        "RUNTIME_IGNOREDOTFILES": "true",
        "GRPC_PORT": str(RATELIMIT_PORT),
        "PORT": "8080",
    }
    container = {
        "name": "ratelimit",
        "image": images.ref(images.ratelimit_image),
        "imagePullPolicy": images.pull_policy,
        "command": ["/bin/ratelimit"],
        "env": [{"name": k, "value": v} for k, v in env.items()]
        + [_redis_password_env("REDIS_AUTH")],
        "ports": [{"name": "grpc", "containerPort": RATELIMIT_PORT}],
        "readinessProbe": {"httpGet": {"path": "/healthcheck", "port": 8080}},
        "resources": SMALL_RESOURCES,
        "securityContext": _container_security(),
        "volumeMounts": [{"name": "config", "mountPath": "/data/ratelimit/config"}],
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(RATELIMIT, namespace, RATELIMIT, install_id),
        "data": {"config.yaml": config},
    }
    pod = _pod(
        images,
        RATELIMIT_UID,
        [container],
        volumes=[{"name": "config", "configMap": {"name": RATELIMIT}}],
    )
    return [
        configmap,
        _deployment(RATELIMIT, namespace, install_id, pod),
        render_service(RATELIMIT, namespace, RATELIMIT_PORT, install_id=install_id),
    ]


def _redis_password_env(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": REDIS, "key": REDIS_PASSWORD_KEY}},
    }


def render_redis(
    images: LlmGatewayImages, *, namespace: str, install_id: str
) -> list[dict[str, Any]]:
    """Deployment, Service. PVC 가 없고 계수기는 다시 뜨면 0 에서 시작한다. 비밀번호 Secret 은
    동기화가 없을 때만 만든다."""
    container = {
        "name": "redis",
        "image": images.ref(images.redis_image),
        "imagePullPolicy": images.pull_policy,
        "command": [
            "sh",
            "-c",
            "exec redis-server --requirepass \"$REDIS_PASSWORD\" --save '' --appendonly no",
        ],
        "env": [_redis_password_env("REDIS_PASSWORD")],
        "ports": [{"name": "redis", "containerPort": REDIS_PORT}],
        "readinessProbe": {"tcpSocket": {"port": REDIS_PORT}},
        "resources": SMALL_RESOURCES,
        "securityContext": _container_security(),
        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
    }
    pod = _pod(images, REDIS_UID, [container], volumes=[{"name": "data", "emptyDir": {}}])
    return [
        _deployment(REDIS, namespace, install_id, pod),
        render_service(REDIS, namespace, REDIS_PORT, install_id=install_id),
    ]


# -- NetworkPolicy (10절) -------------------------------------------------------------------


def _pods(name: str) -> dict[str, Any]:
    return {"podSelector": {"matchLabels": {APP_LABEL: name}}}


def _namespace(name: str) -> dict[str, Any]:
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": name}}}


def _tcp(*ports: int) -> list[dict[str, Any]]:
    return [{"protocol": "TCP", "port": p} for p in ports]


def _policy(
    name: str,
    namespace: str,
    install_id: str,
    *,
    ingress: list[dict[str, Any]] | None,
    egress: list[dict[str, Any]],
    pod: str | None = None,
) -> dict[str, Any]:
    """ingress 가 None 이면 나가는 길만 정하는 정책이다. 받는 길은 그 파드의 다른 정책이 정한다."""
    spec: dict[str, Any] = {
        "podSelector": {"matchLabels": {APP_LABEL: pod or name}},
        "policyTypes": ["Egress"] if ingress is None else ["Ingress", "Egress"],
        "egress": egress,
    }
    if ingress is not None:
        spec["ingress"] = ingress
    return {
        "apiVersion": NETWORK_POLICY_API,
        "kind": "NetworkPolicy",
        "metadata": _meta(name, namespace, "llm-gateway-policy", install_id),
        "spec": spec,
    }


def public_egress(port: int) -> dict[str, Any]:
    """공인 주소의 port 로만 나가는 규칙이다. DNS 가 사설 주소를 돌려줘도(DNS rebinding) 막힌다."""
    return {
        "to": [
            {"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_V4)}},
            {"ipBlock": {"cidr": GLOBAL_V6_BLOCK, "except": list(PUBLIC_V6_EXCEPT)}},
        ],
        "ports": _tcp(port),
    }


def render_policies(
    *,
    namespace: str,
    platform_namespace: str,
    install_id: str,
    gateway_egress: list[dict[str, Any]],
    internal_upstreams: list[tuple[str, int]],
) -> list[dict[str, Any]]:
    """파드마다 하나와 클러스터 안 provider 의 `llm-internal-upstreams` 다. 게이트웨이는 테넌트
    라벨이 있는 네임스페이스의 게이트웨이 경로 파드와 플랫폼 네임스페이스에서만 받는다. 15080 은
    플랫폼만 읽는다. 관리 페이지(15000)는 루프백이고 지표(15020)와 준비(15021)는 어떤 정책도 열지
    않는다. gateway_egress 는 수집기(8절)처럼 게이트웨이가 더 나가는 곳이다."""
    agents = {
        "namespaceSelector": {"matchExpressions": [{"key": TENANT_LABEL, "operator": "Exists"}]},
        "podSelector": {"matchLabels": {LLM_ROUTE_LABEL: "gateway"}},
    }
    platform = _namespace(platform_namespace)
    gateway = _policy(
        GATEWAY,
        namespace,
        install_id,
        ingress=[
            {"from": [agents, platform], "ports": _tcp(GATEWAY_PORT)},
            {"from": [platform], "ports": _tcp(REV_PORT)},
        ],
        egress=[
            dns_egress(),
            {"to": [_pods(RATELIMIT)], "ports": _tcp(RATELIMIT_PORT)},
            *gateway_egress,
            public_egress(443),
        ],
    )
    ratelimit = _policy(
        RATELIMIT,
        namespace,
        install_id,
        ingress=[{"from": [_pods(GATEWAY)], "ports": _tcp(RATELIMIT_PORT)}],
        egress=[dns_egress(), {"to": [_pods(REDIS)], "ports": _tcp(REDIS_PORT)}],
    )
    redis = _policy(
        REDIS,
        namespace,
        install_id,
        ingress=[{"from": [_pods(RATELIMIT)], "ports": _tcp(REDIS_PORT)}],
        egress=[],
    )
    docs = [gateway, ratelimit, redis]
    if internal_upstreams:
        docs.append(
            _policy(
                INTERNAL_UPSTREAMS,
                namespace,
                install_id,
                pod=GATEWAY,
                ingress=None,
                egress=[
                    {"to": [_namespace(ns)], "ports": _tcp(port)}
                    for ns, port in sorted(set(internal_upstreams))
                ],
            )
        )
    return docs


# -- 수집기 (8절) ---------------------------------------------------------------------------


def collector_config(ingest_url: str) -> str:
    """수집기의 설정 파일. 게이트웨이의 OTLP(protobuf, gRPC)를 받아 플랫폼 수집 경로에 OTLP/HTTP
    JSON 으로 보낸다. `gen_ai.operation.name` 이 없는 span(JWT 없는 요청, `/_daegam/rev`, 위로 가는
    자식 span)은 버린다. 토큰은 bearertokenauth 가 파일에서 다시 읽으므로 바꿔도 수집기가 구르지
    않는다. 플랫폼이 구르는 동안은 sending_queue 와 retry_on_failure 가 버틴다."""
    config = {
        "extensions": {
            "bearertokenauth": {"filename": f"/token/{COLLECTOR_TOKEN_KEY}"},
            "health_check": {"endpoint": f"0.0.0.0:{COLLECTOR_HEALTH_PORT}"},
        },
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": f"0.0.0.0:{OTLP_GRPC_PORT}"}}}},
        "processors": {
            "filter/usage": {
                "error_mode": "ignore",
                "traces": {"span": ['attributes["gen_ai.operation.name"] == nil']},
            },
            "batch": {"timeout": "2s"},
        },
        "exporters": {
            "otlp_http": {
                "traces_endpoint": ingest_url,
                "encoding": "json",
                "compression": "none",
                "auth": {"authenticator": "bearertokenauth"},
                "sending_queue": {"enabled": True, "queue_size": 5000},
                "retry_on_failure": {
                    "enabled": True,
                    "initial_interval": "1s",
                    "max_interval": "30s",
                    "max_elapsed_time": "900s",
                },
            }
        },
        "service": {
            "extensions": ["bearertokenauth", "health_check"],
            "pipelines": {
                "traces": {
                    "receivers": ["otlp"],
                    "processors": ["filter/usage", "batch"],
                    "exporters": ["otlp_http"],
                }
            },
        },
    }
    return yaml.safe_dump(config, sort_keys=False)


def render_collector(
    images: LlmGatewayImages, *, namespace: str, install_id: str, config: str
) -> list[dict[str, Any]]:
    """ConfigMap, Deployment(2 레플리카), Service. 수집 토큰 Secret 은 수집기에만 붙는다."""
    container = {
        "name": "otel",
        "image": images.ref(images.collector_image),
        "imagePullPolicy": images.pull_policy,
        "args": ["--config=/conf/config.yaml"],
        "ports": [{"name": "otlp-grpc", "containerPort": OTLP_GRPC_PORT}],
        "readinessProbe": {"httpGet": {"path": "/", "port": COLLECTOR_HEALTH_PORT}},
        "resources": COLLECTOR_RESOURCES,
        "securityContext": _container_security(),
        "volumeMounts": [
            {"name": "conf", "mountPath": "/conf", "readOnly": True},
            {"name": "token", "mountPath": "/token", "readOnly": True},
        ],
    }
    pod = _pod(
        images,
        COLLECTOR_UID,
        [container],
        volumes=[
            {"name": "conf", "configMap": {"name": COLLECTOR}},
            {"name": "token", "secret": {"secretName": COLLECTOR_TOKEN_SECRET}},
        ],
    )
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(COLLECTOR, namespace, COLLECTOR, install_id),
        "data": {"config.yaml": config},
    }
    return [
        configmap,
        _deployment(COLLECTOR, namespace, install_id, pod, replicas=COLLECTOR_REPLICAS),
        render_service(COLLECTOR, namespace, OTLP_GRPC_PORT, install_id=install_id),
    ]


def collector_policy(
    *, namespace: str, install_id: str, ingest_namespace: str, ingest_port: int
) -> dict[str, Any]:
    """수집기는 게이트웨이의 4317 만 받고 DNS 와 수집 주소의 네임스페이스·포트로만 나간다."""
    return _policy(
        COLLECTOR,
        namespace,
        install_id,
        ingress=[{"from": [_pods(GATEWAY)], "ports": _tcp(OTLP_GRPC_PORT)}],
        egress=[dns_egress(), {"to": [_namespace(ingest_namespace)], "ports": _tcp(ingest_port)}],
    )


def collector_egress() -> dict[str, Any]:
    """게이트웨이가 수집기로 나가는 길."""
    return {"to": [_pods(COLLECTOR)], "ports": _tcp(OTLP_GRPC_PORT)}
