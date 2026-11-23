"""샌드박스 프로브 문서의 조건부 쓰기를 두 플랫폼 복제본이 겨루는 경우 (P40 8절).

복제본마다 Mongo 클라이언트가 따로이므로 여기서도 클라이언트 둘로 같은 문서를 동시에 읽고 쓴다.
겨루는 순서는 돌 때마다 다르므로 노드를 바꿔 가며 여러 번 돌리고, 어느 순서에서나 지켜져야 하는
것만 단언한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pymongo import AsyncMongoClient

from daegam.config import Settings
from daegam.store.base import utcnow
from daegam.store.clusters import MongoClusterStore
from daegam.store.sandboxes import MongoSandboxStore, probe_id

ROUNDS = 40
CLUSTER = "c-race"
NS = "daegam-acme"
OPEN = ("wanted", "running")

Stores = tuple[MongoSandboxStore, MongoSandboxStore]


def _race(settings: Settings, body: Callable[[Stores, Any, str], Awaitable[None]]) -> None:
    """복제본 둘의 저장소로 body 를 노드를 바꿔 가며 ROUNDS 번 돌린다."""

    async def go() -> None:
        clients = [AsyncMongoClient(settings.mongo_url) for _ in range(2)]
        try:
            a, b = (MongoSandboxStore(c[settings.mongo_db]) for c in clients)
            await a.init_schema()
            for i in range(ROUNDS):
                await body((a, b), clients[0][settings.mongo_db], f"n{i}")
        finally:
            for c in clients:
                await c.close()

    asyncio.run(go())


def _confirm(store: MongoSandboxStore, node: str, message: str = "87") -> Awaitable[bool]:
    return store.confirm_first_check(
        CLUSTER, node, NS, "acme", message, quiet_since=utcnow() - timedelta(minutes=10)
    )


async def _doc(store: MongoSandboxStore, node: str) -> dict[str, Any]:
    doc = await store.probes.find_one({"_id": probe_id(CLUSTER, node)})
    assert doc is not None
    return doc


async def _running(store: MongoSandboxStore, node: str) -> str:
    """그 노드에서 87 과 상관없이 1초 전부터 도는 프로브 하나. Mongo 는 때를 밀리초로 적으므로,
    방금 시작한 것으로 두면 같은 밀리초의 87 보다 먼저 시작했는지 가릴 수 없다."""
    key = probe_id(CLUSTER, node)
    assert await store.want_probe(CLUSTER, node, NS, "acme", quiet_since=utcnow())
    await store.probe_started(key, f"probe-{node}")
    await store.probes.update_one(
        {"_id": key}, {"$set": {"started_at": utcnow() - timedelta(seconds=1)}}
    )
    return key


def test_two_replicas_marking_the_same_node_ask_for_one_probe(settings):
    """같은 노드의 87 을 두 복제본이 함께 보아도 프로브 문서는 하나이고 둘 다 확인을 맡긴다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        assert await asyncio.gather(_confirm(a, node, "one"), _confirm(b, node, "two")) == [
            True,
            True,
        ]
        docs = await a.probes.find({"cluster_id": CLUSTER, "node": node}).to_list()
        assert len(docs) == 1
        doc = docs[0]
        assert doc["state"] == "wanted" and doc["first_check"]["message"] in ("one", "two")
        # 늦게 쓴 쪽의 때가 더 일러도 `last_at` 은 뒤로 가지 않는다
        assert doc["first_check"]["at"] <= doc["first_check"]["last_at"]

    _race(settings, body)


def test_a_mark_written_late_does_not_move_the_last_failure_back(settings):
    """더 늦은 87 을 다른 복제본이 먼저 적었으면, 뒤에 쓰는 이른 87 이 `last_at` 을 앞으로 돌리지
    않는다. 돌리면 두 87 사이에 시작한 프로브가 늦은 87 의 답으로 읽힌다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        assert await _confirm(a, node)
        ahead = (await _doc(a, node))["first_check"]["at"] + timedelta(seconds=3)
        await a.probes.update_one(
            {"_id": probe_id(CLUSTER, node)}, {"$set": {"first_check.last_at": ahead}}
        )
        assert await _confirm(b, node, "earlier")
        first = (await _doc(a, node))["first_check"]
        assert (first["last_at"], first["message"]) == (ahead, "earlier")

    _race(settings, body)


def test_marking_after_an_old_probe_asks_for_one_new_probe(settings):
    """10분 창 밖에서 끝난 프로브의 문서 위에서 겨루어도 새 프로브 하나를 바라고 표시가 남는다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        key = await _running(a, node)
        assert await a.probe_end(key, "passed") is not None
        old = utcnow() - timedelta(minutes=30)
        await a.probes.update_one({"_id": key}, {"$set": {"started_at": old}})
        assert await asyncio.gather(_confirm(a, node), _confirm(b, node)) == [True, True]
        doc = await _doc(a, node)
        assert doc["state"] == "wanted" and doc["first_check"]["message"] == "87"

    _race(settings, body)


def test_wanting_a_probe_never_drops_a_mark_made_at_the_same_time(settings):
    """88 뒤의 프로브 요청과 87 의 표시가 겹쳐도 맡긴 표시는 문서에 남는다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        _, marked = await asyncio.gather(
            a.want_probe(CLUSTER, node, NS, "acme", quiet_since=utcnow()), _confirm(b, node)
        )
        assert marked
        doc = await _doc(a, node)
        assert doc["state"] == "wanted" and doc["first_check"]["message"] == "87"

    _race(settings, body)


def test_a_mark_survives_the_probe_starting_at_the_same_time(settings):
    """바란 프로브가 뜨는 순간에 붙는 표시도 남는다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        key = probe_id(CLUSTER, node)
        assert await a.want_probe(CLUSTER, node, NS, "acme", quiet_since=utcnow())
        _, marked = await asyncio.gather(a.probe_started(key, "p"), _confirm(b, node))
        assert marked
        doc = await _doc(a, node)
        assert doc["state"] == "running" and doc["first_check"]["message"] == "87"

    _race(settings, body)


