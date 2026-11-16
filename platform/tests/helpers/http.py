"""테스트 플랫폼을 띄우고 그 API 를 부르며 응답을 읽는 테스트가 함께 쓰는 도우미."""

from __future__ import annotations

import dataclasses
import logging
import socket
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daegam.config import Settings
from daegam.main import _warn_about_open_settings, create_app
from tests.helpers.fake_secrets import FakeSecretStore

# TestClient 가 요청을 보내는 주소. /api 밖에 남은 경로(/healthz, /.well-known/*)를 api_client 로
# 부를 때 이 절대 URL 을 붙인다
ROOT_URL = "http://testserver"
# API 는 /api 아래에 있다. 테스트 클라이언트를 거기 뿌리내려 두면 각 테스트가 논리 경로
# ("/clusters")를 그대로 쓴다. 루트에 남은 것은 conftest 의 root_client 로 부르고, 경계 자체는
# tests/test_root_routes.py 가 고정한다. UI 는 플랫폼이 내지 않는다(D52).
API_BASE_URL = f"{ROOT_URL}/api"


def api_client(app) -> TestClient:
    """/api 에 뿌리내린 TestClient. `with` 로 감싸야 lifespan 이 돈다."""
    return TestClient(app, base_url=API_BASE_URL)


def make_app(
    settings: Settings,
    fake_clusters,
    fake_secrets: FakeSecretStore | None,
    *,
    oidc_http_client: httpx.AsyncClient | None = None,
    spec_runner: Any = None,
    **over: Any,
) -> FastAPI:
    """settings 에 over 를 입힌 설정으로 뜨는 테스트 플랫폼. 클러스터는 fake_clusters(conftest 의
    FakeClusters)가 만들고 Secret 은 fake_secrets 에 둔다. fake_secrets 가 None 이면 Secret 저장소가
    없는 플랫폼이다. 같은 DB 를 쓰므로 다른 레플리카나 설정을 바꿔 다시 뜬 플랫폼도 이것으로 흉내
    낸다."""
    return create_app(
        dataclasses.replace(settings, **over),
        client_factory=fake_clusters.factory,
        secret_store=fake_secrets,
        oidc_http_client=oidc_http_client,
        spec_runner=spec_runner,
    )


def startup_warnings(
    settings: Settings, caplog, *, level: int = logging.WARNING, **overrides
) -> list[str]:
    """overrides 를 입힌 설정으로 기동 경고(_warn_about_open_settings)를 돌려 그 메시지만 낸다.
    level 을 낮추면 같은 자리의 안내 로그도 함께 받는다."""
    caplog.clear()
    with caplog.at_level(level, logger="daegam.main"):
        _warn_about_open_settings(dataclasses.replace(settings, **overrides))
    return caplog.messages


def free_port() -> int:
    """지금 비어 있는 로컬 TCP 포트. 따로 띄우는 서버나 port-forward 가 받는다."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def deployed(client, path: str, **kw: Any) -> list[dict[str, Any]]:
    """path 목록에서 draft 를 뺀 것. kw 는 GET 의 인자(headers 등)다."""
    return [d for d in client.get(path, **kw).json() if d["state"] != "draft"]


def detail_code(r: httpx.Response) -> tuple[int, str | None]:
    """플랫폼 오류 응답의 (상태, detail.code). 오류 꼴이 아닌 응답이면 code 는 None 이라, 기대와
    다를 때 단언이 받은 상태를 그대로 보여 준다."""
    try:
        detail = r.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return r.status_code, detail.get("code") if isinstance(detail, dict) else None
