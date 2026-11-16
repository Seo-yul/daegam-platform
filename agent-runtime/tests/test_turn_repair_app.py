"""끊긴 턴의 되살리기를 앱으로 본다(P79 3절). 실제 Postgres·Redis 를 붙인 앱에서 턴을 돌리다 리스를
끝내고 펜스를 세워 파드가 죽은 것과 같은 상태를 만든다(턴은 세션에 더 쓰지 않는다). 다음 읽기가
되살리고, 줄과 turn_end 가 한 번 남고, 다음 말이 받아진다. 잡기가 끊긴 표시에 막히면 되살린 뒤
409 이고, 앱의 sweep 은 아무도 읽지 않는 세션도 되살린다. P79 앞에 막힌 세션은 체크포인트를 읽는
라우트가 맞춘다. 저장소에 끊긴 턴을 직접 만들어 3절 표의 줄마다 보는 것은 test_turn_repair.py 에
있다."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from runtime import turn
from runtime.cancel import LEASE_LOST
from runtime.counters import MemoryCounters
from runtime.sessions import (
    MemorySessionIndex,
    SessionStore,
    new_turn_token,
    sweep_loop,
    utcnow,
)
from runtime.state import Settings
from runtime.turn_record import RUN_CHAT, new_turn_id
from tests.app_fixtures import (
    ToolThenSleepModel,
    decide,
    last_event,
    open_chat,
    read_in_background,
    resume,
    sid_of,
    sleeping_turn,
    status_and_code,
    tool_message,
)
from tests.conftest import RunningApp, chat, parse_sse, run_app, wait_for
from tests.repair_fixtures import lapse, marker_for
from tests.store_fixtures import store_for

STORES = pytest.mark.parametrize("kind", ["postgres", "redis"])
ADD = tool_message("Add", a=1, b=2)


async def _kill(running: RunningApp, sid: str) -> None:
    """턴을 돌던 파드가 죽은 것과 같게 한다. 리스를 끝내고(1분이 지났다) 관리자가 리스를 잃게 해
    턴을 멈춘다. 멈춘 턴은 세션에 더 쓰지 않으므로 열린 표시와 실행 표식이 남는다."""
    state = running.runtime
    await lapse(state.store, sid)
    state.leases.lose(state.leases._leases[sid])


async def _progress(running: RunningApp, sid: str) -> dict[str, Any]:
    response = await running.client.get(f"/sessions/{sid}", params={"view": "progress"})
    assert response.status_code == 200, response.text
    return response.json()


def _repaired_ends(running: RunningApp) -> list[dict[str, Any]]:
    return [event for event in running.observed("turn_end") if event.get("repaired")]


async def _saved(running: RunningApp, sid: str, count: int) -> bool:
    return len(await running.runtime.store.transcripts.read(sid)) >= count


@STORES
async def test_a_dead_chat_is_repaired_by_the_next_read_and_the_session_takes_a_new_chat(
    kind: str, support_doc
) -> None:
    """도구 결과를 받고 다음 모델 호출 도중에 죽은 새 말이다. 그동안 목록은 running 거짓만 알린다.
    다음 읽기가 되살려 running 거짓, 전사의 끝은 결과 카드와 그 실행의 turn_aborted 줄이고,
    turn_end{repaired} 가 한 번 나간다. 다음 말이 받아진다."""
    model = ToolThenSleepModel(model_name="stub", pause=30)
    async with (
        store_for(kind, support_doc) as store,
        run_app(support_doc, store=store, model=model) as running,
    ):
        sid, response = await open_chat(running, ADD)
        reader = read_in_background(response)
        await model.sleeping.wait()
        await wait_for(lambda: _saved(running, sid, 2), what="사용자 항목과 결과 카드의 저장")
        await _kill(running, sid)
        assert last_event(await reader) == ("error", LEASE_LOST.code)
        assert running.observed("turn_end") == []
        (listed,) = (await running.client.get("/sessions", params={"user_sub": "alice"})).json()[
            "sessions"
        ]
        assert listed["running"] is False and "orphan" not in listed
        progress = await _progress(running, sid)
        assert progress["session"]["running"] is False
        assert progress["session"]["status"] == "active"
        items = progress["transcript"]["items"]
        assert [item["kind"] for item in items] == ["user", "tool_call", "error"]
        assert items[1]["result"] is not None
        (start,) = running.observed("turn_start")
        assert items[-1] == {
            "kind": "error",
            "code": LEASE_LOST.code,
            "message": LEASE_LOST.detail,
            "turn_id": start["turn_id"],
            "trace_id": start["trace_id"],
        }
        (end,) = _repaired_ends(running)
        assert (end["status"], end["turn_id"], end["trace_id"], end["span_id"]) == (
            LEASE_LOST.code,
            start["turn_id"],
            start["trace_id"],
            start["span_id"],
        )
        assert "duration_ms" not in end and "latency_ms" not in end
        # 다시 읽어도 되살리기는 한 번이다
        await _progress(running, sid)
        assert len(_repaired_ends(running)) == 1
        model.pause = 0
        response, sse = await chat(running, "다음", session_id=sid)
        assert response.status_code == 200 and sse[-1][0] == "done"


@STORES
async def test_a_refused_settlement_leaves_the_marker_and_the_repair_emits_one_turn_end(
    kind: str, support_doc
) -> None:
    """도는 동안 리스가 끝나면 턴 자신의 쓰기가 서버의 토큰 검사에 막힌다. 펜스가 서고 turn_end 를
    내지 않으며 실행 표식이 남는다. 다음 읽기의 되살리기가 turn_end 를 꼭 한 번 낸다."""
    support_doc["main"]["tools"] = []
    async with (
        store_for(kind, support_doc) as store,
        run_app(support_doc, store=store, over_http=True) as running,
    ):
        task = await sleeping_turn(running, 1)
        (sid,) = running.runtime.cancels
        lease = running.runtime.leases._leases[sid]
        await lapse(running.runtime.store, sid)
        response = await task
        last = parse_sse(response.text)[-1]
        assert last[0] == "error" and last[1]["code"] == LEASE_LOST.code
        assert lease.fence.lost and running.observed("turn_end") == []
        record = await running.runtime.store.index.get(sid)
        assert record is not None and record.orphan == lease.token
        await _progress(running, sid)
        assert len(_repaired_ends(running)) == 1
        await _progress(running, sid)
        assert len(_repaired_ends(running)) == 1


@STORES
async def test_a_claim_refused_by_an_open_mark_repairs_answers_409_and_the_next_chat_works(
    kind: str, support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """입구의 읽기가 되살리지 못했으면(자리가 다 찼다) 잡기가 끊긴 열린 표시에 막힌다. 그 요청은
    세션을 다시 읽어 되살리고 409 turn_in_progress 로 답하며 두 번째로 잡지 않는다. 다시 보내면
    라우트의 검사를 모두 지나 받아진다."""
    support_doc["main"]["tools"] = []
    async with store_for(kind, support_doc) as store, run_app(support_doc, store=store) as running:
        state = running.runtime
        _, sse = await chat(running, "첫 말")
        sid = sid_of(sse, "done")
        token = new_turn_token()
        index = state.store.index
        assert await index.claim_turn(sid, lease_seconds=30, token=token)
        turn_id = new_turn_id()
        marker = marker_for(token, turn_id, RUN_CHAT)
        await index.update_held(sid, token, run=marker, turn_id=turn_id, last_turn=2)
        await lapse(state.store, sid)
        on_read = state.repairs.on_read
        skipped: list[str] = []

        async def busy_once(state_: Any, record: Any) -> Any:
            if not skipped:
                skipped.append(record.session_id)
                return record
            return await on_read(state_, record)

        monkeypatch.setattr(state.repairs, "on_read", busy_once)
        claims: list[str] = []
        claim = index.claim_turn

        async def counted(session_id: str, *, lease_seconds: float, token: str) -> bool:
            claims.append(session_id)
            return await claim(session_id, lease_seconds=lease_seconds, token=token)

        monkeypatch.setattr(index, "claim_turn", counted)
        response, _ = await chat(running, "막힌 말", session_id=sid)
        assert status_and_code(response) == (409, "turn_in_progress")
        assert claims == [sid], "되살린 뒤 두 번째로 잡지 않는다"
        record = await index.get(sid)
        assert record is not None and record.orphan is None
        items = await state.store.transcripts.read(sid)
        assert items[-1]["code"] == LEASE_LOST.code and items[-1]["turn_id"] == turn_id
        assert len(_repaired_ends(running)) == 1
        response, sse = await chat(running, "다시 보낸 말", session_id=sid)
        assert response.status_code == 200 and sse[-1][0] == "done"


@STORES
async def test_a_resume_dead_after_the_interrupt_was_consumed_lets_the_next_chat_through(
    kind: str, support_doc
) -> None:
    """승인 뒤 재개가 도구를 마치고 다음 모델 호출 도중에 죽었다. 색인은 pending 이고 체크포인트에는
    인터럽트가 없다. 다음 말의 입구 읽기가 되살려 active 로 맞추므로 409 session_pending 이 아니다.
    전사 끝은 재개의 결과 카드와 그 실행의 줄이다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    model = ToolThenSleepModel(model_name="stub", pause=30)
    async with (
        store_for(kind, support_doc) as store,
        run_app(support_doc, store=store, model=model) as running,
    ):
        state = running.runtime
        _, sse = await chat(running, ADD)
        sid = sid_of(sse, "interrupt")
        resumed = asyncio.create_task(decide(running, sid, [{"type": "approve"}]))
        await model.sleeping.wait()
        # 앞 턴의 사용자 항목, 결과 없는 카드, 승인 카드 뒤에 재개의 결과 카드가 붙는다
        await wait_for(lambda: _saved(running, sid, 4), what="재개의 결과 카드 저장")
        await _kill(running, sid)
        answer = await resumed
        assert answer.status_code == 200 and "turn_aborted" in answer.text
        assert (await state.store.index.get(sid)).status == "pending"
        # 승인이 걸린 도구를 스텁이 다시 부르므로 다음 말은 승인 대기로 끝난다. 받아진 것이 요점이다
        response, sse = await chat(running, "다음", session_id=sid)
        assert response.status_code == 200 and sse[-1][0] == "interrupt"
        items = await state.store.transcripts.read(sid)
        kinds = [item["kind"] for item in items]
        assert kinds[:5] == ["user", "tool_call", "interrupt", "tool_call", "error"]
        assert items[4]["code"] == LEASE_LOST.code
        (end,) = _repaired_ends(running)
        assert end["turn_id"] == items[4]["turn_id"]


