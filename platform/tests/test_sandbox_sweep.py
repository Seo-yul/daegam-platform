"""샌드박스 스윕과 Agent 해제·삭제의 정리 (P40 5절).

열기 경로로 샌드박스를 연 뒤 파드나 기록을 바꾸고 스윕의 한 틱을 앱의 루프에서 돌린다.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

from daegam.k8s.labels import LABEL_PLATFORM_ID
from daegam.k8s.sandbox_pods import pod_view
from daegam.k8s.templates.sandbox import LABEL_PROBE
from daegam.store.base import utcnow
from daegam.store.clusters import SandboxScheduling
from daegam.store.sandboxes import SLOTS_ID, slot_key
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.sandboxes import (
    AGENT_ID,
    CLUSTER,
    NS,
    OTHER_IMAGE_ID,
    auth,
    cluster,
    kubelet,
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


@pytest.fixture
def opened(client, mongo, fake):
    """세션 s1 의 샌드박스가 열린 클러스터. (클러스터, 파드)."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    r = client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "s1"}, headers=auth())
    assert r.status_code == 200, r.text
    [pod] = sandbox_pods(fake)
    return c, pod


def _sweep(client) -> None:
    client.portal.call(client.app.state.sandbox_sweep.tick)


def _rec(mongo, session: str = "s1") -> dict[str, Any]:
    return mongo["sandboxes"].find_one({"session_id": session}, sort=[("generation", -1)])


def _check(mongo, cluster_id: str) -> dict[str, Any]:
    return mongo["clusters"].find_one({"_id": cluster_id})["sandbox"]["check"]


def test_a_healthy_sandbox_stays(client, mongo, fake, opened):
    _sweep(client)
    assert _rec(mongo)["state"] == "open" and len(sandbox_pods(fake)) == 1


def test_a_gone_or_ended_pod_closes_its_record(client, mongo, fake, opened):
    _, pod = opened
    set_status(fake, pod, phase="Failed")
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "lost" and sandbox_pods(fake) == []


def test_three_restarts_close_and_one_oom_restart_keeps_the_files(client, mongo, fake, opened):
    _, pod = opened
    set_status(fake, pod, exit_code=137, reason="OOMKilled", restarts=1)
    _sweep(client)
    assert _rec(mongo)["state"] == "open"
    set_status(fake, pod, exit_code=137, reason="OOMKilled", restarts=3)
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "restarts"


def test_exit_87_voids_and_exit_88_only_ends_that_pod(client, mongo, fake, opened):
    c, pod = opened
    set_status(fake, pod, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=88)
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "later_check_failed"
    assert _check(mongo, c.id)["passed"] is True
    r = client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "s2"}, headers=auth())
    assert r.status_code == 200
    # 88 뒤의 프로브 파드(8절)가 함께 있다
    pod2 = next(
        p
        for p in sandbox_pods(fake)
        if p["metadata"]["name"] != pod["metadata"]["name"]
        and LABEL_PROBE not in p["metadata"]["labels"]
    )
    set_status(fake, pod2, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=87)
    _sweep(client)
    assert _rec(mongo, "s2")["state"] == "closed"
    check = _check(mongo, c.id)
    assert check["passed"] is False and check["void_reason"] == "self_check"


def test_the_message_of_a_pod_that_failed_its_first_check_is_logged(
    client, mongo, fake, opened, caplog
):
    """87 로 끝난 파드는 곧 지워진다. 무엇이 닿았는지는 그 종료 메시지뿐이라 기록에 남긴다."""
    _, pod = opened
    set_status(fake, pod, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=87)
    key = ("v1", "Pod", pod["metadata"]["namespace"], pod["metadata"]["name"])
    said = '{"check": "first", "result": "reached", "reached": ["tcp:10.43.0.1:443"]}'
    status = fake.objects[key]["status"]["containerStatuses"][0]
    terminated = (status.get("state") or {}).get("terminated") or status["lastState"]["terminated"]
    terminated["message"] = said + "\n"
    assert pod_view(fake.objects[key]).terminated_message == said
    with caplog.at_level(logging.WARNING, logger="daegam"):
        _sweep(client)
    assert any(said in r.getMessage() for r in caplog.records)


