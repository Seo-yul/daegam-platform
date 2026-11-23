"""FastAPI 앱 팩토리."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from daegam import __version__
from daegam.api.schemas import VersionInfo
from daegam.api_docs import install_api_docs
from daegam.audit.middleware import AuditMiddleware
from daegam.body_limit import MULTIPART_SLACK, BodyLimitMiddleware
from daegam.config import Settings, settings_from_env
from daegam.errors import ForwardedError, ServiceError, check_registered, error
from daegam.k8s.client import ClusterUnreachable
from daegam.secrets import SecretStore
from daegam.security_headers import SecurityHeadersMiddleware, browser_idp_origin
from daegam.services.clusters import ClientFactory
from daegam.services.issuer_switch import held_settings
from daegam.services.spec_generator.worker import SpecJobRunner
from daegam.urls import API_PREFIX
from daegam.wiring import attach, build_services, build_stores


def _api_build() -> str | None:
    """플랫폼 이미지의 빌드. platform/Dockerfile 이 빌드 인자 GIT_SHA(scripts/build-stamp.sh
    platform)를 DAEGAM_BUILD_COMMIT 으로 박는다(D52). 로컬 프로세스(make run)에는 없어 None 이다."""
    commit = (os.environ.get("DAEGAM_BUILD_COMMIT") or "").strip()
    return f"{__version__}+{commit}" if commit else None


def _release() -> str | None:
    """이 플랫폼 이미지를 낸 릴리스 태그. push-images 가 RELEASE 로 구울 때 platform/Dockerfile 이
    DAEGAM_RELEASE 로 박는다. 릴리스가 아닌 빌드와 로컬 프로세스에는 없어 None 이다."""
    return (os.environ.get("DAEGAM_RELEASE") or "").strip() or None


def _configure_logging() -> None:
    """`daegam.*` 로거가 실제로 나가게 한다. 컨테이너는 uvicorn 이 앱을 직접 띄우므로 런타임들처럼
    `main()` 에서 설정할 자리가 없고, uvicorn 기본 설정은 root 핸들러를 두지 않아 INFO 가 통째로
    사라지고 WARNING 은 lastResort 로 시각·로거 이름 없이 찍힌다. basicConfig 는 root 에 핸들러가
    이미 있으면 아무것도 하지 않으므로 pytest 처럼 자기 핸들러를 붙이는 쪽은 그대로 둔다."""
    logging.basicConfig(
        level=os.environ.get("DAEGAM_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    quiet_http_logs()


def quiet_http_logs() -> None:
    """httpx 와 httpcore 는 INFO 에 요청 URL 을 통째로 적는다. 웹 Tool·Knowledge·비서가 가져오는 URL
    의 쿼리에 모델 글이 실릴 수 있으므로 WARNING 위만 남긴다 (P39b 16절)."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


logger = logging.getLogger("daegam.main")


def _warn_about_open_settings(settings: Settings) -> None:
    """기동 때 한 번. 개발용 설정이 다른 사람이 닿는 곳에 놓였을 때 로그에서 바로 보이게 한다."""
    if settings.auth_mode == "header":
        logger.warning(
            "auth_mode=header: every request is an anonymous platform-admin. "
            "Never expose this deployment to other people; switch to oidc first"
        )
    if settings.bootstrap_admin_group:
        logger.warning(
            "bootstrap_admin_group=%s grants platform-admin to that IdP group only until the "
            "first platform-admin group mapping is saved; remove the setting after that",
            settings.bootstrap_admin_group,
        )
    _warn_about_plain_http(settings)
    _log_oauth_state(settings)
    if settings.sandbox_allow_unchecked:
        # 개발용 우회 (P40 8절). 로컬 차트 값 deploy/dev/values-local.yaml 만 켠다
        logger.warning(
            "DAEGAM_SANDBOX_ALLOW_UNCHECKED=true: code execution runs on the home cluster without "
            "the isolation check, and sandbox self-checks only report. Local development only"
        )
    if settings.idp_login and not settings.oidc_client_id:
        # 차트는 oidc 모드에 auth.oidc.clientId 를 요구한다. env 로 깐 배포는 여기서만 알 수 있다
        # (P53b 6.4절)
        logger.warning(
            "auth_mode=oidc without DAEGAM_OIDC_CLIENT_ID: the UI has no client to sign people in "
            "with, and issuing personal or service-account tokens and approving login connections "
            "answer 403 reauth_required; set it to the UI's PKCE client at the IdP"
        )
    if (
        settings.auth_mode == "oidc"
        and settings.oidc_issuer
        and browser_idp_origin(settings) is None
    ):
        # UI 컨테이너만 기동을 거절하는 경우다. UI 파드가 재시작을 되풀이하는 까닭을 여기서도 찾게
        # 한다(D52)
        logger.warning(
            "oidc issuer %r is not an http(s)://host[:port] origin: the browser CSP cannot "
            "open the IdP, so the daegam-ui container refuses to start and browser login "
            "would be blocked",
            settings.oidc_issuer,
        )


