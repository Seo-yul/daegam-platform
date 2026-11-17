"""P56 F0.1·F0.5: Agent 런타임의 클라이언트가 Code mode Gateway 에서 무엇을 보는지.
scripts/f0 의 다른 파일과 함께 F0 측정 동안만 둔다.

런타임과 같은 클라이언트(`runtime.mcp.build_client`, `BearerAuth`)로 tools/list 를 읽고,
런타임처럼 langchain.mcp 의 `as_langchain_tool` 로 execute_tool_script 를 Tool 로 만들어
스크립트 하나를 부른다. 런타임의 `ToolSources._wrap` 처럼 `asyncio.timeout(SECONDS)` 안에서
기다린다(기본은 런타임의 DEFAULT_TOOL_TIMEOUT_SECONDS). SRC 의 @이름@ 은 p56_tool.py 의
script 와 같이 광고 이름으로 바꾼다. 토큰은 환경 F0_TOKEN 으로만 받는다. --ip 는 p56_tool.py
와 같다(이 프로세스의 이름 풀이만 바꾼다). 결과를 FILE 에 JSON 으로 남기고 한 줄로 찍는다.

    uv run --project agent-runtime python scripts/f0/p56_runtime.py \\
        --url URL [--ip IP] [--seconds SECONDS] --code SRC --out FILE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from langchain_core._api import suppress_langchain_beta_warning
from p56_tool import SCRIPT, advertise, describe, pin_host, script_functions
from runtime.mcp import DEFAULT_TOOL_TIMEOUT_SECONDS, BearerAuth, build_client

with suppress_langchain_beta_warning():
    from langchain.mcp import as_langchain_tool


async def probe(url: str, code: str, seconds: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    started = time.monotonic()
    c = build_client({"url": url}, BearerAuth(os.environ.get("F0_TOKEN", "")))
    try:
        async with c:
            defs = await c.list_tools()
            out["protocol"] = c.protocol_version
            out["tools"] = [d.name for d in defs]
            virtual = next((d for d in defs if d.name == SCRIPT), None)
            out["listed"] = virtual is not None
            if virtual is not None:
                out["functions"] = script_functions(virtual.description or "")
                tool = await as_langchain_tool(virtual, c)
                src = advertise(code, out["tools"])
                called = time.monotonic()
                try:
                    async with asyncio.timeout(seconds):
                        r = await tool.ainvoke({"script": src})
                    out["call"] = {"outcome": "returned", "detail": str(r)[:300]}
                except TimeoutError:
                    out["call"] = {
                        "outcome": "runtime timeout",
                        "detail": f"no answer in {seconds:g} s",
                    }
                except BaseException as e:  # noqa: BLE001 - 무엇이 오는지 적는다
                    out["call"] = {"outcome": "raised", "detail": describe(e)}
                out["call"].update(waited=seconds, s=round(time.monotonic() - called, 1))
    except BaseException as e:  # noqa: BLE001
        out["session"] = describe(e)
    out["s"] = round(time.monotonic() - started, 2)
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--ip")
    p.add_argument("--seconds", type=float, default=float(DEFAULT_TOOL_TIMEOUT_SECONDS))
    p.add_argument("--code", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv[1:])
    pin_host(args.url, args.ip)
    out = asyncio.run(probe(args.url, args.code, args.seconds))
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    if "session" in out:
        print(f"runtime client: session failed: {out['session']}")
        return 1
    print(
        f"runtime client: protocol {out['protocol']}, {SCRIPT} "
        f"{'listed' if out['listed'] else 'NOT listed'}, functions {out.get('functions')}, "
        f"script call {out.get('call')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
