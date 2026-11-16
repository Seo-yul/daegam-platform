"""P35 17절: 비서의 Agent 안내(knowledge/agent.md)가 그래프 계약을 따라가는지 본다.

안내에 없는 노드·간선·코드는 비서가 가르치지 못하고, 손으로 옮긴 목록은 계약이 늘 때 조용히
뒤처진다. 그래서 기준을 계약 자체에서 읽는다. 노드 종류는 `NODE_TYPES`, 간선 종류는 `EdgeKind`,
노드 data 의 필드는 `NODE_DATA_MODELS`, 코드는 `agent_graph.py` 가 `a.error`·`a.warn` 으로 실제로
내는 것과 `agent_skills.py` 가 `Finding` 으로 내는 것이다(D64. `_check_skills` 가 그대로 이슈로
옮긴다). 어긋나면 안내를 계약에 맞춘다.
"""

from __future__ import annotations

import re
from typing import get_args

from daegam.services import agent_graph
from daegam.services.assistant.tools import KNOWLEDGE_DIR
from daegam.store.agents import NODE_TYPES, EdgeKind
from tests.helpers.source import graph_issue_codes

GUIDE = KNOWLEDGE_DIR / "agent.md"
# `## 노드 data` 절에서 노드 종류 하나를 여는 줄(`- entry: …`)
NODE_LINE = re.compile(r"- ([a-z]+): ")
CODE_TOKEN = re.compile(r"[a-z][a-z0-9_]*")


def _section(heading: str) -> list[str]:
    """`## heading` 다음 줄부터 다음 `## ` 앞까지다."""
    lines = GUIDE.read_text(encoding="utf-8").splitlines()
    marker = f"## {heading}"
    assert marker in lines, f"{GUIDE.name} 에 '{marker}' 절이 없다"
    rest = lines[lines.index(marker) + 1 :]
    end = next((i for i, line in enumerate(rest) if line.startswith("## ")), len(rest))
    return rest[:end]


def _first_column(lines: list[str]) -> list[str]:
    """표의 첫 칸이다. 머리 줄과 구분 줄은 뺀다."""
    cells = [line.split("|")[1].strip() for line in lines if line.startswith("|")]
    return [cell for cell in cells[1:] if not set(cell) <= {"-"}]


def _node_lines() -> dict[str, str]:
    return {m.group(1): line for line in _section("노드 data") if (m := NODE_LINE.match(line))}


def test_every_node_type_has_a_data_line_with_all_its_fields() -> None:
    lines = _node_lines()
    assert sorted(lines) == sorted(NODE_TYPES), "노드 data 절의 `- 종류:` 줄이 노드 종류와 다르다"
    for node_type, model in agent_graph.NODE_DATA_MODELS.items():
        line = lines[node_type]
        missing = [f for f in model.model_fields if not re.search(rf"\b{re.escape(f)}\b", line)]
        assert not missing, f"{node_type} 줄에 없는 data 필드: {missing}"


def test_edge_table_lists_every_edge_kind_once() -> None:
    kinds = sorted(_first_column(_section("그래프")))
    assert kinds == sorted(get_args(EdgeKind)), "그래프 절의 간선 표가 간선 종류와 다르다"


def test_error_table_lists_every_code_the_graph_check_emits() -> None:
    table = _first_column(_section("오류 코드"))
    listed = {token for cell in table for token in CODE_TOKEN.findall(cell)}
    missing = sorted(graph_issue_codes() - listed)
    assert not missing, f"오류 코드 표에 없는 검증 코드: {missing}"


def test_drafting_section_keeps_the_lessons_from_the_proposal_run() -> None:
    """제안 작성 절이 막는 실수들이다 (P48).
    Database 를 찾지 않고 memory 를 지우기, 검증을 넘기려고 승인을 빼기, 긴 프롬프트를 줄이기,
    다듬기에서 앞 제안의 변경을 잃기. 카드 거절은 사유를 반영하고, 승인을 빼 달라는 요청은
    캔버스로 보낸다."""
    text = "\n".join(_section("제안 만들기"))
    for phrase in (
        "list_data_services",
        "validate_agent_graph 로 검증한다",
        "memory, approval, rate_limit, daily_tokens 를 빼지 않는다",
        "캔버스에서 직접 빼라고 말한다",
        "뺀 제안을 create_agent 나 update_agent 로 보낸다",
        "줄이거나 요약하지 않는다",
        "사유를 반영해 새 제안을 짜고",
        "앞 제안이 저장되지 않았으면",
        "앞 제안의 변경을 모두 다시 넣는다",
    ):
        assert phrase in text, f"제안 만들기 절에 '{phrase}' 가 없다"
