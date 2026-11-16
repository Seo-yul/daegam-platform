"""API 문서 화면. `/api/docs`(Swagger UI)·`/api/redoc`(ReDoc)과 둘이 읽는 `/api/openapi.json`.

FastAPI 기본 문서 화면은 swagger-ui·redoc 번들을 cdn.jsdelivr.net 에서 부르고 Swagger UI 를 인라인
스크립트로 띄운다. 플랫폼 CSP(`script-src 'self'`, D39)가 둘 다 막아 화면이 비므로 그 화면을 쓰지
않는다. CSP 는 넓히지 않고 화면이 CSP 안에 들어온다.

- 번들은 `fastapi-offline` 배포판이 패키지 데이터로 싣고 오는 파일이다. 판은 pyproject 의 == 핀이
  정하고, 파일은 이미지의 venv 에 함께 들어간다.
- Swagger UI 는 `static/swagger-ui-init.js` 가 띄우고 OpenAPI 문서 주소는 HTML 의 data 속성에서
  읽는다. ReDoc 은 `<redoc spec-url>` 요소를 보고 스스로 뜬다.
- 번들과 우리 파일 모두 `/api/docs/assets/` 에서 같은 origin 으로 나간다. HTML 에는 인라인
  스크립트도 `<style>` 도 없다. 파비콘은 적지 않는다. 브라우저가 루트의 `/favicon.ico` 를 부르면
  Ingress 가 UI 로 보낸다(D52).
- ReDoc 은 옆 메뉴 아래의 Redocly 로고를 cdn.redoc.ly 에서 부른다. CSP 의 `img-src` 가 막고 ReDoc
  이 그 자리를 비우므로 콘솔에 위반 한 줄이 남을 뿐이다. CSP 를 열 일이 아니다.

자산에는 인증을 걸지 않는다. 누구나 받는 라이브러리와 문서 주소만 든 초기화라 API 표면을 드러내지
않는다. 토큰 모드에서 문서 화면과 OpenAPI 문서를 가리는 것은 부르는 쪽이 `dependencies` 로 정한다.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request, params
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from daegam.urls import API_PREFIX

OPENAPI_PATH = f"{API_PREFIX}/openapi.json"
DOCS_PATH = f"{API_PREFIX}/docs"
REDOC_PATH = f"{API_PREFIX}/redoc"
ASSETS_PATH = f"{DOCS_PATH}/assets"
# 우리 파일(초기화 스크립트·스타일)과 fastapi-offline 의 번들을 한 경로 아래 합쳐 낸다
STATIC_DIR = Path(__file__).parent / "static"
BUNDLE_PACKAGE = ("fastapi_offline", "static")


class _Assets(StaticFiles):
    """파일 이름에 판이 없으므로 브라우저가 매번 ETag 로 다시 확인하게 한다(`no-cache`, 그대로면
    304). UI 의 해시 없는 파일과 같은 규칙이다(D52)."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["cache-control"] = "no-cache"
        return response


def _page(title: str, head: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n{head}</head>\n<body>\n{body}</body>\n</html>\n"
    )


def _swagger_ui_page(title: str) -> str:
    return _page(
        f"{title} - Swagger UI",
        f'<link rel="stylesheet" href="{ASSETS_PATH}/swagger-ui.css">\n',
        f'<div id="swagger-ui" data-openapi-url="{OPENAPI_PATH}"></div>\n'
        f'<script src="{ASSETS_PATH}/swagger-ui-bundle.js"></script>\n'
        f'<script src="{ASSETS_PATH}/swagger-ui-init.js"></script>\n',
    )


def _redoc_page(title: str) -> str:
    return _page(
        f"{title} - ReDoc",
        f'<link rel="stylesheet" href="{ASSETS_PATH}/redoc.css">\n',
        f'<redoc spec-url="{OPENAPI_PATH}"></redoc>\n'
        f'<script src="{ASSETS_PATH}/redoc.standalone.js"></script>\n',
    )


def install_api_docs(app: FastAPI, *, dependencies: Sequence[params.Depends] = ()) -> None:
    """문서 세 경로와 자산 경로를 단다. `dependencies` 는 세 경로에만 걸린다. FastAPI 의 자동 문서
    라우트(`docs_url`·`redoc_url`·`openapi_url`)는 앱을 만들 때 꺼 둔다."""
    deps = list(dependencies)
    swagger, redoc = _swagger_ui_page(app.title), _redoc_page(app.title)
    app.mount(ASSETS_PATH, _Assets(directory=STATIC_DIR, packages=[BUNDLE_PACKAGE]))

    # 앱은 클로저로 잡지 않고 요청에서 꺼낸다. FastAPI 는 엔드포인트 함수를 모듈 전역 LRU 캐시
    # (fastapi.dependencies.models 의 분류 캐시 셋, 각 4096칸)에 강하게 잡아 두므로, 앱을 잡은
    # 클로저가 있으면 버린 앱이 캐시에서 밀려날 때까지 살아 있다. 운영은 프로세스에 앱이 하나라
    # 상관없지만 시험마다 앱을 만드는 테스트에서는 worker 마다 수백 개가 쌓였다
    @app.get(OPENAPI_PATH, include_in_schema=False, dependencies=deps)
    def openapi_json(request: Request) -> JSONResponse:
        return JSONResponse(request.app.openapi())

    @app.get(DOCS_PATH, include_in_schema=False, dependencies=deps)
    def swagger_ui() -> HTMLResponse:
        return HTMLResponse(swagger)

    @app.get(REDOC_PATH, include_in_schema=False, dependencies=deps)
    def redoc_ui() -> HTMLResponse:
        return HTMLResponse(redoc)
