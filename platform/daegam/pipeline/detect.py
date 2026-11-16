"""업로드 본문의 형식 감지: wsdl | swagger2 | openapi3 | unsupported."""

from __future__ import annotations

import json
from typing import Any, Literal

import yaml
from lxml import etree
from spec2openapi import is_swagger2

from daegam import yamlsafe
from daegam.xmlsafe import looks_like_xml, safe_xml_parser

SourceFormat = Literal["wsdl", "swagger2", "openapi3", "unsupported"]
# 형식이 unsupported 일 때의 문구. 변환 오류(convert.inspect)와 verdict 항목이 같은 말을 쓴다
UNSUPPORTED_MESSAGE = "could not recognise the document as WSDL, Swagger 2.0 or OpenAPI 3.x"

WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"


def _is_wsdl(content: bytes) -> bool:
    """루트 요소가 WSDL 네임스페이스의 definitions인지 본다. 외부 엔터티는 풀지 않는다."""
    try:
        root = etree.fromstring(content, parser=safe_xml_parser())
    except etree.XMLSyntaxError:
        return False
    return root.tag == f"{{{WSDL_NS}}}definitions"


def parse_document(content: bytes) -> Any:
    """JSON 우선, 실패하면 YAML. 둘 다 안 되면 ValueError."""
    text = content.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yamlsafe.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"not JSON or YAML: {e}") from e


def detect(content: bytes, *, filename: str | None = None) -> tuple[SourceFormat, Any]:
    """형식과 (JSON/YAML인 경우) 파싱된 문서를 돌려준다. WSDL이면 문서는 None."""
    if looks_like_xml(content):
        if _is_wsdl(content):
            return "wsdl", None
        return "unsupported", None

    try:
        doc = parse_document(content)
    except (ValueError, UnicodeDecodeError):
        return "unsupported", None

    if not isinstance(doc, dict):
        return "unsupported", None
    if is_swagger2(doc):
        return "swagger2", doc
    openapi = doc.get("openapi")
    if isinstance(openapi, str) and openapi.startswith("3."):
        return "openapi3", doc
    return "unsupported", doc
