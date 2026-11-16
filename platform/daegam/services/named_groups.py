"""테넌트가 이름 댄 IdP 그룹 (P53b 6.6절).

oidc 모드의 플랫폼 토큰(개인 토큰, 로그인 연결의 접근 토큰)은 사람의 IdP 그룹 가운데 그 토큰의
테넌트가 이름 댄 것만 `daegam-group:<tenant>:<group>` 꼴로 싣는다(`auth/rbac.py`
`carried_idp_groups`). 이름 댄 그룹은 MCP Server 와 Gateway 의 Tool 규칙(지금 정의와 모든 리비전)과
Agent 노출의 allowed_groups(노출을 켰는지와 상관없이)다. 배포할 리비전이나 나중에 켤 노출이 이름 댄
그룹이 토큰을 다시 만들지 않아도 닿게 하려는 것이다. 허브 `image` 항목의 규칙은 역할만 받는다.
로그인 연결의 갱신마다 읽으므로 테넌트마다 잠깐 기억한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from daegam.auth import rbac
from daegam.store.agents import MongoAgentStore
from daegam.store.gateways import Gateway, MongoGatewayStore
from daegam.store.mcp_servers import MCPServer, MongoMCPServerStore
from daegam.ttl_cache import TTLCache

# 테넌트가 이름 댄 그룹을 기억하는 초
NAMED_GROUPS_TTL_SECONDS = 30.0


class NamedGroups:
    def __init__(
        self,
        mcp_servers: MongoMCPServerStore,
        gateways: MongoGatewayStore,
        agents: MongoAgentStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.mcp_servers = mcp_servers
        self.gateways = gateways
        self.agents = agents
        self._cache: TTLCache[str, frozenset[str]] = TTLCache(
            NAMED_GROUPS_TTL_SECONDS, clock=clock, max_entries=1000
        )

    async def of(self, tenant: str) -> frozenset[str]:
        """tenant 가 이름 댄 그룹의 원래 이름."""
        cached = self._cache.get(tenant)
        if cached is not None:
            return cached
        named: set[str] = set()
        ruled: list[MCPServer | Gateway] = [
            *await self.mcp_servers.list(tenant, include_draft=True),
            *await self.gateways.list(tenant, include_draft=True),
        ]
        for doc in ruled:
            for spec in (doc.spec, *doc.revisions):
                rules = (r for b in spec.backends for r in b.tool_rules.values())
                named.update(g for r in rules for g in r.callers.groups)
        for agent in await self.agents.list(tenant, include_draft=True):
            named.update(agent.exposure.allowed_groups)
        found = frozenset(named)
        self._cache.put(tenant, found)
        return found

    async def carried(self, tenant: str, groups: Iterable[str] | None) -> tuple[str, ...]:
        """사람의 IdP 그룹(groups) 가운데 tenant 의 토큰이 싣는 것의 원래 이름. 그룹을 모르거나
        없으면 비어 있고 저장소를 읽지 않는다."""
        held = list(groups or ())
        if not held:
            return ()
        return tuple(rbac.carried_idp_groups(held, await self.of(tenant)))
