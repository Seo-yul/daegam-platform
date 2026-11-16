"""lab·e2e 가 쓰는 MCP 호출 CLI. 구현은 platform/daegam/mcp_client.py 하나이고 여기는 그것을 감싼다.

실행: uv run --project platform python deploy/dev/lab/mcp_client.py ...
  mcp_client.py [--token TOKEN] [--host HOST] [--timeout SECONDS] list URL
  mcp_client.py [--token TOKEN] [--host HOST] [--timeout SECONDS] call URL TOOL '{"a": 3, "b": 4}'
--token 은 Authorization: Bearer 를 붙인다 (P6 OIDC vMCP 게이트). --host 는 Host 헤더다
(Ingress 를 IP 로 부를 때). --timeout 은 요청 하나의 시간 제한이고 기본 30초다.
stdout 에 JSON 한 객체를 찍는다. seconds 는 목록과 호출에 걸린 시간이다. 프로토콜 오류나 도구
오류면 0 이 아닌 코드로 끝난다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx2

from daegam.mcp_client import call_tool, list_tools

DEFAULT_TIMEOUT = 30.0


def _client(
    token: str | None, host: str | None = None, timeout: float = DEFAULT_TIMEOUT
) -> httpx2.AsyncClient | None:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    if not headers and timeout == DEFAULT_TIMEOUT:
        return None
    return httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout))


async def run(
    mode: str,
    url: str,
    tool: str | None,
    args: dict,
    token: str | None,
    host: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    start = time.monotonic()
    listed = await list_tools(url, timeout=timeout, http_client=_client(token, host, timeout))
    out: dict = {
        "server": listed.server,
        "protocolVersion": listed.protocol_version,
        "tools": listed.tools,
    }
    if mode == "call":
        assert tool is not None
        res = await call_tool(
            url, tool, args, timeout=timeout, http_client=_client(token, host, timeout)
        )
        out["call"] = {
            "tool": tool,
            "arguments": args,
            "isError": res.is_error,
            "content": res.content,
            "structuredContent": res.structured,
        }
    out["seconds"] = round(time.monotonic() - start, 1)
    return out


def main(argv: list[str]) -> int:
    opts: dict[str, str] = {}
    while len(argv) > 2 and argv[1] in ("--token", "--host", "--timeout"):
        opts[argv[1]] = argv[2]
        argv = [argv[0]] + argv[3:]
    token, host = opts.get("--token"), opts.get("--host")
    timeout = float(opts.get("--timeout", DEFAULT_TIMEOUT))
    if len(argv) < 3 or argv[1] not in ("list", "call"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, url = argv[1], argv[2]
    tool = argv[3] if mode == "call" else None
    args = json.loads(argv[4]) if mode == "call" and len(argv) > 4 else {}
    out = asyncio.run(run(mode, url, tool, args, token, host, timeout))
    print(json.dumps(out, ensure_ascii=False))
    if mode == "call" and out["call"]["isError"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
