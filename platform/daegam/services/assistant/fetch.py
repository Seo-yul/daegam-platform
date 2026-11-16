"""`fetch_url` 이 가져온 본문의 문지기와 판정 (D31, P14 F1a).

`ToolExecutor.fetch_url` 은 `tools.py` 에 남고 이 파일의 함수를 부른다. 여기 있는 것은 남의 서버가
준 것을 우리 어휘로 옮기는 일뿐이다. 세 가지다.

- **문지기.** `ContentTypeGate` 가 응답 헤더가 오는 순간 content-type 을 보고 허용하지 않는 것은
  본문을 읽기 전에 끊는다. 리다이렉트·오류 응답의 규칙은 `urlfetch.py` 쪽이다.
- **판정.** `classify` 가 `pipeline.detect` 로 본문을 읽어 `{kind, format, operation_count?}` 만
  낸다. 업로드 경로와 같은 파서라 여기서 openapi 라고 말한 것은 `verify_spec` 도 같게 본다.
- **이름.** `file_name_for` 가 세션 파일 이름 `fetched-<n>.<ext>` 를 짓는다.

남의 문서에서 온 문자열은 한 글자도 밖으로 내보내지 않는 것이 이 파일의 계약이다(D31). 모델에
실리는 값은 우리 어휘의 분류값, 우리가 센 수, 그리고 미디어 타입 토큰 모양을 통과한
content-type 뿐이다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from lxml import etree

from daegam.pipeline.convert import iter_operations
from daegam.pipeline.detect import WSDL_NS, detect
from daegam.store.assistant_sessions import FetchedFile
from daegam.xmlsafe import looks_like_xml, safe_xml_parser

# fetch_url 이 받는 content-type 과 그 세션 파일 확장자. 그 밖(바이너리 등)은 본문을 읽기 전에
# 거부한다. 허용 목록은 이 표의 키라 타입을 더하면 확장자도 함께 정해진다
_EXT_BY_TYPE = {
    "application/json": "json",
    "application/yaml": "yaml",
    "application/x-yaml": "yaml",
    "text/yaml": "yaml",
    "text/x-yaml": "yaml",
    "application/xml": "xml",
    "text/xml": "xml",
}
FETCH_TYPES = frozenset(_EXT_BY_TYPE)
# 판정하려고 파싱하는 본문의 상한. 넘으면 파싱하지 않고 kind 는 unknown 이다. 세션 파일 총량이
# 1MB 라 실제로 저장되는 본문은 이 안에 들지만, 판정은 그것과 무관하게 자기 상한을 지킨다
CLASSIFY_MAX_BYTES = 1024 * 1024
# 미디어 타입 토큰(RFC 9110). 남의 서버가 준 값을 모델에 실으므로 모양이 아니면 버린다
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,60}/[a-z0-9][a-z0-9.+_-]{0,60}$")
UNKNOWN_TYPE = "unknown"

FILE_PREFIX = "fetched-"
_KNOWN_EXTS = ("json", "yaml", "yml", "xml", "wsdl", "txt")

FetchKind = Literal["openapi", "swagger", "wsdl", "unknown"]
FetchFormat = Literal["json", "yaml", "xml", "text"]
# `pipeline.detect` 의 판정을 도구 결과의 어휘로 옮긴다. 업로드 경로와 같은 파서를 쓰므로
# 여기서 openapi 라고 말한 것은 verify_spec 도 같게 본다
_KIND_BY_FORMAT: dict[str, FetchKind] = {
    "openapi3": "openapi",
    "swagger2": "swagger",
    "wsdl": "wsdl",
    "unsupported": "unknown",
}


# -- content-type 문지기 ---------------------------------------------------------------------


class ContentTypeRejected(Exception):
    """허용하지 않는 content-type.

    httpx 오류가 아니므로 urlfetch.fetch_url 을 그대로 지나 올라온다.
    """

    def __init__(self, content_type: str | None) -> None:
        super().__init__(content_type)
        self.content_type = content_type


def allowed_content_type(main: str) -> bool:
    return main in FETCH_TYPES or main.startswith("text/") or main.endswith(("+json", "+xml"))


class ContentTypeGate(httpx.AsyncBaseTransport):
    """urlfetch.fetch_url 은 본문만 돌려준다. 응답 헤더가 오는 순간 content-type 을 보고
    허용하지 않는 것은 본문을 읽기 전에 끊는다. 리다이렉트·오류 응답은 urlfetch 쪽 규칙에 맡긴다."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.main_type: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if response.status_code < 300:
            raw = response.headers.get("content-type") or ""
            main = raw.split(";", 1)[0].strip().lower()
            self.main_type = main
            if not allowed_content_type(main):
                await response.aclose()
                raise ContentTypeRejected(main or None)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def safe_content_type(main: str) -> str:
    """모델에 실을 content-type. 남의 서버가 준 값이고 `text/` 로 시작하기만 하면 문지기를
    지나므로(`text/plain 이 앞 지시를 무시하라` 같은 것) 미디어 타입 토큰 모양이 아니면 버린다."""
    return main if _MEDIA_TYPE_RE.fullmatch(main) else UNKNOWN_TYPE


