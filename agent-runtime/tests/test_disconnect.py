"""떠난 읽기와 끝까지 가는 턴이다(D92, P78 1절). 턴은 요청과 떨어진 태스크라 연결이 어디서 끊기든
읽기만 멈추고 턴은 끝까지 가서 done 으로 마감하고 리스를 푼다. 실제 소켓을 끊어도 같은 세션의 다음
턴은 앞 턴이 끝날 때까지 409 turn_in_progress 이고, 끝난 뒤에는 그 결과가 전사에 있다."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest

from runtime import turn_task
from runtime.turn import DETACHED_HEADER, TURN_ID_HEADER
from runtime.turn_task import KEEPALIVE, TurnEvents, start_turn_task
from tests.app_fixtures import (
    ToolThenSleepModel,
    cut_while_sending,
    lease_free,
    open_chat,
    resume,
    sid_of,
    turns_ended,
)
from tests.conftest import RunningApp, chat, events_named, run_app, wait_for


async def _assert_finished_as_done(running: RunningApp, sid: str) -> None:
    """읽기가 떠난 턴이 끝까지 가서 남긴 것이다. 리스가 풀리고, 턴 수에 들고, 전사가 done 으로
    끝나고, turn_end 는 done 이다."""
    await wait_for(
        lambda: turns_ended(running, "done"), what="읽기가 떠난 턴의 turn_end", tries=200
    )
    store = running.runtime.store
    await wait_for(lambda: lease_free(store.index, sid), what="풀린 리스")
    row = (await store.index.get(sid)).to_dict()
    assert row["turns"] == 1 and row["status"] == "active"
    items = await store.transcripts.read(sid)
    assert [i["kind"] for i in items] == ["user", "tool_call", "assistant", "done"]
    assert items[1]["result"]["content"] == "3"
    assert running.ended("cancelled") == 0


async def test_a_reader_cut_inside_the_stream_leaves_the_turn_running(
    support_doc, fake_mcp
) -> None:
    """진행 중인 모델 호출 도중에 읽기가 떠난다. 턴은 그 호출을 끝까지 마치고 done 으로 마감한다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.3)
    async with run_app(support_doc, model=model) as running:
        sid, response = await open_chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        chunks: list[str] = []

        async def read() -> None:
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        async def cut() -> None:
            await model.sleeping.wait()
            tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            tg.start_soon(cut)
        assert [c.split("\n", 1)[0] for c in chunks] == ["event: tool_call", "event: tool_result"]
        await _assert_finished_as_done(running, sid)
        assert not model.cancelled and fake_mcp.calls["Add"] == 1


async def test_a_reader_cut_while_sending_leaves_the_turn_running(support_doc) -> None:
    """send 도중에 끊긴다. 읽기는 그 자리에서 닫히고 턴은 끝까지 간다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.3)
    async with run_app(support_doc, model=model) as running:
        sid, response = await open_chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        await cut_while_sending(response, b"event: tool_result")
        assert response.body_iterator.closed, "읽기가 아직 닫히지 않았다"
        await _assert_finished_as_done(running, sid)


async def test_a_connection_closed_before_the_first_byte_still_runs_the_turn(support_doc) -> None:
    """응답을 하나도 읽기 전에 연결이 닫혀도 턴은 태스크로 돌고 리스를 푼다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.1)
    async with run_app(support_doc, model=model) as running:
        sid, response = await open_chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        await response.body_iterator.aclose()
        await _assert_finished_as_done(running, sid)


