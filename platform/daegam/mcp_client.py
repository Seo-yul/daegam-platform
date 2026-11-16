"""플랫폼 안의 최소 MCP 클라이언트 (mcp 2.x, streamable-http). 허브 refresh-tools 와 e2e 가 쓴다.

deploy/dev/lab/mcp_client.py 가 이것을 CLI 로 감싼다. `http_client` 를 주면 그 httpx2
클라이언트(인증 헤더·CA·클라이언트 인증서가 미리 실린 것)로 요청한다. 원격 클러스터의 Service 는
K8s API 서버의 services/proxy 를 지나야 하므로(D21) 호출부가 `ClusterClient.service_route` 로
만든 클라이언트를 넘긴다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

# 공개된 이름이 없다. mcp 의 클라이언트들(`mcp.client.sse`, `session_group`)도 같은 곳에서 가져와
# MCP 가 권하는 타임아웃과 리다이렉트 기본값을 맞춘다
from mcp.shared._httpx_utils import create_mcp_http_client

DEFAULT_TIMEOUT = 20.0


@dataclass
class ToolSchema:
    """도구 하나가 스스로 말하는 것. `tools/list` 응답에서 화면이 쓰는 것만 추린다.

    `tools` 가 이름만 담는 것을 그대로 둔 까닭은 그것을 이름 리스트로 읽는 자리가 열 곳을 넘고
    (lab CLI 가 그대로 `json.dumps` 하고 e2e 넷이 값을 단정한다) 그 계약을 깰 이유가 없어서다.
    """

    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: bool = False


@dataclass
class ToolsResult:
    server: str | None
    protocol_version: str | None
    tools: list[str] = field(default_factory=list)
    #: `tools` 와 같은 차례의 자세한 판. 이름만 쓰는 호출부는 이것을 보지 않는다
    tool_schemas: list[ToolSchema] = field(default_factory=list)


@dataclass
class CallResult:
    tool: str
    is_error: bool
    content: list[str | None]
    structured: Any


class AuthRefused(RuntimeError):
    """서버가 401 이나 403 으로 답했다. MCP 클라이언트는 상태 코드를 내주지 않으므로 응답 훅이 잰다
    (`refusals`). 헤더 값이 틀렸는지 가르는 자리다 (P86 6.3절)."""

    def __init__(self, status: int):
        super().__init__(f"the server answered HTTP {status}")
        self.status = status


def _k8s_status(body: bytes) -> bool:
    """Kubernetes API 서버가 낸 `Status` 본문인가. API 서버 프록시 경로의 401·403 은 API 서버 자신의
    거절일 수 있다."""
    try:
        doc = json.loads(body or b"null")
    except ValueError:
        return False
    return isinstance(doc, dict) and doc.get("kind") == "Status" and "apiVersion" in doc


def refusals(client: httpx2.AsyncClient) -> list[int]:
    """client 의 응답 가운데 서버가 낸 401·403 의 상태 코드를 모을 목록. 응답 훅을 하나 더한다.
    Kubernetes API 서버가 낸 거절(`Status` 본문)은 세지 않는다. 서버가 헤더를 거절한 것이 아니기
    때문이다."""
    seen: list[int] = []

    async def hook(response: httpx2.Response) -> None:
        if response.status_code in (401, 403) and not _k8s_status(await response.aread()):
            seen.append(response.status_code)

    hooks = client.event_hooks
    client.event_hooks = {**hooks, "response": [*hooks.get("response", []), hook]}
    return seen


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _schema_of(t: Any) -> ToolSchema:
    """MCP `types.Tool` 에서 화면이 쓰는 것만 옮긴다. 낡은 서버가 카멜케이스로 줄 수 있어
    `_attr` 로 두 이름을 다 본다.

    `annotations` 의 `readOnlyHint`·`destructiveHint` 는 **일부러 옮기지 않는다.** MCP 명세가
    "Clients should never make tool use decisions based on ToolAnnotations received from untrusted
    servers" 라고 못박는다. `remote`·`entry`·`image` 항목은 바로 그 믿을 수 없는 남의 서버이고,
    이 값이 닿는 곳은 무엇을 Gateway 로 열어 줄지 정하는 화면이다. 힌트를 보증으로 보이면 안 된다.
    `annotations.title` 은 표시 이름 폴백이라 그것만 읽는다. 명세가 정한 차례가
    title, annotations.title, name 이다.
    """
    ann = getattr(t, "annotations", None)
    return ToolSchema(
        name=t.name,
        title=getattr(t, "title", None) or (getattr(ann, "title", None) if ann else None),
        description=getattr(t, "description", None),
        input_schema=_attr(t, "input_schema", "inputSchema"),
        output_schema=_attr(t, "output_schema", "outputSchema") is not None,
    )


async def list_tools(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    http_client: httpx2.AsyncClient | None = None,
    headers: Mapping[str, str] | None = None,
) -> ToolsResult:
    """initialize 한 뒤 tools/list 를 next_cursor 가 끝날 때까지 모은다. headers 는 http_client 가
    없을 때 만드는 클라이언트에 싣는다. 헤더를 실은 클라이언트는 리다이렉트를 따라가지 않는다.
    헤더가 항목의 URL 밖으로 나가지 않게 하려는 것이다 (P86 6.5절). 서버가 401·403 으로 답해
    실패하면 `AuthRefused` 다."""
    own = http_client is None
    client = http_client or create_mcp_http_client(headers=dict(headers) if headers else None)
    if own and headers:
        client.follow_redirects = False
    refused = refusals(client)

    async def _run() -> ToolsResult:
        async with streamable_http_client(url, http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                info = _attr(init, "server_info", "serverInfo")
                out = ToolsResult(
                    server=getattr(info, "name", None),
                    protocol_version=_attr(init, "protocol_version", "protocolVersion"),
                )
                cursor: str | None = None
                seen: set[str] = set()
                while True:
                    params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
                    res = await session.list_tools(params=params)
                    for t in res.tools:
                        if t.name not in seen:
                            seen.add(t.name)
                            out.tools.append(t.name)
                            out.tool_schemas.append(_schema_of(t))
                    cursor = _attr(res, "next_cursor", "nextCursor")
                    if not cursor:
                        break
                return out

    try:
        if own:
            async with client:
                return await asyncio.wait_for(_run(), timeout)
        return await asyncio.wait_for(_run(), timeout)
    except Exception as e:
        if refused:
            raise AuthRefused(refused[0]) from e
        raise


async def server_experimental(
    url: str, *, timeout: float = DEFAULT_TIMEOUT, http_client: httpx2.AsyncClient | None = None
) -> dict[str, Any] | None:
    """initialize 만 하고 답의 `capabilities.experimental` 을 낸다. 없으면 None 이다. 서버가 인자
    조건을 지키는지 읽는 기록이 쓴다 (P52 4절, `services/condition_records.py`)."""

    async def _run() -> dict[str, Any] | None:
        async with streamable_http_client(url, http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                experimental = getattr(init.capabilities, "experimental", None)
                return dict(experimental) if isinstance(experimental, dict) else None

    return await asyncio.wait_for(_run(), timeout)


async def call_tool(
    url: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    http_client: httpx2.AsyncClient | None = None,
) -> CallResult:
    async def _run() -> CallResult:
        async with streamable_http_client(url, http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool, arguments)
                return CallResult(
                    tool=tool,
                    is_error=bool(_attr(res, "is_error", "isError", default=False)),
                    content=[getattr(c, "text", None) for c in res.content],
                    structured=_attr(res, "structured_content", "structuredContent"),
                )

    return await asyncio.wait_for(_run(), timeout)
