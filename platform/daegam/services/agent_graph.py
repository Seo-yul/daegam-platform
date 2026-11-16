"""Agent 그래프 검증과 agent.json 컴파일 (D26, resource-model.md 5절).

`validate(spec, ctx)` 는 캔버스 그래프의 규칙과 참조 무결성을 검사해 `Issue` 목록을 돌려준다.
오류는 `level: error`, 경고는 `level: warning` 이고 `node_id`·`edge_id` 로 캔버스가 노드에 표식을
단다. 저장·Deploy·`POST /validate` 가 같은 함수를 쓴다.

`compile(instance, revision, ctx)` 는 검증을 통과한 그래프를
`agent-runtime/schema/agent.schema.json` 을 따르는 런타임 설정으로 펼친다. 메인이 어떤
툴·서브에이전트·메모리를 쓰는가만 남고, 비밀 값 대신 파드 env 이름(`env_key`, `secret_env`,
`dsn_env`)만 가리킨다. 스키마 검사는 기능 칸을 얹은 최종 문서에 서비스가
한다(`agent_features.finish_document`). 웹 Tool 의 결정과 Tool 핀은 `agent_web` 이다.

노드 `data` 의 종류별 형식은 여기의 pydantic 모델이 정한다. 스토어는 dict 로 저장하므로 모양이 틀린
노드는 저장이 거부되는 대신 `invalid_node_data` 이슈로 그 노드에 표식이 붙는다.

모델은 `model` 노드다(D28). 에이전트는 `model` 간선으로 모델 노드 하나에 붙어야 하고(`model_count`),
한 모델 노드를 여러 에이전트가 공유할 수 있다. compile 이 모델 노드를 에이전트의 `model`(과
`fallback`) 객체로 풀어 넣으므로 agent.json 계약은 그대로다.

지식은 `knowledge` 노드다(D32). 에이전트(메인·서브)는 `knowledge` 간선으로 Knowledge Base 하나를
검색 도구 하나로 붙이고 여러 에이전트가 한 노드를 공유할 수 있다. `_check_knowledge` 가 Knowledge
Base 존재·삭제 중·임베딩 provider 키·deepagents 내장 도구 이름을 보고 인덱싱 상태는 경고로 알리며,
한 에이전트 안의 도구 이름 중복은 `_check_knowledge_tool_names` 가 본다.
compile 은 닿는 에이전트가 붙인 knowledge 노드를 `knowledge[]` 로 펼치고
`main.knowledge`·`subagents[].knowledge` 가 ref 로 가리킨다. 임베딩 키는
`DAEGAM_KNOWLEDGE_<n>_API_KEY`(n 은 `knowledge[]` 순서) 이고 벡터 테이블의 DSN 은 memory 와 같은
인스턴스 바인딩(`DAEGAM_DB_<i>_URL`) 이다.

메모리는 `memory` 노드다(D26, D33, D34). 메인이 `memory` 간선으로 Database 하나를 고르고
체크포인터의 종류는 그 Database 의 kind(`postgres` 또는 `redis`)가 정한다. redis 는 verify 가 남긴
`capabilities.checkpointer` 가 `ready`(Redis 8 의 RediSearch·RedisJSON)여야 하고 아니면
`redis_modules_missing` 이며, db 번호가 0 이 아니면 `redis_db_not_zero` 다(RediSearch 가 db 0
밖에서 인덱스를 못 만든다). compile 은 `memory.checkpointer.kind` 에 Database 의 kind 를 그대로
낸다. 같은 노드의 `long_term` 은 사용자별 장기 기억이고 켜져 있으면 compile 이
`memory.long_term` 을 낸다. 켜려면 그 노드가 메인에 붙어 있어야 하고(`long_term_needs_memory`)
메인의 파일 도구가 켜져 있어야 한다(`long_term_filesystem_disabled`).

가드레일은 `guardrail` 노드이고 메인에만 붙는다. 규칙 `injection_check`(P43 7절)는 position tool
에만 오고 판정 모델을 같은 그래프의 모델 노드 id 로 가리킨다. 그 모델 노드는 간선 없이도 붙은
것으로 치고(`_check_attachments`), compile 이 모델 객체로 풀어 규칙에 싣는다. 판정은 그래프에
하나까지다.

`schema_version` 은 사다리다. 둘 다 꺼져 있으면 `"1"`(P15 전과 글자 그대로 같은 문서, knowledge
키 셋 없음), knowledge 항목이 있으면 `"2"`, 장기 기억이 켜져 있으면 `"3"` 이고 `"3"` 은 knowledge
키 셋을 항상(없으면 빈 목록) 함께 낸다. 옛 런타임 이미지가 `"1"` 을 계속 받게 하려는 것이다.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, get_args

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from daegam.contracts import agent_schema

# 런타임 파드의 env 이름은 렌더하는 템플릿이 정한다. 컴파일은 그 이름을 agent.json 에 실을 뿐이다.
# MCP_TOKEN_ENV 는 테넌트 audience 하나인 옛 단일 서비스 토큰이고 표가 없거나 표에 없는 MCP Server
# 소스가 쓴다(D41, P50 9절)
from daegam.k8s.templates.agent import (
    AGENT_CLIENT_SECRET_ENV,
    GATEWAY_TOKEN_ENV,
    MCP_TOKEN_ENV,
    ProviderBinding,
    dsn_env,
    knowledge_key_env,
    provider_binding,
)
from daegam.k8s.templates.common import proxy_service_url
from daegam.k8s.templates.vmcp import vmcp_cluster_url
from daegam.regex_safety import check_regex
from daegam.services.agent_skills import (
    Pin,
    PinnedSkill,
    SkillPins,
    attached_skills,
    check_skills,
)
from daegam.services.toolnames import RESERVED_TOOL_NAMES, TOOL_NAME_RE
from daegam.store.agents import (
    NODE_TYPES,
    Agent,
    AgentSpec,
    EdgeKind,
    Graph,
    GraphEdge,
    GraphNode,
    NodeType,
    ToolPin,
)
from daegam.store.data_services import DataService
from daegam.store.gateways import Gateway
from daegam.store.knowledge import KnowledgeBase, KnowledgeStatus
from daegam.store.mcp_servers import MCPServer
from daegam.store.settings import LLMProvider, WebToolsSettings
from daegam.store.tenants import TenantSettings

if TYPE_CHECKING:
    from daegam.services.tool_effects import SourceTools

# redis Database 의 `capabilities.checkpointer` 값별 `redis_modules_missing` 설명(D33). 캔버스
# 검증(ui/src/lib/agentGraph.ts)이 같은 문구를 낸다
REDIS_CHECKPOINTER_DETAIL: dict[str | None, str] = {
    None: "run verify first; the checkpointer needs Redis 8 with RediSearch and RedisJSON",
    "modules_missing": "Redis has no RediSearch/RedisJSON (Redis 8 bundles them)",
    "unknown": "the verify account cannot run FT._LIST; grant it or verify with another account",
}

Level = Literal["error", "warning"]
Effort = Literal["off", "low", "medium", "high", "xhigh", "max"]
Decision = Literal["approve", "edit", "reject", "respond"]
PIIType = Literal["email", "credit_card", "ip", "mac_address", "url", "regex"]
SourceKind = Literal["gateway_instance", "mcp_instance"]
SOURCE_KINDS: frozenset[str] = frozenset(get_args(SourceKind))
# entry·에이전트에 붙는 노드 종류. 붙이는 간선의 종류도 같은 이름이라(ALLOWED_EDGES) 노드 종류로도
# 간선 종류로도 쓴다
Attachable = Literal["model", "tools", "memory", "guardrail", "knowledge"]
ATTACHABLE: tuple[Attachable, ...] = ("model", "tools", "memory", "guardrail", "knowledge")
# 판정 모델의 가드레일 규칙 종류다(P43 7절). 문서 기능 이름(`agent_features.INJECTION_CHECK`)과
# 글자가 같을 뿐 다른 이름이다
RULE_INJECTION_CHECK = "injection_check"
# 판정 모델은 한 낱말만 답하므로 모델 노드의 추론 강도와 상관없이 추론 없이 부른다(F0 5번이 잰 모양)
JUDGE_EFFORT: Effort = "off"


# -- 이슈 ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    level: Level = "error"
    node_id: str | None = None
    edge_id: str | None = None
    # 화면이 문장에 끼울 값이다. 서버가 낸 이슈 가운데 이름을 싣는 것만 갖는다
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "level": self.level,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
        }
        if self.data is not None:
            out["data"] = self.data
        return out


def errors_of(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]


def issue_lines(issues: list[Issue]) -> list[str]:
    """응답의 warnings[] 와 기록의 경고가 싣는 `code: 설명` 줄이다."""
    return [f"{i.code}: {i.message}" for i in issues]


class GraphError(Exception):
    """검증 오류가 있는 그래프. 라우터는 422 invalid_graph 에 issues 를 실어 돌려준다."""

    status = 422
    code = "invalid_graph"

    def __init__(self, issues: list[Issue]):
        errors = errors_of(issues)
        super().__init__(f"{len(errors)} graph error(s)")
        self.issues = issues

    def to_dict(self) -> dict[str, Any]:
        errors = errors_of(self.issues)
        return {
            "code": self.code,
            "detail": f"graph has {len(errors)} error(s): "
            + ", ".join(sorted({e.code for e in errors})),
            "issues": [i.to_dict() for i in self.issues],
        }


class CompileError(Exception):
    """검증을 통과한 그래프에서 스키마에 어긋나는 문서가 나왔다. compile 의 버그다. 서비스의 최종
    문서 검사(agent_features.finish_document)가 낸다.

    옆의 `GraphError` 와 달리 `to_dict()` 가 없다. 라우터로 가는 변환은 `agents` 의
    compile 이 `AgentError(status, code, str(e), errors=messages)` 로 직접 하고, 그래서 응답
    detail 은 위반 메시지를 `; ` 로 이은 문장이다."""

    status = 500
    code = "compile_failed"

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


# -- 노드 data 모델 (resource-model 5절 표) --------------------------------------------


class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_user_per_minute: int = Field(ge=1)


class EntryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_ttl_seconds: int = Field(default=3600, ge=1)
    max_turns: int = Field(default=50, ge=1)
    rate_limit: RateLimit | None = None
    max_concurrent_sessions: int = Field(default=100, ge=1)
    max_input_chars: int = Field(default=4000, ge=1)
    greeting: str | None = None


class Reasoning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effort: Effort = "medium"


class CompactionTrigger(BaseModel):
    """`{tokens: n}` | `{messages: n}` | `{fraction: f}` 중 하나."""

    model_config = ConfigDict(extra="forbid")

    tokens: int | None = Field(default=None, ge=1)
    messages: int | None = Field(default=None, ge=1)
    fraction: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _exactly_one(self) -> CompactionTrigger:
        given = [k for k in ("tokens", "messages", "fraction") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("trigger must have exactly one of tokens, messages, fraction")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in ("tokens", "messages", "fraction") if getattr(self, k) is not None)

    @property
    def value(self) -> int | float:
        return getattr(self, self.kind)


class CompactionKeep(BaseModel):
    """`{messages: n}` | `{fraction: f}` 중 하나."""

    model_config = ConfigDict(extra="forbid")

    messages: int | None = Field(default=None, ge=1)
    fraction: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _exactly_one(self) -> CompactionKeep:
        given = [k for k in ("messages", "fraction") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("keep must have exactly one of messages, fraction")
        return self

    @property
    def kind(self) -> str:
        return "messages" if self.messages is not None else "fraction"

    @property
    def value(self) -> int | float:
        return getattr(self, self.kind)


class Compaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: CompactionTrigger = Field(default_factory=lambda: CompactionTrigger(fraction=0.85))
    keep: CompactionKeep = Field(default_factory=lambda: CompactionKeep(fraction=0.1))


class ContextData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compaction: Compaction = Field(default_factory=Compaction)


class Fallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str | None = None


class JsonSchemaFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_schema: dict[str, Any]


class BuiltinTools(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 파일 도구 7종은 기본으로 노출한다. 세션 state 안의 파일시스템이라 밖으로 새지 않고,
    # 장기 기억(D34)을 켜려면 어차피 켜져 있어야 한다. 숨기려면 이 값을 끈다
    filesystem: bool = True


class CodeExecution(BaseModel):
    """메인 에이전트의 코드 실행이다(P40 9절, D95). 있으면 켠 것이고 approval 이 있으면 execute
    를 그 결정으로 묻는다. 끄면 칸을 지운다."""

    model_config = ConfigDict(extra="forbid")

    approval: list[Decision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> CodeExecution:
        if len(set(self.approval)) != len(self.approval):
            raise ValueError("code_execution.approval repeats a decision")
        return self


# execute 한 번이 받을 수 있는 가장 긴 시간이다(agent-runtime `runtime/sandbox.py`
# MAX_COMMAND_SECONDS). 메인의 턴 제한이 이보다 짧으면 캔버스가 경고한다(9절)
SANDBOX_COMMAND_MAX_SECONDS = 300


class AgentData(BaseModel):
    """프롬프트·실행 한도·컨텍스트 압축·응답 형식·언어·내장 도구. 모델은 model 노드에 있다(D28)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["main", "sub"]
    name: str = Field(min_length=1)
    description: str = ""  # 서브에이전트의 task 도구 설명
    system_prompt: str = ""
    context: ContextData = Field(default_factory=ContextData)
    max_tool_calls_per_turn: int = Field(default=8, ge=1)
    turn_timeout_seconds: int = Field(default=120, ge=1)
    retries: int = Field(default=2, ge=0)
    response_format: Literal["text"] | JsonSchemaFormat = "text"
    language: str | None = None
    builtin_tools: BuiltinTools = Field(default_factory=BuiltinTools)
    skills: SkillPins = Field(default_factory=list)  # 붙인 Skill 과 핀한 리비전(D64)
    # 코드 실행(P40). 메인의 것만 컴파일이 읽는다. 서브는 메인의 작업 공간을 함께 쓴다
    code_execution: CodeExecution | None = None


