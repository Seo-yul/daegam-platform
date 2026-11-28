"""P43: 캔버스가 바깥 표시의 기본값과 읽기 전용 표시의 효과를 가르는 상수의 사본 파리티.

원본은 플랫폼 `services/tool_effects.py` 의 `ORIGIN_INTERNAL`(바깥 표시의 기본값이 안쪽인 종류,
`agent_tool_safety._inside`)과 `THIRD_PARTY_ORIGINS`(캔버스의 읽기 전용 표시가 효과를 내는 종류)다.
화면 `ui/src/lib/toolSafety.ts` 가 같은 값을 두고, 검사 응답의 `ToolOrigin` 은 그 종류와 웹 Tool,
code mode 의 스크립트(`ORIGIN_SCRIPT`, P56 6.5절), 게시한 Agent 를 부르는 Tool(`ORIGIN_AGENT`, P46
6.6절)의 전부다. 작업을 맡기는 노드의 Tool 다섯(`TASK_TOOLS`, P46 6.7절)과 그 가운데 읽는 것, 답을
가져오는 것도 두 쪽이 따로 적는다.
어긋나면 캔버스가 플랫폼이 렌더하지 않는 표시를 보인다. 플랫폼 쪽이 원본이므로 화면의 사본을 맞춘다.
"""

from __future__ import annotations

import re
from typing import get_args

from daegam.api.schemas.agents import ToolOrigin
from daegam.services.tool_effects import (
    ORIGIN_AGENT,
    ORIGIN_INTERNAL,
    ORIGIN_PLATFORM,
    ORIGIN_SCRIPT,
    TASK_ANSWER_TOOL,
    TASK_READ_TOOLS,
    TASK_TOOLS,
    THIRD_PARTY_ORIGINS,
)
from tests.helpers.source import (
    UI_LIB,
    read_source,
)

TOOL_SAFETY_TS = UI_LIB / "toolSafety.ts"
FIX = "플랫폼 services/tool_effects.py 가 원본이다. 화면 lib/toolSafety.ts 의 사본을 그 값에 맞춘다"


def _ts_const(source: str, name: str) -> str:
    """`export const NAME: Type = <식>;` 의 식이다."""
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?);\n", source)
    assert m, f"{TOOL_SAFETY_TS} 에서 {name} 를 찾지 못했다"
    return m.group(1)


def test_screen_origin_rules_match_the_platform() -> None:
    source = read_source(TOOL_SAFETY_TS)
    assert _ts_const(source, "ORIGIN_INTERNAL") == f'"{ORIGIN_INTERNAL}"', FIX
    assert _ts_const(source, "ORIGIN_SCRIPT") == f'"{ORIGIN_SCRIPT}"', FIX
    assert _ts_const(source, "ORIGIN_AGENT") == f'"{ORIGIN_AGENT}"', FIX
    third_party = re.findall(r'"([a-z]+)"', _ts_const(source, "THIRD_PARTY_ORIGINS"))
    assert set(third_party) == THIRD_PARTY_ORIGINS, FIX


def test_screen_task_tools_match_the_platform() -> None:
    """캔버스는 허용 Tool 에 작업 Tool 다섯을 두어 작업을 맡기는 노드를 만든다. 이름이 어긋나면
    플랫폼이 그 노드를 답을 기다리는 노드로 읽고 `invalid_node_data` 로 막는다."""
    source = read_source(TOOL_SAFETY_TS)
    names = r'"([a-z_]+)"'
    assert tuple(re.findall(names, _ts_const(source, "TASK_TOOLS"))) == TASK_TOOLS, FIX
    assert set(re.findall(names, _ts_const(source, "TASK_READ_TOOLS"))) == TASK_READ_TOOLS, FIX
    assert _ts_const(source, "TASK_ANSWER_TOOL") == f'"{TASK_ANSWER_TOOL}"', FIX


def test_the_validate_origins_are_every_backend_kind_the_rules_know() -> None:
    assert set(get_args(ToolOrigin)) == {
        ORIGIN_INTERNAL,
        ORIGIN_PLATFORM,
        ORIGIN_SCRIPT,
        ORIGIN_AGENT,
        *THIRD_PARTY_ORIGINS,
    }
