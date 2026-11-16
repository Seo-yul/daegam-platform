"""화면용 전사의 접기 규칙이다(P18 F1).

접기 규칙이 UI 의 applyChatEvent 와 같은지, 실제 /chat 이 낸 done·error·model_fallback 이 UI 와 같이
쓰는 표본과 같은 칸인지 본다. 저장소 셋의 계약과 다시 읽은 세션이 스트림과 같은지는
test_transcript_store.py 가 본다.
"""

from __future__ import annotations

from typing import Any

from runtime import stream, transcript
from runtime.stub import FAIL_FIRST_ENV
from runtime.transcript import (
    TRANSCRIPT_LIMIT,
    TurnTranscript,
    apply_event,
    close_open_calls,
    fold_rewinds,
    merge_resumed_calls,
    resolve_interrupts,
    user_item,
)
from tests.app_fixtures import CITATIONS_FIXTURE, folded, sid_of, transcript_items
from tests.attachment_fixtures import ATTACHMENTS_FIXTURE
from tests.conftest import chat, events_named, load_fixture, run_app


def test_event_names_match_the_sse_contract() -> None:
    """전사가 접는 이름은 stream.py 의 SSE 이름 그대로다. 한 글자만 어긋나도 조용히 안 쌓인다."""
    assert (transcript.TOKEN, transcript.TOOL_CALL, transcript.TOOL_RESULT) == (
        stream.TOKEN,
        stream.TOOL_CALL,
        stream.TOOL_RESULT,
    )
    assert (transcript.INTERRUPT, transcript.GUARDRAIL) == (stream.INTERRUPT, stream.GUARDRAIL)
    assert (transcript.DONE, transcript.ERROR) == (stream.DONE, stream.ERROR)
    assert transcript.GREETING == stream.GREETING
    assert transcript.CITATIONS == stream.CITATIONS
    assert transcript.MODEL_FALLBACK == stream.MODEL_FALLBACK
    assert transcript.TOOL_LIMIT == stream.TOOL_LIMIT


def test_greeting_stands_before_the_user_item() -> None:
    """새 세션의 인사말은 이번 턴 사용자 말풍선 앞이다(D65). 사용자 항목은 턴을 열 때 먼저 붙고
    이벤트는 그 뒤에 오므로 끝에 덧붙이지 않고 앞에 끼운다. 두 번 와도 한 번만 선다."""
    items: list[dict[str, Any]] = [{"kind": "user", "text": "안녕"}]
    apply_event(items, "greeting", {"session_id": "s1", "text": "어서 오세요"})
    assert items == [
        {"kind": "assistant", "text": "어서 오세요", "streaming": False, "greeting": True},
        {"kind": "user", "text": "안녕"},
    ]
    apply_event(items, "greeting", {"session_id": "s1", "text": "어서 오세요"})
    assert len(items) == 2
    # 사용자 항목이 없으면 끝에 선다
    alone: list[dict[str, Any]] = []
    apply_event(alone, "greeting", {"session_id": "s1", "text": "어서 오세요"})
    assert [i["greeting"] for i in alone] == [True]


def test_tokens_join_into_one_bubble_per_agent() -> None:
    items: list[dict[str, Any]] = []
    apply_event(items, "token", {"text": "안"})
    apply_event(items, "token", {"text": "녕"})
    assert items == [{"kind": "assistant", "text": "안녕", "streaming": True}]
    # 서브에이전트 라벨이 다르면 다른 말풍선이고, 더 이어지지 않는 앞의 것은 닫힌다(UI 와 같다).
    # 열린 채 두면 그 뒤의 항목이 조각마다 붙지 못한다
    apply_event(items, "token", {"text": "부하", "agent": "tools:1"})
    assert items[0]["streaming"] is False
    assert items[1] == {"kind": "assistant", "text": "부하", "streaming": True, "agent": "tools:1"}
    turn = TurnTranscript()
    turn.add("token", {"text": "안"})
    turn.add("token", {"text": "부하", "agent": "tools:1"})
    assert turn.closed_count() == 1
    # 말풍선이 아닌 항목이 오면 닫는다
    apply_event(items, "error", {"code": "turn_failed", "message": "끊겼다"})
    assert [i["streaming"] for i in items[:2]] == [False, False]
    assert items[2] == {"kind": "error", "code": "turn_failed", "message": "끊겼다"}


