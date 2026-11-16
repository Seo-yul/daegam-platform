"""Tool 규칙을 객체마다 Cedar 로 컴파일한다 (P52 2·3절). Cedar 글은 `auth/rule_cedar.py` 가 짓는다.

받는 호출자는 객체마다 다르다. 짓지 못하는 절은 렌더하지 않는다.

| 객체 | 절 |
|---|---|
| local 모드 vMCP | 역할(`daegam-role:<tenant>:<role>`), Agent(자기 신원·위임), 플랫폼 |
| 플랫폼을 믿는 oidc 모드 vMCP | local 과 같고, 고른 그룹은 `daegam-group:<tenant>:<group>` |
| IdP 를 믿는 oidc 모드 vMCP | 역할에 매핑된 IdP 그룹과 고른 그룹(Agent 클라이언트 제외) |
| local 모드 노출 앞단 | 역할 |
| 플랫폼을 믿는 oidc 모드 노출 앞단 | 역할과 `daegam-group:<tenant>:<group>` |
| IdP 를 믿는 oidc 모드 노출 앞단 | 역할에 매핑된 IdP 그룹과 고른 그룹(Agent 클라이언트 제외) |
| 내부 MCPServer | Agent, vMCP(규칙 지문), 앞단(이름), 플랫폼 |
| image 항목 MCPServer | vMCP(규칙 지문), 플랫폼 |

내부 서버에는 사람 절이 없다. `act` 없는 사람의 토큰은 거기 닿지 않기 때문이다. 서버는 그룹 절을
렌더하지 않는다. 서비스 토큰이 모두 테넌트 그룹을 싣기 때문이다. callers 가 비지 않았는데 짓는
절이 없으면(지운 Agent 만, IdP 를 믿는 oidc vMCP 의 Agent, 내부 서버의 역할) 그 객체에서 그 Tool 은
직접 부르는 호출자에게 닫힌다. header 모드에서는 누가 부를지의 규칙을 렌더하지 않는다.

규칙의 모드(`RuleScope.mode`)는 인증 모드가 아니라 데이터 플레인이 받는 토큰의 모양이다. oidc
모드에서 데이터 플레인이 플랫폼을 믿으면 `platform` 이고 local 처럼 컴파일한다 (P53b 6.1·6.6절).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from daegam.auth.rbac import idp_group, role_group
from daegam.auth.rule_cedar import (
    AgentRef,
    agent_clauses,
    closing_forbid,
    front_clause,
    not_agent_client,
    person_clause,
    platform_clause,
    rule_forbids,
    vmcp_clause,
)
from daegam.services.toolnames import fastmcp_default_name
from daegam.store.mcp_servers import BackendSelection, MCPServerRevision, MCPServerSpec
from daegam.store.tool_rules import (
    QUALIFYING_ROLES,
    AnyRule,
    ToolCallers,
    has_caller_rule,
    rule_agent_ids,
)
from daegam.ttl_cache import TTLCache

Target = Literal["vmcp", "front", "internal", "image"]
# 데이터 플레인이 플랫폼의 토큰을 받는 규칙의 모드. 역할 절이 `daegam_roles` 를 읽고 Gateway 에
# Agent 절이 선다
PLATFORM_TOKEN_MODES = frozenset({"local", "platform"})
# 규칙 지문의 길이(16진 글자). 토큰 객체 이름의 꼬리로도 쓰여 63자 안에 든다
DIGEST_CHARS = 16
# 규칙이 이름 댄 Agent 와 테넌트의 사실을 기억하는 초. 재적용 틱(60초)보다 짧다
NAMED_TTL_SECONDS = 15.0
# 한 틱 안에서 문서마다의 규칙 표시를 기억하는 초 (want_rev 와 paced_revs 가 함께 묻는다)
MARK_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class RuleScope:
    """한 테넌트에서 규칙을 컴파일할 때의 사실. agents 는 그 테넌트에 있는 Agent 의 id → k8s 이름,
    role_groups 는 IdP 를 믿는 oidc 모드에서 역할마다 그 역할로 매핑된 IdP 그룹이다(Group
    Mapping)."""

    tenant: str
    # local | platform(플랫폼을 믿는 oidc) | oidc(IdP 를 믿는 oidc) | header
    mode: str
    agents: Mapping[str, str] = field(default_factory=dict)
    role_groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    agent_client_ids: tuple[str, ...] = ()
    client_id_claim: str = "cid"

    @property
    def enforces_callers(self) -> bool:
        return self.mode != "header"


def qualifying_roles(roles: Iterable[str]) -> list[str]:
    """최소 역할이 받는 역할. 낮은 것부터다."""
    out: set[str] = set()
    for r in roles:
        out.update(QUALIFYING_ROLES.get(r, ()))
    return [r for r in QUALIFYING_ROLES if r in out]


def person_groups(callers: ToolCallers, scope: RuleScope) -> list[str]:
    """사람 절이 받는 THVGroup. local 모드는 역할 그룹, 플랫폼을 믿는 oidc 모드는 역할 그룹과 고른
    그룹을 테넌트 이름을 붙인 꼴(`idp_group`, P53b 6.6절), IdP 를 믿는 oidc 모드는 역할에 매핑된
    IdP 그룹과 고른 그룹이다."""
    roles = qualifying_roles(callers.roles)
    if scope.mode in PLATFORM_TOKEN_MODES:
        groups = [role_group(scope.tenant, r) for r in roles]
        if scope.mode == "platform":
            groups += [idp_group(scope.tenant, g) for g in sorted(set(callers.groups))]
        return groups
    if scope.mode == "oidc":
        mapped = [g for r in roles for g in scope.role_groups.get(r, ())]
        return sorted(set(mapped) | set(callers.groups))
    return []


def named_agents(callers: ToolCallers, scope: RuleScope) -> list[AgentRef]:
    """지금 그 테넌트에 있는 Agent 만. 지운 Agent 의 id 는 렌더가 뺀다 (1절)."""
    return [AgentRef(a, scope.agents[a]) for a in callers.agents if a in scope.agents]


def caller_clauses(
    callers: ToolCallers,
    scope: RuleScope,
    target: Target,
    *,
    digest: str | None = None,
    front: str | None = None,
) -> list[str]:
    """객체 target 이 이 callers 에 대해 렌더하는 절. 모듈 머리말의 표대로다."""
    t = scope.tenant
    out: list[str] = []
    if target in ("vmcp", "front"):
        exclude = (
            not_agent_client(scope.client_id_claim, scope.agent_client_ids)
            if scope.mode == "oidc"
            else []
        )
        clause = person_clause(person_groups(callers, scope), exclude=exclude)
        if clause:
            out.append(clause)
        if target == "vmcp" and scope.mode in PLATFORM_TOKEN_MODES:
            for agent in named_agents(callers, scope):
                out += agent_clauses(agent, t)
            out.append(platform_clause(t))
        return out
    if target == "internal":
        for agent in named_agents(callers, scope):
            out += agent_clauses(agent, t)
    if digest is not None:
        out.append(vmcp_clause(t, digest))
    if target == "internal" and front is not None:
        out.append(front_clause(front, t))
    out.append(platform_clause(t))
    return out


def roles_claimed(callers: Iterable[ToolCallers], scope: RuleScope) -> bool:
    """역할 절이 있는가. 플랫폼의 토큰을 받는 모드의 그런 객체는 role_claim_name 을 렌더한다
    (3절)."""
    return scope.mode in PLATFORM_TOKEN_MODES and any(c.roles for c in callers)


# -- 서버의 이름 -------------------------------------------------------------------------


def operation_name(sel: BackendSelection, op: str) -> str:
    """MCP Server 의 operationId 가 컨테이너에서 갖는 이름. mcp-runtime 이 tool_overrides 를
    스스로 적용한다 (`advertised_names.exposed_pairs` 와 같은 규칙)."""
    ov = sel.tool_overrides.get(op)
    return ov.name if ov is not None and ov.name else fastmcp_default_name(op)


def server_rules_by_name(spec: MCPServerSpec) -> dict[str, AnyRule]:
    """서버 규칙을 컨테이너 이름으로 편 것."""
    return {
        operation_name(sel, op): rule
        for sel in spec.backends
        for op, rule in sel.tool_rules.items()
    }


def ruled_names(rules: Mapping[str, AnyRule]) -> list[str]:
    return sorted(n for n, r in rules.items() if has_caller_rule(r))


def conditioned_names(rules: Mapping[str, AnyRule]) -> list[str]:
    return sorted(n for n, r in rules.items() if getattr(r, "conditions", None))


# -- 지문 ------------------------------------------------------------------------------


def rules_digest(server_id: str, rules: Mapping[str, AnyRule], scope: RuleScope) -> str | None:
    """서버(내부·image 항목)의 규칙 지문 (2절). 서버 id 와 컴파일한 규칙(누가 부를지 규칙이 있는
    이름마다 받는 역할, 그룹, Agent 의 id 와 k8s 이름)의 정해진 JSON 을 해시한다. vMCP·앞단·플랫폼
    절과 닫는 forbid, 앞 리비전의 이름은 들지 않는다. 규칙이 없으면 None 이다. 컴파일한 callers 가
    빈 이름(지운 Agent 만 남은 것)도 지문에 든다."""
    body = {
        name: {
            "roles": qualifying_roles(rules[name].callers.roles),
            "groups": sorted(rules[name].callers.groups),
            "agents": sorted([a.id, a.k8s_name] for a in named_agents(rules[name].callers, scope)),
        }
        for name in ruled_names(rules)
    }
    if not body:
        return None
    raw = json.dumps([server_id, body], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:DIGEST_CHARS]


def sources_digest(
    gateway_id: str, sources: Sequence[Mapping[str, AnyRule]], scope: RuleScope
) -> str | None:
    """게시된 Gateway 가 저장하는 규칙 출처들의 지문 하나 (P57 6.2절). 출처의 차례를 이름에 붙여
    `rules_digest` 로 해시하므로 같은 이름이 두 출처에 있어도 따로 센다. 규칙이 없으면 None 이다."""
    flat = {
        f"{i}:{name}": rule for i, source in enumerate(sources) for name, rule in source.items()
    }
    return rules_digest(gateway_id, flat, scope)


# -- 객체마다의 정책 -----------------------------------------------------------------------


def source_forbids(
    rules: Mapping[str, AnyRule],
    scope: RuleScope,
    target: Target,
    *,
    names: Mapping[str, Sequence[str]] | None = None,
    digest: str | None = None,
    front: str | None = None,
    consumer: str | None = None,
) -> list[str]:
    """규칙 출처 하나의 forbid. rules 는 그 출처의 규칙을 이 객체의 이름으로 편 것이고, names 는
    이름마다 함께 닫을 다른 이름(서버의 앞 리비전 이름, 2절)이다. header 모드에서는 없다.
    consumer 는 게시된 Gateway 가 규칙 있는 Tool 마다 더 받는 소비자 절이다(P57 6.2절,
    `rule_cedar.consumer_clause`).

    앞 이름에는 vMCP 절을 싣지 않는다. vMCP 는 지금 이름만 컴파일하므로 앞 이름을 지키지 않는다.
    그래서 이름 바꾸기가 굴러가는 동안 Gateway 를 거친 옛 이름의 호출은 서버에서 닫힌다. 앞단은
    앞 이름도 스스로 지키므로 앞단 절은 남는다."""
    if not scope.enforces_callers:
        return []
    clauses: dict[str, list[str]] = {}
    for name in ruled_names(rules):
        got = caller_clauses(rules[name].callers, scope, target, digest=digest, front=front)
        if consumer:
            got.append(consumer)
        clauses[name] = got
        earlier = (names or {}).get(name, ())
        if earlier:
            prior = caller_clauses(rules[name].callers, scope, target, digest=None, front=front)
            for n in earlier:
                clauses.setdefault(n, prior)
    return rule_forbids(clauses)


def closed_policies(
    rules: Mapping[str, AnyRule],
    scope: RuleScope,
    *,
    earlier: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """규칙을 컴파일할 수 없는 판(0.53 아래나 모르는 판, 1절)의 렌더. 누가 부를지의 규칙이 있는
    이름(header 모드는 빼고)과 조건이 있는 이름, 그 앞 이름을 누구에게나 닫는다. 어느 적용 길로
    렌더되든 규칙을 든 객체가 열리지 않게 한다."""
    names = [*conditioned_names(rules)]
    if scope.enforces_callers:
        names += ruled_names(rules)
    return closing_forbid(n for name in names for n in [name, *(earlier or {}).get(name, ())])


def server_policies(
    spec: MCPServerSpec,
    scope: RuleScope,
    *,
    server_id: str,
    front: str | None,
    earlier: Mapping[str, Sequence[str]] | None = None,
    conditions_known: bool = False,
) -> list[str]:
    """내부 MCPServer 의 규칙 forbid 와 조건을 지키는지 모르는 Tool 을 닫는 forbid. earlier 는
    이름마다 돌고 있는 백엔드 파드의 리비전이 쓰는 앞 이름이다."""
    rules = server_rules_by_name(spec)
    out = source_forbids(
        rules,
        scope,
        "internal",
        names=earlier,
        digest=rules_digest(server_id, rules, scope),
        front=front,
    )
    if not conditions_known:
        closing = [
            n for name in conditioned_names(rules) for n in [name, *(earlier or {}).get(name, ())]
        ]
        out += closing_forbid(closing)
    return out


def item_policies(rules: Mapping[str, AnyRule], scope: RuleScope, *, item_id: str) -> list[str]:
    """image 항목(과 그 복제본) MCPServer 의 규칙 forbid. vMCP 와 플랫폼만 받는다."""
    return source_forbids(rules, scope, "image", digest=rules_digest(item_id, rules, scope))


# -- 서버의 앞 이름 (2절) -------------------------------------------------------------------


def earlier_names(
    revisions: Sequence[MCPServerRevision],
    rev: MCPServerRevision,
    running: Iterable[int] | None,
) -> dict[str, list[str]]:
    """rev 에서 규칙이나 조건이 있는 operationId 마다, 돌고 있는 백엔드 파드의 리비전(running)이
    그 operationId 에 준 앞 이름. 이름 바꾸기를 배포하면 ToolHive 가 프록시를 백엔드보다 먼저 굴려
    새 Cedar 가 옛 컨테이너를 만나기 때문이다(F0). running 을 모르면(None) 모든 리비전을 본다.
    키는 rev 에서의 이름이다."""
    keep = None if running is None else set(running)
    old = [
        r for r in revisions if r.revision != rev.revision and (keep is None or r.revision in keep)
    ]
    out: dict[str, list[str]] = {}
    for sel in rev.backends:
        for op, rule in sel.tool_rules.items():
            if not (has_caller_rule(rule) or getattr(rule, "conditions", None)):
                continue
            name = operation_name(sel, op)
            seen: list[str] = []
            for r in old:
                for prior in r.backends:
                    if prior.backend_profile_id != sel.backend_profile_id:
                        continue
                    before = operation_name(prior, op)
                    if before != name and before not in seen:
                        seen.append(before)
            if seen:
                out[name] = sorted(seen)
    return out


# -- 테넌트의 사실을 읽는 쪽 -------------------------------------------------------------------


class RuleScopes:
    """테넌트마다 `RuleScope` 를 짓는다. 세 서비스(MCP Server, Gateway, 허브)의 렌더와 Gateway 의
    Entry 토큰이 같은 것을 읽어 지문이 갈라지지 않는다. 그 테넌트의 Agent(draft 포함)와, oidc
    모드면 Group Mapping 과 Agent 클라이언트를 읽는다."""

    def __init__(
        self,
        settings: Any,
        tenant_settings: Any,
        memberships: Any,
        agents: Any,
        *,
        servers: Any = None,
        gateways: Any = None,
    ):
        self.settings = settings
        self.tenant_settings = tenant_settings
        self.memberships = memberships
        self.agents = agents
        self.servers = servers
        self.gateways = gateways
        self._named: TTLCache[str, frozenset[str]] = TTLCache(NAMED_TTL_SECONDS, max_entries=1024)
        self._scopes: TTLCache[str, RuleScope] = TTLCache(NAMED_TTL_SECONDS, max_entries=1024)

    async def named_agents(self, tenant: str) -> frozenset[str]:
        """Agent 절을 컴파일하는 규칙이 이름 댄 그 테넌트의 Agent id (P52 3절).
        배포한 MCP Server 리비전의 규칙(내부 서버는 모든 모드에서 Agent 절을 짓는다)과, 데이터
        플레인이 플랫폼을 믿으면 배포한 Gateway 리비전의 규칙이다. 재적용 틱이 Agent 마다 묻으므로
        잠깐 기억한다."""
        hit = self._named.get(tenant)
        if hit is not None:
            return hit
        named: set[str] = set()
        for server in await self.servers.list(tenant) if self.servers is not None else []:
            rev = server.get_revision(server.deployed_revision or 0)
            for sel in rev.backends if rev is not None else []:
                named |= rule_agent_ids(sel.tool_rules.values())
        if self.gateways is not None and self.settings.data_plane_trusts_platform:
            for gateway in await self.gateways.list(tenant):
                rev = gateway.get_revision(gateway.deployed_revision or 0)
                for b in rev.backends if rev is not None else []:
                    named |= rule_agent_ids(b.tool_rules.values())
        out = frozenset(named)
        self._named.put(tenant, out)
        return out

    def forget(self, tenant: str) -> None:
        """다음 물음이 새로 읽게 한다."""
        self._named.pop(tenant)
        self._scopes.pop(tenant)

    async def scope(self, tenant: str, *, cached: bool = False) -> RuleScope:
        """그 테넌트의 사실. cached 면 재적용 틱의 판 계산이라 잠깐 기억한 것을 쓴다. 렌더는 늘
        새로 읽는다. 기억한 것이 낡아도 다음 틱의 판이 달라 다시 렌더된다."""
        if cached:
            hit = self._scopes.get(tenant)
            if hit is not None:
                return hit
        scope = await self._read_scope(tenant)
        self._scopes.put(tenant, scope)
        return scope

    async def _read_scope(self, tenant: str) -> RuleScope:
        from daegam.services.data_plane_auth import consumer_auth
        from daegam.store.agents import k8s_name_for

        auth = consumer_auth(self.settings, await self.tenant_settings.get(tenant))
        agents = {
            a.id: k8s_name_for(a.name) for a in await self.agents.list(tenant, include_draft=True)
        }
        role_groups: dict[str, tuple[str, ...]] = {}
        mode = auth.mode
        # 곁의 IdP 는 플랫폼을 믿는 oidc 모드에만 있다(`data_plane_auth.idp_side`)
        if auth.idp is not None:
            mode = "platform"
        elif mode == "oidc":
            by_role: dict[str, list[str]] = {}
            for m in await self.memberships.list_mappings():
                if m.tenant == tenant:
                    by_role.setdefault(m.role, []).append(m.group)
            role_groups = {r: tuple(sorted(gs)) for r, gs in by_role.items()}
        return RuleScope(
            tenant=tenant,
            mode=mode,
            agents=agents,
            role_groups=role_groups,
            agent_client_ids=tuple(auth.agent_client_ids),
            client_id_claim=auth.client_id_claim,
        )
