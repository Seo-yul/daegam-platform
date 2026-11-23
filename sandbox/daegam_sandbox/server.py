"""샌드박스 서버 (P40 3절).

- `POST /proof {nonce}`: `HMAC(SHA256(token), nonce)` 를 답한다. 런타임은 토큰이나 데이터를 보내기
  전에 새 연결마다 증명을 받는다.
- `POST /execute {command, timeout}`: 명령 하나(procs.py). 여럿이 함께 돌 수 있다.
- `POST /cancel`: 도는 명령을 모두 멈추고 쓸어 낸다.
- `POST /files/upload`·`/files/download`·`/files/list`: 작업 공간의 파일(files.py).
- `GET /healthz`: 준비 여부와 서버 API 판. kubelet 의 readiness 프로브다.

`/proof` 와 `/healthz` 밖의 호출은 `Authorization: Bearer <token>` 이 있어야 하고, 토큰의 sha256 을
파드 env 의 값과 일정 시간 비교로 맞댄다. 첫 자기 점검을 통과하기 전에는 듣지 않는다. 뒤 점검은
5분마다 돌고, 실패하면 명령을 거절하고 88 로 끝난다. SIGTERM 이면 바로 0 으로 끝나고, PID 1 이
끝나면 컨테이너의 모든 프로세스가 함께 끝난다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from daegam_sandbox import API_VERSION
from daegam_sandbox.config import Layout, Target
from daegam_sandbox.files import BadRequest, download, list_files, upload
from daegam_sandbox.http import MAX_LINE, HttpError, Request, read_request, response
from daegam_sandbox.paths import Resolver
from daegam_sandbox.procs import Refused, Runner, SpawnFailed, clamp_timeout
from daegam_sandbox.selfcheck import (
    FIRST_SETTLE,
    Verdict,
    exit_code,
    run_check,
    run_first_check,
    write_termination,
)

CHECK_INTERVAL = 300.0
MAX_CONNECTIONS = 64
_NONCE = re.compile(r"^(?:[0-9a-fA-F]{2}){16,64}$")

# 경로 -> (메서드, 본문 한도, 토큰이 있어야 하는가)
ROUTES: dict[str, tuple[str, int, bool]] = {
    "/healthz": ("GET", 4 * 1024, False),
    "/proof": ("POST", 4 * 1024, False),
    "/execute": ("POST", 2 * 1024 * 1024, True),
    "/cancel": ("POST", 4 * 1024, True),
    "/files/upload": ("POST", 48 * 1024 * 1024, True),
    "/files/download": ("POST", 64 * 1024, True),
    "/files/list": ("POST", 4 * 1024, True),
}

log = logging.getLogger("daegam_sandbox")


@dataclass
class Settings:
    layout: Layout
    token_hash: bytes
    targets: tuple[Target, ...] | None
    # targets 가 None 인 까닭(env 가 틀렸다)
    targets_error: str
    unchecked: bool
    uid_map: Path
    termination_log: Path
    host: str
    port: int
    port_file: Path | None = None
    check_interval: float = CHECK_INTERVAL
    first_settle: float = FIRST_SETTLE
    nproc_limit: int = 256
    font_cache: Path | None = None
    sweep_pause: float = 0.0


def _body_limit(path: str) -> int:
    # 모르는 경로도 작은 본문은 읽고 404 로 답한다
    return ROUTES.get(path, ("", 4 * 1024, False))[1]


def listening_socket(host: str, port: int) -> socket.socket:
    """듣는 소켓을 직접 만든다. asyncio 에 주소를 넘기면 이름 풀이가 스레드 풀로 갈 수 있다.
    빈 host 면 IPv6 이중 스택(`::`)이고, 커널에 IPv6 가 없으면 `0.0.0.0` 이다."""
    if not host:
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            host = "::"
        except OSError:
            sock, host = socket.socket(socket.AF_INET, socket.SOCK_STREAM), "0.0.0.0"
    else:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


class Server:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.resolver = Resolver(settings.layout)
        self.runner = Runner(
            settings.layout,
            nproc_limit=settings.nproc_limit,
            font_cache=settings.font_cache,
            sweep_pause=settings.sweep_pause,
        )
        self.ready = False
        self.connections = 0

    # ---- 끝내기 ----

    def exit(self, code: int) -> None:
        """끝 코드로 바로 끝난다. PID 1 이 아니면(시험) 자손을 먼저 죽인다."""
        self.runner.kill_others()
        logging.shutdown()
        os._exit(code)

    def fail(self, verdict: Verdict, check: str, code: int) -> None:
        write_termination(self.s.termination_log, verdict.message(check))
        log.error("self-check %s failed (%s): %s", check, verdict.result, verdict.reason)
        self.exit(code)

    async def check(self, *, first: bool) -> bool:
        """점검 한 번. 실패면 개발용 점검 생략이 아닌 한 끝 코드로 끝난다. 통과면 참이다."""
        name = "first" if first else "later"
        if first:
            # 개발용 점검 생략은 결과로 아무것도 막지 않으므로 다시 돌며 기다리지 않는다
            settle = 0.0 if self.s.unchecked else self.s.first_settle
            verdict = await run_first_check(
                self.s.uid_map, self.s.targets, self.s.targets_error, settle=settle
            )
        else:
            verdict = await run_check(self.s.uid_map, self.s.targets, self.s.targets_error)
        code = exit_code(verdict, first=first)
        if code is None:
            if verdict.rounds > 1:
                log.warning("self-check first passed on round %d", verdict.rounds)
            return True
        if self.s.unchecked:
            # 개발용 점검 생략(P40 8절): 끝내지 않고 알리기만 한다
            write_termination(self.s.termination_log, verdict.message(name))
            log.warning(
                "self-check %s failed (%s) but unchecked: %s", name, verdict.result, verdict.reason
            )
            return False
        if not first:
            self.runner.refusing = True
            self.ready = False
        self.fail(verdict, name, code)
        return False

    async def periodic(self) -> None:
        while True:
            await asyncio.sleep(self.s.check_interval)
            await self.check(first=False)

    # ---- 듣기 ----

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.exit, 0)
        self.runner.install(loop)
        await self.check(first=True)
        sock = listening_socket(self.s.host, self.s.port)
        server = await asyncio.start_server(self.handle, sock=sock, limit=MAX_LINE)
        self.ready = True
        if self.s.port_file is not None:
            self.s.port_file.write_text(str(sock.getsockname()[1]))
        log.info("listening on %s (unchecked=%s)", sock.getsockname()[:2], self.s.unchecked)
        checks = asyncio.ensure_future(self.periodic())
        try:
            await server.serve_forever()
        finally:
            checks.cancel()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            if self.connections > MAX_CONNECTIONS:
                writer.write(response(503, {"error": "too_many_connections"}, keep_alive=False))
                await writer.drain()
                return
            while True:
                try:
                    req = await read_request(reader, _body_limit)
                except HttpError as e:
                    writer.write(response(e.status, {"error": e.code}, keep_alive=False))
                    await writer.drain()
                    return
                if req is None:
                    return
                status, payload, closing = await self.dispatch(req, reader)
                keep = req.keep_alive and not closing
                writer.write(response(status, payload, keep_alive=keep))
                await writer.drain()
                if not keep:
                    return
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            return
        finally:
            self.connections -= 1
            writer.close()

    # ---- 호출 ----

    def authorized(self, req: Request) -> bool:
        scheme, _, token = req.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        got = hashlib.sha256(token.strip().encode("utf-8")).digest()
        return hmac.compare_digest(got, self.s.token_hash)

    async def dispatch(self, req: Request, reader: asyncio.StreamReader) -> tuple[int, dict, bool]:
        """(상태, 본문, 답한 뒤 연결을 닫는가)."""
        route = ROUTES.get(req.path)
        if route is None:
            return 404, {"error": "not_found"}, False
        method, _, needs_token = route
        if req.method != method:
            return 405, {"error": "method_not_allowed"}, False
        if req.path == "/healthz":
            status = 200 if self.ready else 503
            body = {"ready": self.ready, "api": API_VERSION, "unchecked": self.s.unchecked}
            return status, body, False
        if needs_token and not self.authorized(req):
            return 401, {"error": "unauthorized"}, False
        try:
            body = json.loads(req.body or b"{}")
        except ValueError:
            return 400, {"error": "bad_json"}, False
        if not isinstance(body, dict):
            return 400, {"error": "bad_json"}, False
        try:
            if req.path == "/proof":
                return 200, self.proof(body), False
            if not self.ready:
                return 503, {"error": "refusing"}, True
            if req.path == "/execute":
                return await self.execute(body, reader)
            if req.path == "/cancel":
                return 200, {"cancelled": await self.runner.cancel()}, False
            if req.path == "/files/upload":
                return 200, upload(self.resolver, body), False
            if req.path == "/files/download":
                return 200, download(self.resolver, body), False
            return 200, list_files(self.resolver, body), False
        except (BadRequest, ValueError) as e:
            return 400, {"error": "bad_request", "detail": str(e)}, False
        except Exception:
            # 서버의 결함이다. 그 요청만 실패시키고 연결은 닫는다
            log.exception("call %s failed", req.path)
            return 500, {"error": "internal"}, True

    def proof(self, body: dict) -> dict:
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not _NONCE.match(nonce):
            raise BadRequest("nonce 는 16~64 바이트의 16진 글자다")
        mac = hmac.new(self.s.token_hash, bytes.fromhex(nonce), hashlib.sha256)
        return {"proof": mac.hexdigest()}

    async def execute(self, body: dict, reader: asyncio.StreamReader) -> tuple[int, dict, bool]:
        command = body.get("command")
        if not isinstance(command, str) or not command or "\0" in command:
            raise BadRequest("command 는 NUL 없는 비지 않은 글자다")
        timeout = clamp_timeout(body.get("timeout"))
        # 명령이 도는 동안 연결이 끊기면(읽기가 빈 바이트로 끝나면) 명령을 멈춘다
        watch = asyncio.ensure_future(reader.read(1))
        try:
            result = await self.runner.run(command, timeout, watch)
        except Refused:
            return 503, {"error": "refusing"}, True
        except SpawnFailed as e:
            return 503, {"error": "spawn_failed", "detail": str(e)}, False
        finally:
            closing = watch.done()
            if not watch.done():
                watch.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                await watch
        # 읽기가 끝났으면 끊겼거나 다음 요청의 첫 바이트를 먹었으므로 답한 뒤 닫는다
        return 200, result, closing


def configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
