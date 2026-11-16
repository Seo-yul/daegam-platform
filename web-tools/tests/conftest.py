from __future__ import annotations

import httpx
import pytest

from webtools.fetch import Fetcher
from webtools.settings import FetchSettings

HOSTS = {
    "site.example.com": ["93.184.216.34"],
    "other.example.org": ["93.184.216.35"],
    "inner.example.com": ["10.0.0.5"],
}


def resolver(host: str) -> list[str]:
    from webtools.urlfetch import BlockedURL

    if host not in HOSTS:
        raise BlockedURL("url_unresolvable", host)
    return HOSTS[host]


def make_fetcher(handler, **overrides) -> Fetcher:
    settings = FetchSettings(**overrides)
    return Fetcher(settings, resolver=resolver, transport=httpx.MockTransport(handler))


@pytest.fixture
def html_page() -> bytes:
    return (
        b"<html><head><title>  Example  Page </title><script>evil()</script></head>"
        b"<body><nav>menu</nav><main><h1>Hello</h1><p>See <a href='/docs'>docs</a> and "
        b"<a href='javascript:alert(1)'>this</a>.</p><img src='x.png' alt='a cat'>"
        b"<form><input name='q'></form></main><footer>foot</footer></body></html>"
    )
