"""추출 결과와 오류의 모양 (D61). 하위 프로세스, 서버, 플랫폼의 호출자가 같은 모양을 주고받는다.

목적(`purpose`)은 둘이다. `upload` 는 채팅 첨부이고 `index` 는 Knowledge 인덱서다. 서버는
레플리카마다 두 자리 가운데 하나를 첨부 몫으로 남기고, 추출은 첨부일 때만 PDF 쪽 머리를 넣는다.
첨부의 글은 50만 자에서 잘라 `truncated` 로 알린다(`limited`). 인덱서의 글은 자르지 않는다.
문서마다의 청크 상한이 그 몫을 한다.

오류 코드는 모두 422 다. `extract_failed`(읽지 못했다·하위 프로세스가 죽었다), `extract_timeout`,
`encrypted_document`, `invalid_archive`, `archive_too_large`, `unsupported_format` 이다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

UPLOAD = "upload"
INDEX = "index"
PURPOSES = (UPLOAD, INDEX)
# 첨부 하나에서 뽑은 글의 상한(글자)
MAX_UPLOAD_CHARS = 500_000


class ExtractError(Exception):
    """받은 문서에서 글을 뽑지 못했다. `code` 가 응답의 오류 코드다."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Extracted:
    text: str
    pages: int | None = None
    sheets: int | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Extracted:
        """서비스 응답을 믿지 않고 칸마다 꼴을 본다. 틀리면 `extract_failed` 다."""
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise ExtractError("extract_failed", "the extraction result has no text")
        counts = {key: data.get(key) for key in ("pages", "sheets")}
        if any(v is not None and (type(v) is not int or v < 0) for v in counts.values()):
            raise ExtractError("extract_failed", "the extraction result has a bad count")
        return cls(text=data["text"], truncated=data.get("truncated") is True, **counts)


def limited(extracted: Extracted, purpose: str) -> Extracted:
    """첨부의 글을 상한에서 자른다. 인덱서의 글은 그대로다."""
    if purpose != UPLOAD or len(extracted.text) <= MAX_UPLOAD_CHARS:
        return extracted
    return replace(extracted, text=extracted.text[:MAX_UPLOAD_CHARS], truncated=True)


__all__ = [
    "INDEX",
    "MAX_UPLOAD_CHARS",
    "PURPOSES",
    "UPLOAD",
    "ExtractError",
    "Extracted",
    "limited",
]
