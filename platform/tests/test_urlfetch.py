"""input.url 의 SSRF 차단과 제한된 fetch."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import re
import threading
import time
import tracemalloc
import zlib
from pathlib import Path

import httpx
import pytest

from daegam import urlfetch
from daegam.urlfetch import (
    FETCH_ERRORS,
    BlockedURL,
    FetchError,
    check_url,
    check_url_async,
    embedded_ipv4,
    escape_untrusted,
    fetch_page,
    fetch_url,
    is_public_address,
    resolve_public,
    same_host,
    ssl_context,
)
from tests.helpers.loop_guard import on_event_loop

HOSTS = {
    "api.example.com": ["93.184.216.34"],
    "dual.example.com": ["93.184.216.34", "10.1.2.3"],  # 하나라도 사설이면 거부
    "v6.example.com": ["2606:2800:220:1:248:1893:25c8:1946"],
    "mapped.example.com": ["::ffff:10.0.0.9"],
    "loop.example.com": ["127.0.0.1"],
    "xn--3e0b707e.kr": ["93.184.216.34"],
    "two.example.com": ["93.184.216.35", "93.184.216.36"],
}


def resolver(host: str) -> list[str]:
    if host not in HOSTS:
        raise BlockedURL("url_unresolvable", host)
    return HOSTS[host]


def blocked(url: str) -> str:
    with pytest.raises(BlockedURL) as ei:
        check_url(url, resolver=resolver)
    return ei.value.code


def test_check_url_allows_public():
    assert check_url("https://api.example.com/openapi.yaml", resolver=resolver).startswith("https")
    assert check_url("http://93.184.216.34:8080/x", resolver=resolver)
    assert check_url("http://[2606:2800:220:1:248:1893:25c8:1946]/x", resolver=resolver)
    assert check_url("http://v6.example.com/x", resolver=resolver)


def test_check_url_blocks_private_and_special():
    assert blocked("http://10.0.0.1/") == "url_private"
    assert blocked("http://192.168.1.1/") == "url_private"
    assert blocked("http://172.16.0.1/") == "url_private"
    assert blocked("http://169.254.169.254/latest/meta-data") == "url_private"
    assert blocked("http://127.0.0.1:9000/") == "url_private"
    assert blocked("http://[::1]/") == "url_private"
    assert blocked("http://[::ffff:10.0.0.9]/") == "url_private"
    assert blocked("http://0.0.0.0/") == "url_private"
    assert blocked("http://localhost/") == "url_private"
    assert blocked("http://daegam-platform.daegam-system.svc/") == "url_private"
    assert blocked("http://redis.tenant.svc.cluster.local/") == "url_private"
    assert blocked("http://metadata.google.internal/") == "url_private"
    assert blocked("http://something.internal/") == "url_private"
    # DNS 가 사설로 풀리는 경우
    assert blocked("http://dual.example.com/") == "url_private"
    assert blocked("http://mapped.example.com/") == "url_private"
    assert blocked("http://loop.example.com/") == "url_private"
    assert blocked("http://nope.example.com/") == "url_unresolvable"
    assert blocked("ftp://api.example.com/") == "url_scheme"
    assert blocked("file:///etc/passwd") == "url_scheme"
    assert blocked("http:///x") == "url_host"
    assert blocked("http://u:p@api.example.com/") == "url_userinfo"


def _fetch(handler, url, *, resolver=resolver, **kw):
    return asyncio.run(
        fetch_url(url, resolver=resolver, transport=httpx.MockTransport(handler), **kw)
    )


def test_fetch_follows_limited_redirects_and_rechecks():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # 연결은 검사한 IP 로 가고 이름은 Host 헤더와 SNI 에 남는다
        seen.append(f"{request.url.scheme}://{request.headers['host']}{request.url.path}")
        assert request.url.host == "93.184.216.34", "검사한 주소로 못 박아야 한다"
        assert request.extensions.get("sni_hostname") == "api.example.com"
        path = request.url.path
        if path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if path == "/b":
            return httpx.Response(301, headers={"location": "https://api.example.com/c"})
        if path == "/c":
            return httpx.Response(200, content=b"openapi: 3.0.3\n")
        if path == "/private":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/x"})
        if path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if path == "/noloc":
            return httpx.Response(302)
        return httpx.Response(404)

    assert _fetch(handler, "http://api.example.com/a") == b"openapi: 3.0.3\n"
    assert seen == [
        "http://api.example.com/a",
        "http://api.example.com/b",
        "https://api.example.com/c",
    ]
    with pytest.raises(BlockedURL) as bi:
        _fetch(handler, "http://api.example.com/private")
    assert bi.value.code == "url_private"
    with pytest.raises(FetchError) as ei:
        _fetch(handler, "http://api.example.com/loop")
    assert ei.value.code == "fetch_redirect"
    with pytest.raises(FetchError) as ei:
        _fetch(handler, "http://api.example.com/noloc")
    assert ei.value.code == "fetch_redirect"
    with pytest.raises(FetchError) as ei:
        _fetch(handler, "http://api.example.com/missing")
    assert ei.value.code == "fetch_status"


def test_fetch_pins_the_checked_address_against_dns_rebinding():
    """검사할 때와 연결할 때 DNS 응답이 달라도 검사한 주소로만 간다.

    이름만 검사하고 연결을 다시 풀게 두면 그 사이에 사설 주소로 바뀌는 재바인딩이 통해
    이 모듈 전체가 무력해진다. 그래서 통과한 IP 로 못 박는다.
    """
    answers = {"n": 0}

    def rebinding(host: str) -> list[str]:
        # 첫 조회(검사)는 공인, 두 번째부터(연결이 다시 푼다면) 메타데이터 주소
        answers["n"] += 1
        return ["93.184.216.34"] if answers["n"] == 1 else ["169.254.169.254"]

    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.host)
        return httpx.Response(200, content=b"ok")

    body = asyncio.run(
        fetch_url(
            "http://rebind.example.com/spec.yaml",
            resolver=rebinding,
            transport=httpx.MockTransport(handler),
        )
    )
    assert body == b"ok"
    assert reached == ["93.184.216.34"], "연결이 검사 결과를 무시하고 이름을 다시 풀었다"


def test_fetch_size_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/declared":
            return httpx.Response(200, headers={"content-length": "999999"}, content=b"")
        return httpx.Response(200, content=b"x" * 2000)

    with pytest.raises(FetchError) as ei:
        _fetch(handler, "http://api.example.com/declared", max_bytes=1000)
    assert ei.value.code == "fetch_too_large"
    with pytest.raises(FetchError) as ei:
        _fetch(handler, "http://api.example.com/stream", max_bytes=1000)
    assert ei.value.code == "fetch_too_large"
    assert len(_fetch(handler, "http://api.example.com/stream", max_bytes=2000)) == 2000


def test_fetch_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(FetchError) as ei:
        _fetch(handler, "http://api.example.com/x")
    assert ei.value.code == "fetch_failed"


def off_loop_resolver(host: str) -> list[str]:
    """이벤트 루프 스레드에서 불리면 거부하는 리졸버 (P69)."""
    assert not on_event_loop(), "name resolution ran on the event loop thread"
    return resolver(host)


def test_resolution_runs_off_the_event_loop():
    """fetch_url 은 첫 호스트와 리다이렉트마다, check_url_async 는 검사할 때 이름을 스레드풀에서
    푼다."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "http://api.example.com/b"})
        return httpx.Response(200, content=b"ok")

    assert _fetch(handler, "http://api.example.com/a", resolver=off_loop_resolver) == b"ok"
    checked = asyncio.run(check_url_async("http://api.example.com/x", resolver=off_loop_resolver))
    assert checked == "http://api.example.com/x"
    with pytest.raises(BlockedURL) as ei:
        asyncio.run(check_url_async("http://loop.example.com/", resolver=off_loop_resolver))
    assert ei.value.code == "url_private"


