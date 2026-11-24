"""HTTP 표면이다(D26, D27). create_app 이 이 라우터를 앱에 건다.

  POST /chat            {session_id?, message, user_sub?, ephemeral?, attachments?, run_id?,
                         untrusted?, task?}
                        -> SSE(token|tool_call|tool_result|interrupt|guardrail|model_fallback
                               |tool_limit|done|error)
                        입구 검사 순서: 도구 소스를 아직 못 읽었으면 503 tools_unavailable(+
                        Retry-After, T18), 사용자별 분당 한도(429 rate_limited + Retry-After),
                        입력 길이(413), 입력 가드레일(422 guardrail_blocked), 일당 토큰 예산
                        (429 daily_budget_exceeded + Retry-After), 첨부(attachments.py), 세션
                        상태, 세션 예산(429). 첨부는 리스를 잡은 뒤 세션에 저장한다. 백그라운드
                        실행(run_id, 플랫폼 경로만)은 본문의 꼴을 본 뒤 무엇보다 먼저 같은 실행의
                        세션을 찾아 409 run_exists 로 답하고 분당 한도를 건너뛴다(runs.py, P41 9절).
                        작업의 실행(task)은 턴이 끝나도 세션을 닫지 않고, 다음 실행은 그
                        session_id 와 새 run_id 로 온다(runs.py)
  GET  /sessions        색인 목록. user_sub·status 로 거르고 q 는 제목 검색어다(외부 경로는 자기
                        sub 로 고정). 줄마다 running 이 있고 진행 중이면 그 시도의 turn_id 가 있다.
                        실행 세션은 빼고 runs=include 면 함께, runs=only 면 실행 세션만이다
  GET  /sessions/{sid}  색인 한 줄 + 체크포인트의 메시지 요약 + 화면용 전사(외부 경로는 소유자만).
                        ?view=progress 는 체크포인트와 요약 없이 색인 한 줄과 전사만 읽는 가벼운
                        읽기다. 진행 중 턴을 폴링하는 화면이 쓴다(P78 2절)
  GET  /sessions/{sid}/pending    보류 중인 승인 요청(action_requests·review_configs. 외부는 소유자)
  POST /sessions/{sid}/decisions  {user_sub?, decisions[], ephemeral?, run_id?} 로 재개 -> /chat 과
                                  같은 SSE. 실행 세션은 플랫폼 경로에서 그 run_id 를 실어야 한다
                                  (외부는 403, 어긋나면 409 run_closed)
                                  edit 은 같은 도구여야 하고(422 edit_name_mismatch) 고친 인자는
                                  도구 가드레일을 지난다(block 은 422 guardrail_blocked, flag 는
                                  앞머리의 guardrail 이벤트). reject·respond 의 message 가
                                  max_input_chars 를 넘으면 413 max_input_chars 다(P76)
  POST /sessions/{sid}/cancel     {user_sub?, turn_id?} 진행 중 턴을 멈춘다(D65). 주체는 결정과
                                  같다. turn_id 를 주면 그 시도일 때만 멈춘다(P78 2절). 턴이 없거나
                                  다른 시도면 409 no_turn_in_progress, 있으면 202 {session_id}
  POST /turns/cancel              {user_sub} 이 인스턴스의 진행 중 턴을 모두 멈춘다(P78 5절).
                                  플랫폼 경로만 받고(외부는 403) {running} 으로 아직 리스가 살아
                                  있는 수를 준다. 배포 해제와 삭제가 인스턴스 토큰을 회수하기 전에
                                  부른다
  POST /drain                     {user_sub, mark} 이 인스턴스의 drain 표시를 그 id(mark)로
                                  세우고 {running} 으로 살아 있는 리스 수를 준다(P44 7절). 표시가
                                  선 동안 모든 잡기는 503 agent_draining 이다. 플랫폼 경로만
                                  받는다(외부는 403)
  DELETE /drain?mark=             id 가 mark 인 drain 표시만 지운다. 맞지 않아도 204 다. 플랫폼
                                  경로만 받는다(외부는 403)
  PATCH·DELETE /sessions/{sid}    소유자의 이름 바꾸기와 지우기(sessions_api.py, P36)
  POST /sessions/{sid}/regenerate {user_sub?, message?, ephemeral?} 마지막 턴을 다시 생성하거나
                                  고쳐 보낸다 -> /chat 과 같은 SSE(regenerate.py, P36)
  GET  /memories, GET·DELETE /memories/files/{key}  호출자 자신의 장기 기억 보기와 하나 지우기
                                  (memories_api.py, P36)
  GET  /sessions/{sid}/files, GET /sessions/{sid}/files/{path}  세션 파일의 목록과 내려받기
                                  (session_files.py, P36)
  GET  /metrics         공유 저장소의 세션 수·토큰 합·일당 토큰, 도구 소스마다 맺은 MCP 규약
                        판(tools.protocols), 받는 첨부의 형식과 상한(chat, attachments.py), 이벤트
                        보내기의 수(telemetry). 턴·지연·Tool·거절의 수는 플랫폼이 이벤트로 센다
                        (P37 11절)(외부 경로는 403)
  GET  /healthz         기동 상태와 경고. 인스턴스 토큰이 없는 요청에는 {"status": "ok"} 만.
                        ?probe=ready 는 준비 검사라 채팅을 받지 않는 상태(D65), 저장소에 아직
                        연결하지 못한 상태(T22·T23), 도구를 아직 준비하지 못한 상태(T18)면
                        503 이다. 저장소에 연결하지 못한 동안은 healthz 밖의 모든 경로가 503
                        dependencies_unavailable + Retry-After 다(main.py 의 미들웨어)

주체는 runtime/auth.py 의 Caller 하나다(D27). 인스턴스 토큰이면 플랫폼 프록시 경로(sub 는 본문·쿼리
user_sub), OIDC 액세스 토큰이면 외부 경로(sub 는 토큰 sub, approver 는 언제나 거짓)다.

`ephemeral` 은 이 턴에 장기 기억 저장소를 안 붙이라는 표시다(D34 밖, 플랫폼 테스트 채팅 자리).
기본은 남긴다. 재개 턴도 같은 표시를 다시 실어야 그 턴이 임시로 돈다. 임시 턴도 화면용 전사는
남긴다(transcript.py).

체크포인트를 읽는 세 길(전체 읽기, 보류 읽기, 결정)은 색인 상태가 체크포인트와 어긋난 P79 앞의
세션을 답하기 전에 맞춘다(turn_repair.py `align_with_checkpoint`). 닫힌 세션에는 쓰지 않는다.

세션 TTL 이 지나 닫힌 대화는 이어 쓸 수 없지만(새 턴은 410 `session_closed`) 기록은 남는다(D46).
`GET /sessions/{sid}` 는 닫힌 세션도 200 이고 전사를 그대로 돌려준다. 전사가 사라지는 것은 기록
보존 기간이 지나 sweep 이 색인 행과 함께 지울 때다(sessions.py 의 `purge_records`).

턴의 수명(열기·실행·마감·끊김·중지, SSE 머리)은 turn.py 의 docstring 이 적는다.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from runtime import error_codes, stream
from runtime import events as ev
from runtime.approvals import MAX_CHOICES, ApprovalChoice
from runtime.attachments import (
    ChatAttachment,
    chat_report,
    check_attachments,
)
from runtime.auth import CallerDep, token_presented
from runtime.cancel import notify_all
from runtime.config import ConfigError
from runtime.counters import daily_tokens_key, day_window
from runtime.dsn import is_unreachable
from runtime.features import runtime_report
from runtime.grant_gate import check_grant, refuse_external_fields
from runtime.hitl import (
    DecisionsRequest,
    HitlError,
    edited_args,
    read_pending,
    resume_command,
    validate_decisions,
)
from runtime.runs import (
    check_run_decision,
    check_run_fields,
    refuse_external_run,
    run_exists,
)
from runtime.sessions import (
    DRAIN_MARK_SECONDS,
    RUN_ID_PATTERN,
    RUNS_EXCLUDE,
    STATUS_CLOSED,
    STATUS_PENDING,
    STATUSES,
    RunsFilter,
    SessionRecord,
    utcnow,
)
from runtime.state import (
    Runtime,
    RuntimeState,
    refusal,
    runtime,
    starting,
)
from runtime.transcript import (
    TRANSCRIPT_LIMIT,
    fold_rewinds,
    merge_resumed_calls,
    resolve_interrupts,
)
from runtime.turn import (
    greeting_event,
    resolve_session,
    seed_turn,
    start_turn,
    unstarted_session_forgotten,
)
from runtime.turn_gate import (
    TOOLS_UNAVAILABLE,
    TOOLS_UNAVAILABLE_MESSAGE,
    check_caller,
    check_decision_args,
    check_decision_messages,
    check_request,
    check_session_budget,
    check_tools_ready,
    error,
    guardrail_prelude,
    load_session,
    session_closed,
)
from runtime.turn_grants import TurnGrant
from runtime.turn_record import (
    SESSION_TITLE_MAX_CHARS,
    TurnAttempt,
)
from runtime.turn_repair import align_with_checkpoint

router = APIRouter()

# 살아 있음 검사의 경로다. 채팅을 받지 않는 앱(main.py 의 RefusingMiddleware)도 이 경로만은 라우터에
# 넘겨 되풀이 재시작이 없다(D65)
HEALTHZ_PATH = "/healthz"
# 준비 검사의 질의 값과 채팅을 받지 않는 상태의 healthz status 다(D65). 렌더의 readinessProbe 경로가
# 앞의 값을 쓴다
PROBE_READY = "ready"
STATUS_REFUSING = "refusing"
# 저장소(Database·Redis)에 아직 연결하지 못해 다시 연결하고 있다(T22). main.py 의 미들웨어가
# healthz 밖의 요청을 이 코드로 거절하고, healthz 의 status 는 starting 이다. 까닭(연결할 수 없음,
# 접속 거부)은 healthz 상세의 unready 에 있다
DEPENDENCIES_UNAVAILABLE = "dependencies_unavailable"
DEPENDENCIES_UNAVAILABLE_MESSAGE = "런타임이 아직 Database 에 연결하지 못했다"
STATUS_STARTING = "starting"
STATUS_OK = "ok"


class CancelRequest(BaseModel):
    # 프록시 경로의 주체. 외부 경로는 무시하고 토큰 sub 를 쓴다. 읽는 곳은 runtime/auth.py 뿐이다
    user_sub: str | None = None
    # 멈출 시도의 id 다(P78 2절). 주면 색인의 turn_id 가 그것일 때만 멈춘다. 옛 화면은 보내지 않는다
    turn_id: str | None = Field(default=None, min_length=1, max_length=64)


class InstanceRequest(BaseModel):
    # 인스턴스 전체를 다루는 플랫폼 경로 요청(`/turns/cancel`, `/drain`)의 본문이다. 프록시 경로의
    # POST 는 본문에 주체가 있어야 한다. 플랫폼이 싣는 주체는 시스템 주체다. 배포 해제와 삭제의 턴
    # 멈추기는 `system:turn-stop`, 재우기의 drain 표시는 `system:sleep` 이다. 런타임은 주체를 보지
    # 않는다. 읽는 곳은 runtime/auth.py 뿐이다
    user_sub: str | None = None


# drain 표시의 id 꼴이다(P44 7절). 플랫폼이 재우기마다 정하는 UUID 이고 Redis 값과 Postgres 열에
# 그대로 담는다
DRAIN_MARK_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class DrainRequest(InstanceRequest):
    # 세울 표시의 id 다. 같은 id 의 지우기만 이 표시를 지운다
    mark: str = Field(pattern=DRAIN_MARK_PATTERN)


class ChatRequest(BaseModel):
    session_id: str | None = None
    # 길이 상한은 입력 길이 검사가 본다 (P76 F2, turn_gate.MESSAGE_MAX_CHARS)
    message: str = Field(min_length=1)
    # 프록시 경로의 소유자. 외부 경로는 무시하고 토큰 sub 를 쓴다. 읽는 곳은 runtime/auth.py 뿐이다
    user_sub: str | None = None
    # 이 턴을 임시로 돌린다. 장기 기억 저장소를 안 붙이므로 무엇을 써도 세션과 함께 사라진다.
    # 플랫폼의 테스트 채팅이 쓸 자리이고 기본은 남긴다. 장기 기억이 꺼져 있으면 아무 뜻이 없다
    ephemeral: bool = False
    # 이 턴에 붙이는 문서 글과 이미지다(attachments.py). 개수와 모양은 입구가 본다
    attachments: list[ChatAttachment] = Field(default_factory=list)
    # 플랫폼이 실은 턴 위임(P53 6절). 프록시 경로만 받는다(grant_gate.py)
    turn_grant: TurnGrant | None = None
    # 이 턴의 사람이 기억한 선택(P43 5절). 프록시 경로만 받는다(approvals.py)
    approval_choices: list[ApprovalChoice] | None = Field(default=None, max_length=MAX_CHOICES)
    # 백그라운드 실행의 id 와 웹훅 실행의 표시다(P41 9절). 프록시 경로만 받고 새 세션에만 싣는다.
    # 세션이 둘을 지킨다(runs.py)
    run_id: str | None = Field(default=None, pattern=RUN_ID_PATTERN)
    untrusted: bool = False
    # 다른 Agent 가 맡긴 작업의 실행이라는 표시다(runs.py). 프록시 경로만 받고 run_id 와 함께만
    # 싣는다. 세션이 지키고, 작업의 다음 실행은 그 세션의 session_id 를 함께 싣는다
    task: bool = False


@router.get(HEALTHZ_PATH)
async def healthz(request: Request, probe: str | None = None) -> dict[str, Any]:
    """kubelet probe 는 200 만 본다. 툴 이름·내부 URL 이 섞이는 상세는 인스턴스 토큰을 낸 요청
    (플랫폼)에만 낸다. 토큰이 아예 없는 로컬 실행은 보호할 것이 없어 전부 낸다.

    문서에 모르는 기능 이름이 있거나(D65), 저장소에 아직 연결하지 못했거나(T22·T23), 도구
    목록으로 아직 컴파일하지 못했으면(T18) 살아 있음 검사는 200 이고 준비 검사(`probe=ready`)만
    503 이다. 옛 이미지는 질의를 보지 않고 둘 다 200 이다. 준비 검사의 본문은 kubelet 도 보므로
    이유의 원문(내부 주소가 섞인다)은 싣지 않는다. 원문은 상세의 `unready{code, message}` 에 있고
    플랫폼이 준비되지 않은 파드의 이유로 화면에 싣는다(T21)."""
    reason = refusal(request)
    if reason is not None:
        if probe == PROBE_READY:
            raise error(503, reason.code, reason.message)
        # 이유는 이 이미지가 모르는 기능 이름뿐이라 내부 주소가 섞이지 않는다. 그래서 토큰 없이도
        # 낸다
        unready = {"code": reason.code, "message": reason.message}
        return {"status": STATUS_REFUSING, "error": unready, "unready": unready}
    waiting = starting(request)
    if waiting is not None:
        if probe == PROBE_READY:
            raise error(503, DEPENDENCIES_UNAVAILABLE, DEPENDENCIES_UNAVAILABLE_MESSAGE)
        if not _detail_allowed(request):
            return {"status": STATUS_STARTING}
        return {"status": STATUS_STARTING, "unready": _starting_reason(waiting)}
    state = runtime(request)
    if probe == PROBE_READY and not state.tools_ready:
        raise error(503, TOOLS_UNAVAILABLE, TOOLS_UNAVAILABLE_MESSAGE)
    if not _detail_allowed(request):
        return {"status": STATUS_OK}
    tools_error = state.compile_error or state.sources.last_error
    unready = None
    if not state.tools_ready:
        unready = {"code": TOOLS_UNAVAILABLE, "message": tools_error or TOOLS_UNAVAILABLE_MESSAGE}
    return {
        "status": STATUS_OK,
        "profile": state.config.profile,
        "instance": state.config.instance,
        "warnings": state.store.warnings + list(state.config.warnings) + state.agent.warnings,
        "tools": [tool.name for tool in state.agent.tools],
        "tools_ready": state.tools_ready,
        "tools_error": tools_error,
        "tools_protocols": state.sources.protocols,
        "unready": unready,
        "greeting": state.config.entry["greeting"],
        "runtime": runtime_report(),
    }


def _detail_allowed(request: Request) -> bool:
    """healthz 상세를 내도 되는가. 인스턴스 토큰을 낸 요청(플랫폼)이거나 토큰이 아예 없는 로컬
    실행이다. 설정은 자원을 열기 전에도 있는 앱 상태에서 읽는다(main.create_app)."""
    token = request.app.state.settings.instance_token
    return not token or token_presented(request, token.encode("utf-8"))


def _starting_reason(waiting: ConfigError) -> dict[str, str]:
    """다시 연결하는 동안의 이유(`unready`)다. 아직 연결할 수 없는 것이면 dependencies_unavailable
    아래에 안쪽 코드를 message 로 함께 싣고, 접속 거부(T23)처럼 다시 연결하다 드러난 설정 오류면
    그 코드와 문구다. 플랫폼이 `code: message` 로 화면에 싣으므로 같은 코드를 두 번 싣지
    않는다(T21)."""
    if is_unreachable(waiting):
        return {"code": DEPENDENCIES_UNAVAILABLE, "message": str(waiting)}
    return {"code": waiting.code, "message": waiting.message}


@router.post("/chat")
async def chat(body: ChatRequest, caller: CallerDep, state: Runtime) -> StreamingResponse:
    """새 말이다. 백그라운드 실행(run_id)은 본문의 꼴을 본 뒤 다른 어떤 검사보다 먼저 같은 실행의
    세션을 찾아 있으면 409 `run_exists` 이고, 분당 한도를 건너뛴다(P41 9절, runs.py). 작업의
    실행(task)이 session_id 를 실으면 그 작업 세션의 다음 실행이다."""
    refuse_external_fields(
        caller,
        turn_grant=body.turn_grant,
        approval_choices=body.approval_choices,
        run_id=body.run_id,
        untrusted=body.untrusted or None,
        task=body.task or None,
    )
    check_run_fields(body.run_id, body.untrusted, body.session_id, body.task)
    if body.run_id is not None:
        held = await state.store.index.find_run(body.run_id)
        if held is not None:
            raise run_exists(body.run_id, held.session_id, held)
    flagged = await check_request(
        state, caller, body.session_id, body.message, per_minute=body.run_id is None
    )
    attachments, attachment_flags = check_attachments(
        state, caller, body.attachments, body.session_id
    )
    record = await resolve_session(
        state,
        caller,
        body.session_id,
        body.message,
        run_id=body.run_id,
        untrusted=body.untrusted,
        task=body.task,
    )
    async with unstarted_session_forgotten(state, record, created=not body.session_id):
        grant = check_grant(caller, body.turn_grant, owner=record.user_sub)
        check_session_budget(state, record)
        return await start_turn(
            state,
            record,
            body.message,
            attempt=TurnAttempt.next(record),
            actor=record.user_sub,
            turn=await seed_turn(state, body.session_id, ephemeral=body.ephemeral),
            ephemeral=body.ephemeral,
            prelude=[
                *greeting_event(state, record, new_session=not body.session_id),
                *guardrail_prelude([*flagged, *attachment_flags]),
            ],
            attachments=attachments,
            grant=grant,
            choices=body.approval_choices,
            next_run=body.run_id if body.session_id else None,
        )


@router.get("/sessions")
async def list_sessions(
    caller: CallerDep,
    state: Runtime,
    user_sub: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    q: str | None = Query(default=None, min_length=1, max_length=SESSION_TITLE_MAX_CHARS),
    runs: RunsFilter = RUNS_EXCLUDE,
) -> dict[str, Any]:
    """색인 목록이다. 검색어 q 를 받으면 응답의 `q` 로 되돌려준다. q 를 모르는 옛 런타임은 그
    쿼리를 버리고 거르지 않은 목록을 주므로, 플랫폼은 이 칸이 없으면 롤링 중인 옛 파드가 답한 것으로
    본다(P36). 백그라운드 실행의 세션은 빼고(`exclude`, 기본), runs 가 `include` 면 함께,
    `only` 면 실행 세션만 준다(P41 9절)."""
    if status is not None and status not in STATUSES:
        raise error(422, "invalid_status", f"status 는 {', '.join(STATUSES)} 중 하나다")
    if caller.external:
        user_sub = caller.sub
    rows = await state.store.index.list(
        user_sub=user_sub, status=status, limit=limit, q=q, runs=runs
    )
    listed: dict[str, Any] = {"sessions": [row.to_dict() for row in rows]}
    if q is not None:
        listed["q"] = q
    return listed


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    caller: CallerDep,
    state: Runtime,
    view: Literal["progress"] | None = Query(default=None),
) -> dict[str, Any]:
    """세션 한 줄과 두 가지 기록이다.

    `messages` 는 예전부터 있던 200자 메시지 요약이라 세션 상세 표가 그대로 쓴다. `transcript`
    는 화면을 다시 그리는 원문 전사이고 `{items, limit}` 이다(P18 F1). items 는 오래된 것부터이고
    저장된 항목 수가 limit 이면 그보다 앞은 상한에 걸려 지워졌다는 뜻이다. 다시 생성이 버린 턴은
    걷어 내고 재개 턴이 다시 알린 호출 카드는 앞 카드에 합치므로 돌려주는 수는 그보다 적을 수 있다.
    전사는 색인과 같은 저장소에 살아서 색인 한 줄을 읽은 뒤 따로 실패할 일이 거의 없으므로 오류를
    감추지 않는다.
    """
    record = await load_session(state, session_id)
    check_caller(caller, record)
    if view == "progress":
        # 진행 중 턴을 폴링하는 가벼운 읽기다(P78 2절). 체크포인트와 메시지 요약을 읽지 않는다.
        # 보류는 색인의 상태가 pending 이고 턴이 돌지 않을 때다. 재개가 도는 동안 색인은 아직
        # pending 이라 그때 승인 카드를 다시 열지 않게 한다
        return await _session_view(
            state, record, pending=record.status == STATUS_PENDING and not record.running
        )
    snapshot = await state.agent.graph.aget_state(stream.thread_config(session_id))
    record = await align_with_checkpoint(state, record, interrupted=bool(snapshot.interrupts))
    # TTL 이 지나 닫힌 것으로 읽은 세션은 첫 sweep 이 지우기 전까지 체크포인트가 남는다(P44 11절).
    # 받을 수 없는 결정의 승인 카드를 열지 않는다
    pending = bool(snapshot.interrupts) and record.status != STATUS_CLOSED
    detail = await _session_view(state, record, pending=pending)
    detail["messages"] = stream.summarize_messages(
        stream.snapshot_messages(snapshot), output_filter=state.agent.output_filter
    )
    return detail


async def _session_view(
    state: RuntimeState, record: SessionRecord, *, pending: bool
) -> dict[str, Any]:
    """세션 한 줄과 화면에 맞게 접은 전사다. 전체 읽기와 가벼운 읽기가 같이 쓴다."""
    items = await state.store.transcripts.read(record.session_id)
    fold_rewinds(items)
    merge_resumed_calls(items)
    resolve_interrupts(items, pending=pending)
    return {
        "session": record.to_dict(),
        "pending": pending,
        "transcript": {"items": items, "limit": TRANSCRIPT_LIMIT},
    }


@router.get("/sessions/{session_id}/pending")
async def get_pending(session_id: str, caller: CallerDep, state: Runtime) -> dict[str, Any]:
    """보류 중인 승인 요청이다. 닫힌 세션이 아니면 체크포인트를 읽고 색인 상태를 그것에 맞춘 뒤
    답한다(P79 3절)."""
    record = await load_session(state, session_id)
    check_caller(caller, record)
    if record.status == STATUS_CLOSED:
        raise error(404, "not_pending", "승인 대기 중인 세션이 아니다")
    pending = await read_pending(state.agent, session_id)
    record = await align_with_checkpoint(state, record, interrupted=pending is not None)
    if record.status != STATUS_PENDING:
        raise error(404, "not_pending", "승인 대기 중인 세션이 아니다")
    if pending is None:
        raise error(404, "not_pending", "색인은 pending 이지만 체크포인트에 인터럽트가 없다")
    return {
        "session_id": session_id,
        "user_sub": record.user_sub,
        "pending_since": record.pending_since.isoformat() if record.pending_since else None,
        "action_requests": pending.action_requests,
        "review_configs": pending.review_configs,
    }


@router.post("/sessions/{session_id}/decisions")
async def post_decisions(
    session_id: str, body: DecisionsRequest, caller: CallerDep, state: Runtime
) -> StreamingResponse:
    refuse_external_fields(
        caller,
        turn_grant=body.turn_grant,
        approval_choices=body.approval_choices,
        run_id=body.run_id,
    )
    check_tools_ready(state, caller, session_id)
    record = await load_session(state, session_id)
    check_caller(caller, record, or_approver=True)
    check_run_decision(caller, record, body.run_id)
    if record.status == STATUS_CLOSED:
        raise session_closed()
    # 인터럽트 위의 active 와 인터럽트 없는 pending 을 맞춘 뒤 본다(P79 3절). 인터럽트가 없으면
    # 맞춘 뒤에도 409 not_pending 이고 다음 말이 받아진다
    pending = await read_pending(state.agent, session_id)
    record = await align_with_checkpoint(state, record, interrupted=pending is not None)
    if record.status != STATUS_PENDING:
        raise error(409, "not_pending", "승인 대기 중인 세션이 아니다")
    if pending is None:
        raise error(409, "not_pending", "색인은 pending 이지만 체크포인트에 인터럽트가 없다")
    try:
        decisions = validate_decisions(body.decisions, pending)
    except HitlError as e:
        raise error(error_codes.status_of(e.code, 422), e.code, e.message) from e
    check_decision_messages(state, caller, session_id, body)
    flagged = check_decision_args(state, caller, session_id, body)
    grant = check_grant(caller, body.turn_grant, owner=record.user_sub, decisions=body.decisions)
    attempt = TurnAttempt.resumed(record)
    turn = pending.seed_with(body.decisions)
    # 결정은 재개 실행을 열기 전에 받지만 그 실행의 트레이스에 든다(P37)
    state.events.emit(
        ev.DECISION,
        session_id=session_id,
        user_sub=caller.sub,
        owner_sub=record.user_sub,
        approver=caller.approver,
        turn=attempt.number,
        turn_id=attempt.turn_id,
        **turn.trace.point_fields,
        decisions=[d["type"] for d in decisions],
        tools=[req.get("name") for req in pending.action_requests],
        message=state.events.body([d.get("message") for d in decisions]),
        args=state.events.body(edited_args(decisions)),
    )
    return await start_turn(
        state,
        record,
        resume_command(decisions),
        attempt=attempt,
        actor=caller.sub,
        turn=turn,
        ephemeral=body.ephemeral,
        prelude=guardrail_prelude(flagged),
        grant=grant,
        choices=body.approval_choices,
    )


@router.post("/sessions/{session_id}/cancel", status_code=202)
async def cancel_turn(
    session_id: str, body: CancelRequest, caller: CallerDep, state: Runtime
) -> dict[str, Any]:
    """진행 중 턴을 멈춘다(D65). 주체는 결정과 같다(소유자나 플랫폼이 표시한 승인자, 외부 경로는
    소유자). 표시는 색인에 적어 다른 파드가 도는 턴에도 전하고, 같은 파드의 턴은 바로 알린다.
    turn_id 를 주면 그 시도일 때만 표시한다(P78 2절). 묵은 화면에서 누른 중단이 다음 턴을 멈추지
    않게 한다. 여러 번 불러도 같다. memory 가 없으면 색인이 파드 안이라 다른 파드의 세션은
    404 다."""
    record = await load_session(state, session_id)
    check_caller(caller, record, or_approver=True)
    refuse_external_run(caller, record)
    marked = await state.store.index.request_cancel(session_id, turn_id=body.turn_id)
    if marked is None:
        raise error(409, "no_turn_in_progress", "이 세션에 진행 중인 턴이 없다")
    # 표시한 리스의 턴만 바로 알린다. 리스를 넘긴 뒤에도 감시가 남은 앞 턴을 멈추지 않는다
    watch = state.cancels.get(session_id)
    if watch is not None and watch.token == marked:
        watch.notify()
    return {"session_id": session_id}


@router.post("/turns/cancel", status_code=202)
async def cancel_all_turns(
    body: InstanceRequest, caller: CallerDep, state: Runtime
) -> dict[str, Any]:
    """이 인스턴스의 진행 중 턴을 모두 멈춘다(P78 5절). 배포 해제와 삭제가 인스턴스 토큰을 회수하기
    전에 플랫폼 경로로 부른다. 표시는 색인에 적어 다른 파드의 턴에도 전하고, 이 파드의 턴은 바로
    알린다. `running` 은 표시를 적은 수, 곧 아직 리스가 살아 있는 턴의 수다. 플랫폼은 이것이 0 이 될
    때까지 다시 부른다."""
    del body  # user_sub 는 Caller 가 읽었다
    if caller.external:
        raise error(403, "forbidden", "인스턴스의 턴은 플랫폼 경로에서만 멈춘다")
    running = await state.store.index.cancel_all()
    notify_all(state.cancels)
    return {"running": running}


@router.post("/drain")
async def mark_drain(body: DrainRequest, caller: CallerDep, state: Runtime) -> dict[str, Any]:
    """이 인스턴스의 drain 표시를 본문의 id 로 세우고 살아 있는 리스 수를 준다(P44 7절). 플랫폼이
    Agent 를 0 으로 줄이기 직전에 부른다. 표시는 색인에 서므로 모든 파드의 잡기가 물러서고, 세기와
    잡기가 원자적이라 셈에 들지 않은 턴은 시작하지 않는다. 다시 부르면 표시의 id 와 시간을 바꾸고
    다시 센다. 표시는 `DRAIN_MARK_SECONDS` 뒤에 풀리거나 같은 id 의 `DELETE /drain` 이 지운다."""
    if caller.external:
        raise error(403, "forbidden", "drain 표시는 플랫폼 경로에서만 다룬다")
    running = await state.store.index.mark_drain(mark=body.mark, seconds=DRAIN_MARK_SECONDS)
    return {"running": running}


@router.delete("/drain", status_code=204)
async def clear_drain(
    caller: CallerDep,
    state: Runtime,
    mark: str = Query(pattern=DRAIN_MARK_PATTERN),
) -> Response:
    """id 가 mark 인 drain 표시를 지운다(P44 7절). 플랫폼이 깨운 Agent 를 깨어 있음으로 옮길 때,
    재우기를 그만둘 때 부른다. 표시가 없거나 id 가 다르면(다음 재우기가 새로 세웠다) 아무것도 하지
    않고 그래도 204 다."""
    if caller.external:
        raise error(403, "forbidden", "drain 표시는 플랫폼 경로에서만 다룬다")
    await state.store.index.clear_drain_mark(mark)
    return Response(status_code=204)


@router.get("/metrics")
async def metrics(caller: CallerDep, state: Runtime) -> dict[str, Any]:
    if caller.external:
        raise error(403, "forbidden", "메트릭은 플랫폼 경로에서만 본다")
    daily_limit = state.config.service["budget"]["daily_tokens"]
    daily: dict[str, Any] | None = None
    if daily_limit:
        window = day_window(utcnow())
        key = daily_tokens_key(state.config.instance["id"], window)
        daily = {
            "used": await state.store.counters.get(key),
            "limit": daily_limit,
            "window": window.suffix,
        }
    # 모든 레플리카에 같은 공유 값만 싣는다. 파드 하나의 카운터는 Service 뒤에서 부를 때마다 다른
    # 파드의 값이라 두지 않는다(P37 11절)
    return {
        "sessions": await state.store.index.counts(),
        "totals": await state.store.index.totals(),
        "daily_tokens": daily,
        "tools": {
            "count": len(state.agent.tools),
            "loaded_at": state.sources.loaded_at,
            "last_error": state.sources.last_error,
            # 소스 ref 마다 tools/list 를 읽은 연결이 맺은 MCP 규약 판이다(P35)
            "protocols": state.sources.protocols,
        },
        # 컴파일 경고는 같은 문서와 같은 소스 목록에서 나오므로 레플리카마다 같다. 목록을 다시 읽는
        # 사이에만 잠시 다를 수 있다
        "warnings": state.store.warnings + state.agent.warnings,
        "runtime": runtime_report(),
        "chat": chat_report(state.config, state.agent.model),
        # 이벤트 보내기의 수다(P37 3절). 수집 주소가 없는 파드는 None 이다
        "telemetry": state.events.shipper.report() if state.events.shipper is not None else None,
    }
