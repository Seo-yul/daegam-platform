"""Tool 규칙의 저장 꼴 (P52 1절).

MCP Server 는 backend 마다 operationId 를 키로(`BackendSelection.tool_rules`), Gateway 는 백엔드
(허브 항목)마다 그 백엔드가 광고하는 원래 이름을 키로(`GatewayBackend.tool_rules`), 허브 `image`
항목은 Tool 이름을 키로(`HubItemBase.tool_rules`) 규칙을 둔다. 서버의 규칙은 리비전의 일부이고
Gateway 의 규칙은 정의의 일부이며 항목은 자기 규칙을 든다.

- `callers` 가 비면 누가 부를지의 규칙이 없다. 바탕 정책이 오늘처럼 적용된다.
  비지 않았는데 어느 객체에서 절을 하나도 짓지 못하면(지운 Agent 만 남았다, oidc Gateway 의 Agent,
  내부 서버의 역할) 그 객체에서 그 Tool 은 닫힌다 (2절).
- 역할은 최소 역할이다. `developer` 는 그 테넌트의 developer 와 tenant-admin 을 받는다.
  platform-admin 의 `*` 소속은 세지 않는다.
- 그룹은 oidc 모드에서만 받는다. Agent 는 id 로 둔다. Agent 를 지워도 저장된 규칙은 고치지 않고
  렌더가 그 id 를 뺀다.
- 규칙이 있는지를 묻는 자리는 모두 저장된 규칙을 읽는다(`has_caller_rule`·`holds_rules`). 그래서
  규칙이 없는 객체의 렌더는 오늘과 바이트까지 같다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from daegam.auth.rbac import DEVELOPER, TENANT_ADMIN, VIEWER, TenantRole

# 규칙에 적는 역할. platform-admin 은 테넌트 역할이 아니라 적을 수 없다
RuleRole = TenantRole
# 최소 역할이 받는 역할. 낮은 것부터다
QUALIFYING_ROLES: dict[str, tuple[str, ...]] = {
    VIEWER: (VIEWER, DEVELOPER, TENANT_ADMIN),
    DEVELOPER: (DEVELOPER, TENANT_ADMIN),
    TENANT_ADMIN: (TENANT_ADMIN,),
}
# 인자 조건의 연산자. 정규식은 없다
ConditionOp = Literal["equals", "one_of", "min", "max", "max_length", "prefix"]

# 한 규칙과 한 객체의 크기 상한. Cedar 크기는 F0 8번이 재었고 이 안에서 넉넉하다
MAX_RULE_KEYS = 500
MAX_CALLERS = 50
MAX_CONDITIONS = 20
MAX_ONE_OF = 100
MAX_PATH_CHARS = 256
MAX_NAME_CHARS = 256
# 조건의 글 값 하나(equals·prefix·one_of 의 항목)의 상한
MAX_VALUE_CHARS = 256
# 한 객체에 컴파일한 규칙 Cedar 의 상한(글자). F0 8번의 최악(규칙 50개가 Tool 200개를 모두 이름
# 댄 것)이 241KB 이고 CRD 한도(1.5MiB)보다 넉넉히 작다
MAX_RULE_CEDAR_CHARS = 256 * 1024


class ArgCondition(BaseModel):
    """인자 조건 하나 (4절). path 는 FastMCP 입력 스키마의 이름을 점으로 이은 경로다. 배열을 지나는
    경로는 받지 않는다. optional 이 아니면 인자가 없을 때 실패한다."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)
    op: ConditionOp
    value: Any = None
    optional: bool = False