class ModelData(BaseModel):
    """model 노드. 에이전트마다 하나가 붙고 여러 에이전트가 공유할 수 있다. name 은 캔버스 표시
    이름이라 agent.json 에 들어가지 않는다."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    provider: str = ""  # llm_providers 이름. 비어 있으면 unknown_provider
    model: str | None = None  # None 이면 provider 의 기본 모델
    max_output_tokens: int = Field(default=2048, ge=1)
    reasoning: Reasoning = Field(default_factory=Reasoning)
    temperature: float | None = Field(default=None, ge=0)  # openai 계열만 적용
    fallback: Fallback | None = None


class ToolsSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    id: str = ""  # 비어 있으면 unknown_source


class ToolsData(BaseModel):
    # 모르는 칸(뒤 판이 더한 것)은 버린다. 앞 판으로 되돌려도 그 그래프를 검증·컴파일한다
    model_config = ConfigDict(extra="ignore")

    source: ToolsSource
    allow_tools: list[str] = Field(default_factory=list)  # 비면 전부
    approval: dict[str, list[Decision]] = Field(default_factory=dict)
    timeouts: dict[str, int] = Field(default_factory=dict)
    # 웹 Tool 곁의 규칙 (P39b 14절, `agent_web`). 읽기 전용으로 표시한 write Tool, fetch 의
    # 허용·막는 도메인
    read_only: list[str] = Field(default_factory=list)
    allow_domains: list[str] = Field(default_factory=list)
    block_domains: list[str] = Field(default_factory=list)
    # 바깥 내용 표시를 백엔드 종류의 기본값에서 바꾼 Tool 이다(P43 2절, `agent_tool_safety`).
    # 참이면 결과가 바깥 내용, 거짓이면 아니다. 없는 이름은 기본값이다
    outside: dict[str, bool] = Field(default_factory=dict)
    # 사람이 승인을 기억해도 되는 Tool 이다(P43 5절, 기본 꺼짐). 그 Tool 의 승인 규칙이 바뀌거나 이
    # 목록이 바뀐 배포는 기억한 선택을 지운다(`services/approval_choices.py`)
    rememberable: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ToolsData:
        if any(not t for t in self.allow_tools):
            raise ValueError("allow_tools entries must not be empty")
        if len(set(self.allow_tools)) != len(self.allow_tools):
            raise ValueError("allow_tools contains duplicates")
        for tool, decisions in self.approval.items():
            if not tool or not decisions:
                raise ValueError("approval needs a tool name and at least one decision")
            if len(set(decisions)) != len(decisions):
                raise ValueError(f"approval for {tool!r} repeats a decision")
        for tool, seconds in self.timeouts.items():
            if not tool or seconds < 1:
                raise ValueError("timeouts need a tool name and seconds >= 1")
        return self


class Checkpointer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_service_id: str = ""  # 비어 있으면 unknown_data_service


class LongTerm(BaseModel):
    """사용자별 장기 기억(D34). 키가 있으면 켠 것이고 없으면 아예 없는 것이다.

    경로는 `/memories` 고정이고 네임스페이스는 `(tenant, instance_id, sha256(세션 소유자 sub))`
    라 설정할 것이 보관 기간 하나뿐이다. `retention_days` 가 null 이면 만료가 없다.
    """

    model_config = ConfigDict(extra="forbid")

    retention_days: int | None = Field(default=None, ge=1)


class MemoryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpointer: Checkpointer = Field(default_factory=Checkpointer)
    # 장기 기억. 꺼져 있으면 None 이고 compile 이 memory.long_term 키를 아예 내지 않는다
    long_term: LongTerm | None = None


class PIIMask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: list[PIIType] = Field(min_length=1)
    regex: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PIIMask:
        if len(set(self.types)) != len(self.types):
            raise ValueError("types contains duplicates")
        if "regex" in self.types and not self.regex:
            raise ValueError("types has regex, so regex is required")
        if self.regex is not None:
            check_regex(self.regex)
        return self


class InjectionCheck(BaseModel):
    """판정 모델이다(P43 7절). model 은 같은 그래프의 모델 노드 id 다. 비어 있거나 모델 노드가
    아니면 `unknown_judge_model` 이다."""

    model_config = ConfigDict(extra="forbid")

    model: str = ""
    max_chars: int = Field(default=8000, ge=200, le=200000)
    max_calls: int = Field(default=20, ge=1, le=1000)


class GuardrailRule(BaseModel):
    """kind 에 따라 value 의 모양이 다르다. max_length 는 정수, deny_pattern 은 정규식,
    pii_mask 는 {types[], regex?}, injection_check 는 {model, max_chars, max_calls} 다."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["max_length", "deny_pattern", "pii_mask", "injection_check"]
    value: Any

    @model_validator(mode="after")
    def _check(self) -> GuardrailRule:
        if self.kind == "max_length":
            if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value < 1:
                raise ValueError("max_length value must be an integer >= 1")
        elif self.kind == "deny_pattern":
            if not isinstance(self.value, str) or not self.value:
                raise ValueError("deny_pattern value must be a non-empty regex")
            check_regex(self.value)
        elif self.kind == RULE_INJECTION_CHECK:
            self.value = InjectionCheck.model_validate(self.value).model_dump()
        else:
            self.value = PIIMask.model_validate(self.value).model_dump()
        return self


class GuardrailData(BaseModel):
    """injection_check 는 position tool 에만 오고 노드 하나에 하나까지다(P43 7절)."""

    model_config = ConfigDict(extra="forbid")

    position: Literal["input", "output", "tool"]
    rules: list[GuardrailRule] = Field(min_length=1)
    on_violation: Literal["block", "flag"] = "block"
    message: str | None = None

    @model_validator(mode="after")
    def _check(self) -> GuardrailData:
        judges = [r for r in self.rules if r.kind == RULE_INJECTION_CHECK]
        if judges and self.position != "tool":
            raise ValueError("injection_check runs only at position tool")
        if len(judges) > 1:
            raise ValueError("only one injection_check rule is allowed in a guardrail node")
        return self

    @property
    def judge(self) -> InjectionCheck | None:
        """이 노드의 injection_check 규칙이다. 없으면 None 이다."""
        for rule in self.rules:
            if rule.kind == RULE_INJECTION_CHECK:
                return InjectionCheck.model_validate(rule.value)
        return None


class KnowledgeData(BaseModel):
    """knowledge 노드. Knowledge Base 하나를 검색 도구 하나로 에이전트에 붙인다(D32). description
    이 비어 있으면 compile 이 Knowledge Base 이름으로 기본 설명을 만든다."""

    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: str = ""  # 비어 있으면 unknown_knowledge_base
    tool_name: str = Field(default="search_knowledge", pattern=TOOL_NAME_RE.pattern)
    description: str = ""
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0, le=1)
    # 검색 결과를 바깥 내용으로 다루는가(P43 2절). 검토를 거친 문서만 있는 Base 는 끌 수 있다
    outside: bool = True


NODE_DATA_MODELS: dict[NodeType, type[BaseModel]] = {
    "entry": EntryData,
    "agent": AgentData,
    "model": ModelData,
    "tools": ToolsData,
    "memory": MemoryData,
    "guardrail": GuardrailData,
    "knowledge": KnowledgeData,
}

# 노드 종류마다 data 모델이 하나씩 있어야 한다. _parse_nodes 가 키 검사 없이
# NODE_DATA_MODELS[node.type] 을 찾으므로 빠지면 저장이 KeyError 로 터진다
assert set(NODE_DATA_MODELS) == set(NODE_TYPES), "NODE_DATA_MODELS must cover every NodeType"

