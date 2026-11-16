"""local 계정, 개인 MCP 토큰, OAuth 인가 서버를 보는 테스트가 함께 쓰는 도우미 (D40, P32, P33).

사람은 관리자 헤더로 만들어 테넌트의 viewer 로 넣는다(`person`). 개인 토큰은 그 사람의 비밀번호로
발급한다(`issued`). OAuth 연결은 DCR 로 등록한 스크립트 클라이언트(`register`)가 인가 요청과 동의를
지나 코드를 바꿔 맺는다(`connect`). OAuth 대상은 tests/helpers/docs.py 의 `seed_*` 로 Mongo 에 바로
넣고 테넌트는 그 빌더의 것(`TENANT`, acme)이다. CIMD 문서는 실제 `fetch_url` 이 받고 이름 풀기와
연결만 가짜(`FakeWeb`)다.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import functools
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from daegam import urlfetch
from daegam.auth.local import access_claims
from daegam.auth.pat_keys import PatKeyRing
from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import CONTROL_PLANE_AUDIENCE, PlatformSigner
from daegam.config import OAUTH_CIMD_DEFAULT_CLIENTS
from daegam.services.oauth.clients import OAuthClients
from daegam.services.oauth.errors import OAuthError
from tests.helpers.auth import bearer, login, settle, slot_keys
from tests.helpers.consts import PUBLIC_URL

# -- 사람 ----------------------------------------------------------------------------------------

NEW_PASSWORD = "a brand new password 42"


def password_of(username: str) -> str:
    """`make_user` 가 만든 사용자의 첫 비밀번호."""
    return f"{username} password 1234"


def make_user(c: TestClient, h: dict[str, str], username: str, mongo=None, **extra) -> dict:
    body = {"username": username, "password": password_of(username), **extra}
    r = c.post("/users", json=body, headers=h)
    assert r.status_code == 201, r.text
    if mongo is not None:
        settle(mongo, username)
    return r.json()


@dataclasses.dataclass(frozen=True)
class Person:
    id: str
    username: str
    password: str
    headers: dict[str, str]


def join(c: TestClient, admin: dict[str, str], tenant: str, *members: tuple[str, str]) -> None:
    """tenant 의 manual 소속에 (sub, role) 을 더한다. PUT 이 통째로 바꾸므로 읽어서 합친다."""
    rows = c.get(f"/tenants/{tenant}/members", headers=admin).json()
    body = [{"sub": m["sub"], "role": m["role"]} for m in rows] + [
        {"sub": sub, "role": role} for sub, role in members
    ]
    r = c.put(f"/tenants/{tenant}/members", json=body, headers=admin)
    assert r.status_code == 200, r.text


def person(
    c: TestClient, admin: dict[str, str], mongo, name: str, *tenants: str, role: str = "viewer"
) -> Person:
    """tenants 의 role(기본 viewer)인 사용자와 그 제어면 헤더."""
    user = make_user(c, admin, name, mongo)
    for tenant in tenants:
        join(c, admin, tenant, (user["id"], role))
    password = password_of(name)
    r = login(c, name, password)
    assert r.status_code == 200, r.text
    return Person(user["id"], name, password, bearer(r.json()["access_token"]))


# -- 발급자와 서명 키 ------------------------------------------------------------------------------

# 사람 토큰의 발급자를 옮긴 뒤의 주소
MOVED_ISSUER = "https://moved.example.com"


def move_issuer(c: TestClient) -> None:
    """플랫폼의 사람 토큰 발급자를 다른 주소로 옮긴다. 옛 발급자가 낸 토큰과 연결은 `issuer_changed`
    로 회수된다."""
    c.app.state.internal_issuer.human_issuer = MOVED_ISSUER


def rotate_signing_key(c: TestClient) -> None:
    """플랫폼 서명 키를 새로 붙인다. 옛 키에 기댄 토큰과 연결은 `key_rotated` 로 회수된다."""
    c.app.state.internal_issuer.attach(PlatformSigner.generate())


# -- 개인 토큰 -----------------------------------------------------------------------------------

# 개인 토큰을 받는 사람의 테넌트와 다른 팀. 다른 팀은 그 사람이 함께 속한 둘째
# 테넌트(개인 토큰)이거나 리소스와 상관없는 테넌트(OAuth)다
PAT_TENANT = "team-a"
OTHER_TEAM = "team-x"


def issue(
    c: TestClient,
    who: Person,
    *,
    tenant: str = PAT_TENANT,
    days: float = 30,
    label: str = "laptop",
    password: str | None = None,
) -> httpx.Response:
    body = {
        "tenant": tenant,
        "label": label,
        "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
        "password": who.password if password is None else password,
    }
    return c.post("/me/tokens", json=body, headers=who.headers)


def issued(c: TestClient, who: Person, **kw) -> dict:
    r = issue(c, who, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def fresh_slot_kids(c: TestClient) -> set[str]:
    """레플리카 캐시(30초)를 거치지 않은 JWKS 의 슬롯 kid. 회수 뒤 키가 빠졌는지 볼 때 쓴다."""
    issuer = c.app.state.internal_issuer
    ring = PatKeyRing(issuer, c.app.state.pat_keys.slots, mints=True, pool_size=1)
    return {k["kid"] for k in slot_keys(c.portal.call(ring.jwks), issuer.kid)}


# -- 인가 요청과 동의 ----------------------------------------------------------------------------

# OAuth 인가 서버의 엔드포인트 (P33)
REGISTER = "/oauth/register"
AUTHORIZE = "/oauth/authorize"
CONSENT = "/oauth/consent"
TOKEN = "/oauth/token"
REVOKE = "/oauth/revoke"
# 인가 요청이 가리키는 Gateway 주소, 클라이언트가 등록한 리디렉트 주소와 인가 요청이 싣는 리디렉트
# 주소, PKCE 짝
GW_HOST = "gw.acme.example"
GW_URL = f"https://{GW_HOST}/mcp"
REGISTERED = "http://127.0.0.1/callback"
REDIRECT = "http://localhost:53112/callback"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
# 최근 인증 창(10분)과 시계 여유(30초)를 넘는 나이
STALE_SECONDS = 11 * 60


def register(c: TestClient, name: str = "Script Client") -> str:
    body = {"client_name": name, "redirect_uris": [REGISTERED]}
    r = c.post(REGISTER, json=body)
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize(c: TestClient, client_id: str, *extra: tuple[str, str], **params) -> httpx.Response:
    """인가 요청. params 의 None 은 그 인자를 빼고, extra 는 같은 이름을 여러 번 싣는다."""
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "mcp offline_access",
        "resource": GW_URL,
        "state": "st-1",
        **params,
    }
    items = [(k, v) for k, v in query.items() if v is not None] + list(extra)
    return c.get(AUTHORIZE, params=items, follow_redirects=False)


def split(url: str) -> tuple[str, dict[str, str]]:
    """주소와 쿼리. 인자마다 값 하나다."""
    base, _, query = url.partition("?")
    parsed = parse_qs(query, keep_blank_values=True)
    assert all(len(v) == 1 for v in parsed.values()), parsed
    return base, {k: v[0] for k, v in parsed.items()}


def location(r: httpx.Response) -> tuple[str, dict[str, str]]:
    """302 의 주소와 쿼리."""
    assert r.status_code == 302, r.text
    return split(r.headers["location"])


def handle_for(c: TestClient, client_id: str, **params) -> str:
    base, query = location(authorize(c, client_id, **params))
    assert base == CONSENT and set(query) == {"request"}, query
    return query["request"]


def consent(c: TestClient, step: str, handle: str, headers: dict[str, str]) -> httpx.Response:
    return c.post(f"{CONSENT}/{step}", json={"request": handle}, headers=headers)


def stale_headers(c: TestClient, who: Person) -> dict[str, str]:
    """iat 가 11분 전인 제어면 토큰. 로그인이 내는 것과 같은 클레임이다."""
    auth = c.app.state.local_auth
    user = c.portal.call(auth.users.get, who.id)
    claims = access_claims(
        issuer=auth.issuer,
        audience=CONTROL_PLANE_AUDIENCE,
        sub=user.id,
        ttl=3600,
        token_version=user.token_version,
        username=user.username,
        email=None,
        display_name=None,
    )
    claims["iat"] -= STALE_SECONDS
    claims["nbf"] -= STALE_SECONDS
    return bearer(auth.signer.sign(claims))


# -- 토큰 엔드포인트 ----------------------------------------------------------------------------


def approve(c: TestClient, client_id: str, who: Person, **params) -> str:
    """허용해서 받은 인가 코드."""
    r = consent(c, "approve", handle_for(c, client_id, **params), who.headers)
    assert r.status_code == 200, r.text
    return split(r.json()["redirect_to"])[1]["code"]


def token(c: TestClient, **form: str | None) -> httpx.Response:
    """토큰 요청. None 인 인자는 빼고 폼으로 보낸다."""
    return c.post(TOKEN, data={k: v for k, v in form.items() if v is not None})


def exchange(c: TestClient, cid: str, code: str, **extra: str | None) -> httpx.Response:
    """코드 교환. extra 가 폼의 인자를 바꾸고 None 이면 뺀다."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": cid,
        "code_verifier": VERIFIER,
        "resource": GW_URL,
    }
    return token(c, **{**form, **extra})


