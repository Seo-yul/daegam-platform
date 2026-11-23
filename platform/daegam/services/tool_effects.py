"""Tool 효과 (P39b 14절 4부). 도구 소스(MCP Server, Gateway, 게시한 Agent)의 Tool 마다
`read`·`write`·`web` 하나다.

서버가 스스로 주는 힌트(`annotations.readOnlyHint`)는 보지 않는다(D48). 효과는 우리가 아는 것으로만
정한다. 서버나 항목의 주인이 Tool 규칙에 적은 읽기 전용 표시는 그 소스 자신의 효과다(P52 4절).

- 우리 mcp-runtime 의 Tool(MCP Server 소스와 Gateway 의 `internal` 백엔드)은 서버 주인의 표시
  (`ServerToolRule.read_only`)가 있으면 그것이고, 없으면 노출 이름을 operationId 로 되짚어 그
  Backend API 버전의 OpenAPI 메서드를 본다. GET·HEAD 는 `read`, 나머지는 `write` 다. 이름 규칙은
  `services/advertised_names.py` 한 곳이다. 메서드를 아직 모르는 버전(채우기 전의 옛 버전)은
  `write` 다.
- Gateway 의 `platform` 백엔드는 `web` 이고 fetch·search 가운데 무엇인지도 함께 든다. `published`
  백엔드는 원본 Gateway 까지 따라 내려가 그 Tool 의 효과를 이어받는다. 웹 Tool 이 게시를 거쳐도 웹
  Tool 로 보이게 하려는 것이다. 필터 없는 `published` 백엔드 아래에 웹 Tool 이 있으면 그 Gateway 는
  이름을 모르는 웹 소스다. `image` 백엔드는 그 항목의 표시(`ItemToolRule.read_only`)를 백엔드의
  원래 이름으로 따르고 표시가 없으면 `write` 다. `remote`·`entry` 는 `write` 다.
- 이름마다 서버나 항목의 표시가 그 효과 뒤에 설 수 있는지(`marked`)도 든다. 우리 mcp-runtime 의
  Tool 은 모두이고(메서드나 주인의 표시가 정한다), `image` 항목의 Tool 은 그 항목에 규칙이 있을
  때다. P43 의 승인 규칙에서 캔버스의 표시는 그 밖의 Tool 에서만 센다
  (`SourceTools.canvas_mark_counts`).
- 이름마다 누가 부를지의 규칙이나 인자 조건이 있는지(`ruled`)도 든다. 서버의 규칙, Gateway 자신의
  규칙과 `image` 항목의 규칙이다. 런타임이 거절된 호출을 견디는지 묻는 경고가 읽는다(P52 5절).
- 이름마다 누가 부를지의 규칙이 Agent 에게 여는 것(`gates`)도 든다. 규칙 하나가 하나다. Agent 가
  고른 Tool 을 규칙이 숨기는지 묻는 경고(`agent_tool_hidden`)가 읽는다(P52 2·7절).
- 캔버스가 tools 노드에 읽기 전용으로 표시한 Tool(`read_only`)은 `write` 대신 `read` 다.
- 이름마다 그 Tool 을 내는 백엔드의 종류(`origins`)도 든다. MCP Server 와 `internal` 백엔드는
  `internal`, 그 밖은 백엔드 종류(`image`·`remote`·`entry`·`platform`)이고 `published` 는 원본을
  따른다. 백엔드 종류를 모르는 이름은 `origins` 에 넣지 않는다. 이름을 모르는 백엔드가 하나라도
  있는 Gateway 는 어느 이름이 그 백엔드의 것인지 알 수 없어 `origins` 가 빈다. P43 의 바깥 표시와
  읽기 표시가 이것으로 갈린다(`agent_tool_safety.py`). 기능 `tool_safety` 를 아는 이미지에서는
  캔버스의 읽기 전용 표시가 서버나 항목의 표시가 설 수 없는 Tool(`remote`·`entry` 와 규칙 없는
  `image`)에만 효과가 있다(`node_effects`).
- 도는 렌더가 code mode 를 실은 Gateway 는 `execute_tool_script` 를 효과 `read`, 종류 `script` 로
  더 낸다(P56 6.5절). 그 Gateway 의 Tool 이 모두 읽기라는 판정이 그 효과 뒤에 선다. 게시된 원본의
  스크립트는 소비자가 내놓지 않으므로(`published_pins`) 소비자의 이름에서 뺀다.
- 게시한 Agent 를 부르는 소스는 Tool 하나(`ask_*`)를 효과 `write`, 종류 `agent` 로 낸다(P46
  6.6절, `agent_source`). 호출받은 Agent 가 제 Tool 로 데이터를 바꿀 수 있어서다. 캔버스의 읽기
  전용 표시는 이 효과를 바꾸지 못한다(`node_effects`).

검증·컴파일은 스레드 풀에서 돌므로 서비스가 효과의 재료를 미리 읽어 `GraphContext.source_tools`
(소스 id → `SourceTools`)에 둔다(`ToolEffectLoader`).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from pydantic import ValidationError

from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.advertised_names import (
    ExposedTool,
    gateway_exposed_name,
    gateway_tools,
    server_tools,
)
from daegam.services.agent_graph import ToolsData
from daegam.services.platform_tools import WEB_FETCH_ITEM
from daegam.store.agents import AgentSpec
from daegam.store.backend_apis import BackendAPIStore, BackendAPIVersion
from daegam.store.gateways import Gateway
from daegam.store.hub import PLATFORM_KIND, HubItem
from daegam.store.mcp_servers import MCPServer, MCPServerRevision
from daegam.store.tool_rules import has_caller_rule, has_conditions

EFFECT_READ = "read"
EFFECT_WRITE = "write"
EFFECT_WEB = "web"
ORIGIN_INTERNAL = "internal"
ORIGIN_PLATFORM = PLATFORM_KIND
THIRD_PARTY_ORIGINS = frozenset({"image", "remote", "entry"})
"""서버가 읽기 전용 여부를 알려 주지 않는 백엔드 종류다. 결과는 처음부터 바깥 내용이고 캔버스의 읽기
전용 표시가 효과를 낸다(P43 2·4절)."""
ORIGIN_SCRIPT = "script"
"""code mode Gateway 의 `execute_tool_script` 다(P56 6.5절). `internal` 도 바깥 서비스도 아니다.
효과는 `read` 이고, 결과는 늘 바깥 내용이며 캔버스의 어느 표시도 효과가 없다
(`agent_tool_safety`)."""
ORIGIN_AGENT = "agent"
"""게시한 Agent 를 부르는 `ask_*` 다(P46 6.6절). 효과는 늘 `write` 이고 결과는 늘 바깥 내용이며
캔버스의 어느 표시도 효과가 없다(`agent_tool_safety`)."""
READ_METHODS = frozenset({"GET", "HEAD"})
WEB_FETCH = "fetch"
WEB_SEARCH = "search"


class AgentGate(NamedTuple):
    """누가 부를지의 규칙 하나가 Agent 에게 여는 것 (P52 2절). agents 는 그 규칙이 이름 댄 Agent
    id 다. item 은 허브 `image` 항목의 규칙이라 역할만 받고 어떤 Agent 도 받지 않는다.
    gateway 는 그 규칙을 Gateway 의 vMCP 가 싣는가다. oidc 모드의 vMCP 는 Agent 절을 짓지
    않으므로 그 규칙은 모든 Agent 에게 닫힌다."""

    agents: frozenset[str]
    item: bool = False
    gateway: bool = False


@dataclass(frozen=True)
class SourceTools:
    """도구 소스 하나가 지금 광고하는 Tool 이다. names 는 광고 이름이고 모르면 None 이다(접두사
    모드의 Gateway 가 백엔드 이름을 모를 때). effects 는 이름마다의 효과이고 없는 이름은 `write` 다.
    web 은 웹 Tool 이름마다의 종류(fetch·search)다. origins 는 이름마다 그 Tool 을 내는 백엔드의
    종류이고 백엔드 종류를 모르는 이름은 넣지 않는다. marked 는 서버나 항목의 읽기 전용 표시가 그
    효과 뒤에 설 수 있는 이름이고, ruled 는 누가 부를지의 규칙이나 인자 조건이 있는 이름이다(모듈
    머리). gates 는 이름마다 누가 부를지의 규칙들이 Agent 에게 여는 것이다. 캔버스의 표시는 효과에도
    이 셋에도 들지 않는다."""

    names: tuple[str, ...] | None
    effects: Mapping[str, str] = field(default_factory=dict)
    web: Mapping[str, str] = field(default_factory=dict)
    origins: Mapping[str, str] = field(default_factory=dict)
    marked: frozenset[str] = frozenset()
    ruled: frozenset[str] = frozenset()
    gates: Mapping[str, tuple[AgentGate, ...]] = field(default_factory=dict)

    def effect(self, name: str) -> str:
        return self.effects.get(name, EFFECT_WRITE)

    def canvas_mark_counts(self, name: str) -> bool:
        """캔버스의 읽기 전용 표시가 바깥 내용 뒤의 승인 규칙에서 세는 Tool 인가. 서버가 읽기 여부를
        알려 주지 않는 백엔드이고 서버나 항목의 표시가 설 수 없는 Tool 이다
        (P43 4절)."""
        return canvas_mark_applies(self.origins.get(name)) and name not in self.marked


UNKNOWN_SOURCE = SourceTools(names=None)


def agent_source(tool: str) -> SourceTools:
    """게시한 Agent 하나를 부르는 소스다. Tool 은 tool 하나이고 쓰기다(모듈 머리)."""
    return SourceTools(names=(tool,), effects={tool: EFFECT_WRITE}, origins={tool: ORIGIN_AGENT})


def without_script(tools: SourceTools) -> SourceTools:
    """Gateway 가 자기 렌더로 내는 스크립트(종류 `script`)를 뺀 소스다. code mode 판정은 백엔드가
    광고하는 이름만 본다(P56 6.2절). 스크립트를 넣은 채로 판정하면 예약 이름으로 거절되어, 판정이
    매 렌더 code mode 를 열고 닫는다. 백엔드가 같은 이름을 낸 것은 그대로 둔다."""
    if tools.origins.get(CODE_MODE_TOOL) != ORIGIN_SCRIPT:
        return tools
    return replace(
        tools,
        names=tuple(n for n in tools.names if n != CODE_MODE_TOOL) if tools.names else tools.names,
        effects={n: e for n, e in tools.effects.items() if n != CODE_MODE_TOOL},
        origins={n: o for n, o in tools.origins.items() if n != CODE_MODE_TOOL},
        marked=tools.marked - {CODE_MODE_TOOL},
    )


def method_effect(method: str | None) -> str:
    return EFFECT_READ if method is not None and method.upper() in READ_METHODS else EFFECT_WRITE


def known_read(effect: str, origin: str | None) -> bool:
    """읽기라고 아는 Tool 인가. 웹 Tool 이거나, 효과가 `read` 이고 백엔드 종류를 안다. 백엔드 종류를
    모르는 이름은 효과가 읽기라도 읽기가 아니다(P43 4절, P45 10절)."""
    return effect == EFFECT_WEB or (effect == EFFECT_READ and origin is not None)


def canvas_mark_applies(origin: str | None) -> bool:
    """서버가 읽기 전용 여부를 알려 주지 않는 백엔드 종류인가(P43 4절). 그 가운데 서버나 항목의
    표시가 없는 Tool 에서만 캔버스의 표시가 센다(`SourceTools.canvas_mark_counts`)."""
    return origin in THIRD_PARTY_ORIGINS


def mark_effect(read_only: bool | None, default: str) -> str:
    """서버나 항목의 읽기 전용 표시가 정한 효과다. 표시가 없으면 default 다."""
    if read_only is None:
        return default
    return EFFECT_READ if read_only else EFFECT_WRITE


def node_effects(
    names: Iterable[str],
    tools: SourceTools,
    read_only: Collection[str] = (),
    *,
    tool_safety: bool = False,
) -> dict[str, str]:
    """tools 노드 하나의 Tool 효과다. 읽기 전용 표시는 `write` 만 `read` 로 바꾼다. tool_safety 면
    바깥 내용 뒤의 승인 규칙이 쓰는 효과다. 표시는 서버나 항목의 표시가 설 수 없는 Tool 에만 효과가
    있고(`SourceTools.canvas_mark_counts`), 백엔드 종류를 모르는 이름은 웹 Tool 이 아니면 쓰기다.
    런타임이 그 이름을 `read` 로 받지 않기 때문이다(`agent_tool_safety.node_marks`). 게시한 Agent
    를 부르는 Tool 은 표시와 상관없이 쓰기다."""
    out: dict[str, str] = {}
    for name in names:
        effect, origin = tools.effect(name), tools.origins.get(name)
        if tool_safety and origin is None and effect == EFFECT_READ:
            effect = EFFECT_WRITE
        marked = name in read_only and (not tool_safety or tools.canvas_mark_counts(name))
        marked = marked and origin != ORIGIN_AGENT
        out[name] = EFFECT_READ if effect == EFFECT_WRITE and marked else effect
    return out


def server_source(
    rev: MCPServerRevision, versions: Mapping[tuple[str, int], BackendAPIVersion]
) -> SourceTools:
    """MCP Server 리비전의 Tool 과 효과다. 필터 없는 백엔드의 버전을 못 찾으면 이름을 모른다. 효과는
    서버 주인의 표시가 먼저이고 없으면 메서드다. 모든 Tool 의 효과를 서버가 정한다(`marked`)."""
    complete = all(
        sel.tools or (sel.backend_profile_id, sel.spec_version) in versions for sel in rev.backends
    )
    methods: dict[str, dict[str, str | None]] = {}
    rules: dict[str, dict[str, Any]] = {}
    for sel in rev.backends:
        version = versions.get((sel.backend_profile_id, sel.spec_version))
        details = version.tool_details if version else []
        methods.setdefault(sel.backend_profile_id, {d.operation_id: d.method for d in details})
        rules.setdefault(sel.backend_profile_id, dict(sel.tool_rules))
    effects: dict[str, str] = {}
    ruled: set[str] = set()
    gates: dict[str, tuple[AgentGate, ...]] = {}
    exposed: list[str] = []
    for tool in server_tools(rev, versions):
        method = methods.get(tool.backend, {}).get(tool.original)
        rule = rules.get(tool.backend, {}).get(tool.original)
        if tool.exposed not in effects:
            read_only = rule.read_only if rule is not None else None
            effects[tool.exposed] = mark_effect(read_only, method_effect(method))
            if rule is not None and (has_caller_rule(rule) or has_conditions(rule)):
                ruled.add(tool.exposed)
            if rule is not None and has_caller_rule(rule):
                gates[tool.exposed] = (AgentGate(frozenset(rule.callers.agents)),)
        exposed.append(tool.exposed)
    names = tuple(dict.fromkeys(exposed)) if complete else None
    origins = dict.fromkeys(effects, ORIGIN_INTERNAL)
    return SourceTools(
        names=names,
        effects=effects,
        origins=origins,
        marked=frozenset(effects),
        ruled=frozenset(ruled),
        gates=gates,
    )


class BackendTool(NamedTuple):
    """Gateway 백엔드의 Tool 하나다. web 은 웹 Tool 의 종류, origin 은 백엔드 종류이고 모르면
    None 이다. marked 는 서버나 항목의 표시가 그 효과 뒤에 설 수 있는가, ruled 는 그 아래의 서버나
    항목에 누가 부를지의 규칙이나 인자 조건이 있는가다. gates 는 그 아래의 규칙들이 이 Gateway 의
    vMCP 에서 Agent 에게 여는 것이다."""

    effect: str
    web: str | None
    origin: str | None
    marked: bool = False
    ruled: bool = False
    gates: tuple[AgentGate, ...] = ()


def at_gateway(gates: Iterable[AgentGate]) -> tuple[AgentGate, ...]:
    """서버의 규칙을 Gateway 의 vMCP 가 실은 것. vMCP 는 서버의 규칙을 광고 이름으로 싣는다(2절)."""
    return tuple(g._replace(gateway=True) for g in gates)


def _backend_effect(
    tool: ExposedTool,
    kind: str,
    hub_item_name: str,
    below: SourceTools | None,
    item: HubItem | None = None,
) -> BackendTool:
    """Gateway 백엔드의 Tool 하나다. below 는 internal 이면 그 MCP Server, published 면 원본
    Gateway 의 것이다. 원본을 모르는 published 의 백엔드 종류는 None 이다. item 은 image 백엔드의
    허브 항목이고, 그 항목의 표시를 백엔드의 원래 이름으로 따른다."""
    original = tool.original
    if kind == PLATFORM_KIND:
        web = WEB_FETCH if hub_item_name == WEB_FETCH_ITEM else WEB_SEARCH
        return BackendTool(EFFECT_WEB, web, ORIGIN_PLATFORM)
    if kind == "internal":
        if below is None:
            return BackendTool(EFFECT_WRITE, None, ORIGIN_INTERNAL, marked=True)
        return BackendTool(
            below.effect(original),
            below.web.get(original),
            ORIGIN_INTERNAL,
            marked=True,
            ruled=original in below.ruled,
            gates=at_gateway(below.gates.get(original, ())),
        )
    if kind == "published":
        if below is None:
            return BackendTool(EFFECT_WRITE, None, None)
        return BackendTool(
            below.effect(original),
            below.web.get(original),
            below.origins.get(original),
            marked=original in below.marked,
            ruled=original in below.ruled,
            gates=below.gates.get(original, ()),
        )
    if kind == "image" and item is not None and item.tool_rules:
        rule = item.tool_rules.get(original)
        effect = mark_effect(rule.read_only if rule is not None else None, EFFECT_WRITE)
        ruled = rule is not None and has_caller_rule(rule)
        gates = (AgentGate(frozenset(), item=True, gateway=True),) if ruled else ()
        return BackendTool(effect, None, kind, marked=True, ruled=ruled, gates=gates)
    return BackendTool(EFFECT_WRITE, None, kind)


def gateway_source(
    gw: Gateway,
    *,
    servers: Mapping[str, SourceTools],
    published: Mapping[str, SourceTools],
    items: Mapping[str, HubItem] | None = None,
) -> SourceTools:
    """Gateway 배포 스냅샷의 Tool 과 효과다. 같은 노출 이름이 둘이면 vMCP 처럼 먼저 나온 것이다.
    servers 는 MCP Server id 마다, published 는 게시 항목 id 마다의 아래 소스이고, items 는 image
    백엔드의 허브 항목 id 마다의 항목이다. Gateway 자신의 규칙(배포한 리비전의 백엔드마다)이 있는
    이름도 `ruled` 에 든다.

    필터 없는 게시 백엔드는 원본 Gateway 가 지금 광고하는 것을 그대로 낸다. 그 아래에 웹 Tool 이
    있으면 이름을 모르는 웹 소스다. 스냅샷이 원본이 웹 Tool 을 얻기 전의 것이어도 검증이
    `web_tools_unknown_names` 로 막게 하려는 것이다. 원본의 `execute_tool_script` 는 빼고, 이
    Gateway 의 도는 렌더가 code mode 를 실었으면(`code_mode_applied`) 이 Gateway 의 스크립트를
    끝에 더한다(모듈 머리)."""
    if gw.deployed_revision is None:
        return UNKNOWN_SOURCE
    resolution, overrides = gw.deployed_conflict_resolution, gw.tool_overrides
    backends = {b.workload: b for b in gw.deployed_backends}
    own_rules = _gateway_rules(gw)
    effects: dict[str, str] = {}
    web: dict[str, str] = {}
    origins: dict[str, str] = {}
    marked: set[str] = set()
    ruled: set[str] = set()
    gates: dict[str, tuple[AgentGate, ...]] = {}
    exposed: list[str] = []
    for tool in gateway_tools(gw.deployed_backends, resolution, overrides):
        b = backends[tool.backend]
        if b.kind == "published" and tool.original == CODE_MODE_TOOL:
            continue  # 게시된 원본의 스크립트는 이 Gateway 가 내놓지 않는다(`published_pins`)
        below = published.get(b.hub_item_id) if b.kind == "published" else None
        if b.kind == "internal":
            below = servers.get(b.server_id or "")
        item = (items or {}).get(b.hub_item_id) if b.kind == "image" else None
        found = _backend_effect(tool, b.kind, b.hub_item_name, below, item)
        if tool.exposed not in effects:
            effects[tool.exposed] = found.effect
            if found.web is not None:
                web[tool.exposed] = found.web
            if found.origin is not None:
                origins[tool.exposed] = found.origin
            if found.marked:
                marked.add(tool.exposed)
            own = own_rules.get(b.hub_item_id, {}).get(tool.original)
            if found.ruled or own is not None:
                ruled.add(tool.exposed)
            own_gate = (AgentGate(own, gateway=True),) if own is not None else ()
            if found.gates or own_gate:
                gates[tool.exposed] = found.gates + own_gate
        exposed.append(tool.exposed)
    known = all(b.exposed_names() or b.tools for b in gw.deployed_backends)
    for b in gw.deployed_backends:
        below = published.get(b.hub_item_id) if b.kind == "published" and not b.tools else None
        if below is None or not below.web:
            continue
        known = False
        for original, kind in below.web.items():
            name = gateway_exposed_name(b.workload, original, resolution, overrides)
            effects.setdefault(name, EFFECT_WEB)
            web.setdefault(name, kind)
            origins.setdefault(name, ORIGIN_PLATFORM)
    if gw.code_mode_applied and CODE_MODE_TOOL not in effects:
        # 도는 렌더가 실은 스크립트다. 이름은 접두사 없이 그대로다(P56 6.5절)
        effects[CODE_MODE_TOOL] = EFFECT_READ
        origins[CODE_MODE_TOOL] = ORIGIN_SCRIPT
        marked.add(CODE_MODE_TOOL)
        exposed.append(CODE_MODE_TOOL)
    return SourceTools(
        names=tuple(dict.fromkeys(exposed)) if known else None,
        effects=effects,
        web=web,
        # 이름을 모르는 백엔드가 있으면 같은 이름이 그 백엔드의 것일 수 있다(모듈 머리)
        origins=origins if known else {},
        marked=frozenset(marked) if known else frozenset(),
        ruled=frozenset(ruled),
        gates=gates,
    )


def _gateway_rules(gw: Gateway) -> dict[str, dict[str, frozenset[str]]]:
    """Gateway 가 배포한 리비전의 규칙이 누가 부를지를 정한 원래 이름과 그 규칙이 이름 댄 Agent
    id 다. 허브 항목 id 마다다."""
    rev = gw.get_revision(gw.deployed_revision or 0)
    if rev is None:
        return {}
    return {
        b.hub_item_id: {
            n: frozenset(r.callers.agents) for n, r in b.tool_rules.items() if has_caller_rule(r)
        }
        for b in rev.backends
        if b.tool_rules
    }


def tools_nodes(spec: AgentSpec) -> list[tuple[str, ToolsData]]:
    """그래프의 tools 노드와 그 data 다. data 형식이 틀린 노드는 검증이 `invalid_node_data` 로
    막으므로 뺀다."""
    out: list[tuple[str, ToolsData]] = []
    for node in spec.graph.nodes:
        if node.type != "tools":
            continue
        try:
            out.append((node.id, ToolsData.model_validate(node.data)))
        except ValidationError:
            continue
    return out


def source_ids(spec: AgentSpec) -> set[str]:
    """tools 노드가 가리키는 도구 소스의 id 다."""
    return {data.source.id for _node, data in tools_nodes(spec)}


def node_names(data: ToolsData, tools: SourceTools) -> list[str]:
    """tools 노드가 쓰는 Tool 이름이다. 고른 Tool 이 있으면 그것이고, 없으면 소스가 지금 광고하는
    전부다. 배포 때의 핀은 `tool_pins.pinned_tools` 가 따른다."""
    if data.allow_tools:
        return list(data.allow_tools)
    return list(tools.names or ())


class GraphTools(NamedTuple):
    """tools 노드마다 Tool 이름의 효과와 백엔드 종류, 서버나 항목의 표시가 효과를 정하는 이름이다
    (`POST /api/agents/validate`). 캔버스가 효과 배지, 승인 표시, 바깥 표시의 기본값과 읽기 전용
    표시가 효과를 내는지를 단다(P43 2·4절, P52 4절). 캔버스의 표시는 효과의 기본값을 바꾸지 않으므로
    표시의 기본값은 소스의 효과다."""

    effects: dict[str, dict[str, str]]
    origins: dict[str, dict[str, str]]
    marked: dict[str, list[str]]


def graph_tools(
    spec: AgentSpec, sources: Mapping[str, SourceTools], *, tool_safety: bool = False
) -> GraphTools:
    """그래프의 tools 노드를 한 번 돌아 효과와 백엔드 종류를 낸다. 이름은 `node_names` 다. 효과는
    읽기 전용 표시를 따르고 tool_safety 면 바깥 내용 뒤의 승인 규칙이 쓰는 것이다(`node_effects`).
    종류를 모르는 이름은 `origins` 에 넣지 않는다."""
    out = GraphTools({}, {}, {})
    for node_id, data in tools_nodes(spec):
        tools = sources.get(data.source.id, UNKNOWN_SOURCE)
        names = node_names(data, tools)
        out.effects[node_id] = node_effects(names, tools, data.read_only, tool_safety=tool_safety)
        out.origins[node_id] = {n: o for n in names if (o := tools.origins.get(n))}
        out.marked[node_id] = [n for n in names if n in tools.marked]
    return out


class ToolEffectLoader:
    """검증·컴파일 문맥의 `source_tools` 를 만든다. 질의는 소스 수와 상관없이 몇 번이다. 게시 항목을
    따라 원본 Gateway 를 일괄로 읽고, 거기서 만난 MCP Server 와 그 버전도 일괄로 읽는다. image
    백엔드의 허브 항목은 게시 항목과 같은 질의로 읽는다."""

    def __init__(self, backend_apis: BackendAPIStore, mcp_servers: Any, gateways: Any, hub: Any):
        self.backend_apis = backend_apis
        self.mcp_servers = mcp_servers
        self.gateways = gateways
        self.hub = hub

    async def load(
        self, gateways: Mapping[str, Gateway], servers: Mapping[str, MCPServer]
    ) -> dict[str, SourceTools]:
        all_gateways = dict(gateways)
        published: dict[str, str] = {}  # 게시 항목 id → 원본 Gateway id
        images: dict[str, HubItem] = {}  # image 백엔드의 허브 항목 id → 항목 (P52 4절)
        frontier = list(gateways.values())
        while frontier:
            ids = [
                b.hub_item_id
                for gw in frontier
                for b in gw.deployed_backends
                if (b.kind == "published" and b.hub_item_id not in published)
                or (b.kind == "image" and b.hub_item_id not in images)
            ]
            items = await self.hub.get_many_unscoped(ids) if ids else {}
            for item_id, item in items.items():
                if item.kind == "image":
                    images[item_id] = item
                else:
                    published[item_id] = item.source.gateway_instance_id or ""
            missing = {g for g in published.values() if g and g not in all_gateways}
            found = await self.gateways.get_many(missing) if missing else {}
            all_gateways.update(found)
            frontier = list(found.values())
        all_servers = dict(servers)
        wanted = {
            b.server_id
            for gw in all_gateways.values()
            for b in gw.deployed_backends
            if b.kind == "internal" and b.server_id and b.server_id not in all_servers
        }
        if wanted:
            all_servers.update(await self.mcp_servers.get_many(wanted))
        revs = {sid: rev for sid, s in all_servers.items() if (rev := s.deployed) is not None}
        refs = {
            (sel.backend_profile_id, sel.spec_version) for r in revs.values() for sel in r.backends
        }
        versions = await self.backend_apis.get_versions(refs)
        server_tools_by_id = {sid: server_source(rev, versions) for sid, rev in revs.items()}
        out: dict[str, SourceTools] = dict(server_tools_by_id)
        resolved: dict[str, SourceTools] = {}

        def resolve(gateway_id: str, path: frozenset[str]) -> SourceTools:
            if gateway_id in resolved:
                return resolved[gateway_id]
            gw = all_gateways.get(gateway_id)
            if gw is None or gateway_id in path:
                return UNKNOWN_SOURCE
            below: dict[str, SourceTools] = {}
            for b in gw.deployed_backends:
                source_id = published.get(b.hub_item_id) if b.kind == "published" else None
                if source_id:
                    below[b.hub_item_id] = resolve(source_id, path | {gateway_id})
            resolved[gateway_id] = gateway_source(
                gw, servers=server_tools_by_id, published=below, items=images
            )
            return resolved[gateway_id]

        for gateway_id in gateways:
            out[gateway_id] = resolve(gateway_id, frozenset())
        return out