# (source 종류, target 종류) → 허용 간선 종류. 방향은 항상 "붙이는 쪽 → 붙는 것" 이다
ALLOWED_EDGES: dict[tuple[NodeType, NodeType], EdgeKind] = {
    ("entry", "agent"): "entry",
    ("agent", "model"): "model",
    ("agent", "tools"): "tools",
    ("agent", "agent"): "delegate",
    ("agent", "memory"): "memory",
    ("agent", "guardrail"): "guardrail",
    ("agent", "knowledge"): "knowledge",
}


# -- 문맥 ---------------------------------------------------------------------------


@dataclass
class GraphContext:
    """검증·컴파일이 보는 플랫폼 상태. 서비스가 테넌트별로 만든다."""

    providers: dict[str, LLMProvider]  # Platform Settings llm_providers (이름 → provider)
    has_key: Callable[[LLMProvider], bool]  # api_key_secret 이 실제 키를 갖는가 (동기 K8s 조회)
    token_auth: bool
    tenant_settings: TenantSettings
    # 인증 모드 (D41). Gateway 소스의 인증 종류를 가른다: oidc 는 agent_oauth, local 은 플랫폼이 낸
    # 데이터 플레인 토큰, header 는 없음. None 이면 token_auth 로 oidc 여부를 짐작한다(옛 호출부)
    auth_mode: str | None = None
    # 데이터 플레인이 플랫폼을 믿는가 (P53b 6.1절). 참인 oidc 모드는 Gateway 를 local 처럼 플랫폼이
    # 낸 토큰으로 부르고 agent_oauth 를 요구하지 않는다
    trusts_platform: bool = False
    gateway_instances: dict[str, Gateway] = field(default_factory=dict)  # 배포 쪽 문서
    mcp_instances: dict[str, MCPServer] = field(default_factory=dict)
    data_services: dict[str, DataService] = field(default_factory=dict)
    # 테넌트의 Knowledge Base (status 집계가 채워진 것). 문맥을 만들 때 한 번 센다
    knowledge_bases: dict[str, KnowledgeBase] = field(default_factory=dict)
    # 그래프가 핀한 Skill 리비전의 메타(D64). 서비스가 핀만 미리 읽는다
    skill_revisions: dict[Pin, PinnedSkill] = field(default_factory=dict)
    # MCP Server 소스마다의 토큰 env (서버 id → env 이름, P50 9절). 컴파일을 받는 파드가 가진 표다.
    # None 이면 표를 모르는 파드라 모든 소스가, 표에 없는 소스는 그 소스가 옛 단일
    # 토큰(`MCP_TOKEN_ENV`)을 쓴다. 그 env 는 모든 템플릿에 있어 파드가 준비된 채로 남는다
    mcp_token_envs: dict[str, str] | None = None
    # 도구 소스마다 지금 광고하는 Tool 과 효과 (소스 id → SourceTools, P39b 14절 4부)
    source_tools: dict[str, SourceTools] = field(default_factory=dict)
    # 컴파일하는 문서의 배포 때 Tool 핀 (노드 id → ToolPin, `services/tool_pins.py`). 검증은 비운다
    tool_pins: dict[str, ToolPin] = field(default_factory=dict)
    # Platform Settings 의 웹 Tool 설정 (P39b 15절). 컴파일이 막는 접미사와 사용자별 한도를 싣는다
    web_tools: WebToolsSettings | None = None
    llm_gateway_url: str | None = None  # 게이트웨이 경로로 컴파일하면 그 주소 (P38 6절)
    # 상태를 파드 하나 안에 두는 미리 보기의 검증이다 (P44b 7절). 공유 카운터를 요구하는
    # `memory_required` 를 보지 않는다. 승인, 분당 한도, 세션 예산은 그 파드 하나 안에서 맞고 일일
    # 예산은 플랫폼이 원장으로 막는다. 미리 보기의 정의에는 memory 노드가 없어
    # `long_term_needs_memory` 는 처음부터 나지 않는다(`services/agent_preview.py` `preview_spec`)
    in_pod_state: bool = False
    # 미리 보기가 파드 안에서 승인을 묻는가 (P44b 7절). 그 리비전이 원본에서 메인에 memory 노드를
    # 가졌을 때만 참이다. 그 미리 보기의 agent.json 은 기능 `in_pod_state` 를 싣고, 검증은 경고
    # `outside_writes_refused` 를 내지 않는다(`services/agent_tool_safety.py`). 서비스가 채운다
    in_pod_approvals: bool = False

    def source_pool(self, kind: str) -> dict[str, Any]:
        """도구 소스 종류의 사전. `gateway_instance` 면 Gateway, 아니면 MCP Server 다."""
        return self.gateway_instances if kind == "gateway_instance" else self.mcp_instances


def key_checker(ctx: GraphContext) -> Callable[[LLMProvider], bool]:
    """provider 에 키가 있는가. stub 은 면제이고 같은 provider 는 한 번만 Secret 을 읽는다."""
    cache: dict[str, bool] = {}

    def key_ok(p: LLMProvider) -> bool:
        if p.name not in cache:
            cache[p.name] = p.is_stub or ctx.has_key(p)
        return cache[p.name]

    return key_ok


# -- 분석 ---------------------------------------------------------------------------


@dataclass
class Analysis:
    """그래프를 한 번 훑은 결과. validate 의 이슈와 compile 의 재료를 함께 갖는다."""

    nodes: dict[str, GraphNode]
    order: list[str]  # graph.nodes 순서 (컴파일 결과의 결정적 순서)
    data: dict[str, BaseModel]  # 형식 검사를 통과한 노드의 data
    edges: list[GraphEdge]
    issues: list[Issue] = field(default_factory=list)
    entry: str | None = None
    main: str | None = None
    reachable: set[str] = field(default_factory=set)  # entry 에서 닿는 노드

    @property
    def main_has_memory(self) -> bool:
        """메인에 memory 노드가 붙었는가. 승인과 공유 카운터는 그것이 있어야 한다."""
        return self.main is not None and bool(self.targets(self.main, "memory"))

    def of_type(self, node_type: NodeType) -> list[str]:
        return [i for i in self.order if self.nodes[i].type == node_type]

    def typed[T: BaseModel](self, node_id: str, model: type[T]) -> T | None:
        """노드의 data 가 model 형식이면 그것, 아니면 None 이다. 형식이 틀린 노드에는 _parse_nodes
        가 invalid_node_data 로 이미 표식을 붙였으므로 부르는 쪽은 조용히 건너뛰면 된다."""
        d = self.data.get(node_id)
        return d if isinstance(d, model) else None

    def nodes_of[T: BaseModel](self, node_type: NodeType, model: type[T]) -> list[tuple[str, T]]:
        """node_type 노드 중 data 형식이 맞는 것을 (노드 id, data) 로. graph.nodes 순서다."""
        return [(i, d) for i in self.of_type(node_type) if (d := self.typed(i, model)) is not None]

    def typed_targets[T: BaseModel](
        self, node_id: str, kind: EdgeKind, model: type[T]
    ) -> list[tuple[str, T]]:
        """node_id 가 kind 간선으로 붙인 노드 중 data 형식이 맞는 것. graph.nodes 순서다."""
        return [
            (t, d) for t in self.targets(node_id, kind) if (d := self.typed(t, model)) is not None
        ]

    def agent(self, node_id: str) -> AgentData | None:
        return self.typed(node_id, AgentData)

    def model_of(self, agent_id: str) -> ModelData | None:
        """에이전트가 model 간선으로 붙인 모델 노드의 data. 검증을 통과한 그래프에서는 하나다."""
        models = self.typed_targets(agent_id, "model", ModelData)
        return models[0][1] if models else None

    def knowledge_of(self, agent_id: str) -> list[str]:
        """에이전트가 knowledge 간선으로 붙인 knowledge 노드 중 data 형식이 맞는 것. graph.nodes
        순서다."""
        return [i for i, _ in self.typed_targets(agent_id, "knowledge", KnowledgeData)]

    def attached_knowledge_nodes(self) -> list[tuple[str, KnowledgeData]]:
        """닿는 에이전트가 붙인 knowledge 노드를 (노드 id, data) 로. graph.nodes 순서이고 한 번씩
        이다. compile 의 `knowledge[]` 순서이자 임베딩 키 env 의 번호 n 이다."""
        used: set[str] = set()
        for agent_id in self.reachable:
            if self.nodes[agent_id].type == "agent":
                used.update(self.knowledge_of(agent_id))
        return [(i, d) for i, d in self.nodes_of("knowledge", KnowledgeData) if i in used]

    def main_guardrails(self) -> list[tuple[str, GuardrailData]]:
        """메인이 붙인 guardrail 노드를 (노드 id, data) 로. graph.nodes 순서이고 한 노드는 한
        번이다. compile 의 `guardrails[]` 와 판정 모델(`judges`)이 같이 쓴다."""
        if self.main is None:
            return []
        attached = set(self.targets(self.main, "guardrail"))
        return [(i, d) for i, d in self.nodes_of("guardrail", GuardrailData) if i in attached]

    def judges(self) -> list[tuple[str, InjectionCheck]]:
        """메인이 붙인 guardrail 노드의 injection_check 를 (노드 id, 규칙) 으로. graph.nodes 순서다.
        검증을 통과한 그래프에서는 하나까지다(`judge_count`)."""
        return [
            (node_id, judge)
            for node_id, data in self.main_guardrails()
            if (judge := data.judge) is not None
        ]

    def judge_model(self) -> ModelData | None:
        """판정 모델 노드의 data 다. 판정이 없거나 모델 노드를 가리키지 않으면 None 이다."""
        judges = self.judges()
        return self.typed(judges[0][1].model, ModelData) if judges else None

    def out_edges(self, node_id: str, kind: EdgeKind) -> list[GraphEdge]:
        return [e for e in self.edges if e.source == node_id and e.kind == kind]

    def targets(self, node_id: str, kind: EdgeKind) -> list[str]:
        """node_id 에서 kind 간선으로 나가는 대상. graph.nodes 순서이고 중복은 하나로 본다."""
        wanted = {e.target for e in self.out_edges(node_id, kind)}
        return [i for i in self.order if i in wanted]

    def approvals(self, agent_id: str) -> dict[str, list[str]]:
        """에이전트가 붙인 tools 노드들의 승인 규칙을 하나로 합친 것. 같은 도구 이름이 여러 노드에
        있으면 graph.nodes 순서에서 뒤엣것이 이긴다. compile 의 `interrupt_on` 이다."""
        merged: dict[str, list[str]] = {}
        for _tools_id, data in self.typed_targets(agent_id, "tools", ToolsData):
            for tool, decisions in data.approval.items():
                merged[tool] = list(decisions)
        return merged

    def error(self, code: str, message: str, **where: Any) -> None:
        """오류 이슈 하나. `where` 는 표식이 붙는 `node_id` 또는 `edge_id` 다."""
        self.issues.append(Issue(code, message, "error", **where))

    def warn(self, code: str, message: str, **where: Any) -> None:
        """경고 이슈 하나. 저장·Deploy 를 막지 않는다."""
        self.issues.append(Issue(code, message, "warning", **where))


