"""쉬기 워커 (P44 7절·11절, D103).

워커는 30초마다 볼 문서를 고르고 문서마다 리스를 걸어 한 걸음씩 다룬다. 창이 지난 Agent 를 재우고,
멈춘 워커가 남긴 draining 을 돌리고, 남은 0 으로 줄이기를 다시 하고, 다시 뜬 파드와 잠들 수 없게 된
Agent 를 깨우고, 끝나지 않는 깨우기를 끝내며, 잠든 Agent 를 하루 한 번 깨웠다가 다시 재운다.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.services.agent_sleep import WAKE_LIMIT, housekeeping_hold
from daegam.store.agent_runs import AGENT_RUNS
from daegam.store.agents import HOUSEKEEPING, AgentSleep
from daegam.store.base import to_millis, utcnow
from daegam.store.leases import Lease
from tests.helpers.agent_runtime import AGENT_DEPLOYMENT, set_ready
from tests.helpers.agent_sleep import (
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
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.docs import agent_doc


def _tick(client) -> int:
    worker = client.app.state.agent_sleep_worker
    forget_deployments(client)
    return in_app(client, worker.tick)


def _settings(client, **changes) -> None:
    """이 앱의 쉬기 설정을 바꾼다. 워커와 게이트가 함께 읽는다."""
    sleep = service(client).sleep
    sleep.settings = dataclasses.replace(sleep.settings, **changes)


def _ago(**kw):
    return to_millis(utcnow() - timedelta(**kw))


# -- 창 (7절) ---------------------------------------------------------------------------------


def test_an_idle_agent_sleeps_after_the_window(client, fake_clusters, mongo):
    """활동이 창보다 오래된 Agent 는 걸음을 밟아 잠든다. 창 안의 활동이 있으면 그대로다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_sleep_after_seconds=120)
    set_sleep(mongo, agent.id, activity=_ago(seconds=30))
    _tick(client)
    assert stored(client, agent.id).sleep.state == "awake"

    set_sleep(mongo, agent.id, activity=_ago(minutes=3))
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "asleep" and doc.sleep.reason == "idle"
    assert doc.sleep.scaled == "applied" and deployment_replicas(agent) == 0
    assert len(runtime_calls(agent, "POST", "/drain")) == 1


def test_the_window_starts_when_the_worker_first_sees_the_agent(client, fake_clusters, mongo):
    """활동이 적힌 적 없는 Agent 는 워커가 처음 볼 때 창이 시작된다 (6절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_sleep_after_seconds=120)
    mongo.agents.update_one({"_id": agent.id}, {"$unset": {"sleep.activity": ""}})
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.activity is not None


@pytest.mark.parametrize("why", ["window_zero", "switch_off", "open_run", "ineligible"])
def test_what_keeps_an_idle_agent_awake(client, fake_clusters, mongo, why):
    """창이 0 이거나 플랫폼 스위치가 꺼졌거나 열린 실행이 있거나 잠들 수 없으면 잠들지 않는다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_sleep_after_seconds=0 if why == "window_zero" else 120)
    set_sleep(mongo, agent.id, activity=_ago(hours=1))
    if why == "switch_off":
        _settings(client, agent_scale_to_zero=False)
    if why == "open_run":
        mongo[AGENT_RUNS].insert_one(
            {
                "_id": "r1",
                "tenant": stored(client, agent.id).tenant,
                "agent_id": agent.id,
                "status": "running",
            }
        )
    if why == "ineligible":
        mongo.agents.update_one({"_id": agent.id}, {"$pull": {"runtime.features": SCALE_TO_ZERO}})
    _tick(client)
    assert stored(client, agent.id).sleep.state == "awake"
    assert runtime_calls(agent, "POST", "/drain") == []


# -- 멈춘 걸음과 남은 일 (7절, 9절) ----------------------------------------------------------------


def test_a_stale_draining_moves_back_to_awake(client, fake_clusters, mongo):
    """표시의 기한보다 오래된 draining 은 멈춘 워커가 남긴 것이다. 새 활동과 함께 awake 로
    돌린다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state="draining", since=_ago(minutes=3), reason="idle")
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "stale_drain"
    assert doc.sleep.activity is not None and doc.sleep.activity > _ago(minutes=1)


def test_a_fresh_draining_is_left_to_its_worker(client, fake_clusters, mongo):
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state="draining", since=_ago(seconds=10), reason="idle")
    _tick(client)
    assert stored(client, agent.id).sleep.state == "draining"


def test_a_pending_change_to_zero_is_made_again(client, fake_clusters, mongo):
    """남이 클레임을 쥐어 하지 못한 0 으로 줄이기는 다음 틱이 다시 한다 (9절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state="asleep", since=_ago(minutes=1), scaled="pending")
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "asleep" and doc.sleep.scaled == "applied"
    assert deployment_replicas(agent) == 0


