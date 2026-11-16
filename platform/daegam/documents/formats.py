"""받는 문서·이미지 형식의 표 (D32, D61). 채팅 첨부와 Knowledge Base 가 같은 표를 쓴다.

형식은 확장자가 먼저이고, 확장자가 없거나 모르면 content type 이다(`format_of`). content type 의
파라미터(`; charset=`)는 떼고 본다. 텍스트 형식은 파서가 필요 없어 플랫폼이 바로 UTF-8 로 풀고,
나머지 문서 형식은 `daegam-docparse` 가 뽑는다. 이미지는 뽑지 않고 모델이 본다. Knowledge Base 는
문서 형식만 받는다.

업로드 하나를 받을지는 `accepted_format` 이 가른다. 받지 않는 형식은 422 `unsupported_format`,
형식의 상한(`byte_limit`)을 넘으면 413 `too_large` 이고 둘 다 `FormatRefused` 다. 채팅 첨부와
Knowledge 업로드는 그것을 자기 오류형으로만 감싼다.
"""

from __future__ import annotations

from collections.abc import Collection

from daegam.units import MIB

# 문서 하나의 본문 상한. 첨부와 Knowledge 업로드의 413 too_large, URL 문서의 받기 상한이 같은 값이다
MAX_DOCUMENT_BYTES = 10 * MIB
# 이미지 첨부 하나의 상한. 런타임 attachments.py 의 MAX_IMAGE_BYTES 와 같고 파리티 테스트가
# 같은지 본다
MAX_IMAGE_BYTES = 2 * MIB
UNKNOWN_CONTENT_TYPE = "application/octet-stream"

# 형식 → 저장할 content type
CONTENT_TYPES: dict[str, str] = {
    "text": "text/plain",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "html": "text/html",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "hwpx": "application/hwp+zip",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
EXTENSIONS: dict[str, str] = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".hwpx": "hwpx",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".gif": "gif",
}
# content type → 형식. 저장하는 값의 역방향에 브라우저가 붙이는 별칭을 더한다
MEDIA_TYPES: dict[str, str] = {
    **{media: fmt for fmt, media in CONTENT_TYPES.items()},
    "text/x-markdown": "markdown",
    "application/xhtml+xml": "html",
}

TEXT_FORMATS = frozenset({"text", "markdown", "csv", "tsv", "json"})
PARSED_FORMATS = frozenset({"html", "pdf", "docx", "pptx", "xlsx", "hwpx"})
DOCUMENT_FORMATS = TEXT_FORMATS | PARSED_FORMATS
IMAGE_FORMATS = frozenset({"png", "jpeg", "webp", "gif"})
# 채팅 첨부가 받는 형식이다. Knowledge Base 는 문서 형식만 받는다
ATTACHMENT_FORMATS = DOCUMENT_FORMATS | IMAGE_FORMATS
# 문서 형식이 받는 확장자. 화면의 accept 가 같은 목록을 쓴다
DOCUMENT_EXTENSIONS = tuple(sorted(e for e, fmt in EXTENSIONS.items() if fmt in DOCUMENT_FORMATS))
# 이미지 첨부가 받는 확장자. 채팅 입력칸의 accept 가 같은 목록을 쓴다
IMAGE_EXTENSIONS = tuple(sorted(e for e, fmt in EXTENSIONS.items() if fmt in IMAGE_FORMATS))


def format_of(
    content_type: str | None, name: str | None, *, accept: Collection[str] = DOCUMENT_FORMATS
) -> str | None:
    """확장자가 먼저, 없거나 모르면 content type 으로 가른 형식. `accept` 밖이면 None 이다."""
    fmt = None
    if name and (dot := name.rfind(".")) >= 0:
        fmt = EXTENSIONS.get(name[dot:].lower())
    if fmt is None and content_type:
        fmt = MEDIA_TYPES.get(content_type.split(";", 1)[0].strip().lower())
    return fmt if fmt in accept else None


class FormatRefused(ValueError):
    """받지 않는 형식이거나 형식의 상한을 넘은 파일이다. 받는 쪽이 status·code·detail 을 자기
    오류형으로 옮긴다."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def byte_limit(fmt: str) -> int:
    """형식 하나의 본문 상한이다. 이미지는 2MiB, 문서는 10MiB 다."""
    return MAX_IMAGE_BYTES if fmt in IMAGE_FORMATS else MAX_DOCUMENT_BYTES


def accepted_format(
    name: str, content_type: str | None, size: int, *, accept: Collection[str] = DOCUMENT_FORMATS
) -> str:
    """받는 형식이고 그 상한 안인 파일의 형식이다. 아니면 `FormatRefused` 이고, 받지 않는 형식의
    문구는 `accept` 가 받는 확장자를 나열한다."""
    fmt = format_of(content_type, name, accept=accept)
    if fmt is None:
        allowed = " ".join(ext for ext, f in EXTENSIONS.items() if f in accept)
        raise FormatRefused(
            422,
            "unsupported_format",
            f"{name!r} ({content_type or 'unknown'}): allowed extensions are {allowed}",
        )
    if size > (limit := byte_limit(fmt)):
        raise FormatRefused(413, "too_large", f"the file exceeds {limit} bytes")
    return fmt


__all__ = [
    "ATTACHMENT_FORMATS",
    "CONTENT_TYPES",
    "DOCUMENT_EXTENSIONS",
    "DOCUMENT_FORMATS",
    "EXTENSIONS",
    "FormatRefused",
    "IMAGE_EXTENSIONS",
    "IMAGE_FORMATS",
    "MAX_DOCUMENT_BYTES",
    "MAX_IMAGE_BYTES",
    "MEDIA_TYPES",
    "MIB",
    "PARSED_FORMATS",
    "TEXT_FORMATS",
    "UNKNOWN_CONTENT_TYPE",
    "accepted_format",
    "byte_limit",
    "format_of",
]
