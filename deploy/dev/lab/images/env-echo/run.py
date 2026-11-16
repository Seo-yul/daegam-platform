"""stdio 로 MCP 를 말하는 카탈로그 모양의 lab 서버 (P55 F8, e2e-gateway 의 허브 이미지 단계).

표준 라이브러리만 쓴다. stdin 의 줄마다 JSON-RPC 메시지 하나를 읽고 답을 stdout 에 줄 하나로 쓴다(MCP stdio 전송).
Tool 은 `echo_env` 하나이고 `name` 인자로 받은 env 의 값을 돌려준다. Secret env 를 바꾼 값이 컨테이너 안에 전달됐는지를
e2e 가 이것으로 본다. 이름이 E2E_ 로 시작하는 env 만 읽어 다른 값을 내보내지 않는다.
"""

from __future__ import annotations

import json
import os
import sys

PREFIX = "E2E_"
TOOL = {
    "name": "echo_env",
    "description": "returns the value of an environment variable whose name starts with E2E_",
    "inputSchema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    "annotations": {"readOnlyHint": True},
}


def call(arguments: dict) -> dict:
    name = str(arguments.get("name") or "")
    if not name.startswith(PREFIX):
        text, error = f"only {PREFIX} names are read", True
    else:
        text, error = json.dumps({"name": name, "value": os.environ.get(name)}), False
    return {"content": [{"type": "text", "text": text}], "isError": error}


def answer(msg: dict) -> dict | None:
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None  # 알림에는 답하지 않는다
    params = msg.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "env-echo", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call" and params.get("name") == TOOL["name"]:
        result = call(params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        error = {"code": -32601, "message": "method not found"}
        return {"jsonrpc": "2.0", "id": rid, "error": error}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = answer(msg) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
