"""코드 실행 샌드박스 시험의 클러스터·Agent·파드 (P40).

클러스터는 판정이 통과했고 권한이 있으며 노드 하나(16 GiB)가 있는 `edge` 다. Agent 는 테넌트 acme 의
배포된 문서이고 메인 에이전트 노드가 코드 실행을 켰다. 네임스페이스 `daegam-acme` 에는 테넌트 정책과
격리 정책이 있다. 파드 훅(`kubelet`)이 만든 샌드박스 파드에 준비된 status 를 적는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from daegam.k8s.templates.sandbox import (
    SANDBOX_CONTAINER,
    TARGETS_ENV,
    placement_fingerprint,
    render_isolation_policy,
    sandbox_image,
)
from daegam.services.tenancy import network_policy_manifest
from daegam.store.agent_instance_tokens import scope_for
from daegam.store.base import hash_token, iso_utc, to_millis, utcnow
from daegam.store.clusters import (
    Cluster,
    ClusterConnection,
    ClusterGates,
    ClusterSandbox,
    ClusterStatus,
    Preflight,
    SandboxCheck,
)
from tests.helpers.consts import ISSUER
from tests.helpers.docs import agent_doc
from tests.helpers.fake_cluster import FakeClusterClient

AGENT_ID = "ag-sbx"
TOKEN = "instance-token-sbx"
TENANT = "acme"
NS = "daegam-acme"
CLUSTER = "edge"
K8S_VERSION = "v1.34.0-fake"
IMAGE_ID = "docker.io/library/daegam-sandbox@sha256:" + "a" * 64
OTHER_IMAGE_ID = "docker.io/library/daegam-sandbox@sha256:" + "b" * 64
POD_IP = "10.42.0.5"
GIB = 1024**3


def node(
    name: str = "n1",
    *,
    memory: str = "16Gi",
    ip: str = "10.0.0.1",
    ready: bool = True,
    labels: dict[str, str] | None = None,
    taints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name, **(labels or {})}},
        "spec": {"taints": taints or []},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "allocatable": {"memory": memory},
            "addresses": [{"type": "InternalIP", "address": ip}],
        },
    }


def service(ns: str, name: str, ip: str, port: int, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"clusterIP": ip, "ports": [{"port": port}], **spec},
    }


def code_exec_spec(on: Any = True) -> dict[str, Any]:
    """메인 에이전트 노드 하나의 그래프. on 이 None 이면 코드 실행 칸이 없다."""
    data: dict[str, Any] = {"role": "main", "name": "main"}
    if on is True:
        data["code_execution"] = {"approval": []}
    elif on is not None:
        data["code_execution"] = on
    return {"graph": {"nodes": [{"id": "main", "type": "agent", "data": data}]}}


def cluster(
    name: str = CLUSTER,
    *,
    verdict: bool = True,
    rights: bool = True,
    shared: bool = False,
    accepted: bool = False,
) -> Cluster:
    c = Cluster(
        name=name,
        connection=ClusterConnection(mode="kubeconfig", secret_name=f"cluster-{name}"),
        platform_internal_url=ISSUER,
        gates=ClusterGates(sandbox_rights=rights),
        status=ClusterStatus(preflight=Preflight(ok=True, k8s_version=K8S_VERSION)),
    )
    if verdict:
        c.sandbox = ClusterSandbox(check=passed_check(c, shared_nodes=shared, accepted=accepted))
    return c


def passed_check(c: Cluster, *, accepted: bool = False, **fields: Any) -> SandboxCheck:
    """c 의 지금 배치·이미지·preflight 에 맞는 통과한 판정."""
    now = to_millis(utcnow())
    base: dict[str, Any] = {
        "passed": True,
        "userns": True,
        "network": True,
        "shared_nodes": False,
        "capacity": 1,
        "placement": placement_fingerprint(c),
        "image": sandbox_image(c),
        "image_id": IMAGE_ID,
        "k8s_version": K8S_VERSION,
        "policy_controller": None,
        "checked_at": now,
    }
    if accepted:
        base.update(accepted_by="root", accepted_at=now)
    return SandboxCheck(**{**base, **fields})


def seed_cluster(mongo, fake: FakeClusterClient, c: Cluster, *nodes: dict[str, Any]) -> Cluster:
    """c 를 Mongo 에 넣고 fake 에 노드·DNS·API Service·네임스페이스 정책을 심는다."""
    mongo["clusters"].insert_one(c.to_doc())
    for n in nodes or (node(),):
        fake.objects[("v1", "Node", None, n["metadata"]["name"])] = n
    fake.apply(service("kube-system", "kube-dns", "10.43.0.10", 53))
    fake.apply(service("default", "kubernetes", "10.43.0.1", 443))
    fake.apply(render_isolation_policy(TENANT, NS))
    fake.apply(network_policy_manifest(TENANT, NS, "daegam-system"))
    return c


def seed_agent(
    mongo,
    c: Cluster,
    *,
    aid: str = AGENT_ID,
    token: str = TOKEN,
    on: Any = True,
    state: str = "deployed",
    tenant: str = TENANT,
):
    """배포된 Agent 와 그 인스턴스 토큰."""
    doc = agent_doc(aid, spec=code_exec_spec(on), state=state, cluster=c.id, tenant=tenant)
    doc.k8s_uid = f"uid-{aid}"
    mongo["agents"].insert_one(doc.to_doc())
    mongo["agent_instance_tokens"].insert_one(
        {
            "_id": f"tok-{aid}",
            "agent_instance_id": aid,
            "agent_profile_id": aid,
            "token_sha256": hash_token(token),
            "scope": [scope_for(aid, doc.deployed_revision or 1)],
            "created_at": utcnow(),
            "revoked_at": None,
        }
    )
    return doc


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def container_status(
    *,
    ready: bool = True,
    running: bool = True,
    exit_code: int | None = None,
    reason: str | None = None,
    restarts: int = 0,
    image_id: str = IMAGE_ID,
    waiting: str | None = None,
    started_ago: timedelta = timedelta(seconds=1),
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if running:
        state["running"] = {"startedAt": iso_utc(utcnow() - started_ago)}
    elif waiting:
        state["waiting"] = {"reason": waiting}
    status: dict[str, Any] = {
        "name": SANDBOX_CONTAINER,
        "ready": ready,
        "restartCount": restarts,
        "state": state,
        "imageID": image_id,
    }
    if exit_code is not None or reason:
        status["lastState"] = {"terminated": {"exitCode": exit_code, "reason": reason}}
    return status


def pod_status(**container: Any) -> dict[str, Any]:
    return {
        "phase": container.pop("phase", "Running"),
        "podIP": container.pop("pod_ip", POD_IP),
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": [container_status(**container)],
    }


def kubelet(fake: FakeClusterClient, **container: Any) -> Callable[[dict[str, Any]], None]:
    """새 샌드박스 파드에 status 를 적는 훅을 건다. 준비된 컨테이너가 기본이다."""

    def hook(obj: dict[str, Any]) -> None:
        if obj.get("kind") != "Pod":
            return
        affinity = (obj.get("spec") or {}).get("affinity") or {}
        terms = (
            affinity.get("nodeAffinity", {})
            .get("requiredDuringSchedulingIgnoredDuringExecution", {})
            .get("nodeSelectorTerms", [])
        )
        if terms:
            obj["spec"]["nodeName"] = terms[0]["matchExpressions"][0]["values"][0]
        obj["status"] = pod_status(**dict(container))

    fake.create_hooks.append(hook)
    return hook


def sandbox_pods(fake: FakeClusterClient, ns: str = NS) -> list[dict[str, Any]]:
    return [
        obj
        for (av, kind, pns, _name), obj in fake.objects.items()
        if av == "v1" and kind == "Pod" and pns == ns
    ]


def set_status(fake: FakeClusterClient, pod: dict[str, Any], **container: Any) -> None:
    """있는 파드의 status 를 바꾼다."""
    key = ("v1", "Pod", pod["metadata"]["namespace"], pod["metadata"]["name"])
    fake.objects[key]["status"] = pod_status(**container)


# -- 클러스터 점검의 파드 (8절) ----------------------------------------------------------------

Reach = Callable[[dict[str, Any]], bool]


def _always(t: dict[str, Any]) -> bool:
    return t["host"] in ("10.43.0.10", "10.43.0.1") or t.get("port") == 10250


def _terminated(code: int, *, message: str | None = None) -> dict[str, Any]:
    phase = "Succeeded" if code == 0 else "Failed"
    state = {"terminated": {"exitCode": code, "message": message}}
    return {
        "phase": phase,
        "podIP": "10.42.0.9",
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": [
            {"name": SANDBOX_CONTAINER, "ready": False, "restartCount": 0, "state": state}
        ],
    }


def kubelet_for_check(
    fake: FakeClusterClient,
    *,
    reach: Reach = _always,
    sandbox: dict[str, Any] | None = None,
    client_exit: int = 3,
    control_exit: int = 0,
) -> None:
    """점검의 세 파드에 끝을 적는다. sandbox 는 진짜 샌드박스의 컨테이너 status 다(기본 준비됨)."""

    def hook(obj: dict[str, Any]) -> None:
        if obj.get("kind") != "Pod":
            return
        name, ns = obj["metadata"]["name"], obj["metadata"]["namespace"]
        if name.startswith("sbx-control-"):
            env = {e["name"]: e["value"] for e in obj["spec"]["containers"][0]["env"]}
            for t in json.loads(env[TARGETS_ENV]):
                line = {**t, "port": t.get("port"), "reached": reach(t), "seconds": 0.01}
                fake.add_pod_log(ns, name, json.dumps(line))
            obj["status"] = _terminated(control_exit)
        elif name.startswith("sbx-client-"):
            obj["status"] = _terminated(client_exit)
        elif name.startswith("sbx-probe-"):
            return
        else:
            obj["status"] = pod_status(**(sandbox or {}))

    fake.create_hooks.append(hook)
