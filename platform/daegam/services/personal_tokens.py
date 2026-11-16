"""개인 MCP 토큰의 발급·목록·회수 (P32 5절·6절, P53b 6.5절).

플랫폼이 사람 토큰을 내는 모드(local, oidc)의 사람이 밖의 MCP 클라이언트에 넣는 데이터 플레인
토큰이다. 테넌트 하나와 이름과 만료(1일부터 `DAEGAM_PAT_MAX_DAYS` 까지)를 선택한다. 12시간짜리
제어면 토큰만으로 긴 자격을 만들지 못하게, local 은 현재 비밀번호를 다시 넣고 oidc 는 10분 안에
IdP 에서 로그인했어야 한다(P53b 6.4절, 라우트가 `api/deps.py` `require_recent_sign_in` 으로
본다). 틀린 비밀번호는 로그인 잠금에 센다(`LocalAuthService.confirm_password`). 틀리면 403 이다.
401 이면 화면의 401 훅이 세션을 지운다.

사람은 local 이면 사용자 문서이고 `tv` 가 그 `token_version` 이다. oidc 면 사람 기록(`idp_people`)
이고 `tv` 는 0 이다. 그 기록이 긴 한도 안에 확인됐어야 하고, 아니면 403 `reauth_required` 다. oidc
토큰은 사람 기록의 IdP 그룹 가운데 그 테넌트의 Tool 규칙이나 노출 Agent 가 이름 댄 것을
`daegam-group:<tenant>:<group>` 꼴로 싣는다(`services/named_groups.py`, 6.6절). 토큰의 테넌트
그룹은 언제나 고정 그룹 `daegam-<tenant>` 이다(6.1절). `svc:` 나 `sa:` 로 시작하는 사람의 sub 은
토큰을 받지 못한다.

발급 순서는 비밀번호나 사람 기록의 확인(그때 읽은 `token_version` 이 문서의 `tv`), 소속 확인, 상한,
슬롯 잡기, 서명, 문서 넣기, 상한 다시 세기다. 그 사이에 비밀번호 변경·계정 막기·멤버 교체가
끼어들어 훅이 아직 없는 토큰을 못 봐도, 접근 정리가 다음 틱에 문서의 `tv` 와 소속을 지금 값과 맞대
회수한다. 상한부터의 걸음은 서비스 어카운트의 토큰도 함께 쓴다(`mint`, 6.7절).

상한은 한 사람(또는 서비스 어카운트)의 활성 토큰, 한 시간 발급, 플랫폼 전체 활성 토큰이다. 전체
상한이 JWKS 의 크기를 1MB 안팎에 묶는다. 앞의 검사와 문서 넣기 사이는 원자적이지 않아 동시에 온
발급이 모두 앞의 검사를 지날 수 있다. 그래서 문서를 넣은 뒤 자기를 빼고 다시 세어, 넘쳤으면 그
발급을 되돌리고 429 를 낸다. 가장 늦게 센 발급은 먼저 넣은 모든 발급을 보므로 상한을 넘겨 남는
토큰은 없다. 경합이 심하면 모두 되돌려질 수는 있다. 따뜻한 슬롯이 없어도 발급은 거절하지 않고
`ready_at` 을 준다.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta

from daegam.auth import rbac
from daegam.auth.pat_keys import sign_personal_token
from daegam.auth.recent import REAUTH_REQUIRED
from daegam.auth.service_token import InternalIssuer, is_person_sub
from daegam.auth.signer import tenant_token_claims
from daegam.config import Settings
from daegam.errors import ServiceError, retry_after_header
from daegam.services.access_revocation import AccessRevoker
from daegam.services.local_auth import LocalAuthService
from daegam.services.named_groups import NamedGroups
from daegam.store.base import from_epoch, naive_utc, utcnow
from daegam.store.idp_people import MongoIdpPeopleStore
from daegam.store.memberships import MongoMembershipStore
from daegam.store.pat_keys import MongoPatKeyStore
from daegam.store.personal_tokens import (
    PURGE_AFTER,
    MongoPersonalTokenStore,
    PersonalToken,
    RevokeReason,
)
from daegam.store.tenants import default_mcp_group
from daegam.store.users import User

# 상한 (P32 4절). 한도 계산과 JWKS 크기의 재료라 설정으로 흔들지 않는다
MAX_LIVE_PER_PERSON = 20
MAX_ISSUED_PER_WINDOW = 10
ISSUE_WINDOW = timedelta(hours=1)
MAX_LIVE_TOTAL = 5000
MIN_LIFETIME = timedelta(days=1)
# 화면이 계산한 만료와 서버 시계 사이의 틈. 경계의 선택을 거절하지 않게 한다
EXPIRY_LEEWAY = timedelta(minutes=5)
LABEL_MAX = 64


class PersonalTokenError(ServiceError):
    """개인 토큰 API 의 오류. 비밀번호 확인의 오류는 `LocalAuthError` 로 그대로 올라온다."""


def check_name(raw: str, *, field: str, limit: int) -> str:
    """토큰이나 서비스 어카운트의 이름. 앞뒤 공백을 빼고 1~limit 자의 보이는 글자여야 한다. 아니면
    422 `invalid_value` 다."""
    name = raw.strip()
    if not name or len(name) > limit or not name.isprintable():
        raise ServiceError(
            422,
            "invalid_value",
            f"{field} must be 1 to {limit} printable characters",
            field=field,
        )
    return name


def _limit(detail: str, limit: str, *, retry_after: timedelta | None = None) -> PersonalTokenError:
    """429 token_limit. 시간이 지나면 풀리는 상한만 Retry-After(초)를 싣는다."""
    headers = None if retry_after is None else retry_after_header(retry_after)
    return PersonalTokenError(429, "token_limit", detail, headers=headers, limit=limit)


@dataclass(frozen=True)
class TokenHolder:
    """토큰을 받는 쪽. 사람이거나 서비스 어카운트다. name 은 문서와 화면의 이름이고,
    claimed_name 은 토큰의 `preferred_username` 이다(서비스 어카운트는 None). idp_groups 는 토큰이
    테넌트 이름을 붙여 싣는 IdP 그룹의 원래 이름이다."""

    sub: str
    name: str
    tv: int
    role: str | None
    claimed_name: str | None
    idp_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfirmedPerson:
    """`confirm_person` 이 확인한 사람. local 은 사용자 문서, oidc 는 사람 기록의 값이다."""

    sub: str
    name: str
    tv: int
    claimed_name: str | None
    # oidc 사람 기록의 IdP 그룹. local 은 None 이다
    idp_groups: list[str] | None = None


class PersonalTokenService:
    """플랫폼이 사람 토큰을 내는 모드(local, oidc)에서만 있다. 서명 키와 사람 발급자는 플랫폼
    발급자(`InternalIssuer`)의 것이다. local_auth 는 local 모드에만 있다."""

    def __init__(
        self,
        tokens: MongoPersonalTokenStore,
        slots: MongoPatKeyStore,
        memberships: MongoMembershipStore,
        issuer: InternalIssuer,
        revoker: AccessRevoker,
        settings: Settings,
        *,
        people: MongoIdpPeopleStore,
        named: NamedGroups,
        local_auth: LocalAuthService | None = None,
    ):
        self.tokens = tokens
        self.slots = slots
        self.memberships = memberships
        self.issuer = issuer
        self.revoker = revoker
        self.settings = settings
        self.people = people
        self.named = named
        self.local_auth = local_auth

    async def issue(
        self, sub: str, *, tenant: str, label: str, expires_at: datetime, password: str | None
    ) -> tuple[PersonalToken, str]:
        """(문서, 평문 토큰). 평문은 이 응답에만 있다. password 는 local 모드에서만 본다."""
        now = utcnow()
        label = check_name(label, field="label", limit=LABEL_MAX)
        ttl = self.lifetime(expires_at, now)
        if not is_person_sub(sub):
            raise PersonalTokenError(403, "forbidden", "this subject cannot hold personal tokens")
        holder = await self._holder(sub, tenant, password, now)
        return await self.mint(holder, tenant=tenant, label=label, ttl=ttl, now=now)

    async def _holder(
        self, sub: str, tenant: str, password: str | None, now: datetime
    ) -> TokenHolder:
        """확인한 사람과 소속. 발급 때의 역할을 싣고 토큰 문서에도 남긴다. 낮아지면 회수한다
        (`role_changed`, P52 3절). local 은 사용자 문서, oidc 는 사람 기록이다."""
        person = await self.confirm_person(sub, password, now)
        await self._check_member(person.sub, tenant)
        holder = TokenHolder(
            sub=person.sub,
            name=person.name,
            tv=person.tv,
            role=await self.memberships.role_in(person.sub, tenant),
            claimed_name=person.claimed_name,
        )
        if self.local_auth is not None:
            return holder
        return dataclasses.replace(
            holder, idp_groups=await self.named.carried(tenant, person.idp_groups)
        )

    async def confirm_person(
        self, sub: str, password: str | None, now: datetime | None = None
    ) -> ConfirmedPerson:
        """오래 가는 자격을 만들 사람. local 은 지금 비밀번호를 다시 확인한 사용자 문서이고(그때
        읽은 `token_version` 이 tv), oidc 는 긴 한도 안에 확인된 사람 기록이다(아니면 403
        `reauth_required`). 개인 토큰과 Teams 연결 코드(P47 6.3절)가 함께 쓴다."""
        if self.local_auth is not None:
            user = await self._confirm_local(self.local_auth, sub, password)
            return ConfirmedPerson(user.id, user.username, user.token_version, user.username)
        record = await self.people.get(sub)
        oldest = (now or utcnow()) - self.settings.idp_confirm_bound
        if record is None or not record.confirmed_since(oldest):
            raise PersonalTokenError(
                403, REAUTH_REQUIRED, "the IdP has not confirmed this person recently"
            )
        name = record.username or None
        return ConfirmedPerson(sub, name or sub, 0, name, record.groups)

    async def confirm_password(self, sub: str, password: str | None) -> None:
        """local 모드면 sub 의 현재 비밀번호를 다시 확인한다. oidc 모드는 할 일이 없다. 라우트가
        최근 로그인을 본다. 서비스 어카운트의 토큰을 내는 관리자가 지난다 (P53b 6.7절)."""
        if self.local_auth is not None:
            await self._confirm_local(self.local_auth, sub, password)

    async def _confirm_local(self, auth: LocalAuthService, sub: str, password: str | None) -> User:
        """sub 의 현재 비밀번호를 다시 확인하고 그때 읽은 사용자 문서를 돌려준다. 그
        `token_version` 이 개인 토큰 문서의 tv 다. 없으면 422, 틀리면 403 이고 로그인 잠금에
        센다."""
        if not password:
            raise PersonalTokenError(422, "invalid_value", "password is required", field="password")
        user = await auth.users.get(sub)
        if user is None:
            raise PersonalTokenError(403, "invalid_credentials", "password is wrong")
        return await auth.confirm_password(
            user.username, password, status=403, detail="password is wrong"
        )

    async def mint(
        self, holder: TokenHolder, *, tenant: str, label: str, ttl: int, now: datetime
    ) -> tuple[PersonalToken, str]:
        """상한을 보고 슬롯을 잡아 서명하고 문서를 넣는다. 사람의 개인 토큰과 서비스 어카운트의
        토큰(P53b 6.7절)이 함께 쓴다. 그룹은 테넌트의 고정 그룹이다 (6.1절)."""
        await self._check_limits(holder.sub, now)
        group = default_mcp_group(tenant)
        claims = tenant_token_claims(
            issuer=self.issuer.human_issuer,
            audience=self.settings.mcp_audience,
            sub=holder.sub,
            ttl=ttl,
            tenant=tenant,
            group=group,
            username=holder.claimed_name,
            role=holder.role,
            idp_groups=holder.idp_groups,
        )
        expires = from_epoch(claims["exp"])
        signer = self.issuer.signer
        slot = await self.slots.claim(
            signer.kid, token_id=claims["jti"], expires_at=expires, now=now
        )
        signed = sign_personal_token(signer, slot.id, claims)
        token = PersonalToken(
            id=claims["jti"],
            sub=holder.sub,
            username=holder.name,
            tenant=tenant,
            group=group,
            role=holder.role,
            idp_groups=list(holder.idp_groups) or None,
            label=label,
            kid=slot.id,
            iss=claims["iss"],
            tv=holder.tv,
            created_at=now,
            expires_at=expires,
            ready_at=slot.ready_at(now),
            purge_at=expires + PURGE_AFTER,
        )
        await self.tokens.insert(token)
        await self._settle_limits(token, now)
        return token, signed

    def lifetime(self, expires_at: datetime, now: datetime) -> int:
        """만료까지의 초. 지금부터 1일 미만이거나 상한을 넘으면 422 다."""
        expires_at = naive_utc(expires_at)
        longest = timedelta(days=self.settings.pat_max_days)
        if not now + MIN_LIFETIME - EXPIRY_LEEWAY <= expires_at <= now + longest + EXPIRY_LEEWAY:
            raise PersonalTokenError(
                422,
                "invalid_value",
                f"expires_at must be 1 to {self.settings.pat_max_days} days from now",
                field="expires_at",
            )
        return int((expires_at - now).total_seconds())

    async def _check_member(self, sub: str, tenant: str) -> None:
        """그 테넌트의 소속 줄이 있어야 한다. 역할은 묻지 않는다. 데이터 플레인은 역할이 아니라
        테넌트 그룹으로 가르기 때문이다. platform-admin 의 `*` 는 소속이 아니다."""
        if not rbac.holds_tenant(await self.memberships.tenants_of(sub), tenant):
            raise PersonalTokenError(
                403, "not_a_member", f"not a member of tenant {tenant}", tenant=tenant
            )

    async def _check_limits(self, sub: str, now: datetime, *, issued: str | None = None) -> None:
        """상한 셋. issued 를 주면 방금 넣은 그 토큰을 빼고 센다."""
        if await self.tokens.count_live(now, sub=sub, exclude=issued) >= MAX_LIVE_PER_PERSON:
            raise _limit(f"at most {MAX_LIVE_PER_PERSON} active tokens per person", "person")
        recent = await self.tokens.created_since(sub, now - ISSUE_WINDOW, exclude=issued)
        if len(recent) >= MAX_ISSUED_PER_WINDOW:
            # 창에서 가장 먼저 빠지는 발급이 빠지면 하나를 더 받을 수 있다
            frees_at = recent[-MAX_ISSUED_PER_WINDOW] + ISSUE_WINDOW
            raise _limit(
                f"at most {MAX_ISSUED_PER_WINDOW} tokens per hour",
                "hourly",
                retry_after=frees_at - now,
            )
        if await self.tokens.count_live(now, exclude=issued) >= MAX_LIVE_TOTAL:
            raise _limit(f"the platform holds {MAX_LIVE_TOTAL} active tokens", "platform")

    async def _settle_limits(self, token: PersonalToken, now: datetime) -> None:
        """넣은 뒤 다시 센다. 넘쳤으면 문서와 슬롯을 지우고 429 를 그대로 올린다. 평문이 나간 적
        없는 토큰이라 회수로 남기지 않는다. 슬롯 삭제가 실패해도 접근 정리가 주인 없는 슬롯을
        지운다."""
        try:
            await self._check_limits(token.sub, now, issued=token.id)
        except PersonalTokenError:
            await self.tokens.delete(token.id)
            await self.slots.remove_kids([token.kid])
            raise

    async def list_for(self, sub: str) -> list[PersonalToken]:
        """sub 의 토큰, 새것부터. 회수됐거나 만료한 것도 30일 동안 남는다."""
        return await self.tokens.list_for_sub(sub)

    async def list_for_subs(self, subs: list[str]) -> dict[str, list[PersonalToken]]:
        """subs 마다의 토큰, 새것부터. 한 번에 읽는다."""
        return await self.tokens.list_for_subs(subs)

    async def owned(self, sub: str, token_id: str) -> PersonalToken | None:
        """sub 의 토큰. 없거나 남의 것이면 None 이다. 라우터가 둘을 같은 404 로 낸다."""
        token = await self.tokens.get(token_id)
        return token if token is not None and token.sub == sub else None

    async def revoke(self, token: PersonalToken, reason: RevokeReason, *, by: str) -> None:
        """본인(`user`)이나 관리자(`admin`)의 회수. 이미 회수된 토큰이면 할 일이 없다."""
        await self.revoker.revoke([token.id], reason, by=by)
