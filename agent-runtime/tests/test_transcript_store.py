"""화면용 전사를 저장하고 다시 읽는다(P18 F1).

저장소 셋이 같은 계약인지, 그리고 세션을 다시 읽었을 때 스트림으로 받은 것과 같은 순서·같은 원문이
나오는지를 본다. 접는 규칙은 test_transcript.py 가 본다.

저장소 셋의 계약은 store_fixtures.py 의 assert_transcripts_round_trip 이고 여기서 메모리·Postgres·
Redis 에 함께 건다. 실제 Postgres·Redis 는 `DAEGAM_TEST_PG_DSN`·`DAEGAM_TEST_REDIS_URL` 이 없으면
건너뛴다.
"""

from __future__ import annotations

from typing import Any

import pytest

from runtime.sessions import open_postgres_store
from runtime.stub import FAIL_FIRST_ENV, NO_TOOL_PREFIX
from runtime.transcript import TRANSCRIPT_LIMIT, MemoryTranscripts, resolve_interrupts
from tests.app_fixtures import deny_rule, folded, guardrail, resume, sid_of, transcript_items
from tests.conftest import chat, event_names, events_named, needs_pg, run_app
from tests.store_fixtures import (
    assert_transcripts_round_trip,
    contract_store,
    pg_schema,
    real_store,
)

# ---- 저장소 ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_transcripts_round_trip_and_limit(kind: str) -> None:
    """세 저장소가 같은 계약이다. Postgres 는 넣는 SQL 이 문장 하나라 자리표 개수가 항목 수에 따라
    달라지지 않고, WITH ORDINALITY 가 넣는 차례를 못 박는다. 상한 정리(TRIM_SQL)는 그 세션의 옛
    행만 지운다."""
    async with contract_store(kind) as store:
        await assert_transcripts_round_trip(store.transcripts)


@needs_pg
async def test_real_postgres_index_setup_also_creates_the_transcript_table() -> None:
    """전사 표는 세션 색인의 setup 이 세션·카운터 표와 같이 만든다. 빈 스키마에서 연다."""
    async with pg_schema() as schema:
        store = await open_postgres_store(schema.dsn, instance_id="inst-1")
        await store.close()
        tables = set(await schema.tables())
    assert {"daegam_sessions", "daegam_counters", "daegam_transcripts"} <= tables


# ---- 세션 상세 ---------------------------------------------------------------------------


