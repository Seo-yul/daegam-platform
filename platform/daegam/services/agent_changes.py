"""Agent 정의의 변경 (P48 6.2~6.4절). 청지기의 제안을 저장된 리비전 위에 짜 맞추고, 제안이 넘지
못하는 선을 가리고, 저장된 것과 무엇이 다른지를 낸다. 모두 순수 함수다. 플랫폼 앱을 사람의 신원으로
부르는 쪽은 `services/assistant/proposals.py` 이고, 여기는 그 앱이 돌려준 정의와 검증 결과만 받는다.

- **짜 맞추기(`apply_patch`).** 바꾸기 제안은 정의 전체가 아니라 저장된 리비전 위의 패치다.
  노드는 id 로 통째로 더하거나 바꾸고(`set_nodes`), id 로 지우며(`remove_nodes`, 그 노드의
  간선도 함께 빠진다), 간선은 (source, target, kind) 로 더하고 지운다(`add_edges`·
  `remove_edges`). `set_service` 는 service 의 칸을 통째로 바꾸고 `set_display_name` 은 표시
  이름이다. 패치가 건드리지 않은 노드는 모델을 거치지 않으므로 id·글·배치가 그대로 남는다.
  리비전에 없는 노드나 간선을 가리키면 오류다.
- **읽기(`outline`·`node_of`).** 청지기는 패치할 것을 개요 하나와 노드 하나씩으로 읽는다. 개요는
  노드마다 id·종류·역할·이름·소스나 Knowledge Base·Tool 이름·글 길이와 간선·service 다.
- **넘지 못하는 선(`refusals`).** 검증 오류, 이 판이 모르는 키 가운데 기준 노드가 같은 종류·소스·
  값으로 갖고 있던 것이 아닌 키, `service.evaluation` 의 변경, 더하거나 바꾼 tools 노드의 빈
  `allow_tools`, 그리고 사람만 다는 표시(`person_marks`)다.
- **사람만 다는 표시(`person_marks`, 6.3절).** 읽기 전용(`read_only`), 승인 기억(`rememberable`),
  바깥 내용이 아니라는 표시(`outside: false`)와 사람이 단 `outside: true` 를 거두는 것이다. 거두는
  것은 그 Tool 의 백엔드 종류가 안쪽이 아니라고 알 때만 된다(모르면 안쪽일 수 있다). 노드 id 가
  아니라 묶인 대상으로 견준다. Tool 은 (소스 종류, 소스 id, Tool 이름), 지식은 `knowledge_base_id`
  다. 그래서 소스를 바꾼 노드는 표시를 데려가지 못한다. 제안은 기준의 표시를 지키거나 더 엄격하게만
  바꿀 수 있다.
- **비교(`compare`, 6.4절).** 기준과 짜 맞춘 정의를 노드 id 로 견준다. 더하고 지우고 바뀐 노드(바뀐
  data 칸 이름과 함께), (source, target, kind) 로 더하고 지운 간선, 바뀐 service 칸과 표시 이름이다.
  글 칸은 줄 비교를 싣되 칸마다 바뀐 줄 40개, 줄마다 200자까지이고 잘리면 표시한다. 만들기는 모든
  노드가 더한 것이다. 카드와 캔버스는 이것을 그리기만 한다.
- **먼저 볼 것(`attention`, 6.4절).** 사람이 저장 전에 먼저 볼 변경을 코드로 낸다. Tool 효과는 짜
  맞춘 정의를 검증한 `tool_effects` 를 쓴다. 에이전트에 새로 붙은 tools 노드는 새 소스로 보고,
  승인은 노드마다 견준다. 승인 때 다시 세어(`attention_key`) 카드에 없던 것이 나오면 저장하지
  않는다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from daegam.services.advertised_names import free_name
from daegam.services.agent_graph import (
    NODE_DATA_MODELS,
    AgentData,
    Analysis,
    EntryData,
    GuardrailData,
    KnowledgeData,
    MemoryData,
    ToolsData,
    analyze_structure,
)
from daegam.services.tool_effects import EFFECT_WEB, EFFECT_WRITE, ORIGIN_INTERNAL, tools_nodes
from daegam.services.toolnames import TOOL_NAME_RE
from daegam.store.agents import (
    AgentSpec,
    EdgeKind,
    Graph,
    GraphEdge,
    GraphNode,
    ServiceSettings,
)
from daegam.units import json_bytes

# 바꾸기 제안의 인자 이름. Agent 본문의 어느 칸과도 겹치지 않는다(`name`·`graph` 가 아니다).
# 옛 판이 이 인자를 자기 PUT 본문으로 보내면 name 이 없어 422 로 끝난다(P48 10절). 도구 표의 손
# 스키마 (`catalog.AGENT_PATCH`)가 이 이름들이고 테스트가 둘을 맞춘다
PATCH_FIELDS = (
    "set_nodes",
    "remove_nodes",
    "add_edges",
    "remove_edges",
    "set_service",
    "set_display_name",
)
# 노드 종류마다 글 칸. 개요는 길이만 싣고 카드는 글 비교를 싣는다(6.4절)
TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "agent": ("system_prompt", "description"),
    "entry": ("greeting",),
    "guardrail": ("message",),
    "knowledge": ("description",),
}
# 거절 하나에 싣는 설명의 길이. 거절은 4KB 로 잘리는 도구 결과라 짧게 둔다
SHORT_MESSAGE = 160
# 카드의 글 비교 상한. 칸마다 바뀐 줄 수와 줄마다 글자 수다. 캔버스는 글 전체를 보인다(6.5절)
DIFF_LINES = 40
DIFF_LINE_CHARS = 200
# 카드에 싣는 에이전트 이름의 길이. 이름은 모델이 쓴 글이라 짧게 자른다
LABEL_CHARS = 64


def problem(code: str, message: str, node_id: str | None = None) -> dict[str, Any]:
    """거절에 싣는 문제 하나 `{code, node_id, message}`."""
    return {"code": code, "node_id": node_id, "message": message[:SHORT_MESSAGE]}


def first_error(e: ValidationError) -> str:
    """pydantic 검증 오류의 첫 줄 `<자리>: <뜻>`. 거절의 짧은 설명이다."""
    first = e.errors()[0]
    loc = ".".join(str(p) for p in first["loc"]) or "value"
    return f"{loc}: {first['msg']}"


class PatchError(Exception):
    """패치를 리비전 위에 짜 맞출 수 없다. issues 는 `problem` 의 목록이다."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__(", ".join(i["code"] for i in issues))
        self.issues = issues


