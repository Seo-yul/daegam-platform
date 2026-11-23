"""Tool 규칙의 Cedar (P52 2절). 규칙 출처(서버, Gateway, `image` 항목)마다 받는 호출자 집합 하나에
`forbid` 하나다.

```
forbid(principal, action == Action::"call_tool", resource)
  when { resource in [Tool::"a", Tool::"b"] }
  unless { <받는 호출자> };
```

forbid 는 바탕 permit, 소유자의 authz_policy, 옮긴 authz_override 곁에 서므로 출처가 모두 함께
지켜지고 어느 permit 도 규칙을 넓히지 못한다. cedar-go 는 조건이 오류 나는 정책을 건너뛰고
ToolHive 0.53 은 평가에 오류가 하나라도 있으면 요청을 거절한다(F0 3번). 그래서 절마다 `has` 로
지키고, 클레임은 `==`·`in`·`like` 로만 견주며, 클레임의 속성을 읽지 않는다. 위임 절은 `act`
레코드를 통째로 견주고, 형식이 다른 `==` 는 오류가 아니라 거짓이다. `like` 는 늘 문자열인
`claim_sub` 만 읽는다. ToolHive 는 `sub` 가 문자열이 아닌 토큰을 Cedar 전에 거절하기 때문이다.
그래서 렌더한 forbid 는 ToolHive 가 받는 모든 토큰에서 오류 없이 평가된다. 점 경로인 client id
클레임만 바탕 Agent permit 처럼 마디마다 `has` 로 지켜 내려간다.

사람 절은 `act` 가 없는 토큰만 받는다. oidc 모드에서는 테넌트의 Agent 클라이언트도
받지 않는다(`not_agent_client`). Agent 절은 그 Agent 자신의 신원과 그 Agent 의 위임 토큰을 받는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from daegam.auth.cedar import (
    CLIENT_ID_CLAIM,
    VMCP_SUB_PATTERN,
    _claim_is,
    _group_entity,
    _quote,
    set_literal,
)

CALL_TOOL = 'action == Action::"call_tool"'


@dataclass(frozen=True)
class AgentRef:
    """규칙이 이름 댄 Agent 하나. k8s_name 은 서비스 신원 `svc:agent:<k8s 이름>` 의 이름이다."""

    id: str
    k8s_name: str


def _has_eq(claim: str, value: str) -> str:
    """`principal has claim_x && principal.claim_x == "v"`. 점 경로는 마디마다 지킨다."""
    return _claim_is(claim, value)


def _tenant(tenant: str) -> str:
    return _has_eq("tenant", tenant)


def not_agent_client(client_id_claim: str, client_ids: Iterable[str]) -> list[str]:
    """oidc 모드의 사람 절이 테넌트의 Agent 클라이언트를 받지 않게 하는 조각들. 바탕 Agent permit 이
    읽는 `cid` 와 플랫폼이 검증하는 클레임(`DAEGAM_OIDC_CLIENT_ID_CLAIM`)마다, client id 마다
    하나다. Agent 는 그 클라이언트의 토큰으로 부르고 그 토큰에는 `act` 가 없으며 IdP 그룹이 실릴
    수 있기 때문이다 (2절)."""
    claims = list(dict.fromkeys([CLIENT_ID_CLAIM, client_id_claim]))
    out: list[str] = []
    for cid in dict.fromkeys(c for c in client_ids if c):
        for claim in claims:
            out.append(f"!({_has_eq(claim, cid)})")
    return out


def person_clause(groups: Sequence[str], *, exclude: Sequence[str] = ()) -> str | None:
    """`act` 없는 토큰이 그룹 하나에 든다. exclude 는 `not_agent_client` 의 조각이다. 그룹이 없으면
    None 이다."""
    if not groups:
        return None
    member = " || ".join(f"principal in THVGroup::{_quote(g)}" for g in groups)
    parts = ["!(principal has claim_act)", *exclude, f"({member})" if len(groups) > 1 else member]
    return "(" + " && ".join(parts) + ")"


def agent_clauses(agent: AgentRef, tenant: str) -> list[str]:
    """Agent 자신의 신원과 그 Agent 의 위임 토큰. 둘 다 Agent id 와 테넌트를 본다. 위임 절은 `act`
    레코드를 통째로 견주므로 다른 칸이 더 붙은 `act` 는 받지 않는다."""
    sub = f"svc:agent:{agent.k8s_name}"
    own = f"({_has_eq('sub', sub)} && {_has_eq('agent_id', agent.id)} && {_tenant(tenant)})"
    record = "{" + f'"sub": {_quote(sub)}, "agent_id": {_quote(agent.id)}' + "}"
    delegated = f"(principal has claim_act && principal.claim_act == {record} && {_tenant(tenant)})"
    return [own, delegated]


def vmcp_clause(tenant: str, digest: str) -> str:
    """이 서버의 지금 규칙 지문을 Entry 토큰에 실은 vMCP. 그 vMCP 가 규칙을 스스로 지킨다."""
    return (
        f'(principal has claim_sub && principal.claim_sub like "{VMCP_SUB_PATTERN}" && '
        f"{_tenant(tenant)} && {_has_eq('rules', digest)})"
    )


def consumer_clause(
    target: str, tenant: str, consumers: Iterable[str], digest: str | None
) -> str | None:
    """게시된 Gateway 의 규칙 있는 Tool 이 받는 소비자 vMCP (P57 6.2절). 소비자 집합에 들고, 이
    Gateway 에 묶이고, 이 테넌트이고, 이 Gateway 가 저장한 규칙 지문을 실은 토큰이다. 그 소비자가
    이 규칙을 스스로 지킨다(`vmcp_clause` 와 같은 뜻). 소비자나 지문이 없으면 None 이다."""
    names = set(consumers)
    if not names or not digest:
        return None
    return (
        f"(principal has claim_sub && {set_literal(names)}.contains(principal.claim_sub) && "
        f"{_has_eq('bound_to', target)} && {_tenant(tenant)} && {_has_eq('rules', digest)})"
    )


def published_fence(names: Iterable[str], tenant: str, tenant_group: str) -> list[str]:
    """소비자 Gateway 가 게시된 백엔드에 광고하는 이름의 테넌트 울타리 (P57 6.1절). 다른 테넌트를
    적은 토큰과, `tenant` 가 없는 토큰 가운데 고정 그룹 밖의 것을 거절한다. local 모드의 `POST
    /api/auth/token` 토큰은 `tenant` 없이 테넌트마다의 고정 그룹을 싣는다. 소유자의 permit 이 다른
    테넌트를 받아도 게시된 Gateway 의 Tool 은 그 테넌트 안에 남는다. 이름이 없으면 빈 목록이다."""
    names = sorted(set(names))
    if not names:
        return []
    return [
        f"forbid(principal, {CALL_TOOL}, resource) when {{ resource in {_tools(names)} }} "
        f"unless {{ ({_tenant(tenant)}) || (!(principal has claim_tenant) && "
        f"principal in {_group_entity(tenant_group)}) }};"
    ]


def front_clause(name: str, tenant: str) -> str:
    """이 서버의 노출 앞단. 그 앞단이 규칙을 스스로 지킨다."""
    return f"({_has_eq('sub', f'svc:expose:{name}')} && {_tenant(tenant)})"


def platform_clause(tenant: str) -> str:
    """플랫폼 자신의 tools/list 와 점검."""
    return f"({_has_eq('sub', 'svc:platform')} && {_tenant(tenant)})"


def _tools(names: Iterable[str]) -> str:
    return "[" + ", ".join(f"Tool::{_quote(n)}" for n in names) + "]"


def rule_forbids(clauses: Mapping[str, Sequence[str]]) -> list[str]:
    """Tool 이름마다 받는 절 → 같은 절 집합끼리 묶은 forbid. 받는 절이 없는 Tool 은 누구에게나
    닫힌다. 순서는 정해져 있어 같은 입력이 같은 글을 낸다."""
    by_set: dict[tuple[str, ...], list[str]] = {}
    for name in sorted(clauses):
        key = tuple(dict.fromkeys(clauses[name]))
        by_set.setdefault(key, []).append(name)
    out: list[str] = []
    for key in sorted(by_set, key=lambda k: by_set[k][0]):
        when = f"when {{ resource in {_tools(by_set[key])} }}"
        unless = f" unless {{ {' || '.join(key)} }}" if key else ""
        out.append(f"forbid(principal, {CALL_TOOL}, resource) {when}{unless};")
    return out


def known_forbid(names: Iterable[str]) -> list[str]:
    """그 서버가 아는 Tool 이름 밖의 것을 누구에게나 닫는다 (P52 2절). 프록시의 정책이 백엔드보다
    낡은 동안(이름을 바꾸거나 Tool 을 더한 배포가 굴러가는 동안, 어느 길로 스펙이 적용됐든) 낡은
    정책이 모르는 이름은 규칙이 있든 없든 열리지 않는다. tools/list 도 Tool 마다 call_tool 로
    판정되므로 그 이름은 목록에서도 빠진다. 다른 액션은 건드리지 않는다."""
    known = sorted(set(names))
    return [f"forbid(principal, {CALL_TOOL}, resource) unless {{ resource in {_tools(known)} }};"]


def closing_forbid(names: Iterable[str]) -> list[str]:
    """그 Tool 들을 누구에게나 닫는다. 인자 조건을 지키는지 모르는 서버(4절)와 0.53 아래 클러스터의
    규칙(1절)이 쓴다. 이름이 없으면 빈 목록이다."""
    names = sorted(set(names))
    if not names:
        return []
    return [f"forbid(principal, {CALL_TOOL}, resource) when {{ resource in {_tools(names)} }};"]


__all__ = [
    "AgentRef",
    "agent_clauses",
    "closing_forbid",
    "consumer_clause",
    "front_clause",
    "not_agent_client",
    "person_clause",
    "platform_clause",
    "published_fence",
    "rule_forbids",
    "vmcp_clause",
]
