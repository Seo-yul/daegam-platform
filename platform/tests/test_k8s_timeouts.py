"""P65: API 서버로 가는 요청마다 시한이 있고, 시한은 ClusterTimeout(연결할 수 없는 클러스터)이다.

진짜 ClusterClient 를 로컬 HTTP 서버에 붙인다. 서버는 디스커버리에는 답하고, 이름 공간 아래 경로와
`hang` 으로 고른 경로에는 연결만 받고 답하지 않는다.
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from collections.abc import Iterator

import pytest
from kubernetes import client as k8s

import daegam.k8s.client as kclient
import daegam.k8s.portforward as pfmod
from daegam.k8s.client import ClusterClient, ClusterTimeout, ClusterUnreachable

SHORT = (0.5, 0.5)

_DISCOVERY = {
    "/version": {"major": "1", "minor": "33", "gitVersion": "v1.33.0"},
    "/api": {"kind": "APIVersions", "versions": ["v1"]},
    "/apis": {"kind": "APIGroupList", "apiVersion": "v1", "groups": []},
    "/api/v1": {
        "kind": "APIResourceList",
        "groupVersion": "v1",
        "resources": [
            {
                "name": "configmaps",
                "singularName": "configmap",
                "namespaced": True,
                "kind": "ConfigMap",
                "verbs": ["get", "list", "patch", "delete", "create"],
            }
        ],
    },
}


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, hang_all: bool) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.hang_all = hang_all
        self.requests: list[str] = []
        self.release = threading.Event()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        line = self.rfile.readline().decode("latin-1")
        if not line:
            return
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        path = line.split(" ")[1].split("?")[0]
        server.requests.append(f"{line.split(' ')[0]} {path}")
        body = _DISCOVERY.get(path)
        if path.endswith("/log") and not server.hang_all:
            # 머리는 보내고 본문 도중에 멈춘다
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")
            self.wfile.flush()
            server.release.wait(10)
            return
        if server.hang_all or body is None:
            # 연결은 받았고 답하지 않는다
            server.release.wait(10)
            return
        data = json.dumps(body).encode()
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(data)}\r\n\r\n".encode()
            + data
        )


@pytest.fixture
def short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kclient, "API_REQUEST_TIMEOUT", SHORT)
    monkeypatch.setattr(kclient, "API_WRITE_TIMEOUT", SHORT)
    monkeypatch.setattr(pfmod, "TUNNEL_HANDSHAKE_TIMEOUT", SHORT[1])


def _serve(hang_all: bool) -> Iterator[_Server]:
    server = _Server(hang_all)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()


@pytest.fixture
def silent() -> Iterator[_Server]:
    yield from _serve(hang_all=True)


@pytest.fixture
def stuck() -> Iterator[_Server]:
    yield from _serve(hang_all=False)


def _api(server: _Server) -> k8s.ApiClient:
    cfg = k8s.Configuration()
    cfg.host = f"http://127.0.0.1:{server.server_address[1]}"
    return k8s.ApiClient(cfg)


def _client(server: _Server) -> ClusterClient:
    return ClusterClient(_api(server))


def _within(seconds: float, call) -> BaseException:
    started = time.monotonic()
    with pytest.raises(ClusterTimeout) as info:
        call()
    assert time.monotonic() - started < seconds
    return info.value


def test_construction_against_a_silent_server_times_out(short, silent) -> None:
    """DynamicClient 를 만들 때의 디스커버리도 시한 안이다."""
    err = _within(3, lambda: _client(silent))
    assert isinstance(err, ClusterUnreachable) and "read timed out after 0.5 s" in str(err)


def test_typed_calls_time_out(short, silent, monkeypatch) -> None:
    """타입 API(version, can_i)와 pods/log 도 같은 시한이다. 디스커버리를 건너뛰려고
    DynamicClient 는 만들지 않는다."""
    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)
    cc = _client(silent)
    _within(3, cc.version)
    _within(3, lambda: cc.can_i("get", "pods"))
    _within(3, lambda: cc.pod_log("p", "ns"))


def test_dynamic_calls_time_out_without_read_retry(short, stuck) -> None:
    """디스커버리는 답하고 리소스 요청은 매달리는 서버. get·apply·delete 가 ClusterTimeout 이고 읽기
    시한은 다시 하지 않는다(요청 한 번)."""
    cc = _client(stuck)
    for call in (
        lambda: cc.get("v1", "ConfigMap", "x", "ns"),
        lambda: cc.apply(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x", "namespace": "ns"}}
        ),
        lambda: cc.delete("v1", "ConfigMap", "x", "ns"),
    ):
        before = len(stuck.requests)
        _within(3, call)
        resource = [r for r in stuck.requests[before:] if "/namespaces/" in r]
        assert len(resource) == 1, stuck.requests[before:]


def test_pod_log_stalled_mid_body_times_out(short, stuck, monkeypatch) -> None:
    """머리를 받은 뒤 본문 도중에 멈춘 pods/log 도 ClusterTimeout 이다."""
    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)
    err = _within(3, lambda: _client(stuck).pod_log("p", "ns"))
    assert str(err).startswith("pods/log ns/p: read timed out")


def test_explicit_timeout_wins(short, stuck) -> None:
    """시한을 직접 준 호출은 그 값을 쓴다."""
    cc = _client(stuck)
    started = time.monotonic()
    with pytest.raises(ClusterTimeout, match="0.2"):
        cc.api_client.call_api(
            "/api/v1/namespaces/ns/configmaps/x", "GET", _request_timeout=(0.2, 0.2)
        )
    assert time.monotonic() - started < 2


def test_retry_policy_and_default(stuck) -> None:
    """기본 시한은 읽기 요청 (10, 30), 상태를 바꾸는 요청 (10, 45) 이고 풀은 읽기 시한을 다시 하지
    않는 재시도 정책을 쓴다."""
    assert kclient._default_timeout("GET") == (10.0, 30.0)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert kclient._default_timeout(method) == (10.0, 45.0)
    cc = _client(stuck)
    assert cc.api_client.rest_client.pool_manager.connection_pool_kw["retries"] is kclient.API_RETRY


def test_tunnel_handshake_times_out(short, silent) -> None:
    """포트 포워드 웹소켓의 핸드셰이크가 답을 못 받으면 ClusterTimeout 이다."""

    class _Client:
        api_client = _api(silent)

    _within(3, lambda: pfmod.open_tunnel(_Client(), "ns", "pod", 8080))


def test_open_tunnel_clears_the_socket_timeout(short, monkeypatch) -> None:
    """핸드셰이크가 끝난 웹소켓은 시한이 없다. 열린 터널의 읽기는 터널 위 httpx 가 정한다."""
    from kubernetes.stream import ws_client
    from websocket import WebSocket

    pfmod._install_handshake_bound()
    bounded = ws_client.WebSocket
    assert issubclass(bounded, WebSocket) and bounded is not WebSocket

    seen: dict[str, object] = {}

    def fake_connect(self, url, **options):
        seen["timeout"] = options.get("timeout")
        self.sock_opt.timeout = options["timeout"]
        self.sock = socket.socket()

    monkeypatch.setattr(WebSocket, "connect", fake_connect)
    ws = bounded()
    try:
        ws.connect("ws://example.invalid/")
        assert seen["timeout"] == SHORT[1]
        assert ws.gettimeout() is None
    finally:
        ws.sock.close()
