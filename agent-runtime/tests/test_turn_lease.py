"""턴 리스의 관리와 리스를 잃은 턴이다(P79 1·2절). 잡기는 60초이고 관리자가 맡아 늘린다. 리스를
잃은 턴은 turn_aborted 로 멈추고 세션에 더 쓰지 않는다. 턴 태스크 전에 잃으면 503 turn_aborted,
지우는 동안 잃으면 503 session_delete_incomplete 다. 저장소에 거는 펜스 자체는 test_fence.py 가,
관리자의 바퀴는 test_lease_keeper.py 가 본다."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import fence, turn
from runtime.fence import Fence, fenced
from runtime.lease_keeper import LEASE_SECONDS
from runtime.sessions import utcnow
from runtime.stream import TurnState
from tests.app_fixtures import running_sid, sid_of, sleeping_turn
from tests.conftest import chat, parse_sse, run_app
from tests.store_fixtures import assert_turn_writes_carry_the_lease_token, contract_store


async def test_a_turn_claims_for_a_minute_and_the_keeper_holds_it_until_the_turn_ends(
    support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        state = running.runtime
        claims: list[float] = []
        claim = state.store.index.claim_turn

        async def recorded(session_id: str, *, lease_seconds: float, token: str) -> bool:
            claims.append(lease_seconds)
            return await claim(session_id, lease_seconds=lease_seconds, token=token)

        monkeypatch.setattr(state.store.index, "claim_turn", recorded)
        task = await sleeping_turn(running, 1)
        sid = running_sid(running)
        lease = state.leases._leases[sid]
        assert lease.kept and lease.on_lost is not None and not lease.fence.standing
        response = await task
        assert parse_sse(response.text)[-1][0] == "done"
        assert claims == [LEASE_SECONDS]
        assert state.leases._leases == {}
        assert (await state.store.index.get(sid)).running is False


# 공유 저장소(Postgres·Redis)면 리스를 잃은 턴은 리스를 풀지 않고 만료를 기다린다. 파드 안 색인은
# 리스가 만료되지 않고 다른 파드도 없으므로 푼다. 시험 앱의 저장소는 파드 안 색인이라 shared 만 바꿔
# 본다
SHARED = pytest.mark.parametrize("shared", [True, False], ids=["shared", "in-pod"])


@SHARED
async def test_a_lost_lease_stops_the_turn_and_writes_nothing_more_to_the_session(
    support_doc, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    """관리자가 리스를 잃으면 도는 턴은 turn_aborted 로 멈춘다. 색인과 전사를 고치지 않고, 공유
    저장소면 리스도 풀지 않으며, turn_end 를 내지 않는다(되살리기가 낸다)."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        state = running.runtime
        monkeypatch.setattr(state.store, "shared", shared)
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        before = await state.store.index.get(sid)
        saved = await state.store.transcripts.read(sid)
        started = asyncio.get_running_loop().time()
        state.leases.lose(state.leases._leases[sid])
        response = await task
        assert asyncio.get_running_loop().time() - started < 2
        last = parse_sse(response.text)[-1]
        assert last[0] == "error" and last[1]["code"] == "turn_aborted"
        assert running.observed("turn_end") == []
        after = await state.store.index.get(sid)
        assert after.running is shared
        assert (after.turns, after.last_turn_at, after.status) == (
            before.turns,
            before.last_turn_at,
            before.status,
        )
        assert await state.store.transcripts.read(sid) == saved
        assert sid not in state.cancels and state.leases._leases == {}


