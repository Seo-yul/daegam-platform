"""플랫폼 `web_fetch` 복제본의 렌더 (P39b 10·11·13절).

Gateway 가 사는 네임스페이스마다 하나(`platform-web-fetch`)이고 그 네임스페이스의 Gateway 들이 함께
쓴다. 문서는 [NetworkPolicy, MCPServer] 순이다.

- MCPServer 는 우리 이미지 `daegam-web-tools` 를 uid 1000·루트 읽기 전용으로 돌리고 ServiceAccount
  토큰을 싣지 않는다. 비밀을 하나도 들지 않는다. 설정은 `spec.env` 이고 관리자가 준 CA 묶음도 env
  `WEB_FETCH_CA_B64`(PEM 의 base64) 다. 공개 인증서라 Secret 이 필요 없고, 플랫폼은 테넌트
  네임스페이스에 ConfigMap 을 쓸 권한이 없다. ToolHive 는 줄바꿈이 든 env 값을 거절하므로 base64 로
  싣는다. env 와 CA 의 해시(`daegam.devops.ai.kr/web-config`)가 바뀌면 파드가 다시 뜬다.
- 들어오는 토큰은 fetch audience(`daegam-internal:<tenant>:web-fetch`)만 받는다. 다른 내부 MCPServer
  는 그 audience 를 거절하므로 프록시가 토큰을 컨테이너로 넘겨도 파서가 쥔 토큰으로 테넌트의 다른
  서버를 부를 수 없다.
- NetworkPolicy `daegam-web-fetch` 는 라벨 `daegam.devops.ai.kr/web-tool: fetch` 파드의 나가는
  길만 좁힌다. kube-dns 의 53 과 허용 포트로 가는 공인 주소뿐이다. IPv4 는 `0.0.0.0/0` 에서, IPv6 는
  `2000::/3` 에서 urlfetch 의 차단 표와 관리자가 막은 CIDR 을 뺀다. 첫 번째 방어는 주소 검사이고
  이것은 두 번째다 (D38).
"""

from __future__ import annotations

import ipaddress
from typing import Any

from daegam.auth.signer import fetch_audience
from daegam.config.resolve import EffectiveConfig
from daegam.k8s.client import TOOLHIVE_API
from daegam.k8s.labels import stable_hash
from daegam.k8s.templates.common import (
    CONTAINER_SECURITY,
    NETWORK_POLICY_API,
    POD_SECURITY,
    PROXY_PORT,
    ca_env,
    dns_egress,
    hub_labels,
    internal_oidc_config_ref,
    mark_mcp_pods,
    pod_placement,
    proxy_resources,
    proxy_service_url,
)
from daegam.store.hub import PLATFORM_KIND
from daegam.store.settings import WebFetchSettings

FETCH_LABEL = "daegam.devops.ai.kr/web-tool"
FETCH_LABEL_VALUE = "fetch"
WEB_CONFIG_ANNOTATION = "daegam.devops.ai.kr/web-config"
FETCH_POLICY_NAME = "daegam-web-fetch"
CA_ENV = "WEB_FETCH_CA_B64"
MCP_PORT = 8000
FETCH_RESOURCES = {
    "requests": {"cpu": "20m", "memory": "64Mi"},
    "limits": {"cpu": "500m", "memory": "384Mi"},
}


def fetch_env(fetch: WebFetchSettings) -> list[dict[str, str]]:
    """`webtools/settings.py` 가 읽는 env. 순서가 고정이라 해시가 같은 값에 같다."""
    values = {
        "WEB_FETCH_MAX_BYTES": fetch.max_bytes,
        "WEB_FETCH_MAX_CHARS": fetch.max_chars,
        "WEB_FETCH_TIMEOUT_SECONDS": fetch.timeout_seconds,
        "WEB_FETCH_MAX_REDIRECTS": fetch.max_redirects,
        "WEB_FETCH_ALLOWED_PORTS": ",".join(str(p) for p in fetch.allowed_ports),
        "WEB_FETCH_BLOCKED_SUFFIXES": ",".join(fetch.blocked_host_suffixes),
        "WEB_FETCH_BLOCKED_CIDRS": ",".join(fetch.blocked_cidrs),
        "WEB_FETCH_RESPECT_ROBOTS": "true" if fetch.respect_robots else "false",
        "WEB_FETCH_USER_AGENT_CONTACT": fetch.user_agent_contact or "",
        "WEB_FETCH_PER_MINUTE": fetch.per_minute,
    }
    return [{"name": k, "value": str(v)} for k, v in values.items()]


