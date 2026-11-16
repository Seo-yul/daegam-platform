"""진입점이다. /config/agent.json 을 deepagents 그래프로 만들어 FastAPI :8080 에 올린다.

앱을 만들고 lifespan 에서 boot.py 로 자원(체크포인터·색인·knowledge·장기 기억·도구 소스)을 열어
RuntimeState 를 건다. 자원 열기와 배경 작업은 boot.py, HTTP 표면은 routes.py, 턴의 수명은 turn.py,
입구 검사 순서는 turn_gate.py 가 적는다.

저장소(Database·Redis)에 아직 연결하지 못하거나(T22·T23) 도구 서버를 못 읽어도(T18) 기동을 막지
않는다. 그동안 살아 있음 검사는 200 이고 준비 검사가 503 이다. 그 정책은 boot.py 가 적는다.

문서의 features 에 이 이미지가 모르는 이름이 있으면(runtime_image_too_old) 죽지 않고 채팅을 받지
않는 채로 뜬다(D65). 살아 있음 검사 /healthz 는 200 이라 다시 뜨기를 되풀이하지 않고, 준비 검사
/healthz?probe=ready 가 503 이라 새 파드가 Ready 가 되지 않아 롤링이 멈추고 옛 파드가 계속 답한다.

환경변수
  DAEGAM_AGENT_CONFIG   agent.json 경로. 기본 /config/agent.json
  DAEGAM_INSTANCE_TOKEN 있으면 healthz 를 뺀 모든 요청에 Authorization: Bearer 를 요구한다
  DAEGAM_TENANT         세션 색인의 tenant 열이자 장기 기억 네임스페이스의 첫 라벨이다(D34).
                        플랫폼이 파드 env 로 넣는다. 장기 기억이 켜졌는데 비어 있으면 기동 실패다
  DAEGAM_KNOWLEDGE_<n>_API_KEY   knowledge[].embedding.provider.env_key 가 가리키는 임베딩 키(D32)
  DAEGAM_OIDC_ISSUER / DAEGAM_OIDC_AUDIENCE   둘 다 있으면 데이터 플레인이 믿는 발급자의 토큰을
                        받는다
  DAEGAM_OIDC_JWKS_URL  있으면 discovery 없이 이 주소에서 JWKS 를 받는다. 이것이나 DAEGAM_IDP_ISSUER
                        가 있으면 첫 발급자는 플랫폼이라 위임 토큰은 이 Agent 에 묶인 것만 받고
                        client id 로는 들이지 않는다(auth.py 의 read_issuers)
  DAEGAM_OIDC_INSECURE_HTTP   true 면 평문 http 발급자·JWKS 를 받는다(클러스터 안 주소 전용)
  DAEGAM_ALLOWED_GROUPS / DAEGAM_AGENT_CLIENT_IDS   DAEGAM_OIDC_ISSUER 토큰의 인가(콤마). 그룹이
                        비어 있으면 그 발급자가 audience 로 서명한 주체 전부
  DAEGAM_IDP_*          데이터 플레인이 믿는 발급자 곁의 IdP(능력 two_issuers). 위 이름들과 같은
                        뜻이고 IdP 토큰에만 쓰인다(auth.py 의 IDP_ENV). audience 는
                        DAEGAM_OIDC_AUDIENCE 를 같이 쓰고, IdP 는 플랫폼이 아니라 위임 토큰을 묶지
                        않으며 sub 가 sa:·svc: 로 시작하는 토큰은 막는다
  DAEGAM_CORS_ORIGINS   콤마. 비어 있으면 CORS 미들웨어를 붙이지 않는다
  DAEGAM_JWKS_REFRESH_MIN_SECONDS   모르는 kid 로 인한 JWKS 재조회 최소 간격. 기본 30
  DAEGAM_TRANSCRIPT_RETENTION_DAYS  닫힌 대화의 기록이 남는 날 수. 기본 30, 0 이면 닫을 때 지운다
  RUNTIME_HOST / RUNTIME_PORT   기본 0.0.0.0 / 8080
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TextIO

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from runtime import error_codes
from runtime.attachments import MAX_CHAT_BODY_BYTES, MIB
from runtime.boot import Boot, Injected, write_termination_message
from runtime.config import AgentConfig, ConfigError, load_config
from runtime.drain import Drain, DrainServer
from runtime.events import EventLog
from runtime.features import UNKNOWN_FEATURE_CODE
from runtime.knowledge import KnowledgeTools
from runtime.long_term import LongTermMemory
from runtime.mcp import ToolSources
from runtime.memories_api import router as memories_router
from runtime.regenerate import router as regenerate_router
from runtime.routes import (
    DEPENDENCIES_UNAVAILABLE,
    DEPENDENCIES_UNAVAILABLE_MESSAGE,
    HEALTHZ_PATH,
    router,
)
from runtime.session_files import router as session_files_router
from runtime.sessions import SessionStore
from runtime.sessions_api import router as sessions_router
from runtime.shipper import shipper_from_env
from runtime.state import Settings
from runtime.turn import DETACHED_HEADER, SESSION_HEADER, TURN_ID_HEADER
from runtime.turn_gate import UNREADY_RETRY_AFTER_SECONDS
from runtime.validation import validation_error

log = logging.getLogger("daegam.agent-runtime")


def create_app(
    config: AgentConfig | None = None,
    settings: Settings | None = None,
    *,
    model: BaseChatModel | None = None,
    sources: ToolSources | None = None,
    store: SessionStore | None = None,
    knowledge: KnowledgeTools | None = None,
    long_term: LongTermMemory | None = None,
    event_out: TextIO | None = None,
    oidc_http_client: httpx.AsyncClient | None = None,
    refusal: ConfigError | None = None,
) -> FastAPI:
    """앱을 만든다. config·model·sources·store·knowledge·long_term 은 테스트가 끼운다. 없으면
    env 와 agent.json 이다. refusal 이 있으면 자원을 열지 않고 채팅을 받지 않는 앱이다(D65).

    store 를 주면 앱이 닫혀도 닫지 않는다. 같은 프로세스의 앱 둘이 체크포인터·색인을 나눠 쓰는
    교차 파드 재개 테스트가 쓴다. sources·knowledge·long_term 도 같아서 주면 여는 것을 건너뛰고 앱이
    닫혀도 닫지 않는다(앱 둘이 같은 기억을 나눠 쓰는 테스트, 가짜 풀을 끼운 테스트).
    oidc_http_client 는 검증기가 discovery·JWKS 를 받을 클라이언트다(테스트가 MockTransport 를
    끼운다).
    """
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if refusal is not None:
            app.state.refusal = refusal
            yield
            return
        cfg = config or load_config(settings.config_path)
        for warning in cfg.warnings:
            log.warning("agent.json: %s", warning)
        evlog = EventLog(cfg, out=event_out)
        # 수집 주소가 있으면 같은 이벤트를 플랫폼에도 밀어 보낸다(P37 3절). 보내는 작업은 기동 뒤에
        # 뜬다(boot.py)
        evlog.shipper = shipper_from_env(
            dict(os.environ), token=settings.instance_token, revision=cfg.profile["revision"]
        )
        injected = Injected(
            model=model,
            sources=sources,
            store=store,
            knowledge=knowledge,
            long_term=long_term,
            oidc_http_client=oidc_http_client,
        )
        # 스택에는 여기서 연 자원만 올린다. 주입받은 것은 준 쪽이 닫는다. 앱이 닫히면 연 순서의
        # 역순으로 닫는다(작업, 검증기, 도구 소스, 장기 기억, knowledge, 세션 저장소)
        async with AsyncExitStack() as stack:
            boot = Boot(
                app=app, cfg=cfg, settings=settings, evlog=evlog, injected=injected, stack=stack
            )
            await boot.start()
            yield
            # 종료다. 남은 턴의 마감을 스택(이벤트 보내기, 도구 소스, 위임, 저장소)을 닫기 전에
            # 기다린다(P78 5절, drain.py)
            runtime_state = getattr(app.state, "runtime", None)
            if runtime_state is not None:
                await app.state.drain.settle(runtime_state)

    app = FastAPI(title="daegam agent-runtime", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.drain = Drain(settings.turn_drain_seconds)
    # 자원을 열기 전(T22)의 healthz 가 인스턴스 토큰을 볼 때 읽는다
    app.state.settings = settings
    # 채팅을 받을 수 없는 동안(D65 의 거절, T22 의 자원 열기) healthz 만 라우터로 가고 나머지는
    # 의존성(상태·주체)에 들어가기 전에 503 이다. CORS 보다 안쪽이라 노출 경로의 거절에도
    # Allow-Origin 이 붙는다
    app.add_middleware(RefusingMiddleware)
    # 본문 상한이다. CORS 보다 안쪽이라 노출 경로의 413 에도 Allow-Origin 이 붙는다
    app.add_middleware(BodyLimitMiddleware)
    if settings.cors_origins:
        # 가장 바깥이라 SSE 200 과 예외 핸들러의 401·403·429 전부에 Allow-Origin·Vary 가 붙는다.
        # 자격은 Authorization 헤더이지 쿠키가 아니므로 credentials 는 내지 않는다(D27)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            # PATCH·DELETE 는 소유자의 이름 바꾸기와 지우기다(sessions_api.py)
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["authorization", "content-type"],
            # 세션 id 헤더는 첫 턴을 멈추는 데 쓴다(D65). 내려받기 이름은 세션 파일
            # (session_files.py)의 저장 이름이다. 브라우저가 읽으려면 둘 다 드러내야 한다
            expose_headers=[
                "Retry-After",
                SESSION_HEADER,
                TURN_ID_HEADER,
                DETACHED_HEADER,
                "Content-Disposition",
            ],
            max_age=600,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"code": "error", "message": str(exc.detail)}
        )
        code = str(detail.get("code"))
        if error_codes.STRICT and not error_codes.allowed(exc.status_code, code):
            raise AssertionError(f"{exc.status_code} {code} 가 오류 코드표와 다르다")
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """본문·쿼리가 요청 모델을 지나지 못했다. 런타임 오류 모양으로 내고 받은 값은 싣지 않는다
        (P76 F1). 턴 거절이 아니라 호출자의 잘못이라 세지 않는다."""
        return await _http_error(request, validation_error(exc.errors()))

    app.include_router(router)
    app.include_router(sessions_router)
    app.include_router(regenerate_router)
    app.include_router(memories_router)
    app.include_router(session_files_router)
    return app


class RefusingMiddleware:
    """채팅을 받을 수 없는 동안 healthz 를 뺀 모든 요청을 503 으로 끊는다. 응답 모양은 `_http_error`
    와 같다. 이유는 둘이다. 문서에 모르는 기능 이름이 있으면(D65) 끝까지 받지 않는다. 저장소에
    아직 연결하지 못했으면(T22·T23) 연결할 때까지 받지 않고 Retry-After 를 단다. 이유는 요청마다
    앱 상태에서 읽는다. 자원 열기가 끝나면 상태가 비어 그대로 지나간다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != HEALTHZ_PATH:
            state = scope["app"].state
            refusal = getattr(state, "refusal", None)
            if refusal is not None:
                # 처리기를 거치지 않고 답하므로 상태를 코드표에서 찾는다 (P74 2.2절)
                detail = {"code": refusal.code, "message": refusal.message}
                status = error_codes.status_of(refusal.code, 503)
                await JSONResponse(detail, status_code=status)(scope, receive, send)
                return
            if getattr(state, "starting", None) is not None:
                headers = {"Retry-After": str(UNREADY_RETRY_AFTER_SECONDS)}
                detail = {
                    "code": DEPENDENCIES_UNAVAILABLE,
                    "message": DEPENDENCIES_UNAVAILABLE_MESSAGE,
                }
                await JSONResponse(detail, status_code=503, headers=headers)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# 플랫폼의 본문 상한과 같은 코드다 (P74 2.3절)