def test_a_mark_racing_the_end_of_an_earlier_probe_is_answered_or_refused(settings):
    """87 보다 먼저 시작한 프로브가 끝나는 순간에 표시가 붙는다. 프로브가 통과했어도 그 87 의
    답으로 치지 않는다. 표시를 맡았으면 문서가 새 프로브를 바라는 채로 표시를 지니고, 맡지
    못했으면 부른 쪽이 판정을 무효로 하도록 거짓이 돌아온다."""
    seen = {"kept": 0, "refused": 0}

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        key = await _running(a, node)
        end, marked = await asyncio.gather(a.probe_end(key, "passed"), _confirm(b, node))
        assert end is not None
        doc = await _doc(a, node)
        if marked:
            # 끝낼 때 표시를 보았든 못 보았든 표시는 남고 새 프로브가 확인한다
            assert doc["state"] == "wanted" and doc["first_check"]["message"] == "87"
            assert end.first_check is None or end.again
            seen["kept"] += 1
        else:
            # 방금 끝난 프로브가 10분 창 안에 있어 새로 띄울 수 없다
            assert doc["state"] == "done" and end.first_check is None
            seen["refused"] += 1

    _race(settings, body)
    assert seen["kept"] + seen["refused"] == ROUNDS


def test_a_failed_probe_racing_a_mark_voids_either_way(settings):
    """87 로 끝난 프로브는 표시가 언제 붙었든 무효로 가는 길이 하나는 열려 있다. 끝낼 때 표시를
    보았거나, 표시가 거절돼 부른 쪽이 무효로 하거나, 새 프로브가 그 표시를 확인한다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        key = await _running(a, node)
        end, marked = await asyncio.gather(a.probe_end(key, "failed"), _confirm(b, node))
        assert end is not None and not end.again
        doc = await _doc(a, node)
        if end.first_check is not None:
            assert marked and doc["state"] == "done" and doc["first_check"] is None
        elif marked:
            assert doc["state"] == "wanted" and doc["first_check"]["message"] == "87"
        else:
            assert doc["state"] == "done"

    _race(settings, body)


def test_only_one_replica_ends_a_probe(settings):
    """두 복제본의 스윕이 같은 프로브의 끝을 보아도 하나만 결말을 받는다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        key = await _running(a, node)
        assert await _confirm(a, node)
        await a.probes.update_one(
            {"_id": key}, {"$set": {"started_at": utcnow() + timedelta(seconds=5)}}
        )
        ends = await asyncio.gather(a.probe_end(key, "failed"), b.probe_end(key, "failed"))
        got = [e for e in ends if e is not None]
        assert len(got) == 1 and got[0].first_check is not None

    _race(settings, body)


def test_a_late_first_check_is_ended_once(settings):
    """시한을 넘긴 확인은 두 복제본 가운데 하나만 끝낸다. 프로브의 끝과 겹쳐도 그 87 의 결말을
    받는 쪽은 하나다."""

    async def body(stores: Stores, _db, node: str) -> None:
        a, b = stores
        key = await _running(a, node)
        assert await _confirm(a, node)
        late = utcnow() + timedelta(hours=1)
        mine = {"cluster_id": CLUSTER, "node": node}

        async def ended(store: MongoSandboxStore) -> int:
            # 다른 라운드의 문서는 이미 끝났으므로 이 노드의 것만 돌아온다
            docs = await store.end_late_first_checks(CLUSTER, late)
            assert all(d["node"] == node for d in docs)
            return len(docs)

        first, second, end = await asyncio.gather(ended(a), ended(b), a.probe_end(key, "passed"))
        # 프로브는 87 보다 먼저 시작했으므로 끝이 이겼으면 다시 바라는 것으로 돌렸고, 그 문서는
        # 아직 시한을 넘긴 채라 스윕이 끝낸다
        doc = await a.probes.find_one(mine)
        assert doc is not None
        if doc["state"] == "wanted":
            assert end is not None and end.again and first + second == 0
            assert len(await a.end_late_first_checks(CLUSTER, late)) == 1
        else:
            assert first + second == 1
            assert doc["result"] == "unconfirmed" and doc["first_check"] is None
            assert doc["orphan_pod"] == f"probe-{node}"

    _race(settings, body)


def test_two_replicas_void_a_verdict_once(settings):
    """두 복제본이 같은 판정을 무효로 해도 쓰는 것은 하나이고 처음 까닭이 남는다."""

    async def go() -> None:
        clients = [AsyncMongoClient(settings.mongo_url) for _ in range(2)]
        try:
            a, b = (MongoClusterStore(c[settings.mongo_db]) for c in clients)
            for i in range(ROUNDS):
                cid = f"{CLUSTER}-{i}"
                await a.coll.insert_one(
                    {
                        "_id": cid,
                        "name": cid,
                        "sandbox": {"check": {"passed": True, "voided_at": None}},
                    }
                )
                wrote = await asyncio.gather(
                    a.void_sandbox_check(cid, "self_check"), b.void_sandbox_check(cid, "probe")
                )
                assert sorted(wrote) == [False, True]
                check = (await a.coll.find_one({"_id": cid}))["sandbox"]["check"]
                assert check["passed"] is False
                assert check["void_reason"] == ("self_check" if wrote[0] else "probe")
        finally:
            for c in clients:
                await c.close()

    asyncio.run(go())
