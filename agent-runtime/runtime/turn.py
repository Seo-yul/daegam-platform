"""한 턴의 수명이다(P76 F3). 세션 고르기, 턴 열기(리스·첨부·시도 기록), 그래프 실행과 SSE, 마감
(색인·전사·일당 토큰·리스 풀기)과 턴 로그다. `/chat` 과 결정 재개(routes.py), 다시 생성
(regenerate.py)이 이 모듈로 턴을 연다. 라우트와 요청 모델, 살아 있음·지표 답은 routes.py 에 있다.

턴은 SSE 로 낸 것을 그대로 ChatItem 으로 접어 세션 전사에 붙인다(P18 F1). 이벤트 계약은 그대로이고
저장이 늘 뿐이다. `GET /sessions/{sid}` 의 `messages` 요약도 그대로 두고 `transcript` 를 더한다.

턴 시도마다 `turn_id` 를 내어 `done`·`error`·전사·관측 이벤트에 싣는다. 요청 하나는 실행 하나이고
트레이스 하나다(P37). 실행의 트레이서(`TurnState.trace`, trace.py)가 모델 호출과 도구 실행의 스팬을
내고, `turn_start`·`turn_end` 는 실행 스팬의 id 를, 실행 안의 점 이벤트는 트레이스 id 와 부모 스팬
id 를 든다. `done`·`error` 와 전사도 `trace_id` 를 든다. 새 세션의 제목, 시도의 번호와 id 를 정하는
규칙은 turn_record.py 에 있고 여기서는 부르기만 한다(P36 3절). 입구 검사와 턴 리스는 turn_gate.py 에
있다. 다시 생성(regenerate.py)도 여기의 턴 실행(`open_turn`)으로 열고, 갈래를 낸 턴의 마감만
`Fork`(fork.py)가 더한다. 첨부의 검사·저장·안내는 attachments.py 에 있고 턴을 열 때 세션의 첨부를 한
번 읽어 턴 문맥에 둔다.

턴은 요청과 떨어진 태스크다(D92, turn_task.py). `open_turn` 이 리스와 시도 기록을 잡은 뒤 태스크를
띄우고, 응답은 그 이벤트 큐를 읽기만 한다. 연결이 끊겨도 읽기만 멈추고 턴은 끝까지 간다. 턴을 멈추는
길은 중지 요청(`/cancel`, cancel.py), 갱신이 거절된 사람(turn_grants.py), 파드 종료(drain.py)이고,
셋 모두 그 까닭의 코드로 오류 턴처럼 마감한다. 끝난 모델 호출의 사용량은 색인과 일일 카운터에 넣고,
전사에 그 `error` 를 붙이며, 아직 붙어 있는 읽기에 그것을 마지막 이벤트로 낸다. 첫 턴도 멈출 수 있게
SSE 응답 헤더 `X-Daegam-Session-Id` 가 세션 id 를, `X-Daegam-Turn-Id` 가 이 시도의 id 를 알리고,
`X-Daegam-Detached-Turn: 1` 이 이 파드가 턴을 떼어 돌린다는 것을 알린다(플랫폼의 위임 끝내기).

백그라운드 실행의 세션(P41 9절, runs.py)은 턴마다 임시로 돌고, 응답 머리 `X-Daegam-Run-Id` 와
`turn_start`·`turn_end` 의 `run_id` 를 싣는다. 승인 대기 없이 끝난 턴은 세션을 닫는다. 색인은
마감이 closed 로 적고, 체크포인트와 첨부는 리스를 푼 뒤 지우며, 전사는 남는다. `turn_end` 는 오류
턴의 코드(`code`)도 싣는다. 플랫폼이 중지와 접근을 잃은 사람을 그것으로 가른다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager, nullcontext
from datetime import datetime
from functools import partial
from typing import Any

import anyio
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from starlette.types import Receive, Scope, Send

from runtime import events as ev
from runtime import stream
from runtime.approvals import ApprovalChoice, choices_of
from runtime.attachments import (
    load_turn_uploads,
    observed,
    receive_attachments,
    turn_attachments,
    typed_text,
)
from runtime.auth import Caller
from runtime.cancel import (
    ACCESS_LOST,
    LEASE_LOST,
    SHUTDOWN,
    TURN_ABORTED,
    TURN_ACCESS_LOST,
    TURN_CANCELLED,
    CancelWatch,
)
from runtime.citations import ledger_for
from runtime.counters import daily_tokens_key, day_window
from runtime.events import EventLog
from runtime.fence import TurnFenced, fenced
from runtime.fork import Fork
from runtime.lease_keeper import HeldLease
from runtime.outside import OutsideFlag
from runtime.runs import run_exists, run_headers
from runtime.sessions import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_PENDING,
    DrainMarked,
    RunTaken,
    SessionRecord,
    utcnow,
)
from runtime.state import (
    RuntimeState,
)
from runtime.trace import RunTracer
from runtime.transcript import (
    TurnTranscript,
)
from runtime.turn_gate import (
    check_open_session,
    check_still_open,
    held_lease,
    load_session,
    note_attempt,
    rejected,
    turn_aborted,
)
from runtime.turn_grants import (
    END_CANCELLED,
    END_DONE,
    END_ERROR,
    END_INTERRUPT,
    END_SHUTDOWN,
    TURN_GRANT_HEADER,
    TurnGrant,
    TurnRef,
)
from runtime.turn_record import (
    RUN_CHAT,
    RUN_RESUME,
    TurnAttempt,
    cite_seq_fields,
    title_from_message,
)
from runtime.turn_task import start_turn_task
from runtime.uploads import StoredUpload
from runtime.web_guard import WebBudget

log = logging.getLogger("daegam.agent-runtime.turn")

# 끊긴 턴의 마감(색인·전사·리스)에 주는 시간이다. 저장소가 멈췄을 때 태스크가 끝없이 남지 않게
# 묶고, 넘기면 리스 만료가 마지막 그물이다
FINISH_GRACE_SECONDS = 10.0
# 조각마다의 전사 붙이기가 실패한 뒤 다시 붙이기까지 기다리는 시간이다. 그동안의 조각 끝 표시는
# 붙이기를 깨우지 않으므로 흔들리는 저장소에 토큰마다 붙이고 로그를 남기지 않는다
SAVE_RETRY_SECONDS = 5.0

# 사람이 멈춘 턴(중지, 접근을 잃음)의 turn_end status 다. error 이벤트의 code 는 그대로이고 P37 이
# 이 값을 읽어 오류율과 지연 표본에서 뺀다
TURN_END_CANCELLED = "cancelled"

# SSE 응답 헤더다. 첫 턴은 done 전에는 세션 id 를 모르므로 헤더로 알려 `/cancel` 을 부를 수 있게
# 한다. 시도의 id 는 중지가 그 시도만 멈추게 하고, 떼어 낸 턴의 표시는 플랫폼이 연결이 닫혀도
# 위임을 끝내지 않게 한다(P78). 노출 경로의 CORS 가 셋 다 드러낸다(main.py)
SESSION_HEADER = "X-Daegam-Session-Id"
TURN_ID_HEADER = "X-Daegam-Turn-Id"
DETACHED_HEADER = "X-Daegam-Detached-Turn"


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


class ClosingStreamingResponse(StreamingResponse):
    """응답이 끝나는 길마다 본문(턴의 이벤트 큐 읽기, turn_task.py)을 닫는 SSE 응답이다.

    Starlette 는 연결이 끊기면 응답 태스크 그룹만 취소하고 body_iterator 를 닫지 않는다. 여기서는
    finally 에서 읽기를 닫아 그 뒤의 이벤트를 버리고 `on_close` 가 한 번 불리게 한다. 턴 태스크는
    그대로 간다(D92).
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()