def test_repeated_tool_call_reuses_the_open_card() -> None:
    """결과 없는 같은 id 의 카드가 있으면 다시 온 tool_call 은 그 카드를 이어 쓴다(UI 와 같다).

    승인 뒤 재개 스트림이 같은 tool_call 을 다시 내는 자리다. 새 카드를 달면 결과는 앞 카드에
    붙고 뒤 카드는 영영 "실행 중" 에 남는다.
    """
    items: list[dict[str, Any]] = []
    apply_event(items, "tool_call", {"id": "c1", "name": "Add", "args": {"a": 1}})
    apply_event(items, "tool_call", {"id": "c1", "name": "Add", "args": {"a": 2}, "agent": "x"})
    assert len(items) == 1
    assert items[0]["call"] == {"id": "c1", "name": "Add", "args": {"a": 2}, "agent": "x"}
    apply_event(
        items, "tool_result", {"id": "c1", "name": "Add", "status": "success", "content": "2"}
    )
    assert items[0]["result"] == {"id": "c1", "name": "Add", "status": "success", "content": "2"}
    # 결과를 받은 뒤 같은 id 가 다시 오면 새 카드다. id 없는 호출은 합치지 않는다
    apply_event(items, "tool_call", {"id": "c1", "name": "Add", "args": {}})
    apply_event(items, "tool_call", {"name": "Add", "args": {}})
    apply_event(items, "tool_call", {"name": "Add", "args": {}})
    assert len(items) == 4
    # 짝 없는 결과는 인자 없는 카드를 만든다
    apply_event(
        items, "tool_result", {"id": "zzz", "name": "Sub", "status": "error", "content": "x"}
    )
    assert items[4]["call"] == {"id": "zzz", "name": "Sub", "args": {}}
    assert items[4]["result"]["status"] == "error"


def test_merge_resumed_calls_joins_the_card_split_across_turns() -> None:
    """승인을 기다리다 끊긴 턴의 카드와 재개 턴이 다시 알린 카드는 저장소의 다른 묶음에 있다.
    읽을 때 앞 카드에 뒤 카드의 인자와 결과를 합치고 뒤 카드를 뺀다."""
    items: list[dict[str, Any]] = [
        {"kind": "user", "text": "더해 줘"},
        {
            "kind": "tool_call",
            "call": {"id": "c1", "name": "Add", "args": {"a": 1}},
            "result": None,
        },
        {"kind": "interrupt", "pending": {}, "resolved": False},
        {
            "kind": "tool_call",
            "call": {"id": "c1", "name": "Add", "args": {"a": 9}},
            "result": {"id": "c1", "status": "success", "content": "10"},
        },
        {"kind": "assistant", "text": "결과: 10", "streaming": False},
        # 결과가 이미 있는 카드와 id 없는 카드는 그대로 둔다
        {"kind": "tool_call", "call": {"id": "c1", "name": "Add", "args": {}}, "result": None},
        {"kind": "tool_call", "call": {"name": "Add", "args": {}}, "result": None},
        {"kind": "tool_call", "call": {"name": "Add", "args": {}}, "result": None},
    ]
    merge_resumed_calls(items)
    assert [i["kind"] for i in items] == [
        "user",
        "tool_call",
        "interrupt",
        "assistant",
        "tool_call",
        "tool_call",
        "tool_call",
    ]
    assert items[1]["call"]["args"] == {"a": 9}
    assert items[1]["result"]["content"] == "10"


def test_guardrail_and_done_items_match_the_ui_shape() -> None:
    items: list[dict[str, Any]] = []
    # 런타임의 flag 위반에는 문구가 없다. UI 가 채우는 문장을 여기서도 채운다
    apply_event(items, "guardrail", {"position": "input", "rule": "deny_pattern", "action": "flag"})
    assert items[0] == {
        "kind": "guardrail",
        "action": "flag",
        "message": "가드레일에 걸렸습니다.",
        "position": "input",
        "rule": "deny_pattern",
    }
    apply_event(
        items,
        "done",
        {
            "session_id": "s1",
            "turn": 2,
            "usage": {"input_tokens": 3},
            "tool_calls": 1,
            "text": "끝",
        },
    )
    # done 은 usage·turn·tool_calls 만 남긴다. 본문은 앞의 말풍선에 이미 있다
    assert items[1] == {"kind": "done", "usage": {"input_tokens": 3}, "turn": 2, "tool_calls": 1}
    # 구조화 결과는 done 이 실어 왔을 때만 같은 칸에 남는다(D65)
    done = {"session_id": "s1", "turn": 3, "usage": {}, "tool_calls": 0, "text": "{}"}
    apply_event(items, "done", {**done, "structured_response": {"answer": "7"}})
    assert items[2]["structured_response"] == {"answer": "7"}
    apply_event(items, "model_retry", {"attempt": 1})
    assert len(items) == 3, "화면 항목이 아닌 이벤트는 접지 않는다"