def _warn_about_plain_http(settings: Settings) -> None:
    """평문 http 인 주소를 종류마다 따로 말한다. 공개 주소, 사람 토큰의 발급자, 데이터 플레인이
    받는 JWKS 주소는 서로 다른 값이라 어느 쪽이 평문인지 로그에서 가려야 한다 (P32 1절)."""
    if settings.insecure_public_url:
        # 로컬 벌만 http 다(D58). 공개 서버에 두면 로그인과 MCP 클라이언트의 토큰이 평문으로 오간다
        logger.warning(
            "public url %s is plain http: logins and MCP client tokens travel in the clear; "
            "use an https origin anywhere but a local cluster",
            settings.public_url,
        )
    # local 의 사람 발급자가 공개 주소면 위의 경고가 이미 그 주소를 말했다
    if settings.insecure_issuer and settings.issuer != settings.public_url:
        logger.warning(
            "issuer %s is plain http: fine for an in-cluster mock or the platform itself, "
            "never for a real IdP",
            settings.issuer,
        )
    if settings.token_auth and settings.insecure_platform_issuer:
        # 내부 홉의 검증기는 모든 토큰 모드에서, 사람 토큰의 검증기는 local 에서 이 주소의 JWKS 를
        # 평문으로 받는다 (D41, P32 1절). 클러스터 안 svc 주소일 때만 감수하는 것이다
        logger.warning(
            "platform JWKS %s is plain http: data-plane verifiers fetch the signing keys in the "
            "clear; fine for an in-cluster svc address, set DAEGAM_PLATFORM_ISSUER to an https "
            "address when other clusters fetch it",
            settings.platform_jwks_url,
        )


def _log_oauth_state(settings: Settings) -> None:
    """플랫폼이 사람 토큰을 내는 모드(local, oidc)에서 OAuth 인가 서버가 꺼졌거나 잠든 까닭을
    남기고, 평문 허용 플래그로 켜졌으면 경고한다 (P33 1절·15절, P53b 6.5절). header 모드에는 인가
    서버가 없으므로 말하지 않는다."""
    if not settings.platform_mints_human_tokens:
        return
    reason = settings.oauth_off_reason
    if reason is not None:
        logger.info("OAuth authorization server is off: %s", reason)
    elif settings.oauth_dormant:
        logger.info(
            "OAuth authorization server is dormant: the data plane still trusts the IdP, so it "
            "issues nothing until the data plane issuer is the platform"
        )
    elif settings.insecure_public_url:
        logger.warning(
            "DAEGAM_OAUTH_ALLOW_HTTP=true: the OAuth authorization server runs on plain http %s; "
            "authorization codes and refresh tokens travel in the clear. Local e2e only",
            settings.public_url,
        )


