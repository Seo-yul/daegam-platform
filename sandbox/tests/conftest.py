"""샌드박스 서버 시험의 도우미. 진짜 서버를 하위 프로세스로 띄우고 HTTP 로 부른다.

서버는 `python -I` 로 돈다. 파드에서는 이미지의 .pth 가 읽기 전용 /opt/sandbox 를 가져오고,
시험은 같은 일을 고정 경로(이 저장소의 sandbox/)를 sys.path 에 넣는 짧은 시작 글로 한다. 서버 env 는
파드처럼 SANDBOX_* 로 주고, 뿌리·포트·uid map·종료 메시지 경로는 시험 전용 인자로 준다.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]
BOOT = (
    f"import sys; sys.path.insert(0, {str(SOURCE)!r}); "
    "from daegam_sandbox.__main__ import main; sys.exit(main(sys.argv[1:]))"
)
LINUX = sys.platform.startswith("linux")
# 사용자 uid 가 이미 프로세스 256 개를 넘게 돌리는 개발 머신(macOS)에서는 명령의 fork 가 막히므로
# 리눅스에서만 파드와 같은 한도를 건다
NPROC = "256" if LINUX else "0"
USERNS_MAP = "         0  395575296      65536\n"
IDENTITY_MAP = "         0          0 4294967295\n"


def closed_port() -> socket.socket:
    """듣지 않는 포트(연결하면 거절)를 쥐고 있는 소켓."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def targets_env(*targets: dict) -> str:
    return json.dumps(list(targets))


@dataclass
class Sandbox:
    proc: subprocess.Popen
    root: Path
    token: str
    port: int
    termination: Path
    log: Path
    conns: list[http.client.HTTPConnection] = field(default_factory=list)

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    def connect(self) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        self.conns.append(conn)
        return conn

    def call(
        self,
        path: str,
        body: dict | None = None,
        *,
        method: str = "POST",
        token: str | None = "",
        conn: http.client.HTTPConnection | None = None,
    ) -> tuple[int, dict]:
        conn = conn or self.connect()
        headers = {"Content-Type": "application/json"}
        token = self.token if token == "" else token
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None if method == "GET" else json.dumps(body or {}).encode()
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def execute(self, command: str, timeout: float = 30, **kw) -> dict:
        status, body = self.call("/execute", {"command": command, "timeout": timeout}, **kw)
        assert status == 200, body
        return body

    def wait_exit(self, timeout: float = 15) -> int:
        return self.proc.wait(timeout=timeout)

    def close(self) -> None:
        for conn in self.conns:
            conn.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def start(
    tmp: Path,
    *,
    targets: str | None,
    uid_map: str = USERNS_MAP,
    unchecked: bool = False,
    token_hash: str | None = None,
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
    wrapper: tuple[str, ...] = (),
    wait: bool = True,
) -> Sandbox:
    """서버를 띄운다. wait 면 듣기 시작할 때까지(또는 끝날 때까지) 기다린다."""
    root = tmp / "root"
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    uid_path = tmp / "uid_map"
    uid_path.write_text(uid_map)
    port_file = tmp / "port"
    termination = tmp / "termination-log"
    token = secrets.token_urlsafe(32)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    env["SANDBOX_TOKEN_SHA256"] = token_hash or hashlib.sha256(token.encode()).hexdigest()
    if targets is not None:
        env["SANDBOX_TARGETS"] = targets
    if unchecked:
        env["SANDBOX_UNCHECKED"] = "true"
    env.update(extra_env or {})
    args = [
        *wrapper,
        sys.executable,
        "-I",
        "-c",
        BOOT,
        "--root",
        str(root),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--port-file",
        str(port_file),
        "--uid-map",
        str(uid_path),
        "--termination-log",
        str(termination),
        "--nproc-limit",
        NPROC,
        "--font-cache",
        str(tmp / "no-font-cache"),
        *extra_args,
    ]
    # 서버 기록은 파일로 받는다. 파이프로 받으면 다 차서 서버가 멈출 수 있다
    with open(tmp / "server.log", "wb") as log:
        proc = subprocess.Popen(args, env=env, cwd=root / "workspace", stderr=log)
    box = Sandbox(proc, root, token, 0, termination, tmp / "server.log")
    if not wait:
        return box
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if port_file.exists() and port_file.read_text():
            box.port = int(port_file.read_text())
            return box
        if proc.poll() is not None:
            return box
        time.sleep(0.02)
    box.close()
    raise AssertionError("server did not start")


@pytest.fixture
def blocked_targets() -> Iterator[str]:
    """거절되는 TCP 대상 하나. 서버가 도는 동안 포트를 쥐고 있는다."""
    sock = closed_port()
    yield targets_env(
        {"kind": "tcp", "host": "127.0.0.1", "port": sock.getsockname()[1], "timeout": 0.5}
    )
    sock.close()


@pytest.fixture
def sandbox(tmp_path: Path, blocked_targets: str) -> Iterator[Sandbox]:
    box = start(tmp_path, targets=blocked_targets)
    assert box.proc.poll() is None, box.log.read_text()
    yield box
    box.close()


def gone(pid: int, timeout: float = 5.0) -> bool:
    """pid 가 timeout 안에 없어지면(또는 좀비가 되면) 참이다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if LINUX:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except (OSError, IndexError):
                return True
            if state == "Z":
                return True
        time.sleep(0.05)
    return False