def test_a_resolver_that_never_returns_fails_within_the_limit_and_leaves_the_loop_free():
    """답하지 않는 리졸버. fetch 는 제 시한 안에 url_unresolvable 로 끝나고, 그동안 다른 코루틴이
    돈다."""
    release = threading.Event()

    def stuck(host: str) -> list[str]:
        release.wait(30)
        return ["93.184.216.34"]

    async def scenario(call) -> tuple[BaseException, int, float]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        started = time.monotonic()
        try:
            await call()
        except BlockedURL as e:
            error: BaseException = e
        else:  # pragma: no cover - 실패 경로
            raise AssertionError("the fetch should fail")
        finally:
            task.cancel()
        return error, ticks, time.monotonic() - started

    def fetch():
        return fetch_url(
            "http://api.example.com/x",
            resolver=stuck,
            timeout=0.3,
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )

    try:
        for call in (
            fetch,
            lambda: check_url_async("http://api.example.com/x", resolver=stuck, timeout=0.3),
        ):
            error, ticks, elapsed = asyncio.run(scenario(call))
            assert error.code == "url_unresolvable"  # type: ignore[attr-defined]
            assert elapsed < 2
            assert ticks >= 10, "the event loop was blocked while the name was resolved"
    finally:
        release.set()


def test_stuck_lookups_use_their_own_thread_limit():
    """막힌 이름 풀기는 이름 풀기 전용 상한만 쓰고 run_in_threadpool 의 공용 상한은 비워 둔다."""
    import anyio.to_thread

    release = threading.Event()

    def stuck(host: str) -> list[str]:
        release.wait(30)
        return ["93.184.216.34"]

    async def scenario() -> int:
        lookups = [
            asyncio.create_task(
                check_url_async("http://api.example.com/x", resolver=stuck, timeout=1)
            )
            for _ in range(4)
        ]
        await asyncio.sleep(0.2)
        borrowed = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
        for task in lookups:
            with pytest.raises(BlockedURL):
                await task
        return borrowed

    try:
        assert asyncio.run(scenario()) == 0
    finally:
        release.set()


