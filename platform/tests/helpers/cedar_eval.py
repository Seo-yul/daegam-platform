"""렌더한 Cedar 를 ToolHive 의 principal 꼴로 평가한다 (P52 2절, F0 사전 점검).

ToolHive 0.53(`pkg/authz/authorizers/cedar/core.go`·`entity.go`)처럼 principal 은 `Client::<sub>`
이고 클레임마다 `claim_<이름>` 속성이다. 배열은 집합, 객체는 레코드가 되고 `null` 은 빠진다.
부모는 `groups`(없으면 `roles`, `cognito:groups`)와 role_claim_name 클레임의 `THVGroup` 이다.
resource 는 `MCP::<서버>` 아래의 `Tool::<이름>` 이고 action 은 `Action::"call_tool"` 이다.
ToolHive 는 평가에 오류가 하나라도 있으면 결정과 관계없이 거절하므로 `allowed` 도 그렇게
판정한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import cedarpy

GROUP_CLAIMS = ("groups", "roles", "cognito:groups")


def _value(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _value(x) for k, x in v.items() if x is not None}
    if isinstance(v, list | tuple):
        return [_value(x) for x in v if x is not None]
    return v


def _groups(claims: Mapping[str, Any], role_claim: str | None) -> list[str]:
    out: list[str] = []
    for name in GROUP_CLAIMS:
        if isinstance(claims.get(name), list):
            out += [g for g in claims[name] if isinstance(g, str)]
            break
    if role_claim and isinstance(claims.get(role_claim), list):
        out += [g for g in claims[role_claim] if isinstance(g, str)]
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    errors: tuple[str, ...]


def evaluate(
    policies: Iterable[str],
    claims: Mapping[str, Any],
    tool: str,
    *,
    role_claim: str | None = None,
    server: str = "srv",
    action: str = "call_tool",
    tool_attrs: Mapping[str, Any] | None = None,
) -> Verdict:
    """claims 를 실은 토큰이 tool 을 부를 수 있는가. ToolHive 처럼 오류가 있으면 거절이다. action 은
    ToolHive 의 액션 이름이다(`read_resource`, `get_prompt`). 그때 tool 은 그 리소스나 프롬프트의
    이름으로 읽는다. tool_attrs 는 ToolHive 가 광고한 Tool 의 annotations 에서 싣는 속성이다
    (`readOnlyHint` 등, P56 6.3절). 광고 밖의 이름은 속성이 없다."""
    sub = claims["sub"]
    assert isinstance(sub, str), "ToolHive 는 sub 가 문자열이 아닌 토큰을 Cedar 전에 거절한다"
    attrs = {f"claim_{k}": _value(v) for k, v in claims.items() if v is not None}
    groups = _groups(claims, role_claim)
    entities = [
        {
            "uid": {"type": "Client", "id": sub},
            "attrs": attrs,
            "parents": [{"type": "THVGroup", "id": g} for g in groups],
        },
        *({"uid": {"type": "THVGroup", "id": g}, "attrs": {}, "parents": []} for g in groups),
        {
            "uid": {"type": "Tool", "id": tool},
            "attrs": dict(tool_attrs or {}),
            "parents": [{"type": "MCP", "id": server}],
        },
        {"uid": {"type": "MCP", "id": server}, "attrs": {}, "parents": []},
    ]
    request = {
        "principal": f'Client::"{sub}"',
        "action": f'Action::"{action}"',
        "resource": f'Tool::"{tool}"',
        "context": {},
    }
    result = cedarpy.is_authorized(request, "\n".join(policies), entities)
    errors = tuple(result.diagnostics.errors)
    return Verdict(result.decision == cedarpy.Decision.Allow and not errors, errors)


def allowed(policies: Iterable[str], claims: Mapping[str, Any], tool: str, **kw: Any) -> bool:
    verdict = evaluate(policies, claims, tool, **kw)
    assert not verdict.errors, verdict.errors
    return verdict.allowed
