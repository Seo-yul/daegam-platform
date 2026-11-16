"""코드 실행 샌드박스의 열기·찾기·쓰기 표시·닫기 (P40 5절·7절).

런타임의 기계 경로를 인스턴스 토큰으로 부른다. 클러스터는 대역(fake)이고, 파드 훅이 kubelet 처럼
샌드박스 파드에 status 를 적는다(`tests/helpers/sandboxes.py`).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from kubernetes.client import ApiException
from kubernetes.dynamic.exceptions import ForbiddenError

from daegam.k8s.labels import LABEL_AGENT
from daegam.k8s.templates.sandbox import (
    TARGETS_ENV,
    TOKEN_SHA_ENV,
    UNCHECKED_ENV,
)
from daegam.services.sandboxes import code_execution_on
from daegam.store.base import utcnow
from daegam.store.sandboxes import SLOTS_ID, slot_key
from tests.helpers.docs import agent_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.sandboxes import (
    AGENT_ID,
    CLUSTER,
    IMAGE_ID,
    NS,
    OTHER_IMAGE_ID,
    POD_IP,
    auth,
    cluster,
    code_exec_spec,
    kubelet,
    node,
    sandbox_pods,
    seed_agent,
    seed_cluster,
)

OPEN = f"/agents/{AGENT_ID}/sandboxes"


@pytest.fixture
def fake(fake_clusters) -> FakeClusterClient:
    client = FakeClusterClient()
    fake_clusters.by_name[CLUSTER] = client
    return client


def _open(client, session: str = "s1", *, agent: str = AGENT_ID, **body: Any):
    return client.post(
        f"/agents/{agent}/sandboxes", json={"session_id": session, **body}, headers=auth()
    )


def _env(pod: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}


def _record(mongo, session: str = "s1") -> dict[str, Any]:
    return mongo["sandboxes"].find_one({"session_id": session}, sort=[("generation", -1)])


def _slots(mongo) -> dict[str, Any]:
    return mongo["sandbox_slots"].find_one({"_id": SLOTS_ID}) or {}


def test_open_creates_one_pinned_pod_and_answers_the_same_sandbox(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    r = _open(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["address"] == f"http://{POD_IP}:8080" and body["ready"] is True
    assert body["token"] and body["generation"] == 1 and body["unchecked"] is False
    [pod] = sandbox_pods(fake)
    env = _env(pod)
    # 파드에는 토큰이 아니라 그 sha256 만 간다
    assert body["token"] not in json.dumps(pod) and len(env[TOKEN_SHA_ENV]) == 64
    assert UNCHECKED_ENV not in env
    affinity = pod["spec"]["affinity"]["nodeAffinity"]
    term = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]
    assert term["matchExpressions"][0]["values"] == ["n1"]
    assert pod["metadata"]["ownerReferences"][0]["uid"] == f"uid-{AGENT_ID}"
    rec = _record(mongo)
    assert rec["state"] == "open" and rec["node"] == "n1" and rec["image_id"] == IMAGE_ID
    # 같은 세션의 두 번째 열기는 같은 샌드박스다
    again = _open(client)
    assert again.json()["sandbox_id"] == body["sandbox_id"] and len(sandbox_pods(fake)) == 1
    # Agent 의 샌드박스 정책을 두었다
    assert ("networking.k8s.io/v1", "NetworkPolicy", NS, f"daegam-sandbox-{AGENT_ID}") in (
        fake.objects
    )
    events = list(mongo["agent_events"].find({"event": "sandbox_opened"}))
    assert len(events) == 1 and events[0]["session_id"] == "s1"


def test_targets_cover_dns_api_metadata_and_node_ports(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    assert _open(client).status_code == 200
    targets = json.loads(_env(sandbox_pods(fake)[0])[TARGETS_ENV])
    keys = {(t["kind"], t["host"], t.get("port")) for t in targets}
    assert ("dns", "10.43.0.10", 53) in keys and ("tcp", "10.43.0.1", 443) in keys
    assert ("tcp", "169.254.169.254", 80) in keys and ("tcp", "10.0.0.1", 10250) in keys
    assert ("icmp", "1.1.1.1", None) in keys
    # 서버는 kind·host·port·timeout 밖의 칸을 거절한다
    assert all(set(t) <= {"kind", "host", "port"} for t in targets)


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda: cluster(verdict=False), "unchecked"),
        (lambda: cluster(rights=False), "rights"),
        (lambda: cluster(shared=True), "unaccepted"),
    ],
)
def test_a_closed_gate_answers_409_with_its_reason(client, mongo, fake, make, reason):
    c = seed_cluster(mongo, fake, make())
    seed_agent(mongo, c)
    r = _open(client)
    assert detail_code(r) == (409, "sandbox_gate_closed") and r.json()["detail"]["reason"] == reason
    assert sandbox_pods(fake) == []


def test_shared_nodes_with_acceptance_open(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(shared=True, accepted=True))
    seed_agent(mongo, c)
    kubelet(fake)
    assert _open(client).status_code == 200


def test_no_memory_for_one_sandbox_closes_the_gate_with_capacity(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(), node(memory="3Gi"))
    seed_agent(mongo, c)
    r = _open(client)
    assert detail_code(r) == (409, "sandbox_gate_closed")
    assert r.json()["detail"]["reason"] == "capacity"


def test_a_full_cluster_keeps_its_gate_open_and_refuses_with_cluster(client, mongo, fake):
    """5 GiB 노드는 2 GiB 예비를 빼면 2 GiB 샌드박스 하나가 든다."""
    c = seed_cluster(mongo, fake, cluster(), node(memory="5Gi"))
    seed_agent(mongo, c)
    kubelet(fake)
    assert _open(client, "s1").status_code == 200
    r = _open(client, "s2")
    assert detail_code(r) == (429, "sandbox_limit") and r.json()["detail"]["cap"] == "cluster"
    # 빈 슬롯이 있는 노드가 없으면 기록을 넣기 전에 거절한다
    assert _record(mongo, "s2") is None


def test_feature_off_or_undeploying_refuse(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c, on=None)
    assert detail_code(_open(client)) == (409, "sandbox_not_enabled")
    mongo["agents"].update_one({"_id": AGENT_ID}, {"$set": {"state": "undeploying"}})
    assert detail_code(_open(client)) == (409, "not_deployed")


def test_code_execution_shape():
    """열기는 그래프의 칸(`AgentData.code_execution`)을 읽는다. 칸이 있으면 켠 것이고 형식이 틀린
    노드는 끈 것이다(9절)."""
    assert code_execution_on(agent_doc("a", spec=code_exec_spec(True), state="deployed"))
    asks = code_exec_spec({"approval": ["approve", "reject"]})
    assert code_execution_on(agent_doc("a", spec=asks, state="deployed"))
    assert not code_execution_on(agent_doc("a", spec=code_exec_spec(None), state="deployed"))
    for wrong in ({"enabled": False}, False, {"approval": ["approve", "approve"]}):
        off = code_exec_spec(wrong)
        assert not code_execution_on(agent_doc("a", spec=off, state="deployed"))
    # 배포하지 않은 문서는 핀한 리비전이 없다
    assert not code_execution_on(agent_doc("a", spec=code_exec_spec(True)))


def test_a_wrong_instance_token_is_401(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    r = client.post(OPEN, json={"session_id": "s1"}, headers=auth("wrong"))
    assert r.status_code == 401


def test_an_extra_allow_all_policy_refuses_in_its_namespace_only(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    fake.apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-all", "namespace": NS},
            "spec": {"podSelector": {}, "ingress": [{}], "egress": [{}]},
        }
    )
    r = _open(client)
    assert detail_code(r) == (503, "sandbox_namespace_policy")
    assert r.json()["detail"]["policy"] == "allow-all"
    # 판정은 무효로 하지 않는다
    assert mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]["passed"] is True


def test_exit_87_voids_the_verdict_by_a_write(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake, ready=False, running=False, waiting="CrashLoopBackOff", exit_code=87)
    r = _open(client)
    assert detail_code(r) == (503, "sandbox_open_failed") and r.json()["detail"]["reason"] == (
        "self_check"
    )
    check = mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]
    assert check["passed"] is False and check["void_reason"] == "self_check"
    assert sandbox_pods(fake) == [] and _record(mongo)["state"] == "closed"


@pytest.mark.parametrize(
    ("container", "reason"),
    [
        (
            {"ready": False, "running": False, "waiting": "CrashLoopBackOff", "exit_code": 86},
            "check_could_not_run",
        ),
        (
            {
                "ready": False,
                "running": True,
                "exit_code": 137,
                "reason": "OOMKilled",
                "restarts": 1,
            },
            "oom",
        ),
        ({"ready": False, "running": False, "waiting": "CreateContainerError"}, "container_error"),
    ],
)
def test_other_failures_fail_the_open_and_void_nothing(client, mongo, fake, container, reason):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake, **container)
    r = _open(client)
    assert detail_code(r) == (503, "sandbox_open_failed") and r.json()["detail"]["reason"] == reason
    assert mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]["passed"] is True


def test_a_pending_pod_answers_starting_and_the_next_open_waits_for_it(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake, ready=False, running=False, waiting="ContainerCreating")
    r = _open(client, wait_seconds=0)
    assert detail_code(r) == (503, "sandbox_starting")
    assert mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]["passed"] is True
    rec = _record(mongo)
    assert rec["state"] == "open" and rec["ready_at"] is None
    [pod] = sandbox_pods(fake)
    from tests.helpers.sandboxes import set_status

    set_status(fake, pod)
    r = _open(client)
    assert r.status_code == 200 and r.json()["sandbox_id"] == rec["_id"]


def test_an_image_id_other_than_the_verdicts_reads_as_void(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake, image_id=OTHER_IMAGE_ID)
    r = _open(client)
    assert detail_code(r) == (409, "sandbox_gate_closed") and r.json()["detail"]["reason"] == (
        "unchecked"
    )
    assert sandbox_pods(fake) == [] and _record(mongo)["close_reason"] == "image_mismatch"
    # 읽을 때의 무효라 쓰기가 없다
    assert mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]["voided_at"] is None


def test_dropped_host_users_deletes_the_pod(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    fake.drop_host_users = True
    r = _open(client)
    assert detail_code(r) == (503, "sandbox_open_failed") and r.json()["detail"]["reason"] == (
        "host_users"
    )
    assert sandbox_pods(fake) == []


def _quota_refusal() -> Exception:
    """API 서버가 ResourceQuota 로 파드를 거절한 답. 동적 클라이언트가 올리는 꼴 그대로다."""
    status = {
        "kind": "Status",
        "status": "Failure",
        "message": 'pods "sbx-x" is forbidden: exceeded quota: q, requested: memory=2Gi',
        "reason": "Forbidden",
        "code": 403,
    }
    e = ApiException(status=403, reason="Forbidden")
    e.body = json.dumps(status)
    e.headers = {"Content-Type": "application/json", "Audit-Id": "secret-audit-id"}
    return ForbiddenError(e)


def test_a_quota_refusal_answers_503_with_the_status_message_only(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    fake.pod_create_error = _quota_refusal()
    r = _open(client)
    detail = r.json()["detail"]
    assert detail_code(r) == (503, "sandbox_open_failed") and detail["reason"] == "refused"
    assert "exceeded quota: q" in detail["detail"]
    # HTTP 머리와 본문은 런타임과 모델에게 가지 않는다
    assert "Audit-Id" not in detail["detail"] and "HTTP response" not in detail["detail"]
    assert _record(mongo)["state"] == "closed"
    # 돌려준 슬롯이 남지 않는다
    assert all(v == 0 for v in (_slots(mongo).get("nodes") or {}).values())


def test_lookup_answers_restarts_and_404_after_close(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    opened = _open(client).json()
    [pod] = sandbox_pods(fake)
    from tests.helpers.sandboxes import set_status

    set_status(fake, pod, exit_code=137, reason="OOMKilled", restarts=1)
    r = client.get(OPEN, params={"session_id": "s1"}, headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["sandbox_id"] == opened["sandbox_id"] and r.json()["restarts"] == 1
    assert r.json()["last_termination"] == {"reason": "OOMKilled", "exit_code": 137}
    closed = client.post(
        f"{OPEN}/close", json={"session_id": "s1", "reason": "session_closed"}, headers=auth()
    )
    assert closed.json() == {"closed": True} and sandbox_pods(fake) == []
    r = client.get(OPEN, params={"session_id": "s1"}, headers=auth())
    assert detail_code(r) == (404, "sandbox_not_found")
    assert r.json()["detail"]["closed_reason"] == "session_closed"
    events = [e["data"]["reason"] for e in mongo["agent_events"].find({"event": "sandbox_closed"})]
    assert events == ["session_closed"]
    # 닫은 세션의 다음 열기는 새 세대다
    assert _open(client).json()["generation"] == 2


def test_lookup_of_a_gone_pod_closes_it_as_lost(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    _open(client)
    [pod] = sandbox_pods(fake)
    fake.delete("v1", "Pod", pod["metadata"]["name"], NS)
    r = client.get(OPEN, params={"session_id": "s1"}, headers=auth())
    assert detail_code(r) == (404, "sandbox_not_found") and r.json()["detail"]["closed_reason"] == (
        "lost"
    )


def test_touch_moves_last_used_and_tells_a_closed_record_at_once(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    _open(client)
    old = utcnow() - timedelta(minutes=20)
    mongo["sandboxes"].update_one({"session_id": "s1"}, {"$set": {"last_used_at": old}})
    r = client.post(f"{OPEN}/touch", json={"session_id": "s1"}, headers=auth())
    assert r.status_code == 200
    assert _record(mongo)["last_used_at"] > old
    r = client.post(f"{OPEN}/touch", json={"session_id": "s9"}, headers=auth())
    assert detail_code(r) == (404, "sandbox_not_found")


def _own(mongo, session: str, person: str) -> None:
    now = utcnow()
    mongo["agent_session_owners"].insert_one(
        {
            "_id": f"{AGENT_ID}/{session}",
            "agent_id": AGENT_ID,
            "session_id": session,
            "sub": person,
            "confirmed_at": now,
            "last_turn_at": now,
            "expires_at": now + timedelta(days=1),
        }
    )


def test_the_persons_cap_reclaims_their_least_recently_used_sandbox(
    settings, mongo, fake_clusters, fake_secrets, fake
):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    for s in ("s1", "s2", "s3", "s4"):
        _own(mongo, s, "alice")
    with api_client(make_app(settings, fake_clusters, fake_secrets, sandbox_per_person=2)) as cl:
        assert _open(cl, "s1").status_code == 200
        assert _open(cl, "s2").status_code == 200
        # 둘 다 방금 쓰였다. 회수할 것이 없다
        r = _open(cl, "s3")
        assert detail_code(r) == (429, "sandbox_limit") and r.json()["detail"]["cap"] == "person"
        old = utcnow() - timedelta(minutes=10)
        mongo["sandboxes"].update_one(
            {"session_id": "s1", "state": "open"}, {"$set": {"last_used_at": old}}
        )
        assert _open(cl, "s4").status_code == 200
    assert _record(mongo, "s1")["close_reason"] == "reclaimed"
    assert _record(mongo, "s2")["state"] == "open"
    person_counts = _slots(mongo)["persons"]
    assert person_counts[slot_key("alice")] == 2


def test_agent_and_tenant_caps_name_themselves(settings, mongo, fake_clusters, fake_secrets, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    with api_client(make_app(settings, fake_clusters, fake_secrets, sandbox_per_agent=1)) as cl:
        assert _open(cl, "s1").status_code == 200
        r = _open(cl, "s2")
        assert r.json()["detail"]["cap"] == "agent"
    with api_client(make_app(settings, fake_clusters, fake_secrets, sandbox_per_tenant=1)) as cl:
        r = _open(cl, "s3")
        assert r.json()["detail"]["cap"] == "tenant"


def test_a_runs_session_uses_the_run_bucket_not_the_persons(
    settings, mongo, fake_clusters, fake_secrets, fake
):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    _own(mongo, "run-s", "alice")
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
    over = {"sandbox_per_person": 0, "sandbox_runs_per_agent": 1}
    with api_client(make_app(settings, fake_clusters, fake_secrets, **over)) as cl:
        assert _open(cl, "run-s").status_code == 200
    rec = _record(mongo, "run-s")
    assert rec["run_id"] == "run-1" and rec["person"] == "alice"
    assert _slots(mongo)["runs"][slot_key(AGENT_ID)] == 1
    assert not (_slots(mongo).get("persons") or {})


def test_the_route_bucket_is_per_agent(client, mongo, fake):
    """한 Agent 가 버킷을 비워도 다른 Agent 는 막히지 않는다."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    seed_agent(mongo, c, aid="ag-two", token="tok-two")
    codes = {
        client.post(f"{OPEN}/touch", json={"session_id": "s"}, headers=auth()).status_code
        for _ in range(70)
    }
    assert codes == {404, 429}
    other = client.post(
        "/agents/ag-two/sandboxes/touch", json={"session_id": "s"}, headers=auth("tok-two")
    )
    assert detail_code(other) == (404, "sandbox_not_found")


