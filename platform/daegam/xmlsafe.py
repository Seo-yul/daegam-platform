"""남이 준 XML·HTML 을 여는 문. lxml 파서 설정을 한 곳에 둔다.

외부 엔티티를 풀지 않고(XXE), 망을 타지 않고, 거대 트리와 헐거운 복구를 막는다. 이 설정이 두 곳에
있으면 한쪽을 늦추고 다른 쪽은 그대로 남는 날이 온다. 남의 문서를 읽는 자리(WSDL 판별, 비서의
가져오기, 첨부·Knowledge 문서 추출)는 여기서 받아 간다. libxml2 는 엔티티를 풀지 않아도 선언의
증폭을 재어 billion laughs 를 구문 오류로 끊는다.

문서 추출(D61)은 한 겹 더 막는다. OOXML·HWPX 의 XML 조각에는 DTD 가 올 까닭이 없으므로
`parse_without_dtd` 는 DOCTYPE 이 있으면 트리를 만들기 전에 거절한다. xlsx 는 openpyxl 이 자기
파서(expat)로 다시 읽으므로 그 앞에서 `has_doctype` 으로 조각마다 본다. 루트 요소 앞까지만 읽어 큰
시트를 통째로 트리로 만들지 않는다.
"""

from __future__ import annotations

import io

import lxml.html
from lxml import etree

# 본문이 XML 인지 앞부분으로 가를 때 읽는 길이다. 큰 본문을 통째로 훑지 않는다
XML_PROBE_BYTES = 4096


class DoctypeRejected(ValueError):
    """DTD 가 든 XML 이다. 문서 추출은 이것을 열지 않는다."""


def looks_like_xml(data: bytes) -> bool:
    """앞 `XML_PROBE_BYTES` 바이트에서 BOM·공백을 걷고 나서 `<` 로 시작하면 XML 로 본다."""
    return data[:XML_PROBE_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


def safe_xml_parser() -> etree.XMLParser:
    """남이 준 XML 을 읽을 때 쓰는 파서."""
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)


def safe_html_parser() -> lxml.html.HTMLParser:
    """남이 준 HTML 을 읽을 때 쓰는 파서. 망을 타지 않고 거대 트리를 막으며 주석과 처리 지시는
    버린다."""
    return lxml.html.HTMLParser(
        no_network=True, huge_tree=False, remove_comments=True, remove_pis=True
    )


def has_doctype(data: bytes) -> bool:
    """루트 요소의 시작까지만 읽어 DOCTYPE 이 있는지 본다. 구문이 틀리면
    `etree.XMLSyntaxError` 다."""
    events = etree.iterparse(
        io.BytesIO(data),
        events=("start",),
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
    )
    for _event, element in events:
        return bool(element.getroottree().docinfo.doctype)
    return False


def parse_without_dtd(data: bytes) -> etree._Element:
    """DTD 가 없는 XML 만 트리로 연다. 있으면 `DoctypeRejected`, 구문이 틀리면
    `etree.XMLSyntaxError` 다."""
    if has_doctype(data):
        raise DoctypeRejected("XML parts must not declare a DTD")
    return etree.fromstring(data, parser=safe_xml_parser())


__all__ = [
    "XML_PROBE_BYTES",
    "DoctypeRejected",
    "has_doctype",
    "looks_like_xml",
    "parse_without_dtd",
    "safe_html_parser",
    "safe_xml_parser",
]