def test_pods_after_an_applied_change_wake_the_agent_and_it_sleeps_again(
    client, fake_clusters, mongo
):
    """적용한 뒤 Deployment 가 다시 파드를 원하면(옛 레플리카의 렌더, 손으로 한 scale) 바로 0 으로
    줄이지 않고 awake 로 돌려 걸음을 다시 밟는다 (7절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    agent.fake.objects[("apps/v1", "Deployment", DEFAULT_NS, AGENT_DEPLOYMENT)]["spec"][
        "replicas"
    ] = 1
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "stray_pods"
    assert deployment_replicas(agent) == 1

    _settings(client, agent_sleep_after_seconds=120)
    set_sleep(mongo, agent.id, activity=_ago(minutes=3))
    _tick(client)
    assert stored(client, agent.id).sleep.state == "asleep"
    assert len(runtime_calls(agent, "POST", "/drain")) == 2


@pytest.mark.parametrize("why", ["agent_off", "setting_off"])
def test_an_asleep_agent_that_cannot_sleep_any_more_wakes(client, fake_clusters, mongo, why):
    """Agent 의 스위치를 끄거나 플랫폼 스위치를 끄면(되돌리기 단계) 잠든 Agent 가 다음 틱에 깬다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    if why == "agent_off":
        mongo.agents.update_one({"_id": agent.id}, {"$set": {"scale_to_zero": False}})
    else:
        _settings(client, agent_scale_to_zero=False)
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "waking" and doc.sleep.reason == "ineligible"
    assert deployment_replicas(agent) == 1


def test_a_wake_without_a_ready_pod_ends_after_five_minutes(client, fake_clusters, mongo):
    """준비된 파드 없이 5분이 지난 깨우기는 awake 로 끝낸다. 파드는 그대로 원하고 상태가 그
    까닭을 보인다. 준비된 파드가 있으면 그 전에 awake 다 (8절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    set_sleep(mongo, agent.id, state="waking", since=_ago(minutes=1), reason="turn")
    _tick(client)
    assert stored(client, agent.id).sleep.state == "waking"
    # 늦게 닿은 0 으로 줄이기가 있었으면 다시 되돌린다
    assert deployment_replicas(agent) == 1

    set_sleep(mongo, agent.id, since=utcnow() - WAKE_LIMIT - timedelta(seconds=1))
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "wake_timeout"
    assert deployment_replicas(agent) == 1


def test_a_waking_agent_with_a_ready_pod_becomes_awake(client, fake_clusters, mongo):
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, state="waking", since=_ago(seconds=20), reason="wake", mark="m1")
    set_ready(agent.fake)
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "wake"
    (undrain,) = runtime_calls(agent, "DELETE", "/drain")
    assert undrain["params"] == {"mark": "m1"}


# -- 매일 깨우기 (11절) ------------------------------------------------------------------------


def test_the_daily_wake_sweeps_and_sleeps_again_at_window_zero(client, fake_clusters, mongo):
    """하루 잔 Agent 를 깨워 런타임이 시작에서 정리하게 하고, 10분 뒤 창과 상관없이 다시 재운다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    set_sleep(mongo, agent.id, since=_ago(hours=24, minutes=1))
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "waking" and doc.sleep.reason == HOUSEKEEPING
    assert deployment_replicas(agent) == 1

    set_ready(agent.fake)
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == HOUSEKEEPING

    _tick(client)
    assert stored(client, agent.id).sleep.state == "awake"
    # hold 안에 활동이 없었다. 깨우기가 적은 활동은 hold 가 시작되기 전이다
    held_from = utcnow() - housekeeping_hold(service(client).sleep.settings) - timedelta(seconds=1)
    set_sleep(mongo, agent.id, since=held_from, activity=held_from - timedelta(minutes=1))
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "asleep" and doc.sleep.reason == HOUSEKEEPING
    assert deployment_replicas(agent) == 0


def test_the_hold_follows_its_setting(client, fake_clusters, mongo):
    """hold 는 설정을 따른다. 게이트는 짧은 hold 로 매일 깨우기를 본다 (11절, 14절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_housekeeping_hold_seconds=60)
    set_sleep(
        mongo, agent.id, reason=HOUSEKEEPING, since=_ago(seconds=30), activity=_ago(minutes=1)
    )
    _tick(client)
    assert stored(client, agent.id).sleep.state == "awake"

    set_sleep(mongo, agent.id, since=_ago(seconds=61), activity=_ago(minutes=2))
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "asleep" and doc.sleep.reason == HOUSEKEEPING


def test_a_turn_at_the_end_of_the_hold_restarts_it(client, fake_clusters, mongo):
    """hold 가 끝날 때 턴이 리스를 쥐고 있으면 잠들지 않고 hold 가 다시 시작한다. 창이 0 이어도
    다음 hold 뒤에 다시 잠든다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(
        mongo, agent.id, reason=HOUSEKEEPING, since=_ago(minutes=11), activity=_ago(minutes=12)
    )
    agent.runtime.state.drain_leases = 1
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == HOUSEKEEPING
    assert doc.sleep.since is not None and doc.sleep.since > _ago(minutes=1)


