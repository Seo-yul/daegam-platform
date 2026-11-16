"""OAuth 돌아갈 곳의 규칙 (P33 5절). loopback 만 받고 셋을 같게 보며 포트를 보지 않는다."""

from __future__ import annotations

import pytest

from daegam.auth.oauth_redirect import (
    MAX_REDIRECT_URI_CHARS,
    loopback_key,
    loopback_only,
    redirect_matches,
)


@pytest.mark.parametrize(
    ("registered", "requested", "ok"),
    [
        # 5절 표
        ("http://localhost/callback", "http://localhost:53112/callback", True),
        ("http://127.0.0.1/callback", "http://localhost:53112/callback", True),
        ("http://127.0.0.1:33418/", "http://127.0.0.1:61001/", True),
        ("http://localhost/callback", "http://localhost:53112/callback/abc", False),
        ("http://localhost/callback", "http://localhost:53112/callback?x=1", False),
        ("https://vscode.dev/redirect", "https://vscode.dev/redirect", False),
        ("http://localhost/callback", "http://example.com/callback", False),
        ("http://localhost/callback", "https://localhost/callback", False),
        ("http://localhost/callback", "cursor://anysphere.cursor-retrieval/callback", False),
        ("http://localhost/callback", "http://localhost/callback#frag", False),
        # IPv6 loopback, 대문자 호스트, 기본 포트도 같은 컴퓨터다
        ("http://localhost/callback", "http://[::1]:40000/callback", True),
        ("http://[::1]/callback", "http://127.0.0.1/callback", True),
        ("http://localhost/callback", "http://LOCALHOST:8080/callback", True),
        ("http://localhost/callback", "http://localhost:80/callback", True),
        ("http://localhost/callback", "http://localhost:65535/callback", True),
        ("http://127.0.0.1/callback", "http://127.0.0.1:99999/callback", False),
        # 경로와 쿼리는 글자 그대로다
        ("http://localhost/cb?a=1", "http://localhost:1/cb?a=1", True),
        ("http://localhost/cb?a=1", "http://localhost:1/cb?a=2", False),
        ("http://localhost", "http://localhost:1/", False),
        ("http://localhost/Callback", "http://localhost/callback", False),
    ],
)
def test_matching(registered, requested, ok):
    assert redirect_matches([registered], requested) is ok


@pytest.mark.parametrize(
    "uri",
    [
        "https://localhost/callback",
        "HTTP://localhost/callback",
        "http://localhost.evil.com/callback",
        "http://127.0.0.2/callback",
        "http://0.0.0.0/callback",
        "http://[::2]/callback",
        "http://user@localhost/callback",
        "http://localhost@evil.com/callback",
        "http://localhost:53112@evil.com/callback",
        "http://localhost\\@evil.com/callback",
        "http://localhost:/callback",
        "http://localhost:abc/callback",
        # 포트는 1~65535 다. 밖이면 브라우저가 갈 수 없고 동의 화면이 포트를 읽지 못한다
        "http://127.0.0.1:99999/callback",
        "http://localhost:65536/callback",
        "http://localhost:0/callback",
        "http://[::1]:00000/callback",
        "http://localhost/call back",
        "http://localhost/callback\n",
        "\thttp://localhost/callback",
        "http://localhost/callback#",
        "http://localhost/\ud800",
        "//localhost/callback",
        "",
    ],
)
def test_anything_but_a_plain_loopback_is_refused(uri):
    assert loopback_key(uri) is None
    assert not redirect_matches([uri, "http://localhost/callback"], uri)


def test_length_cap():
    base = "http://localhost/"
    fits = base + "a" * (MAX_REDIRECT_URI_CHARS - len(base))
    assert loopback_key(fits) == "/" + "a" * (MAX_REDIRECT_URI_CHARS - len(base))
    assert loopback_key(fits + "a") is None


def test_loopback_only_keeps_the_order_and_drops_repeats():
    """Cursor 는 loopback, 사용자 정의 스킴, https 를 함께 보낸다. loopback 만 처음 순서대로
    남는다."""
    uris = [
        "cursor://anysphere.cursor-retrieval/oauth/callback",
        "http://localhost:8787/callback",
        "https://www.cursor.com/oauth/callback",
        "http://127.0.0.1/cb",
        "http://localhost:8787/callback",
    ]
    assert loopback_only(uris) == ["http://localhost:8787/callback", "http://127.0.0.1/cb"]
