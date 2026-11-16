"""내부 MCP 홉의 서비스 토큰과 포트포워드 브리지를 보는 테스트가 함께 쓰는 도우미 (D41).

`make_svc_tokens` 는 앱 없이 만든 발급 창구다. `header_echo_server` 는 터널 너머의 파드 자리에 서서
받은 요청 줄과 헤더를 JSON 으로 되돌려 주는 TCP 서버다. `entry_token_secret` 은 Gateway 응답에서
Entry 하나의 토큰 Secret 이름을 짓는다 (P50 9절). `claims_of`·`aud_of` 는 토큰을 서명 없이 읽고,
`plant_key` 는 fake 클러스터의 Secret 키 하나를 바꿔 둔다.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import jwt

from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import PlatformSigner
from daegam.config import Settings
from daegam.k8s.templates.svc_token import entry_token_objects
from daegam.services.svc_tokens import SvcTokens
from daegam.store.tenants import TenantSettings
from tests.helpers.consts import DEFAULT_NS, ISSUER
from tests.helpers.fake_cluster import FakeClusterClient


class _TenantSettingsStub:
    def __init__(self, group: str | None = None):
        self.group = group

    async def get(self, tenant: str) -> TenantSettings:
        return TenantSettings(tenant=tenant, mcp_group=self.group)


def make_svc_tokens(group: str | None = None, settings: Settings | None = None) -> SvcTokens:
    """저장된 mcp_group 이 group 인 창구. 설정을 주지 않으면 그 값을 그대로 쓰는 oidc 모드다."""
    issuer = InternalIssuer(issuer=ISSUER, human_issuer=ISSUER, mcp_audience="daegam-mcp")
    issuer.attach(PlatformSigner.generate())
    stub = _TenantSettingsStub(group)
    return SvcTokens(issuer, stub, settings or Settings())  # type: ignore[arg-type]


def entry_token_secret(gateway: dict, index: int = 0) -> str:
    """Gateway 응답(배포된 것)에서 토큰을 받는 index 번째 Entry 의 토큰 Secret 이름."""
    backends = [SimpleNamespace(**b) for b in gateway["deployed_backends"]]
    return list(entry_token_objects(gateway["k8s"]["name"], backends).values())[index]


def entry_object_name(k8s_name: str, workload: str) -> str:
    """vMCP k8s_name 이 토큰을 받는 Entry workload 에 쓰는 토큰 객체 이름."""
    backend = SimpleNamespace(kind="internal", workload=workload)
    return entry_token_objects(k8s_name, [backend])[workload]


def claims_of(token: str) -> dict[str, Any]:
    """토큰의 클레임. 서명은 보지 않는다."""
    return jwt.decode(token, options={"verify_signature": False})


def aud_of(token: str) -> Any:
    """토큰의 aud. 하나면 글이고 여럿이면 목록이다."""
    return claims_of(token)["aud"]


def plant_key(
    fake: FakeClusterClient, name: str, key: str, token: str, namespace: str = DEFAULT_NS
) -> None:
    """Secret name 의 키 key 를 token 으로 바꿔 둔다. 노출 앞단·Entry 토큰(key `token`)이면 Bearer
    값도 맞춘다."""

    def edit(secret: dict[str, Any]) -> None:
        secret["stringData"][key] = token
        if key == "token":
            secret["stringData"]["authorization"] = f"Bearer {token}"

    fake.edit_live("v1", "Secret", name, namespace, edit)


async def header_echo_server() -> tuple[asyncio.AbstractServer, int]:
    """(서버, 포트). 연결마다 요청 하나를 읽고 닫는다."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = json.dumps({"headers": headers, "request": lines[0]}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    return server, server.sockets[0].getsockname()[1]