def refresh(c: TestClient, cid: str, value: str, **extra: str | None) -> httpx.Response:
    form = {"grant_type": "refresh_token", "refresh_token": value, "client_id": cid}
    return token(c, **{**form, **extra})


def connect(c: TestClient, client_id: str, who: Person) -> dict:
    """허용하고 코드를 바꿔 받은 토큰 응답."""
    r = exchange(c, client_id, approve(c, client_id, who))
    assert r.status_code == 200, r.text
    return r.json()


def grant_of(mongo, body: dict) -> dict:
    """토큰 응답의 연결 문서. 접근 토큰의 sid 로 찾는다."""
    sid = jwt.decode(body["access_token"], options={"verify_signature": False})["sid"]
    return mongo["oauth_grants"].find_one({"_id": sid})


# -- 클라이언트 등록과 CIMD ---------------------------------------------------------------------


@functools.cache
def shared_signer() -> PlatformSigner:
    """RSA 키 만들기는 느리므로 서명 키를 가리지 않는 테스트는 하나를 나눠 쓴다."""
    return PlatformSigner.generate()


def oauth_service(
    signer: PlatformSigner | None = None, *, cimd_clients: tuple[str, ...] = (), **kw
) -> OAuthClients:
    """앱 없이 만든 등록 서비스. 서명 키를 주지 않으면 나눠 쓰는 키다."""
    issuer = InternalIssuer(issuer=PUBLIC_URL, human_issuer=PUBLIC_URL, mcp_audience="daegam-mcp")
    issuer.attach(signer or shared_signer())
    return OAuthClients(issuer, cimd_clients, **kw)


