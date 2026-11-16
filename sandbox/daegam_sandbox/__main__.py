"""`python3 -I -m daegam_sandbox [모드]` (P40 3절, 8절).

파드는 인자를 주지 않는다. 그러면 서버다. 같은 이미지의 다른 모드는 F3 의 클러스터 점검이 파드 args
로 고른다.

- (없음): 서버. SIGTERM 이면 0, 자기 점검이 실패하면 86·87·88 이다.
- `control`: 양성 대조. `SANDBOX_TARGETS` 의 대상마다 JSON 한 줄(kind, host, port, reached,
  seconds)을 찍고 0 이다. 돌지 못하면 86 이다.
- `probe`: 사용자 코드 없는 프로브. 첫 자기 점검만 돌고 통과면 0, 실패면 87, 돌지 못하면 86 이다.
- `client <host> <port>`: Agent 가 아닌 파드에서 8080 이 닫혔는지 본다. 2초 안에 연결되면 0,
  연결을 시도했고 거절이나 시간 초과로 막혔으면 3(`EXIT_CLIENT_BLOCKED`), 시도하지 못했으면 1 이다.
  플랫폼은 0 과 3 만 증거로 읽고 나머지(1, OOM 같은)는 점검을 하지 못한 것으로 본다.

`--root` 같은 인자는 시험만 쓴다. 파드에서 뿌리는 `/` 이고 작업 공간은 `/workspace` 다.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import signal
import sys
from pathlib import Path

from daegam_sandbox.config import (
    TARGETS_ENV,
    TOKEN_ENV,
    UNCHECKED_ENV,
    ConfigError,
    Layout,
    parse_targets,
    parse_token_hash,
    take_server_env,
)
from daegam_sandbox.hardening import harden
from daegam_sandbox.selfcheck import (
    ERROR,
    EXIT_COULD_NOT_RUN,
    EXIT_FIRST_FAILED,
    Verdict,
    run_check,
    write_termination,
)

# 클라이언트 모드가 연결을 시도했고 막혔다(거절, 시간 초과, 닿을 수 없음). 시도하지 못한 1 과
# argparse 의 2 와 가른다
EXIT_CLIENT_BLOCKED = 3
# 이미지가 미리 만든 matplotlib 글꼴 캐시(읽기 전용)
FONT_CACHE = Path("/opt/sandbox/mplconfig")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daegam_sandbox")
    p.add_argument("--root", type=Path, default=Path("/"))
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--port-file", type=Path)
    p.add_argument("--uid-map", type=Path, default=Path("/proc/self/uid_map"))
    p.add_argument("--termination-log", type=Path, default=Path("/dev/termination-log"))
    p.add_argument("--check-interval", type=float, default=300.0)
    p.add_argument("--nproc-limit", type=int, default=256)
    p.add_argument("--font-cache", type=Path, default=FONT_CACHE)
    p.add_argument("--sweep-pause", type=float, default=0.0)
    sub = p.add_subparsers(dest="mode")
    sub.add_parser("control")
    sub.add_parser("probe")
    client = sub.add_parser("client")
    client.add_argument("host")
    client.add_argument("port", type=int)
    return p


def _serve(args: argparse.Namespace) -> int:
    from daegam_sandbox.server import Server, Settings, configure_logging

    configure_logging()
    try:
        harden()
    except OSError as e:
        write_termination(args.termination_log, Verdict(ERROR, f"harden: {e}").message("first"))
        print(f"daegam_sandbox: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    # 작업 디렉터리는 읽기 전용 이미지 안이다. 쓰는 마운트에서 아무것도 싣지 않는다
    os.chdir("/")
    env = take_server_env()
    unchecked = env.get(UNCHECKED_ENV, "").lower() == "true"
    try:
        token_hash = parse_token_hash(env.get(TOKEN_ENV))
    except ConfigError as e:
        write_termination(args.termination_log, Verdict(ERROR, str(e)).message("first"))
        print(f"daegam_sandbox: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    try:
        targets, targets_error = parse_targets(env.get(TARGETS_ENV)), ""
    except ConfigError as e:
        targets, targets_error = None, str(e)
    settings = Settings(
        layout=Layout(args.root),
        token_hash=token_hash,
        targets=targets,
        targets_error=targets_error,
        unchecked=unchecked,
        uid_map=args.uid_map,
        termination_log=args.termination_log,
        host=args.host,
        port=args.port,
        port_file=args.port_file,
        check_interval=args.check_interval,
        nproc_limit=args.nproc_limit,
        font_cache=args.font_cache,
        sweep_pause=args.sweep_pause,
    )
    asyncio.run(Server(settings).run())
    return 0


def _control() -> int:
    from daegam_sandbox.probes import probe_all

    try:
        targets = parse_targets(os.environ.get(TARGETS_ENV))
    except ConfigError as e:
        print(f"daegam_sandbox control: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    for outcome in asyncio.run(probe_all(targets)):
        print(json.dumps(outcome.as_dict()), flush=True)
    return 0


def _probe(args: argparse.Namespace) -> int:
    try:
        targets, reason = parse_targets(os.environ.get(TARGETS_ENV)), ""
    except ConfigError as e:
        targets, reason = None, str(e)
    verdict = asyncio.run(run_check(args.uid_map, targets, reason))
    write_termination(args.termination_log, verdict.message("probe"))
    print(verdict.message("probe"), flush=True)
    if verdict.passed:
        return 0
    return EXIT_COULD_NOT_RUN if verdict.result == ERROR else EXIT_FIRST_FAILED


def _client(args: argparse.Namespace) -> int:
    from daegam_sandbox.config import DEFAULT_TARGET_TIMEOUT, Target
    from daegam_sandbox.probes import probe

    try:
        host = str(ipaddress.ip_address(args.host))
        if not 0 < args.port < 65536:
            raise ValueError(f"port {args.port} out of range")
        target = Target(kind="tcp", host=host, port=args.port, timeout=DEFAULT_TARGET_TIMEOUT)
        outcome = asyncio.run(probe(target))
    except (OSError, ValueError) as e:
        print(f"daegam_sandbox client: {e}", file=sys.stderr)
        return 1
    if outcome.reached:
        return 0
    if outcome.error:
        print(f"daegam_sandbox client: {outcome.note}", file=sys.stderr)
        return 1
    return EXIT_CLIENT_BLOCKED


def main(argv: list[str] | None = None) -> int:
    # PID 1 은 처리기가 없는 신호를 무시한다. asyncio 가 처리기를 걸기 전에 온 SIGTERM 에도 바로
    # 끝난다. SIGINT 는 무시한다. 파이썬은 SIGINT 에 처리기(KeyboardInterrupt)를 걸어 두므로
    # 같은 uid 의 명령이 `kill -INT 1` 로 서버를 쓰러뜨릴 수 있기 때문이다. asyncio.run 은
    # 무시된 SIGINT 를 덮지 않는다
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    args = _parser().parse_args(argv)
    if args.mode == "control":
        return _control()
    if args.mode == "probe":
        return _probe(args)
    if args.mode == "client":
        return _client(args)
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())
