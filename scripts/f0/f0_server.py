"""P54·P54b F0 의 시험 서버. scripts/f0 의 다른 파일과 함께 F0 측정 동안만 둔다.

플랫폼 이미지(fastmcp·uvicorn 이 든 venv)에 ConfigMap 으로 얹어 돈다(scripts/f0/lib.sh 의
f0_server_up). 한 프로세스가 두 포트를 연다. 클러스터 밖의 서버를 흉내 내는 자리라 인증이 없다.

  :8080  /mcp        streamable HTTP MCP. Tool add·echo·slow·fail, 리소스 f0://note, 프롬프트 greet
         /rest/...   P54 F0.1 의 MCP Server(mcp-runtime)가 부르는 REST 백엔드 echo·slow·fail
         /healthz
  :8081  /sse        같은 MCP 서버의 SSE 전송 (P54b F0.4)

  python f0_server.py serve           두 포트를 연다
  python f0_server.py spec BASE_URL   REST 백엔드의 OpenAPI 3 JSON 을 찍는다(servers 는 BASE_URL)
  python f0_server.py check           두 앱을 만들기만 하고 끝낸다 (정적 확인)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

HTTP_PORT = 8080
SSE_PORT = 8081
# slow 가 기다리는 상한(초). 호출이 클러스터 안에 오래 남지 않게 한다
MAX_SLOW_SECONDS = 120.0

mcp = FastMCP("daegam-f0")


@mcp.tool(annotations={"readOnlyHint": True})
def add(a: int, b: int) -> int:
    """두 정수를 더한다."""
    return a + b


@mcp.tool(annotations={"readOnlyHint": True})
def echo(text: str) -> str:
    """받은 글을 그대로 돌려준다."""
    return text


@mcp.tool
async def slow(seconds: float) -> str:
    """seconds 초 기다린 뒤 답한다."""
    await asyncio.sleep(min(max(seconds, 0.0), MAX_SLOW_SECONDS))
    return f"slept {seconds}"


@mcp.tool
def fail(message: str) -> str:
    """늘 Tool 오류(isError)로 답한다."""
    raise ToolError(message)


@mcp.resource("f0://note")
def note() -> str:
    """리소스 하나. 앞단이 resources 를 넘기는지 본다."""
    return "f0 note"


@mcp.prompt
def greet(name: str) -> str:
    """프롬프트 하나. 앞단이 prompts 를 넘기는지 본다."""
    return f"hello {name}"


def _seconds(request: Request) -> float:
    try:
        return min(max(float(request.query_params.get("seconds", "0")), 0.0), MAX_SLOW_SECONDS)
    except ValueError:
        return 0.0


@mcp.custom_route("/rest/echo", methods=["GET"])
async def rest_echo(request: Request) -> JSONResponse:
    return JSONResponse({"text": request.query_params.get("text", "")})


@mcp.custom_route("/rest/slow", methods=["GET"])
async def rest_slow(request: Request) -> JSONResponse:
    seconds = _seconds(request)
    await asyncio.sleep(seconds)
    return JSONResponse({"slept": seconds})


@mcp.custom_route("/rest/fail", methods=["GET"])
async def rest_fail(request: Request) -> JSONResponse:
    return JSONResponse({"error": "f0 backend failure"}, status_code=500)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def rest_spec(base_url: str) -> dict[str, Any]:
    """REST 백엔드의 OpenAPI 3. operationId 가 mcp-runtime 의 Tool 이름(echo·slow·fail)이 된다."""

    def query(name: str, kind: str) -> dict[str, Any]:
        return {"name": name, "in": "query", "required": True, "schema": {"type": kind}}

    ok = {
        "200": {
            "description": "ok",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    }
    return {
        "openapi": "3.0.3",
        "info": {"title": "daegam-f0", "version": "1"},
        "servers": [{"url": base_url.rstrip("/")}],
        "paths": {
            "/rest/echo": {
                "get": {
                    "operationId": "echo",
                    "summary": "Return the text",
                    "parameters": [query("text", "string")],
                    "responses": ok,
                }
            },
            "/rest/slow": {
                "get": {
                    "operationId": "slow",
                    "summary": "Answer after the given seconds",
                    "parameters": [query("seconds", "number")],
                    "responses": ok,
                }
            },
            "/rest/fail": {
                "get": {"operationId": "fail", "summary": "Always answer 500", "responses": ok}
            },
        },
    }


def apps() -> list[tuple[Any, int]]:
    # 이 서버는 0.0.0.0 에 붙고 Host 머리는 Ingress·Service 이름마다 달라 Host 검사를 끈다
    http = mcp.http_app(path="/mcp", transport="http", host_origin_protection=False)
    sse = mcp.http_app(path="/sse", transport="sse")
    return [(http, HTTP_PORT), (sse, SSE_PORT)]


async def serve() -> None:
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info"))
        for app, port in apps()
    ]
    await asyncio.gather(*(s.serve() for s in servers))


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "serve":
        asyncio.run(serve())
        return 0
    if cmd == "spec" and len(argv) == 3:
        print(json.dumps(rest_spec(argv[2]), indent=1))
        return 0
    if cmd == "check":
        print(" ".join(f"{type(app).__name__}:{port}" for app, port in apps()))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
