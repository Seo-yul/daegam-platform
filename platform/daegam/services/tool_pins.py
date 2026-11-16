"""배포 때의 Tool 핀 (P39b 14절 5부, D26).

런타임은 5분마다 `tools/list` 를 다시 읽고, 고른 Tool 이 없는 tools 노드는 소스가 주는 전부를 쓴다.
배포 뒤 Gateway 에 생긴 웹 Tool 이 가드 없이 들어오거나 웹 Tool 곁에 새로 생긴 write Tool 이 기본
승인 없이 들어오지 않게, 배포·재배포가 tools 노드마다 그때 쓰는 Tool 의 이름·효과·웹 종류를
`Agent.tool_pins {노드 id: ToolPin}` 에 박고 그 리비전을 `tool_pins_revision` 에 적는다.

- 고른 Tool 이 없는 노드의 컴파일(init 받기 포함) `allow` 는 핀한 이름 가운데 소스가 지금 광고하는
  것이다. 지금 이름을 모르면 핀한 이름 그대로다. 고른 Tool 이 있는 노드는 그 이름이 `allow` 이고
  핀은 효과와 웹 종류만 더한다. 빈 `allow` 는 "전부" 라서 남는 이름이 없는 소스는 문서에서 뺀다.
  그래서 빈 선택은 "배포 때의 모든 Tool" 이고 새 Tool 은 재배포 때 들어온다. 빈 핀 `[]` 은 "배포 때
  Tool 이 없었다" 하나의 뜻이다.
- 효과는 핀과 지금 가운데 센 쪽이다(`read` < `write` < `web`). 핀에 웹 Tool 이 있으면 그 이름이
  소스에서 사라져도 그래프는 웹 그래프다. 웹 백엔드를 뺐다가 다시 넣어도 가드가 남는다.
- 이름을 모르는 소스(백엔드 이름을 모르는 접두사 모드 Gateway)는 `names` 가 None 이고 예전처럼 다시
  읽는다. 웹 Tool 이 있는 그래프에서는 그것이 검증 오류다(`agent_web`). 핀이 없는 소스가 웹
  그래프에 남으면 컴파일이 409 `redeploy_required` 다.
- 상태 경고 `tools_behind: <노드>` 는 소스에 핀에 없는 Tool 이 생긴 것이고, `tools_missing: <노드>`
  는 핀한 이름이 소스에서 사라진 것이다(그 이름은 호출되지 않는다).
- 핀의 효과는 배포가 소스마다 컴파일한 효과의 기록이기도 하다. 서버나 허브 항목의 주인이 읽기 전용
  표시를 바꿔 소스에 지금 있는 Tool 의 효과가 핀과 달라지면 상태 경고 `tool_marks_changed: <노드>`
  다(P52 4절). 표시는 Agent 를 다시 배포해야 승인 설정에 들어가고, 표시를 바꾼 주인은 남의 Agent 를
  다시 배포할 수 없기 때문이다. 사라진 Tool 과 웹 Tool 의 효과는 표시가 아니므로 세지 않는다.
- 핀이 없는 Agent 가 쓰는 Gateway 에 웹 백엔드를 더하는 배포는 409 `web_backend_unpinned_agents`
  이고, 그 Agent 를 재배포하면 핀이 생긴다. 이름을 모를 때 핀한 Agent 는 409
  `web_backend_unknown_pins` 다.
- 노드가 내는 이름이 그 Agent 에 내장되는 Tool 의 이름과 같으면 런타임이 소스의 Tool 을 빼고 상태
  경고 `builtin_tool_name` 을 낸다. 같은 이름을 저장과 배포의 검증이 먼저 경고로 알린다
  (`builtin_name_issues`, 고른 Tool 이나 소스의 지금 이름). 내장 이름은 하위 Agent, 응답 형식,
  Codex 모델에 따라 다르다(`toolnames.builtin_tool_names`).
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from daegam.services.agent_graph import (
    GraphContext,
    Issue,
    JsonSchemaFormat,
    ModelData,
    ToolsData,
    analyze_structure,
)
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    UNKNOWN_SOURCE,
    SourceTools,
    node_names,
    tools_nodes,
)
from daegam.services.toolnames import CODEX_MODELS, builtin_tool_names
from daegam.store.agents import Agent, AgentSpec, ToolPin

ToolPins = dict[str, ToolPin]

REDEPLOY_REQUIRED = "redeploy_required"
PINNED_AGENTS_KEEP_TOOLS = "pinned_agents_keep_tools"
TOOLS_BEHIND = "tools_behind"
TOOLS_MISSING = "tools_missing"
TOOL_MARKS_CHANGED = "tool_marks_changed"
BUILTIN_TOOL_NAME = "builtin_tool_name"
# 서버나 항목의 읽기 전용 표시가 오가는 효과다. 웹 Tool 의 효과는 표시가 아니다
_MARK_EFFECTS = frozenset({EFFECT_READ, EFFECT_WRITE})
_STRENGTH = {EFFECT_READ: 0, EFFECT_WRITE: 1, EFFECT_WEB: 2}


def pins_of(spec: AgentSpec, sources: Mapping[str, SourceTools]) -> ToolPins:
    """tools 노드마다 지금 쓰는 Tool 의 이름·효과·웹 종류다. 이름을 모르면 names 가 None 이다."""
    pins: ToolPins = {}
    for node_id, data in tools_nodes(spec):
        tools = sources.get(data.source.id, UNKNOWN_SOURCE)
        if not data.allow_tools and tools.names is None:
            pins[node_id] = ToolPin(names=None, web=dict(tools.web))
            continue
        names = node_names(data, tools)
        pins[node_id] = ToolPin(
            names=names,
            effects={n: tools.effect(n) for n in names},
            web={n: tools.web[n] for n in names if n in tools.web},
        )
    return pins


def valid_pins(agent: Agent) -> ToolPins:
    """배포가 핀한 리비전에 박은 핀이다. 배포와 재배포가 핀과 리비전을 함께 쓴다."""
    return agent.tool_pins


def _effect(name: str, tools: SourceTools, pin: ToolPin) -> str:
    """핀과 지금 소스가 아는 효과 가운데 센 쪽이다. 둘 다 모르면 `write` 다."""
    found = [e for e in (pin.effects.get(name), tools.effects.get(name)) if e is not None]
    return max(found, key=lambda e: _STRENGTH.get(e, 1)) if found else EFFECT_WRITE


def pinned_tools(
    allow_tools: Collection[str], tools: SourceTools, pin: ToolPin | None
) -> tuple[list[str] | None, SourceTools]:
    """핀을 따른 노드의 (이름, 소스)다. None 은 이름을 모르는 것이다.

    이름은 고른 Tool 이 있으면 그것이고, 없으면 핀 가운데 소스에 지금 있는 것(지금 이름을 모르면
    핀 그대로)이다. 핀이 없거나 이름을 모를 때 핀했으면 지금 이름이다. 소스의 효과는 핀과 지금
    가운데 센 쪽이고 웹 종류는 지금 것이 앞이다."""
    if pin is None:
        return (list(allow_tools) if allow_tools else _names(tools)), tools
    if allow_tools:
        names: list[str] | None = list(allow_tools)
    elif pin.names is None:
        names = _names(tools)
    elif tools.names is None:
        names = list(pin.names)
    else:
        names = [n for n in pin.names if n in tools.names]
    if names is None:
        web = {**pin.web, **tools.web}
        return None, SourceTools(
            names=None,
            effects=tools.effects,
            web=web,
            origins=tools.origins,
            marked=tools.marked,
            ruled=tools.ruled,
        )
    effects = {n: _effect(n, tools, pin) for n in names}
    web = {n: kind for n in names if (kind := tools.web.get(n) or pin.web.get(n))}
    # 백엔드 종류는 핀하지 않는다. 지금 모르는 이름은 바깥이고 쓰기다(P43 2·4절). 서버나 항목의
    # 표시가 설 수 있는지와 규칙이 있는지도 지금 것이다(P52 4·5절)
    origins = {n: origin for n in names if (origin := tools.origins.get(n))}
    return names, SourceTools(
        names=tuple(names),
        effects=effects,
        web=web,
        origins=origins,
        marked=tools.marked & set(names),
        ruled=tools.ruled & set(names),
    )


def _names(tools: SourceTools) -> list[str] | None:
    return list(tools.names) if tools.names is not None else None


def apply_pins(
    document: dict[str, Any], spec: AgentSpec, pins: ToolPins, sources: Mapping[str, SourceTools]
) -> dict[str, Any]:
    """컴파일한 agent.json 에서 고른 Tool 이 없는 소스의 `allow` 를 핀으로 정한다(`pinned_tools`).
    남는 이름이 없는 소스는 그 참조와 함께 뺀다. 빈 allow 는 "전부" 이기 때문이다. 핀이 없거나
    이름을 모를 때 핀한 소스는 그대로 둔다. 받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    data = dict(tools_nodes(spec))
    dropped: set[str] = set()
    for source in final["tools"]:
        ref = source["ref"]
        pin = pins.get(ref)
        if ref not in data or data[ref].allow_tools or pin is None or pin.names is None:
            continue
        tools = sources.get(data[ref].source.id, UNKNOWN_SOURCE)
        names, _merged = pinned_tools((), tools, pin)
        if names:
            source["allow"] = names
        else:
            dropped.add(ref)
    if dropped:
        final["tools"] = [s for s in final["tools"] if s["ref"] not in dropped]
        for agent in (final["main"], *final["subagents"]):
            agent["tools"] = [ref for ref in agent["tools"] if ref not in dropped]
    return final


