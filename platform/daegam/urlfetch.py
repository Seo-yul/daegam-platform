"""공개 URL 만 가져오는 제한된 fetch 와 SSRF 차단.

스펙 생성기의 `input.url`, 비서의 `fetch_url` 도구, Knowledge 인덱서의 URL 문서가 같이 쓴다.

플랫폼 파드가 클러스터 안에서 임의 URL 을 가져오게 되므로 RFC1918·링크로컬·루프백·메타데이터
(169.254.169.254)·`.cluster.local`·`.svc`·`.internal` 을 기본 거부한다(ToolHive
`allowPrivateEndpoint` 와 같은 규칙). 크기 1 MiB·10초·리다이렉트 3회 상한이고, 리다이렉트
대상마다 같은 검사를 다시 한다. DNS 는 검사 시점에 풀어 모든 주소가 공인이어야 한다.

검사한 주소로 못 박아 연결한다. 이름만 검사하고 연결은 다시 풀게 두면, 그 사이에 DNS 응답이
바뀌어 사설 주소로 가는 재바인딩으로 이 모듈 전체가 무력해진다(TOCTOU). 그래서 요청 URL 의
host 를 통과한 IP 로 바꾸고 `Host` 헤더와 TLS `sni_hostname` 에 원래 이름을 남긴다. 인증서
검증은 `sni_hostname` 기준이라 그대로다.

주소 판정은 이 파일의 명시 표만 쓴다(P39b 11절). `ipaddress` 의 플래그 표는 인터프리터 패치
판마다 바뀔 수 있어 두 구성요소(플랫폼과 web-tools 의 바이트 사본)가 다르게 판정할 수 있기
때문이다. 바깥 주소와 그 안에 실린 모든 IPv4(매핑, NAT64, 6to4, Teredo)가 표를 지나야 한다.

압축은 받은 날 바이트(`aiter_raw`)를 남은 상한만큼만 푼다. httpx 가 조각마다 상한 없이 풀게
두면 작은 압축 조각 하나가 상한을 훨씬 넘게 부푼 뒤에야 셀 수 있다. 그래서 gzip 과 deflate 만
청하고 그 밖의 인코딩은 거부한다.

이 파일은 `web-tools/webtools/urlfetch.py` 에 바이트 그대로 복사된다(`tests/test_urlfetch_copy.py`).
`daegam` 을 import 하지 않는다.
"""

from __future__ import annotations

import asyncio
import functools
import html
import ipaddress
import math
import re
import socket
import ssl
import zlib
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import anyio.to_thread
import certifi
import httpx
import idna

MAX_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
# `fetch_url` 호출 전체의 기본 시한(초). 주소가 여럿이면 연결을 차례로 해 보므로 시한이 없으면 주소
# 수와 홉 수만큼 timeout 이 쌓인다
DEADLINE_SECONDS = 60.0

BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal"})
BLOCKED_SUFFIXES = (".localhost", ".cluster.local", ".svc", ".internal", ".local")

Resolver = Callable[[str], list[str]]

# 이름 풀기에만 쓰는 스레드 상한 (P69). 공용 상한(run_in_threadpool, 40)과 나눠 두어야 막힌 이름
# 풀기가 K8s 호출과 Secret 읽기의 자리를 차지하지 않는다. 정상 조회는 수 밀리초라 8 이면 비서,
# Knowledge, OAuth 의 동시 fetch 를 넉넉히 받고, 시한 안에서 막힌 조회가 붙잡는 자리는 8 개다
RESOLVE_THREADS = 8
_resolve_limiter: anyio.CapacityLimiter | None = None


def _limiter() -> anyio.CapacityLimiter:
    """이름 풀기 전용 상한. 처음 쓸 때 만든다."""
    global _resolve_limiter
    if _resolve_limiter is None:
        _resolve_limiter = anyio.CapacityLimiter(RESOLVE_THREADS)
    return _resolve_limiter