TURN_FIXTURE = load_fixture("chat/turn.json")
"""done·error 이벤트와 전사 항목의 표본이다. UI 의 되살리기 테스트와 msw 목이 같은 파일을
읽는다(P36)."""


def test_done_and_error_items_carry_the_turn_id() -> None:
    """done·error 는 그 시도의 turn_id 를 싣고 전사 항목도 같은 칸을 든다. 표본의 이벤트를 접으면
    표본의 전사가 된다."""
    events = TURN_FIXTURE["events"]
    first = TurnTranscript()
    first.add_user("환불 기한이 언제까지야?")
    first.add("token", {"text": events["done"]["text"]})
    first.add("done", events["done"])
    second = TurnTranscript()
    second.add_user("영수증도 다시 받을 수 있어?")
    second.add("error", events["error"])
    assert [*first.take(), *second.take()] == TURN_FIXTURE["transcript"]
    # turn_id 가 없는 이벤트(옛 런타임)는 칸을 두지 않는다
    items: list[dict[str, Any]] = []
    apply_event(items, "error", {"code": "turn_failed", "message": "x"})
    assert "turn_id" not in items[0]


async def test_emitted_done_and_error_have_the_fixture_keys(support_doc, monkeypatch) -> None:
    """실제 /chat 이 낸 done·error 는 표본과 같은 칸이다. 구조화 결과는 있을 때만 실린다. UI 의 목이
    표본의 이벤트를 펼쳐 쓰므로 마지막 이벤트의 칸이 바뀌면 여기서 먼저 걸린다."""
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    support_doc["main"]["tools"] = []
    support_doc["main"]["limits"]["retries"] = 0
    support_doc["main"]["fallback"] = None
    events = TURN_FIXTURE["events"]
    async with run_app(support_doc) as running:
        _, failed = await chat(running, "실패할 턴")
        assert failed[-1][0] == "error"
        assert failed[-1][1].keys() == events["error"].keys()
        _, sse = await chat(running, "환불 기한이 언제까지야?", session_id=sid_of(failed))
        assert sse[-1][0] == "done"
        assert sse[-1][1].keys() - {"structured_response"} == events["done"].keys()


def test_citations_attach_to_the_card_of_the_same_call() -> None:
    """citations 는 항목이 아니라 같은 call_id 의 도구 호출 카드에 붙는다. 표본의 이벤트를 접으면
    표본의 전사가 되고, 카드가 없는 citations 는 버린다."""
    events = CITATIONS_FIXTURE["events"]
    fixture = CITATIONS_FIXTURE["transcript"]
    done = next(i for i in fixture if i["kind"] == "done")
    answer = next(i for i in fixture if i["kind"] == "assistant")
    turn = TurnTranscript()
    turn.add_user(fixture[0]["text"])
    for name in ("tool_call", "tool_result", "citations"):
        turn.add(name, events[name])
    turn.add("token", {"text": answer["text"]})
    turn.add("done", {"session_id": "s1", "text": answer["text"], **done})
    assert turn.take() == fixture

    items: list[dict[str, Any]] = []
    apply_event(items, "citations", {"call_id": "nope", "items": [{"n": 1}]})
    assert items == []


def test_merge_resumed_calls_keeps_the_citations_of_the_resumed_card() -> None:
    """승인 뒤 재개 턴의 카드가 앞 카드에 합쳐질 때 결과와 함께 출처도 옮긴다."""
    call = {"id": "c1", "name": "search_faq", "args": {}}
    items: list[dict[str, Any]] = [
        {"kind": "tool_call", "call": call, "result": None},
        {"kind": "tool_call", "call": call, "result": {"id": "c1"}, "citations": [{"n": 1}]},
    ]
    merge_resumed_calls(items)
    assert items == [
        {"kind": "tool_call", "call": call, "result": {"id": "c1"}, "citations": [{"n": 1}]}
    ]


