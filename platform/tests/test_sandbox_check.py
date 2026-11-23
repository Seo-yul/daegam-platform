"""클러스터의 격리 점검과 허용, API 칸, shared_nodes, 사용자 코드 없는 프로브 (P40 8절).

대역 클러스터의 파드 훅이 kubelet 처럼 점검 파드마다 끝을 적는다. 양성 대조는 받은 대상 가운데
`reach` 가 고른 것에 닿았다고 찍는다.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import pytest

from daegam.k8s.templates.sandbox import (
    LABEL_PROBE,
    PROBE_NAMESPACE,
    TARGETS_ENV,
)
from daegam.services.sandbox_check import check_lease
from daegam.services.sandboxes import first_check_confirm
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


@pytest.fixture
def first_failed(client, mongo, fake):
    """여는 중인 샌드박스의 첫 점검이 87 로 끝난 클러스터. 판정은 그대로이고 프로브를 바란다."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    hook = kubelet(fake, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=87)
    r = client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "s1"}, headers=auth())
    assert detail_code(r) == (503, "sandbox_open_failed")
    fake.create_hooks.remove(hook)
    kubelet(fake)
    mongo["sandbox_probes"].update_one({}, {"$set": {"first_check.message": "reached tcp:x"}})
    assert _sandbox(mongo, c.id)["check"]["passed"] is True
    return c


def _end_probe(fake: FakeClusterClient, code: int) -> None:
    [probe] = _probes(fake)
    key = ("v1", "Pod", probe["metadata"]["namespace"], probe["metadata"]["name"])
    fake.objects[key]["status"] = {
        "phase": "Succeeded" if code == 0 else "Failed",
        "containerStatuses": [
            container_status(running=False, ready=False)
            | {"state": {"terminated": {"exitCode": code}}}
        ],
    }


def test_a_passed_probe_leaves_the_verdict_after_a_failed_first_check(
    client, mongo, fake, first_failed, caplog
):
    _tick(client)
    _end_probe(fake, 0)
    with caplog.at_level(logging.WARNING, logger="daegam"):
        _tick(client)
    assert _sandbox(mongo, first_failed.id)["check"]["passed"] is True
    assert mongo["sandbox_probes"].find_one()["result"] == "passed"
    assert any(
        "not confirmed" in r.getMessage() and "reached tcp:x" in r.getMessage()
        for r in caplog.records
    )
    # 게이트가 열려 있어 다시 열면 된다
    r = client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "s1"}, headers=auth())
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("code", [87, 86])
def test_a_probe_that_fails_or_cannot_run_voids_after_a_failed_first_check(
    client, mongo, fake, first_failed, code
):
    _tick(client)
    _end_probe(fake, code)
    _tick(client)
    check = _sandbox(mongo, first_failed.id)["check"]
    assert check["passed"] is False and check["void_reason"] == "self_check"
    assert _probes(fake) == []


@pytest.mark.parametrize("state", ["wanted", "pending", "gone"])
def test_a_first_check_no_probe_confirmed_in_time_voids(client, mongo, fake, first_failed, state):
    """87 을 확인하는 프로브는 끝없이 다시 하지 않는다. 시한을 넘기면 판정이 무효다."""
    if state != "wanted":
        _tick(client)
        [probe] = _probes(fake)
        key = ("v1", "Pod", probe["metadata"]["namespace"], probe["metadata"]["name"])
        if state == "pending":
            fake.objects[key]["status"] = {"phase": "Pending"}
        else:
            del fake.objects[key]
    mongo["sandbox_probes"].update_one(
        {}, {"$set": {"first_check.at": utcnow() - timedelta(minutes=6)}}
    )
    _tick(client)
    check = _sandbox(mongo, first_failed.id)["check"]
    assert check["passed"] is False and check["void_reason"] == "self_check"
    doc = mongo["sandbox_probes"].find_one()
    assert (doc["state"], doc["result"]) == ("done", "unconfirmed") and _probes(fake) == []


def test_a_probe_after_exit_88_does_not_inherit_an_earlier_first_check(
    client, mongo, fake, first_failed
):
    """끝난 확인의 표시는 다음 프로브로 넘어가지 않는다. 88 뒤의 프로브는 끝없이 다시 한다."""
    _tick(client)
    _end_probe(fake, 0)
    _tick(client)
    store = client.app.state.sandboxes.store
    assert client.portal.call(
        lambda: store.want_probe(first_failed.id, "n1", NS, "acme", quiet_since=utcnow())
    )
    assert mongo["sandbox_probes"].find_one()["first_check"] is None


def _mark(client, cluster_id: str) -> bool:
    """그 노드에서 여는 중의 샌드박스가 87 로 끝났다고 표시한다."""
    store = client.app.state.sandboxes.store
    return client.portal.call(
        lambda: store.confirm_first_check(
            cluster_id, "n1", NS, "acme", "late 87", quiet_since=utcnow() - timedelta(minutes=10)
        )
    )


def test_a_first_check_marked_after_the_sweep_read_the_probe_is_not_lost(
    client, mongo, fake, later_failed
):
    """스윕이 프로브 문서를 읽은 뒤에 붙은 87 의 표시도 프로브를 끝낼 때 본다. 그 프로브는 87
    보다 먼저 시작했으므로 답이 아니고, 표시를 둔 채 새 프로브를 바란다."""
    _tick(client)
    _end_probe(fake, 86)
    store = client.app.state.sandboxes.store
    read = store.probes_of

    async def stale(cluster_id: str):
        docs = await read(cluster_id)
        assert await store.confirm_first_check(
            cluster_id, "n1", NS, "acme", "late 87", quiet_since=utcnow() - timedelta(minutes=10)
        )
        return docs

    store.probes_of = stale
    try:
        _tick(client)
    finally:
        store.probes_of = read
    doc = mongo["sandbox_probes"].find_one()
    assert doc["state"] == "wanted" and doc["first_check"]["message"] == "late 87"
    assert _sandbox(mongo, later_failed.id)["check"]["passed"] is True
    # 새 프로브가 돌지 못하면 닫힌 쪽이다
    _tick(client)
    _end_probe(fake, 86)
    _tick(client)
    check = _sandbox(mongo, later_failed.id)["check"]
    assert check["passed"] is False and check["void_reason"] == "self_check"


