"""세션 전사다(P18 F1). 새로고침해도 대화가 원문 그대로 다시 그려지게 세션 곁에 쌓는다.

`GET /sessions/{sid}` 의 `messages` 는 `stream.summarize_messages` 가 만든 200자 요약이다. 세션
목록·상세 표에는 맞지만 본문을 자르고 도구 결과를 `{tool, status}` 로 줄이므로 화면을 다시
그리지 못한다. 전사는 그 요약을 대신하지 않고 `transcript` 로 더한다. 요약 계약은 그대로다.

항목 어휘는 UI 의 ChatItem(`ui/src/components/chat/items.ts`)이다. 아래 `apply_event` 는 그 파일의
`applyChatEvent` 와 같은 규칙을 파이썬으로 적은 것이고, 플랫폼 비서의
`platform/daegam/services/assistant/loop.py:apply_event` 와도 짝이다. 셋이 어긋나면 새로고침한
화면이 스트림으로 본 화면과 달라진다.

저장이 늘 뿐이라 SSE 이벤트 계약과 agent.json 은 한 글자도 바뀌지 않는다. 전사에 담는 것은
스트림이 실제로 내보낸 data 그대로이고 본문을 자르는 자리가 없다. 스트림을 열지 못하고 입구에서
거절된 요청(429·413·422)은 턴이 아니므로 전사도 없다.

상한은 항목 수 `TRANSCRIPT_LIMIT` 이고 넘으면 가장 오래된 항목부터 버린다(비서와 같은 성질이다).
항목 하나는 아무리 커도 자르지 않는다. 전사의 뜻이 원문이기 때문이고, 큰 도구 결과는 어차피 같은
내용이 체크포인트의 ToolMessage 에 통째로 남아 있어 전사만 잘라 봐야 저장소를 아끼지도 못한다.

저장소는 세션 색인이 사는 곳이다(D33). postgres 면 색인과 같은 DB 의 `daegam_transcripts` 표,
redis 면 인스턴스 접두어 아래 리스트, memory 가 없으면 파드 안 dict 다. 쓰기는 언제나 덧붙이기
하나(INSERT·RPUSH)이고 읽고 고쳐 쓰지 않는다. 파드가 여럿이어도 전사가 뒤섞이거나 사라지지 않게
하는 자리다. 같은 세션의 턴은 색인의 임차(claim_turn)가 이미 하나로 막고 있다.

임시 턴(`ephemeral`)도 전사를 남긴다. 그 세션 안에서는 보여야 하기 때문이다.

다시 생성(P36 5절)도 덧붙이기다. 갈래가 생긴 다시 생성 턴은 맨 앞에 `{"kind": "rewind"}` 표식을
두고, 읽을 때 `fold_rewinds` 가 표식마다 바로 앞 사용자 항목까지 걷어 낸다. 버린 갈래의 항목도 상한
안의 자리를 차지한다.

전사는 세션이 닫혀도 남는다(D46). 세션 TTL 이 지나면 sweep 이 세션을 닫고 체크포인트만 지우므로
닫힌 대화도 목록에서 열어 읽을 수 있고, 기록 보존 기간(`DAEGAM_TRANSCRIPT_RETENTION_DAYS`, 기본
30일)이 지나야 전사와 색인 행이 함께 사라진다. 보존 기간이 0 이면 세션을 닫는 그 sweep 이 기록까지
지운다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from runtime.dsn import turn_lease_key
from runtime.fence import HELD_LEASE_LUA, HELD_LEASE_SQL, refused

TRANSCRIPT_LIMIT = 500
"""세션 하나가 남기는 항목 수 상한이다. 넘으면 앞(오래된 것)부터 버린다."""

# 접는 이벤트 이름이다. SSE 계약이라 stream.py 의 상수와 같은 글자여야 한다(test_transcript 가
# 두 자리가 같은지 본다). 관측용 이벤트(budget·model_retry)는 화면 항목이 아니므로 여기 없다. UI 의
# applyChatEvent 도 그것들을 항목으로 접지 않는다. citations 는 항목이 아니라 같은 call_id 의 도구
# 호출 카드에 `citations` 칸으로 붙는다(P36 7절)
GREETING = "greeting"
TOKEN = "token"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
CITATIONS = "citations"
INTERRUPT = "interrupt"
GUARDRAIL = "guardrail"
MODEL_FALLBACK = "model_fallback"
TOOL_LIMIT = "tool_limit"
DONE = "done"
ERROR = "error"

# ChatItem 의 kind 다. user 는 이벤트가 아니라 턴을 연 요청 본문에서 온다
KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_TOOL_CALL = "tool_call"
KIND_INTERRUPT = "interrupt"
KIND_GUARDRAIL = "guardrail"
# 예비 모델이 대신 답했다는 알림이다(P36 11절). model_fallback 이벤트를 접은 것이다
KIND_FALLBACK = "fallback"
# 한 실행의 Tool 호출 한도에 닿았다는 알림이다(T82). tool_limit 이벤트를 접은 것이다
KIND_TOOL_LIMIT = "tool_limit"
KIND_DONE = "done"
KIND_ERROR = "error"
# 다시 생성 턴이 맨 앞에 두는 표식이다(P36 5절). 런타임 안의 항목이라 읽을 때 접혀 응답에 없다
KIND_REWIND = "rewind"

GUARDRAIL_FALLBACK_MESSAGE = "가드레일에 걸렸습니다."
"""문구 없는 위반(action: flag)에 UI 가 채우는 문장이다(items.ts). 같은 값을 여기서도 채워야
다시 그린 화면이 스트림으로 본 화면과 같다."""


def user_item(
    text: str, attachments: Sequence[dict[str, Any]] = (), *, turn_id: str | None = None
) -> dict[str, Any]:
    """턴을 연 사용자 말풍선이다. SSE 이벤트가 아니라 요청 본문에서 온다. 첨부가 있으면
    `attachments: [{name, kind, bytes, pages?, sheets?, truncated?}]` 를 싣는다(P36 9절). 본문은
    첨부 저장소에 있고 세션이 닫히면 지워지므로 전사에는 이것만 남는다. turn_id 는 그 시도의 id 다
    (P79 3절). 되살리기가 끊긴 새 말의 사용자 항목이 저장됐는지 이것으로 본다. 옛 런타임의 전사에는
    없다."""
    item: dict[str, Any] = {"kind": KIND_USER, "text": text}
    if attachments:
        item["attachments"] = list(attachments)
    if turn_id is not None:
        item["turn_id"] = turn_id
    return item


def error_item(data: dict[str, Any]) -> dict[str, Any]:
    """error 이벤트 하나를 접은 전사 항목이다. 되살리기가 끊긴 시도의 줄을 이 모양으로 저장한다."""
    return _error_item(data)


def apply_event(items: list[dict[str, Any]], name: str, data: dict[str, Any]) -> None:
    """SSE 이벤트 하나를 ChatItem 목록에 접는다(제자리). UI 의 applyChatEvent 와 같은 규칙이다.

    `data` 는 SSE 로 실제 나간 본문 그대로이고 잘라 담는 자리가 없다.
    """
    if name == TOKEN:
        _apply_token(items, data)
        _trim(items)
        return
    _close_streaming(items)
    if name == GREETING:
        _insert_greeting(items, data)
    elif name == TOOL_CALL:
        if _merge_resumed_call(items, data) is None:
            items.append({"kind": KIND_TOOL_CALL, "call": dict(data), "result": None})
    elif name == TOOL_RESULT:
        if _attach_result(items, data):
            return
        # 짝 없는 결과다. UI 도 인자 없는 호출 카드를 만들어 결과만 보여 준다
        call: dict[str, Any] = {"id": data.get("id"), "name": data.get("name"), "args": {}}
        if data.get("agent") is not None:
            call["agent"] = data["agent"]
        items.append({"kind": KIND_TOOL_CALL, "call": call, "result": dict(data)})
    elif name == CITATIONS:
        _attach_citations(items, data)
        return
    elif name == INTERRUPT:
        items.append({"kind": KIND_INTERRUPT, "pending": dict(data), "resolved": False})
    elif name == GUARDRAIL:
        items.append(_guardrail_item(data))
    elif name == MODEL_FALLBACK:
        _add_fallback(items, data)
    elif name == TOOL_LIMIT:
        items.append(tool_limit_item(data))
    elif name == DONE:
        items.append(_done_item(data))
    elif name == ERROR:
        items.append(_error_item(data))
    else:
        return
    _trim(items)


def _apply_token(items: list[dict[str, Any]], data: dict[str, Any]) -> None:
    """흘러 들어오는 글을 말풍선 하나로 잇는다. 서브에이전트 라벨이 다르면 다른 말풍선이고, 앞
    말풍선은 더 이어지지 않으므로 닫는다. 열린 채 두면 그 뒤의 항목이 조각마다 붙지 못한다(P78
    3절)."""
    text = str(data.get("text") or "")
    agent = data.get("agent")
    last = items[-1] if items else None
    if (
        last is not None
        and last.get("kind") == KIND_ASSISTANT
        and last.get("streaming")
        and last.get("agent") == agent
    ):
        last["text"] += text
        return
    _close_streaming(items)
    item: dict[str, Any] = {"kind": KIND_ASSISTANT, "text": text, "streaming": True}
    if agent is not None:
        item["agent"] = agent
    items.append(item)


def _insert_greeting(items: list[dict[str, Any]], data: dict[str, Any]) -> None:
    """새 세션의 인사말을 이번 턴 사용자 항목 앞에 세운다(D65). 이미 있으면 그대로다. UI 의
    insertGreeting 과 같은 규칙이고, 말풍선 모양에 greeting 표시를 더해 복원한 화면이 "인사말"
    라벨을 단다."""
    if any(item.get("kind") == KIND_ASSISTANT and item.get("greeting") for item in items):
        return
    item: dict[str, Any] = {
        "kind": KIND_ASSISTANT,
        "text": str(data.get("text") or ""),
        "streaming": False,
        "greeting": True,
    }
    user_at = _last_index(items, KIND_USER)
    items.insert(len(items) if user_at is None else user_at, item)


def _last_index(items: list[dict[str, Any]], kind: str) -> int | None:
    """그 종류의 마지막 항목 자리다. 없으면 None 이다."""
    return next((i for i in range(len(items) - 1, -1, -1) if items[i].get("kind") == kind), None)


def _open_call(items: list[dict[str, Any]], call_id: Any) -> dict[str, Any] | None:
    """결과를 아직 못 받은 같은 id 의 호출 카드 중 가장 앞의 것이다(UI 의 findIndex 차례)."""
    return next(
        (
            item
            for item in items
            if item.get("kind") == KIND_TOOL_CALL
            and item["call"].get("id") == call_id
            and not item["result"]
        ),
        None,
    )


def _merge_resumed_call(items: list[dict[str, Any]], call: dict[str, Any]) -> dict[str, Any] | None:
    """다시 알린 호출을 결과 없는 같은 id 의 카드에 합친다. 합친 카드를 돌려주고 없으면 None 이다.

    승인 뒤 재개하면 런타임이 같은 id 의 tool_call 을 한 번 더 낸다(재개 턴의 씨앗에 승인을
    기다리던 호출은 없다. stream.TurnState.seeded). 새 카드를 달면 결과는 앞 카드에 붙고 뒤 카드는
    영영 "실행 중" 에 남으므로 UI 의 applyChatEvent 처럼 앞 카드를 이어 쓴다. id 가 없는 호출은
    합치지 않는다(UI 와 같다).
    """
    call_id = call.get("id")
    card = _open_call(items, call_id) if call_id else None
    if card is not None:
        card["call"] = {**card["call"], **call}
    return card


def _attach_result(items: list[dict[str, Any]], data: dict[str, Any]) -> bool:
    """결과를 아직 답이 없는 같은 id 의 호출 카드에 붙인다. 붙였으면 참이다.

    재개 턴이 다시 알린 호출은 `_merge_resumed_call` 이 앞 카드에 합쳐 두므로 같은 턴 안에서는
    카드가 하나이고 결과도 그 카드에 붙는다.
    """
    card = _open_call(items, data.get("id"))
    if card is None:
        return False
    card["result"] = dict(data)
    return True


def _attach_citations(items: list[dict[str, Any]], data: dict[str, Any]) -> None:
    """인용 출처를 같은 call_id 의 마지막 도구 호출 카드에 붙인다. 카드가 없으면 버린다. 결과 바로
    뒤에 오므로 그 카드는 이미 결과를 받았다."""
    call_id = data.get("call_id")
    if call_id is None:
        return
    card = next(
        (
            item
            for item in reversed(items)
            if item.get("kind") == KIND_TOOL_CALL and item["call"].get("id") == call_id
        ),
        None,
    )
    if card is not None:
        card["citations"] = list(data.get("items") or [])


def _guardrail_item(data: dict[str, Any]) -> dict[str, Any]:
    """가드레일 위반 항목이다. 런타임은 규칙 이름을 rule 로, 비서는 kind 로 실어 둘 다 본다."""
    item: dict[str, Any] = {
        "kind": KIND_GUARDRAIL,
        "action": data.get("action") or "block",
        "message": data.get("message") or GUARDRAIL_FALLBACK_MESSAGE,
    }
    if data.get("position") is not None:
        item["position"] = data["position"]
    rule = data.get("rule") or data.get("kind")
    if rule is not None:
        item["rule"] = rule
    return item


def _add_fallback(items: list[dict[str, Any]], data: dict[str, Any]) -> None:
    """예비 모델 알림을 붙인다. 같은 턴에 같은 전환이 이미 있으면 붙이지 않는다. UI 의 addFallback
    과 같은 규칙이다.

    기본 모델이 계속 실패하면 모델 호출마다 전환이 일어나 도구를 부른 턴에서는 알림이 호출 수만큼
    쌓인다. 턴의 시작은 마지막 사용자 항목이나 승인 카드 바로 뒤다. 재개 턴의 전사는 승인 카드
    뒤에서 시작하므로 스트림으로 본 화면과 같은 자리에서 나뉜다.
    """
    item = fallback_item(data)
    if item not in items[_turn_start(items) :]:
        items.append(item)


def fallback_item(data: dict[str, Any]) -> dict[str, Any]:
    """model_fallback 이벤트를 접은 항목이다. 같은 턴의 같은 전환인지 이 모양으로 견준다."""
    item: dict[str, Any] = {"kind": KIND_FALLBACK, "from": data.get("from"), "to": data.get("to")}
    if data.get("agent") is not None:
        item["agent"] = data["agent"]
    return item


def tool_limit_item(data: dict[str, Any]) -> dict[str, Any]:
    """tool_limit 이벤트를 접은 항목이다. 서브에이전트의 한도일 때만 agent 칸을 둔다."""
    item: dict[str, Any] = {"kind": KIND_TOOL_LIMIT, "limit": data.get("limit")}
    if data.get("agent") is not None:
        item["agent"] = data["agent"]
    return item


def _turn_start(items: list[dict[str, Any]]) -> int:
    """이번 턴의 첫 자리다. 마지막 사용자 항목이나 승인 카드 바로 뒤이고 둘 다 없으면 처음이다."""
    starts = (_last_index(items, kind) for kind in (KIND_USER, KIND_INTERRUPT))
    return max((at + 1 for at in starts if at is not None), default=0)


def _done_item(data: dict[str, Any]) -> dict[str, Any]:
    """턴 마감 항목이다. 본문(text)은 앞의 말풍선에 이미 있어 싣지 않고, 구조화 결과는 done 이
    실어 왔을 때만 같은 칸(structured_response)에 둔다. UI 의 done 항목과 같은 모양이라 복원해도
    남는다."""
    item: dict[str, Any] = {
        "kind": KIND_DONE,
        "usage": dict(data.get("usage") or {}),
        "turn": data.get("turn"),
        "tool_calls": data.get("tool_calls"),
    }
    if data.get("structured_response") is not None:
        item["structured_response"] = data["structured_response"]
    return _with_ids(item, data)


def _error_item(data: dict[str, Any]) -> dict[str, Any]:
    """오류로 끝난 턴의 항목이다. done 처럼 그 시도의 turn_id 와 실행의 trace_id 를 든다."""
    item = {"kind": KIND_ERROR, "code": data.get("code"), "message": data.get("message")}
    return _with_ids(item, data)


def _with_ids(item: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """이벤트가 turn_id·trace_id 를 실어 왔으면 항목에도 둔다. 옛 런타임의 전사에는 없다. 화면의
    "트레이스" 단추가 새로고침 뒤에도 trace_id 를 읽는다(P37)."""
    for key in ("turn_id", "trace_id"):
        if data.get(key) is not None:
            item[key] = data[key]
    return item


def _close_streaming(items: list[dict[str, Any]]) -> None:
    last = items[-1] if items else None
    if last is not None and last.get("kind") == KIND_ASSISTANT and last.get("streaming"):
        last["streaming"] = False


def close_open_calls(items: list[dict[str, Any]], *, content: str) -> None:
    """결과를 못 받은 호출 카드를 오류 결과로 닫는다(제자리). 오류로 끝난 턴의 마감이 쓴다.

    끊기거나 시간을 넘긴 턴은 결과가 오지 않으므로 그대로 두면 다시 그린 화면에 "실행 중" 카드가
    영영 남는다. 결과 모양은 tool_result 이벤트와 같고 status 는 error 다. 도구 쪽에서는 그 호출이
    끝났을 수 있지만 이 턴은 결과를 받지 못했다는 뜻이다.
    """
    for item in items:
        if item.get("kind") != KIND_TOOL_CALL or item["result"]:
            continue
        call = item["call"]
        result: dict[str, Any] = {
            "id": call.get("id"),
            "name": call.get("name"),
            "status": "error",
            "content": content,
        }
        if call.get("agent") is not None:
            result["agent"] = call["agent"]
        item["result"] = result


def _trim(items: list[dict[str, Any]]) -> None:
    if len(items) > TRANSCRIPT_LIMIT:
        del items[: len(items) - TRANSCRIPT_LIMIT]


def merge_resumed_calls(items: list[dict[str, Any]]) -> None:
    """읽어 낸 전사에서 재개 턴이 다시 알린 호출 카드를 앞 턴의 카드에 합친다(제자리).

    전사는 턴마다 따로 접어 덧붙이므로 승인을 기다리다 끊긴 턴의 카드(결과 없음)와 재개 턴이
    다시 알린 카드(결과 있음)가 서로 다른 묶음에 남는다. 쓰기를 덧붙이기로 두고 읽을 때 합쳐야
    새로고침한 화면이 스트림으로 본 화면(카드 하나)과 같다. 합친 카드는 뒤 카드의 인자와 결과,
    인용 출처를 갖고 뒤 카드는 빠진다.
    """
    merged: list[dict[str, Any]] = []
    for item in items:
        if item.get("kind") == KIND_TOOL_CALL:
            card = _merge_resumed_call(merged, item["call"])
            if card is not None:
                card["result"] = item["result"]
                if "citations" in item:
                    card["citations"] = item["citations"]
                continue
        merged.append(item)
    items[:] = merged


def fold_rewinds(items: list[dict[str, Any]]) -> None:
    """읽어 낸 전사의 다시 생성 표식을 접는다(제자리). `merge_resumed_calls` 보다 먼저 부른다.

    전사는 덧붙이기만 하므로 다시 생성이 버린 턴의 항목도 남아 있다. 표식마다 바로 앞 사용자
    항목까지 걷어 내면 체크포인트의 새 갈래와 같은 대화가 된다. 상한에 걸려 앞 사용자 항목이 이미
    잘렸으면 목록 처음까지만 걷는다. 인사말은 첫 사용자 항목 앞이라 걷히지 않는다.
    """
    folded: list[dict[str, Any]] = []
    for item in items:
        if item.get("kind") == KIND_REWIND:
            del folded[_last_index(folded, KIND_USER) or 0 :]
            continue
        folded.append(item)
    items[:] = folded


def resolve_interrupts(items: list[dict[str, Any]], *, pending: bool) -> None:
    """읽어 낸 전사의 승인 카드 상태를 채운다(제자리).

    `resolved` 는 저장하지 않고 읽을 때 정한다. 아직 열려 있는 카드는 세션이 지금 보류 중일 때의
    마지막 인터럽트 하나뿐이고 그 앞의 것은 모두 결정이 끝난 것이기 때문이다. 저장한 값을 나중에
    고치려 들면 덧붙이기만 하던 쓰기가 읽고 고쳐 쓰기가 되어 파드끼리 경합한다.
    """
    last = _last_index(items, KIND_INTERRUPT)
    for index, item in enumerate(items):
        if item.get("kind") == KIND_INTERRUPT:
            item["resolved"] = not (pending and index == last)


class TurnTranscript:
    """턴 하나가 화면에 낸 것을 모았다가 닫힌 것부터 저장소에 붙인다(P78 3절).

    턴은 그래프 조각이 끝날 때마다 앞에서부터 이어진 닫힌 항목(`closed_count`)을 붙이고 나머지는
    마감에서 한 번에 붙인다(`take`). 그래서 돌아온 사람이 도는 턴의 지난 항목을 본다. 닫힌 항목은
    다시 바뀌지 않는다. 흘러가는 말풍선(`token` 이 조각마다 이어 붙는다), 결과를 못 받은 호출 카드,
    그리고 그 뒤의 것은 메모리에 남는다. 그래서 인용(결과와 같은 조각에 온다)은 제 카드에 붙어 있고,
    병렬 호출은 앞의 것이 모두 닫힌 뒤 차례대로 붙는다. 붙이기에 실패한 항목은 버리지 않고(`drop` 을
    부르지 않는다) 다음 조각이나 마감에서 다시 붙인다. 파드가 턴 도중에 죽으면 그때까지 붙인 항목만
    남는다(체크포인트는 남는다).

    예비 모델 알림이 같은 턴에 겹치는지는 붙인 항목이 버퍼에서 빠져도 알도록 버퍼 밖에서 센다.

    다시 생성 턴(rewind)은 맨 앞에 표식을 두고 시작한다. 갈래가 생기기 전에 실패했으면 마감이
    `discard` 로 표식과 이번 턴 항목을 버려 마지막 항목만 붙게 한다. 갈래가 생기기 전에는 붙이지
    않는다(turn.py).
    """

    def __init__(self, *, rewind: bool = False) -> None:
        self._items: list[dict[str, Any]] = [{"kind": KIND_REWIND}] if rewind else []
        self._fallbacks: list[dict[str, Any]] = []

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._items

    def add_user(
        self, text: str, attachments: Sequence[dict[str, Any]] = (), *, turn_id: str | None = None
    ) -> None:
        self._items.append(user_item(text, attachments, turn_id=turn_id))
        _trim(self._items)

    def add(self, name: str, data: dict[str, Any]) -> None:
        if name == MODEL_FALLBACK:
            item = fallback_item(data)
            if item in self._fallbacks:
                return
            self._fallbacks.append(item)
        apply_event(self._items, name, data)

    def closed_count(self) -> int:
        """앞에서부터 이어진 닫힌 항목의 수다. 그래프 조각이 끝났을 때 부른다. 흘러가는 말풍선과
        결과를 못 받은 호출 카드에서 멈춘다."""
        for index, item in enumerate(self._items):
            if not _closed(item):
                return index
        return len(self._items)

    def drop(self, count: int) -> None:
        """붙인 앞 항목을 버퍼에서 뺀다. 붙이기에 성공했을 때만 부른다."""
        del self._items[:count]

    def close_open_calls(self, content: str) -> None:
        """결과를 못 받은 호출 카드를 오류 결과로 닫는다. 오류로 끝난 턴이 마지막 항목을 붙이기 전에
        부른다."""
        close_open_calls(self._items, content=content)

    def discard(self) -> None:
        """지금까지 모은 항목을 버린다. 갈래를 내기 전에 실패한 다시 생성 턴은 앞 턴이 그대로이므로
        표식도 사용자 항목도 붙이지 않는다."""
        self._items = []

    def take(self) -> list[dict[str, Any]]:
        """아직 붙이지 않은 항목을 떼어 준다. 흘러가던 말풍선은 닫는다.

        떼어 갔으므로 다시 부르면 빈 목록이다. 저장을 두 번 부르는 자리(정상 종료와 finally)가
        같은 항목을 두 번 붙이지 않게 한다.
        """
        _close_streaming(self._items)
        items = self._items
        self._items = []
        return items


def _closed(item: dict[str, Any]) -> bool:
    """더는 바뀌지 않는 항목인가. 흘러가는 말풍선과 결과를 못 받은 호출 카드만 열려 있다."""
    kind = item.get("kind")
    if kind == KIND_ASSISTANT:
        return not item.get("streaming")
    if kind == KIND_TOOL_CALL:
        return bool(item.get("result"))
    return True


class TranscriptStore(Protocol):
    async def append(self, session_id: str, items: Sequence[dict[str, Any]]) -> None:
        """항목을 세션 전사 끝에 붙이고 상한을 넘은 앞부분을 버린다. 원자적이어야 한다."""
        ...

    async def append_held(
        self, session_id: str, items: Sequence[dict[str, Any]], token: str
    ) -> None:
        """턴 자신의 붙이기다(P79 2절). 그 토큰이 살아 있는 리스를 쥐고 있을 때만 붙이고, 아니면
        `fence.refused()` 를 던진다."""
        ...

    async def read(self, session_id: str, *, limit: int = TRANSCRIPT_LIMIT) -> list[dict[str, Any]]:
        """오래된 것부터 차례로 돌려준다. 상한을 넘으면 뒤(최근) limit 개다."""
        ...

    async def delete(self, session_id: str) -> None: ...


class MemoryTranscripts:
    """파드 하나 안에서만 보이는 전사다. memory 노드가 없을 때 쓴다."""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}

    async def append(self, session_id: str, items: Sequence[dict[str, Any]]) -> None:
        if not items:
            return
        row = self._rows.setdefault(session_id, [])
        row.extend(items)
        if len(row) > TRANSCRIPT_LIMIT:
            del row[: len(row) - TRANSCRIPT_LIMIT]

    async def append_held(
        self, session_id: str, items: Sequence[dict[str, Any]], token: str
    ) -> None:
        """파드 안 전사는 같은 세션을 쓸 다른 파드가 없어 토큰을 보지 않는다."""
        await self.append(session_id, items)

    async def read(self, session_id: str, *, limit: int = TRANSCRIPT_LIMIT) -> list[dict[str, Any]]:
        return [dict(item) for item in self._rows.get(session_id, [])[-limit:]]

    async def delete(self, session_id: str) -> None:
        self._rows.pop(session_id, None)


# 표는 여기서 정의하고 세션 색인의 setup 이 같은 DB 에 만든다(sessions.py). 카운터 표와 같은
# 모양이다. session_id 는 uuid4 라 인스턴스로 나누지 않아도 섞이지 않고, 읽는 길은 인스턴스로
# 걸러진 색인을 먼저 지난다
TRANSCRIPTS_DDL = """
CREATE TABLE IF NOT EXISTS daegam_transcripts (
    seq        bigserial PRIMARY KEY,
    session_id text NOT NULL,
    item       jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS daegam_transcripts_session ON daegam_transcripts (session_id, seq);
"""

# 항목이 몇 개든 문장 하나다. 자리표 개수가 달라지면 prepare_threshold 0 인 풀이 개수마다 다른
# 준비문을 만든다. WITH ORDINALITY 로 넣는 차례를 못 박아 seq 가 전사 순서 그대로다
APPEND_SQL = """
INSERT INTO daegam_transcripts (session_id, item)
SELECT %s, t.value::jsonb FROM unnest(%s::text[]) WITH ORDINALITY AS t(value, ord) ORDER BY t.ord
"""
# 턴 자신의 붙이기다(P79 2절). 세션 행을 그 토큰과 살아 있는 리스를 조건으로 FOR KEY SHARE 로
# 먼저 잡는다. 그 잠금은 되살리기의 FOR UPDATE 와 부딪혀, 도는 붙이기는 되살리기가 전사를 읽기 전에
# 들어가거나 되살리기 뒤에 조건을 다시 보고 아무것도 넣지 않는다. 갱신의 잠금과는 부딪히지 않는다
HELD_APPEND_SQL = f"""
WITH s AS (
  SELECT session_id FROM daegam_sessions
   WHERE session_id = %s AND {HELD_LEASE_SQL}
     FOR KEY SHARE
)
INSERT INTO daegam_transcripts (session_id, item)
SELECT s.session_id, t.value::jsonb
  FROM s, unnest(%s::text[]) WITH ORDINALITY AS t(value, ord)
 ORDER BY t.ord
"""
# 뒤에서 TRANSCRIPT_LIMIT 번째보다 오래된 행을 지운다. 행이 상한 이하면 안쪽 SELECT 가 비어
# NULL 이고 그러면 아무 행도 지우지 않는다
TRIM_SQL = """
DELETE FROM daegam_transcripts
 WHERE session_id = %s
   AND seq <= (SELECT seq FROM daegam_transcripts WHERE session_id = %s
                ORDER BY seq DESC OFFSET %s LIMIT 1)
"""
READ_SQL = "SELECT item FROM daegam_transcripts WHERE session_id = %s ORDER BY seq DESC LIMIT %s"
DELETE_SQL = "DELETE FROM daegam_transcripts WHERE session_id = %s"


def dumps(item: dict[str, Any]) -> str:
    """저장용 JSON 한 줄이다. 한글을 그대로 두고 모르는 형은 문자열로 만든다."""
    return json.dumps(item, ensure_ascii=False, default=str)


class PostgresTranscripts:
    """memory 의 Postgres 에 두는 전사다. 세션 색인과 풀 하나를 나눠 쓴다.

    쓰기는 덧붙이기(INSERT)뿐이라 파드가 여럿이어도 서로의 항목을 지우거나 뒤섞지 않는다. 상한
    정리(TRIM)는 새로 넣은 것보다 오래된 행만 지우므로 같은 성질이다.
    """

    def __init__(self, pool: Any) -> None:
        # psycopg_pool.AsyncConnectionPool 이다. 색인과 같은 이유로 Any 다
        self._pool = pool

    async def append(self, session_id: str, items: Sequence[dict[str, Any]]) -> None:
        if not items:
            return
        async with self._pool.connection() as conn:
            await conn.execute(APPEND_SQL, (session_id, [dumps(item) for item in items]))
            await conn.execute(TRIM_SQL, (session_id, session_id, TRANSCRIPT_LIMIT))

    async def append_held(
        self, session_id: str, items: Sequence[dict[str, Any]], token: str
    ) -> None:
        """HELD_APPEND_SQL 이다. 넣은 행이 없으면 리스를 잃은 것이다."""
        if not items:
            return
        payload = [dumps(item) for item in items]
        async with self._pool.connection() as conn:
            cur = await conn.execute(HELD_APPEND_SQL, (session_id, token, payload))
            if cur.rowcount == 0:
                raise refused()
            await conn.execute(TRIM_SQL, (session_id, session_id, TRANSCRIPT_LIMIT))

    async def read(self, session_id: str, *, limit: int = TRANSCRIPT_LIMIT) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(READ_SQL, (session_id, limit))
            rows = await cur.fetchall()
        # 최근 limit 개를 뒤에서부터 읽었으므로 뒤집어 오래된 것부터 돌려준다
        return [row["item"] for row in reversed(rows)]

    async def delete(self, session_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(DELETE_SQL, (session_id,))


# 턴 자신의 붙이기다(P79 2절). 리스가 그 턴의 토큰일 때만 RPUSH 와 LTRIM 을 한다. 아니면 0 이다.
# KEYS 는 transcript:{sid}·turn_lease:{sid} 이고 ARGV 는 토큰, 상한, 붙일 항목들이다
HELD_APPEND_LUA = (
    HELD_LEASE_LUA
    + """redis.call('RPUSH', KEYS[1], unpack(ARGV, 3))
redis.call('LTRIM', KEYS[1], -tonumber(ARGV[2]), -1)
return 1
"""
)


def transcript_key(prefix: str, session_id: str) -> str:
    """세션 전사의 Redis 리스트 키다. 전사 저장소와 되살리기 스크립트(session_index_redis.py)가
    같이 쓴다."""
    return f"{prefix}transcript:{session_id}"


class RedisTranscripts:
    """memory 의 Redis 에 두는 전사다. 키는 접두어 아래 `transcript:{sid}` 리스트다.

    붙이기는 MULTI 하나에 RPUSH 와 LTRIM 이다. 둘이 한 묶음이라 파드가 여럿이어도 상한을 넘긴 채로
    남거나 남의 항목을 지우지 않는다. LTRIM 이 버리는 쪽이 앞(오래된 것)이다. 턴 자신의 붙이기는
    스크립트 하나(HELD_APPEND_LUA)가 리스의 토큰을 본 뒤에 같은 것을 한다.
    """

    def __init__(self, client: Any, prefix: str) -> None:
        # redis.asyncio.Redis 다. 색인·카운터와 클라이언트 하나를 나눠 쓴다
        self._client = client
        self._prefix = prefix
        self._held_append = client.register_script(HELD_APPEND_LUA)

    def _key(self, session_id: str) -> str:
        return transcript_key(self._prefix, session_id)

    async def append_held(
        self, session_id: str, items: Sequence[dict[str, Any]], token: str
    ) -> None:
        """HELD_APPEND_LUA 이다. 상한을 넘는 앞부분은 어차피 버려지므로 뒤의 상한 개만 실어
        스크립트의 인자 수를 묶는다(Lua 의 unpack 은 수천 개에서 넘친다). 빈 토큰은 검사를
        건너뛰게 되므로 받지 않는다."""
        if not token:
            raise ValueError("a held append needs the lease token")
        if not items:
            return
        appended = await self._held_append(
            keys=[self._key(session_id), turn_lease_key(self._prefix, session_id)],
            args=[token, TRANSCRIPT_LIMIT, *[dumps(item) for item in items[-TRANSCRIPT_LIMIT:]]],
        )
        if int(appended) != 1:
            raise refused()

    async def append(self, session_id: str, items: Sequence[dict[str, Any]]) -> None:
        if not items:
            return
        key = self._key(session_id)
        pipe = self._client.pipeline(transaction=True)
        pipe.rpush(key, *[dumps(item) for item in items])
        pipe.ltrim(key, -TRANSCRIPT_LIMIT, -1)
        await pipe.execute()

    async def read(self, session_id: str, *, limit: int = TRANSCRIPT_LIMIT) -> list[dict[str, Any]]:
        raw = await self._client.lrange(self._key(session_id), -limit, -1)
        return [json.loads(value) for value in raw]

    async def delete(self, session_id: str) -> None:
        await self._client.delete(self._key(session_id))


__all__ = [
    "TRANSCRIPTS_DDL",
    "TRANSCRIPT_LIMIT",
    "MemoryTranscripts",
    "PostgresTranscripts",
    "RedisTranscripts",
    "TranscriptStore",
    "TurnTranscript",
    "apply_event",
    "error_item",
    "fold_rewinds",
    "merge_resumed_calls",
    "resolve_interrupts",
    "transcript_key",
    "user_item",
]
