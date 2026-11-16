"""API 문서 화면 (api_docs/).

플랫폼 CSP(`script-src 'self'`, D39)는 인라인 스크립트와 다른 origin 의 스크립트를 막는다. 문서
화면은 CSP 를 넓히지 않고 같은 origin 의 자산만 부른다. HTML 을 읽어 그것을 보고, 그 자산이 제
형식으로 나가는지 본다. 토큰 모드에서 문서 화면이 로그인 뒤에 있고 자산은 인증 없이 나가는 것은
test_security_headers.py 가 본다.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from urllib.parse import urlsplit

from daegam.security_headers import content_security_policy

JS = "text/javascript"
CSS = "text/css"


class _Refs(HTMLParser):
    """페이지가 부르는 스크립트·스타일시트와, 파일이 아닌 곳에 적힌 스크립트·스타일을 모은다."""

    def __init__(self) -> None:
        super().__init__()
        self.assets: list[tuple[str, str]] = []  # (주소, 기대하는 content-type)
        self.inline: list[str] = []
        self.attrs: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.attrs.setdefault(tag, a)
        if tag == "script":
            if a.get("src"):
                self.assets.append((a["src"] or "", JS))
            else:
                self.inline.append("<script> 본문")
        elif tag == "link" and "stylesheet" in (a.get("rel") or "").split():
            self.assets.append((a.get("href") or "", CSS))
        elif tag == "style":
            self.inline.append("<style> 본문")
        self.inline.extend(f"<{tag} {k}>" for k in a if k == "style" or k.startswith("on"))


def _same_origin_path(url: str) -> bool:
    parts = urlsplit(url)
    return not parts.scheme and not parts.netloc and url.startswith("/")


# fastapi-offline 이 싣고 오는 번들과 그 상류 npm 판의 sha256. 브라우저에서 도는 코드를 제3자가 다시
# 포장한 것이라, 판을 올리면 이 값이 깨져 상류 tarball 과 대조하게 한다
UPSTREAM_BUNDLES = {
    # swagger-ui-dist@5.32.6
    "swagger-ui-bundle.js": "4b5a9b35a1adf37f00d39c0bae23cdb37007e2946240ef035137cc85b4d55349",
    "swagger-ui.css": "ca238f7d7c2cf4480c1e77a9c3b9da915ab216e96ffd354e69076560c650c6de",
    # redoc@2.5.3 의 bundles/redoc.standalone.js
    "redoc.standalone.js": "1320f442151c57c447d3b70c7ffc6c4f86d08464020fe34c8cc5d3164e9944f0",
}


def test_the_pages_point_at_the_openapi_document(root_client):
    """초기화 스크립트는 문서 주소를 HTML 의 data 속성에서, ReDoc 은 spec-url 에서 읽는다. 두
    페이지는 같은 origin 의 자산만 부르고 CSP 를 넓히지 않는다. 자산 경로는 번들과 우리 정적 파일만
    내고(패키지의 파이썬 코드나 디렉터리 밖은 없다), 번들은 상류 npm 판과 바이트까지 같다."""
    pages = {
        "/api/docs": ["swagger-ui-bundle.js", "swagger-ui-init.js"],
        "/api/redoc": ["redoc.standalone.js"],
    }
    for page, scripts in pages.items():
        r = root_client.get(page)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        # CSP 는 API 응답과 같은 것 그대로다. 문서 화면을 위해 넓히지 않는다
        assert r.headers["content-security-policy"] == content_security_policy(idp_origin=None)
        refs = _Refs()
        refs.feed(r.text)
        assert refs.inline == []
        assert [url.rsplit("/", 1)[-1] for url, kind in refs.assets if kind == JS] == scripts
        assert any(kind == CSS for _, kind in refs.assets)
        for url, kind in refs.assets:
            assert _same_origin_path(url), url
            asset = root_client.get(url)
            assert asset.status_code == 200, url
            assert asset.headers["content-type"].split(";")[0] == kind, url
            assert asset.content, url
            # 이름에 판이 없으므로 매번 ETag 로 다시 확인한다. 그대로면 304 다
            assert asset.headers["cache-control"] == "no-cache", url
            again = root_client.get(url, headers={"If-None-Match": asset.headers["etag"]})
            assert again.status_code == 304, url
    for path in (
        "/api/docs/assets/nope.js",
        "/api/docs/assets/__init__.py",
        "/api/docs/assets/%2e%2e/__init__.py",
        "/api/docs/assets/%2e%2e/%2e%2e/main.py",
    ):
        assert root_client.get(path).status_code == 404, path
    for name, digest in UPSTREAM_BUNDLES.items():
        body = root_client.get(f"/api/docs/assets/{name}").content
        assert hashlib.sha256(body).hexdigest() == digest, name
    docs, redoc = _Refs(), _Refs()
    docs.feed(root_client.get("/api/docs").text)
    redoc.feed(root_client.get("/api/redoc").text)
    assert docs.attrs["div"]["data-openapi-url"] == "/api/openapi.json"
    assert redoc.attrs["redoc"]["spec-url"] == "/api/openapi.json"
    init = root_client.get("/api/docs/assets/swagger-ui-init.js").text
    assert "dataset.openapiUrl" in init and "SwaggerUIBundle(" in init
    assert root_client.get("/api/openapi.json").json()["info"]["title"] == "daegam"
