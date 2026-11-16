"""D25: 화면의 ui/src/lib/providers.ts 는 서버의 provider kind 표를 손으로 옮긴 사본이다.

설정 화면의 placeholder 와 캔버스의 추론 강도·temperature 칸이 이 표로 갈린다. 화면이 서버 표를
따로 받아 오는 API 는 없어서 복사본을 두었고, 어긋나면 make verify(플랫폼 테스트)가 막는다. 서버 쪽
(store/settings.py)이 원본이므로 화면 쪽을 그 값에 맞춘다.
"""

from __future__ import annotations

import re

from daegam.store.settings import DEFAULT_BASE_URLS, DEFAULT_MODELS, SDK_KINDS
from tests.helpers.source import UI_LIB, ts_object

UI_COPY = UI_LIB / "providers.ts"
FIX = "ui/src/lib/providers.ts 를 서버 store/settings.py 값에 맞춘다"

# `openrouter: "https://openrouter.ai/api/v1",` 처럼 값은 늘 따옴표 글자다
_ENTRY_RE = re.compile(r'"?([A-Za-z0-9_.-]+)"?\s*:\s*"([^"]*)"')


def test_default_models_match_the_server_table() -> None:
    assert ts_object(UI_COPY, "DEFAULT_MODELS", _ENTRY_RE) == DEFAULT_MODELS, FIX


def test_default_base_urls_match_the_server_table() -> None:
    assert ts_object(UI_COPY, "DEFAULT_BASE_URLS", _ENTRY_RE) == DEFAULT_BASE_URLS, FIX


def test_sdk_kinds_match_the_server_table() -> None:
    assert ts_object(UI_COPY, "SDK_KINDS", _ENTRY_RE) == SDK_KINDS, FIX
