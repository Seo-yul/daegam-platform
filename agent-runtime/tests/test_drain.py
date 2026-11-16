"""파드 종료의 기다림이다(P78 5절, drain.py). env 가 없으면 SIGTERM 에 도는 턴을 곧바로 멈춰
turn_aborted 로 마감한다. drain 이 있으면 그 안에 끝난 턴은 done 이고 남은 턴은 drain 끝에 함께
멈춘다. 공유하지 않는 저장소에서는 읽는 사람이 없는 턴을 곧바로 멈춘다. 위임 갱신의 403 은 사람이
접근을 잃은 것이라 턴을 멈추고(turn_access_lost), 종료 중의 401 과 이벤트 보내기의 401 은 기다리던
턴을 멈춘다. 신호 처리기는 drain 을 한 번만 건다."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import pytest
import uvicorn

from runtime.cancel import ACCESS_LOST, SHUTDOWN, CancelWatch
from runtime.drain import SETTLE_SECONDS, Drain, DrainServer, drain_seconds
from runtime.sessions import SessionStore
from runtime.state import Settings
from runtime.turn import _grant_refused
from tests.app_fixtures import (
    ToolThenSleepModel,
    last_event,
    lease_free,
    open_chat,
    read_in_background,
    turns_ended,
)
from tests.conftest import RunningApp, run_app, wait_for

ADD = '{"tool": "Add", "args": {"a": 1, "b": 2}}'


async def _start(running: RunningApp, model: ToolThenSleepModel) -> tuple[str, Any]:
    """도구를 부른 뒤 자는 턴을 열고 모델이 자기 시작하기를 기다린다."""
    sid, response = await open_chat(running, ADD)
    await model.sleeping.wait()
    return sid, response


async def test_without_a_drain_sigterm_stops_every_turn_as_aborted(support_doc, fake_mcp) -> None:
    """drain 이 없으면 종료가 시작될 때 도는 턴을 곧바로 멈춘다. 읽기는 마지막 이벤트로 turn_aborted
    를 받고, turn_end 의 status 도 turn_aborted 라 오류율에 든다. 전사에는 결과를 받은 카드와 그
    오류가 남는다."""
    model = ToolThenSleepModel(model_name="stub", pause=30)
    async with run_app(support_doc, model=model) as running:
        sid, response = await _start(running, model)
        reader = read_in_background(response)
        running.app.state.drain.begin(running.runtime)
        chunks = await reader
        assert last_event(chunks) == ("error", "turn_aborted")
        await wait_for(lambda: turns_ended(running, "turn_aborted"), what="turn_aborted 마감")
        items = await running.runtime.store.transcripts.read(sid)
        assert [i["kind"] for i in items] == ["user", "tool_call", "error"]
        assert items[-1]["code"] == "turn_aborted"


async def test_a_watched_turn_finishes_within_the_drain(support_doc, fake_mcp) -> None:
    """drain 안에 끝나는 턴은 done 으로 끝나고, 종료는 그 마감을 기다린다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.3)
    settings = Settings(turn_drain_seconds=5)
    async with run_app(support_doc, model=model, settings=settings) as running:
        _, response = await _start(running, model)
        reader = read_in_background(response)
        drain = running.app.state.drain
        drain.begin(running.runtime)
        await drain.settle(running.runtime)
        assert running.ended("done") == 1 and running.ended("turn_aborted") == 0
        assert (await reader)[-1].startswith("event: done")


async def test_the_drain_deadline_stops_the_rest(support_doc, fake_mcp) -> None:
    """drain 이 끝날 때 아직 도는 턴은 함께 멈춘다. 읽기는 마지막 이벤트를 받는다."""
    model = ToolThenSleepModel(model_name="stub", pause=30)
    settings = Settings(turn_drain_seconds=0.2)
    async with run_app(support_doc, model=model, settings=settings) as running:
        _, response = await _start(running, model)
        reader = read_in_background(response)
        drain = running.app.state.drain
        drain.begin(running.runtime)
        await drain.settle(running.runtime)
        assert running.ended("turn_aborted") == 1
        assert last_event(await reader) == ("error", "turn_aborted")


