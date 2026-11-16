"""플랫폼 루트에 남는 경로와 더 내지 않는 것 (D52).

UI 는 daegam-ui 컨테이너가 내고 Ingress 가 /api/·/.well-known/·/healthz 만 플랫폼으로 보낸다. 그래서
플랫폼은 UI 경로에 index.html 을 내지 않는다. UI 의 정적 규칙(딥링크 폴백, 캐시, 옛 /ui
리다이렉트)은 scripts/e2e_ui_serving.sh 가 nginx 에 대고 본다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from daegam import __version__
from daegam.urls import API_PREFIX
from tests.helpers.http import make_app


def _app(settings, fake_clusters, fake_secrets):
    return make_app(settings, fake_clusters, fake_secrets)


def test_platform_keeps_the_api_probe_and_issuer_routes(
    settings, mongo, fake_clusters, fake_secrets, monkeypatch
):
    # api_build 는 플랫폼 이미지의 도장이다. UI 빌드는 daegam-ui 의 /build.json 이 따로 낸다
    monkeypatch.setenv("DAEGAM_BUILD_COMMIT", "abc1234-dirty")
    monkeypatch.setenv("DAEGAM_RELEASE", "1.0.0-rc3")
    with TestClient(_app(settings, fake_clusters, fake_secrets)) as c:
        assert c.get("/api/version").json() == {
            "api_version": __version__,
            "api_build": f"{__version__}+abc1234-dirty",
            "release": "1.0.0-rc3",
        }
    # 로컬 프로세스(make run)에는 도장도 릴리스 태그도 없다
    monkeypatch.delenv("DAEGAM_BUILD_COMMIT")
    monkeypatch.delenv("DAEGAM_RELEASE")
    with TestClient(_app(settings, fake_clusters, fake_secrets)) as c:
        assert c.get("/api/version").json() == {
            "api_version": __version__,
            "api_build": None,
            "release": None,
        }
        assert c.get("/api/clusters").status_code == 200
        # 없는 API 경로는 플랫폼 오류 꼴의 JSON 404 다
        r = c.get("/api/nope")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "not_found"
        # 있는 경로에 메서드만 틀리면 그대로 405 다 (404 로 뭉개지 않는다)
        assert c.post("/api/settings", json={}).status_code == 405
        # probe 는 루트에 남아 있다 (차트의 플랫폼 Deployment 가 참조하고 Ingress 도 이리로
        # 보낸다)
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        # 발급자 디스커버리·JWKS 는 모든 모드에서 루트에 있다 (D41)
        assert c.get("/.well-known/openid-configuration").json()["issuer"]
        # 대화형 문서와 OpenAPI 문서는 /api 아래다
        assert c.get("/api/openapi.json").status_code == 200
        assert c.get("/api/docs").status_code == 200


def test_the_root_holds_only_the_probe_and_the_issuer(
    offline_settings, fake_clusters, fake_secrets
):
    """/api 밖의 경로는 probe 와 발급자의 /.well-known/ 셋뿐이다. Ingress 는 /api/·/.well-known/·
    /healthz 말고는 전부 daegam-ui 로 보내므로 여기에 경로가 늘면 그 경로는 조용히 index.html 이
    된다. FastAPI 는 문서를 켜면 /docs/oauth2-redirect 를 루트에 기본으로 단다. OAuth 인가 서버
    메타데이터(P33 2절)는 모든 모드에 등록되고 꺼져 있으면 404 다."""
    app = _app(offline_settings, fake_clusters, fake_secrets)
    root = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            # include_router 로 붙인 라우터는 경로 대신 접두사 하나를 든다(FastAPI 0.141)
            path = route.include_context.prefix + "/"
        if not path.startswith(f"{API_PREFIX}/"):
            root.add(path)
    assert root == {
        "/healthz",
        "/.well-known/openid-configuration",
        "/.well-known/jwks.json",
        "/.well-known/oauth-authorization-server",
    }