# -- P39b 11절: 명시 주소 표, 포트, 이름, 리다이렉트, 시한, 압축 --------------------------

TABLE = json.loads((Path(__file__).parent / "fixtures" / "urlfetch-address-table.json").read_text())


@pytest.mark.parametrize(("address", "public"), TABLE["addresses"])
def test_the_explicit_address_table(address, public):
    """두 구성요소가 같은 표를 돈다 (web-tools/tests 도 이 파일을 읽는다)."""
    assert is_public_address(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(("address", "embedded"), TABLE["embedded"])
def test_embedded_ipv4(address, embedded):
    assert [str(v4) for v4 in embedded_ipv4(ipaddress.ip_address(address))] == embedded


@pytest.mark.parametrize(("cidr", "address", "public"), TABLE["blocked_cidrs"])
def test_admin_blocked_cidrs_join_the_list(cidr, address, public):
    assert is_public_address(ipaddress.ip_address(address), [ipaddress.ip_network(cidr)]) is public


def test_the_list_is_not_looser_than_this_interpreter():
    """지금 인터프리터의 `ipaddress` 가 막는 표본은 우리 표도 막는다. 표가 덜 막으면 실패한다."""
    samples = [ipaddress.ip_address(a) for a, _ in TABLE["addresses"]]
    for ip in samples:
        flags = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not flags.is_global or flags.is_private or flags.is_multicast or flags.is_reserved:
            assert not is_public_address(ip), f"{ip} passes our list but not ipaddress"


def test_ports_and_admin_suffixes():
    def code(url, **rules):
        with pytest.raises(BlockedURL) as ei:
            resolve_public(url, resolver=resolver, **rules)
        return ei.value.code

    assert resolve_public("https://api.example.com/", resolver=resolver, allowed_ports={443})
    assert code("http://api.example.com:8080/", allowed_ports={80, 443}) == "url_port"
    assert code("http://api.example.com/", allowed_ports={443}) == "url_port"
    assert code("https://api.example.com/", blocked_suffixes=["example.com"]) == "url_blocked_host"
    assert resolve_public(
        "https://api.example.com/", resolver=resolver, blocked_suffixes=["le.com"]
    )
    assert (
        code("https://v6.example.com/", blocked_cidrs=[ipaddress.ip_network("2606:2800::/32")])
        == "url_private"
    )
    # UTS 46 이 전각·반각 마침표를 점으로 바꾼다. 끝의 점을 떼므로 이름 목록을 비켜 가지 못한다
    for dot in ("\u3002", "\uff0e", "\uff61"):
        url = f"https://api.example.com{dot}/"
        assert code(url, blocked_suffixes=["example.com"]) == "url_blocked_host"
        assert code(f"https://localhost{dot}/") == "url_private"


def test_admin_allowed_private_networks():
    """관리자가 연 접미사 아래의 이름은 어느 주소로 풀리든 받고, 연 범위 안의 주소는 공인이 아니어도
    받는다. 그 밖의 판정은 그대로다."""

    def private(host: str) -> list[str]:
        return ["10.20.0.7", "93.184.216.34"]

    def code(url, **rules):
        with pytest.raises(BlockedURL) as ei:
            resolve_public(url, resolver=private, **rules)
        return ei.value.code

    net = [ipaddress.ip_network("10.20.0.0/16")]
    assert code("http://mcp.corp.example/") == "url_private"
    assert resolve_public("http://mcp.corp.example/", resolver=private, allowed_cidrs=net) == (
        "http://mcp.corp.example/",
        ["10.20.0.7", "93.184.216.34"],
    )
    assert resolve_public("http://10.20.0.7:8080/", allowed_cidrs=net)[1] == ["10.20.0.7"]
    assert code("http://10.21.0.7/", allowed_cidrs=net) == "url_private"
    assert code("http://[::ffff:10.20.0.7]/", allowed_cidrs=net) == "url_private"
    for url in ("http://mcp.Corp.Example./", "http://corp.example/", "http://db.internal/"):
        suffixes = ["CORP.example.", "internal"]
        assert resolve_public(url, resolver=private, allowed_suffixes=suffixes)
    assert code("http://notcorp.example/", allowed_suffixes=["corp.example"]) == "url_private"
    assert code("http://localhost/", allowed_suffixes=["corp.example"]) == "url_private"
    # 자격 증명과 스킴은 열린 이름에도 그대로 본다
    rules = {"allowed_suffixes": ["corp.example"], "allowed_cidrs": net}
    assert code("http://u:p@mcp.corp.example/", **rules) == "url_userinfo"
    assert code("ftp://mcp.corp.example/", **rules) == "url_scheme"


def _page(handler, url, **kw):
    return asyncio.run(
        fetch_page(url, resolver=resolver, transport=httpx.MockTransport(handler), **kw)
    )


def test_same_host_only_returns_a_cross_host_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/up":
            return httpx.Response(301, headers={"location": "https://api.example.com/p"})
        if request.url.path == "/away":
            return httpx.Response(302, headers={"location": "https://evil.example.org/x?q=1"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>hi</p>")

    up = _page(handler, "http://api.example.com/up", same_host_only=True)
    assert (up.final_url, up.status, up.redirect_to) == ("https://api.example.com/p", 200, None)
    away = _page(handler, "http://api.example.com/away", same_host_only=True)
    assert away.redirect_to == "https://evil.example.org/x?q=1"
    assert (away.status, away.body) == (302, b"")
    assert same_host("http://a.com/x", "https://a.com/y")
    assert not same_host("https://a.com/x", "http://a.com/y")
    assert not same_host("http://a.com:8080/", "https://a.com/")


def test_content_types_and_user_agent():
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers.get("user-agent", ""))
        assert request.headers["accept-encoding"] == "gzip, deflate"
        kind = "image/png" if request.url.path == "/img" else "text/html; charset=utf-8"
        return httpx.Response(200, headers={"content-type": kind}, content=b"x")

    ok = _page(handler, "http://api.example.com/", content_types={"text/html"}, user_agent="ua/1")
    assert ok.content_type == "text/html; charset=utf-8"
    assert agents == ["ua/1"]
    with pytest.raises(FetchError) as ei:
        _page(handler, "http://api.example.com/img", content_types={"text/html"})
    assert ei.value.code == "unsupported_content_type"


def test_the_deadline_bounds_a_slow_trickle():
    async def trickle():
        for _ in range(50):
            await asyncio.sleep(0.05)
            yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, stream=_Stream(trickle())
        )

    with pytest.raises(FetchError) as ei:
        _page(handler, "http://api.example.com/", deadline=0.3)
    assert ei.value.code == "fetch_timeout"


class _Stream(httpx.AsyncByteStream):
    """읽히지 않은 응답 본문. 날 바이트(`aiter_raw`)를 시험한다."""

    def __init__(self, gen):
        self.gen = gen

    async def __aiter__(self):
        async for chunk in self.gen:
            yield chunk


def _raw(body: bytes, encoding: str) -> httpx.Response:
    async def once():
        yield body

    return httpx.Response(
        200,
        headers={"content-type": "text/plain", "content-encoding": encoding},
        stream=_Stream(once()),
    )


def test_a_compression_bomb_stops_at_the_limit_without_inflating_it():
    """1 MiB 상한에 1 GiB 로 풀리는 gzip. 남은 상한만큼만 풀므로 잠깐 쓰는 메모리도 상한 근처다."""
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)

    def handler(request: httpx.Request) -> httpx.Response:
        return _raw(bomb, "gzip")

    tracemalloc.start()
    try:
        with pytest.raises(FetchError) as ei:
            _page(handler, "http://api.example.com/", max_bytes=1024 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert ei.value.code == "fetch_too_large"
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes"


def test_gzip_and_deflate_inflate_and_others_are_refused():
    text = b"hello " * 100

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/gz":
            return _raw(gzip.compress(text), "gzip")
        if path == "/zlib":
            return _raw(zlib.compress(text), "deflate")
        if path == "/raw":
            raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
            return _raw(raw.compress(text) + raw.flush(), "deflate")
        return _raw(text, "br")

    assert _page(handler, "http://api.example.com/gz").body == text
    assert _page(handler, "http://api.example.com/zlib").body == text
    assert _page(handler, "http://api.example.com/raw").body == text
    with pytest.raises(FetchError) as ei:
        _page(handler, "http://api.example.com/br")
    assert ei.value.code == "unsupported_content_encoding"


def test_ssl_context_adds_the_ca_bundle():
    default = ssl_context(None)
    pem = (Path(__file__).parent / "fixtures" / "test-ca.pem").read_text()
    extra = ssl_context(pem)
    assert len(extra.get_ca_certs()) == len(default.get_ca_certs()) + 1


def test_every_error_code_has_our_sentence():
    source = Path(urlfetch.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'(?:BlockedURL|FetchError)\(\s*"([a-z_]+)"', source))
    assert codes <= set(FETCH_ERRORS)


