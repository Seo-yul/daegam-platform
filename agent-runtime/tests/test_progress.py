"""돌아온 사람이 보는 진행 중 턴이다(P78 2·3절). 턴은 그래프 조각이 끝날 때마다 앞에서부터 닫힌 전사
항목을 쓰기 태스크로 붙이고, `GET /sessions/{sid}?view=progress` 는 체크포인트를 읽지 않고 색인 한
줄(running, turn_id)과 그 전사를 준다. 재개가 도는 동안은 승인 카드를 다시 열지 않는다. 붙이기에
실패한 항목은 잠시 뒤 다시 붙어 전사에 빈자리가 없고, 흔들리는 저장소에 토큰마다 붙이지 않는다.
붙이는 동안 온 중지는 그 까닭으로 마감된다."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk

from runtime import turn
from runtime.stub import StubChatModel
from runtime.transcript import MemoryTranscripts, TurnTranscript
from runtime.turn import _ClosedItems
from tests.app_fixtures import (
    ToolThenSleepModel,
    last_event,
    lease_free,
    open_chat,
    read_in_background,
    resume,
    sid_of,
    turns_ended,
)
from tests.conftest import RunningApp, chat, run_app, wait_for

ADD = '{"tool": "Add", "args": {"a": 1, "b": 2}}'


async def _progress(running: RunningApp, sid: str) -> dict[str, Any]:
    response = await running.client.get(f"/sessions/{sid}", params={"view": "progress"})
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_light_read_shows_a_running_turn_and_its_saved_items(
    support_doc, fake_mcp
) -> None:
    """모델이 도구 결과를 받고 자는 동안 가벼운 읽기는 진행 중임과 그 시도의 id, 이미 붙은 사용자
    항목과 결과를 받은 호출 카드를 보인다. 메시지 요약은 없다. 턴이 끝나면 running 이 거짓이고
    전사가 done 으로 끝난다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.5)
    async with run_app(support_doc, model=model) as running:
        sid, response = await open_chat(running, ADD)
        reader = read_in_background(response)
        await model.sleeping.wait()
        body = await _progress(running, sid)
        assert "messages" not in body and body["pending"] is False
        assert body["session"]["running"] is True and body["session"]["turn_id"]
        items = body["transcript"]["items"]
        assert [i["kind"] for i in items] == ["user", "tool_call"]
        assert items[1]["result"]["content"] == "3"
        await reader
        body = await _progress(running, sid)
        assert body["session"]["running"] is False and "turn_id" not in body["session"]
        kinds = [i["kind"] for i in body["transcript"]["items"]]
        assert kinds == ["user", "tool_call", "assistant", "done"]


async def test_a_resume_in_flight_reads_no_open_approval_card(support_doc, fake_mcp) -> None:
    """재개가 도는 동안 색인은 아직 pending 이지만 가벼운 읽기는 보류로 보지 않아 승인 카드가 닫혀
    있다. 끝나면 보류가 아니다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    model = ToolThenSleepModel(model_name="stub", pause=0.5)
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, ADD)
        sid = sid_of(sse, "interrupt")
        decided = asyncio.create_task(resume(running, sid, [{"type": "approve"}]))
        await model.sleeping.wait()
        assert (await running.runtime.store.index.get(sid)).status == "pending"
        body = await _progress(running, sid)
        assert body["pending"] is False and body["session"]["running"] is True
        cards = [i for i in body["transcript"]["items"] if i["kind"] == "interrupt"]
        assert cards and all(card["resolved"] for card in cards)
        response, resumed = await decided
        assert response.status_code == 200 and resumed[-1][0] == "done"
        assert (await _progress(running, sid))["pending"] is False


class _FlakyTranscripts(MemoryTranscripts):
    """처음 fail 번의 붙이기를 실패시킨다. delay 초 동안 걸리는 붙이기이고, 들어오면 entered 를
    켠다."""

    def __init__(self, fail: int = 0, delay: float = 0) -> None:
        super().__init__()
        self.fail = fail
        self.delay = delay
        self.calls = 0
        self.entered = asyncio.Event()

    async def append(self, session_id: str, items: Any) -> None:
        self.calls += 1
        self.entered.set()
        await asyncio.sleep(self.delay)
        if self.calls <= self.fail:
            raise ConnectionError("transcript store is down")
        await super().append(session_id, items)


async def test_a_failed_append_is_retried_without_a_hole(
    support_doc, fake_mcp, monkeypatch
) -> None:
    """조각마다의 붙이기가 실패해도 그 항목은 버퍼에 남아 잠시 뒤 다시 붙는다. 전사는 차례대로
    온전하다."""
    monkeypatch.setattr(turn, "SAVE_RETRY_SECONDS", 0.01)
    model = ToolThenSleepModel(model_name="stub", pause=0.3)
    async with run_app(support_doc, model=model) as running:
        store = running.runtime.store
        store.transcripts = _FlakyTranscripts(fail=1)
        sid, response = await open_chat(running, ADD)
        reader = read_in_background(response)
        await model.sleeping.wait()
        await wait_for(lambda: _saved(running, sid, 2), what="실패 뒤 다시 붙은 항목")
        await reader
        items = await store.transcripts.read(sid)
        assert [i["kind"] for i in items] == ["user", "tool_call", "assistant", "done"]


async def _saved(running: RunningApp, sid: str, count: int) -> bool:
    return len(await running.runtime.store.transcripts.read(sid)) == count


class _WordsModel(StubChatModel):
    """답을 낱말마다 한 조각으로 흘리는 스텁이다. 조각 끝이 토큰마다 온다."""

    async def _astream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        for word in self.reply(messages).text.split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(content=word + " "))


async def test_a_failing_store_is_not_retried_at_every_chunk(support_doc, fake_mcp) -> None:
    """저장소가 실패하는 동안 쓰기 태스크는 기다렸다 다시 붙인다. 흐르는 토큰(조각)마다 붙이지 않고
    그래프도 붙이기를 기다리지 않는다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, model=_WordsModel(model_name="stub")) as running:
        store = running.runtime.store
        store.transcripts = _FlakyTranscripts(fail=1_000)
        _, sse = await chat(running, "글 " * 200)
        tokens = [data for event, data in sse if event == "token"]
        assert len(tokens) > 20 and sse[-1][0] == "done"
        # 첫 실패 하나와 마감의 붙이기 하나다. 다시 붙이기까지의 기다림 안에 턴이 끝났다
        assert store.transcripts.calls <= 3