async def test_an_unshared_store_stops_a_turn_nobody_reads(support_doc, fake_mcp) -> None:
    """memory 노드가 없는 파드에서는 읽는 사람이 남지 않은 턴을 곧바로 멈춘다. 결과가 파드와 함께
    사라지기 때문이다. 종료가 시작된 뒤에 읽기가 떠나도 그때 멈춘다. 공유하는 저장소면 기다린다."""
    model = ToolThenSleepModel(model_name="stub", pause=30)
    settings = Settings(turn_drain_seconds=30)
    async with run_app(support_doc, model=model, settings=settings) as running:
        assert running.runtime.store.shared is False
        _, response = await _start(running, model)
        await response.body_iterator.aclose()
        running.app.state.drain.begin(running.runtime)
        await wait_for(
            lambda: turns_ended(running, "turn_aborted"), what="읽는 사람 없는 턴의 마감"
        )

    model = ToolThenSleepModel(model_name="stub", pause=30)
    async with run_app(support_doc, model=model, settings=settings) as running:
        _, response = await _start(running, model)
        reader = read_in_background(response)
        running.app.state.drain.begin(running.runtime)
        await asyncio.sleep(0.05)
        assert running.ended("turn_aborted") == 0, "읽는 사람이 있는 동안은 기다린다"
        # 응답이 닫히면 ClosingStreamingResponse 가 읽기를 닫는다
        reader.cancel()
        await response.body_iterator.aclose()
        await wait_for(lambda: turns_ended(running, "turn_aborted"), what="떠난 읽기의 턴")


async def test_a_shared_store_keeps_waiting_for_a_turn_nobody_reads(support_doc, fake_mcp) -> None:
    """공유하는 저장소면 읽는 사람이 없어도 drain 동안 기다린다. 결과를 다른 파드에서 읽는다."""
    model = ToolThenSleepModel(model_name="stub", pause=0.3)
    settings = Settings(turn_drain_seconds=5)
    async with run_app(support_doc, model=model, settings=settings) as running:
        running.runtime.store.shared = True
        _, response = await _start(running, model)
        await response.body_iterator.aclose()
        drain = running.app.state.drain
        drain.begin(running.runtime)
        await drain.settle(running.runtime)
        assert running.ended("done") == 1


async def test_a_turn_that_starts_after_shutdown_began_is_stopped_without_a_drain(
    support_doc, fake_mcp
) -> None:
    """SIGTERM 때 아직 입구에 있던 요청의 턴이다. drain 이 없으면 감시를 걸자마자 멈춰 마지막
    이벤트로 turn_aborted 를 낸다. 종료가 시작될 때 한 번 멈춘 턴들만 멈추면 이 턴은 끝까지 돈다."""
    model = ToolThenSleepModel(model_name="stub", pause=30)
    async with run_app(support_doc, model=model) as running:
        running.app.state.drain.begin(running.runtime)
        _, response = await open_chat(running, ADD)
        chunks = await read_in_background(response)
        assert last_event(chunks) == ("error", "turn_aborted")


async def test_the_lifespan_settles_running_turns_before_it_closes_the_store(
    support_doc, fake_mcp, monkeypatch
) -> None:
    """앱이 닫힐 때 도는 턴의 마감(turn_end, 전사, 리스)이 저장소를 닫기 전에 끝난다(main.py 의
    lifespan 본문). 스택이 먼저 닫히면 drain 안에 끝난 턴의 결과가 남지 않는다."""
    apps: list[RunningApp] = []
    closed_after: list[int] = []
    close = SessionStore.close

    async def spied_close(store: SessionStore) -> None:
        closed_after.append(apps[0].ended("done"))
        await close(store)

    # 기동이 스택에 올리는 것은 그때의 close 라 앱을 열기 전에 바꾼다
    monkeypatch.setattr(SessionStore, "close", spied_close)
    model = ToolThenSleepModel(model_name="stub", pause=0.3)
    async with run_app(
        support_doc, model=model, settings=Settings(turn_drain_seconds=5)
    ) as running:
        apps.append(running)
        # 공유하는 저장소라 읽는 사람이 없어도 drain 안에서 끝까지 기다린다
        running.runtime.store.shared = True
        _, response = await _start(running, model)
        await response.body_iterator.aclose()
    assert closed_after == [1], "저장소를 닫을 때 턴이 아직 마감하지 않았다"


class _GrantEnds:
    """턴이 위임을 닫으며 알린 끝난 까닭을 모은다(`TurnGrants.close`)."""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def close(self, calls: Any, reason: str) -> None:
        self.reasons.append(reason)


@pytest.mark.parametrize(
    ("stop", "reason"),
    [("cut", "done"), ("shutdown", "shutdown"), ("access_lost", "error")],
)
async def test_each_end_closes_the_grant_with_its_reason(
    support_doc, fake_mcp, monkeypatch, stop: str, reason: str
) -> None:
    """읽기가 떠난 턴은 끝까지 가서 done, 파드 종료(turn_aborted)는 shutdown, 접근을 잃은
    사람(turn_access_lost)은 error 로 위임을 닫는다(P53 4절, P78 1절)."""
    pause = 0.3 if stop == "cut" else 30
    model = ToolThenSleepModel(model_name="stub", pause=pause)
    async with run_app(support_doc, model=model) as running:
        ends = _GrantEnds()
        monkeypatch.setattr(running.runtime.grants, "close", ends.close)
        sid, response = await _start(running, model)
        if stop == "cut":
            await response.body_iterator.aclose()
        elif stop == "shutdown":
            running.runtime.cancels[sid].notify(SHUTDOWN)
        else:
            running.runtime.cancels[sid].notify(ACCESS_LOST)
        await wait_for(lambda: lease_free(running.runtime.store.index, sid), what="턴의 마감")
        assert ends.reasons == [reason]


