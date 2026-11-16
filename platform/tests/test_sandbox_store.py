"""샌드박스 기록과 슬롯 수의 저장 (P40 5절·7절).

열기는 기록을 먼저 넣고 상한과 노드 슬롯을 조건부 `$inc` 하나로 잡는다. 두 레플리카가 같은 노드의
마지막 슬롯을 함께 잡아도 하나만 이긴다. 스윕의 다시 세기는 그 사이 잡기나 돌려주기가 지나갔으면
쓰지 않는다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from daegam.store.base import utcnow
from daegam.store.sandboxes import (
    MongoSandboxStore,
    SandboxRecord,
    SlotCaps,
    slot_key,
)
from tests.helpers.aio import run_store

CAPS = SlotCaps(person=2, agent=5, run=2, tenant=10, cluster=20, node=1)


def _rec(session: str, *, node: str = "n1", person: str | None = "alice", **kw) -> SandboxRecord:
    return SandboxRecord(
        agent_id=kw.pop("agent_id", "ag-1"),
        tenant="acme",
        cluster_id="c1",
        namespace="daegam-acme",
        session_id=session,
        person=person,
        node=node,
        pod=f"sbx-{session}",
        **kw,
    )


def test_two_opens_for_the_last_node_slot_leave_one_winner(settings):
    async def go(store: MongoSandboxStore):
        a, b = _rec("s1"), _rec("s2")
        await store.insert(a)
        await store.insert(b)
        results = await asyncio.gather(store.reserve(a, CAPS), store.reserve(b, CAPS))
        nodes = (await store.slot_counts())["nodes"]
        return sorted(r or "" for r in results), nodes[slot_key("c1", "n1")]

    results, held = run_store(settings, MongoSandboxStore, go)
    assert results == ["", "cluster"] and held == 1


def test_refusals_name_their_cap_and_a_zero_cap_refuses(settings):
    async def go(store: MongoSandboxStore):
        out = []
        out.append(await store.reserve(_rec("s1"), SlotCaps(1, 1, 1, 1, 1, 0)))
        out.append(await store.reserve(_rec("s2"), SlotCaps(0, 5, 2, 10, 20, 5)))
        assert await store.reserve(_rec("s3"), SlotCaps(5, 1, 2, 10, 20, 5)) is None
        out.append(await store.reserve(_rec("s4", person="bob"), SlotCaps(5, 1, 2, 10, 20, 5)))
        out.append(await store.reserve(_rec("s5", agent_id="ag-2"), SlotCaps(5, 5, 2, 1, 20, 5)))
        run = _rec("s6", agent_id="ag-3", run_id="run-1")
        out.append(await store.reserve(run, SlotCaps(0, 5, 0, 10, 20, 5)))
        return out

    assert run_store(settings, MongoSandboxStore, go) == [
        "cluster",
        "person",
        "agent",
        "tenant",
        "run",
    ]


def test_release_and_recount_follow_open_records(settings):
    async def go(store: MongoSandboxStore):
        a, b = _rec("s1"), _rec("s2", node="n2")
        for r in (a, b):
            await store.insert(r)
            assert await store.reserve(r, CAPS) is None
        closing = await store.begin_close(a.id, "session_closed")
        assert closing is not None and await store.begin_close(a.id, "again") is None
        await store.release(closing)
        await store.finish_close(a.id)
        after_release = (await store.slot_counts())["agents"][slot_key("ag-1")]
        # 어긋난 수를 스윕이 열린 기록에서 다시 센다
        await store.slots.update_one({"_id": "slots"}, {"$set": {"agents": {}, "nodes": {}}})
        assert await store.recount()
        counts = await store.slot_counts()
        return after_release, counts["agents"], counts["nodes"]

    after_release, agents, nodes = run_store(settings, MongoSandboxStore, go)
    assert after_release == 1
    assert agents == {slot_key("ag-1"): 1} and nodes == {slot_key("c1", "n2"): 1}


def test_recount_skips_when_a_reservation_passed_in_between(settings):
    async def go(store: MongoSandboxStore):
        await store.ensure_slots()
        original = store.slot_counts

        async def slow_counts():
            doc = await original()
            # 다시 세기가 seq 를 읽은 바로 뒤에 다른 열기가 슬롯을 잡는다
            rec = _rec("late")
            await store.insert(rec)
            await store.slots.update_one({"_id": "slots"}, {"$inc": {"seq": 1}})
            return doc

        store.slot_counts = slow_counts  # type: ignore[method-assign]
        wrote = await store.recount()
        store.slot_counts = original  # type: ignore[method-assign]
        return wrote

    assert run_store(settings, MongoSandboxStore, go) is False


def test_the_open_record_is_unique_per_session_and_generations_follow(settings):
    async def go(store: MongoSandboxStore):
        first = _rec("s1")
        assert await store.insert(first)
        assert not await store.insert(_rec("s1"))
        await store.drop_unreserved(first.id, "limit_person")
        second = _rec("s1", generation=2)
        assert await store.insert(second)
        last = await store.last_for("ag-1", "s1")
        return last.generation if last else None

    assert run_store(settings, MongoSandboxStore, go) == 2


def test_reclaimable_takes_the_persons_oldest_idle_session(settings):
    async def go(store: MongoSandboxStore):
        old = utcnow() - timedelta(minutes=20)
        recs = [
            _rec("a", last_used_at=old - timedelta(minutes=5)),
            _rec("b", last_used_at=old),
            _rec("c"),
            _rec("run", run_id="r1", last_used_at=old - timedelta(hours=1)),
            _rec("bob", person="bob", last_used_at=old),
        ]
        for r in recs:
            await store.insert(r)
        found = await store.reclaimable("alice", utcnow() - timedelta(minutes=5))
        return [r.session_id for r in found]

    assert run_store(settings, MongoSandboxStore, go) == ["a", "b"]


def test_mark_ready_is_first_once(settings):
    async def go(store: MongoSandboxStore):
        rec = _rec("s1")
        await store.insert(rec)
        _, first = await store.mark_ready(rec.id, "img")
        again, second = await store.mark_ready(rec.id, "img")
        return first, second, again.ready if again else None

    assert run_store(settings, MongoSandboxStore, go) == (True, False, True)


def test_a_reclaim_close_needs_the_record_untouched_since_its_cutoff(settings):
    async def go(store: MongoSandboxStore):
        rec = _rec("s1", last_used_at=utcnow() - timedelta(minutes=40))
        await store.insert(rec)
        await store.touch("ag-1", "s1")
        refused = await store.begin_close(
            rec.id, "idle", untouched_before=utcnow() - timedelta(minutes=30)
        )
        closed = await store.begin_close(rec.id, "session_closed")
        return refused, closed is not None

    assert run_store(settings, MongoSandboxStore, go) == (None, True)


def test_latest_for_pods_reads_only_the_listed_pods(settings):
    async def go(store: MongoSandboxStore):
        await store.insert(_rec("s1"))
        old = _rec("s2", generation=1)
        await store.insert(old)
        await store.drop_unreserved(old.id, "x")
        await store.insert(_rec("s3"))
        found = await store.latest_for_pods(
            "c1", [("daegam-acme", "sbx-s1"), ("daegam-acme", "sbx-s2"), ("other", "sbx-s3")]
        )
        return sorted((k[1], v.state) for k, v in found.items())

    assert run_store(settings, MongoSandboxStore, go) == [("sbx-s1", "open"), ("sbx-s2", "closed")]


def test_refusing_names_every_cap_that_blocks(settings):
    async def go(store: MongoSandboxStore):
        a = _rec("s1")
        await store.insert(a)
        assert await store.reserve(a, CAPS) is None
        caps = SlotCaps(1, 1, 2, 10, 20, 5)
        both = await store.refusing(_rec("s2"), caps)
        person_only = await store.refusing(_rec("s3", agent_id="ag-2"), caps)
        return both, person_only

    assert run_store(settings, MongoSandboxStore, go) == (["person", "agent"], ["person"])