def _parse_nodes(graph: Graph, a: Analysis) -> None:
    seen: set[str] = set()
    for node in graph.nodes:
        if node.id in seen:
            a.error("duplicate_node_id", f"node id {node.id!r} is used twice", node_id=node.id)
            continue
        seen.add(node.id)
        a.nodes[node.id] = node
        a.order.append(node.id)
        model = NODE_DATA_MODELS[node.type]
        try:
            a.data[node.id] = model.model_validate(node.data)
        except ValidationError as e:
            first = e.errors()[0]
            loc = ".".join(str(p) for p in first["loc"]) or "data"
            a.error(
                "invalid_node_data",
                f"{node.type} node {node.id!r}: {loc}: {first['msg']}",
                node_id=node.id,
            )
            continue
        # 모르는 칸을 버리는 모델(ToolsData)도 저장하는 사람에게는 알린다. `allow_domain` 같은
        # 오타가 안전 규칙을 조용히 빼지 않게 한다
        unknown = sorted(set(node.data) - set(model.model_fields))
        if unknown and model.model_config.get("extra") == "ignore":
            a.warn(
                "unknown_node_keys",
                f"{node.type} node {node.id!r}: keys this release does not know are ignored: "
                + ", ".join(unknown),
                node_id=node.id,
            )
    for node_id, data in a.nodes_of("agent", AgentData):
        if data.role == "sub" and not data.description.strip():
            a.error(
                "invalid_node_data",
                f"agent node {node_id!r}: a sub agent needs a description "
                "(the main agent reads it as the task tool description)",
                node_id=node_id,
            )


def _check_counts(a: Analysis) -> None:
    entries = a.of_type("entry")
    if not entries:
        a.error("entry_count", "the graph needs exactly one entry node")
    else:
        a.entry = entries[0]
        for extra in entries[1:]:
            a.error("entry_count", "only one entry node is allowed", node_id=extra)
    mains = [i for i, d in a.nodes_of("agent", AgentData) if d.role == "main"]
    if not mains:
        a.error("main_agent_count", "the graph needs exactly one agent node with role main")
    else:
        a.main = mains[0]
        for extra in mains[1:]:
            a.error("main_agent_count", "only one main agent is allowed", node_id=extra)
    memories = a.of_type("memory")
    for extra in memories[1:]:
        a.error("memory_count", "at most one memory node is allowed", node_id=extra)


def _check_edges(graph: Graph, a: Analysis) -> None:
    seen: set[str] = set()
    for edge in graph.edges:
        if edge.id in seen:
            a.error("duplicate_edge_id", f"edge id {edge.id!r} is used twice", edge_id=edge.id)
            continue
        seen.add(edge.id)
        src, dst = a.nodes.get(edge.source), a.nodes.get(edge.target)
        if src is None or dst is None:
            missing = edge.source if src is None else edge.target
            a.error(
                "dangling_edge",
                f"edge {edge.id!r} refers to unknown node {missing!r}",
                edge_id=edge.id,
            )
            continue
        a.edges.append(edge)
        expected = ALLOWED_EDGES.get((src.type, dst.type))
        if expected is None or expected != edge.kind:
            a.error(
                "invalid_edge_kind",
                f"edge {edge.id!r}: {edge.kind} from {src.type} to {dst.type} is not allowed",
                edge_id=edge.id,
            )
            continue
        src_agent, dst_agent = a.agent(edge.source), a.agent(edge.target)
        if edge.kind == "entry" and dst_agent is not None and dst_agent.role != "main":
            a.error(
                "invalid_edge_kind", "the entry edge must target the main agent", edge_id=edge.id
            )
        elif edge.kind == "delegate":
            if edge.source == edge.target:
                a.error(
                    "delegation_cycle",
                    f"agent {edge.source!r} delegates to itself",
                    edge_id=edge.id,
                )
            elif src_agent is not None and src_agent.role != "main":
                a.error(
                    "invalid_edge_kind",
                    "delegation only goes out from the main agent (sub agents cannot delegate)",
                    edge_id=edge.id,
                )
            elif dst_agent is not None and dst_agent.role != "sub":
                a.error("invalid_edge_kind", "delegation must target a sub agent", edge_id=edge.id)
        elif (
            edge.kind in ("memory", "guardrail")
            and src_agent is not None
            and src_agent.role != "main"
        ):
            # model·tools·knowledge 간선은 메인·서브 어느 쪽에서든 나갈 수 있다
            a.error(
                "invalid_edge_kind",
                f"{edge.kind} attaches to the main agent, not to a sub agent",
                edge_id=edge.id,
            )


def _check_delegation_cycles(a: Analysis) -> None:
    """delegate 간선의 순환. 되돌아가는 간선(back edge)마다 이슈 하나다. 자기 순환은 _check_edges
    가 봤다."""
    out: dict[str, list[GraphEdge]] = {}
    for e in a.edges:
        if e.kind == "delegate" and e.source != e.target:
            out.setdefault(e.source, []).append(e)
    state: dict[str, int] = {}  # 1 방문 중, 2 끝

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for e in out.get(node_id, []):
            s = state.get(e.target, 0)
            if s == 1:
                a.error(
                    "delegation_cycle",
                    f"edge {e.id!r} closes a delegation cycle back to {e.target!r}",
                    edge_id=e.id,
                )
            elif s == 0:
                visit(e.target)
        state[node_id] = 2

    for node_id in a.of_type("agent"):
        if state.get(node_id, 0) == 0:
            visit(node_id)


def _check_reachability(a: Analysis) -> None:
    """entry → 메인(entry 간선) → 서브(delegate 간선). 에이전트는 전부 닿아야 한다."""
    if a.entry is None:
        return
    a.reachable.add(a.entry)
    stack = [t for t in a.targets(a.entry, "entry") if a.nodes[t].type == "agent"]
    while stack:
        node_id = stack.pop()
        if node_id in a.reachable:
            continue
        a.reachable.add(node_id)
        stack.extend(a.targets(node_id, "delegate"))
    for node_id in a.of_type("agent"):
        if node_id not in a.reachable:
            data = a.agent(node_id)
            hint = (
                "connect the entry node to it"
                if data is not None and data.role == "main"
                else "delegate to it from the main agent"
            )
            a.error(
                "unreachable_agent",
                f"agent {node_id!r} is not reachable from the entry; {hint}",
                node_id=node_id,
            )


def _check_models(a: Analysis) -> None:
    """에이전트마다 model 간선이 정확히 하나다(D28). 없거나 둘 이상이면 model_count 이고 표식은
    에이전트에 붙는다(둘 이상이면 넘치는 간선도 함께 가리킨다). 종류가 틀린 간선은 invalid_edge_kind
    가 이미 봤으므로 모델 노드로 가는 것만 센다."""
    for node_id in a.of_type("agent"):
        edges = [e for e in a.out_edges(node_id, "model") if a.nodes[e.target].type == "model"]
        if not edges:
            a.error(
                "model_count",
                f"agent {node_id!r} needs exactly one model node; attach one with a model edge",
                node_id=node_id,
            )
        for extra in edges[1:]:
            a.error(
                "model_count",
                f"agent {node_id!r} can attach only one model node",
                node_id=node_id,
                edge_id=extra.id,
            )


def _check_attachments(a: Analysis) -> None:
    """메인의 memory 간선은 하나까지다. 닿는 에이전트에 붙지 않은 model·tools·memory·guardrail·
    knowledge 는 경고다."""
    if a.main is not None:
        for extra in a.out_edges(a.main, "memory")[1:]:
            a.error(
                "memory_count", "the main agent can attach only one memory node", edge_id=extra.id
            )
    attached: set[str] = set()
    for node_id in a.reachable:
        for kind in ATTACHABLE:
            attached.update(a.targets(node_id, kind))
    # 판정 모델 노드는 간선 없이 메인이 붙인 guardrail 노드의 규칙이 가리킨다(P43 7절)
    attached.update(judge.model for _node_id, judge in a.judges())
    for node_type in ATTACHABLE:
        for node_id in a.of_type(node_type):
            if node_id not in attached:
                a.warn(
                    "unattached_node",
                    f"{node_type} node {node_id!r} is not attached to the entry or a reachable "
                    "agent, so it is ignored",
                    node_id=node_id,
                )


def _check_judges(a: Analysis) -> None:
    """injection_check 의 판정 모델은 같은 그래프의 모델 노드다(`unknown_judge_model`). 메인이 붙인
    guardrail 노드 가운데 판정은 하나까지다(`judge_count`). 둘째부터 표식이 붙는다(P43 7절)."""
    for node_id, data in a.nodes_of("guardrail", GuardrailData):
        judge = data.judge
        if judge is None:
            continue
        node = a.nodes.get(judge.model)
        if node is None or node.type != "model":
            a.error(
                "unknown_judge_model",
                _missing_ref(
                    f"guardrail node {node_id!r}",
                    "judge model",
                    judge.model,
                    choose="a judge model",
                    where="is not a model node of this graph",
                ),
                node_id=node_id,
            )
    for node_id, _judge in a.judges()[1:]:
        a.error(
            "judge_count",
            f"guardrail node {node_id!r}: only one injection_check rule is allowed in a graph",
            node_id=node_id,
        )


def _missing_ref(
    node: str, what: str, value: str, *, choose: str, where: str = "does not exist in this tenant"
) -> str:
    """참조가 닿지 않을 때의 문구. 자리가 비어 있으면 "고르라" 이고 차 있으면 "그런 것이 없다" 다.
    provider·도구 소스·Database·Knowledge Base 네 검사가 같은 규칙을 쓰므로 여기 한 곳에서 적는다.
    `node` 는 표식이 붙는 노드를 가리키는 말(예: `tools node 'tools-1'`)이고 `choose` 는 무엇을
    고르라고 할지다. 문구는 캔버스 검증(ui/src/lib/agentGraph.ts)이 글자대로 따라간다."""
    if not value:
        return f"{node}: choose {choose}"
    return f"{node}: {what} {value!r} {where}"