def _add_middleware(app: FastAPI, settings: Settings) -> None:
    """본문 상한, 감사, 브라우저 보호 헤더 미들웨어를 차례로 더한다. 나중에 더한 것이 바깥이다."""
    # 요청 본문 상한. 업로드 상한에 multipart 여유를 더한 값이 모든 요청의 상한이다. 업로드 라우트는
    # 그 안에서 자기 상한을 세어 "too_large" 리포트를 낸다. 채팅 첨부 업로드는 그보다 작은 경로
    # 상한이다. 이것을 먼저 더해 가장 안쪽에 둔다. 그래야 읽기 전에 끊은 413 도 감사에 남고 보호
    # 헤더를 받는다. 감사와 보호 헤더는 요청 본문을 읽지 않는다
    from daegam.api.agent_calls import AGENT_CALL_BODY_LIMIT
    from daegam.api.agent_chat import UPLOAD_BODY_LIMIT
    from daegam.api.sandboxes import SANDBOX_BODY_LIMIT
    from daegam.api.skills import IMPORT_BODY_LIMIT
    from daegam.api.telemetry import INGEST_BODY_LIMIT, OTLP_BODY_LIMIT
    from daegam.api.turn_grants import TURN_GRANT_BODY_LIMIT

    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=settings.max_upload_bytes + MULTIPART_SLACK,
        tighter=[
            UPLOAD_BODY_LIMIT,
            IMPORT_BODY_LIMIT,
            INGEST_BODY_LIMIT,
            OTLP_BODY_LIMIT,
            TURN_GRANT_BODY_LIMIT,
            SANDBOX_BODY_LIMIT,
            AGENT_CALL_BODY_LIMIT,
        ],
    )
    # 감사 미들웨어. 저장소는 lifespan 뒤에 생기므로 호출 시점에 app.state 에서 읽는다
    app.add_middleware(AuditMiddleware, store_getter=lambda: getattr(app.state, "audit", None))
    # 브라우저 보호 헤더. IdP origin 은 기동 설정에서 정해지므로 여기서 한 번 계산한다. 화면(HTML)의
    # CSP 는 UI 컨테이너가 같은 판단으로 붙인다(D52)
    app.add_middleware(SecurityHeadersMiddleware, idp_origin=browser_idp_origin(settings))