def test_idle_sandboxes_are_reclaimed_after_30_minutes(client, mongo, fake, opened):
    mongo["sandboxes"].update_one(
        {"session_id": "s1"}, {"$set": {"last_used_at": utcnow() - timedelta(minutes=29)}}
    )
    _sweep(client)
    assert _rec(mongo)["state"] == "open"
    mongo["sandboxes"].update_one(
        {"session_id": "s1"}, {"$set": {"last_used_at": utcnow() - timedelta(minutes=31)}}
    )
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "idle"
    events = [e["data"]["reason"] for e in mongo["agent_events"].find({"event": "sandbox_closed"})]
    assert events == ["idle"]


def test_a_verdict_that_reads_as_void_closes_the_clusters_sandboxes(client, mongo, fake, opened):
    c, _ = opened
    sched = SandboxScheduling(node_selector={"pool": "sandbox"}).model_dump()
    mongo["clusters"].update_one({"_id": c.id}, {"$set": {"sandbox.scheduling": sched}})
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "verdict_void"
    # 읽을 때의 무효라 판정에 쓰지 않는다
    assert _check(mongo, c.id)["voided_at"] is None


def test_a_pod_on_another_image_id_closes(client, mongo, fake, opened):
    _, pod = opened
    set_status(fake, pod, image_id=OTHER_IMAGE_ID)
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "verdict_void"


@pytest.mark.parametrize("state", ["undeploying", "deleting", None])
def test_an_undeployed_or_deleted_agents_sandboxes_close(client, mongo, fake, opened, state):
    if state is None:
        mongo["agents"].delete_one({"_id": AGENT_ID})
    else:
        mongo["agents"].update_one({"_id": AGENT_ID}, {"$set": {"state": state}})
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "undeployed" and sandbox_pods(fake) == []


def test_stray_pods_go_and_probes_and_other_installs_stay(client, mongo, fake, opened):
    _, pod = opened

    def copy(name: str, **labels: str) -> dict[str, Any]:
        meta = {**pod["metadata"], "name": name, "labels": {**pod["metadata"]["labels"], **labels}}
        return {**pod, "metadata": meta}

    fake.apply(copy("sbx-stray"))
    fake.apply(copy("sbx-probe-x", **{LABEL_PROBE: "true"}))
    fake.apply(copy("sbx-theirs", **{LABEL_PLATFORM_ID: "another-install"}))
    _sweep(client)
    names = sorted(p["metadata"]["name"] for p in sandbox_pods(fake))
    assert names == sorted([pod["metadata"]["name"], "sbx-probe-x", "sbx-theirs"])


def test_a_pod_whose_record_closed_is_deleted(client, mongo, fake, opened):
    mongo["sandboxes"].update_one({"session_id": "s1"}, {"$set": {"state": "closed"}})
    _sweep(client)
    assert sandbox_pods(fake) == []


def test_a_namespace_policy_failure_closes_that_namespaces_sandboxes(client, mongo, fake, opened):
    fake.delete("networking.k8s.io/v1", "NetworkPolicy", "daegam-sandbox-isolation", NS)
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "policy"


def test_the_sweep_recounts_slots_from_open_records(client, mongo, fake, opened):
    mongo["sandbox_slots"].update_one(
        {"_id": SLOTS_ID}, {"$set": {"nodes": {slot_key("bogus", "n1"): 7}, "agents": {}}}
    )
    _sweep(client)
    c, _ = opened
    slots = mongo["sandbox_slots"].find_one({"_id": SLOTS_ID})
    assert slots["nodes"] == {slot_key(c.id, "n1"): 1}
    assert slots["agents"] == {slot_key(AGENT_ID): 1}


def test_a_stuck_closing_record_is_finished(client, mongo, fake, opened):
    mongo["sandboxes"].update_one(
        {"session_id": "s1"},
        {"$set": {"state": "closing", "closed_at": utcnow() - timedelta(minutes=10)}},
    )
    _sweep(client)
    assert _rec(mongo)["state"] == "closed" and sandbox_pods(fake) == []


def test_a_stale_opening_record_closes(client, mongo, fake, opened):
    mongo["sandboxes"].update_one(
        {"session_id": "s1"},
        {"$set": {"ready_at": None, "opened_at": utcnow() - timedelta(minutes=10)}},
    )
    _, pod = opened
    set_status(fake, pod, ready=False, running=False, waiting="ContainerCreating")
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "open_stale"


