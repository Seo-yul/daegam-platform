"""다시 생성과 고쳐 보내기(P36 5절, runtime/regenerate.py)다.

같은 턴 번호와 새 turn_id, 하나로 접힌 전사, 다음 /chat 이 새 갈래에서 이어지는 것, 고쳐 보내기,
첫 턴의 스레드 지우기, 오류·중지로 끝난 턴 뒤 turns 가 하나 늘어나는 것, 서브에이전트를 부른 턴
뒤에도 앞 턴이 남는 것, 새 갈래의 승인 대기와 재개가 같은 turn_id 인 것, 리스를 잡은 뒤에 입력
체크포인트를 찾는 것, 갈래를 내기 전에 실패한 턴, 요청 검사의 거절과 횟수 상한, 재배포로 강화한
입력 검사가 그대로 다시 싣는 질문에도 적용되는 것을 본다. 서브에이전트 턴을 두 번 가르는 경우는
실제 Postgres·Redis 에서도 돈다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from runtime import fork, stream
from runtime import regenerate as regenerate_module
from runtime.hitl import APPROVER_HEADER
from runtime.sessions import SessionRecord, new_turn_token
from runtime.transcript import resolve_interrupts
from runtime.turn import SESSION_HEADER
from runtime.turn_record import TurnAttempt, turn_id_of
from tests.app_fixtures import (
    FAIL_MARK,
    FailOnceModel,
    bearer,
    checkpoint_humans,
    deny_rule,
    folded,
    guardrail,
    regenerate,
    resume,
    running_sid,
    sid_of,
    sleeping_turn,
    tool_message,
    transcript_items,
    with_input_deny,
    with_subagent,
)
from tests.conftest import RunningApp, chat, event_names, events_named, parse_sse, run_app
from tests.idp_fixtures import FakeIdp, Signer, external_app
from tests.store_fixtures import pod_store, store_for


def _add(a: int, b: int) -> str:
    return tool_message("Add", a=a, b=b)


async def _human_texts(running: RunningApp, sid: str) -> list[str]:
    return [m.text for m in await checkpoint_humans(running, sid)]


def _refused(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


async def test_regenerate_keeps_the_turn_number_and_folds_the_transcript(support_doc) -> None:
    """같은 턴 번호에 새 turn_id 다. 전사는 다시 읽으면 바꾼 턴 하나로 접혀 있고 체크포인트에도
    질문이 한 번만 있다. turn_start 는 regenerated 를, turn_end 는 버린 턴의 id 를 싣는다."""
    first, second = f"첫 {_add(1, 2)}", f"둘째 {_add(2, 3)}"
    async with run_app(support_doc) as running:
        _, sse1 = await chat(running, first)
        sid = sid_of(sse1)
        _, sse2 = await chat(running, second, session_id=sid)
        old = events_named(sse2, "done")[0]["turn_id"]

        response, again = await regenerate(running, sid)
        assert response.status_code == 200, response.text
        assert response.headers[SESSION_HEADER] == sid
        done = events_named(again, "done")[0]
        assert done["turn"] == 2 and done["turn_id"] != old
        assert events_named(again, "tool_result")[0]["content"] == "5"
        assert not events_named(again, "greeting")

        assert await transcript_items(running, sid) == folded((first, sse1), (second, again))
        assert await _human_texts(running, sid) == [first, second]
        record = await running.runtime.store.index.get(sid)
        assert (record.turns, record.last_turn, record.turn_id) == (2, 2, done["turn_id"])
        start = running.observed("turn_start")[-1]
        assert (start["turn"], start["turn_id"], start["regenerated"]) == (2, done["turn_id"], True)
        assert "edited" not in start
        assert running.observed("turn_end")[-1]["replaced_turn_id"] == old


async def test_edit_replaces_the_question_and_the_next_chat_continues_on_it(support_doc) -> None:
    """고쳐 보내기는 마지막 질문의 글만 바꾼다. 다음 /chat 은 새 갈래에서 이어진다."""
    first, edited, third = f"첫 {_add(1, 2)}", f"고친 둘째 {_add(5, 5)}", f"셋째 {_add(1, 1)}"
    async with run_app(support_doc) as running:
        _, sse1 = await chat(running, first)
        sid = sid_of(sse1)
        await chat(running, f"둘째 {_add(2, 3)}", session_id=sid)

        response, again = await regenerate(running, sid, message=edited)
        assert response.status_code == 200, response.text
        assert events_named(again, "tool_result")[0]["content"] == "10"
        turn_id = events_named(again, "done")[0]["turn_id"]
        assert running.observed("turn_start")[-1]["edited"] is True

        _, sse3 = await chat(running, third, session_id=sid)
        assert events_named(sse3, "done")[0]["turn"] == 3
        humans = await checkpoint_humans(running, sid)
        assert [m.text for m in humans] == [first, edited, third]
        assert humans[1].id == f"u-{turn_id}"
        assert await transcript_items(running, sid) == folded(
            (first, sse1), (edited, again), (third, sse3)
        )


async def test_first_turn_starts_over_and_keeps_one_greeting(support_doc) -> None:
    """입력 체크포인트에 부모가 없는 첫 턴은 스레드를 지우고 새로 돈다. 인사말은 다시 내지 않고
    전사에 하나만 남는다."""
    support_doc["entry"]["greeting"] = "어서 오세요"
    first, edited = f"하나 {_add(1, 2)}", f"고친 하나 {_add(2, 2)}"
    async with run_app(support_doc) as running:
        _, sse1 = await chat(running, first)
        sid = sid_of(sse1)
        old = events_named(sse1, "done")[0]["turn_id"]

        response, again = await regenerate(running, sid, message=edited)
        assert response.status_code == 200, response.text
        assert again[0][0] != "greeting" and events_named(again, "done")[0]["turn"] == 1
        assert await _human_texts(running, sid) == [edited]
        items = await transcript_items(running, sid)
        assert [i["kind"] for i in items][:2] == ["assistant", "user"]
        assert items[0]["greeting"] is True and items[1]["text"] == edited
        assert sum(1 for i in items if i.get("greeting")) == 1
        assert running.observed("turn_end")[-1]["replaced_turn_id"] == old


async def test_a_failed_turn_is_regenerated_and_counted(support_doc) -> None:
    """오류로 끝난 턴은 턴 수에 들지 않았으므로 다시 생성하면 turns 가 하나 늘어난다. 번호는 실패한
    시도의 것이다."""
    support_doc["main"]["limits"]["retries"] = 0
    first, failing = f"첫 {_add(1, 2)}", f"{FAIL_MARK} {_add(3, 4)}"
    async with run_app(support_doc, model=FailOnceModel(model_name="stub")) as running:
        _, sse1 = await chat(running, first)
        sid = sid_of(sse1)
        _, failed = await chat(running, failing, session_id=sid)
        assert failed[-1][0] == "error"
        record = await running.runtime.store.index.get(sid)
        assert (record.turns, record.last_turn) == (1, 2)

        response, again = await regenerate(running, sid)
        assert response.status_code == 200, response.text
        assert events_named(again, "done")[0]["turn"] == 2
        assert (await running.runtime.store.index.get(sid)).turns == 2
        assert await _human_texts(running, sid) == [first, failing]
        assert await transcript_items(running, sid) == folded((first, sse1), (failing, again))
        assert running.observed("turn_end")[-1]["replaced_turn_id"] == failed[-1][1]["turn_id"]


async def test_a_stopped_first_turn_is_sent_again(support_doc) -> None:
    """중지로 끝난 첫 턴도 마지막 턴 시도다. 고쳐 보내면 그 턴이 1번으로 끝난다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        cancel = await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        assert cancel.status_code == 202
        assert parse_sse((await task).text)[-1][1]["code"] == "turn_cancelled"
        assert (await running.runtime.store.index.get(sid)).turns == 0

        response, again = await regenerate(running, sid, message="다시 물어요")
        assert response.status_code == 200, response.text
        assert events_named(again, "done")[0]["turn"] == 1
        assert (await running.runtime.store.index.get(sid)).turns == 1
        assert await _human_texts(running, sid) == ["다시 물어요"]


