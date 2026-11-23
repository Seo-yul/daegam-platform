"""플랫폼이 entry 항목의 URL 을 직접 부를 때의 주소 규칙.

Tool 새로고침, Tools 탭, Gateway 배포의 도달 확인은 플랫폼 파드에서 그 URL 로 나간다. 플랫폼 파드는
서명 키와 저장소 자격 증명을 들고 있으므로, 등록한 사람이 적은 주소로 사설망을 읽는 길이 되면 안
된다. 그래서 웹 fetch 와 같은 규칙(`urlfetch.resolve_public`)으로 거른다.

- 클러스터 안 Service 주소(`parse_service_url`)는 그 네임스페이스가 항목 테넌트의 허용
  네임스페이스일 때만 부른다. 길은 지금처럼 클러스터 클라이언트가 정한다.
- 그 밖의 주소는 http(s) 이고 자격 증명이 없으며 풀린 주소가 모두 공인이어야 한다. 검사한 주소로 못
  박아 연결하고(`PinnedTransport`), 다른 호스트로 가는 리다이렉트는 따라가지 않는다.
- 사설 주소는 관리자가 연 것(`Settings.entry_private_allow`)만 부른다. 연 이름 접미사 아래의
  이름이라도 루프백과 링크로컬 주소(플랫폼 파드 자신과 메타데이터 주소)로 풀리면 부르지 않는다.
- Service 주소 꼴이 아닌 클러스터 안 이름(`IN_CLUSTER_SUFFIXES`)은 허용 목록과 상관없이 부르지
  않는다.

거절된 호출은 하지 않는다. 422 `entry_address_refused` 이고 `reason` 이 걸린 규칙의 코드다. vMCP 가
Entry 로 부르는 길은 여기를 지나지 않는다. 그 길은 테넌트 egress 정책 아래에 있다.
"""

from __future__ import annotations

import contextlib
import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from urllib.parse import urlsplit

import httpx2
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from daegam.config import EntryPrivateAllow
from daegam.errors import ServiceError
from daegam.k8s.client import parse_service_url
from daegam.mcp_client import ToolsResult, list_tools
from daegam.urlfetch import (
    BlockedURL,
    Resolver,
    public_detail,
    resolve_public_async,
    system_resolver,
)

ENTRY_ADDRESS_REFUSED = "entry_address_refused"
# 클러스터 안 Service 주소의 네임스페이스가 항목 테넌트의 것이 아니다. 그 밖의 reason 은
# `urlfetch.FETCH_ERRORS` 의 주소 규칙 코드다
SERVICE_NAMESPACE = "service_namespace"
SERVICE_NAMESPACE_TEXT = "the Service is not in one of the hub item's tenant's namespaces"

# 클러스터 안 이름의 접미사. 그 아래에서 Service 주소 꼴(`parse_service_url`)이 아닌 이름(헤드리스
# Service 의 파드, 파드 이름)은 네임스페이스 규칙으로 가릴 수 없다
IN_CLUSTER_SUFFIXES = (".svc", ".cluster.local")

Namespaces = Callable[[], Awaitable[Collection[str]]]


class EntryAddressRefused(ServiceError):
    """주소 규칙이 거절했다. `reason` 은 걸린 규칙의 코드다."""

    def __init__(self, reason: str, detail: str):
        super().__init__(422, ENTRY_ADDRESS_REFUSED, detail, reason=reason)
        self.reason = reason


class CheckedURL(str):
    """규칙을 지난 바깥 URL 과 그때 확인한 주소. 문자열로 쓰이고, 기본 조회기는 이 주소로 못 박아
    연결한다(`list_tools_pinned`)."""

    addresses: tuple[str, ...] = ()

    @classmethod
    def of(cls, url: str, addresses: Sequence[str]) -> CheckedURL:
        checked = cls(url)
        checked.addresses = tuple(addresses)
        return checked


