"""바깥 표시와 읽기 표시의 렌더와 검증이다(P43 2·4·8절, D96).

기능 `tool_safety` 가 허락되면 agent.json 의 소스마다
`inside`(결과가 바깥 내용이 아닌 Tool)와 `read`(읽기만 하는 Tool)를, Knowledge 항목마다 `outside` 를
싣는다. 두 목록은 안전하다고 아는 이름만 담는다. 런타임은 나머지를 바깥 내용과 쓰기로 다룬다.

- **바깥 표시.** 우리 mcp-runtime 의 Tool(백엔드 종류 `internal`)은 기본이 안쪽이고, 그 밖의
  백엔드(`image`·`remote`·`entry`)의 Tool 은 기본이 바깥이다. tools 노드의 `outside` 가 Tool 마다
  기본값을 바꾼다. 웹 Tool, 백엔드 종류를 모르는 이름과 code mode 의 스크립트(종류 `script`)는 늘
  바깥이다. 스크립트가 닿는 Tool 은 Agent 의 배포 뒤에도 바뀌기 때문이다(P56 6.5절). 바깥 서비스의
  Tool 에서 표시를 끄면 경고 `outside_mark_off` 다.
- **읽기 표시.** 바깥 내용 뒤의 승인 규칙이 쓰는 효과(`tool_effects.node_effects` 의 tool_safety)가
  `read` 거나 웹 Tool 이면 읽기다. 캔버스의 읽기 전용 표시는 서버가 읽기 여부를 알려 주지 않는
  Tool(`image`·`remote`·`entry`)에서만 읽기로 바꾸고 경고 `read_only_by_canvas` 를 낸다. `internal`
  Tool 의 표시는 저장된 채 효과가 없다(화면이 적용되지 않았다고 보인다). 백엔드 종류를 모르는 이름은
  핀이 읽기라고 해도 읽기가 아니다. 스크립트는 읽기이고 어느 캔버스 표시도 받지 않는다.
- **memory 노드가 없는 그래프.** 승인을 받을 수 없으므로 런타임이 바깥 내용 뒤의 쓰기를 거절한다.
  읽기라고 알 수 없는 Tool 이 하나라도 있으면 경고 `outside_writes_refused` 다. 첨부는 어느
  그래프에든 올 수 있으므로 웹 Tool 이 없는 그래프도 이 경고를 받는다. 원본에 memory 노드가 있던
  미리 보기는 이 경고를 받지 않는다. 상태가 파드 하나에 살아 런타임이 memory 노드 없이도 묻기
  때문이다(`GraphContext.in_pod_approvals`, 기능 `in_pod_state`).
- **이 기능이 있어야 도는 그래프.** 웹 Tool 이 있는 그래프는 웹 내용 뒤의 쓰기를 이 기능이 묻는다.
  그런 그래프는 기능을 `required` 로 요구해 그 이름을 모르는 이미지가 init 에서 거절한다
  (`agent_features.agent_features`).

이름과 효과는 웹 Tool 의 결정과 같은 노드 목록을 쓴다(`agent_web.web_graph`, 배포 때의 핀을 따른다).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from daegam.services.agent_graph import GraphContext, Issue, KnowledgeData, analyze_structure
from daegam.services.agent_web import WebNode, web_graph
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    ORIGIN_INTERNAL,
    ORIGIN_SCRIPT,
    canvas_mark_applies,
    known_read,
    node_effects,
)
from daegam.store.agents import AgentSpec

OUTSIDE_MARK_OFF = "outside_mark_off"
READ_ONLY_BY_CANVAS = "read_only_by_canvas"
OUTSIDE_WRITES_REFUSED = "outside_writes_refused"


@dataclass(frozen=True)
class NodeMarks:
    """tools 노드 하나의 표시다. inside 와 read 는 문서에 실리는 목록이고, marks_off 와 by_canvas
    는 경고가 읽는 이름이다."""

    inside: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    marks_off: list[str] = field(default_factory=list)
    by_canvas: list[str] = field(default_factory=list)


def _inside(name: str, node: WebNode) -> bool:
    """결과가 바깥 내용이 아닌가. 웹 Tool, 백엔드 종류를 모르는 이름과 스크립트는 노드의 표시와
    상관없이 늘 바깥이다(모듈 머리)."""
    origin = node.tools.origins.get(name)
    if origin in (None, ORIGIN_SCRIPT) or node.tools.effect(name) == EFFECT_WEB:
        return False
    override = node.data.outside.get(name)
    return origin == ORIGIN_INTERNAL if override is None else not override


def node_marks(node: WebNode) -> NodeMarks:
    """tools 노드 하나의 바깥 표시와 읽기 표시다(모듈 머리). 이름을 모르는 노드는 빈 목록이다."""
    marks = NodeMarks()
    names = node.names or []
    effects = node_effects(names, node.tools, node.data.read_only, tool_safety=True)
    for name in names:
        origin = node.tools.origins.get(name)
        if _inside(name, node):
            marks.inside.append(name)
        if canvas_mark_applies(origin) and node.data.outside.get(name) is False:
            marks.marks_off.append(name)
        effect = effects[name]
        if known_read(effect, origin):
            marks.read.append(name)
        if effect == EFFECT_READ and node.tools.effect(name) == EFFECT_WRITE:
            marks.by_canvas.append(name)
    return marks


def attach_tool_safety(
    document: dict[str, Any], rev: AgentSpec, ctx: GraphContext
) -> dict[str, Any]:
    """기능 `tool_safety` 가 허락됐을 때의 붙이는 함수다. 소스마다 `inside`·`read` 를, Knowledge
    항목마다 `outside` 를 싣는다. 문맥의 핀을 따르므로 `apply_pins` 가 정한 `allow` 와 같은
    이름이다. 받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    a = analyze_structure(rev)
    graph = web_graph(rev, ctx.source_tools, ctx.tool_pins, a=a)
    for source in final["tools"]:
        node = graph.nodes.get(source["ref"])
        marks = node_marks(node) if node is not None else NodeMarks()
        source["inside"] = marks.inside
        source["read"] = marks.read
    knowledge = dict(a.nodes_of("knowledge", KnowledgeData))
    for item in final.get("knowledge", []):
        data = knowledge.get(item["ref"])
        item["outside"] = data.outside if data is not None else True
    return final