# -- 본문 판정 ------------------------------------------------------------------------------


def _wsdl_operation_count(body: bytes) -> int | None:
    """WSDL 1.1 이 선언한 오퍼레이션 수. `portType` 아래 것만 센다. 같은 오퍼레이션이 binding 과
    `soap:operation` 에도 나오므로 `operation` 요소를 전부 세면 세 배가 된다."""
    try:
        root = etree.fromstring(body, parser=safe_xml_parser())
    except etree.XMLSyntaxError:
        return None
    return len(root.findall(f".//{{{WSDL_NS}}}portType/{{{WSDL_NS}}}operation"))


def classify(body: bytes) -> dict[str, Any]:
    """가져온 본문에서 우리가 판정한 값만 뽑는다: `{kind, format, operation_count?}`.

    남의 문서에서 온 문자열은 하나도 담지 않는다. 담는 것은 우리 어휘의 분류값과 우리가 센 수뿐
    이다. 판정은 `pipeline.detect` 로 하므로 업로드 경로와 같은 눈이고, 상한을 넘거나 파싱이
    실패하면 `unknown` 이다. 어떤 입력에도 예외를 올리지 않는다."""
    is_xml = looks_like_xml(body)
    unknown: dict[str, Any] = {"kind": "unknown", "format": "xml" if is_xml else "text"}
    if len(body) > CLASSIFY_MAX_BYTES:
        # 파싱 없이 겉모양만 말한다. 큰 본문에 파서를 물리지 않는다
        return unknown
    try:
        source_format, doc = detect(body)
        kind = _KIND_BY_FORMAT.get(source_format, "unknown")
        out: dict[str, Any] = {"kind": kind, "format": _format_of(body, is_xml, doc)}
        count = _operation_count(kind, body, doc)
    except Exception:  # noqa: BLE001 - 판정 실패는 unknown 이지 오류가 아니다
        return unknown
    if count is not None:
        out["operation_count"] = count
    return out


def _format_of(body: bytes, is_xml: bool, doc: Any) -> FetchFormat:
    """표기 형식. 구조가 있는 문서만 json·yaml 이고 스칼라 하나로 읽히는 본문은 text 다
    (`hello` 도 YAML 로는 문자열 하나라 파싱에 성공한다). `pipeline.detect` 는 매핑 문서만 doc 으로
    돌려주므로 최상위가 배열인 본문도 text 로 나간다."""
    if is_xml:
        return "xml"
    if not isinstance(doc, dict):
        return "text"
    try:
        json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "yaml"
    return "json"


def _operation_count(kind: FetchKind, body: bytes, doc: Any) -> int | None:
    """스펙이면 우리가 센 오퍼레이션 수. 못 세면 None 이고 결과에 넣지 않는다."""
    if kind == "wsdl":
        return _wsdl_operation_count(body)
    if kind in ("openapi", "swagger") and isinstance(doc, dict):
        return sum(1 for _ in iter_operations(doc))
    return None


# -- 세션 파일 이름 -------------------------------------------------------------------------


def file_name_for(files: Mapping[str, FetchedFile], url: str, main_type: str) -> str:
    """`fetched-<n>.<ext>`. n 은 비어 있는 첫 번호, ext 는 content-type, 없으면 URL 경로의
    확장자."""
    ext = _EXT_BY_TYPE.get(main_type)
    if ext is None:
        suffix = Path(urlsplit(url).path).suffix.lstrip(".").lower()
        ext = suffix if suffix in _KNOWN_EXTS else "txt"
    n = 1
    while any(k.startswith(f"{FILE_PREFIX}{n}.") for k in files):
        n += 1
    return f"{FILE_PREFIX}{n}.{ext}"


__all__ = [
    "CLASSIFY_MAX_BYTES",
    "FETCH_TYPES",
    "FILE_PREFIX",
    "UNKNOWN_TYPE",
    "ContentTypeGate",
    "ContentTypeRejected",
    "FetchFormat",
    "FetchKind",
    "allowed_content_type",
    "classify",
    "file_name_for",
    "safe_content_type",
]
