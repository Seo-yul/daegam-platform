"""쉬기의 라우트와 턴 라우트의 게이트 (P44 7절·8절·14절, D103).

`POST /sleep` 은 지금 재우고 `POST /wake` 는 깨운다. 채팅·결정·다시 생성은 로더 바로 뒤의 게이트가
위임을 열거나 업로드를 쓰기 전에 깨우고, 깨우는 동안 503 agent_waking 과 Retry-After 로 답한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.store.chat_uploads import CHAT_UPLOADS
from tests.helpers.agent_runtime import AGENT_DEPLOYMENT, chat_upload, deployed_agent, set_ready
from tests.helpers.agent_sleep import (
    SLEEP_FEATURES,
    deployment_replicas,
    forget_deployments,
    pods_gone,
    runtime_calls,
    sleepy_agent,
    stored,
)
from tests.helpers.consts import DEFAULT_NS

WITH_ATTACHMENTS = f"{SLEEP_FEATURES},attachments,regenerate"


def _asleep(client, fake_clusters, mongo, features: str = WITH_ATTACHMENTS):
    agent = sleepy_agent(client, fake_clusters, mongo, features=features)
    r = client.post(f"{agent.url}/sleep")
    assert r.status_code == 200, r.text
    pods_gone(agent)
    agent.runtime.state.calls.clear()
    return agent


def _grants_opened(client, monkeypatch) -> list[dict[str, Any]]:
    """턴 위임 열기를 적는다. 게이트가 위임 전에 서는지 본다."""
    opened: list[dict[str, Any]] = []
    grants = client.app.state.turn_grants
    real = grants.open

    async def recording(**kwargs: Any):
        opened.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(grants, "open", recording)
    return opened


def _ready(client, agent) -> None:
    set_ready(agent.fake)
    forget_deployments(client)


# -- POST /sleep 과 POST /wake (14절) ------------------------------------------------------------


def test_the_sleep_route_answers_once_the_pods_are_gone(client, fake_clusters, mongo):
    """0 으로 줄이기를 적용한 뒤 상태로 답한다. 이미 잠든 Agent 는 그대로 답한다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    r = client.post(f"{agent.url}/sleep")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"]["phase"] == "Sleeping" and body["status"]["sleep"]["state"] == "asleep"
    assert body["status"]["sleep"]["reason"] == "request"
    assert deployment_replicas(agent) == 0
    assert client.post(f"{agent.url}/sleep").status_code == 200
    assert len(runtime_calls(agent, "POST", "/drain")) == 1


def test_the_sleep_route_refuses(client, fake_clusters, mongo):
    """잠들 수 없으면 409 agent_sleep_ineligible 과 까닭, 턴이 리스를 쥐고 있으면 409 agent_busy,
    배포되지 않았으면 409 not_deployed 다."""
    agent = deployed_agent(client, fake_clusters, mongo, features="cancel")
    r = client.post(f"{agent.url}/sleep")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "agent_sleep_ineligible"
    assert r.json()["detail"]["ineligible"] == "runtime_image"

    mongo.agents.update_one({"_id": agent.id}, {"$addToSet": {"runtime.features": SCALE_TO_ZERO}})
    agent.runtime.state.drain_leases = 1
    r = client.post(f"{agent.url}/sleep")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_busy"

    assert client.post(f"{agent.url}/undeploy").status_code == 200
    r = client.post(f"{agent.url}/sleep")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_deployed"


def test_the_wake_route_starts_the_pods_and_answers_at_once(client, fake_clusters, mongo):
    """잠든 Agent 의 파드를 다시 띄우고 기다리지 않고 202 로 답한다. 깨어 있는 Agent 도 202 이고
    활동만 적힌다."""
    agent = _asleep(client, fake_clusters, mongo)
    r = client.post(f"{agent.url}/wake")
    assert r.status_code == 202, r.text
    status = r.json()["status"]
    assert status["phase"] == "Waking" and status["sleep"]["state"] == "waking"
    assert status["sleep"]["reason"] == "wake"
    assert deployment_replicas(agent) == 1

    _ready(client, agent)
    r = client.post(f"{agent.url}/wake")
    assert r.status_code == 202 and r.json()["status"]["phase"] == "Ready"
    assert stored(client, agent.id).sleep.state == "awake"
    assert stored(client, agent.id).sleep.activity is not None