class BlockedURL(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class FetchError(Exception):
    def __init__(self, code: str, detail: str, *, status: int | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


FETCH_ERRORS: dict[str, str] = {
    "url_scheme": "the URL must be http or https",
    "url_host": "the URL has no host",
    "url_userinfo": "the URL must not carry credentials",
    "url_port": "the URL's port is not allowed",
    "url_private": "the address is private or link-local, not a public one",
    "url_blocked_host": "the host is blocked by the platform",
    "url_unresolvable": "the host does not resolve",
    "fetch_redirect": "the server redirected too many times or to a blocked address",
    "fetch_too_large": "the body is larger than the upload limit",
    "fetch_timeout": "the server did not finish answering in time",
    "fetch_failed": "the server did not answer",
    "fetch_status": "the server answered with an error status",
    "unsupported_content_type": "the content type is not one this tool reads",
    "unsupported_content_encoding": "the server used a compression this tool does not read",
}
"""오류 코드마다 우리가 쓴 문장. BlockedURL·FetchError 의 detail 은 남의 서버가 정한 문자열
(리다이렉트 Location 등)을 품어 모델 문맥이나 저장 문서에 넣을 수 없다. 비서 fetch_url 과
Knowledge 인덱서가 같은 표를 쓴다. 키는 이 파일이 실제로 내는 코드다."""


def public_detail(e: BlockedURL | FetchError) -> str:
    """저장·응답에 실어도 되는 한 줄. fetch_status 는 HTTP 상태 번호를 붙인다."""
    text = FETCH_ERRORS.get(e.code, "the fetch failed")
    status = getattr(e, "status", None)
    return f"{text} (HTTP {status})" if status else text


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# 막는 IPv4 (P39b 11절). 0/8, 사설, CGNAT, 루프백, 링크로컬, 프로토콜 할당, 문서용, 6to4 릴레이,
# 벤치마크, 멀티캐스트와 그 위 예약·브로드캐스트다
BLOCKED_V4: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/3",
    )
)
# IPv6 는 전역 유니캐스트만 받고 그 안에서 Teredo 를 품은 2001::/23, 문서용, 6to4, 3fff::/20 을
# 막는다. 그래서 NAT64, IPv4 호환, ULA, 링크로컬, 멀티캐스트는 바깥 주소에서 막힌다
GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")
BLOCKED_V6: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.IPv6Network(n) for n in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)
MAPPED_V6 = ipaddress.IPv6Network("::ffff:0:0/96")
NAT64_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)
SIX_TO_FOUR_V6 = ipaddress.IPv6Network("2002::/16")
TEREDO_V6 = ipaddress.IPv6Network("2001::/32")


def _v4(bits: int) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(bits & 0xFFFFFFFF)


def embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv6 주소에 실린 IPv4. 매핑과 두 NAT64 접두는 마지막 32비트, 6to4 는 둘째와 셋째 16비트
    묶음, Teredo 는 서버(32~63비트)와 클라이언트(마지막 32비트를 뒤집은 것)다."""
    n = int(ip)
    if ip in MAPPED_V6 or any(ip in net for net in NAT64_V6):
        return [_v4(n)]
    if ip in SIX_TO_FOUR_V6:
        return [_v4(n >> 80)]
    if ip in TEREDO_V6:
        return [_v4(n >> 64), _v4(~n)]
    return []


def _v4_public(ip: ipaddress.IPv4Address, extra: Iterable[IPNetwork]) -> bool:
    return not any(ip in net for net in BLOCKED_V4) and not any(
        ip in net for net in extra if net.version == 4
    )


def is_public_address(ip: IPAddress, blocked_cidrs: Iterable[IPNetwork] = ()) -> bool:
    """명시 표로 공인 주소인지 정한다. 관리자가 막은 CIDR 도 표에 더한다. IPv4 매핑 주소는 그
    IPv4 로 연결되므로 실린 주소만 본다."""
    extra = tuple(blocked_cidrs)
    if isinstance(ip, ipaddress.IPv4Address):
        return _v4_public(ip, extra)
    if ip in MAPPED_V6:
        return _v4_public(_v4(int(ip)), extra)
    if not all(_v4_public(v4, extra) for v4 in embedded_ipv4(ip)):
        return False
    if ip not in GLOBAL_V6 or any(ip in net for net in BLOCKED_V6):
        return False
    return not any(ip in net for net in extra if net.version == 6)


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL("url_unresolvable", f"cannot resolve {host}: {e}") from e
    # getaddrinfo 의 차례(RFC 6724)를 지킨다. 연결은 검사한 주소를 이 차례로 시도한다
    return list(dict.fromkeys(info[4][0] for info in infos))


def _netloc_for(ip: str, port: int | None) -> str:
    """연결에 쓸 netloc. IPv6 는 대괄호로 감싼다."""
    host = f"[{ip}]" if ":" in ip else ip
    return f"{host}:{port}" if port is not None else host


def _host_of(hostname: str) -> str:
    """검사와 오류 글이 쓰는 호스트 이름. 소문자이고 끝의 점을 뗀다."""
    return hostname.lower().rstrip(".")


def a_label(hostname: str) -> str:
    """이름 풀기, Host 머리, SNI 가 쓰는 호스트다. ASCII 밖의 글자가 든 이름은 UTS 46 A-label 로
    바꾸고, 바꿀 수 없으면 url_host 로 거절한다. UTS 46 은 전각·반각 마침표(U+3002·U+FF0E·
    U+FF61)를 점으로 바꾸므로 끝의 점은 바꾼 뒤에 다시 뗀다. 그래야 이름 차단 목록을 비켜 가지
    못한다."""
    host = _host_of(hostname)
    if host.isascii():
        return host
    try:
        label = idna.encode(host, uts46=True).decode("ascii").rstrip(".")
    except idna.IDNAError as e:
        raise BlockedURL("url_host", "the host is not a valid name") from e
    if not label:
        raise BlockedURL("url_host", "the host is not a valid name")
    return label


def normalize_url(url: str) -> str:
    """검사를 통과하면 `resolve_public` 이 돌려줄 정규화된 URL. 이름을 풀지 않는다. ASCII 밖의
    글자가 든 호스트는 A-label 로 적는다."""
    parts = urlsplit(url.strip())
    if parts.hostname and not parts.hostname.isascii():
        netloc = a_label(parts.hostname)
        if ":" in netloc:
            netloc = f"[{netloc}]"
        if parts.port is not None:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
    return parts.geturl()


def _port_of(parts: Any) -> int:
    return parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)


def _under(host: str, suffixes: Iterable[str]) -> bool:
    """host 가 suffixes 가운데 한 이름이거나 그 아래 이름인가. 접미사는 대소문자와 앞뒤 점을 가리지
    않는다."""
    for suffix in suffixes:
        suffix = suffix.lower().strip(".")
        if suffix and (host == suffix or host.endswith("." + suffix)):
            return True
    return False


def resolve_public(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    allowed_ports: Collection[int] | None = None,
    blocked_suffixes: Iterable[str] = (),
    blocked_cidrs: Iterable[IPNetwork] = (),
    allowed_suffixes: Iterable[str] = (),
    allowed_cidrs: Iterable[IPNetwork] = (),
) -> tuple[str, list[str]]:
    """검사를 통과한 URL 과 그때 확인한 주소 목록을 함께 돌려준다.

    주소를 돌려주는 이유는 하나다. 연결이 이름을 다시 풀면 검사가 의미를 잃는다. allowed_ports 가
    있으면 그 포트만, blocked_suffixes 는 관리자가 막은 이름 접미사, blocked_cidrs 는 관리자가 막은
    주소 범위다. allowed_suffixes 와 allowed_cidrs 는 관리자가 연 사설망이다. 그 접미사 아래의
    이름은 어느 주소로 풀리든 받고, 그 범위 안의 주소는 공인이 아니어도 받는다. 다섯 다 기본값이면
    예전 판정 그대로다."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("url_scheme", "only http(s) URLs are allowed")
    if not parts.hostname:
        raise BlockedURL("url_host", "URL has no host")
    if parts.username or parts.password:
        raise BlockedURL("url_userinfo", "URL must not carry credentials")
    try:
        port = _port_of(parts)
    except ValueError as e:
        raise BlockedURL("url_port", "URL has an invalid port") from e
    if allowed_ports is not None and port not in allowed_ports:
        raise BlockedURL("url_port", f"port {port} is not allowed")
    host = a_label(parts.hostname)
    opened = _under(host, allowed_suffixes)
    if not opened and (host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES)):
        raise BlockedURL("url_private", f"host {host!r} is not allowed")
    if _under(host, blocked_suffixes):
        raise BlockedURL("url_blocked_host", f"host {host!r} is blocked")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal is not None else resolver(host)
    if not addresses:
        raise BlockedURL("url_unresolvable", f"cannot resolve {host}")
    cidrs, allowed = tuple(blocked_cidrs), tuple(allowed_cidrs)
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as e:
            raise BlockedURL("url_unresolvable", f"bad address {addr!r} for {host}") from e
        if opened or is_public_address(ip, cidrs) or any(ip in net for net in allowed):
            continue
        raise BlockedURL("url_private", f"host {host!r} resolves to non-public address {addr}")
    return normalize_url(url), addresses