async def _charge_daily(state: RuntimeState, turn: stream.TurnState, now: datetime) -> None:
    """턴이 쓴 토큰을 오늘의 공유 카운터에 더한다. 끊긴 턴의 토큰도 쓴 것이다. 리스를 잃은 턴도
    쓴 토큰은 더하므로 턴의 펜스 문맥 밖에서 더한다(P79 2절). 세션이 아니라 인스턴스의 카운터다.
    한 턴에 한 번이다(`TurnState.charged`)."""
    if turn.charged:
        return
    turn.charged = True
    if not state.config.service["budget"]["daily_tokens"]:
        return
    spent = turn.usage.input_tokens + turn.usage.output_tokens
    if spent <= 0:
        return
    window = day_window(now)
    key = daily_tokens_key(state.config.instance["id"], window)
    try:
        with fenced(None):
            await state.store.counters.incr(key, spent, window=window)
    except Exception as e:  # noqa: BLE001  카운터 장애가 턴 결과를 막지는 않는다
        log.warning("일당 토큰 카운터 갱신 실패: %s", e)


async def resolve_session(
    state: RuntimeState,
    caller: Caller,
    session_id: str | None,
    message: str,
    *,
    run_id: str | None = None,
    untrusted: bool = False,
) -> SessionRecord:
    """기존 세션이면 상태·소유자·턴 한도를 검사하고, 없으면 새로 만든다. 소유자는 caller.sub 다.

    run_id 를 주면 백그라운드 실행의 세션을 만든다(P41 9절, runs.py). 색인이 run_id 를 한 세션에만
    주므로 같은 실행의 요청이 함께 와도 하나만 만들고, 진 쪽은 409 `run_exists` 다."""
    entry = state.config.entry
    index = state.store.index
    if session_id:
        record = await load_session(state, session_id)
        check_open_session(caller, record)
        if record.turns >= entry["max_turns"]:
            raise rejected(
                state,
                429,
                "max_turns",
                f"세션당 최대 {entry['max_turns']} 턴을 다 썼다",
                user_sub=caller.sub,
                session_id=session_id,
            )
        return record
    counts = await index.counts()
    active = counts.get(STATUS_ACTIVE, 0) + counts.get(STATUS_PENDING, 0)
    if active >= entry["max_concurrent_sessions"]:
        raise rejected(
            state,
            429,
            "max_concurrent_sessions",
            f"동시에 열 수 있는 세션 {entry['max_concurrent_sessions']}개가 모두 찼다",
            user_sub=caller.sub,
            headers={"Retry-After": "60"},
        )
    now = utcnow()
    record = SessionRecord(
        session_id=uuid.uuid4().hex,
        tenant=state.settings.tenant,
        user_sub=caller.sub,
        created_at=now,
        last_turn_at=now,
        instance_id=state.config.instance["id"],
        title=title_from_message(message),
        run_id=run_id,
        untrusted=untrusted,
        # 웹훅 실행의 세션은 처음부터 바깥 내용을 가진 대화다. P43 을 아는 옛 이미지의 파드가 배포
        # 도중에 결정을 이어도 표시된 대화로 돌게 바깥 표시도 함께 적는다(P41 10절)
        outside_at=now if untrusted else None,
    )
    try:
        await index.create(record)
    except RunTaken as e:
        raise run_exists(e.session_id, await index.get(e.session_id)) from e
    return record


@asynccontextmanager
async def unstarted_session_forgotten(
    state: RuntimeState, record: SessionRecord, *, created: bool
) -> AsyncIterator[None]:
    """`/chat` 이 연 세션의 턴 태스크가 뜨기 전에 요청이 실패하면 그 세션을 지운다. 턴 태스크가 뜬
    뒤에는 실패하지 않고, 턴이 돈 적이 없으니 샌드박스는 닫지 않는다. 지우지 못하면 로그만 남기고
    원래 오류를 낸다.

    - 실행 세션(P41 9절, runs.py)은 어느 실패에서든 지운다. 턴 없는 세션이 run_id 를 쥐면 플랫폼이
      다시 한 시작이 `run_exists`(턴 id null)를 받고 실행을 잃은 것으로 끝낸다(P41 5절). 실행
      세션은 `/chat` 이 늘 새로 만든다.
    - 이 요청이 만든(created) 채팅 세션은 drain 표시가 첫 잡기를 물렸을 때만 지운다(P44 7절).
      입구(`check_request`)가 표시를 본 뒤 잡기 전에 표시가 선 경우다. 빈 세션이 재우기 직전에 센
      세션 수와 동시 세션 상한에 들지 않게 한다."""
    try:
        yield
    except Exception as e:
        drained = created and isinstance(e.__cause__, DrainMarked)
        if record.run_id is not None or drained:
            try:
                await state.store.delete_session(record.session_id, close_sandbox=False)
            except Exception as err:  # noqa: BLE001  원래 오류를 그대로 낸다
                log.warning("시작하지 못한 세션 %s 를 지우지 못했다: %s", record.session_id, err)
        raise


def greeting_event(
    state: RuntimeState, record: SessionRecord, *, new_session: bool
) -> list[tuple[str, dict[str, Any]]]:
    """새 세션의 첫 SSE 이벤트다(D65). 입구의 인사말이 비어 있지 않을 때만 있고, 모델 메시지에는
    넣지 않아 모델의 동작과 토큰이 그대로다. 이어지는 턴과 재개 턴에는 없다."""
    text = state.config.entry.get("greeting")
    if not new_session or not text:
        return []
    return [(stream.GREETING, {"session_id": record.session_id, "text": text})]


