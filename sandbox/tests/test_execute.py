"""명령: 무리 죽이기(끝, 시간 초과, 끊김, /cancel), 함께 도는 명령, 쓸기, 거둠, 고정 env
(P40 3절)."""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import LINUX, Sandbox, gone, start


def test_output_exit_code_and_workspace_cwd(sandbox: Sandbox) -> None:
    result = sandbox.execute("pwd; echo err >&2; exit 3")
    assert result["exit_code"] == 3
    assert result["output"].splitlines()[0].endswith("/workspace")
    assert "err" in result["output"]
    assert (result["truncated"], result["timed_out"], result["cancelled"]) == (False, False, False)


def test_output_is_capped_at_one_mebibyte(sandbox: Sandbox) -> None:
    result = sandbox.execute("head -c 3000000 /dev/zero | tr '\\0' a")
    assert result["truncated"] is True
    assert len(result["output"]) == 1 << 20


def test_the_command_env_is_fixed_and_holds_no_token(sandbox: Sandbox) -> None:
    out = sandbox.execute("env")["output"]
    names = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    # sh 가 스스로 두는 PWD·SHLVL·_ 밖에는 고정 env 뿐이다
    assert names - {"PWD", "SHLVL", "_", "OLDPWD"} == {
        "PATH",
        "LANG",
        "HOME",
        "MPLCONFIGDIR",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
    }
    token_hash = __import__("hashlib").sha256(sandbox.token.encode()).hexdigest()
    assert "SANDBOX_" not in out and token_hash not in out
    home = sandbox.root / "tmp" / "home"
    assert f"HOME={home}" in out and (home / ".config" / "matplotlib").is_dir()


def test_the_group_dies_when_the_command_ends(sandbox: Sandbox) -> None:
    result = sandbox.execute("sleep 600 & echo $!")
    assert result["exit_code"] == 0
    assert gone(int(result["output"].strip()))


def test_a_timeout_kills_the_group(sandbox: Sandbox) -> None:
    started = time.monotonic()
    result = sandbox.execute("echo $$ > pid; sleep 600 & sleep 600", timeout=1)
    assert time.monotonic() - started < 6
    assert result["timed_out"] is True and result["exit_code"] == 137
    assert gone(int((sandbox.workspace / "pid").read_text()))


def test_the_timeout_is_clamped(sandbox: Sandbox) -> None:
    started = time.monotonic()
    result = sandbox.execute("sleep 5", timeout=0.01)
    assert result["timed_out"] is True
    assert 0.9 < time.monotonic() - started < 4
    status, _ = sandbox.call("/execute", {"command": "true", "timeout": "soon"})
    assert status == 400


