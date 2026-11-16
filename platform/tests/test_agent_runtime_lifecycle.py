"""P22 F2: 배포한 Agent 의 상태와 재배포·삭제 (D42, D43, D44).

Deployment 상태 매핑(롤아웃 중 옛 파드, 멈춘 롤아웃의 그레이스), status 의 런타임 수치·경고·인사말과
뜨지 못하는 파드, 재배포의 키 재복사·핀 이동·경고, 라벨로 잔재까지 지우는 삭제, 클러스터를 부르지
않는 가벼운 목록(D35 F2)을 든다. 해제·삭제가 장기 기억을 지우거나 남기는 것은
test_agent_long_term.py 가 본다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

import daegam.services.agents as agents_module
from daegam.services.agent_status import map_deployment_status
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import (
    agent_deployment,
    deployed_agent,
    env_map,
    set_ready,
    token_of,
)
from tests.helpers.agents import (
    agent_body,
    anthropic_for,
    anthropic_with_openai_fallback,
    create_agent,
    node_of,
)
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, LLM_KEY_SECRET, PLATFORM_NS
from tests.helpers.http import detail_code
from tests.helpers.runtime import drop_route, fake_runtime

# -- status ----------------------------------------------------------------------------------


def test_map_deployment_status():
    now = utcnow()

    def dep(status, generation=1, replicas=1):
        return {
            "metadata": {"generation": generation},
            "spec": {"replicas": replicas},
            "status": status,
        }

    assert map_deployment_status(dep({}), None, now)["phase"] == "Pending"
    ready = {"observedGeneration": 1, "replicas": 1, "readyReplicas": 1, "updatedReplicas": 1}
    assert map_deployment_status(dep(ready), None, now) == {
        "phase": "Ready",
        "message": None,
        "ready_replicas": 1,
        "conditions": [],
    }
    rolling = map_deployment_status(dep(ready, generation=2), None, now)
    assert rolling["phase"] == "Rolling" and "generation 2" in rolling["message"]
    # 조건의 전이 시각을 그대로 옮긴다. 화면이 Ready 조건의 그것으로 "떠 있은 지" 를 낸다 (D44 셋째)
    timed = {
        **ready,
        "conditions": [
            {"type": "Available", "status": "True", "lastTransitionTime": "2026-09-16T01:02:03Z"},
            {"type": "Progressing", "status": "True"},
        ],
    }
    conds = map_deployment_status(dep(timed), None, now)["conditions"]
    assert [c["last_transition"] for c in conds] == ["2026-09-16T01:02:03Z", None]
    # 원하는 레플리카가 둘인데 하나만 Ready 면 아직 롤아웃 중이다
    assert map_deployment_status(dep(ready, replicas=2), None, now)["phase"] == "Rolling"
    failed = {
        "observedGeneration": 1,
        "conditions": [{"type": "ReplicaFailure", "status": "True", "message": "quota exceeded"}],
    }
    out = map_deployment_status(dep(failed), None, now)
    assert out["phase"] == "Failed" and out["message"] == "quota exceeded"
    stuck = {
        "observedGeneration": 1,
        "conditions": [
            {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
        ],
    }
    assert map_deployment_status(dep(stuck), now - timedelta(minutes=5), now)["phase"] == "Failed"
    assert map_deployment_status(dep(stuck), now, now)["phase"] == "Rolling"
    waiting = {
        "observedGeneration": 1,
        "conditions": [
            {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}
        ],
    }
    out = map_deployment_status(dep(waiting), None, now)
    assert out["phase"] == "Pending" and out["message"] == "MinimumReplicasUnavailable"
    # conditions 는 그대로 옮긴다. 화면이 MCP Server 와 같은 표로 보인다
    assert out["conditions"] == [
        {
            "type": "Available",
            "status": "False",
            "reason": "MinimumReplicasUnavailable",
            "message": None,
            "last_transition": None,
        }
    ]


def test_ready_waits_until_no_old_pod_remains():
    """Ready 는 롤아웃이 끝난 뒤다. 새 파드가 준비됐어도 옛 ReplicaSet 의 파드가 남아 있으면 그
    파드가 옛 설정으로 답하므로 Rolling 이다. `status.replicas` 는 종료 중이 아닌 파드만 세므로 옛
    파드가 내려가기 시작하면 Ready 다."""
    now = utcnow()

    def phase(wanted: int, *, running: int, ready: int, updated: int) -> dict[str, Any]:
        dep = {
            "metadata": {"generation": 2},
            "spec": {"replicas": wanted},
            "status": {
                "observedGeneration": 2,
                "replicas": running,
                "readyReplicas": ready,
                "updatedReplicas": updated,
            },
        }
        return map_deployment_status(dep, None, now)

    # maxSurge 로 뜬 새 파드가 준비됐지만 옛 파드가 아직 돈다
    surge = phase(1, running=2, ready=2, updated=1)
    assert surge["phase"] == "Rolling" and surge["message"] == "1 old pod(s) still running"
    assert surge["ready_replicas"] == 2
    # 새 파드는 아직 준비 전이고 준비된 것은 옛 파드뿐이다
    assert phase(1, running=2, ready=1, updated=1)["phase"] == "Rolling"
    # 레플리카 둘이면 새 파드 둘이 준비돼도 옛 파드 하나가 남는 동안 Rolling 이다
    two = phase(2, running=3, ready=3, updated=2)
    assert two["phase"] == "Rolling" and two["message"] == "1 old pod(s) still running"
    # 옛 파드가 종료 중이 되면 replicas 에서 빠져 Ready 다
    done = phase(1, running=1, ready=1, updated=1)
    assert done["phase"] == "Ready" and done["message"] is None


def test_a_stuck_rollout_with_the_old_pod_still_ready_fails_after_the_grace():
    """새 파드가 이미지를 못 받거나 계속 죽으면 준비된 옛 파드가 남은 채 롤아웃이 멈추고, 진행
    기한이 지나면 Progressing=False 가 온다. 옛 파드가 남은 것보다 이것을 먼저 보아 그레이스 뒤에는
    Failed 와 그 까닭을 낸다. 그레이스 안에서는 Rolling 이다."""
    now = utcnow()
    dep = {
        "metadata": {"generation": 2},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 2,
            "replicas": 2,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                    "message": 'ReplicaSet "agent-support-7d9" has timed out progressing.',
                }
            ],
        },
    }
    out = map_deployment_status(dep, now - timedelta(hours=1), now)
    assert out["phase"] == "Failed" and "timed out progressing" in out["message"]
    assert map_deployment_status(dep, now, now)["phase"] == "Rolling"


def test_status_reads_deployment_and_runtime_metrics(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    st = client.get(agent.url).json()["status"]
    assert st["phase"] == "Pending" and st["active_sessions"] is None and st["error"] is None
    # 인사말은 핀된 리비전 입구의 값이다 (D65)
    assert st["greeting"] == "안녕하세요. 무엇을 도와드릴까요?"
    # 파드가 준비됐는데 런타임이 토큰을 거부하면 phase 는 Ready 이고 error 에 남는다
    set_ready(fake)
    fake.service_app = fake_runtime("other-token")
    st = client.get(agent.url).json()["status"]
    assert st["phase"] == "Ready" and st["ready_replicas"] == 1
    assert st["active_sessions"] is None and st["error"] == "runtime_metrics: HTTP 401"
    # 런타임이 답하면 세션·보류 수와 경고
    fake.service_app = fake_runtime(agent.token, warnings=["no_shared_state"])
    st = client.get(agent.url).json()["status"]
    assert st["phase"] == "Ready" and st["error"] is None
    assert (st["active_sessions"], st["pending_approvals"]) == (2, 1)
    # 런타임은 코드만 보내고 플랫폼이 `code: 설명` 으로 편다
    assert len(st["warnings"]) == 1 and st["warnings"][0].startswith("no_shared_state: ")
    assert "memory node" in st["warnings"][0]
    # 보류 세션이 있는데 저장된 리비전이 앞서가면 경고가 하나 더 붙는다
    changed = agent.spec()
    node_of(changed, "entry")["data"]["greeting"] = "반갑습니다."
    assert client.put(agent.url, json=agent_body(changed)).json()["revision"] == 2
    st = client.get(agent.url).json()["status"]
    assert len(st["warnings"]) == 2 and st["warnings"][1].startswith(
        "pending_sessions_revision_changed: 1 session(s)"
    )
    # 정의를 고쳐 저장해도 재배포 전에는 도는 리비전의 인사말이다
    assert st["greeting"] == "안녕하세요. 무엇을 도와드릴까요?"
    # 목록도 같은 상태를 준다. 클러스터에 연결할 수 없으면 phase 없이 error 다
    assert client.get(AG).json()[0]["status"]["phase"] == "Ready"
    fake.unreachable = True
    st = client.get(agent.url).json()["status"]
    assert st["phase"] is None and st["error"].startswith("cluster_unreachable")


# -- redeploy·delete -------------------------------------------------------------------------


def test_redeploy_recopies_secrets_moves_pin_and_warns(client, fake_clusters, fake_secrets, mongo):
    agent = deployed_agent(
        client,
        fake_clusters,
        mongo,
        spec=anthropic_with_openai_fallback,
        openai_secrets=fake_secrets,
        runtime=False,
    )
    inst = agent.doc
    fake = agent.fake
    token = token_of(fake, inst)
    token_doc = mongo["agent_instance_tokens"].find_one({"_id": inst["token_id"]})
    assert token_doc["scope"] == [f"agent-profile:{inst['id']}:rev:1"]

    # 키 교체 + 폴백 제거 + 리비전 2. 런타임은 보류 세션 하나를 갖고 있다
    fake_secrets.put(LLM_KEY_SECRET, {"api_key": "sk-rotated"}, namespace=PLATFORM_NS)
    changed = anthropic_for(agent.gw, agent.ds)
    assert client.put(f"{AG}/{inst['id']}", json=agent_body(changed)).json()["revision"] == 2
    set_ready(fake)
    fake.service_app = fake_runtime(token)
    r = client.post(f"{AG}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["deployed_revision"] == 2 and out["state"] == "deployed"
    assert out["llm_secrets"] == ["agent-support-llm-local-anthropic"]
    assert any(
        w.startswith("pending_sessions_revision_changed: 1 session(s)") for w in out["warnings"]
    )
    assert any(w.startswith("network_policy_unenforced") for w in out["warnings"])
    # 복사본이 새 키로 갱신됐고 폴백 복사본은 지워졌다
    assert fake.get("v1", "Secret", "agent-support-llm-local-anthropic", DEFAULT_NS)[
        "stringData"
    ] == {"api_key": "sk-rotated"}
    assert fake.get("v1", "Secret", "agent-support-llm-local-openai", DEFAULT_NS) is None
    dep = agent_deployment(fake)
    assert dep["metadata"]["labels"]["daegam.devops.ai.kr/profile-revision"] == "2"
    assert "OPENAI_API_KEY" not in env_map(dep["spec"]["template"]["spec"]["containers"][0])
    # 토큰은 그대로이고 scope 에 리비전 2 가 더해져 initContainer 가 새 agent.json 을 받는다
    assert token_of(fake, inst) == token
    token_doc = mongo["agent_instance_tokens"].find_one({"_id": inst["token_id"]})
    assert token_doc["scope"] == [
        f"agent-profile:{inst['id']}:rev:1",
        f"agent-profile:{inst['id']}:rev:2",
    ]
    r = client.get(f"{AG}/{inst['id']}/agent.json", headers=bearer(token))
    assert r.status_code == 200 and r.json()["profile"]["revision"] == 2
    # 리비전이 그대로면 경고 없이 재복사·재적용만 한다
    r = client.post(f"{AG}/{inst['id']}/redeploy")
    assert r.status_code == 200 and not any(
        w.startswith("pending_sessions") for w in r.json()["warnings"]
    )
    # 저장된 정의의 provider 가 사라지면 422
    assert client.patch("/settings", json={"llm_providers": []}).status_code == 200
    r = client.post(f"{AG}/{inst['id']}/redeploy")
    assert detail_code(r) == (422, "invalid_graph")


def test_delete_cleans_up_with_label_fallback(client, fake_clusters, fake_secrets, mongo):
    agent = deployed_agent(
        client,
        fake_clusters,
        mongo,
        spec=anthropic_for,
        openai_secrets=fake_secrets,
        runtime=False,
    )
    inst = agent.doc
    fake = agent.fake
    # 이름이 다른 잔재 (라벨만 같다)
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "agent-support-old",
                "namespace": DEFAULT_NS,
                "labels": {"daegam.devops.ai.kr/id": inst["id"]},
            },
            "spec": {},
        }
    )
    r = client.delete(f"{AG}/{inst['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["revoked_tokens"] == 1 and r.json()["k8s"]["name"] == "agent-support"
    assert agent_deployment(fake) is None
    assert fake.get("v1", "Service", "agent-support", DEFAULT_NS) is None
    assert fake.get("v1", "Service", "agent-support-old", DEFAULT_NS) is None
    assert (
        fake.get("networking.k8s.io/v1", "NetworkPolicy", "daegam-agent-support", DEFAULT_NS)
        is None
    )
    assert fake.get("v1", "Secret", inst["k8s"]["token_secret"], DEFAULT_NS) is None
    assert fake.get("v1", "Secret", "agent-support-llm-local-anthropic", DEFAULT_NS) is None
    # 문서를 남기지 않는다. 다시 부르면 404 다 (D42: 소프트 삭제가 없어졌다)
    assert client.get(f"{AG}/{inst['id']}").status_code == 404
    assert client.delete(f"{AG}/{inst['id']}").status_code == 404
    assert mongo["agents"].find_one({"_id": inst["id"]}) is None
    # 이름은 곧바로 다시 쓸 수 있다
    assert create_agent(client, agent.spec()).status_code == 201


# -- 해제·삭제가 도는 턴을 먼저 멈춘다 (P78 5절) ------------------------------------------------


def _record_stop_and_revoke(svc: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`stop_turns` 와 토큰 회수가 불린 차례를 남긴다."""
    order: list[str] = []
    real_stop, real_revoke = svc.proxy.stop_turns, svc._revoke_access

    async def stop(doc: Any, *, user_sub: str) -> int:
        order.append(f"stop:{doc.state}:{user_sub}")
        return await real_stop(doc, user_sub=user_sub)

    async def revoke(doc: Any) -> int:
        order.append("revoke")
        return await real_revoke(doc)

    monkeypatch.setattr(svc.proxy, "stop_turns", stop)
    monkeypatch.setattr(svc, "_revoke_access", revoke)
    return order


