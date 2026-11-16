"""MCP 클라이언트처럼 OAuth 로 Gateway 에 붙는 스크립트 클라이언트 (P33 F6).
e2e_oauth 가 쓰고 사람도 쓴다.

실제 클라이언트(Claude Code, VS Code, Cursor)가 하는 일을 명령 하나씩으로
나눴다. 토큰 없는 호출의 401 에서 PRM 과 인가 서버 메타데이터를 찾고(discover),
DCR 로 등록하거나 CIMD client_id 를 쓰고(register·cimd), PKCE S256 으로 인가를
요청한 뒤(authorize), 사람 대신 API 로 로그인해 동의 화면이 부르는 호출을
하고(inspect·approve·deny), 코드를 바꾸고(token), Gateway 를 부르고(call),
갱신하고(refresh), 회수한다(revoke). loopback 에 서버를 띄우지 않고 허용 응답의
redirect_to 쿼리를 읽는다.

명령은 상태 파일(--state)에서 앞 명령이 남긴 것을 읽고 제 몫을 적는다.
코드·토큰·요청 핸들 같은 비밀은 그 파일에만 있고 stdout 에는 판정에 쓰는 요약
JSON 한 객체만 찍는다. 요약의 `status` 는 HTTP 상태이고 `error` 는 OAuth 의
error 나 플랫폼 오류의 detail.code 다. 답을 받았으면 상태가 무엇이든 0 으로
끝난다. 닿지 못했거나, 앞 명령이 남길 것이 없거나, discover·call 이 끝까지 가지
못하면 1 이다.

--resolve HOST:PORT:IP 는 curl 과 같은 뜻이고 여러 번 준다. /etc/hosts 없이
orbstack 의 플랫폼 호스트와 노출 호스트를 Traefik IP 로 푼다. 연결만 그 IP 로
가고 Host 헤더와 TLS SNI 는 원래 이름이다(daegam/urlfetch.py 가 검사한 IP 로
못 박는 것과 같은 수다). TLS 검증은 httpx2 기본이고 SSL_CERT_FILE 을 따른다.

인가 서버 쪽 규칙(PKCE, loopback 돌아갈 곳, 메타데이터 주소)은 플랫폼 코드를
빌리지 않고 명세대로 다시 짰다. 클라이언트가 서버와 함께 틀리면 e2e 가 그것을
잡지 못한다. MCP 호출만 platform/daegam/mcp_client.py 의 list_tools·call_tool 에
이 httpx2 클라이언트를 넘긴다(deploy/dev/lab/mcp_client.py 와 같다).

실행: uv run --project platform python deploy/dev/lab/mcp_oauth_client.py
        [--resolve H:P:IP]... --state FILE COMMAND ...
  discover URL                  URL 은 Gateway 나 노출 MCP Server 의 공개 주소다
  register [--redirect-uri U]... [--name N]
                                DCR. 기본 돌아갈 곳은 포트 없는 127.0.0.1 이다
  cimd URL                      CIMD client_id. 문서의 loopback 주소 첫째를 쓴다
  authorize [--client-id C] [--redirect-uri U] [--resource R | --no-resource]
  inspect|approve|deny --user U 비밀번호는 환경변수 MCP_OAUTH_PASSWORD 다. oidc 모드는
                                로그인하지 않고 사람의 IdP 액세스 토큰(MCP_OAUTH_BEARER)과
                                최근 로그인의 ID 토큰(MCP_OAUTH_ID_TOKEN)을 싣는다 (P53b 6.4절)
  token [--verifier V] [--resource R]
  refresh [--previous] [--client-id C] [--scope S] [--resource R]
  revoke [--access] [--client-id C]
                                기본은 갱신 토큰이다
  call --tool SUFFIX (--args JSON | --fill PATTERN=VALUE ...)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx2
from mcp.types import LATEST_PROTOCOL_VERSION
from tool_fill import fill_args

from daegam.mcp_client import call_tool, list_tools

PASSWORD_ENV = "MCP_OAUTH_PASSWORD"
# oidc 모드의 동의(P53b 6.4절). 사람의 IdP 액세스 토큰(aud 는 제어면)이 있으면 로그인하지 않고, ID
# 토큰이 있으면 최근 로그인의 증명으로 머리에 싣는다
BEARER_ENV = "MCP_OAUTH_BEARER"
ID_TOKEN_ENV = "MCP_OAUTH_ID_TOKEN"
ID_TOKEN_HEADER = "X-Daegam-Id-Token"
CLIENT_NAME = "daegam e2e-oauth"
DEFAULT_REDIRECT_URI = "http://127.0.0.1/callback"
# Claude Code 처럼 갱신 토큰을 청한다
DEFAULT_SCOPE = "mcp offline_access"
GRANT_TYPES = ["authorization_code", "refresh_token"]
TIMEOUT_SECONDS = 30.0
# RFC 8414 3.1. 발급자 경로 앞에 끼운다
AS_WELL_KNOWN = "/.well-known/oauth-authorization-server"
# 동의 화면(UI)이 부르는 API. 동의 화면은 인가 엔드포인트와 같은 origin 이다
LOGIN_PATH = "/api/auth/login"
CONSENT_PATH = "/api/oauth/consent"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# 등록한 돌아갈 곳에 포트가 없으면 Claude Code 처럼 임의 포트로 온다 (RFC 8252 7.3)
EPHEMERAL_PORTS = (49152, 65535)
DEFAULT_PORTS = {"http": 80, "https": 443}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
# 토큰 없는 initialize. 인증이 본문보다 먼저라 401 과 WWW-Authenticate 만 본다
PROBE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": "0"},
    },
}
_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')
# 요약에 싣지 않는 토큰 응답의 칸. 값 대신 있었는지만 싣는다
_SECRET_FIELDS = ("access_token", "refresh_token")


class CliError(Exception):
    """닿지 못했거나 앞 명령이 남길 것이 없다. stderr 에 찍고 1 로 끝난다."""


class PinnedTransport(httpx2.AsyncBaseTransport):
    """curl --resolve. 표에 있는 (host, port) 는 그 IP 로 연결하고 Host 헤더와
    TLS SNI 는 원래 이름을 둔다. Host 헤더는 요청을 만들 때 이미 정해졌다."""

    def __init__(self, pins: dict[tuple[str, int], str]):
        self.pins = pins
        self.inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        ip = self.pins.get((url.host, url.port or DEFAULT_PORTS[url.scheme]))
        if ip is not None:
            request.extensions = {**request.extensions, "sni_hostname": url.host}
            request.url = url.copy_with(host=ip)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def parse_pin(value: str) -> tuple[tuple[str, int], str]:
    """HOST:PORT:IP. IPv6 는 [::1] 처럼 대괄호로 적는다."""
    host, port, ip = value.split(":", 2)
    return (host.lower(), int(port)), ip.strip("[]")


@dataclass
class Ctx:
    """명령 하나가 보는 것. state 는 상태 파일의 내용이고 명령이 고친 것을 main 이 다시 적는다."""

    state: dict[str, Any]
    pins: dict[tuple[str, int], str] = field(default_factory=dict)

    def client(self, token: str | None = None) -> httpx2.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx2.AsyncClient(
            transport=PinnedTransport(self.pins), headers=headers, timeout=TIMEOUT_SECONDS
        )

    def need(self, key: str, command: str) -> Any:
        value = self.state.get(key)
        if value is None:
            raise CliError(f"the state has no {key}; run {command} first")
        return value

    def endpoint(self, name: str) -> str:
        url = self.need("metadata", "discover").get(name)
        if not url:
            raise CliError(f"the authorization server metadata has no {name}")
        return url


# -- 작은 도우미 ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce() -> tuple[str, str]:
    """(verifier, S256 challenge). verifier 는 32바이트 난수의 base64url 43자다 (RFC 7636)."""
    verifier = _b64(secrets.token_bytes(32))
    return verifier, _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def _is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS


def _with_port(uri: str) -> str:
    """포트 없는 loopback 돌아갈 곳에 임의 포트를 붙인다. 포트가 있으면 그대로다."""
    parts = urlsplit(uri)
    if parts.port is not None:
        return uri
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    port = secrets.choice(range(*EPHEMERAL_PORTS))
    return urlunsplit(parts._replace(netloc=f"{host}:{port}"))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _metadata_url(issuer: str) -> str:
    parts = urlsplit(issuer)
    return f"{_origin(issuer)}{AS_WELL_KNOWN}{parts.path.rstrip('/')}"


def _json(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:200]} if response.content else None


def _error_of(body: Any) -> str | None:
    """OAuth 의 error, 아니면 플랫폼 오류의 detail.code."""
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    return body.get("error") or (detail.get("code") if isinstance(detail, dict) else None)


def _answer(response: httpx2.Response, body: Any = None) -> dict[str, Any]:
    body = _json(response) if body is None else body
    return {"status": response.status_code, "error": _error_of(body), "body": body}


def _landing(url: str, oauth_state: str | None) -> dict[str, Any]:
    """브라우저가 닿는 곳의 요약. 코드와 요청 핸들은 있는지만 싣는다."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    return {
        "host": parts.hostname,
        "port": parts.port,
        "path": parts.path,
        "error": query.get("error"),
        "iss": query.get("iss"),
        "state_ok": query.get("state") == oauth_state,
        "code": "code" in query,
        "request": "request" in query,
    }