@STORES
async def test_the_apps_sweep_repairs_a_session_nobody_reads(kind: str, support_doc) -> None:
    """아무도 다시 열지 않는 세션도 파드의 sweep 이 되살려 줄과 turn_end 를 얻는다."""
    support_doc["main"]["tools"] = []
    settings = Settings(sweep_interval=0.05)
    async with (
        store_for(kind, support_doc) as store,
        run_app(support_doc, store=store, settings=settings) as running,
    ):
        index = running.runtime.store.index
        _, sse = await chat(running, "첫 말")
        sid = sid_of(sse, "done")
        token = new_turn_token()
        assert await index.claim_turn(sid, lease_seconds=30, token=token)
        turn_id = new_turn_id()
        await index.update_held(
            sid, token, run=marker_for(token, turn_id, RUN_CHAT), turn_id=turn_id, last_turn=2
        )
        await lapse(running.runtime.store, sid)

        async def repaired() -> bool:
            return len(_repaired_ends(running)) == 1

        await wait_for(repaired, what="sweep 의 되살리기")
        record = await index.get(sid)
        assert record is not None and record.orphan is None
        items = await running.runtime.store.transcripts.read(sid)
        assert items[-1]["code"] == LEASE_LOST.code


async def test_the_sweep_loop_repairs_before_it_closes_and_a_failed_repair_still_sweeps() -> None:
    """sweep 은 TTL 로 닫기 전에 되살린다. 되살리기가 실패해도 그 주기의 sweep 은 돈다."""
    store = SessionStore(
        checkpointer=None,  # type: ignore[arg-type]
        index=MemorySessionIndex(),
        counters=MemoryCounters(),
        shared=False,
        warnings=[],
    )
    order: list[str] = []

    async def sweep(ttl_seconds: int, *, retention_days: int) -> list[str]:
        order.append("sweep")
        return []

    async def repair() -> None:
        order.append("repair")
        if len(order) > 2:
            raise OSError("store blip")

    store.sweep = sweep  # type: ignore[method-assign]
    task = asyncio.create_task(sweep_loop(store, 60, 0.01, lambda sid: None, repair=repair))

    async def swept_twice() -> bool:
        return order.count("sweep") >= 2

    try:
        await wait_for(swept_twice, what="sweep 두 번")
    finally:
        task.cancel()
    assert order[:4] == ["repair", "sweep", "repair", "sweep"]


