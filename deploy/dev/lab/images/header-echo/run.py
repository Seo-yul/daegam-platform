"""헤더를 요구하고 되돌려 주는 외부 MCP 서버 흉내 (P86 8절, e2e-gateway 의 header 단계).

표준 라이브러리만 쓴다. POST /mcp 의 JSON-RPC 를 JSON 으로 답한다(streamable HTTP 의 JSON 응답 꼴).
HEADER_NAME 헤더의 값이 HEADER_VALUES(쉼표로 구분)의 하나가 아니면 MCP 를 처리하기 전에 401 이다.
Tool 은 `echo_header` 하나이고 받은 헤더 값을 그대로 돌려준다. GET /healthz 는 readiness 다.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEADER = os.environ.get("HEADER_NAME", "X-API-Key")
ACCEPTED = {v for v in os.environ.get("HEADER_VALUES", "").split(",") if v}
PORT = int(os.environ.get("PORT", "18082"))
TOOL = {
    "name": "echo_header",
    "description": "returns the header value the server received",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True},
}


def answer(msg: dict, value: str | None) -> dict | None:
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None  # 알림에는 답하지 않는다
    if method == "initialize":
        result = {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "header-echo", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = {
            "content": [{"type": "text", "text": json.dumps({"header": HEADER, "value": value})}],
            "structuredContent": {"header": HEADER, "value": value},
            "isError": False,
        }
    elif method == "ping":
        result = {}
    else:
        error = {"code": -32601, "message": "method not found"}
        return {"jsonrpc": "2.0", "id": rid, "error": error}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: object | None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server 의 이름이다
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(405, None)

    def do_DELETE(self) -> None:  # noqa: N802
        self._send(200, None)

    def do_POST(self) -> None:  # noqa: N802
        value = self.headers.get(HEADER)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if value not in ACCEPTED:
            print(json.dumps({"event": "refused", "value_set": value is not None}), flush=True)
            self._send(401, {"error": "invalid_token"})
            return
        msg = json.loads(raw or b"{}")
        print(json.dumps({"event": "request", "method": msg.get("method")}), flush=True)
        out = answer(msg, value) if isinstance(msg, dict) else None
        self._send(200 if out is not None else 202, out)

    def log_message(self, *_args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 - 파드 안이다