# stop_attempts 는 서비스가 stop_turns 를 부른 수, delivered 는 그 가운데 런타임에 닿은 수다. 닿지
# 못한 런타임에는 한 번 부르고 그친다
@pytest.mark.parametrize(
    ("case", "features", "running", "stop_attempts", "delivered"),
    [
        pytest.param("undeploy", "cancel,detached_turns", [2, 1], 3, 3, id="undeploy-waits"),
        pytest.param("reconcile", "cancel,detached_turns", [3], 1, 1, id="reconcile-marks-once"),
        pytest.param("unreachable", "cancel,detached_turns", [], 1, 0, id="unreachable-goes-on"),
    ],
)
def test_removal_stops_running_turns_before_revoking_the_token(
    client,
    fake_clusters,
    mongo,
    monkeypatch,
    caplog,
    case,
    features,
    running,
    stop_attempts,
    delivered,
):
    """턴을 떼어 돌리는 런타임이면 해제와 삭제가 토큰을 회수하기 전에 `/turns/cancel` 로 도는 턴을
    멈춘다(P78 5절). API 경로는 남은 턴이 없어질 때까지 다시 부르고, 리컨실(기다리지 않는 해제)은
    멈추라고만 한다. `detached_turns` 를 모르는 옛 이미지는 부르지 않고, 런타임에 닿지 못해도
    내리기는 이어 간다."""
    monkeypatch.setattr(agents_module, "TURN_STOP_POLL_SECONDS", 0)
    svc = client.app.state.agent_service
    agent = deployed_agent(client, fake_clusters, mongo, features=features)
    order = _record_stop_and_revoke(svc, monkeypatch)
    agent.runtime.state.running = running
    token = agent.token
    if case == "unreachable":
        drop_route(agent.runtime, "/turns/cancel")
    if case == "reconcile":

        async def undeploy_without_waiting() -> str:
            doc = await svc.agents.get(agent.id)
            return (await svc.undeploy(doc, wait=False)).state

        assert client.portal.call(undeploy_without_waiting) == "draft"
    elif case == "undeploy":
        assert client.post(f"{agent.url}/undeploy").status_code == 200
    else:
        assert client.delete(agent.url).status_code == 200

    state = "deleting" if case in ("delete", "unreachable") else "undeploying"
    assert order == [f"stop:{state}:{agents_module.TURN_STOP_SUB}"] * stop_attempts + ["revoke"]
    calls = [c for c in agent.runtime.state.calls if c["path"] == "/turns/cancel"]
    assert len(calls) == delivered
    assert all(c["headers"]["authorization"] == f"Bearer {token}" for c in calls)
    assert ("도는 턴을 멈추지 못했다" in caplog.text) is (case == "unreachable")