def test_the_sweep_asks_for_a_probe_when_an_opening_pod_exits_87(client, mongo, fake):
    """열기가 기다리다 떠난 뒤에 87 로 끝난 여는 중의 파드도 판정을 바로 무효로 하지 않는다."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake, ready=False, running=False, waiting="ContainerCreating")
    r = client.post(
        f"/agents/{AGENT_ID}/sandboxes",
        json={"session_id": "s1", "wait_seconds": 0},
        headers=auth(),
    )
    assert r.status_code == 503
    [pod] = sandbox_pods(fake)
    set_status(fake, pod, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=87)
    _sweep(client)
    assert _rec(mongo)["close_reason"] == "failed_self_check"
    assert _check(mongo, c.id)["passed"] is True
    assert mongo["sandbox_probes"].find_one()["first_check"]["at"] is not None


def test_undeploy_and_delete_close_the_agents_sandboxes(client, mongo, fake, opened):
    service = client.app.state.agent_service
    doc = client.portal.call(client.app.state.agents.get, AGENT_ID)
    client.portal.call(lambda: service._teardown(doc, deleting=False))
    assert _rec(mongo)["close_reason"] == "undeployed" and sandbox_pods(fake) == []


# -- 회수와 닫기의 경계 --------------------------------------------------------------------------


def test_a_touch_after_the_sweeps_read_stops_the_idle_reclaim(client, mongo, fake, opened):
    """30분 동안 쓰이지 않았다는 조건은 닫는 쓰기에 든다."""
    mongo["sandboxes"].update_one(
        {"session_id": "s1"}, {"$set": {"last_used_at": utcnow() - timedelta(minutes=31)}}
    )
    store = client.app.state.sandboxes.store
    read = store.live

    async def read_then_touch(**kw):
        found = await read(**kw)
        await store.touch(AGENT_ID, "s1")
        return found

    store.live = read_then_touch
    _sweep(client)
    store.live = read
    assert _rec(mongo)["state"] == "open"


def test_a_runs_sandbox_left_after_its_run_ended_closes(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    mongo["agent_runs"].insert_one(
        {
            "_id": "run-1",
            "tenant": "acme",
            "agent_id": AGENT_ID,
            "source": "manual",
            "acts_as": {"sub": "alice"},
            "session_id": "run-s",
            "status": "running",
            "last_event_at": utcnow(),
            "created_at": utcnow(),
            "expires_at": utcnow() + timedelta(days=1),
        }
    )
    r = client.post(f"/agents/{AGENT_ID}/sandboxes", json={"session_id": "run-s"}, headers=auth())
    assert r.status_code == 200, r.text
    _sweep(client)
    assert _rec(mongo, "run-s")["state"] == "open"
    mongo["agent_runs"].update_one({"_id": "run-1"}, {"$set": {"status": "succeeded"}})
    _sweep(client)
    assert _rec(mongo, "run-s")["close_reason"] == "run_ended"
    assert mongo["sandbox_slots"].find_one({"_id": SLOTS_ID})["runs"] == {}


def test_the_sweep_records_a_pod_on_another_image_for_the_gate(client, mongo, fake, opened):
    c, pod = opened
    set_status(fake, pod, image_id=OTHER_IMAGE_ID)
    _sweep(client)
    assert _check(mongo, c.id)["seen_image_id"] == OTHER_IMAGE_ID


def test_a_gone_agents_sandbox_policy_is_deleted_and_a_deployed_ones_kept(
    client, mongo, fake, opened
):
    """내린 뒤 끝난 열기가 다시 둔 정책을 지운다. 배포된 Agent 의 정책은 열기가 기록을 넣기 전에
    두므로 남긴다."""
    policy = ("networking.k8s.io/v1", "NetworkPolicy", NS, f"daegam-sandbox-{AGENT_ID}")
    client.post(
        f"/agents/{AGENT_ID}/sandboxes/close",
        json={"session_id": "s1", "reason": "session_closed"},
        headers=auth(),
    )
    _sweep(client)
    assert policy in fake.objects
    mongo["agents"].update_one({"_id": AGENT_ID}, {"$set": {"state": "draft"}})
    _sweep(client)
    assert policy not in fake.objects
