"""본문을 글로 바꾸기 (P39b 10절)."""

from __future__ import annotations

from webtools.page import decode, render


def test_html_becomes_markdown_of_the_main_part(html_page):
    page = render("text/html; charset=utf-8", html_page, "https://site.example.com/a/b")
    assert page.title == "Example Page"
    assert "# Hello" in page.text
    assert "[docs](https://site.example.com/docs)" in page.text
    assert "javascript" not in page.text and "this" in page.text
    assert "a cat" in page.text and "x.png" not in page.text
    for dropped in ("evil", "menu", "foot", "input"):
        assert dropped not in page.text


def test_article_then_body():
    page = render(
        "text/html", b"<body><p>outside</p><article>inside</article></body>", "https://x/"
    )
    assert page.text == "inside"
    page = render("text/html", b"<body><p>only</p></body>", "https://x/")
    assert page.text == "only"


def test_charset_from_header_then_meta_then_utf8():
    korean = "안녕하세요".encode("euc-kr")
    assert decode("text/html; charset=euc-kr", korean) == "안녕하세요"
    meta = b'<html><head><meta charset="EUC-KR"></head><body>' + korean + b"</body></html>"
    assert "안녕하세요" in render("text/html", meta, "https://x/").text
    assert decode("text/plain", "é".encode()) == "é"


def test_json_and_xml_pass_as_text():
    body = b'{"a": "<b>x</b>"}'
    assert render("application/json", body, "https://x/").text == body.decode()
    xml = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    assert render("application/xml", xml, "https://x/").text == xml.decode()
