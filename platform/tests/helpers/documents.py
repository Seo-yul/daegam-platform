"""문서 추출 테스트가 함께 쓰는 표본 만들기와 같은 프로세스의 가짜 서비스 (D61).

표본은 테스트 안에서 만든다. 형식마다 글을 뽑는 데 필요한 조각만 담은 가장 작은 꾸러미다.
`docparse_client` 는 진짜 `daegam-docparse` 앱을 ASGI 로 붙인 클라이언트이고, 추출은 하위
프로세스 대신 같은 프로세스에서 돈다(`run_in_process`). 서버와 클라이언트의 HTTP 모양을 함께 본다.
"""

from __future__ import annotations

import io
import random
import zipfile
from typing import Any

import httpx
import openpyxl
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from daegam.documents.extract import extract
from daegam.documents.server import Slots, create_app
from daegam.services.docparse import DocparseClient

TOKEN = "docparse-test-token"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
HP_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"
OPF_NS = "http://www.idpf.org/2007/opf/"
REL_SLIDE = f"{R_NS}/slide"
REL_NOTES = f"{R_NS}/notesSlide"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _lol(n: int) -> str:
    inner = f"&lol{n - 1};" if n > 1 else "&lol;"
    return f'<!ENTITY lol{n} "{inner * 10}">'


BILLION_LAUGHS = '<!DOCTYPE lolz [<!ENTITY lol "lol">' + "".join(map(_lol, range(1, 10))) + "]>"


