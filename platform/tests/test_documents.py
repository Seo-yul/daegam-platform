"""문서에서 글을 뽑는 함수 (D32, D61). 형식 표, 형식마다의 추출과 차례, 암호, XML 폭탄과 외부
엔티티, 빈 글, 첨부의 50만 자 자르기와 쪽 머리, zip 오류 코드의 옮김.

여기의 추출은 하위 프로세스 없이 같은 프로세스에서 부른다. 가두기와 서버는 `test_docparse.py` 다.
"""

from __future__ import annotations

import datetime as dt
import io
import stat
import zipfile

import pytest

from daegam.documents import formats as F
from daegam.documents import ooxml
from daegam.documents.extract import MAX_PDF_PAGES, extract
from daegam.documents.result import INDEX, MAX_UPLOAD_CHARS, UPLOAD, Extracted, ExtractError
from tests.helpers.documents import (
    BILLION_LAUGHS,
    XML_HEAD,
    hp_p,
    make_docx,
    make_hwpx,
    make_pdf,
    make_pptx,
    make_xlsx,
    replace_entries,
    set_central_flags,
    varied_text,
    w_p,
    zip_of,
)


def _code(fmt: str, data: bytes, purpose: str = UPLOAD) -> str:
    with pytest.raises(ExtractError) as e:
        extract(fmt, data, purpose=purpose)
    return e.value.code


# -- 형식 표 -----------------------------------------------------------------------------


def test_format_of_extension_first_then_content_type():
    assert F.format_of("application/octet-stream", "notes.MD") == "markdown"
    assert F.format_of("text/html; charset=utf-8", None) == "html"
    assert F.format_of("text/html", "page") == "html"
    assert F.format_of(None, "a.htm") == "html"
    assert F.format_of("application/pdf", "x.bin") == "pdf"
    assert F.format_of(None, "보고서.HWPX") == "hwpx"
    assert F.format_of(None, "표.csv") == "csv" and F.format_of(None, "a.tsv") == "tsv"
    assert F.format_of(F.CONTENT_TYPES["docx"], "upload") == "docx"
    assert F.format_of(None, None) is None


def test_images_are_formats_only_where_they_are_accepted():
    """Knowledge Base 는 문서만 받는다. 확장자가 이미지면 content type 으로 물러나지 않는다."""
    assert F.format_of("image/png", "a.png") is None
    assert F.format_of("text/plain", "a.png") is None
    both = F.DOCUMENT_FORMATS | F.IMAGE_FORMATS
    assert F.format_of(None, "화면.JPG", accept=both) == "jpeg"
    assert F.format_of("image/webp", "clip", accept=both) == "webp"


def test_format_table_is_closed():
    assert F.TEXT_FORMATS.isdisjoint(F.PARSED_FORMATS)
    assert set(F.EXTENSIONS.values()) == F.DOCUMENT_FORMATS | F.IMAGE_FORMATS
    assert set(F.CONTENT_TYPES) == F.DOCUMENT_FORMATS | F.IMAGE_FORMATS
    assert ".hwp" not in F.EXTENSIONS and ".doc" not in F.EXTENSIONS
    assert {".htm", ".html", ".hwpx"} <= set(F.DOCUMENT_EXTENSIONS)
    assert ".png" not in F.DOCUMENT_EXTENSIONS


# -- HTML·PDF ----------------------------------------------------------------------------


def test_html_text_without_script_and_style():
    html = (
        b"<html><head><title>T</title><style>x{}</style></head>"
        b"<body><p>Hello</p><script>steal()</script><p>World</p></body></html>"
    )
    text = extract("html", html, purpose=UPLOAD).text
    assert "Hello" in text and "World" in text
    assert "<p>" not in text and "steal" not in text and "x{}" not in text
    assert extract("html", b"   ", purpose=UPLOAD).text == ""


def test_html_does_not_load_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    html = f'<!DOCTYPE html [<!ENTITY e SYSTEM "file://{secret}">]><p>a &e; b</p>'
    assert "TOP-SECRET" not in extract("html", html.encode(), purpose=UPLOAD).text