def test_trailing_data_after_the_compressed_body_is_not_read():
    """압축이 끝난 뒤에 붙은 글은 읽지 않는다. 날 바이트도 상한으로 세므로 상한 없이 쌓이지
    않는다."""
    reads: list[int] = []

    async def stream():
        yield gzip.compress(b"hello")
        for _ in range(100):
            reads.append(1)
            yield b"\0" * 65536

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": "gzip"},
            stream=_Stream(stream()),
        )

    assert _page(handler, "http://api.example.com/", max_bytes=1024).body == b"hello"
    assert len(reads) <= 1


def test_a_stream_that_inflates_to_nothing_stops_at_the_raw_limit():
    """풀어도 나오는 것이 없는 압축(빈 deflate 블록의 반복)도 날 바이트 상한에서 멈춘다."""
    empty = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    block = empty.compress(b"") + empty.flush(zlib.Z_SYNC_FLUSH)

    async def stream():
        for _ in range(10000):
            yield block * 64

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": "deflate"},
            stream=_Stream(stream()),
        )

    with pytest.raises(FetchError) as ei:
        _page(handler, "http://api.example.com/", max_bytes=4096)
    assert ei.value.code == "fetch_too_large"


@pytest.mark.parametrize(
    "location",
    ["https://evil.example.org/a b", 'https://evil.example.org/"x', "https://e.org/<x>"],
)
def test_a_location_that_is_not_a_url_is_refused(location):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(FetchError) as ei:
        _page(handler, "http://api.example.com/", same_host_only=True)
    assert ei.value.code == "fetch_redirect"