class EntryAddresses:
    """entry 항목의 URL 을 불러도 되는지 정한다."""

    # 이름 풀기. 테스트가 DNS 를 가짜로 바꾸는 자리다
    resolver: Resolver = staticmethod(system_resolver)

    def __init__(self, allow: EntryPrivateAllow) -> None:
        self.allow = allow

    async def check(self, url: str, namespaces: Namespaces) -> str:
        """불러도 되는 URL 을 돌려준다. Service 주소는 그대로이고 바깥 주소는 `CheckedURL` 이다.
        namespaces 는 항목 테넌트의 허용 네임스페이스를 읽는다. 거절이면 `EntryAddressRefused`
        다."""
        service = parse_service_url(url)
        if service is not None:
            if service[0] not in await namespaces():
                raise EntryAddressRefused(SERVICE_NAMESPACE, SERVICE_NAMESPACE_TEXT)
            return url
        try:
            host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
            if host.endswith(IN_CLUSTER_SUFFIXES):
                raise BlockedURL("url_private", f"host {host!r} is an in-cluster name")
            checked, addresses = await resolve_public_async(
                url,
                resolver=self.resolver,
                allowed_suffixes=self.allow.suffixes,
                allowed_cidrs=self.allow.cidrs,
            )
            for address in addresses:
                if self._never_by_name(address):
                    raise BlockedURL("url_private", f"host {host!r} resolves to {address}")
        except BlockedURL as e:
            raise EntryAddressRefused(e.code, public_detail(e)) from e
        return CheckedURL.of(checked, addresses)

    def _never_by_name(self, address: str) -> bool:
        """이름 접미사를 열었다고 받지는 않는 주소인가. 루프백과 링크로컬이고, 그 주소를 범위로 연
        것은 받는다. 열지 않은 이름의 그런 주소는 `resolve_public` 이 이미 거절했다."""
        ip = ipaddress.ip_address(address)
        ip = getattr(ip, "ipv4_mapped", None) or ip
        local = ip.is_loopback or ip.is_link_local
        return local and not any(ip in net for net in self.allow.cidrs)


class PinnedTransport(httpx2.AsyncBaseTransport):
    """검사한 호스트로 가는 요청만, 검사한 주소로 보낸다. 이름을 그대로 두면 연결이 다시 풀어 그
    사이에 바뀐 DNS 답으로 사설 주소에 닿는다. `Host` 머리와 TLS 의 이름은 원래 호스트로 남는다.
    다른 호스트로 가는 요청(리다이렉트)은 보내지 않는다."""

    def __init__(
        self, host: str, addresses: Sequence[str], inner: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.host = host
        self.addresses = tuple(addresses)
        self.inner = inner or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host != self.host:
            raise httpx2.ConnectError("the request left the checked host", request=request)
        named = request.url
        request.extensions = {**request.extensions, "sni_hostname": self.host}
        last: Exception | None = None
        for address in self.addresses:
            request.url = named.copy_with(host=address)
            try:
                return await self.inner.handle_async_request(request)
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as e:
                last = e
        raise last or httpx2.ConnectError("no checked address", request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@contextlib.asynccontextmanager
async def pinned_client(
    url: CheckedURL, headers: Mapping[str, str] | None = None
) -> AsyncIterator[httpx2.AsyncClient]:
    """url 의 호스트를 확인한 주소로 못 박은 MCP 용 클라이언트. 타임아웃은 MCP 클라이언트의 기본값과
    같고, 리다이렉트는 MCP 전송이 같은 origin 안에서만 따라간다."""
    host = httpx2.URL(url).host
    async with httpx2.AsyncClient(
        headers=dict(headers) if headers else None,
        timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        transport=PinnedTransport(host, url.addresses),
    ) as client:
        yield client


async def list_tools_pinned(
    url: CheckedURL, *, timeout: float, headers: Mapping[str, str] | None = None
) -> ToolsResult:
    """확인한 주소로만 연결해 tools/list 를 부른다."""
    async with pinned_client(url, headers) as client:
        return await list_tools(url, timeout=timeout, http_client=client)
