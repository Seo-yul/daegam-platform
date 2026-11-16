"""D49: 화면의 ui/src/lib/hub.ts 의 `DERIVED_SOURCE` 키는 서버의 파생 종류를 손으로 옮긴 사본이다.

화면은 이 표로 파생 항목을 가른다. 목록에서 지울 수 있는가, 다른 테넌트에 열 수 있는가, 도구
새로고침이 있는가, 이름 어긋남 안내가 어느 원본을 짚는가가 모두 이것을 본다. 어긋나면 서버가
422 로 거절할 버튼을 보이거나 있지도 않은 새로고침을 하라고 안내한다. 서버 쪽(store/hub.py)이
원본이므로 화면 쪽을 그 값에 맞춘다.
"""

from __future__ import annotations

import re

from daegam.store.hub import DERIVED_KINDS
from tests.helpers.source import UI_LIB, ts_object

UI_COPY = UI_LIB / "hub.ts"
FIX = "ui/src/lib/hub.ts 의 DERIVED_SOURCE 키를 서버 store/hub.py 의 DERIVED_KINDS 에 맞춘다"

# `internal: "mcp-server",` 처럼 값은 늘 따옴표 글자다
_ENTRY_RE = re.compile(r'"?([A-Za-z0-9_-]+)"?\s*:\s*"([^"]*)"')


def test_derived_kinds_match_the_server() -> None:
    assert set(ts_object(UI_COPY, "DERIVED_SOURCE", _ENTRY_RE)) == set(DERIVED_KINDS), FIX
