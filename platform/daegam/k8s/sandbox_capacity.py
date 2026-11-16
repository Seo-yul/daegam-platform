"""샌드박스가 들어갈 노드와 그 메모리 규칙 (P40 7절).

- 들어갈 노드(eligible): Ready 이고 cordon 되지 않았으며, 샌드박스 노드 셀렉터에 맞고, 모든
  `NoSchedule`·`NoExecute` taint 를 샌드박스 toleration 이 견딘다. 메모리 규칙, `shared_nodes`,
  자체 점검 대상(8절)이 이 한 집합을 쓴다.
- 노드 용량: `cap_i = floor((allocatable_i - reserve - others_i) / 샌드박스 한도)`. `others_i` 는
  노드 i 에 묶인 `Pending`·`Running` 파드의 실효 메모리 한도를 쿠버네티스가 셈하는 대로(앱
  컨테이너와 네이티브 사이드카, 그보다 크면 가장 큰 init 컨테이너) 더한 것이다. 이 설치의
  샌드박스는 슬롯 수가 덮으므로 빼고, 다른 설치의 샌드박스는 센다. 메모리 한도가 없는 컨테이너는
  요청과 네임스페이스 LimitRange 기본값 가운데 큰 것으로 세고, 그런 파드를 화면에 알린다. 아직
  노드에 묶이지 않은 Pending 파드는 여유가 가장 큰 들어갈 노드에 센다.
- 클러스터 용량: `min(cap_i 의 합, DAEGAM_SANDBOX_MAX)`. 물을 때마다 센다. 노드와 파드 목록은
  60초까지 캐시한다(`ListCache`).

규칙은 샌드박스가 열릴 때 성립한다. 뒤에 요청으로 놓인 파드(롤아웃의 surge, 캐시 창 안의 배포,
Advanced 배치)가 노드를 allocatable 너머로 밀 수 있고, reserve 가 그것을 받는다.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kubernetes.utils import parse_quantity

from daegam.k8s.labels import LABEL_PLATFORM_ID, LABEL_RESOURCE
from daegam.k8s.templates.sandbox import GIB, SANDBOX_RESOURCE, sandbox_placement
from daegam.store.clusters import Cluster

# 노드마다 남기는 메모리 (DAEGAM_SANDBOX_NODE_RESERVE). 롤아웃이 surge 하는 가장 큰 파드(한도
# 2 GiB 인 daegam-docparse)와, kubelet 이 시스템 몫을 두지 않는 노드라면 호스트 자신의 몫을 덮는다
NODE_RESERVE_BYTES = 2 * GIB
# 클러스터에서 한 번에 열 수 있는 샌드박스의 위 (DAEGAM_SANDBOX_MAX)
MAX_SANDBOXES = 20
# 노드·파드·LimitRange 목록을 다시 묻지 않는 시간(초)
LIST_TTL_SECONDS = 60.0
COUNTED_PHASES = frozenset({"Pending", "Running"})
BLOCKING_EFFECTS = frozenset({"NoSchedule", "NoExecute"})


def _bytes(value: Any) -> int:
    """메모리 quantity 를 바이트로 올림한다. 없거나 읽지 못하면 0 이다."""
    if value is None:
        return 0
    try:
        return math.ceil(parse_quantity(str(value)))
    except ValueError:
        return 0


def _name(obj: Mapping[str, Any]) -> str:
    return str((obj.get("metadata") or {}).get("name") or "")


def node_ready(node: Mapping[str, Any]) -> bool:
    conditions = (node.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def tolerates(tolerations: Iterable[Mapping[str, Any]], taint: Mapping[str, Any]) -> bool:
    """toleration 가운데 하나가 taint 를 견디는가. 쿠버네티스의 규칙이다. 키가 빈 Exists 는 모든
    taint 를, 효과가 빈 toleration 은 모든 효과를 견딘다. 연산자의 기본은 Equal 이다."""
    for t in tolerations:
        op = t.get("operator") or "Equal"
        key = t.get("key") or ""
        if t.get("effect") and t.get("effect") != taint.get("effect"):
            continue
        if op == "Exists" and (not key or key == taint.get("key")):
            return True
        if (
            op == "Equal"
            and key == taint.get("key")
            and (t.get("value") or "") == (taint.get("value") or "")
        ):
            return True
    return False


def eligible_nodes(
    nodes: Iterable[Mapping[str, Any]],
    node_selector: Mapping[str, str],
    tolerations: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """샌드박스가 들어갈 노드. 이름 차례다."""
    tolerations = list(tolerations)
    out = []
    for node in nodes:
        spec = node.get("spec") or {}
        labels = (node.get("metadata") or {}).get("labels") or {}
        if not node_ready(node) or spec.get("unschedulable"):
            continue
        if any(labels.get(k) != v for k, v in node_selector.items()):
            continue
        taints = [t for t in spec.get("taints") or [] if t.get("effect") in BLOCKING_EFFECTS]
        if all(tolerates(tolerations, t) for t in taints):
            out.append(node)
    return sorted(out, key=_name)


def eligible_for(cluster: Cluster, nodes: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """그 클러스터의 샌드박스 배치(`sandbox_placement`)로 본 들어갈 노드."""
    placement = sandbox_placement(cluster)
    return eligible_nodes(nodes, placement["node_selector"], placement["tolerations"])


def limit_range_defaults(limit_ranges: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """네임스페이스마다 LimitRange 의 Container 기본 메모리 한도(`default.memory`). 여럿이면 큰
    것이다."""
    out: dict[str, int] = {}
    for lr in limit_ranges:
        ns = str((lr.get("metadata") or {}).get("namespace") or "")
        for item in (lr.get("spec") or {}).get("limits") or []:
            if item.get("type") != "Container":
                continue
            value = _bytes((item.get("default") or {}).get("memory"))
            if value:
                out[ns] = max(out.get(ns, 0), value)
    return out


def limit_range_max(
    limit_ranges: Iterable[Mapping[str, Any]], namespace: str, resource: str = "memory"
) -> Decimal | None:
    """네임스페이스의 LimitRange 가 컨테이너나 파드 하나에 허락하는 가장 큰 resource(`max.memory`·
    `max.cpu`)다. 여럿이면 가장 작은 것이고, 없으면 None 이다. 메모리는 바이트, CPU 는 코어 수다.
    샌드박스는 컨테이너 하나의 파드라 둘 다 본다(7절, 배포 검사). 읽지 못하는 값은 건너뛴다."""
    caps: list[Decimal] = []
    for lr in limit_ranges:
        if str((lr.get("metadata") or {}).get("namespace") or "") != namespace:
            continue
        for item in (lr.get("spec") or {}).get("limits") or []:
            value = (item.get("max") or {}).get(resource)
            if item.get("type") not in ("Container", "Pod") or value is None:
                continue
            try:
                caps.append(parse_quantity(str(value)))
            except ValueError:
                continue
    return min(caps) if caps else None


def _container_memory(container: Mapping[str, Any], default: int) -> tuple[int, bool]:
    """(바이트, 한도 없이 셌는가). 한도가 없으면 요청과 LimitRange 기본값 가운데 큰 것이다."""
    resources = container.get("resources") or {}
    limit = (resources.get("limits") or {}).get("memory")
    if limit is not None:
        return _bytes(limit), False
    return max(_bytes((resources.get("requests") or {}).get("memory")), default), True


def pod_memory_limit(pod: Mapping[str, Any], default: int = 0) -> tuple[int, bool]:
    """파드의 실효 메모리 한도와, 한도 없이 센 컨테이너가 있었는가. 쿠버네티스의 셈(KEP-753)이다.
    init 컨테이너는 차례로 돌며, 그때까지 뜬 네이티브 사이드카(`restartPolicy: Always`)와 함께
    셈한다. 앱 컨테이너는 모든 사이드카와 함께 돈다. 둘 가운데 큰 것에 RuntimeClass overhead 를
    더한다."""
    spec = pod.get("spec") or {}
    assumed = False
    sidecars = 0
    init_peak = 0
    for container in spec.get("initContainers") or []:
        value, guessed = _container_memory(container, default)
        assumed |= guessed
        if container.get("restartPolicy") == "Always":
            sidecars += value
            init_peak = max(init_peak, sidecars)
        else:
            init_peak = max(init_peak, value + sidecars)
    app = 0
    for container in spec.get("containers") or []:
        value, guessed = _container_memory(container, default)
        assumed |= guessed
        app += value
    overhead = _bytes((spec.get("overhead") or {}).get("memory"))
    return max(app + sidecars, init_peak) + overhead, assumed


def is_own_sandbox(pod: Mapping[str, Any], platform_id: str) -> bool:
    """이 설치의 샌드박스 파드인가. 슬롯 수가 그 메모리를 덮는다."""
    labels = (pod.get("metadata") or {}).get("labels") or {}
    return labels.get(LABEL_RESOURCE) == SANDBOX_RESOURCE and (
        labels.get(LABEL_PLATFORM_ID) == platform_id
    )


@dataclass(frozen=True)
class NodeCapacity:
    """들어갈 노드 하나. others 는 이 설치의 샌드박스를 뺀 파드의 메모리 한도 합이고 cap 은 그
    노드에 열 수 있는 이 설치의 샌드박스 수다."""

    name: str
    allocatable: int
    others: int
    cap: int


@dataclass(frozen=True)
class Capacity:
    """메모리 규칙의 답. total 은 클러스터 용량이고 assumed 는 메모리 한도 없이 센 파드
    (`namespace/name`)다."""

    nodes: tuple[NodeCapacity, ...]
    total: int
    assumed: tuple[str, ...] = ()

    def caps(self) -> dict[str, int]:
        return {n.name: n.cap for n in self.nodes}


def node_capacity(
    nodes: Iterable[Mapping[str, Any]],
    pods: Iterable[Mapping[str, Any]],
    limit_ranges: Iterable[Mapping[str, Any]],
    *,
    node_selector: Mapping[str, str],
    tolerations: Iterable[Mapping[str, Any]],
    platform_id: str,
    sandbox_bytes: int,
    reserve: int = NODE_RESERVE_BYTES,
    max_sandboxes: int = MAX_SANDBOXES,
) -> Capacity:
    """들어갈 노드마다의 용량과 클러스터 용량."""
    if sandbox_bytes <= 0:
        raise ValueError("sandbox_bytes must be positive")
    eligible = eligible_nodes(nodes, node_selector, tolerations)
    allocatable = {
        _name(n): _bytes(((n.get("status") or {}).get("allocatable") or {}).get("memory"))
        for n in eligible
    }
    others = dict.fromkeys(allocatable, 0)
    defaults = limit_range_defaults(limit_ranges)
    assumed: list[str] = []
    unbound: list[tuple[str, int]] = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        if (pod.get("status") or {}).get("phase") not in COUNTED_PHASES:
            continue
        if is_own_sandbox(pod, platform_id):
            continue
        ns = str(meta.get("namespace") or "")
        value, guessed = pod_memory_limit(pod, defaults.get(ns, 0))
        node = (pod.get("spec") or {}).get("nodeName")
        if guessed and (not node or node in others):
            assumed.append(f"{ns}/{_name(pod)}")
        if node:
            if node in others:
                others[node] += value
        else:
            unbound.append((f"{ns}/{_name(pod)}", value))
    # 묶이지 않은 Pending 파드는 그때 여유가 가장 큰 노드에 하나씩 센다. 차례는 이름이라 어느
    # 레플리카에서나 같다
    for _, value in sorted(unbound):
        if not others:
            break
        best = max(others, key=lambda n: (allocatable[n] - others[n], n))
        others[best] += value
    rows = tuple(
        NodeCapacity(
            name=name,
            allocatable=allocatable[name],
            others=others[name],
            cap=max(0, (allocatable[name] - reserve - others[name]) // sandbox_bytes),
        )
        for name in sorted(allocatable)
    )
    total = min(sum(r.cap for r in rows), max_sandboxes)
    return Capacity(nodes=rows, total=total, assumed=tuple(sorted(assumed)))


def pick_node(caps: Mapping[str, int], used: Mapping[str, int]) -> str | None:
    """빈 슬롯이 가장 많은 노드. 같으면 이름이 앞선 것이다. 빈 슬롯이 없으면 None 이다. 열기가 이
    노드의 슬롯을 조건부 쓰기로 잡고 그 노드에 파드를 묶는다(`render_sandbox_pod` 의 node)."""
    free = {name: cap - used.get(name, 0) for name, cap in caps.items()}
    open_nodes = [name for name, n in free.items() if n > 0]
    if not open_nodes:
        return None
    return min(open_nodes, key=lambda name: (-free[name], name))


@dataclass(frozen=True)
class ClusterLists:
    """메모리 규칙이 읽는 목록 셋과, 자체 점검 대상(8절, `k8s/sandbox_targets.py`)이 함께 읽는
    Service 목록."""

    nodes: list[dict[str, Any]]
    pods: list[dict[str, Any]]
    limit_ranges: list[dict[str, Any]]
    services: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ListCache:
    """클러스터마다의 목록을 ttl 초까지 둔다. 레플리카마다 따로이고 슬롯 수는 Mongo 의 조건부
    쓰기가 맞추므로, 두 레플리카의 캐시가 달라도 같은 노드의 슬롯을 넘겨 잡지 않는다(F2b)."""

    ttl: float = LIST_TTL_SECONDS
    clock: Callable[[], float] = time.monotonic
    _entries: dict[str, tuple[float, ClusterLists]] = field(default_factory=dict)

    def get(self, cluster_id: str, fetch: Callable[[], ClusterLists]) -> ClusterLists:
        now = self.clock()
        hit = self._entries.get(cluster_id)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        lists = fetch()
        self._entries[cluster_id] = (now, lists)
        return lists

    def invalidate(self, cluster_id: str) -> None:
        self._entries.pop(cluster_id, None)


def read_lists(client: Any) -> ClusterLists:
    """클러스터에서 노드·파드·LimitRange·Service 목록을 읽는다. 노드의 get·list 권한은 P40 의
    RBAC 가 준다."""
    return ClusterLists(
        nodes=client.list("v1", "Node"),
        pods=client.list("v1", "Pod"),
        limit_ranges=client.list("v1", "LimitRange"),
        services=client.list("v1", "Service"),
    )
