"""텍스트 → 청크 → 토큰 수 (D32).

문서에서 글을 뽑는 일은 `daegam.documents` 와 그것을 제어면 밖에서 돌리는 `daegam-docparse` 가 한다
(D61, `services/docparse.py`). 여기에는 자르기와 토큰 세기만 남는다. 자르기는 문단(빈 줄),
문장(`.!?` 뒤 공백. 한국어 종결 어미 `다.`·`요.` 뒤 공백도 같은 규칙에 든다), 고정 길이 순이고
청크마다 앞 청크의 끝 `overlap` 자를 앞에 붙인다. 결정적이고 빈 청크는 내지 않는다. 토큰은
tiktoken cl100k_base 로 센 추정치다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import tiktoken

_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_ENCODING_NAME = "cl100k_base"
_encoding: tiktoken.Encoding | None = None


def _units(text: str, size: int) -> list[tuple[str, str]]:
    """(앞 구분자, 조각). 문단은 `\\n\\n`, 문단 안의 문장은 공백, 고정 길이 조각은 빈 구분자."""
    out: list[tuple[str, str]] = []
    for para in _PARAGRAPH_RE.split(text.replace("\r\n", "\n").replace("\r", "\n")):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            out.append(("\n\n", para))
            continue
        first = True
        for sent in _SENTENCE_RE.split(para):
            sent = sent.strip()
            if not sent:
                continue
            sep = "\n\n" if first else " "
            first = False
            if len(sent) <= size:
                out.append((sep, sent))
                continue
            for i in range(0, len(sent), size):
                out.append((sep if i == 0 else "", sent[i : i + size]))
    return out


def chunk(text: str, size: int, overlap: int) -> list[str]:
    """size 자 상한으로 문단·문장을 채우고 앞 청크의 끝 overlap 자를 앞에 붙인다.
    빈 문서는 빈 목록이고 청크는 비어 있지 않다."""
    if size < 1:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be in [0, size)")
    pieces: list[str] = []
    seps: list[str] = []  # 각 조각의 첫 단위 앞 구분자 (겹침을 이을 때 쓴다)
    buf = ""
    buf_sep = ""
    for sep, unit in _units(text, size):
        if not buf:
            buf, buf_sep = unit, sep
        elif len(buf) + len(sep) + len(unit) <= size:
            buf += sep + unit
        else:
            pieces.append(buf)
            seps.append(buf_sep)
            buf, buf_sep = unit, sep
    if buf:
        pieces.append(buf)
        seps.append(buf_sep)
    if not overlap:
        return pieces
    out: list[str] = []
    for i, piece in enumerate(pieces):
        if i == 0:
            out.append(piece)
        else:
            out.append(pieces[i - 1][-overlap:] + (seps[i] or " ") + piece)
    return out


def _get_encoding() -> tiktoken.Encoding:
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoding


def count_tokens(texts: Iterable[str]) -> int:
    """cl100k_base 토큰 수 합계. 인코딩 객체는 모듈에 한 번 캐시한다 (첫 로드가 BPE 파일을
    받는다)."""
    encoding = _get_encoding()
    return sum(len(encoding.encode(t, disallowed_special=())) for t in texts)