def test_pdf_pages_heads_only_for_attachments():
    pdf = make_pdf("Hello pdf world", "Second page")
    upload = extract("pdf", pdf, purpose=UPLOAD)
    assert upload.text == "[page 1]\nHello pdf world\n\n[page 2]\nSecond page"
    assert upload.pages == 2 and not upload.truncated
    index = extract("pdf", pdf, purpose=INDEX)
    assert index.text == "Hello pdf world\n\nSecond page" and index.pages == 2


def test_pdf_empty_user_password_opens_and_a_real_one_is_refused():
    opened = extract("pdf", make_pdf("Locked words", user_password=""), purpose=UPLOAD)
    assert "Locked words" in opened.text
    assert _code("pdf", make_pdf("Locked words", user_password="s3cret")) == "encrypted_document"


def test_pdf_stops_at_the_page_cap(monkeypatch):
    from daegam.documents import extract as extract_mod

    monkeypatch.setattr(extract_mod, "MAX_PDF_PAGES", 2)
    got = extract("pdf", make_pdf("one", "two", "three"), purpose=INDEX)
    assert got.text == "one\n\ntwo" and got.pages == 3 and got.truncated
    assert MAX_PDF_PAGES == 2000


def test_broken_documents_fold_into_extract_failed():
    assert _code("pdf", b"not a pdf at all") == "extract_failed"
    with pytest.raises(ExtractError) as e:
        extract("pdf", b"%PDF-1.4 garbage", purpose=INDEX)
    assert e.value.code == "extract_failed" and " " not in e.value.detail  # 예외 클래스 이름뿐
    assert _code("text", b"plain") == "unsupported_format"  # 텍스트 형식은 서비스가 뽑지 않는다


# -- docx --------------------------------------------------------------------------------


def test_docx_paragraphs_tables_and_controls_in_order():
    body = (
        "<w:p><w:r><w:t>첫 문단</w:t></w:r><w:r><w:tab/><w:t>탭 뒤</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc>" + w_p("A1") + "</w:tc><w:tc>" + w_p("B1") + "</w:tc></w:tr>"
        "<w:tr><w:tc>" + w_p("A2") + w_p("A2 둘째") + "</w:tc><w:tc>" + w_p("B2") + "</w:tc></w:tr>"
        "</w:tbl>"
        "<w:p><w:r><w:delText>지운 글</w:delText></w:r><w:r><w:t>둘째 문단</w:t></w:r></w:p>"
        "<w:sdt><w:sdtContent>" + w_p("컨트롤 안") + "</w:sdtContent></w:sdt>"
        "<w:p><mc:AlternateContent><mc:Choice>" + w_p("글상자") + "</mc:Choice>"
        "<mc:Fallback>" + w_p("글상자") + "</mc:Fallback></mc:AlternateContent></w:p>"
    )
    got = extract("docx", make_docx(body), purpose=UPLOAD)
    assert got.text == (
        "첫 문단\t탭 뒤\n\nA1\tB1\nA2 A2 둘째\tB2\n\n둘째 문단\n\n컨트롤 안\n\n글상자"
    )
    assert got.pages is None and got.sheets is None


def test_docx_without_the_main_part_is_an_invalid_archive():
    assert _code("docx", zip_of({"word/other.xml": "<x/>"})) == "invalid_archive"
    assert _code("docx", b"PK\x03\x04 not really") == "invalid_archive"


def test_docx_xml_bombs_and_external_entities_are_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    xxe = f'<!DOCTYPE w:document [<!ENTITY e SYSTEM "file://{secret}">]>'
    with pytest.raises(ExtractError) as e:
        extract("docx", make_docx(w_p("&e;"), prolog=xxe), purpose=UPLOAD)
    assert e.value.code == "extract_failed" and "TOP-SECRET" not in str(e.value)
    assert _code("docx", make_docx(w_p("&lol9;"), prolog=BILLION_LAUGHS)) == "extract_failed"


# -- pptx --------------------------------------------------------------------------------


def test_pptx_follows_the_presentation_order_and_reads_notes():
    data = make_pptx(
        [("셋째 파일", None), ("첫 장", "발표자 노트"), ("둘째 장", None)], order=[1, 2, 0]
    )
    got = extract("pptx", data, purpose=UPLOAD)
    assert got.text == (
        "## Slide 1\n첫 장\nNotes: 발표자 노트\n\n## Slide 2\n둘째 장\n\n## Slide 3\n셋째 파일"
    )
    assert got.pages == 3  # 슬라이드 수. 노트의 쪽 번호 자리표시는 읽지 않는다


