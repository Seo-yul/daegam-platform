"""진입점. 계약 파일(`schema/tools.json`)의 도구를 FastMCP streamable-http 로 `:8000/mcp` 에 띄운다.

도구의 입력 스키마는 계약 파일 그대로다. FastMCP 가 함수 서명에서 스키마를 만들게 두면 계약과 갈라질
수 있어 `Tool` 을 직접 만든다. 플랫폼은 같은 파일을 이미지에 담아 허브의 도구 탭과 도구 이름(D49)에
쓴다.

환경변수: WEB_TOOLS_HOST / WEB_TOOLS_PORT / WEB_TOOLS_PATH (기본 0.0.0.0 / 8000 / /mcp), 그리고
`webtools/settings.py` 의 WEB_FETCH_*.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult

from webtools import settings as settings_mod
from webtools.contract import tool_spec
from webtools.fetch import Fetcher, ToolFailure, error_body, log, log_call, model_text, text_block


class FetchTool(Tool):
    """`web_fetch`. 결과는 structuredContent 와 모델 글이고, 실패는 isError 와 `{code, message}`
    다."""

    fetcher: Any = None

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        url = arguments.get("url") if isinstance(arguments, dict) else None
        try:
            args = arguments if isinstance(arguments, dict) else {}
            data, size = await self.fetcher.fetch_sized(args)
        except ToolFailure as failure:
            return _failed(url, failure, started)
        except Exception as e:  # noqa: BLE001  남의 글이 모델에게 가지 않게 우리 코드로 바꾼다
            # 예외의 글에는 가져온 URL 이나 본문이 들 수 있어 종류만 적는다
            log.warning("web_fetch failed: %s", type(e).__name__)
            return _failed(url, ToolFailure("fetch_failed"), started)
        log_call(url, data, size, "ok", started)
        return ToolResult(content=text_block(model_text(data)), structured_content=data)


def _failed(url: Any, failure: ToolFailure, started: float) -> ToolResult:
    body = error_body(failure)
    log_call(url, None, 0, failure.code, started)
    return ToolResult(content=text_block(body), structured_content=body, is_error=True)


def build_server(fetcher: Fetcher) -> FastMCP:
    # 예상하지 못한 예외의 글이 모델에게 가지 않게 한다. 실패는 run() 이 우리 코드로 바꾼다
    mcp = FastMCP(name="daegam-web-tools", mask_error_details=True)
    spec = tool_spec("web_fetch")
    mcp.add_tool(
        FetchTool(
            name=spec["name"],
            title=spec.get("title"),
            description=spec["description"],
            parameters=spec["inputSchema"],
            fetcher=fetcher,
        )
    )
    _add_health_routes(mcp)
    return mcp


def _add_health_routes(mcp: FastMCP) -> None:
    """ToolHive proxyrunner 의 `GET /` 헬스체크와 `/health` 에 200 을 준다."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    for path in ("/", "/health"):
        mcp.custom_route(path, methods=["GET"], include_in_schema=False)(health)


def configure_logging() -> None:
    """기록은 `log_call` 의 한 줄뿐이다. httpx 와 httpcore 는 INFO 에 요청 URL 을 통째로 적으므로
    WARNING 위만 남긴다. 모델이 쓴 쿼리가 파드 기록에 남지 않게 하려는 것이다 (P39b 16절)."""
    logging.basicConfig(level=os.environ.get("WEB_TOOLS_LOG_LEVEL", "INFO"), stream=sys.stderr)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> int:
    configure_logging()
    host = os.environ.get("WEB_TOOLS_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_TOOLS_PORT", "8000"))
    path = os.environ.get("WEB_TOOLS_PATH", "/mcp")
    mcp = build_server(Fetcher(settings_mod.load()))
    mcp.run(transport="http", host=host, port=port, path=path, show_banner=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