class _EdgeRef(BaseModel):
    """패치가 가리키는 간선. 지울 때는 id 가 필요 없고, 더할 때 id 가 없으면 플랫폼이 짓는다."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: EdgeKind
    id: str | None = Field(default=None, min_length=1)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.kind)


def _edge_key(edge: GraphEdge) -> tuple[str, str, str]:
    return (edge.source, edge.target, edge.kind)


def _items(patch: Mapping[str, Any], key: str) -> list[Any]:
    value = patch.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise PatchError([problem("invalid_patch", f"{key} must be a list")])
    return value


def apply_patch(
    base: AgentSpec, patch: Mapping[str, Any], *, node_cap: int
) -> tuple[AgentSpec, set[str]]:
    """패치를 기준 리비전 위에 짜 맞춘 정의와, 패치가 더하거나 바꾼 노드 id 다.

    노드 순서는 기준을 따르고 더한 노드는 패치 순서로 뒤에 붙는다. 기준 노드의 data 가 `node_cap`
    바이트를 넘으면 그 노드는 통째로 읽을 수 없으므로 바꿀 수도 없다(`node_too_large`). 문제는 한
    번에 모아 `PatchError` 로 올린다."""
    issues: list[dict[str, Any]] = []
    nodes: dict[str, GraphNode] = {n.id: n for n in base.graph.nodes}
    touched: set[str] = set()
    for raw in _items(patch, "set_nodes"):
        try:
            node = GraphNode.model_validate(raw)
        except ValidationError as e:
            node_id = raw.get("id") if isinstance(raw, dict) else None
            issues.append(problem("invalid_node", first_error(e), node_id))
            continue
        if node.id in touched:
            issues.append(problem("duplicate_node_id", "set_nodes names this node twice", node.id))
            continue
        old = nodes.get(node.id)
        if old is not None and json_bytes(old.data) > node_cap:
            issues.append(
                problem(
                    "node_too_large",
                    "this node is too large to read, so it cannot be replaced",
                    node.id,
                )
            )
            continue
        nodes[node.id] = node
        touched.add(node.id)
    removed: set[str] = set()
    for node_id in _items(patch, "remove_nodes"):
        if not isinstance(node_id, str) or node_id not in {n.id for n in base.graph.nodes}:
            issues.append(
                problem("unknown_node", "the saved revision has no such node", str(node_id))
            )
        elif node_id in touched:
            issues.append(
                problem("node_conflict", "a node cannot be set and removed at once", node_id)
            )
        else:
            removed.add(node_id)
            nodes.pop(node_id, None)
    # 지운 노드에 닿은 간선은 함께 빠진다. 캔버스에서 노드를 지울 때와 같다
    edges = [e for e in base.graph.edges if e.source not in removed and e.target not in removed]
    for raw in _items(patch, "remove_edges"):
        try:
            ref = _EdgeRef.model_validate(raw)
        except ValidationError as e:
            issues.append(problem("invalid_edge", first_error(e)))
            continue
        kept = [e for e in edges if _edge_key(e) != ref.key]
        if len(kept) == len(edges):
            issues.append(
                problem("unknown_edge", f"no {ref.kind} edge {ref.source} -> {ref.target}")
            )
        edges = kept
    for raw in _items(patch, "add_edges"):
        try:
            ref = _EdgeRef.model_validate(raw)
        except ValidationError as e:
            issues.append(problem("invalid_edge", first_error(e)))
            continue
        if any(_edge_key(e) == ref.key for e in edges):
            issues.append(
                problem("edge_exists", f"the {ref.kind} edge {ref.source} -> {ref.target} exists")
            )
            continue
        taken = {e.id for e in edges}
        # 캔버스와 같은 꼴의 id 다(`ui/src/lib/agentGraph.ts` `uniqueId`)
        edge_id = ref.id or free_name(f"e-{ref.source}-{ref.target}", taken, sep="-")
        if edge_id in taken:
            issues.append(problem("duplicate_edge_id", f"edge id {edge_id!r} is taken"))
            continue
        edges.append(GraphEdge(id=edge_id, source=ref.source, target=ref.target, kind=ref.kind))
    service = base.service.model_dump()
    set_service = patch.get("set_service")
    if set_service is not None:
        if not isinstance(set_service, dict):
            issues.append(problem("invalid_service", "set_service must be an object"))
        else:
            unknown = sorted(set(set_service) - set(ServiceSettings.model_fields))
            if unknown:
                issues.append(
                    problem("invalid_service", "unknown service fields: " + ", ".join(unknown))
                )
            service.update({k: v for k, v in set_service.items() if k not in unknown})
    try:
        settings = ServiceSettings.model_validate(service)
    except ValidationError as e:
        issues.append(problem("invalid_service", first_error(e)))
        settings = base.service
    if issues:
        raise PatchError(issues)
    order = [n.id for n in base.graph.nodes if n.id in nodes]
    order += [n_id for n_id in nodes if n_id not in order]
    graph = Graph(nodes=[nodes[i] for i in order], edges=edges)
    return AgentSpec(graph=graph, service=settings), touched


# -- 읽기 ------------------------------------------------------------------------------------


def _text_sizes(node_type: str, data: Mapping[str, Any]) -> dict[str, int]:
    return {
        f: len(data[f])
        for f in TEXT_FIELDS.get(node_type, ())
        if isinstance(data.get(f), str) and data[f]
    }


def outline(agent: Mapping[str, Any]) -> dict[str, Any]:
    """`GET /api/agents/{id}` 의 답에서 청지기가 패치를 짤 때 볼 개요. 노드마다 id·종류와 고를 때
    쓰는 값, 글 칸의 길이, data 의 크기(노드 읽기의 상한과 견준다)다. 글 자체는 노드 읽기로 본다."""
    graph = agent.get("graph") or {}
    nodes: list[dict[str, Any]] = []
    for node in graph.get("nodes") or []:
        data = node.get("data") or {}
        node_type = node.get("type")
        item: dict[str, Any] = {"id": node.get("id"), "type": node_type}
        for key in ("role", "name", "provider", "model"):
            if isinstance(data.get(key), str):
                item[key] = data[key]
        if node_type == "tools":
            item["source"] = data.get("source")
            item["tools"] = list(data.get("allow_tools") or [])
        elif node_type == "knowledge":
            item["knowledge_base_id"] = data.get("knowledge_base_id")
        elif node_type == "memory":
            item["data_service_id"] = (data.get("checkpointer") or {}).get("data_service_id")
        sizes = _text_sizes(str(node_type), data)
        if sizes:
            item["text_chars"] = sizes
        item["bytes"] = json_bytes(data)
        nodes.append(item)
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "display_name": agent.get("display_name"),
        "revision": agent.get("revision"),
        "deployed_revision": agent.get("deployed_revision"),
        "nodes": nodes,
        "edges": [
            {k: e.get(k) for k in ("id", "source", "target", "kind")}
            for e in graph.get("edges") or []
        ],
        "service": agent.get("service"),
    }


def node_of(agent: Mapping[str, Any], node_id: str) -> dict[str, Any] | None:
    """`GET /api/agents/{id}` 의 답에서 노드 하나 `{id, type, data}`. 없으면 None 이다."""
    for node in (agent.get("graph") or {}).get("nodes") or []:
        if node.get("id") == node_id:
            return {"id": node_id, "type": node.get("type"), "data": node.get("data") or {}}
    return None


# -- 사람만 다는 표시 (6.3절) -------------------------------------------------------------------

ToolBinding = tuple[str, str, str]
"""Tool 표시가 묶이는 대상 (소스 종류, 소스 id, Tool 이름)."""


@dataclass
class _Marks:
    """정의 하나의 사람 표시. 값은 그 표시가 달린 노드 id 다."""

    read_only: dict[ToolBinding, str] = field(default_factory=dict)
    rememberable: dict[ToolBinding, str] = field(default_factory=dict)
    outside_off: dict[ToolBinding, str] = field(default_factory=dict)
    outside_on: dict[ToolBinding, str] = field(default_factory=dict)
    knowledge_inside: dict[str, str] = field(default_factory=dict)


def _bind(data: ToolsData, tool: str) -> ToolBinding:
    return (data.source.kind, data.source.id, tool)


def _marks_of(spec: AgentSpec | None) -> _Marks:
    marks = _Marks()
    if spec is None:
        return marks
    for node_id, data in tools_nodes(spec):
        for tool in data.read_only:
            marks.read_only.setdefault(_bind(data, tool), node_id)
        for tool in data.rememberable:
            marks.rememberable.setdefault(_bind(data, tool), node_id)
        for tool, outside in data.outside.items():
            target = marks.outside_on if outside else marks.outside_off
            target.setdefault(_bind(data, tool), node_id)
    for node in spec.graph.nodes:
        if node.type != "knowledge":
            continue
        try:
            data = KnowledgeData.model_validate(node.data)
        except ValidationError:
            continue
        if not data.outside:
            marks.knowledge_inside.setdefault(data.knowledge_base_id, node.id)
    return marks


def _using_node(spec: AgentSpec, binding: ToolBinding) -> str | None:
    """그 Tool 을 쓰는 tools 노드 id. 같은 소스이고 그 Tool 을 고르거나 전부를 쓰는 노드다."""
    kind, source_id, tool = binding
    for node_id, data in tools_nodes(spec):
        same = data.source.kind == kind and data.source.id == source_id
        if same and (not data.allow_tools or tool in data.allow_tools):
            return node_id
    return None


def person_marks(
    base: AgentSpec | None,
    composed: AgentSpec,
    origins: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """제안이 새로 단 사람 표시와 사람이 단 `outside: true` 를 거둔 자리. 비어 있으면 넘는 것이
    없다. origins 는 짜 맞춘 정의를 검증한 `tool_origins`(노드 id → Tool → 백엔드 종류)다."""
    before, after = _marks_of(base), _marks_of(composed)
    places: list[dict[str, Any]] = []
    for mark, now, then in (
        ("read_only", after.read_only, before.read_only),
        ("rememberable", after.rememberable, before.rememberable),
        ("outside_false", after.outside_off, before.outside_off),
    ):
        for binding, node_id in now.items():
            if binding not in then:
                places.append({"node_id": node_id, "mark": mark, "tool": binding[2]})
    for kb_id, node_id in after.knowledge_inside.items():
        if kb_id not in before.knowledge_inside:
            places.append({"node_id": node_id, "mark": "outside_false", "knowledge_base_id": kb_id})
    for binding in before.outside_on:
        if binding in after.outside_on:
            continue
        node_id = _using_node(composed, binding)
        # 그 Tool 을 더는 쓰지 않으면 거둔 것이 아니다. 백엔드 종류를 모르면(Tool 목록을 아직 못
        # 읽었다) 안쪽일 수 있으므로, 안쪽이 아니라고 알 때만 거둘 수 있다
        origin = origins.get(node_id, {}).get(binding[2]) if node_id is not None else None
        if node_id is not None and origin in (None, ORIGIN_INTERNAL):
            places.append({"node_id": node_id, "mark": "outside_true_removed", "tool": binding[2]})
    return places


# -- 넘지 못하는 선 (6.2절 4) ------------------------------------------------------------------


def _unknown_keys(node: GraphNode, base_node: GraphNode | None) -> list[str]:
    """모르는 칸을 버리는 data 모델의 노드에서 이 판이 모르는 키 가운데 남길 수 없는 것. 뒤 판이
    저장한 키는 기준 노드가 같은 종류·같은 소스로 같은 값을 가졌을 때만 그대로 둔다. 값을 바꾸거나
    소스를 바꾸면 뒤 판이 읽을 표시를 심을 수 있기 때문이다."""
    model = NODE_DATA_MODELS[node.type]
    if model.model_config.get("extra") != "ignore":
        return []
    unknown = set(node.data) - set(model.model_fields)
    if (
        base_node is None
        or base_node.type != node.type
        or base_node.data.get("source") != node.data.get("source")
    ):
        return sorted(unknown)
    return sorted(
        k for k in unknown if k not in base_node.data or base_node.data[k] != node.data[k]
    )


def refusals(
    base: AgentSpec | None,
    composed: AgentSpec,
    touched: Iterable[str],
    *,
    issues: Iterable[Mapping[str, Any]],
    origins: Mapping[str, Mapping[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(문제, 사람 표시의 자리). 둘 다 비어야 제안이 카드로 간다. issues 는 짜 맞춘 정의의 검증
    이슈이고 오류만 문제가 된다. touched 는 패치가 더하거나 바꾼 노드이고 만들기는 모든 노드다."""
    problems = [
        problem(str(i.get("code")), str(i.get("message") or ""), i.get("node_id"))
        for i in issues
        if i.get("level") == "error"
    ]
    base_nodes = {n.id: n for n in base.graph.nodes} if base is not None else {}
    marked = set(touched)
    for node in composed.graph.nodes:
        if node.id not in marked:
            continue
        unknown = _unknown_keys(node, base_nodes.get(node.id))
        if unknown:
            problems.append(
                problem(
                    "unknown_node_keys",
                    "keys this release does not know: " + ", ".join(unknown),
                    node.id,
                )
            )
        if node.type == "tools" and not node.data.get("allow_tools"):
            problems.append(
                problem(
                    "empty_allow_tools",
                    "name the tools in allow_tools; empty means every tool",
                    node.id,
                )
            )
    before = base.service.evaluation if base is not None else None
    if composed.service.evaluation != before:
        problems.append(
            problem("evaluation_changed", "service.evaluation is set on the screen only")
        )
    return problems, person_marks(base, composed, origins)