def test_the_user_item_carries_attachment_summaries() -> None:
    """첨부가 든 턴의 사용자 항목은 `attachments` 를 들고, 표본의 이벤트를 접으면 표본의 전사가
    된다. 첨부가 없으면 칸을 두지 않는다."""
    fixture = ATTACHMENTS_FIXTURE["transcript"]
    turn = TurnTranscript()
    turn.add_user(fixture[0]["text"], attachments=fixture[0]["attachments"])
    turn.add("token", {"text": fixture[1]["text"]})
    turn.add("done", ATTACHMENTS_FIXTURE["events"]["done"])
    assert turn.take() == fixture
    assert user_item("첨부 없음") == {"kind": "user", "text": "첨부 없음"}


FALLBACK_FIXTURE = load_fixture("chat/fallback.json")
"""예비 모델이 대신 답한 턴의 model_fallback·done 이벤트와 전사 표본이다(P36 11절). done 은 구조화
결과를 싣는다."""


def test_model_fallback_becomes_one_fallback_item_per_turn() -> None:
    """model_fallback 은 fallback 항목으로 접힌다. 모델 호출마다 오는 같은 전환은 턴에 하나만
    남는다. 표본의 이벤트를 접으면 표본의 전사가 된다."""
    events = FALLBACK_FIXTURE["events"]
    fixture = FALLBACK_FIXTURE["transcript"]
    turn = TurnTranscript()
    turn.add_user(fixture[0]["text"])
    turn.add("model_fallback", events["model_fallback"])
    turn.add("token", {"text": fixture[2]["text"]})
    turn.add("model_fallback", events["model_fallback"])
    turn.add("done", events["done"])
    assert turn.take() == fixture


def test_fallback_items_split_at_the_user_item_and_the_approval_card() -> None:
    """같은 전환도 새 사용자 항목이나 승인 카드 뒤에서는 다시 남는다. 서브에이전트의 전환은
    따로다."""
    switch = {"from": "a", "to": "b"}
    items: list[dict[str, Any]] = []
    apply_event(items, "model_fallback", switch)
    apply_event(items, "model_fallback", {**switch, "agent": "researcher"})
    apply_event(items, "model_fallback", switch)
    assert items == [
        {"kind": "fallback", "from": "a", "to": "b"},
        {"kind": "fallback", "from": "a", "to": "b", "agent": "researcher"},
    ]
    apply_event(items, "interrupt", {"action_requests": []})
    apply_event(items, "model_fallback", switch)
    items.append({"kind": "user", "text": "다음"})
    apply_event(items, "model_fallback", switch)
    assert [i["kind"] for i in items[2:]] == ["interrupt", "fallback", "user", "fallback"]


async def test_fallback_turn_reads_back_with_one_notice(support_doc, monkeypatch) -> None:
    """실제 /chat 에서 두 모델 호출이 모두 예비 모델로 넘어가면 이벤트는 둘이지만 다시 읽은 전사의
    알림은 하나다. 이벤트의 칸은 표본과 같다."""
    monkeypatch.setenv(FAIL_FIRST_ENV, "5")
    support_doc["main"]["limits"]["retries"] = 0
    fallback = dict(support_doc["main"]["model"], name="stub-fallback")
    support_doc["main"]["fallback"] = fallback
    async with run_app(support_doc) as running:
        message = '{"tool": "Add", "args": {"a": 4, "b": 5}}'
        _, sse = await chat(running, message)
        switches = events_named(sse, "model_fallback")
        assert len(switches) == 2
        assert switches[0].keys() == FALLBACK_FIXTURE["events"]["model_fallback"].keys()
        items = await transcript_items(running, sid_of(sse))
        assert items == folded((message, sse))
        assert [i["kind"] for i in items] == ["user", "fallback", "tool_call", "assistant", "done"]
        assert items[1] == {"kind": "fallback", "from": "stub", "to": "stub-fallback"}


TOOL_LIMIT_FIXTURE = load_fixture("chat/tool_limit.json")
"""한 실행의 Tool 호출 한도에 닿은 턴의 tool_limit·done 이벤트와 전사 표본이다(T82). 서브에이전트의
한도와 메인의 한도가 따로 남는다. UI 의 applyChatEvent 와 전사 복원이 같은 표본을 견준다."""


