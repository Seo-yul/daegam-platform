"""브라우저 보호 응답 헤더와 문서 라우트 가림 (security_headers.py, main.py)."""

from __future__ import annotations

from daegam.security_headers import CALLBACK_FRAME_ANCESTORS, content_security_policy
from daegam.urls import origin_of
from tests.helpers.auth import bearer
from tests.helpers.http import api_client, make_app, startup_warnings

REQUIRED = {
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
}


def test_origin_of_is_a_csp_host_allowlist():
    """CSP host-part 문법만 받는다(D52). 받지 않으면 발급자 값이 CSP 지시어·정책을 새로 열거나(; ,)
    모든 호스트를 열거나(*) UI 컨테이너의 nginx 설정 문자열을 끊는다(", $). 줄바꿈은 urlsplit 이
    조용히 지우던 것이다."""
    assert origin_of("HTTPS://IdP.Example.com:8443/x?y#z") == "https://IdP.Example.com:8443"
    assert origin_of(" https://idp.example.com/ ") == "https://idp.example.com"
    assert origin_of("https://idp.example.com/\nanything") == "https://idp.example.com"
    for bad in (
        "https://idp;script-src *",
        "https://idp.example.com,https://evil.example.com",
        "https://*",
        'https://idp";x',
        "https://idp$host",
        "https://user@idp.example.com",
        "https://idp.example.com\n.evil",
        "https://[::1]:8443",
        "https://idp_x.example.com",
        "https://idp..example.com",
        "https://idp.example.com:",
        "https://idp.example.com:123456",
        "ftp://idp.example.com",
    ):
        assert origin_of(bad) is None, bad


def test_startup_warns_when_the_ui_would_refuse(offline_settings, caplog):
    """oidc 인데 발급자가 origin 이 아니면 UI 컨테이너만 기동을 거절한다. 그 까닭을 플랫폼
    로그에서도 찾게 한다."""
    bad = startup_warnings(
        offline_settings, caplog, auth_mode="oidc", oidc_issuer="https://user@idp.example.com"
    )
    assert any("daegam-ui container refuses to start" in m for m in bad)
    good = startup_warnings(
        offline_settings, caplog, auth_mode="oidc", oidc_issuer="https://idp.example.com"
    )
    assert not any("daegam-ui" in m for m in good)


def test_csp_opens_the_idp_only_when_there_is_one():
    plain = content_security_policy(idp_origin=None)
    directives = dict(d.split(" ", 1) for d in plain.split("; "))
    assert directives["connect-src"] == "'self'"
    assert directives["frame-src"] == "'none'"
    assert "script-src 'self'" in plain and "'unsafe-inline'" not in plain.split("style-src")[0]
    with_idp = content_security_policy(idp_origin="https://idp.example.com")
    assert "connect-src 'self' https://idp.example.com" in with_idp
    # silent renew iframe 은 IdP 를 거쳐 같은 origin 의 /callback 으로 돌아온다 (T13)
    assert "frame-src 'self' https://idp.example.com" in with_idp
    assert "frame-ancestors 'none'" in with_idp
    callback = content_security_policy(
        idp_origin="https://idp.example.com", frame_ancestors=CALLBACK_FRAME_ANCESTORS
    )
    assert "frame-ancestors 'self'" in callback
    assert "form-action 'self' https://idp.example.com" in with_idp
    # 워커는 blob 으로만, 스크립트는 여전히 self 만
    assert "worker-src 'self' blob:" in with_idp and "script-src 'self'" in with_idp


def test_hsts_only_over_https(settings, mongo, fake_clusters, fake_secrets):
    """로컬은 http 다. 보호 헤더는 모든 응답(404 포함)에 붙는다.

    여기서 HSTS 를 내면 브라우저가 그 호스트를 https 로 고정해 로컬 배포가 깨진다."""
    app = make_app(settings, fake_clusters, fake_secrets)
    with api_client(app) as c:
        for path in ("/version", "/backend-apis", "/nope"):
            r = c.get(path)
            missing = REQUIRED - {k.lower() for k in r.headers}
            assert missing == set(), (path, missing)
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["x-content-type-options"] == "nosniff"
        assert "strict-transport-security" not in c.get("/version").headers
        r = c.get("/version", headers={"X-Forwarded-Proto": "https"})
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert "includeSubDomains" in r.headers["strict-transport-security"]


def test_docs_need_a_user_in_oidc_mode(oidc_client, token):
    """oidc 모드에서는 API 표면 전체가 익명에게 보이지 않는다. 로그인한 사람은 그대로 본다. 문서
    화면의 자산은 누구나 받는 라이브러리와 문서 주소만 든 초기화라 인증 없이 나간다."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert oidc_client.get(path).status_code == 401, path
    for name in ("swagger-ui-bundle.js", "swagger-ui-init.js", "redoc.standalone.js"):
        assert oidc_client.get(f"/docs/assets/{name}").status_code == 200, name
    tok = token("viewer1", "daegam-a-view")
    assert oidc_client.get("/openapi.json", headers=bearer(tok)).status_code == 200
    assert oidc_client.get("/docs", headers=bearer(tok)).status_code == 200
    assert oidc_client.get("/redoc", headers=bearer(tok)).status_code == 200
    # CSP 가 IdP origin 을 연다
    csp = oidc_client.get("/version").headers["content-security-policy"]
    assert "connect-src 'self' http://mock-oidc.test" in csp
