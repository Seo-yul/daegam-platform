from __future__ import annotations

import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from tests.mock_soap_server import SoapHandler

FIXTURES = Path(__file__).parent / "fixtures"
# serve_forever 가 shutdown 요청을 확인하는 간격(초)이다.
# 기본값 0.5초면 서버를 내릴 때마다 그만큼 기다린다
POLL_INTERVAL = 0.01


def load_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


@contextmanager
def serve(
    handler: type[BaseHTTPRequestHandler], tls: ssl.SSLContext | None = None
) -> Iterator[str]:
    """핸들러를 ThreadingHTTPServer 로 스레드에 띄우고 base URL 을 준다. tls 가 있으면 그 서버
    문맥으로 https 를 연다. 나갈 때 내린다."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, args=(POLL_INTERVAL,), daemon=True)
    thread.start()
    try:
        scheme = "https" if tls is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def calculator_spec() -> dict:
    return load_fixture("calculator.openapi.yaml")


@pytest.fixture
def petstore_spec() -> dict:
    return load_fixture("petstore.openapi.yaml")


@pytest.fixture
def mock_soap() -> Iterator[str]:
    """spec2openapi 의 테스트 SOAP 서버를 스레드로 띄우고 base URL 을 돌려준다."""
    with serve(SoapHandler) as base_url:
        yield base_url