def test_tool_limit_becomes_a_notice_item() -> None:
    """tool_limit 은 tool_limit 항목으로 접힌다. 서브에이전트의 한도는 agent 칸을 든다. 표본의
    이벤트를 접으면 표본의 전사가 된다."""
    events = TOOL_LIMIT_FIXTURE["events"]
    fixture = TOOL_LIMIT_FIXTURE["transcript"]
    turn = TurnTranscript()
    turn.add_user(fixture[0]["text"])
    turn.add("tool_limit", events["subagent_tool_limit"])
    turn.add("tool_limit", events["tool_limit"])
    turn.add("token", {"text": fixture[3]["text"]})
    turn.add("done", events["done"])
    assert turn.take() == fixture


def _turn(question: str, answer: str, *, rewind: bool = False) -> list[dict[str, Any]]:
    """질문 하나와 답 하나로 끝난 턴의 전사 항목이다. rewind 면 다시 생성 턴이다."""
    turn = TurnTranscript(rewind=rewind)
    turn.add_user(question)
    turn.add("token", {"text": answer})
    turn.add("done", {"session_id": "s1", "turn": 1, "usage": {}, "tool_calls": 0})
    return turn.take()


def test_fold_rewinds_drops_the_replaced_turn_back_to_its_user_item() -> None:
    """다시 생성 턴은 맨 앞에 표식을 두고, 읽을 때 표식마다 바로 앞 사용자 항목까지 걷어 낸다.
    인사말은 첫 사용자 항목 앞이라 남고, 두 번 다시 만들어도 마지막 갈래 하나만 남는다(P36)."""
    greeting = {"kind": "assistant", "text": "어서 오세요", "streaming": False, "greeting": True}
    first = _turn("첫 질문", "첫 답")
    items = [greeting, *first, *_turn("둘째", "버릴 답"), *_turn("둘째", "또 버릴 답", rewind=True)]
    items += _turn("고친 둘째", "남을 답", rewind=True)
    assert items.count({"kind": "rewind"}) == 2
    fold_rewinds(items)
    assert items == [greeting, *first, *_turn("고친 둘째", "남을 답")]
    # 오류 턴도 사용자 항목부터 걷힌다
    failed = [{"kind": "user", "text": "실패"}, {"kind": "error", "code": "turn_failed"}]
    items = [*first, *failed, *_turn("실패", "다시 만든 답", rewind=True)]
    fold_rewinds(items)
    assert items == [*first, *_turn("실패", "다시 만든 답")]


def test_fold_rewinds_without_a_user_item_drops_up_to_the_start() -> None:
    """상한에 걸려 앞 사용자 항목이 이미 잘렸으면 목록 처음까지만 걷는다."""
    tail = [{"kind": "assistant", "text": "잘린 턴의 답", "streaming": False}]
    items = [*tail, *_turn("다시", "새 답", rewind=True)]
    fold_rewinds(items)
    assert items == _turn("다시", "새 답")
    assert fold_rewinds(empty := []) is None and empty == []


def test_discard_keeps_only_what_comes_after() -> None:
    """갈래를 내기 전에 실패한 다시 생성 턴은 표식과 사용자 항목을 버리고 마지막 항목만 붙인다."""
    turn = TurnTranscript(rewind=True)
    turn.add_user("다시")
    turn.discard()
    turn.add("error", {"code": "turn_failed", "message": "멈췄다"})
    assert turn.take() == [{"kind": "error", "code": "turn_failed", "message": "멈췄다"}]


def test_limit_drops_the_oldest_item() -> None:
    items: list[dict[str, Any]] = []
    for i in range(TRANSCRIPT_LIMIT + 20):
        apply_event(items, "error", {"code": "e", "message": str(i)})
    assert len(items) == TRANSCRIPT_LIMIT
    assert items[0]["message"] == "20" and items[-1]["message"] == str(TRANSCRIPT_LIMIT + 19)


