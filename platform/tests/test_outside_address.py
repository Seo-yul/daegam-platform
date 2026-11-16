"""P55a F7: 허브 항목 주소의 갈래(`outside_address`)와 응답의 `address_class`.

클라이언트 설정 내보내기는 `status.external_url` 만 읽는다. 그래서 서버가 등록한 문자열을 갈라
밖에서 닿거나 사설망인 주소만 그 칸에 싣고, 나머지는 갈래만 싣는다. 갈래를 헷갈리기 쉬운
주소(Python 과 WHATWG 파서가 다른 호스트를 읽는 것, 숫자 줄임 꼴, IPv6 literal, 이름 접미사)가
여기 다 있다.
"""

from __future__ import annotations

import pytest

from daegam import urls
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import HUB

CASES: dict[str, list[str]] = {
    "outside": [
        "https://mcp.example.com/mcp",
        "https://mcp.example.com:8443/mcp",
        "HTTPS://MCP.Example.COM/mcp",
        "https://8.8.8.8/mcp",
        # IDN 이름과 이름 가운데의 전각 마침표는 A-label 로 바꾼 뒤 본다
        "https://ｅｘａｍｐｌｅ.com/mcp",
        "https://mcp。example。com/mcp",
        # 사설 주소로 풀리는 두 라벨 클러스터 이름은 접미사로 가를 수 없다. 등록한 그대로 남는다
        "http://my-mcp.my-ns/mcp",
        "http://kubernetes.default/mcp",
        # 프래그먼트 안의 ? 는 쿼리가 아니다
        "https://mcp.example.com/mcp#a?b",
    ],
    "private": [
        "http://10.0.0.5:8080/mcp",
        "http://172.16.1.1/mcp",
        "http://192.168.1.5/mcp",
        "http://100.64.0.1/mcp",
        "http://printer.local/mcp",
        "https://mcp.corp.internal/mcp",
    ],
    "local": [
        # WHATWG 는 \ 에서 authority 를 끝내 127.0.0.1 로 간다. Python 은 example.com 을 읽는다
        "http://127.0.0.1\\@example.com/mcp",
        "http://127.0.0.1\\@ｅｘａｍｐｌｅ.com/mcp",
        # 퍼센트 인코딩은 클라이언트가 풀어 127.0.0.1 로 간다
        "http://127.0.0.1%2e/mcp",
        "http://%31%32%37.0.0.1/mcp",
        # 정수, 줄임, 16진, 8진 IPv4
        "http://2130706433/mcp",
        "http://167772161/mcp",
        "http://127.1/mcp",
        "http://10.1/mcp",
        "http://0x7f.0.0.1/mcp",
        "http://0x0a.0.0.1/mcp",
        "http://0177.0.0.1/mcp",
        "http://012.0.0.1/mcp",
        # IPv6 literal 은 문법이 받지 않는다. NAT64 와 IPv4 매핑도 그 안이다
        "http://[64:ff9b::a00:1]/mcp",
        "http://[::ffff:127.0.0.1]/mcp",
        "http://[::ffff:8.8.8.8]/mcp",
        "http://[::1]/mcp",
        "https://[2606:4700::1111]/mcp",
        # 공인도 사설망도 아닌 literal
        "http://127.0.0.1:8080/mcp",
        "http://169.254.169.254/latest",
        "http://0.0.0.1/mcp",
        "http://198.18.0.1/mcp",
        "http://192.0.2.10/mcp",
        "http://224.0.0.1/mcp",
        # 끝의 점과 전각 마침표
        "https://mcp.example.com./mcp",
        "https://mcp.example.com。/mcp",
        "http://localhost./mcp",
        "http://ｌｏｃａｌｈｏｓｔ/mcp",
        "http://ｌｏｃａｌｈｏｓｔ。/mcp",
        # 이름 접미사. .cluster.local 은 .local 보다 먼저다
        "http://api.localhost/mcp",
        "http://api。localhost/mcp",
        "http://mcp.ns.svc.cluster.local:8080/mcp",
        "http://a.cluster.local/mcp",
        "http://a.ns.svc:8080/mcp",
        "http://metadata.google.internal/computeMetadata",
        # 접미사 표가 잡지 못하는 이름. cluster.local 은 .local 로 private 이 될 것이었다
        "http://cluster.local/mcp",
        "http://localhost.localdomain/mcp",
        # 라벨 하나짜리 이름과 메타데이터 이름
        "http://my-mcp/mcp",
        "http://localhost:1/mcp",
        "http://metadata/mcp",
        # 주소가 아닌 값
        "ftp://mcp.example.com/mcp",
        "https:///mcp",
        "http:\\\\127.0.0.1/mcp",
        "https://mcp.example.com:0/mcp",
        "https://mcp.example.com:99999/mcp",
        "https://mcp.example.com:/mcp",
        "https://mcp.example.com /mcp",
        "https://mcp.example.com/m\ncp",
        "https://exa_mple.com/mcp",
        "https://ex：ample.com/mcp",
    ],
    "credentials": [
        # Claude Code 는 url 의 ${VAR} 와 ${VAR:-기본값} 을 펼친다. 내보내면 그 사람의 env 값이
        # 남의 서버로 간다
        "https://evil.example.com/${DAEGAM_PAT_DEFAULT}",
        "https://evil.example.com/mcp/${GITHUB_TOKEN:-x}",
        "https://evil.example.com/mcp?t=${DAEGAM_PAT_DEFAULT}",
        "https://evil.example.com/mcp$x",
        "https://${HOST}/mcp",
        "http://user:pw@ｅｘａｍｐｌｅ.com/mcp",
        "https://user@mcp.example.com/mcp",
        "https://u:p%40ss@mcp.example.com/mcp",
        "https://mcp.example.com/mcp?key=abc",
        "https://mcp.example.com/mcp?",
    ],
}


@pytest.mark.parametrize(("url", "expected"), [(u, cls) for cls, us in CASES.items() for u in us])
def test_outside_address_classes(url: str, expected: str) -> None:
    assert urls.outside_address(url) == expected


def _entry(client, name: str, url: str) -> dict:
    r = client.post(
        HUB, json={"name": name, "kind": "entry", "url": url, "transport": "streamable-http"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_status_carries_the_class_and_only_exported_addresses(client, fake_clusters) -> None:
    """outside 와 private 만 `external_url` 에 등록한 문자열이 실리고, 갈래는 remote·entry 에 늘
    실린다. 상세와 목록이 같다."""
    default_cluster(client)
    registered = {
        "out": ("https://mcp.example.com/mcp", "outside"),
        "vpn": ("http://10.0.0.5:8080/mcp", "private"),
        "loop": ("http://127.0.0.1\\@example.com/mcp", "local"),
        "key": ("https://mcp.example.com/mcp?key=abc", "credentials"),
    }
    ids = {name: _entry(client, name, url)["id"] for name, (url, _) in registered.items()}
    listed = {i["name"]: i["status"] for i in client.get(HUB).json()}
    for name, (url, cls) in registered.items():
        detail = client.get(f"{HUB}/{ids[name]}").json()["status"]
        exported = url if cls in ("outside", "private") else None
        assert (detail["external_url"], detail["address_class"]) == (exported, cls), name
        assert (listed[name]["external_url"], listed[name]["address_class"]) == (exported, cls)
        # 클러스터 안에서 부를 주소는 갈래와 상관없이 등록한 그대로다
        assert detail["url"] == url