def unpinned_sources(document: Mapping[str, Any]) -> list[str]:
    """`allow` 가 빈 소스다. 웹 가드가 든 문서에서는 이름을 박지 않은 소스라 막는다."""
    return [s["ref"] for s in document["tools"] if not s["allow"]]


def pin_drift(spec: AgentSpec, pins: ToolPins, sources: Mapping[str, SourceTools]) -> list[str]:
    """고른 Tool 이 없는 노드에서 핀과 소스의 지금 이름이 갈린 것의 상태 경고다. 핀이나 지금 이름을
    모르면 보지 않는다."""
    out: list[str] = []
    for node_id, data in tools_nodes(spec):
        pin = pins.get(node_id)
        current = sources.get(data.source.id, UNKNOWN_SOURCE).names
        if data.allow_tools or pin is None or pin.names is None or current is None:
            continue
        if set(current) - set(pin.names):
            out.append(f"{TOOLS_BEHIND}: {node_id}")
        if set(pin.names) - set(current):
            out.append(f"{TOOLS_MISSING}: {node_id}")
    return out


def marks_drift(spec: AgentSpec, pins: ToolPins, sources: Mapping[str, SourceTools]) -> list[str]:
    """배포 뒤 소스의 읽기 전용 표시가 바뀐 tools 노드의 상태 경고다 (P52 4절). 핀한 이름 가운데
    소스에 지금 있는 Tool 의 효과가 핀의 효과와 다르면 그 노드다. 이름을 모를 때 핀했거나 효과를
    핀하지 않은 이름은 보지 않는다."""
    out: list[str] = []
    for node_id, data in tools_nodes(spec):
        pin = pins.get(node_id)
        if pin is None or pin.names is None:
            continue
        tools = sources.get(data.source.id, UNKNOWN_SOURCE)
        present = set(tools.names) if tools.names is not None else set(tools.effects)
        for name in pin.names:
            before, now = pin.effects.get(name), tools.effects.get(name, EFFECT_WRITE)
            if name not in present or before not in _MARK_EFFECTS or now not in _MARK_EFFECTS:
                continue
            if before != now:
                out.append(f"{TOOL_MARKS_CHANGED}: {node_id}")
                break
    return out