def test_close_open_calls_gives_result_less_cards_an_error_result() -> None:
    """오류로 끝난 턴(끊김·시간 초과)의 마감이 부른다. 결과 없는 카드만 오류 결과로 닫혀 다시 그린
    화면에 "실행 중" 이 남지 않고, 결과 있는 카드와 다른 항목은 그대로다."""
    items: list[dict[str, Any]] = []
    apply_event(items, "tool_call", {"id": "c1", "name": "Add", "args": {"a": 1}})
    done = {"id": "c1", "name": "Add", "status": "success", "content": "3"}
    apply_event(items, "tool_result", done)
    apply_event(items, "tool_call", {"id": "c2", "name": "Add", "args": {"a": 2}, "agent": "sub"})
    apply_event(items, "token", {"text": "잠깐"})
    close_open_calls(items, content="연결이 끊겨 턴을 멈췄다")
    assert items[0]["result"]["content"] == "3"
    assert items[1]["result"] == {
        "id": "c2",
        "name": "Add",
        "status": "error",
        "content": "연결이 끊겨 턴을 멈췄다",
        "agent": "sub",
    }
    assert items[2] == {"kind": "assistant", "text": "잠깐", "streaming": True}


def test_resolve_interrupts_leaves_only_the_live_card_open() -> None:
    items = [
        {"kind": "interrupt", "pending": {}, "resolved": False},
        {"kind": "user", "text": "다시"},
        {"kind": "interrupt", "pending": {}, "resolved": False},
    ]
    resolve_interrupts(items, pending=True)
    assert [i.get("resolved") for i in items if i["kind"] == "interrupt"] == [True, False]
    resolve_interrupts(items, pending=False)
    assert [i.get("resolved") for i in items if i["kind"] == "interrupt"] == [True, True]


def test_turn_transcript_hands_items_over_once() -> None:
    """저장은 정상 종료와 finally 두 곳에서 부른다. 두 번 붙지 않아야 한다."""
    turn = TurnTranscript()
    turn.add_user("안녕")
    turn.add("token", {"text": "네"})
    items = turn.take()
    assert items == [
        {"kind": "user", "text": "안녕"},
        {"kind": "assistant", "text": "네", "streaming": False},
    ], "떼어 갈 때 흘러가던 말풍선을 닫는다"
    assert turn.take() == []


def test_only_the_closed_prefix_is_saved_and_citations_stay_with_their_card() -> None:
    """앞에서부터 이어진 닫힌 항목만 붙인다(P78 3절). 흘러가는 말풍선과 결과를 못 받은 카드에서
    멈추므로, 결과와 같은 조각에 오는 인용은 붙이기 전에 제 카드에 붙어 있고 병렬 호출은 앞의 것이
    닫힌 뒤 차례대로 붙는다. 인사말은 사용자 항목 앞이다."""
    turn = TurnTranscript()
    turn.add_user("질문")
    turn.add("greeting", {"text": "안녕하세요"})
    turn.add("token", {"text": "찾아"})
    assert turn.closed_count() == 2, "흘러가는 말풍선에서 멈춘다"
    turn.add("tool_call", {"id": "a", "name": "search", "args": {}})
    turn.add("tool_call", {"id": "b", "name": "search", "args": {}})
    assert turn.closed_count() == 3, "말풍선은 닫혔고 결과 없는 카드에서 멈춘다"
    turn.drop(3)
    turn.add("tool_result", {"id": "b", "name": "search", "content": "B", "status": "success"})
    assert turn.closed_count() == 0, "앞 카드가 열려 있으면 뒤 카드도 기다린다"
    turn.add("tool_result", {"id": "a", "name": "search", "content": "A", "status": "success"})
    turn.add("citations", {"call_id": "a", "items": [{"n": 1}]})
    assert turn.closed_count() == 2
    first, second = turn.items[:2]
    assert first["call"]["id"] == "a" and first["citations"] == [{"n": 1}]
    assert second["call"]["id"] == "b"


def test_a_fallback_is_not_repeated_after_the_turn_start_was_saved() -> None:
    """예비 모델 알림이 같은 턴에 겹치는지는 버퍼 밖에서 센다. 사용자 항목을 붙여 버퍼에서 뺀 뒤에도
    같은 전환은 다시 붙지 않는다."""
    turn = TurnTranscript()
    turn.add_user("질문")
    turn.add("model_fallback", {"from": "main", "to": "spare"})
    turn.drop(turn.closed_count())
    turn.add("model_fallback", {"from": "main", "to": "spare"})
    turn.add("model_fallback", {"from": "main", "to": "other"})
    assert [item["to"] for item in turn.items] == ["other"]
