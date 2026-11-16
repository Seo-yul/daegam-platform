"""잠들기와 깨우기의 상태 이동, 프록시와 상태 조회 (P44 6절~10절, D103).

잠들 수 있는 Agent(`sleepy_agent`)를 서비스의 걸음으로 재운다. 잠든 Agent 의 읽기는 런타임을 부르지
않고, 턴의 게이트는 이긴 한 쪽만 깨우며, 레플리카 수는 늘 문서의 쉬는 상태가 정한다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from daegam.config import Settings
from daegam.k8s.templates.common import current_template_ready
from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.services.agent_runtime import AGENT_WAKING
from daegam.services.agent_sleep import (
    JUST_WOKEN,
    housekeeping_at,
    housekeeping_offset,
    ineligible,
    ineligible_all,
)
from daegam.services.agents import AgentError
from daegam.store.agents import AgentRuntime, AgentSleep
from daegam.store.base import utcnow
from daegam.store.exposure import AgentExposure
from tests.helpers.agent_runtime import (
    AGENT_DEPLOYMENT,
    deployed_agent,
    minimal_agent_doc,
    set_ready,
)
from tests.helpers.agent_sleep import (
    admit,
    deployment_replicas,
    forget_deployments,
    in_app,
    put_to_sleep,
    runtime_calls,
    service,
    set_sleep,
    sleepy_agent,
    stored,
)
from tests.helpers.agents import template_for, without_memory
from tests.helpers.consts import AG, DEFAULT_NS
from tests.helpers.docs import agent_doc
from tests.helpers.runtime import add_session_admin

# 런타임 `/metrics` 의 받는 첨부 (D61)
CHAT_LIMITS = {
    "documents": True,
    "images": True,
    "attachments_limit_fetch": False,
    "limits": {
        "max_attachments_per_turn": 5,
        "max_images_per_turn": 3,
        "max_session_images": 20,
        "max_image_bytes": 5242880,
        "max_session_bytes": 52428800,
    },
}

# -- 잠들 수 있는가 (6절) ----------------------------------------------------------------------


def _eligible(*, memory: bool = True):
    """memory 노드가 있고 런타임이 `scale_to_zero` 를 알린 배포된 문서다."""
    spec = template_for({"id": "gw-1"}, {"id": "ds-1"})
    doc = agent_doc(spec=spec if memory else without_memory(spec), state="deployed")
    doc.effective_config = {"images": {"agent_image": "img"}, "replicas": 2}
    doc.runtime = AgentRuntime(image="img", features=[SCALE_TO_ZERO])
    return doc


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({}, None),
        ({"memory": False}, "no_memory"),
        ({"exposure": AgentExposure(enabled=True)}, "exposed"),
        ({"features": []}, "runtime_image"),
        ({"overrides": {"spec": {"replicas": 3}}}, "replicas_override"),
        ({"scale_to_zero": False}, "agent_off"),
        ({"setting": False}, "setting_off"),
        # 표의 차례로 첫 까닭이다
        ({"memory": False, "scale_to_zero": False}, "no_memory"),
    ],
)
def test_the_eligibility_table(change, code):
    doc = _eligible(memory=change.get("memory", True))
    if "features" in change:
        doc.runtime = doc.runtime.model_copy(update={"features": change["features"]})
    for name in ("exposure", "overrides", "scale_to_zero"):
        if name in change:
            setattr(doc, name, change[name])
    settings = Settings(agent_scale_to_zero=change.get("setting", True))
    assert ineligible(doc, settings) == code


def test_every_failing_condition_is_listed_in_table_order():
    """`ineligible_all` 은 표의 차례로 모든 까닭이다. 배포 대화상자가 자기 입력으로 앞의 까닭을 다시
    가를 때 뒤의 까닭(Advanced 의 spec.replicas, 플랫폼 스위치)을 놓치지 않는다."""
    doc = _eligible()
    assert ineligible_all(doc, Settings()) == []
    doc.exposure = AgentExposure(enabled=True)
    doc.overrides = {"spec": {"replicas": 3}}
    codes = ineligible_all(doc, Settings(agent_scale_to_zero=False))
    assert codes == ["exposed", "replicas_override", "setting_off"]
    assert ineligible(doc, Settings(agent_scale_to_zero=False)) == "exposed"


def test_an_image_without_a_record_is_not_eligible():
    """배포 전 이미지의 Agent 는 스냅숏 이미지의 기록이 없거나 이름이 없다. 재배포 뒤에 잠든다."""
    doc = _eligible()
    doc.runtime = None
    assert ineligible(doc, Settings()) == "runtime_image"


# -- 잠들기 (7절) ---------------------------------------------------------------------------


def test_sleeping_drains_counts_and_scales_to_zero(client, fake_clusters, mongo):
    """표시를 두고 리스를 센 뒤 `/metrics` 의 수와 함께 asleep 으로 옮기고 0 으로 줄인다. 상태는
    Sleeping 이고 남긴 수를 보이며 런타임을 읽지 않는다 (7절, 10절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    agent.runtime.state.chat = CHAT_LIMITS
    put_to_sleep(client, agent)

    (drain,) = runtime_calls(agent, "POST", "/drain")
    doc = stored(client, agent.id)
    assert drain["body"] == {"user_sub": "system:sleep", "mark": doc.sleep.mark}
    assert doc.sleep.mark and agent.runtime.state.drain_mark == doc.sleep.mark
    assert deployment_replicas(agent) == 0
    dep = agent.fake.get("apps/v1", "Deployment", AGENT_DEPLOYMENT, DEFAULT_NS)
    assert doc.sleep.state == "asleep" and doc.sleep.reason == "request"
    assert (doc.sleep.active_sessions, doc.sleep.pending_approvals) == (2, 1)
    assert doc.sleep.scaled == "applied"
    assert doc.sleep.generation == dep["metadata"]["generation"]

    agent.runtime.state.calls.clear()
    status = client.get(agent.url).json()["status"]
    assert status["phase"] == "Sleeping" and status["message"] is None
    assert (status["active_sessions"], status["pending_approvals"]) == (2, 1)
    assert status["sleep"]["state"] == "asleep" and status["sleep"]["ineligible"] is None
    assert status["sleep"]["ineligible_all"] == []
    assert status["sleep"]["window_seconds"] == 0
    # 받는 첨부는 잠들 때의 값이라 화면이 첨부 단추를 그대로 둔다 (8절)
    assert status["chat"] == CHAT_LIMITS
    assert agent.runtime.state.calls == []
    summary = client.get(f"{AG}/summary").json()
    assert [s["phase"] for s in summary if s["id"] == agent.id] == ["Sleeping"]


def test_waking_keeps_the_saved_chat_and_a_failed_read_keeps_the_last_one(
    client, fake_clusters, mongo
):
    """깨우는 동안에도 상태의 `chat` 은 잠들 때의 값이다. 다음 잠들기에서 `/metrics` 를 읽지 못하면
    앞 잠들기의 값을 둔다 (8절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    agent.runtime.state.chat = CHAT_LIMITS
    put_to_sleep(client, agent)
    with pytest.raises(AgentError):
        admit(client, agent.id)
    status = client.get(agent.url).json()["status"]
    assert status["phase"] == "Waking" and status["chat"] == CHAT_LIMITS

    set_ready(agent.fake)
    forget_deployments(client)
    admit(client, agent.id)
    agent.runtime.state.metrics_status = 500
    put_to_sleep(client, agent)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "asleep" and doc.sleep.chat == CHAT_LIMITS
    assert doc.sleep.pending_approvals is None


def test_a_live_lease_keeps_the_agent_up(client, fake_clusters, mongo):
    """턴이 리스를 쥐고 있으면 표시를 지우고 새 활동과 함께 awake 로 돌린다. 요청은 409
    agent_busy 다 (7절 2단계)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    agent.runtime.state.drain_leases = 1
    with pytest.raises(AgentError) as e:
        put_to_sleep(client, agent)
    assert e.value.status == 409 and e.value.code == "agent_busy"
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "busy"
    assert doc.sleep.activity is not None
    assert agent.runtime.state.drain_mark is None
    (undrain,) = runtime_calls(agent, "DELETE", "/drain")
    assert undrain["params"] == {"mark": doc.sleep.mark}
    assert deployment_replicas(agent) == 1


@pytest.mark.parametrize(
    ("pod_phase", "slept"), [("Running", False), ("Pending", True), (None, True)]
)
def test_no_answer_is_idle_only_without_a_running_pod(
    client, fake_clusters, mongo, pod_phase, slept
):
    """드레인에 답이 없을 때 Running 파드가 있으면(준비되지 않았어도) 바쁜 것으로 보고, 없으면 어떤
    턴도 돌 수 없으므로 잠든다 (7절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    agent.fake.service_app = None
    if pod_phase is not None:
        labels = {"daegam.devops.ai.kr/id": agent.id}
        agent.fake.add_pod(DEFAULT_NS, "agent-support-x", labels, phase=pod_phase)
    if slept:
        put_to_sleep(client, agent)
        assert stored(client, agent.id).sleep.state == "asleep"
        assert stored(client, agent.id).sleep.active_sessions is None
    else:
        with pytest.raises(AgentError) as e:
            put_to_sleep(client, agent)
        assert e.value.code == "runtime_unreachable"
        assert stored(client, agent.id).sleep.state == "awake"
        assert stored(client, agent.id).sleep.reason == "no_answer"


def test_an_ineligible_agent_refuses_the_sleep(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, features=SLEEPLESS)
    with pytest.raises(AgentError) as e:
        put_to_sleep(client, agent)
    assert e.value.status == 409 and e.value.code == "agent_sleep_ineligible"
    assert e.value.extra == {"ineligible": "runtime_image"}
    assert runtime_calls(agent, "POST", "/drain") == []


SLEEPLESS = "cancel"


# -- 읽기는 깨우지 않는다 (8절) ---------------------------------------------------------------

READS: list[tuple[str, str]] = [
    ("GET", "/sessions"),
    ("GET", "/sessions/s1"),
    ("GET", "/sessions/s1/pending"),
    ("GET", "/sessions/s1/files"),
    ("GET", "/sessions/s1/files/a.txt"),
    ("PATCH", "/sessions/s1"),
    ("DELETE", "/sessions/s1"),
    ("GET", "/memories/me"),
    ("GET", "/memories/me/files/a.md"),
    ("DELETE", "/memories/me/files/a.md"),
    ("GET", "/sessions/s1/citations/1/source"),
]


@pytest.mark.parametrize(
    ("state", "status", "code"), [("asleep", 409, "agent_asleep"), ("waking", 503, AGENT_WAKING)]
)
def test_every_runtime_read_answers_without_the_runtime(
    client, fake_clusters, mongo, state, status, code
):
    """잠든 Agent 의 읽기는 409 agent_asleep, 깨우는 중이면 503 agent_waking 과 Retry-After 이고
    런타임을 부르지 않는다. 열어 둔 화면이 Agent 를 깨워 두지 않는다 (8절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state=state)
    agent.runtime.state.calls.clear()
    for method, path in READS:
        body = {"title": "새 제목"} if method == "PATCH" else None
        r = client.request(method, f"{agent.url}{path}", json=body)
        assert r.status_code == status, (method, path, r.text)
        assert r.json()["detail"]["code"] == code, (method, path)
        if state == "waking":
            assert r.headers["retry-after"] == "5"
    metrics = client.get(f"{agent.url}/metrics").json()
    assert metrics["runtime_error"]["code"] == code
    assert agent.runtime.state.calls == []


@pytest.mark.parametrize("state", ["asleep", "waking"])
def test_cancel_without_a_running_pod(client, fake_clusters, mongo, state):
    """잠들었거나 깨우는 중인 Agent 에 Running 파드가 없으면 드레인 표시가 어떤 턴도 시작하지
    못하게 했으므로 409 no_turn_in_progress 이고, Running 파드가 남았으면 런타임을 부른다 (8절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state=state)
    labels = {"daegam.devops.ai.kr/id": agent.id}
    agent.fake.add_pod(DEFAULT_NS, "agent-support-new", labels, phase="Pending")
    r = client.post(f"{agent.url}/sessions/s1/cancel")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_turn_in_progress"
    assert runtime_calls(agent, "POST", "/sessions/s1/cancel") == []
    agent.fake.add_pod(DEFAULT_NS, "agent-support-x", labels)
    assert client.post(f"{agent.url}/sessions/s1/cancel").status_code == 202
    assert len(runtime_calls(agent, "POST", "/sessions/s1/cancel")) == 1


# -- 깨우기 (8절) ---------------------------------------------------------------------------


def test_the_gate_wakes_once_and_passes_on_a_ready_pod(client, fake_clusters, mongo):
    """asleep 을 waking 으로 옮긴 쪽이 활동을 적고 레플리카를 되돌린다. 준비된 파드가 없는 동안은
    503 agent_waking 이고, 파드가 준비되면 표시를 지우고 awake 로 옮겨 지나간다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    agent.fake.scales.clear()

    with pytest.raises(AgentError) as e:
        admit(client, agent.id)
    assert e.value.status == 503 and e.value.code == AGENT_WAKING
    assert e.value.headers == {"Retry-After": "5"}
    doc = stored(client, agent.id)
    assert doc.sleep.state == "waking" and doc.sleep.reason == "turn"
    assert doc.sleep.activity is not None and doc.sleep.scaled is None
    assert agent.fake.scales == [(DEFAULT_NS, AGENT_DEPLOYMENT, 1)]
    status = client.get(agent.url).json()["status"]
    assert status["phase"] == "Waking"

    with pytest.raises(AgentError):
        admit(client, agent.id)
    assert agent.fake.scales == [(DEFAULT_NS, AGENT_DEPLOYMENT, 1)]

    set_ready(agent.fake)
    forget_deployments(client)
    doc = admit(client, agent.id)
    assert doc.sleep.state == "awake" and stored(client, agent.id).sleep.state == "awake"
    assert stored(client, agent.id).sleep.reason == "turn"
    assert len(runtime_calls(agent, "DELETE", "/drain")) >= 1


def test_two_replicas_wake_once(client, fake_clusters, mongo):
    """플랫폼 레플리카 둘이 함께 깨우면 조건부 갱신에서 하나만 이겨 레플리카를 한 번 되돌린다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    agent.fake.scales.clear()
    svc = service(client)

    async def both() -> list[Any]:
        docs = [await svc.agents.get(agent.id) for _ in range(2)]
        return await asyncio.gather(
            *(svc.sleep.admit(d, "turn") for d in docs), return_exceptions=True
        )

    results = in_app(client, both)
    assert [getattr(r, "code", None) for r in results] == [AGENT_WAKING, AGENT_WAKING]
    assert agent.fake.scales == [(DEFAULT_NS, AGENT_DEPLOYMENT, 1)]


def test_a_turn_on_draining_writes_awake(client, fake_clusters, mongo):
    """잠들기 사이에 온 턴은 awake 를 쓰고 지나간다. 워커의 asleep 옮기기가 실패한다 (7절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state="draining", since=utcnow(), reason="idle")
    doc = admit(client, agent.id, "turn")
    assert doc.sleep.state == "awake"
    fresh = stored(client, agent.id)
    assert fresh.sleep.state == "awake" and fresh.sleep.reason == "turn"


def test_the_gate_ignores_documents_without_a_runtime(client, fake_clusters, mongo):
    """런타임을 부르지 않는 상태는 게이트가 보지 않는다. 뒤의 상태 가드가 답한다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    mongo.agents.update_one({"_id": agent.id}, {"$set": {"state": "deploying"}})
    set_sleep(mongo, agent.id, state="asleep")
    assert admit(client, agent.id).sleep.state == "asleep"


# -- 드레인 거절 (7절) ----------------------------------------------------------------------


def _chat(client, agent_id: str):
    async def go():
        doc = await service(client).agents.get(agent_id)
        proxied = await service(client).proxy.chat(doc, user_sub="u", message="hi", session_id=None)
        raw = b"".join([c async for c in proxied.body])
        return proxied.status, dict(proxied.headers), raw

    return in_app(client, go)


def test_a_drain_refusal_on_an_awake_agent_is_cleared_and_retried(client, fake_clusters, mongo):
    """문서가 awake 인데 표시에 거절되면 문서의 마지막 표시를 지우고 한 번 다시 보낸다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, mark="m1")
    agent.runtime.state.drain_mark = "m1"
    status, _headers, _raw = _chat(client, agent.id)
    assert status == 200
    assert len(runtime_calls(agent, "POST", "/chat")) == 2
    (undrain,) = runtime_calls(agent, "DELETE", "/drain")
    assert undrain["params"] == {"mark": "m1"}


