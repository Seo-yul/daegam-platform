"""잠든 동안 세션 TTL 이 지난 세션이다(P44 11절). 세션을 읽는 길(`load_session`)은 TTL 이 지났고
살아 있는 리스가 잡고 있지 않은 열린 세션을 쓰지 않고 닫힌 것으로 읽는다. 그래서 새 턴, 다시 생성,
결정, 보류 읽기, 파일 읽기가 깨어난 파드의 첫 sweep 전에도 그 세션에 닿지 않는다. 리스를 잡은 뒤의
다시 읽기(`check_still_open`)도 TTL 을 다시 본다. 리스가 살아 있는 세션은 sweep 처럼 닫지 않는다.
sweep 은 기동하자마자 첫 주기를 돌아, 깨어난 파드가 그 세션을 체크포인트·첨부와 함께 닫는다."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from runtime import stream
from runtime.counters import MemoryCounters
from runtime.sessions import (
    STATUS_CLOSED,
    STATUS_PENDING,
    MemorySessionIndex,
    SessionRecord,
    SessionStore,
    sweep_loop,
    utcnow,
)
from runtime.turn_gate import check_still_open
from tests.app_fixtures import decide, regenerate, sid_of, status_and_code
from tests.attachment_fixtures import document
from tests.conftest import RunningApp, chat, wait_for
from tests.safety_fixtures import ADD, RULE, safety_app, send, turns_doc
from tests.store_fixtures import real_store

APPROVE = [{"type": "approve"}]


async def _expired_pending_session(running: RunningApp) -> str:
    """승인을 기다리는 세션을 하나 만들고 마지막 턴을 세션 TTL 보다 1분 앞으로 옮긴다."""
    sid = sid_of(await send(running, ADD), "interrupt")
    ttl = running.config.entry["session_ttl_seconds"]
    old = utcnow() - timedelta(seconds=ttl + 60)
    assert await running.runtime.store.index.update(sid, last_turn_at=old) is not None
    return sid


async def test_an_expired_pending_session_reads_as_closed_without_a_write(fake_mcp) -> None:
    """상세는 200 이고 닫힌 상태에 승인 카드가 없다. 보류 읽기는 404 not_pending, 결정과 새 말과
    다시 생성과 파일 목록은 410 session_closed 이고, 색인은 그대로 pending 이며 Tool 은 돌지
    않는다."""
    async with safety_app(turns_doc(fake_mcp.url, approval=RULE)) as running:
        sid = await _expired_pending_session(running)
        detail = await running.client.get(f"/sessions/{sid}")
        assert detail.status_code == 200
        assert detail.json()["session"]["status"] == STATUS_CLOSED
        assert detail.json()["pending"] is False
        progress = await running.client.get(f"/sessions/{sid}", params={"view": "progress"})
        assert progress.json()["session"]["status"] == STATUS_CLOSED
        assert progress.json()["pending"] is False
        pending = await running.client.get(f"/sessions/{sid}/pending")
        assert status_and_code(pending) == (404, "not_pending")
        refusals = [
            await decide(running, sid, APPROVE),
            (await chat(running, ADD, session_id=sid))[0],
            (await regenerate(running, sid))[0],
            await running.client.get(f"/sessions/{sid}/files", params={"user_sub": "alice"}),
        ]
        for response in refusals:
            assert status_and_code(response) == (410, "session_closed"), response.text
        record = await running.runtime.store.index.get(sid)
        assert record is not None and record.status == STATUS_PENDING
        assert fake_mcp.calls["Add"] == 0


async def test_an_expired_session_held_by_a_live_lease_stays_open(fake_mcp) -> None:
    """리스가 살아 있는 세션은 sweep 이 닫지 않으므로 읽기도 닫힌 것으로 읽지 않는다. 보류와 파일은
    읽히고, 결정은 그 리스에 409 turn_in_progress 로 물린다."""
    async with safety_app(turns_doc(fake_mcp.url, approval=RULE)) as running:
        index = running.runtime.store.index
        sid = await _expired_pending_session(running)
        assert await index.claim_turn(sid, lease_seconds=30, token="held")
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["session"]["status"] == STATUS_PENDING
        assert detail["session"]["running"] is True
        pending = await running.client.get(f"/sessions/{sid}/pending")
        assert pending.status_code == 200 and pending.json()["action_requests"]
        files = await running.client.get(f"/sessions/{sid}/files", params={"user_sub": "alice"})
        assert files.status_code == 200
        assert status_and_code(await decide(running, sid, APPROVE)) == (409, "turn_in_progress")
        await index.release_turn(sid, "held")
        assert status_and_code(await decide(running, sid, APPROVE)) == (410, "session_closed")
        assert fake_mcp.calls["Add"] == 0


@pytest.mark.parametrize("resume", [False, True])
def test_the_reread_under_the_lease_refuses_a_session_past_its_ttl(resume: bool) -> None:
    """리스를 잡은 뒤 다시 읽은 한 줄은 이 턴의 리스가 잡고 있어 닫힌 것으로 읽히지 않으므로,
    `check_still_open` 이 TTL 을 다시 본다. TTL 안이면 받는다."""
    state = SimpleNamespace(config=SimpleNamespace(entry={"session_ttl_seconds": 3600}))
    status = STATUS_PENDING if resume else "active"
    now = utcnow()
    old = now - timedelta(seconds=3601)
    with pytest.raises(HTTPException) as e:
        check_still_open(
            state, SessionRecord("s1", None, "alice", old, old, status=status), resume=resume
        )
    assert (e.value.status_code, e.value.detail["code"]) == (410, "session_closed")
    fresh = now - timedelta(seconds=3500)
    check_still_open(
        state, SessionRecord("s1", None, "alice", fresh, fresh, status=status), resume=resume
    )


async def test_the_sweep_loop_runs_its_first_round_at_once() -> None:
    """첫 주기는 기동하자마자 돈다. 주기가 한 시간이어도 깨어난 파드가 기다리지 않는다."""
    store = SessionStore(
        checkpointer=None,  # type: ignore[arg-type]
        index=MemorySessionIndex(),
        counters=MemoryCounters(),
        shared=False,
        warnings=[],
    )
    rounds: list[int] = []

    async def sweep(ttl_seconds: int, *, retention_days: int) -> list[str]:
        rounds.append(ttl_seconds)
        return ["expired"]

    store.sweep = sweep  # type: ignore[method-assign]
    closed: list[str] = []
    task = asyncio.create_task(sweep_loop(store, 60, 3600, closed.append))

    async def swept() -> bool:
        return closed == ["expired"]

    try:
        await wait_for(swept, what="첫 주기")
    finally:
        task.cancel()
    assert rounds == [60]


@pytest.mark.parametrize("kind", ["postgres", "redis"])
async def test_a_woken_pod_closes_a_session_that_expired_while_it_slept(
    kind: str, fake_mcp
) -> None:
    """앞 파드가 승인 대기와 첨부를 남기고 내려간 뒤 세션 TTL 이 지났다. 같은 저장소로 뜬 파드의 첫
    sweep 이 그 세션을 닫아 체크포인트와 첨부가 남지 않고 session_expired 가 나며, 전사는 남는다."""
    doc = turns_doc(fake_mcp.url, approval=RULE)
    async with real_store(kind, doc) as store:
        async with safety_app(doc, store=store) as asleep:
            response, sse = await chat(asleep, ADD, attachments=[document()])
            assert response.status_code == 200, response.text
            sid = sid_of(sse, "interrupt")
            snapshot = await asleep.runtime.agent.graph.aget_state(stream.thread_config(sid))
            assert snapshot.interrupts
        assert await store.uploads.read(sid)
        ttl = doc["entry"]["session_ttl_seconds"]
        old = utcnow() - timedelta(seconds=ttl + 60)
        assert await store.index.update(sid, last_turn_at=old) is not None
        async with safety_app(doc, store=store) as woken:

            async def closed() -> bool:
                record = await store.index.get(sid)
                return record is not None and record.status == STATUS_CLOSED

            await wait_for(closed, what="깨어난 파드의 첫 sweep")
            assert [e["session_id"] for e in woken.observed("session_expired")] == [sid]
            snapshot = await woken.runtime.agent.graph.aget_state(stream.thread_config(sid))
            assert not snapshot.values
            assert await store.uploads.read(sid) == []
            assert await store.transcripts.read(sid)
