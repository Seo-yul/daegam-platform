"""D32: 화면의 ui/src/lib/knowledge.ts 는 서버의 임베딩 표와 청크 기본값, 받는 문서 확장자(D61)를
손으로 옮긴 사본이다. 받는 문서 확장자는 오류 사전도 읽어 ui/src/lib/limits.ts 에 있다.

폼이 서버가 채울 차원을 묻거나 기본 모델을 잘못 보이지 않으려면 두 쪽이 같아야 한다. 화면이
서버 표를 따로 받아 오는 API 는 없어서 복사본을 두었고, 어긋나면 make verify(플랫폼 테스트)가
막는다. 서버 쪽(store/settings.py·store/knowledge.py·documents/formats.py)이 원본이므로 화면 쪽을
그 값에 맞춘다.
"""

from __future__ import annotations

import re

from daegam.documents.formats import DOCUMENT_EXTENSIONS, MAX_DOCUMENT_BYTES
from daegam.store.knowledge import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, DEFAULT_MAX_CHUNKS
from daegam.store.settings import DEFAULT_EMBEDDING_MODELS, EMBEDDING_DIMENSIONS
from tests.helpers.source import UI_LIB, ts_number, ts_object, ts_string

UI_COPY = UI_LIB / "knowledge.ts"
UI_LIMITS = UI_LIB / "limits.ts"
FIX = (
    "ui/src/lib/knowledge.ts 를 서버 store/settings.py·store/knowledge.py·documents/formats.py 값에"
    " 맞춘다"
)
FIX_LIMITS = (
    "ui/src/lib/limits.ts 의 DOCUMENT_ACCEPT 를 서버 documents/formats.py 의 확장자에 맞춘다"
)

# `"key": 1536,` 과 `stub: 64` 둘 다 받는다
_ENTRY_RE = re.compile(r'"?([A-Za-z0-9_.-]+)"?\s*:\s*"?([A-Za-z0-9_.-]+)"?')


def test_embedding_dimensions_match_the_server_table() -> None:
    got = {k: int(v) for k, v in ts_object(UI_COPY, "EMBEDDING_DIMENSIONS", _ENTRY_RE).items()}
    assert got == EMBEDDING_DIMENSIONS, FIX


def test_default_embedding_models_match_the_server_table() -> None:
    models = ts_object(UI_COPY, "DEFAULT_EMBEDDING_MODELS", _ENTRY_RE)
    assert models == DEFAULT_EMBEDDING_MODELS, FIX


def test_chunking_defaults_match_the_server() -> None:
    chunking = {k: int(v) for k, v in ts_object(UI_COPY, "DEFAULT_CHUNKING", _ENTRY_RE).items()}
    assert chunking == {"size": DEFAULT_CHUNK_SIZE, "overlap": DEFAULT_CHUNK_OVERLAP}, FIX
    assert ts_number(UI_COPY, "DEFAULT_MAX_CHUNKS") == DEFAULT_MAX_CHUNKS, FIX


def test_document_size_limit_matches_the_server() -> None:
    assert ts_number(UI_COPY, "MAX_DOCUMENT_MB") * 1024 * 1024 == MAX_DOCUMENT_BYTES, FIX


def test_document_accept_matches_the_server_format_table() -> None:
    accept = ts_string(UI_LIMITS, "DOCUMENT_ACCEPT")
    assert tuple(sorted(accept.split(","))) == DOCUMENT_EXTENSIONS, FIX_LIMITS
