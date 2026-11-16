"""플랫폼과 같은 주소 표를 바이트 사본으로 돈다 (P39b 11절). 표는 플랫폼 시험의 픽스처에 있다."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest

from webtools.urlfetch import (
    BlockedURL,
    a_label,
    embedded_ipv4,
    is_public_address,
    resolve_public,
)

DOMAINS = json.loads(
    (Path(__file__).resolve().parents[1] / "schema" / "samples" / "domains.json").read_text(
        encoding="utf-8"
    )
)
TABLE = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "platform"
        / "tests"
        / "fixtures"
        / "urlfetch-address-table.json"
    ).read_text()
)


@pytest.mark.parametrize(("address", "public"), TABLE["addresses"])
def test_the_explicit_address_table(address, public):
    assert is_public_address(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(("address", "embedded"), TABLE["embedded"])
def test_embedded_ipv4(address, embedded):
    assert [str(v4) for v4 in embedded_ipv4(ipaddress.ip_address(address))] == embedded


@pytest.mark.parametrize(("cidr", "address", "public"), TABLE["blocked_cidrs"])
def test_admin_blocked_cidrs(cidr, address, public):
    assert is_public_address(ipaddress.ip_address(address), [ipaddress.ip_network(cidr)]) is public


def test_the_list_is_not_looser_than_this_interpreter():
    for address, _ in TABLE["addresses"]:
        ip = ipaddress.ip_address(address)
        flags = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not flags.is_global or flags.is_private or flags.is_multicast or flags.is_reserved:
            assert not is_public_address(ip), address


def test_host_labels_match_the_shared_domain_table():
    """플랫폼·런타임과 같은 표다. 전각·반각 마침표로 끝나는 이름도 점을 뗀다."""
    assert [[value, a_label(value)] for value, _ in DOMAINS["a_label"]] == DOMAINS["a_label"]


@pytest.mark.parametrize("dot", ["\u3002", "\uff0e", "\uff61"])
def test_a_trailing_full_stop_does_not_pass_the_name_lists(dot):
    def code(url: str) -> str:
        with pytest.raises(BlockedURL) as e:
            resolve_public(
                url, resolver=lambda _h: ["93.184.215.14"], blocked_suffixes=["corp.example"]
            )
        return e.value.code

    assert code(f"https://foo.corp.example{dot}/") == "url_blocked_host"
    assert code(f"https://localhost{dot}/") == "url_private"