# -- 비교 (6.4절) ---------------------------------------------------------------------------


def text_diff(before: str, after: str) -> dict[str, Any]:
    """글 칸 하나의 줄 비교. 바뀐 줄만 `-`(앞)·`+`(뒤) 로 싣고 상한을 넘으면 `cut` 이다."""
    old, new = before.splitlines(), after.splitlines()
    lines: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        lines += [("-", line) for line in old[i1:i2]]
        lines += [("+", line) for line in new[j1:j2]]
    cut = len(lines) > DIFF_LINES
    out: list[dict[str, str]] = []
    for op, text in lines[:DIFF_LINES]:
        if len(text) > DIFF_LINE_CHARS:
            text, cut = text[:DIFF_LINE_CHARS], True
        out.append({"op": op, "text": text})
    return {"lines": out, "cut": cut}


def _label(node: GraphNode) -> dict[str, Any]:
    out: dict[str, Any] = {"id": node.id, "type": node.type}
    name = node.data.get("name")
    if isinstance(name, str) and name:
        out["name"] = name[:LABEL_CHARS]
    return out


def _changed_fields(before: GraphNode, after: GraphNode) -> list[str]:
    if before.type != after.type:
        return ["type"]
    keys = set(before.data) | set(after.data)
    return sorted(k for k in keys if before.data.get(k) != after.data.get(k))