def _watch() -> CancelWatch:
    return CancelWatch(None, "s1", "t1", poll_seconds=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "draining", "code"),
    [
        (403, False, "turn_access_lost"),
        (403, True, "turn_access_lost"),
        (401, True, "turn_aborted"),
        (401, False, None),
        (410, True, None),
    ],
)
def test_a_refused_renewal_stops_the_turn_for_its_reason(
    status: int, draining: bool, code: str | None
) -> None:
    """갱신의 403 은 사람이 접근을 잃은 것이라 언제든 멈춘다. 401 은 종료 중일 때만 인스턴스 토큰을
    회수당한 것으로 보고 멈춘다. 그 밖의 거절은 위임만 멈춘다."""

    class _State:
        pass

    state = _State()
    state.draining = draining
    watch = _watch()
    _grant_refused(state, watch, status)  # type: ignore[arg-type]
    assert (watch.reason.code if watch.reason else None) == code


async def test_an_unauthorized_event_push_while_draining_stops_the_turns() -> None:
    """종료가 시작되면 이벤트 보내기의 401(회수된 인스턴스 토큰)이 기다리던 턴을 모두 멈추게
    한다."""

    class _Shipper:
        on_unauthorized: Any = None

    class _Events:
        shipper = _Shipper()

    class _Store:
        shared = True

    class _State:
        cancels = {"s1": _watch()}
        events = _Events()
        store = _Store()
        draining = False

    state = _State()
    drain = Drain(30)
    drain.begin(state)  # type: ignore[arg-type]
    assert state.draining and state.events.shipper.on_unauthorized == drain.stop_all
    assert state.cancels["s1"].reason is None, "공유하는 저장소면 기다린다"
    state.events.shipper.on_unauthorized()
    assert state.cancels["s1"].reason is SHUTDOWN


async def test_a_turn_admitted_after_the_drain_deadline_stops_at_once() -> None:
    """drain 끝의 타이머가 이미 돈 뒤에 감시를 건 턴이다. 아무도 멈추지 않으면 lifespan 이 마감 몫만
    기다리고 그 밑의 저장소를 닫으므로 곧바로 멈춘다. 끝 전에 든 턴은 기다린다."""

    class _Store:
        shared = True

    class _State:
        cancels: dict[str, CancelWatch] = {}
        events = type("_Events", (), {"shipper": None})()
        store = _Store()
        draining = False

    drain = Drain(30)
    drain.begin(_State())  # type: ignore[arg-type]
    early = _watch()
    drain.admit(early)
    assert early.reason is None
    assert drain.deadline is not None
    drain.deadline = asyncio.get_running_loop().time() - 1
    late = _watch()
    drain.admit(late)
    assert late.reason is SHUTDOWN


async def test_the_signal_handler_arms_the_drain_once() -> None:
    """신호 처리기는 루프에 drain 을 한 번만 건다. 다시 온 신호는 시계를 다시 걸지 않는다."""

    class _App:
        class state:  # noqa: N801  앱의 state 꼴
            runtime = None

    calls: list[Any] = []

    class _Drain(Drain):
        def begin(self, state: Any) -> None:
            calls.append(state)

    drain = _Drain(30)
    server = DrainServer(uvicorn.Config(app=None), drain=drain, app=_App())
    server._loop = asyncio.get_running_loop()
    server.handle_exit(signal.SIGTERM, None)
    server.handle_exit(signal.SIGTERM, None)
    await asyncio.sleep(0)
    assert calls == [None] and server.should_exit
    assert drain.graceful_timeout == int(30 + SETTLE_SECONDS)
    assert Drain(None).graceful_timeout == int(SETTLE_SECONDS)


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [(None, None), ("", None), ("abc", None), ("-1", None), ("0", 0.0), ("120", 120.0)],
)
def test_drain_seconds_reads_the_env(raw: str | None, seconds: float | None) -> None:
    assert drain_seconds(raw) == seconds


def test_settings_read_the_drain_from_the_env(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_TURN_DRAIN_SECONDS", "90")
    assert Settings.from_env().turn_drain_seconds == 90.0
    monkeypatch.delenv("DAEGAM_TURN_DRAIN_SECONDS")
    assert Settings.from_env().turn_drain_seconds is None


def test_a_stop_reason_keeps_the_first_one() -> None:
    """먼저 온 까닭이 남는다. 종료로 멈춘 뒤 온 중지 요청이 코드를 바꾸지 않는다."""
    watch = _watch()
    watch.notify(SHUTDOWN)
    watch.notify()
    assert watch.reason is SHUTDOWN
