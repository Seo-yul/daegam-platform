"""MCP 도구 인자를 이름으로 채우는 규칙 하나 (P32·P33 e2e).

e2e 는 대상 도구의 스키마를 모른 채 계산기 같은 도구를 부른다. 규칙 PATTERN=VALUE 마다 이름이
PATTERN(대소문자를 가리지 않는 정규식)에 맞는 첫 인자에 VALUE 를 넣는다. VALUE 는 JSON 으로 읽히면
그 값이고 아니면 글자다. 스크립트 클라이언트(mcp_oauth_client.py 의 call)는 import 하고, bash 판정
(scripts/lib/e2e_mcp_access.sh 의 tool_args)은 명령으로 부른다. 둘이 같은 규칙에 다른 인자를 만들지
않게 여기 하나만 둔다. 표준 라이브러리만 쓰므로 시스템 python3 로도 돈다.

실행: python3 deploy/dev/lab/tool_fill.py TOOLS_LIST_JSON TOOL_NAME [PATTERN=VALUE]...
  tools/list 응답을 담은 파일에서 TOOL_NAME 의 inputSchema 를 채운 인자 JSON 을 찍는다. 채우지
  못하면 까닭을 찍고 1 이다.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


def fill_args(schema: dict[str, Any] | None, rules: list[str]) -> dict[str, Any]:
    """schema(inputSchema)의 인자를 rules 로 채운다. 맞는 인자가 없는 규칙이 있으면
    LookupError 다."""
    props = list(((schema or {}).get("properties") or {}).keys())
    args: dict[str, Any] = {}
    for rule in rules:
        pattern, _, raw = rule.partition("=")
        key = next((k for k in props if re.search(pattern, k, re.IGNORECASE)), None)
        if key is None:
            raise LookupError(f"no argument matches {pattern!r}: {props}")
        try:
            args[key] = json.loads(raw)
        except ValueError:
            args[key] = raw
    return args


def main(argv: list[str]) -> int:
    path, name, *rules = argv
    with open(path, encoding="utf-8") as f:
        tools = json.load(f)["result"]["tools"]
    tool = next((t for t in tools if t["name"] == name), None)
    if tool is None:
        print(f"tools/list has no {name}", file=sys.stderr)
        return 1
    try:
        print(json.dumps(fill_args(tool.get("inputSchema"), rules)))
    except LookupError as e:
        print(f"cannot fill the arguments of {name}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
