"""Tool 규칙의 저장 검증 (P52 1절). 저장 꼴은 `store/tool_rules.py` 에 있다.

검증은 저장 때 한 번이다. 키는 모두 있어야 하고(`rule_names_unknown_tool`·
`rule_names_unknown_gateway_tool`), 조건의 경로는 FastMCP 입력 스키마에서 풀리고 배열을 지나지
않으며 형식이 맞아야 하고(`rule_unknown_argument`·`rule_array_path`·`rule_argument_type`), 이미지를
직접 지정한 서버는 조건을 둘 수 없다(`rule_custom_image`). 이 저장이 더하는 Agent id 는 그 테넌트에
있어야 하고(`rule_unknown_agent`), 이미 저장된 지운 Agent 의 id 는 그대로 둔다. oidc 모드의 Gateway
규칙은 Agent 를 받지 않는다(`rule_agent_oidc`). 그룹은 oidc 모드에서만, 허브 항목은 역할만 받는다.

ToolHive 0.53 은 규칙이 컴파일되는 곳의 요구다(`RULES_VERSION`). `Feature` 에 넣지 않는다. 넣으면
`render_epoch` 가 못 지난 게이트를 싣는 자리가 바뀌어 대체 형태의 문서가 모두 다시 렌더된다
(`services/turn_gates.py` 와 같은 까닭).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from daegam.auth.rbac import ROLE_GROUP_PREFIX
from daegam.errors import ServiceError
from daegam.k8s.gates import Version, toolhive_at_least, toolhive_has
from daegam.store.backend_apis import BackendAPIVersion
from daegam.store.clusters import Cluster
from daegam.store.gateways import GatewaySpec
from daegam.store.mcp_servers import MCPServerSpec
from daegam.store.tool_rules import (
    MAX_NAME_CHARS,
    MAX_ONE_OF,
    MAX_RULE_CEDAR_CHARS,
    MAX_RULE_KEYS,
    MAX_VALUE_CHARS,
    AnyRule,
    ArgCondition,
    ToolCallers,
    holds_rules,
    normalized_rules,
    opened_keys,
    rule_agent_ids,
)

__all__ = [
    "RULES_VERSION",
    "RuleNotice",
    "ToolRuleError",
    "check_callers",
    "check_condition",
    "check_rule_keys",
    "require_rules_toolhive",
    "rules_toolhive_ok",
    "server_holds_rules",
    "server_rule_keys",
    "unknown_agents",
    "validate_server_rules",
]

# Tool 규칙이 기대는 ToolHive 판 (1절, F0). 그 아래 내부 서버에는 Cedar 가 없다
RULES_VERSION: Version = (0, 53, 0)
# 조건 연산자마다 받는 인자 형식
_OP_TYPES: dict[str, frozenset[str]] = {
    "equals": frozenset({"string", "integer", "number", "boolean"}),
    "one_of": frozenset({"string", "integer", "number"}),
    "min": frozenset({"integer", "number"}),
    "max": frozenset({"integer", "number"}),
    "max_length": frozenset({"string"}),
    "prefix": frozenset({"string"}),
}


class ToolRuleError(ServiceError):
    pass


@dataclass
class RuleNotice:
    """저장의 답에 싣는 알림 (1절). opened 는 callers 를 모두 지워 모든 멤버와
    Agent 에게 열리는 Tool 이고, rolling_agents 는 토큰을 다시 받으려고 한 번 다시 뜨는 Agent 다."""

    opened: list[str] = field(default_factory=list)
    rolling_agents: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"opened": list(self.opened), "rolling_agents": list(self.rolling_agents)}


def rules_toolhive_ok(cluster: Cluster) -> bool:
    """클러스터의 ToolHive 가 Tool 규칙을 다루는 판인가. 판을 모르거나 읽지 못하면 아니다."""
    return toolhive_at_least(cluster, RULES_VERSION)


def require_rules_toolhive(cluster: Cluster) -> None:
    """규칙이나 조건을 든 서버를 0.53 아래 클러스터에 배포하면 422 다 (1절)."""
    if not rules_toolhive_ok(cluster):
        raise ToolRuleError(
            422,
            "rules_toolhive_old",
            "Tool rules need ToolHive 0.53.0; this cluster has " + toolhive_has(cluster),
            needed="ToolHive 0.53.0",
            found=toolhive_has(cluster),
        )


def server_holds_rules(spec: MCPServerSpec) -> bool:
    return holds_rules(r for sel in spec.backends for r in sel.tool_rules.values())


def server_rule_keys(spec: MCPServerSpec) -> dict[str, AnyRule]:
    """서버 규칙을 `<backend_profile_id>/<operationId>` 하나로 편 것. 열림 알림과 Agent 비교가
    읽는다."""
    return {
        f"{sel.backend_profile_id}/{op}": rule
        for sel in spec.backends
        for op, rule in sel.tool_rules.items()
    }


def gateway_rule_keys(spec: GatewaySpec) -> dict[str, AnyRule]:
    """Gateway 규칙을 `<hub_item_id>/<원래 이름>` 하나로 편 것."""
    return {
        f"{b.hub_item_id}/{name}": rule
        for b in spec.backends
        for name, rule in b.tool_rules.items()
    }


def gateway_holds_rules(spec: GatewaySpec) -> bool:
    return holds_rules(r for b in spec.backends for r in b.tool_rules.values())


def normalized_gateway_rules(spec: GatewaySpec) -> GatewaySpec:
    backends = [
        b.model_copy(update={"tool_rules": normalized_rules(b.tool_rules)}) for b in spec.backends
    ]
    return spec.model_copy(update={"backends": backends})


def check_callers(
    callers: ToolCallers, *, oidc: bool, where: str, roles_only: bool = False
) -> None:
    """callers 의 꼴. 허브 항목은 역할만, 그룹은 oidc 모드에서만 받는다. 그룹 이름은 플랫폼 역할
    그룹의 접두사를 쓸 수 없다 (3절)."""
    if roles_only and (callers.groups or callers.agents):
        raise ToolRuleError(422, "invalid_value", f"{where}: item rules select roles only")
    if callers.groups and not oidc:
        raise ToolRuleError(422, "invalid_value", f"{where}: groups are offered in oidc mode only")
    for group in callers.groups:
        if not group or len(group) > MAX_NAME_CHARS or group.startswith(ROLE_GROUP_PREFIX):
            raise ToolRuleError(
                422, "invalid_value", f"{where}: group {group!r} cannot be used in a rule"
            )
    if any(not a or len(a) > MAX_NAME_CHARS for a in callers.agents):
        raise ToolRuleError(422, "invalid_value", f"{where}: an Agent id is empty or too long")


def check_rule_keys(
    keys: Collection[str], offered: Collection[str], *, code: str, **ctx: Any
) -> None:
    """규칙의 키가 모두 offered 에 있는가. 없으면 그 키들과 함께 422 다."""
    if len(keys) > MAX_RULE_KEYS:
        raise ToolRuleError(422, "invalid_value", f"at most {MAX_RULE_KEYS} Tool rules", **ctx)
    unknown = sorted(set(keys) - set(offered))
    if unknown:
        raise ToolRuleError(
            422,
            code,
            f"rules name Tools that are not offered: {', '.join(unknown)}",
            unknown=unknown,
            **ctx,
        )


def _value_fits(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    return False


def check_condition(cond: ArgCondition, arguments: Mapping[str, str] | None, **ctx: Any) -> None:
    """조건 하나가 그 Tool 의 인자에서 풀리고 형식이 맞는가 (4절). arguments 는 그 Tool 의
    {점 경로: 형식} 이고(`pipeline/tool_arguments.py`), None 이면 그 버전에 인자 목록이 없다."""
    ctx = {"path": cond.path, **ctx}
    if arguments is None:
        raise ToolRuleError(
            422,
            "rule_unknown_argument",
            "this Backend API version holds no argument list; upload the spec again to add "
            "conditions",
            **ctx,
        )
    parts = cond.path.split(".")
    for i in range(1, len(parts)):
        if arguments.get(".".join(parts[:i])) == "array":
            raise ToolRuleError(
                422, "rule_array_path", f"{cond.path} passes through an array", **ctx
            )
    kind = arguments.get(cond.path)
    if kind is None:
        raise ToolRuleError(
            422, "rule_unknown_argument", f"the Tool has no argument {cond.path}", **ctx
        )
    if kind == "array":
        raise ToolRuleError(422, "rule_array_path", f"{cond.path} is an array", **ctx)
    if kind not in _OP_TYPES[cond.op]:
        raise ToolRuleError(
            422,
            "rule_argument_type",
            f"{cond.op} does not apply to {cond.path}, which is {kind}",
            **ctx,
        )
    value = cond.value
    texts = value if cond.op == "one_of" and isinstance(value, list) else [value]
    if any(isinstance(v, str) and len(v) > MAX_VALUE_CHARS for v in texts):
        raise ToolRuleError(
            422,
            "invalid_value",
            f"a condition value on {cond.path} is longer than {MAX_VALUE_CHARS} characters",
            **ctx,
        )
    if cond.op == "one_of":
        ok = (
            isinstance(value, list)
            and 0 < len(value) <= MAX_ONE_OF
            and all(_value_fits(v, kind) for v in value)
        )
    elif cond.op == "max_length":
        ok = _value_fits(value, "integer") and value >= 0
    elif cond.op in ("min", "max"):
        ok = _value_fits(value, "number")
    else:
        ok = _value_fits(value, kind)
    if not ok:
        raise ToolRuleError(
            422, "rule_argument_type", f"the value of {cond.op} on {cond.path} does not fit", **ctx
        )


def validate_server_rules(
    spec: MCPServerSpec, versions: Sequence[BackendAPIVersion], *, oidc: bool
) -> None:
    """MCP Server 의 규칙 (1절). 키는 그 backend 가 노출하는 operationId 이고, 조건은 그 Tool 의
    인자에서 풀려야 한다. 이미지를 직접 지정한 서버는 조건을 둘 수 없다. 그 이미지가 조건을
    지키는지 알 수 없기 때문이다."""
    custom_image = spec.runtime.image is not None
    for i, (sel, version) in enumerate(zip(spec.backends, versions, strict=True)):
        ctx = {"backend_profile_id": sel.backend_profile_id, "backend_index": i}
        offered = sel.tools or version.tool_names
        check_rule_keys(sel.tool_rules, offered, code="rule_names_unknown_tool", **ctx)
        arguments = {d.operation_id: d.arguments for d in version.tool_details}
        for op, rule in sel.tool_rules.items():
            check_callers(rule.callers, oidc=oidc, where=op)
            if rule.conditions and custom_image:
                raise ToolRuleError(
                    422,
                    "rule_custom_image",
                    "a server that names its own image cannot hold argument conditions",
                    operation=op,
                    **ctx,
                )
            for cond in rule.conditions:
                check_condition(cond, arguments.get(op), operation=op, **ctx)


def check_cedar_size(policies: Iterable[str], **ctx: Any) -> None:
    """한 객체에 컴파일할 규칙 Cedar 가 상한(`MAX_RULE_CEDAR_CHARS`) 안인가. 넘으면 422
    rules_too_large 다. 저장 때 그 출처 하나로 잰다."""
    size = sum(len(p) for p in policies)
    if size > MAX_RULE_CEDAR_CHARS:
        raise ToolRuleError(
            422,
            "rules_too_large",
            f"the Tool rules compile to {size} characters of Cedar; the limit is "
            f"{MAX_RULE_CEDAR_CHARS}. Group Tools that share callers or name fewer Tools",
            size=size,
            limit=MAX_RULE_CEDAR_CHARS,
            **ctx,
        )


def unknown_agents(
    after: Iterable[AnyRule], before: Iterable[AnyRule], existing: Collection[str]
) -> list[str]:
    """이 저장이 더하는 Agent id 가운데 그 테넌트에 없는 것. 이미 저장된 id 는 지운 Agent 여도
    그대로 둔다 (1절)."""
    added = rule_agent_ids(after) - rule_agent_ids(before)
    return sorted(added - set(existing))


def refuse_unknown_agents(missing: Sequence[str]) -> None:
    if missing:
        raise ToolRuleError(
            422,
            "rule_unknown_agent",
            f"rules name Agents this tenant does not have: {', '.join(missing)}",
            unknown=list(missing),
        )


async def check_new_agents(
    agents: Any, tenant: str, *, after: Iterable[AnyRule], before: Iterable[AnyRule]
) -> None:
    """이 저장이 더한 Agent id 가 모두 그 테넌트의 Agent 인가. 아니면 422 rule_unknown_agent 다.
    agents 는 Agent 스토어(`get_many`)다."""
    after, before = list(after), list(before)
    added = rule_agent_ids(after) - rule_agent_ids(before)
    if not added:
        return
    found = await agents.get_many(added)
    existing = [agent_id for agent_id, doc in found.items() if doc.tenant == tenant]
    refuse_unknown_agents(unknown_agents(after, before, existing))


def normalized_server_rules(spec: MCPServerSpec) -> MCPServerSpec:
    """backend 마다 빈 규칙을 빼고 callers 를 정리한 정의 (`normalized_rules`)."""
    backends = [
        sel.model_copy(update={"tool_rules": normalized_rules(sel.tool_rules)})
        for sel in spec.backends
    ]
    return spec.model_copy(update={"backends": backends})


async def rolling_agents(
    agents: Any,
    scopes: Any,
    tenant: str,
    *,
    after: Iterable[AnyRule],
    before: Iterable[AnyRule],
) -> list[dict[str, str]]:
    """저장이 새로 이름 댄 Agent 가운데 배포된 것. 그 규칙이 배포되면 이 기능 전에
    낸 토큰을 든 Agent 가 자기 id 를 실은 토큰을 받으려고 한 번 다시 뜬다. 이미 다른 규칙이 이름
    댄 Agent 는 뺀다. 이미 id 를 실은 토큰을 든 Agent 는 파드가 굴러가지 않는다."""
    added = rule_agent_ids(after) - rule_agent_ids(before)
    if scopes is not None:
        added -= await scopes.named_agents(tenant)
    if not added:
        return []
    found = await agents.get_many(added)
    return [
        {"id": a.id, "name": a.name}
        for a in sorted(found.values(), key=lambda a: a.name)
        if a.tenant == tenant and a.deployed_revision is not None
    ]


def notice_of(before: Mapping[str, AnyRule], after: Mapping[str, AnyRule]) -> RuleNotice:
    return RuleNotice(opened=opened_keys(before, after))


# -- 상태 경고 (P52 7절) ------------------------------------------------------------------
# 상태 warnings[] 의 줄이다. 화면은 코드로 화면 언어의 말을 그린다(F5)

RULES_NOT_ENFORCED = "rules_not_enforced"
CONDITIONS_CLOSED = "conditions_closed"
CONDITIONS_UNRESOLVED = "conditions_unresolved"
RULES_TOOLHIVE_OLD = "rules_toolhive_old"
RULES_OWNER_CEDAR_CLOSED = "rules_owner_cedar_closed"
RULES_DIGEST_BEHIND = "rules_digest_behind"
RULES_TOOL_MISSING = "rules_tool_missing"
RULES_PENDING = "rules_pending"
RULES_GATEWAYS_BEHIND = "rules_gateways_behind"
RULES_FRONT_ROLLING = "rules_front_rolling"

RULE_WARNINGS: dict[str, str] = {
    RULES_NOT_ENFORCED: "in header auth mode the Tool rules' callers are not enforced",
    CONDITIONS_CLOSED: (
        "not every pod is known to enforce argument conditions, so Tools with conditions are closed"
    ),
    CONDITIONS_UNRESOLVED: (
        "the server could not resolve some argument conditions in its Tools' input schemas, so "
        "those Tools refuse every call; fix the conditions in the Tool rules"
    ),
    RULES_TOOLHIVE_OLD: (
        "this cluster's ToolHive is older than 0.53 or unknown, so Tools with rules are closed"
    ),
    RULES_OWNER_CEDAR_CLOSED: (
        "Cedar saved in Advanced editing keeps the last render, so the servers' Tool rules "
        "cannot apply and Tools with rules cannot be called"
    ),
    RULES_DIGEST_BEHIND: (
        "a backend's Tool rules changed and this Gateway has not applied them yet, so its "
        "Tools with rules cannot be called for now"
    ),
    RULES_TOOL_MISSING: "some Tools with rules are no longer offered",
    RULES_PENDING: (
        "the vMCP is still rolling out, so the previous Tool rules hold until every pod runs "
        "the new template"
    ),
    RULES_GATEWAYS_BEHIND: (
        "these Gateways have not applied this server's current Tool rules yet, so its Tools with "
        "rules cannot be called through them for now"
    ),
    RULES_FRONT_ROLLING: (
        "the exposure front is still rolling out, so the previous Tool rules hold there until "
        "its pods run the new template"
    ),
}


def coded_warning(code: str, text: str, detail: str | None = None) -> str:
    """상태 warnings[] 의 한 줄 `코드: 영어 설명 (이름들)`. 화면은 코드로 문장을 그리고 끝의
    괄호에서 걸린 이름을 읽는다(ui/src/lib/warningText.ts)."""
    return f"{code}: {text}" + (f" ({detail})" if detail else "")


def rule_warning(code: str, detail: str | None = None) -> str:
    return coded_warning(code, RULE_WARNINGS[code], detail)
