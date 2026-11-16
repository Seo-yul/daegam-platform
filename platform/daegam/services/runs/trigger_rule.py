"""웹훅 트리거의 규칙 (P41 4·10절).

Agent 는 실행의 전제 조건(10절)과 함께 이 규칙을 지날 때만 트리거를 가진다. 핀된 효과가 `write` 인
Tool 은 메인 에이전트와 모든 서브에이전트에서 승인 규칙이 있어야 하고, 이름을 모르는 소스가 없어야
한다. 아니면 409 `trigger_needs_approval` 이다. 트리거를 저장할 때, 배포마다, 웹훅 실행을 시작하기
전마다 본다.
"""

from __future__ import annotations

from daegam.services.agent_web import web_graph
from daegam.services.runs.errors import RunError
from daegam.store.agents import Agent

TRIGGER_NEEDS_APPROVAL = "trigger_needs_approval"


def unguarded_tools(agent: Agent) -> list[str]:
    """트리거를 막는 것. 핀된 효과가 `write` 인데 승인 규칙이 없는 Tool(`<tools 노드>/<이름>`)과
    이름을 모르는 tools 노드다. 노드마다의 이름과 효과는 `agent_web.web_graph` 가 핀으로 정한다."""
    revision = agent.deployed
    if revision is None:
        return []
    pins = agent.tool_pins
    out: list[str] = []
    for node in web_graph(revision, {}, pins).nodes.values():
        if node.names is None:
            out.append(f"{node.node_id}: tool names unknown")
        else:
            out.extend(f"{node.node_id}/{name}" for name in node.defaults)
    return out


def check_trigger_rule(agent: Agent) -> None:
    unguarded = unguarded_tools(agent)
    if unguarded:
        raise RunError(
            409,
            TRIGGER_NEEDS_APPROVAL,
            "a webhook trigger needs an approval rule on every writing tool of this agent",
            tools=unguarded,
        )


__all__ = ["TRIGGER_NEEDS_APPROVAL", "check_trigger_rule", "unguarded_tools"]
