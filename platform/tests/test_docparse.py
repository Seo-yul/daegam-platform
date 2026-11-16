"""문서 파싱 서비스 `daegam-docparse` 와 그것을 부르는 플랫폼 클라이언트 (D61).

서버: 토큰 없는 요청 401, 준비 바이트 전에는 문서를 쓰지 않는 것, 시간을 넘긴 하위 프로세스가 그룹째
죽는 것(테스트 훅으로 멈춘 추출), 하위 프로세스가 죽어도 서버는 사는 것, 동시 둘 뒤 429 와 인덱서가
한 자리만 쓰는 것, 하위 프로세스 env 에 비밀이 없는 것. 클라이언트: 텍스트 형식은 서비스를 부르지
않는 것, 첨부의 글은 서비스 답도 상한에서 자르는 것, 오류 코드와 다시 부를 만한 실패의 구분.

Linux 에서만 도는 경우는 가두기 자체다. 자손을 남기려는 fork 가 실패하고 끝난 뒤 그 그룹에
프로세스가 없는 것, 서버와 하위 프로세스의 dumpable 이 0 인 것, 같은 UID 의 다른 프로세스가 서버의
`environ` 을 읽지 못하는 것, `selfcheck` 의 다섯 칸이 모두 참인 것이다. 다섯째 칸이 가둔 하위
프로세스가 이웃의 표준 입출력(소켓)을 `/proc` 로 열지 못하는 것이다. 개발 호스트(macOS)에는
`/proc` 와 `prctl` 이 없고 루트는 `RLIMIT_NPROC` 을 받지 않으므로 건너뛴다. 실제 판정은 e2e 가 파드
안에서 부른 `selfcheck` 가 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

from daegam.documents import TOKEN_ENV, sandbox, worker
from daegam.documents.formats import MAX_DOCUMENT_BYTES
from daegam.documents.result import INDEX, MAX_UPLOAD_CHARS, UPLOAD, ExtractError
from daegam.documents.sandbox import (
    Worker,
    group_alive,
    kill_group,
    run_worker,
    send_request,
    spawn,
)
from daegam.documents.server import Slots
from daegam.services.docparse import DocparseClient, DocparseError
from tests.helpers.auth import bearer
from tests.helpers.documents import TOKEN, docparse_app, make_docx, w_p
from tests.helpers.http import free_port

LINUX_JAIL = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() == 0,
    reason="가두기는 Linux 의 루트 아닌 프로세스에서만 선다(e2e 의 selfcheck 가 판정한다)",
)
AUTH = bearer(TOKEN)
# 멈춘 추출을 끊는 시간(초). pid 는 띄우는 자리에서 받으므로 훅이 문서를 받기 전에 끊겨도 판정이
# 선다. 그래서 훅이 뜨는 시간(0.1초 안팎)만 넘기면 되고 테스트는 이만큼만 기다린다
STUCK_TIMEOUT = 0.5


def _hook(script: str, *args: str) -> tuple[str, ...]:
    """하위 프로세스 자리에 넣는 테스트 훅. 진짜 worker 처럼 `-I` 로 뜬다. 표준 입력은 worker 와
    같은 `read_document` 로 읽는다. 막히는 read 로 읽으면 macOS 에서 드물게 30초를 다 채운다."""
    return (sys.executable, "-I", "-c", script, *args)


# 준비 바이트를 내기 전에 표준 입력에 무엇이 왔는지 보고, 받은 바이트 수와 함께 글로 답한다
EARLY_HOOK = """
import json, select, sys, time
from daegam.documents.sandbox import READY, read_document
time.sleep(0.3)
early = bool(select.select([sys.stdin], [], [], 0)[0])
sys.stdout.buffer.write(READY); sys.stdout.buffer.flush()
line, data = read_document(sys.stdin.buffer, 1 << 26).split(b"\\n", 1)
request = json.loads(line)
text = json.dumps({"early": early, "bytes": len(data), "task": request["task"]})
sys.stdout.buffer.write(json.dumps({"ok": True, "result": {"text": text}}).encode())
"""
# 문서를 받은 뒤 멈춘다
HANG_HOOK = """
import sys, time
from daegam.documents.sandbox import READY, read_document
sys.stdout.buffer.write(READY); sys.stdout.buffer.flush()
read_document(sys.stdin.buffer, 1 << 26)
time.sleep(600)
"""
# 문서를 받은 뒤 스스로 SIGKILL 로 죽는다. 메모리 한도로 죽은 하위 프로세스와 같은 꼴이다
DIE_HOOK = """
import os, signal, sys
from daegam.documents.sandbox import READY, read_document
sys.stdout.buffer.write(READY); sys.stdout.buffer.flush()
read_document(sys.stdin.buffer, 1 << 26)
os.kill(os.getpid(), signal.SIGKILL)
"""
# 표준 입력과 출력이 소켓인지 글로 답한다
STDIO_HOOK = """
import json, os, stat, sys
from daegam.documents.sandbox import READY, read_document
sys.stdout.buffer.write(READY); sys.stdout.buffer.flush()
read_document(sys.stdin.buffer, 1 << 26)
kinds = json.dumps([stat.S_ISSOCK(os.fstat(fd).st_mode) for fd in (0, 1)])
sys.stdout.buffer.write(json.dumps({"ok": True, "result": {"text": kinds}}).encode())
"""
# 가둔 뒤 1GiB 를 잡으려 한다
GREEDY_HOOK = """
import sys
from daegam.documents.sandbox import READY, confine, read_document
confine()
sys.stdout.buffer.write(READY); sys.stdout.buffer.flush()
read_document(sys.stdin.buffer, 1 << 26)
hog = bytearray(1 << 30)
"""


async def _post(app, *, fmt="docx", purpose=UPLOAD, body=b"x", headers=AUTH) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://docparse") as c:
        params = {"format": fmt, "purpose": purpose}
        return await c.post("/extract", params=params, content=body, headers=headers)


# -- 서버의 HTTP 모양 ------------------------------------------------------------------------


def test_requests_without_the_token_are_refused():
    app = docparse_app()
    assert asyncio.run(_post(app, headers={})).status_code == 401
    assert asyncio.run(_post(app, headers={"Authorization": "Bearer nope"})).json()["code"] == (
        "unauthorized"
    )


def test_query_and_size_checks():
    app = docparse_app()
    assert asyncio.run(_post(app, fmt="text")).json()["code"] == "unsupported_format"
    assert asyncio.run(_post(app, fmt="exe")).status_code == 422
    assert asyncio.run(_post(app, purpose="other")).json()["code"] == "invalid_purpose"
    too_big = asyncio.run(_post(app, body=b"x" * (MAX_DOCUMENT_BYTES + 1)))
    assert too_big.status_code == 413 and too_big.json()["code"] == "too_large"


def test_extracts_through_the_app_with_the_result_shape():
    r = asyncio.run(_post(docparse_app(), body=make_docx(w_p("안녕"))))
    assert r.status_code == 200
    assert r.json() == {"text": "안녕", "pages": None, "sheets": None, "truncated": False}
    broken = asyncio.run(_post(docparse_app(), fmt="pdf", body=b"not a pdf"))
    assert broken.status_code == 422 and broken.json()["code"] == "extract_failed"


def test_two_slots_and_the_indexer_gets_only_one():
    async def main():
        gate = asyncio.Event()
        started: list[str] = []

        async def slow(request, data=b"", *, timeout):
            started.append(request["purpose"])
            await gate.wait()
            return {"text": "ok"}

        app = docparse_app(slow, slots=Slots())
        first_index = asyncio.create_task(_post(app, purpose=INDEX))
        while len(started) < 1:
            await asyncio.sleep(0.01)
        second_index = await _post(app, purpose=INDEX)
        assert second_index.status_code == 429 and second_index.json()["code"] == "extract_busy"
        assert second_index.headers["retry-after"] == "5"
        upload = asyncio.create_task(_post(app, purpose=UPLOAD))
        while len(started) < 2:
            await asyncio.sleep(0.01)
        third = await _post(app, purpose=UPLOAD)
        assert third.status_code == 429  # 레플리카마다 둘이다
        gate.set()
        assert (await first_index).status_code == 200 and (await upload).status_code == 200
        assert (await _post(app, purpose=INDEX)).status_code == 200  # 자리를 돌려받았다

    asyncio.run(main())


# -- 하위 프로세스 ---------------------------------------------------------------------------


def test_the_document_is_written_only_after_the_ready_byte():
    runner = functools.partial(run_worker, argv=_hook(EARLY_HOOK))
    r = asyncio.run(_post(docparse_app(runner), body=b"secret document"))
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["text"]) == {"early": False, "bytes": 15, "task": "extract"}


def test_the_worker_talks_over_a_socket_pair():
    """하위 프로세스의 표준 입력과 출력은 소켓이다. 파이프였다면 가두기 전의 틈에 먼저 뚫린 다른
    하위 프로세스가 `/proc/<pid>/fd` 로 다시 열 수 있다."""
    got = asyncio.run(run_worker({"task": "extract"}, b"document", argv=_hook(STDIO_HOOK)))
    assert json.loads(got["text"]) == [True, True]


def test_a_stuck_extraction_is_killed_with_its_group(monkeypatch):
    """훅이 문서를 받은 뒤 멈춘다. pid 는 띄우는 자리에서 받는다. 병렬 실행에서 훅이 시간 상한
    안에 문서를 받지 못해도 그룹째 죽었는지 볼 수 있다."""
    pids: list[int] = []

    async def recording_spawn(argv: tuple[str, ...]) -> Worker:
        worker = await spawn(argv)
        pids.append(worker.pid)
        return worker

    monkeypatch.setattr(sandbox, "spawn", recording_spawn)
    runner = functools.partial(run_worker, argv=_hook(HANG_HOOK))
    started = time.monotonic()
    r = asyncio.run(_post(docparse_app(runner, timeout=STUCK_TIMEOUT)))
    assert r.status_code == 422 and r.json()["code"] == "extract_timeout"
    assert time.monotonic() - started < 10
    assert len(pids) == 1 and not group_alive(pids[0])


def test_a_dead_worker_is_extract_failed_and_the_server_lives_on():
    """하위 프로세스가 죽으면 시간 상한을 기다리지 않고 바로 extract_failed 다."""
    app = docparse_app(functools.partial(run_worker, argv=_hook(DIE_HOOK)))
    started = time.monotonic()
    r = asyncio.run(_post(app))
    elapsed = time.monotonic() - started
    assert r.status_code == 422 and r.json()["code"] == "extract_failed", (r.text, elapsed)
    assert elapsed < 10
    assert asyncio.run(_post(docparse_app(), body=make_docx(w_p("다음")))).json()["text"] == "다음"


def test_the_worker_env_has_no_secret(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "server-only-secret")
    monkeypatch.setenv("DAEGAM_MONGO_URL", "mongodb://user:pass@db")
    probe = asyncio.run(run_worker({"task": "probe"}))
    assert TOKEN_ENV not in probe["env_keys"] and "DAEGAM_MONGO_URL" not in probe["env_keys"]
    assert probe["fork_blocked"] is True  # RLIMIT_NPROC 은 개발 호스트에서도 선다


def test_the_real_worker_extracts_a_document():
    got = asyncio.run(
        run_worker({"task": "extract", "format": "docx", "purpose": UPLOAD}, make_docx(w_p("가")))
    )
    assert got["text"] == "가"


@contextlib.contextmanager
def _sent(payload: bytes):
    """서버처럼 payload 를 보내고 쓰기를 닫는 소켓 쌍. worker 의 표준 입력처럼 다른 끝을
    BufferedReader 로 준다. 소켓 버퍼를 넘는 payload 를 위해 다른 스레드에서 보낸다."""
    ours, theirs = socket.socketpair()

    def send() -> None:
        ours.sendall(payload)
        ours.shutdown(socket.SHUT_WR)

    sender = threading.Thread(target=send)
    sender.start()
    with ours, theirs:
        try:
            with theirs.makefile("rb") as stdin:
                yield stdin
        finally:
            sender.join()


def test_the_worker_keeps_what_readline_buffered():
    """worker 는 요청 줄을 readline 으로 읽으므로 문서의 앞부분이 이미 버퍼에 있다. 그 앞부분과
    여러 번에 나눠 오는 나머지를 빠짐없이 읽는다."""
    document = os.urandom(300_000)
    with _sent(b'{"task": "extract"}\n' + document) as stdin:
        assert json.loads(stdin.readline(worker.MAX_REQUEST_LINE)) == {"task": "extract"}
        assert sandbox.read_document(stdin, len(document) + 1) == document


def test_the_worker_reads_one_byte_past_the_cap_to_refuse_a_large_document(monkeypatch):
    document = b"<p>a</p>"
    monkeypatch.setattr(worker, "MAX_DOCUMENT_BYTES", len(document))
    request = {"format": "html", "purpose": UPLOAD}
    with _sent(document) as stdin:
        assert worker._extract(request, stdin)["text"] == "a"
    with _sent(document + b" ") as stdin, pytest.raises(ExtractError, match="than the limit"):
        worker._extract(request, stdin)


# -- Linux 에서만 서는 가두기 ---------------------------------------------------------------------


@LINUX_JAIL
def test_forking_fails_and_the_group_is_empty_after_the_kill():
    async def main():
        worker = await spawn()
        try:
            await send_request(worker, {"task": "hold"}, b"")
            assert await worker.reader.read(1) == b"H"
        finally:
            await kill_group(worker)
        return worker.pid

    assert not group_alive(asyncio.run(main()))
    assert asyncio.run(run_worker({"task": "probe"}))["fork_blocked"] is True


@LINUX_JAIL
def test_the_memory_limit_kills_only_the_worker():
    app = docparse_app(functools.partial(run_worker, argv=_hook(GREEDY_HOOK)))
    assert asyncio.run(_post(app)).json()["code"] == "extract_failed"


@pytest.fixture
def running_server():
    """`python -m daegam.documents.server` 를 따로 띄운다. 파드의 서버와 같은 기동이다."""
    port = free_port()
    env = {**os.environ, TOKEN_ENV: "server-only-secret", "DAEGAM_DOCPARSE_PORT": str(port)}
    server = subprocess.Popen([sys.executable, "-m", "daegam.documents.server"], env=env)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        yield server.pid
    finally:
        server.terminate()
        server.wait(10)


@LINUX_JAIL
def test_the_server_and_its_workers_are_not_dumpable(running_server):
    with pytest.raises(PermissionError):
        open(f"/proc/{running_server}/environ", "rb").read()
    assert asyncio.run(run_worker({"task": "probe"}))["dumpable"] == 0


@LINUX_JAIL
def test_selfcheck_reports_five_trues(running_server):
    out = subprocess.run(
        [sys.executable, "-m", "daegam.documents.selfcheck", str(running_server)],
        capture_output=True,
        timeout=60,
    )
    assert json.loads(out.stdout) == {
        "fork_blocked": True,
        "dumpable_zero": True,
        "server_environ_unreadable": True,
        "sibling_stdio_unopenable": True,
        "group_killed": True,
    }
    assert out.returncode == 0


# -- 플랫폼 클라이언트 ------------------------------------------------------------------------


def _client(handler) -> DocparseClient:
    return DocparseClient("http://docparse", TOKEN, transport=httpx.MockTransport(handler))


def _never(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"text formats must not call the service: {request.url}")


def test_text_formats_are_decoded_here_without_the_service():
    client = _client(_never)
    got = asyncio.run(client.extract("csv", "a,b\n가,\xff".encode() + b"\xff", purpose=UPLOAD))
    assert got.text == "a,b\n가,ÿ�" and not got.truncated
    long = "가" * (MAX_UPLOAD_CHARS + 1)
    upload = asyncio.run(client.extract("text", long.encode(), purpose=UPLOAD))
    assert len(upload.text) == MAX_UPLOAD_CHARS and upload.truncated
    assert asyncio.run(client.extract("markdown", long.encode(), purpose=INDEX)).text == long


def test_attachment_text_from_the_service_is_capped_here_too():
    """서비스 답의 `truncated` 를 믿지 않고 첨부의 글은 상한에서 다시 자른다. 인덱서의 글은
    그대로다."""
    long = "가" * (MAX_UPLOAD_CHARS + 1)
    client = _client(lambda _request: httpx.Response(200, json={"text": long, "truncated": False}))
    upload = asyncio.run(client.extract("pdf", b"%PDF", purpose=UPLOAD))
    assert len(upload.text) == MAX_UPLOAD_CHARS and upload.truncated
    assert asyncio.run(client.extract("pdf", b"%PDF", purpose=INDEX)).text == long


@pytest.mark.parametrize(
    ("answer", "code", "retryable"),
    [
        (
            httpx.Response(422, json={"code": "encrypted_document", "detail": "x"}),
            "encrypted_document",
            False,
        ),
        (httpx.Response(413, json={"code": "too_large", "detail": "x"}), "too_large", False),
        (
            httpx.Response(429, json={"code": "extract_busy"}, headers={"Retry-After": "5"}),
            "extract_busy",
            True,
        ),
        (httpx.Response(401, json={"code": "unauthorized"}), "extract_unavailable", True),
        (httpx.Response(502, text="bad gateway"), "extract_unavailable", True),
        (httpx.Response(200, text="not json"), "extract_failed", False),
        (httpx.ConnectError("refused"), "extract_unavailable", True),
    ],
)
def test_service_answers_fold_into_codes(answer, code, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert dict(request.url.params) == {"format": "pdf", "purpose": "index"}
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(DocparseError) as e:
        asyncio.run(_client(handler).extract("pdf", b"%PDF", purpose=INDEX))
    assert e.value.code == code and e.value.retryable is retryable
    if code == "extract_busy":
        assert e.value.headers == {"Retry-After": "5"}


def test_no_token_means_unavailable_without_a_call():
    client = DocparseClient("http://docparse", None, transport=httpx.MockTransport(_never))
    with pytest.raises(DocparseError) as e:
        asyncio.run(client.extract("pdf", b"%PDF", purpose=UPLOAD))
    assert e.value.code == "extract_unavailable" and e.value.retryable
