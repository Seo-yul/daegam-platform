"""UI 컨테이너(nginx)와 플랫폼이 같은 보안 헤더를 내는지 본다 (D52).

HTML 은 daegam-ui 가, API 응답과 `/api/docs` 는 플랫폼이 낸다. 헤더 규칙이 셸·nginx
설정(`ui/docker/daegam-csp.sh`, `security.conf`, `nginx.conf`)과 파이썬(`security_headers.py`)
양쪽에 있으므로 한쪽만 고치면 화면과 API 의 CSP 가 갈린다. 이 테스트는 `ui/` 를 읽으므로
`make verify-ui-links` 갈래에 든다. 이미지 안의 busybox 셸은 `ui/docker/selftest.sh` 가 이미지
빌드 때 따로 본다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from daegam.config import settings_from_env
from daegam.security_headers import (
    CALLBACK_FRAME_ANCESTORS,
    browser_idp_origin,
    content_security_policy,
    security_headers,
)
from tests.helpers.consts import REPO

UI_DOCKER = REPO / "ui" / "docker"

# 플랫폼은 모드 키가 없을 때만 oidc 로 보고 앞뒤 ASCII 공백을 지우고 소문자로 본다. 빈 값과 모르는
# 값에서는 기동을 거절한다. NBSP 는 공백이 아니라 값의 일부다(셸의 [:space:] 와 같게)
MODES = [None, "header", "local", "oidc", " OIDC ", "", "basic", "oidc\xa0", "\x0boidc\x0c"]
ISSUERS = [
    None,
    "",
    "   ",
    "https://idp.example.com/realms/daegam",
    "https://dev-1.okta.com/oauth2/default/",
    "http://mock-oidc.daegam-system.svc:8080",
    "HTTPS://IdP.Example.com:8443/x?y#z",
    "https://idp.example.com?x=1#y",
    " https://idp.example.com \t",
    "\x0bhttps://idp.example.com/\x0c",
    "/",
    "///",
    " / ",
    # 유니코드 공백은 지우지 않으므로 origin 이 아니다
    "https://idp.example.com\xa0",
    "\x1chttps://idp.example.com",
    "https://idp.example.com\u3000",
    "https://-idp-.example.com/",
    "https://idp.example.com/\nfoo",
    # origin 이 아닌 것. CSP 에 들어가면 지시어나 정책이 새로 열리거나(; ,) 모든 호스트가
    # 열리거나(*) nginx 설정 문자열이 끊기거나(", $) CSP 문법에 자리가 없다(IPv6, 밑줄, % , 한글)
    "idp.example.com",
    "ftp://idp.example.com",
    "https://user@idp.example.com",
    'https://idp.example.com";add_header X 1;',
    "https://idp;script-src *",
    "https://idp.example.com,https://evil.example.com",
    "https://*",
    "https://*.example.com",
    "https://idp$host.example.com",
    "https://idp\\evil.example.com",
    "https://idp.example.com\n.evil",
    "https://id\tp.example.com",
    "https://idp_x.example.com",
    "https://idp%2eexample.com",
    "https://아이디피.example.com",
    "https://[::1]:8443/",
    "https://",
    "https://:443",
    "https://idp..example.com",
    "https://.idp.example.com",
    "https://idp.example.com.",
    "https://idp.example.com:",
    "https://idp.example.com:123456/",
    "https://idp.example.com:8443x/",
]

# 모드와 발급자의 모든 조합을 돌지는 않는다. 두 구현 모두 둘을 따로 푼다. 모드는 발급자를 보지 않고
# oidc·local·header 중 하나가 되거나 기동 거절로 끝나고, 발급자는 모드가 oidc 일 때만 쓰인다
# (셸의 `[ "$mode" = "oidc" ]`, 플랫폼의 browser_idp_origin). 그래서 발급자는 모두 모드 키가 없을
# 때와 oidc 일 때 보고, 모드는 모두 발급자의 세 갈래(없음, origin, origin 아님)에서 하나씩 고른
# 대표와 본다. 한쪽이라도 모드에 따라 발급자를 달리 읽게 되면 모든 조합으로 되돌린다
MODES_FOR_EVERY_ISSUER = [None, "oidc"]
ISSUERS_FOR_EVERY_MODE = [None, "https://idp.example.com/realms/daegam", "https://idp;script-src *"]
CASES = [(mode, issuer) for mode in MODES_FOR_EVERY_ISSUER for issuer in ISSUERS] + [
    (mode, issuer)
    for mode in MODES
    if mode not in MODES_FOR_EVERY_ISSUER
    for issuer in ISSUERS_FOR_EVERY_MODE
]

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _shell(mode: str | None, issuer: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if mode is not None:
        env["DAEGAM_AUTH_MODE"] = mode
    if issuer is not None:
        env["DAEGAM_OIDC_ISSUER"] = issuer
    return subprocess.run(
        ["sh", str(UI_DOCKER / "daegam-csp.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _platform(
    monkeypatch: pytest.MonkeyPatch, mode: str | None, issuer: str | None
) -> tuple[str | None, bool]:
    """(플랫폼 CSP 또는 기동 거절이면 None, UI 만 기동을 거절해야 하는가).

    플랫폼이 기동 때 CSP 를 정하는 경로 그대로다. settings_from_env 로 설정을 읽고
    browser_idp_origin 으로 origin 을 고른 뒤 content_security_policy 로 만든다."""
    for key, value in (("DAEGAM_AUTH_MODE", mode), ("DAEGAM_OIDC_ISSUER", issuer)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    try:
        settings = settings_from_env()
    except ValueError:
        return None, False
    idp = browser_idp_origin(settings)
    ui_only_refuses = (
        settings.auth_mode == "oidc" and settings.oidc_issuer is not None and idp is None
    )
    return content_security_policy(idp_origin=idp), ui_only_refuses


@pytest.mark.parametrize(("mode", "issuer"), CASES)
def test_ui_csp_is_the_platform_csp(monkeypatch, mode, issuer):
    """무리는 셋이다. 둘 다 뜨면 CSP 가 같다. 플랫폼이 기동을 거절하는 입력(빈 모드, 모르는 모드)은
    UI 도 거절한다. oidc 인데 발급자가 origin 이 아니면 UI 만 거절하고(원인을 남기고) 플랫폼 CSP 는
    IdP 를 열지 않는다."""
    csp, ui_only_refuses = _platform(monkeypatch, mode, issuer)
    done = _shell(mode, issuer)
    if csp is None:
        assert done.returncode != 0, done.stdout
        assert "DAEGAM_AUTH_MODE" in done.stderr
    elif ui_only_refuses:
        assert done.returncode != 0, done.stdout
        assert "DAEGAM_OIDC_ISSUER" in done.stderr
        assert "frame-src 'none';" in csp
    else:
        assert done.returncode == 0, done.stderr
        assert done.stdout.rstrip("\n") == csp


def test_real_origins_open_the_idp():
    """위 대칭이 둘 다 틀린 채로 맞을 수는 있으므로 기대값도 직접 적는다."""
    out = _shell("oidc", "https://dev-1.okta.com/oauth2/default/").stdout
    assert "connect-src 'self' https://dev-1.okta.com;" in out
    assert "frame-src 'self' https://dev-1.okta.com;" in out
    assert "frame-ancestors 'none';" in out
    assert "form-action 'self' https://dev-1.okta.com;" in out
    assert (
        "connect-src 'self' https://idp.example.com:8443;"
        in _shell(None, "HTTPS://idp.example.com:8443/x").stdout
    )
    for mode in ("header", "local"):
        out = _shell(mode, "https://idp.example.com").stdout
        assert "frame-src 'none';" in out and "connect-src 'self';" in out, mode
    for bad in ("https://*", "https://idp;script-src *", "https://idp.example.com\n.evil"):
        assert _shell("oidc", bad).returncode != 0, bad


def test_the_callback_csp_is_the_platform_callback_csp(monkeypatch):
    """/callback 응답의 CSP(`daegam-csp --callback`)도 플랫폼의 같은 함수와 같다. silent renew
    iframe 이 그 페이지를 같은 origin 에서 연다 (T13)."""
    for mode, issuer in (("oidc", "https://idp.example.com/realms/daegam"), ("header", None)):
        _csp, _ = _platform(monkeypatch, mode, issuer)
        settings = settings_from_env()
        expected = content_security_policy(
            idp_origin=browser_idp_origin(settings), frame_ancestors=CALLBACK_FRAME_ANCESTORS
        )
        done = _shell(mode, issuer, "--callback")
        assert done.returncode == 0, done.stderr
        assert done.stdout.rstrip("\n") == expected


def test_only_the_callback_may_be_framed_by_the_same_origin():
    """X-Frame-Options 는 nginx 의 map 이 정한다. 기본은 플랫폼과 같은 DENY 이고 /callback 만
    SAMEORIGIN 이다. start.sh 의 CSP map 도 같은 경로만 따로 둔다."""
    platform = dict(security_headers(idp_origin=None, https=False))[b"x-frame-options"].decode()
    conf = (UI_DOCKER / "nginx.conf").read_text("utf-8")
    # $uri 로 가르면 안 된다. try_files 가 /callback 을 안에서 /index.html 로 넘긴 뒤라 기본값이다
    block = conf[conf.index("map $request_uri $daegam_xfo {") :]
    block = block[: block.index("}")]
    assert f'default "{platform}";' in block and '"~^/callback(?:\\?|$)" "SAMEORIGIN";' in block
    start = (UI_DOCKER / "start.sh").read_text("utf-8")
    assert "map $request_uri $daegam_csp" in start and "~^/callback" in start
    pattern = re.compile(r"^/callback(?:\?|$)")
    for uri, framed in (
        ("/callback", True),
        ("/callback?code=x&state=y", True),
        ("/callback/", False),
        ("/%63allback", False),
        ("//callback", False),
        ("/callbackX", False),
        ("/", False),
    ):
        assert bool(pattern.match(uri)) is framed, uri


def _conf_headers() -> dict[str, str]:
    """security.conf 의 `add_header 이름 값 always;` 를 이름(소문자) → 값으로 읽는다. 값의 따옴표는
    벗긴다."""
    out: dict[str, str] = {}
    pattern = re.compile(r'\s*add_header\s+(\S+)\s+(?:"([^"]*)"|(\$\w+))\s+always;\s*')
    for line in (UI_DOCKER / "security.conf").read_text("utf-8").splitlines():
        m = pattern.fullmatch(line)
        if m:
            out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def test_fixed_headers_are_the_platform_headers():
    conf = _conf_headers()
    platform = {k.decode(): v.decode() for k, v in security_headers(idp_origin=None, https=True)}
    assert set(conf) == set(platform)
    assert conf["content-security-policy"] == "$daegam_csp"
    assert conf["strict-transport-security"] == "$daegam_hsts"
    assert conf["x-frame-options"] == "$daegam_xfo"
    variable = {"content-security-policy", "strict-transport-security", "x-frame-options"}
    for name in set(platform) - variable:
        assert conf[name] == platform[name], name


def test_hsts_line_is_the_platform_line():
    hsts = dict(security_headers(idp_origin=None, https=True))[
        b"strict-transport-security"
    ].decode()
    assert f'"{hsts}"' in (UI_DOCKER / "nginx.conf").read_text("utf-8")


def test_headers_reach_every_location():
    """nginx 는 location 에 add_header 가 하나라도 있으면 위 단계의 것을 물려받지 않는다. 서버
    단계에서 add_header_inherit merge 로 내려야 Cache-Control 을 가진 location 에도 CSP 가 붙는다.
    """
    conf = (UI_DOCKER / "nginx.conf").read_text("utf-8")
    server = conf[conf.index("server {") :]
    # 주석에도 location 이라는 낱말이 있으므로 지시어 줄의 첫 location 까지를 서버 단계로 본다
    head = server[: re.search(r"^\s*location\b", server, re.M).start()]
    assert "add_header_inherit merge;" in head
    assert "include /etc/nginx/daegam/security.conf;" in head
    # location 마다 되풀이하지 않는다. 되풀이하면 merge 와 겹쳐 같은 헤더가 두 번 나간다
    assert server.count("include /etc/nginx/daegam/security.conf;") == 1
