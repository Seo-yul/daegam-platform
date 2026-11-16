"""Tool 소스가 내는 이름이 내장 Tool 과 같을 때의 캔버스 경고 `builtin_tool_name` 이다
(`services/tool_pins.py` `builtin_name_issues`).

런타임은 그런 Tool 을 빼고 Agent 가 돌 때에야 경고한다. 플랫폼이 내장 이름을 따로 알아야 저장과
배포 앞에서 경고할 수 있으므로, 런타임 `tool_names.py` 와 플랫폼 `toolnames.py` 가 같은 이름을
내는지 여기서 맞댄다. 런타임 쪽은 `compile.builtin_names` 가 같은 결과를 내는지
agent-runtime 테스트가 맞댄다.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from daegam.services.tool_effects import EFFECT_READ, SourceTools
from daegam.services.tool_pins import BUILTIN_TOOL_NAME, builtin_name_issues
from daegam.services.toolnames import ALWAYS_BUILT_IN, CODEX_MODELS, builtin_tool_names
from daegam.store.agents import AgentSpec
from daegam.store.settings import LLMProvider
from tests.helpers.agents import make_ctx, node_of, template_spec
from tests.helpers.source import AGENT_RUNTIME, runtime_module

GW = "gi-calc-0001"
NODE = "tools-calculator"
FLAGS = list(itertools.product([False, True], repeat=3))


def test_builtin_names_match_the_runtime_list() -> None:
    """조건 셋의 모든 조합에서 플랫폼의 내장 이름이 런타임 tool_names.py 와 같다. 런타임이 내장
    이름을 더하거나 Codex 이름이 바뀌면 여기서 깨진다."""
    runtime = runtime_module(AGENT_RUNTIME / "tool_names.py")
    assert CODEX_MODELS == runtime.CODEX_MODELS
    # 기억할 수 없는 늘 내장된 이름이다(approval_choices)
    assert ALWAYS_BUILT_IN == runtime.ALWAYS_BUILTIN_TOOLS
    for subagents, structured, todos in FLAGS:
        assert builtin_tool_names(
            subagents=subagents, structured=structured, todos=todos
        ) == runtime.builtin_tools_for(subagents=subagents, structured=structured, todos=todos)
    assert builtin_tool_names(subagents=True, structured=True, todos=True) == runtime.BUILTIN_TOOLS


def _spec(
    *, sub: bool = False, response_format: bool = False, model: str | None = None, **tools: Any
) -> AgentSpec:
    spec = template_spec()
    node_of(spec, NODE)["data"].update({"allow_tools": [], **tools})
    node_of(spec, "model-main")["data"]["model"] = model
    nodes, edges = spec["graph"]["nodes"], spec["graph"]["edges"]
    if response_format:
        node_of(spec, "agent-main")["data"]["response_format"] = {"json_schema": {"type": "object"}}
    if sub:
        nodes.append(
            {"id": "agent-sub", "type": "agent", "data": {"role": "sub", "name": "helper"}}
        )
        for kind, target in (("model", "model-main"), ("tools", NODE)):
            edges.append(
                {"id": f"e-sub-{kind}", "source": "agent-sub", "target": target, "kind": kind}
            )
        edges.append(
            {"id": "e-delegate", "source": "agent-main", "target": "agent-sub", "kind": "delegate"}
        )
    return AgentSpec.model_validate(spec)


def _issues(spec: AgentSpec, names: tuple[str, ...], provider: str = "local-stub", **ctx_kw: Any):
    ctx = make_ctx()
    ctx.source_tools = {GW: SourceTools(names=names, effects={n: EFFECT_READ for n in names})}
    ctx.providers["codex-provider"] = LLMProvider(name="codex-provider", kind="openai")
    for node in spec.graph.nodes:
        if node.id == "model-main":
            node.data["provider"] = provider
    for key, value in ctx_kw.items():
        setattr(ctx, key, value)
    return builtin_name_issues(spec, ctx)


def test_file_tools_and_execute_clash_for_every_agent() -> None:
    [issue] = _issues(_spec(), ("Add", "write_file", "execute"))
    assert (issue.code, issue.level, issue.node_id) == (BUILTIN_TOOL_NAME, "warning", NODE)
    assert "Agent: execute, write_file" in issue.message
    assert issue.data == {"clashes": [{"agent": "Agent", "names": ["execute", "write_file"]}]}
    assert _issues(_spec(), ("Add", "Divide")) == []


@pytest.mark.parametrize(
    ("name", "kwargs", "clashes"),
    [
        ("task", {"sub": True}, True),
        ("task", {}, False),
        ("structured_response", {"response_format": True}, True),
        ("structured_response", {}, False),
    ],
)
def test_task_and_structured_response_depend_on_the_graph(name, kwargs, clashes) -> None:
    assert bool(_issues(_spec(**kwargs), ("Add", name))) is clashes


def test_a_sub_agent_has_only_the_always_built_ins() -> None:
    """메인은 task 를 받지만 하위 Agent 는 받지 않는다. 같은 노드를 둘이 붙이면 이름은 에이전트마다
    따로 적힌다."""
    [issue] = _issues(_spec(sub=True), ("Add", "task", "ls"))
    assert issue.data == {
        "clashes": [
            {"agent": "Agent", "names": ["ls", "task"]},
            {"agent": "helper", "names": ["ls"]},
        ]
    }


@pytest.mark.parametrize(
    ("provider", "model", "clashes"),
    [
        ("codex-provider", "gpt-5.2-codex", True),
        ("codex-provider", "gpt-4o", False),
        ("local-anthropic", "gpt-5.2-codex", False),
        ("missing", "gpt-5.2-codex", False),
    ],
)
def test_write_todos_clashes_for_codex_models_only(provider, model, clashes) -> None:
    assert bool(_issues(_spec(model=model), ("write_todos",), provider)) is clashes


def test_the_selection_decides_the_names_of_a_node() -> None:
    """고른 Tool 이 있으면 그 이름만 센다. 없으면 소스가 지금 내는 이름이다."""
    assert len(_issues(_spec(allow_tools=["grep"]), ("Add", "grep"))) == 1
    assert _issues(_spec(allow_tools=["Add"]), ("Add", "grep")) == []


def test_unknown_names_do_not_warn() -> None:
    ctx = make_ctx()
    assert builtin_name_issues(_spec(), ctx) == []
