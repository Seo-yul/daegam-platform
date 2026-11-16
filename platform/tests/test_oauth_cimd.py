"""CIMD 클라이언트 (P33 4절). 정확한 URL 허용 목록, fetch_url 의 제한, 문서 확인, 캐시와 실패 기억.

문서는 실제 `fetch_url` 이 받는다. 이름 풀기와 연결만 가짜(resolver, MockTransport)로 바꿔 끼워
리다이렉트·크기·특수 용도 주소의 판정이 실제 코드를 지나게 한다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from daegam.config import OAUTH_CIMD_DEFAULT_CLIENTS, settings_from_env
from daegam.services.oauth.clients import (
    CIMD_CACHE_SECONDS,
    CIMD_FAILURE_SECONDS,
    CIMD_MAX_BYTES,
    OAuthClients,
)
from tests.helpers.oauth import (
    CLAUDE,
    CLAUDE_DOC,
    VSCODE,
    FakeWeb,
    oauth_service,
    resolve_error,
)

# VS Code 의 실제 문서 모양. device flow grant 와 웹 중계 주소를 함께 적는다
VSCODE_DOC = {
    "client_id": VSCODE,
    "client_name": "Visual Studio Code",
    "grant_types": [
        "authorization_code",
        "refresh_token",
        "urn:ietf:params:oauth:grant-type:device_code",
    ],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
    "application_type": "native",
    "redirect_uris": ["http://127.0.0.1:33418/", "https://vscode.dev/redirect"],
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def web(monkeypatch) -> FakeWeb:
    return FakeWeb(monkeypatch)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def svc(clock) -> OAuthClients:
    return oauth_service(cimd_clients=OAUTH_CIMD_DEFAULT_CLIENTS, clock=clock)


def resolve(svc: OAuthClients, client_id: str):
    return asyncio.run(svc.resolve(client_id))


def test_vscode_keeps_the_loopback_and_the_supported_grants(svc, web):
    """문서의 웹 중계 주소는 버리고(5절), device flow 는 지원하지 않으므로 두 grant 만 쓴다."""
    web.serve(VSCODE, VSCODE_DOC)
    client = resolve(svc, VSCODE)
    assert client.redirect_uris == ("http://127.0.0.1:33418/",)
    assert client.grant_types == ("authorization_code", "refresh_token")
    assert client.accepts("http://127.0.0.1:61001/")
    assert not client.accepts("https://vscode.dev/redirect")
    assert client.host == "vscode.dev"


@pytest.mark.parametrize(
    "client_id",
    [
        "https://claude.ai/oauth/other-client-metadata",
        "https://claude.ai/oauth/claude-code-client-metadata/",
        "https://claude.ai/oauth/claude-code-client-metadata?x=1",
        "http://claude.ai/oauth/claude-code-client-metadata",
        "https://evil.example.com/client.json",
    ],
)
def test_only_the_exact_allowed_urls(svc, web, client_id):
    """허용 목록은 호스트가 아니라 URL 이다. 같은 호스트의 다른 문서도 받지 않고 받으러 가지도
    않는다."""
    web.serve(client_id, CLAUDE_DOC | {"client_id": client_id})
    e = resolve_error(svc, client_id)
    assert (e.status, e.code) == (401, "invalid_client")
    assert web.calls == 0


def test_an_empty_allow_list_turns_cimd_off(web, clock):
    web.serve(CLAUDE, CLAUDE_DOC)
    assert resolve_error(oauth_service(clock=clock), CLAUDE).code == "invalid_client"
    assert web.calls == 0


def unavailable(svc: OAuthClients) -> None:
    e = resolve_error(svc, CLAUDE)
    assert (e.status, e.code) == (503, "client_metadata_unavailable")


def test_redirects_are_not_followed(svc, web):
    web.responses[CLAUDE] = httpx.Response(302, headers={"location": "https://claude.ai/x"})
    web.serve("https://claude.ai/x", CLAUDE_DOC)
    unavailable(svc)
    assert web.calls == 1


def test_documents_over_5kb_are_refused(svc, web):
    web.serve(CLAUDE, CLAUDE_DOC | {"padding": "x" * CIMD_MAX_BYTES})
    unavailable(svc)


def test_special_use_addresses_are_refused(svc, web):
    web.private.add("claude.ai")
    web.serve(CLAUDE, CLAUDE_DOC)
    unavailable(svc)
    assert web.calls == 0


@pytest.mark.parametrize(
    "doc",
    [
        CLAUDE_DOC | {"client_id": VSCODE},
        CLAUDE_DOC | {"client_secret": "s3cret"},
        CLAUDE_DOC | {"token_endpoint_auth_method": "client_secret_basic"},
        CLAUDE_DOC | {"redirect_uris": []},
        CLAUDE_DOC | {"redirect_uris": "http://localhost/callback"},
        CLAUDE_DOC | {"grant_types": ["refresh_token"]},
        {k: v for k, v in CLAUDE_DOC.items() if k != "client_id"},
        ["not", "an", "object"],
        "not json",
    ],
)
def test_documents_that_are_not_public_clients_of_this_url_are_invalid(svc, web, doc):
    if doc == "not json":
        web.responses[CLAUDE] = httpx.Response(200, content=b"{not json")
    else:
        web.serve(CLAUDE, doc)
    e = resolve_error(svc, CLAUDE)
    assert (e.status, e.code) == (401, "invalid_client")


def test_missing_grant_types_mean_the_code_grant_only(svc, web):
    """RFC 7591 의 기본값이다. 그 클라이언트에게는 갱신 토큰을 내지 않는다(9절)."""
    web.serve(CLAUDE, {k: v for k, v in CLAUDE_DOC.items() if k != "grant_types"})
    assert resolve(svc, CLAUDE).grant_types == ("authorization_code",)


def test_documents_are_cached_for_an_hour(svc, web, clock):
    web.serve(CLAUDE, CLAUDE_DOC)
    for _ in range(5):
        resolve(svc, CLAUDE)
    assert web.calls == 1
    clock.now += CIMD_CACHE_SECONDS + 1
    web.serve(CLAUDE, CLAUDE_DOC | {"client_name": "Claude Code 2"})
    assert resolve(svc, CLAUDE).name == "Claude Code 2"
    assert web.calls == 2


def test_failures_are_remembered_for_a_minute(svc, web, clock):
    web.responses[CLAUDE] = httpx.Response(503)
    for _ in range(5):
        unavailable(svc)
    assert web.calls == 1
    web.serve(CLAUDE, CLAUDE_DOC)
    clock.now += CIMD_FAILURE_SECONDS - 1
    unavailable(svc)
    clock.now += 2
    assert resolve(svc, CLAUDE).name == "Claude Code"
    assert web.calls == 2


# -- DAEGAM_OAUTH_CIMD_CLIENTS (17절) -------------------------------------------------------------


def test_the_allow_list_defaults_to_the_three_clients(bare_env):
    assert settings_from_env().oauth_cimd_clients == OAUTH_CIMD_DEFAULT_CLIENTS


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("", ()),
        (" , ", ()),
        (f" {CLAUDE} ,{VSCODE}", (CLAUDE, VSCODE)),
    ],
)
def test_the_allow_list_is_read_from_env(bare_env, raw, want):
    bare_env.setenv("DAEGAM_OAUTH_CIMD_CLIENTS", raw)
    assert settings_from_env().oauth_cimd_clients == want


@pytest.mark.parametrize(
    "raw", ["http://claude.ai/x", "claude.ai/x", f"{CLAUDE},ftp://x.example/y", "https://a b/c"]
)
def test_anything_but_https_urls_refuses_to_start(bare_env, raw):
    bare_env.setenv("DAEGAM_OAUTH_CIMD_CLIENTS", raw)
    with pytest.raises(ValueError, match="DAEGAM_OAUTH_CIMD_CLIENTS"):
        settings_from_env()
