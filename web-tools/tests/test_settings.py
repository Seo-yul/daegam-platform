"""`WEB_FETCH_*` env 읽기 (P39b 10절). 없거나 틀린 값은 `FetchSettings` 의 기본값이고 상한 밖은
상한이다."""

from __future__ import annotations

import base64
import logging

from webtools import settings, urlfetch
from webtools.settings import CA_ENV, FetchSettings, load

PEM = "-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n"


def test_an_empty_env_gives_the_defaults():
    loaded = load({})
    defaults = FetchSettings()
    assert loaded == defaults and loaded.ca_bundle is None


def test_values_are_read_and_clamped():
    env = {
        "WEB_FETCH_MAX_BYTES": str(100 * 1024 * 1024),
        "WEB_FETCH_MAX_CHARS": "100",
        "WEB_FETCH_TIMEOUT_SECONDS": "600",
        "WEB_FETCH_MAX_REDIRECTS": "9",
        "WEB_FETCH_ALLOWED_PORTS": "443, 8443, x, 70000",
        "WEB_FETCH_BLOCKED_SUFFIXES": "Corp.Example., lab",
        "WEB_FETCH_BLOCKED_CIDRS": "10.0.0.0/8, nope",
        "WEB_FETCH_RESPECT_ROBOTS": "false",
        "WEB_FETCH_USER_AGENT_CONTACT": "https://ops.example.com",
        "WEB_FETCH_PER_MINUTE": "0",
        "WEB_FETCH_CA_B64": base64.b64encode(PEM.encode()).decode(),
    }
    s = load(env)
    assert s.max_bytes == settings.MAX_BYTES_CAP and s.max_chars == 500
    assert (
        s.timeout_seconds == settings.TIMEOUT_CAP and s.max_redirects == settings.MAX_REDIRECTS_CAP
    )
    assert s.allowed_ports == frozenset({443, 8443})
    assert s.blocked_suffixes == ("corp.example", "lab")
    assert [str(n) for n in s.blocked_cidrs] == ["10.0.0.0/8"]
    assert not s.respect_robots and s.per_minute == 1 and s.ca_bundle == PEM
    assert s.user_agent.endswith("(+https://ops.example.com)")


def test_a_bad_number_falls_back_to_the_default():
    s = load({"WEB_FETCH_MAX_CHARS": "lots"})
    assert s.max_chars == FetchSettings().max_chars


def test_a_ca_that_is_not_a_base64_pem_is_left_out_with_one_warning(caplog):
    """base64 가 아니거나 풀어도 PEM 인증서가 아니면 CA 없이 가져오고, 읽을 때 한 번 알린다."""
    caplog.set_level(logging.WARNING, logger="webtools.settings")
    for value in ("not base64!", base64.b64encode(b"PEM").decode()):
        caplog.clear()
        assert load({CA_ENV: value}).ca_bundle is None
        [record] = caplog.records
        assert CA_ENV in record.getMessage() and "PEM" in record.getMessage()
    caplog.clear()
    assert load({}).ca_bundle is None and not caplog.records


def test_one_ssl_context_per_ca():
    """가져오기마다 TLS 문맥을 새로 만들지 않는다. CA 하나에 하나다."""
    pem = (
        urlfetch.certifi.contents().split("-----END CERTIFICATE-----")[0]
        + "-----END CERTIFICATE-----\n"
    )
    assert urlfetch.ssl_context(pem) is urlfetch.ssl_context(pem)
    assert urlfetch.ssl_context(None) is not urlfetch.ssl_context(pem)