def test_the_override_opens_unchecked_on_home_only(settings, mongo, fake_clusters, fake_secrets):
    home = FakeClusterClient()
    fake_clusters.by_name["home"] = home
    c = seed_cluster(mongo, home, cluster("home", verdict=False))
    seed_agent(mongo, c)
    kubelet(home)
    with api_client(
        make_app(settings, fake_clusters, fake_secrets, sandbox_allow_unchecked=True)
    ) as cl:
        r = _open(cl)
        assert r.status_code == 200, r.text
        assert r.json()["unchecked"] is True
        assert _env(sandbox_pods(home)[0])[UNCHECKED_ENV] == "true"
        assert _record(mongo)["unchecked"] is True
        # home 밖에는 들지 않는다
        edge = FakeClusterClient()
        fake_clusters.by_name[CLUSTER] = edge
        e = seed_cluster(mongo, edge, cluster(verdict=False))
        seed_agent(mongo, e, aid="ag-edge", token="tok-edge")
        r = cl.post("/agents/ag-edge/sandboxes", json={"session_id": "x"}, headers=auth("tok-edge"))
        assert r.json()["detail"]["reason"] == "unchecked"


def test_close_agent_closes_every_sandbox_and_sweeps_its_pods(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    _open(client, "s1")
    _open(client, "s2")
    # 기록 없이 남은 그 Agent 의 샌드박스 파드와 다른 Agent 의 것
    stray = dict(sandbox_pods(fake)[0])
    stray = {**stray, "metadata": {**stray["metadata"], "name": "sbx-stray"}}
    fake.apply(stray)
    other = {**stray, "metadata": {**stray["metadata"], "name": "sbx-other"}}
    other["metadata"]["labels"] = {**other["metadata"]["labels"], LABEL_AGENT: "ag-other"}
    fake.apply(other)
    service = client.app.state.sandboxes
    doc = client.portal.call(client.app.state.agents.get, AGENT_ID)
    assert client.portal.call(service.close_agent, doc, "undeployed") == 2
    assert [p["metadata"]["name"] for p in sandbox_pods(fake)] == ["sbx-other"]
    assert {r["close_reason"] for r in mongo["sandboxes"].find()} == {"undeployed"}
    assert all(v == 0 for v in (_slots(mongo).get("agents") or {}).values())


def test_the_token_key_loads_at_start(client, fake_secrets):
    """기동이 home 클러스터 Secret 저장소에서 샌드박스 토큰의 키를 읽거나 만든다."""
    from daegam.auth.sandbox_key import SANDBOX_KEY_SECRET
    from tests.helpers.consts import PLATFORM_NS

    assert fake_secrets.get(SANDBOX_KEY_SECRET, namespace=PLATFORM_NS)
    assert client.app.state.sandboxes.key is not None


# -- 판정 뒤에 바뀐 이미지 -----------------------------------------------------------------------


def _pods_created(fake: FakeClusterClient) -> int:
    return sum(1 for o in fake.applied if o.get("kind") == "Pod")


def test_a_seen_image_id_closes_the_gate_before_a_pod_is_made(client, mongo, fake):
    """판정과 다른 이미지 id 를 본 뒤의 열기는 2 GiB 파드를 만들지 않고 409 다."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake, image_id=OTHER_IMAGE_ID)
    assert _open(client, "s1").json()["detail"]["reason"] == "unchecked"
    assert mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]["seen_image_id"] == (
        OTHER_IMAGE_ID
    )
    made = _pods_created(fake)
    r = _open(client, "s2")
    assert detail_code(r) == (409, "sandbox_gate_closed") and r.json()["detail"]["reason"] == (
        "unchecked"
    )
    assert _pods_created(fake) == made
    # 판정은 쓰기로 무효가 되지 않았다
    assert mongo["clusters"].find_one({"_id": c.id})["sandbox"]["check"]["passed"] is True


def test_under_the_override_a_moved_image_opens_unchecked(
    settings, mongo, fake_clusters, fake_secrets
):
    home = FakeClusterClient()
    fake_clusters.by_name["home"] = home
    c = seed_cluster(mongo, home, cluster("home"))
    seed_agent(mongo, c)
    kubelet(home, image_id=OTHER_IMAGE_ID)
    over = {"sandbox_allow_unchecked": True}
    with api_client(make_app(settings, fake_clusters, fake_secrets, **over)) as cl:
        r = _open(cl, "s1")
        assert r.status_code == 200, r.text
        assert r.json()["unchecked"] is True and _record(mongo, "s1")["unchecked"] is True
        # 다음 열기는 처음부터 점검 없이 연다
        r = _open(cl, "s2")
        assert r.status_code == 200, r.text
        assert _env(sandbox_pods(home)[-1])[UNCHECKED_ENV] == "true"


def test_without_node_rights_the_open_and_status_say_rights(client, mongo, fake):
    """새 RBAC 를 적용하지 않은 원격 클러스터. 노드 목록이 403 이면 500 이 아니라 rights 다."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    fake.denied.add(("", "nodes", "list"))
    r = _open(client)
    assert detail_code(r) == (409, "sandbox_gate_closed") and r.json()["detail"]["reason"] == (
        "rights"
    )
    status = client.get(f"/clusters/{c.id}/sandbox")
    assert status.status_code == 200, status.text
    assert status.json()["gate"] == {"open": False, "reason": "rights"}


def test_without_the_rights_gate_nothing_is_listed(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster(rights=False))
    seed_agent(mongo, c)
    fake.unreachable = True
    r = _open(client)
    assert detail_code(r) == (409, "sandbox_gate_closed") and r.json()["detail"]["reason"] == (
        "rights"
    )
    assert client.get(f"/clusters/{c.id}/sandbox").json()["gate"]["reason"] == "rights"


def test_an_open_record_is_not_answered_after_its_verdict_reads_as_void(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    first = _open(client).json()
    voided = {"sandbox.check.voided_at": utcnow(), "sandbox.check.passed": False}
    mongo["clusters"].update_one({"_id": c.id}, {"$set": voided})
    r = _open(client)
    assert detail_code(r) == (409, "sandbox_gate_closed")
    assert "token" not in r.json()["detail"]
    rec = mongo["sandboxes"].find_one({"_id": first["sandbox_id"]})
    assert rec["state"] == "closed" and rec["close_reason"] == "verdict_void"
    r = client.get(OPEN, params={"session_id": "s1"}, headers=auth())
    assert detail_code(r) == (404, "sandbox_not_found")


def test_lookup_does_not_answer_a_record_whose_verdict_reads_as_void(client, mongo, fake):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    _open(client)
    mongo["clusters"].update_one({"_id": c.id}, {"$set": {"sandbox.image": "other:1"}})
    r = client.get(OPEN, params={"session_id": "s1"}, headers=auth())
    assert detail_code(r) == (404, "sandbox_not_found")
    assert r.json()["detail"]["closed_reason"] == "verdict_void"


def test_no_reclaim_when_another_cap_refuses_too(
    settings, mongo, fake_clusters, fake_secrets, fake
):
    """사람의 몫과 Agent 의 몫이 함께 찼으면 회수해도 거절이다. 다른 Agent 의 샌드박스를 잃지
    않는다."""
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    seed_agent(mongo, c, aid="ag-b", token="tok-b")
    kubelet(fake)
    _own(mongo, "bob-s", "bob")
    _own(mongo, "alice-s", "alice")
    mongo["agent_session_owners"].insert_one(
        {
            "_id": "ag-b/alice-b",
            "agent_id": "ag-b",
            "session_id": "alice-b",
            "sub": "alice",
            "confirmed_at": utcnow(),
            "last_turn_at": utcnow(),
            "expires_at": utcnow() + timedelta(days=1),
        }
    )
    over = {"sandbox_per_person": 1, "sandbox_per_agent": 1}
    with api_client(make_app(settings, fake_clusters, fake_secrets, **over)) as cl:
        b = cl.post("/agents/ag-b/sandboxes", json={"session_id": "alice-b"}, headers=auth("tok-b"))
        assert b.status_code == 200, b.text
        assert _open(cl, "bob-s").status_code == 200
        old = utcnow() - timedelta(minutes=10)
        mongo["sandboxes"].update_one({"session_id": "alice-b"}, {"$set": {"last_used_at": old}})
        r = _open(cl, "alice-s")
        # 거절은 차례의 첫 상한을 싣는다
        assert detail_code(r) == (429, "sandbox_limit") and r.json()["detail"]["cap"] == "person"
    assert _record(mongo, "alice-b")["state"] == "open"


def test_a_touch_after_the_read_stops_the_persons_reclaim(
    settings, mongo, fake_clusters, fake_secrets, fake
):
    c = seed_cluster(mongo, fake, cluster())
    seed_agent(mongo, c)
    kubelet(fake)
    for s in ("s1", "s2"):
        _own(mongo, s, "alice")
    with api_client(make_app(settings, fake_clusters, fake_secrets, sandbox_per_person=1)) as cl:
        assert _open(cl, "s1").status_code == 200
        old = utcnow() - timedelta(minutes=10)
        mongo["sandboxes"].update_one({"session_id": "s1"}, {"$set": {"last_used_at": old}})
        store = cl.app.state.sandboxes.store
        read = store.reclaimable

        async def read_then_touch(person, before):
            found = await read(person, before)
            # 회수할 기록을 읽은 바로 뒤에 그 세션의 턴이 쓰기를 알린다
            await store.touch(AGENT_ID, "s1")
            return found

        store.reclaimable = read_then_touch
        r = _open(cl, "s2")
        assert detail_code(r) == (429, "sandbox_limit") and r.json()["detail"]["cap"] == "person"
    assert _record(mongo, "s1")["state"] == "open"