def check_url(url: str, *, resolver: Resolver = system_resolver) -> str:
    """허용되는 URL 이면 정규화된 문자열을 돌려주고, 아니면 BlockedURL.
    job 을 만들기 전 미리 거르는 자리에서 쓴다. 실제로 가져올 때는 resolve_public 이다. 이름을
    풀므로 동기 호출부 전용이다. async 코드는 `check_url_async` 다."""
    return resolve_public(url, resolver=resolver)[0]


async def resolve_public_async(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    timeout: float = TIMEOUT_SECONDS,
    **rules: Any,
) -> tuple[str, list[str]]:
    """`resolve_public` 을 이벤트 루프 밖에서 돌린다 (P69, D81). 이름 풀기는 시스템 리졸버가 포기할
    때까지 막히므로 timeout(상한을 기다리는 시간 포함) 안에 끝나지 않으면 url_unresolvable 이다.
    rules 는 `resolve_public` 의 포트·이름·주소 규칙이다.

    이름 풀기 전용 상한(`RESOLVE_THREADS`)으로 anyio 일꾼 스레드에서 돌리고 abandon_on_cancel 로
    부른다. 그래야 시한이 지났을 때 스레드를 기다리지 않고 돌아온다. 버린 스레드는 상한의 자리를
    돌려주지만 리졸버가 포기할 때까지 계속 돈다. 그래서 살아 있는 스레드 수는 요청 빈도와 리졸버가
    포기하는 시간의 곱만큼 늘 수 있다."""
    call = functools.partial(resolve_public, url, resolver=resolver, **rules)
    try:
        return await asyncio.wait_for(
            anyio.to_thread.run_sync(call, abandon_on_cancel=True, limiter=_limiter()), timeout
        )
    except TimeoutError as e:
        hostname = urlsplit(url.strip()).hostname
        host = _host_of(hostname) if hostname else url
        raise BlockedURL("url_unresolvable", f"cannot resolve {host} within {timeout} s") from e


async def check_url_async(
    url: str, *, resolver: Resolver = system_resolver, timeout: float = TIMEOUT_SECONDS
) -> str:
    """`check_url` 의 async 판. 이름 풀기는 스레드풀에서, timeout 안에 한다."""
    return (await resolve_public_async(url, resolver=resolver, timeout=timeout))[0]


