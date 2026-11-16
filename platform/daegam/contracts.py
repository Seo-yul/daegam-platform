"""플랫폼 밖에 있는 계약 파일을 찾는다. 경로는 저장소 루트 기준이다.

저장소에서는 `platform/` 의 형제 디렉터리에 있고, 이미지에서는 Dockerfile 이 같은 상대 경로로
`/app/` 아래에 복사한다. 둘 다 본다.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

# agent.json 계약 (D26). services/agent_graph.py 가 compile 결과를 이 스키마로 검증한다
AGENT_SCHEMA = Path("agent-runtime") / "schema" / "agent.schema.json"
# 웹 Tool 계약 (P39b 10절). web_fetch 의 이름·스키마와 웹 결과의 고정 머리 줄이다
WEB_TOOLS_CONTRACT = Path("web-tools") / "schema" / "tools.json"


def contract_path(rel: Path) -> Path:
    """저장소 안(platform/ 의 형제)과 이미지 안(/app/ 아래) 순으로 찾는다."""
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[1]):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{rel} not found next to {here}")


@cache
def agent_schema() -> dict[str, Any]:
    """agent.json 계약을 한 번 읽어 둔다. 그래프 검증기와 Skill 형식 검사가 함께 쓴다. 고치지 않고
    읽기만 한다."""
    return json.loads(contract_path(AGENT_SCHEMA).read_text(encoding="utf-8"))


@cache
def web_tools_contract() -> dict[str, Any]:
    """웹 Tool 계약을 한 번 읽어 둔다. 허브의 도구 탭·도구 이름과 web_search 결과의 머리 줄이
    읽는다."""
    return json.loads(contract_path(WEB_TOOLS_CONTRACT).read_text(encoding="utf-8"))
