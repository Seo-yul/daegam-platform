"""D49: 화면의 ui/src/lib/hub.ts 의 `DERIVED_SOURCE` 키는 서버의 파생 종류를 손으로 옮긴 사본이다.
게시한 Agent 가 내는 Tool 이름의 머리(`AGENT_CALL_TOOL_PREFIX`)와 그 Tool 설명 길이의 위 끝
(`AgentPublish.tsx` 의 `DESCRIPTION_MAX`)도 서버 값의 사본이다 (P46 6.1·6.2절).

화면은 이 표로 파생 항목을 가른다. 목록에서 지울 수 있는가, 다른 테넌트에 열 수 있는가, 도구
새로고침이 있는가, 이름 어긋남 안내가 어느 원본을 짚는가가 모두 이것을 본다. 어긋나면 서버가
422 로 거절할 버튼을 보이거나 있지도 않은 새로고침을 하라고 안내한다. Tool 이름의 머리가 어긋나면
채팅의 Tool 카드가 실행 링크를 달지 못하고, 설명 길이가 어긋나면 폼이 서버가 거절할 글을 받는다.
서버 쪽(store/hub.py, services/agent_calls.py)이 원본이므로 화면 쪽을 그 값에 맞춘다.
"""

from __future__ import annotations

import re

from daegam.services.agent_calls import TOOL_PREFIX
from daegam.store.hub import AGENT_DESCRIPTION_MAX, DERIVED_KINDS
from tests.helpers.source import REPO, UI_LIB, ts_number, ts_object, ts_string

UI_COPY = UI_LIB / "hub.ts"
PUBLISH_DIALOG = REPO / "ui" / "src" / "components" / "agents" / "AgentPublish.tsx"
FIX = "ui/src/lib/hub.ts 의 DERIVED_SOURCE 키를 서버 store/hub.py 의 DERIVED_KINDS 에 맞춘다"
FIX_AGENT = "화면의 사본을 서버 services/agent_calls.py 와 store/hub.py 의 값에 맞춘다"

# `internal: "mcp-server",` 처럼 값은 늘 따옴표 글자다
_ENTRY_RE = re.compile(r'"?([A-Za-z0-9_-]+)"?\s*:\s*"([^"]*)"')


def test_derived_kinds_match_the_server() -> None:
    assert set(ts_object(UI_COPY, "DERIVED_SOURCE", _ENTRY_RE)) == set(DERIVED_KINDS), FIX


def test_the_agent_tool_name_and_description_bound_match_the_server() -> None:
    assert ts_string(UI_COPY, "AGENT_CALL_TOOL_PREFIX") == TOOL_PREFIX, FIX_AGENT
    assert ts_number(PUBLISH_DIALOG, "DESCRIPTION_MAX") == AGENT_DESCRIPTION_MAX, FIX_AGENT