def test_a_late_clear_leaves_a_newer_mark(client, fake_clusters, mongo):
    """프록시가 awake 를 읽은 뒤 새 잠들기가 표시를 세웠으면 늦게 닿은 지우기는 옛 id 를 실어 새
    표시를 남긴다. 다시 보낸 턴은 새 표시를 만나 503 agent_waking 이다 (7절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, mark="old")
    agent.runtime.state.drain_mark = "new"
    status, _headers, raw = _chat(client, agent.id)
    assert status == 503 and b'"agent_waking"' in raw
    assert agent.runtime.state.drain_mark == "new"
    assert len(runtime_calls(agent, "POST", "/chat")) == 2


def test_a_drain_refusal_on_a_sleeping_agent_answers_waking(client, fake_clusters, mongo):
    """문서가 잠드는 중이면 표시를 지우지 않고 503 agent_waking 으로 답한다. 다음 시도의 게이트가
    깨운다. 다른 503 은 그대로다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    agent.runtime.state.drain_mark = "m1"
    set_sleep(mongo, agent.id, state="draining", since=utcnow(), mark="m1")
    status, headers, raw = _chat(client, agent.id)
    assert status == 503 and headers["retry-after"] == "5"
    assert b'"agent_waking"' in raw
    assert runtime_calls(agent, "DELETE", "/drain") == []
    assert len(runtime_calls(agent, "POST", "/chat")) == 1


