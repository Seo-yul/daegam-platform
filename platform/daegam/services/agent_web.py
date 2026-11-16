"""그래프 전체의 웹 Tool 결정 (P39b 14절 2·4·5부, D64).

웹 Tool(플랫폼 `web_fetch`·`web_search`)이 그래프 어디에든 있으면 그래프 전체가 같은 결정을 받는다.
웹 내용이 서브에이전트의 결과로 메인에 오고 메인의 위임 글이 서브에이전트의 입력이 되므로 에이전트
하나씩 정할 수 없다.

- **데이터 Tool.** 닿는 에이전트 어디든 웹 Tool 이 아닌 MCP Tool, Knowledge 검색, 장기 기억이 있으면
  `web_data_tools` 가 참이고 런타임 가드는 모든 대화에서 fetch 를 허용 목록으로 좁힌다.
- **승인.** 웹 Tool 이 있는 그래프에서 바깥 내용이 든 대화의 쓰기는 런타임이 묻는다(P43 4절,
  `agent_tool_safety`). 그래서 웹 Tool 이 있는 그래프는 기능 `tool_safety` 를 요구한다.
- **소스의 `web` 블록.** 웹 Tool 을 주는 소스마다 fetch·search 이름, 노드의 허용·막는 도메인(막는
  쪽에 Platform Settings 의 `blocked_host_suffixes` 를 더한다), 사용자별 한도를 싣는다.
- **검증.** 웹 Tool 이 있는 그래프에서 이름을 모르는 소스는 `web_tools_unknown_names`, 한 에이전트의
  두 소스가 같은 웹 Tool 이름을 주면 `duplicate_tool_name` 이다. 경고는
  `web_fetch_allowlist_empty`(허용 목록이 빈 fetch 노드)다. 도메인 목록은 어느 그래프에서든
  `invalid_value`·`domain_is_public_suffix` 를 본다.

이름과 효과는 배포 때의 핀을 따르고(`tool_pins.pinned_tools`), 검증처럼 핀이 없으면 소스가 지금
광고하는 것이다. 컴파일은 문맥의 핀(`GraphContext.tool_pins`)을 따른다.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from daegam.services.agent_graph import (
    Analysis,
    GraphContext,
    Issue,
    KnowledgeData,
    ToolsData,
    analyze_structure,
    long_term_memory,
)
from daegam.services.domains import NODE_DOMAINS_MAX, DomainError, check_domains
from daegam.services.tool_effects import (
    EFFECT_WEB,
    EFFECT_WRITE,
    UNKNOWN_SOURCE,
    WEB_FETCH,
    WEB_SEARCH,
    SourceTools,
    node_effects,
)
from daegam.services.tool_pins import pinned_tools
from daegam.store.agents import AgentSpec, ToolPin
from daegam.store.settings import WebToolsSettings


@dataclass
class WebNode:
    """닿는 에이전트가 붙인 tools 노드 하나의 Tool 이다. names 가 None 이면 이름을 모른다."""

    node_id: str
    data: ToolsData
    names: list[str] | None
    effects: dict[str, str]
    web: dict[str, str]
    # 핀을 따른 소스의 Tool 이다(`pinned_tools`). P43 의 표시가 백엔드 종류를 읽는다
    tools: SourceTools = UNKNOWN_SOURCE
    # 배포 때의 핀에 웹 Tool 이 있었다. 그 이름이 소스에서 사라져도 그래프는 웹 그래프다
    pinned_web: bool = False

    @property
    def writes(self) -> list[str]:
        return [n for n, e in self.effects.items() if e == EFFECT_WRITE]

    @property
    def defaults(self) -> list[str]:
        """승인 설정이 없는 write Tool 이다. 트리거가 이것을 막는다(`runs/trigger_rule.py`)."""
        return [n for n in self.writes if n not in self.data.approval]


@dataclass
class WebGraph:
    nodes: dict[str, WebNode] = field(default_factory=dict)
    # 에이전트 노드 id → 그 에이전트가 붙인 tools 노드 id (닿는 것만)
    agents: dict[str, list[str]] = field(default_factory=dict)
    knowledge: bool = False
    long_term: bool = False

    @property
    def has_web(self) -> bool:
        return any(n.web or n.pinned_web for n in self.nodes.values())

    @property
    def data_tools(self) -> bool:
        tools = any(e != EFFECT_WEB for n in self.nodes.values() for e in n.effects.values())
        return tools or self.knowledge or self.long_term


def web_graph(
    spec: AgentSpec,
    sources: Mapping[str, SourceTools],
    pins: Mapping[str, ToolPin] | None = None,
    a: Analysis | None = None,
) -> WebGraph:
    """닿는 에이전트와 그 tools 노드의 이름·효과다. pins 를 주면 핀을 따른다(`pinned_tools`).
    이름을 모르는 소스도 아는 웹 Tool 이 있으면 웹 노드다. 그래야 검증이 그것을 막는다."""
    a = a or analyze_structure(spec)
    graph = WebGraph(long_term=long_term_memory(spec) is not None)
    for agent_id in a.order:
        if agent_id not in a.reachable or a.nodes[agent_id].type != "agent":
            continue
        graph.knowledge = graph.knowledge or bool(
            a.typed_targets(agent_id, "knowledge", KnowledgeData)
        )
        attached = a.typed_targets(agent_id, "tools", ToolsData)
        graph.agents[agent_id] = [node_id for node_id, _ in attached]
        for node_id, data in attached:
            if node_id in graph.nodes:
                continue
            pin = (pins or {}).get(node_id)
            current = sources.get(data.source.id, UNKNOWN_SOURCE)
            names, tools = pinned_tools(data.allow_tools, current, pin)
            graph.nodes[node_id] = WebNode(
                node_id=node_id,
                data=data,
                names=names,
                effects=node_effects(names or (), tools, data.read_only),
                web=dict(tools.web) if names is None else _web_of(names, tools),
                tools=tools,
                pinned_web=pin is not None and bool(pin.web),
            )
    return graph


def _web_of(names: list[str], tools: SourceTools) -> dict[str, str]:
    return {n: tools.web[n] for n in names if n in tools.web}


def uses_web(
    spec: AgentSpec, pins: Mapping[str, ToolPin], sources: Mapping[str, SourceTools]
) -> bool:
    """배포 때의 핀이나 지금 소스에 웹 Tool 이 있는가. 그러면 문서 기능 `web_guard` 가 필요하다."""
    return web_graph(spec, sources, pins).has_web


# -- 검증 ---------------------------------------------------------------------------------------


def _domain_issues(spec: AgentSpec) -> list[Issue]:
    out: list[Issue] = []
    for node in spec.graph.nodes:
        if node.type != "tools":
            continue
        for key in ("allow_domains", "block_domains"):
            values = node.data.get(key)
            if not isinstance(values, list):
                continue
            if len(values) > NODE_DOMAINS_MAX:
                message = f"tools node {node.id!r}: {key} holds more than {NODE_DOMAINS_MAX} values"
                out.append(Issue("invalid_value", message, "error", node_id=node.id))
                continue
            try:
                check_domains([str(v) for v in values])
            except DomainError as e:
                message = f"tools node {node.id!r}: {key} {e.value!r} is not allowed ({e.code})"
                out.append(Issue(e.code, message, "error", node_id=node.id))
    return out


def web_issues(spec: AgentSpec, ctx: GraphContext) -> list[Issue]:
    """웹 Tool 의 검증 이슈다. 그래프 전체의 규칙이라 agent_graph 의 검증 뒤에 더한다."""
    issues = _domain_issues(spec)
    a = analyze_structure(spec)
    graph = web_graph(spec, ctx.source_tools, a=a)
    if not graph.has_web:
        return issues
    for node in graph.nodes.values():
        if node.names is None:
            issues.append(
                Issue(
                    "web_tools_unknown_names",
                    f"tools node {node.node_id!r}: the source does not know its tool names, so no "
                    "per-tool effect or approval can be set beside web tools; choose the tools or "
                    "refresh the gateway's backends",
                    "error",
                    node_id=node.node_id,
                )
            )
    for agent_id, node_ids in graph.agents.items():
        first: dict[str, str] = {}
        for node_id in node_ids:
            for name in graph.nodes[node_id].web:
                if name in first and first[name] != node_id:
                    issues.append(
                        Issue(
                            "duplicate_tool_name",
                            f"agent {agent_id!r}: tools nodes {first[name]!r} and {node_id!r} "
                            f"both expose the web tool {name!r}",
                            "error",
                            node_id=node_id,
                        )
                    )
                first.setdefault(name, node_id)
    for node in graph.nodes.values():
        if WEB_FETCH in node.web.values() and not node.data.allow_domains:
            reach = "every conversation" if graph.data_tools else "conversations with attachments"
            issues.append(
                Issue(
                    "web_fetch_allowlist_empty",
                    f"tools node {node.node_id!r}: the allow list is empty, so fetch is refused in "
                    f"{reach}",
                    "warning",
                    node_id=node.node_id,
                )
            )
    return issues


# -- 컴파일 -------------------------------------------------------------------------------------


def _web_block(node: WebNode, settings: WebToolsSettings) -> dict[str, Any]:
    kinds = {kind: name for name, kind in node.web.items()}
    block = sorted({*check_domains(node.data.block_domains), *settings.fetch.blocked_host_suffixes})
    return {
        "fetch": kinds.get(WEB_FETCH),
        "search": kinds.get(WEB_SEARCH),
        "allow_domains": check_domains(node.data.allow_domains),
        "block_domains": block,
        "per_user": {
            "fetch_per_minute": settings.fetch.per_user_per_minute,
            "fetch_per_day": settings.fetch.per_user_per_day,
            "search_per_minute": settings.search.per_user_per_minute,
            "search_per_day": settings.search.per_user_per_day,
        },
    }


def attach_web(document: dict[str, Any], rev: AgentSpec, ctx: GraphContext) -> dict[str, Any]:
    """기능 `web_guard` 가 허락됐을 때의 붙이는 함수다. 문맥의 핀(`ctx.tool_pins`)을 따라
    `apply_pins` 가 정한 `allow` 와 같은 이름으로 `web_data_tools` 와 소스의 `web` 블록을 싣는다.
    받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    a = analyze_structure(rev)
    graph = web_graph(rev, ctx.source_tools, ctx.tool_pins, a=a)
    settings = ctx.web_tools or WebToolsSettings()
    final["web_data_tools"] = graph.data_tools
    for source in final["tools"]:
        node = graph.nodes.get(source["ref"])
        if node is not None and node.web:
            source["web"] = _web_block(node, settings)
    return final