def web_config_hash(fetch: WebFetchSettings, ca_bundle: str | None, image: str) -> str:
    """복제본이 렌더하는 웹 Tool 설정의 판. 바뀌면 파드가 다시 뜨고, 복제본 기록의 판 첫 절반에도
    든다(`SharedClones.wanted_hash`)."""
    return stable_hash({"env": fetch_env(fetch), "ca": ca_bundle or "", "image": image}, 16)


# IPv6 의 나가는 길은 전역 유니캐스트 안에서만 연다. urlfetch 의 `GLOBAL_V6` 와 같은 값이다
GLOBAL_V6_BLOCK = "2000::/3"


def egress_excepts(blocked: list[str]) -> tuple[list[str], list[str]]:
    """막는 CIDR 을 IPv4 와 IPv6 로 나눈다. NetworkPolicy 의 `except` 는 그 블록의 `cidr` 안에서만
    받는다."""
    nets = [ipaddress.ip_network(n) for n in blocked]
    return [str(n) for n in nets if n.version == 4], [str(n) for n in nets if n.version == 6]


def fetch_network_policy(
    *, namespace: str, labels: dict[str, str], fetch: WebFetchSettings, blocked: list[str]
) -> dict[str, Any]:
    """blocked 는 urlfetch 의 차단 표와 관리자가 막은 CIDR 을 합친 것이다(부르는 쪽이 만든다)."""
    ports = [{"protocol": "TCP", "port": p} for p in fetch.allowed_ports]
    v4_except, v6_except = egress_excepts(blocked)
    web = {
        "to": [
            {"ipBlock": {"cidr": "0.0.0.0/0", "except": v4_except}},
            {"ipBlock": {"cidr": GLOBAL_V6_BLOCK, "except": v6_except}},
        ],
        "ports": ports,
    }
    return {
        "apiVersion": NETWORK_POLICY_API,
        "kind": "NetworkPolicy",
        "metadata": {"name": FETCH_POLICY_NAME, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "podSelector": {"matchLabels": {FETCH_LABEL: FETCH_LABEL_VALUE}},
            "policyTypes": ["Egress"],
            "egress": [dns_egress(), web],
        },
    }


def render_fetch_clone(
    *,
    tenant: str,
    item_id: str,
    name: str,
    namespace: str,
    group: str,
    image: str,
    fetch: WebFetchSettings,
    ca_bundle: str | None,
    effective: EffectiveConfig | None,
    blocked: list[str],
) -> list[dict[str, Any]]:
    labels = hub_labels(tenant, item_id, PLATFORM_KIND)
    docs: list[dict[str, Any]] = [
        fetch_network_policy(namespace=namespace, labels=labels, fetch=fetch, blocked=blocked)
    ]
    mcp: dict[str, Any] = {
        "name": "mcp",
        "securityContext": CONTAINER_SECURITY,
        "resources": {k: dict(v) for k, v in FETCH_RESOURCES.items()},
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
    }
    pod: dict[str, Any] = {
        "securityContext": POD_SECURITY,
        "automountServiceAccountToken": False,
        "containers": [mcp],
        "volumes": [{"name": "tmp", "emptyDir": {}}],
    }
    env = fetch_env(fetch)
    if ca_bundle:
        env.append(ca_env(CA_ENV, ca_bundle))
    if effective is not None:
        pod.update(pod_placement(effective))
        mcp["imagePullPolicy"] = effective.images.pull_policy
    spec: dict[str, Any] = {
        "image": image,
        "transport": "streamable-http",
        "mcpPort": MCP_PORT,
        "proxyPort": PROXY_PORT,
        "groupRef": {"name": group},
        "replicas": 1,
        "audit": {"enabled": True},
        "oidcConfigRef": internal_oidc_config_ref(
            tenant, proxy_service_url(name, namespace), audience=fetch_audience(tenant)
        ),
        "env": env,
        "resources": proxy_resources(),
        "podTemplateSpec": {
            "metadata": {
                "labels": {FETCH_LABEL: FETCH_LABEL_VALUE},
                "annotations": {WEB_CONFIG_ANNOTATION: web_config_hash(fetch, ca_bundle, image)},
            },
            "spec": pod,
        },
    }
    mark_mcp_pods(spec, name)
    docs.append(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPServer",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": spec,
        }
    )
    return docs
