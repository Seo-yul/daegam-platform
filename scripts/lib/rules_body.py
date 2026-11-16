"""e2e 의 Tool 규칙 저장 본문 (P52 1절). e2e_agent·e2e_mcp_auth 가 함께 쓴다.

`GET /mcp-servers/{id}` 의 답을 그대로 `PUT /mcp-servers/{id}` 의 본문으로 바꾸고, 첫 backend 의 규칙과 오버라이드를 준
것으로 갈아 끼운다. 규칙을 비우면(`{}`) 규칙이 없는 정의가 된다. 이름과 나머지 정의는 저장된 것 그대로라 새 리비전은
규칙과 오버라이드만 다르다.

    python3 rules_body.py SERVER_JSON RULES_JSON [--overrides OVERRIDES_JSON]

RULES_JSON 은 operationId 마다의 규칙({"Add": {"callers": {"agents": ["<id>"]}}})이고, OVERRIDES_JSON 은
tool_overrides({"Add": {"name": "add_numbers"}}) 다. --overrides 를 주지 않으면 저장된 오버라이드를 둔다.
맥의 시스템 python3(3.9)에서도 돌아야 하므로 3.10 이후 문법을 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("server", help="GET /mcp-servers/{id} 의 답을 담은 파일")
    ap.add_argument("rules", help="첫 backend 의 tool_rules (JSON)")
    ap.add_argument("--overrides", default=None, help="첫 backend 의 tool_overrides (JSON)")
    a = ap.parse_args(argv[1:])
    with open(a.server) as f:
        doc = json.load(f)
    backends = [dict(b) for b in doc["backends"]]
    backends[0]["tool_rules"] = json.loads(a.rules)
    if a.overrides is not None:
        backends[0]["tool_overrides"] = json.loads(a.overrides)
    body = {
        "name": doc["name"],
        "display_name": doc.get("display_name"),
        "backends": backends,
        "runtime": doc["runtime"],
        "bindings": doc.get("bindings") or [],
    }
    print(json.dumps(body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
