"""P53 F5: 턴의 사람과 그 위임의 앞뒤(`services/turn_person.py`, 5.1~5.3절과 4절의 끝내기).

위임을 여는 서비스는 가짜(`FakeGrants`)로 두고 소유자 기록은 테스트 DB 에 쓴다. 채팅과 다시 생성은
부른 사람, 결정은 결정자·입력·소유자 검사로 사람이 정해지는 것, 런타임이 받아들인 세션에만 소유자
기록이 생기고 주인이 바뀌지 않는 것, 플랫폼이 스스로 위임을 끝내는 네 길을 본다. 끝으로 세 라우트가
위임을 런타임 본문에 싣고 끝내는 것을 앱으로 한 번 돌린다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

import daegam.services.turn_person as turn_person
from daegam.auth.principal import Principal
from daegam.services.agent_runtime import ProxyResponse
from daegam.services.turn_grants import (
    END_NOT_ACCEPTED,
    END_NOT_SENT,
    END_REFUSED,
    END_UNREACHABLE,
    GrantPerson,
    TurnLimits,
)
from daegam.services.turn_person import TurnDelegation, TurnPlan, holds_input
from daegam.store.agent_session_owners import (
    AGENT_SESSION_OWNERS,
    MongoAgentSessionOwnerStore,
    SessionOwner,
    owner_key,
)
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.aio import run_store
from tests.helpers.runtime import drop_route, record_call, runtime_refusal, sse
from tests.helpers.turns import FakeGrants

T0 = datetime(2026, 10, 26, 9, 0, 0)
DOC = SimpleNamespace(id="ag1")
SESSION_TTL = 3600
APPROVE = [{"type": "approve"}]


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(
        turn_person,
        "turn_limits",
        lambda _doc: TurnLimits(turn_timeout_seconds=120, session_ttl_seconds=SESSION_TTL),
    )


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


def _user(sub: str, tv: int | None = 3) -> Principal:
    raw: dict[str, Any] = {"preferred_username": sub}
    if tv is not None:
        raw["tv"] = tv
    return Principal(sub=sub, raw=raw)


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _answer(
    status: int = 200,
    *,
    session: str | None = None,
    grant: str | None = None,
    detached: bool = False,
) -> ProxyResponse:
    headers = {"content-type": "text/event-stream"}
    if session is not None:
        headers["x-daegam-session-id"] = session
    if grant is not None:
        headers["x-daegam-turn-grant"] = grant
    if detached:
        headers["x-daegam-detached-turn"] = "1"
    return ProxyResponse(status, headers, _chunks(b"event: done\n\n"))


class Runtime:
    """턴 하나를 받는 가짜 런타임 호출이다. 받은 위임을 남기고 `answer` 로 답한다. 위임이 있으면
    답이 그 id 를 메아리친다. detached 면 턴을 요청과 떼어 돌리는 런타임의 답이다(P78)."""

    def __init__(
        self,
        *,
        session: str | None = None,
        echo: bool = True,
        status: int = 200,
        detached: bool = False,
    ):
        self.session = session
        self.echo = echo
        self.status = status
        self.detached = detached
        self.grants: list[dict[str, Any] | None] = []

    async def __call__(self, turn_grant: dict[str, Any] | None) -> ProxyResponse:
        self.grants.append(turn_grant)
        grant = turn_grant["id"] if turn_grant is not None and self.echo else None
        return _answer(self.status, session=self.session, grant=grant, detached=self.detached)


async def _drain(proxied: ProxyResponse) -> None:
    async for _ in proxied.body:
        pass


async def _turn(turns: TurnDelegation, plan: Any, runtime: Runtime) -> ProxyResponse:
    proxied = await turns.send(plan, runtime)
    await _drain(proxied)
    return proxied


def _record(sub: str = "alice", *, tv: int | None = 3, session: str = "s1") -> SessionOwner:
    return SessionOwner(
        agent_id=DOC.id,
        session_id=session,
        sub=sub,
        username=sub,
        tv=tv,
        confirmed_at=T0,
        last_turn_at=T0,
        expires_at=T0 + timedelta(days=1, seconds=SESSION_TTL),
    )


def _run(settings, scenario):
    """테스트 DB 의 소유자 기록 위에서 scenario(turns, grants, store) 를 돌린다."""

    async def go(store: MongoAgentSessionOwnerStore) -> Any:
        grants = FakeGrants()
        turns = TurnDelegation(grants, store, now=Clock())  # type: ignore[arg-type]
        return await scenario(turns, grants, store)

    return run_store(settings, MongoAgentSessionOwnerStore, go)


# -- 입력 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decisions", "expected"),
    [
        ([{"type": "approve"}], False),
        ([{"type": "approve", "message": "ok"}], False),
        ([{"type": "reject"}], False),
        ([{"type": "reject", "message": None}], False),
        ([{"type": "approve"}, {"type": "reject"}], False),
        ([{"type": "reject", "message": ""}], True),
        ([{"type": "reject", "message": "아니"}], True),
        ([{"type": "edit", "edited_action": {"name": "Divide", "args": {}}}], True),
        ([{"type": "respond", "message": "대신 이렇게"}], True),
        ([{"type": "approve"}, {"type": "later-kind"}], True),
        ([{}], True),
        (["approve"], True),
    ],
)
def test_input_follows_the_runtime_rule(decisions, expected):
    assert holds_input(decisions) is expected


def test_work_that_fails_before_the_call_ends_the_grant_as_not_sent():
    """기억한 선택을 읽거나 검사하다 실패하면 런타임을 부르지 않았으므로 위임을 끝낸다."""
    grants = FakeGrants()
    turns = TurnDelegation(grants, None)  # type: ignore[arg-type]
    plan = TurnPlan(DOC, "s1", datetime.now(), grant=SimpleNamespace(id="g1"))  # type: ignore[arg-type]

    async def refused() -> None:
        async with turns.preparing(plan):
            raise ValueError("remember refused")

    async def passes() -> None:
        async with turns.preparing(plan):
            pass

    with pytest.raises(ValueError):
        asyncio.run(refused())
    asyncio.run(passes())
    assert grants.ended == [("g1", END_NOT_SENT)]


# -- 채팅과 다시 생성 -------------------------------------------------------------------------


def test_the_owner_record_follows_only_the_session_the_runtime_accepted(settings, mongo):
    async def scenario(turns, grants, store):
        # 같은 세션 id 로 답하면 적고, 다른 id 로 답하면 어느 쪽에도 적지 않는다
        first = await turns.chat(DOC, _user("alice"), "s1")
        # 위임은 비밀과 토큰을 담으므로 계획의 repr 에 나오지 않는다
        assert first.grant is not None and "OpenedGrant" not in repr(first)
        await _turn(turns, first, Runtime(session="s1"))
        await _turn(turns, await turns.chat(DOC, _user("alice"), "s2"), Runtime(session="s9"))
        # 다음 채팅이 tv 를 고친다
        await _turn(turns, await turns.chat(DOC, _user("alice", 4), "s1"), Runtime(session="s1"))
        # 새 세션이라던 답이 남의 기록이 있는 세션을 대도 아무것도 바뀌지 않는다
        await _turn(turns, await turns.chat(DOC, _user("bob"), None), Runtime(session="s1"))
        # 기록이 다른 사람을 대는 세션의 채팅과 다시 생성은 위임을 열지 않는다
        before = len(grants.opened)
        mallory = Runtime(session="s1")
        await _turn(turns, await turns.chat(DOC, _user("mallory"), "s1"), mallory)
        await _turn(turns, await turns.regenerate(DOC, _user("mallory"), "s1"), mallory)
        return grants, before, mallory

    grants, before, mallory = _run(settings, scenario)
    assert len(grants.opened) == before and mallory.grants == [None, None]
    docs = {d["session_id"]: d for d in mongo[AGENT_SESSION_OWNERS].find()}
    assert set(docs) == {"s1"}
    assert docs["s1"]["sub"] == "alice" and docs["s1"]["tv"] == 4
    # bob 의 위임은 새 세션의 id 를 받는다. 기록만 남의 것이라 적지 않았다
    assert grants.attached[-1] == ("g4", "s1")


# -- 결정 ------------------------------------------------------------------------------------


def test_an_approvers_decision_without_input_resumes_as_the_owner(settings, mongo):
    async def scenario(turns, grants, store):
        await store.confirm(_record())
        out = []
        for decisions in (APPROVE, [{"type": "reject"}], [{"type": "reject", "message": None}]):
            plan = await turns.decide(DOC, _user("carol"), "s1", decisions, approver=True)
            await _turn(turns, plan, Runtime(session="s1"))
            out.append(grants.opened[-1])
        return out, await store.get(DOC.id, "s1")

    opened, record = _run(settings, scenario)
    owner = GrantPerson("alice", "alice", tv=3)
    for o in opened:
        assert o["person"] == owner and o["trigger"] == "decide"
        assert o["decided_by"] == "carol" and "session_owner" not in o
    # 승인자의 재개는 만료만 늘리고 소유자의 확인 시각과 tv 는 그대로다
    assert record.confirmed_at == T0 and record.tv == 3 and record.last_turn_at == T0
    assert record.expires_at > T0 + timedelta(days=1, seconds=SESSION_TTL)


def test_an_approvers_input_resumes_under_the_approvers_own_grant(settings, mongo):
    """입력을 싣는 결정(무엇이 입력인지는 `holds_input` 의 표가 든다)은 승인자 자신의 위임으로
    재개한다."""
    decisions = [{"type": "reject", "message": "아니"}]

    async def scenario(turns, grants, store):
        await store.confirm(_record())
        plan = await turns.decide(DOC, _user("carol", 7), "s1", decisions, approver=True)
        await _turn(turns, plan, Runtime(session="s1"))
        return grants, await store.get(DOC.id, "s1")

    grants, record = _run(settings, scenario)
    (opened,) = grants.opened
    assert opened["person"] == GrantPerson("carol", "carol", tv=7)
    assert opened["session_owner"] == "alice" and opened["trigger"] == "decide"
    assert record == _record()


def test_an_owner_who_fails_the_checks_gives_way_to_the_approvers_grant(settings, mongo):
    async def scenario(turns, grants, store):
        await store.confirm(_record())
        grants.refuse.add("alice")
        plan = await turns.decide(DOC, _user("carol"), "s1", APPROVE, approver=True)
        await _turn(turns, plan, Runtime(session="s1"))
        return grants, await store.get(DOC.id, "s1")

    grants, record = _run(settings, scenario)
    tried, took = grants.opened
    assert tried["person"].sub == "alice" and tried["decided_by"] == "carol"
    assert took["person"] == GrantPerson("carol", "carol", tv=3)
    assert took["session_owner"] == "alice"
    assert grants.ended == []
    # 소유자의 이름으로 돌지 않았으니 기록의 만료도 그대로다
    assert record == _record()


def test_without_an_owner_record_every_decider_takes_their_own_grant(settings, mongo):
    async def scenario(turns, grants, store):
        for sub, approver in (("carol", True), ("dave", False)):
            plan = await turns.decide(DOC, _user(sub), "s1", APPROVE, approver=approver)
            await _turn(turns, plan, Runtime(session="s1"))
        # 기록이 있으면 소유자도 승인자도 아닌 결정자에게는 위임을 열지 않는다
        await store.confirm(_record())
        runtime = Runtime(status=403)
        plan = await turns.decide(DOC, _user("dave"), "s1", APPROVE, approver=False)
        await turns.send(plan, runtime)
        return grants, runtime

    grants, refused = _run(settings, scenario)
    assert [(o["person"].sub, o["session_owner"]) for o in grants.opened] == [
        ("carol", None),
        ("dave", None),
    ]
    assert refused.grants == [None]
    # 기록은 소유자를 모를 때 생기지 않는다
    assert mongo[AGENT_SESSION_OWNERS].count_documents({}) == 1


# -- 플랫폼이 스스로 끝내기 ----------------------------------------------------------------------


def test_the_platform_ends_the_grant_on_every_way_the_call_ends(settings, mongo):
    class Unreachable(Exception):
        pass

    async def fails(_grant):
        raise Unreachable

    async def scenario(turns, grants, store):
        user = _user("alice")
        # 런타임이 거절했다. 상태와 본문은 라우트가 올리도록 그대로 돌려준다
        proxied = await turns.send(await turns.chat(DOC, user, None), Runtime(status=409))
        assert proxied.status == 409
        # 답 전에 실패했다
        with pytest.raises(Unreachable):
            await turns.send(await turns.chat(DOC, user, None), fails)
        # 위임 칸을 모르는 옛 파드가 받았다. 그 본문을 닫아도 다시 끝내지 않는다
        old = Runtime(session="s-old", echo=False)
        proxied = await turns.send(await turns.chat(DOC, user, None), old)
        await proxied.body.aclose()
        # 읽지 않고 닫힌 스트림(브라우저가 끊었다)은 끝내지 않는다. 런타임의 `/end` 와 기한이 끝낸다
        proxied = await turns.send(await turns.chat(DOC, user, None), Runtime(session="s-cut"))
        await proxied.body.aclose()
        return grants

    grants = _run(settings, scenario)
    assert grants.ended == [
        ("g1", END_REFUSED),
        ("g2", END_UNREACHABLE),
        ("g3", END_NOT_ACCEPTED),
    ]
    # 받아들이지 않은 위임에는 세션을 붙이지 않는다. 소유자 기록은 런타임이 받은 턴이라 적는다
    assert grants.attached == [("g4", "s-cut")]
    assert {d["session_id"] for d in mongo[AGENT_SESSION_OWNERS].find()} == {"s-old", "s-cut"}


def test_a_detached_turn_keeps_its_grant_when_the_stream_closes(settings, mongo):
    """런타임은 턴을 요청과 떼어 돌리므로 넘긴 스트림이 닫히거나 답 전에 브라우저가 떠나도 위임을
    끝내지 않는다. 런타임의 `/end` 와 기한이 끝낸다(P78 6절)."""

    async def cut(_grant):
        raise asyncio.CancelledError

    async def scenario(turns, grants, store):
        user = _user("alice")
        proxied = await turns.send(
            await turns.chat(DOC, user, None), Runtime(session="s1", detached=True)
        )
        await proxied.body.aclose()
        with pytest.raises(asyncio.CancelledError):
            await turns.send(await turns.chat(DOC, user, None), cut)
        return grants

    grants = _run(settings, scenario)
    assert grants.ended == []
    assert grants.attached == [("g1", "s1")]


def test_a_failing_end_or_record_does_not_break_the_turn(settings, mongo, caplog):
    class Broken(FakeGrants):
        async def end(self, grant_id: str, reason: str) -> None:
            raise RuntimeError("mongo is down")

    class BrokenOwners(MongoAgentSessionOwnerStore):
        async def confirm(self, owner: SessionOwner) -> bool:
            raise RuntimeError("mongo is down")

    async def go(store: BrokenOwners) -> list[bytes]:
        turns = TurnDelegation(Broken(), store, now=Clock())  # type: ignore[arg-type]
        refused = await turns.send(await turns.chat(DOC, _user("alice"), None), Runtime(status=409))
        assert refused.status == 409
        proxied = await turns.send(
            await turns.chat(DOC, _user("alice"), None), Runtime(session="s1")
        )
        return [chunk async for chunk in proxied.body]

    assert run_store(settings, BrokenOwners, go) == [b"event: done\n\n"]
    assert "끝내지 못했다" in caplog.text and "기록을 적지 못했다" in caplog.text


# -- 라우트 ----------------------------------------------------------------------------------


def _echoing_runtime(runtime: FastAPI) -> None:
    """채팅, 결정, 다시 생성이 받은 위임의 id 를 메아리치고 세션 id 를 알리는 런타임이다."""

    def answer(request: Request, body: dict[str, Any], sid: str) -> StreamingResponse:
        record_call(runtime, request, body=body)
        if body.get("message") == "busy":
            return runtime_refusal(429, "rate_limit", "잠시 뒤에")  # type: ignore[return-value]
        grant = body.get("turn_grant")
        headers = {"X-Daegam-Session-Id": sid}
        if grant is not None and not runtime.state.drop_grant:
            headers["X-Daegam-Turn-Grant"] = grant["id"]

        async def events() -> AsyncIterator[str]:
            yield sse("done", {"session_id": sid})

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    runtime.state.drop_grant = False
    for path in ("/chat", "/sessions/{sid}/decisions", "/sessions/{sid}/regenerate"):
        drop_route(runtime, path)

    @runtime.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        return answer(request, body, body.get("session_id") or "s-new")

    @runtime.post("/sessions/{sid}/decisions")
    async def decisions(sid: str, request: Request):
        return answer(request, await request.json(), sid)

    @runtime.post("/sessions/{sid}/regenerate")
    async def regenerate(sid: str, request: Request):
        return answer(request, await request.json(), sid)


def test_the_three_routes_carry_the_grant_and_end_it(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, features="regenerate")
    _echoing_runtime(agent.runtime)
    grants = FakeGrants()
    client.app.state.turn_grants = grants
    calls = agent.runtime.state.calls

    r = client.post(f"{agent.url}/chat", json={"message": "3 더하기 4"})
    assert r.status_code == 200 and r.headers["x-daegam-turn-grant"] == "g1"
    assert calls[-1]["body"]["turn_grant"] == {"id": "g1", "sub": "header"}
    owner = mongo[AGENT_SESSION_OWNERS].find_one({"_id": owner_key(agent.id, "s-new")})
    assert owner["sub"] == "header" and owner["tv"] is None
    r = client.post(
        f"{agent.url}/sessions/s-new/decisions", json={"decisions": [{"type": "approve"}]}
    )
    assert r.status_code == 200 and calls[-1]["body"]["turn_grant"]["id"] == "g2"
    r = client.post(f"{agent.url}/sessions/s-new/regenerate", json={})
    assert r.status_code == 200 and calls[-1]["body"]["turn_grant"]["id"] == "g3"
    # 런타임의 거절과 위임을 받지 않은 옛 파드
    r = client.post(f"{agent.url}/chat", json={"message": "busy"})
    assert r.status_code == 429
    agent.runtime.state.drop_grant = True
    assert client.post(f"{agent.url}/chat", json={"message": "또"}).status_code == 200

    assert [o["trigger"] for o in grants.opened] == ["chat", "decide", "regenerate", "chat", "chat"]
    assert grants.opened[1]["session_id"] == "s-new"
    assert grants.attached == [("g1", "s-new")]
    assert grants.ended == [("g4", END_REFUSED), ("g5", END_NOT_ACCEPTED)]


def test_a_chat_whose_upload_cleanup_fails_still_answers(
    client, fake_clusters, mongo, monkeypatch, caplog
):
    """런타임이 턴을 받은 뒤 업로드 정리가 실패해도 응답을 그대로 낸다. 본문을 닫아도 떼어 돈 턴은
    끝나지 않고, 남은 업로드는 기한이 지운다(P78 6절). 위임은 평소대로 스트림과 함께 끝난다."""
    agent = deployed_agent(client, fake_clusters, mongo)
    _echoing_runtime(agent.runtime)
    grants = FakeGrants()
    client.app.state.turn_grants = grants

    async def fails(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("upload store down")

    monkeypatch.setattr(client.app.state.chat_uploads, "consume", fails)
    r = client.post(f"{agent.url}/chat", json={"message": "3 더하기 4"})
    assert r.status_code == 200 and "event: done" in r.text
    assert "채팅 첨부를 지우지 못했다" in caplog.text
    assert grants.ended == []