def _texts(before: GraphNode | None, after: GraphNode) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in TEXT_FIELDS.get(after.type, ()):
        old = before.data.get(name) if before is not None and before.type == after.type else None
        new = after.data.get(name)
        old_text = old if isinstance(old, str) else ""
        new_text = new if isinstance(new, str) else ""
        if old_text != new_text:
            out.append({"node_id": after.id, "field": name, **text_diff(old_text, new_text)})
    return out


def _service_fields(base: AgentSpec | None, composed: AgentSpec) -> list[str]:
    """바뀐 service 칸 이름. 만들기는 기본값과 견준다."""
    old = (base.service if base is not None else ServiceSettings()).model_dump()
    new = composed.service.model_dump()
    return sorted(k for k in new if new[k] != old.get(k))


def compare(
    base: AgentSpec | None,
    composed: AgentSpec,
    *,
    display_before: str | None = None,
    display_after: str | None = None,
) -> dict[str, Any]:
    """기준(만들기면 None)과 짜 맞춘 정의의 변경 목록이다(모듈 머리)."""
    before = {n.id: n for n in base.graph.nodes} if base is not None else {}
    after = {n.id: n for n in composed.graph.nodes}
    added, changed, texts = [], [], []
    for node in composed.graph.nodes:
        old = before.get(node.id)
        if old is None:
            added.append(_label(node))
        else:
            fields = _changed_fields(old, node)
            if fields:
                changed.append({**_label(node), "fields": fields})
        texts += _texts(old, node)
    removed = [
        _label(n) for n in (base.graph.nodes if base is not None else []) if n.id not in after
    ]
    old_edges = {_edge_key(e) for e in base.graph.edges} if base is not None else set()
    new_edges = {_edge_key(e) for e in composed.graph.edges}

    def edge_list(keys: Iterable[tuple[str, str, str]]) -> list[dict[str, str]]:
        return [{"source": s, "target": t, "kind": k} for s, t, k in sorted(keys)]

    out: dict[str, Any] = {
        "nodes": {"added": added, "changed": changed, "removed": removed},
        "edges": {
            "added": edge_list(new_edges - old_edges),
            "removed": edge_list(old_edges - new_edges),
        },
        "service": _service_fields(base, composed),
        "texts": texts,
    }
    if display_after is not None and display_after != display_before:
        out["display_name"] = {"before": display_before, "after": display_after}
    return out