def _filled(schema: dict[str, Any] | None, rules: list[str]) -> dict[str, Any]:
    """도구 인자를 이름으로 채운다. 규칙은 bash 판정과 같은 tool_fill.py 하나다."""
    try:
        return fill_args(schema, rules)
    except LookupError as e:
        raise CliError(str(e)) from e


async def _get_json(http: httpx2.AsyncClient, url: str) -> Any:
    response = await http.get(url)
    if response.status_code != 200:
        raise CliError(f"GET {url} -> {response.status_code}")
    return _json(response)


# -- 명령 ----------------------------------------------------------------------------------


async def discover(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """401 의 resource_metadata 로 PRM 을, 그 authorization_servers 첫째로 인가 서버
    메타데이터를 읽는다."""
    async with ctx.client() as http:
        probe = await http.post(a.url, json=PROBE, headers=MCP_HEADERS)
        header = probe.headers.get("www-authenticate", "")
        challenge = dict(_CHALLENGE_PARAM.findall(header))
        out: dict[str, Any] = {
            "status": probe.status_code,
            "www_authenticate": header,
            "challenge": challenge,
            "ok": False,
        }
        if probe.status_code != 401 or "resource_metadata" not in challenge:
            return out
        out["prm"] = prm = await _get_json(http, challenge["resource_metadata"])
        issuer = (prm.get("authorization_servers") or [None])[0]
        if not issuer:
            return out
        out["metadata"] = metadata = await _get_json(http, _metadata_url(issuer))
    ctx.state.update(resource=prm.get("resource"), issuer=issuer, metadata=metadata)
    return {**out, "ok": True}


async def register(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """DCR (RFC 7591). 등록이 돌려준 돌아갈 곳 첫째를 인가 요청에 쓴다."""
    body = {
        "client_name": a.name,
        "redirect_uris": a.redirect_uri or [DEFAULT_REDIRECT_URI],
        "grant_types": GRANT_TYPES,
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with ctx.client() as http:
        response = await http.post(ctx.endpoint("registration_endpoint"), json=body)
    out = _answer(response)
    if response.status_code == 201:
        registered = out["body"]
        ctx.state.update(
            client_id=registered["client_id"], redirect_uri=registered["redirect_uris"][0]
        )
    return out


async def cimd(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """CIMD client_id 를 쓴다. 문서를 받아 loopback 돌아갈 곳 첫째를 고른다."""
    async with ctx.client() as http:
        doc = await _get_json(http, a.url)
    uris = (doc.get("redirect_uris") if isinstance(doc, dict) else None) or []
    loopback = [u for u in uris if isinstance(u, str) and _is_loopback(u)]
    if not loopback:
        raise CliError(f"{a.url} lists no loopback redirect_uri: {uris}")
    ctx.state.update(client_id=a.url, redirect_uri=loopback[0])
    return {"client_id": a.url, "redirect_uris": uris, "redirect_uri": loopback[0]}


async def authorize(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """PKCE S256 인가 요청. 따라가지 않고 302 의 Location 을 읽는다. 동의 화면으로
    가면 요청 핸들을 남긴다."""
    verifier, challenge = _pkce()
    oauth_state = secrets.token_urlsafe(16)
    redirect_uri = a.redirect_uri or _with_port(ctx.need("redirect_uri", "register or cimd"))
    params = [
        ("response_type", "code"),
        ("client_id", a.client_id or ctx.need("client_id", "register or cimd")),
        ("redirect_uri", redirect_uri),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("scope", DEFAULT_SCOPE),
        ("state", oauth_state),
    ]
    if not a.no_resource:
        params.append(("resource", a.resource or ctx.need("resource", "discover")))
    endpoint = ctx.endpoint("authorization_endpoint")
    async with ctx.client() as http:
        response = await http.get(endpoint, params=params)
    target = urljoin(endpoint, response.headers.get("location", ""))
    ctx.state.update(
        verifier=verifier,
        oauth_state=oauth_state,
        request_redirect_uri=redirect_uri,
        request=dict(parse_qsl(urlsplit(target).query)).get("request"),
        code=None,
    )
    landing = _landing(target, oauth_state)
    return {"status": response.status_code, "error": landing["error"], "landing": landing}


async def _login(http: httpx2.AsyncClient, origin: str, user: str) -> str:
    """사람의 새 제어면 토큰. 허용은 최근 10분 안의 로그인을 요구하므로 부를 때마다 로그인한다."""
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise CliError(f"{PASSWORD_ENV} is empty")
    response = await http.post(
        f"{origin}{LOGIN_PATH}", json={"username": user, "password": password}
    )
    body = _json(response)
    if response.status_code != 200:
        raise CliError(f"login as {user} -> {response.status_code} {_error_of(body)}")
    if body.get("must_change_password"):
        raise CliError(f"{user} must change the password before approving")
    return body["access_token"]


async def consent(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """동의 화면이 부르는 inspect·approve·deny. 허용이면 redirect_to 의 코드를 남긴다."""
    origin = _origin(ctx.endpoint("authorization_endpoint"))
    handle = ctx.need("request", "authorize")
    async with ctx.client() as http:
        token = os.environ.get(BEARER_ENV) or await _login(http, origin, a.user)
        headers = {"Authorization": f"Bearer {token}"}
        id_token = os.environ.get(ID_TOKEN_ENV)
        if id_token:
            headers[ID_TOKEN_HEADER] = id_token
        response = await http.post(
            f"{origin}{CONSENT_PATH}/{a.command}", json={"request": handle}, headers=headers
        )
    body = _json(response)
    redirect_to = body.pop("redirect_to", None) if isinstance(body, dict) else None
    if redirect_to:
        body["redirect"] = _landing(redirect_to, ctx.state.get("oauth_state"))
        ctx.state["code"] = dict(parse_qsl(urlsplit(redirect_to).query)).get("code")
    return _answer(response, body)


async def _token_request(ctx: Ctx, form: dict[str, str]) -> dict[str, Any]:
    """토큰 엔드포인트의 폼 요청. 받은 토큰은 상태 파일에만 두고 요약에는 있었는지만 싣는다."""
    async with ctx.client() as http:
        response = await http.post(ctx.endpoint("token_endpoint"), data=form)
    body = _json(response)
    if response.status_code == 200:
        ctx.state.update({k: body[k] for k in _SECRET_FIELDS if k in body})
    if isinstance(body, dict):
        body = {k: (k in body) if k in _SECRET_FIELDS else v for k, v in body.items()}
    return {**_answer(response, body), "cache_control": response.headers.get("cache-control")}


async def token(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """코드 교환. 코드는 상태 파일에 남으므로 다시 부르면 재사용이다."""
    form = {
        "grant_type": "authorization_code",
        "code": ctx.need("code", "approve"),
        "redirect_uri": ctx.need("request_redirect_uri", "authorize"),
        "client_id": ctx.need("client_id", "register or cimd"),
        "code_verifier": a.verifier or ctx.need("verifier", "authorize"),
        "resource": a.resource or ctx.need("resource", "discover"),
    }
    return await _token_request(ctx, form)


async def refresh(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """갱신. --previous 는 바로 앞 세대의 갱신 토큰을 다시 낸다(재사용)."""
    key = "previous_refresh_token" if a.previous else "refresh_token"
    sent = ctx.need(key, "refresh" if a.previous else "token")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": sent,
        "client_id": a.client_id or ctx.need("client_id", "register or cimd"),
        "resource": a.resource or ctx.need("resource", "discover"),
    }
    if a.scope:
        form["scope"] = a.scope
    out = await _token_request(ctx, form)
    if out["status"] == 200:
        ctx.state["previous_refresh_token"] = sent
    return out


async def revoke(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """RFC 7009. 모르는 토큰도 200 이고 본문이 없다."""
    kind = "access_token" if a.access else "refresh_token"
    form = {
        "token": ctx.need(kind, "token"),
        "token_type_hint": kind,
        "client_id": a.client_id or ctx.need("client_id", "register or cimd"),
    }
    async with ctx.client() as http:
        response = await http.post(ctx.endpoint("revocation_endpoint"), data=form)
    return _answer(response)


async def call(ctx: Ctx, a: argparse.Namespace) -> dict[str, Any]:
    """접근 토큰으로 리소스(PRM 의 resource)의 tools/list 와, 이름이 --tool 로
    끝나는 도구의 tools/call."""
    url = ctx.need("resource", "discover")
    async with ctx.client(ctx.need("access_token", "token")) as http:
        listed = await list_tools(url, http_client=http)
        found = next((t for t in listed.tool_schemas if t.name.endswith(a.tool)), None)
        if found is None:
            return {"ok": False, "tools": listed.tools}
        args = json.loads(a.args) if a.args else _filled(found.input_schema, a.fill)
        result = await call_tool(url, found.name, args, http_client=http)
    outcome = {"tool": found.name, "arguments": args, "isError": result.is_error}
    outcome["content"] = result.content
    return {"ok": not result.is_error, "tools": listed.tools, "call": outcome}


Command = Callable[[Ctx, argparse.Namespace], Awaitable[dict[str, Any]]]


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--resolve", action="append", default=[], metavar="HOST:PORT:IP")
    ap.add_argument("--state", required=True, type=Path)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("discover").add_argument("url")
    p = sub.add_parser("register")
    p.add_argument("--redirect-uri", action="append")
    p.add_argument("--name", default=CLIENT_NAME)
    sub.add_parser("cimd").add_argument("url")
    p = sub.add_parser("authorize")
    p.add_argument("--client-id")
    p.add_argument("--redirect-uri")
    p.add_argument("--resource")
    p.add_argument("--no-resource", action="store_true")
    for name in ("inspect", "approve", "deny"):
        sub.add_parser(name).add_argument("--user", required=True)
    p = sub.add_parser("token")
    p.add_argument("--verifier")
    p.add_argument("--resource")
    p = sub.add_parser("refresh")
    p.add_argument("--previous", action="store_true")
    p.add_argument("--client-id")
    p.add_argument("--scope")
    p.add_argument("--resource")
    p = sub.add_parser("revoke")
    p.add_argument("--access", action="store_true")
    p.add_argument("--client-id")
    p = sub.add_parser("call")
    p.add_argument("--tool", required=True)
    p.add_argument("--args")
    p.add_argument("--fill", action="append", default=[], metavar="PATTERN=VALUE")
    return ap


COMMANDS: dict[str, Command] = {
    "discover": discover,
    "register": register,
    "cimd": cimd,
    "authorize": authorize,
    "inspect": consent,
    "approve": consent,
    "deny": consent,
    "token": token,
    "refresh": refresh,
    "revoke": revoke,
    "call": call,
}


def main(argv: list[str]) -> int:
    a = _parser().parse_args(argv[1:])
    state = json.loads(a.state.read_text()) if a.state.exists() else {}
    ctx = Ctx(state, dict(parse_pin(p) for p in a.resolve))
    try:
        out = asyncio.run(COMMANDS[a.command](ctx, a))
    except (CliError, httpx2.HTTPError) as e:
        print(f"{a.command}: {e}", file=sys.stderr)
        return 1
    finally:
        a.state.write_text(json.dumps(ctx.state))
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