def create_app(
    settings: Settings | None = None,
    *,
    client_factory: ClientFactory | None = None,
    secret_store: SecretStore | None = None,
    oidc_http_client: httpx.AsyncClient | None = None,
    spec_runner: SpecJobRunner | None = None,
) -> FastAPI:
    """client_factory / secret_store / spec_runner 는 테스트가 fake 를 넣는 자리다. 운영에서는 비워
    둔다. spec_runner 가 없으면 실제 LLM 루프(SpecGeneratorRunner)를 쓰고,
    settings.spec_worker_enabled 가 거짓이면 Spec Generator 워커를 띄우지 않는다. UI 는 이 앱이 내지
    않는다. daegam-ui 컨테이너가 내고 Ingress 가 /api/·/.well-known/·/healthz 만 이리로 보낸다(D52).
    """
    _configure_logging()
    settings = settings or settings_from_env()
    api_build = _api_build()
    release = _release()

    # 플랫폼 web_search 의 MCP 앱 (P39b 12절). 그 lifespan 을 플랫폼 lifespan 안에서 연다
    from daegam.api.web_search_mcp import WebSearchDispatch
    from daegam.api.web_search_mcp import build_app as build_web_search_app

    web_search_app = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 조립은 daegam/wiring.py 에 있다. 여기서는 순서만 정한다
        stores = build_stores(settings)
        await stores.init_schemas()
        # 데이터 플레인 발급자의 전환을 막을 테넌트를 서비스를 짓기 전에 본다 (P53b 6.10절)
        running = await held_settings(settings, stores.tenant_settings)
        services = build_services(
            stores,
            running,
            client_factory=client_factory,
            secret_store=secret_store,
            oidc_http_client=oidc_http_client,
            spec_runner=spec_runner,
            # 비서(D31)는 앱을 ASGI 로, API 표면을 OpenAPI dict 로만 받는다
            asgi_app=app,
            openapi=app.openapi(),
        )
        attach(app, stores, services)
        _warn_about_open_settings(running)
        await services.start()
        try:
            async with web_search_app.router.lifespan_context(web_search_app):
                yield
        finally:
            await services.stop()
            await stores.close()

    # 대화형 문서와 OpenAPI 문서는 FastAPI 의 자동 라우트를 끄고 api_docs 가 /api 아래에 단다. 자동
    # 문서 화면은 CDN 과 인라인 스크립트를 써서 CSP 에 막힌다. 자동 라우트를 끄므로 FastAPI 가
    # 문서와 함께 루트에 다는 /docs/oauth2-redirect 도 없다(루트는 Ingress 가 UI 로 보낸다, D52)
    app = FastAPI(
        title="daegam",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    _add_middleware(app, settings)
    web_search_app = build_web_search_app(app.state)
    # 가장 바깥이다. 감사 미들웨어는 이 경로를 보지 않고 엔드포인트가 tools/call 마다 남긴다
    app.add_middleware(WebSearchDispatch, search_app=web_search_app, state=app.state)

    # 토큰 모드에서는 문서 화면과 OpenAPI 문서를 인증 뒤에 둔다. API 표면 전체를 익명에게 보이지
    # 않게 하려는 것이다. header 모드는 어차피 전부 익명 관리자라 가릴 것이 없다
    from daegam.api.deps import require_user

    install_api_docs(app, dependencies=[Depends(require_user)] if settings.token_auth else ())

    from daegam.services.oauth.errors import OAuthError

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        """서비스 계층 오류(`ServiceError` 자손)를 플랫폼 형식으로 낸다. 라우터는 이 오류를
        잡지 않는다. 잡아서 다른 일을 더 해야 하는 곳만 각자 좁게 잡고 다시 올린다. OAuth 오류는
        RFC 6749 꼴이라 오류 코드표 밖이다."""
        if not isinstance(exc, OAuthError):
            check_registered(exc.status, exc.to_dict())
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.to_dict()}, headers=exc.headers
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """라우터가 올린 HTTPException 은 FastAPI 의 기본 처리 그대로다. 테스트에서는 그 전에 오류
        코드표와 맞춰 본다(P74 2.2절). 런타임의 답을 넘기는 `ForwardedError` 는 런타임 표가
        집이다."""
        if not isinstance(exc, ForwardedError):
            check_registered(exc.status_code, exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        """요청 검증의 422 는 FastAPI 의 기본 처리에서 오류마다 `input` 만 뺀다. 그 값은 틀린 필드의
        값이거나 본문 전체라서, 비밀번호·키·토큰을 받는 라우트가 그 값을 되돌려 주기 때문이다.
        라우트마다 등록하면 새 라우트가 빠뜨릴 수 있어 모든 라우트에 건다."""
        safe = RequestValidationError(
            [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
        )
        return await request_validation_exception_handler(request, safe)

    @app.exception_handler(ClusterUnreachable)
    async def _cluster_unreachable(_: Request, exc: ClusterUnreachable) -> JSONResponse:
        """대상 클러스터에 닿지 못했다. 서비스가 자기 오류로 옮기지 않고 올린 것도 500 이 아니라
        502 `cluster_unreachable` 로 낸다. 라우터마다 잡아 옮기던 것을 여기 한 곳에 둔다."""
        return JSONResponse(
            status_code=502, content={"detail": {"code": "cluster_unreachable", "detail": str(exc)}}
        )

    # 플랫폼 Deployment(charts/daegam/templates/platform.yaml)의 readiness/liveness probe 가 부르는
    # 경로다. 루트에 남긴다.
    # Ingress 도 호스트의 /healthz 를 이리로 보낸다. 이 주소는 플랫폼만 말한다. UI 는 daegam-ui 의
    # /build.json 으로 본다(D52)
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        body: dict[str, Any] = {"status": "ok", "version": __version__}
        if settings.sandbox_allow_unchecked:
            # 개발용 우회가 켜졌다 (P40 8절). 점검 없이 home 에서 코드를 실행한다
            body["sandbox_unchecked"] = True
        return body

    @app.get(f"{API_PREFIX}/version", response_model=VersionInfo)
    def version() -> dict[str, Any]:
        """API 버전과 이 플랫폼 이미지의 빌드(`<version>+<도장>`, 로컬 프로세스면 null). UI 빌드는
        UI 컨테이너의 /build.json 이 따로 낸다(D52)."""
        return {"api_version": __version__, "api_build": api_build, "release": release}

    from daegam.api.agent_calls import router as agent_calls_router
    from daegam.api.agent_chat import router as agent_chat_router
    from daegam.api.agent_evaluation import router as agent_evaluation_router
    from daegam.api.agent_feedback import router as agent_feedback_router
    from daegam.api.agent_preview import router as agent_preview_router
    from daegam.api.agent_runs import me_router as runs_as_me_router
    from daegam.api.agent_runs import router as agent_runs_router
    from daegam.api.agent_telemetry import router as agent_telemetry_router
    from daegam.api.agents import router as agents_router
    from daegam.api.approval_choices import router as approval_choices_router
    from daegam.api.assistant import router as assistant_router
    from daegam.api.audit import router as audit_router
    from daegam.api.auth import router as auth_router
    from daegam.api.backend_apis import router as backend_apis_router
    from daegam.api.channels import inbound_router as teams_inbound_router
    from daegam.api.channels import me_router as channels_me_router
    from daegam.api.channels import settings_router as channels_settings_router
    from daegam.api.clusters import router as clusters_router
    from daegam.api.data_services import router as data_services_router
    from daegam.api.gateways import router as gateways_router
    from daegam.api.hooks import router as hooks_router
    from daegam.api.hub import router as hub_router
    from daegam.api.knowledge import router as knowledge_router
    from daegam.api.llm_gateway import agents_router as llm_token_router
    from daegam.api.llm_gateway import router as llm_gateway_router
    from daegam.api.mcp_servers import router as mcp_servers_router
    from daegam.api.members import router as members_router
    from daegam.api.sandboxes import router as sandboxes_router
    from daegam.api.settings import router as settings_router
    from daegam.api.skills import router as skills_router
    from daegam.api.spec_generator import router as spec_generator_router
    from daegam.api.telemetry import router as telemetry_router
    from daegam.api.tenants import router as tenants_router
    from daegam.api.turn_grants import router as turn_grants_router
    from daegam.api.usage import router as usage_router

    routers = [
        auth_router,
        members_router,
        audit_router,
        backend_apis_router,
        clusters_router,
        data_services_router,
        mcp_servers_router,
        hub_router,
        gateways_router,
        agents_router,
        agent_chat_router,
        agent_feedback_router,
        agent_evaluation_router,
        # 저장한 최신 리비전의 미리 보기 (P44b). 원본 Agent 아래의 경로다
        agent_preview_router,
        # 스케줄과 지금 실행, 나를 대신하는 소스 (P41). 켜기 전에는 404 다
        agent_runs_router,
        runs_as_me_router,
        # 사람이 기억한 승인 선택 (P43 5절). 계정 화면이 보고 지운다
        approval_choices_router,
        # 웹훅 트리거의 공개 경로 (P41 4절). Bearer 없이 서명으로 부른다
        hooks_router,
        agent_telemetry_router,
        knowledge_router,
        skills_router,
        usage_router,
        telemetry_router,
        # 런타임이 턴 위임을 갱신하고 끝내는 기계 경로 (P53 4절)
        turn_grants_router,
        # 부르는 Agent 의 런타임이 다른 Agent 의 실행을 시작하고 묻고 취소하는 기계 경로 (P46 6.3절)
        agent_calls_router,
        # 런타임이 세션의 코드 실행 샌드박스를 열고 찾고 닫는 기계 경로 (P40 5절)
        sandboxes_router,
        settings_router,
        # Teams 채널 (P47). 메시지 엔드포인트는 Bot Connector 의 토큰으로 부르고 OpenAPI 밖이다.
        # 채널을 껐거나 header 모드면 셋 다 404 다
        teams_inbound_router,
        channels_settings_router,
        channels_me_router,
        # LLM 게이트웨이의 상태와 Agent 의 LLM 토큰 교체 (P38 5절·11절)
        llm_gateway_router,
        llm_token_router,
        spec_generator_router,
        tenants_router,
        assistant_router,
    ]
    for router in routers:
        app.include_router(router, prefix=API_PREFIX)

    # local 모드의 로그인·사용자 관리 라우터와, 플랫폼이 사람 토큰을 내는 모드(local, oidc)의 개인
    # MCP 토큰·서비스 어카운트 라우터 (D40, P32, P53b 6.5·6.7절). 언제나 등록해 OpenAPI 와 UI 타입에
    # 보이게 하고, 그 모드가 아니면 require_local_mode 나 require_minting 이 404 를 낸다
    from daegam.api.local_auth import router as local_auth_router
    from daegam.api.personal_tokens import router as personal_tokens_router
    from daegam.api.service_accounts import router as service_accounts_router
    from daegam.api.users import router as users_router

    app.include_router(local_auth_router, prefix=API_PREFIX)
    app.include_router(users_router, prefix=API_PREFIX)
    app.include_router(personal_tokens_router, prefix=API_PREFIX)
    app.include_router(service_accounts_router, prefix=API_PREFIX)

    # MCP 클라이언트의 OAuth 인가 서버 (P33). 클라이언트가 부르는 라우트는 OpenAPI 에 싣지 않고 동의
    # 화면과 연결 목록이 부르는 라우트만 싣는다. 꺼져 있으면 require_oauth 가 404 를 낸다
    from daegam.api.oauth import consent_router as oauth_consent_router
    from daegam.api.oauth import router as oauth_router
    from daegam.api.oauth_grants import router as oauth_grants_router

    app.include_router(oauth_router, prefix=API_PREFIX)
    app.include_router(oauth_consent_router, prefix=API_PREFIX)
    app.include_router(oauth_grants_router, prefix=API_PREFIX)

    # 플랫폼이 발급자다 (D40, D41). 모든 모드에서 내부 홉의 서비스 토큰을, local 에서는 사람 토큰도
    # 낸다. 데이터 플레인 검증기들은 설정으로 받은 주소의 JWKS 만 읽고(P32 1절) 디스커버리는 밖의
    # 클라이언트가 읽는다. 문서의 issuer 는 local 에서 사람 발급자(공개 주소), 그 밖에서 서비스
    # 발급자다. OAuth 가 켜졌으면 인가 서버 메타데이터(RFC 8414)도 내고 디스커버리가 그 필드를
    # 싣는다(P33 2절). 모두 발급자 주소의 루트에 있어야 하므로 /api 밖이다. Ingress 가 /.well-known/
    # 을 UI 가 아니라 이리로 보낸다(D52)
    from daegam.api.deps import require_oauth_awake
    from daegam.api.oauth import authorization_server_metadata
    from daegam.auth.signer import (
        JWKS_CACHE_CONTROL,
        JWKS_PATH,
        SERVICE_JWKS_USE,
        discovery_document,
    )

    @app.get("/.well-known/openid-configuration", include_in_schema=False)
    def openid_configuration() -> JSONResponse:
        oauth = authorization_server_metadata(settings)
        return JSONResponse(discovery_document(settings.platform_human_issuer, oauth))

    @app.get(
        "/.well-known/oauth-authorization-server",
        include_in_schema=False,
        dependencies=[Depends(require_oauth_awake)],
    )
    def oauth_authorization_server() -> JSONResponse:
        return JSONResponse(authorization_server_metadata(settings))

    @app.get(JWKS_PATH, include_in_schema=False)
    async def jwks(request: Request, use: str | None = None) -> JSONResponse:
        """플랫폼 서명 키와 local 모드의 개인 토큰 슬롯 키. 서비스 토큰 검증기
        (daegam-internal-oidc)는 `?use=service` 로 RSA 키 하나를 받는다 (P32 1절·4절). 조립과 503 은
        PatKeyRing 이 한다."""
        body = await request.app.state.pat_keys.jwks(service=use == SERVICE_JWKS_USE)
        return JSONResponse(body, headers={"Cache-Control": JWKS_CACHE_CONTROL})

    # --- 미매칭 /api 경로 ---------------------------------------------------------------
    # FastAPI 기본 404({"detail": "Not Found"}) 대신 플랫폼 오류 꼴({"detail": {code, detail}})로
    # 답한다. GET 뿐이다. 있는 경로에 메서드만 틀린 요청(POST /api/settings 등)은 여기 걸리지 않아야
    # 405 로 남는다.
    @app.get(f"{API_PREFIX}/{{path:path}}", include_in_schema=False)
    def api_not_found(path: str) -> None:
        raise error(404, "not_found", f"no API route for {API_PREFIX}/{path}")

    return app


app = create_app()