async def seed_turn(
    state: RuntimeState, session_id: str | None, *, ephemeral: bool
) -> stream.TurnState:
    """이 턴의 TurnState 다. 이어지는 세션이면 체크포인트의 메시지로 씨앗을 채워 이력의 사용량과
    도구 이벤트를 다시 내지 않는다. 새 세션은 읽을 것이 없다. astream 이 어차피 읽는 체크포인트다.
    """
    if not session_id:
        return stream.TurnState()
    config = stream.thread_config(session_id)
    return await stream.seed_from(state.agent, config, ephemeral=ephemeral)


async def start_turn(
    state: RuntimeState,
    record: SessionRecord,
    turn_input: str | HumanMessage | Command,
    *,
    attempt: TurnAttempt,
    actor: str,
    turn: stream.TurnState,
    ephemeral: bool = False,
    prelude: list[tuple[str, dict[str, Any]]] | None = None,
    attachments: list[StoredUpload] | None = None,
    grant: TurnGrant | None = None,
    choices: list[ApprovalChoice] | None = None,
) -> StreamingResponse:
    """진행 중 턴을 막고 SSE 응답을 연다. /chat 과 /decisions 가 같이 쓴다.

    prelude 는 입구에서 이미 정해진 이벤트(새 세션의 인사말, 입력 가드레일 flag)다. 그래프
    이벤트보다 먼저 나간다. ephemeral 이면 이 턴은 장기 기억 저장소를 안 붙인다. 리스를 잡으면
    입구를 지난 첨부를 세션에 저장하고(attachments.py) 이 시도의 번호와 id 를 색인에
    적는다(turn_gate.py).
    """
    async with held_lease(state, record.session_id) as lease:
        # 리스 전에 읽은 색인은 그 사이에 끝난 턴의 사용량과 `cite_seq` 를 모를 수 있다. 다시
        # 생성처럼 리스 안에서 다시 읽어 인용 번호가 앞 턴과 겹치지 않게 한다. 그 사이에 TTL 이
        # 세션을 닫았거나 다른 결정이 재개를 마쳤으면 여기서 멈춘다. 리스를 잡은 세션은 TTL 이
        # 닫지 않는다
        record = await load_session(state, record.session_id)
        check_still_open(state, record, resume=isinstance(turn_input, Command))
        if attachments and isinstance(turn_input, str):
            record, turn_input = await receive_attachments(
                state, record, turn, turn_input, attachments, attempt.turn_id, token=lease.token
            )
        await note_attempt(
            state,
            record,
            attempt,
            token=lease.token,
            kind=RUN_RESUME if isinstance(turn_input, Command) else RUN_CHAT,
            trace=turn.trace,
        )
        return await open_turn(
            state,
            record,
            turn_input,
            lease=lease,
            attempt=attempt,
            actor=actor,
            turn=turn,
            ephemeral=ephemeral,
            prelude=prelude,
            grant=grant,
            choices=choices,
        )


async def open_turn(
    state: RuntimeState,
    record: SessionRecord,
    turn_input: str | HumanMessage | Command,
    *,
    lease: HeldLease,
    attempt: TurnAttempt,
    actor: str,
    turn: stream.TurnState,
    ephemeral: bool = False,
    prelude: list[tuple[str, dict[str, Any]]] | None = None,
    fork: Fork | None = None,
    grant: TurnGrant | None = None,
    choices: list[ApprovalChoice] | None = None,
) -> StreamingResponse:
    """리스(lease)를 잡은 턴의 태스크를 띄우고 그 이벤트를 읽는 SSE 응답을 연다(D92). 응답을 하나도
    읽기 전에 연결이 닫혀도 턴은 돌고 리스를 푼다. 다시 생성은 리스를 잡은 뒤 입력 체크포인트를 찾고
    갈래(fork)를 정해 여기로 온다. 세션의 첨부는 태스크를 띄우기 전에 읽는다. 읽지 못하면 리스를
    풀고 오류 응답이다.

    새 메시지, 결정 재개, 다시 생성과 고쳐 보내기가 모두 여기를 지나므로 웹 가드의 턴 몫도 여기서
    채운다. 첨부 여부는 색인의 `upload_bytes` 이고 이번 요청이 받은 첨부도 이미 들어 있다.
    위임(grant, P53 6절)을 받았으면 그 id 를 응답 머리로 되돌려 플랫폼이 받았는지 알게 한다.
    세션의 바깥 표시(P43 3절)도 여기서 다시 정한다. 색인의 `outside_at`, 첨부, 씨앗의 기록 가운데
    하나라도 있으면 표시된 대화이고, 바깥 결과가 처음 오면 리스의 토큰으로 `outside_at` 을 쓴다.
    기억한 선택(choices)은 턴 문맥에만 둔다.

    턴 태스크가 리스를 넘겨받아(`HeldLease.kept`) 마감에서 놓는다. 태스크를 띄우기 전에 리스를
    잃었으면 503 turn_aborted 다(P79 2절).

    백그라운드 실행의 세션(P41 9절, runs.py)은 요청이 무엇을 싣든 임시로 돈다. 웹훅 실행의
    `untrusted` 는 첨부를 받은 대화처럼 웹 가드의 엄격한 URL 규칙과 바깥 표시를 켠다(P43 3절). 그
    세션은 만들 때 `outside_at` 도 적었다(`resolve_session`). 실행 세션의 기억한 선택은 거절만
    남긴다(P43 5절)."""
    ephemeral = ephemeral or record.run_id is not None
    await load_turn_uploads(state, record, turn)
    turn.holds_attachments = record.upload_bytes > 0 or record.untrusted
    turn.web = WebBudget(counters=state.store.counters, instance_id=state.config.instance["id"])
    turn.outside = OutsideFlag(
        flagged=record.outside_at is not None or turn.holds_attachments or turn.history_outside,
        stored=record.outside_at is not None,
        write=partial(_mark_outside, state, record.session_id, lease.token),
    )
    turn.approval_choices = choices_of(choices, rejects_only=record.run_id is not None)
    if lease.fence.standing:
        raise turn_aborted()
    watch = CancelWatch(
        state.store.index,
        record.session_id,
        lease.token,
        poll_seconds=state.settings.cancel_poll_interval,
    )
    lease.kept = True
    body = _run_turn(
        state,
        record,
        turn_input,
        attempt=attempt,
        actor=actor,
        turn=turn,
        ephemeral=ephemeral,
        prelude=prelude or [],
        watch=watch,
        lease=lease,
        fork=fork,
        grant=grant,
    )
    events = start_turn_task(state.turns, body, session_id=record.session_id)
    watch.reader = events
    return ClosingStreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            SESSION_HEADER: record.session_id,
            TURN_ID_HEADER: attempt.turn_id,
            DETACHED_HEADER: "1",
            **({TURN_GRANT_HEADER: grant.id} if grant is not None else {}),
            **run_headers(record),
        },
    )


