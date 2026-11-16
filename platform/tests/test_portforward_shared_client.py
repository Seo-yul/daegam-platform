"""터널을 여는 일이 클러스터가 함께 쓰는 ApiClient 를 건드리지 않는다.

kubernetes stream 모듈(36.0.3)은 터널을 여는 동안 ApiClient.request 를 웹소켓 함수로
바꿔 끼우고, 끝나면 연 때 본 값으로 되돌린다. 시험은 그 모듈의 진짜 바꿔 끼우기
(`_websocket_request`)를 그대로 쓰고 웹소켓 연결만 가짜로 둔다. 두 열기를 스레드 둘로 겹치게
맞춘다. 먼저 들어온 쪽은 둘째가 들어올 때까지, 둘째는 첫째가 끝날 때까지 기다린다.
"""

from __future__ import annotations

import functools
import importlib
import socket
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client as k8s

import daegam.k8s.portforward as pfmod

WAIT = 5.0

# `kubernetes.stream.stream` 은 패키지가 다시 내보낸 같은 이름의 partial 이라 모듈은 이렇게 얻는다
stream_module = importlib.import_module("kubernetes.stream.stream")


class _Tunnel:
    """`PortForward` 의 가짜. `socket(port)` 가 `_socket` 을 든 프록시를 준다."""

    def __init__(self) -> None:
        self.ours, self.theirs = socket.socketpair()

    def socket(self, port: int) -> Any:
        return SimpleNamespace(_socket=self.ours)

    def close(self) -> None:
        self.ours.close()
        self.theirs.close()


def _is_rest(api_client: k8s.ApiClient) -> bool:
    """ApiClient.request 가 바뀌지 않은 REST 함수인가."""
    return getattr(api_client.request, "__func__", None) is k8s.ApiClient.request


class _Opens:
    """가짜 웹소켓 연결. 두 열기를 겹치게 맞추고, 열린 동안 함께 쓰는 클라이언트가 REST 인지
    적는다."""

    def __init__(self, shared: k8s.ApiClient) -> None:
        self.shared = shared
        self.lock = threading.Lock()
        self.entered = 0
        self.first_in = threading.Event()
        self.second_in = threading.Event()
        self.first_done = threading.Event()
        self.shared_was_rest: list[bool] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, configuration: Any, _method: str, url: str, **kwargs: Any) -> _Tunnel:
        with self.lock:
            self.entered += 1
            order = self.entered
        self.calls.append({"configuration": configuration, "headers": kwargs.get("headers") or {}})
        self.shared_was_rest.append(_is_rest(self.shared))
        if order == 1:
            self.first_in.set()
            assert self.second_in.wait(WAIT), "the second open never started"
        else:
            self.second_in.set()
            assert self.first_done.wait(WAIT), "the first open never finished"
        return _Tunnel()


@pytest.fixture
def shared() -> k8s.ApiClient:
    cfg = k8s.Configuration()
    cfg.host = "https://cluster.invalid:6443"
    cfg.api_key = {"authorization": "Bearer cluster-token"}
    return k8s.ApiClient(cfg)


@pytest.fixture
def opens(shared: k8s.ApiClient, monkeypatch) -> _Opens:
    fake = _Opens(shared)
    monkeypatch.setattr(
        "kubernetes.stream.portforward",
        functools.partial(stream_module._websocket_request, fake, {"_preload_content": False}),
    )
    # 핸드셰이크 시한을 거는 웹소켓 바꾸기는 이 시험과 상관없다
    monkeypatch.setattr(pfmod, "_install_handshake_bound", lambda: None)
    return fake


def _open_two_overlapping(shared: k8s.ApiClient, opens: _Opens) -> None:
    cluster = SimpleNamespace(api_client=shared)
    tunnels: list[Any] = []
    errors: list[BaseException] = []

    def open_one(first: bool) -> None:
        try:
            tunnels.append(pfmod.open_tunnel(cluster, "ns", "pod", 8080))
        except BaseException as e:  # noqa: BLE001 - 스레드의 실패를 본 스레드로 넘긴다
            errors.append(e)
        finally:
            if first:
                opens.first_done.set()

    first = threading.Thread(target=open_one, args=(True,))
    first.start()
    # 첫째가 가짜 연결 안에서 기다리기 시작한 뒤에 둘째를 띄운다
    assert opens.first_in.wait(WAIT), "the first open never started"
    second = threading.Thread(target=open_one, args=(False,))
    second.start()
    first.join(WAIT)
    second.join(WAIT)
    for _raw, close in tunnels:
        close()
    assert not errors, errors
    assert len(tunnels) == 2


def test_overlapping_opens_leave_the_shared_client_on_rest(shared, opens) -> None:
    """겹친 두 열기 뒤에도 함께 쓰는 ApiClient 의 request 는 REST 다. 열리는 동안에도 그
    클라이언트는 REST 다. 다른 스레드의 요청이 웹소켓으로 가지 않는다."""
    _open_two_overlapping(shared, opens)

    assert opens.shared_was_rest == [True, True]
    assert _is_rest(shared)


def test_the_tunnel_opens_with_the_cluster_configuration_and_token(shared, opens) -> None:
    """터널은 그 클러스터의 Configuration(CA, 주소)과 인증 헤더로 열린다. 웹소켓 연결이 보내는
    헤더는 authorization 과 sec-websocket-protocol 뿐이라 그것만 본다. 함께 쓰던 ApiClient 로 열던
    때도 통과하는 시험이다. 사본으로 바꾼 뒤에도 같은 값으로 여는지 지킨다."""
    _open_two_overlapping(shared, opens)

    for call in opens.calls:
        assert call["configuration"] is shared.configuration
        assert call["headers"]["authorization"] == "Bearer cluster-token"