@functools.lru_cache(maxsize=8)
def ssl_context(ca_bundle: str | None) -> ssl.SSLContext:
    """certifi 에 관리자가 준 CA 묶음(PEM)을 더한 TLS 문맥. SSL 을 다시 서명하는 사내 방화벽 뒤에서
    쓴다 (P39b 11절). CA 하나에 한 번만 만든다. 만든 문맥은 바꾸지 않는다."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    if ca_bundle:
        ctx.load_verify_locations(cadata=ca_bundle)
    return ctx


def tls_verify(ca_bundle: str | None) -> ssl.SSLContext | bool:
    """httpx 의 `verify`. CA 묶음이 있으면 그것을 더한 문맥이고, 없으면 기본 신뢰(True)다."""
    return ssl_context(ca_bundle) if ca_bundle else True


def same_host(a: str, b: str) -> bool:
    """같은 호스트로 가는 리다이렉트인가. 이름이 같고 포트가 같거나 http 80 에서 https 443 으로
    오르는 것만이다."""
    pa, pb = urlsplit(a), urlsplit(b)
    if not pa.hostname or not pb.hostname or _host_of(pa.hostname) != _host_of(pb.hostname):
        return False
    try:
        port_a, port_b = _port_of(pa), _port_of(pb)
    except ValueError:
        return False
    if (pa.scheme, port_a) == (pb.scheme, port_b):
        return True
    return (pa.scheme, port_a, pb.scheme, port_b) == ("http", 80, "https", 443)


@dataclass(frozen=True)
class FetchResult:
    """가져온 결과. 다른 호스트로 가는 리다이렉트를 따르지 않았으면 redirect_to 에 그 주소가 있고
    body 는 비어 있다."""

    url: str
    final_url: str
    status: int
    content_type: str | None
    body: bytes
    redirect_to: str | None = None


ACCEPT_ENCODING = "gzip, deflate"

# 모델이 읽는 웹 내용의 감싸개 꼬리표다(P39b 10절). 본문에 같은 꼬리표가 있으면 감싸개를 닫거나 새로
# 열 수 있으므로 대소문자와 상관없이 글로 바꾼다. `<` 뒤와 `/` 뒤의 공백(탭, NBSP 포함)도 꼬리표다.
# 웹훅 본문(P41 4절)처럼 다른 감싸개에 넣는 글은 그 꼬리표를 준다
WEB_CONTENT_TAG = "web_content"
# 꼬리표를 사람 눈과 패턴에서 숨기는 보이지 않는 글자다. 공백이 아닌 제어 문자와 유니코드의 default
# ignorable 글자(폭 없는 공백과 이음표, 소프트 하이픈, 방향 표시, 이체 선택자, 한글 채움 문자, 태그
# 글자, BOM)이고, 꼬리표 이름 사이에 끼면 패턴이 꼬리표를 놓친다
_INVISIBLE = re.compile(
    r"[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
    r"\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
# 리다이렉트 Location 에 오면 안 되는 글자다. 공백, 따옴표, 꺾쇠, 제어 문자는 URL 이 아니라 글을
# 섞는 것이다
_BAD_LOCATION = re.compile(r"[\s\"'<>\x00-\x1f\x7f]")


def retry_after_seconds(seconds: float) -> int:
    """`rate_limited` 결과의 `retry_after`. 남은 시간을 올림한 초이고 최소 1 이다."""
    return max(1, math.ceil(seconds))


@functools.lru_cache(maxsize=8)
def _wrapper_tag(tag: str) -> re.Pattern[str]:
    return re.compile(rf"<(\s*/?\s*{re.escape(tag)})", re.IGNORECASE)


def escape_tags(text: str, tag: str = WEB_CONTENT_TAG) -> str:
    """보이지 않는 글자를 빼고 감싸개 꼬리표만 `&lt;` 로 바꾼다. 엔티티는 풀지 않는다. 런타임 가드가
    글자 그대로 모으는 URL 줄에 쓴다."""
    return _wrapper_tag(tag).sub(lambda m: "&lt;" + m.group(1), _INVISIBLE.sub("", text))


def escape_untrusted(text: str, tag: str = WEB_CONTENT_TAG) -> str:
    """밖에서 온 글을 감싸개 안에 넣을 수 있게 만든다. HTML 엔티티를 먼저 풀고(`&lt;/web_content` 가
    모델에게 꼬리표로 읽히지 않게), 감싸개 꼬리표를 대소문자와 상관없이 `&lt;` 로 바꾼다
    (`escape_tags`). fetch 서버, 플랫폼 검색, 런타임 인용 단계, 웹훅 메시지가 같은 규칙을 쓴다."""
    return escape_tags(html.unescape(text), tag)


class _Inflater:
    """받은 날 바이트를 남은 상한만큼만 푼다. gzip·deflate(zlib 머리, 없으면 날 deflate)만 안다.

    날 바이트도 같은 상한으로 센다. 풀어도 아무것도 나오지 않는 스트림이나 압축 끝 뒤에 붙은 글이
    상한 없이 들어오지 않게 하려는 것이다. 압축이 끝나면(`eof`) 더 읽지 않는다."""

    def __init__(self, encoding: str, limit: int) -> None:
        self.limit = limit
        self.size = 0
        self.raw = 0
        self.encoding = encoding
        self.raw_deflate = False
        if encoding in ("", "identity"):
            self.d = None
        elif encoding in ("gzip", "x-gzip"):
            self.d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self.d = zlib.decompressobj()
        else:
            raise FetchError("unsupported_content_encoding", f"content-encoding {encoding!r}")

    @property
    def done(self) -> bool:
        """압축 스트림이 끝났는가. 끝난 뒤의 날 바이트는 읽지 않는다."""
        return self.d is not None and self.d.eof

    def _count(self, out: bytes) -> bytes:
        self.size += len(out)
        if self.size > self.limit:
            raise FetchError("fetch_too_large", f"body exceeds {self.limit} bytes")
        return out

    def feed(self, chunk: bytes) -> bytes:
        if self.d is None:
            return self._count(chunk)
        if self.done:
            return b""
        self.raw += len(chunk)
        if self.raw > self.limit:
            raise FetchError("fetch_too_large", f"compressed body exceeds {self.limit} bytes")
        try:
            out = bytearray(self.d.decompress(chunk, self.limit - self.size + 1))
            self._count(bytes(out))
            while self.d.unconsumed_tail and not self.d.eof:
                more = self.d.decompress(self.d.unconsumed_tail, self.limit - self.size + 1)
                self._count(more)
                out.extend(more)
        except zlib.error as e:
            if self.encoding == "deflate" and not self.raw_deflate and self.size == 0:
                # zlib 머리 없이 보내는 서버가 있다. 처음부터 날 deflate 로 다시 푼다
                self.raw_deflate = True
                self.raw = 0
                self.d = zlib.decompressobj(-zlib.MAX_WBITS)
                return self.feed(chunk)
            raise FetchError("fetch_failed", "the compressed body is broken") from e
        return bytes(out)


@dataclass(frozen=True)
class _Options:
    """한 번의 fetch 가 끝까지 지니는 설정이다."""

    max_bytes: int
    timeout: float
    max_redirects: int
    resolver: Resolver
    transport: Any
    same_host_only: bool
    content_types: Collection[str] | None
    user_agent: str | None
    verify: ssl.SSLContext | bool
    rules: dict[str, Any]


def _address_rules(
    allowed_ports: Collection[int] | None,
    blocked_suffixes: Iterable[str],
    blocked_cidrs: Iterable[IPNetwork],
) -> dict[str, Any]:
    """`resolve_public` 의 포트·이름·주소 규칙이다."""
    return {
        "allowed_ports": allowed_ports,
        "blocked_suffixes": tuple(blocked_suffixes),
        "blocked_cidrs": tuple(blocked_cidrs),
    }


def _location(current: str, location: str | None) -> str:
    """리다이렉트가 가리키는 URL. Location 이 없거나 URL 이 아닌 글자가 들었으면 거절한다."""
    if not location:
        raise FetchError("fetch_redirect", "redirect without Location")
    if _BAD_LOCATION.search(location):
        raise FetchError("fetch_redirect", "redirect Location is not a URL")
    return normalize_url(urljoin(current, location))


async def read_body(resp: httpx.Response, max_bytes: int) -> bytes:
    """스트림으로 연 응답의 본문을 상한 안에서 읽는다. 날 바이트와 풀린 바이트를 모두 센다
    (`_Inflater`). 미리 읽어 둔 응답(`httpx.Response(content=...)`, 시험의 MockTransport)은 httpx 가
    이미 풀었으므로 풀린 것을 센다. 넘으면 fetch_too_large, 모르는 content-encoding 은
    unsupported_content_encoding 이다. 요청은 `ACCEPT_ENCODING` 만 받겠다고 알려야 한다."""
    encoding = (resp.headers.get("content-encoding") or "").strip().lower()
    preread = resp.is_stream_consumed
    inflater = _Inflater("" if preread else encoding, max_bytes)
    declared = resp.headers.get("content-length")
    if inflater.d is None and declared and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError("fetch_too_large", f"body exceeds {max_bytes} bytes")
    if preread:
        return inflater.feed(resp.content)
    body = bytearray()
    async for chunk in resp.aiter_raw():
        body.extend(inflater.feed(chunk))
        if inflater.done:
            break
    return bytes(body)


async def _open(
    client: httpx.AsyncClient, current: str, addresses: list[str], headers: dict[str, str]
) -> Any:
    """검사한 주소로 못 박아 연다. 연결이 되지 않으면 다음 주소로 간다. 이름을 그대로 넘기면 연결이
    다시 풀어 재바인딩이 통한다."""
    parts = urlsplit(current)
    request = {
        "headers": {**headers, "Host": parts.netloc},
        "extensions": {"sni_hostname": parts.hostname},
    }
    last: Exception | None = None
    for address in addresses:
        pinned = parts._replace(netloc=_netloc_for(address, parts.port)).geturl()
        stream = client.stream("GET", pinned, **request)
        try:
            resp = await stream.__aenter__()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last = e
            continue
        return stream, resp
    assert last is not None
    raise last


async def _get(url: str, opts: _Options) -> FetchResult:
    requested, addresses = await resolve_public_async(
        url, resolver=opts.resolver, timeout=opts.timeout, **opts.rules
    )
    current = requested
    headers = {"Accept-Encoding": ACCEPT_ENCODING}
    if opts.user_agent:
        headers["User-Agent"] = opts.user_agent
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=opts.timeout, transport=opts.transport, verify=opts.verify
    ) as client:
        for _hop in range(opts.max_redirects + 1):
            try:
                stream, resp = await _open(client, current, addresses, headers)
                try:
                    if resp.is_redirect:
                        target = _location(current, resp.headers.get("location"))
                        if opts.same_host_only and not same_host(current, target):
                            return FetchResult(
                                url=requested,
                                final_url=current,
                                status=resp.status_code,
                                content_type=None,
                                body=b"",
                                redirect_to=target,
                            )
                        current, addresses = await resolve_public_async(
                            target, resolver=opts.resolver, timeout=opts.timeout, **opts.rules
                        )
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(
                            "fetch_status",
                            f"{current} answered HTTP {resp.status_code}",
                            status=resp.status_code,
                        )
                    raw_type = resp.headers.get("content-type")
                    main = (raw_type or "").split(";", 1)[0].strip().lower()
                    if opts.content_types is not None and main not in opts.content_types:
                        raise FetchError("unsupported_content_type", f"content-type {main!r}")
                    body = await read_body(resp, opts.max_bytes)
                    return FetchResult(
                        url=requested,
                        final_url=current,
                        status=resp.status_code,
                        content_type=raw_type,
                        body=body,
                    )
                finally:
                    await stream.__aexit__(None, None, None)
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                raise FetchError("fetch_failed", f"{type(e).__name__}: {e}") from e
    raise FetchError("fetch_redirect", f"more than {opts.max_redirects} redirects")


async def fetch_page(
    url: str,
    *,
    max_bytes: int = MAX_BYTES,
    timeout: float = TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
    resolver: Resolver = system_resolver,
    transport: Any = None,
    deadline: float | None = None,
    same_host_only: bool = False,
    content_types: Collection[str] | None = None,
    user_agent: str | None = None,
    ca_bundle: str | None = None,
    allowed_ports: Collection[int] | None = None,
    blocked_suffixes: Iterable[str] = (),
    blocked_cidrs: Iterable[IPNetwork] = (),
) -> FetchResult:
    """검사를 통과한 URL 을 받아 최종 주소, 상태, content-type 과 함께 돌려준다. 리다이렉트는 직접
    따라가며 매번 다시 검사한다. same_host_only 면 다른 호스트로 가는 리다이렉트를 따르지 않고
    redirect_to 로 돌려준다. deadline 은 호출 전체의 시한(초), content_types 는 받는 미디어 타입,
    ca_bundle 은 certifi 에 더할 CA 다."""
    opts = _Options(
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=max_redirects,
        resolver=resolver,
        transport=transport,
        same_host_only=same_host_only,
        content_types=content_types,
        user_agent=user_agent,
        verify=tls_verify(ca_bundle),
        rules=_address_rules(allowed_ports, blocked_suffixes, blocked_cidrs),
    )
    if deadline is None:
        return await _get(url, opts)
    try:
        return await asyncio.wait_for(_get(url, opts), deadline)
    except TimeoutError as e:
        raise FetchError("fetch_timeout", f"no answer within {deadline} s") from e


async def fetch_url(
    url: str,
    *,
    max_bytes: int = MAX_BYTES,
    timeout: float = TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
    resolver: Resolver = system_resolver,
    transport: Any = None,
    deadline: float = DEADLINE_SECONDS,
) -> bytes:
    """검사를 통과한 URL 의 본문만 받아온다. 리다이렉트는 직접 따라가며 매번 다시 검사한다. 이름
    풀기는 스레드에서 하고 홉마다 timeout 안에 끝나야 한다 (P69). HTTP 도 연결 시도와 읽기마다
    timeout 이다. 주소가 여럿이면 연결을 차례로 해 보므로 호출 전체는 deadline(기본
    `DEADLINE_SECONDS`) 안에 끝나야 하고, 넘기면 fetch_timeout 이다."""
    result = await fetch_page(
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=max_redirects,
        resolver=resolver,
        transport=transport,
        deadline=deadline,
    )
    return result.body