async def _mark_outside(state: RuntimeState, session_id: str, token: str) -> None:
    """세션 색인에 바깥 표시를 쓴다(P43 3절). 턴 자신의 쓰기라 리스의 토큰을 건다. 리스를 잃었으면
    거절되고 바깥 결과는 내보내지 않는다(tool_safety.py)."""
    await state.store.index.update_held(session_id, token, outside_at=utcnow())


async def _turn_events(
    state: RuntimeState,
    prelude: list[tuple[str, dict[str, Any]]],
    *,
    session_id: str,
    turn_input: str | HumanMessage | Command,
    turn: stream.TurnState,
    owner_sub: str,
    ephemeral: bool,
    watch: CancelWatch,
    turn_id: str,
    checkpoint_id: str | None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """입구 이벤트(prelude) 뒤에 그래프 이벤트를 잇는다. 닫히면 stream_turn 도 그 자리에서
    닫는다."""
    for event in prelude:
        yield event
    graph_events = stream.stream_turn(
        state.agent,
        session_id=session_id,
        turn_input=turn_input,
        state=turn,
        owner_sub=owner_sub,
        ephemeral=ephemeral,
        cancel=watch,
        turn_id=turn_id,
        checkpoint_id=checkpoint_id,
        chunk_marks=True,
    )
    async with aclosing(graph_events) as events:
        async for event in events:
            yield event


def _log_stream_event(
    evlog: EventLog,
    event: str,
    data: dict[str, Any],
    *,
    where: dict[str, Any],
) -> dict[str, Any] | None:
    """스트림 이벤트 하나를 관측 이벤트로 남기고 SSE 로 내보낼 data 를 돌려준다. where 는 점
    이벤트가 드는 자리 칸(세션·사용자·턴·트레이스)이다.

    budget·model_retry 는 stdout 에만 남기므로 None 이고 호출자가 건너뛴다. interrupt 만 SSE
    쪽 data 에 session_id 를 얹는다. 도구 호출과 결과는 SSE 로만 나가고 관측 이벤트는 트레이서의
    tool_exec 스팬이다(P37).
    """
    if event in stream.INTERNAL_EVENTS:
        evlog.emit(event, **where, **data)
        return None
    if event in (stream.GUARDRAIL, stream.MODEL_FALLBACK, stream.TOOL_LIMIT):
        _log_notice(evlog, event, data, where=where)
    elif event == stream.INTERRUPT:
        requests = data["action_requests"]
        evlog.emit(
            ev.INTERRUPT,
            **where,
            tools=[req.get("name") for req in requests],
            args=evlog.body([req.get("args") for req in requests]),
        )
        return {"session_id": where["session_id"], **data}
    return data


def _log_notice(
    evlog: EventLog,
    event: str,
    data: dict[str, Any],
    *,
    where: dict[str, Any],
) -> None:
    """guardrail·model_fallback·tool_limit 을 관측 이벤트로 남긴다. 첨부 글의 위반은 SSE 로는
    이름이 나가고 관측 이벤트에는 record_bodies 일 때만 싣는다."""
    evlog.emit(event, **where, **evlog.scrub(data))


async def _finish_turn(
    state: RuntimeState,
    record: SessionRecord,
    turn: stream.TurnState,
    *,
    attempt: TurnAttempt,
    token: str,
    latency_ms: float,
    replaced_turn_id: str | None = None,
) -> None:
    """턴을 마감한다. 실행의 트레이서를 닫고(열린 스팬이 이 턴의 코드로 닫힌다) 결과를 메트릭에 넣고
    쓴 토큰을 오늘 카운터에 더한 뒤 색인을 갱신하고 turn_end 를 남긴다. turn_end 는 실행 스팬이라
    트레이스 칸과 시작 시각, 길이(latency_ms 와 같은 값)를 든다. replaced_turn_id 는 다시 생성이
    갈래를 냈을 때 버린 턴의 id 다.

    오류로 끝난 턴은 사용량과 인용 번호를 적고 턴 수는 그대로 두며(끊긴 턴의 토큰도 쓴 것이지만
    턴을 센 것은 아니다) 상태는 체크포인트에 맞춘다(`_status_from_checkpoint`). 승인 대기로 끝났으면
    pending 이고 아니면 active 다. 사람 쪽에서 멈춘 턴(turn_cancelled, turn_access_lost)은 오류 턴과
    같되 오류율·지연 표본에 들지 않고 turn_end 는 cancelled 다.
    """
    sid = record.session_id
    evlog = state.events
    turn.trace.close(turn.error[0] if turn.error is not None else None)
    now = utcnow()
    await _charge_daily(state, turn, now)
    fields: dict[str, Any] = {
        "last_turn_at": now,
        "usage_input": record.usage_input + turn.usage.input_tokens,
        "usage_output": record.usage_output + turn.usage.output_tokens,
        **cite_seq_fields(record, turn.citations.seq),
    }
    end: dict[str, Any] = {
        "session_id": sid,
        "user_sub": record.user_sub,
        "turn": attempt.number,
        "turn_id": attempt.turn_id,
        "run_id": record.run_id,
        **turn.trace.run_fields,
        "start": turn.trace.start,
        "duration_ms": latency_ms,
        "latency_ms": latency_ms,
        "usage": turn.usage.to_dict(),
        "tool_calls": turn.tool_calls,
        ev.REPLACED_TURN_ID: replaced_turn_id,
    }
    if turn.error is not None:
        code, detail = turn.error
        status = _end_status(code)
        fields.update(await _status_from_checkpoint(state, record, now))
        await _write_end(state, record, turn, fields, token=token)
        evlog.emit_span(ev.TURN_END, **end, status=status, code=code, error=detail)
        return

    fields["turns"] = attempt.number
    if turn.interrupted:
        fields["status"] = STATUS_PENDING
        fields["pending_since"] = now
    else:
        fields["status"] = STATUS_ACTIVE
        fields["pending_since"] = None
    await _write_end(state, record, turn, fields, token=token)
    evlog.emit_span(
        ev.TURN_END,
        **end,
        status=turn.status,
        text=evlog.body(turn.text),
        structured_response=evlog.body(turn.structured),
    )


async def _write_end(
    state: RuntimeState,
    record: SessionRecord,
    turn: stream.TurnState,
    fields: dict[str, Any],
    *,
    token: str,
) -> None:
    """마감의 색인 쓰기다. 턴 자신의 쓰기라 리스의 토큰을 걸고, 같은 문장에서 이 시도의 실행
    표식을 지운다(P79 3절). 거절되면 표식이 남아 되살리기가 turn_end 를 낸다.

    백그라운드 실행의 세션은 승인 대기 없이 끝난 턴에서 닫는다(P41 9절). 상태를 active 대신 closed
    로 적고, 그 쓰기가 된 뒤에 `turn.closes_session` 을 세워 리스를 푼 뒤 체크포인트와 첨부를
    지우게 한다(`_run_turn`). 전사는 남는다. 체크포인트를 읽지 못해 상태를 모르는 오류 턴은 닫지
    않고 TTL 에 맡긴다."""
    closing = record.run_id is not None and fields.get("status") == STATUS_ACTIVE
    if closing:
        fields["status"] = STATUS_CLOSED
    await state.store.index.update_held(record.session_id, token, settle=True, **fields)
    turn.index_settled = True
    turn.closes_session = closing


async def _status_from_checkpoint(
    state: RuntimeState, record: SessionRecord, now: datetime
) -> dict[str, Any]:
    """오류·중지로 끝난 턴의 세션 상태를 체크포인트에 맞춘다(D65).

    재개 턴이 도구나 다음 모델 호출 도중에 멈추면 인터럽트는 이미 소비됐고, interrupt 를 내다 끊긴
    턴은 체크포인트가 보류다. 색인이 그와 어긋나면 같은 세션의 다음 말(409 session_pending)과
    결정(409 not_pending)이 함께 막힌다. 보류가 이어지면 처음 시각을 둔다. 체크포인트를 읽지
    못하면 상태를 건드리지 않는다.
    """
    try:
        snapshot = await state.agent.graph.aget_state(stream.thread_config(record.session_id))
    except Exception as e:  # noqa: BLE001  체크포인트 장애가 사용량·turn_end 마감을 막지는 않는다
        log.warning("세션 %s 체크포인트 읽기 실패: %s", record.session_id, e)
        return {}
    if snapshot.interrupts:
        return {"status": STATUS_PENDING, "pending_since": record.pending_since or now}
    return {"status": STATUS_ACTIVE, "pending_since": None}


def _end_status(code: str) -> str:
    """오류로 끝난 턴의 turn_end status 다. 중지와 접근을 잃은 사람은 사람 쪽에서 멈춘 것이라
    cancelled 이고, 플랫폼의 지표가 오류율과 지연 표본에서 뺀다. 나머지(파드 종료의 turn_aborted
    포함)는 오류 코드가 그대로 status 다."""
    if code in (TURN_CANCELLED, TURN_ACCESS_LOST):
        return TURN_END_CANCELLED
    return code


def _grant_refused(state: RuntimeState, watch: CancelWatch, status: int) -> None:
    """위임 갱신이 거절됐다(P78 1·5·7절). 403 은 사람이 접근을 잃은 것이라 턴을 멈춘다. 받을 사람이
    없는 답을 이어 가지 않는다. 파드 종료 중의 401 은 배포 해제·삭제가 인스턴스 토큰을 회수한 것이라
    기다리던 턴을 멈춘다. 그 밖의 거절은 위임만 멈추고 Tool 호출이 그 까닭으로 실패한다."""
    if status == 403:
        watch.notify(ACCESS_LOST)
    elif status == 401 and state.draining:
        watch.notify(SHUTDOWN)


def _grant_end_reason(turn: stream.TurnState) -> str:
    """플랫폼에 알리는 위임의 끝난 까닭이다(P53 4절). 중지, 파드 종료, 오류, 승인 대기, 완료 가운데
    하나다. 접근을 잃어 멈춘 턴은 오류다(P78 1절)."""
    if turn.error is not None:
        code = turn.error[0]
        if code == TURN_CANCELLED:
            return END_CANCELLED
        return END_SHUTDOWN if code == TURN_ABORTED else END_ERROR
    return END_INTERRUPT if turn.interrupted else END_DONE


async def _save_transcript(
    state: RuntimeState, session_id: str, items: list[dict[str, Any]], *, token: str
) -> bool:
    """이번 턴이 만든 전사 항목을 세션에 붙인다(P18 F1). 붙였거나 붙일 것이 없으면 참이다.

    붙이지 못해도 턴 결과는 그대로 낸다. 전사는 화면을 다시 그리려고 두는 것이라 저장소가
    흔들렸다고 이미 끝난 턴을 실패로 만들 이유가 없다. 턴 자신의 붙이기라 리스의 토큰을 건다(P79
    2절). 저장소가 거절하면 리스를 잃은 것이라 펜스가 서고 턴이 멈춘다.
    """
    if not items:
        return True
    try:
        await state.store.transcripts.append_held(session_id, items, token)
    except TurnFenced as e:
        log.info("세션 %s 전사: 리스를 잃어 붙이지 않았다: %s", session_id, e)
        return False
    except Exception as e:  # noqa: BLE001  전사 저장 실패가 턴 결과를 막지는 않는다
        log.warning("세션 %s 전사 저장 실패: %s", session_id, e)
        return False
    return True


class _ClosedItems:
    """그래프 조각이 끝날 때마다 앞에서부터 닫힌 전사 항목을 붙이는 턴의 쓰기 태스크다(P78 3절).

    턴의 생성기는 조각 끝에서 `mark` 만 하고 저장소를 기다리지 않는다. 그 기다림은 `stream_turn` 의
    제한 시간 밖이라, 중지·제한 시간·drain 끝이 그 자리에 떨어지면 까닭을 잃고 턴 태스크가 취소된
    것(turn_aborted)으로 마감되며 마지막 이벤트도 나가지 않는다. 붙이지 못한 항목은 버퍼에 남아 다음
    표시에 다시 붙되, 실패한 뒤에는 SAVE_RETRY_SECONDS 를 기다린다. 남은 것은 마감이 `take()` 로
    붙인다. 갈래를 내는 다시 생성은 head 가 옮겨 간 뒤부터 붙인다. 그 전에 파드가 죽으면 앞 턴이
    그대로 남는다. 첫 턴의 다시 생성은 스레드를 지우고 시작하므로 처음부터 붙인다."""

    def __init__(
        self,
        state: RuntimeState,
        session_id: str,
        transcript: TurnTranscript,
        fork: Fork | None,
        *,
        token: str,
    ) -> None:
        self._state = state
        self._session_id = session_id
        self._transcript = transcript
        self._fork = fork
        self._token = token
        # 이 턴의 갈래가 섰다(head 가 옮겨 갔다). 그 뒤에야 붙이고, 마감이 갈래를 다시 읽지 않는
        # 데도 쓴다(fork.py). 다시 생성이 아니거나 첫 턴의 다시 생성이면 처음부터 섰다
        self.branched = fork is None or fork.parent_id is None
        self._wake = asyncio.Event()
        self._closing = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    def mark(self) -> None:
        """조각 하나가 끝났다. 쓰기 태스크를 깨운다."""
        self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._closing.is_set():
                return
            count = self._transcript.closed_count() if await self._may_save() else 0
            if not count:
                continue
            items = self._transcript.items[:count]
            if await _save_transcript(self._state, self._session_id, items, token=self._token):
                self._transcript.drop(count)
                continue
            with anyio.move_on_after(SAVE_RETRY_SECONDS):
                await self._closing.wait()

    async def _may_save(self) -> bool:
        if not self.branched and self._fork is not None:
            self.branched = await self._fork.head_moved(self._state.store.checkpointer)
        return self.branched

    async def close(self) -> None:
        """마감 전에 부른다. 붙이는 중인 한 번은 마감 시간 안에서 끝나기를 기다리고 멈춘다. 그 뒤의
        항목은 마감이 붙인다. 여러 번 불러도 같다."""
        self._closing.set()
        self._wake.set()
        with anyio.move_on_after(FINISH_GRACE_SECONDS, shield=True):
            await asyncio.wait({self._task})
        self._task.cancel()


async def _run_turn(
    state: RuntimeState,
    record: SessionRecord,
    turn_input: str | HumanMessage | Command,
    *,
    attempt: TurnAttempt,
    actor: str,
    turn: stream.TurnState,
    ephemeral: bool,
    prelude: list[tuple[str, dict[str, Any]]],
    watch: CancelWatch,
    lease: HeldLease,
    fork: Fork | None = None,
    grant: TurnGrant | None = None,
) -> AsyncIterator[str]:
    """한 턴(또는 재개)을 돌리고 SSE 이벤트를 낸다. 끝나면 _finish_turn 이 마감한다.

    이벤트의 user_sub 는 언제나 세션 소유자다. actor 는 이 턴을 시작한 사람이고 재개면 결정자라
    turn_start.decided_by 로만 남긴다. guardrail·model_fallback 은 SSE 와 stdout 양쪽에,
    budget·model_retry 는 stdout 에만 남긴다.

    장기 기억의 주체도 소유자다(record.user_sub). 재개 턴은 승인자가 열 수 있어서 actor 를
    쓰면 승인자 기억에 쓴다(D34). 인용 장부는 세션의 `cite_seq` 에서 이어진다(turn_record.py).

    화면용 전사(P18 F1)는 SSE 로 내보낸 data 를 그대로 접어 모으며, 그래프 조각이 끝날 때마다
    앞에서부터 닫힌 항목을 붙이고(P78 3절) 나머지는 턴이 끝날 때 붙인다. 그래서 돌아온 사람이 도는
    턴의 지난 항목을 본다. 마지막 이벤트(done·error)는 마감(전사, 위임 닫기, 리스 풀기)이 다 끝난
    뒤에 낸다. 그래서 클라이언트가 끝을 본 시점에는 이미 저장돼 있고, 끝을 보자마자 보낸 다음 말이
    turn_in_progress 로 거절되지 않는다. 인터럽트로 끝난 턴도 같은 자리에서 붙어 승인 카드가
    새로고침 뒤에도 남는다.

    이 생성기는 턴 태스크(turn_task.py)가 끝까지 돌린다. 연결이 끊겨도 닫히지 않는다(D92).
    태스크가 취소되면(파드가 마감 없이 내려가는 중) turn_aborted 로 마감한다. 안쪽 생성기는
    aclosing 으로 돌아 stream_turn 의 그래프 태스크가 그 자리에서 멈추므로 진행 중이던 모델 호출이
    멈춘다. 마감은 막이 안에서 시간을 묶고 돈다. 취소 범위는 그 안의 await 마다 취소를 다시 걸기
    때문이다. 마감 뒤 취소는 다시 올린다. 그래프가 도는 동안 이 생성기는 기다리지 않는다. 조각마다의
    전사는 쓰기 태스크(`_ClosedItems`)가 붙인다. 멈추라는 말은 stream_turn 의 제한 시간에 떨어져야
    그 까닭으로 마감되기 때문이다.

    멈추라는 말(watch, cancel.py)은 stream_turn 이 그 까닭의 코드로 돌려주므로 보통의 오류 턴처럼
    마감하고 마지막 이벤트 error 를 낸다. 감시는 도는 동안 state.cancels 에 걸어 같은 파드의 중지
    요청과 위임 갱신, 파드 종료가 바로 알리게 한다.

    다시 생성(fork)은 부모 체크포인트에서 갈래를 내고 전사 맨 앞에 표식을 둔다(regenerate.py). 첫
    턴의 다시 생성은 부모가 없어 그대로 새로 돈다.

    위임(grant, P53 6절)은 그래프를 돌리기 전에 열어 이 턴의 문맥에 두고 갱신을 시작한다. 위임 없는
    턴도 없음을 둔다. 어느 길로 끝나든 마감에서 리스를 풀기 전에 이 턴의 것만 닫으며 플랫폼에 끝난
    까닭을 알린다. 그래서 리스를 받은 다음 턴의 위임과 엇갈리지 않는다.

    리스(lease)는 파드의 관리자가 늘린다(lease_keeper.py). 관리자가 리스를 잃으면 이 턴을 리스를
    잃은 까닭(turn_aborted)으로 멈추고, 마감은 그 까닭이 아니라 관리자의 상태(펜스)를 보고
    `_settle_fenced` 로 간다. 먼저 온 중지나 그래프 안에서 막힌 쓰기가 남긴 까닭과 상관없다. 그
    턴은 세션에 더 쓰지 않으므로 전사도 리스 풀기도 하지 않는다(P79 2절).
    """
    sid = record.session_id
    evlog = state.events
    turn.citations = ledger_for(record)
    place = {
        "session_id": sid,
        "user_sub": record.user_sub,
        "turn": attempt.number,
        "turn_id": attempt.turn_id,
    }
    turn.trace.begin(evlog, where=place, sources=state.agent.tool_sources)
    where = {**place, **turn.trace.point_fields}
    transcript = _begin_turn(
        state,
        record,
        turn_input,
        turn,
        attempt,
        actor=actor,
        ephemeral=ephemeral,
        fork=fork,
        grant_id=grant.id if grant is not None else None,
    )
    started = time.monotonic()
    events = _turn_events(
        state,
        prelude,
        session_id=sid,
        turn_input=turn_input,
        turn=turn,
        owner_sub=record.user_sub,
        ephemeral=ephemeral,
        watch=watch,
        turn_id=attempt.turn_id,
        checkpoint_id=fork.parent_id if fork is not None else None,
    )
    settled = False
    closed_items = _ClosedItems(state, sid, transcript, fork, token=lease.token)
    state.cancels[sid] = watch
    if state.drain is not None:
        state.drain.admit(watch)
    lease.on_lost = partial(watch.notify, LEASE_LOST)
    if lease.fence.standing:
        watch.notify(LEASE_LOST)
    calls = state.grants.open(
        grant,
        on_refused=partial(_grant_refused, state, watch),
        turn=TurnRef(sid, attempt.turn_id),
    )

    async def settle() -> tuple[str, dict[str, Any]] | None:
        """앞부분 쓰기를 닫고 턴을 마감한다. 마지막 이벤트를 돌려준다. 펜스는 여기와 아래
        finally 에서 따로 읽는다. 마감하는 동안 리스를 잃을 수 있어서다."""
        await closed_items.close()
        if lease.fence.standing:
            return await _settle_fenced(state, record, turn, attempt)
        try:
            return await _settle_turn(
                state,
                record,
                turn,
                transcript,
                attempt=attempt,
                token=lease.token,
                started=started,
                fork=fork,
                branched=closed_items.branched,
            )
        except TurnFenced:
            # 마감하다 리스를 잃었다(색인 쓰기가 펜스에 막혔거나 서버가 토큰을 거절했다). 이미 더한
            # 토큰은 다시 더하지 않는다(`TurnState.charged`)
            return await _settle_fenced(state, record, turn, attempt)

    final: tuple[str, dict[str, Any]] | None = None
    # 턴이 리스를 쥔 동안 샌드박스에 쓰기를 알린다(P40 5절). 결정을 기다리는 동안은 알리지 않는다
    touching = state.sandboxes.turn(sid) if state.sandboxes is not None else nullcontext()
    try:
        async with touching, aclosing(events) as turn_events:
            async for event, data in turn_events:
                if event == stream.CHUNK_END:
                    closed_items.mark()
                    continue
                out = _log_stream_event(evlog, event, data, where=where)
                if out is None:
                    continue
                transcript.add(event, out)
                yield sse(event, out)
        final = await settle()
        settled = True
    except asyncio.CancelledError:
        # 턴 태스크가 취소됐다(파드가 마감 없이 내려가는 중). 이미 마감한 턴은 다시 마감하지 않는다
        if not settled:
            turn.error = (SHUTDOWN.code, SHUTDOWN.detail)
            await settle()
        raise
    finally:
        await closed_items.close()
        if state.cancels.get(sid) is watch:
            # 리스를 넘긴 뒤 같은 세션의 다음 턴이 건 감시는 그대로 둔다
            del state.cancels[sid]
        lost = lease.fence.standing
        # 관리자는 풀기를 기다리기 전에 놓는다. 풀기가 실패해도 리스는 1분 안에 만료된다
        state.leases.drop(lease)
        # 위임은 리스를 풀기 전에 따로 막이 안에서 닫는다. 플랫폼이 늦어도 리스 풀기를 붙잡지
        # 않는다. 리스를 잃은 턴은 오류로 닫는다(P79 2절)
        with anyio.move_on_after(FINISH_GRACE_SECONDS, shield=True):
            await state.grants.close(calls, END_ERROR if lost else _grant_end_reason(turn))
        # 위에서 붙이지 못한 것(예외)이 남아 있으면 마저 붙인다. 다 붙였으면 take() 가 비어 있어
        # 저장소를 건드리지 않는다. 리스는 앞의 마감이 시간을 넘겨도 따로 막이 안에서 푼다. 리스를
        # 잃은 턴은 둘 다 하지 않는다. 마감의 색인 쓰기가 끝나지 않은 턴(저장소 오류, 시간 넘김)도
        # 풀지 않는다. 그러면 실행 표식과 열린 표시가 남아 리스가 끝난 뒤 되살리기가 줄과
        # turn_end 로 마감한다(P79 3절). 파드 안 색인만은 리스가 만료되지 않고 되살리기도 없으므로
        # 푼다
        with anyio.move_on_after(FINISH_GRACE_SECONDS, shield=True):
            if not lost:
                await _save_transcript(state, sid, transcript.take(), token=lease.token)
            if (not lost and turn.index_settled) or not state.store.shared:
                try:
                    await state.store.index.release_turn(sid, watch.token)
                except Exception as e:  # noqa: BLE001  못 풀어도 리스는 1분 안에 만료된다
                    log.warning("세션 %s 리스 풀기 실패: %s", sid, e)
    # 마지막 이벤트는 리스를 푼 뒤에 낸다. 끝을 본 사람이 곧바로 보낸 다음 말이 거절되지 않는다
    if final is not None:
        yield sse(*final)
    if turn.closes_session:
        # 닫힌 실행 세션의 상태를 지운다(P41 9절). 색인은 이미 closed 이고 닫힌 세션은 다시 턴도
        # 결정도 받지 않으므로, 마감 뒤에 리스를 잃었어도 지워도 된다. 그래서 턴의 펜스 밖에서
        # 제 시간 안에 지운다. 지우지 못한 것은 기록을 지울 때 마저 지운다(`purge_records`)
        with anyio.move_on_after(FINISH_GRACE_SECONDS, shield=True), fenced(None):
            await state.store.drop_state(sid)


def _begin_turn(
    state: RuntimeState,
    record: SessionRecord,
    turn_input: str | HumanMessage | Command,
    turn: stream.TurnState,
    attempt: TurnAttempt,
    *,
    actor: str,
    ephemeral: bool,
    fork: Fork | None,
    grant_id: str | None = None,
) -> TurnTranscript:
    """턴의 전사를 열고 turn_start 를 남긴다. 사용자 항목에는 친 글과 첨부의 요약, 이 시도의
    turn_id 가 붙는다(되살리기가 저장됐는지 본다, P79 3절). 재개 턴은 사용자가 카드에서 결정만 한
    것이라 말풍선이 없다. 결정은 인터럽트 항목이 닫히는 것으로 보인다."""
    text = _input_text(turn_input)
    attached = turn_attachments(turn, turn_input)
    transcript = TurnTranscript(rewind=fork is not None)
    if text is not None:
        transcript.add_user(
            text,
            attachments=[upload.summary() for upload in attached],
            turn_id=attempt.turn_id,
        )
    start = _turn_start_fields(
        state.events,
        record,
        attempt,
        turn.trace,
        actor=actor,
        text=text,
        ephemeral=ephemeral,
        fork=fork,
        attached=attached,
        grant_id=grant_id,
    )
    state.events.emit(ev.TURN_START, **start)
    return transcript


async def _settle_turn(
    state: RuntimeState,
    record: SessionRecord,
    turn: stream.TurnState,
    transcript: TurnTranscript,
    *,
    attempt: TurnAttempt,
    token: str,
    started: float,
    fork: Fork | None = None,
    branched: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    """턴을 마감하고 마지막 이벤트를 돌려준다. 색인·카운터·turn_end 를 적고 마지막 이벤트를 전사에
    접어 저장한다. 인터럽트로 끝난 턴은 마지막 이벤트가 없다.

    막이 안에서 시간을 묶고 돈다. 클라이언트가 끊겨도 끝난 모델 호출의 사용량과 전사는 남아야
    하고, 저장소가 멈췄을 때 태스크가 끝없이 남지 않아야 한다. 시간을 넘기면 남은 것은 건너뛰고
    리스 만료가 마지막 그물이다. 다시 생성 턴은 갈래가 생겼는지를 먼저 본다(`Fork.settle`).
    branched 는 조각마다의 붙이기가 이미 갈래를 본 것이다. 그러면 다시 읽지 않는다.
    """
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    final = _final_event(record.session_id, turn, attempt)
    with anyio.move_on_after(FINISH_GRACE_SECONDS, shield=True):
        replaced = None
        if fork is not None:
            replaced = await fork.settle(
                state.store.checkpointer, turn, transcript, branched=branched
            )
        await _finish_turn(
            state,
            record,
            turn,
            attempt=attempt,
            token=token,
            latency_ms=latency_ms,
            replaced_turn_id=replaced,
        )
        if turn.error is not None:
            # 오류로 끝난 턴에는 결과가 오지 않는다. 열린 호출 카드를 닫아 "실행 중" 이 남지 않게
            # 한다
            transcript.close_open_calls(turn.error[1])
        if final is not None:
            transcript.add(*final)
        await _save_transcript(state, record.session_id, transcript.take(), token=token)
    return final


async def _settle_fenced(
    state: RuntimeState,
    record: SessionRecord,
    turn: stream.TurnState,
    attempt: TurnAttempt,
) -> tuple[str, dict[str, Any]]:
    """리스를 잃은 턴의 마감이다(P79 2절). 세션에는 더 쓰지 않는다. 색인도 전사도 고치지 않고 리스도
    풀지 않는다. 트레이서를 닫아 열린 모델 스팬이 비용 이벤트로 남게 하고, 쓴 토큰을 오늘 카운터에
    더한다(펜스 문맥 밖, 보통의 마감이 이미 더했으면 건너뛴다). turn_end 는 내지 않는다. 이 파드에서
    읽는 사람에게는 turn_aborted 를 낸다.
    """
    turn.error = (LEASE_LOST.code, LEASE_LOST.detail)
    turn.trace.close(LEASE_LOST.code)
    with anyio.move_on_after(FINISH_GRACE_SECONDS, shield=True):
        await _charge_daily(state, turn, utcnow())
    final = _final_event(record.session_id, turn, attempt)
    assert final is not None  # 오류로 끝난 턴에는 마지막 이벤트가 있다
    return final


def _input_text(turn_input: str | HumanMessage | Command) -> str | None:
    """턴을 연 사용자 글이다. 사람 메시지면 첨부 안내를 뗀 친 글이고 재개 턴이면 None 이다."""
    if isinstance(turn_input, Command):
        return None
    return typed_text(turn_input) if isinstance(turn_input, HumanMessage) else turn_input


def _turn_start_fields(
    evlog: EventLog,
    record: SessionRecord,
    attempt: TurnAttempt,
    trace: RunTracer,
    *,
    actor: str,
    text: str | None,
    ephemeral: bool,
    fork: Fork | None,
    attached: list[StoredUpload],
    grant_id: str | None = None,
) -> dict[str, Any]:
    """turn_start 의 칸이다. 실행 스팬의 id(trace)를 든다. text 가 None 이면 재개 턴이라 글 대신
    결정자(actor)를 남긴다. 다시 생성이면 갈래의 칸이 붙는다. 첨부(attached)는 크기와 해시만이고
    이름은 record_bodies 일 때만이다. 위임을 받은 턴은 그 id 만 싣는다(토큰과 비밀은 싣지 않는다,
    P53 6절). 값이 None 인 칸은 이벤트에 실리지 않는다."""
    resumed = text is None
    return {
        "session_id": record.session_id,
        "user_sub": record.user_sub,
        "turn": attempt.number,
        "turn_id": attempt.turn_id,
        **trace.run_fields,
        "run_id": record.run_id,
        "resumed": True if resumed else None,
        "decided_by": actor if resumed else None,
        "ephemeral": True if ephemeral else None,
        "message": None if resumed else evlog.body(text),
        "attachments": observed(attached, body=evlog.body),
        "grant_id": grant_id,
        **(fork.start_fields() if fork is not None else {}),
    }


def _final_event(
    sid: str, turn: stream.TurnState, attempt: TurnAttempt
) -> tuple[str, dict[str, Any]] | None:
    """턴의 마지막 SSE 이벤트다. 오류면 error, 인터럽트면 없음, 아니면 done 이다. 둘 다 이 시도의
    turn_id 와 이 실행의 trace_id 를 싣는다. 재개된 턴은 실행마다 trace_id 가 다르다.

    구조화 결과(응답 형식이 JSON 스키마일 때 출력 필터를 지난 값)는 있을 때만 싣는다. 전사와
    화면의 done 항목이 같은 칸을 들어 새로고침해도 남는다.
    """
    if turn.error is not None:
        code, detail = turn.error
        data = {
            "session_id": sid,
            "turn_id": attempt.turn_id,
            "trace_id": turn.trace.trace_id,
            "code": code,
            "message": detail,
        }
        return (stream.ERROR, data)
    if turn.interrupted:
        return None
    done: dict[str, Any] = {
        "session_id": sid,
        "turn": attempt.number,
        "turn_id": attempt.turn_id,
        "trace_id": turn.trace.trace_id,
        "usage": turn.usage.to_dict(),
        "tool_calls": turn.tool_calls,
        "text": turn.text,
        # 이 요청이 받아 저장한 첨부 수다. 옛 런타임에는 없어 첨부가 버려졌는지 안다(P36 9절)
        "attachments_received": turn.uploads.received,
    }
    if turn.structured is not None:
        done["structured_response"] = turn.structured
    return (stream.DONE, done)
