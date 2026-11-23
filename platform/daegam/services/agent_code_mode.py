"""Agent 가 code mode Gateway 의 스크립트를 쓰는 규칙이다 (P56 6.5절, D106).

도는 렌더가 code mode 를 실은 Gateway 는 `execute_tool_script` 를 효과 `read`, 백엔드 종류 `script`
로 낸다(`tool_effects.gateway_source`). tools 노드가 쓰는 이름(고른 Tool, 고른 것이 없으면 배포 때의
핀을 따른 소스의 이름, `agent_web.web_graph`)에 그것이 있으면 그 노드가 지금 스크립트를 든다.

- **선택.** 스크립트는 Gateway 가 그 Agent 에게 주는 Tool 을 모두 부른다. 그래서 스크립트를 고른
  노드는 그 Gateway 의 Tool 을 모두 골라야 한다. 일부만 고르면 오류 `code_mode_partial_selection`
  이다. 좁은 선택을 보이면서 스크립트가 더 넓게 닿지 않게 하려는 것이고, 스크립트를 가두지는
  않는다. 고른 것이 없으면 배포 때의 핀이 스크립트를 포함한 모든 이름이 된다.
- **중복.** 에이전트 하나(메인이든 하위든)는 Gateway 하나의 스크립트만 든다. 승인과 런타임의 Tool
  이 맨 이름으로 묶여 두 Gateway 의 스크립트가 하나로 섞이기 때문이다(오류
  `code_mode_duplicate_script`).
- **받을 수 있는 소스.** 승인과 타임아웃은 컴파일하는 때 Gateway 가 code mode 를 실었는지가 아니라
  런타임이 닿을 수 있는 것으로 정한다. 런타임은 5분마다 `tools/list` 를 다시 읽으므로, 컴파일할 때
  code mode 가 닫혀 있던 Gateway 도 다시 열리면 스크립트를 준다. 그래서 최종 `allow` 가 비었거나
  스크립트를 이름 대는 Gateway 소스는 모두 스크립트를 받을 수 있는 소스로 본다. 그 Gateway 의
  백엔드가 지금 같은 이름의 보통 Tool 을 내면 뺀다.
- **승인.** 승인은 에이전트마다 Tool 이름으로 합쳐지는데 스크립트는 어느 노드가 골랐든 그 Gateway
  의 Tool 을 모두 부른다. 그래서 컴파일은 Agent 의 모든 tools 노드(메인과 하위 모두)의 승인을
  읽는다. 그 Gateway 를 소스로 둔 노드의 승인은 이름과 상관없이 모두이고(컴파일 때 스냅샷에 없던
  Tool 이 뒤에 다시 들어올 수 있다), 다른 소스의 노드는 그 Gateway 가 지금 광고하는 이름의 승인이다.
  그 결정들의 합집합을 스크립트를 받을 수 있는 소스마다 `execute_tool_script` 의 승인으로 둔다.
- **타임아웃.** 노드가 정하지 않으면 `execute_tool_script` 의 타임아웃은 ToolHive 가 스크립트를
  끊는 1분보다 15 초 긴 75 초다. 런타임이 아직 도는 스크립트를 먼저 끊지 않게 하려는 것이다(F0.5).
  런타임은 타임아웃을 소스와 상관없이 맨 이름으로 합치므로, 받을 수 있는 소스 가운데 하나라도 제
  값을 정했으면 기본값을 두지 않는다.
- **방언 줄.** 지금 스크립트를 든 에이전트의 시스템 프롬프트 끝에 Starlark 에 없는 Python 형식과,
  Tool 을 키워드 인자로만 부를 수 있다는 것을 적은 한 줄을 붙인다. 런타임 이미지 없이 agent.json 에
  실린다.

스크립트의 결과는 늘 바깥 내용이고 읽기다(`agent_tool_safety`). 런타임은 바뀌지 않는다. 스크립트는
Tool 호출 하나이고 턴의 한도가 한 번 센다.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from itertools import chain
from typing import Any

from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.agent_graph import GraphContext, Issue, analyze_structure
from daegam.services.agent_web import WebGraph, WebNode, web_graph
from daegam.services.gateway_timeouts import SCRIPT_MINUTE_SECONDS
from daegam.services.tool_effects import ORIGIN_SCRIPT, UNKNOWN_SOURCE, SourceTools, tools_nodes
from daegam.store.agents import AgentSpec

CODE_MODE_PARTIAL_SELECTION = "code_mode_partial_selection"
CODE_MODE_DUPLICATE_SCRIPT = "code_mode_duplicate_script"
# ToolHive 가 스크립트를 끊는 1분보다 길다 (P56 6.5절, F0.5)
SCRIPT_TIMEOUT_SECONDS = SCRIPT_MINUTE_SECONDS + 15
GATEWAY_SOURCE = "gateway_instance"
# 모델에게 주는 한 줄이다. F0.8 의 스크립트가 실패한 형식을 적는다 (P56 6.5절)
DIALECT_LINE = (
    "execute_tool_script runs Starlark, not Python: there is no set(), sum(), round(), import, "
    "json module or generator expression, no zero padding in % formats (%03d fails), no octal "
    "literals such as 010, and lists have no .sort(); use sorted(). Call tools with keyword "
    "arguments only, such as get_order(order_id=x): positional arguments fail. Use lists, dicts, "
    "for loops and list comprehensions, and deduplicate with a dict's keys."
)


def _holds_script(node: WebNode) -> bool:
    """tools 노드가 지금 code mode Gateway 의 스크립트를 쓰는가. 이름이 같아도 백엔드 종류가
    `script` 가 아니면 그 Gateway 의 백엔드가 낸 보통 Tool 이다."""
    return (
        node.data.source.kind == GATEWAY_SOURCE
        and CODE_MODE_TOOL in (node.names or ())
        and node.tools.origins.get(CODE_MODE_TOOL) == ORIGIN_SCRIPT
    )


def _held(graph: WebGraph) -> dict[str, str]:
    """지금 스크립트를 든 tools 노드 id → 그 Gateway id 다."""
    return {nid: n.data.source.id for nid, n in graph.nodes.items() if _holds_script(n)}


def code_mode_issues(spec: AgentSpec, ctx: GraphContext) -> list[Issue]:
    """선택과 중복의 오류다(모듈 머리). 검증 문맥에는 핀이 없어 소스의 지금 이름을 본다."""
    a = analyze_structure(spec)
    graph = web_graph(spec, ctx.source_tools, a=a)
    held = _held(graph)
    issues: list[Issue] = []
    for node_id, gateway_id in held.items():
        node = graph.nodes[node_id]
        if not node.data.allow_tools:
            continue
        missing = [n for n in node.tools.names or () if n not in node.data.allow_tools]
        if missing:
            issues.append(
                Issue(
                    CODE_MODE_PARTIAL_SELECTION,
                    f"tools node {node_id!r}: {CODE_MODE_TOOL} can call every tool of gateway "
                    f"{gateway_id}, so it cannot be used in a node that selects only some of them "
                    f"(not selected: {', '.join(missing)}); clear the selection to use every "
                    f"tool, or remove {CODE_MODE_TOOL} from it",
                    "error",
                    node_id=node_id,
                )
            )
    flagged: set[str] = set()
    for agent_id, node_ids in graph.agents.items():
        first: str | None = None
        for node_id in node_ids:
            gateway_id = held.get(node_id)
            if gateway_id is None:
                continue
            if first is None:
                first = gateway_id
            elif gateway_id != first and node_id not in flagged:
                flagged.add(node_id)
                agent = a.agent(agent_id)
                name = agent.name if agent is not None else agent_id
                issues.append(
                    Issue(
                        CODE_MODE_DUPLICATE_SCRIPT,
                        f"tools node {node_id!r}: agent {name!r} already takes {CODE_MODE_TOOL} "
                        f"from gateway {first}, and an agent can take it from one gateway only, "
                        "because approval settings and tool names do not tell gateways apart; "
                        f"select this gateway's tools without {CODE_MODE_TOOL}",
                        "error",
                        node_id=node_id,
                    )
                )
    return issues


def _merged(groups: Iterable[Iterable[str]]) -> list[str]:
    """결정 목록들의 합집합이다. 처음 나온 차례를 지킨다."""
    return list(dict.fromkeys(chain.from_iterable(groups)))


def _may_hold(source: dict[str, Any], tools: SourceTools) -> bool:
    """컴파일한 소스가 런타임에서 스크립트를 받을 수 있는가(모듈 머리). 빈 allow 는 다시 읽을 때
    얻는 Tool 을 모두 받고, allow 가 스크립트를 이름 대면 code mode 가 다시 열릴 때 받는다. 그
    Gateway 의 백엔드가 지금 같은 이름의 보통 Tool 을 내면 스크립트가 아니다."""
    if source["source"]["kind"] != GATEWAY_SOURCE:
        return False
    if source["allow"] and CODE_MODE_TOOL not in source["allow"]:
        return False
    plain = CODE_MODE_TOOL in tools.effects and tools.origins.get(CODE_MODE_TOOL) != ORIGIN_SCRIPT
    return not plain


def _gateway_decisions(spec: AgentSpec, gateway_id: str, tools: SourceTools) -> list[str]:
    """스크립트가 따라야 할 승인 결정의 합집합이다(모듈 머리). 그 Gateway 를 소스로 둔 노드의 승인은
    이름과 상관없이 모두이고, 다른 소스의 노드는 그 Gateway 가 지금 광고하는 이름의 승인이다."""
    advertised = set(tools.names or tools.effects)
    return _merged(
        decisions
        for _node_id, data in tools_nodes(spec)
        for name, decisions in data.approval.items()
        if (data.source.kind == GATEWAY_SOURCE and data.source.id == gateway_id)
        or name in advertised
    )


def _with_dialect(prompt: str) -> str:
    return f"{prompt}\n\n{DIALECT_LINE}" if prompt else DIALECT_LINE


def attach_code_mode(
    document: dict[str, Any], spec: AgentSpec, ctx: GraphContext
) -> dict[str, Any]:
    """핀을 따른(`tool_pins.apply_pins`) agent.json 에 스크립트의 승인, 타임아웃과 방언 줄을
    싣는다(모듈 머리). 스크립트를 받을 수 있는 소스가 없으면 문서를 그대로 돌려준다. 받은 문서는
    바꾸지 않는다."""
    current = ctx.source_tools
    may_hold = {
        s["ref"]: s["source"]["id"]
        for s in document["tools"]
        if _may_hold(s, current.get(s["source"]["id"], UNKNOWN_SOURCE))
    }
    if not may_hold:
        return document
    final = copy.deepcopy(document)
    sources = [s for s in final["tools"] if s["ref"] in may_hold]
    decisions = {
        gw: _gateway_decisions(spec, gw, current.get(gw, UNKNOWN_SOURCE))
        for gw in set(may_hold.values())
    }
    own_timeout = any(CODE_MODE_TOOL in s["timeouts"] for s in sources)
    for source in sources:
        rule = _merged(
            [source["approval"].get(CODE_MODE_TOOL, []), decisions[may_hold[source["ref"]]]]
        )
        if rule:
            source["approval"][CODE_MODE_TOOL] = rule
        if not own_timeout:
            source["timeouts"][CODE_MODE_TOOL] = SCRIPT_TIMEOUT_SECONDS
    by_ref = {s["ref"]: s for s in sources}
    # 하위 에이전트는 승인을 문서의 `interrupt_on` 으로 받는다. 메인은 런타임이 tools[].approval 을
    # 모은다
    for sub in final["subagents"]:
        rule = _merged(
            [
                sub["interrupt_on"].get(CODE_MODE_TOOL, []),
                *(
                    by_ref[ref]["approval"].get(CODE_MODE_TOOL, [])
                    for ref in sub["tools"]
                    if ref in by_ref
                ),
            ]
        )
        if rule:
            sub["interrupt_on"][CODE_MODE_TOOL] = rule
    held = set(_held(web_graph(spec, current, ctx.tool_pins))) & set(may_hold)
    for agent in (final["main"], *final["subagents"]):
        if held.intersection(agent["tools"]):
            agent["system_prompt"] = _with_dialect(agent["system_prompt"])
    return final