def zip_of(entries: dict[str, bytes | str], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """꾸러미 하나. 압축하지 않으면(`ZIP_STORED`) 크기가 조각의 바이트 수만큼 정확히 는다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buf.getvalue()


def set_central_flags(data: bytes, name: str, flags: int) -> bytes:
    """중앙 디렉터리에서 그 항목의 일반 목적 비트를 켠다. zipfile 은 쓸 때 이 비트를 지운다."""
    out = bytearray(data)
    at = out.find(b"PK\x01\x02")
    while at >= 0:
        size = int.from_bytes(out[at + 28 : at + 30], "little")
        if out[at + 46 : at + 46 + size] == name.encode():
            current = int.from_bytes(out[at + 8 : at + 10], "little")
            out[at + 8 : at + 10] = (current | flags).to_bytes(2, "little")
        at = out.find(b"PK\x01\x02", at + 4)
    return bytes(out)


def varied_text(n: int, seed: int = 0) -> str:
    """압축이 잘 되지 않는 한글 n 자. 반복 글은 zip 의 압축률 상한에 걸린다."""
    rng = random.Random(seed)
    return "".join(chr(0xAC00 + rng.randrange(11172)) for _ in range(n))


def replace_entries(data: bytes, changes: dict[str, bytes | str]) -> bytes:
    """꾸러미의 몇 조각을 바꿔 새 꾸러미로 낸다."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries: dict[str, bytes | str] = {i.filename: archive.read(i) for i in archive.infolist()}
    return zip_of({**entries, **changes})


def make_pdf(*pages: str, user_password: str | None = None) -> bytes:
    """Helvetica 한 줄짜리 쪽들. pypdf 가 글을 다시 읽을 수 있는 가장 작은 PDF."""
    writer = PdfWriter()
    for line in pages:
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 100 Td ({line}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if user_password is not None:
        writer.encrypt(user_password=user_password, owner_password="owner-secret")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_docx(body: str, *, prolog: str = "", compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    document = (
        f'{XML_HEAD}{prolog}<w:document xmlns:w="{W_NS}" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return zip_of(
        {
            "[Content_Types].xml": f"{XML_HEAD}<Types/>",
            "word/document.xml": document,
            "word/media/image1.png": b"\x89PNG\r\n\x1a\n" + b"\0" * 64,
        },
        compression=compression,
    )


def w_p(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t>{text}</w:t></w:r>" for text in runs) + "</w:p>"


def _a_text(*paragraphs: str) -> str:
    return "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs)


def _shape(text: str, placeholder: str | None = None) -> str:
    ph = f'<p:nvPr><p:ph type="{placeholder}"/></p:nvPr>' if placeholder else "<p:nvPr/>"
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/>{ph}</p:nvSpPr>'
        f"<p:txBody>{_a_text(text)}</p:txBody></p:sp>"
    )


def _part(root: str, body: str) -> str:
    return (
        f'{XML_HEAD}<p:{root} xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        f"<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:{root}>"
    )


def _rels(*targets: tuple[str, str, str]) -> str:
    rels = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{p}"/>' for i, t, p in targets)
    return f'{XML_HEAD}<Relationships xmlns="{PKG_NS}">{rels}</Relationships>'


def make_pptx(slides: list[tuple[str, str | None]], *, order: list[int] | None = None) -> bytes:
    """slides[i] = (본문, 노트). 슬라이드 파일은 slide{i+1}.xml 이고 `order` 가 발표 차례다."""
    order = order if order is not None else list(range(len(slides)))
    ids = "".join(f'<p:sldId id="{256 + n}" r:id="rId{n + 1}"/>' for n in order)
    entries: dict[str, bytes | str] = {
        "[Content_Types].xml": f"{XML_HEAD}<Types/>",
        "ppt/presentation.xml": (
            f'{XML_HEAD}<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">'
            f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            *((f"rId{n + 1}", REL_SLIDE, f"slides/slide{n + 1}.xml") for n in range(len(slides)))
        ),
    }
    for n, (text, notes) in enumerate(slides, 1):
        entries[f"ppt/slides/slide{n}.xml"] = _part("sld", _shape(text))
        if notes is not None:
            notes_body = _shape("", "sldImg") + _shape(notes, "body") + _shape(str(n), "sldNum")
            entries[f"ppt/notesSlides/notesSlide{n}.xml"] = _part("notes", notes_body)
            entries[f"ppt/slides/_rels/slide{n}.xml.rels"] = _rels(
                ("rId9", REL_NOTES, f"../notesSlides/notesSlide{n}.xml")
            )
    return zip_of(entries)


def hp_p(*texts: str, inner: str = "") -> str:
    runs = "".join(f"<hp:run><hp:t>{text}</hp:t></hp:run>" for text in texts)
    return f"<hp:p>{runs}{inner}</hp:p>"


def make_hwpx(
    sections: list[str], *, spine: list[int] | None = None, manifest: bool = True
) -> bytes:
    """sections[i] 는 section{i}.xml 의 문단들이다. `spine` 이 hpf 의 차례다."""
    entries: dict[str, bytes | str] = {"mimetype": "application/hwp+zip"}
    for n, body in enumerate(sections):
        entries[f"Contents/section{n}.xml"] = (
            f'{XML_HEAD}<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
            f'xmlns:hp="{HP_NS}">{body}</hs:sec>'
        )
    if manifest:
        items = "".join(
            f'<opf:item id="s{n}" href="Contents/section{n}.xml" media-type="application/xml"/>'
            for n in range(len(sections))
        )
        refs = "".join(f'<opf:itemref idref="s{n}"/>' for n in (spine or []))
        entries["Contents/content.hpf"] = (
            f'{XML_HEAD}<opf:package xmlns:opf="{OPF_NS}"><opf:manifest>'
            '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
            f"{items}</opf:manifest><opf:spine>{refs}</opf:spine></opf:package>"
        )
    return zip_of(entries)


def make_xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


async def run_in_process(
    request: dict[str, Any], data: bytes = b"", *, timeout: float
) -> dict[str, Any]:
    """하위 프로세스 없이 같은 프로세스에서 뽑는 runner. 서버의 HTTP 모양만 볼 때 쓴다."""
    return extract(request["format"], data, purpose=request["purpose"]).to_dict()


def docparse_app(run: Any = run_in_process, *, slots: Slots | None = None, **kw: Any):
    return create_app(TOKEN, run=run, slots=slots, **kw)


def docparse_client(run: Any = run_in_process, **kw: Any) -> DocparseClient:
    """진짜 서버 앱을 ASGI 로 붙인 플랫폼 클라이언트."""
    transport = httpx.ASGITransport(app=docparse_app(run, **kw))
    return DocparseClient("http://docparse", TOKEN, transport=transport)