def test_a_refused_connection_right_after_a_wake_answers_waking(client, fake_clusters, mongo):
    """게이트가 준비된 파드로 awake 로 옮긴 직후 Service 에 닿지 못하면(엔드포인트가 아직 없다)
    503 agent_waking 과 Retry-After 다. `JUST_WOKEN` 이 지난 뒤의 연결 실패는 502 그대로다 (8절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    with pytest.raises(AgentError):
        admit(client, agent.id)
    set_ready(agent.fake)
    forget_deployments(client)
    assert admit(client, agent.id).sleep.state == "awake"
    # kube-proxy 가 아직 엔드포인트를 쓰지 않았다
    agent.fake.service_app = None

    with pytest.raises(AgentError) as e:
        _chat(client, agent.id)
    assert e.value.status == 503 and e.value.code == AGENT_WAKING
    assert e.value.headers == {"Retry-After": "5"}

    set_sleep(mongo, agent.id, since=utcnow() - JUST_WOKEN - timedelta(seconds=1))
    with pytest.raises(AgentError) as e:
        _chat(client, agent.id)
    assert e.value.status == 502 and e.value.code == "runtime_unreachable"


@pytest.mark.parametrize(("state", "status", "tries"), [("awake", 200, 2), ("draining", 503, 1)])
def test_a_session_delete_refused_by_the_mark(client, fake_clusters, mongo, state, status, tries):
    """세션 지우기도 리스를 잡는다. 표시에 거절되면 턴과 같이 awake 문서는 지우고 다시 보내고,
    잠드는 중이면 503 agent_waking 이다 (7절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    add_session_admin(agent.runtime)
    set_sleep(mongo, agent.id, mark="m1")
    agent.runtime.state.drain_mark = "m1"
    if state == "draining":
        set_sleep(mongo, agent.id, state="draining", since=utcnow())
    r = client.delete(f"{agent.url}/sessions/s1")
    assert r.status_code == status, r.text
    if status == 503:
        assert r.json()["detail"]["code"] == "agent_waking"
        assert r.json()["detail"]["retry_after"] == "5"
    assert len(runtime_calls(agent, "DELETE", "/sessions/s1")) == tries


# -- 레플리카 (9절) --------------------------------------------------------------------------


def test_reapply_and_advanced_edits_keep_a_sleeping_agent_at_zero(client, fake_clusters, mongo):
    """재적용과 Advanced 편집의 렌더는 잠든 Agent 를 0 으로 둔다. 템플릿만 바뀐다 (9절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)

    async def reapply() -> bool:
        return await service(client).reapply_auth(await service(client).agents.get(agent.id))

    in_app(client, reapply)
    assert deployment_replicas(agent) == 0
    patch = {"spec": {"template": {"metadata": {"annotations": {"team": "a"}}}}}
    r = client.put(f"{agent.url}/overrides", json={"patch": patch})
    assert r.status_code == 200, r.text
    assert deployment_replicas(agent) == 0
    assert stored(client, agent.id).sleep.state == "asleep"


def test_deploy_starts_from_a_fresh_state(client, fake_clusters, mongo):
    """배포는 자기 저장에서 쉬는 상태를 비우고 활동을 적는다. 해제 전에 남은 상태가 새 배포로
    넘어가지 않고 창이 배포에서 시작한다 (12절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    assert client.post(f"{agent.url}/undeploy").status_code == 200
    set_sleep(mongo, agent.id, state="asleep", reason="idle", mark="old", scaled="applied")
    before = utcnow()
    r = client.post(f"{agent.url}/deploy", json={})
    assert r.status_code == 201, r.text
    sleep = stored(client, agent.id).sleep
    assert (sleep.state, sleep.reason, sleep.mark, sleep.scaled) == ("awake", None, None, None)
    assert sleep.activity is not None and sleep.activity >= before.replace(microsecond=0)


def test_redeploy_of_an_awake_agent_marks_activity_and_frees_its_slot(client, fake_clusters, mongo):
    """깨어 있는 Agent 의 재배포도 활동을 적고 까닭을 deploy 로 바꾼다. 매일 깨우기의 몫을 쥔
    채로 남지 않는다 (11절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    old = utcnow() - timedelta(hours=2)
    set_sleep(mongo, agent.id, reason="housekeeping", since=old, activity=old)
    assert client.post(f"{agent.url}/redeploy").status_code == 200
    sleep = stored(client, agent.id).sleep
    assert sleep.state == "awake" and sleep.reason == "deploy"
    assert sleep.activity is not None and sleep.activity > old


def test_redeploy_wakes_the_agent(client, fake_clusters, mongo):
    """재배포는 먼저 깨워 새 리비전의 파드를 띄운다 (12절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    r = client.post(f"{agent.url}/redeploy")
    assert r.status_code == 200, r.text
    assert deployment_replicas(agent) == 1
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "deploy"
    assert doc.sleep.scaled is None


def test_the_replica_patch_reads_the_state_under_the_claim(client, fake_clusters, mongo):
    """패치는 클레임을 쥔 뒤 쉬는 상태를 다시 읽고 그 상태의 수로 `spec.replicas` 만 바꾼다.
    메모리의 문서가 asleep 이어도 저장된 상태가 waking 이면 되돌린다 (9절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    before = agent.fake.get("apps/v1", "Deployment", AGENT_DEPLOYMENT, DEFAULT_NS)
    svc = service(client)

    async def patch_with_a_stale_document() -> bool:
        doc = await svc.agents.get(agent.id)
        set_sleep(mongo, agent.id, state="waking")
        return await svc.sleep.apply_replicas(doc)

    assert in_app(client, patch_with_a_stale_document) is True
    after = agent.fake.get("apps/v1", "Deployment", AGENT_DEPLOYMENT, DEFAULT_NS)
    assert after["spec"].pop("replicas") == 1 and before["spec"].pop("replicas") == 0
    assert after["spec"] == before["spec"]
    assert after["metadata"]["labels"] == before["metadata"]["labels"]


def test_overrides_that_set_replicas_wake_an_asleep_agent(client, fake_clusters, mongo):
    """`spec.replicas` 를 정하는 overrides 는 Agent 를 잠들 수 없게 하므로 같은 저장에서 awake 로
    옮기고, overrides 의 수로 파드가 뜬다 (6절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    r = client.put(f"{agent.url}/overrides", json={"patch": {"spec": {"replicas": 2}}})
    assert r.status_code == 200, r.text
    assert deployment_replicas(agent) == 2
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "ineligible"
    assert doc.sleep.scaled is None
    assert client.get(agent.url).json()["status"]["sleep"]["ineligible"] == "replicas_override"


def test_an_rc1_document_with_replicas_overrides_runs_as_before(client, fake_clusters, mongo):
    """2.0.0-rc1 의 문서에는 쉬는 상태가 없고 overrides 가 `spec.replicas` 를 정할 수 있다. 렌더,
    재적용, 재배포가 그 수로 돌고 잠들 수 없는 까닭은 replicas_override 다 (6절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    mongo.agents.update_one(
        {"_id": agent.id},
        {
            "$unset": {"sleep": "", "scale_to_zero": ""},
            "$set": {"overrides": {"spec": {"replicas": 3}}},
        },
    )

    async def reapply() -> bool:
        return await service(client).reapply_auth(await service(client).agents.get(agent.id))

    in_app(client, reapply)
    assert deployment_replicas(agent) == 3
    assert client.post(f"{agent.url}/redeploy").status_code == 200
    assert deployment_replicas(agent) == 3
    status = client.get(agent.url).json()["status"]
    assert status["sleep"]["state"] == "awake"
    assert status["sleep"]["ineligible"] == "replicas_override"


# -- 해제 (12절) -----------------------------------------------------------------------------


def test_undeploy_of_an_asleep_agent_clears_the_state(client, fake_clusters, mongo):
    """파드가 없으면 해제의 턴 멈추기가 런타임을 부르지 않고, 해제가 쉬는 상태를 비운다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    assert client.post(f"{agent.url}/undeploy").status_code == 200
    assert runtime_calls(agent, "POST", "/turns/cancel") == []
    raw = mongo.agents.find_one({"_id": agent.id})
    assert raw["state"] == "draft" and "sleep" not in raw
    assert raw["scale_to_zero"] is True


# -- 매일 깨우기의 때 (11절) -----------------------------------------------------------------


def test_the_housekeeping_wake_spreads_by_id():
    """잠든 때에서 주기를 지나되 id 의 몫만큼 당긴다. 몫은 주기의 절반 안이라 한 주기보다 오래
    자지 않고, 함께 잠든 Agent 가 함께 깨지 않는다."""
    period = timedelta(hours=24)
    since = utcnow()
    offsets = {housekeeping_offset(f"agent-{n}", period) for n in range(20)}
    assert len(offsets) == 20
    assert all(timedelta(0) <= o < period / 2 for o in offsets)
    doc = minimal_agent_doc(sleep=AgentSleep(state="asleep", since=since))
    due = housekeeping_at(doc, period)
    assert due is not None and since + period / 2 < due <= since + period
    assert housekeeping_at(minimal_agent_doc(), period) is None


def test_every_pod_on_the_current_template_and_one_ready():
    """여러 레플리카를 깨우면 모두 준비될 때까지 Rolling 이지만 첨부 턴은 지나간다 (8절)."""
    meta = {"generation": 3}
    assert current_template_ready(
        {
            "metadata": meta,
            "status": {
                "observedGeneration": 3,
                "replicas": 3,
                "updatedReplicas": 3,
                "readyReplicas": 1,
            },
        }
    )
    assert not current_template_ready(
        {
            "metadata": meta,
            "status": {
                "observedGeneration": 3,
                "replicas": 3,
                "updatedReplicas": 2,
                "readyReplicas": 2,
            },
        }
    )
    assert not current_template_ready(
        {
            "metadata": meta,
            "status": {
                "observedGeneration": 2,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
            },
        }
    )
    assert not current_template_ready(None)