async def test_a_lease_lost_while_settling_still_ends_the_stream_with_turn_aborted(
    support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """마감이 색인을 고치다 펜스에 막혀도 읽는 사람은 마지막 이벤트 turn_aborted 를 받는다.
    turn_end 는 그 쓰기 뒤라 나가지 않는다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        state = running.runtime
        task = await sleeping_turn(running, 1)
        sid = running_sid(running)
        lease = state.leases._leases[sid]
        update = state.store.index.update

        async def fenced_update(session_id: str, **fields: Any) -> Any:
            if fence.current() is lease.fence:
                state.leases.lose(lease)
                fence.check()
            return await update(session_id, **fields)

        monkeypatch.setattr(state.store.index, "update", fenced_update)
        response = await task
        last = parse_sse(response.text)[-1]
        assert last[0] == "error" and last[1]["code"] == "turn_aborted"
        assert running.observed("turn_end") == []
        assert state.leases._leases == {}


async def test_a_failed_release_still_ends_the_turn_and_the_keeper_lets_the_lease_go(
    support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """마감이 리스를 풀지 못해도(저장소 장애) 관리자는 그 리스를 놓아 1분 안에 만료되게 하고, 읽는
    사람은 마지막 이벤트를 받는다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        state = running.runtime
        task = await sleeping_turn(running, 1)

        async def broken(session_id: str, token: str) -> None:
            raise OSError("store blip")

        monkeypatch.setattr(state.store.index, "release_turn", broken)
        response = await task
        assert parse_sse(response.text)[-1][0] == "done"
        assert state.leases._leases == {}


@SHARED
async def test_a_lease_lost_before_the_turn_task_answers_503(
    support_doc, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    """턴 태스크를 띄우기 전에 리스를 잃으면 503 turn_aborted 이고 턴은 시작하지 않는다. 공유
    저장소면 리스를 풀지 않는다."""
    async with run_app(support_doc) as running:
        state = running.runtime
        monkeypatch.setattr(state.store, "shared", shared)
        hold = state.leases.hold

        def lost_at_once(session_id: str, token: str, *, sent_at: float) -> Any:
            lease = hold(session_id, token, sent_at=sent_at)
            state.leases.lose(lease)
            return lease

        monkeypatch.setattr(state.leases, "hold", lost_at_once)
        response, _ = await chat(running, "안녕")
        assert response.status_code == 503
        assert response.json()["code"] == "turn_aborted"
        assert running.observed("turn_start") == []
        assert state.leases._leases == {}
        (row,) = await state.store.index.list(user_sub="alice")
        assert row.running is shared


@SHARED
async def test_a_delete_that_meets_the_fence_answers_session_delete_incomplete(
    support_doc, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    """지우는 동안 리스를 잃어 펜스에 막히면 503 session_delete_incomplete 다. 공유 저장소면 리스를
    풀지 않고, 리스가 만료된 뒤 다시 지우면 이어서 지운다."""
    async with run_app(support_doc) as running:
        state = running.runtime
        monkeypatch.setattr(state.store, "shared", shared)
        _, sse = await chat(running, "지울 대화")
        sid = sid_of(sse)

        async def fenced_delete(session_id: str) -> None:
            state.leases.lose(state.leases._leases[session_id])
            fence.check()

        monkeypatch.setattr(state.store, "delete_session", fenced_delete)
        response = await running.client.delete(f"/sessions/{sid}", params={"user_sub": "alice"})
        assert response.status_code == 503
        assert response.json()["code"] == "session_delete_incomplete"
        assert (await state.store.index.get(sid)).running is shared
        assert state.leases._leases == {}


async def test_the_daily_charge_runs_outside_the_turns_fence() -> None:
    """리스를 잃은 턴도 쓴 토큰은 오늘 카운터에 더한다. 그 쓰기는 턴의 펜스 밖에서 간다."""
    seen: list[Fence | None] = []

    class Counters:
        async def incr(self, key: str, amount: int, *, window: Any) -> int:
            seen.append(fence.current())
            return amount

    state = SimpleNamespace(
        config=SimpleNamespace(service={"budget": {"daily_tokens": 100}}, instance={"id": "i"}),
        store=SimpleNamespace(counters=Counters()),
    )
    spent = TurnState()
    spent.usage.input_tokens = 3
    stood = Fence(asyncio.get_running_loop().time() - 1)
    with fenced(stood):
        await turn._charge_daily(state, spent, utcnow())  # type: ignore[arg-type]
    assert seen == [None]


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_the_turns_own_writes_carry_the_lease_token(kind: str) -> None:
    """세 저장소가 같은 계약이다(store_fixtures.py). Postgres 는 같은 문장의 조건, Redis 는 스크립트
    하나다. 실제 Postgres·Redis 는 주소가 없으면 건너뛴다."""
    async with contract_store(kind) as store:
        await assert_turn_writes_carry_the_lease_token(store)


async def test_a_refused_settlement_write_ends_the_turn_as_turn_aborted(support_doc) -> None:
    """도는 동안 다른 쪽이 리스를 가져가면 마감의 색인 쓰기가 토큰 검사에 막힌다. 펜스가 서고 턴은
    turn_aborted 로 끝나며 turn_end 를 내지 않는다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        state = running.runtime
        task = await sleeping_turn(running, 1)
        sid = running_sid(running)
        lease = state.leases._leases[sid]
        await state.store.index.release_turn(sid, lease.token)
        response = await task
        last = parse_sse(response.text)[-1]
        assert last[0] == "error" and last[1]["code"] == "turn_aborted"
        assert lease.fence.lost and running.observed("turn_end") == []
        assert state.leases._leases == {}


async def test_a_refused_attempt_record_answers_503_before_the_turn_task(
    support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """잡은 리스를 시도 기록 전에 잃으면(토큰 검사에 막힌 note_attempt) 503 turn_aborted 이고 턴은
    시작하지 않는다."""
    async with run_app(support_doc) as running:
        state = running.runtime
        index = state.store.index
        claim = index.claim_turn

        async def claim_then_lose(session_id: str, *, lease_seconds: float, token: str) -> bool:
            claimed = await claim(session_id, lease_seconds=lease_seconds, token=token)
            await index.release_turn(session_id, token)
            return claimed

        monkeypatch.setattr(index, "claim_turn", claim_then_lose)
        response, _ = await chat(running, "안녕")
        assert response.status_code == 503
        assert response.json()["code"] == "turn_aborted"
        assert running.observed("turn_start") == []
        assert state.leases._leases == {}
