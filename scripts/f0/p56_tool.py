"""P56 F0.1·F0.4·F0.5·F0.6 의 측정 도구. scripts/f0 의 다른 파일과 함께 F0 측정 동안만 둔다.

플랫폼의 가상 환경에서 돈다(scripts/f0/p56.sh 의 p56_tool):
    uv run --project platform python scripts/f0/p56_tool.py <하위 명령> ...
토큰은 인자가 아니라 환경 F0_TOKEN 으로만 받고, 무엇도 토큰을 찍지 않는다. --ip 를 주면
이 프로세스가 URL 의 호스트를 그 IP 로 푼다(Traefik 의 LoadBalancer IP 에 못 박으므로
/etc/hosts 가 필요 없다). URL 과 Host 머리는 그대로다. MCP 클라이언트는 fastmcp 의
Client 이고, mode auto 는 2026-07-28 server/discover 를 먼저 보내는 Modern 클라이언트,
legacy 는 initialize 로 세션을 여는 클라이언트다.

  listing   --url URL [--ip IP] --mode auto|legacy --out FILE
            tools/list 를 읽어 맺은 규약 판, Tool 이름과 readOnlyHint,
            execute_tool_script 가 있는지와 그 설명이 늘어놓는 함수 이름을 FILE 에
            JSON 으로 남기고 한 줄로 찍는다
  script    --url URL [--ip IP] --code SRC --label NAME --out FILE [--timeout SECONDS]
            execute_tool_script 를 한 번 부르고 결과(ok·error·raised, 걸린 초,
            결과 글의 길이와 앞부분)를 FILE 에 JSON 한 줄로 더하고 한 줄로 찍는다.
            SRC 의 @이름@ 은 tools/list 에서 그 이름으로 끝나는 Tool 의 광고
            이름으로 바꾼다(vMCP 의 접두)
  ready     --url URL [--ip IP] --since EPOCH [--timeout SECONDS]
            새 세션의 tools/list 가 execute_tool_script 를 보일 때까지 1초마다
            다시 부르고, 그때와 SINCE 에서 걸린 초를 JSON 으로 찍는다
  codemode  CONFIGMAPS.json --owner NAME
            kubectl get configmap -o json 의 목록 가운데 VirtualMCPServer NAME 이
            소유하거나 이름에 NAME 이 든 ConfigMap 에서 codeMode 를 말하는 줄과 그
            아래 몇 줄을 줄마다 200자까지 찍는다
  rollup    BEFORE.json AFTER.json
            P51 메트릭 응답(GET .../metrics) 둘의 합계, Tool 별, 백엔드별 호출
            수를 앞뒤로 견주는 표를 찍는다
  settled   METRICS.json --after EPOCH
            메트릭 응답의 source 를 한 줄로 찍는다. rolled_up_at 이 AFTER 뒤이고
            counted_from 이 비었으면 0, 아니면 1 이다
  sample    --node NODE --uid UID --out FILE --stop FILE [--interval SECONDS]
            k3d 노드 컨테이너 안의 파드 cgroup 에서 memory.current·memory.peak·
            memory.max 와 memory.events 의 oom·oom_kill 을 STOP 파일이 생길
            때까지 읽어 FILE 에 JSON 한 줄씩 남긴다
  memory    --samples FILE --start EPOCH --end EPOCH [--suggest]
            sample 기록을 스크립트의 창으로 줄여 한 줄로 찍는다. --suggest 면
            6.4절의 기본값 후보(잰 최고치의 두 배, 256 MiB 아래로는 가지 않는다)를
            덧붙인다
  others    --loop FILE --start EPOCH --end EPOCH
            f0_tool loop 의 기록(다른 세션의 직접 호출)을 그 창으로 줄여 한 줄로
            찍는다
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from f0_tool import tool_named

SCRIPT = "execute_tool_script"
MIB = 1024 * 1024
# 6.4절: 기본값은 잰 최고치의 두 배이고 proxy_resources 의 256 MiB 아래로 가지 않는다
FLOOR_MIB = 256
HEAD = 300  # 결과 글에서 남기는 앞부분


def pin_host(url: str, ip: str | None) -> None:
    """이 프로세스의 이름 풀이에서 URL 의 호스트를 ip 로 바꾼다. 소켓을 여는 클라이언트
    (httpx, 런타임의 클라이언트)가 모두 socket.getaddrinfo 를 거치므로 클라이언트 코드는
    그대로 둔다."""
    if not ip:
        return
    host = urlsplit(url).hostname
    real: Callable[..., Any] = socket.getaddrinfo

    def resolve(name: Any, *args: Any, **kwargs: Any) -> Any:
        # anyio 는 이름을 idna 로 바꾼 bytes 로 넘긴다
        text = name.decode() if isinstance(name, bytes) else name
        return real(ip if text == host else name, *args, **kwargs)

    socket.getaddrinfo = resolve  # type: ignore[assignment]


def describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return " | ".join(describe(x) for x in e.exceptions)
    return f"{type(e).__name__}: {str(e)[:300]}"


def script_functions(description: str) -> list[str]:
    """execute_tool_script 의 설명이 늘어놓는 함수 이름 (ToolHive 0.53 의 `  - name: ...` 줄)."""
    out = []
    for line in description.splitlines():
        m = re.match(r"\s+- ([^:]+):", line)
        if m:
            out.append(m.group(1))
    return out


def text_of(result: Any) -> str:
    return " ".join(getattr(c, "text", "") or "" for c in (getattr(result, "content", None) or []))


def client(url: str, mode: str = "auto", timeout: float = 30.0) -> Any:
    from fastmcp import Client

    return Client(url, auth=os.environ.get("F0_TOKEN") or None, mode=mode, timeout=timeout)


def mib(v: int | None) -> str:
    return "-" if v is None else f"{v / MIB:.1f} MiB"


def jsonl(path: str) -> list[dict[str, Any]]:
    """한 줄에 JSON 하나. 아직 쓰는 중인 파일(다른 세션의 loop)의 반쯤 쓴 줄은 건너뛴다."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def advertise(code: str, names: list[str]) -> str:
    """SRC 의 @이름@ 을 그 이름으로 끝나는 광고 이름으로 바꾼다(vMCP 의 접두)."""
    listed = [{"name": n} for n in names]
    return re.sub(
        r"@(\w+)@", lambda m: (tool_named(listed, m.group(1)) or {"name": m.group(1)})["name"], code
    )