def resolve_error(svc: OAuthClients, client_id: str) -> OAuthError:
    with pytest.raises(OAuthError) as ei:
        asyncio.run(svc.resolve(client_id))
    return ei.value


CLAUDE, VSCODE, CODEX = OAUTH_CIMD_DEFAULT_CLIENTS
CLAUDE_DOC = {
    "client_id": CLAUDE,
    "client_name": "Claude Code",
    "client_uri": "https://claude.ai",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
PUBLIC_IP = "93.184.216.34"


class FakeWeb:
    """URL 마다의 응답과 받은 요청 수. 이름은 모두 공인 주소로 풀리고 `private` 만 사설로 풀린다."""

    def __init__(self, monkeypatch, **responses: httpx.Response):
        self.responses = dict(responses)
        self.private: set[str] = set()
        self.calls = 0
        transport = httpx.MockTransport(self.handle)
        fake = functools.partial(urlfetch.fetch_url, resolver=self.resolve, transport=transport)
        monkeypatch.setattr(urlfetch, "fetch_url", fake)

    def resolve(self, host: str) -> list[str]:
        return ["10.0.0.7"] if host in self.private else [PUBLIC_IP]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        url = f"https://{request.headers['host']}{request.url.path}"
        return self.responses.get(url) or httpx.Response(404)

    def serve(self, url: str, doc: object) -> None:
        self.responses[url] = httpx.Response(200, content=json.dumps(doc).encode())
