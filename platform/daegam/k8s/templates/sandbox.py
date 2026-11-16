"""코드 실행 샌드박스의 렌더 (P40 2절·4절, D95).

- 파드: 세션마다 Agent 네임스페이스의 맨 파드 하나다. `daegam-sandbox` 이미지의 서버가 PID
  1 이고(`sandbox/`), 파드 env 는 토큰의 sha256 과 자체 점검 대상뿐이다. user namespace
  (`hostUsers: false`), ServiceAccount 토큰·서비스 링크·DNS 없음, 메모리 emptyDir 두 개
  (`/workspace`, `/tmp`), 메모리 요청과 한도가 같다. 배치는 클러스터의 `sandbox.scheduling` 이
  있으면 그것이고 없으면 일반 배치다. 그 뒤에 `priorityClassName: daegam-sandbox` 를 두고, 연
  자리가 슬롯을 잡은 노드 하나에 hostname 으로 묶는다(7절). Advanced 편집은 이 파드에 닿지 않는다.
- 라벨: `resource: sandbox`, `id: <샌드박스 id>`, `agent: <Agent id>`, 테넌트, 설치 id 다. Agent 를
  고르는 어느 셀렉터도(Deployment, Service, Agent 정책, 테넌트 정책, 감사 수집, 인스턴스 상태,
  토큰 정리, port-forward) 이 라벨에 맞지 않는다.
- 정책 셋(4절): 테넌트 ingress 정책은 샌드박스를 뺀다(`services/tenancy.py`
  `network_policy_manifest`). 격리 정책 `daegam-sandbox-isolation` 은 모든 샌드박스를 골라 규칙 없이
  ingress·egress 를 닫고 테넌트 id 만 싣는다. Agent 의 정책 `daegam-sandbox-<Agent id>` 는 그 Agent
  파드에서 8080 으로 오는 것만 연다.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any

from kubernetes.utils import parse_quantity

from daegam.config.resolve import image_digest, with_registry
from daegam.k8s.labels import (
    LABEL_AGENT,
    LABEL_PLATFORM_ID,
    LABEL_RESOURCE,
    daegam_labels,
    stable_hash,
)
from daegam.k8s.templates.agent import agent_owner_ref, selector_labels
from daegam.k8s.templates.common import CONTAINER_SECURITY, NETWORK_POLICY_API, POD_SECURITY
from daegam.store.agents import Agent
from daegam.store.clusters import Cluster

SANDBOX_RESOURCE = "sandbox"
SANDBOX_CONTAINER = "sandbox"
SANDBOX_PORT = 8080
PRIORITY_CLASS = "daegam-sandbox"
ISOLATION_POLICY_NAME = "daegam-sandbox-isolation"
RESOURCE_ISOLATION_POLICY = "tenant-sandbox-isolation"
RESOURCE_AGENT_SANDBOX_POLICY = "agent-sandbox-policy"
# 격리 점검의 양성 대조와 진짜 샌드박스가 도는 네임스페이스 (8절). 매니페스트가 만들고 테넌트는
# 이 이름을 자기 네임스페이스로 삼지 못한다(`services/tenancy.py` `RESERVED_NAMESPACES`)
PROBE_NAMESPACE = "daegam-probe"
# 클러스터의 `sandbox.image` 가 비었을 때의 이미지. 레지스트리 접두어는 클러스터의 것이 붙는다
DEFAULT_SANDBOX_IMAGE = "daegam-sandbox:0.1.0"
HOSTNAME_LABEL = "kubernetes.io/hostname"

MIB = 1024**2
GIB = 1024**3
# 작업 공간의 기본 크기(DAEGAM_SANDBOX_WORKSPACE)와 /tmp. 둘 다 메모리 emptyDir 이라 크기가 단단한
# 한도다
WORKSPACE_BYTES = GIB
TMP_BYTES = 128 * MIB
# 작업 공간 밖의 메모리. 메모리 요청과 한도는 작업 공간에 이것을 더한 값이다(2 GiB)
HEADROOM_BYTES = GIB
CPU_REQUEST = "10m"
CPU_LIMIT = "1"
# 하루가 지나면 kubelet 이 끝낸다. 회수와 스윕이 먼저 지우지 못했을 때의 마지막 받침이다
ACTIVE_DEADLINE_SECONDS = 86400

# 서버가 읽는 파드 env (`sandbox/` 의 계약). 토큰 자체는 싣지 않는다
TOKEN_SHA_ENV = "SANDBOX_TOKEN_SHA256"
TARGETS_ENV = "SANDBOX_TARGETS"
UNCHECKED_ENV = "SANDBOX_UNCHECKED"
TARGET_KINDS = frozenset({"tcp", "dns", "icmp"})
# 자체 점검 대상 하나의 시한(초). 양성 대조가 쓴 값이고 서버가 더 길게 받지 않는다
TARGET_TIMEOUT_MAX = 10.0
# 서버가 받는 대상 수의 위(`sandbox/daegam_sandbox/config.py` `MAX_TARGETS`)
TARGETS_MAX = 512
NAME_HASH_LENGTH = 20
PLACEMENT_FINGERPRINT_LENGTH = 16
# 사용자 코드 없는 프로브(8절)의 표시 라벨. 스윕은 이 라벨로 프로브를 기록 없는 샌드박스와 가른다
LABEL_PROBE = "daegam.devops.ai.kr/sandbox-probe"
# 클러스터 점검의 양성 대조와 클라이언트 파드의 resource 라벨. 샌드박스 라벨이 아니라 격리 정책에
# 들지 않는다
CHECK_HELPER_RESOURCE = "sandbox-check"
# 프로브·양성 대조·클라이언트 파드의 메모리 요청이자 한도와, kubelet 이 끝내는 시한(초)
SMALL_POD_BYTES = 128 * MIB
SMALL_POD_DEADLINE_SECONDS = 600


def sandbox_selector() -> dict[str, str]:
    """모든 샌드박스 파드를 고르는 라벨. 격리 정책과 스윕이 쓴다."""
    return {LABEL_RESOURCE: SANDBOX_RESOURCE}


def sandbox_labels(
    *, tenant: str, agent_id: str, sandbox_id: str, platform_id: str
) -> dict[str, str]:
    """샌드박스 파드의 라벨. 설치 id 는 이 렌더가 직접 붙인다(`daegam_labels` 에는 없다). 스윕이
    이 설치의 것만 고르는 라벨이다."""
    labels = daegam_labels(tenant=tenant, resource=SANDBOX_RESOURCE, id=sandbox_id)
    labels[LABEL_AGENT] = agent_id
    labels[LABEL_PLATFORM_ID] = platform_id
    return labels


def sandbox_pod_name(agent_id: str, session_id: str, generation: int) -> str:
    """`sbx-<hash(Agent, 세션, 세대)>`. 같은 세대의 열기는 같은 이름이라 두 번 만들지 않는다."""
    digest = hashlib.sha256(f"{agent_id}\0{session_id}\0{generation}".encode()).hexdigest()
    return f"sbx-{digest[:NAME_HASH_LENGTH]}"


def sandbox_image(cluster: Cluster) -> str:
    """클러스터의 `sandbox.image`(home 은 배포가 digest 로 채운다), 없으면 플랫폼 기본값.
    레지스트리가 없는 참조에는 클러스터의 `images.registry_prefix` 가 붙는다."""
    image = cluster.sandbox.image or DEFAULT_SANDBOX_IMAGE
    return with_registry(image, cluster.images.registry_prefix)


def sandbox_placement(cluster: Cluster) -> dict[str, Any]:
    """샌드박스의 노드 셀렉터와 toleration. `sandbox.scheduling` 에 하나라도 있으면 둘 다 그것을
    쓰고, 없으면 클러스터의 일반 배치를 쓴다. 노드 고르기(7절)와 배치의 지문이 같은 값을 읽는다."""
    own = cluster.sandbox.scheduling
    source: Any = own if own.is_set else cluster.scheduling
    return {
        "node_selector": dict(source.node_selector or {}),
        "tolerations": [dict(t) for t in source.tolerations or []],
    }


def placement_fingerprint(cluster: Cluster) -> str:
    """점검이 판정에 적고 읽을 때 맞대는 배치의 지문 (8절). 배치가 바뀌면 판정이 무효로 읽힌다."""
    return stable_hash(sandbox_placement(cluster), PLACEMENT_FINGERPRINT_LENGTH)


def verdict_void_reason(cluster: Cluster, *, image_id: str | None = None) -> str | None:
    """판정이 무효로 읽히는 까닭 (8절). 읽을 때마다 정하므로 쓰기가 필요 없고, 옛 레플리카나
    되돌린 판이 바꾼 것도 놓치지 않는다. 무효가 아니면 None 이다. 통과했는지는 따로 본다.

    - `voided`: 87 이나 프로브 실패가 적은 무효
    - `placement`: 점검 뒤 샌드박스 배치(`sandbox.scheduling`, 없으면 일반 배치)가 바뀌었다
    - `image`: 실효 이미지 참조가 바뀌었다(사람의 저장, home 의 env). 또는 열린 파드가 알린
      imageID(image_id 인자나 판정에 적힌 `seen_image_id`)가 점검이 돌린 이미지 id 와 digest 가
      다르다. 기본값처럼 digest 없는 태그는 참조가 그대로여도 태그가 옮겨 가므로 이 맞대기가 잡는다
    - `k8s_version`, `policy_controller`: preflight 가 지금 본 값과 다르다
    """
    check = cluster.sandbox.check
    if check.voided_at is not None:
        return "voided"
    if check.checked_at is None:
        return None
    if check.placement != placement_fingerprint(cluster):
        return "placement"
    if check.image != sandbox_image(cluster):
        return "image"
    checked = image_digest(check.image_id)
    for seen_id in (image_id, check.seen_image_id):
        seen = image_digest(seen_id)
        if seen is not None and seen != checked:
            return "image"
    preflight = cluster.status.preflight
    if preflight is not None:
        if check.k8s_version != preflight.k8s_version:
            return "k8s_version"
        if check.policy_controller != preflight.policy_controller:
            return "policy_controller"
    return None


def sandbox_memory_bytes(workspace_bytes: int = WORKSPACE_BYTES) -> int:
    """샌드박스 파드의 메모리 요청이자 한도. 노드 용량(7절)이 이 값으로 나눈다."""
    return workspace_bytes + HEADROOM_BYTES


def sandbox_cpu_request(limit_range: dict[str, Any] | None) -> str:
    """CPU 요청. 10m 이고, 네임스페이스 LimitRange 의 Container 최솟값이 더 크면 그 값이다."""
    floor = parse_quantity(CPU_REQUEST)
    best = CPU_REQUEST
    for item in (limit_range or {}).get("limits") or []:
        if item.get("type") != "Container":
            continue
        minimum = (item.get("min") or {}).get("cpu")
        if minimum is not None and parse_quantity(str(minimum)) > floor:
            floor, best = parse_quantity(str(minimum)), str(minimum)
    return best


def _quantity(n: int) -> str:
    """바이트 수를 Gi·Mi quantity 로 적는다. 나누어떨어지지 않으면 바이트 그대로다."""
    for unit, size in (("Gi", GIB), ("Mi", MIB)):
        if n % size == 0:
            return f"{n // size}{unit}"
    return str(n)


def check_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """자체 점검 대상의 꼴을 서버의 계약대로 확인하고 정렬한 사본을 돌려준다. 호스트는 IP 글자만
    받는다. 샌드박스 안에서는 이름을 풀 수 없기 때문이다. 대상이 없는 점검은 아무것도 증명하지
    못하므로 서버처럼 빈 목록도 거절한다. 틀리면 ValueError 다."""
    if not targets or len(targets) > TARGETS_MAX:
        raise ValueError(f"a sandbox needs 1 to {TARGETS_MAX} self-check targets")
    out: list[dict[str, Any]] = []
    for target in targets:
        kind = target.get("kind")
        if kind not in TARGET_KINDS:
            raise ValueError(f"sandbox target kind must be one of {sorted(TARGET_KINDS)}: {kind!r}")
        host = target.get("host")
        try:
            ipaddress.ip_address(str(host))
        except ValueError:
            raise ValueError(f"sandbox target host must be an IP address: {host!r}") from None
        item: dict[str, Any] = {"kind": kind, "host": str(host)}
        if kind != "icmp":
            port = target.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ValueError(f"sandbox target {kind} {host} needs a port: {port!r}")
            item["port"] = port
        if "timeout" in target:
            timeout = target["timeout"]
            if not isinstance(timeout, int | float) or not 0 < timeout <= TARGET_TIMEOUT_MAX:
                raise ValueError(f"sandbox target timeout must be in (0, 10]: {timeout!r}")
            item["timeout"] = float(timeout)
        out.append(item)
    return out


def render_sandbox_pod(
    *,
    cluster: Cluster,
    agent: Agent,
    owner_uid: str | None,
    session_id: str,
    generation: int,
    sandbox_id: str,
    node: str,
    token_sha256: str,
    targets: list[dict[str, Any]],
    platform_id: str,
    unchecked: bool = False,
    workspace_bytes: int = WORKSPACE_BYTES,
    cpu_request: str = CPU_REQUEST,
) -> dict[str, Any]:
    """세션의 샌드박스 파드 (2절). owner_uid 는 Agent Deployment 의 uid 이고 ownerReference 는
    blockOwnerDeletion 없이 그것을 가리킨다. uid 를 모르면 None 이고 ownerReference 를 두지
    않는다. 없는 uid 를 가리키면 GC 가 파드를 바로 지운다. node 는 열기가 슬롯을 잡은 노드다.
    targets 는 자체 점검 대상(8절)이고 `check_targets` 로 꼴을 본다. unchecked 는 개발용 우회가
    켜졌을 때만 참이다."""
    if agent.k8s is None:
        raise ValueError(f"agent {agent.id} is not deployed")
    if len(token_sha256) != 64 or any(c not in "0123456789abcdef" for c in token_sha256):
        raise ValueError("token_sha256 must be 64 lowercase hex characters")
    if not node:
        raise ValueError("a sandbox pod is pinned to one node")
    placement = sandbox_placement(cluster)
    memory = _quantity(sandbox_memory_bytes(workspace_bytes))
    env = [{"name": TOKEN_SHA_ENV, "value": token_sha256}, _targets_env(targets)]
    if unchecked:
        env.append({"name": UNCHECKED_ENV, "value": "true"})
    container = {
        "name": SANDBOX_CONTAINER,
        "image": sandbox_image(cluster),
        "imagePullPolicy": cluster.images.pull_policy,
        "ports": [{"name": "http", "containerPort": SANDBOX_PORT, "protocol": "TCP"}],
        "env": env,
        "resources": {
            "requests": {"cpu": cpu_request, "memory": memory},
            "limits": {"cpu": CPU_LIMIT, "memory": memory},
        },
        "securityContext": CONTAINER_SECURITY,
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": SANDBOX_PORT},
            "periodSeconds": 1,
            "timeoutSeconds": 1,
            "failureThreshold": 3,
        },
        "volumeMounts": [
            {"name": "workspace", "mountPath": "/workspace"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
    }
    spec: dict[str, Any] = {
        "hostUsers": False,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        # 들을 것이 없는 주소다. 이름 풀기는 실패하고 클러스터 DNS 로 새지 않는다
        "dnsPolicy": "None",
        "dnsConfig": {"nameservers": ["127.0.0.1"]},
        "restartPolicy": "OnFailure",
        "terminationGracePeriodSeconds": 1,
        "activeDeadlineSeconds": ACTIVE_DEADLINE_SECONDS,
        "securityContext": POD_SECURITY,
        "containers": [container],
        "volumes": [
            {
                "name": "workspace",
                "emptyDir": {"medium": "Memory", "sizeLimit": _quantity(workspace_bytes)},
            },
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": _quantity(TMP_BYTES)}},
        ],
    }
    # kubelet 이 이미지를 당길 때만 쓴다. 컨테이너에 붙지 않는다
    if cluster.images.pull_secret:
        spec["imagePullSecrets"] = [{"name": cluster.images.pull_secret}]
    if placement["node_selector"]:
        spec["nodeSelector"] = placement["node_selector"]
    if placement["tolerations"]:
        spec["tolerations"] = placement["tolerations"]
    # 배치 뒤에 둔다. 일반 배치의 priority class 가 무엇이든 샌드박스는 이것이다
    spec["priorityClassName"] = PRIORITY_CLASS
    spec["affinity"] = pin_to_node(node)
    labels = sandbox_labels(
        tenant=agent.tenant, agent_id=agent.id, sandbox_id=sandbox_id, platform_id=platform_id
    )
    metadata: dict[str, Any] = {
        "name": sandbox_pod_name(agent.id, session_id, generation),
        "namespace": agent.k8s.namespace,
        "labels": labels,
    }
    if owner_uid:
        metadata["ownerReferences"] = [agent_owner_ref(agent, owner_uid)]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": spec}


def pin_to_node(node: str) -> dict[str, Any]:
    """노드 하나에 묶는 필수 node affinity (7절). 노드 집합이 아니라 이름 하나다."""
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {"key": HOSTNAME_LABEL, "operator": "In", "values": [node]}
                        ]
                    }
                ]
            }
        }
    }


def _targets_env(targets: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "name": TARGETS_ENV,
        "value": json.dumps(check_targets(targets), sort_keys=True, separators=(",", ":")),
    }


def _small_pod(
    cluster: Cluster,
    *,
    name: str,
    namespace: str,
    labels: dict[str, str],
    args: list[str],
    env: list[dict[str, str]],
    node: str | None,
    host_users: bool,
) -> dict[str, Any]:
    """작업 공간 없는 한 번짜리 파드. 사용자 코드 없는 프로브와 클러스터 점검의 양성
    대조·클라이언트가 쓴다. 요청과 한도가 128 MiB 로 같고 한 번 돌고 끝난다. 샌드박스와 같은
    이미지·보안 문맥이다. 배치는 샌드박스의 것이고, node 를 주면 그 노드 하나에 묶는다."""
    placement = sandbox_placement(cluster)
    memory = _quantity(SMALL_POD_BYTES)
    spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 1,
        "activeDeadlineSeconds": SMALL_POD_DEADLINE_SECONDS,
        "securityContext": POD_SECURITY,
        "containers": [
            {
                "name": SANDBOX_CONTAINER,
                "image": sandbox_image(cluster),
                "imagePullPolicy": cluster.images.pull_policy,
                "args": args,
                "env": env,
                "resources": {
                    "requests": {"cpu": CPU_REQUEST, "memory": memory},
                    "limits": {"cpu": CPU_LIMIT, "memory": memory},
                },
                "securityContext": CONTAINER_SECURITY,
            }
        ],
    }
    if not host_users:
        spec["hostUsers"] = False
        # 들을 것이 없는 주소다. 샌드박스와 같게 이름 풀기가 클러스터 DNS 로 새지 않는다
        spec["dnsPolicy"] = "None"
        spec["dnsConfig"] = {"nameservers": ["127.0.0.1"]}
    if cluster.images.pull_secret:
        spec["imagePullSecrets"] = [{"name": cluster.images.pull_secret}]
    if placement["node_selector"]:
        spec["nodeSelector"] = placement["node_selector"]
    if placement["tolerations"]:
        spec["tolerations"] = placement["tolerations"]
    spec["priorityClassName"] = PRIORITY_CLASS
    if node:
        spec["affinity"] = pin_to_node(node)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": spec,
    }


def probe_pod_name(cluster_id: str, node: str, nonce: str) -> str:
    digest = hashlib.sha256(f"{cluster_id}\0{node}\0{nonce}".encode()).hexdigest()
    return f"sbx-probe-{digest[:NAME_HASH_LENGTH]}"


def render_probe_pod(
    *,
    cluster: Cluster,
    name: str,
    namespace: str,
    node: str | None,
    targets: list[dict[str, Any]],
    tenant: str,
    platform_id: str,
) -> dict[str, Any]:
    """사용자 코드 없는 프로브 (8절). 88 로 끝난 샌드박스가 있던 네임스페이스와 노드에서 첫
    자체 점검만 돈다(`probe` 모드: 통과 0, 실패 87, 돌지 못함 86). 샌드박스 라벨을 달아 격리
    정책을 그대로 받고, Agent 라벨이 없어 어느 Agent 정책에도 들지 않는다. 스윕은 프로브 라벨로
    이 파드를 가른다."""
    labels = daegam_labels(tenant=tenant, resource=SANDBOX_RESOURCE, id=name)
    labels[LABEL_PLATFORM_ID] = platform_id
    labels[LABEL_PROBE] = "true"
    return _small_pod(
        cluster,
        name=name,
        namespace=namespace,
        labels=labels,
        args=["probe"],
        env=[_targets_env(targets)],
        node=node,
        host_users=False,
    )


def render_check_helper_pod(
    *,
    cluster: Cluster,
    name: str,
    args: list[str],
    targets: list[dict[str, Any]] | None,
    platform_id: str,
    node: str | None = None,
) -> dict[str, Any]:
    """클러스터 점검의 양성 대조(`control`)와 클라이언트(`client <IP> 8080`) (8절 1·3단계).
    `daegam-probe` 에 서고 샌드박스 라벨이 없어 어느 egress 정책에도 들지 않는다. 양성 대조는
    샌드박스와 같은 대상을 같은 시한으로 재고, 클라이언트는 Agent 가 아닌 파드에서 8080 을
    두드린다."""
    labels = daegam_labels(tenant=PROBE_NAMESPACE, resource=CHECK_HELPER_RESOURCE, id=name)
    labels[LABEL_PLATFORM_ID] = platform_id
    env = [_targets_env(targets)] if targets is not None else []
    return _small_pod(
        cluster,
        name=name,
        namespace=PROBE_NAMESPACE,
        labels=labels,
        args=args,
        env=env,
        node=node,
        host_users=True,
    )


def host_users_kept(pod: dict[str, Any]) -> bool:
    """만든 파드를 다시 읽은 값에 `hostUsers: false` 가 남았는가 (2절). user namespace 를 모르는
    API 서버는 그 칸을 조용히 버리므로, 거짓이면 열기가 그 파드를 지운다."""
    return ((pod.get("spec") or {}).get("hostUsers")) is False


def render_isolation_policy(tenant: str, namespace: str) -> dict[str, Any]:
    """`daegam-sandbox-isolation` (4절). 모든 샌드박스를 골라 규칙 없이 ingress 와 egress 를 닫는다.
    테넌트 id 만 싣고 Agent id 는 싣지 않으므로 Agent 를 내릴 때 지워지지 않는다."""
    return {
        "apiVersion": NETWORK_POLICY_API,
        "kind": "NetworkPolicy",
        "metadata": {
            "name": ISOLATION_POLICY_NAME,
            "namespace": namespace,
            "labels": daegam_labels(tenant=tenant, resource=RESOURCE_ISOLATION_POLICY, id=tenant),
        },
        "spec": {
            "podSelector": {"matchLabels": sandbox_selector()},
            "policyTypes": ["Ingress", "Egress"],
        },
    }


def agent_sandbox_policy_name(agent_id: str) -> str:
    return f"daegam-sandbox-{agent_id}"


def agent_sandbox_ingress(agent: Agent) -> list[dict[str, Any]]:
    """Agent 정책의 ingress 규칙. 그 Agent 의 파드에서 8080 으로 오는 것뿐이다. 다시 읽기
    (`k8s/sandbox_policy.py`)가 이 값과 맞댄다."""
    return [
        {
            "from": [{"podSelector": {"matchLabels": selector_labels(agent)}}],
            "ports": [{"protocol": "TCP", "port": SANDBOX_PORT}],
        }
    ]


def render_agent_sandbox_policy(agent: Agent) -> dict[str, Any]:
    """`daegam-sandbox-<Agent id>` (4절). 그 Agent 의 샌드박스만 골라 Agent 파드에서 8080 으로 오는
    것만 연다. Agent id 를 싣고 있어 Agent 를 내리면 라벨로 지워지고, 지워지면 접근만 닫힌다."""
    if agent.k8s is None:
        raise ValueError(f"agent {agent.id} is not deployed")
    return {
        "apiVersion": NETWORK_POLICY_API,
        "kind": "NetworkPolicy",
        "metadata": {
            "name": agent_sandbox_policy_name(agent.id),
            "namespace": agent.k8s.namespace,
            "labels": daegam_labels(
                tenant=agent.tenant, resource=RESOURCE_AGENT_SANDBOX_POLICY, id=agent.id
            ),
        },
        "spec": {
            "podSelector": {
                "matchLabels": {**sandbox_selector(), LABEL_AGENT: agent.id},
            },
            "policyTypes": ["Ingress"],
            "ingress": agent_sandbox_ingress(agent),
        },
    }


def tenant_policy_selector() -> dict[str, Any]:
    """테넌트 ingress 정책(`daegam-tenant-ingress`)의 podSelector. 샌드박스만 빼고 모든 파드다."""
    return {
        "matchExpressions": [
            {"key": LABEL_RESOURCE, "operator": "NotIn", "values": [SANDBOX_RESOURCE]}
        ]
    }
