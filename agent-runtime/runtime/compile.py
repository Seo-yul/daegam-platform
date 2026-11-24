"""agent.json 을 deepagents 그래프로 컴파일한다(D26).

- 모델은 provider kind 별 BaseChatModel 객체다(models.py). 문자열 모델은 쓰지 않는다.
- deepagents 가 자동으로 붙이는 것은 HarnessProfile 로 다룬다. general-purpose 서브에이전트는
  항상 끄고, 파일시스템 도구 7종은 builtin_tools.filesystem 이 false 면 숨긴다.
- SummarizationMiddleware 는 같은 이름으로 넘겨 압축 정책(trigger·keep)만 바꾼 채 제자리 교체한다.
  요약 입력은 자르지 않는다(trim_tokens_to_summarize=None). 생성자 기본 4000 은 요약 대상의 끝
  4000 토큰만 요약해 앞 요약과 앞 구간을 버린다. 비용은 발동 기준으로 다룬다(P35).
- Tool 호출 한도(tool_limit.py 의 TurnToolLimit, 실행마다 센다)와 턴 타임아웃(asyncio.timeout,
  stream.py)은 항상 둔다.
- 서브에이전트는 SubAgent{name, description, system_prompt, tools, model, interrupt_on} 이고
  mode 는 isolated 다. 서브의 limits.turn_timeout_seconds 는 메인의 SubagentTimeout
  (resilience.py)이 task 호출에 건다.
- 메인에는 가드레일(guards.py), 세션 예산(budget.py), 폴백·재시도(resilience.py) 미들웨어를 더
  건다. wrap_model_call 은 목록 앞이 바깥이라 폴백이 재시도를 감싼다. 서브에이전트는 자기
  limits.retries 의 재시도만 건다.
- 도구는 MCP 도구(tools[] ref)와 knowledge 검색 도구(knowledge[] ref, knowledge.py)를 이어 붙인
  것이다(D32). 기능 agent_tools 의 Agent 소스는 런타임이 만든 Tool 하나를 제 ref 에 둔 소스다
  (agent_calls.py). 작업을 맡기는 Agent 소스들은 메인에서 Tool 다섯 한 벌을 같이 쓰고 맡긴 작업은
  그래프 상태에 남는다(agent_task_tools.py). 승인 규칙, 바깥 표시, 출처 표가 MCP 소스와 같은 길로
  다룬다. 병합 결과에 같은 이름이 둘이면 tool_name_conflict 로 막는다. ToolNode 는 같은 이름을
  조용히 덮기 때문이다. 소스가 그 에이전트의 내장 도구와 같은 이름의 도구를 주면 그 도구를 빼고
  경고(builtin_tool_name)한다. 내장 이름은 deepagents 가 그 모델로 쌓는 것에서 읽는다. 소스의
  목록은 배포 뒤에도 바뀌어서 막지 않는다.
- 파일 백엔드는 CompositeBackend 이고 기본은 세션 안 StateBackend 다. `/uploads/` 는 첨부 글을
  읽기 전용으로 보이는 라우트이고(uploads.py) 쓰기 막기 규칙(permissions.py)이 메인과
  서브에이전트의 파일 도구에 걸린다. 메인 모델 호출에는 첨부 이미지를 채우는 미들웨어가 우리
  목록의 끝에 선다.
- 장기 기억(D34)이 켜지면 /memories/ 라우트가 더해져 그 경로만 영구 저장소로 가고, 시스템
  프롬프트에 그 경로가 무엇인지 알리는 고정 문구가 붙는다. 임시 턴(테스트 채팅)용으로 같은
  그래프를 /memories/ 라우트 없이 한 벌 더 만들어 두고 stream.py 가 턴마다 고른다.
- Skill(D64)이 붙으면 init 이 agent.json 곁에 받아 둔 디렉터리가 읽기 전용 `/skills/` 라우트로
  더해지고, 붙인 에이전트마다 우리 문구의 SkillsMiddleware 가 같은 이름으로 기본 것을 바꾼다. 점검과
  조각은 skills.py 의 `load_skills`·`AgentSkills.parts` 가 만든다. 쓰기 막기 규칙에도 `/skills/**`
  가 더해진다. 메인에 붙었으면 턴마다 목록을 다시 읽는다(`reloads_skills`).
- 기능 web_guard(D64)가 든 문서면 메인과 웹 Tool 을 가진 서브에이전트마다 웹 가드가 ToolGuard
  바깥에, 인용 단계가 그 안쪽에 선다(web_guard.py). 서브에이전트는 isolated 라 메인 미들웨어를
  받지 않으므로 따로 건다. 메인은 웹 Tool 이 없어도 사람의 URL 을 모으려고 가드를 둔다.
- 기능 tool_safety(P43)가 든 문서면 메인과 서브에이전트마다 거절 층이 가장 바깥, 바깥 표시 층이
  가장 안쪽 Tool 층에 서고, 판정 층이 폴백·재시도 바깥의 모델 호출 층에 선다. 승인 항목은 모든 Tool
  에 두고 시스템 프롬프트에 바깥 글의 문구를 더한다(tool_safety.py). 기능이 없으면 그래프가 예전과
  같다.
- 기능 injection_check(P43 7절)가 든 문서면 메인과 서브에이전트마다 판정 모델 층이 웹 가드 안쪽,
  ToolGuard 바깥에 선다(judge.py). 판정 모델 하나를 모두가 같이 쓴다. 기능이 없으면 그래프가
  예전과 같다.
- 기능 code_execution(P40, D95)이 든 문서면 파일 백엔드의 기본이 세션마다의 샌드박스
  (sandbox.py 의 `SandboxBackend`)이고 execute Tool 이 보인다. 압축이 흘려보내는 파일은
  `ARTIFACTS_ROOT` 아래 세션 state 라우트로 가서 압축이 파드를 열지 않는다. 메인과
  서브에이전트마다 작업 공간 문구가 프롬프트에 붙고, `code_execution.approval` 이 있으면 모두의
  `interrupt_on["execute"]` 가 된다. execute 의 설명은 HarnessProfile 의 설명 바꾸기로 시한을
  말한다. 백엔드 하나가 두 그래프를 섬긴다. 기능이 없으면 그래프가 예전과 같다.
- 문맥 스키마는 늘 `TurnContext` 다. 장기 기억의 주체와 함께 검색 도구가 쓰는 인용
  장부(citations.py)와 턴의 첨부(uploads.py)가 실리므로 장기 기억과 상관없이 턴마다 문맥을
  넘긴다.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import BackendProtocol, CompositeBackend, StateBackend
from deepagents.middleware import FilesystemMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware as DeepSummarization
from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from runtime.agent_calls import AgentCalls
from runtime.agent_task_tools import AsyncTaskState, task_tools_by_ref
from runtime.budget import SessionBudget
from runtime.config import AgentConfig, ConfigError
from runtime.guards import (
    Guardrails,
    InputGuard,
    OutputFilter,
    Rule,
    ToolGuard,
    build_guardrails,
    parse_groups,
)
from runtime.judge import judge_layer, judge_middleware
from runtime.knowledge import KnowledgeTools
from runtime.long_term import MEMORY_ROUTE, LongTermMemory, TurnContext
from runtime.models import build_chat_model, correlation_warnings, harness_key
from runtime.permissions import build_permissions
from runtime.resilience import ModelFallback, ModelRetry, SubagentTimeout
from runtime.sandbox import EXECUTE_DESCRIPTION, PROMPT_LINE, SandboxBackend, Sandboxes
from runtime.skills import NO_PARTS, NO_SKILLS, AgentSkills, SkillParts, load_skills
from runtime.stub import fail_first_from_env
from runtime.tool_limit import TurnToolLimit
from runtime.tool_names import (
    EXECUTE_TOOL,
    FILESYSTEM_TOOLS,
    STRUCTURED_RESPONSE_TOOL,
    SUBAGENT_TOOL,
)
from runtime.tool_safety import (
    NO_SAFETY,
    AgentSafety,
    SafetyLayers,
    ToolSafety,
    safety_layers,
    tool_safety_plan,
)
from runtime.trace import ROLE_FALLBACK, ROLE_SUBAGENT
from runtime.uploads import UPLOADS_ROUTE, AttachmentImages, UploadsBackend
from runtime.web_guard import NO_LAYERS, WebLayers, web_plan

log = logging.getLogger("daegam.agent-runtime.compile")

TOOL_EXCLUSION_NAME = "_ToolExclusionMiddleware"
"""deepagents 가 HarnessProfile 의 excluded_tools 를 적용할 때 스택에 넣는 미들웨어 이름이다."""

SUMMARIZATION_NAME = "SummarizationMiddleware"
"""deepagents 기본 요약 미들웨어의 .name 이다. 같은 이름을 넘기면 제자리 교체된다."""

BUILTIN_TOOL_NAME = "builtin_tool_name"
"""소스의 도구가 내장 도구와 이름이 겹쳐 뺐다는 상태 경고다."""

LANGUAGE_INSTRUCTION = "\n\nAlways respond in the language whose code is {language!r}."

MEMORY_INSTRUCTION = (
    f"\n\nFiles under {MEMORY_ROUTE} are your long-term memory. They outlive this conversation "
    "and only the current user can see them. Write there what you must still know in a later "
    "conversation, and look there first when the user refers to something from before. Every "
    "other path is scratch space for this conversation only and is gone when it ends."
)
"""장기 기억이 켜졌을 때 시스템 프롬프트에 더하는 고정 문구다(D34).