# ---------- listing ----------


async def _listing(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": args.mode}
    started = time.monotonic()
    try:
        async with client(args.url, args.mode) as c:
            tools = await c.list_tools()
            out["protocol"] = c.protocol_version
    except BaseException as e:  # noqa: BLE001 - 무엇이 오는지 적는다
        out["session"] = describe(e)
        return out
    out["s"] = round(time.monotonic() - started, 2)
    out["tools"] = [t.name for t in tools]
    out["read_only_hint"] = {
        t.name: (t.annotations.read_only_hint if t.annotations else None) for t in tools
    }
    virtual = next((t for t in tools if t.name == SCRIPT), None)
    out["listed"] = virtual is not None
    if virtual is not None:
        out["functions"] = script_functions(virtual.description or "")
        out["input_schema"] = virtual.input_schema
        out["description_head"] = (virtual.description or "")[:600]
    return out


def listing(args: argparse.Namespace) -> int:
    pin_host(args.url, args.ip)
    out = asyncio.run(_listing(args))
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    if "session" in out:
        print(f"{args.mode}: session failed: {out['session']}")
        return 1
    others = [n for n in out["tools"] if n != SCRIPT]
    print(
        f"{args.mode}: protocol {out['protocol']}, {SCRIPT} "
        f"{'listed' if out['listed'] else 'NOT listed'}, functions {out.get('functions')}, "
        f"backend tools {others} (readOnlyHint {[out['read_only_hint'][n] for n in others]})"
    )
    return 0


# ---------- script ----------


async def _script(args: argparse.Namespace) -> dict[str, Any]:
    rec: dict[str, Any] = {"label": args.label, "t0": round(time.time(), 3)}
    started = time.monotonic()
    try:
        async with client(args.url, "auto", args.timeout) as c:
            code = args.code
            if "@" in code:
                code = advertise(code, [t.name for t in await c.list_tools()])
            rec["script"] = code
            r = await c.call_tool_mcp(SCRIPT, {"script": code}, timeout=args.timeout)
        text = text_of(r)
        rec.update(outcome="error" if r.is_error else "ok", size=len(text), head=text[:HEAD])
    except BaseException as e:  # noqa: BLE001 - 무엇이 오는지 적는다
        rec.update(outcome="raised", detail=describe(e))
    rec["s"] = round(time.monotonic() - started, 2)
    rec["t1"] = round(time.time(), 3)
    return rec


def script(args: argparse.Namespace) -> int:
    pin_host(args.url, args.ip)
    rec = asyncio.run(_script(args))
    with open(args.out, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    body = rec.get("head") if "head" in rec else rec.get("detail")
    size = f", {rec['size']} chars" if "size" in rec else ""
    print(f"{args.label}: {rec['outcome']} in {rec['s']}s{size}: {str(body)[:200]}")
    return 0


# ---------- ready ----------


async def _ready(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.time() + args.timeout
    tries, last = 0, ""
    while time.time() < deadline:
        tries += 1
        try:
            async with asyncio.timeout(10):
                async with client(args.url, "auto", 5.0) as c:
                    names = [t.name for t in await c.list_tools()]
            if SCRIPT in names:
                now = time.time()
                return {
                    "ready_at": round(now, 3),
                    "after": round(now - args.since, 1),
                    "tries": tries,
                }
            last = f"listed without {SCRIPT}"
        except BaseException as e:  # noqa: BLE001 - 다시 뜨는 중이다
            last = describe(e)
        await asyncio.sleep(1)
    return {"ready_at": None, "after": None, "tries": tries, "last": last}


def ready(args: argparse.Namespace) -> int:
    pin_host(args.url, args.ip)
    out = asyncio.run(_ready(args))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ready_at"] else 1


# ---------- codemode ----------


LINE = 200  # 공개 로그에 싣는 한 줄의 글자 수


def _owned(cm: dict[str, Any], owner: str) -> bool:
    meta = cm.get("metadata") or {}
    refs = meta.get("ownerReferences") or []
    return owner in str(meta.get("name", "")) or any(
        r.get("kind") == "VirtualMCPServer" and r.get("name") == owner for r in refs
    )


def codemode(args: argparse.Namespace) -> int:
    found = 0
    items = json.loads(Path(args.configmaps).read_text()).get("items") or []
    for cm in (c for c in items if _owned(c, args.owner)):
        for key, value in (cm.get("data") or {}).items():
            lines = str(value).splitlines()
            for i, line in enumerate(lines):
                if re.search(r"code_?mode", line, re.IGNORECASE):
                    found += 1
                    print(f"ConfigMap {cm['metadata']['name']} key {key}:")
                    for shown in lines[i : i + 6]:
                        print(f"  {shown[:LINE]}")
    if not found:
        print(f"no ConfigMap of {args.owner} mentions codeMode")
    return 0


# ---------- rollup ----------


def _rows(d: dict[str, Any], key: str, name: str) -> dict[str, int]:
    return {str(r.get(name)): int(r.get("calls") or 0) for r in d.get(key) or []}


def rollup(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text())
    after = json.loads(Path(args.after).read_text())
    tb, ta = before.get("totals") or {}, after.get("totals") or {}
    print("| | before | after |")
    print("|---|---|---|")
    print(f"| calls | {tb.get('calls')} | {ta.get('calls')} |")
    print(f"| errors | {tb.get('errors')} | {ta.get('errors')} |")
    print(f"| outcomes | {json.dumps(tb.get('outcomes'))} | {json.dumps(ta.get('outcomes'))} |")
    for key, name in (("by_tool", "tool"), ("by_backend", "entry")):
        rb, ra = _rows(before, key, name), _rows(after, key, name)
        for row in sorted(set(rb) | set(ra)):
            print(f"| {key} {row} | {rb.get(row, 0)} | {ra.get(row, 0)} |")
    print(f"| source | {json.dumps(before.get('source'))} | {json.dumps(after.get('source'))} |")
    return 0


def _epoch(value: str) -> float:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()


def settled(args: argparse.Namespace) -> int:
    try:
        src = json.loads(Path(args.file).read_text()).get("source") or {}
    except (OSError, ValueError) as e:
        print(f"no metrics answer: {describe(e)}")
        return 1
    at, since = src.get("rolled_up_at"), src.get("counted_from")
    print(
        f"rolled_up_at {at}, counted_from {since}, audit {src.get('audit')}, "
        f"collector {src.get('collector')}"
    )
    return 0 if at and since is None and _epoch(at) > args.after else 1


# ---------- sample / memory ----------


def _cgroup(node: str, uid: str) -> str | None:
    """노드 컨테이너 안의 파드 cgroup 디렉터리. cgroupfs(pod<uid>)와
    systemd(…pod<uid_>.slice) 두 꼴을 찾는다."""
    for pattern in (f"pod{uid}", f"*pod{uid.replace('-', '_')}.slice"):
        try:
            r = subprocess.run(
                ["docker", "exec", node, "find", "/sys/fs/cgroup", "-maxdepth", "6", "-type", "d",
                 "-name", pattern],
                capture_output=True, text=True, timeout=30, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            continue
        hits = r.stdout.split()
        if hits:
            return min(hits, key=len)
    return None


# memory.events 의 줄 가운데 남기는 것. 그 파일의 max 는 한도에 닿은 횟수라
# memory.max 와 이름을 나누지 않는다
EVENTS = ("oom", "oom_kill")


def _read(node: str, cgroup: str) -> dict[str, Any]:
    cmd = (
        f"for f in memory.current memory.peak memory.max; do printf '%s ' $f; "
        f"cat {cgroup}/$f 2>/dev/null || echo -; done; "
        f'while read -r k v; do echo "events.$k $v"; done < {cgroup}/memory.events'
    )
    try:
        r = subprocess.run(
            ["docker", "exec", node, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return {"error": "docker exec timed out after 10 s"}
    rec: dict[str, Any] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        value: Any = int(parts[1]) if parts[1].isdigit() else parts[1]
        if parts[0].startswith("memory."):
            rec[parts[0].removeprefix("memory.")] = value
        elif parts[0].removeprefix("events.") in EVENTS:
            rec[parts[0].removeprefix("events.")] = value
    if r.returncode:
        rec["error"] = r.stderr.strip()[:200]
    return rec


def sample(args: argparse.Namespace) -> int:
    stop = Path(args.stop)
    with open(args.out, "a") as out:
        cgroup = _cgroup(args.node, args.uid)
        out.write(json.dumps({"t": round(time.time(), 3), "cgroup": cgroup}) + "\n")
        if cgroup is None:
            return 0
        while not stop.exists():
            started = time.time()
            out.write(json.dumps({"t": round(started, 3), **_read(args.node, cgroup)}) + "\n")
            out.flush()
            time.sleep(max(0.0, args.interval - (time.time() - started)))
    return 0


def memory(args: argparse.Namespace) -> int:
    recs = jsonl(args.samples)
    head = recs[0] if recs else {}
    if not head.get("cgroup"):
        print("memory: the pod's cgroup was not found in the node")
        return 0
    rows = [r for r in recs if isinstance(r.get("current"), int)]
    before = [r for r in rows if r["t"] < args.start]
    window = [r for r in rows if args.start <= r["t"] <= args.end]
    after = [r for r in rows if r["t"] > args.end]
    idle = before[-1]["current"] if before else None
    top = max((r["current"] for r in window), default=None)

    def last(rs: list[dict[str, Any]], key: str) -> Any:
        return next((r[key] for r in reversed(rs) if isinstance(r.get(key), int)), None)

    peak0, peak1 = last(before, "peak"), last(after or window, "peak")
    kill0, kill1 = last(before, "oom_kill"), last(after or window, "oom_kill")
    limit = last(rows, "max")
    line = (
        f"idle {mib(idle)}, max sampled {mib(top)} ({len(window)} samples), memory.peak "
        f"{mib(peak0)} -> {mib(peak1)}, limit {mib(limit)}, oom_kill {kill0} -> {kill1}"
    )
    if args.suggest:
        # memory.peak 는 파드가 뜬 뒤의 최고치다. 이 창에서 올랐으면 그것이 이 스크립트의 최고치다
        peak = peak1 if peak0 is not None and peak1 is not None and peak1 > peak0 else top
        if peak is not None:
            line += (
                f"; default candidate max({FLOOR_MIB} MiB, 2 x {mib(peak)}) = "
                f"{max(FLOOR_MIB, round(2 * peak / MIB))} MiB"
            )
    print(line)
    return 0


# ---------- others ----------


def others(args: argparse.Namespace) -> int:
    recs = sorted(jsonl(args.loop), key=lambda r: r["t"])
    during = [r for r in recs if args.start <= r["t"] <= args.end]
    failed = [r for r in during if not r.get("ok")]
    line = f"{len(during)} calls by another session, {len(failed)} failed"
    if failed:
        kinds = Counter(
            f"init={r.get('init')} call={r.get('call')} "
            + str(r.get("err") or r.get("detail") or "")[:60]
            for r in failed
        )
        first, last = failed[0]["t"] - args.start, failed[-1]["t"] - args.start
        back = next(
            (r["t"] - args.start for r in recs if r["t"] > failed[-1]["t"] and r.get("ok")), None
        )
        line += f" from +{first:.1f}s to +{last:.1f}s"
        line += f", next success +{back:.1f}s" if back is not None else ", no success after"
        line += "; " + "; ".join(f"{n} x {k}" for k, n in kinds.most_common(4))
    print(line)
    return 0


# ---------- main ----------


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("listing")
    li.add_argument("--url", required=True)
    li.add_argument("--ip")
    li.add_argument("--mode", choices=["auto", "legacy"], default="auto")
    li.add_argument("--out", required=True)
    li.set_defaults(fn=listing)

    sc = sub.add_parser("script")
    sc.add_argument("--url", required=True)
    sc.add_argument("--ip")
    sc.add_argument("--code", required=True)
    sc.add_argument("--label", required=True)
    sc.add_argument("--out", required=True)
    sc.add_argument("--timeout", type=float, default=90.0)
    sc.set_defaults(fn=script)

    rd = sub.add_parser("ready")
    rd.add_argument("--url", required=True)
    rd.add_argument("--ip")
    rd.add_argument("--since", type=float, required=True)
    rd.add_argument("--timeout", type=float, default=300.0)
    rd.set_defaults(fn=ready)

    cm = sub.add_parser("codemode")
    cm.add_argument("configmaps")
    cm.add_argument("--owner", required=True)
    cm.set_defaults(fn=codemode)

    ro = sub.add_parser("rollup")
    ro.add_argument("before")
    ro.add_argument("after")
    ro.set_defaults(fn=rollup)

    se = sub.add_parser("settled")
    se.add_argument("file")
    se.add_argument("--after", type=float, required=True)
    se.set_defaults(fn=settled)

    sa = sub.add_parser("sample")
    sa.add_argument("--node", required=True)
    sa.add_argument("--uid", required=True)
    sa.add_argument("--out", required=True)
    sa.add_argument("--stop", required=True)
    sa.add_argument("--interval", type=float, default=0.25)
    sa.set_defaults(fn=sample)

    me = sub.add_parser("memory")
    me.add_argument("--samples", required=True)
    me.add_argument("--start", type=float, required=True)
    me.add_argument("--end", type=float, required=True)
    me.add_argument("--suggest", action="store_true")
    me.set_defaults(fn=memory)

    ot = sub.add_parser("others")
    ot.add_argument("--loop", required=True)
    ot.add_argument("--start", type=float, required=True)
    ot.add_argument("--end", type=float, required=True)
    ot.set_defaults(fn=others)

    args = p.parse_args(argv[1:])
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