# -- 턴의 게이트 (8절) ---------------------------------------------------------------------------


def test_a_chat_wakes_before_the_grant_and_the_upload(client, fake_clusters, mongo, monkeypatch):
    """잠든 Agent 의 채팅은 위임을 열거나 업로드를 쓰기 전에 깨우고 503 agent_waking 이다. 같은
    메시지를 다시 보내면 파드가 준비된 뒤 지나가고 그때 업로드를 쓴다."""
    agent = _asleep(client, fake_clusters, mongo)
    upload = chat_upload(client, agent.id, "a.md", b"# hi", "text/markdown").json()["id"]
    opened = _grants_opened(client, monkeypatch)
    body = {"message": "안녕", "attachments": [upload]}

    r = client.post(f"{agent.url}/chat", json=body)
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.json()["detail"]["code"] == "agent_waking"
    assert opened == [] and runtime_calls(agent, "POST", "/chat") == []
    assert mongo[CHAT_UPLOADS].count_documents({"_id": upload}) == 1
    assert deployment_replicas(agent) == 1

    r = client.post(f"{agent.url}/chat", json=body)
    assert r.status_code == 503 and opened == []

    _ready(client, agent)
    r = client.post(f"{agent.url}/chat", json=body)
    assert r.status_code == 200, r.text
    assert len(opened) == 1 and len(runtime_calls(agent, "POST", "/chat")) == 1
    assert mongo[CHAT_UPLOADS].count_documents({"_id": upload}) == 0
    assert stored(client, agent.id).sleep.state == "awake"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/sessions/s1/decisions", {"decisions": [{"type": "approve"}]}),
        ("/sessions/s1/regenerate", {}),
    ],
)
def test_decisions_and_regenerate_wake_before_anything(
    client, fake_clusters, mongo, monkeypatch, path, body
):
    """결정과 다시 생성도 로더 바로 뒤에 깨운다. 실행의 결정은 실행을 옮기기 전이다."""
    agent = _asleep(client, fake_clusters, mongo)
    opened = _grants_opened(client, monkeypatch)
    begun: list[Any] = []
    approvals = client.app.state.run_service.approvals

    async def begin(*args: Any, **kwargs: Any) -> None:
        begun.append(args)

    monkeypatch.setattr(approvals, "begin", begin)
    r = client.post(f"{agent.url}{path}", json=body)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "agent_waking"
    assert opened == [] and begun == [] and agent.runtime.state.calls == []
    assert stored(client, agent.id).sleep.state == "waking"


def test_attachments_pass_a_scale_up_of_the_current_template(client, fake_clusters, mongo):
    """여러 레플리카를 깨우면 모두 준비될 때까지 phase 가 Rolling 이지만, 모든 파드가 지금
    템플릿이고 하나가 준비됐으면 첨부 턴이 지나간다. 옛 템플릿의 파드가 남았으면 409 runtime_rolling
    이다."""
    agent = sleepy_agent(client, fake_clusters, mongo, features=WITH_ATTACHMENTS)
    dep = agent.fake.get("apps/v1", "Deployment", AGENT_DEPLOYMENT, DEFAULT_NS)
    generation = dep["metadata"]["generation"]
    agent.fake.objects[("apps/v1", "Deployment", DEFAULT_NS, AGENT_DEPLOYMENT)]["spec"][
        "replicas"
    ] = 3

    def status(updated: int) -> None:
        agent.fake.set_status(
            "apps/v1",
            "Deployment",
            AGENT_DEPLOYMENT,
            DEFAULT_NS,
            {
                "observedGeneration": generation,
                "replicas": 3,
                "updatedReplicas": updated,
                "readyReplicas": 1,
            },
        )
        forget_deployments(client)

    def chat() -> Any:
        upload = chat_upload(client, agent.id, "a.md", b"# hi", "text/markdown").json()["id"]
        return client.post(f"{agent.url}/chat", json={"message": "hi", "attachments": [upload]})

    status(updated=2)
    r = chat()
    assert r.status_code == 409 and r.json()["detail"]["code"] == "runtime_rolling"
    status(updated=3)
    assert chat().status_code == 200