async def test_the_response_names_the_attempt_and_the_detached_turn(support_doc) -> None:
    """응답 머리가 이 시도의 id 와 떼어 낸 턴을 알린다. 시도의 id 는 done 의 turn_id 와 같다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc) as running:
        response, sse = await chat(running, "안녕")
        assert response.headers[DETACHED_HEADER] == "1"
        assert response.headers[TURN_ID_HEADER] == events_named(sse, "done")[0]["turn_id"]


async def _next_turn_after(running: RunningApp, sid: str, seconds: float) -> tuple[int, Any, float]:
    """끊은 뒤 같은 세션의 다음 턴을 409 turn_in_progress 동안 다시 연다. 받은 409 의 수와 마지막
    답, 걸린 초를 돌려준다."""
    loop = asyncio.get_running_loop()
    started, refused = loop.time(), 0
    while True:
        response, sse = await chat(running, "다시", session_id=sid)
        elapsed = loop.time() - started
        if response.status_code != 409 or elapsed >= seconds:
            return refused, (response, sse), elapsed
        refused += 1
        # 거절된 요청도 사용자별 분당 한도(20)에 드므로 띄엄띄엄 다시 연다
        await asyncio.sleep(0.25)


async def test_a_cut_connection_keeps_the_session_busy_until_the_turn_ends(support_doc) -> None:
    """실제 소켓이다. 진행 중인 모델 호출을 두고 연결을 끊어도 턴은 끝까지 가므로 같은 세션의 다음
    턴은 그동안 409 이고, 끝난 뒤에 돈다. 끊긴 턴은 전사에 done 으로 남고 턴 수에 든다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        _, sse = await chat(running, "안녕")
        sid = sid_of(sse, "done")
        body = {"message": '{"sleep": 1}', "user_sub": "alice", "session_id": sid}
        async with running.client.stream("POST", "/chat", json=body) as response:
            assert response.status_code == 200
        # 본문을 읽지 않고 나왔으므로 연결이 닫힌다
        refused, (response, sse), elapsed = await _next_turn_after(running, sid, 5)
        assert refused > 0, "끊긴 턴이 앞서 끝났다"
        assert response.status_code == 200, response.text
        assert 0.5 < elapsed < 5
        assert events_named(sse, "done")[0]["turn"] == 3
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        kinds = [i["kind"] for i in detail["transcript"]["items"]]
        assert kinds == ["user", "assistant", "done"] * 3
        assert detail["session"]["turns"] == 3


async def test_a_reader_cut_while_sending_the_interrupt_leaves_the_session_pending(
    support_doc, fake_mcp
) -> None:
    """interrupt 를 보내다 읽기가 떠나도 턴은 interrupt 로 마감해 보류다. 결정이 재개한다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        sid, response = await open_chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        await cut_while_sending(response, b"event: interrupt")
        await wait_for(lambda: lease_free(running.runtime.store.index, sid), what="풀린 리스")
        assert running.ended("cancelled") == 0
        row = (await running.runtime.store.index.get(sid)).to_dict()
        assert row["status"] == "pending" and row["pending_since"] is not None
        decided, resumed = await resume(running, sid, [{"type": "approve"}])
        assert decided.status_code == 200, decided.text
        assert resumed[-1][0] == "done" and fake_mcp.calls["Add"] == 1


async def test_the_reader_sends_a_keepalive_while_the_turn_is_quiet(monkeypatch) -> None:
    """큐가 조용하면 읽기가 SSE 주석을 낸다. 프록시가 말 없는 모델 호출 도중에 연결을 닫지 않게
    한다."""
    monkeypatch.setattr(turn_task, "KEEPALIVE_SECONDS", 0.01)
    events = TurnEvents()
    assert await anext(events) == KEEPALIVE
    events.put("event: done\n\n")
    events.put(None)
    assert [chunk async for chunk in events] == ["event: done\n\n"]
    assert events.closed


async def test_a_turn_task_that_fails_is_logged_and_forgotten(caplog) -> None:
    """턴 태스크가 예상하지 못한 예외로 끝나면 로그에 남고 turns 에서 빠진다. 읽기는 끝 표시를
    받는다."""

    async def broken() -> AsyncIterator[str]:
        yield "event: token\n\n"
        raise RuntimeError("boom")

    turns: set[asyncio.Task[Any]] = set()
    with caplog.at_level(logging.ERROR, logger="daegam.agent-runtime.turn_task"):
        events = start_turn_task(turns, broken(), session_id="s1")
        assert [chunk async for chunk in events] == ["event: token\n\n"]
        await asyncio.sleep(0)
    assert not turns
    assert any("turn:s1" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("closed_early", [True, False])
async def test_events_after_the_reader_left_are_dropped(closed_early: bool) -> None:
    """읽기가 떠난 뒤의 이벤트는 버린다. 큐가 자라지 않는다."""
    events = TurnEvents()
    if closed_early:
        events.close()
    events.put("event: token\n\n")
    assert events._queue.qsize() == (0 if closed_early else 1)
