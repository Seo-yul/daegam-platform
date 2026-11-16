"""백그라운드 실행의 세션 규칙이다(P41 9절, D94 초안). 기능 이름은 `runs` 다(features.py).

플랫폼의 시작 워커는 실행 하나를 `POST /chat` 하나로 연다. 본문에 실행의 `run_id` 와 웹훅 실행의
표시 `untrusted` 를 싣고 세션 없이 부른다. 둘은 플랫폼 경로만 받는다(외부 경로는 422,
grant_gate.py 의 `refuse_external_fields`). 새 세션의 색인 한 줄이 둘을 지킨다(sessions.py).

- **한 실행에 한 세션.** `/chat` 은 본문의 꼴(외부 경로의 칸, 실행 칸의 짝)을 본 뒤 다른 어떤
  검사보다 먼저 run_id 로 세션을 찾고, 닫힌 세션도 찾는다. 있으면 409 `run_exists` 가 그 세션 id 와
  턴 id(아직 시도를 적지 않았으면 null)를 싣는다. 두 요청이 함께 와도 색인이 run_id 를 한 세션에만
  준다(`RunTaken`). 그래서 시작 워커가 턴을 연 뒤 죽어도, 다시 해도 둘째 턴이 열리지 않는다.
- **실행 세션의 턴.** 모든 턴이 임시로 돌고(장기 기억을 붙이지 않는다) 세션의 `untrusted` 를
  따른다. 요청이 무엇을 싣든 같다. `untrusted` 는 첨부를 받은 대화처럼 웹 가드의 엄격한 URL 규칙을
  켜고(`holds_attachments`), P43 의 바깥 표시를 켠다(turn.py). 기억한 선택은 거절만 쓴다. 실행은
  스스로 승인을 결정하지 않기 때문이다(P41 목표 4, P43 5절). 승인 대기 없이 끝난 턴은 세션을
  닫는다(turn.py).
- **결정.** 실행 세션의 결정은 플랫폼 경로에서만 받고(외부 경로는 403) 그 세션의 run_id 를 실어야
  한다. 없거나 다르면 409 `run_closed` 다. 플랫폼이 실행을 닫았다고 알리는 코드와 같은 이름이다.
  새 말과 다시 생성은 409 `run_session` 이다(turn_gate.py 의 `check_open_session`). 사람이나
  승인자가 플랫폼에서 내린 결정만 실행을 잇는다.
- **목록.** `GET /sessions` 는 실행 세션을 빼고 `runs=include` 면 함께, `runs=only` 면 그것만 준다.
- **응답 머리.** 실행 세션의 턴은 `X-Daegam-Run-Id` 로 run_id 를 되돌린다. 시작 워커는 이 머리가
  없는 답을 실행을 모르는 옛 파드의 것으로 본다.
"""

from __future__ import annotations

from fastapi import HTTPException

from runtime.auth import Caller
from runtime.sessions import SessionRecord
from runtime.turn_gate import error
from runtime.validation import body_field_error, validation_error

RUN_ID_HEADER = "X-Daegam-Run-Id"

RUN_EXISTS = "run_exists"
RUN_CLOSED = "run_closed"
# 라우트가 맞춰 보는 실행 칸의 짝이 어긋난 검증 항목의 종류다
RUN_FIELDS = "run_fields"


def check_run_fields(run_id: str | None, untrusted: bool, session_id: str | None) -> None:
    """`/chat` 의 실행 칸이 맞는 꼴인가. 실행은 새 세션으로만 열고, `untrusted` 는 실행의
    표시다. 어긋나면 422 다. 외부 경로는 이보다 먼저 `refuse_external_fields` 가 막았다."""
    errors = []
    if run_id is not None and session_id is not None:
        errors.append(
            body_field_error("run_id", "a run starts a new session: send no session_id", RUN_FIELDS)
        )
    if untrusted and run_id is None:
        errors.append(
            body_field_error("untrusted", "untrusted marks a run: send it with run_id", RUN_FIELDS)
        )
    if errors:
        raise validation_error(errors)


def run_exists(session_id: str, held: SessionRecord | None) -> HTTPException:
    """같은 run_id 의 세션이 있다. 시작 워커가 그 세션과 턴으로 잇는다(P41 5절). 턴 id 는 그
    세션(held)이 적은 마지막 시도의 것이고, 아직 적지 않았거나 그 줄을 읽지 못했으면 null 이다."""
    turn_id = held.turn_id if held is not None else None
    return error(
        409, RUN_EXISTS, "이 실행의 세션이 이미 있다", session_id=session_id, turn_id=turn_id
    )


def refuse_external_run(caller: Caller, record: SessionRecord) -> None:
    """외부 경로는 실행 세션을 읽기만 한다. 결정, 중지, 이름 바꾸기, 지우기는 403 이다. 실행의
    결과와 기록은 플랫폼이 정하고(P41 7절), 지우기는 run_id 를 풀어 다시 한 시작이 둘째 턴을 열 수
    있기 때문이다."""
    if record.run_id is not None and caller.external:
        raise error(403, "forbidden", "백그라운드 실행의 세션은 플랫폼 경로에서만 바꾼다")


def check_run_decision(caller: Caller, record: SessionRecord, run_id: str | None) -> None:
    """결정이 이 세션에 맞는 실행 칸을 실었는가. 실행 세션이면 외부 경로는 403 이고, 플랫폼
    경로는 그 세션의 run_id 를 실어야 한다. 실행 세션이 아닌데 run_id 를 실은 결정도 맞지 않는
    것이라 409 `run_closed` 다."""
    refuse_external_run(caller, record)
    if run_id != record.run_id:
        raise error(409, RUN_CLOSED, "이 결정은 이 세션의 실행을 싣지 않았다")


def run_headers(record: SessionRecord) -> dict[str, str]:
    """실행 세션의 턴이 싣는 응답 머리다. 실행 세션이 아니면 비어 있다."""
    return {RUN_ID_HEADER: record.run_id} if record.run_id is not None else {}