def test_escape_untrusted_closes_every_spelling_of_the_wrapper_tag():
    text = "a </web_content> b <WEB_CONTENT x> c &lt;/Web_Content&gt; d </ web_content>"
    out = escape_untrusted(text)
    assert "<web_content" not in out.lower() and "</web_content" not in out.lower()
    assert "</ web_content" not in out.lower()
    assert out.count("&lt;") == 4


@pytest.mark.parametrize(
    "raw",
    [
        "< /web_content>",
        "<\t/web_content>",
        "<\xa0/web_content>",
        "</\xa0web_content>",
        "<\x00/web_content>",
        "</web_\u200bcontent>",
        "<\u200b/web_content>",
        "&lt;\u2060/web_content&gt;",
        "</web\u00ad_content>",
        "</web_c\u2062ontent>",
        "</web_\U000e0020content>",
        "</\u202eweb_content>",
    ],
)
def test_hidden_spellings_of_the_wrapper_tag_are_closed_too(raw):
    """`<` 뒤의 공백·탭·NBSP 와, 꼬리표를 숨기는 NUL·폭 없는 공백·소프트 하이픈·방향 표시·태그
    글자도 꼬리표다."""
    out = escape_untrusted(f"x {raw} y")
    assert "<" not in out and "&lt;" in out


