"""클러스터의 격리 점검과 허용, API 칸, shared_nodes, 사용자 코드 없는 프로브 (P40 8절).

대역 클러스터의 파드 훅이 kubelet 처럼 점검 파드마다 끝을 적는다. 양성 대조는 받은 대상 가운데
`reach` 가 고른 것에 닿았다고 찍는다.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from daegam.k8s.templates.sandbox import (
    LABEL_PROBE,
    PROBE_NAMESPACE,
    TARGETS_ENV,
)
from daegam.services.sandbox_check import check_lease
from daegam.store.base import iso_utc, utcnow
from daegam.store.leases import Lease
from tests.helpers.aio import run_db
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import detail_code
from tests.helpers.sandboxes import (
    AGENT_ID,
    CLUSTER,
    IMAGE_ID,
    NS,
    auth,
    cluster,
    container_status,
    kubelet,
    kubelet_for_check,
    node,
    sandbox_pods,
    seed_agent,
    seed_cluster,
    set_status,
)


@pytest.fixture
def fake(fake_clusters) -> FakeClusterClient:
    client = FakeClusterClient()
    fake_clusters.by_name[CLUSTER] = client
    return client


def _run(client, cluster_id: str):
    state = client.app.state
    doc = client.portal.call(state.clusters.get, cluster_id)
    return client.portal.call(state.sandbox_checks.run, doc, "root")


def _sandbox(mongo, cluster_id: str) -> dict[str, Any]:
    return mongo["clusters"].find_one({"_id": cluster_id})["sandbox"]


def test_a_full_check_writes_a_passed_verdict_and_cleans_up(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    kubelet_for_check(fake)
    assert _run(client, c.id) is not None
    sbx = _sandbox(mongo, c.id)
    check = sbx["check"]
    assert check["passed"] is True and check["userns"] is True and check["network"] is True
    kept = {(t["kind"], t["host"], t.get("port")) for t in check["targets"]}
    assert kept == {
        ("dns", "10.43.0.10", 53),
        ("tcp", "10.43.0.10", 53),
        ("tcp", "10.43.0.1", 443),
        ("tcp", "10.0.0.1", 10250),
    }
    assert all(t["seconds"] == 0.01 for t in check["targets"])
    assert check["image_id"] == IMAGE_ID and check["shared_nodes"] is False
    assert check["capacity"] == 7 and check["checked_at"] and check["accepted_at"] is None
    assert sbx["run"]["error"] is None and sbx["run"]["finished_at"]
    # 점검 파드를 지우고 슬롯을 돌려준다
    assert sandbox_pods(fake, PROBE_NAMESPACE) == []
    rec = mongo["sandboxes"].find_one({"kind": "check"})
    assert rec["state"] == "closed" and rec["close_reason"] == "check_done"
    # 진짜 샌드박스는 남긴 대상만 받았다
    real = next(
        o
        for o in fake.applied
        if o.get("kind") == "Pod"
        and not o["metadata"]["name"].startswith(("sbx-control", "sbx-client"))
    )
    env = {e["name"]: e["value"] for e in real["spec"]["containers"][0]["env"]}
    assert len(json.loads(env[TARGETS_ENV])) == 4
    assert "ownerReferences" not in real["metadata"]
    # 대역 테넌트의 정책이 점검 네임스페이스에 있다
    assert (
        "networking.k8s.io/v1",
        "NetworkPolicy",
        PROBE_NAMESPACE,
        "daegam-sandbox-isolation",
    ) in (fake.objects)


def test_a_new_check_replaces_the_verdict_and_clears_acceptance(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(shared=True, accepted=True))
    kubelet_for_check(fake)
    _run(client, c.id)
    assert _sandbox(mongo, c.id)["check"]["accepted_at"] is None


def test_a_control_that_cannot_reach_dns_means_the_check_could_not_run(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    kubelet_for_check(fake, reach=lambda t: t["host"] == "10.43.0.1")
    assert _run(client, c.id) is None
    sbx = _sandbox(mongo, c.id)
    assert sbx["run"]["error"].startswith("control:") and "10.43.0.10" in sbx["run"]["error"]
    # 앞의 판정은 그대로다
    assert sbx["check"]["passed"] is True


def test_a_self_check_failure_fails_userns_or_network(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    identity = json.dumps({"check": "first", "result": "identity"})
    failed = {"ready": False, "running": False, "waiting": "CrashLoopBackOff", "exit_code": 87}
    kubelet_for_check(fake, sandbox=failed)
    pods = fake.create_hooks[-1]

    def with_message(obj: dict[str, Any]) -> None:
        pods(obj)
        if (
            obj.get("kind") == "Pod"
            and obj["metadata"]["name"].startswith("sbx-")
            and ("control" not in obj["metadata"]["name"])
        ):
            obj["status"]["containerStatuses"][0]["lastState"]["terminated"]["message"] = identity

    fake.create_hooks[-1] = with_message
    _run(client, c.id)
    check = _sandbox(mongo, c.id)["check"]
    assert check["passed"] is False and check["userns"] is False and check["network"] is None
    fake.create_hooks[-1] = pods
    _run(client, c.id)
    check = _sandbox(mongo, c.id)["check"]
    assert check["passed"] is False and check["userns"] is True and check["network"] is False


def test_8080_open_to_a_non_agent_pod_fails_the_network(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    kubelet_for_check(fake, client_exit=0)
    _run(client, c.id)
    check = _sandbox(mongo, c.id)["check"]
    assert check["passed"] is False and check["network"] is False


def test_on_home_the_platform_namespace_also_knocks_on_8080(client, mongo, fake_clusters):
    home = FakeClusterClient()
    fake_clusters.by_name["home"] = home
    c = seed_cluster(mongo, home, cluster("home", verdict=False))
    kubelet_for_check(home)
    knocked: list[tuple[str, int]] = []

    async def connects(host: str, port: int) -> str:
        knocked.append((host, port))
        return "connected"

    client.app.state.sandbox_checks.connect = connects
    _run(client, c.id)
    assert knocked == [("10.42.0.5", 8080)]
    assert _sandbox(mongo, c.id)["check"]["network"] is False


def test_missing_rights_or_preflight_cannot_run(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False, rights=False))
    assert _run(client, c.id) is None
    assert _sandbox(mongo, c.id)["run"]["error"].startswith("rights:")


def test_shared_nodes_counts_other_pods_but_not_daemonsets_or_sandboxes(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    kubelet_for_check(fake)
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "ds-pod",
                "namespace": "kube-system",
                "ownerReferences": [{"kind": "DaemonSet", "name": "x"}],
            },
            "spec": {"nodeName": "n1", "containers": [{"name": "c"}]},
            "status": {"phase": "Running"},
        }
    )
    _run(client, c.id)
    assert _sandbox(mongo, c.id)["check"]["shared_nodes"] is False
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "agent-pod", "namespace": NS},
            "spec": {"nodeName": "n1", "containers": [{"name": "c"}]},
            "status": {"phase": "Running"},
        }
    )
    client.app.state.sandboxes.lists.invalidate(c.id)
    _run(client, c.id)
    assert _sandbox(mongo, c.id)["check"]["shared_nodes"] is True


def test_acceptance_needs_the_checked_at_the_admin_saw(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(shared=True))
    seen = _sandbox(mongo, c.id)["check"]["checked_at"]
    stale = iso_utc(seen - timedelta(seconds=5))
    r = client.post(f"/clusters/{c.id}/sandbox/accept", json={"checked_at": stale})
    assert detail_code(r) == (409, "sandbox_check_stale")
    r = client.post(f"/clusters/{c.id}/sandbox/accept", json={"checked_at": iso_utc(seen)})
    assert r.status_code == 200, r.text
    assert r.json()["sandbox"]["check"]["accepted_by"] == "header"
    audit = mongo["audit_events"].find_one({"path": {"$regex": "sandbox/accept"}})
    assert audit is not None


def test_acceptance_of_a_verdict_that_reads_as_void_is_stale(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(shared=True))
    seen = _sandbox(mongo, c.id)["check"]["checked_at"]
    mongo["clusters"].update_one({"_id": c.id}, {"$set": {"sandbox.image": "other:1"}})
    r = client.post(f"/clusters/{c.id}/sandbox/accept", json={"checked_at": iso_utc(seen)})
    assert detail_code(r) == (409, "sandbox_check_stale")


def test_a_second_check_while_one_runs_is_409(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    settings = client.app.state.settings
    assert run_db(settings, lambda db: Lease(db, "other").acquire(check_lease(c.id)))
    r = client.post(f"/clusters/{c.id}/sandbox/check")
    assert detail_code(r) == (409, "sandbox_check_running")
    assert client.get(f"/clusters/{c.id}/sandbox").json()["check_running"] is True


def test_the_check_route_answers_202_and_records_the_run(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    kubelet_for_check(fake)
    r = client.post(f"/clusters/{c.id}/sandbox/check")
    assert r.status_code == 202 and r.json() == {"started": True}
    client.portal.call(_wait_checks, client.app.state.sandbox_checks)
    sbx = _sandbox(mongo, c.id)
    assert sbx["run"]["by"] == "header" and sbx["check"]["passed"] is True


async def _wait_checks(checks) -> None:
    import asyncio

    for task in list(checks._tasks):
        await asyncio.wait_for(task, timeout=30)


def test_cluster_out_carries_void_reason_and_override(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    body = client.get(f"/clusters/{c.id}").json()["sandbox"]
    assert body["void_reason"] is None and body["override"] is False and body["run"]["by"] is None
    mongo["clusters"].update_one({"_id": c.id}, {"$set": {"sandbox.image": "other:1"}})
    assert client.get(f"/clusters/{c.id}").json()["sandbox"]["void_reason"] == "image"


def test_the_status_route_counts_capacity_and_assumed_limits(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(), node(memory="9Gi"))
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "nolimit", "namespace": "x"},
            "spec": {
                "nodeName": "n1",
                "containers": [{"name": "c", "resources": {"requests": {"memory": "1Gi"}}}],
            },
            "status": {"phase": "Running"},
        }
    )
    r = client.get(f"/clusters/{c.id}/sandbox")
    assert r.status_code == 200, r.text
    body = r.json()
    # (9 - 2 예비 - 1) / 2 = 3
    assert body["capacity"] == 3 and body["gate"] == {"open": True, "reason": None}
    assert body["assumed"] == 1 and body["assumed_pods"] == ["x/nolimit"]
    assert body["nodes"][0] == {
        "name": "n1",
        "allocatable": 9 * 1024**3,
        "others": 1024**3,
        "cap": 3,
        "used": 0,
    }
    assert body["check_running"] is False and body["open_sandboxes"] == 0


def test_the_sweep_recomputes_shared_nodes_and_closes_the_gate_as_unaccepted(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "advanced-agent", "namespace": NS},
            "spec": {"nodeName": "n1", "containers": [{"name": "c"}]},
            "status": {"phase": "Running"},
        }
    )
    client.portal.call(client.app.state.sandbox_sweep.tick)
    check = _sandbox(mongo, c.id)["check"]
    assert check["shared_nodes"] is True and check["passed"] is True
    r = client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "s1"}, headers=auth())
    assert r.json()["detail"]["reason"] == "unaccepted"


# -- 프로브 ------------------------------------------------------------------------------------


@pytest.fixture
def later_failed(client, mongo, fake):
    """88 로 끝난 샌드박스 하나를 스윕이 닫은 클러스터."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "s1"}, headers=auth())
    [pod] = sandbox_pods(fake)
    set_status(fake, pod, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=88)
    client.portal.call(client.app.state.sandbox_sweep.tick)
    return c