class ToolCallers(BaseModel):
    """누가 부를 수 있는가. 셋은 OR 로 합친다."""

    model_config = ConfigDict(extra="forbid")

    roles: list[RuleRole] = Field(default_factory=list, max_length=len(QUALIFYING_ROLES))
    groups: list[str] = Field(default_factory=list, max_length=MAX_CALLERS)
    agents: list[str] = Field(default_factory=list, max_length=MAX_CALLERS)

    @property
    def empty(self) -> bool:
        return not (self.roles or self.groups or self.agents)

    def normalized(self) -> ToolCallers:
        """겹친 값을 빼고 순서를 정한 것. 저장과 비교가 이 꼴을 쓴다."""
        return ToolCallers(
            roles=sorted(set(self.roles), key=list(QUALIFYING_ROLES).index),
            groups=sorted(set(self.groups)),
            agents=sorted(set(self.agents)),
        )


class ServerToolRule(BaseModel):
    """MCP Server 의 Tool 하나의 규칙. read_only 가 None 이면 메서드의 기본값이다 (GET·HEAD 는 읽기,
    WSDL 오퍼레이션은 쓰기)."""

    model_config = ConfigDict(extra="forbid")

    callers: ToolCallers = Field(default_factory=ToolCallers)
    read_only: bool | None = None
    conditions: list[ArgCondition] = Field(default_factory=list, max_length=MAX_CONDITIONS)


class GatewayToolRule(BaseModel):
    """Gateway 가 광고하는 Tool 하나의 규칙. 그 Gateway 를 거친 호출에만 적용된다."""

    model_config = ConfigDict(extra="forbid")

    callers: ToolCallers = Field(default_factory=ToolCallers)


class ItemToolRule(BaseModel):
    """허브 `image` 항목의 Tool 하나의 규칙. 역할만 받는다. read_only 는 Tool
    효과에만 쓰이고 기본값은 쓰기다 (D48)."""

    model_config = ConfigDict(extra="forbid")

    callers: ToolCallers = Field(default_factory=ToolCallers)
    read_only: bool | None = None


AnyRule = ServerToolRule | GatewayToolRule | ItemToolRule


def has_caller_rule(rule: AnyRule) -> bool:
    """저장된 규칙에 누가 부를지의 규칙이 있는가. 렌더가 이 Tool 에 forbid 를 짓는 조건이다."""
    return not rule.callers.empty


def has_conditions(rule: AnyRule) -> bool:
    return bool(getattr(rule, "conditions", None))


def holds_rules(rules: Iterable[AnyRule]) -> bool:
    """누가 부를지의 규칙이나 인자 조건이 하나라도 있는가. 0.53 검사와 적용 건너뛰기가 읽는다.
    읽기 전용 표시만 있는 규칙은 Cedar 를 짓지 않으므로 세지 않는다."""
    return any(has_caller_rule(r) or has_conditions(r) for r in rules)


def is_blank(rule: AnyRule) -> bool:
    """아무것도 정하지 않은 규칙. 저장하지 않는다."""
    return (
        rule.callers.empty
        and getattr(rule, "read_only", None) is None
        and not getattr(rule, "conditions", None)
    )


def normalized_rules[R: AnyRule](rules: Mapping[str, R]) -> dict[str, R]:
    """빈 규칙을 빼고 callers 를 정리한 규칙. 저장 전에 한 번 지난다."""
    out: dict[str, R] = {}
    for key in sorted(rules):
        rule = rules[key]
        if is_blank(rule):
            continue
        out[key] = rule.model_copy(update={"callers": rule.callers.normalized()})
    return out


def opened_keys[K](before: Mapping[K, AnyRule], after: Mapping[K, AnyRule]) -> list[K]:
    """저장 전에는 callers 가 있었는데 저장 뒤에는 없는 키. 저장의 답과 화면이 그 Tool 이 모든
    멤버와 Agent 에게 열린다고 알린다 (1절)."""
    return [
        key
        for key, rule in before.items()
        if has_caller_rule(rule) and (key not in after or not has_caller_rule(after[key]))
    ]


def rule_agent_ids(rules: Iterable[AnyRule]) -> set[str]:
    return {a for r in rules for a in r.callers.agents}