# -- 먼저 볼 것 (6.4절) ------------------------------------------------------------------------


def attention_key(mark: Mapping[str, Any]) -> str:
    """승인 때 다시 센 표시를 카드의 것과 견주는 열쇠. 이름처럼 나중에 붙이는 값은 붙이기 전에
    잰다."""
    return json.dumps(mark, sort_keys=True, ensure_ascii=False)


def _tool(name: str) -> dict[str, str]:
    """카드에 싣는 Tool 이름. MCP 프로필이 받는 꼴일 때만 싣는다(6.4절)."""
    return {"tool": name} if TOOL_NAME_RE.fullmatch(name) else {}


def _tools_by_id(spec: AgentSpec | None) -> dict[str, ToolsData]:
    return dict(tools_nodes(spec)) if spec is not None else {}


def _tool_users(spec: AgentSpec | None) -> dict[str, set[str]]:
    """tools 노드 id → 그 노드를 tools 간선으로 붙인 에이전트 id."""
    out: dict[str, set[str]] = {}
    for edge in spec.graph.edges if spec is not None else []:
        if edge.kind == "tools":
            out.setdefault(edge.target, set()).add(edge.source)
    return out


def _agents(a: Analysis | None) -> dict[str, AgentData]:
    return dict(a.nodes_of("agent", AgentData)) if a is not None else {}