async def test_detail_keeps_the_message_summary_and_adds_the_transcript(support_doc) -> None:
    """요약 계약은 그대로 두고 전사를 더한다. 세션 목록·상세 표가 요약을 쓰고 있다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, '더해 줘 {"tool": "Add", "args": {"a": 3, "b": 4}}')
        sid = sid_of(sse)
        detail = (await running.client.get(f"/sessions/{sid}")).json()

        assert [m["role"] for m in detail["messages"]] == ["human", "ai", "tool", "ai"]
        assert detail["transcript"]["limit"] == TRANSCRIPT_LIMIT
        items = detail["transcript"]["items"]
        assert [i["kind"] for i in items] == ["user", "tool_call", "assistant", "done"]
        assert items[0]["text"] == '더해 줘 {"tool": "Add", "args": {"a": 3, "b": 4}}'
        assert items[1]["call"]["args"] == {"a": 3, "b": 4}
        assert items[1]["result"]["content"] == "7"
        assert items[2] == {"kind": "assistant", "text": "결과: 7", "streaming": False}
        assert items[3]["turn"] == 1 and items[3]["tool_calls"] == 1

        # 다음 턴은 같은 전사에 이어 붙는다
        await chat(running, "다시", session_id=sid)
        items = await transcript_items(running, sid)
        assert [i["kind"] for i in items].count("user") == 2
        assert items[4]["text"] == "다시"


async def test_long_answer_is_not_cut_while_the_summary_still_is(support_doc) -> None:
    """전사의 본문은 원문 그대로다. 200자 요약과 나란히 두고 본다."""
    support_doc["main"]["tools"] = []
    message = "가" * 400
    async with run_app(support_doc) as running:
        _, sse = await chat(running, message)
        done = events_named(sse, "done")[0]
        answer = NO_TOOL_PREFIX + message
        assert done["text"] == answer and len(answer) > 400

        detail = (await running.client.get(f"/sessions/{sid_of(sse)}")).json()
        assert len(detail["messages"][-1]["text"]) == 200, "요약은 예전처럼 200자다"
        items = detail["transcript"]["items"]
        assert items[0]["text"] == message
        assert items[1]["text"] == answer


async def test_hitl_conversation_reads_back_the_same_as_the_stream(support_doc) -> None:
    """가장 중요한 자리다. 도구 호출과 승인이 낀 대화를 한 세션에서 만들고 다시 읽었을 때
    스트림으로 받은 것과 같은 순서·같은 원문이 나와야 한다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "reject"]}
    async with run_app(support_doc) as running:
        first = '{"tool": "Add", "args": {"a": 10, "b": 20}}'
        _, sse1 = await chat(running, first)
        assert sse1[-1][0] == "interrupt"
        sid = sid_of(sse1)

        # 승인 전: 카드가 열려 있다
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["pending"] is True
        assert detail["transcript"]["items"] == folded((first, sse1))
        assert detail["transcript"]["items"][-1]["resolved"] is False

        _, sse2 = await resume(running, sid, [{"type": "approve"}])
        assert event_names(sse2)[-1] == "done"

        # 승인 뒤: 한 대화로 이어지고 카드는 닫혔다
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["pending"] is False
        expected = folded((first, sse1), (None, sse2))
        resolve_interrupts(expected, pending=False)
        assert detail["transcript"]["items"] == expected

        kinds = [i["kind"] for i in expected]
        assert kinds == ["user", "tool_call", "interrupt", "assistant", "done"]
        assert expected[0]["text"] == first
        # 승인을 기다리던 카드와 승인 뒤 실제로 돈 카드는 하나다. 스트림으로 본 화면과 같다
        assert expected[1]["call"]["args"] == {"a": 10, "b": 20}
        assert expected[1]["result"]["content"] == "30"
        assert expected[3]["text"] == "결과: 30"

        # 세 번째 턴(거절)까지 이어 붙는다
        third = '{"tool": "Add", "args": {"a": 1, "b": 1}}'
        _, sse3 = await chat(running, third, session_id=sid)
        _, sse4 = await resume(running, sid, [{"type": "reject", "message": "지금은 아니다"}])
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        expected = folded((first, sse1), (None, sse2), (third, sse3), (None, sse4))
        resolve_interrupts(expected, pending=detail["pending"])
        assert detail["transcript"]["items"] == expected
        assert [i["kind"] for i in expected][5:] == [
            "user",
            "tool_call",
            "interrupt",
            "assistant",
            "done",
        ]
        assert "지금은 아니다" in expected[6]["result"]["content"]


async def test_failed_turn_leaves_an_error_item(support_doc, monkeypatch) -> None:
    """턴이 실패해도 전사가 깨지지 않는다. 사용자 말풍선과 오류가 남는다."""
    monkeypatch.setenv(FAIL_FIRST_ENV, "3")
    support_doc["main"]["limits"]["retries"] = 0
    support_doc["main"]["fallback"] = None
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "실패할 턴")
        assert event_names(sse) == ["error"]
        sid = sid_of(sse)
        items = await transcript_items(running, sid)
        assert items == folded(("실패할 턴", sse))
        assert [i["kind"] for i in items] == ["user", "error"]
        assert items[1]["code"] == "turn_failed"
        # 전사 항목도 그 시도의 turn_id 를 든다(P36)
        error = sse[0][1]
        assert items[1]["turn_id"] == error["turn_id"] and len(error["turn_id"]) == 32


