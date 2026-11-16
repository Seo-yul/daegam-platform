"""Agent 런타임 프록시 라우트가 함께 쓰는 도우미 (D21, D65, D61).

세션 소유자는 호출자의 `sub` 이고 `agent:approve` scope 가 있으면 승인자다. 런타임 오류 본문
`{code, message, ...}` 는 플랫폼 형식 `{code, detail, ...}` 로 올린다.

런타임 능력에 기대는 라우트는 두 겹으로 가른다. 먼저 이 에이전트의 기록
(`status.runtime.features`, 스냅숏 이미지의 init 이 알린 이름)에 그 이름이 없으면 런타임을
부르지 않고 409 runtime_feature_missing 이다. 기록은 새 파드의 init 이 쓰므로 롤링 중에는 기록에
이름이 있어도 옛 파드가 요청을 받을 수 있다. 그 파드는 새 라우트를 모르므로 FastAPI 기본
404·405 로 답하고, 본문에 `code` 가 없고 `detail` 이 글자인 그 모양만 409 runtime_rolling 으로
바꾼다. `session_not_found` 같은 런타임의 404 는 그대로 넘긴다.

옛 파드가 거절하지 않고 조용히 버리는 요청은 따로 가른다. 세션 목록의 제목 검색은 런타임이
되돌려준 `q` 가 없으면 옛 파드가 답한 것이다(`runtime_rolling`). 첨부 턴은 phase 까지 보는
`require_rolled_out` 으로 가른다(`turn_attachments`). 문서에 기억된 phase 가 Ready 가 아니면 상태를
한 번 새로 읽어 그 값으로 다시 가른다. 파드가 모두 지금 템플릿이고 하나가 준비됐으면 phase 가
Rolling 이어도 보낸다. 깨운 Agent 가 레플리카를 모두 띄우기 전이 그렇다(P44 8절).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import Response

from daegam.api._common import error, or_404
from daegam.api._download import download_response
from daegam.api._sse import ClosingStreamingResponse
from daegam.auth.principal import Principal
from daegam.errors import ForwardedError
from daegam.services.agent_features import ATTACHMENTS, RUNTIME_FEATURE_MISSING, RUNTIME_ROLLING
from daegam.services.agent_runtime import ProxyResponse, read_json
from daegam.services.agent_status import READY_PHASE
from daegam.services.agents import AgentService
from daegam.services.approval_choices import ApprovalChoiceService
from daegam.services.chat_uploads import ChatUploadService
from daegam.services.runs.service import RunService
from daegam.services.turn_person import TurnDelegation
from daegam.store.agents import Agent, MongoAgentStore

APPROVE_SCOPE = "agent:approve"
# FastAPI 가 모르는 경로와 메서드에 내는 상태다. 본문은 `{"detail": "Not Found"}` 꼴이다
ROUTE_MISSING_STATUSES: frozenset[int] = frozenset({404, 405})


def get_turns(request: Request) -> TurnDelegation:
    """세 턴 라우트(채팅, 결정, 다시 생성)가 런타임을 부르는 앞뒤의 일이다(P53). 위임을 열고
    소유자 기록을 적고 위임을 스스로 끝낸다."""
    state = request.app.state
    return TurnDelegation(state.turn_grants, state.agent_session_owners)


def get_approval_choices(request: Request) -> ApprovalChoiceService:
    """기억한 승인 선택이다(P43 5절). 세 턴 라우트가 싣고 결정이 기억하며 계정 화면이 보고
    지운다."""
    return request.app.state.approval_choices


def get_run_service(request: Request) -> RunService:
    return request.app.state.run_service


def get_enabled_runs(request: Request) -> RunService | None:
    """백그라운드 실행을 켰을 때의 실행 서비스(P41). 끄면 None 이라 세션 라우트가 실행 기록을 보지
    않는다."""
    return get_run_service(request) if request.app.state.settings.runs_enabled else None


async def get_agent_or_404(store: MongoAgentStore, tenant: str, agent_id: str) -> Agent:
    return or_404(await store.get(agent_id, tenant=tenant), "agent", agent_id)


def is_approver(user: Principal, tenant: str) -> bool:
    return APPROVE_SCOPE in user.scopes_in(tenant)


def check_owner(body: Any, user_sub: str, approver: bool) -> None:
    """런타임 응답에 세션 소유자가 있으면 소유자 또는 approver 만 본다."""
    if approver or not isinstance(body, dict):
        return
    session = body.get("session") if isinstance(body.get("session"), dict) else body
    owner = session.get("user_sub")
    if owner is not None and owner != user_sub:
        raise error(403, "not_session_owner", "session belongs to another user")


async def owned_session(
    service: AgentService,
    doc: Agent,
    session_id: str,
    user: Principal,
    *,
    approver: bool,
    view: str | None = None,
) -> Any:
    """런타임 세션 상세다. 소유자이거나 approver 일 때만 돌려주고 런타임의 오류는 그대로 올린다.
    approver 면 승인자 헤더를 실어 남의 세션도 읽는다. view 는 진행 중 턴을 따라가는 가벼운
    읽기(`progress`, P78 2절)다."""
    st, body = await service.proxy.session(
        doc, session_id, user_sub=user.sub, approver=approver, view=view
    )
    expect_status(st, body)
    check_owner(body, user.sub, approver)
    return body


def require_runtime_feature(doc: Agent, name: str) -> None:
    """런타임 능력에 기대는 프록시 라우트의 가르기다(D65). 이 에이전트의 스냅숏 이미지가 알린 기록에
    그 이름이 없으면 기록이 아예 없을 때도 런타임을 부르지 않고 409 다. 옛 이미지의 404·405 에
    판정을 맡기지 않는다."""
    if not doc.image_reports(name):
        raise error(
            409,
            RUNTIME_FEATURE_MISSING,
            f"the deployed runtime image has not reported the {name!r} capability",
            feature=name,
        )


async def require_rolled_out(service: AgentService, doc: Agent, name: str) -> None:
    """옛 파드가 조용히 버리는 요청의 가르기다. 기록에 이름이 있고 phase 가 Ready 여야
    보낸다. 첨부 턴(P36 F11b)이 부른다.

    기억된 phase 는 상태 조회가 값을 바꿀 때만 적히므로, Ready 가 아니면 거절하기 전에 상태를
    한 번 새로 읽어 그 phase 로 다시 가른다. 재배포 뒤 아무도 상태를 보지 않았다고 계속 막히지
    않게 하려는 것이다. 그래도 Ready 가 아니면, 모든 파드가 지금 템플릿이고 하나가 준비됐을 때만
    보내고 아니면 409 runtime_rolling 이다. 옛 템플릿의 파드는 첨부를 조용히 버리지만, 지금
    템플릿의 파드만 남았으면 원하는 수가 모두 준비되기 전이라도 받는다. 깨운 Agent 가 레플리카를
    모두 띄우기 전이 그렇다(P44 8절). 잠든 Agent 는 부르는 라우트의 게이트가 먼저 깨운다."""
    require_runtime_feature(doc, name)
    phase = doc.last_phase
    if phase != READY_PHASE:
        observed = await service.status(doc)
        phase = observed["phase"] if observed is not None else None
    if phase != READY_PHASE and not await service.sleep.ready_now(doc):
        raise error(
            409,
            RUNTIME_ROLLING,
            f"the agent is {phase or 'not observed'}, not {READY_PHASE}; "
            "the rollout may still be in progress",
            feature=name,
        )


async def turn_attachments(
    service: AgentService,
    uploads: ChatUploadService,
    doc: Agent,
    user_sub: str,
    ids: list[str],
) -> list[dict[str, Any]] | None:
    """턴에 싣는 첨부의 런타임 모양이다(P36 9절). 없으면 None 이다. 업로드마다 테넌트·Agent·sub 와
    만료를 먼저 보고(422 unknown_upload), 그다음 첨부를 아는 새 파드만 받는지 가른다. 업로드는
    이 자리에서 지우지 않는다. 입구에서 거절되면 다시 보낼 수 있어야 하기 때문이다."""
    if not ids:
        return None
    attachments = await uploads.resolve(doc, user_sub, ids)
    await require_rolled_out(service, doc, ATTACHMENTS)
    return attachments


def runtime_rolling(feature: str) -> HTTPException:
    """기록에는 그 능력이 있는데 그것을 모르는 옛 파드가 답했다. 롤링이 아직 끝나지 않았다."""
    return error(
        409,
        RUNTIME_ROLLING,
        f"a runtime pod without the {feature!r} capability answered; "
        "the rollout is still in progress",
        feature=feature,
    )


def is_route_missing(status_code: int, body: Any) -> bool:
    """FastAPI 기본 404·405 인가. 런타임의 오류는 맨 위에 `code` 가 있어 이 모양이 아니다."""
    return (
        status_code in ROUTE_MISSING_STATUSES
        and isinstance(body, dict)
        and "code" not in body
        and isinstance(body.get("detail"), str)
    )


def runtime_error(
    status_code: int,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
    feature: str | None = None,
) -> HTTPException:
    """런타임 오류 본문 `{code, message, ...extra}` 를 플랫폼 형식 `{code, detail, ...extra}` 로
    올린다. 플랫폼 API 는 스트리밍 경로까지 이 한 형식이라 UI 가 오류 코드를 한 곳에서 읽는다.

    feature 는 런타임 능력에 기대는 라우트가 그 이름을 준다. 그 라우트를 모르는 옛 파드의
    FastAPI 기본 404·405 는 409 runtime_rolling 이다. 넘긴 답은 `ForwardedError` 라 플랫폼의 오류
    코드표 검사가 건너뛰고, 플랫폼 자신의 runtime_rolling 은 평범한 HTTPException 이라 검사를
    받는다 (P74 2.2절)."""
    if feature is not None and is_route_missing(status_code, body):
        return runtime_rolling(feature)
    extra: dict[str, Any] = {}
    if isinstance(body, dict):
        code = str(body.get("code") or "runtime_error")
        detail = str(body.get("message") or body.get("detail") or body)
        extra = {k: v for k, v in body.items() if k not in ("code", "message", "detail")}
    else:
        code, detail = "runtime_error", f"runtime answered HTTP {status_code}"
    return ForwardedError(status_code, {"code": code, "detail": detail, **extra}, headers=headers)


def expect_status(
    status_code: int, body: Any, *, ok: int = 200, feature: str | None = None
) -> None:
    """런타임 JSON 응답의 상태가 ok 가 아니면 `runtime_error` 로 올린다."""
    if status_code != ok:
        raise runtime_error(status_code, body, feature=feature)


async def stream_error(proxied: ProxyResponse, *, feature: str | None = None) -> HTTPException:
    """스트림 응답의 오류다. 오류는 스트림이 시작되기 전에 오므로 본문을 끝까지 읽어(읽기가 끝나면
    런타임 연결이 닫힌다) 플랫폼 형식으로 바꾼다. Retry-After 는 헤더와 `detail.retry_after` 양쪽에
    남는다."""
    body = await read_json(proxied)
    retry_after = proxied.headers.get("retry-after")
    if retry_after and isinstance(body, dict):
        body = {"retry_after": retry_after, **body}
    keep = {"retry-after": retry_after} if retry_after else None
    return runtime_error(proxied.status, body, headers=keep, feature=feature)


async def proxied_sse(
    proxied: ProxyResponse, *, feature: str | None = None
) -> ClosingStreamingResponse:
    """성공 응답은 그대로 넘기고 오류는 `stream_error` 로 올린다. 브라우저가 끊으면
    `ClosingStreamingResponse` 가 본문을 바로 닫아 런타임 연결과 터널이 함께 닫힌다."""
    if proxied.status >= 400:
        raise await stream_error(proxied, feature=feature)
    headers = dict(proxied.headers)
    media_type = headers.pop("content-type", None)
    return ClosingStreamingResponse(
        proxied.body, status_code=proxied.status, media_type=media_type, headers=headers
    )


async def proxied_download(proxied: ProxyResponse, path: str, *, feature: str) -> Response:
    """런타임의 바이트 스트림을 내려받기로 낸다. 런타임의 헤더는 버리고 `_download` 가 다시
    만든다. 세션 파일 내려받기(P36 F12)가 부른다."""
    if proxied.status >= 400:
        raise await stream_error(proxied, feature=feature)
    return download_response(proxied.body, path)
