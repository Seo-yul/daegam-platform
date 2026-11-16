"""평가 미리 보기의 컴파일이 쓰기 Tool 을 잡는 단계 (P45 10절, F0.2, D109).

평가 미리 보기의 컴파일은 배포처럼 Tool 핀을 받고 `apply_pins` 를 지난 뒤 이 단계를 탄다.

- **이름을 모르는 소스는 거절한다.** `allow` 가 빈 소스(`unpinned_sources`)는 무엇이 쓰기인지
  가를 수 없다. 그래서 `ToolsUnknown` 과 그 노드로 거절하고, 설정 지문의 컴파일에서 멈추므로 파드가
  뜨지 않는다.
- **읽기는 플랫폼이 아는 것으로만 센다.** OpenAPI 메서드, 서버나 허브 항목의 읽기 전용 표시, 웹
  Tool 이 읽기라고 할 때만 읽기다(`tool_effects.known_read`). 소스가 지금 주는 효과와 핀과 지금
  가운데 센 효과가 모두 읽기여야 하므로, 배포 때는 웹 Tool 이었으나 지금은 쓰기인 이름도 잡는다.
  캔버스의 읽기 전용 표시는 평가하는 리비전의 것이라 세지 않는다. 그 소스가 지금 광고하지 않는
  이름(`tools_missing`)과 백엔드 종류를 모르는 이름은 읽기라고 알 수 없다.
- **이름은 문서 전체로 잡는다.** 한 소스라도 읽기라고 알지 못하는 이름은 잡는다. 메인 에이전트는
  붙인 소스들의 `approval` 을 이름마다 합집합으로 합치고(`AgentConfig.approvals_for`),
  서브에이전트는 제 `interrupt_on` 을 쓴다(런타임 `compile.py`). 그래서 그 이름을 내거나 그 이름의
  승인을 가진 모든 소스의 `approval` 과, 그 이름을 내는 소스를 붙였거나 그 이름의 승인을 가진
  서브에이전트의 `interrupt_on` 을 결정이 `respond`·`reject` 뿐인 승인으로 바꾼다. 한 곳에만 걸면
  다른 곳의 `approve` 가 남기 때문이다. 모든 소스가 읽기라고 하는 이름은 노드의 승인을 그대로
  둔다.

langchain 은 요청의 허용 목록 밖의 결정을 거절하므로 잡힌 호출은 누구도 승인하거나 고칠 수 없고,
`respond` 나 `reject` 로만 답할 수 있다.
"""

from __future__ import annotations

import copy
from typing import Any

from daegam.services.agent_graph import Decision, GraphContext
from daegam.services.tool_effects import UNKNOWN_SOURCE, SourceTools, known_read, tools_nodes
from daegam.services.tool_pins import pinned_tools, unpinned_sources
from daegam.store.agents import AgentSpec

EVALUATION_TOOLS_UNKNOWN = "evaluation_tools_unknown"
# 잡힌 이름의 승인 결정. 누구도 승인하거나 고칠 수 없다 (10절)
HELD_DECISIONS: tuple[Decision, ...] = ("respond", "reject")


class ToolsUnknown(Exception):
    """이름을 모르는 소스가 있어 평가할 수 없다. refs 는 그 tools 노드다."""

    def __init__(self, refs: list[str]):
        super().__init__(f"tools nodes without known tool names: {', '.join(refs)}")
        self.refs = refs


def _is_read(name: str, current: SourceTools, merged: SourceTools) -> bool:
    """플랫폼이 읽기라고 아는 이름인가. 소스가 지금 광고하는 이름이어야 하고, 지금의 효과와 핀과
    지금 가운데 센 효과가 모두 읽기여야 한다."""
    if current.names is None or name not in current.names:
        return False
    return all(
        known_read(tools.effect(name), tools.origins.get(name)) for tools in (current, merged)
    )


def _read_names(
    document: dict[str, Any], spec: AgentSpec, ctx: GraphContext
) -> dict[str, set[str]]:
    """소스 ref → 그 소스의 `allow` 가운데 플랫폼이 읽기라고 아는 이름이다."""
    nodes = dict(tools_nodes(spec))
    out: dict[str, set[str]] = {}
    for source in document["tools"]:
        ref = source["ref"]
        data = nodes.get(ref)
        if data is None:
            out[ref] = set()
            continue
        current = ctx.source_tools.get(data.source.id, UNKNOWN_SOURCE)
        _names, merged = pinned_tools(data.allow_tools, current, ctx.tool_pins.get(ref))
        out[ref] = {n for n in source["allow"] if _is_read(n, current, merged)}
    return out


def hold_writes(document: dict[str, Any], spec: AgentSpec, ctx: GraphContext) -> dict[str, Any]:
    """`apply_pins` 를 지난 문서의 쓰기 Tool 을 잡는다(모듈 머리). 이름을 모르는 소스가 있으면
    `ToolsUnknown` 이다. 받은 문서는 바꾸지 않는다."""
    unknown = unpinned_sources(document)
    if unknown:
        raise ToolsUnknown(unknown)
    final = copy.deepcopy(document)
    reads = _read_names(final, spec, ctx)
    held = {n for s in final["tools"] for n in s["allow"] if n not in reads[s["ref"]]}
    offered = {s["ref"]: set(s["allow"]) for s in final["tools"]}
    for source in final["tools"]:
        _hold(source["approval"], held & (offered[source["ref"]] | set(source["approval"])))
    for sub in final["subagents"]:
        names = set().union(*(offered.get(ref, set()) for ref in sub["tools"]))
        _hold(sub["interrupt_on"], held & (names | set(sub["interrupt_on"])))
    return final


def _hold(rules: dict[str, list[str]], names: set[str]) -> None:
    for name in names:
        rules[name] = list(HELD_DECISIONS)