async def test_flagged_guardrail_is_part_of_the_transcript(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [guardrail("input", [deny_rule("비밀")], "flag")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "비밀 이야기")
        items = await transcript_items(running, sid_of(sse))
        assert items == folded(("비밀 이야기", sse))
        assert [i["kind"] for i in items] == ["user", "guardrail", "assistant", "done"]
        assert items[1]["rule"] == "deny_pattern" and items[1]["action"] == "flag"


@pytest.mark.parametrize("kind", ["postgres", "redis"])
async def test_real_store_keeps_the_transcript_through_an_approval(support_doc, kind) -> None:
    """체크포인터가 Postgres 든 Redis 든 전사가 같이 돈다(D33).

    전사는 세션 색인이 사는 곳에 함께 살아서 승인으로 끊긴 턴과 재개된 턴이 한 대화로 이어진다.
    """
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with (
        real_store(kind, support_doc) as store,
        run_app(support_doc, store=store) as running,
    ):
        message = '{"tool": "Add", "args": {"a": 7, "b": 8}}'
        response, sse1 = await chat(running, message)
        assert response.status_code == 200, response.text
        sid = sid_of(sse1)
        assert (await running.client.get(f"/sessions/{sid}")).json()["pending"] is True

        _, sse2 = await resume(running, sid, [{"type": "approve"}])
        expected = folded((message, sse1), (None, sse2))
        resolve_interrupts(expected, pending=False)
        assert await transcript_items(running, sid) == expected
        assert [i["kind"] for i in expected] == [
            "user",
            "tool_call",
            "interrupt",
            "assistant",
            "done",
        ]
        assert expected[-2]["text"] == "결과: 15"
        # 세션이 닫혀도 기록은 남는다(D46). 보존 기간이 지나야 색인 행과 함께 사라진다
        assert await store.sweep(0) == [sid]
        # 저장된 것은 덧붙이기 그대로라 재개 턴의 카드가 따로 남아 있다. 합치기와 resolved 는
        # 읽을 때다
        assert [i["kind"] for i in await store.transcripts.read(sid)] == [
            "user",
            "tool_call",
            "interrupt",
            "tool_call",
            "assistant",
            "done",
        ]
        assert await store.sweep(0, retention_days=0) == []
        assert await store.transcripts.read(sid) == []
        assert await store.index.get(sid) is None


async def test_ephemeral_turn_leaves_a_transcript_until_retention_passes(support_doc) -> None:
    """임시 턴도 그 세션 안에서는 보여야 한다. 장기 기억에만 안 남을 뿐 전사는 다른 턴과 같아서
    세션이 닫혀도 남고 보존 기간이 지나야 사라진다(D46)."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "임시로 묻는다", ephemeral=True)
        sid = sid_of(sse)
        items = await transcript_items(running, sid)
        assert items == folded(("임시로 묻는다", sse))
        assert [i["kind"] for i in items] == ["user", "assistant", "done"]

        store = running.runtime.store
        assert await store.sweep(0) == [sid]
        assert await store.transcripts.read(sid) == items
        assert await store.sweep(0, retention_days=0) == []
        assert await store.transcripts.read(sid) == []


@pytest.mark.parametrize("kind", ["chat", "decisions"])
async def test_transcript_failure_does_not_break_the_turn(support_doc, kind) -> None:
    """전사 저장이 실패해도 턴 결과는 그대로 나간다. 화면 기록 때문에 끝난 턴을 뒤엎지 않는다."""

    class _Broken(MemoryTranscripts):
        async def append(self, session_id: str, items: Any) -> None:
            raise RuntimeError("저장소가 흔들렸다")

    support_doc["tools"][0]["approval"] = {"Add": ["approve"]} if kind == "decisions" else {}
    async with run_app(support_doc) as running:
        running.runtime.store.transcripts = _Broken()
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        sid = sid_of(sse)
        if kind == "decisions":
            _, sse = await resume(running, sid, [{"type": "approve"}])
        assert event_names(sse)[-1] == "done"
        assert await transcript_items(running, sid) == []