@dataclass
class PinStates:
    """Gateway 를 고른 Tool 없이 쓰는 배포된 Agent 의 id 를 핀의 상태로 가른 것이다. missing 은 핀이
    없는 것, unknown 은 이름을 모를 때 핀한 것, pinned 는 모두 핀한 것이다."""

    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)


def _open_nodes(agent: Agent, gateway_ids: Collection[str]) -> list[str]:
    """배포된 Agent 가 이 Gateway 들을 고른 Tool 없이 쓰는 tools 노드다. 그 노드에만 새 Tool 이
    들어온다."""
    rev = agent.deployed
    if rev is None or agent.state == "draft":
        return []
    return [
        node_id
        for node_id, data in tools_nodes(rev)
        if data.source.id in gateway_ids and not data.allow_tools
    ]


def pin_states(agents: Iterable[Agent], gateway_ids: Collection[str]) -> PinStates:
    """이 Gateway 들에 새 Tool 이 생길 때 Agent 가 그것을 언제 받는지로 가른다. 핀이 없으면 다음
    다시 읽기에, 이름을 모를 때 핀했으면 그 Agent 를 재배포해도 다음 다시 읽기에, 모두 핀했으면 다음
    재배포에 받는다."""
    out = PinStates()
    for agent in agents:
        nodes = _open_nodes(agent, gateway_ids)
        if not nodes:
            continue
        pins = valid_pins(agent)
        found = [pins.get(node_id) for node_id in nodes]
        if any(pin is None for pin in found):
            out.missing.append(agent.id)
        elif any(pin is not None and pin.names is None for pin in found):
            out.unknown.append(agent.id)
        else:
            out.pinned.append(agent.id)
    return out