# -- 가벼운 목록 (D35 F2) ---------------------------------------------------------------------


def test_summary_list_answers_without_touching_the_cluster(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo)
    fake = agent.fake
    set_ready(fake)
    # 상태를 한 번 읽어 두면 그 phase 가 문서에 남는다
    assert client.get(agent.url).json()["status"]["phase"] == "Ready"
    fake.unreachable = True
    rows = client.get(f"{AG}/summary").json()
    assert rows == [
        {
            "id": agent.id,
            "name": "support",
            "display_name": "support",
            "phase": "Ready",
        }
    ]
    # 클러스터에 연결할 수 없어도 답한다. 상태를 붙이는 목록은 error 만 준다
    assert client.get(AG).json()[0]["status"]["error"].startswith("cluster_unreachable")
    # 상세 경로는 그대로다. `summary` 가 문서 id 로 읽히지 않는다
    assert client.get(agent.url).json()["id"] == agent.id


def test_status_survives_a_forbidden_deployment_read(client, fake_clusters, mongo):
    """권한이 모자란 클러스터에서 Deployment 읽기가 403 이어도 상태 조회는 예외를 던지지 않고 빠진
    권한을 `cluster_forbidden:` 으로 보인다. 플랫폼 계정의 권한 문제다 (P71 2.4절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    agent.fake.denied.add(("apps", "deployments", "get"))
    r = client.get(agent.url)
    assert r.status_code == 200, r.text
    assert r.json()["status"]["error"].startswith("cluster_forbidden: deployments:get")
