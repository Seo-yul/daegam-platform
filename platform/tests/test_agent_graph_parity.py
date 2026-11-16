"""캔버스 검증기(`ui/src/lib/agentGraph.ts`)가 받는 노드 키와 서버 모델이 어긋나지 않게 막는다.

두 곳이 같은 규칙을 갖는 것은 편집 중에 바로 알려 주려는 것이다(D26). 어긋나면 캔버스는
멀쩡한데 저장만 422 `invalid_node_data` 로 거부되고, 사용자는 이유를 알 수 없다.
`test_oidc_copy.py`·`test_toolname_copy.py` 와 같은 취지이고, 여기서는 파일이 아니라
"서버 모델의 필드 집합" 과 "TS 의 허용 키 집합" 을 비교한다. Skill 상한(D64)도 같은 까닭으로
`ui/src/lib/agentSkills.ts` 의 값이 서버와 같은지 본다. 이슈 code 가 화면 문장 표에 다 있는지는
`ui/src/test/agent-graph.test.ts` 가 서버 파일을 읽어 본다. 같은 그래프에 두 검사기가 같은 이슈를
내는지는 묶음 `tests/fixtures/agent-graph-corpus.json` 이 본다(P77, `test_agent_graph_corpus.py`).
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel

from daegam.services import agent_graph, agent_skills
from daegam.units import MIB
from tests.helpers.source import UI_LIB, read_source

CANVAS = UI_LIB / "agentGraph.ts"
CANVAS_SKILLS = UI_LIB / "agentSkills.ts"

# 노드 종류 → 서버 모델 (일곱 전부. knowledge 는 D32 부터 extra=forbid 다)
MODELS: dict[str, type[BaseModel]] = {
    "entry": agent_graph.EntryData,
    "agent": agent_graph.AgentData,
    "model": agent_graph.ModelData,
    "tools": agent_graph.ToolsData,
    "memory": agent_graph.MemoryData,
    "guardrail": agent_graph.GuardrailData,
    "knowledge": agent_graph.KnowledgeData,
}


def _canvas_keys() -> dict[str, list[str]]:
    """TS 의 NODE_DATA_KEYS 를 읽는다. 값이 문자열 배열뿐이라 JSON 으로 푼다."""
    text = read_source(CANVAS)
    m = re.search(r"export const NODE_DATA_KEYS[^=]*=\s*\{(.*?)\n\};", text, re.S)
    assert m, "NODE_DATA_KEYS 를 찾지 못했다. 이름이 바뀌었으면 이 테스트도 함께 고친다"
    out: dict[str, list[str]] = {}
    for line in m.group(1).splitlines():
        row = re.match(r"\s*(\w+):\s*\[(.*?)\],", line)
        if row:
            items = row.group(2).strip()
            out[row.group(1)] = json.loads(f"[{items}]") if items else []
    return out


def _canvas_extra_ignored() -> set[str]:
    """TS 의 EXTRA_IGNORED 를 읽는다."""
    m = re.search(
        r"export const EXTRA_IGNORED[^=]*=\s*new Set<NodeType>\(\[(.*?)\]\)", read_source(CANVAS)
    )
    assert m, "EXTRA_IGNORED 를 찾지 못했다. 이름이 바뀌었으면 이 테스트도 함께 고친다"
    return set(json.loads(f"[{m.group(1)}]"))


def test_canvas_node_keys_match_the_server_models() -> None:
    canvas = _canvas_keys()
    assert set(canvas) == set(MODELS), (
        f"노드 종류가 다르다: 캔버스 {sorted(canvas)} vs 서버 {sorted(MODELS)}"
    )
    ignored = _canvas_extra_ignored()
    for node_type, model in MODELS.items():
        extra = "ignore" if node_type in ignored else "forbid"
        assert model.model_config.get("extra") == extra, (
            f"{model.__name__} 이 extra={extra} 가 아니다. 그러면 캔버스와 서버가 다르게 막는다"
        )
        assert sorted(canvas[node_type]) == sorted(model.model_fields), (
            f"{node_type} 노드의 허용 키가 다르다:\n"
            f"  캔버스: {sorted(canvas[node_type])}\n"
            f"  서버  : {sorted(model.model_fields)}"
        )


def test_canvas_skill_limits_match_the_server() -> None:
    """어긋나면 캔버스가 통과시킨 그래프를 저장이 `too_many_skills`·`skills_too_large` 로 거부하거나
    그 반대가 된다. TS 값은 `숫자` 나 `숫자 * MIB` 꼴만 쓴다."""
    text = read_source(CANVAS_SKILLS)
    found = re.findall(r"^export const (MAX_SKILLS_\w+) = (\d+)( \* MIB)?;$", text, re.M)
    canvas = {name: int(n) * (MIB if unit else 1) for name, n, unit in found}
    assert canvas == {
        "MAX_SKILLS_PER_AGENT": agent_skills.MAX_PER_AGENT,
        "MAX_SKILLS_PER_GRAPH": agent_skills.MAX_PER_GRAPH,
        "MAX_SKILLS_DISTINCT_BYTES": agent_skills.MAX_DISTINCT_BYTES,
        "MAX_SKILLS_EXPANDED_BYTES": agent_skills.MAX_EXPANDED_BYTES,
    }