async def test_a_resumed_branch_keeps_its_turn_id(support_doc) -> None:
    """새 갈래가 승인 대기로 끝나면 재개가 같은 turn_id 를 이어 쓴다. 대기 중에는 다시 생성할 수
    없다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    question = _add(1, 2)
    async with run_app(support_doc) as running:
        _, sse1 = await chat(running, question)
        sid = sid_of(sse1)
        _refused((await regenerate(running, sid))[0], 409, "session_pending")
        _, approved = await resume(running, sid, [{"type": "approve"}])
        assert events_named(approved, "done")

        response, again = await regenerate(running, sid)
        assert response.status_code == 200 and again[-1][0] == "interrupt"
        record = await running.runtime.store.index.get(sid)
        assert record.status == "pending"
        _, resumed = await resume(running, sid, [{"type": "approve"}])
        done = events_named(resumed, "done")[0]
        assert (done["turn"], done["turn_id"]) == (1, record.turn_id)
        expected = folded((question, again), (None, resumed))
        resolve_interrupts(expected, pending=False)
        assert await transcript_items(running, sid) == expected


async def test_the_input_checkpoint_is_found_after_the_lease(support_doc, monkeypatch) -> None:
    """도는 턴이 리스를 쥐고 있으면 입력 체크포인트를 찾지 않고 409 다. 찾을 때는 이 요청이 리스를
    쥐고 있다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, _add(1, 2))
        sid = sid_of(sse)
        index = running.runtime.store.index
        found: list[bool] = []
        real = regenerate_module.find_input_checkpoint

        async def spy(saver: Any, head: Any) -> Any:
            # 리스가 이미 잡혀 있으면 다른 토큰으로는 잡지 못한다
            found.append(await index.claim_turn(sid, lease_seconds=1, token=new_turn_token()))
            return await real(saver, head)

        monkeypatch.setattr(regenerate_module, "find_input_checkpoint", spy)
        token = new_turn_token()
        assert await index.claim_turn(sid, lease_seconds=60, token=token)
        _refused((await regenerate(running, sid))[0], 409, "turn_in_progress")
        assert found == []
        await index.release_turn(sid, token)

        response, _ = await regenerate(running, sid)
        assert response.status_code == 200 and found == [False]


