"""가져온 본문을 모델이 읽을 글로 바꾼다 (P39b 10절).

HTML 은 lxml 의 HTML 파서(네트워크 없음, 큰 트리 거부)로 읽고 머리말·꼬리말·스크립트 같은 것을 버린
뒤 `main` 이나 `article`, 없으면 `body` 를 Markdown 으로 바꾼다. 링크는 최종 URL 기준 절대 주소가
되고 `javascript:`·`data:` 링크와 이미지는 버리며 이미지의 alt 글은 남긴다. XML 과 JSON 은 파싱하지
않고 글 그대로 넘긴다. 그래서 XML 파서가 끼지 않는다.

글자 집합은 응답 헤더, 문서의 meta, UTF-8 순이다. EUC-KR 은 그 상위 집합인 CP949 로 읽는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from lxml import etree, html
from markdownify import markdownify

TEXT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/xml",
        "text/xml",
    }
)
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
    "canvas",
    "form",
    "nav",
    "footer",
    "aside",
)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
_ALIASES = {"euc-kr": "cp949", "ks_c_5601-1987": "cp949", "x-windows-949": "cp949"}
_UNSAFE_SCHEMES = ("javascript:", "data:", "vbscript:")
TITLE_MAX = 300


@dataclass(frozen=True)
class Page:
    title: str | None
    text: str


def media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _charset(content_type: str | None, body: bytes) -> str:
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"').lower()
    m = _META_CHARSET.search(body[:4096])
    if m:
        return m.group(1).decode("ascii", "replace").lower()
    return "utf-8"


def decode(content_type: str | None, body: bytes) -> str:
    name = _charset(content_type, body)
    name = _ALIASES.get(name, name)
    try:
        return body.decode(name, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _clean_links(root: html.HtmlElement, base_url: str) -> None:
    try:
        root.make_links_absolute(base_url, resolve_base_href=True, handle_failures="discard")
    except ValueError:
        pass
    for a in root.iter("a"):
        href = (a.get("href") or "").strip()
        scheme = urlsplit(href).scheme.lower()
        if href.lower().startswith(_UNSAFE_SCHEMES) or (scheme and scheme not in ("http", "https")):
            a.attrib.pop("href", None)
    for img in list(root.iter("img")):
        alt = (img.get("alt") or "").strip()
        parent = img.getparent()
        if parent is None:
            continue
        tail = img.tail or ""
        text = f"{alt}{tail}" if alt else tail
        prev = img.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + text
        else:
            parent.text = (parent.text or "") + text
        parent.remove(img)


def html_to_markdown(text: str, base_url: str) -> Page:
    parser = html.HTMLParser(no_network=True, huge_tree=False, remove_comments=True)
    try:
        root = html.document_fromstring(text, parser=parser)
    except (etree.ParserError, ValueError):
        return Page(title=None, text="")
    title_el = root.find(".//title")
    title = " ".join((title_el.text_content() if title_el is not None else "").split())
    for tag in DROP_TAGS:
        for el in list(root.iter(tag)):
            el.drop_tree()
    main = root.find(".//main")
    if main is None:
        main = root.find(".//article")
    if main is None:
        main = root.find(".//body")
    if main is None:
        main = root
    _clean_links(main, base_url)
    fragment = html.tostring(main, encoding="unicode")
    markdown = markdownify(fragment, heading_style="ATX", strip=["img"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return Page(title=title[:TITLE_MAX] or None, text=markdown)


def render(content_type: str | None, body: bytes, final_url: str) -> Page:
    """본문을 글로 바꾼다. HTML 은 Markdown, 그 밖의 글 타입은 그대로다."""
    text = decode(content_type, body)
    if media_type(content_type) in HTML_TYPES:
        return html_to_markdown(text, final_url)
    return Page(title=None, text=text)
