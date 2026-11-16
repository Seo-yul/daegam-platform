"""P52 5절: Agent 런타임이 거절된 Tool 호출을 가르는 메시지를 ToolHive 판에 묶는다.

런타임(`agent-runtime/runtime/mcp.py`)은 ToolHive 의 프록시와 vMCP 가 거절한 호출(403)과 한도에 걸린
호출(429)을 `MCPError` 의 메시지로 가른다. 그 메시지는 F0 4번이 ToolHive 0.53.0 에서 잰
`Unauthorized` 와 `Rate limit exceeded` 다. 오퍼레이터를 올리면 메시지가 바뀔 수 있고, 그러면 거절이
다시 턴을 끝낸다. 그래서 메시지를 잰 판이 설치하는 판(Makefile `TOOLHIVE_VERSION`)과 플랫폼이 규칙에
요구하는 판(`RULES_VERSION`, 1절의 0.53 검사)과 같아야 한다. 판을 올리는 변경은 메시지를 다시 재고
런타임의 판을 함께 고쳐야 이 시험을 지난다.
"""

from __future__ import annotations

import re

from daegam.services.tool_rules import RULES_VERSION
from tests.helpers.consts import REPO
from tests.helpers.source import AGENT_RUNTIME, py_constant, read_source

RUNTIME_MCP = AGENT_RUNTIME / "mcp.py"
# F0 4번이 ToolHive 0.53.0 에서 잰 메시지 (mcp SDK 클라이언트와 런타임의 fastmcp 클라이언트가 같다)
MEASURED = {"REFUSED_MESSAGE": "Unauthorized", "LIMITED_MESSAGE": "Rate limit exceeded"}
FIX = (
    "ToolHive 를 올렸다면 거절(403)과 한도(429)의 MCPError 메시지를 새 판에서 다시 재고, "
    "agent-runtime/runtime/mcp.py 의 메시지와 REFUSALS_MEASURED_ON 을 함께 고친다"
)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_the_runtime_reads_the_measured_messages() -> None:
    for name, text in MEASURED.items():
        assert py_constant(RUNTIME_MCP, name) == text, f"{name}: {FIX}"


def test_the_messages_were_measured_on_the_installed_toolhive() -> None:
    measured = py_constant(RUNTIME_MCP, "REFUSALS_MEASURED_ON")
    pinned = re.search(r"^TOOLHIVE_VERSION \?= (\S+)$", read_source(REPO / "Makefile"), re.M)
    assert pinned, "Makefile 에서 TOOLHIVE_VERSION 을 찾지 못했다"
    assert measured == pinned.group(1), FIX


def test_the_messages_were_measured_on_the_version_rules_require() -> None:
    assert _version(py_constant(RUNTIME_MCP, "REFUSALS_MEASURED_ON")) == RULES_VERSION, FIX
