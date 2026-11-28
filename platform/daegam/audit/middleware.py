"""플랫폼 API 감사 미들웨어.

모든 쓰기 요청(POST/PUT/PATCH/DELETE)을 응답이 나간 뒤 `audit_events` 에 남긴다. 순수 ASGI
미들웨어라 스트리밍 응답을 막지 않고, 라우터가 scope 에 남긴 route·path_params·state(principal,
tenant) 를 응답 후에 읽는다. 기록에 실패해도 요청은 성공한다(로그만 남긴다).

인증 전 경로의 OAuth 핸들러(P33 18절)는 state 에 세 가지를 더 둔다. `client_id` 는 어느 MCP
클라이언트가 불렀는지(client_ref)이고, `audit_action` 은 핸들러 이름 대신 쓸 행동 이름(토큰 요청의
`oauth_token_code`·`oauth_token_refresh`)이며, `audit_error_code` 는 응답의 `error` 와 다르게 남길
오류 코드(갱신 토큰의 `refresh_reuse`·`refresh_reuse_grace`)다. 오류 본문은 플랫폼의 모양과 OAuth
명세의 모양(`{error, error_description}`)을 둘 다 읽는다.

X-Request-ID 는 없으면 만들어 응답 헤더로 돌려준다(모든 요청).

`via` 와 내부 호출의 request_id 는 ASGI scope state 에서만 읽는다(비서 실행기의 `via_app` 이 넣는다,
D31). `X-Daegam-Via` 같은 헤더는 어디서도 읽지 않으므로 외부 요청이 `via` 를 위조할 수 없다.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from daegam.audit.store import AuditEvent, MongoAuditStore
from daegam.urls import (
    AGENT_CALL_PATHS,
    API_PREFIX,
    HOOK_PATHS,
    SANDBOX_PATHS,
    TEAMS_MESSAGES_PATHS,
    TELEMETRY_PATHS,
    TURN_GRANT_PATHS,
    exact_api_paths,
)

logger = logging.getLogger("daegam.audit")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REQUEST_ID_HEADER = b"x-request-id"
_MAX_ERROR_BODY = 64 * 1024
# 감사에서 빼는 경로 접미사. initContainer 의 스펙·profile.json 다운로드는 GET 이라
# 어차피 안 남지만 명시해 둔다.
EXCLUDED_SUFFIXES = ("/openapi.yaml", "/profile.json")
# 감사에서 빼는 기계의 쓰기 (P37 3절·8절, P53 4절). 런타임 이벤트와 게이트웨이 사용량이 몇 초마다
# 오고, 턴 위임의 갱신이 몇 분마다 온다. 위임은 그 기록이, 호출은 MCP 감사가 남긴다. 경로 전체를
# 맞대므로 같은 끝을 가진 다른 경로는 빠지지 않는다. 웹훅 트리거의 공개 경로는 위조한 호출마다 행을
# 남기지 않게 스스로 센 행을 쓴다(P41 4절). 샌드박스의 열기·쓰기 표시·닫기는 기록과 이벤트가
# 남긴다(P40 5절). Teams 봇의 메시지 엔드포인트도 웹훅처럼 스스로 행을 쓴다(P47 7절). 다른 Agent 를
# Tool 로 부르는 호출 경로는 요청마다 사람과 두 Agent 를 실은 행을 스스로 쓴다(P46 6.8절)
UNAUDITED_PATHS = (
    exact_api_paths(TELEMETRY_PATHS),
    TURN_GRANT_PATHS,
    HOOK_PATHS,
    SANDBOX_PATHS,
    TEAMS_MESSAGES_PATHS,
    AGENT_CALL_PATHS,
)
# 성공해도 남기는 읽기. 읽기인데도 플랫폼이 밖으로 나가는 호출을 일으키는 자리다 (D48). 허브 항목의
# 주소는 사용자가 등록 때 적은 임의의 URL 이라, 누가 어떤 항목으로 플랫폼을 내보냈는지가 보안
# 기록이다. 그전에는 그 호출을 manage 의 refresh-tools(POST)만 일으켜 쓰기로 남았다.
#
# 피드백 내보내기(D61)는 남의 평가를, 인용 원문 내려받기는 테넌트 문서를 밖으로 내는 자리라 같은
# 까닭으로 남긴다. Skill 리비전 내보내기(D64)도 다른 테넌트에 공유된 Skill 을 내보낼 수 있어 남긴다.
#
# 판정은 라우팅 전이라 매칭된 라우트를 볼 수 없고 경로 문자열로 한다. 그래서 `/tools` 로 끝나는지가
# 아니라 경로 전체 모양을 본다. 접미사만 보면 이름이 `tools` 인 다른 리소스의 조회까지 걸린다.
#
# 호출자에 따라 남길지가 갈리는 읽기는 핸들러가 state 의 `audit_read` 로 알린다(`api/_common.py` 의
# `audit_read`). 세션 파일 다운로드가 그렇다. 자기 세션 파일은 남기지 않고, 승인자로 부른 다운로드는
# 남의 파일을 밖으로 낼 수 있어 남긴다.
AUDITED_READ_PATHS = (
    re.compile(rf"^{re.escape(API_PREFIX)}/hub/[^/]+/tools$"),
    re.compile(rf"^{re.escape(API_PREFIX)}/agents/[^/]+/feedback/export$"),
    # 평가 사례는 대화에서 복사한 질문과 답을 담는다 (P45 7절)
    re.compile(rf"^{re.escape(API_PREFIX)}/agents/[^/]+/evaluation-sets/[^/]+/cases$"),
    # 실행의 결과는 시작한 사람의 권한으로 읽은 답과 Tool 결과를 담는다 (P45 9절)
    re.compile(rf"^{re.escape(API_PREFIX)}/agents/[^/]+/evaluation-sets/[^/]+/runs/[^/]+$"),
    re.compile(
        rf"^{re.escape(API_PREFIX)}/agents/[^/]+(?:/preview)?/sessions/[^/]+/citations/[^/]+/source$"
    ),
    re.compile(rf"^{re.escape(API_PREFIX)}/skills/[^/]+/revisions/[^/]+/archive$"),
)


def _audited_read(path: str) -> bool:
    return any(p.match(path) for p in AUDITED_READ_PATHS)


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return None


def resource_of(path: str) -> str | None:
    """API 접두사(/api) 아래 첫 경로 조각. 접두사가 없는 경로도 그대로 받는다."""
    if path.startswith(f"{API_PREFIX}/"):
        path = path[len(API_PREFIX) :]
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else None


def resource_id_of(path_params: dict[str, Any]) -> str | None:
    """경로 파라미터에서 리소스 id 하나. *_id 가 우선, 없으면 tenant·version 이 아닌 첫 값."""
    for k, v in path_params.items():
        if k.endswith("_id"):
            return str(v)
    for k, v in path_params.items():
        if k not in ("tenant", "version", "n", "format"):
            return str(v)
    return None


def _error_code(body: bytes) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body[:_MAX_ERROR_BODY])
    except ValueError:
        return None
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            return detail["code"]
        if isinstance(data.get("code"), str):
            return data["code"]
        if isinstance(data.get("error"), str):
            return data["error"]
        if isinstance(detail, list):
            return "validation_error"
    return None


class AuditMiddleware:
    def __init__(self, app: ASGIApp, *, store_getter: Any):
        """store_getter() 는 앱 상태의 MongoAuditStore 를 돌려준다(없으면 None).
        lifespan 뒤에 생기므로 호출 시점에 읽는다."""
        self.app = app
        self.store_getter = store_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        # 인프로세스 호출(비서)은 state 에 "<턴 request_id>.<n>" 을 미리 넣는다. 헤더보다 앞선다
        request_id = (
            state.get("request_id") or _header(scope, REQUEST_ID_HEADER) or uuid.uuid4().hex
        )
        state["request_id"] = request_id
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        excluded = path.endswith(EXCLUDED_SUFFIXES) or any(p.match(path) for p in UNAUDITED_PATHS)
        # 쓰기는 늘 남긴다. 읽기는 거절(401·403)만 남긴다. 누가 무엇을 열어 보려다 막혔는지가 보안
        # 기록이고, 성공한 읽기까지 남기면 표가 조회로 넘친다. 읽기의 판정은 응답 상태를 보고야
        # 할 수 있다
        audited = (method in WRITE_METHODS or _audited_read(path)) and not excluded

        status_holder: dict[str, Any] = {"status": None, "body": bytearray(), "capture": False}
        started = time.monotonic()

        def audited_now() -> bool:
            """핸들러가 state 에 남긴 읽기 감사 표시까지 본다. 응답을 시작할 때와 끝난 뒤에 본다."""
            return audited or (bool(state.get("audit_read")) and not excluded)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers") or [])
                if not any(k == REQUEST_ID_HEADER for k, _ in headers):
                    headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
                status_holder["capture"] = (
                    audited_now() or (not excluded and message["status"] in (401, 403))
                ) and message["status"] >= 400
            elif message["type"] == "http.response.body" and status_holder["capture"]:
                if len(status_holder["body"]) < _MAX_ERROR_BODY:
                    status_holder["body"].extend(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if audited_now():
                await self._record(scope, method, path, request_id, 500, b"", started)
            raise
        status = int(status_holder["status"] or 500)
        recorded = audited_now()
        denied_read = not recorded and not excluded and status in (401, 403)
        if recorded or denied_read:
            body = bytes(status_holder["body"])
            await self._record(scope, method, path, request_id, status, body, started)

    async def _record(
        self,
        scope: Scope,
        method: str,
        path: str,
        request_id: str,
        status: int,
        error_body: bytes,
        started: float,
    ) -> None:
        store: MongoAuditStore | None = self.store_getter()
        if store is None:
            return
        try:
            state = scope.get("state") or {}
            principal = state.get("principal")
            # Starlette 는 매칭된 라우트의 endpoint(핸들러 함수)와 path_params 를 scope 에 남긴다.
            # 라우트 이름은 기본이 핸들러 이름이므로 그것을 action 으로 쓴다.
            # 매칭 전 실패(404·405)는 "METHOD path" 다.
            endpoint = scope.get("endpoint")
            path_params = dict(scope.get("path_params") or {})
            tenant = state.get("tenant") or path_params.get("tenant")
            action = (
                state.get("audit_action")
                or getattr(endpoint, "__name__", None)
                or f"{method} {path}"
            )
            if status in (404, 405):
                # 405 는 경로만 맞은 다른 메서드의 핸들러가 scope 에 남는다. 그 이름은 쓰지 않는다
                action = f"{method} {path}"
            via = state.get("via")
            event = AuditEvent(
                sub=getattr(principal, "sub", None),
                subject_type=getattr(principal, "subject_type", "user"),
                # local 로그인 핸들러가 시도한 이름을 남긴다 (D40). 실패한 시도는 principal 이 없다
                login_username=state.get("login_username"),
                client_id=state.get("client_id"),
                tenant=str(tenant) if tenant else None,
                action=action,
                resource=resource_of(path),
                # 경로에 id 가 없거나 여럿인 라우트는 핸들러가 state 에 둔다 (개인 토큰 발급·회수)
                resource_id=state.get("audit_resource_id") or resource_id_of(path_params),
                path_params={k: str(v) for k, v in path_params.items()},
                method=method,
                path=path,
                request_id=request_id,
                status=status,
                result="ok" if status < 400 else "error",
                error_code=(
                    state.get("audit_error_code") or _error_code(error_body)
                    if status >= 400
                    else None
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
                via=str(via) if via else None,
            )
            await store.record(event)
        except Exception:  # noqa: BLE001 - 감사 실패는 요청을 실패시키지 않는다
            logger.exception("audit record failed: %s %s (%s)", method, path, request_id)