def _check_providers(a: Analysis, ctx: GraphContext, key_ok: Callable[[LLMProvider], bool]) -> None:

    def check(node_id: str, name: str, *, temperature: float | None, what: str) -> None:
        p = ctx.providers.get(name)
        if p is None:
            a.error(
                "unknown_provider",
                _missing_ref(
                    f"model {node_id!r}",
                    what,
                    name,
                    choose=f"a {what}",
                    where="is not registered in Platform Settings",
                ),
                node_id=node_id,
            )
            return
        if not key_ok(p):
            a.error(
                "provider_no_key",
                f"model {node_id!r}: {what} {name!r} has no API key Secret (api_key_secret)",
                node_id=node_id,
            )
        if p.kind == "anthropic" and temperature is not None:
            a.warn(
                "unsupported_param",
                f"model {node_id!r}: temperature is ignored for anthropic {what} {name!r}",
                node_id=node_id,
            )

    # provider 이슈의 표식은 모델 노드에 붙는다. 붙지 않은 모델 노드도 같은 규칙이다
    for node_id, data in a.nodes_of("model", ModelData):
        check(node_id, data.provider, temperature=data.temperature, what="provider")
        if data.fallback is not None:
            check(
                node_id,
                data.fallback.provider,
                temperature=data.temperature,
                what="fallback provider",
            )


def _check_agent_names(a: Analysis) -> None:
    names: dict[str, str] = {}
    for node_id, data in a.nodes_of("agent", AgentData):
        if data.name in names:
            a.error(
                "duplicate_agent_name",
                f"agent name {data.name!r} is already used by node {names[data.name]!r}",
                node_id=node_id,
            )
        names.setdefault(data.name, node_id)


def _check_sources(a: Analysis, ctx: GraphContext) -> None:
    ts = ctx.tenant_settings
    for node_id, data in a.nodes_of("tools", ToolsData):
        pool = ctx.source_pool(data.source.kind)
        if data.source.id not in pool:
            a.error(
                "unknown_source",
                _missing_ref(
                    f"tools node {node_id!r}",
                    data.source.kind,
                    data.source.id,
                    choose=f"a {data.source.kind}",
                ),
                node_id=node_id,
            )
            continue
        if pool[data.source.id].state != "deployed":
            # 도구 소스는 배포된 MCP Server·Gateway 만 받는다 (D42). 정의만 있거나 실패한 것은
            # URL 이 없다
            label = "gateway" if data.source.kind == "gateway_instance" else "mcp server"
            a.error(
                "source_not_deployed",
                f"tools node {node_id!r}: {label} {data.source.id} is "
                f"{pool[data.source.id].state}, not deployed",
                node_id=node_id,
            )
            continue
        if data.source.kind == "gateway_instance" and gateway_auth_kind(ctx) == "oauth":
            # vMCP 는 oidc 모드에서 client credentials 로만 부를 수 있다 (Cedar 의 client id 정책)
            if ts.agent_oauth is None:
                a.error(
                    "agent_oauth_missing",
                    f"tools node {node_id!r}: the platform runs in oidc mode, so Tenant Settings "
                    "agent_oauth (client credentials) is required for a Gateway source",
                    node_id=node_id,
                )
            elif ts.agent_oauth.client_id not in ts.agent_client_ids:
                a.error(
                    "agent_client_id_not_allowed",
                    f"tools node {node_id!r}: agent_oauth.client_id {ts.agent_oauth.client_id!r} "
                    "is not in Tenant Settings agent_client_ids, so the vMCP would reject it",
                    node_id=node_id,
                )


def _check_memory(a: Analysis, ctx: GraphContext) -> None:
    """memory 노드마다의 규칙(D26, D33, D34 F5). Database 가 테넌트에 있고 postgres 또는 redis 여야
    하며, redis 는 verify 가 남긴 `capabilities.checkpointer` 가 `ready` 여야 하고(Redis 7 이나
    verify 전에는 `redis_modules_missing`) db 번호가 0 이어야 한다. RediSearch 가 `db != 0` 에서
    인덱스를 못 만들어 체크포인터도 장기 기억도 기동에서 죽기 때문이다(D33 의 기존 구멍)."""
    for node_id, data in a.nodes_of("memory", MemoryData):
        ds = ctx.data_services.get(data.checkpointer.data_service_id)
        if ds is None:
            a.error(
                "unknown_data_service",
                _missing_ref(
                    f"memory node {node_id!r}",
                    "data service",
                    data.checkpointer.data_service_id,
                    choose="a postgres or redis data service",
                ),
                node_id=node_id,
            )
        elif ds.kind not in ("postgres", "redis"):
            a.error(
                "invalid_data_service_kind",
                f"memory node {node_id!r}: data service {ds.name!r} is {ds.kind}; "
                "the checkpointer needs postgres or redis",
                node_id=node_id,
            )
        elif ds.kind == "redis":
            capability = ds.capabilities.get("checkpointer")
            if capability != "ready":
                detail = REDIS_CHECKPOINTER_DETAIL.get(capability, REDIS_CHECKPOINTER_DETAIL[None])
                a.error(
                    "redis_modules_missing",
                    f"memory node {node_id!r}: data service {ds.name!r}: {detail}",
                    node_id=node_id,
                )
            if (ds.db or 0) != 0:
                a.error(
                    "redis_db_not_zero",
                    f"memory node {node_id!r}: data service {ds.name!r} uses db {ds.db}; "
                    "RediSearch cannot create an index outside db 0, so the pod would fail to "
                    "start; register the Redis with db 0",
                    node_id=node_id,
                )


def _check_long_term(a: Analysis) -> None:
    """장기 기억을 켠 memory 노드의 규칙이다(D34). 켜면 세션 밖 저장소와 파일 도구 둘 다 필요하다.

    저장소는 그 memory 노드가 고른 Database 이므로 노드가 메인에 붙어 있어야 하고(안 붙으면
    compile 이 `memory` 를 null 로 내어 런타임이 `long_term_memory_missing` 으로 기동을 막는다),
    `/memories/` 아래를 읽고 쓰는 것은 파일 도구 7종이라 `builtin_tools.filesystem` 이 켜져 있어야
    한다. 보관 기간이 1 이상인 것은 `LongTerm` 이 본다(어긋나면 `invalid_node_data` 다).
    """
    attached = set(a.targets(a.main, "memory")) if a.main is not None else set()
    main = a.agent(a.main) if a.main is not None else None
    for node_id, data in a.nodes_of("memory", MemoryData):
        if data.long_term is None:
            continue
        if node_id not in attached:
            a.error(
                "long_term_needs_memory",
                f"memory node {node_id!r}: long-term memory is on, but the node is not attached "
                "to the main agent, so there is no store to keep it in; attach it with a memory "
                "edge or turn long-term memory off",
                node_id=node_id,
            )
        if main is not None and not main.builtin_tools.filesystem:
            a.error(
                "long_term_filesystem_disabled",
                f"memory node {node_id!r}: long-term memory is on, but the main agent has "
                "builtin_tools.filesystem off, so the agent has no tool to read or write "
                "/memories; turn the file tools on or turn long-term memory off",
                node_id=node_id,
            )


def _check_code_execution(a: Analysis) -> None:
    """코드 실행의 규칙이다(P40 9절). 파일 Tool 이 같은 작업 공간을 쓰고 첨부와 Skill 도 파일
    Tool 로 읽으므로 메인의 `builtin_tools.filesystem` 이 켜져 있어야 한다. 메인의 턴 제한이 명령
    하나의 최대 시간보다 짧으면 그 턴이 명령을 끝까지 기다리지 못하므로 경고한다."""
    main = a.agent(a.main) if a.main is not None else None
    if main is None or main.code_execution is None:
        return
    if not main.builtin_tools.filesystem:
        a.error(
            "code_execution_filesystem_disabled",
            "code execution is on, but the main agent has builtin_tools.filesystem off, so the "
            "file tools cannot use the workspace; turn the file tools on or code execution off",
            node_id=a.main,
        )
    if main.turn_timeout_seconds < SANDBOX_COMMAND_MAX_SECONDS:
        a.warn(
            "code_execution_turn_timeout_short",
            f"the main agent's turn limit ({main.turn_timeout_seconds}s) is below the longest "
            f"command ({SANDBOX_COMMAND_MAX_SECONDS}s), so a long command is cut at the turn limit",
            node_id=a.main,
        )


def code_execution_of(spec: AgentSpec) -> CodeExecution | None:
    """리비전의 메인 에이전트 노드가 켠 코드 실행이다(P40 9절). 꺼졌거나 노드 형식이 틀렸으면
    None 이다. 플랫폼의 샌드박스 열기(`services/sandboxes.py` `code_execution_on`), 요구 기능,
    컴파일, 배포 검사가 이것 하나를 읽는다."""
    for node in spec.graph.nodes:
        if node.type != "agent" or node.data.get("role") != "main":
            continue
        try:
            return AgentData.model_validate(node.data).code_execution
        except ValidationError:
            return None
    return None


def _check_memory_required(a: Analysis, spec: AgentSpec) -> None:
    """그래프 전체의 규칙이다. 메인에 memory 가 없는데 승인·분당 한도·일당 예산이 켜져 있으면
    `memory_required` 다(공유 카운터와 체크포인터가 있어야 하기 때문이다). 노드 하나를 가리키는
    규칙이 아니라 표식이 붙는 노드도 없다. 상태를 파드 안에 두는 미리 보기는 이 규칙을 보지 않는다
    (`GraphContext.in_pod_state`)."""
    if a.main is None or a.main_has_memory:
        return
    reasons: list[str] = []
    entry = a.typed(a.entry, EntryData) if a.entry is not None else None
    if entry is not None and entry.rate_limit is not None:
        reasons.append("entry.rate_limit")
    if spec.service.budget.daily_tokens is not None:
        reasons.append("service.budget.daily_tokens")
    for agent_id in sorted(a.reachable):
        for tools_id, data in a.typed_targets(agent_id, "tools", ToolsData):
            if data.approval:
                reasons.append(f"tools {tools_id!r} approval")
    if reasons:
        a.error(
            "memory_required",
            "a memory node (checkpointer and shared counters) is required by: "
            + ", ".join(reasons),
        )


