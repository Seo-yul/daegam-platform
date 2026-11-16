"""브라우저 보호 응답 헤더. 순수 ASGI 미들웨어라 `audit/middleware.py` 와 같은 꼴이다.

화면은 LLM 이 만든 글, 외부 문서 요약, 스펙의 `summary`·`description`, 서버 오류 `detail` 을 그린다.
React 가 이스케이프하지만 그 한 겹에만 기대지 않는다. CSP 가 인라인 스크립트와 다른 origin 의
스크립트를 막으므로 이스케이프가 어디선가 빠져도 실행까지 가지 않는다.

CSP 는 화면이 실제로 쓰는 것만 연다.
- 스크립트는 같은 origin 뿐이다. 워커는 Vite 가 `blob:` 으로 열므로 `worker-src` 에만
  `blob:` 을 준다.
- 스타일은 `'unsafe-inline'` 이 필요하다. 캔버스·팔레트·OpenApiDocs 가 `style={{…}}` 을 쓴다.
  글꼴은 Google Fonts 다(`index.html`).
- OIDC 를 켜면 브라우저가 IdP 로 직접 붙는다(discovery·token·silent renew iframe). 그 origin 을
  `connect-src`·`frame-src`·`form-action` 에 연다. header 모드에는 IdP 가 없으니 열지 않는다.
- silent renew iframe 은 IdP 를 거쳐 같은 origin 의 `/callback` 으로 돌아온다. 그래서 oidc 면
  `frame-src` 에 `'self'` 도 열고, 화면 쪽(daegam-ui)은 `/callback` 응답에만 같은 origin 의
  프레임을 허락한다(`CALLBACK_FRAME_ANCESTORS`, `X-Frame-Options: SAMEORIGIN`). 다른 화면은 어디에도
  프레임으로 넣을 수 없다.

HSTS 는 https 로 들어온 응답에만 붙인다. 로컬은 http 라 붙이면 브라우저가 로컬 호스트를 https 로
고정해 로컬 배포가 깨진다. Ingress 뒤에서는 `X-Forwarded-Proto` 로 원래 scheme 을 본다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from daegam.urls import origin_of

if TYPE_CHECKING:
    from daegam.config import Settings

FONT_STYLE_ORIGIN = "https://fonts.googleapis.com"
FONT_FILE_ORIGIN = "https://fonts.gstatic.com"
HSTS_MAX_AGE = 60 * 60 * 24 * 180  # 반년. preload 는 도메인이 정해진 뒤 운영이 정한다
# 화면 응답의 frame-ancestors. `/callback` 만 같은 origin 의 silent renew iframe 에 열린다
FRAME_ANCESTORS = "'none'"
CALLBACK_FRAME_ANCESTORS = "'self'"


def browser_idp_origin(settings: Settings) -> str | None:
    """브라우저가 직접 말하는 IdP 의 origin. oidc 모드의 IdP 뿐이다. local 의 발급자는 플랫폼
    자신이고 브라우저는 같은 origin 의 /api/auth/login 을 쓰므로 CSP 를 넓히지 않는다(D40). UI
    컨테이너의 daegam-csp.sh 가 같은 판단을 한다(D52)."""
    return origin_of(settings.oidc_issuer) if settings.auth_mode == "oidc" else None


def content_security_policy(
    *, idp_origin: str | None, frame_ancestors: str = FRAME_ANCESTORS
) -> str:
    connect = ["'self'"]
    frame = ["'none'"]
    form = ["'self'"]
    if idp_origin:
        connect.append(idp_origin)
        # silent renew iframe. IdP 에서 시작해 같은 origin 의 /callback 으로 돌아온다
        frame = ["'self'", idp_origin]
        form.append(idp_origin)  # 인가 요청은 리다이렉트지만 IdP 가 form post 로 답할 수 있다
    directives = [
        "default-src 'self'",
        "script-src 'self'",
        f"style-src 'self' 'unsafe-inline' {FONT_STYLE_ORIGIN}",
        f"font-src 'self' {FONT_FILE_ORIGIN} data:",
        "img-src 'self' data:",
        f"connect-src {' '.join(connect)}",
        "worker-src 'self' blob:",
        f"frame-src {' '.join(frame)}",
        f"frame-ancestors {frame_ancestors}",
        "base-uri 'self'",
        f"form-action {' '.join(form)}",
        "object-src 'none'",
    ]
    return "; ".join(directives)


def security_headers(*, idp_origin: str | None, https: bool) -> list[tuple[bytes, bytes]]:
    out = [
        (b"content-security-policy", content_security_policy(idp_origin=idp_origin).encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    ]
    if https:
        out.append(
            (b"strict-transport-security", f"max-age={HSTS_MAX_AGE}; includeSubDomains".encode())
        )
    return out


def _is_https(scope: Scope) -> bool:
    for k, v in scope.get("headers") or []:
        if k == b"x-forwarded-proto":
            return v.split(b",")[0].strip().lower() == b"https"
    return scope.get("scheme") == "https"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, idp_origin: str | None):
        self.app = app
        self.idp_origin = idp_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers(idp_origin=self.idp_origin, https=_is_https(scope))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                # 라우터가 직접 정한 값이 있으면 그것을 존중한다
                headers.extend((k, v) for k, v in extra if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
