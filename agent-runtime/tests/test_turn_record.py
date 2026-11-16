"""턴 기록과 제목·검색이다(P36 3절, runtime/turn_record.py). 자동 제목과 LIKE 패턴, 색인의 턴
기록 칸, 턴 시도마다 나는 turn_id 와 번호를 본다. 실제 Postgres 는 `DAEGAM_TEST_PG_DSN` 이 없으면
건너뛴다."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from runtime.sessions import MemorySessionIndex, open_postgres_store
from runtime.turn_record import human_message_id, like_pattern, title_from_message
from tests.app_fixtures import (
    FAIL_MARK,
    FailOnceModel,
    checkpoint_humans,
    cut_while_sending,
    lease_free,
    open_chat,
    resume,
    sid_of,
    tool_message,
)
from tests.conftest import chat, load_fixture, needs_pg, run_app, wait_for
from tests.store_fixtures import assert_title_search, pg_schema, session_record

TITLE_CASES = load_fixture("chat/titles.json")["cases"]


@pytest.mark.parametrize("case", TITLE_CASES, ids=range(len(TITLE_CASES)))
def test_auto_title_matches_the_shared_fixture(case: dict[str, str]) -> None:
    """자동 제목은 글이 있는 첫 줄에서 공백이 아닌 제어·서식 문자를 빼고 공백을 줄여 NFC 로 맞춘 뒤
    60자에서 자른다. 기대값은 UI 테스트와 같이 쓰는 표본에 손으로 적었다."""
    assert title_from_message(case["message"]) == case["title"]


def test_a_message_without_text_has_no_title() -> None:
    assert title_from_message(" \n\u200b\n") is None


def test_like_pattern_escapes_the_like_metacharacters() -> None:
    assert like_pattern("100%") == "%100\\%%"
    assert like_pattern("a_b") == "%a\\_b%"
    assert like_pattern("c:\\x") == "%c:\\\\x%"
    assert like_pattern("\u1112\u1161\u11ab") == "%한%"


async def test_memory_index_searches_titles() -> None:
    await assert_title_search(MemorySessionIndex())


async def test_new_fields_ride_the_record_but_stay_out_of_the_row() -> None:
    """색인의 다섯 칸 가운데 목록과 상세의 한 줄에는 title 만 실린다. 나머지 넷은 내부값이다."""
    index = MemorySessionIndex()
    await index.create(replace(session_record("s1", "alice"), title="제목", turn_id="t" * 32))
    row = await index.update("s1", last_turn=2, upload_bytes=10, cite_seq=3)
    assert (row.turn_id, row.last_turn, row.upload_bytes, row.cite_seq) == ("t" * 32, 2, 10, 3)
    out = row.to_dict()
    assert out["title"] == "제목"
    assert not {"turn_id", "last_turn", "upload_bytes", "cite_seq"} & out.keys()


_ADD = tool_message("Add", a=1, b=2)


async def _approve(running: Any, sid: str) -> dict[str, Any]:
    """보류 중인 호출을 승인하고 재개 턴의 마지막 이벤트를 돌려준다."""
    _, resumed = await resume(running, sid, [{"type": "approve"}])
    return resumed[-1][1]


def _observed_turn_ids(running: Any, name: str) -> list[str]:
    return [e["turn_id"] for e in running.observed(name)]


async def test_turn_id_marks_each_attempt_and_resume_keeps_it(support_doc) -> None:
    """턴 시도마다 turn_id 가 나고 재개는 같은 id 와 번호를 이어 쓴다. 턴을 연 사람 메시지에는
    `u-<turn_id>` 가 붙고 제목은 첫 메시지의 첫 줄이다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        _, sse = await chat(running, f"{_ADD}\n둘째 줄")
        sid = sid_of(sse)
        row = await running.runtime.store.index.get(sid)
        assert row.title == _ADD
        assert (row.last_turn, row.turns) == (1, 1) and len(row.turn_id) == 32
        humans = await checkpoint_humans(running, sid)
        assert humans[-1].id == human_message_id(row.turn_id)

        done = await _approve(running, sid)
        assert (done["turn"], done["turn_id"]) == (1, row.turn_id)
        for name in ("turn_start", "turn_end"):
            assert _observed_turn_ids(running, name) == [row.turn_id, row.turn_id]


async def test_a_failed_attempt_moves_last_turn_but_not_turns(support_doc) -> None:
    """오류로 끝난 시도도 last_turn 과 turn_id 를 옮기지만 turns 는 끝난 턴만 센다. 다음 턴은 그
    번호를 새 id 로 다시 쓰고 재개가 그 id 를 이어 쓴다. 제목은 새 세션을 만들 때 한 번만 채운다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    support_doc["main"]["limits"]["retries"] = 0
    async with run_app(support_doc, model=FailOnceModel(model_name="stub")) as running:
        index = running.runtime.store.index
        _, sse = await chat(running, '{"sleep": 0}')
        sid, first = sid_of(sse), sse[-1][1]["turn_id"]
        _, failed = await chat(running, FAIL_MARK, session_id=sid)
        assert failed[-1][0] == "error"
        retry = failed[-1][1]["turn_id"]
        row = await index.get(sid)
        assert (row.last_turn, row.turns, row.turn_id) == (2, 1, retry)

        _, sse = await chat(running, _ADD, session_id=sid)
        assert sse[-1][0] == "interrupt"
        done = await _approve(running, sid)
        assert done["turn"] == 2 and done["turn_id"] not in (first, retry)
        row = await index.get(sid)
        assert (row.last_turn, row.turns, row.turn_id) == (2, 2, done["turn_id"])
        assert row.title == '{"sleep": 0}'
        expected = [first, retry, done["turn_id"], done["turn_id"]]
        for name in ("turn_start", "turn_end"):
            assert _observed_turn_ids(running, name) == expected


async def test_resuming_a_turn_cut_at_its_interrupt_keeps_the_attempt_number(support_doc) -> None:
    """interrupt 를 보내다 읽기가 떠나도 턴은 끊기지 않은 승인 대기 턴과 똑같이 마감한다(D92). 턴
    수는 그 시도의 번호다. 재개는 그 번호를 이어 써 last_turn 이 뒤로 가지 않고, 다음 턴은 그 다음
    번호다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        index = running.runtime.store.index
        _, sse = await chat(running, '{"sleep": 0}')
        sid = sid_of(sse)
        _, response = await open_chat(running, _ADD, sid)
        await cut_while_sending(response, b"event: interrupt")
        # 읽기가 떠나도 턴 태스크는 interrupt 까지 가서 마감한다
        await wait_for(lambda: lease_free(index, sid), what="보류로 마감한 턴")
        row = await index.get(sid)
        assert (row.status, row.turns, row.last_turn) == ("pending", 2, 2)

        done = await _approve(running, sid)
        assert (done["turn"], done["turn_id"]) == (2, row.turn_id)
        row = await index.get(sid)
        assert (row.turns, row.last_turn) == (2, 2)
        _, sse = await chat(running, '{"sleep": 0}', session_id=sid)
        assert sse[-1][1]["turn"] == 3


@needs_pg
async def test_real_postgres_index_searches_titles() -> None:
    """Postgres 색인의 제목 검색도 메모리 색인과 같은 규칙이다."""
    async with pg_schema() as schema:
        store = await open_postgres_store(schema.dsn, instance_id="inst-1")
        try:
            await assert_title_search(store.index)
        finally:
            await store.close()