def test_fetch_url_ends_within_its_deadline():
    """주소가 많고 연결마다 시한까지 기다려도 호출 전체는 deadline 안에 fetch_timeout 으로
    끝난다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        raise httpx.ConnectTimeout("slow", request=request)

    addresses = [f"93.184.215.{i}" for i in range(1, 21)]
    started = time.monotonic()
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            fetch_url(
                "http://many.example/",
                resolver=lambda _h: addresses,
                transport=httpx.MockTransport(handler),
                timeout=0.2,
                max_redirects=0,
                deadline=0.5,
            )
        )
    assert ei.value.code == "fetch_timeout" and time.monotonic() - started < 2


def test_a_unicode_host_is_resolved_and_sent_as_its_a_label():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    page = _page(handler, "https://한국.kr/a")
    assert page.final_url == "https://xn--3e0b707e.kr/a" and seen == ["xn--3e0b707e.kr"]
    with pytest.raises(BlockedURL) as ei:
        resolve_public("https://\u2603.com/", resolver=resolver)
    assert ei.value.code == "url_host"


def test_connection_tries_each_checked_address_in_order():
    """getaddrinfo 의 차례를 지키고, 연결이 안 되는 주소 다음의 주소로 간다. 모두 검사한 주소다."""
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == "93.184.216.35":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    assert _page(handler, "http://two.example.com/").body == b"ok"
    assert tried == ["93.184.216.35", "93.184.216.36"]


def test_an_invalid_url_inside_httpx_is_a_fetch_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad")

    with pytest.raises(FetchError) as ei:
        _page(handler, "http://api.example.com/")
    assert ei.value.code == "fetch_failed"
