"""플랫폼 `web_search` 의 MCP 엔드포인트 (P39b 12절).

`/api/internal/mcp/web-search` 에 FastMCP streamable-http 앱을 `stateless_http` 로 단다. 세션이 없어
두 레플리카 어디로 가도 된다. OpenAPI 밖이라 비서의 도구 표에도 없다. vMCP 가 headerInjection 으로
보내는 자기 서비스 토큰만 받고(`verify_gateway_token`), 그 밖은 401 이다. `tools/list` 는 메모리의
정의로 답해 Mongo 를 밟지 않는다. 감사 미들웨어는 이 경로를 건너뛰고, 여기서 `tools/call` 마다 감사
행 하나를 남긴다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from daegam.audit.store import AuditEvent
from daegam.auth.oidc import AuthError
from daegam.auth.service_token import service_sub, verify_gateway_token
from daegam.body_limit import BodyLimitMiddleware
from daegam.services.web_search import (
    SEARCH_TOOL,
    Caller,
    SearchFailure,
    model_text,
    query_fingerprint,
)
from daegam.store.base import new_id
from daegam.urls import WEB_SEARCH_MCP_PATH

logger = logging.getLogger("daegam.web_search")

PATH = WEB_SEARCH_MCP_PATH
CALLER = "web_search_caller"
# 검색 요청 본문의 상한. JSON-RPC 한 통에 질의 400 자라 넉넉하다. 앱 전체의 상한(업로드 몫)보다 작다
BODY_MAX_BYTES = 64 * 1024


class _GatewayTokenOnly:
    """Authorization 의 Gateway 서비스 토큰을 검증해 scope state 에 (테넌트, Gateway)를 둔다."""

    def __init__(self, app: ASGIApp, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        issuer = self.state.internal_issuer
        try:
            if not token or not issuer.loaded:
                raise AuthError("no service token")
            tenant, gateway = verify_gateway_token(
                token, issuer.signer, issuer=self.state.settings.platform_issuer_url
            )
        except AuthError:
            await JSONResponse({"code": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        scope.setdefault("state", {})[CALLER] = Caller(tenant=tenant, gateway=gateway)
        await self.app(scope, receive, send)


class SearchTool(Tool):
    """`web_search`. 결과는 structuredContent 와 모델 글이고, 실패는 isError 와 `{code,
    message}`."""

    app_state: Any = None

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        request = get_http_request()
        caller: Caller = request.scope["state"][CALLER]
        state = self.app_state
        code = "ok"
        try:
            data = await state.web_search.search(caller, arguments)
        except Exception as e:  # noqa: BLE001  남의 글이 모델에게 가지 않게 우리 코드로 바꾼다
            failure = e if isinstance(e, SearchFailure) else SearchFailure("search_unavailable")
            if not isinstance(e, SearchFailure):
                # 예외의 글에는 provider 의 URL(질의)이나 답이 들 수 있어 종류만 적는다
                logger.warning("web_search failed: %s", type(e).__name__)
            code = failure.code
            body = failure.body()
            result = ToolResult(content=_text(body), structured_content=body, is_error=True)
        else:
            text = [TextContent(type="text", text=model_text(data))]
            result = ToolResult(content=text, structured_content=data)
        query = arguments.get("query") if isinstance(arguments, dict) else None
        logger.info(
            "web_search tenant=%s gateway=%s code=%s %s",
            caller.tenant,
            caller.gateway,
            code,
            query_fingerprint(query) if isinstance(query, str) else {},
        )
        await _audit(state, caller, code)
        return result


def _text(body: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(body, ensure_ascii=False))]


async def _audit(state: Any, caller: Caller, code: str) -> None:
    """`tools/call` 하나의 감사 행. 실패해도 답을 막지 않는다."""
    try:
        await state.audit.record(
            AuditEvent(
                sub=service_sub("vmcp", caller.gateway),
                subject_type="service",
                tenant=caller.tenant,
                action="web_search",
                resource="web-search",
                resource_id=caller.gateway_id or caller.gateway,
                method="POST",
                path=PATH,
                request_id=new_id(),
                status=200,
                result="ok" if code == "ok" else "error",
                error_code=None if code == "ok" else code,
            )
        )
    except Exception as e:  # noqa: BLE001 - 감사 실패가 검색을 막지 않는다
        logger.warning("web_search audit row not written: %s", e)


def build_app(state: Any) -> Any:
    """검색 MCP 앱. state 는 FastAPI 의 app.state 다(서비스는 lifespan 뒤에 붙는다). 앱의
    lifespan 은 플랫폼 lifespan 안에서 연다(`main.py`)."""
    # 예상하지 못한 예외의 글이 모델에게 가지 않게 한다. 실패는 SearchTool.run 이 우리 코드로 바꾼다
    mcp = FastMCP(name="daegam-web-search", mask_error_details=True)
    mcp.add_tool(
        SearchTool(
            name=SEARCH_TOOL["name"],
            title=SEARCH_TOOL["title"],
            description=SEARCH_TOOL["description"],
            parameters=SEARCH_TOOL["inputSchema"],
            app_state=state,
        )
    )
    return mcp.http_app(path="/", stateless_http=True, json_response=True)


class WebSearchDispatch:
    """`PATH` 로 오는 요청을 검색 앱으로 보낸다(경로는 앱 안에서 `/`). 감사·보호 헤더 미들웨어보다
    바깥에 두어 감사가 이 경로를 보지 않는다. 그 밖의 요청은 그대로 지난다."""

    def __init__(self, app: ASGIApp, *, search_app: Any, state: Any) -> None:
        self.app = app
        self.search = BodyLimitMiddleware(
            _GatewayTokenOnly(search_app, state), max_bytes=BODY_MAX_BYTES
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in (PATH, PATH + "/"):
            inner = {**scope, "path": "/", "raw_path": b"/", "root_path": PATH}
            await self.search(inner, receive, send)
            return
        await self.app(scope, receive, send)