# -- xlsx --------------------------------------------------------------------------------


def test_xlsx_sheets_as_csv_with_iso_dates():
    data = make_xlsx(
        {
            "매출": [
                ["이름", "금액", "날짜"],
                ["사과", 1200, dt.date(2026, 10, 3)],
                ["배, 큰 것", 3.5, dt.datetime(2026, 1, 2, 3, 4, 5)],
                [None, None, None],
            ],
            "빈 시트": [],
        }
    )
    got = extract("xlsx", data, purpose=UPLOAD)
    assert got.text == (
        "## Sheet: 매출\n이름,금액,날짜\n사과,1200,2026-10-03\n"
        '"배, 큰 것",3.5,2026-01-02T03:04:05\n\n## Sheet: 빈 시트'
    )
    assert got.sheets == 2 and not got.truncated


def test_xlsx_stops_at_the_cell_and_sheet_caps(monkeypatch):
    monkeypatch.setattr(ooxml, "MAX_CELLS", 5)
    got = extract("xlsx", make_xlsx({"s": [[1, 2], [3, 4], [5, 6]]}), purpose=INDEX)
    assert got.text == "## Sheet: s\n1,2\n3,4" and got.truncated
    monkeypatch.setattr(ooxml, "MAX_CELLS", 200_000)
    monkeypatch.setattr(ooxml, "MAX_SHEETS", 1)
    got = extract("xlsx", make_xlsx({"a": [["x"]], "b": [["y"]]}), purpose=INDEX)
    assert got.text == "## Sheet: a\nx" and got.sheets == 2 and got.truncated


