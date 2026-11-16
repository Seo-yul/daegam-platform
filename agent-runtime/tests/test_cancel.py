"""진행 중 턴을 멈추는 API 다(D65). 같은 파드에 닿은 중지는 감시를 바로 알리고 다른 파드가 적은
표시는 감시가 읽어 멈춘다. 멈출 수 있는 사람은 결정할 수 있는 사람과 같다. 승인 뒤 재개 턴을 멈추면
세션이 풀리고, 턴 제한 시간이 지나는 중에 온 중지는 시간 초과로 남는다."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import PrivateAttr

from runtime.cancel import CancelWatch
from runtime.hitl import APPROVER_HEADER
from runtime.sessions import MemorySessionIndex
from runtime.state import Settings
from runtime.stub import StubChatModel
from tests.app_fixtures import ToolThenSleepModel, decide, running_sid, sid_of, sleeping_turn
from tests.conftest import (
    RunningApp,
    chat,
    event_names,
    events_named,
    parse_sse,
    run_app,
    wait_for,
)

# 다른 파드가 적은 표시를 읽는 주기를 줄인 설정이다. 표시를 읽는 테스트가 기본 1초를 기다리지 않는다
FAST_POLL = Settings(cancel_poll_interval=0.05)


async def _assert_stopped_by_request(running: RunningApp, response: httpx.Response) -> None:
    """요청으로 멈춘 턴은 아직 붙어 있는 클라이언트에 마지막 이벤트 error{turn_cancelled} 를 내고,
    끊긴 턴과 같은 마감을 지난다(turn_end.status 는 cancelled, 전사의 turn_cancelled)."""
    assert response.status_code == 200
    sse = parse_sse(response.text)
    assert event_names(sse) == ["error"]
    assert sse[0][1]["code"] == "turn_cancelled" and "요청" in sse[0][1]["message"]
    ends = running.observed("turn_end")
    assert ends[-1]["status"] == "cancelled"


async def test_cancel_request_on_the_same_pod_stops_the_turn_at_once(support_doc) -> None:
    """같은 파드에 닿은 중지는 감시를 바로 알린다. 5초 자는 호출이 곧바로 멈추고 리스가 풀려 같은
    세션의 다음 턴이 200 이다. 턴이 없으면 409, 여러 번 불러도 같다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        started = asyncio.get_running_loop().time()
        cancel = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        assert cancel.status_code == 202 and cancel.json() == {"session_id": sid}
        # 아직 마감 중이면 다시 불러도 같다
        again = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        assert again.status_code in (202, 409)
        response = await task
        assert asyncio.get_running_loop().time() - started < 2
        await _assert_stopped_by_request(running, response)
        # 턴이 끝난 뒤에는 멈출 것이 없다
        idle = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        assert idle.status_code == 409 and idle.json()["code"] == "no_turn_in_progress"
        response, sse = await chat(running, "다시", session_id=sid)
        assert response.status_code == 200 and events_named(sse, "done")[0]["turn"] == 1
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        kinds = [i["kind"] for i in detail["transcript"]["items"]]
        assert kinds == ["user", "error", "user", "assistant", "done"]
        assert detail["transcript"]["items"][1]["code"] == "turn_cancelled"
        assert running.ended("cancelled") == 1 and running.ended("done") == 1


async def test_cancel_mark_from_another_pod_is_polled(support_doc) -> None:
    """표시만 적힌 경우(다른 파드가 받은 요청)는 감시가 주기마다 표시를 읽어 멈춘다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, settings=FAST_POLL, over_http=True) as running:
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        started = asyncio.get_running_loop().time()
        assert await running.runtime.store.index.request_cancel(sid) is not None
        response = await task
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 2, f"{elapsed:.1f}초 걸렸다"
        await _assert_stopped_by_request(running, response)


async def test_late_mark_with_the_previous_token_does_not_stop_the_next_turn(
    support_doc,
) -> None:
    """앞 턴의 토큰을 든 늦은 표시는 다음 턴을 멈추지 않는다. 표시는 리스를 잡은 턴의 토큰에 묶여
    있기 때문이다. 턴이 자는 동안 감시는 그 표시를 여러 번 읽는다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, settings=FAST_POLL, over_http=True) as running:
        index = running.runtime.store.index
        assert isinstance(index, MemorySessionIndex)
        task = await sleeping_turn(running, 1)
        sid = running_sid(running)
        # 앞 턴이 남긴 것과 같은 꼴의 표시다. 지금 턴의 토큰이 아니다
        index._cancels[sid] = "token-of-the-previous-turn"
        response = await task
        sse = parse_sse(response.text)
        assert event_names(sse)[-1] == "done"
        assert running.ended("cancelled") == 0


async def test_cancel_follows_the_decision_subject_rules(support_doc) -> None:
    """멈출 수 있는 사람은 결정할 수 있는 사람과 같다. 남의 세션은 403 이고 플랫폼이 표시한 승인자는
    멈출 수 있다. 없는 세션은 404 다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        missing = await running.client.post("/sessions/nope/cancel", json={"user_sub": "alice"})
        assert missing.status_code == 404 and missing.json()["code"] == "session_not_found"
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        other = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "bob"})
        assert other.status_code == 403 and other.json()["code"] == "not_session_owner"
        # 본문의 user_sub 가 없으면 프록시 경로의 422 다
        assert (await running.client.post(f"/sessions/{sid}/cancel", json={})).status_code == 422
        approver = await running.client.post(
            f"/sessions/{sid}/cancel", json={"user_sub": "bob"}, headers={APPROVER_HEADER: "1"}
        )
        assert approver.status_code == 202
        await _assert_stopped_by_request(running, await task)


async def test_cancelled_resume_frees_the_session(support_doc, fake_mcp) -> None:
    """승인 뒤 재개 턴을 도구 결과를 받은 다음 모델 호출에서 멈춘다. 체크포인트의 인터럽트는 이미
    소비됐으므로 색인도 active 이고 같은 세션의 다음 말이 200 이다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    model = ToolThenSleepModel(model_name="stub")
    async with run_app(support_doc, model=model, over_http=True) as running:
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        sid = sid_of(sse, "interrupt")
        task = asyncio.create_task(decide(running, sid, [{"type": "approve"}]))
        await model.sleeping.wait()
        cancel = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        assert cancel.status_code == 202
        resumed = parse_sse((await task).text)
        assert resumed[-1][0] == "error" and resumed[-1][1]["code"] == "turn_cancelled"
        assert fake_mcp.calls["Add"] == 1
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["pending"] is False and detail["session"]["status"] == "active"
        assert detail["session"]["pending_since"] is None
        response, sse = await chat(running, '{"sleep": 0}', session_id=sid)
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done"


