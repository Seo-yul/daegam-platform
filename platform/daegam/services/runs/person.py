"""실행이 묶인 사람의 확인 (P41 6절).

실행은 소스를 마지막으로 만들거나 바꾸거나 다시 켠 사람의 이름으로 돈다. 그 사람을 묶을 때와 시작할
때마다 본다.

- **묶을 때**는 그 테넌트의 소속 줄이 `agent:write` 와 `agent:chat` 을 주는 role 이어야 한다.
  platform-admin 의 `*` 줄만으로는 묶이지 않는다(D58). 그런 사람에게 묶으면 시작마다 멈춘다.
- **시작할 때**는 local 모드에서 사용자 문서와 묶을 때의 `tv` 를 `lost_access` 로 보고, oidc
  모드에서 사람 기록을 긴 한도로 `lost_idp_access` 로 보고(P53b 6.3절), 두 모드에서 묶을 때와 같은
  소속 줄을 본다. 접근을 잃은 원인은 `account`(지웠거나 막았다), `password`(`tv` 가 다르다),
  `unconfirmed`(마지막 IdP 확인이 긴 한도를 넘었다), `role`(소속이 없거나 role 이 모자란다)이다.
- oidc 모드에서 기록이 한 번도 없었던 사람(업그레이드 뒤 아직 요청하지 않았다)은 한도로 멈추지
  않고 지금처럼 Agent 의 이름으로 돈다(`runs_as_person`). 그래서 업그레이드가 아무것도 멈추지
  않는다. 비운 기록은 기록이 있었던 것이다.
- header 모드는 아무도 확인하지 않으므로(P53 5.6절) 묶을 때도 시작할 때도 보지 않는다.

다른 Agent 가 Tool 로 부른 실행(P46 6.4절)은 묶인 사람이 아니라 부른 턴의 사람으로 돈다. 그
사람에게는 `agent:chat` 만 요구한다. 그 Agent 와 직접 대화할 수 있는 사람이면 된다. 뿌리 턴이
채팅이면 oidc 모드의 한도는 긴 한도가 아니라 대화형 한도다(`interactive`). 호출할 때와 그 실행의
턴을 시작할 때마다 `lost_for` 가 출처에 맞는 규칙으로 본다.

Teams 턴(P47 6.3절)도 같은 확인을 쓴다. 요구하는 scope 는 인자이고(실행은 `RUN_SCOPES`, Teams 는
`agent:chat`), `strict` 면 더 엄하다. oidc 모드에서 기록이 없는 사람도 `unconfirmed` 이고, local
모드에서 마지막 로그인(`last_login_at`)이 긴 한도를 넘었으면 `unconfirmed` 다. Teams 의 연결은
사람이 웹에 오지 않아도 쓰이므로 local 에도 끝을 두려는 것이다.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from daegam.auth import rbac
from daegam.config import Settings
from daegam.services.access_revocation import HeldAccess, lost_access, lost_idp_access
from daegam.store.agent_runs import ActsAs, AgentRun
from daegam.store.base import utcnow
from daegam.store.idp_people import MongoIdpPeopleStore
from daegam.store.memberships import Membership, MongoMembershipStore
from daegam.store.personal_tokens import (
    DELETED,
    DISABLED,
    MEMBERSHIP,
    PASSWORD,
    UNCONFIRMED,
    Revocation,
)
from daegam.store.users import MongoUserStore

# 묶인 사람이 접근을 잃어 소스를 멈추고 실행을 끝낸 까닭
LOST_ACCESS = "acts_as_lost_access"
# 묶인 사람의 마지막 IdP 확인이 긴 한도를 넘어 끝낸 실행의 까닭 (P53b 6.3절). 소스는 LOST_ACCESS 와
# 원인 unconfirmed 로 멈추고, 화면은 실행과 소스 모두 그 사람이 며칠 동안 로그인하지 않았다고 말한다
ACTS_AS_UNCONFIRMED = "acts_as_unconfirmed"
# 실행을 그 사람의 이름으로 돌릴 수 없어 끝낸 까닭이다. 평가 실행과 다른 Agent 가 부른 실행이 쓰고,
# 호출 경로는 같은 코드로 거절한다
ACTS_AS_UNAVAILABLE = "acts_as_unavailable"
CAUSE_ACCOUNT = "account"
CAUSE_PASSWORD = "password"
CAUSE_ROLE = "role"
CAUSE_UNCONFIRMED = "unconfirmed"
# 실행에 묶이는 role 이 주어야 하는 scope
RUN_SCOPES = frozenset({"agent:write", "agent:chat"})
# 다른 Agent 가 부른 실행의 사람에게 요구하는 scope (P46 6.4절)
CALL_SCOPES = frozenset({"agent:chat"})
_CAUSES: dict[str, str] = {
    DELETED: CAUSE_ACCOUNT,
    DISABLED: CAUSE_ACCOUNT,
    PASSWORD: CAUSE_PASSWORD,
    MEMBERSHIP: CAUSE_ROLE,
    UNCONFIRMED: CAUSE_UNCONFIRMED,
}


def cause_of(reason: Revocation) -> str:
    """접근 회수의 까닭을 실행 소스가 남기는 원인으로 옮긴다."""
    return _CAUSES.get(reason, CAUSE_ACCOUNT)


def ended_reason(cause: str) -> str:
    """묶인 사람이 접근을 잃어 끝낸 실행의 까닭. 원인이 긴 한도면 `acts_as_unconfirmed`, 그 밖은
    `acts_as_lost_access` 다."""
    return ACTS_AS_UNCONFIRMED if cause == CAUSE_UNCONFIRMED else LOST_ACCESS


class BoundPersonCheck:
    def __init__(
        self,
        users: MongoUserStore,
        memberships: MongoMembershipStore,
        settings: Settings,
        people: MongoIdpPeopleStore,
    ):
        self.users = users
        self.memberships = memberships
        self.settings = settings
        self.people = people

    @property
    def confirms(self) -> bool:
        """사람을 확인하는 모드인가. header 모드는 아무도 확인하지 않는다."""
        return self.settings.token_auth

    async def may_bind(self, sub: str, tenant: str) -> bool:
        """sub 의 그 테넌트 소속 줄이 실행에 묶일 role 인가. platform-admin 의 `*` 줄은 소속이
        아니다."""
        if not self.confirms:
            return True
        return _grants(await self.memberships.list_for_sub(sub), tenant, RUN_SCOPES)

    async def lost(
        self,
        acts_as: ActsAs,
        tenant: str,
        *,
        scopes: Collection[str] = RUN_SCOPES,
        strict: bool = False,
        interactive: bool = False,
    ) -> str | None:
        """묶인 사람이 접근을 잃은 원인. 아직 쓸 수 있으면 None 이다. 소속 줄은 한 번 읽는다.
        scopes 는 그 테넌트의 소속 줄이 주어야 하는 scope 다. strict 는 Teams 턴의 확인이다.
        interactive 면 oidc 모드의 한도가 대화형 한도다."""
        if not self.confirms:
            return None
        rows = await self.memberships.list_for_sub(acts_as.sub)
        tenants = [m.tenant for m in rows]
        held = HeldAccess.of(acts_as.sub, acts_as.tv, tenant)
        reason: Revocation | None = None
        if self.settings.auth_mode == "local":
            user = await self.users.get(acts_as.sub)
            reason = lost_access(held, user, tenants)
            # local 모드의 끝은 Teams 턴만 보고 늘 긴 한도다
            oldest = self._oldest(self.settings.idp_confirm_bound)
            stale = user is not None and (user.last_login_at is None or user.last_login_at < oldest)
            if reason is None and strict and stale:
                reason = UNCONFIRMED
        elif self.settings.auth_mode == "oidc":
            person = await self.people.get(acts_as.sub)
            bound = (
                self.settings.interactive_bound if interactive else self.settings.idp_confirm_bound
            )
            if person is not None or strict:
                reason = lost_idp_access(held, person, tenants, oldest=self._oldest(bound))
        if reason is not None:
            return cause_of(reason)
        return None if _grants(rows, tenant, scopes) else CAUSE_ROLE

    def _oldest(self, bound: timedelta) -> datetime:
        """bound 한도에서 마지막 확인이 이보다 앞이면 끝난 것이다. 위임과 같은 끝이다. 토큰 한
        수명이 남지 않으면 위임이 거절되어 Agent 의 이름으로 돌게 되므로 시작에서 멈춘다."""
        return utcnow() - bound + timedelta(seconds=self.settings.turn_token_ttl_seconds)

    async def lost_for(self, run: AgentRun) -> str | None:
        """실행의 사람이 접근을 잃은 원인을 그 출처의 규칙으로 본다. 다른 Agent 가 부른 실행은
        `CALL_SCOPES` 와 뿌리 턴의 한도이고 그 밖은 `RUN_SCOPES` 와 긴 한도다."""
        if run.caller is None:
            return await self.lost(run.acts_as, run.tenant)
        return await self.lost(
            run.acts_as, run.tenant, scopes=CALL_SCOPES, interactive=not run.caller.long_bound
        )

    async def runs_as_person(self, sub: str) -> bool:
        """사람을 확인하는 모드에서 sub 의 실행을 그 사람의 이름으로 돌리는가. local 모드는 늘
        그렇고, oidc 모드는 사람 기록이 한 번이라도 있었던 사람만이다. 없었던 사람의 실행은 Agent 의
        이름으로 돈다 (P53b 6.3절)."""
        if self.settings.auth_mode != "oidc":
            return True
        return await self.people.get(sub) is not None


def _grants(rows: list[Membership], tenant: str, scopes: Collection[str]) -> bool:
    """그 테넌트의 소속 줄이 scopes 를 모두 주는 role 인가."""
    roles = [m.role for m in rows if rbac.holds_tenant([m.tenant], tenant)]
    return set(scopes) <= rbac.scopes_for_roles(roles)


__all__ = [
    "ACTS_AS_UNAVAILABLE",
    "ACTS_AS_UNCONFIRMED",
    "CALL_SCOPES",
    "CAUSE_ACCOUNT",
    "CAUSE_PASSWORD",
    "CAUSE_ROLE",
    "CAUSE_UNCONFIRMED",
    "LOST_ACCESS",
    "RUN_SCOPES",
    "BoundPersonCheck",
    "cause_of",
    "ended_reason",
]
