"""백그라운드 실행의 세션이다(P41 9절, runs.py). 플랫폼의 시작 워커처럼 `/chat` 에 run_id 를 싣고
세션 하나, 임시 턴, 세션의 untrusted, 결정의 run_id, 턴 끝의 닫기, 목록, 이벤트를 본다. 웹훅 실행의
바깥 표시와 실행 세션의 기억한 선택(P43 3·5절)도 여기서 본다. 색인이 run_id 를 지키는 계약은
store_fixtures.py 에 있다."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from runtime import stream
from runtime.auth import Caller, get_caller
from runtime.runs import RUN_ID_HEADER, check_run_decision, check_run_fields
from runtime.sessions import STATUS_CLOSED, STATUS_PENDING, SessionRecord, utcnow
from runtime.turn_gate import check_still_open
from tests.app_fixtures import (
    FAIL_MARK,
    FailOnceModel,
    regenerate,
    resume,
    sid_of,
    status_and_code,
)
from tests.conftest import RunningApp, chat, events_named
from tests.safety_fixtures import ADD, RULE, choose, interrupt_of, safety_app, turns_doc
from tests.store_fixtures import run_record
from tests.web_fixtures import ScriptModel, call, results_of

RUN = "run-0001"
OWNER = Caller(sub="alice", external=False, approver=False)
OUTSIDE = Caller(sub="alice", external=True, approver=False)


def write_doc(url: str, **kwargs: Any) -> dict[str, Any]:
    """Add 가 안쪽의 쓰기 Tool 인 문서다. 바깥 내용이 없는 대화에서는 묻지 않고 돈다."""
    return turns_doc(url, inside=("Add",), **kwargs)


async def record_of(running: RunningApp, sid: str) -> SessionRecord:
    record = await running.runtime.store.index.get(sid)
    assert record is not None
    return record


async def test_a_run_turn_closes_its_session_and_a_second_start_names_it(fake_mcp) -> None:
    doc = write_doc(fake_mcp.url)
    async with safety_app(doc) as running:
        response, sse = await chat(running, ADD, run_id=RUN)
        assert response.status_code == 200, response.text
        assert response.headers[RUN_ID_HEADER] == RUN
        assert response.headers["X-Daegam-Detached-Turn"] == "1"
        sid = sid_of(sse, "done")
        record = await record_of(running, sid)
        assert (record.run_id, record.untrusted, record.status) == (RUN, False, STATUS_CLOSED)
        # 닫기는 체크포인트를 지우고 전사는 남긴다
        snapshot = await running.runtime.agent.graph.aget_state(stream.thread_config(sid))
        assert not snapshot.values
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["session"]["run_id"] == RUN and "untrusted" not in detail["session"]
        assert detail["transcript"]["items"][-1]["kind"] == "done"

        # 같은 실행의 둘째 시작은 닫힌 세션도 찾고, 어떤 검사보다 먼저 답한다(넘친 입력도)
        too_long = "x" * (doc["entry"]["max_input_chars"] + 1)
        again = await running.client.post(
            "/chat", json={"message": too_long, "user_sub": "alice", "run_id": RUN}
        )
        assert status_and_code(again) == (409, "run_exists")
        assert again.json()["session_id"] == sid
        assert record.turn_id is not None
        assert again.json()["turn_id"] == record.turn_id


async def test_a_start_that_loses_the_race_for_its_run_names_the_winner(
    fake_mcp, monkeypatch
) -> None:
    """앞의 찾기를 지난 둘째 시작도 색인이 run_id 를 한 세션에만 주므로 409 `run_exists` 다. 찾기가
    아무것도 못 찾은 것처럼 두고, 이긴 세션은 색인에 바로 만들어 둔다(아직 시도를 적지 않았다)."""
    async with safety_app(write_doc(fake_mcp.url)) as running:
        index = running.runtime.store.index
        await index.create(run_record("winner", "alice", RUN))

        async def nothing(run_id: str) -> None:
            return None

        monkeypatch.setattr(index, "find_run", nothing)
        response, _ = await chat(running, ADD, run_id=RUN)
        assert status_and_code(response) == (409, "run_exists")
        assert response.json()["session_id"] == "winner"
        assert response.json()["turn_id"] is None
        assert fake_mcp.calls["Add"] == 0
        listed = await index.list(runs="only")
        assert [row.session_id for row in listed] == ["winner"]


async def test_an_untrusted_run_flags_its_session_and_every_turn_keeps_it(fake_mcp) -> None:
    """웹훅 실행은 처음부터 바깥 내용을 가진 대화다. 안쪽 쓰기도 이유 outside 로 묻고, 결정 재개는
    untrusted 와 ephemeral 을 싣지 않아도 표시된 대화로 임시로 돈다. 재개된 턴의 둘째 쓰기도
    묻는다."""
    model = ScriptModel(calls=[call("Add", a=1, b=2), call("Add", a=3, b=4)])
    async with safety_app(write_doc(fake_mcp.url), model=model) as running:
        response, sse = await chat(running, ADD, run_id=RUN, untrusted=True)
        assert response.status_code == 200, response.text
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "outside" and fake_mcp.calls["Add"] == 0
        sid = response.headers["X-Daegam-Session-Id"]
        record = await record_of(running, sid)
        assert record.untrusted and record.outside_at is not None
        assert record.status == STATUS_PENDING

        approve = [{"type": "approve"}]
        resumed, sse = await resume(running, sid, approve, run_id=RUN)
        assert resumed.status_code == 200, resumed.text
        assert resumed.headers[RUN_ID_HEADER] == RUN
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "outside" and fake_mcp.calls["Add"] == 1
        resumed, sse = await resume(running, sid, approve, run_id=RUN)
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 2
        assert (await record_of(running, sid)).status == STATUS_CLOSED

        starts = running.observed("turn_start")
        assert [(s["run_id"], s["ephemeral"]) for s in starts] == [(RUN, True)] * 3
        assert [e["run_id"] for e in running.observed("turn_end")] == [RUN] * 3


async def test_a_schedule_run_is_not_flagged(fake_mcp) -> None:
    async with safety_app(write_doc(fake_mcp.url)) as running:
        _, sse = await chat(running, ADD, run_id=RUN)
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1
        [result] = results_of(sse, "Add")
        assert "untrusted" not in result


async def test_decisions_new_messages_and_regenerates_into_a_run_session(fake_mcp) -> None:
    async with safety_app(write_doc(fake_mcp.url)) as running:
        response, _ = await chat(running, ADD, run_id=RUN, untrusted=True)
        sid = response.headers["X-Daegam-Session-Id"]
        approve = [{"type": "approve"}]
        for run_id in (None, "run-other"):
            refused, _ = await resume(running, sid, approve, run_id=run_id)
            assert status_and_code(refused) == (409, "run_closed")
        message, _ = await chat(running, ADD, session_id=sid)
        assert status_and_code(message) == (409, "run_session")
        again, _ = await regenerate(running, sid)
        assert status_and_code(again) == (409, "run_session")
        assert fake_mcp.calls["Add"] == 0
        assert (await record_of(running, sid)).status == STATUS_PENDING


def test_a_run_decision_needs_the_platform_path_and_the_sessions_run() -> None:
    now = utcnow()
    run = SessionRecord("s1", None, "alice", now, now, run_id=RUN)
    chat_session = SessionRecord("s2", None, "alice", now, now)
    check_run_decision(OWNER, run, RUN)
    check_run_decision(OWNER, chat_session, None)
    check_run_decision(OUTSIDE, chat_session, None)
    for caller, record, run_id, expected in (
        (OUTSIDE, run, RUN, (403, "forbidden")),
        (OWNER, run, None, (409, "run_closed")),
        (OWNER, chat_session, RUN, (409, "run_closed")),
    ):
        with pytest.raises(HTTPException) as e:
            check_run_decision(caller, record, run_id)
        assert (e.value.status_code, e.value.detail["code"]) == expected


def test_run_fields_come_with_a_new_session_only() -> None:
    check_run_fields(RUN, True, None)
    check_run_fields(None, False, "s1")
    for run_id, untrusted, session_id, field in (
        (RUN, False, "s1", "run_id"),
        (None, True, None, "untrusted"),
    ):
        with pytest.raises(HTTPException) as e:
            check_run_fields(run_id, untrusted, session_id)
        assert e.value.detail["code"] == "validation_error"
        [entry] = e.value.detail["errors"]
        assert entry["loc"] == ["body", field] and entry["type"] == "run_fields"
        assert e.value.detail["message"] == f"body.{field}: {entry['msg']}"
        assert entry["msg"] != "Value error"


async def test_run_fields_are_refused_on_the_external_path(fake_mcp) -> None:
    """외부 경로의 run_id 와 untrusted 는 422 다. 외부 주체는 주체 의존성을 바꿔 넣는다."""
    async with safety_app(write_doc(fake_mcp.url)) as running:
        running.app.dependency_overrides[get_caller] = lambda: OUTSIDE
        for body in ({"run_id": RUN}, {"untrusted": True}):
            response = await running.client.post("/chat", json={"message": "hi", **body})
            assert status_and_code(response) == (422, "validation_error")
            assert [err["loc"][-1] for err in response.json()["errors"]] == list(body)
        decided = await running.client.post(
            "/sessions/s1/decisions", json={"decisions": [{"type": "approve"}], "run_id": RUN}
        )
        assert status_and_code(decided) == (422, "validation_error")


async def test_a_start_that_fails_before_its_turn_frees_the_run(fake_mcp) -> None:
    """턴이 열리기 전에 실패한 시작은 만든 세션을 지워 run_id 를 푼다. 그래야 플랫폼이 다시 한
    시작이 턴 없는 세션의 `run_exists` 를 받지 않는다(P41 5절). 위임의 사람이 어긋난 시작이 그런
    실패다."""
    async with safety_app(write_doc(fake_mcp.url)) as running:
        bob = {"id": "g-1", "secret": "s", "sub": "bob", "expires_in": 60, "renew_url": "http://x"}
        refused = await running.client.post(
            "/chat",
            json={"message": ADD, "user_sub": "alice", "run_id": RUN, "turn_grant": bob},
        )
        assert status_and_code(refused) == (409, "turn_grant_mismatch")
        assert await running.runtime.store.index.find_run(RUN) is None
        response, sse = await chat(running, ADD, run_id=RUN)
        assert response.status_code == 200 and sse[-1][0] == "done"


async def test_the_external_path_reads_a_run_session_but_does_not_change_it(fake_mcp) -> None:
    """외부 경로의 소유자는 실행 세션을 읽기만 한다. 중지, 이름 바꾸기, 지우기는 결정처럼 403
    이다(runs.py `refuse_external_run`)."""
    async with safety_app(write_doc(fake_mcp.url)) as running:
        response, _ = await chat(running, ADD, run_id=RUN, untrusted=True)
        sid = response.headers["X-Daegam-Session-Id"]
        running.app.dependency_overrides[get_caller] = lambda: OUTSIDE
        assert (await running.client.get(f"/sessions/{sid}")).status_code == 200
        for method, path, body in (
            ("POST", f"/sessions/{sid}/cancel", {}),
            ("PATCH", f"/sessions/{sid}", {"title": "새 이름"}),
            ("DELETE", f"/sessions/{sid}", None),
            ("POST", f"/sessions/{sid}/decisions", {"decisions": [{"type": "approve"}]}),
        ):
            refused = await running.client.request(method, path, json=body)
            assert status_and_code(refused) == (403, "forbidden"), (method, path)
        assert (await record_of(running, sid)).status == STATUS_PENDING


async def test_a_run_id_outside_its_pattern_is_refused(fake_mcp) -> None:
    async with safety_app(write_doc(fake_mcp.url)) as running:
        for run_id in ("", "a b", "x\r\ny", "가" * 3, "r" * 65):
            response, _ = await chat(running, "hi", run_id=run_id)
            assert status_and_code(response) == (422, "validation_error"), run_id


def test_a_session_closed_or_decided_under_the_lease_takes_no_turn() -> None:
    now = utcnow()
    state = SimpleNamespace(config=SimpleNamespace(entry={"session_ttl_seconds": 3600}))
    for status, resumed, expected in (
        (STATUS_CLOSED, False, (410, "session_closed")),
        (STATUS_CLOSED, True, (410, "session_closed")),
        ("active", True, (409, "not_pending")),
    ):
        with pytest.raises(HTTPException) as e:
            check_still_open(
                state, SessionRecord("s1", None, "alice", now, now, status=status), resume=resumed
            )
        assert (e.value.status_code, e.value.detail["code"]) == expected
    check_still_open(state, SessionRecord("s1", None, "alice", now, now), resume=False)
    check_still_open(
        state, SessionRecord("s1", None, "alice", now, now, status=STATUS_PENDING), resume=True
    )


async def test_runs_skip_the_minute_rate(fake_mcp) -> None:
    doc = write_doc(fake_mcp.url)
    doc["entry"]["rate_limit"] = {"per_user_per_minute": 1}
    async with safety_app(doc) as running:
        for run_id in ("run-a", "run-b"):
            response, _ = await chat(running, "hi", run_id=run_id)
            assert response.status_code == 200, response.text
        first, _ = await chat(running, "hi")
        assert first.status_code == 200
        second, _ = await chat(running, "hi")
        assert status_and_code(second) == (429, "rate_limited")


async def test_the_session_list_leaves_run_sessions_out_unless_asked(fake_mcp) -> None:
    async with safety_app(write_doc(fake_mcp.url)) as running:
        _, sse = await chat(running, "hi")
        chat_sid = sid_of(sse, "done")
        _, sse = await chat(running, "hi", run_id=RUN)
        run_sid = sid_of(sse, "done")

        async def listed(**params: str) -> list[tuple[str, str | None]]:
            body = (await running.client.get("/sessions", params=params)).json()
            return sorted((row["session_id"], row["run_id"]) for row in body["sessions"])

        assert await listed() == [(chat_sid, None)]
        assert await listed(runs="include") == sorted([(chat_sid, None), (run_sid, RUN)])
        assert await listed(runs="only") == [(run_sid, RUN)]
        bad = await running.client.get("/sessions", params={"runs": "all"})
        assert status_and_code(bad) == (422, "validation_error")


async def test_a_run_session_drops_remembered_approves_and_keeps_rejects(fake_mcp) -> None:
    """실행은 결정을 하지 않으므로 실행 세션에서 기억한 승인은 쓰지 않고 거절은 쓴다(P43 5절)."""
    async with safety_app(write_doc(fake_mcp.url, approval=RULE)) as running:
        _, sse = await chat(running, ADD, approval_choices=choose("Add", "approve"))
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1

        _, sse = await chat(running, ADD, run_id="run-a", approval_choices=choose("Add", "approve"))
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "rule" and fake_mcp.calls["Add"] == 1

        _, sse = await chat(running, ADD, run_id="run-b", approval_choices=choose("Add", "reject"))
        [result] = results_of(sse, "Add")
        assert result["status"] == "error" and fake_mcp.calls["Add"] == 1
        assert sse[-1][0] == "done"


async def test_an_error_turn_of_a_run_carries_its_code_and_closes_the_session(fake_mcp) -> None:
    doc = write_doc(fake_mcp.url)
    doc["main"]["limits"]["retries"] = 0
    async with safety_app(doc, model=FailOnceModel(model_name="stub")) as running:
        response, sse = await chat(running, f"{FAIL_MARK} 해라", run_id=RUN)
        assert response.status_code == 200
        assert sse[-1][0] == "error"
        sid = response.headers["X-Daegam-Session-Id"]
        assert (await record_of(running, sid)).status == STATUS_CLOSED
        [end] = running.observed("turn_end")
        assert end["run_id"] == RUN and end["code"] == sse[-1][1]["code"]
        assert end["status"] == end["code"]
        assert events_named(sse, "error")[0]["session_id"] == sid
