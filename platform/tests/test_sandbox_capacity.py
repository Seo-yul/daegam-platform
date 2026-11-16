"""샌드박스가 들어갈 노드와 메모리 규칙, 목록 캐시, 노드 고르기 (P40 7절)."""

from __future__ import annotations

from typing import Any

import pytest

from daegam.k8s.labels import LABEL_PLATFORM_ID, LABEL_RESOURCE
from daegam.k8s.sandbox_capacity import (
    ClusterLists,
    ListCache,
    eligible_nodes,
    node_capacity,
    pick_node,
    pod_memory_limit,
    tolerates,
)
from daegam.k8s.templates.sandbox import sandbox_memory_bytes

MIB = 1024**2
GIB = 1024**3
OURS = "install-1"
SANDBOX = sandbox_memory_bytes()  # 2 GiB


def _node(
    name: str,
    allocatable: str = "8Gi",
    *,
    ready: bool = True,
    unschedulable: bool = False,
    labels: dict[str, str] | None = None,
    taints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"taints": taints or []}
    if unschedulable:
        spec["unschedulable"] = True
    return {
        "metadata": {"name": name, "labels": labels or {}},
        "spec": spec,
        "status": {
            "allocatable": {"memory": allocatable},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def _container(limit: str | None = None, request: str | None = None, **kw: Any) -> dict:
    resources: dict[str, Any] = {}
    if limit:
        resources["limits"] = {"memory": limit}
    if request:
        resources["requests"] = {"memory": request}
    return {"name": "c", "resources": resources, **kw}


def _pod(
    name: str,
    node: str | None = "n1",
    *,
    limit: str | None = "1Gi",
    phase: str = "Running",
    namespace: str = "ns",
    labels: dict[str, str] | None = None,
    containers: list[dict] | None = None,
    init: list[dict] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"containers": containers or [_container(limit)]}
    if node:
        spec["nodeName"] = node
    if init:
        spec["initContainers"] = init
    return {
        "metadata": {"name": name, "namespace": namespace, "labels": labels or {}},
        "spec": spec,
        "status": {"phase": phase},
    }


def _capacity(nodes, pods, limit_ranges=(), **kw: Any):
    args: dict[str, Any] = {
        "node_selector": {},
        "tolerations": [],
        "platform_id": OURS,
        "sandbox_bytes": SANDBOX,
    }
    return node_capacity(nodes, pods, limit_ranges, **{**args, **kw})


def test_a_small_node_holds_one_sandbox_with_one_docparse_replica() -> None:
    """작은 단일 노드의 예: allocatable 19,377 MiB, reserve 2 GiB. docparse 두 벌이면
    다른 파드의 한도가 15,771 MiB 라 0, 한 벌이면 13,723 MiB 라 1 이다."""
    node = _node("node-a", f"{19377}Mi")
    two = _capacity([node], [_pod("others", "node-a", limit=f"{15771}Mi")])
    one = _capacity([node], [_pod("others", "node-a", limit=f"{13723}Mi")])
    assert (two.total, one.total) == (0, 1)
    assert one.nodes[0].others == 13723 * MIB


def test_pods_without_a_limit_count_their_request_or_the_namespace_default() -> None:
    limit_ranges = [
        {
            "metadata": {"namespace": "tenant"},
            "spec": {"limits": [{"type": "Container", "default": {"memory": "256Mi"}}]},
        }
    ]
    pods = [
        _pod("bare", namespace="tenant", containers=[_container(request="64Mi")]),
        _pod("big-request", namespace="tenant", containers=[_container(request="1Gi")]),
        _pod("no-default", namespace="other", containers=[_container(request="70Mi")]),
        _pod("limited", limit="100Mi"),
    ]
    cap = _capacity([_node("n1")], pods, limit_ranges)
    assert cap.nodes[0].others == (256 + 1024 + 70 + 100) * MIB
    assert cap.assumed == ("other/no-default", "tenant/bare", "tenant/big-request")


@pytest.mark.parametrize(
    ("init", "containers", "want"),
    [
        # 가장 큰 init 이 앱보다 크다
        ([_container("500Mi")], [_container("100Mi")], 500),
        # 네이티브 사이드카는 앱과 함께 돈다
        ([_container("50Mi", restartPolicy="Always")], [_container("100Mi")], 150),
        # 사이드카 뒤의 init 은 그 사이드카와 함께 돈다
        (
            [_container("300Mi", restartPolicy="Always"), _container("400Mi")],
            [_container("100Mi")],
            700,
        ),
        # 사이드카 앞의 init 은 혼자다
        (
            [_container("400Mi"), _container("300Mi", restartPolicy="Always")],
            [_container("100Mi")],
            400,
        ),
    ],
)
def test_init_containers_and_native_sidecars(init, containers, want) -> None:
    pod = _pod("p", init=init, containers=containers)
    assert pod_memory_limit(pod) == (want * MIB, False)


def test_overhead_adds_to_the_pod() -> None:
    pod = _pod("p", limit="100Mi")
    pod["spec"]["overhead"] = {"memory": "10Mi"}
    assert pod_memory_limit(pod)[0] == 110 * MIB


def test_an_unbound_pending_pod_counts_on_the_node_with_most_headroom() -> None:
    nodes = [_node("n1", "8Gi"), _node("n2", "8Gi")]
    pods = [
        _pod("busy", "n1", limit="2Gi"),
        _pod("waiting", None, limit="3Gi", phase="Pending"),
    ]
    cap = _capacity(nodes, pods)
    assert {n.name: n.others for n in cap.nodes} == {"n1": 2 * GIB, "n2": 3 * GIB}
    # (8 - 2 - 2) // 2 = 2, (8 - 2 - 3) // 2 = 1
    assert cap.caps() == {"n1": 2, "n2": 1} and cap.total == 3


def test_own_sandboxes_are_left_to_the_slots_and_others_count() -> None:
    def sandbox(name: str, install: str) -> dict:
        labels = {LABEL_RESOURCE: "sandbox", LABEL_PLATFORM_ID: install}
        return _pod(name, labels=labels, limit="2Gi")

    pods = [
        sandbox("mine", OURS),
        sandbox("theirs", "install-2"),
        _pod("done", limit="4Gi", phase="Succeeded"),
        _pod("failed", limit="4Gi", phase="Failed"),
    ]
    cap = _capacity([_node("n1", "8Gi")], pods)
    assert cap.nodes[0].others == 2 * GIB
    assert cap.caps() == {"n1": 2}


def test_the_reserve_and_the_cluster_cap() -> None:
    assert _capacity([_node("n1", "8Gi")], [], reserve=0).total == 4
    many = [_node(f"n{i}", "64Gi") for i in range(3)]
    assert _capacity(many, []).total == 20
    assert _capacity(many, [], max_sandboxes=5).total == 5
    with pytest.raises(ValueError):
        _capacity(many, [], sandbox_bytes=0)


# -- 들어갈 노드 ---------------------------------------------------------------------------


def test_eligible_nodes_skip_cordoned_not_ready_untolerated_and_unselected() -> None:
    taint = {"key": "sandbox", "value": "yes", "effect": "NoSchedule"}
    nodes = [
        _node("cordoned", unschedulable=True),
        _node("not-ready", ready=False),
        _node("tainted", taints=[taint]),
        _node("other-pool", labels={"pool": "web"}),
        _node("soft", labels={"pool": "sbx"}, taints=[{"key": "x", "effect": "PreferNoSchedule"}]),
        _node("ok", labels={"pool": "sbx"}),
    ]
    names = [n["metadata"]["name"] for n in eligible_nodes(nodes, {"pool": "sbx"}, [])]
    assert names == ["ok", "soft"]
    tolerating = [{"key": "sandbox", "operator": "Equal", "value": "yes", "effect": "NoSchedule"}]
    names = [n["metadata"]["name"] for n in eligible_nodes(nodes, {}, tolerating)]
    assert names == ["ok", "other-pool", "soft", "tainted"]


@pytest.mark.parametrize(
    ("toleration", "want"),
    [
        ({"operator": "Exists"}, True),
        ({"key": "sandbox", "operator": "Exists"}, True),
        ({"key": "sandbox", "value": "yes"}, True),
        ({"key": "sandbox", "value": "no"}, False),
        ({"key": "sandbox", "operator": "Exists", "effect": "NoExecute"}, False),
        ({"key": "other", "operator": "Exists"}, False),
    ],
)
def test_toleration_rules(toleration, want) -> None:
    assert (
        tolerates([toleration], {"key": "sandbox", "value": "yes", "effect": "NoSchedule"}) is want
    )


def test_pods_on_other_nodes_do_not_count() -> None:
    nodes = [_node("ok", labels={"pool": "sbx"}), _node("web", labels={"pool": "web"})]
    cap = _capacity(nodes, [_pod("p", "web", limit="6Gi")], node_selector={"pool": "sbx"})
    assert cap.caps() == {"ok": 3}


# -- 캐시와 노드 고르기 ---------------------------------------------------------------------------


def test_the_lists_are_cached_for_the_ttl() -> None:
    now = [0.0]
    calls: list[str] = []
    cache = ListCache(ttl=60, clock=lambda: now[0])

    def fetch() -> ClusterLists:
        calls.append("fetch")
        return ClusterLists(nodes=[], pods=[], limit_ranges=[])

    first = cache.get("c1", fetch)
    now[0] = 59
    assert cache.get("c1", fetch) is first
    cache.get("c2", fetch)
    now[0] = 61
    cache.get("c1", fetch)
    cache.invalidate("c2")
    cache.get("c2", fetch)
    assert len(calls) == 4


def test_pick_node_takes_the_most_free_slots() -> None:
    assert pick_node({"a": 2, "b": 3}, {"b": 2}) == "a"
    assert pick_node({"a": 2, "b": 2}, {}) == "a"
    assert pick_node({"a": 1, "b": 1}, {"a": 1, "b": 1}) is None
    assert pick_node({}, {}) is None
