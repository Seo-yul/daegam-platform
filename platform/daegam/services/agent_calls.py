"""게시한 Agent 를 Tool 로 쓰는 tools 노드 (P46 6.2절).

tools 노드의 소스 종류 `agent` 는 허브의 `agent` 항목 하나를 가리킨다. 소스 id 가 항목 id 다. 그
노드는 Tool 하나를 낸다. 이름은 `ask_` 뒤에 항목 이름의 `-` 를 `_` 로 바꿔 붙인 것이고 인자는
`message` 하나다. 항목 이름에는 `_` 가 없어 두 항목이 한 이름이 되지 않는다.

- **읽기.** `AgentSourceLoader` 가 리비전이 가리키는 항목마다 `AgentSource` 를 만들어 검증·컴파일
  문맥(`GraphContext.agent_sources`)에 둔다. 저장하는 사람이 그 항목을 쓸 수 없는 까닭(`refusal`)도
  거기서 정한다. 항목이 없거나 `agent` 항목이 아니거나 그 Agent 가 사라졌으면
  `agent_source_unpublished`, 항목의 consume 이 없으면 `agent_source_no_consume`, 제 항목이면
  `agent_source_self`, 호출받는 Agent 가 턴을 받지 않으면 `source_not_deployed` 이고, 그 뒤는 게시의
  전제 조건이다(`AgentPublishing.call_refusal`).
- **검증.** 저장과 배포가 `call_issues` 를 본다. 위의 까닭, 그 노드의 규칙이 제 Tool 밖의 이름을
  대는 것(`invalid_node_data`. 읽기 전용 표시와 바깥 표시는 제 Tool 에도 걸지 못한다), 같은
  에이전트에 붙은 다른 노드와 겹치는 Tool 이름(`duplicate_tool_name`)과 내장 Tool 이름
  (`reserved_tool_name`)이 오류다. 경고는 셋이다. 호출받는 Agent 의 배포된 리비전이 제 Agent
  소스를 따라 저장하는 Agent 로 돌아오면 `agent_call_cycle`, 부르는 턴의 시간이 호출 하나가
  기다리는 시간보다 길지 않으면 `agent_call_timeout_short`, 이 플랫폼에서 호출이 꺼져 있으면
  `agent_calls_disabled` 다.
- **핀.** 배포가 노드마다 호출받는 Agent 의 id 와 이름, 항목의 Tool 설명, 기다리는 시간을 핀에
  박는다(`AgentCallPin`, `tool_pins.pins_of`). 기다리는 시간은 호출받는 Agent 의 배포된 턴 시간에
  30초를 더한 것이다.
- **컴파일.** `AgentSource.tool_doc` 이 핀으로 agent.json 의 `tools[]` 항목을 만든다. 소스의 id 는
  항목이 아니라 호출받는 Agent 의 것이고, `url` 은 플랫폼의 호출 경로다. 노드의 `timeouts` 는
  기다리는 시간을 줄이기만 한다. 컴파일은 위의 검증을 보지 않는다. 게시를 거두거나 호출받는 Agent
  가 멈춰도 부르는 Agent 의 파드는 핀으로 뜨고, 그 Tool 은 호출할 때 거절된다.
- **상태 경고.** 배포된 리비전의 소스를 지금 상태로 다시 읽어 `agent_source_unpublished`,
  `agent_source_not_serving`, `agent_call_timeout_short`, `agent_calls_disabled` 를 낸다
  (`status_warnings`).

- **작업을 맡기는 노드 (6.7절).** 노드가 작업 Tool 다섯(`tool_effects.TASK_TOOLS`)을 고르면 답을
  기다리지 않고 작업을 맡긴다. 다섯을 함께 골라야 하고 `ask_*` 와 섞지 못한다. 작업은 부르는
  세션의 상태에 남으므로 메인 에이전트에만 붙이고, 호출받는 Agent 의 이미지가 `agent_tasks` 를
  알려야 한다(`runtime_feature_missing`). 메인에 붙은 그런 노드들은 Tool 다섯 한 벌을 같이 쓰므로
  서로 이름이 겹치는 것은 오류가 아니다. 기다리는 시간이 없어 `timeouts` 를 받지 않고
  `agent_call_timeout_short` 도 내지 않는다. 문서의 항목은 `mode` 가 `task` 이고 `url` 이 그 Agent
  의 Agent Protocol 경로다.

Tool 의 효과와 바깥 표시는 `tool_effects.agent_source` 와 `agent_tool_safety` 가 정한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from daegam.auth.hub_access import can
from daegam.auth.principal import Principal
from daegam.k8s.templates.mcpserver import api_base
from daegam.services.agent_features import AGENT_TASKS, RUNTIME_FEATURE_MISSING
from daegam.services.agent_graph import (
    AGENT_SOURCE,
    Analysis,
    GraphContext,
    GraphError,
    Issue,
    KnowledgeData,
    ToolsData,
    agent_source_items,
    analyze_structure,
)
from daegam.services.agent_publish import CALLS_DISABLED, RUNS_UNMET, AgentPublishing
from daegam.services.agent_runtime import serves_turns
from daegam.services.hub import HubService
from daegam.services.tool_effects import (
    TASK_TOOLS,
    UNKNOWN_SOURCE,
    SourceTools,
    agent_source,
    assigns_tasks,
    node_names,
    tools_nodes,
)
from daegam.services.tool_rules import coded_warning
from daegam.services.toolnames import RESERVED_TOOL_NAMES
from daegam.store.agents import Agent, AgentCallPin, AgentSpec, MongoAgentStore, ToolPin
from daegam.store.hub import AGENT_KIND, HubItem
from daegam.urls import AGENT_CALL_PATH, AGENT_PROTOCOL_PATH

# Tool 이름의 머리다. 뒤에 항목 이름이 붙는다
TOOL_PREFIX = "ask_"
# 호출 하나가 호출받는 Agent 의 턴 시간보다 더 기다리는 시간(초)이다. 실행을 시작하고 답을 읽어
# 오는 데 든다
CALL_GRACE_SECONDS = 30
# 작업을 맡기는 소스의 문서 항목이 싣는 `mode` 다. 답을 기다리는 소스의 항목에는 그 칸이 없다
TASK_MODE = "task"

SOURCE_UNPUBLISHED = "agent_source_unpublished"
SOURCE_NO_CONSUME = "agent_source_no_consume"
SOURCE_SELF = "agent_source_self"
SOURCE_NOT_DEPLOYED = "source_not_deployed"
SOURCE_NOT_SERVING = "agent_source_not_serving"
CALL_CYCLE = "agent_call_cycle"
CALL_TIMEOUT_SHORT = "agent_call_timeout_short"

# 새 배포가 뜨는 동안에는 `agent_source_not_serving` 을 내지 않는다. 곧 턴을 받는다
_DEPLOYING = "deploying"
# 상태 경고의 문장이다. 화면은 코드로 문장을 그리고 끝의 괄호에서 tools 노드를 읽는다
STATUS_TEXT: dict[str, str] = {
    SOURCE_UNPUBLISHED: "agents connected as tools are no longer published, so calls are refused",
    SOURCE_NOT_SERVING: "agents connected as tools do not take conversations now, so calls are "
    "refused",
    CALL_TIMEOUT_SHORT: "this agent's turn time is not longer than the wait for the called "
    "agent, so a call may stop before the answer comes",
}
CALLS_DISABLED_TEXT = "agent calls are turned off on this platform, so calls are refused"


def tool_name(item_name: str) -> str:
    """항목 이름이 내는 Tool 이름이다."""
    return TOOL_PREFIX + item_name.replace("-", "_")


def call_url(platform_url: str, agent_id: str) -> str:
    """부르는 파드가 그 Agent 의 호출을 시작하는 플랫폼 주소다."""
    return api_base(platform_url) + AGENT_CALL_PATH.format(agent_id=agent_id)


def task_url(platform_url: str, agent_id: str) -> str:
    """부르는 파드가 그 Agent 에게 작업을 맡기는 플랫폼 주소다. 그 아래에 Agent Protocol 의 경로가
    있다."""
    return api_base(platform_url) + AGENT_PROTOCOL_PATH.format(agent_id=agent_id)


class Refusal(NamedTuple):
    """저장하는 사람이 그 항목을 Tool 로 쓸 수 없는 까닭이다."""

    code: str
    message: str


@dataclass(frozen=True)
class AgentSource:
    """tools 노드가 가리키는 `agent` 항목 하나를 지금 상태로 읽은 것이다. refusal 이 있으면 저장과
    배포가 그 코드로 막는다. tool 부터 record_bodies 까지는 항목과 호출받는 Agent 를 읽었을 때만
    차고, state 는 그 Agent 의 state 다. timeout_seconds 는 호출 하나가 답을 기다리는 시간이다.
    provider 와 record_bodies 는 부르는 대화의 글이 가는 곳이라 캔버스가 보인다. cycle 은 그 Agent
    가 저장하는 Agent 를 다시 부르는가다. takes_tasks 는 그 Agent 의 이미지가 맡긴 작업의 세션을
    아는가다. platform_url 은 컴파일하는 파드가 플랫폼을 부르는 주소이고 컴파일 문맥에서만 찬다."""

    item_id: str
    refusal: Refusal | None = None
    tool: str = ""
    description: str = ""
    agent_id: str = ""
    agent_name: str = ""
    state: str | None = None
    timeout_seconds: int = 0
    provider: str = ""
    record_bodies: bool = False
    cycle: bool = False
    takes_tasks: bool = False
    platform_url: str = ""

    def pin(self) -> AgentCallPin | None:
        """배포가 박을 것이다. 쓸 수 없는 소스는 None 이다."""
        if self.refusal is not None:
            return None
        return AgentCallPin(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            description=self.description,
            timeout_seconds=self.timeout_seconds,
        )

    def tool_doc(self, ref: str, data: ToolsData, pin: ToolPin | None) -> dict[str, Any]:
        """agent.json 의 `tools[]` 항목이다. 배포 때의 핀을 따르고, 핀이 없으면 지금 읽은 것이다.
        둘 다 없으면 `GraphError` 다. 승인 규칙은 제 Tool 의 것만 싣는다. 작업을 맡기는 노드의
        항목은 Tool 다섯과 `mode` 를 싣고 기다리는 시간이 없다."""
        call, tool = self.pin(), self.tool
        if pin is not None and pin.call is not None and pin.names:
            call, tool = pin.call, pin.names[0]
        if call is None:
            code, message = self.refusal or _unpublished(self.item_id)
            raise GraphError([Issue(code, f"tools node {ref!r}: {message}", node_id=ref)])
        task = assigns_tasks(data)
        own = TASK_TOOLS if task else (tool,)
        entry = {
            "ref": ref,
            "source": {"kind": AGENT_SOURCE, "id": call.agent_id, "name": call.agent_name},
            "allow": list(own),
            "approval": {t: list(d) for t, d in data.approval.items() if t in own},
            "timeouts": {},
            "description": call.description,
        }
        if task:
            return {**entry, "url": task_url(self.platform_url, call.agent_id), "mode": TASK_MODE}
        wait = call.timeout_seconds
        return {
            **entry,
            "url": call_url(self.platform_url, call.agent_id),
            "timeout_seconds": min(wait, data.timeouts.get(tool, wait)),
        }

    def view(self) -> dict[str, Any]:
        """캔버스가 노드 곁에 보이는 것이다."""
        return {
            "tool": self.tool,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "provider": self.provider,
            "record_bodies": self.record_bodies,
            "timeout_seconds": self.timeout_seconds,
        }


def _unpublished(item_id: str) -> Refusal:
    return Refusal(SOURCE_UNPUBLISHED, f"hub item {item_id!r} is not a published agent")


def source_tools(sources: Mapping[str, AgentSource]) -> dict[str, SourceTools]:
    """읽은 소스마다의 Tool 과 효과다(항목 id → SourceTools). 이름을 읽지 못한 소스는 없다."""
    return {item_id: agent_source(s.tool) for item_id, s in sources.items() if s.tool}


def addressed(sources: Mapping[str, AgentSource], platform_url: str) -> dict[str, AgentSource]:
    """컴파일 문맥의 소스다. 컴파일을 받는 파드가 플랫폼을 부르는 주소를 싣는다."""
    return {item_id: replace(s, platform_url=platform_url) for item_id, s in sources.items()}


def node_views(spec: AgentSpec, sources: Mapping[str, AgentSource]) -> dict[str, dict[str, Any]]:
    """tools 노드 id → 그 노드가 부르는 Agent (`AgentSource.view`). 항목과 Agent 를 읽은 노드만
    있다."""
    return {
        node_id: source.view()
        for node_id, data in _call_nodes(tools_nodes(spec))
        if (source := sources.get(data.source.id)) is not None and source.agent_id
    }


def _call_nodes(nodes: Iterable[tuple[str, ToolsData]]) -> list[tuple[str, ToolsData]]:
    return [(node_id, data) for node_id, data in nodes if data.source.kind == AGENT_SOURCE]


# -- 검증 ---------------------------------------------------------------------------------------


def _turn_timeout(a: Analysis) -> int | None:
    """부르는 턴의 시간이다. 메인 에이전트의 것이고, 메인이 없는 그래프는 None 이다."""
    main = a.agent(a.main) if a.main is not None else None
    return main.turn_timeout_seconds if main is not None else None


def _waits_too_long(source: AgentSource, turn_timeout: int | None) -> bool:
    """부르는 턴이 호출 하나가 기다리는 시간보다 길지 않은가. 그러면 답이 오기 전에 턴이 끝난다."""
    return turn_timeout is not None and turn_timeout <= source.timeout_seconds


def _stray_rules(data: ToolsData, own: Iterable[str], *, waits: bool) -> list[str]:
    """노드의 규칙 가운데 그 노드가 받지 않는 것(`칸: 이름`)이다. 고른 Tool, 승인, 승인 기억은 제
    Tool(own)만 대고, 읽기 전용 표시와 바깥 표시는 어느 이름도 대지 못한다. 타임아웃은 답을
    기다리는 노드(waits)만 제 Tool 에 건다."""
    named = {
        "allow_tools": data.allow_tools,
        "approval": data.approval,
        "rememberable": data.rememberable,
        **({"timeouts": data.timeouts} if waits else {}),
    }
    fixed = {
        "read_only": data.read_only,
        "outside": data.outside,
        **({} if waits else {"timeouts": data.timeouts}),
    }
    mine = set(own)
    stray = [f"{key}: {name}" for key, names in named.items() for name in names if name not in mine]
    return stray + [f"{key}: {name}" for key, names in fixed.items() for name in names]


def _task_issues(node_id: str, data: ToolsData, source: AgentSource, main: bool) -> list[Issue]:
    """작업을 맡기는 노드에만 있는 오류다(모듈 머리)."""
    where = f"tools node {node_id!r}"
    out: list[Issue] = []
    missing = [name for name in TASK_TOOLS if name not in data.allow_tools]
    if missing:
        out.append(
            Issue(
                "invalid_node_data",
                f"{where}: the task tools come together; add {', '.join(missing)}",
                node_id=node_id,
            )
        )
    if not main:
        out.append(
            Issue(
                "invalid_node_data",
                f"{where}: task tools connect only to the main agent, whose session keeps the "
                "tasks",
                node_id=node_id,
            )
        )
    if not source.takes_tasks:
        out.append(
            Issue(
                RUNTIME_FEATURE_MISSING,
                f"{where}: the runtime image of agent {source.agent_name!r} has not reported "
                f"the {AGENT_TASKS!r} capability, so it takes no tasks; redeploy that agent",
                node_id=node_id,
                data={"feature": AGENT_TASKS},
            )
        )
    return out


def _own_tools(data: ToolsData, source: AgentSource) -> tuple[str, ...]:
    """노드가 내는 Tool 이다. 작업을 맡기는 노드는 다섯, 답을 기다리는 노드는 `ask_*` 하나다."""
    return TASK_TOOLS if assigns_tasks(data) else (source.tool,)


def _node_issues(
    node_id: str, data: ToolsData, source: AgentSource, turn_timeout: int | None, *, main: bool
) -> list[Issue]:
    """노드 하나의 이슈다. main 은 그 노드가 메인 에이전트에만 붙었는가다."""
    where = f"tools node {node_id!r}"
    if source.refusal is not None:
        return [Issue(source.refusal.code, f"{where}: {source.refusal.message}", node_id=node_id)]
    out: list[Issue] = []
    waits = not assigns_tasks(data)
    stray = _stray_rules(data, _own_tools(data, source), waits=waits)
    if stray:
        out.append(
            Issue(
                "invalid_node_data",
                f"{where}: an agent source offers the tool {source.tool!r} or the five task "
                f"tools, with fixed effects and outside content; remove {', '.join(stray)}",
                node_id=node_id,
            )
        )
    if not waits:
        out.extend(_task_issues(node_id, data, source, main))
    if source.tool in RESERVED_TOOL_NAMES:
        out.append(
            Issue(
                "reserved_tool_name",
                f"{where}: tool name {source.tool!r} is a built-in tool of the runtime",
                node_id=node_id,
            )
        )
    if source.cycle:
        out.append(
            Issue(
                CALL_CYCLE,
                f"{where}: agent {source.agent_name!r} calls this agent back through its own "
                "agent tools; that call is refused when it runs",
                "warning",
                node_id=node_id,
            )
        )
    if waits and _waits_too_long(source, turn_timeout):
        out.append(
            Issue(
                CALL_TIMEOUT_SHORT,
                f"{where}: the main agent's turn limit ({turn_timeout}s) is not longer than the "
                f"wait for agent {source.agent_name!r} ({source.timeout_seconds}s), so a call may "
                "stop before the answer comes",
                "warning",
                node_id=node_id,
            )
        )
    return out


def _offered(a: Analysis, ctx: GraphContext, agent_id: str) -> list[tuple[str, str]]:
    """에이전트 하나에 붙은 tools·knowledge 노드가 내는 (노드 id, Tool 이름)이다. 이름을 모르는
    소스는 없다."""
    out: list[tuple[str, str]] = []
    for node_id, data in a.typed_targets(agent_id, "tools", ToolsData):
        tools = ctx.source_tools.get(data.source.id, UNKNOWN_SOURCE)
        out.extend((node_id, name) for name in node_names(data, tools))
    for node_id, knowledge in a.typed_targets(agent_id, "knowledge", KnowledgeData):
        out.append((node_id, knowledge.tool_name))
    return out


def _name_issues(
    a: Analysis, ctx: GraphContext, calls: Mapping[str, tuple[str, ...]]
) -> list[Issue]:
    """같은 에이전트에 붙은 다른 노드가 Agent 소스의 Tool 이름을 내면 런타임에서 하나가 다른 하나를
    덮으므로 `duplicate_tool_name` 이다. calls 는 Agent 소스 노드 id → 그 노드의 Tool 이름이다.
    작업을 맡기는 노드끼리는 Tool 다섯 한 벌을 같이 쓰므로 겹침이 아니다."""
    found: dict[str, Issue] = {}
    for agent_id in a.of_type("agent"):
        offered = _offered(a, ctx, agent_id)
        for node_id, name in offered:
            shares = name in TASK_TOOLS
            others = sorted(
                {
                    n
                    for n, other in offered
                    if other == name and n != node_id and not (shares and name in calls.get(n, ()))
                }
            )
            if name not in calls.get(node_id, ()) or not others:
                continue
            found.setdefault(
                node_id,
                Issue(
                    "duplicate_tool_name",
                    f"agent {agent_id!r}: nodes {others[0]!r} and {node_id!r} both expose the "
                    f"tool {name!r}",
                    node_id=node_id,
                ),
            )
    return list(found.values())


def call_issues(spec: AgentSpec, ctx: GraphContext, *, enabled: bool = True) -> list[Issue]:
    """Agent 소스의 검증 이슈다(모듈 머리). 저장 앞의 검증과 배포·재배포가 그래프 검증 뒤에 더한다.
    enabled 는 이 플랫폼에서 호출이 켜져 있는가다. Agent 소스가 없는 그래프는 이슈가 없다."""
    a = analyze_structure(spec)
    nodes = _call_nodes(a.nodes_of("tools", ToolsData))
    if not nodes:
        return []
    issues: list[Issue] = []
    calls: dict[str, tuple[str, ...]] = {}
    turn_timeout = _turn_timeout(a)
    on_subs = {
        node_id
        for agent_id in a.of_type("agent")
        if agent_id != a.main
        for node_id in a.targets(agent_id, "tools")
    }
    for node_id, data in nodes:
        source = ctx.agent_sources.get(data.source.id)
        if source is None:
            source = AgentSource(data.source.id, refusal=_unpublished(data.source.id))
        calls[node_id] = _own_tools(data, source)
        main = node_id not in on_subs
        issues.extend(_node_issues(node_id, data, source, turn_timeout, main=main))
    issues.extend(_name_issues(a, ctx, calls))
    if not enabled:
        issues.append(Issue(CALLS_DISABLED, CALLS_DISABLED_TEXT, "warning"))
    return issues


# -- 상태 경고 -----------------------------------------------------------------------------------


def _status_code(
    source: AgentSource | None, turn_timeout: int | None, *, waits: bool
) -> str | None:
    """배포된 소스 하나가 내는 상태 경고의 코드다. 낼 것이 없으면 None 이다. waits 는 그 노드가
    답을 기다리는가다."""
    if source is None or (source.refusal is not None and source.refusal.code == SOURCE_UNPUBLISHED):
        return SOURCE_UNPUBLISHED
    if source.refusal is None:
        return CALL_TIMEOUT_SHORT if waits and _waits_too_long(source, turn_timeout) else None
    stopped = source.refusal.code == SOURCE_NOT_DEPLOYED and source.state != _DEPLOYING
    return SOURCE_NOT_SERVING if stopped or source.refusal.code == RUNS_UNMET else None


def status_warnings(
    rev: AgentSpec, sources: Mapping[str, AgentSource], *, enabled: bool = True
) -> list[str]:
    """배포된 리비전의 Agent 소스가 내는 상태 경고다(모듈 머리). 코드마다 한 줄이고 끝의 괄호가
    그 tools 노드들이다."""
    nodes = _call_nodes(tools_nodes(rev))
    if not nodes:
        return []
    turn_timeout = _turn_timeout(analyze_structure(rev))
    found: dict[str, list[str]] = {}
    for node_id, data in nodes:
        waits = not assigns_tasks(data)
        code = _status_code(sources.get(data.source.id), turn_timeout, waits=waits)
        if code is not None:
            found.setdefault(code, []).append(node_id)
    lines = [] if enabled else [coded_warning(CALLS_DISABLED, CALLS_DISABLED_TEXT)]
    return lines + [
        coded_warning(code, STATUS_TEXT[code], ", ".join(found[code]))
        for code in STATUS_TEXT
        if code in found
    ]


# -- 읽기 ---------------------------------------------------------------------------------------


class AgentSourceLoader:
    """리비전이 가리키는 `agent` 항목을 읽어 `AgentSource` 로 만든다. 질의는 항목과 호출받는 Agent
    를 한 번씩 일괄로 읽고, 순환은 저장하는 Agent 가 게시돼 있을 때만 따라간다."""

    def __init__(self, *, hub: HubService, agents: MongoAgentStore, publishing: AgentPublishing):
        self.hub = hub
        self.agents = agents
        self.publishing = publishing

    @property
    def enabled(self) -> bool:
        return self.publishing.enabled

    async def load(
        self,
        tenant: str,
        spec: AgentSpec,
        *,
        principal: Principal | None = None,
        agent_id: str | None = None,
    ) -> dict[str, AgentSource]:
        """항목 id → AgentSource. principal 은 저장하는 사람이고 없으면(컴파일, 상태) 항목의 접근
        정책을 보지 않는다. agent_id 는 저장하는 Agent 이고 없으면(새 Agent, 상태) 제 항목과 순환을
        보지 않는다. 리비전이 가리키는 모든 항목이 답에 있다."""
        ids = agent_source_items(spec)
        if not ids:
            return {}
        items = await self.hub.store.get_many_unscoped(ids)
        called = await self.agents.get_many(
            {i.source.agent_id for i in items.values() if i.source.agent_id}
        )
        back = await self._calling_back(agent_id, called.values())
        out: dict[str, AgentSource] = {}
        for item_id in ids:
            item = items.get(item_id)
            doc = called.get(item.source.agent_id or "") if item is not None else None
            if item is None or doc is None or not self._visible(item, tenant, principal):
                out[item_id] = AgentSource(item_id, refusal=_unpublished(item_id))
                continue
            refusal = self._refusal(item, doc, tenant, principal, agent_id)
            out[item_id] = _read(item, doc, refusal, cycle=doc.id in back)
        return out

    @staticmethod
    def _visible(item: HubItem, tenant: str, principal: Principal | None) -> bool:
        """그 테넌트의 `agent` 항목이고 저장하는 사람이 볼 수 있는가. 아니면 없는 항목과 같이
        답해 존재를 드러내지 않는다."""
        if item.kind != AGENT_KIND or item.tenant != tenant:
            return False
        return principal is None or can(principal, tenant, item, "view")

    def _refusal(
        self,
        item: HubItem,
        doc: Agent,
        tenant: str,
        principal: Principal | None,
        agent_id: str | None,
    ) -> Refusal | None:
        if principal is not None and not can(principal, tenant, item, "consume"):
            return Refusal(SOURCE_NO_CONSUME, f"hub item {item.name!r} needs the consume right")
        if doc.id == agent_id:
            return Refusal(SOURCE_SELF, "an agent cannot use itself as a tool")
        if not serves_turns(doc) or doc.deployed is None:
            return Refusal(
                SOURCE_NOT_DEPLOYED, f"agent {doc.name!r} is {doc.state}, not serving turns"
            )
        refusal = self.publishing.call_refusal(doc)
        if refusal is not None:
            return Refusal(refusal.code, f"agent {doc.name!r}: {refusal.detail}")
        return None

    async def _calling_back(self, agent_id: str | None, called: Iterable[Agent]) -> set[str]:
        """called 가운데 배포된 리비전이 제 Agent 소스를 따라 agent_id 로 돌아오는 Agent 의 id 다.
        agent_id 가 게시돼 있지 않으면 아무도 그것을 부르지 못하므로 따라가지 않는다."""
        if agent_id is None or await self.hub.store.get_by_agent(agent_id) is None:
            return set()
        return {doc.id for doc in called if await self._reaches(doc, agent_id)}

    async def _reaches(self, start: Agent, target: str) -> bool:
        """start 의 배포된 리비전에서 Agent 소스를 따라가면 target 에 닿는가. 단계마다 항목과 Agent
        를 일괄로 읽고 한 번 본 Agent 는 다시 보지 않는다."""
        seen = {start.id}
        frontier = [start]
        while frontier:
            item_ids = {
                item_id
                for doc in frontier
                if doc.deployed is not None
                for item_id in agent_source_items(doc.deployed)
            }
            items = await self.hub.store.get_many_unscoped(sorted(item_ids))
            ids = {
                i.source.agent_id
                for i in items.values()
                if i.kind == AGENT_KIND and i.source.agent_id
            }
            if target in ids:
                return True
            frontier = list((await self.agents.get_many(ids - seen)).values())
            seen |= ids
        return False

    async def status_warnings(self, doc: Agent) -> list[str]:
        """배포된 Agent 의 상태 경고다(`status_warnings`). Agent 소스가 없으면 읽지 않는다."""
        rev = doc.deployed
        if rev is None or not agent_source_items(rev):
            return []
        sources = await self.load(doc.tenant, rev)
        return status_warnings(rev, sources, enabled=self.enabled)


def _read(item: HubItem, doc: Agent, refusal: Refusal | None, *, cycle: bool) -> AgentSource:
    """항목과 호출받는 Agent 에서 읽은 소스다. 배포된 리비전이 없으면 그 리비전의 칸은 빈다."""
    rev = doc.deployed
    a = analyze_structure(rev) if rev is not None else None
    turn_timeout = _turn_timeout(a) if a is not None else None
    model = a.model_of(a.main) if a is not None and a.main is not None else None
    return AgentSource(
        item.id,
        refusal=refusal,
        tool=tool_name(item.name),
        description=item.description or "",
        agent_id=doc.id,
        agent_name=doc.name,
        state=doc.state,
        timeout_seconds=(turn_timeout or 0) + CALL_GRACE_SECONDS,
        provider=model.provider if model is not None else "",
        record_bodies=rev.service.observability.record_bodies if rev is not None else False,
        cycle=cycle,
        takes_tasks=doc.image_reports(AGENT_TASKS),
    )