def _check_knowledge(a: Analysis, ctx: GraphContext, key_ok: Callable[[LLMProvider], bool]) -> None:
    """knowledge 노드마다의 규칙(D32). Knowledge Base 가 테넌트에 있어야 하고 삭제 중이 아니어야
    하며 임베딩 provider 에 키가 있어야 한다(stub 면제). 도구 이름은 deepagents 내장 이름이 아니어야
    한다. 인덱싱된 문서가 없거나 실패한 문서가 있으면 경고다. 표식은 전부 knowledge 노드이고, 붙지
    않은 노드도 같은 규칙이다(모델 노드와 같다)."""
    for node_id, data in a.nodes_of("knowledge", KnowledgeData):
        if data.tool_name in RESERVED_TOOL_NAMES:
            a.error(
                "reserved_tool_name",
                f"knowledge node {node_id!r}: tool name {data.tool_name!r} is a built-in tool of "
                "the runtime; choose another name",
                node_id=node_id,
            )
        kb = ctx.knowledge_bases.get(data.knowledge_base_id)
        if kb is None:
            a.error(
                "unknown_knowledge_base",
                _missing_ref(
                    f"knowledge node {node_id!r}",
                    "knowledge base",
                    data.knowledge_base_id,
                    choose="a knowledge base",
                ),
                node_id=node_id,
            )
            continue
        if kb.deleting_at is not None:
            a.error(
                "knowledge_base_deleting",
                f"knowledge node {node_id!r}: knowledge base {kb.name!r} is being deleted",
                node_id=node_id,
            )
        provider = ctx.providers.get(kb.embedding.provider)
        if provider is None:
            a.error(
                "unknown_provider",
                f"knowledge node {node_id!r}: embedding provider {kb.embedding.provider!r} of "
                f"knowledge base {kb.name!r} is not registered in Platform Settings",
                node_id=node_id,
            )
        elif not provider.supports_embedding:
            # 만든 뒤 provider 의 kind 가 임베딩 없는 쪽(anthropic, openrouter, zai)으로 바뀐 경우다
            a.error(
                "embedding_not_supported",
                f"knowledge node {node_id!r}: embedding provider {provider.name!r} of knowledge "
                f"base {kb.name!r} is {provider.kind}, which has no embedding API",
                node_id=node_id,
            )
        elif not key_ok(provider):
            a.error(
                "provider_no_key",
                f"knowledge node {node_id!r}: embedding provider {provider.name!r} of knowledge "
                f"base {kb.name!r} has no API key Secret (api_key_secret)",
                node_id=node_id,
            )
        status = kb.status or KnowledgeStatus()
        if status.indexed == 0:
            a.warn(
                "knowledge_base_empty",
                f"knowledge node {node_id!r}: knowledge base {kb.name!r} has no indexed document "
                "yet, so searches return nothing",
                node_id=node_id,
            )
        if status.failed:
            a.warn(
                "knowledge_base_failed",
                f"knowledge node {node_id!r}: knowledge base {kb.name!r} has {status.failed} "
                "failed document(s)",
                node_id=node_id,
            )


def _check_knowledge_tool_names(a: Analysis) -> None:
    """에이전트마다의 규칙이다(D32). 한 에이전트에 붙은 knowledge 노드들이 같은 도구 이름을 내면
    런타임에서 하나가 다른 하나를 덮으므로 `duplicate_tool_name` 이다."""
    for agent_id in a.of_type("agent"):
        first: dict[str, str] = {}
        for node_id, data in a.typed_targets(agent_id, "knowledge", KnowledgeData):
            if data.tool_name in first:
                a.error(
                    "duplicate_tool_name",
                    f"agent {agent_id!r}: knowledge nodes {first[data.tool_name]!r} and "
                    f"{node_id!r} both expose the tool {data.tool_name!r}",
                    node_id=node_id,
                )
            first.setdefault(data.tool_name, node_id)


def _check_skills(a: Analysis, spec: AgentSpec, ctx: GraphContext) -> None:
    """Skill 규칙과 문구는 `agent_skills` 한 곳이다(D64). 핀 목록도 거기서 읽고, data 형식이 틀린
    노드는 `invalid_node_data` 가 이미 막았으므로 뺀다. 결과를 이슈로 옮긴다."""
    main = a.agent(a.main) if a.main is not None else None
    parsed = {i for i, _ in a.nodes_of("agent", AgentData)}
    agents = [s for s in attached_skills(spec) if s.node_id in parsed]
    filesystem = main is None or main.builtin_tools.filesystem
    for f in check_skills(agents, filesystem=filesystem, revisions=ctx.skill_revisions):
        a.issues.append(Issue(f.code, f.message, f.level, node_id=f.node_id))


def analyze_structure(spec: AgentSpec) -> Analysis:
    """문맥 없이 그래프만으로 되는 검사. 노드 형식·개수·간선·순환·도달 가능성이라 provision 의
    접근자(used_providers·knowledge_providers)도 같은 결과 위에서 돈다."""
    a = Analysis(nodes={}, order=[], data={}, edges=[])
    _parse_nodes(spec.graph, a)
    _check_counts(a)
    _check_edges(spec.graph, a)
    _check_delegation_cycles(a)
    _check_reachability(a)
    return a


def analyze(spec: AgentSpec, ctx: GraphContext) -> Analysis:
    a = analyze_structure(spec)
    # 모델 노드와 Knowledge Base 임베딩이 같은 provider 를 쓰면 Secret 을 한 번만 읽는다
    key_ok = key_checker(ctx)
    _check_models(a)
    _check_judges(a)
    _check_attachments(a)
    _check_agent_names(a)
    _check_providers(a, ctx, key_ok)
    _check_sources(a, ctx)
    _check_memory(a, ctx)
    _check_long_term(a)
    _check_code_execution(a)
    if not ctx.in_pod_state:
        _check_memory_required(a, spec)
    _check_knowledge(a, ctx, key_ok)
    _check_knowledge_tool_names(a)
    _check_skills(a, spec, ctx)
    return a


def validate(spec: AgentSpec, ctx: GraphContext) -> list[Issue]:
    """docs/guide/agents.md 1절의 규칙 전부. 오류와 경고를 함께 돌려주고 순서는 검사 순서다."""
    return analyze(spec, ctx).issues


def knowledge_providers(
    spec: AgentSpec, knowledge_bases: dict[str, KnowledgeBase]
) -> list[tuple[int, str]]:
    """compile 의 `knowledge[]` 순서 n 과 그 항목의 임베딩 provider 이름. provision 이 임베딩 키
    env(`DAEGAM_KNOWLEDGE_<n>_API_KEY`)를 렌더할 때 쓴다. 문맥 없이 그래프와 Knowledge Base 목록만
    읽고, 테넌트에 없는 Knowledge Base 는 validate 가 이미 막았으므로 건너뛴다."""
    a = analyze_structure(spec)
    out: list[tuple[int, str]] = []
    for n, (_node_id, data) in enumerate(a.attached_knowledge_nodes()):
        kb = knowledge_bases.get(data.knowledge_base_id)
        if kb is not None:
            out.append((n, kb.embedding.provider))
    return out


def used_providers(spec: AgentSpec, knowledge_bases: dict[str, KnowledgeBase]) -> list[str]:
    """그래프가 쓰는 provider 이름. 메인이 붙인 모델 노드의 provider·폴백이 먼저이고 나머지 모델
    노드는 graph.nodes 순서이며, 닿는 에이전트가 붙인 knowledge 노드의 임베딩 provider 가 그 뒤에
    중복 없이 온다. 문맥 없이 노드 data 만 읽으므로 provision 이 복사할 Secret 을 고를 때 쓴다.
    형식이 틀린 노드는 validate 가 이미 막았으므로 건너뛴다.

    `knowledge_bases` 는 필수다. 기본값을 두면 인자를 잊은 호출이 오류 없이 임베딩 provider 가
    빠진 목록을 받고, 그 목록이 곧 복사할 Secret 목록이라 조용히 틀린다."""
    models: dict[str, ModelData] = {}
    mains: set[str] = set()
    for node in spec.graph.nodes:
        if node.type == "model":
            try:
                models[node.id] = ModelData.model_validate(node.data)
            except ValidationError:
                continue
        elif node.type == "agent" and node.data.get("role") == "main":
            mains.add(node.id)
    first = [e.target for e in spec.graph.edges if e.kind == "model" and e.source in mains]
    order = [i for i in first if i in models] + [i for i in models if i not in first]
    names: list[str] = []
    for model_id in dict.fromkeys(order):
        data = models[model_id]
        for name in (data.provider, data.fallback.provider if data.fallback else None):
            if name and name not in names:
                names.append(name)
    for _n, name in knowledge_providers(spec, knowledge_bases):
        if name not in names:
            names.append(name)
    return names


def compiled_model_providers(spec: AgentSpec) -> list[str]:
    """compile 이 agent.json 의 모델 자리(main.model·main.fallback·subagents[].model, 판정 모델)에
    싣는 provider 이름이다. 문맥 없이 그래프만 읽고 compile 과 같은 규칙을 따른다. 떠 있는 모델
    노드와 서브에이전트·판정 모델의 폴백은 문서에 실리지 않으므로 세지 않는다. 문서 기능을 셀 때
    붙이는 함수가 보는 자리와 같게 하려는 것이다(`agent_features`)."""
    a = analyze_structure(spec)
    names: list[str] = []
    if a.main is not None and (main := a.model_of(a.main)) is not None:
        names.append(main.provider)
        if main.fallback is not None:
            names.append(main.fallback.provider)
    for node_id, data in a.nodes_of("agent", AgentData):
        if data.role == "sub" and (model := a.model_of(node_id)) is not None:
            names.append(model.provider)
    if (judge := a.judge_model()) is not None:
        names.append(judge.provider)
    return list(dict.fromkeys(names))


def uses_injection_check(spec: AgentSpec) -> bool:
    """메인이 붙인 guardrail 노드에 판정 모델(injection_check)이 있는가(P43 7절). 문서 기능을
    셀 때 쓴다(`agent_features`)."""
    return bool(analyze_structure(spec).judges())


def long_term_memory(spec: AgentSpec) -> tuple[str, LongTerm] | None:
    """장기 기억이 켜져 있으면 (그 memory 노드가 고른 Database id, 설정), 아니면 None 이다(D34).

    문맥 없이 그래프만 읽으므로 provision 이 `daegam_store` 스키마를 만들지, 삭제가 어느 Database
    에 붙을지 정할 때 쓴다. 메인에 붙지 않은 memory 노드는 저장소가 아니므로 보지 않는다
    (`_check_long_term` 이 저장·Deploy 에서 이미 막았다)."""
    a = analyze_structure(spec)
    if a.main is None:
        return None
    for _node_id, data in a.typed_targets(a.main, "memory", MemoryData)[:1]:
        if data.long_term is not None:
            return data.checkpointer.data_service_id, data.long_term
    return None


def references_knowledge_base(spec: AgentSpec, kb_id: str) -> bool:
    """그래프에 이 Knowledge Base 를 가리키는 knowledge 노드가 있는가. 붙었는지는 보지 않는다
    (삭제 가드는 노드가 남아 있기만 해도 막는다)."""
    return any(
        node.type == "knowledge" and node.data.get("knowledge_base_id") == kb_id
        for node in spec.graph.nodes
    )