사용자가 프로필 프롬프트에 직접 쓰게 하지 않는다. 언어 지시보다 앞에 붙인다(언어 지시가
마지막이어야 한다). 서브에이전트도 같은 문구를 받는다. 격리 서브에이전트가 부모와 같은
네임스페이스에 쓸 수 있어서 무엇인지 모르는 채 쓰게 두지 않는다.
회수는 여기까지다. 실제로 읽을지는 모델 재량이다(기계로 보장하지 않는다).
"""


@dataclass
class CompiledAgent:
    """컴파일 결과와 턴 실행에 필요한 부속이다."""

    graph: CompiledStateGraph
    model: BaseChatModel
    tools: list[BaseTool]
    interrupt_on: dict[str, InterruptOnConfig]
    turn_timeout: float
    harness_key: str
    subagent_names: list[str] = field(default_factory=list)
    input_guard: InputGuard = field(default_factory=lambda: InputGuard([]))
    tool_guard: ToolGuard = field(default_factory=lambda: ToolGuard([]))
    """도구 가드레일이다. 그래프 안에서는 미들웨어로 돌고 결정의 고친 인자에는 hitl.py 가 댄다."""
    output_filter: OutputFilter | None = None
    """글을 바꾸는 출력 가드레일이 있을 때의 필터다. 있으면 스트림이 글을 메시지 단위로, 필터를
    지난 뒤 낸다(stream.py)."""
    fallback_model: BaseChatModel | None = None
    session_budget: int | None = None
    ephemeral_graph: CompiledStateGraph | None = None
    """임시 턴용 그래프다. 장기 기억이 켜졌을 때만 있고 /memories/ 가 세션 안 state 로 간다."""
    reloads_skills: bool = False
    """메인에 Skill 이 붙었는가. 그러면 스트림이 턴마다 입력에 `skills_metadata: None` 을 싣는다."""
    tool_sources: dict[str, dict[str, str]] = field(default_factory=dict)
    """도구 이름마다의 출처 {kind, id, name} 다. tool_exec 스팬이 싣는다(trace.py)."""
    body_masks: tuple[Rule, ...] = ()
    """관측 이벤트의 본문에 대는 pii_mask 규칙이다(`Guardrails.masks`)."""
    warnings: list[str] = field(default_factory=list)
    """컴파일이 찾은 상태 경고다(`correlation_hook_missing`, `builtin_tool_name`). healthz,
    runtime_start, /metrics 가 싣는다."""
    safety: AgentSafety | None = None
    """기능 tool_safety 가 든 문서의 메인 재료다. 턴을 열 때 기록을 훑는다(`history_outside`)."""
    gateway: bool = False
    """모델 호출이 LLM 게이트웨이 경로인가(P38 6절). 메인, 폴백, 서브 모델 가운데 하나라도 provider
    가 `gateway: true` 면 참이다. 턴 오류가 게이트웨이의 코드를 가를 때(stream.py)와 재시도가
    게이트웨이의 표시를 볼 때(resilience.py) 읽는다."""

    def history_outside(self, messages: list[BaseMessage]) -> bool:
        """씨앗의 메시지가 바깥 내용을 가졌는가. 기능이 없으면 아무도 읽지 않으므로 거짓이다."""
        return self.safety is not None and self.safety.history_holds_outside(messages)

    def graph_for(self, *, ephemeral: bool) -> CompiledStateGraph:
        """이 턴이 쓸 그래프다. 임시 턴은 영구 저장소를 안 붙인 쪽으로 돈다."""
        if ephemeral and self.ephemeral_graph is not None:
            return self.ephemeral_graph
        return self.graph


def build_interrupt_on(approvals: dict[str, list[str]]) -> dict[str, InterruptOnConfig]:
    return {name: {"allowed_decisions": list(decisions)} for name, decisions in approvals.items()}


def _interrupt_on(
    safety: SafetyLayers, rules: dict[str, list[str]]
) -> dict[str, InterruptOnConfig]:
    """에이전트 하나의 승인 항목이다. 기능 tool_safety 가 있으면 모든 Tool 의
    항목이고(tool_safety.py) 없으면 정적 규칙 rules 의 항목이다."""
    if safety.interrupt_on is not None:
        return safety.interrupt_on
    return build_interrupt_on(rules)


ARTIFACTS_ROOT = "/.internal"
"""코드 실행 Agent 의 압축이 흘려보내는 파일(`large_tool_results`, `conversation_history`)의 뿌리다.
이 아래는 세션 state 라우트라 압축이 샌드박스를 열지 않고, state 에는 뿌리를 뗀 경로로 남아 세션
파일이 예전처럼 뺀다(P40 1절)."""


def build_backend(
    long_term: LongTermMemory | None,
    skills: AgentSkills = NO_SKILLS,
    sandbox: SandboxBackend | None = None,
) -> BackendProtocol:
    """파일 백엔드다. 기본은 세션 안 state 이고 `/uploads/` 는 첨부, `/memories/` 는 장기 기억이
    켜졌을 때만의 영구 저장소, `/skills/` 는 Skill 이 붙었을 때만의 읽기 전용 디렉터리다. 압축이
    흘려보내는 파일은 기본 백엔드로 간다. sandbox 가 있으면(코드 실행) 기본이 샌드박스이고 압축의
    파일은 `ARTIFACTS_ROOT` 아래 state 라우트로 간다."""
    routes: dict[str, BackendProtocol] = {UPLOADS_ROUTE: UploadsBackend(), **skills.routes()}
    if long_term is not None:
        routes[MEMORY_ROUTE] = long_term.route()
    if sandbox is None:
        return CompositeBackend(default=StateBackend(), routes=routes)
    routes[f"{ARTIFACTS_ROOT}/"] = StateBackend()
    return CompositeBackend(default=sandbox, routes=routes, artifacts_root=ARTIFACTS_ROOT)


def build_compaction(
    model: BaseChatModel, backend: BackendProtocol, compaction: dict[str, Any]
) -> AgentMiddleware:
    """압축 정책을 deepagents 요약 미들웨어에 그대로 옮긴다. 이름이 같아 기본 것을 교체한다.

    요약 입력은 자르지 않는다(모듈 머리). 요약 한 번의 모델 호출이 창이 큰 모델에서 턴 제한을 넘길
    수 있고, 그러면 요약이 상태에 남지 않아 다음 턴이 같은 요약을 처음부터 다시 한다. 캔버스의 압축
    안내가 그것을 알린다.
    """
    trigger = (compaction["trigger"]["kind"], compaction["trigger"]["value"])
    keep = (compaction["keep"]["kind"], compaction["keep"]["value"])
    return DeepSummarization(
        model=model, backend=backend, trigger=trigger, keep=keep, trim_tokens_to_summarize=None
    )


def build_tool_limit(limits: dict[str, Any], *, subagent: bool = False) -> AgentMiddleware:
    # 처음 넘으면 모델이 가진 결과로 답하게 하고, 그 뒤에도 부르면 턴을 닫는다(tool_limit.py).
    # 서브에이전트는 닫을 때 메인 모델이 읽을 task 결과를 남긴다
    return TurnToolLimit(limits["max_tool_calls_per_turn"], subagent=subagent)


def register_profile(model: BaseChatModel, *, filesystem: bool, executes: bool = False) -> str:
    """이 모델 키에 HarnessProfile 을 건다. general-purpose 는 끄고 파일 도구는 필요할 때만 보인다.
    executes 면(코드 실행) execute 의 설명을 우리 것으로 바꾼다. execute 는 실행 백엔드가 있을 때만
    보이므로 같은 키의 다른 그래프에는 해가 없다.

    등록은 누적(합집합)이라 한 프로세스 안에서 같은 키에 filesystem 을 껐다 켤 수는 없다.
    런타임 파드는 agent.json 하나만 섬기므로 문제가 없다.
    """
    key = harness_key(model)
    excluded = frozenset() if filesystem else frozenset(FILESYSTEM_TOOLS)
    register_harness_profile(
        key,
        HarnessProfile(
            excluded_tools=excluded,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            tool_description_overrides={EXECUTE_TOOL: EXECUTE_DESCRIPTION} if executes else {},
        ),
    )
    return key


def _reachable_middleware(obj: Any, seen: set[int]) -> Iterator[AgentMiddleware]:
    """클로저·데코레이터 래핑(__wrapped__)·바운드 메서드(__self__)를 따라가 찾은 미들웨어
    인스턴스다.

    langchain create_agent 는 wrap_model_call·wrap_tool_call 훅을 노드로 두지 않고 model·tools 노드
    함수 안에 합성하므로, 컴파일 결과에서 그 미들웨어를 보려면 클로저를 따라가는 수밖에 없다.
    미들웨어 인스턴스를 찾으면 그 안으로는 들어가지 않는다.
    """
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, AgentMiddleware):
        yield obj
        return
    if isinstance(obj, list | tuple):
        for item in obj:
            yield from _reachable_middleware(item, seen)
        return
    for inner in (getattr(obj, "__wrapped__", None), getattr(obj, "__self__", None)):
        if inner is not None:
            yield from _reachable_middleware(inner, seen)
    for cell in getattr(obj, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        yield from _reachable_middleware(value, seen)


def wrapped_middleware_names(graph: CompiledStateGraph) -> set[str]:
    """컴파일된 그래프의 model·tools 노드에 합성된 wrap 훅 미들웨어의 클래스 이름이다."""
    roots = [
        getattr(node.bound, attr, None)
        for name, attr in (("model", "func"), ("tools", "_wrap_tool_call"))
        if (node := graph.nodes.get(name)) is not None
    ]
    return {type(m).__name__ for root in roots for m in _reachable_middleware(root, set())}


def _check_filesystem_hidden(graph: CompiledStateGraph, key: str) -> None:
    """HarnessProfile 이 실제로 걸렸는지 기동 시 확인한다.

    프로필 키는 deepagents 가 pre-built 모델에서 유도하는 값(models.harness_key)과 글자 그대로
    같아야 걸린다. 상류가 키 유도를 바꾸면 조용히 어긋나고 파일 도구 7종이 모델에 그대로 보이므로,
    그 결과인 _ToolExclusionMiddleware 가 스택에 없으면 기동을 막는다.
    """
    if TOOL_EXCLUSION_NAME not in wrapped_middleware_names(graph):
        raise ConfigError(
            "harness_profile_not_applied",
            f"harness profile did not apply (key {key!r}); filesystem tools would be exposed",
        )


@functools.cache
def _filesystem_tool_names() -> frozenset[str]:
    """FilesystemMiddleware 가 붙이는 도구 이름이다(파일 도구 7종과 execute). 백엔드와 상관없이
    같아서 한 번만 만들어 본다."""
    return frozenset(tool.name for tool in FilesystemMiddleware(backend=StateBackend()).tools)


def builtin_names(model: BaseChatModel, *, subagents: bool, structured: bool) -> frozenset[str]:
    """한 에이전트에 deepagents·langchain 이 붙이는 도구 이름이다. create_deep_agent 가 그 모델로
    쌓는 것을 따른다. FilesystemMiddleware 의 도구, 모델의 harness profile 이 더하는 미들웨어의
    도구(Codex 모델의 write_todos), 서브에이전트가 있을 때 task, 응답 형식이 있을 때 구조화 출력
    도구다. 프로필은 register_profile 뒤에 읽어야 우리 등록과 합친 것이다.

    파일 도구를 끄거나 실행 backend 가 없으면 내장 도구는 이름으로 모델에게서 숨는다. 그때 같은
    이름의 소스 도구도 함께 숨으므로 빼도 모델이 보는 것은 같다."""
    profile = _harness_profile_for_model(model, None)
    names = set(_filesystem_tool_names())
    names.update(
        tool.name
        for middleware in profile.materialize_extra_middleware()
        for tool in getattr(middleware, "tools", ())
    )
    if subagents:
        names.add(SUBAGENT_TOOL)
    if structured:
        names.add(STRUCTURED_RESPONSE_TOOL)
    return frozenset(names)


def _tools_for(
    refs: list[str], tools_by_ref: dict[str, list[BaseTool]], builtins: frozenset[str]
) -> tuple[list[BaseTool], list[tuple[str, str]]]:
    """refs 소스들의 도구와, 내장 이름이라 뺀 (ref, 도구 이름)이다.

    langchain 은 내장 도구 뒤에 우리 도구를 잇고 ToolNode 는 같은 이름이면 뒤의 것을 남긴다. 그대로
    두면 소스의 도구가 내장 도구를 대신해 세션 파일, Skill 읽기, 장기 기억, 서브에이전트 호출이 그
    소스로 간다. 구조화 출력 도구 이름은 모델 노드가 ToolNode 보다 먼저 가로채고, 같은 이름의 도구
    정의 둘을 provider 가 거절한다. 그래서 내장 도구를 지키고 소스의 것을 뺀다."""
    kept: list[BaseTool] = []
    dropped: list[tuple[str, str]] = []
    for ref in refs:
        for tool in tools_by_ref.get(ref, []):
            if tool.name in builtins:
                dropped.append((ref, tool.name))
            else:
                kept.append(tool)
    return kept, dropped


def builtin_name_warnings(
    config: AgentConfig, dropped: dict[str, list[tuple[str, str]]]
) -> list[str]:
    """뺀 도구가 있으면 경고 한 줄이다. 에이전트(main, subagents[이름])마다 도구 이름과 소스 이름을
    싣고, 같은 줄을 로그에도 남긴다."""
    parts = [
        f"{where}: "
        + ", ".join(
            f"{name} from {config.tool_source(ref)['source']['name']}" for ref, name in pairs
        )
        for where, pairs in dropped.items()
        if pairs
    ]
    if not parts:
        return []
    warning = (
        f"{BUILTIN_TOOL_NAME}: tool sources offer tools named like built-in tools "
        f"({'; '.join(parts)}). The Agent keeps its built-in tools and does not offer these. "
        "Rename them at their source, or connect that source through a Gateway in prefix mode."
    )
    log.warning("%s", warning)
    return [warning]


def _merge_tools(
    where: str,
    mcp_tools: list[BaseTool],
    knowledge: KnowledgeTools | None,
    knowledge_refs: list[str],
) -> list[BaseTool]:
    """MCP 도구 뒤에 검색 도구를 잇는다. 검색 도구 이름이 MCP 툴이나 다른 검색 도구와 겹치면
    ConfigError(tool_name_conflict) 다. MCP 소스끼리 같은 이름은 전과 같이 둔다(같은 Backend 로 만든
    두 인스턴스가 같은 툴을 주는 그래프가 이미 돌고 있고, approvals_for 도 그것을 합집합으로 본다).

    knowledge 항목을 가리키는데 KnowledgeTools 가 없으면 있어야 할 도구가 없는 채 도는 것이므로
    그것도 막는다.
    """
    if knowledge_refs and knowledge is None:
        raise ConfigError(
            "knowledge_missing",
            f"{where} 가 knowledge 를 가리키는데 검색 Tool 이 만들어지지 않았다",
        )
    merged = list(mcp_tools)
    search_tools = knowledge.tools_for(knowledge_refs) if knowledge is not None else []
    seen: set[str] = {tool.name for tool in mcp_tools}
    for tool in search_tools:
        if tool.name in seen:
            raise ConfigError(
                "tool_name_conflict",
                f"{where} 의 검색 Tool 이름 {tool.name!r} 가 MCP Tool 이나 "
                "다른 검색 Tool 과 겹친다",
            )
        merged.append(tool)
        seen.add(tool.name)
    return merged


def _with_agent_calls(
    config: AgentConfig,
    tools_by_ref: dict[str, list[BaseTool]],
    agent_calls: AgentCalls | None,
) -> dict[str, list[BaseTool]]:
    """읽어 둔 MCP 도구에 Agent 소스의 Tool 을 제 ref 로 더한 것이다. Agent 소스가 있는데
    AgentCalls 가 없으면 있어야 할 도구가 없는 채 도는 것이므로 ConfigError(agent_calls_missing)
    다. 작업을 맡기는 소스의 Tool 다섯은 메인만 쓴다. 작업이 메인 세션의 상태에 남기 때문이다.
    서브에이전트가 그런 소스를 가리키면 ConfigError(agent_task_not_main) 다."""
    if not config.agent_sources:
        return tools_by_ref
    if agent_calls is None:
        raise ConfigError(
            "agent_calls_missing", "tools[] 에 Agent 소스가 있는데 그 Tool 이 만들어지지 않았다"
        )
    task_refs = {source["ref"] for source in config.task_sources}
    for spec in config.subagents:
        if task_refs.intersection(spec["tools"]):
            raise ConfigError(
                "agent_task_not_main",
                f"subagents[{spec['name']}] 가 작업을 맡기는 Agent 소스를 가리킨다. 메인만 쓴다",
            )
    return {**tools_by_ref, **agent_calls.tools_by_ref, **task_tools_by_ref(config, agent_calls)}


def tool_source_table(
    config: AgentConfig,
    tools_by_ref: dict[str, list[BaseTool]],
    dropped: frozenset[str],
) -> dict[str, dict[str, str]]:
    """도구 이름마다의 출처다. MCP 도구와 Agent 소스의 Tool 은 그 소스의 `tools[].source`, 검색
    도구는 그 Knowledge Base 다. 내장 도구와 task 는 표에 없고 트레이서가 builtin·subagent 로 본다.
    내장 이름이라 뺀
    도구(dropped)도 그래서 넣지 않는다. 소스끼리 같은 이름은 뒤의 것이다. ToolNode 가 같은 이름을
    뒤의 것으로 덮기 때문이다."""
    table: dict[str, dict[str, str]] = {}
    for source in config.tools:
        for tool in tools_by_ref.get(source["ref"], []):
            if tool.name not in dropped:
                table[tool.name] = dict(source["source"])
    for item in config.knowledge:
        base = item["base"]
        table[item["tool"]["name"]] = {
            "kind": "knowledge",
            "id": str(base["id"]),
            "name": str(base["name"]),
        }
    return table


def _system_prompt(
    main: dict[str, Any],
    *,
    long_term: bool,
    safety: SafetyLayers = NO_SAFETY,
    executes: bool = False,
) -> str:
    """메인의 시스템 프롬프트다. 장기 기억 문구, 작업 공간 문구, 바깥 글의 문구 다음에 언어 지시가
    온다(언어 지시가 마지막이다)."""
    prompt = main["system_prompt"]
    if long_term:
        prompt += MEMORY_INSTRUCTION
    if executes:
        prompt += PROMPT_LINE
    prompt += safety.instruction
    if main["language"]:
        prompt += LANGUAGE_INSTRUCTION.format(language=main["language"])
    return prompt


def _response_format(main: dict[str, Any]) -> dict[str, Any] | None:
    """응답 형식이 JSON 스키마면 그 스키마다. `title` 은 늘 STRUCTURED_RESPONSE_TOOL 로 덮어쓴다.

    모델이 구조화 출력을 모르면 langchain 이 `title` 을 이름으로 하는 도구를 만들어 도구 전략으로
    돌고(없으면 무작위 이름이다), 번역은 그 이름의 tool_call·tool_result 를 버린다. 이름을 못 박아야
    사용자가 적은 `title` 이 실제 도구와 겹쳐 그 도구의 이벤트까지 버리는 일이 없다.
    """
    fmt = main["response_format"]
    if fmt["kind"] == "json_schema":
        return {**fmt["schema"], "title": STRUCTURED_RESPONSE_TOOL}
    return None


def _subagent(
    spec: dict[str, Any],
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    backend: BackendProtocol,
    compaction: dict[str, Any],
    long_term: bool,
    skills: SkillParts = NO_PARTS,
    web: WebLayers = NO_LAYERS,
    safety: SafetyLayers = NO_SAFETY,
    judge: tuple[AgentMiddleware, ...] = (),
    execute_rule: dict[str, list[str]] | None = None,
    gateway: bool = False,
) -> SubAgent:
    """서브에이전트 하나다. 백엔드는 메인과 같은 것을 쓴다(파일도 같은 자리에 남는다). skills 는
    이 서브에이전트에 거는 Skill 조각이고(skills.py) web 은 웹 가드와 인용 단계(web_guard.py),
    safety 는 바깥 표시와 판정의 층이다(tool_safety.py). judge 는 판정 모델 층이다(judge.py).
    execute_rule 이 있으면(코드 실행) 작업 공간 문구를 붙이고 그 승인 규칙을 이 서브에이전트의
    규칙에 더한다. gateway 는 재시도가 게이트웨이의 표시를 볼지다(P38 7절)."""
    prompt = spec["system_prompt"]
    if long_term:
        prompt += MEMORY_INSTRUCTION
    if execute_rule is not None:
        prompt += PROMPT_LINE
    prompt += safety.instruction
    sub: SubAgent = {
        "name": spec["name"],
        "description": spec["description"],
        "system_prompt": prompt,
        "tools": tools,
        "model": model,
        "middleware": [
            *safety.block,
            build_compaction(model, backend, compaction),
            build_tool_limit(spec["limits"], subagent=True),
            *safety.verdicts,
            ModelRetry(spec["limits"]["retries"], gateway=gateway),
            *skills.middleware,
            *web.outer,
            *judge,
            *web.inner,
            *safety.marks,
        ],
        # 비어 있어도 넘긴다. 빠뜨리면 메인의 interrupt_on 을 물려받는다.
        "interrupt_on": _interrupt_on(safety, {**spec["interrupt_on"], **(execute_rule or {})}),
        "mode": "isolated",
    }
    if skills.sources:
        sub["skills"] = list(skills.sources)
    return sub


def compile_agent(
    config: AgentConfig,
    *,
    tools_by_ref: dict[str, list[BaseTool]],
    knowledge: KnowledgeTools | None = None,
    checkpointer: BaseCheckpointSaver | None,
    model: BaseChatModel | None = None,
    long_term: LongTermMemory | None = None,
    sandboxes: Sandboxes | None = None,
    agent_calls: AgentCalls | None = None,
) -> CompiledAgent:
    """agent.json 과 이미 읽어 둔 MCP 도구·knowledge 검색 도구로 그래프를 만든다.

    model 을 주면 main.model 대신 그것을 쓴다(테스트가 스크립트 모델을 끼운다). long_term 을
    주면 /memories/ 가 그 저장소로 가고, 임시 턴용 그래프를 한 벌 더 만든다. 안 주면 백엔드도
    프롬프트도 예전과 같다. 코드 실행 문서는 sandboxes 가 있어야 한다(boot.py 가 연다). Agent
    소스가 든 문서는 agent_calls 가 있어야 한다.
    """
    tools_by_ref = _with_agent_calls(config, tools_by_ref, agent_calls)
    main = config.main
    filesystem = config.filesystem
    skills = load_skills(config)
    execute_rule = _execute_rule(config)
    if execute_rule is not None and sandboxes is None:
        raise ConfigError("sandbox_url_missing", "code execution is on but no sandbox client")
    executes = execute_rule is not None
    main_model = model or build_chat_model(main["model"], stub_fail_first=fail_first_from_env())
    key = register_profile(main_model, filesystem=filesystem, executes=executes)
    remembers = long_term is not None

    wanted = set(main["subagents"])
    specs = [spec for spec in config.subagents if spec["name"] in wanted]
    # 경로는 Agent 하나에 하나지만(P38 6절) 메인, 폴백, 서브 가운데 하나라도 게이트웨이면
    # 게이트웨이로 본다
    models = [main["model"], *([main["fallback"]] if main["fallback"] else [])]
    gateway = any(m["provider"].get("gateway") for m in [*models, *(s["model"] for s in specs)])
    response_format = _response_format(main)
    dropped: dict[str, list[tuple[str, str]]] = {}
    main_builtins = builtin_names(
        main_model, subagents=bool(specs), structured=response_format is not None
    )
    main_mcp, dropped["main"] = _tools_for(main["tools"], tools_by_ref, main_builtins)
    main_tools = _merge_tools("main", main_mcp, knowledge, config.main_knowledge_refs())
    plan = web_plan(config)
    main_web = plan.layers("main", main["tools"], main=True)
    safety_plan = tool_safety_plan(config, plan.sources)
    # execute 의 규칙은 소스의 것이 아니라 Agent 의 것이라 내장 이름이라도 남는다(P40 9절)
    main_rules = {**config.approvals_for(main["tools"]), **(execute_rule or {})}

    sub_models: dict[str, BaseChatModel] = {}
    sub_tools: dict[str, list[BaseTool]] = {}
    sub_web: dict[str, WebLayers] = {}
    sub_safety: dict[str, AgentSafety] = {}
    # 서브의 턴 제한은 서브 스택이 아니라 메인의 task 호출에 건다(모듈 머리)
    sub_timeouts = {spec["name"]: spec["limits"]["turn_timeout_seconds"] for spec in specs}
    for spec in specs:
        sub_model = build_chat_model(spec["model"], role=ROLE_SUBAGENT)
        register_profile(sub_model, filesystem=filesystem, executes=executes)
        sub_models[spec["name"]] = sub_model
        where = f"subagents[{spec['name']}]"
        sub_builtins = builtin_names(sub_model, subagents=False, structured=False)
        sub_mcp, dropped[where] = _tools_for(spec["tools"], tools_by_ref, sub_builtins)
        knowledge_refs = config.subagent_knowledge_refs(spec["name"])
        sub_tools[spec["name"]] = _merge_tools(where, sub_mcp, knowledge, knowledge_refs)
        sub_web[spec["name"]] = plan.layers(where, spec["tools"], main=False)
        if safety_plan is not None:
            sub_safety[spec["name"]] = safety_plan.agent(
                where,
                spec["tools"],
                tools_by_ref,
                sub_builtins,
                knowledge_refs,
                spec["interrupt_on"],
                builtin_rules=execute_rule,
            )
    main_safety = _main_safety(
        safety_plan, config, tools_by_ref, main_builtins, main_rules, sub_safety, execute_rule
    )
    main_layers = safety_layers(main_safety) if main_safety is not None else NO_SAFETY
    sub_layers = {name: safety_layers(agent) for name, agent in sub_safety.items()}
    interrupt_on = _interrupt_on(main_layers, main_rules)

    groups = parse_groups(config)
    guardrails: Guardrails = build_guardrails(config, groups)
    judges = judge_layer(config, groups)
    fallback_model = (
        build_chat_model(main["fallback"], role=ROLE_FALLBACK) if main["fallback"] else None
    )
    session_budget = config.service["budget"]["session_tokens"]
    system_prompt = _system_prompt(main, long_term=remembers, safety=main_layers, executes=executes)

    def build(backend: BackendProtocol) -> CompiledStateGraph:
        """백엔드 하나로 그래프를 만든다. 압축 미들웨어가 백엔드를 물고 있어 벌마다 새로 만든다."""
        subagents = [
            _subagent(
                spec,
                model=sub_models[spec["name"]],
                tools=sub_tools[spec["name"]],
                backend=backend,
                compaction=main["compaction"],
                long_term=remembers,
                skills=skills.parts(backend, spec["name"]),
                web=sub_web[spec["name"]],
                safety=sub_layers.get(spec["name"], NO_SAFETY),
                judge=judge_middleware(judges),
                execute_rule=execute_rule,
                gateway=gateway,
            )
            for spec in specs
        ]
        main_skills = skills.parts(backend)
        return create_deep_agent(
            model=main_model,
            tools=main_tools,
            system_prompt=system_prompt,
            subagents=subagents or None,
            skills=list(main_skills.sources) or None,
            middleware=[
                *main_layers.block,
                build_compaction(main_model, backend, main["compaction"]),
                *main_skills.middleware,
                build_tool_limit(main["limits"]),
                # 맡긴 작업을 세션의 상태에 둔다(agent_task_tools.py)
                *([AsyncTaskState()] if config.task_sources else []),
                *([SubagentTimeout(sub_timeouts)] if sub_timeouts else []),
                *main_web.outer,
                *judge_middleware(judges),
                *guardrails.middleware,
                *main_web.inner,
                *main_layers.marks,
                *([SessionBudget(session_budget)] if session_budget else []),
                # 폴백·재시도 바깥이라 그 안의 시도가 버린 갱신과 상관없이 판정이 메시지와 함께
                # 남는다
                *main_layers.verdicts,
                *([ModelFallback(fallback_model)] if fallback_model is not None else []),
                ModelRetry(main["limits"]["retries"], gateway=gateway),
                # 폴백·재시도의 안쪽이라 시도마다 그 요청에 이미지를 채운다(uploads.py)
                AttachmentImages(),
            ],
            interrupt_on=interrupt_on or None,
            permissions=build_permissions(config),
            checkpointer=checkpointer,
            response_format=response_format,
            backend=backend,
            context_schema=TurnContext,
            name=config.profile["name"],
        )

    # 샌드박스 백엔드는 하나가 두 그래프를 섬긴다. 세션은 호출마다 실행 설정에서 읽는다(sandbox.py)
    sandbox = SandboxBackend(sandboxes) if sandboxes is not None and executes else None
    graph = build(build_backend(long_term, skills, sandbox))
    if not filesystem:
        _check_filesystem_hidden(graph, key)
    # 임시 턴(테스트 채팅)은 저장소를 안 붙인 그래프로 돈다. /memories/ 가 라우트에서 빠져
    # 세션 안 state 로 가므로 그 세션 안에서는 읽고 쓰다가 세션과 함께 사라진다. /skills/ 는
    # 그대로 붙는다
    ephemeral_graph = build(build_backend(None, skills, sandbox)) if remembers else None
    # 표는 이름으로만 찾는다. 메인에서만 뺀 이름(task, structured_response)을 서브에이전트가
    # 소스에서 받으면 그 호출은 출처 없이 남고, 소스의 task 는 서브에이전트 호출로 보인다
    dropped_names = frozenset(name for pairs in dropped.values() for _, name in pairs)
    return CompiledAgent(
        graph=graph,
        model=main_model,
        tools=main_tools,
        interrupt_on=interrupt_on,
        turn_timeout=float(main["limits"]["turn_timeout_seconds"]),
        harness_key=key,
        subagent_names=[spec["name"] for spec in specs],
        input_guard=guardrails.input,
        tool_guard=guardrails.tool,
        output_filter=guardrails.output_filter,
        fallback_model=fallback_model,
        session_budget=session_budget,
        ephemeral_graph=ephemeral_graph,
        reloads_skills=skills.main is not None,
        tool_sources=tool_source_table(config, tools_by_ref, dropped_names),
        body_masks=guardrails.masks,
        warnings=[
            *correlation_warnings(
                [
                    main_model,
                    fallback_model,
                    *sub_models.values(),
                    judges.model if judges is not None else None,
                ]
            ),
            *builtin_name_warnings(config, dropped),
        ],
        safety=main_safety,
        gateway=gateway,
    )


def _main_safety(
    plan: ToolSafety | None,
    config: AgentConfig,
    tools_by_ref: dict[str, list[BaseTool]],
    builtins: frozenset[str],
    rules: dict[str, list[str]],
    sub_safety: dict[str, AgentSafety],
    execute_rule: dict[str, list[str]] | None = None,
) -> AgentSafety | None:
    """메인의 tool_safety 재료다. 서브에이전트마다 바깥 Tool 을 가졌는가를 싣는다(task 결과)."""
    if plan is None:
        return None
    return plan.agent(
        "main",
        config.main["tools"],
        tools_by_ref,
        builtins,
        config.main_knowledge_refs(),
        rules,
        subagents_outside={name: agent.has_outside for name, agent in sub_safety.items()},
        builtin_rules=execute_rule,
    )


def _execute_rule(config: AgentConfig) -> dict[str, list[str]] | None:
    """코드 실행의 execute 승인 규칙이다(P40 9절). 코드 실행이 꺼졌으면 None, 승인을 묻지 않으면 빈
    사전이다."""
    approval = config.code_execution
    if approval is None:
        return None
    return {EXECUTE_TOOL: approval} if approval else {}
