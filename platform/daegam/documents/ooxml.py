"""zip 으로 된 문서에서 글을 뽑는다. OOXML 셋(docx·pptx·xlsx)과 넷이 함께 쓰는 꾸러미 열기 (D61).

넷(docx·pptx·xlsx·hwpx)은 먼저 `open_package` 로 `read_zip` 을 지난다. 상한은 항목 2000개, 합
50MiB, 항목 50MiB, 압축률 100 이다. zip 오류는 `archive_too_large` 는 그대로, `archive_encrypted` 는
PDF 와 같은 `encrypted_document`, 나머지는 `invalid_archive` 로 옮긴다. XML 조각은 DTD 가 없는 것만
안전 파서로 연다(`xmlsafe.parse_without_dtd`). 글을 뽑지 않는 조각(그림 등)은 풀지 않는다. 오류의
detail 은 zip 코드 같은 우리 문장뿐이고, 문서가 정한 조각 이름은 싣지 않는다.

- docx: `word/document.xml` 본문의 문단 차례다. 표는 행마다 한 줄이고 칸은 탭으로 가른다. 내용
  컨트롤(`w:sdt`) 안의 문단도 그 자리에서 읽고, 지운 글과 필드 명령은 읽지 않는다.
- pptx: 슬라이드 차례는 `ppt/presentation.xml` 의 `sldIdLst` 가 가리키는 관계를 따른다. 슬라이드마다
  `## Slide n` 머리와 도형의 문단을 내고, 발표자 노트(노트 슬라이드의 본문 자리표시)는 `Notes:` 한
  줄이다.
- xlsx: openpyxl 을 읽기 전용·값만(`read_only`, `data_only`)으로 연다. 시트마다 `## Sheet: 이름` 과
  CSV 줄이고 날짜는 ISO 다. 50 시트·20만 칸에서 멈추고 `truncated` 다. openpyxl 은 자기 XML 파서를
  쓰므로 그 앞에서 XML 로 보이는 모든 조각의 DTD 를 거절한다.

문단 안에 든 문단(글상자, 표 칸)은 바깥 문단의 줄 뒤에 따로 한 줄이다. 대체 내용(`mc:Fallback`)은
같은 글을 한 번 더 담으므로 읽지 않는다.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import posixpath
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import openpyxl
from lxml import etree

from daegam.documents.formats import MIB
from daegam.documents.result import Extracted, ExtractError
from daegam.xmlsafe import (
    XML_PROBE_BYTES,
    DoctypeRejected,
    has_doctype,
    looks_like_xml,
    parse_without_dtd,
)
from daegam.zipsafe import ArchiveError, read_zip

MAX_ENTRIES = 2000
MAX_TOTAL_BYTES = 50 * MIB
MAX_ENTRY_BYTES = 50 * MIB
MAX_RATIO = 100
MAX_SHEETS = 50
MAX_CELLS = 200_000
# zip 오류 코드 → 추출 오류 코드. 표에 없는 것은 invalid_archive 다
ARCHIVE_CODES = {
    "archive_too_large": "archive_too_large",
    "archive_encrypted": "encrypted_document",
}
XML_PART_SUFFIXES = (".xml", ".rels", ".hpf")
# 오류의 detail 은 우리가 쓴 문장뿐이다. 조각 이름은 문서가 정한 글이라 싣지 않는다
DTD_DETAIL = "an XML part declares a DTD"
# BOM 이 있거나 없는 UTF-16 의 첫 두 바이트
_UTF16_STARTS = frozenset({b"\xff\xfe", b"\xfe\xff", b"<\x00", b"\x00<"})

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

Reader = Callable[[etree._Element], str]


def _text_of(node: etree._Element) -> str:
    return node.text or ""


def _put(value: str) -> Reader:
    return lambda _node: value


# 문단 안에서 글이 되는 요소. 표에 없는 요소는 그 아래를 읽는다
DOCX_READERS: Mapping[str, Reader] = {
    f"{W}t": _text_of,
    f"{W}tab": _put("\t"),
    f"{W}br": _put("\n"),
    f"{W}cr": _put("\n"),
}
DOCX_SKIP = frozenset({MC_FALLBACK, f"{W}delText", f"{W}instrText"})
DRAWING_READERS: Mapping[str, Reader] = {f"{A}t": _text_of, f"{A}br": _put("\n")}
DRAWING_SKIP = frozenset({MC_FALLBACK})


# -- 꾸러미와 조각 ------------------------------------------------------------------------


def _not_xml_part(name: str) -> bool:
    return not name.endswith(XML_PART_SUFFIXES)


def open_package(data: bytes, *, xml_only: bool = True) -> dict[str, bytes]:
    """zip 을 상한 안에서 푼다. `xml_only` 면 XML 조각만 풀고 나머지는 이름만 검사한다."""
    try:
        contents = read_zip(
            data,
            max_entries=MAX_ENTRIES,
            max_total=MAX_TOTAL_BYTES,
            max_entry=MAX_ENTRY_BYTES,
            max_ratio=MAX_RATIO,
            skip=_not_xml_part if xml_only else None,
        )
    except ArchiveError as e:
        raise ExtractError(ARCHIVE_CODES.get(e.code, "invalid_archive"), e.code) from e
    return contents.files


def parse_part(parts: Mapping[str, bytes], name: str) -> etree._Element:
    """꾸러미의 XML 조각 하나. 없으면 `invalid_archive`, DTD 가 있으면 `extract_failed` 다."""
    body = parts.get(name)
    if body is None:
        raise ExtractError("invalid_archive", f"{name} is missing")
    try:
        return parse_without_dtd(body)
    except DoctypeRejected as e:
        raise ExtractError("extract_failed", DTD_DETAIL) from e


def relationships(parts: Mapping[str, bytes], part: str) -> dict[str, tuple[str, str]]:
    """조각의 관계 {Id: (Type, 꾸러미 안 경로)}. 관계 파일이 없으면 비어 있고 바깥 링크는 뺀다."""
    folder, file = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", f"{file}.rels")
    if rels not in parts:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for rel in parse_part(parts, rels).iter(f"{PKG_REL}Relationship"):
        target = rel.get("Target") or ""
        if rel.get("TargetMode") == "External" or not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
        out[rel.get("Id") or ""] = (rel.get("Type") or "", posixpath.normpath(path))
    return out


# -- 문단 -----------------------------------------------------------------------------


def paragraph_lines(
    paragraph: etree._Element, *, tag: str, readers: Mapping[str, Reader], skip: frozenset[str]
) -> Iterator[str]:
    """문단 하나의 글 한 줄과, 그 안에 든 문단의 줄을 차례로 낸다. 빈 줄은 내지 않는다."""
    parts: list[str] = []
    nested: list[etree._Element] = []

    def walk(node: etree._Element) -> None:
        for child in node:
            if child.tag in skip:
                continue
            if child.tag == tag:
                nested.append(child)
            elif (read := readers.get(child.tag)) is not None:
                parts.append(read(child))
            else:
                walk(child)

    walk(paragraph)
    if line := "".join(parts).strip():
        yield line
    for inner in nested:
        yield from paragraph_lines(inner, tag=tag, readers=readers, skip=skip)


def block_lines(
    element: etree._Element, *, tag: str, readers: Mapping[str, Reader], skip: frozenset[str]
) -> Iterator[str]:
    """element 아래의 문단을 문서 차례로 한 줄씩 낸다."""
    for child in element:
        if child.tag in skip:
            continue
        if child.tag == tag:
            yield from paragraph_lines(child, tag=tag, readers=readers, skip=skip)
        else:
            yield from block_lines(child, tag=tag, readers=readers, skip=skip)


# -- docx -----------------------------------------------------------------------------


def _docx_paragraphs(paragraph: etree._Element) -> Iterator[str]:
    return paragraph_lines(paragraph, tag=f"{W}p", readers=DOCX_READERS, skip=DOCX_SKIP)


def _docx_blocks(element: etree._Element) -> Iterator[str]:
    """본문의 덩이를 차례로 낸다. 문단은 줄 하나, 표는 행을 줄로 이은 덩이 하나다."""
    for child in element:
        if child.tag in DOCX_SKIP:
            continue
        if child.tag == f"{W}p":
            yield from _docx_paragraphs(child)
        elif child.tag == f"{W}tbl":
            if rows := "\n".join(row for row in _docx_rows(child) if row.strip()):
                yield rows
        else:
            yield from _docx_blocks(child)


def _docx_rows(table: etree._Element) -> Iterator[str]:
    for row in table.iterchildren(f"{W}tr"):
        cells = (" ".join(_docx_blocks(cell)) for cell in row.iterchildren(f"{W}tc"))
        yield "\t".join(cell.replace("\t", " ").replace("\n", " ") for cell in cells)


def docx(data: bytes) -> Extracted:
    parts = open_package(data)
    body = parse_part(parts, "word/document.xml").find(f"{W}body")
    blocks = [] if body is None else list(_docx_blocks(body))
    return Extracted("\n\n".join(blocks))


# -- pptx -----------------------------------------------------------------------------


def _slide_paths(parts: Mapping[str, bytes]) -> list[str]:
    presentation = "ppt/presentation.xml"
    rels = relationships(parts, presentation)
    ids = parse_part(parts, presentation).iterfind(f"{P}sldIdLst/{P}sldId")
    return [rels[rid][1] for slide in ids if (rid := slide.get(f"{R}id")) in rels]


def _drawing_lines(element: etree._Element) -> Iterator[str]:
    return block_lines(element, tag=f"{A}p", readers=DRAWING_READERS, skip=DRAWING_SKIP)


def _notes(parts: Mapping[str, bytes], slide: str) -> str:
    """노트 슬라이드의 본문 자리표시 글. 슬라이드 번호 같은 다른 자리표시는 뺀다."""
    for rel_type, path in relationships(parts, slide).values():
        if rel_type.endswith("/notesSlide") and path in parts:
            shapes = parse_part(parts, path).iter(f"{P}sp")
            bodies = (s for s in shapes if _placeholder_type(s) == "body")
            return " ".join(line for shape in bodies for line in _drawing_lines(shape))
    return ""


def _placeholder_type(shape: etree._Element) -> str | None:
    placeholder = shape.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
    return None if placeholder is None else placeholder.get("type")


def pptx(data: bytes) -> Extracted:
    parts = open_package(data)
    slides = _slide_paths(parts)
    blocks: list[str] = []
    for n, path in enumerate(slides, 1):
        lines = [f"## Slide {n}"]
        if path in parts:
            lines.extend(_drawing_lines(parse_part(parts, path)))
            if notes := _notes(parts, path):
                lines.append(f"Notes: {notes}")
        blocks.append("\n".join(lines))
    return Extracted("\n\n".join(blocks), pages=len(slides))


# -- xlsx -----------------------------------------------------------------------------


def _xml_like(body: bytes) -> bool:
    """XML 로 읽힐 본문의 머리. UTF-8 은 BOM·공백 뒤의 `<`, UTF-16 은 BOM 이나, BOM 없이 LE·BE
    로 풀어 공백 뒤에 `<` 가 오는 꼴이다. expat 은 앞 두 바이트의 NUL 로 BOM 없는 UTF-16 을 알아보고
    앞의 공백도 받는데, libxml2 는 그 본문을 UTF-8 로 읽다 실패하므로 여기서 XML 로 셈해야 거절된다.
    NUL 로 시작하는 그림(EMF 의 `01 00 00 00`)은 풀어도 `<` 가 오지 않아 넘어간다."""
    if looks_like_xml(body) or body[:2] in _UTF16_STARTS:
        return True
    head = body[:XML_PROBE_BYTES]
    head = head[: len(head) - len(head) % 2]
    return any(
        head.decode(codec, errors="replace").lstrip().startswith("<")
        for codec in ("utf-16-le", "utf-16-be")
    )


def _reject_dtds(parts: Mapping[str, bytes]) -> None:
    """모든 조각에서 DTD 를 거절한다. openpyxl 은 관계가 가리키는 아무 이름이나 XML 로 읽기
    때문이다. XML 로 보이는데 구문이 틀린 조각도 거절하고, 그림처럼 XML 이 아닌 조각은 넘어간다."""
    for body in parts.values():
        try:
            declared = has_doctype(body)
        except etree.XMLSyntaxError as e:
            if _xml_like(body):
                raise ExtractError("extract_failed", "an XML part is not well-formed") from e
            continue
        if declared:
            raise ExtractError("extract_failed", DTD_DETAIL)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime) and value.time() == dt.time():
        return value.date().isoformat()
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    return str(value)


def _csv_line(values: tuple[Any, ...]) -> str:
    cells = [_cell(v) for v in values]
    while cells and not cells[-1]:
        cells.pop()
    if not cells:
        return ""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(cells)
    return buf.getvalue()


def _sheet_blocks(sheets: list[Any]) -> tuple[list[str], bool]:
    """시트마다 덩이 하나. 칸 상한에 이르면 거기서 멈추고 잘렸다고 알린다."""
    blocks: list[str] = []
    cells = 0
    for sheet in sheets[:MAX_SHEETS]:
        sheet.reset_dimensions()  # 선언한 범위를 믿지 않는다. 빈 칸으로 채우지 않게 한다
        lines = [f"## Sheet: {sheet.title}"]
        blocks.append("")
        for row in sheet.iter_rows(values_only=True):
            cells += len(row)
            if cells > MAX_CELLS:
                blocks[-1] = "\n".join(lines)
                return blocks, True
            if line := _csv_line(row):
                lines.append(line)
        blocks[-1] = "\n".join(lines)
    return blocks, len(sheets) > MAX_SHEETS


def xlsx(data: bytes) -> Extracted:
    _reject_dtds(open_package(data, xml_only=False))
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheets = list(book.worksheets)
        blocks, truncated = _sheet_blocks(sheets)
    finally:
        book.close()
    return Extracted("\n\n".join(blocks), sheets=len(sheets), truncated=truncated)


__all__ = [
    "MAX_CELLS",
    "MAX_ENTRIES",
    "MAX_SHEETS",
    "block_lines",
    "docx",
    "open_package",
    "paragraph_lines",
    "parse_part",
    "pptx",
    "relationships",
    "xlsx",
]