def _node_sources(spec: AgentSpec) -> Iterator[tuple[str, Any, Any]]:
    """tools 노드마다 (노드 id, source 의 kind, source 의 id). source 가 객체가 아닌 노드는 건너뛰고
    값의 꼴은 보지 않는다."""
    for node in spec.graph.nodes:
        source = node.data.get("source") if node.type == "tools" else None
        if isinstance(source, dict):
            yield node.id, source.get("kind"), source.get("id")


def _tool_sources(spec: AgentSpec) -> Iterator[tuple[str, str]]:
    """tools 노드가 가리키는 도구 소스 (종류, id). 꼴이 맞지 않는 source 는 건너뛴다."""
    for _node_id, kind, source_id in _node_sources(spec):
        if isinstance(kind, str) and isinstance(source_id, str) and source_id:
            yield kind, source_id


def newer_sources(spec: AgentSpec) -> dict[str, str]:
    """이 판이 모르는 종류(뒤 판이 더한 것, P46 의 `agent`)의 소스를 가리키는 tools 노드다. 노드 id
    → 종류다. 저장과 배포의 검증은 그런 노드를 `invalid_node_data` 로 막으므로 배포된 리비전에서는
    뒤 판이 배포한 것뿐이다."""
    return {
        node_id: kind
        for node_id, kind, _source_id in _node_sources(spec)
        if isinstance(kind, str) and kind not in SOURCE_KINDS
    }


def without_nodes[S: AgentSpec](spec: S, node_ids: Collection[str]) -> S:
    """node_ids 의 노드와 그 노드에 닿는 간선을 뺀 사본이다. 그래프는 깊이 복사한다."""
    graph = spec.graph.model_copy(deep=True)
    graph.nodes = [n for n in graph.nodes if n.id not in node_ids]
    graph.edges = [e for e in graph.edges if e.source not in node_ids and e.target not in node_ids]
    return spec.model_copy(update={"graph": graph})


def without_newer_sources[S: AgentSpec](spec: S) -> S:
    """`newer_sources` 의 노드와 그 노드에 닿는 간선을 뺀 리비전이다 (P46 A0). 뒤 판이 배포한
    리비전을 이 판이 컴파일하면 그 소스의 Tool 없이 파드가 뜬다. 그런 노드가 없으면 spec
    그대로다."""
    newer = newer_sources(spec)
    return without_nodes(spec, newer) if newer else spec


def references_tool_source(spec: AgentSpec, kind: str, source_id: str) -> bool:
    """그래프에 이 도구 소스(`mcp_instance`·`gateway_instance`)를 가리키는 tools 노드가 있는가.
    스토어 질의 `referencing_tool_source` 와 같은 조건을 문서 하나 안에서 본다. 배포가 핀한
    리비전이 붙드는지 가릴 때 쓴다."""
    return (kind, source_id) in set(_tool_sources(spec))


def tool_sources(spec: AgentSpec) -> list[tuple[str, str]]:
    """tools 노드가 가리키는 도구 소스의 (종류, id) (정렬, 중복 없음). 턴 위임이 토큰을 줄 대상을
    여기서 고른다 (P53 2절)."""
    return sorted(set(_tool_sources(spec)))


def turn_token_sources(spec: AgentSpec, *, trusts_platform: bool) -> list[tuple[str, str]]:
    """턴 위임이 토큰을 줄 수 있는 도구 소스 (P53 2절). MCP Server 는 토큰 인증의 어느
    모드에서나, Gateway 는 데이터 플레인이 플랫폼을 믿을 때(P53b 6.1절)만 받는다. IdP 를 믿는
    oidc 모드의 Gateway 소스는 이미지와 상관없이 Agent 의 자격으로 부른다."""
    return [s for s in tool_sources(spec) if trusts_platform or s[0] == "mcp_instance"]


def mcp_server_sources(spec: AgentSpec) -> list[str]:
    """tools 노드가 가리키는 MCP Server 소스의 id (정렬, 중복 없음). Agent 가 소스마다 낼 서비스
    토큰의 표를 정한다 (P50 9절)."""
    return sorted({sid for kind, sid in _tool_sources(spec) if kind == "mcp_instance"})


def uses_gateway_source(spec: AgentSpec) -> bool:
    """tools 노드 중 Gateway 소스가 있는가. IdP 를 믿는 oidc 모드에서는 agent_oauth env 를, 플랫폼을
    믿는 모드에서는 gateway_token 을 넣을지 정한다."""
    return any(kind == "gateway_instance" for kind, _id in _tool_sources(spec))


# -- 컴파일 --------------------------------------------------------------------------


def schema_version(knowledge: bool, long_term: bool) -> str:
    """agent.json 의 schema_version. 사다리다(D32, D34).

    `"1"` 은 둘 다 없는 P15 전과 같은 문서, `"2"` 는 knowledge 키 셋을 가진 문서, `"3"` 은
    knowledge 키 셋(없으면 빈 목록)과 `memory.long_term` 을 함께 가진 문서다. 직교하는 두 기능에
    선형 버전을 쓰는 값이라 `"3"` 이 knowledge 키 셋을 항상 포함한다(스키마의 `allOf` 사다리 1이
    `"2"`·`"3"` 에서 그 키 셋을 필수로 두기 때문이다). 켜진 기능이 없으면 낮은 번호를 내어 옛
    런타임 이미지가 그대로 돈다."""
    if long_term:
        return "3"
    return "2" if knowledge else "1"


