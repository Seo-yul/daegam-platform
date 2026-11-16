"""서비스 어카운트와 그 토큰 (P53b 6.7절).

CI 처럼 사람 없이 MCP 를 부르는 프로그램의 계정이다. 테넌트 관리자(`members:write`)가 이름과 역할을
정해 만들고, 개인 토큰과 같은 규칙(D58)으로 토큰을 낸다. 토큰마다 ES256 키 하나이고 회수는 그 키를
JWKS 에서 빼는 것이며 상한은 개인 토큰과 함께 센다(`PersonalTokenService.mint`). 토큰의 클레임은
`sub` `sa:<계정 id>`, `tenant`, `groups` 그 테넌트의 고정 그룹, `daegam_roles` 계정의 역할이고
`preferred_username`, `act`, IdP 그룹은 없다. 화면과 감사는 계정의 이름을 이 저장소에서 읽는다.

`DAEGAM_SERVICE_ACCOUNTS` 가 누가 넓히는지(계정 만들기, 역할 바꾸기, 토큰 내기)를 정한다.

- `tenant-admin`: 그 테넌트의 `members:write` 를 가진 사람.
- `platform-admin`: 플랫폼 관리자만. 이미 낸 토큰은 그대로 쓴다.
- `off`: 아무도 못 한다(409 `service_accounts_off`). 접근 정리가 있는 토큰을 모두 회수한다
  (`accounts_off`).

좁히는 일(토큰 회수, 계정 지우기, 역할 낮추기)은 어느 값에서나 `members:write` 면 된다. 계정의
역할은 테넌트 역할(tenant-admin 까지)이라 만드는 사람(`members:write`, tenant-admin 이상) 자기
역할보다 높을 수 없다. 계정은 있는 테넌트(설정 문서나 소속 줄이 있다)에만 만든다. 토큰을 내려면
local 은 내는 사람의 비밀번호를, oidc 는 10분 안의 IdP 로그인을 다시 확인한다. 만료는 1일부터
`DAEGAM_PAT_MAX_DAYS` 까지이고 고르지 않으면 90일이다.

계정을 지우면 그 토큰을 모두 회수하고(`deleted`), 역할을 낮추면 사람의 역할이 내려갈 때처럼 그
계정의 살아 있는 토큰을 모두 회수한다(`role_changed`). 둘 다 빠른 길이고, 접근 정리는 계정이 없거나
토큰보다 낮은 역할인 토큰을 같은 까닭으로 회수한다. 만든 사람의 계정 사건은
계정과 토큰에 닿지 않는다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from daegam.auth import rbac
from daegam.auth.principal import Principal, token_username
from daegam.auth.service_token import account_sub
from daegam.config import SERVICE_ACCOUNTS_OFF, SERVICE_ACCOUNTS_PLATFORM_ADMIN, Settings
from daegam.errors import ServiceError
from daegam.services.access_revocation import AccessRevoker
from daegam.services.personal_tokens import (
    LABEL_MAX,
    PersonalTokenService,
    TokenHolder,
    check_name,
)
from daegam.store.base import new_id, utcnow
from daegam.store.memberships import MongoMembershipStore
from daegam.store.personal_tokens import ADMIN, DELETED, ROLE_CHANGED, PersonalToken
from daegam.store.service_accounts import MongoServiceAccountStore, ServiceAccount
from daegam.store.tenants import MongoTenantSettingsStore

# 고르지 않은 토큰의 수명. DAEGAM_PAT_MAX_DAYS 가 더 짧으면 그것이다
DEFAULT_LIFETIME = timedelta(days=90)
NAME_MAX = 64
# 감사와 회수 기록이 계정을 가리키는 resource 이름
WHAT = "service account"


class ServiceAccountError(ServiceError):
    """서비스 어카운트 API 의 오류."""


class ServiceAccountService:
    def __init__(
        self,
        accounts: MongoServiceAccountStore,
        tokens: PersonalTokenService,
        revoker: AccessRevoker,
        settings: Settings,
        *,
        tenant_settings: MongoTenantSettingsStore,
        memberships: MongoMembershipStore,
    ):
        self.accounts = accounts
        self.tokens = tokens
        self.revoker = revoker
        self.settings = settings
        self.tenant_settings = tenant_settings
        self.memberships = memberships

    # -- 누가 넓히는가 --------------------------------------------------------------------

    def widening_refusal(self, principal: Principal) -> ServiceAccountError | None:
        """계정을 만들거나 역할을 바꾸거나 토큰을 낼 수 없는 까닭. 라우트의 scope(`members:write`)
        뒤에 `DAEGAM_SERVICE_ACCOUNTS` 를 본다. 할 수 있으면 None 이다."""
        mode = self.settings.service_accounts
        if mode == SERVICE_ACCOUNTS_OFF:
            return ServiceAccountError(
                409, "service_accounts_off", "service accounts are turned off in this deployment"
            )
        if mode == SERVICE_ACCOUNTS_PLATFORM_ADMIN and not principal.is_platform_admin:
            return ServiceAccountError(
                403,
                "forbidden",
                "only platform admins manage service accounts in this deployment",
                role=rbac.PLATFORM_ADMIN,
            )
        return None

    def check_widening(self, principal: Principal) -> None:
        refusal = self.widening_refusal(principal)
        if refusal is not None:
            raise refusal

    # -- 계정 -----------------------------------------------------------------------------

    async def list(self, tenant: str) -> list[tuple[ServiceAccount, list[PersonalToken]]]:
        """테넌트의 계정과 각 계정의 토큰(새것부터, 회수됐거나 만료한 것도 30일 동안 남는다). 토큰은
        한 번에 읽는다."""
        accounts = await self.accounts.list(tenant)
        subs = [account_sub(a.id) for a in accounts]
        tokens = await self.tokens.list_for_subs(subs)
        return [(a, tokens[sub]) for a, sub in zip(accounts, subs, strict=True)]

    async def tokens_of(self, account: ServiceAccount) -> list[PersonalToken]:
        """계정의 토큰, 새것부터."""
        return await self.tokens.list_for(account_sub(account.id))

    async def get(self, account_id: str, tenant: str) -> ServiceAccount | None:
        return await self.accounts.get(account_id, tenant)

    async def _check_tenant(self, tenant: str) -> None:
        """있는 테넌트여야 한다. 설정 문서나 소속 줄이 있는 테넌트다. 없는 테넌트의 계정은 그
        테넌트가 생기는 날 그 테넌트를 여는 토큰을 남긴다."""
        if tenant in await self.tenant_settings.list_tenants():
            return
        if await self.memberships.list_for_tenant(tenant):
            return
        raise ServiceAccountError(404, "not_found", f"tenant {tenant} not found", tenant=tenant)

    async def create(
        self, principal: Principal, tenant: str, *, name: str, role: str
    ) -> ServiceAccount:
        self.check_widening(principal)
        name = check_name(name, field="name", limit=NAME_MAX)
        await self._check_tenant(tenant)
        now = utcnow()
        account = ServiceAccount(
            id=new_id(),
            tenant=tenant,
            name=name,
            role=role,
            created_by=principal.sub,
            created_by_name=token_username(principal.raw, principal.email) or None,
            created_at=now,
            updated_at=now,
            updated_by=principal.sub,
        )
        if not await self.accounts.insert(account):
            raise ServiceAccountError(
                409, "name_conflict", f"a service account named {name} exists", name=name
            )
        return account

    async def change_role(
        self, principal: Principal, account: ServiceAccount, role: str
    ) -> ServiceAccount:
        """역할을 바꾼다. 낮추면 계정의 살아 있는 토큰을 모두 바로 회수한다. 낮추는 것은 좁히는
        일이라 설정을 보지 않고, 같거나 올리는 것만 본다. 올린 역할은 토큰을 다시 낼 때 닿는다."""
        lowered = rbac.role_below(role, account.role)
        if not lowered:
            self.check_widening(principal)
        changed = await self.accounts.set_role(
            account.id, account.tenant, role, by=principal.sub, now=utcnow()
        )
        if changed is None:
            raise ServiceAccountError(404, "not_found", f"{WHAT} {account.id} not found")
        if lowered:
            await self.revoker.revoke_tokens_for(
                account_sub(account.id), ROLE_CHANGED, by=principal.sub, tenant=account.tenant
            )
        return changed

    async def delete(self, principal: Principal, account: ServiceAccount) -> None:
        """계정을 지우고 그 토큰을 모두 회수한다. 회수가 먼저다. 계정이 먼저 사라지면 접근 정리가
        다음 틱에 같은 토큰을 `deleted` 로 잡는다."""
        await self.revoker.revoke_tokens_for(
            account_sub(account.id), DELETED, by=principal.sub, tenant=account.tenant
        )
        await self.accounts.delete(account.id, account.tenant)

    # -- 토큰 -----------------------------------------------------------------------------

    async def issue(
        self,
        principal: Principal,
        account: ServiceAccount,
        *,
        label: str,
        expires_at: datetime | None,
        password: str | None,
        confirm_recent: Callable[[], Awaitable[None]],
    ) -> tuple[PersonalToken, str]:
        """계정의 토큰. (문서, 평문)이고 평문은 이 응답에만 있다. 설정을 먼저 보고, 그다음
        confirm_recent(oidc 의 최근 로그인, 라우트가 준다)를 본다. 넓힐 수 없는 사람을 IdP
        로그인으로 돌려보내지 않으려는 차례다. local 은 내는 사람의 비밀번호를 다시 확인한다."""
        self.check_widening(principal)
        await confirm_recent()
        now = utcnow()
        label = check_name(label, field="label", limit=LABEL_MAX)
        default = min(DEFAULT_LIFETIME, timedelta(days=self.settings.pat_max_days))
        ttl = self.tokens.lifetime(expires_at or now + default, now)
        await self.tokens.confirm_password(principal.sub, password)
        holder = TokenHolder(
            sub=account_sub(account.id),
            name=account.name,
            tv=0,
            role=account.role,
            claimed_name=None,
        )
        return await self.tokens.mint(holder, tenant=account.tenant, label=label, ttl=ttl, now=now)

    async def owned_token(self, account: ServiceAccount, token_id: str) -> PersonalToken | None:
        return await self.tokens.owned(account_sub(account.id), token_id)

    async def revoke(self, principal: Principal, token: PersonalToken) -> None:
        """관리자의 회수(`admin`). 이미 회수된 토큰이면 할 일이 없다."""
        await self.tokens.revoke(token, ADMIN, by=principal.sub)