PAYLOAD_TOO_LARGE = "payload_too_large"


def _payload_too_large() -> HTTPException:
    message = f"요청 본문이 {MAX_CHAT_BODY_BYTES // MIB}MiB 를 넘는다"
    return HTTPException(413, detail={"code": PAYLOAD_TOO_LARGE, "message": message})


class BodyLimitMiddleware:
    """요청 본문을 `MAX_CHAT_BODY_BYTES` 에서 끊는다(P36 9절). 첨부를 실은 `/chat` 이 가장 큰
    본문이라 그 상한을 모든 경로에 건다. `Content-Length` 가 넘으면 읽기 전에 413 이고, 그
    헤더가 없거나 거짓이어도 읽으면서 세다가 넘는 순간 413 이다. 읽는 쪽(FastAPI)은 받기에서 난
    HTTPException 을 그대로 올리므로 응답 모양은 `_http_error` 와 같다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > MAX_CHAT_BODY_BYTES:
            error = _payload_too_large()
            await JSONResponse(error.detail, status_code=error.status_code)(scope, receive, send)
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_CHAT_BODY_BYTES:
                    raise _payload_too_large()
            return message

        await self.app(scope, counted, send)


def main() -> int:
    import uvicorn

    logging.basicConfig(level=os.environ.get("RUNTIME_LOG_LEVEL", "INFO"), stream=sys.stderr)
    settings = Settings.from_env()
    try:
        config = load_config(settings.config_path)
    except ConfigError as e:
        if e.code != UNKNOWN_FEATURE_CODE:
            log.error("agent.json 오류 %s", e)
            write_termination_message(str(e))
            return 3
        # 이미지가 낡았다. 죽으면 파드가 되풀이해 뜨므로 채팅만 거절하고 준비 검사를 실패시킨다
        log.error("agent.json 오류 %s. 채팅을 받지 않는다", e)
        app = create_app(settings=settings, refusal=e)
    else:
        app = create_app(config, settings)
    # SIGTERM 에 drain 시계를 걸고 열린 읽기를 drain 과 마감 몫까지 기다린다(P78 5절)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        timeout_graceful_shutdown=app.state.drain.graceful_timeout,
    )
    DrainServer(config, drain=app.state.drain, app=app).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