@pytest.mark.parametrize("part", ["xl/worksheets/sheet1.xml", "xl/sharedStrings.xml"])
def test_xlsx_dtds_never_reach_openpyxl(tmp_path, part):
    """openpyxl 은 자기 파서(expat)를 쓴다. 외부 엔티티와 billion laughs 는 그 앞에서 거절된다."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    base = make_xlsx({"s": [["hello"]]})
    xxe = (
        f'{XML_HEAD}<!DOCTYPE x [<!ENTITY e SYSTEM "file://{secret}">]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&e;</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    bomb = f"{XML_HEAD}{BILLION_LAUGHS}<sst><si><t>&lol9;</t></si></sst>"
    for body in (xxe, bomb):
        with pytest.raises(ExtractError) as e:
            extract("xlsx", replace_entries(base, {part: body}), purpose=UPLOAD)
        assert e.value.code == "extract_failed" and "TOP-SECRET" not in str(e.value)
    # 이름이 XML 이 아니어도 XML 로 보이면 같은 검사를 지난다
    renamed = replace_entries(base, {"xl/media/data.bin": xxe})
    assert _code("xlsx", renamed) == "extract_failed"


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("part", ["xl/worksheets/sheet1.xml", "xl/sharedStrings.xml"])
def test_xlsx_dtds_in_utf16_without_a_bom_never_reach_openpyxl(part, codec):
    """BOM 없는 UTF-16 을 공백으로 시작하면 libxml2 는 읽지 못하지만 expat 은 읽는다. 그 조각도
    XML 로 보고 거절하므로 내부 엔티티가 openpyxl 에서 풀리지 않는다."""
    body = (
        ' <!DOCTYPE x [<!ENTITY e "ENTITY-EXPANDED">]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&e;</t></is></c></row>'
        "</sheetData></worksheet>"
    ).encode(codec)
    data = replace_entries(make_xlsx({"s": [["hello"]]}), {part: body})
    with pytest.raises(ExtractError) as e:
        extract("xlsx", data, purpose=UPLOAD)
    assert e.value.code == "extract_failed" and "ENTITY-EXPANDED" not in str(e.value)


def test_a_picture_that_starts_with_nul_is_not_read_as_xml():
    """EMF 그림은 `01 00 00 00` 으로 시작한다. UTF-16 으로 풀어도 `<` 가 오지 않으므로 넘어간다."""
    emf = b"\x01\x00\x00\x00" + bytes(range(256)) * 4
    data = replace_entries(make_xlsx({"s": [["hello"]]}), {"xl/media/image1.emf": emf})
    assert extract("xlsx", data, purpose=UPLOAD).text == "## Sheet: s\nhello"


# -- hwpx --------------------------------------------------------------------------------


def test_hwpx_sections_follow_the_spine_then_manifest_then_numbers():
    sections = [hp_p("영 구역"), hp_p("일 구역"), hp_p("이 구역")]
    spine = extract("hwpx", make_hwpx(sections, spine=[2, 0, 1]), purpose=UPLOAD)
    assert spine.text == "이 구역\n\n영 구역\n\n일 구역"
    manifest = extract("hwpx", make_hwpx(sections, spine=[]), purpose=UPLOAD)
    assert manifest.text == "영 구역\n\n일 구역\n\n이 구역"
    numbered = make_hwpx([hp_p(f"구역 {n}") for n in range(11)], manifest=False)
    got = extract("hwpx", numbered, purpose=UPLOAD).text.split("\n\n")
    assert got == [f"구역 {n}" for n in range(11)]  # 10 이 2 앞에 오지 않는다


def test_hwpx_table_cells_are_their_own_lines():
    table = "<hp:tbl><hp:tr><hp:tc><hp:subList>{}</hp:subList></hp:tc></hp:tr></hp:tbl>"
    cell = table.format(hp_p("칸"))
    body = hp_p("앞", inner=f"<hp:run>{cell}</hp:run><hp:run><hp:t>뒤<hp:tab/>끝</hp:t></hp:run>")
    got = extract("hwpx", make_hwpx([body], spine=[0]), purpose=UPLOAD)
    assert got.text == "앞뒤\t끝\n\n칸"


# -- 빈 글·자르기 ---------------------------------------------------------------------------


def test_empty_text_is_not_an_error_here():
    """빈 글을 오류로 바꾸는 것은 부르는 쪽이다(첨부 422, 인덱서 실패 모두 empty_document)."""
    assert extract("docx", make_docx(""), purpose=UPLOAD).text == ""
    assert extract("hwpx", make_hwpx([""], spine=[0]), purpose=UPLOAD).text == ""


def test_attachments_are_cut_at_half_a_million_characters_and_index_text_is_not():
    text = varied_text(MAX_UPLOAD_CHARS + 5000)
    long_body = "".join(w_p(text[i : i + 1000]) for i in range(0, len(text), 1000))
    data = make_docx(long_body)
    upload = extract("docx", data, purpose=UPLOAD)
    assert len(upload.text) == MAX_UPLOAD_CHARS and upload.truncated
    index = extract("docx", data, purpose=INDEX)
    assert len(index.text) > MAX_UPLOAD_CHARS and not index.truncated


def test_extracted_from_dict_checks_every_field():
    assert Extracted.from_dict({"text": "a", "pages": 2, "truncated": True}) == Extracted(
        "a", pages=2, truncated=True
    )
    for bad in ({}, {"text": 1}, {"text": "a", "pages": "2"}, {"text": "a", "sheets": -1}, []):
        with pytest.raises(ExtractError):
            Extracted.from_dict(bad)


# -- zip 오류의 옮김 -------------------------------------------------------------------------


def _with_entry(info: zipfile.ZipInfo, body: bytes = b"<x/>") -> bytes:
    with zipfile.ZipFile(io.BytesIO(make_docx(w_p("본문")))) as archive:
        entries = {i.filename: archive.read(i) for i in archive.infolist()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr(info, body)
    return buf.getvalue()


def test_archive_codes_map_to_the_p36_answers():
    assert (
        extract("docx", _with_entry(zipfile.ZipInfo("word/x.xml")), purpose=UPLOAD).text == "본문"
    )
    encrypted = set_central_flags(
        _with_entry(zipfile.ZipInfo("word/secret.xml")), "word/secret.xml", 1
    )
    assert _code("docx", encrypted) == "encrypted_document"
    link = zipfile.ZipInfo("word/link.xml")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert _code("docx", _with_entry(link)) == "invalid_archive"
    assert _code("docx", _with_entry(zipfile.ZipInfo("../escape.xml"))) == "invalid_archive"
    bomb = zip_of({"word/document.xml": b" " * (2 * 1024 * 1024)})
    with pytest.raises(ExtractError) as e:
        extract("docx", bomb, purpose=UPLOAD)
    assert e.value.code == "archive_too_large"
    assert e.value.detail == "archive_too_large"  # 조각 이름을 싣지 않는다
