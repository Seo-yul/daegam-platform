"""소유자의 세션 관리다(P36 4절). 이름 바꾸기와 지우기를 routes.py 의 라우터와 따로 둔다.

  PATCH  /sessions/{sid}  {title}  제목을 바꾼다. 닫힌 세션도 된다 -> {session}
  DELETE /sessions/{sid}           체크포인트, 첨부, 전사, 색인 행을 지운다 -> {session_id, deleted}

둘 다 세션 소유자만 부른다. 승인자 헤더(`X-Daegam-Approver`)도 통하지 않는다. 남의 대화를 지우거나
이름을 바꿀 까닭이 없기 때문이다. 프록시 경로는 쿼리 `user_sub` 가 주체이고 없으면 422
`user_sub_required` 다. 외부 경로는 토큰의 sub 다.

지우기는 턴 리스를 잡고 돈다. 도는 턴이 있으면 409 `turn_in_progress` 이고, 승인 대기 세션과 닫힌
세션도 지운다. 리스를 풀지 못하고 끝난 턴의 열린 표시 위에서도 되살리기를 기다리지 않고
잡는다(P79 3절). 그 턴의 실행 표식이 남았으면 되살린 turn_end 를 낸다. 장기 기억은 사람의 것이라
남는다. 지운 뒤 관측 이벤트 `session_deleted` 를 남긴다.
Redis 체크포인트를 정한 횟수만큼 지워도 남으면 503 `session_delete_incomplete` 이고, 색인 행이 남아
다시 지우면 이어서 지운다. memory 노드가 없는 에이전트는 색인이 파드 안이라 그 파드의 세션만 보인다.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from runtime import events as ev
from runtime.auth import Caller, CallerDep, require_owner_sub
from runtime.runs import refuse_external_run
from runtime.sessions import CheckpointsRemain, SessionRecord
from runtime.state import Runtime, RuntimeState
from runtime.turn_gate import check_owner, error, held_lease, load_session, session_not_found
from runtime.turn_record import SESSION_TITLE_MAX_CHARS, SESSION_TITLE_RAW_MAX_CHARS, checked_title
from runtime.turn_repair import emit_repaired_end

router = APIRouter()


class RenameRequest(BaseModel):
    title: str = Field(max_length=SESSION_TITLE_RAW_MAX_CHARS)


async def _owned_session(state: RuntimeState, caller: Caller, session_id: str) -> SessionRecord:
    """소유자의 세션 한 줄이다. 프록시 경로에 주체가 없으면 422, 없는 세션은 404, 남의 세션은
    승인자라도 403 이다. 외부 경로의 실행 세션도 403 이다(runs.py `refuse_external_run`)."""
    require_owner_sub(caller)
    record = await load_session(state, session_id)
    check_owner(caller, record)
    refuse_external_run(caller, record)
    return record


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str, body: RenameRequest, caller: CallerDep, state: Runtime
) -> dict[str, Any]:
    await _owned_session(state, caller, session_id)
    title = checked_title(body.title)
    if title is None:
        raise error(
            422,
            "invalid_title",
            f"제목은 공백과 제어 문자를 뺀 길이가 1~{SESSION_TITLE_MAX_CHARS}자여야 한다",
        )
    record = await state.store.index.update(session_id, title=title)
    if record is None:
        raise session_not_found()
    return {"session": record.to_dict()}


# 다 지우지 못한 지우기의 까닭이다. 둘 다 503 session_delete_incomplete 이고 다시 지우면 이어서
# 지운다
TOO_MANY_RECORDS = "대화 기록이 많아 한 번에 다 삭제하지 못했다. 다시 삭제해야 한다"
LEASE_LOST_WHILE_DELETING = "지우는 동안 세션의 리스를 잃었다. 다시 삭제해야 한다"


def _delete_incomplete(message: str) -> HTTPException:
    """다 지우지 못한 지우기의 답이다. 색인 행이 남아 다시 지우면 이어서 지운다."""
    return error(503, "session_delete_incomplete", message)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, caller: CallerDep, state: Runtime) -> dict[str, Any]:
    record = await _owned_session(state, caller, session_id)
    # 지우다 실패해 색인 행이 남으면 held_lease 가 리스를 풀어 다음 지우기가 다시 잡는다. 지우는
    # 동안 리스를 잃으면(P79) 풀지 않고, 리스가 만료된 뒤 다시 지우면 이어서 지운다
    lost = partial(_delete_incomplete, LEASE_LOST_WHILE_DELETING)
    try:
        async with held_lease(
            state,
            session_id,
            on_fence=lost,
            on_orphan=partial(emit_repaired_end, state.events, record),
        ):
            await state.store.delete_session(session_id)
    except CheckpointsRemain as e:
        raise _delete_incomplete(TOO_MANY_RECORDS) from e
    state.events.emit(ev.SESSION_DELETED, session_id=session_id, user_sub=record.user_sub)
    return {"session_id": session_id, "deleted": True}