async def test_a_session_whose_ttl_passes_before_the_lease_is_not_regenerated(
    support_doc, monkeypatch
) -> None:
    """입구에서는 TTL 안이던 세션이 리스를 잡는 사이에 TTL 을 넘기면 리스 안의 다시 읽기가 410
    session_closed 로 멈춘다(P44 11절). 리스는 풀리고 체크포인트와 전사는 그대로다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, _add(1, 2))
        sid = sid_of(sse)
        index = running.runtime.store.index
        before = await checkpoint_humans(running, sid)
        items = await transcript_items(running, sid)
        claim = index.claim_turn
        ttl = running.config.entry["session_ttl_seconds"]

        async def claim_after_the_ttl(session_id: str, **kwargs: Any) -> bool:
            claimed = await claim(session_id, **kwargs)
            old = datetime.now(UTC) - timedelta(seconds=ttl + 1)
            await index.update(session_id, last_turn_at=old)
            return claimed

        monkeypatch.setattr(index, "claim_turn", claim_after_the_ttl)
        _refused((await regenerate(running, sid))[0], 410, "session_closed")
        record = await index.get(sid)
        assert record is not None and record.running is False
        assert await checkpoint_humans(running, sid) == before
        assert await transcript_items(running, sid) == items


async def test_a_turn_that_fails_before_branching_leaves_the_old_turn(
    support_doc, monkeypatch
) -> None:
    """갈래를 내기 전에 멈춘 다시 생성은 앞 턴이 그대로다. 전사에는 표식 없이 오류 항목만 붙고
    turn_end 에 replaced_turn_id 가 없다."""
    first, second = f"첫 {_add(1, 2)}", f"둘째 {_add(2, 3)}"

    async def no_start(agent: Any, *, state: stream.TurnState, **_: Any):
        state.error = (stream.TURN_FAILED, "갈래를 내기 전에 멈췄다")
        return
        yield  # 생성기로 만든다

    async with run_app(support_doc) as running:
        _, sse1 = await chat(running, first)
        sid = sid_of(sse1)
        _, sse2 = await chat(running, second, session_id=sid)
        monkeypatch.setattr(stream, "stream_turn", no_start)
        response, again = await regenerate(running, sid)
        assert event_names(again) == ["error"]
        error = {"kind": "error", "code": "turn_failed", "message": "갈래를 내기 전에 멈췄다"}
        expected = [
            *folded((first, sse1), (second, sse2)),
            {**error, "turn_id": again[0][1]["turn_id"], "trace_id": again[0][1]["trace_id"]},
        ]
        assert await transcript_items(running, sid) == expected
        assert "replaced_turn_id" not in running.observed("turn_end")[-1]
        assert await _human_texts(running, sid) == [first, second]


async def test_request_refusals(support_doc) -> None:
    """다시 생성은 /chat 과 같은 검사를 지난다. 남의 세션은 승인자 헤더가 있어도 403 이고, 고쳐
    보낸 글은 길이와 입력 가드레일을 지나며, 닫힌 세션은 410, 턴이 없는 세션은 409 다."""
    deny = [deny_rule("금지어")]
    support_doc["guardrails"] = [guardrail("input", deny, "block", "보낼 수 없는 말이다")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, _add(1, 2))
        sid = sid_of(sse)
        approver = {APPROVER_HEADER: "1"}
        _refused(
            (await regenerate(running, sid, user_sub="bob", headers=approver))[0],
            403,
            "not_session_owner",
        )
        _refused((await regenerate(running, "missing"))[0], 404, "session_not_found")
        too_long = "가" * (support_doc["entry"]["max_input_chars"] + 1)
        _refused((await regenerate(running, sid, message=too_long))[0], 413, "max_input_chars")
        _refused((await regenerate(running, sid, message="금지어"))[0], 422, "guardrail_blocked")
        assert (await regenerate(running, sid, message=""))[0].status_code == 422

        now = datetime.now(UTC)
        empty = SessionRecord("empty", None, "alice", now, now, instance_id="ai-support-0001")
        await running.runtime.store.index.create(empty)
        _refused((await regenerate(running, "empty"))[0], 409, "nothing_to_regenerate")
        assert await running.runtime.store.index.get("empty") is not None

        assert sid in await running.runtime.store.sweep(0)
        _refused((await regenerate(running, sid))[0], 410, "session_closed")


async def test_a_turn_is_regenerated_up_to_the_daily_limit(support_doc, monkeypatch) -> None:
    """한 턴은 정한 횟수까지만 다시 생성한다. 상한을 넘긴 요청은 입력 체크포인트를 찾지 않고, 다음
    턴은 새로 센다."""
    monkeypatch.setattr(regenerate_module, "MAX_REGENERATIONS_PER_TURN", 1)
    async with run_app(support_doc) as running:
        _, sse = await chat(running, _add(1, 2))
        sid = sid_of(sse)
        assert (await regenerate(running, sid))[0].status_code == 200
        walked: list[Any] = []
        real = regenerate_module.find_input_checkpoint

        async def spy(saver: Any, head: Any) -> Any:
            walked.append(head)
            return await real(saver, head)

        monkeypatch.setattr(regenerate_module, "find_input_checkpoint", spy)
        _refused((await regenerate(running, sid))[0], 429, "max_regenerations")
        # 사슬 걷기와 상태 읽기는 세션 길이만큼 든다
        assert walked == []
        await chat(running, _add(2, 2), session_id=sid)
        assert (await regenerate(running, sid))[0].status_code == 200
        assert len(walked) == 1


def _limit_rate(doc: dict[str, Any]) -> None:
    doc["entry"]["rate_limit"] = {"per_user_per_minute": 1}


def _limit_session_tokens(doc: dict[str, Any]) -> None:
    doc["service"]["budget"]["session_tokens"] = 1


@pytest.mark.parametrize(
    ("limit", "code"),
    [(_limit_rate, "rate_limited"), (_limit_session_tokens, "session_budget_exceeded")],
)
async def test_new_turn_limits_apply(support_doc, limit, code) -> None:
    """분당 한도와 세션 예산을 새 턴과 같이 지난다. 앞선 /chat 이 한도를 다 쓴다."""
    limit(support_doc)
    async with run_app(support_doc) as running:
        _, sse = await chat(running, _add(1, 2))
        _refused((await regenerate(running, sid_of(sse)))[0], 429, code)


async def test_a_redeployed_input_guard_checks_the_question_sent_again(support_doc) -> None:
    """질문을 그대로 다시 생성해도 입력 체크포인트의 질문이 지금 배포의 입력 검사를 지난다.
    재배포로 강화한 규칙이 그 전에 받은 질문에도 적용되어 block 은 막고, flag 위반은 다시 돌 때마다
    앞머리와 관측 이벤트에 남는다."""
    question = f"비밀 {_add(1, 2)}"
    async with pod_store(support_doc) as store:
        async with run_app(support_doc, store=store) as before:
            _, sse = await chat(before, question)
            sid = sid_of(sse)
        async with run_app(with_input_deny(support_doc, "비밀", "block"), store=store) as blocked:
            _refused((await regenerate(blocked, sid))[0], 422, "guardrail_blocked")
        async with run_app(with_input_deny(support_doc, "비밀", "flag"), store=store) as flagged:
            response, again = await regenerate(flagged, sid)
            assert response.status_code == 200, response.text
            assert again[0] == (
                "guardrail",
                {"position": "input", "rule": "deny_pattern", "action": "flag"},
            )
            assert flagged.observed("guardrail")[0]["action"] == "flag"
            assert events_named(again, "done")[0]["turn"] == 1


async def test_external_callers_regenerate_only_their_own_sessions(support_doc) -> None:
    """외부 경로의 주체는 토큰 sub 다. 본문 user_sub 로 남의 세션을 고르지 못한다."""
    signer = Signer()
    async with external_app(support_doc, FakeIdp(signer)) as running:
        alice = bearer(signer.token(sub="alice"))
        bob = bearer(signer.token(sub="bob"))
        _, sse = await chat(running, _add(1, 2), headers=alice)
        sid = sid_of(sse)
        _refused((await regenerate(running, sid, headers=bob))[0], 403, "not_session_owner")
        response, again = await regenerate(running, sid, user_sub="bob", headers=alice)
        assert response.status_code == 200 and events_named(again, "done")


async def test_the_question_comes_from_the_input_channel_not_the_state(support_doc) -> None:
    """입력 체크포인트 E 는 입력이 입력 채널에 들어간 직후다. E 의 상태에서 읽으면 앞 턴 질문이
    나오므로 질문은 입력 채널에서 읽는다(F0 기록)."""
    first, second = f"첫 {_add(1, 2)}", f"둘째 {_add(2, 3)}"
    async with run_app(support_doc) as running:
        _, sse = await chat(running, first)
        sid = sid_of(sse)
        await chat(running, second, session_id=sid)
        saver = running.runtime.store.checkpointer
        found = await regenerate_module.find_input_checkpoint(
            saver, await fork.root_head(saver, sid)
        )
        assert regenerate_module.input_question(found).text == second
        snapshot = await running.runtime.agent.graph.aget_state(found.config)
        at_entry = [m for m in stream.snapshot_messages(snapshot) if isinstance(m, HumanMessage)]
        assert at_entry[-1].text == first


def test_resend_message_carries_the_attachment_references() -> None:
    """다시 싣는 메시지는 글만 바꾸고 첨부 참조(`additional_kwargs.daegam`)를 그대로 옮긴다. id 는
    새 턴의 것이다."""
    refs = {"daegam": {"attachments": [{"upload_id": "upl_1"}]}}
    question = HumanMessage(content="이 문서 요약", id="u-old", additional_kwargs=refs)
    edited = regenerate_module.resend_message(question, "다시 요약", "new")
    assert (edited.id, edited.text, edited.additional_kwargs) == ("u-new", "다시 요약", refs)
    assert edited.additional_kwargs is not question.additional_kwargs
    same = regenerate_module.resend_message(question, None, "n2")
    assert (same.id, same.content) == ("u-n2", "이 문서 요약")


def test_regenerated_attempt_number_and_replaced_turn_id() -> None:
    """번호는 가장 최근 시도의 것이다."""
    now = datetime.now(UTC)
    record = SessionRecord("s1", None, "alice", now, now, turns=2, last_turn=3, turn_id="t3")
    attempt = TurnAttempt.regenerated(record)
    assert attempt.number == 3 and attempt.turn_id != "t3"
    assert (turn_id_of("u-abc"), turn_id_of("msg-1"), turn_id_of(None)) == ("abc", None, None)


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_regenerating_a_subagent_turn_keeps_the_earlier_turns(support_doc, kind) -> None:
    """서브에이전트는 부모 없는 input 체크포인트를 `tools:<id>` 에 남긴다. 그 턴을 두 번 다시
    만들어도 앞 턴은 지워지지 않고, 다시 읽은 대화와 전사가 마지막 갈래와 같다."""
    with_subagent(support_doc)
    first = f"앞 턴 {_add(1, 1)}"
    task = tool_message("task", description=_add(1, 2), subagent_type="calc")
    edited = tool_message("task", description=_add(3, 4), subagent_type="calc")
    async with store_for(kind, support_doc) as store, run_app(support_doc, store=store) as running:
        _, sse1 = await chat(running, first)
        sid = sid_of(sse1)
        try:
            _, sse2 = await chat(running, task, session_id=sid)
            assert any(c.get("agent") for c in events_named(sse2, "tool_call"))

            response, again = await regenerate(running, sid)
            assert response.status_code == 200, response.text
            assert events_named(again, "done")[0]["turn"] == 2
            assert await _human_texts(running, sid) == [first, task]

            response, twice = await regenerate(running, sid, message=edited)
            assert response.status_code == 200, response.text
            assert await _human_texts(running, sid) == [first, edited]
            assert await transcript_items(running, sid) == folded((first, sse1), (edited, twice))
            assert (await running.runtime.store.index.get(sid)).turns == 2
        finally:
            await running.runtime.store.delete_session(sid)