def pins_behind(agents: Iterable[Agent], gateway_id: str, names: Collection[str]) -> list[str]:
    """이 Gateway 를 고른 Tool 없이 쓰는 배포된 Agent 가운데 핀한 이름이 names 와 다른 것의 id 다.
    그 Agent 는 재배포할 때까지 핀한 이름을 쓴다."""
    out: list[str] = []
    for agent in agents:
        pins = valid_pins(agent)
        for node_id in _open_nodes(agent, {gateway_id}):
            pin = pins.get(node_id)
            if pin is not None and pin.names is not None and set(pin.names) != set(names):
                out.append(agent.id)
                break
    return out


def builtin_name_issues(spec: AgentSpec, ctx: GraphContext) -> list[Issue]:
    """tools 노드의 소스가 내는 이름이 붙은 Agent 의 내장 Tool 이름과 같으면 노드마다 경고 하나다.
    런타임은 그 소스의 Tool 을 빼고 내장 Tool 을 쓴다(상태 경고 `builtin_tool_name`). 이름은 고른
    Tool, 없으면 소스가 지금 내는 이름이다. 검증 문맥에는 핀이 없다. 이름을 모르는 노드는 보지
    않는다. 내장 이름은 메인이 하위 Agent 를 부르면 task, 응답 형식이 있으면 structured_response,
    OpenAI SDK 로 부르는 Codex 모델이면 write_todos 가 더해진다. 하위 Agent 는 파일 Tool 과
    execute 뿐이다. 파일 Tool 을 꺼도 런타임이 같은 경고를 내므로 여기서도 센다. 이슈의 `data` 는
    겹친 이름을 Agent 의 캔버스 이름마다 싣는다."""
    a = analyze_structure(spec)
    clashes: dict[str, list[dict[str, Any]]] = {}
    for agent_id in a.order:
        agent = a.agent(agent_id)
        if agent is None or agent_id not in a.reachable:
            continue
        main = agent.role == "main"
        builtin = builtin_tool_names(
            subagents=main and bool(a.targets(agent_id, "delegate")),
            structured=main and isinstance(agent.response_format, JsonSchemaFormat),
            todos=_is_codex(a.model_of(agent_id), ctx),
        )
        for node_id, data in a.typed_targets(agent_id, "tools", ToolsData):
            source = ctx.source_tools.get(data.source.id, UNKNOWN_SOURCE)
            names, _tools = pinned_tools(data.allow_tools, source, None)
            found = sorted(builtin.intersection(names or ()))
            if found:
                clashes.setdefault(node_id, []).append({"agent": agent.name, "names": found})
    return [
        Issue(
            BUILTIN_TOOL_NAME,
            f"tools node {node_id!r}: the source offers tools named like built-in tools "
            f"({'; '.join(f'{c["agent"]}: {", ".join(c["names"])}' for c in found)}); "
            "the runtime keeps the built-in tool and drops the source's tool",
            "warning",
            node_id=node_id,
            data={"clashes": found},
        )
        for node_id, found in clashes.items()
    ]


def _is_codex(model: ModelData | None, ctx: GraphContext) -> bool:
    """런타임이 Codex harness profile 을 거는 모델인가. OpenAI SDK 로 부르는 provider(openai,
    openrouter, zai)의 모델 이름이 Codex 이름 가운데 하나여야 한다."""
    provider = ctx.providers.get(model.provider) if model is not None else None
    if model is None or provider is None or provider.sdk_kind != "openai":
        return False
    return (model.model or provider.effective_model) in CODEX_MODELS