@pytest.mark.parametrize("code", [0, 86])
def test_a_probe_that_ran_before_the_failed_first_check_does_not_answer_it(
    client, mongo, fake, later_failed, code
):
    _tick(client)
    assert _mark(client, later_failed.id)
    _end_probe(fake, code)
    _tick(client)
    doc = mongo["sandbox_probes"].find_one()
    assert doc["state"] == "wanted" and doc["first_check"]["at"] is not None
    assert _probes(fake) == [] and _sandbox(mongo, later_failed.id)["check"]["passed"] is True
    # 새 프로브가 통과해야 87 이 확인되지 않은 것이다
    _tick(client)
    _end_probe(fake, 0)
    _tick(client)
    doc = mongo["sandbox_probes"].find_one()
    assert (doc["state"], doc["result"], doc["first_check"]) == ("done", "passed", None)
    assert _sandbox(mongo, later_failed.id)["check"]["passed"] is True


def test_a_running_probe_that_exits_87_voids_whenever_it_started(client, mongo, fake, later_failed):
    _tick(client)
    assert _mark(client, later_failed.id)
    _end_probe(fake, 87)
    _tick(client)
    check = _sandbox(mongo, later_failed.id)["check"]
    assert check["passed"] is False and check["void_reason"] == "self_check"


def test_a_second_87_while_the_probe_runs_asks_for_another_probe(client, mongo, fake, first_failed):
    """프로브가 뜬 뒤의 87 은 그 프로브가 답하지 못한다. 시한은 첫 87 부터 잰다."""
    _tick(client)
    at = mongo["sandbox_probes"].find_one()["first_check"]["at"]
    assert _mark(client, first_failed.id)
    _end_probe(fake, 0)
    _tick(client)
    doc = mongo["sandbox_probes"].find_one()
    assert doc["state"] == "wanted" and doc["first_check"]["at"] == at
    assert doc["first_check"]["last_at"] > at


def test_a_late_first_check_voids_while_the_cluster_is_unreachable(
    client, mongo, fake, first_failed, monkeypatch
):
    """시한은 Mongo 만으로 본다. 클러스터에 닿지 않는 스윕도 게이트를 연 채 두지 않는다."""
    _tick(client)

    async def unreachable(cluster):
        raise RuntimeError("no route")

    monkeypatch.setattr(client.app.state.sandboxes, "client_for", unreachable)
    _tick(client)
    assert _sandbox(mongo, first_failed.id)["check"]["passed"] is True
    mongo["sandbox_probes"].update_one(
        {}, {"$set": {"first_check.at": utcnow() - timedelta(minutes=6)}}
    )
    _tick(client)
    check = _sandbox(mongo, first_failed.id)["check"]
    assert check["passed"] is False and check["void_reason"] == "self_check"
    doc = mongo["sandbox_probes"].find_one()
    assert (doc["state"], doc["result"]) == ("done", "unconfirmed")
    # 닿게 되면 남은 프로브 파드를 지운다
    monkeypatch.undo()
    assert len(_probes(fake)) == 1
    _tick(client)
    assert _probes(fake) == [] and mongo["sandbox_probes"].find_one()["orphan_pod"] is None


def test_a_probe_that_cannot_start_does_not_stop_the_other_probes(
    client, mongo, fake, first_failed, monkeypatch
):
    store = client.app.state.sandboxes.store
    assert client.portal.call(
        lambda: store.want_probe(first_failed.id, "a-gone", NS, "acme", quiet_since=utcnow())
    )
    checks = client.app.state.sandbox_checks
    start = checks._start_probe

    async def broken(cluster, kube, doc):
        if doc["node"] == "a-gone":
            raise RuntimeError("boom")
        await start(cluster, kube, doc)

    monkeypatch.setattr(checks, "_start_probe", broken)
    _tick(client)
    states = {d["node"]: d["state"] for d in mongo["sandbox_probes"].find()}
    assert states == {"a-gone": "wanted", "n1": "running"}


def test_wanting_a_probe_does_not_clear_a_first_check_marked_meanwhile(
    client, mongo, fake, first_failed, monkeypatch
):
    """`want_probe` 가 읽고 쓰는 사이에 87 의 표시가 붙어도 그 표시는 남는다."""
    store = client.app.state.sandboxes.store
    find_one = store.probes.find_one

    async def stale(*args, **kwargs):
        return None

    monkeypatch.setattr(store.probes, "find_one", stale)
    assert client.portal.call(
        lambda: store.want_probe(first_failed.id, "n1", NS, "acme", quiet_since=utcnow())
    )
    monkeypatch.setattr(store.probes, "find_one", find_one)
    doc = mongo["sandbox_probes"].find_one()
    assert doc["state"] == "wanted" and doc["first_check"]["at"] is not None


def test_the_confirm_window_follows_the_sweep_interval_within_bounds():
    assert first_check_confirm(60) == timedelta(minutes=5)
    assert first_check_confirm(10) == timedelta(minutes=5)
    assert first_check_confirm(300) == timedelta(minutes=20)
    assert first_check_confirm(3600) == timedelta(minutes=60)


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
