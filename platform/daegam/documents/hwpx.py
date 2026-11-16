"""한글 hwpx 에서 글을 뽑는다 (D61). 꾸러미 열기와 문단 읽기는 `ooxml` 의 것을 함께 쓴다.

구역 차례는 `Contents/content.hpf`(OPF) 가 정한다. spine 의 차례가 먼저이고, spine 에 구역이
없으면 manifest 의 차례이며, hpf 가 없으면 `Contents/section<n>.xml` 의 숫자 차례다. OPF 의
네임스페이스는 판마다 달라 요소의 지역 이름으로 읽는다. 구역마다 문단(`hp:p`)의 글(`hp:t`)을 문서
차례로 잇고, 표 칸 안의 문단은 바깥 문단 뒤에 따로 한 줄이다. 옛 이진 형식 `.hwp` 는 받지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from lxml import etree

from daegam.documents.ooxml import block_lines, open_package, parse_part
from daegam.documents.result import Extracted

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
MANIFEST = "Contents/content.hpf"
SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")
# `hp:t` 안에서 글이 되는 요소. 그 밖의 요소(형광펜 표시 등)는 꼬리 글만 남는다
_INLINE = {f"{HP}tab": "\t", f"{HP}lineBreak": "\n", f"{HP}nbSpace": " ", f"{HP}fwSpace": " "}


def _t_text(node: etree._Element) -> str:
    parts = [node.text or ""]
    for child in node:
        parts.append(_INLINE.get(child.tag, "") + (child.tail or ""))
    return "".join(parts)


READERS = {f"{HP}t": _t_text}


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _package_path(href: str) -> str:
    """hpf 의 href 는 꾸러미 뿌리에서 센 경로다. hpf 곁(`Contents/`)에서 센 짧은 꼴도 받는다."""
    return href if href.startswith("Contents/") else f"Contents/{href}"


def _manifest_sections(parts: Mapping[str, bytes]) -> list[str]:
    """hpf 의 spine 차례, 없으면 manifest 차례로 구역 경로를 낸다."""
    root = parse_part(parts, MANIFEST)
    hrefs = {
        item.get("id"): _package_path(item.get("href") or "")
        for item in root.iter()
        if _local(item) == "item"
    }
    spine = [hrefs.get(ref.get("idref"), "") for ref in root.iter() if _local(ref) == "itemref"]
    for order in (spine, list(hrefs.values())):
        sections = [href for href in order if SECTION_RE.fullmatch(href)]
        if sections:
            return sections
    return []


def _section_paths(parts: Mapping[str, bytes]) -> list[str]:
    if MANIFEST in parts and (sections := _manifest_sections(parts)):
        return sections
    numbered = (m for name in parts if (m := SECTION_RE.fullmatch(name)))
    return [m.group(0) for m in sorted(numbered, key=lambda m: int(m.group(1)))]


def hwpx(data: bytes) -> Extracted:
    parts = open_package(data)
    lines: list[str] = []
    for path in _section_paths(parts):
        if path in parts:
            root = parse_part(parts, path)
            lines.extend(block_lines(root, tag=f"{HP}p", readers=READERS, skip=frozenset()))
    return Extracted("\n\n".join(lines))


__all__ = ["hwpx"]
