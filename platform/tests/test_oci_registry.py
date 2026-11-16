"""허브 미러의 OCI 클라이언트 (scripts/lib/oci_registry.py, P55 6절). 이 컴퓨터의 평문 http
레지스트리 대역으로 토큰 흐름, 다음 쪽과 태그 대체 경로, 조각 업로드, 자격 증명이 다른 출처로 가지
않는 것을 본다.
"""

from __future__ import annotations

import base64
import http.server
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import oci_registry
import pytest

from tests.helpers.registry import digest_of

BASIC = "Basic " + base64.b64encode(b"user:pw").decode()


class Server:
    """요청을 받아 routes 의 함수로 답하는 레지스트리 대역. 받은 요청(메서드, 경로, 머리)을
    남긴다."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        self.blobs: dict[str, bytes] = {}
        self.uploads: dict[str, bytearray] = {}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _do(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.seen.append((self.command, self.path, dict(self.headers)))
                route = server.routes.get((self.command, self.path.split("?")[0]))
                status, headers, payload = route(self, body) if route else (404, {}, b"")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = _do  # noqa: N815 - http.server 의 이름

            def log_message(self, *_args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self._server.server_address[1]}"
        self.base = f"http://{self.host}"

    def route(self, method: str, path: str, fn: Any) -> None:
        self.routes[(method, path)] = fn


@contextmanager
def serving(n: int = 1) -> Iterator[list[Server]]:
    servers = [Server() for _ in range(n)]
    for s in servers:
        threading.Thread(target=s._server.serve_forever, daemon=True).start()
    try:
        yield servers
    finally:
        for s in servers:
            s._server.shutdown()
            s._server.server_close()


def ok(payload: bytes, **headers: str):
    return lambda _h, _b: (200, headers, payload)


def test_the_bearer_flow_sends_basic_only_to_a_realm_on_the_registry_host() -> None:
    index = json.dumps({"schemaVersion": 2, "manifests": []}).encode()
    with serving(2) as (reg, auth):

        def manifest(h, _b):
            if h.headers.get("Authorization") != "Bearer t0k":
                realm = f'Bearer realm="{auth.base}/token",service="x"'
                return 401, {"WWW-Authenticate": realm}, b""
            return 200, {"Content-Type": "application/vnd.oci.image.index.v1+json"}, index

        reg.route("GET", "/v2/a/b/manifests/1", manifest)
        auth.route("GET", "/token", ok(json.dumps({"token": "t0k"}).encode()))
        client = oci_registry.Client(reg.host, basic=("user", "pw"))
        raw, media, digest = client.manifest("a/b", "1")
        assert (raw, digest) == (index, digest_of(index))
        assert media == "application/vnd.oci.image.index.v1+json"
        # 다른 호스트의 realm 에는 basic 을 싣지 않는다
        assert all("Authorization" not in h for _m, _p, h in auth.seen)


def test_referrer_pages_follow_only_the_same_origin() -> None:
    page = json.dumps({"manifests": [{"digest": "sha256:" + "1" * 64}]}).encode()
    with serving(2) as (reg, other):
        reg.route(
            "GET",
            "/v2/a/b/referrers/sha256:" + "0" * 64,
            ok(page, Link='</v2/a/b/next>; rel="next"'),
        )
        reg.route("GET", "/v2/a/b/next", ok(page))
        client = oci_registry.Client(reg.host)
        assert len(client.referrers("a/b", "sha256:" + "0" * 64)) == 2
        reg.route("GET", "/v2/a/b/next", ok(page, Link=f'<{other.base}/v2/x>; rel="next"'))
        with pytest.raises(oci_registry.RegistryError, match="another origin"):
            client.referrers("a/b", "sha256:" + "0" * 64)
        assert other.seen == []


def test_referrers_fall_back_to_the_tag_schema() -> None:
    fallback = json.dumps({"manifests": [{"digest": "sha256:" + "2" * 64}]}).encode()
    with serving() as (reg,):
        reg.route("GET", "/v2/a/b/manifests/sha256-" + "0" * 64, ok(fallback))
        got = oci_registry.Client(reg.host).referrers("a/b", "sha256:" + "0" * 64)
        assert got == [{"digest": "sha256:" + "2" * 64}]


def test_large_blobs_go_up_in_chunks(monkeypatch) -> None:
    monkeypatch.setattr(oci_registry, "CHUNK", 4)
    data = b"0123456789"
    digest = digest_of(data)
    with serving() as (reg,):
        reg.route("HEAD", f"/v2/a/b/blobs/{digest}", lambda _h, _b: (404, {}, b""))
        reg.route(
            "POST", "/v2/a/b/blobs/uploads/", lambda _h, _b: (202, {"Location": "/up/1"}, b"")
        )

        def patch(_h, body):
            reg.uploads.setdefault("1", bytearray()).extend(body)
            return 202, {"Location": "/up/1"}, b""

        reg.route("PATCH", "/up/1", patch)
        reg.route("PUT", "/up/1", lambda _h, _b: (201, {}, b""))
        oci_registry.Client(reg.host, basic=("user", "pw")).put_blob("a/b", digest, data)
        assert bytes(reg.uploads["1"]) == data
        patches = [h for m, _p, h in reg.seen if m == "PATCH"]
        assert len(patches) == 3 and all(h["Authorization"] == BASIC for h in patches)
        put = next(p for m, p, _h in reg.seen if m == "PUT")
        assert put == f"/up/1?digest={digest}"


def test_an_upload_location_on_another_origin_is_refused() -> None:
    data = b"x" * 3
    with serving(2) as (reg, other):
        reg.route("HEAD", f"/v2/a/b/blobs/{digest_of(data)}", lambda _h, _b: (404, {}, b""))
        reg.route(
            "POST",
            "/v2/a/b/blobs/uploads/",
            lambda _h, _b: (202, {"Location": f"{other.base}/up"}, b""),
        )
        with pytest.raises(oci_registry.RegistryError, match="another origin"):
            oci_registry.Client(reg.host, basic=("user", "pw")).put_blob(
                "a/b", digest_of(data), data
            )
        assert other.seen == []


def test_a_redirect_does_not_carry_the_credential() -> None:
    data = b"layer"
    digest = digest_of(data)
    with serving(2) as (reg, cdn):
        reg.route(
            "GET",
            f"/v2/a/b/blobs/{digest}",
            lambda _h, _b: (307, {"Location": f"{cdn.base}/blob"}, b""),
        )
        cdn.route("GET", "/blob", ok(data))
        got = oci_registry.Client(reg.host, basic=("user", "pw")).blob("a/b", digest, len(data))
        assert got == data
        assert reg.seen[0][2]["Authorization"] == BASIC
        assert all("Authorization" not in h for _m, _p, h in cdn.seen)


def test_a_blob_is_read_only_up_to_its_descriptor_size() -> None:
    data = b"longer than said"
    with serving() as (reg,):
        reg.route("GET", f"/v2/a/b/blobs/{digest_of(data)}", ok(data))
        with pytest.raises(oci_registry.RegistryError, match="size or digest"):
            oci_registry.Client(reg.host).blob("a/b", digest_of(data), 4)
        with pytest.raises(oci_registry.RegistryError, match="sha256"):
            oci_registry.Client(reg.host).blob("a/b", "sha256:../x", 4)