class _SlowToStopModel(StubChatModel):
    """취소를 늦게 받는 모델이다. 호출은 오래 자고, 취소가 오면 `stopping` 을 알린 뒤 `release` 가
    열릴 때까지 멈추지 않는다. 턴 제한 시간이 지나 그래프를 닫는 동안이 만료 중인 제한 시간이다."""

    _stopping: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def stopping(self) -> asyncio.Event:
        return self._stopping

    @property
    def release(self) -> asyncio.Event:
        return self._release

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self._stopping.set()
            await asyncio.wait_for(self._release.wait(), timeout=5)
            raise
        return await super()._agenerate(messages, stop, run_manager, **kw)


async def test_cancel_while_the_turn_limit_expires_stays_a_timeout(support_doc) -> None:
    """턴 제한 시간이 지나 그래프를 닫는 사이에 온 중지는 제한 시간을 당기지 않는다. 중지는 202 이고
    턴은 turn_timeout 으로 남는다."""
    support_doc["main"]["tools"] = []
    support_doc["main"]["limits"]["turn_timeout_seconds"] = 1
    model = _SlowToStopModel(model_name="stub")
    async with run_app(support_doc, model=model, over_http=True) as running:
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        await model.stopping.wait()
        cancel = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        model.release.set()
        assert cancel.status_code == 202, cancel.text
        sse = parse_sse((await task).text)
        assert event_names(sse) == ["error"] and sse[0][1]["code"] == "turn_timeout"
        assert running.ended("turn_timeout") == 1 and running.ended("cancelled") == 0


async def test_a_named_stop_stops_only_that_attempt(support_doc) -> None:
    """진행 중인 세션 한 줄은 그 시도의 turn_id 를 싣는다(P78 2절). 다른 시도의 이름으로 부른 중지는
    409 no_turn_in_progress 이고 턴은 그대로 돌며, 그 시도의 이름으로 부르면 멈춘다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        row = (await running.client.get(f"/sessions/{sid}")).json()["session"]
        assert row["running"] is True and row["turn_id"]
        body = {"user_sub": "alice", "turn_id": "turn-of-another-attempt"}
        missed = await running.client.post(f"/sessions/{sid}/cancel", json=body)
        assert missed.status_code == 409 and missed.json()["code"] == "no_turn_in_progress"
        assert not task.done()
        body["turn_id"] = row["turn_id"]
        stopped = await running.client.post(f"/sessions/{sid}/cancel", json=body)
        assert stopped.status_code == 202
        await _assert_stopped_by_request(running, await task)


async def test_a_stop_naming_the_previous_attempt_spares_the_next_turn(support_doc) -> None:
    """앞 턴이 끝나고 다음 턴이 리스를 잡았지만 아직 제 시도를 적지 않은 사이다(P78 2절). 앞 시도의
    이름으로 온 늦은 중지는 표시를 적지 않아 409 이고, 이 파드에 남은 앞 턴의 감시가 있어도 리스의
    토큰과 다르면 알리지 않는다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "안녕")
        sid = sid_of(sse, "done")
        previous = events_named(sse, "done")[0]["turn_id"]
        index = running.runtime.store.index
        assert await index.claim_turn(sid, lease_seconds=30, token="next") is True
        stale = CancelWatch(index, sid, "previous", poll_seconds=1)
        running.runtime.cancels[sid] = stale
        body = {"user_sub": "alice", "turn_id": previous}
        named = await running.client.post(f"/sessions/{sid}/cancel", json=body)
        assert named.status_code == 409 and named.json()["code"] == "no_turn_in_progress"
        assert await index.cancel_requested(sid, "next") is False
        unnamed = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        assert unnamed.status_code == 202
        assert await index.cancel_requested(sid, "next") is True
        assert stale.reason is None, "리스를 잡은 턴이 아닌 감시를 알렸다"
        del running.runtime.cancels[sid]
        await index.release_turn(sid, "next")


async def test_cancel_all_turns_stops_every_running_turn(support_doc) -> None:
    """`POST /turns/cancel` 은 이 인스턴스의 진행 중 턴을 모두 멈추고 아직 리스가 살아 있는 수를
    준다(P78 5절). 다 멈춘 뒤에는 0 이다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        first = await sleeping_turn(running, 5)
        second = await sleeping_turn(running, 5)

        async def both_watched() -> bool:
            return len(running.runtime.cancels) == 2

        await wait_for(both_watched, what="두 턴의 중지 감시")
        answer = await running.client.post("/turns/cancel", json={"user_sub": "alice"})
        assert answer.status_code == 202 and answer.json() == {"running": 2}
        for task in (first, second):
            await _assert_stopped_by_request(running, await task)
        again = await running.client.post("/turns/cancel", json={"user_sub": "alice"})
        assert again.json() == {"running": 0}
