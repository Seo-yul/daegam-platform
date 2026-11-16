"""DCR 등록과 DCR client_id (P33 4절). 등록은 아무것도 저장하지 않고 client_id 가 서명한 등록
값이다. OAuth 가 꺼진 까닭의 기동 로그(1절)도 여기서 본다. 꺼졌을 때의 404 는
test_oauth_metadata.py 가 본다."""

from __future__ import annotations

import asyncio
import logging

import pytest

from daegam.api.oauth import REGISTER_MAX_BYTES
from daegam.auth.signer import PlatformSigner
from daegam.services.oauth.clients import (
    MAX_CLIENT_NAME_CHARS,
    UNNAMED_CLIENT,
    clean_client_name,
    client_ref,
    name_collides,
)
from tests.helpers.http import startup_warnings
from tests.helpers.oauth import REGISTER, oauth_service, resolve_error

LOOPBACK = "http://localhost:8787/callback"
# Cursor 가 보내는 모양. loopback, 사용자 정의 스킴, https 가 함께 온다
CURSOR = {
    "client_name": "Cursor",
    "redirect_uris": [
        LOOPBACK,
        "cursor://anysphere.cursor-retrieval/oauth/user-callback",
        "https://www.cursor.com/oauth/callback",
    ],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def test_cursor_shaped_registration_keeps_only_the_loopback(oauth_client):
    r = oauth_client.post(REGISTER, json=CURSOR)
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    got = r.json()
    assert got["client_id"].startswith("dcr.") and isinstance(got["client_id_issued_at"], int)
    assert {k: v for k, v in got.items() if k not in ("client_id", "client_id_issued_at")} == {
        "client_name": "Cursor",
        "redirect_uris": [LOOPBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    client = asyncio.run(oauth_client.app.state.oauth_clients.resolve(got["client_id"]))
    assert (client.kind, client.name, client.redirect_uris) == ("dcr", "Cursor", (LOOPBACK,))
    assert client.accepts("http://127.0.0.1:50000/callback")
    assert client.ref == client_ref(got["client_id"]) and client.ref.startswith("dcr:")
    assert len(client.ref) == len("dcr:") + 16 and client.host is None
    # 비밀을 내지 않는다. 요청한 인증 방식과 상관없이 등록 값은 none 이다. 지원하지 않는
    # grant(client_credentials)는 버린다
    grants = ["refresh_token", "authorization_code", "client_credentials"]
    body = CURSOR | {"token_endpoint_auth_method": "client_secret_basic", "grant_types": grants}
    got = oauth_client.post(REGISTER, json=body).json()
    assert got["token_endpoint_auth_method"] == "none"
    assert got["grant_types"] == ["authorization_code", "refresh_token"]
    assert not any("secret" in k for k in got)


def test_no_loopback_left_is_invalid_redirect_uri(oauth_client):
    """loopback 이 아닌 http 와 조각이 붙은 loopback 은 버린다. 남는 것이 없으면 거절한다. 남은
    것은 200자까지의 넷이다."""
    uris = ["http://example.com/callback", "http://localhost/callback#x"]
    body = {"client_name": "x", "redirect_uris": uris}
    r = oauth_client.post(REGISTER, json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri", r.text
    assert r.headers["cache-control"] == "no-store"
    long = "http://localhost/" + "a" * 200
    uris = [long] + [f"http://localhost/cb{i}" for i in range(6)]
    r = oauth_client.post(REGISTER, json={"redirect_uris": uris})
    assert r.status_code == 201, r.text
    assert r.json()["redirect_uris"] == sorted(f"http://localhost/cb{i}" for i in range(4))


def test_grant_types_without_the_code_grant_are_refused(oauth_client):
    """인가 코드 없이는 쓸 길이 없는 등록이다. 목록이 아닌 값은 인가 코드를 적어도 그렇다."""
    for value in (["refresh_token"], "authorization_code"):
        r = oauth_client.post(REGISTER, json={"redirect_uris": [LOOPBACK], "grant_types": value})
        assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata", r.text


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Cursor", "Cursor"),
        ("Ｃｕｒｓｏｒ", "Cursor"),  # NFKC
        ("Claude\u200bCode", "ClaudeCode"),  # 폭 없는 공백(Cf)
        ("evil\u202eedoC", "eviledoC"),  # bidi 재정렬(Cf)
        ("  My \t\n  tool\u3000 v2 ", "My tool v2"),  # Cc 지우기와 공백 줄이기
        ("bad\ud800name", "badname"),  # 짝 없는 대리 문자
        ("x" * 100, "x" * MAX_CLIENT_NAME_CHARS),
        ("a" * (MAX_CLIENT_NAME_CHARS - 1) + " b", "a" * (MAX_CLIENT_NAME_CHARS - 1)),
        ("", UNNAMED_CLIENT),
        (None, UNNAMED_CLIENT),
        ("\u200b\u200e \t", UNNAMED_CLIENT),
    ],
)
def test_client_name_is_cleaned(raw, want):
    assert clean_client_name(raw) == want


@pytest.mark.parametrize(
    ("raw", "collides"),
    [
        ("Claude\u200bCode", True),
        ("Claude Code", True),
        ("Visual Studio Code", True),
        ("my vscode helper", True),
        ("Ｃｕｒｓｏｒ", True),
        ("Codex", True),
        ("Totally Legit Tool", False),
        (None, False),
    ],
)
def test_known_names_collide_after_cleaning(raw, collides):
    """폭 없는 공백이나 전각 글자를 끼워도 정리한 이름으로 판정하므로 경고를 피하지 못한다."""
    assert name_collides(clean_client_name(raw)) is collides


def test_a_new_signing_key_invalidates_old_client_ids():
    """두 HMAC 키는 서명 키에서 나온다. 키를 바꾸면 클라이언트는 invalid_client 를 받고 다시
    등록한다."""
    old, new = oauth_service(PlatformSigner.generate()), oauth_service(PlatformSigner.generate())
    client_id = old.register(CURSOR)["client_id"]
    assert asyncio.run(old.resolve(client_id)).name == "Cursor"
    assert resolve_error(new, client_id).code == "invalid_client"
    assert new.register(CURSOR)["client_id"] != client_id


def test_registration_stores_nothing(oauth_client, mongo):
    """인증 없는 엔드포인트는 인가 전에 저장하지 않는다(3절). 감사 행만 남는다."""

    def counts() -> dict[str, int]:
        return {n: mongo[n].count_documents({}) for n in mongo.list_collection_names()}

    before = counts()
    for i in range(20):
        body = CURSOR | {"client_name": f"c{i}"}
        assert oauth_client.post(REGISTER, json=body).status_code == 201
    after = counts()
    assert after.pop("audit_events") == before.pop("audit_events", 0) + 20
    assert after == before


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"x" * (REGISTER_MAX_BYTES + 1), 413),
        (b"not json", 400),
        (b'{"client_name": 7, "redirect_uris": ["http://localhost/cb"]}', 400),
        (b'{"redirect_uris": "http://localhost/cb"}', 400),
    ],
)
def test_bad_bodies_are_invalid_client_metadata(oauth_client, raw, status):
    r = oauth_client.post(REGISTER, content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == status and r.json()["error"] == "invalid_client_metadata", r.text
    assert set(r.json()) == {"error", "error_description"}


def test_client_routes_are_not_in_openapi(oauth_client):
    """클라이언트는 메타데이터로 찾는다. 화면 타입(schema.d.ts)에는 동의 화면이 부르는 셋만
    싣는다."""
    paths = {p for p in oauth_client.app.openapi()["paths"] if p.startswith("/api/oauth")}
    assert paths == {f"/api/oauth/consent/{step}" for step in ("inspect", "approve", "deny")}


# -- 켜지는 조건 (1절) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "said"),
    [
        ({"public_url": None}, "OAuth authorization server is off: DAEGAM_PUBLIC_URL is not set"),
        ({}, None),
        ({"auth_mode": "header"}, None),
    ],
)
def test_startup_says_why_oauth_is_off(offline_oauth_settings, caplog, overrides, said):
    """local 에서 꺼졌으면 까닭을 남긴다(15절). 다른 모드에는 인가 서버가 없으므로 말하지 않는다."""
    logs = startup_warnings(offline_oauth_settings, caplog, level=logging.INFO, **overrides)
    assert [m for m in logs if "OAuth" in m] == ([said] if said else [])
