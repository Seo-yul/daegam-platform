"""MCP 클라이언트와 동의 화면이 부르는 OAuth 인가 서버 라우트 (P33 1절·2절·4절·6절·7절).

라우터는 HTTP 모양만 맡고 규칙은 services/oauth/ 다(`api/local_auth.py` 와 같은 나눔). OAuth 가
켜졌거나 잠들었을 때 있고(`require_oauth`, P53b 6.5절) 꺼지면 404 다. 클라이언트가 부르는 라우트는
RFC 6749·7591·7009 의 오류 모양 `{error, error_description}` 으로 답하고 OpenAPI 에 싣지 않는다.
명세가 그 모양을 정하고 클라이언트는 메타데이터로 엔드포인트를 찾기 때문이다. 그 모양으로 바꾸는
것은 `oauth_error_response` 하나다.

POST /oauth/register   DCR (RFC 7591). JSON 본문 16KB 상한. 201 등록 값. 400 invalid_redirect_uri·
                       invalid_client_metadata, 413 invalid_client_metadata(본문 상한). 아무것도
                       저장하지 않고 감사는 미들웨어가 인증 없는 쓰기로 남긴다. 행의 client_id 는
                       발급한 client_id 의 client_ref 다
GET  /oauth/authorize  인가 요청. 언제나 302 다. 통과하면 서명한 요청 핸들을 실어 동의 화면으로,
                       확인하지 않은 client_id·redirect_uri 의 오류는 동의 화면의 `?error=` 로, 그
                       뒤의 오류는 redirect_uri 로 보낸다. 저장하지 않고 감사하지 않는다
POST /oauth/token      코드 교환과 갱신 (9절). 400 invalid_request·invalid_grant·invalid_scope·
                       unsupported_grant_type, 401 invalid_client
POST /oauth/revoke     RFC 7009. 200(모르는 토큰도), 401 invalid_client(다른 클라이언트의 토큰)

클라이언트는 이 넷을 인가 서버 메타데이터(`authorization_server_metadata`)로 찾는다. 그 문서는
발급자 루트의 `/.well-known/oauth-authorization-server` 에 있어 main.py 가 낸다.

token·revoke 는 폼을 직접 파싱한다. 틀린 폼에 플랫폼의 422 목록이 아니라 RFC 6749 의 400
`invalid_request` 를 `{error, error_description}` 꼴로 답하기 위해서다. 서비스는 확인한
주체·클라이언트·테넌트와 행동 이름을 `OAuthAudit` 에 채우고, 핸들러가 응답이 오류여도 그것을
`request.state` 로 옮겨 감사 미들웨어가 읽는다 (18절). 둘은 인증 전 경로라 주체도 거기서 온다.

동의 화면이 부르는 셋은 `consent_router` 이고 플랫폼의 오류 모양으로 OpenAPI 에 실린다. 핸들이
수 KB 까지 커지므로 경로가 아니라 본문 `{request}` 로 받는다. 비밀번호를 바꿔야 하는 사람은
`require_user` 가 403 으로 막는다.

POST /oauth/consent/inspect  화면의 내용. 404(서명)·410 request_expired·409 request_used,
                             400 invalid_target(노출이 꺼짐), 503 client_metadata_unavailable
POST /oauth/consent/approve  {redirect_to}. 403 reauth_required·not_a_member·forbidden, 429
                             consent_limit, 503 temporarily_unavailable(잠든 서버). 최근 인증은
                             local 이면 제어면 토큰의 iat, oidc 면 `X-Daegam-Id-Token` 이다. 감사
                             행의 테넌트는 리소스를 푼 뒤부터 리소스의 테넌트다
POST /oauth/consent/deny     {redirect_to}. error=access_denied 가 실린다

셋의 감사 행은 서명이 맞는 핸들이면 그 클라이언트의 client_ref 를 client_id 칸에 싣는다.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from daegam.api.deps import User, require_oauth, require_recent_sign_in
from daegam.api.local_auth import local_user
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.oauth import ConsentDecisionOut, ConsentIn, ConsentOut
from daegam.auth.principal import Principal
from daegam.auth.signer import SCOPE_MCP, jwks_url
from daegam.config import Settings
from daegam.request_body import body_upto
from daegam.services.oauth.audit import OAuthAudit
from daegam.services.oauth.authorize import S256, SCOPE_OFFLINE, OAuthAuthorizer, OAuthResource
from daegam.services.oauth.clients import (
    PUBLIC_CLIENT_AUTH,
    RESPONSE_TYPE_CODE,
    SUPPORTED_GRANTS,
    OAuthClients,
    client_ref,
)
from daegam.services.oauth.consent import Approver, ConsentView, OAuthConsent, token_iat_check
from daegam.services.oauth.errors import INVALID_CLIENT_METADATA, INVALID_REQUEST, OAuthError
from daegam.services.oauth.tokens import ACCESS_TOKEN_SECONDS, OAuthTokens
from daegam.store.base import from_epoch
from daegam.urls import API_PREFIX

router = APIRouter(tags=["oauth"], include_in_schema=False, dependencies=[Depends(require_oauth)])
consent_router = APIRouter(
    responses=ERROR_RESPONSES, tags=["oauth"], dependencies=[Depends(require_oauth)]
)

# 클라이언트가 부르는 네 라우트. 인가 서버 메타데이터가 이 경로를 광고한다
REGISTER_PATH = "/oauth/register"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REVOKE_PATH = "/oauth/revoke"
# 인가 응답을 돌려주는 모양. 코드·state·iss 가 redirect_uri 의 쿼리로 간다
RESPONSE_MODE_QUERY = "query"
# 등록 본문의 상한. 앱 전체의 상한(`body_limit.py`)은 업로드 크기라 이 라우트들에는 너무 커서
# `body_upto` 로 따로 센다
REGISTER_MAX_BYTES = 16 * 1024
FORM_TYPE = "application/x-www-form-urlencoded"
# 토큰·회수 폼의 상한. 인자 몇 개라 등록 본문보다 작다
FORM_MAX_BYTES = 8 * 1024
# 명세가 등록·토큰 응답에 요구하는 헤더. 오류 응답에도 붙인다
NO_STORE = {"Cache-Control": "no-store"}


def _clients(request: Request) -> OAuthClients:
    return request.app.state.oauth_clients


def _authorizer(request: Request) -> OAuthAuthorizer:
    return request.app.state.oauth_authorizer


def _consent(request: Request) -> OAuthConsent:
    return request.app.state.oauth_consent


def _tokens(request: Request) -> OAuthTokens:
    return request.app.state.oauth_tokens


def authorization_server_metadata(settings: Settings) -> dict[str, Any] | None:
    """RFC 8414 인가 서버 메타데이터 (2절). OAuth 가 꺼졌으면 None 이다.

    `/.well-known/oauth-authorization-server` 가 이것을 내고 `openid-configuration` 이 이 필드를
    얹는다(`discovery_document`). `issuer` 는 PRM 의 `authorization_servers`(MCPOIDCConfig 의
    issuer)와 글자가 같아야 한다. OAuth 가 켜지면 둘 다 사람 발급자, 곧 공개 주소다. 잠든 서버는
    메타데이터를 내지 않는다 (P53b 6.5절).
    CIMD 허용 목록이 비면 CIMD 를 광고하지 않는다. 클라이언트는 이 광고와 `none` 을 보고 CIMD 로
    올지 DCR 로 등록할지 정한다."""
    if not settings.oauth_enabled:
        return None
    issuer = settings.platform_human_issuer
    base = f"{issuer}{API_PREFIX}"
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{base}{AUTHORIZE_PATH}",
        "token_endpoint": f"{base}{TOKEN_PATH}",
        "registration_endpoint": f"{base}{REGISTER_PATH}",
        "revocation_endpoint": f"{base}{REVOKE_PATH}",
        "jwks_uri": jwks_url(issuer),
        "response_types_supported": [RESPONSE_TYPE_CODE],
        "response_modes_supported": [RESPONSE_MODE_QUERY],
        "grant_types_supported": list(SUPPORTED_GRANTS),
        "code_challenge_methods_supported": [S256],
        "token_endpoint_auth_methods_supported": [PUBLIC_CLIENT_AUTH],
        "revocation_endpoint_auth_methods_supported": [PUBLIC_CLIENT_AUTH],
        "scopes_supported": [SCOPE_MCP, SCOPE_OFFLINE],
        "client_id_metadata_document_supported": bool(settings.oauth_cimd_clients),
        # RFC 9207. 인가 응답과 오류 응답에 iss 를 싣는다
        "authorization_response_iss_parameter_supported": True,
    }


def oauth_error_response(e: OAuthError) -> JSONResponse:
    return JSONResponse(e.body(), status_code=e.status, headers={**NO_STORE, **(e.headers or {})})


async def _registration_body(request: Request) -> dict[str, Any]:
    raw = await body_upto(request, REGISTER_MAX_BYTES)
    if raw is None:
        raise OAuthError(
            INVALID_CLIENT_METADATA,
            f"the registration body exceeds {REGISTER_MAX_BYTES} bytes",
            status=413,
        )
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise OAuthError(INVALID_CLIENT_METADATA, "the registration body must be a JSON object")
    return body


@router.post(REGISTER_PATH, status_code=201)
async def register(request: Request) -> JSONResponse:
    try:
        registered = _clients(request).register(await _registration_body(request))
    except OAuthError as e:
        return oauth_error_response(e)
    request.state.client_id = client_ref(registered["client_id"])
    return JSONResponse(registered, status_code=201, headers=NO_STORE)


async def _form(request: Request) -> dict[str, str]:
    """토큰·회수의 폼. 같은 이름의 인자가 둘이면 invalid_request 다. 오류 설명에 받은 값을 넣지
    않는다."""
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != FORM_TYPE:
        raise OAuthError(INVALID_REQUEST, f"the body must be {FORM_TYPE}")
    raw = await body_upto(request, FORM_MAX_BYTES)
    if raw is None:
        raise OAuthError(INVALID_REQUEST, f"the body exceeds {FORM_MAX_BYTES} bytes", status=413)
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except ValueError as e:  # UnicodeDecodeError 도 여기다
        raise OAuthError(INVALID_REQUEST, "the body is not a valid form") from e
    form: dict[str, str] = {}
    for name, value in pairs:
        if name in form:
            raise OAuthError(INVALID_REQUEST, "a parameter appears more than once")
        form[name] = value
    return form


def _note_audit(request: Request, audit: OAuthAudit, audit_code: str | None = None) -> None:
    """서비스가 확인한 주체·클라이언트·테넌트와 행동 이름을 감사 미들웨어가 읽는 자리에 둔다.
    로그인처럼 확인한 sub 만 주체로 둔다 (18절)."""
    state = request.state
    if audit.sub is not None:
        state.principal = Principal(sub=audit.sub)
    state.client_id = audit.client_ref
    state.tenant = audit.tenant
    state.audit_action = audit.action
    state.audit_error_code = audit_code


async def _form_route[T](
    request: Request,
    call: Callable[[dict[str, str], OAuthAudit], Awaitable[T]],
    answer: Callable[[T], Response],
) -> Response:
    """폼을 읽어 서비스에 넘기고 결과나 OAuth 오류를 응답으로 바꾼다. 어느 쪽이든 감사 재료를
    남긴다."""
    audit, audit_code = OAuthAudit(), None
    try:
        return answer(await call(await _form(request), audit))
    except OAuthError as e:
        audit_code = e.audit_code
        return oauth_error_response(e)
    finally:
        _note_audit(request, audit, audit_code)


@router.post(TOKEN_PATH)
async def oauth_token(request: Request) -> Response:
    return await _form_route(
        request, _tokens(request).token, lambda body: JSONResponse(body, headers=NO_STORE)
    )


@router.post(REVOKE_PATH)
async def oauth_revoke(request: Request) -> Response:
    """RFC 7009 2.2. 회수했든 모르는 토큰이든 200 이고 본문이 없다."""
    return await _form_route(request, _tokens(request).revoke, lambda _: Response(headers=NO_STORE))


@router.get(AUTHORIZE_PATH)
async def authorize(request: Request) -> RedirectResponse:
    params: dict[str, list[str]] = {}
    for name, value in request.query_params.multi_items():
        params.setdefault(name, []).append(value)
    location = await _authorizer(request).authorize(params)
    return RedirectResponse(location, status_code=302, headers=NO_STORE)


# inspect 가 소속에게만 싣는 대상의 필드
_MEMBER_ONLY_FIELDS = ("kind", "name", "display_name", "tenant")


def _resource_out(resource: OAuthResource, member: bool) -> dict[str, Any]:
    """소속이 아니면 공개 주소만 싣는다. 공개 주소만 있으면 누구나 핸들을 만들 수 있으므로 테넌트와
    표시 이름을 알려 주지 않는다 (7절)."""
    detail = {f: getattr(resource, f) if member else None for f in _MEMBER_ONLY_FIELDS}
    return {"url": resource.url, **detail}


def _consent_out(view: ConsentView, grant_days: int) -> dict[str, Any]:
    request, client = view.request, view.client
    target = urlsplit(request.redirect_uri)
    return {
        "client": {
            "name": client.name,
            "ref": client.ref,
            "kind": client.kind,
            "host": client.host,
            "name_collides": client.collides,
        },
        "redirect": {"host": target.hostname, "port": target.port},
        "member": view.member,
        "resource": _resource_out(view.resource, view.member),
        "scope": request.scope.split(),
        "expires_at": from_epoch(request.exp),
        "access_token_seconds": ACCESS_TOKEN_SECONDS,
        "grant_days": grant_days,
    }


async def _consent_route[T](request: Request, call: Callable[[OAuthAudit], Awaitable[T]]) -> T:
    """동의 서비스를 부르고, 오류로 끝나도 핸들에서 확인한 클라이언트와 테넌트를 감사 자리에
    둔다. 주체는 인증이 이미 두었다."""
    audit = OAuthAudit()
    try:
        return await call(audit)
    finally:
        _note_audit(request, audit)


@consent_router.post("/oauth/consent/inspect", response_model=ConsentOut)
async def inspect_consent(request: Request, body: ConsentIn, principal: User) -> dict[str, Any]:
    view = await _consent_route(
        request, lambda audit: _consent(request).inspect(body.request, principal.sub, audit)
    )
    return _consent_out(view, request.app.state.settings.oauth_refresh_days)


async def _approver(request: Request, principal: Principal) -> Approver:
    """허용하는 사람 (P53b 6.5절). local 은 사용자 문서와 제어면 토큰의 iat 다. 로그인과 비밀번호
    변경이 매번 새 토큰을 낸다. oidc 는 IdP 의 sub 과, UI 가 함께 보낸 ID 토큰의 최근 로그인이다."""
    settings: Settings = request.app.state.settings
    if settings.auth_mode == "local":
        user = await local_user(request.app.state.local_auth, principal)
        return Approver(user.id, user.token_version, token_iat_check(principal.raw.get("iat")))

    async def confirm(now: float) -> None:
        await require_recent_sign_in(request, settings, principal, now=now)

    return Approver(principal.sub, 0, confirm)


@consent_router.post("/oauth/consent/approve", response_model=ConsentDecisionOut)
async def approve_consent(request: Request, body: ConsentIn, principal: User) -> dict[str, Any]:
    approver = await _approver(request, principal)
    redirect_to = await _consent_route(
        request, lambda audit: _consent(request).approve(body.request, approver, audit)
    )
    return {"redirect_to": redirect_to}


@consent_router.post("/oauth/consent/deny", response_model=ConsentDecisionOut)
async def deny_consent(request: Request, body: ConsentIn, principal: User) -> dict[str, Any]:
    redirect_to = await _consent_route(
        request, lambda audit: _consent(request).deny(body.request, principal.sub, audit)
    )
    return {"redirect_to": redirect_to}