@cache
def schema_validator() -> Draft202012Validator:
    schema = agent_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def check_schema(doc: dict[str, Any]) -> list[str]:
    """스키마 위반 메시지 목록. 비어 있으면 통과다."""
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(schema_validator().iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def _model_doc(
    provider: LLMProvider, binding: ProviderBinding, model: str | None, data: ModelData
) -> dict[str, Any]:
    """docs/guide/agents.md 3절 모델 표(D26). 키 값은 없고 env 이름과 base_url 은 렌더와 함께 읽는
    자리다(`provider_binding`, P35 12절, P38 6절). 폴백도 같은 params 다. kind 는 SDK 계열이라
    런타임은 openrouter·zai 를 모르고, 업체 선호(T19)는 붙이는 함수가 싣는다(`agent_features`)."""
    return {
        "provider": {
            "name": provider.name,
            "kind": provider.sdk_kind,
            "env_key": binding.env,
            "base_url": binding.base_url,
        },
        "name": model or provider.effective_model,
        "params": {
            "max_output_tokens": data.max_output_tokens,
            "effort": data.reasoning.effort,
            "temperature": data.temperature,
        },
    }


def _node_model_doc(
    model: ModelData, instance: Agent, ctx: GraphContext, *, effort: Effort | None = None
) -> dict[str, Any]:
    """모델 노드 하나를 그 provider 의 자리와 함께 모델 객체로 푼다(`_model_doc`). 메인,
    서브에이전트, 판정 모델이 쓴다. effort 를 주면 노드의 추론 강도 대신 그것이다(판정 모델의
    `JUDGE_EFFORT`)."""
    provider = ctx.providers[model.provider]
    binding = provider_binding(instance, provider, ctx.llm_gateway_url)
    doc = _model_doc(provider, binding, model.model, model)
    if effort is not None:
        doc["params"]["effort"] = effort
    return doc


def _limits(data: AgentData) -> dict[str, Any]:
    return {
        "max_tool_calls_per_turn": data.max_tool_calls_per_turn,
        "turn_timeout_seconds": data.turn_timeout_seconds,
        "retries": data.retries,
    }


def _response_format(data: AgentData) -> dict[str, Any]:
    if isinstance(data.response_format, JsonSchemaFormat):
        return {"kind": "json_schema", "schema": data.response_format.json_schema}
    return {"kind": "text"}


def _compaction(data: AgentData) -> dict[str, Any]:
    c = data.context.compaction
    return {
        "trigger": {"kind": c.trigger.kind, "value": c.trigger.value},
        "keep": {"kind": c.keep.kind, "value": c.keep.value},
    }


def gateway_auth_kind(ctx: GraphContext) -> str:
    """Gateway(vMCP) 소스에 붙는 인증. oauth(IdP 를 믿는 oidc 모드) | bearer(local 모드와 플랫폼을
    믿는 oidc 모드) | none(header 모드)."""
    mode = ctx.auth_mode
    if mode is None:
        return "oauth" if ctx.token_auth else "none"
    if mode == "oidc":
        return "bearer" if ctx.trusts_platform else "oauth"
    return "bearer" if mode == "local" else "none"


def _tool_auth(kind: SourceKind, ctx: GraphContext, source_id: str) -> dict[str, Any]:
    """MCP Server 는 모든 모드에서 플랫폼이 낸 서비스 토큰(D41)이고, Gateway 는 모드가 정한다.
    MCP Server 의 토큰 env 는 컴파일을 받는 파드의 표가 정하고, 표가 없거나 표에 없는 소스는 옛
    단일 토큰이다 (P50 9절, `mcp_token_envs`)."""
    if kind == "mcp_instance":
        env = (ctx.mcp_token_envs or {}).get(source_id, MCP_TOKEN_ENV)
        return {"kind": "bearer", "secret_env": env}
    auth = gateway_auth_kind(ctx)
    if auth == "oauth":
        oauth = ctx.tenant_settings.agent_oauth
        assert oauth is not None  # validate 가 agent_oauth_missing 으로 막았다
        return {
            "kind": "oauth_client_credentials",
            "client_id": oauth.client_id,
            "token_url": oauth.token_url,
            "scope": oauth.scope,
            "secret_env": AGENT_CLIENT_SECRET_ENV,
        }
    if auth == "bearer":
        return {"kind": "bearer", "secret_env": GATEWAY_TOKEN_ENV}
    return {"kind": "none"}


def _tool_doc(ref: str, data: ToolsData, ctx: GraphContext) -> dict[str, Any]:
    if data.source.kind == "gateway_instance":
        gw = ctx.gateway_instances[data.source.id]
        source = {"kind": "gateway_instance", "id": gw.id, "name": gw.name}
        assert gw.k8s is not None  # _check_sources 가 deployed 만 통과시킨다
        url = vmcp_cluster_url(gw.k8s.name, gw.k8s.namespace)
    else:
        mi = ctx.mcp_instances[data.source.id]
        source = {"kind": "mcp_instance", "id": mi.id, "name": mi.name}
        assert mi.k8s is not None  # _check_sources 가 deployed 만 통과시킨다
        url = proxy_service_url(mi.k8s.name, mi.k8s.namespace)
    return {
        "ref": ref,
        "source": source,
        "url": url,
        "transport": "streamable_http",
        "auth": _tool_auth(data.source.kind, ctx, data.source.id),
        "allow": list(data.allow_tools),
        "approval": {t: list(d) for t, d in data.approval.items()},
        "timeouts": dict(data.timeouts),
    }


def _guardrail_doc(
    data: GuardrailData, a: Analysis, instance: Agent, ctx: GraphContext
) -> dict[str, Any]:
    """guardrail 노드 하나다. injection_check 는 판정 모델 노드를 추론 없이(`JUDGE_EFFORT`) 부를
    모델 객체로 풀어 싣는다(P43 7절). 폴백은 싣지 않는다."""
    rules = []
    for r in data.rules:
        value = r.value
        if r.kind == "pii_mask":
            value = {k: v for k, v in r.value.items() if v is not None}
        elif r.kind == RULE_INJECTION_CHECK and (judge := data.judge) is not None:
            model = a.typed(judge.model, ModelData)
            assert model is not None  # validate 가 unknown_judge_model 로 막았다
            value = {
                "model": _node_model_doc(model, instance, ctx, effort=JUDGE_EFFORT),
                "max_chars": judge.max_chars,
                "max_calls": judge.max_calls,
            }
        rules.append({"kind": r.kind, "value": value})
    return {
        "position": data.position,
        "rules": rules,
        "on_violation": data.on_violation,
        "message": data.message,
    }


def default_knowledge_description(kb: KnowledgeBase) -> str:
    """노드 description 이 비어 있을 때의 도구 설명. 런타임이 뒤에 min_score 안내 문장을 잇는다."""
    return f"Search the knowledge base '{kb.display_name}' for passages relevant to a query."


def _knowledge_doc(
    ref: str, n: int, data: KnowledgeData, instance: Agent, ctx: GraphContext
) -> dict[str, Any]:
    """D32 knowledge 항목. 키 값은 없고 임베딩 키 env 이름(`DAEGAM_KNOWLEDGE_<n>_API_KEY`, stub 은
    null)과 DSN env 이름(memory 와 같은 인스턴스 바인딩)만 가리킨다."""
    kb = ctx.knowledge_bases[data.knowledge_base_id]
    provider = ctx.providers[kb.embedding.provider]
    return {
        "ref": ref,
        "base": {"id": kb.id, "name": kb.name},
        "store": {
            "kind": "postgres",
            "dsn_env": dsn_env(instance, kb.data_service_id),
            "table": kb.table,
            "dimensions": kb.embedding.dimensions,
        },
        "embedding": {
            "provider": {
                "name": provider.name,
                "kind": provider.kind,
                "env_key": None if provider.is_stub else knowledge_key_env(n),
                "base_url": provider_binding(instance, provider, ctx.llm_gateway_url).base_url,
            },
            "model": kb.embedding.model,
        },
        "tool": {
            "name": data.tool_name,
            "description": data.description or default_knowledge_description(kb),
            "top_k": data.top_k,
            "min_score": data.min_score,
        },
    }


def _knowledge_refs(a: Analysis, agent_id: str, *, with_knowledge: bool) -> dict[str, Any]:
    """에이전트가 붙인 knowledge 노드의 ref. schema_version "2" 에서만 있는 `knowledge` 키이고
    "1" 문서에는 키 자체가 없다."""
    return {"knowledge": a.knowledge_of(agent_id)} if with_knowledge else {}


def _tool_docs(a: Analysis, ctx: GraphContext) -> list[dict[str, Any]]:
    """닿는 에이전트가 붙인 tools 노드를 항목으로. graph.nodes 순서이고 한 노드는 한 번이다.
    서브만 붙인 소스도 여기 있고 `main.tools`·`subagents[].tools` 가 ref 로 가리킨다."""
    used: set[str] = set()
    for agent_id in a.reachable:
        if a.nodes[agent_id].type == "agent":
            used.update(a.targets(agent_id, "tools"))
    return [
        _tool_doc(node_id, data, ctx)
        for node_id, data in a.nodes_of("tools", ToolsData)
        if node_id in used
    ]


def _subagent_docs(
    a: Analysis, instance: Agent, ctx: GraphContext, *, with_knowledge: bool
) -> list[dict[str, Any]]:
    """서브에이전트 항목. graph.nodes 순서이고 메인이 delegate 로 부르는 이름은 `main.subagents`
    가 따로 적는다."""
    docs: list[dict[str, Any]] = []
    for node_id, data in a.nodes_of("agent", AgentData):
        if data.role != "sub":
            continue
        model = a.model_of(node_id)
        assert model is not None  # validate 가 model_count 로 막았다
        docs.append(
            {
                "name": data.name,
                "description": data.description,
                "system_prompt": data.system_prompt,
                "model": _node_model_doc(model, instance, ctx),
                "tools": a.targets(node_id, "tools"),
                **_knowledge_refs(a, node_id, with_knowledge=with_knowledge),
                "limits": _limits(data),
                "interrupt_on": a.approvals(node_id),
            }
        )
    return docs


def _memory_doc(
    a: Analysis, main_id: str, instance: Agent, ctx: GraphContext
) -> dict[str, Any] | None:
    """메인이 붙인 memory 노드 하나. 체크포인터의 종류는 그 Database 의 kind 그대로다(postgres
    또는 redis, D33). memory 가 없으면 null 이다.

    장기 기억은 켜져 있을 때만 `long_term` 키를 낸다(D34). 꺼져 있으면 null 로 두지 않고 키를
    아예 내지 않으므로 `"1"`·`"2"` 문서가 P17 전과 바이트로 같다."""
    for _node_id, data in a.typed_targets(main_id, "memory", MemoryData)[:1]:
        doc: dict[str, Any] = {
            "checkpointer": {
                "kind": ctx.data_services[data.checkpointer.data_service_id].kind,
                "dsn_env": dsn_env(instance, data.checkpointer.data_service_id),
            }
        }
        if data.long_term is not None:
            doc["long_term"] = {"retention_days": data.long_term.retention_days}
        return doc
    return None


def _guardrail_docs(a: Analysis, instance: Agent, ctx: GraphContext) -> list[dict[str, Any]]:
    """메인이 붙인 guardrail 노드(`Analysis.main_guardrails`). 어디에 적용할지는 붙인 자리가 아니라
    노드의 `position`(input·output·tool)이 정한다."""
    return [_guardrail_doc(data, a, instance, ctx) for _node_id, data in a.main_guardrails()]


def _entry_doc(data: EntryData) -> dict[str, Any]:
    return {
        "session_ttl_seconds": data.session_ttl_seconds,
        "max_turns": data.max_turns,
        "rate_limit": (
            {"per_user_per_minute": data.rate_limit.per_user_per_minute}
            if data.rate_limit is not None
            else None
        ),
        "max_concurrent_sessions": data.max_concurrent_sessions,
        "max_input_chars": data.max_input_chars,
        "greeting": data.greeting,
    }


def compile(  # noqa: A001 - 플랜·설계가 부르는 이름이다
    instance: Agent,
    revision: AgentSpec,
    ctx: GraphContext,
) -> dict[str, Any]:
    """검증을 통과한 리비전을 agent.json 으로 펼친다. 오류가 있으면 GraphError 다. 스키마 검사는
    여기 없다. 서비스가 기능 칸(`features`)을 얹은 최종 문서를 검사한다(agent_features, D65).
    순서는 graph.nodes 순서라 같은 입력이면 같은 문서다. knowledge 항목이 하나도 없고 장기 기억도
    꺼져 있으면 schema_version "1" 이고 knowledge 키 셋은 아예 없다. 장기 기억을 켜면 `"3"` 이고
    그때는 knowledge 항목이 없어도 키 셋을 빈 목록으로 낸다. 뒤 판이 더한 종류의 소스를 가리키는
    tools 노드는 빼고 펼친다(`without_newer_sources`, P46 A0). 그 노드가 파드의 시작을 막지 않고,
    상태가 알린다(`services/agent_status.py` `newer_source_warning`)."""
    revision = without_newer_sources(revision)
    a = analyze(revision, ctx)
    if errors_of(a.issues):
        raise GraphError(a.issues)
    assert a.entry is not None and a.main is not None
    entry = a.typed(a.entry, EntryData)
    main = a.agent(a.main)
    main_model = a.model_of(a.main)
    assert entry is not None and main is not None and main_model is not None

    # 지식: 닿는 에이전트가 붙인 knowledge 노드 (graph.nodes 순서). n 이 임베딩 키 env 의 번호이고
    # 항목이 하나라도 있으면 문서가 schema_version "2" 다
    knowledge = [
        _knowledge_doc(node_id, n, data, instance, ctx)
        for n, (node_id, data) in enumerate(a.attached_knowledge_nodes())
    ]
    memory = _memory_doc(a, a.main, instance, ctx)
    has_long_term = memory is not None and "long_term" in memory
    # 사다리 "3" 은 knowledge 키 셋을 항상 포함한다. 장기 기억만 켠 문서도 knowledge 를 빈 목록으로
    # 내야 스키마의 allOf 사다리 1을 지난다(D34)
    has_knowledge = bool(knowledge) or has_long_term
    main_provider = ctx.providers[main_model.provider]
    fallback = None
    if main_model.fallback is not None:
        fb_provider = ctx.providers[main_model.fallback.provider]
        fb_env = provider_binding(instance, fb_provider, ctx.llm_gateway_url)
        fallback = _model_doc(fb_provider, fb_env, main_model.fallback.model, main_model)
    sub_names = [d.name for _i, d in a.typed_targets(a.main, "delegate", AgentData)]
    doc: dict[str, Any] = {
        "schema_version": schema_version(has_knowledge, has_long_term),
        # 정의와 실행이 한 문서라 둘 다 같은 id·이름이다. 런타임 스키마와 감사 수집기가 이 두 키를
        # 그대로 읽으므로 모양은 바꾸지 않는다 (D42)
        "profile": {
            "id": instance.id,
            "name": instance.name,
            "revision": instance.deployed_revision,
        },
        "instance": {"id": instance.id, "name": instance.name},
        "main": {
            "model": _node_model_doc(main_model, instance, ctx),
            "system_prompt": main.system_prompt,
            "compaction": _compaction(main),
            "limits": _limits(main),
            "fallback": fallback,
            "response_format": _response_format(main),
            "language": main.language or None,
            "builtin_tools": {"filesystem": main.builtin_tools.filesystem},
            "tools": a.targets(a.main, "tools"),
            **_knowledge_refs(a, a.main, with_knowledge=has_knowledge),
            "subagents": sub_names,
        },
        "subagents": _subagent_docs(a, instance, ctx, with_knowledge=has_knowledge),
        "tools": _tool_docs(a, ctx),
        **({"knowledge": knowledge} if has_knowledge else {}),
        "memory": memory,
        "guardrails": _guardrail_docs(a, instance, ctx),
        "entry": _entry_doc(entry),
        "service": {
            "budget": {
                "session_tokens": revision.service.budget.session_tokens,
                "daily_tokens": revision.service.budget.daily_tokens,
            },
            "observability": {"record_bodies": revision.service.observability.record_bodies},
        },
        "stub": main_provider.is_stub,
    }
    return doc