async def test_a_stop_while_an_append_is_in_flight_ends_with_its_reason(
    support_doc, fake_mcp
) -> None:
    """조각의 붙이기가 저장소를 기다리는 동안 중지가 온다. 턴은 그 까닭(turn_cancelled)으로 마감하고
    읽기는 마지막 error 를 받는다. 파드 종료(turn_aborted)로 잘못 마감하지 않는다."""
    model = ToolThenSleepModel(model_name="stub", pause=5)
    async with run_app(support_doc, model=model) as running:
        store = running.runtime.store
        store.transcripts = _FlakyTranscripts(delay=0.3)
        sid, response = await open_chat(running, ADD)
        reader = read_in_background(response)
        await store.transcripts.entered.wait()
        running.runtime.cancels[sid].notify()
        chunks = await reader
        assert last_event(chunks) == ("error", "turn_cancelled")
        await wait_for(lambda: turns_ended(running, "cancelled"), what="중지의 마감")
        assert running.ended("turn_aborted") == 0
        items = await store.transcripts.read(sid)
        assert items[-1]["kind"] == "error" and items[-1]["code"] == "turn_cancelled"


@dataclass
class _Fork:
    """갈래를 내는 다시 생성이다. head 가 옮겨 갔는지만 답한다."""

    moved: bool
    parent_id: str | None = "parent"

    async def head_moved(self, saver: Any) -> bool:
        return self.moved


async def test_a_branching_regenerate_saves_only_after_the_head_moved(support_doc) -> None:
    """갈래를 내는 다시 생성은 head 가 옮겨 가기 전에는 붙이지 않는다. 그 전에 파드가 죽으면 앞 턴이
    그대로 남는다. 옮겨 간 뒤에는 표식과 사용자 항목부터 붙인다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc) as running:
        state = running.runtime
        transcript = TurnTranscript(rewind=True)
        transcript.add_user("다시")
        fork = _Fork(moved=False)
        closed = _ClosedItems(state, "s1", transcript, fork, token="t1")  # type: ignore[arg-type]
        closed.mark()
        await asyncio.sleep(0.05)
        assert not closed.branched and await state.store.transcripts.read("s1") == []
        fork.moved = True
        closed.mark()
        await wait_for(lambda: _saved(running, "s1", 2), what="갈래 뒤의 붙이기")
        saved = await state.store.transcripts.read("s1")
        assert [i["kind"] for i in saved] == ["rewind", "user"]
        assert closed.branched and transcript.items == []
        await closed.close()


async def test_a_regenerate_of_the_first_turn_saves_from_the_start(support_doc, fake_mcp) -> None:
    """첫 턴의 다시 생성은 스레드를 지우고 시작하므로 처음 조각부터 붙인다. 끝난 전사는 다시 생성된
    턴 하나다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.05)
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, ADD)
        sid = sid_of(sse, "done")
        response = await running.client.post(
            f"/sessions/{sid}/regenerate", json={"user_sub": "alice"}
        )
        assert response.status_code == 200, response.text
        await wait_for(lambda: turns_ended(running, "done", 2), what="다시 생성의 마감")
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        kinds = [i["kind"] for i in detail["transcript"]["items"]]
        assert kinds == ["user", "tool_call", "assistant", "done"]


async def test_the_final_event_comes_after_the_lease_is_released(
    support_doc, fake_mcp, monkeypatch
) -> None:
    """끝을 본 사람이 곧바로 보낸 다음 말은 turn_in_progress 로 거절되지 않는다. 마지막
    이벤트(done)는 마감(전사, 위임, 리스)이 끝난 뒤에 나간다. 리스 풀기가 늦어도 done 을 받은
    때에는 이미 풀렸다."""
    async with run_app(support_doc) as running:
        index = running.runtime.store.index
        release = index.release_turn

        async def slow_release(session_id: str, token: str) -> None:
            await asyncio.sleep(0.2)
            await release(session_id, token)

        monkeypatch.setattr(index, "release_turn", slow_release)
        sid, response = await open_chat(running, ADD)
        free_at_done = None
        async for chunk in response.body_iterator:
            if chunk.startswith("event: done"):
                free_at_done = await lease_free(index, sid)
        assert free_at_done is True
