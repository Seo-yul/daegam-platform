"""Agent 의 쉬는 상태를 옮기는 스토어 (P44 7절, D103).

쉬는 상태는 자기 조건부 갱신(`move_sleep`)만 쓰고 배포 쪽 저장은 쓰지 않는다. 칸이 없는 옛 문서는
awake 로 읽고, 레플리카 둘이 같은 상태를 함께 옮기면 하나만 지나간다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.store.agents import HOUSEKEEPING, AgentRuntime, AgentSleep, MongoAgentStore
from daegam.store.base import to_millis, utcnow
from tests.helpers.aio import run_store
from tests.helpers.docs import agent_doc

pytestmark = pytest.mark.usefixtures("settings")


def _now():
    """Mongo 가 담는 밀리초까지의 지금이다. 읽은 값과 견주는 조건이 맞는다."""
    return to_millis(utcnow())


def _deployed(aid: str = "ag-1", **kw):
    return agent_doc(aid, state="deployed", **kw)


def test_a_document_without_the_state_reads_awake(settings):
    """2.0.0-rc1 의 문서에는 `sleep` 이 없다. 읽으면 awake 이고 awake 를 기대한 이동이 맞는다."""

    async def go(store: MongoAgentStore):
        doc = _deployed()
        raw = doc.to_doc()
        raw.pop("sleep")
        raw.pop("scale_to_zero")
        await store.coll.insert_one(raw)
        read = await store.get(doc.id)
        assert read.sleep == AgentSleep() and read.scale_to_zero is True
        moved = await store.move_sleep(doc.id, ("awake",), {"state": "draining"})
        return moved, (await store.sleep_of(doc.id)).state

    assert run_store(settings, MongoAgentStore, go) == (True, "draining")


def test_two_replicas_move_the_same_state_once(settings):
    """asleep 에서 waking 으로 둘이 함께 옮기면 하나만 이긴다 (P44 8절). 이긴 쪽이 활동을 적는다."""

    async def go(store: MongoAgentStore):
        doc = _deployed(sleep=AgentSleep(state="asleep"))
        await store.create(doc)
        now = _now()
        results = await asyncio.gather(
            *(
                store.move_sleep(doc.id, ("asleep",), {"state": "waking"}, touch=now)
                for _ in range(2)
            )
        )
        return sorted(results), await store.sleep_of(doc.id)

    results, sleep = run_store(settings, MongoAgentStore, go)
    assert results == [False, True]
    assert sleep.state == "waking" and sleep.activity is not None


def test_a_move_checks_what_it_read(settings):
    """활동이 읽은 것과 달라졌으면 잠들기 첫 걸음이 지나가지 않는다 (P44 7절 1단계)."""

    async def go(store: MongoAgentStore):
        read = _now() - timedelta(hours=1)
        doc = _deployed(sleep=AgentSleep(activity=read))
        await store.create(doc)
        await store.mark_activity(doc.id, _now())
        stale = await store.move_sleep(
            doc.id, ("awake",), {"state": "draining"}, also={"activity": read}
        )
        fresh = (await store.sleep_of(doc.id)).activity
        moved = await store.move_sleep(
            doc.id, ("awake",), {"state": "draining"}, also={"activity": fresh}
        )
        return stale, moved

    assert run_store(settings, MongoAgentStore, go) == (False, True)


def test_activity_only_moves_forward(settings):
    async def go(store: MongoAgentStore):
        now = _now()
        doc = _deployed(sleep=AgentSleep(activity=now))
        await store.create(doc)
        await store.mark_activity(doc.id, now - timedelta(minutes=5))
        return (await store.sleep_of(doc.id)).activity, now

    got, want = run_store(settings, MongoAgentStore, go)
    assert got == want


def test_the_window_starts_once(settings):
    """활동이 없는 문서는 워커가 처음 볼 때 창이 시작된다 (P44 6절). 두 번째는 쓰지 않는다."""

    async def go(store: MongoAgentStore):
        doc = _deployed()
        await store.create(doc)
        first = await store.start_window(doc.id, _now())
        second = await store.start_window(doc.id, _now() + timedelta(minutes=1))
        return first, second

    assert run_store(settings, MongoAgentStore, go) == (True, False)


def test_a_deployment_save_never_writes_the_state(settings):
    """상태 전이는 읽은 문서의 배포 쪽 칸만 쓴다. 그 사이 잠든 상태를 되돌리지 않는다 (P44 7절)."""

    async def go(store: MongoAgentStore):
        doc = _deployed()
        await store.create(doc)
        read = await store.get(doc.id)
        await store.move_sleep(doc.id, ("awake",), {"state": "asleep", "scaled": "pending"})
        read.last_error = "changed"
        assert await store.transition(read, expect={"deployed"})
        assert await store.set_fields(doc.id, {"replicas": 2}, expect_state={"deployed"})
        return await store.get(doc.id)

    fresh = run_store(settings, MongoAgentStore, go)
    assert fresh.sleep.state == "asleep" and fresh.sleep.scaled == "pending"
    assert fresh.last_error == "changed" and fresh.replicas == 2


@pytest.mark.parametrize("state", ["draft", "undeploying", "deleting"])
def test_no_move_outside_the_deployed_states(settings, state):
    """해제가 상태를 비우므로 draft 와 해제·삭제 중인 문서의 상태는 옮기지 않는다."""

    async def go(store: MongoAgentStore):
        doc = agent_doc(state=state)
        await store.create(doc)
        return await store.move_sleep(doc.id, ("awake",), {"state": "draining"})

    assert run_store(settings, MongoAgentStore, go) is False


def test_clearing_drops_the_state(settings):
    async def go(store: MongoAgentStore):
        doc = _deployed(sleep=AgentSleep(state="asleep", activity=_now()))
        await store.create(doc)
        await store.clear_sleep(doc.id)
        raw = await store.coll.find_one({"_id": doc.id})
        return "sleep" in raw, (await store.get(doc.id)).sleep

    present, sleep = run_store(settings, MongoAgentStore, go)
    assert not present and sleep == AgentSleep()


def test_the_worker_candidates(settings):
    """쉬기 워커가 보는 문서다 (P44 7절·11절). 매일 깨우기로 깨어 있는 awake 는 hold 안에서도
    든다. 창이 꺼져 있으면 awake 는 그것만 든다."""
    now = _now()
    old = now - timedelta(hours=2)

    def able(aid: str, **kw):
        runtime = AgentRuntime(image="img", features=[SCALE_TO_ZERO])
        return _deployed(aid, runtime=runtime, **kw)

    docs = {
        "idle": able("idle", sleep=AgentSleep(activity=old)),
        "unmarked": able("unmarked"),
        "busy": able("busy", sleep=AgentSleep(activity=now)),
        "off": able("off", scale_to_zero=False, sleep=AgentSleep(activity=old)),
        # 다시 배포하기 전의 이미지와 레플리카를 정하는 overrides 는 질의가 뺀다
        "old_image": _deployed("old_image", sleep=AgentSleep(activity=old)),
        "override": able(
            "override", overrides={"spec": {"replicas": 2}}, sleep=AgentSleep(activity=old)
        ),
        "asleep": _deployed("asleep", sleep=AgentSleep(state="asleep", since=old)),
        "waking": _deployed("waking", sleep=AgentSleep(state="waking", since=now)),
        "stale": _deployed("stale", sleep=AgentSleep(state="draining", since=old)),
        "draining": _deployed("draining", sleep=AgentSleep(state="draining", since=now)),
        "held": _deployed("held", sleep=AgentSleep(reason=HOUSEKEEPING, since=now, activity=now)),
        "draft": agent_doc("draft", sleep=AgentSleep(state="asleep")),
    }

    async def go(store: MongoAgentStore):
        for doc in docs.values():
            await store.create(doc)
        cut = now - timedelta(minutes=30)
        on = await store.sleep_candidates(
            idle_before=cut, stale_before=cut, capability=SCALE_TO_ZERO
        )
        off = await store.sleep_candidates(
            idle_before=None, stale_before=cut, capability=SCALE_TO_ZERO
        )
        return {d.id for d in on}, {d.id for d in off}

    on, off = run_store(settings, MongoAgentStore, go)
    assert on == {"idle", "unmarked", "asleep", "waking", "stale", "held"}
    assert off == {"asleep", "waking", "stale", "held"}


def test_housekeeping_wakes_count_per_cluster(settings):
    """매일 깨우기로 깨어 있는 수는 클러스터마다 센다. 다시 잠든 것과, 매일 깨우기 하나가 이어질
    수 있는 시간보다 오래 그 까닭으로 남은 것은 빠진다 (P44 11절)."""
    now = _now()

    def held(aid: str, state: str, cluster: str = "c1", since=now):
        sleep = AgentSleep(state=state, reason=HOUSEKEEPING, since=since)
        return _deployed(aid, cluster_id=cluster, sleep=sleep)

    async def go(store: MongoAgentStore):
        for doc in (
            held("a", "waking"),
            held("b", "awake"),
            held("c", "asleep"),
            held("d", "awake", "c2"),
            held("stuck", "awake", since=now - timedelta(hours=1)),
            _deployed("e", cluster_id="c1", sleep=AgentSleep(state="waking", reason="turn")),
        ):
            await store.create(doc)
        after = now - timedelta(minutes=20)
        return (
            await store.housekeeping_wakes("c1", since_after=after),
            await store.housekeeping_wakes("c2", since_after=after),
        )

    assert run_store(settings, MongoAgentStore, go) == (2, 1)


def test_a_move_to_the_same_values_counts_as_moved(settings):
    """조건이 맞았으면 같은 값을 다시 써도 옮긴 것이다. 까닭을 같은 값으로 다시 적는 이동이
    실패로 읽히지 않는다."""

    async def go(store: MongoAgentStore):
        doc = _deployed(sleep=AgentSleep(reason="turn"))
        await store.create(doc)
        return await store.move_sleep(doc.id, ("awake",), {"reason": "turn"})

    assert run_store(settings, MongoAgentStore, go) is True
