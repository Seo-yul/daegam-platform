"""증명, 토큰, 준비 답, HTTP 의 경계 (P40 3절)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import socket

from conftest import Sandbox

from daegam_sandbox import API_VERSION


def test_proof_needs_no_bearer_and_answers_the_hmac(sandbox: Sandbox) -> None:
    nonce = secrets.token_hex(32)
    status, body = sandbox.call("/proof", {"nonce": nonce}, token=None)
    key = hashlib.sha256(sandbox.token.encode()).digest()
    want = hmac.new(key, bytes.fromhex(nonce), hashlib.sha256).hexdigest()
    assert (status, body) == (200, {"proof": want})


def test_a_bad_nonce_is_refused(sandbox: Sandbox) -> None:
    for nonce in ("zz" * 16, "ab" * 15, "ab" * 65, 12, None):
        status, _ = sandbox.call("/proof", {"nonce": nonce}, token=None)
        assert status == 400


def test_every_other_call_needs_the_token(sandbox: Sandbox) -> None:
    calls = ["/execute", "/cancel", "/files/upload", "/files/download", "/files/list"]
    for path in calls:
        for token in (None, "wrong", sandbox.token + "x"):
            status, body = sandbox.call(path, {"command": "true"}, token=token)
            assert (status, body) == (401, {"error": "unauthorized"}), path
    assert sandbox.call("/files/list", {})[0] == 200


def test_healthz_answers_readiness_and_the_api_version(sandbox: Sandbox) -> None:
    status, body = sandbox.call("/healthz", method="GET", token=None)
    assert (status, body) == (200, {"ready": True, "api": API_VERSION, "unchecked": False})


def test_one_connection_carries_the_proof_and_then_the_calls(sandbox: Sandbox) -> None:
    conn = sandbox.connect()
    assert sandbox.call("/proof", {"nonce": "00" * 16}, token=None, conn=conn)[0] == 200
    assert sandbox.execute("echo one", conn=conn)["output"] == "one\n"
    assert sandbox.execute("echo two", conn=conn)["output"] == "two\n"


def test_unknown_routes_and_methods(sandbox: Sandbox) -> None:
    assert sandbox.call("/nope", {})[0] == 404
    assert sandbox.call("/execute", method="GET")[0] == 405
    assert sandbox.call("/healthz", {})[0] == 405


def raw(sandbox: Sandbox, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", sandbox.port), timeout=10) as sock:
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def test_bodies_over_the_route_limit_and_chunked_bodies_are_refused(sandbox: Sandbox) -> None:
    big = raw(
        sandbox,
        b"POST /proof HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n",
    )
    assert big.startswith(b"HTTP/1.1 413 ")
    chunked = raw(
        sandbox,
        b"POST /proof HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    )
    assert chunked.startswith(b"HTTP/1.1 501 ")
    long_line = raw(sandbox, b"GET /" + b"a" * 20000 + b" HTTP/1.1\r\n\r\n")
    assert long_line.startswith(b"HTTP/1.1 431 ")
    garbage = raw(sandbox, b"hello\r\n\r\n")
    assert garbage.startswith(b"HTTP/1.1 400 ")