def _model(a: Analysis, agent_id: str) -> tuple[str, str | None] | None:
    m = a.model_of(agent_id)
    return (m.provider, m.model) if m is not None else None


def _attached_knowledge(a: Analysis | None) -> dict[str, str]:
    """에이전트에 붙은 Knowledge Base id → knowledge 노드 id."""
    out: dict[str, str] = {}
    for node_id, data in a.attached_knowledge_nodes() if a is not None else []:
        out.setdefault(data.knowledge_base_id, node_id)
    return out


def _prompt_marks(old: dict[str, AgentData], new: dict[str, AgentData]) -> list[dict[str, Any]]:
    fields = TEXT_FIELDS["agent"]
    marks = []
    for node_id, data in new.items():
        was = old.get(node_id)
        if was is None or any(getattr(was, f) != getattr(data, f) for f in fields):
            marks.append(
                {
                    "code": "system_prompt_changed",
                    "node_id": node_id,
                    "agent": data.name[:LABEL_CHARS],
                }
            )
    return marks


def _tool_marks(
    base: AgentSpec | None,
    composed: AgentSpec,
    effects: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """tools 노드의 먼저 볼 것. 새 노드, 소스를 바꾼 노드, 새 에이전트에 붙은 노드는 새 소스다."""
    old_tools, marks = _tools_by_id(base), []
    old_users, new_users = _tool_users(base), _tool_users(composed)
    for node_id, data in tools_nodes(composed):
        was = old_tools.get(node_id)
        fresh = (
            was is None
            or was.source != data.source
            or bool(new_users.get(node_id, set()) - old_users.get(node_id, set()))
        )
        node_effects = effects.get(node_id) or {}
        if fresh:
            source = {"kind": data.source.kind, "id": data.source.id}
            marks.append({"code": "tool_source_added", "node_id": node_id, "source": source})
        elif was == data:
            continue
        for tool, effect in sorted(node_effects.items()):
            if effect != EFFECT_WRITE or tool in data.approval:
                continue
            # 같은 에이전트들이 같은 소스에서 이미 승인 없이 쓰던 Tool 은 바뀐 것이 아니다
            if (
                not fresh
                and was is not None
                and tool not in was.approval
                and (not was.allow_tools or tool in was.allow_tools)
            ):
                continue
            marks.append({"code": "write_without_approval", "node_id": node_id, **_tool(tool)})
        web = {t for t, e in node_effects.items() if e == EFFECT_WEB}
        if fresh:
            wider = bool(web)
        else:
            assert was is not None
            old_allow, new_allow = set(was.allow_domains), set(data.allow_domains)
            joined = bool(was.allow_tools) and bool(web - set(was.allow_tools))
            wider = (
                joined
                or (bool(old_allow) and (not new_allow or bool(new_allow - old_allow)))
                or bool(set(was.block_domains) - set(data.block_domains))
            )
        if wider:
            marks.append({"code": "web_reach_widened", "node_id": node_id})
    return marks + _approval_marks(base, composed)


def _approval_marks(base: AgentSpec | None, composed: AgentSpec) -> list[dict[str, Any]]:
    """기준 노드마다 그 승인이 짜 맞춘 정의에서 사라졌거나 결정을 잃었는가. 같은 노드(같은 소스)가
    있으면 그 노드와, 없으면 그 Tool 을 쓰는 같은 소스의 노드와 견준다. 더는 쓰지 않는 Tool 은
    아니다."""
    new_tools = dict(tools_nodes(composed))
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for node_id, was in _tools_by_id(base).items():
        for tool, decisions in sorted(was.approval.items()):
            target: str | None = node_id
            now = new_tools.get(node_id)
            if now is None or now.source != was.source:
                target = _using_node(composed, _bind(was, tool))
                now = new_tools.get(target) if target is not None else None
            if now is None or target is None or (now.allow_tools and tool not in now.allow_tools):
                continue
            if not set(decisions) <= set(now.approval.get(tool, [])):
                found[(target, tool)] = {
                    "code": "approval_removed",
                    "node_id": target,
                    **_tool(tool),
                }
    return list(found.values())


def _guardrail_marks(a: Analysis | None, c: Analysis) -> list[dict[str, Any]]:
    if a is None:
        return []
    new = dict(c.nodes_of("guardrail", GuardrailData))
    new_edges = {(e.source, e.target) for e in c.edges if e.kind == "guardrail"}
    marks = []
    for node_id, old in a.nodes_of("guardrail", GuardrailData):
        now = new.get(node_id)
        edge_gone = any(
            e.target == node_id and (e.source, e.target) not in new_edges
            for e in a.edges
            if e.kind == "guardrail"
        )
        rules = [r.model_dump() for r in now.rules] if now is not None else []
        weaker = (
            now is None
            or edge_gone
            or now.position != old.position
            or (old.on_violation == "block" and now.on_violation == "flag")
            or any(r.model_dump() not in rules for r in old.rules)
        )
        if weaker:
            marks.append(
                {"code": "guardrail_weakened", "node_id": node_id, "position": old.position}
            )
    return marks


def _entry_marks(a: Analysis | None, c: Analysis) -> list[dict[str, Any]]:
    old = a.typed(a.entry, EntryData) if a is not None and a.entry is not None else None
    new = c.typed(c.entry, EntryData) if c.entry is not None else None
    if old is None or old.rate_limit is None or new is None:
        return []
    limit = old.rate_limit.per_user_per_minute
    if new.rate_limit is None or new.rate_limit.per_user_per_minute > limit:
        return [{"code": "rate_limit_removed", "node_id": c.entry}]
    return []


def _agent_marks(a: Analysis | None, c: Analysis) -> list[dict[str, Any]]:
    old, marks = _agents(a), []
    for node_id, data in c.nodes_of("agent", AgentData):
        was = old.get(node_id)
        run = data.code_execution
        if run is not None and (
            was is None
            or was.code_execution is None
            or (bool(was.code_execution.approval) and not run.approval)
        ):
            marks.append({"code": "code_execution", "node_id": node_id})
        if a is not None and was is not None:
            model = _model(c, node_id)
            if model is not None and model != _model(a, node_id):
                marks.append(
                    {
                        "code": "model_changed",
                        "node_id": node_id,
                        "agent": data.name[:LABEL_CHARS],
                        "provider": model[0],
                        "model": model[1],
                    }
                )
        pinned = {p.id: p.revision for p in was.skills} if was is not None else {}
        for pin in data.skills:
            # 핀을 내린 것도 다른 본문이 지시문이 되는 변경이다
            if pinned.get(pin.id) != pin.revision:
                skill = {"id": pin.id, "revision": pin.revision}
                marks.append({"code": "skill_attached", "node_id": node_id, "skill": skill})
    return marks


def _memory_marks(a: Analysis | None, c: Analysis) -> list[dict[str, Any]]:
    old = dict(a.nodes_of("memory", MemoryData)) if a is not None else {}
    marks = []
    for node_id, data in c.nodes_of("memory", MemoryData):
        now = data.long_term
        was = old[node_id].long_term if node_id in old else None
        if now is None:
            continue
        longer = was is not None and (
            was.retention_days is not None
            and (now.retention_days is None or now.retention_days > was.retention_days)
        )
        if was is None or longer:
            marks.append({"code": "memory_changed", "node_id": node_id})
    return marks


def attention(
    base: AgentSpec | None,
    composed: AgentSpec,
    effects: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """저장 전에 먼저 볼 변경(모듈 머리). effects 는 짜 맞춘 정의를 검증한 `tool_effects` 다.
    표시마다 `code` 와 그것이 가리키는 노드, 카드 문장에 끼울 값이다. 소스·지식·Skill 의 이름은
    부르는 쪽이 그 문서에서 읽어 붙인다."""
    a = analyze_structure(base) if base is not None else None
    c = analyze_structure(composed)
    marks = _prompt_marks(_agents(a), _agents(c))
    marks += _tool_marks(base, composed, effects)
    marks += _guardrail_marks(a, c)
    marks += _entry_marks(a, c)
    old_kb = _attached_knowledge(a)
    for kb_id, node_id in _attached_knowledge(c).items():
        if kb_id not in old_kb:
            marks.append(
                {"code": "knowledge_attached", "node_id": node_id, "knowledge": {"id": kb_id}}
            )
    marks += _agent_marks(a, c)
    fields = _service_fields(base, composed)
    if fields:
        marks.append({"code": "service_changed", "fields": fields})
    marks += _memory_marks(a, c)
    return marks


__all__ = [
    "DIFF_LINES",
    "DIFF_LINE_CHARS",
    "PATCH_FIELDS",
    "PatchError",
    "ToolBinding",
    "apply_patch",
    "attention",
    "attention_key",
    "compare",
    "first_error",
    "node_of",
    "outline",
    "person_marks",
    "problem",
    "refusals",
    "text_diff",
]