# --- P79 앞에 막힌 세션 ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_a_session_blocked_before_p79_is_aligned_by_the_reads_that_see_its_checkpoint(
    kind: str, support_doc
) -> None:
    """열린 표시 없이 색인 상태가 체크포인트와 어긋난 세션(P79 앞의 런타임이 남긴 것)이다.
    체크포인트를 읽는 전체 읽기, 보류 읽기, 결정이 리스도 표시도 없을 때 상태를 맞춘다. 가벼운
    읽기는 체크포인트를 읽지 않으므로 그대로이고, 도는 턴이 있으면 쓰지 않는다. 인터럽트가 없는
    결정은 맞춘 뒤에도 409 not_pending 이고 다음 말이 받아진다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with store_for(kind, support_doc) as store, run_app(support_doc, store=store) as running:
        index = running.runtime.store.index

        async def status_of(sid: str) -> str:
            record = await index.get(sid)
            assert record is not None
            return record.status

        async def set_status(sid: str, status: str) -> None:
            record = await index.get(sid)
            assert record is not None
            pending_since = utcnow() if status == "pending" else None
            await index.update(
                sid, status=status, pending_since=pending_since, last_turn_at=record.last_turn_at
            )

        # interrupt 를 내다 끊긴 턴: 색인 active, 체크포인트 보류
        _, sse = await chat(running, ADD)
        sid = sid_of(sse, "interrupt")
        await set_status(sid, "active")
        assert (await _progress(running, sid))["session"]["status"] == "active"
        token = new_turn_token()
        assert await index.claim_turn(sid, lease_seconds=30, token=token)
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["session"]["status"] == "active", "도는 턴이 있으면 쓰지 않는다"
        await index.release_turn(sid, token)
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["session"]["status"] == "pending" and detail["pending"] is True
        assert await status_of(sid) == "pending"
        _, approved = await resume(running, sid, [{"type": "approve"}])
        assert approved[-1][0] == "done"

        # 재개가 도구 도중에 끊긴 턴: 색인 pending, 체크포인트에 인터럽트 없음
        await set_status(sid, "pending")
        response = await running.client.get(
            f"/sessions/{sid}/pending", params={"user_sub": "alice"}
        )
        assert status_and_code(response) == (404, "not_pending")
        assert await status_of(sid) == "active"
        await set_status(sid, "pending")
        assert status_and_code(await decide(running, sid, [{"type": "approve"}])) == (
            409,
            "not_pending",
        )
        assert await status_of(sid) == "active"
        # 다음 말이 받아진다. 승인이 걸린 도구를 스텁이 다시 불러 승인 대기로 끝난다
        response, sse = await chat(running, "다음", session_id=sid)
        assert response.status_code == 200 and sse[-1][0] == "interrupt"

        # 결정이 인터럽트 위의 active 를 맞추고 그대로 재개한다
        await set_status(sid, "active")
        _, approved = await resume(running, sid, [{"type": "approve"}])
        assert approved[-1][0] == "done"

        # 닫힌 세션의 전체 읽기는 아무것도 쓰지 않는다
        _, sse = await chat(running, ADD)
        closed = sid_of(sse, "interrupt")
        await set_status(closed, "closed")
        before = await index.get(closed)
        detail = (await running.client.get(f"/sessions/{closed}")).json()
        assert detail["session"]["status"] == "closed"
        assert await index.get(closed) == before


@STORES
async def test_a_settlement_whose_index_write_failed_keeps_the_lease_for_the_repair(
    kind: str, support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """마감의 색인 쓰기가 저장소 오류로 실패한 턴은 리스를 풀지 않는다. 풀면 열린 표시가 사라져 그
    실행은 줄도 turn_end 도 없이 남는다. 남긴 리스가 끝나면 되살리기가 줄과 turn_end 로 마감한다."""
    support_doc["main"]["tools"] = []
    async with (
        store_for(kind, support_doc) as store,
        run_app(support_doc, store=store, over_http=True) as running,
    ):
        index = running.runtime.store.index
        update_held = index.update_held
        releases: list[str] = []
        release = index.release_turn

        async def settle_fails(session_id: str, token: str, **kwargs: Any) -> Any:
            if kwargs.get("settle"):
                raise OSError("store blip")
            return await update_held(session_id, token, **kwargs)

        async def counted(session_id: str, token: str) -> None:
            releases.append(session_id)
            await release(session_id, token)

        monkeypatch.setattr(index, "update_held", settle_fails)
        monkeypatch.setattr(index, "release_turn", counted)
        task = await sleeping_turn(running, 1)
        (sid,) = running.runtime.cancels
        await task
        await wait_for(lambda: _no_turn_on_the_pod(running), what="턴 태스크의 끝")
        assert releases == [] and running.observed("turn_end") == []
        record = await index.get(sid)
        assert record is not None and record.running is True, "리스를 풀지 않았다"
        await lapse(running.runtime.store, sid)
        progress = await _progress(running, sid)
        assert progress["session"]["running"] is False
        assert progress["transcript"]["items"][-1]["code"] == LEASE_LOST.code
        assert len(_repaired_ends(running)) == 1


async def _no_turn_on_the_pod(running: RunningApp) -> bool:
    return not running.runtime.turns


@STORES
async def test_a_fence_after_the_attempt_record_leaves_a_lone_repaired_turn_end(
    kind: str, support_doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """시도 기록 뒤, 턴 태스크 전에 리스를 잃으면 503 turn_aborted 이고 turn_start 는 없다. 실행
    표식은 남아 되살리기가 그 실행의 turn_end 하나를 낸다. 플랫폼은 turn_start 없는 turn_end 도 실행
    하나로 받는다(platform/daegam/services/telemetry/traces.py `_turn`). 그래서 이 표식은 지우지
    않는다."""
    support_doc["main"]["tools"] = []
    async with store_for(kind, support_doc) as store, run_app(support_doc, store=store) as running:
        state = running.runtime
        _, sse = await chat(running, "첫 말")
        sid = sid_of(sse, "done")
        note_attempt = turn.note_attempt

        async def lost_right_after(*args: Any, **kwargs: Any) -> None:
            await note_attempt(*args, **kwargs)
            state.leases.lose(state.leases._leases[sid])

        monkeypatch.setattr(turn, "note_attempt", lost_right_after)
        response, _ = await chat(running, "막힐 말", session_id=sid)
        assert status_and_code(response) == (503, LEASE_LOST.code)
        starts = running.observed("turn_start")
        assert len(starts) == 1, "막힌 시도는 turn_start 를 내지 않는다"
        record = await state.store.index.get(sid)
        assert record is not None and record.running is True
        attempt_id = record.turn_id
        await lapse(state.store, sid)
        await _progress(running, sid)
        (end,) = _repaired_ends(running)
        assert end["turn_id"] == attempt_id != starts[0]["turn_id"]
        traces = {start["trace_id"] for start in running.observed("turn_start")}
        assert end["trace_id"] not in traces
