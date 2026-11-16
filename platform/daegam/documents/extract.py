"""파서가 필요한 형식에서 글을 뽑는 입구 (D32, D61). `daegam-docparse` 의 하위 프로세스만 부른다.

텍스트 형식은 여기 오지 않는다. 플랫폼이 서비스를 부르지 않고 바로 푼다(`services/docparse.py`).

- HTML: 안전 파서로 열어 `text_content` 다. script·style 처럼 글이 아닌 요소는 걷는다.
- PDF: pypdf 로 쪽마다 뽑는다. 암호는 빈 사용자 암호를 먼저 시도하고 안 되면 `encrypted_document`
  다. 2000쪽에서 멈추고 `truncated` 다. 첨부는 쪽마다 `[page n]` 머리를 넣고, 인덱서는 쪽을 빈 줄로
  잇는다.
- docx·pptx·xlsx: `ooxml`. hwpx: `hwpx`.

파서의 예외는 종류가 많아 `extract_failed` 하나와 예외 클래스 이름으로 접는다. 문서의 글은 오류에
싣지 않는다.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import lxml.html
from lxml import etree
from pypdf import PasswordType, PdfReader

from daegam.documents import hwpx, ooxml
from daegam.documents.result import UPLOAD, Extracted, ExtractError, limited
from daegam.xmlsafe import safe_html_parser

MAX_PDF_PAGES = 2000
# HTML 에서 글로 치지 않는 요소
HTML_DROP = ("script", "style", "noscript", "template")


def _html(data: bytes) -> Extracted:
    text = data.decode("utf-8", errors="replace")
    try:
        root = lxml.html.document_fromstring(text, parser=safe_html_parser())
    except etree.ParserError:  # 빈 문서
        return Extracted("")
    for element in list(root.iter(*HTML_DROP)):
        element.drop_tree()
    return Extracted(root.text_content())


def _pdf(data: bytes, *, page_heads: bool) -> Extracted:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
        raise ExtractError("encrypted_document", "the PDF needs a password")
    total = len(reader.pages)
    texts: list[str] = []
    for n in range(min(total, MAX_PDF_PAGES)):
        text = (reader.pages[n].extract_text() or "").strip()
        if text:
            texts.append(f"[page {n + 1}]\n{text}" if page_heads else text)
    return Extracted("\n\n".join(texts), pages=total, truncated=total > MAX_PDF_PAGES)


_ZIPPED: dict[str, Callable[[bytes], Extracted]] = {
    "docx": ooxml.docx,
    "pptx": ooxml.pptx,
    "xlsx": ooxml.xlsx,
    "hwpx": hwpx.hwpx,
}


def _run(fmt: str, data: bytes, purpose: str) -> Extracted:
    if fmt == "html":
        return _html(data)
    if fmt == "pdf":
        return _pdf(data, page_heads=purpose == UPLOAD)
    read = _ZIPPED.get(fmt)
    if read is None:
        raise ExtractError("unsupported_format", f"{fmt} is not extracted by this service")
    return read(data)


def extract(fmt: str, data: bytes, *, purpose: str) -> Extracted:
    """형식 하나의 글. 실패는 모두 `ExtractError` 다."""
    try:
        extracted = _run(fmt, data, purpose)
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - lxml·pypdf·openpyxl 예외는 종류가 많다. 코드 하나로 접는다
        raise ExtractError("extract_failed", type(e).__name__) from e
    return limited(extracted, purpose)


__all__ = ["MAX_PDF_PAGES", "extract"]