def test_a_disconnect_kills_the_command(sandbox: Sandbox) -> None:
    body = json.dumps({"command": "echo $$ > pid; exec sleep 600", "timeout": 120}).encode()
    sock = socket.create_connection(("127.0.0.1", sandbox.port))
    sock.sendall(
        b"POST /execute HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
        + sandbox.token.encode()
        + f"\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    pid_file = sandbox.workspace / "pid"
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    sock.close()
    assert gone(int(pid_file.read_text()), timeout=5)


def test_cancel_stops_running_commands(sandbox: Sandbox) -> None:
    results: list[dict] = []
    worker = threading.Thread(
        target=lambda: results.append(sandbox.execute("sleep 600", timeout=120))
    )
    worker.start()
    time.sleep(0.5)
    status, body = sandbox.call("/cancel", {})
    worker.join(timeout=10)
    assert (status, body) == (200, {"cancelled": 1})
    assert results and results[0]["cancelled"] is True


def test_a_short_call_leaves_a_long_command_running(sandbox: Sandbox) -> None:
    results: list[dict] = []
    long = threading.Thread(
        target=lambda: results.append(sandbox.execute("sleep 1.5; echo done > long.txt"))
    )
    long.start()
    time.sleep(0.3)
    (sandbox.workspace / "a.txt").write_text("hello")
    assert sandbox.execute("cat a.txt")["output"] == "hello"
    long.join(timeout=10)
    assert results[0]["exit_code"] == 0
    assert (sandbox.workspace / "long.txt").read_text() == "done\n"


def test_two_concurrent_commands_finish_with_their_own_status(sandbox: Sandbox) -> None:
    results: dict[str, dict] = {}

    def run(name: str, command: str) -> None:
        results[name] = sandbox.execute(command)

    threads = [
        threading.Thread(target=run, args=("a", "sleep 0.5; echo a; exit 4")),
        threading.Thread(target=run, args=("b", "sleep 0.7; echo b; exit 5")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert (results["a"]["output"], results["a"]["exit_code"]) == ("a\n", 4)
    assert (results["b"]["output"], results["b"]["exit_code"]) == ("b\n", 5)


def test_a_command_started_during_a_sweep_runs(tmp_path: Path, blocked_targets: str) -> None:
    box = start(tmp_path, targets=blocked_targets, extra_args=("--sweep-pause", "1.0"))
    try:
        first = threading.Thread(target=box.execute, args=("true",))
        first.start()
        # 첫 명령이 끝나 쓸기가 자물쇠를 쥐고 쉬는 동안 다음 명령이 들어온다
        time.sleep(0.4)
        result = box.execute("sleep 0.3; echo survived")
        first.join(timeout=10)
        assert result["output"] == "survived\n" and result["exit_code"] == 0
    finally:
        box.close()


@pytest.mark.skipif(not LINUX, reason="떼어 낸 프로세스를 서버로 돌리는 subreaper 는 리눅스다")
def test_detached_processes_do_not_outlive_their_commands(sandbox: Sandbox) -> None:
    result = sandbox.execute("setsid sleep 600 > /dev/null 2>&1 < /dev/null & echo $!")
    assert gone(int(result["output"].strip()))


@pytest.mark.skipif(not LINUX, reason="RLIMIT_NPROC 와 고아 거둠은 리눅스에서 잰다")
def test_exited_detached_processes_leave_room_for_a_new_command(sandbox: Sandbox) -> None:
    # 300 개의 고아가 끝난다. 거두지 않으면 좀비가 RLIMIT_NPROC 256 을 채워 다음 fork 가 막힌다
    sandbox.execute("i=0; while [ $i -lt 300 ]; do (true &); i=$((i+1)); done", timeout=120)
    result = sandbox.execute("echo ok | cat; sleep 0 & wait")
    assert result["output"] == "ok\n" and result["exit_code"] == 0


@pytest.mark.skipif(not LINUX, reason="RLIMIT_NPROC 은 리눅스에서 잰다")
def test_the_process_limit_holds(sandbox: Sandbox) -> None:
    # 명령을 돌리는 /bin/sh 는 리눅스에서 dash 라 ulimit -u 가 없다. 커널이 보는 한도를 읽는다
    limits = {}
    for line in sandbox.execute("cat /proc/self/limits")["output"].splitlines()[1:]:
        name, soft, hard = re.match(r"(.+?)\s{2,}(\S+)\s+(\S+)", line).groups()
        limits[name] = (soft, hard)
    assert limits["Max processes"] == ("256", "256")
    assert limits["Max file size"] == (str(128 << 20), str(128 << 20))
    assert limits["Max core file size"] == ("0", "0")


def test_a_file_cannot_outgrow_the_size_limit(sandbox: Sandbox) -> None:
    """작업 공간 밖의 쓸 수 있는 파일(노드 디스크의 /dev/termination-log)을 이 한도가 막는다."""
    result = sandbox.execute("head -c 134217729 /dev/zero > big; wc -c < big", timeout=60)
    # 한도를 넘는 쓰기는 SIGXFSZ 로 끝나고 셸이 그것을 알린다. 마지막 줄이 남은 파일의 크기다
    assert int(result["output"].split()[-1]) == 128 << 20


def test_sigint_does_not_stop_the_server_and_commands_get_it_back(sandbox: Sandbox) -> None:
    """같은 uid 의 명령이 서버에 SIGINT 를 보내도 서버는 산다. 명령은 SIGINT 를 기본으로 받는다."""
    pid = sandbox.proc.pid
    sandbox.execute(f"kill -INT {pid}")
    assert sandbox.proc.poll() is None
    probe = "import signal; print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)"
    result = sandbox.execute(f"python3 -c '{probe}'")
    assert result["output"].strip() == "False", result


def test_a_refused_spawn_is_reported(sandbox: Sandbox) -> None:
    status, body = sandbox.call("/execute", {"command": ""})
    assert status == 400
    status, body = sandbox.call("/execute", {"command": "a\0b"})
    assert status == 400


def test_the_server_keeps_answering_while_a_command_runs(sandbox: Sandbox) -> None:
    worker = threading.Thread(target=sandbox.execute, args=("sleep 1",))
    worker.start()
    conn = http.client.HTTPConnection("127.0.0.1", sandbox.port, timeout=2)
    conn.request("GET", "/healthz")
    assert conn.getresponse().status == 200
    conn.close()
    worker.join(timeout=10)