def _probes(fake: FakeClusterClient) -> list[dict[str, Any]]:
    return [p for p in sandbox_pods(fake) if LABEL_PROBE in p["metadata"]["labels"]]


def _tick(client) -> None:
    client.portal.call(client.app.state.sandbox_sweep.tick)


def test_exit_88_starts_one_probe_pinned_to_the_node(client, mongo, fake, later_failed):
    _tick(client)
    [probe] = _probes(fake)
    spec = probe["spec"]
    assert spec["restartPolicy"] == "Never" and spec["hostUsers"] is False
    container = spec["containers"][0]
    assert container["args"] == ["probe"]
    assert container["resources"]["requests"]["memory"] == "128Mi"
    assert container["resources"]["limits"]["memory"] == "128Mi"
    assert "volumeMounts" not in container
    term = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert term["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["n1"]
    # 상한에 세지 않는다
    assert mongo["sandboxes"].count_documents({"state": "open"}) == 0
    # 한 노드에 한 번에 하나다
    _tick(client)
    assert len(_probes(fake)) == 1


def test_a_failed_probe_voids_the_verdict_and_a_passed_one_does_not(
    client, mongo, fake, later_failed
):
    _tick(client)
    [probe] = _probes(fake)
    set_status(fake, probe, phase="Succeeded", running=False, ready=False)
    key = ("v1", "Pod", probe["metadata"]["namespace"], probe["metadata"]["name"])
    fake.objects[key]["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
    _tick(client)
    assert _probes(fake) == [] and _sandbox(mongo, later_failed.id)["check"]["passed"] is True
    doc = mongo["sandbox_probes"].find_one()
    assert doc["state"] == "done" and doc["result"] == "passed"
    # 10분 안의 다음 88 은 프로브를 띄우지 않는다
    store = client.app.state.sandboxes.store
    wanted = client.portal.call(
        lambda: store.want_probe(
            later_failed.id, "n1", NS, "acme", quiet_since=utcnow() - timedelta(minutes=10)
        )
    )
    assert wanted is False
    mongo["sandbox_probes"].update_one(
        {}, {"$set": {"started_at": utcnow() - timedelta(minutes=11)}}
    )
    assert client.portal.call(
        lambda: store.want_probe(
            later_failed.id, "n1", NS, "acme", quiet_since=utcnow() - timedelta(minutes=10)
        )
    )
    _tick(client)
    [probe] = _probes(fake)
    key = ("v1", "Pod", probe["metadata"]["namespace"], probe["metadata"]["name"])
    fake.objects[key]["status"] = {
        "phase": "Failed",
        "containerStatuses": [
            container_status(running=False, ready=False)
            | {"state": {"terminated": {"exitCode": 87}}}
        ],
    }
    _tick(client)
    check = _sandbox(mongo, later_failed.id)["check"]
    assert check["passed"] is False and check["void_reason"] == "probe"


def test_an_unscheduled_probe_is_retried(client, mongo, fake, later_failed):
    _tick(client)
    [probe] = _probes(fake)
    key = ("v1", "Pod", probe["metadata"]["namespace"], probe["metadata"]["name"])
    fake.objects[key]["status"] = {"phase": "Pending"}
    mongo["sandbox_probes"].update_one(
        {}, {"$set": {"started_at": utcnow() - timedelta(minutes=3)}}
    )
    _tick(client)
    assert mongo["sandbox_probes"].find_one()["state"] == "wanted" and _probes(fake) == []
    _tick(client)
    assert len(_probes(fake)) == 1


def test_the_sweep_removes_check_pods_an_interrupted_check_left(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    from daegam.k8s.templates.sandbox import render_check_helper_pod

    platform_id = client.portal.call(client.app.state.sandboxes.install_id)
    fake.apply(
        render_check_helper_pod(
            cluster=c,
            name="sbx-control-left",
            args=["control"],
            targets=None,
            platform_id=platform_id,
        )
    )
    _tick(client)
    assert sandbox_pods(fake, PROBE_NAMESPACE) == []


@pytest.mark.parametrize("client_exit", [1, 137])
def test_a_client_that_could_not_try_is_no_network_evidence(client, mongo, fake, client_exit):
    """클라이언트의 0(연결됨)과 3(막힘)만 증거다. 그 밖의 끝은 점검을 하지 못한 것이다."""
    c = seed_cluster(mongo, fake, cluster())
    kubelet_for_check(fake, client_exit=client_exit)
    assert _run(client, c.id) is None
    sbx = _sandbox(mongo, c.id)
    assert sbx["run"]["error"].startswith("client:") and sbx["check"]["passed"] is True


@pytest.mark.parametrize(("knock", "passed"), [("blocked", True), ("unreachable", None)])
def test_the_home_knock_tells_blocked_from_unreachable(client, mongo, fake_clusters, knock, passed):
    home = FakeClusterClient()
    fake_clusters.by_name["home"] = home
    c = seed_cluster(mongo, home, cluster("home", verdict=False))
    kubelet_for_check(home)

    async def knocks(_host: str, _port: int) -> str:
        return knock

    client.app.state.sandbox_checks.connect = knocks
    result = _run(client, c.id)
    if passed is None:
        assert result is None
        assert _sandbox(mongo, c.id)["run"]["error"].startswith("platform:")
    else:
        assert result is not None and result.passed is passed


def test_tcp_knock_reads_refused_as_blocked_and_no_route_as_unreachable(monkeypatch):
    import asyncio
    import errno

    from daegam.services import sandbox_check

    def failing(err: int):
        async def open_connection(*_a, **_k):
            raise OSError(err, "x")

        return open_connection

    monkeypatch.setattr(asyncio, "open_connection", failing(errno.ECONNREFUSED))
    assert asyncio.run(sandbox_check.tcp_knock("10.0.0.1", 8080)) == "blocked"
    monkeypatch.setattr(asyncio, "open_connection", failing(errno.ENETUNREACH))
    assert asyncio.run(sandbox_check.tcp_knock("10.0.0.1", 8080)) == "unreachable"
