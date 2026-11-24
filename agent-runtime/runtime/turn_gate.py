"""턴 입구 검사와 턴 리스다. `/chat`, 결정 재개, 다시 생성(regenerate.py), 세션 관리
(sessions_api.py)가 같은 검사를 같은 오류 코드로 쓴다.

요청 검사(`check_request`)는 세션을 읽기 전에 돈다. 도구 준비, 새 세션의 drain 표시(P44 7절),
사용자별 분당 한도, 입력 길이와 입력 가드레일, 일당 토큰 예산 차례다. 세션을 읽은 뒤에는
소유자·상태(`check_open_session`)와 세션 예산(`check_session_budget`)을 보고, 턴
리스(`held_lease`)를 잡으면 이 시도의 번호와 id 를 실행 표식과 함께 색인에
적는다(`note_attempt`). 세션 한 줄을 읽는 `load_session` 은 끊긴 열린 표시를
검사 전에 되살린다(turn_repair.py, P79 3절). 세션을 볼 수 있는 주체인가(`check_caller`)와 파일 경로
검사(`checked_file_key`)도 여기 있어 세션 상세, 세션 파일(session_files.py), 기억 파일
(memories_api.py)이 같은 오류 코드로 쓴다. 거절은 모두 `{code, message}` 본문의 HTTPException 이다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from runtime import events as ev
from runtime import stream
from runtime.auth import Caller
from runtime.budget import ACTION_REJECTED, SCOPE_DAILY, SCOPE_SESSION
from runtime.cancel import SHUTDOWN
from runtime.counters import daily_tokens_key, day_window, minute_window, rate_key
from runtime.fence import TurnFenced, fenced
from runtime.guards import POSITION_INPUT, GuardrailBlocked, Violation
from runtime.hitl import DecisionsRequest, check_edited_args
from runtime.lease_keeper import HeldLease
from runtime.sessions import (
    STATUS_CLOSED,
    STATUS_PENDING,
    DrainMarked,
    SessionRecord,
    new_turn_token,
    utcnow,
)
from runtime.state import RuntimeState
from runtime.trace import RunTracer
from runtime.turn_record import RunMarker, TurnAttempt

log = logging.getLogger("daegam.agent-runtime.turn_gate")

# 한 턴 입력 본문의 상한이다. 붙여 넣은 문서 몇 장이고 가드레일 정규식과 토크나이저가 한 번에 받는
# 크기의 천장이다. 입구의 max_input_chars 는 이 안에서 에이전트마다 정한다. 요청 모델의 칸 상한이
# 아니라 입력 길이 검사(`check_input_length`)가 보므로 넘으면 413 max_input_chars 이고 거절로 센다
# (P76 F2)
MESSAGE_MAX_CHARS = 200_000

# 읽은 도구 목록으로 아직 그래프를 컴파일하지 못했다(T18). 준비 검사와 턴 입구가 같은 코드로
# 거절한다. 다시 읽기는 배경 루프가 하므로 클라이언트에는 잠시 뒤 다시 보내라고 한다
TOOLS_UNAVAILABLE = "tools_unavailable"
# 백그라운드 실행의 세션에 새 말이나 다시 생성을 보냈다(P41 9절, runs.py). 작업의 다음 실행을
# 작업 세션이 아닌 세션에 보낸 것도 같은 코드다
RUN_SESSION = "run_session"
RUN_SESSION_MESSAGE = "백그라운드 실행의 세션은 새 말도 다시 생성도 받지 않는다"
NOT_TASK_SESSION_MESSAGE = "작업 세션이 아닌 세션은 작업의 다음 실행을 받지 않는다"
TOOLS_UNAVAILABLE_MESSAGE = "Tool 을 아직 준비하지 못했다"
# 곧 받을 수 있는데 지금은 거절한 요청에 다는 Retry-After 다. 준비되지 않은 런타임(T18·T22, 배경
# 다시 시도가 따로 돈다)과 drain 표시가 선 인스턴스(P44 7절, 플랫폼이 깨운다)가 같이 쓴다
UNREADY_RETRY_AFTER_SECONDS = 5
# 인스턴스의 drain 표시가 턴을 물렸다(P44 7절). 플랫폼이 agent_waking 으로 바꿔 답하고, 다시 보낸
# 턴이 Agent 를 깨운다
AGENT_DRAINING = "agent_draining"
AGENT_DRAINING_MESSAGE = "Agent 를 재우는 중이라 턴을 받지 않는다"

# 파일 경로의 상한(글자)이다
FILE_KEY_MAX_CHARS = 512
# 파일 경로에 둘 수 없는 조각이다
_DOT_SEGMENTS = frozenset({".", ".."})


def error(
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTTPException:
    return HTTPException(
        status, detail={"code": code, "message": message, **extra}, headers=headers
    )


def rejected(
    state: RuntimeState,
    status: int,
    code: str,
    message: str,
    *,
    user_sub: str | None,
    session_id: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTTPException:
    """제 이벤트가 없는 입구의 거절(413 max_input_chars·attachments_too_large, 422 첨부 검사, 429
    max_turns·max_concurrent_sessions·max_regenerations, 503 tools_unavailable)이다.
    `rejected{code}` 를 남긴 오류를 돌려준다. extra 는 `error` 처럼 응답 본문에만 붙는다. 플랫폼의
    지표가 거절을 이 이벤트와 제 이벤트(분당 한도, 예산, 입력 가드레일)로 센다(P37)."""
    state.events.emit(ev.REJECTED, code=code, user_sub=user_sub, session_id=session_id)
    return error(status, code, message, headers=headers, **extra)


def session_not_found() -> HTTPException:
    return error(404, "session_not_found", "세션이 없다")


async def load_session(state: RuntimeState, session_id: str) -> SessionRecord:
    """색인의 세션 한 줄이다. 없으면 404 다. 끊긴 열린 표시가 있으면 돌려주기 전에 되살린다(P79
    3절). 세션을 가리키는 모든 라우트가 검사 전에 부르므로 409 검사가 고친 상태를 본다. 주체 검사
    전이라 403 을 받을 호출자도 쓰기를 일으키지만, 되살리기는 멱등이고 끝난 리스의 표시만
    건드린다.

    세션 TTL 이 지났고 살아 있는 리스가 잡고 있지 않은 열린 세션은 닫힌 것으로 돌려준다(P44
    11절). 쓰지는 않는다. 닫기는 sweep 의 `SessionStore.close_expired` 가 체크포인트·첨부·샌드박스를
    지우고 session_expired 를 내며 한다. 그래서 잠든 동안 TTL 이 지난 세션에 깨어난 파드의 첫
    sweep 이 끝나기 전에도 새 턴, 다시 생성, 결정, 보류 읽기, 파일 읽기가 닿지 않는다."""
    record = await state.store.index.get(session_id)
    if record is not None and record.orphan is not None:
        record = await state.repairs.on_read(state, record)
    if record is None:
        raise session_not_found()
    if record.status != STATUS_CLOSED and not record.running and past_ttl(state, record):
        return replace(record, status=STATUS_CLOSED)
    return record


def past_ttl(state: RuntimeState, record: SessionRecord) -> bool:
    """마지막 턴이 세션 TTL 보다 오래됐는가(P44 11절). sweep 의 닫기와 같은 비교다."""
    ttl = timedelta(seconds=state.config.entry["session_ttl_seconds"])
    return record.last_turn_at < utcnow() - ttl


async def _reload_after_refused_claim(state: RuntimeState, session_id: str) -> None:
    """잡기가 실패한 요청이 세션을 다시 읽어 끊긴 표시를 되살린다(P79 1절). 다시 잡지는 않으므로
    라우트의 검사를 건너뛰지 않고, 다시 보내면 된다. 읽기가 실패해도 답은 409 그대로다."""
    try:
        record = await state.store.index.get(session_id)
        if record is not None:
            await state.repairs.on_read(state, record)
    except Exception as e:  # noqa: BLE001  다음 읽기가 다시 되살린다
        log.warning("세션 %s 를 다시 읽지 못했다: %s", session_id, e)


def check_owner(caller: Caller, record: SessionRecord) -> None:
    """세션 소유자만 받는다. 아니면 승인자라도 403 이다. 새 턴, 다시 생성, 이름 바꾸기와 지우기가
    이 검사를 쓴다. 남의 대화에 말을 보태거나 그 대화를 바꿀 까닭이 없기 때문이다."""
    if record.user_sub != caller.sub:
        raise error(403, "not_session_owner", "세션 소유자가 아니다")


def check_caller(caller: Caller, record: SessionRecord, *, or_approver: bool = False) -> None:
    """세션을 볼 수 있는 주체인지 본다. 아니면 403 이다.

    소유자는 언제나 볼 수 있다. or_approver 면 플랫폼이 `X-Daegam-Approver` 로 표시한 승인자도
    받는다(결정 보내기, 턴 중지, 세션 파일이 그렇다). 그 밖에는 외부 경로만 막는다. 프록시 경로의
    소유자 검사는 플랫폼이 이미 했고 caller.sub 도 플랫폼이 실어 준 값이기 때문이다. 외부 경로는
    approver 가 언제나 거짓이라 어느 쪽이든 소유자만 남는다(D27).
    """
    exempt = caller.approver if or_approver else not caller.external
    if not exempt:
        check_owner(caller, record)


def session_closed() -> HTTPException:
    return error(410, "session_closed", "세션이 닫혔다")


def checked_file_key(key: str) -> str:
    """라우트 경로의 파일 키(`notes/a.md`)를 체크포인트의 파일 목록과 첨부 저장소, 장기 기억
    저장소의 키(`/notes/a.md`)로 옮긴다. 키는 글자 그대로 맞춰 찾으므로 디렉터리를 거슬러 오를 길이
    없지만, 빈 키, `.`·`..` 조각, NUL, 상한을 넘는 키는 먼저 422 invalid_path 로 거절한다."""
    if (
        not key
        or "\x00" in key
        or len(key) > FILE_KEY_MAX_CHARS
        or any(part in _DOT_SEGMENTS for part in key.split("/"))
    ):
        raise error(
            422,
            "invalid_path",
            f"파일 경로는 1~{FILE_KEY_MAX_CHARS}자이고 '.'·'..' 조각과 NUL 이 없어야 한다",
        )
    return f"/{key}"


def check_open_session(caller: Caller, record: SessionRecord, *, next_run: bool = False) -> None:
    """새 턴을 받을 세션인지 본다. 소유자가 아니면 403(`check_owner`), 실행 세션이면 409
    `run_session`(P41 9절. 실행은 결정으로만 이어 간다), 닫혔으면 410, 승인 대기면 409 다.
    next_run 은 작업의 다음 실행이다(runs.py). 작업 세션만 받고 다른 세션이면 409 `run_session`
    이다."""
    check_owner(caller, record)
    if next_run and not record.task:
        raise error(409, RUN_SESSION, NOT_TASK_SESSION_MESSAGE)
    if record.run_id is not None and not next_run:
        raise error(409, RUN_SESSION, RUN_SESSION_MESSAGE)
    if record.status == STATUS_CLOSED:
        raise session_closed()
    if record.status == STATUS_PENDING:
        raise error(409, "session_pending", "승인 대기 중인 세션이다. 결정을 먼저 보내야 한다")


def check_still_open(state: RuntimeState, record: SessionRecord, *, resume: bool) -> None:
    """리스를 잡은 뒤 다시 읽은 세션이 아직 이 턴을 받는가. 입구의 검사와 리스 사이에 TTL 이
    닫았거나 지났으면 410, 재개(resume)인데 그사이 다른 결정이 대기를 끝냈으면 409 `not_pending`
    이다. 다시 읽은 한 줄은 이 턴의 리스가 잡고 있어 `load_session` 이 닫힌 것으로 읽지 않으므로
    TTL 을 여기서 다시 본다(P44 11절). 잡기 앞에는 살아 있는 리스가 없었다."""
    if record.status == STATUS_CLOSED or past_ttl(state, record):
        raise session_closed()
    if resume and record.status != STATUS_PENDING:
        raise error(409, "not_pending", "승인 대기 중인 세션이 아니다")


def check_session_budget(state: RuntimeState, record: SessionRecord) -> None:
    """service.budget.session_tokens 다. 색인의 누적 사용량이 예산 이상이면 429 다."""
    budget = state.agent.session_budget
    used = record.usage_input + record.usage_output
    if budget is None or used < budget:
        return
    state.events.emit(
        ev.BUDGET,
        session_id=record.session_id,
        user_sub=record.user_sub,
        scope=SCOPE_SESSION,
        action=ACTION_REJECTED,
        used=used,
        limit=budget,
    )
    raise error(
        429, "session_budget_exceeded", f"세션 토큰 예산 {budget} 을 다 썼다. 새 세션을 열어야 한다"
    )


async def check_request(
    state: RuntimeState,
    caller: Caller,
    session_id: str | None,
    message: str | None,
    *,
    per_minute: bool = True,
) -> list[Violation]:
    """세션을 읽기 전의 요청 검사다. `/chat` 과 다시 생성이 같은 차례로 부른다.

    도구 준비, 분당 한도, 입력 길이와 입력 가드레일(message 가 있을 때), 일당 예산 차례다.
    per_minute 가 거짓이면 분당 한도를 건너뛴다. 백그라운드 실행의 시작이 그렇다(P41 9절. 사람이
    보낸 요청이 아니고, 세션과 일당 예산은 그대로 걸린다). 돌려주는 것은 SSE 앞머리에 실을 flag
    위반이다.

    새 세션이면(session_id 가 없다) 도구 준비 다음에 drain 표시를 본다(P44 7절). 세션 행을 만들기
    전에 물려 재우기 직전의 세션 수와 동시 세션 상한에 빈 세션이 들지 않게 한다. 이어지는 세션은
    리스 잡기가 같은 표시로 물린다.
    """
    check_tools_ready(state, caller, session_id)
    if not session_id and await state.store.index.drain_marked():
        raise agent_draining()
    if per_minute:
        await check_rate_limit(state, caller, session_id)
    flagged = check_input(state, caller, message, session_id) if message is not None else []
    await check_daily_budget(state, caller, session_id)
    return flagged


def check_tools_ready(state: RuntimeState, caller: Caller, session_id: str | None) -> None:
    """도구 목록으로 그래프를 컴파일하기 전에는 턴을 받지 않는다(T18). 도구 없는 그래프는 도구가
    있어야 할 답을 지어낸다. 준비 검사가 503 이라 Service 로는 오지 않고, 파드로 바로 들어온 요청만
    여기서 막힌다."""
    if not state.tools_ready:
        raise rejected(
            state,
            503,
            TOOLS_UNAVAILABLE,
            TOOLS_UNAVAILABLE_MESSAGE,
            user_sub=caller.sub,
            session_id=session_id,
            headers={"Retry-After": str(UNREADY_RETRY_AFTER_SECONDS)},
        )


async def check_rate_limit(state: RuntimeState, caller: Caller, session_id: str | None) -> None:
    """entry.rate_limit.per_user_per_minute 다. 공유 카운터를 먼저 올리고 넘으면 429 다.

    거절된 요청도 창 안 횟수에 들어간다(고정 창). Retry-After 는 창이 끝날 때까지의 초다.
    """
    limit = state.config.entry["rate_limit"]
    if not limit:
        return
    per_minute = int(limit["per_user_per_minute"])
    now = utcnow()
    window = minute_window(now)
    key = rate_key(state.config.instance["id"], caller.sub, window)
    count = await state.store.counters.incr(key, 1, window=window)
    if count <= per_minute:
        return
    retry_after = window.retry_after(now)
    state.events.emit(
        ev.RATE_LIMITED,
        session_id=session_id,
        user_sub=caller.sub,
        limit=per_minute,
        count=count,
        retry_after=retry_after,
        window=window.suffix,
    )
    raise error(
        429,
        "rate_limited",
        f"사용자별 분당 {per_minute} 요청을 넘었다",
        headers={"Retry-After": str(retry_after)},
    )


async def check_daily_budget(state: RuntimeState, caller: Caller, session_id: str | None) -> None:
    """service.budget.daily_tokens 다. 오늘(UTC) 쓴 토큰이 한도 이상이면 429 다."""
    limit = state.config.service["budget"]["daily_tokens"]
    if not limit:
        return
    now = utcnow()
    window = day_window(now)
    used = await state.store.counters.get(daily_tokens_key(state.config.instance["id"], window))
    if used < limit:
        return
    retry_after = window.retry_after(now)
    state.events.emit(
        ev.BUDGET,
        session_id=session_id,
        user_sub=caller.sub,
        scope=SCOPE_DAILY,
        action=ACTION_REJECTED,
        used=used,
        limit=limit,
        retry_after=retry_after,
    )
    raise error(
        429,
        "daily_budget_exceeded",
        f"오늘의 토큰 예산 {limit} 을 다 썼다",
        headers={"Retry-After": str(retry_after)},
    )


def check_input(
    state: RuntimeState, caller: Caller, text: str, session_id: str | None
) -> list[Violation]:
    """턴에 실을 글의 입력 길이와 입력 가드레일이다. 넘거나 block 이면 거절하고 flag 위반을
    돌려준다."""
    check_input_length(state, text, user_sub=caller.sub, session_id=session_id)
    return check_input_guard(state, caller, text, session_id)


def check_input_length(
    state: RuntimeState, message: str, *, user_sub: str, session_id: str | None
) -> None:
    """entry.max_input_chars 와 천장 MESSAGE_MAX_CHARS 중 작은 쪽이다. 넘으면 413 이다."""
    limit = min(state.config.entry["max_input_chars"], MESSAGE_MAX_CHARS)
    if len(message) > limit:
        raise rejected(
            state,
            413,
            "max_input_chars",
            f"입력이 {limit} 자를 넘는다",
            user_sub=user_sub,
            session_id=session_id,
        )


def check_input_guard(
    state: RuntimeState,
    caller: Caller,
    message: str,
    session_id: str | None,
    *,
    attachment: str | None = None,
) -> list[Violation]:
    """position: input 가드레일이다. block 은 422 이고 flag 는 SSE 앞머리에 실을 위반 목록이다.
    attachment 는 첨부 글을 볼 때 그 이름이다(attachments.py). 길이 규칙 없이 보고 422 와 위반에
    이름이 붙는다."""
    try:
        return state.agent.input_guard.check(message, attachment=attachment)
    except GuardrailBlocked as e:
        raise _blocked(state, e, session_id=session_id, user_sub=caller.sub) from e


def check_decision_messages(
    state: RuntimeState, caller: Caller, session_id: str, body: DecisionsRequest
) -> None:
    """거절·응답 결정의 글(`message`)도 모델에 가는 사용자의 말이라 입력과 같은 길이 상한을 본다
    (P76 F2). 승인·고치기의 글은 모델에 가지 않으므로 보지 않는다. 가드레일은 보지 않는다."""
    for decision in body.decisions:
        # approve·edit 의 message 는 재개에 싣지 않는다(Decision.to_resume)
        if decision.type in ("reject", "respond") and decision.message is not None:
            check_input_length(state, decision.message, user_sub=caller.sub, session_id=session_id)


def check_decision_args(
    state: RuntimeState, caller: Caller, session_id: str, body: DecisionsRequest
) -> list[Violation]:
    """edit 결정의 고친 인자에 대는 도구 가드레일이다(D65). 입력 검사와 같이 block 은 422 이고
    flag 는 재개 스트림 앞머리에 실을 위반 목록이다. 메인의 호출이면 실행 때 ToolGuard 가 같은
    인자를 한 번 더 보므로 flag 가 두 번 날 수 있다. 서브에이전트의 호출은 여기가 유일한 검사다."""
    try:
        return check_edited_args(body.decisions, state.agent.tool_guard)
    except GuardrailBlocked as e:
        raise _blocked(state, e, session_id=session_id, user_sub=caller.sub) from e


def _blocked(
    state: RuntimeState, e: GuardrailBlocked, *, session_id: str | None, user_sub: str
) -> HTTPException:
    """입구 검사의 block 위반이다. 위반 본문을 관측 이벤트로 남긴 뒤 422
    guardrail_blocked(rule 포함)를 돌려준다. 입력, 첨부 글, 결정의 고친 인자가 같은 꼴이고 첨부
    글이면 그 이름(`attachment`)이 붙는다. 플랫폼은 입력 자리의 block 만 거절로 세므로, 고친 인자의
    위반(Tool 자리)은 `rejected` 를 하나 더 남긴다."""
    violation = e.violation.to_dict()
    state.events.emit(
        ev.GUARDRAIL, session_id=session_id, user_sub=user_sub, **state.events.scrub(violation)
    )
    if e.violation.position != POSITION_INPUT:
        state.events.emit(
            ev.REJECTED, code="guardrail_blocked", user_sub=user_sub, session_id=session_id
        )
    named = {"attachment": e.violation.attachment} if e.violation.attachment else {}
    return error(422, "guardrail_blocked", e.message, rule=e.rule, **named)


def guardrail_prelude(violations: Iterable[Violation]) -> list[tuple[str, dict[str, Any]]]:
    """입구 검사가 돌려준 flag 위반을 SSE 앞머리의 guardrail 이벤트로 옮긴다."""
    return [(stream.GUARDRAIL, v.to_dict()) for v in violations]


def turn_aborted() -> HTTPException:
    """턴 태스크를 띄우기 전에 리스를 잃은 요청의 답이다(P79 2절). 다시 보내면 된다."""
    return error(503, "turn_aborted", SHUTDOWN.detail)


def agent_draining() -> HTTPException:
    """인스턴스의 drain 표시가 선 동안의 턴 거절이다(P44 7절). 아무것도 쓰지 않았으므로 다시
    보내면 된다. 거절로 세지 않는다. 플랫폼이 Agent 를 깨운 뒤 같은 턴을 다시 보내기 때문이다."""
    return error(
        503,
        AGENT_DRAINING,
        AGENT_DRAINING_MESSAGE,
        headers={"Retry-After": str(UNREADY_RETRY_AFTER_SECONDS)},
    )


@asynccontextmanager
async def held_lease(
    state: RuntimeState,
    session_id: str,
    *,
    on_fence: Callable[[], HTTPException] = turn_aborted,
    on_orphan: Callable[[RunMarker], None] | None = None,
) -> AsyncIterator[HeldLease]:
    """세션의 턴 리스를 잡아 리스 관리자(lease_keeper.py)에 맡기고 그 리스를 준다. 안의 일은 그
    리스의 펜스 문맥에서 돈다(fence.py). 안의 일이 실패하면 풀고, 끝까지 가면 턴 태스크가 넘겨받은
    리스(`HeldLease.kept`)만 그대로 둔다.

    턴, 다시 생성, 재개, 세션 지우기(sessions_api.py)가 같은 60초로 잡고, 못 잡으면 같은 409
    turn_in_progress 로 거절한다. 관리자가 10초마다 늘리므로 리스는 턴 제한 시간과 상관없고, 파드가
    죽으면 세션은 1분 안에 풀린다. 턴의 리스는 다 돈 뒤 turn.py 의 `_run_turn` 이 풀고, 지운 세션의
    리스는 색인 행과 함께 사라진다.

    안의 일이 펜스에 막히면(`TurnFenced`) 리스를 잃은 것이라 세션에 더 쓰지 않고(풀기도 하지 않는다)
    on_fence 의 답을 낸다. 턴은 503 turn_aborted, 지우기는 503 session_delete_incomplete 다. 파드 안
    색인(memory 노드 없음)은 리스가 만료되지 않고 다른 파드도 없으므로 그때도 푼다.

    진행 중 턴 막기는 색인이 한다. 파드 로컬 집합으로 막으면 replicas 가 둘 이상일 때 같은 세션의
    두 요청이 다른 파드에 걸려 같은 thread 에 동시에 쓴다. 리스에 적는 토큰이 이 턴의 중지 표시를
    다음 턴과 가른다(D65).

    잡기는 끊긴 열린 표시가 남은 세션을 잡지 않는다(P79 1절). 잡지 못한 요청은 세션을 다시 읽어
    되살리고 409 로 답한다. on_orphan 을 주면(지우기) 리스가 살아 있지 않은 한 끊긴 표시 위에서도
    잡고, 그 토큰의 실행 표식이 있으면 그것으로 on_orphan 을 부른다. 지우기가 되살리기가 고칠 것을
    모두 지우기 때문이다.

    인스턴스의 drain 표시가 서 있으면 어느 잡기든 503 agent_draining 이다(P44 7절). 그 오류의
    원인(`__cause__`)이 `DrainMarked` 라 새 채팅 세션을 지우는 쪽(turn.py)이 그것으로 가른다.
    """
    token = new_turn_token()
    index = state.store.index
    sent_at = asyncio.get_running_loop().time()
    lease_seconds = state.leases.lease_seconds
    marker: RunMarker | None = None
    try:
        if on_orphan is None:
            claimed = await index.claim_turn(session_id, lease_seconds=lease_seconds, token=token)
        else:
            claimed, marker = await index.claim_delete(
                session_id, lease_seconds=lease_seconds, token=token
            )
    except DrainMarked as e:
        raise agent_draining() from e
    if not claimed:
        await _reload_after_refused_claim(state, session_id)
        raise error(409, "turn_in_progress", "이 세션의 턴이 아직 진행 중이다")
    lease = state.leases.hold(session_id, token, sent_at=sent_at)
    try:
        if marker is not None and on_orphan is not None:
            on_orphan(marker)
        with fenced(lease.fence):
            yield lease
    except BaseException as e:
        state.leases.drop(lease)
        # 리스를 잃었으면 풀지 않는다. 파드 안 색인만은 리스가 만료되지 않으므로 푼다
        if not lease.fence.standing or not state.store.shared:
            await index.release_turn(session_id, token)
        if isinstance(e, TurnFenced):
            raise on_fence() from e
        raise
    if not lease.kept:
        state.leases.drop(lease)


async def note_attempt(
    state: RuntimeState,
    record: SessionRecord,
    attempt: TurnAttempt,
    *,
    token: str,
    kind: str,
    trace: RunTracer,
) -> None:
    """이 시도의 번호와 id 를 색인에 적는다(turn_record.py). 리스를 잡은 직후 부른다. 같은 쓰기가
    실행 표식(`RunMarker`)을 적으므로 칸이 이미 같은 재개도 언제나 쓴다(P79 3절). kind 는 시도의
    종류이고 trace 는 그 실행의 트레이서다."""
    run = RunMarker(
        token=token,
        turn_id=attempt.turn_id,
        turn=attempt.number,
        kind=kind,
        trace_id=trace.trace_id,
        span_id=trace.span_id,
        start=utcnow(),
    )
    await state.store.index.update_held(
        record.session_id, token, run=run, **attempt.index_fields(record)
    )
