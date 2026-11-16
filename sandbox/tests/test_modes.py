"""같은 이미지의 다른 모드(F3 이 쓴다)와 서버 굳히기 (P40 3절, 8절)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import BOOT, LINUX, USERNS_MAP, Sandbox, closed_port, start, targets_env


def run_mode(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", BOOT, *args],
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_control_prints_one_line_per_target() -> None:
    shut, open_sock = closed_port(), socket.socket()
    open_sock.bind(("127.0.0.1", 0))
    open_sock.listen(4)
    ports = [shut.getsockname()[1], open_sock.getsockname()[1]]
    targets = targets_env(
        *({"kind": "tcp", "host": "127.0.0.1", "port": port, "timeout": 0.5} for port in ports)
    )
    done = run_mode("control", env={"SANDBOX_TARGETS": targets})
    shut.close()
    open_sock.close()
    assert done.returncode == 0, done.stderr
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    assert [(line["port"], line["reached"]) for line in lines] == [
        (ports[0], False),
        (ports[1], True),
    ]
    assert all({"kind", "host", "port", "reached", "seconds"} <= set(line) for line in lines)
    assert run_mode("control").returncode == 86


def test_probe_exits_like_the_first_check(tmp_path: Path) -> None:
    uid_map = tmp_path / "uid_map"
    uid_map.write_text(USERNS_MAP)
    shut = closed_port()
    targets = targets_env(
        {"kind": "tcp", "host": "127.0.0.1", "port": shut.getsockname()[1], "timeout": 0.5}
    )
    args = ("--uid-map", str(uid_map), "--termination-log", str(tmp_path / "t"), "probe")
    assert run_mode(*args, env={"SANDBOX_TARGETS": targets}).returncode == 0
    shut.listen(4)
    assert run_mode(*args, env={"SANDBOX_TARGETS": targets}).returncode == 87
    shut.close()
    assert run_mode(*args).returncode == 86


def test_client_exits_0_when_connected_3_when_blocked_and_1_when_it_cannot_try() -> None:
    """플랫폼은 0 과 3 만 8080 의 증거로 읽는다. 시도하지 못한 것은 닫힌 것이 아니다."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    assert run_mode("client", "127.0.0.1", str(port)).returncode == 0
    server.close()
    assert run_mode("client", "127.0.0.1", str(port)).returncode == 3
    assert run_mode("client", "not-an-ip", str(port)).returncode == 1
    assert run_mode("client", "127.0.0.1", "70000").returncode == 1


@pytest.mark.skipif(not LINUX, reason="dumpable 은 리눅스의 prctl 이다")
def test_the_server_is_not_dumpable(sandbox: Sandbox) -> None:
    pid = sandbox.proc.pid
    # dumpable 이 꺼진 프로세스의 /proc 파일은 root 것이 되어 같은 uid 도 environ 을 읽지 못한다
    assert os.stat(f"/proc/{pid}/environ").st_uid == 0
    with pytest.raises(PermissionError):
        Path(f"/proc/{pid}/environ").read_bytes()
    out = sandbox.execute(f"cat /proc/{pid}/environ; echo rc=$?")["output"]
    assert "SANDBOX_TOKEN_SHA256" not in out and "rc=1" in out


@pytest.mark.skipif(not LINUX, reason="스레드 수는 리눅스의 /proc 에서 읽는다")
def test_the_server_has_one_thread(sandbox: Sandbox) -> None:
    sandbox.execute("true")
    sandbox.call("/files/list", {})
    status = Path(f"/proc/{sandbox.proc.pid}/status").read_text()
    assert "Threads:\t1\n" in status


def _pid_namespace_works() -> bool:
    if not LINUX or shutil.which("unshare") is None:
        return False
    done = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
        capture_output=True,
    )
    return done.returncode == 0


PID_NAMESPACE = (
    "unshare",
    "--user",
    "--map-root-user",
    "--pid",
    "--fork",
    "--mount-proc",
    "--kill-child",
)


@pytest.mark.skipif(not _pid_namespace_works(), reason="권한 없는 PID 이름공간을 만들 수 없다")
def test_as_pid_1_the_sweep_ends_every_other_process(tmp_path: Path, blocked_targets: str) -> None:
    """파드처럼 PID 1 로 띄운다. 명령이 떼어 낸 프로세스는 명령이 끝날 때 kill(-1) 로 끝난다."""
    box = start(tmp_path, targets=blocked_targets, wrapper=PID_NAMESPACE)
    try:
        assert box.proc.poll() is None, box.log.read_text()
        assert box.execute("echo $PPID")["output"] == "1\n"
        box.execute("setsid sleep 600 > /dev/null 2>&1 < /dev/null &")
        listing = box.execute("cat /proc/[0-9]*/comm")["output"].split()
        assert "sleep" not in listing
    finally:
        box.close()