def safety_issues(spec: AgentSpec, ctx: GraphContext) -> list[Issue]:
    """바깥 표시의 경고다(모듈 머리)."""
    a = analyze_structure(spec)
    graph = web_graph(spec, ctx.source_tools, a=a)
    issues: list[Issue] = []
    unread = False
    for node in graph.nodes.values():
        marks = node_marks(node)
        unread = unread or node.names is None or len(marks.read) < len(node.names)
        if marks.marks_off:
            issues.append(
                Issue(
                    OUTSIDE_MARK_OFF,
                    f"tools node {node.node_id!r}: the outside content mark is off for tools of "
                    f"an outside service ({', '.join(marks.marks_off)}); instructions in their "
                    "results can call write tools without approval",
                    "warning",
                    node_id=node.node_id,
                )
            )
        if marks.by_canvas:
            issues.append(
                Issue(
                    READ_ONLY_BY_CANVAS,
                    f"tools node {node.node_id!r}: {', '.join(marks.by_canvas)} are marked "
                    "read-only on the canvas, but their server cannot say whether they are "
                    "read-only; if they change data and have no approval setting, they run "
                    "without approval even in chats that hold outside content",
                    "warning",
                    node_id=node.node_id,
                )
            )
    if unread and a.main is not None and not a.main_has_memory and not ctx.in_pod_approvals:
        issues.append(
            Issue(
                OUTSIDE_WRITES_REFUSED,
                "the graph has no memory node, so it cannot ask for approval: once a chat holds "
                "outside content, tools that can change data do not run; add a memory node to ask "
                "for approval instead",
                "warning",
            )
        )
    return issues