@pytest.mark.parametrize(("window", "state"), [(120, "awake"), (0, "asleep")])
def test_activity_in_the_hold_hands_the_agent_to_the_window(
    client, fake_clusters, mongo, window, state
):
    """창이 있고 hold 안에 활동이 있었으면 hold 가 끝나도 재우지 않고 까닭을 바꿔 창에 넘긴다.
    사람이 쓰기 시작한 Agent 를 창과 상관없이 재우지 않는다. 창이 0 이면 명세대로 재운다 (11절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_sleep_after_seconds=window)
    set_sleep(
        mongo, agent.id, reason=HOUSEKEEPING, since=_ago(minutes=11), activity=_ago(minutes=2)
    )
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == state
    if state == "awake":
        assert doc.sleep.reason == "active"
        assert runtime_calls(agent, "POST", "/drain") == []


def test_no_answer_at_the_end_of_the_hold_frees_the_slot(client, fake_clusters, mongo):
    """hold 끝의 잠들기를 답이 없어 그만두면 까닭을 바꿔 클러스터의 몫을 놓는다. 준비되지 않은
    파드가 남은 동안 매번 그만두며 몫을 쥐지 않는다 (11절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(
        mongo, agent.id, reason=HOUSEKEEPING, since=_ago(minutes=11), activity=_ago(minutes=12)
    )
    agent.fake.service_app = None
    agent.fake.add_pod(DEFAULT_NS, "agent-support-x", {"daegam.devops.ai.kr/id": agent.id})
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "no_answer"


def test_switching_off_during_the_hold_frees_the_slot(client, fake_clusters, mongo):
    """hold 안에 Agent 의 스위치를 끄면 까닭을 바꿔 클러스터의 몫을 바로 놓는다 (11절)."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    set_sleep(mongo, agent.id, reason=HOUSEKEEPING, since=_ago(minutes=1), activity=_ago(minutes=2))
    cluster = stored(client, agent.id).cluster_id
    store = service(client).agents

    def held() -> int:
        async def count() -> int:
            return await store.housekeeping_wakes(cluster, since_after=_ago(hours=1))

        return in_app(client, count)

    assert held() == 1
    mongo.agents.update_one({"_id": agent.id}, {"$set": {"scale_to_zero": False}})
    _tick(client)
    doc = stored(client, agent.id)
    assert doc.sleep.state == "awake" and doc.sleep.reason == "ineligible"
    assert held() == 0


def test_an_agent_that_can_never_sleep_takes_no_lease(client, fake_clusters, mongo, monkeypatch):
    """스스로 잠들 수 없는 awake 문서는 리스를 걸기 전에 뺀다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_sleep_after_seconds=120)
    set_sleep(mongo, agent.id, activity=_ago(hours=1))
    mongo.agents.update_one({"_id": agent.id}, {"$pull": {"runtime.features": SCALE_TO_ZERO}})
    worker = client.app.state.agent_sleep_worker
    taken: list[str] = []
    real = worker.lease.acquire

    async def recording(name: str, now=None) -> bool:
        taken.append(name)
        return await real(name, now)

    monkeypatch.setattr(worker.lease, "acquire", recording)
    assert _tick(client) == 0 and taken == []


def test_the_daily_wakes_are_capped_per_cluster(client, fake_clusters, mongo):
    """한 클러스터에서 매일 깨우기로 깨어 있는 Agent 가 상한(2)이면 다음 Agent 는 기다린다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    put_to_sleep(client, agent)
    set_sleep(mongo, agent.id, since=_ago(hours=25))
    cluster = stored(client, agent.id).cluster_id
    held = AgentSleep(state="waking", reason=HOUSEKEEPING, since=utcnow())
    for aid in ("held-1", "held-2"):
        mongo.agents.insert_one(
            agent_doc(aid, state="deployed", cluster=cluster, sleep=held).to_doc()
        )
    _tick(client)
    assert stored(client, agent.id).sleep.state == "asleep"

    mongo.agents.delete_one({"_id": "held-2"})
    _tick(client)
    assert stored(client, agent.id).sleep.state == "waking"
    assert stored(client, agent.id).sleep.reason == HOUSEKEEPING


def test_one_replica_handles_an_agent_at_a_time(client, fake_clusters, mongo):
    """문서마다 리스를 걸어 두 레플리카의 틱이 같은 Agent 를 함께 재우지 않는다."""
    agent = sleepy_agent(client, fake_clusters, mongo)
    _settings(client, agent_sleep_after_seconds=120)
    set_sleep(mongo, agent.id, activity=_ago(minutes=3))
    worker = client.app.state.agent_sleep_worker

    async def held_elsewhere() -> int:
        other = Lease(worker.lease.coll.database, "other-replica")
        assert await other.acquire(f"agent-sleep:{agent.id}")
        return await worker.tick()

    assert in_app(client, held_elsewhere) == 0
    assert stored(client, agent.id).sleep.state == "awake"
    assert runtime_calls(agent, "POST", "/drain") == []
