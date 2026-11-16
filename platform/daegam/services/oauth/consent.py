"""MCP 클라이언트 인가의 동의 (P33 7절·8절).

동의 화면은 서명한 요청 핸들을 본문으로 되돌려 보내고, 여기서 핸들을 풀어 다시 확인한다. 서명이
틀리면 404, 만료면 410 `request_expired`, 이미 쓰였으면 409 `request_used` 다. 서명이 맞으면 감사
행이 핸들의 클라이언트를 싣는다(`OAuthAudit`). inspect 는 아무것도 저장하지 않는다. 처음 저장하는
것이 허용과 거절의 핸들 사용 기록이고, 허용이면 인가 코드도 저장한다. 허용은 이 순서로 본다.

1. 핸들을 다시 확인한다. 만료했거나 쓰인 핸들이면 다시 로그인해도 쓸 수 없으므로 최근 인증보다
   먼저 본다. 인가 서버가 잠들었으면(P53b 6.5절) 503 `temporarily_unavailable` 이고, 사람의 sub 이
   `svc:` 나 `sa:` 로 시작하면 403 `forbidden` 이다.
2. 최근 인증(`Approver.confirm_recent`). local 의 제어면 토큰은 갱신 없이 로그인과 비밀번호 변경
   때마다 새로 나가므로 그 `iat` 가 곧 마지막 비밀번호 입력 시각이다. oidc 는 UI 가 함께 보낸 ID
   토큰의 `auth_time` 이다(P53b 6.4절). 10분(시계 여유 30초)보다 오래됐으면 403 `reauth_required`
   이고 아무것도 쓰지 않는다. 12시간짜리 제어면 토큰을 훔친 것만으로 30일 연결을 만드는 길을 막는다.
3. `resource` 를 다시 푼다. 그 사이 노출이 꺼졌을 수 있다. 여기부터의 감사 행은 리소스의 테넌트로
   남는다.
4. 그 테넌트의 소속이어야 한다(`rbac.holds_tenant`, `*` 는 소속이 아니다). 아니면 403
   `not_a_member` 이고 테넌트 이름을 싣지 않는다.
5. 사용 기록을 넣는다. 같은 핸들이 동시에 두 번 와도 기록의 `_id` 가 겹쳐 하나만 지난다.
6. 한 사람의 허용은 10분에 10번까지다. 기록을 넣은 뒤 방금 넣은 것까지 세고, 넘으면 그 기록을
   지우고 429 `consent_limit` 이다. 세기와 넣기가 한 걸음이 아니어도 동시에 온 허용이 함께 상한을
   넘지 못한다. 마지막으로 센 허용이 먼저 지난 허용의 기록을 모두 보기 때문이다. 걸린 허용은 핸들을
   쓰지 않는다.
7. 인가 코드를 만든다.

거절은 연결을 만들지 않으므로 최근 인증과 소속을 묻지 않는다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from daegam.auth import rbac
from daegam.auth.oauth_redirect import redirect_with
from daegam.auth.recent import REAUTH_DETAIL, REAUTH_REQUIRED, signed_in_recently
from daegam.auth.service_token import is_person_sub
from daegam.errors import ServiceError
from daegam.services.oauth.audit import OAuthAudit
from daegam.services.oauth.authorize import ConsentRequest, OAuthAuthorizer, OAuthResource
from daegam.services.oauth.clients import OAuthClient, client_ref
from daegam.services.oauth.errors import ACCESS_DENIED, TEMPORARILY_UNAVAILABLE
from daegam.store.base import from_epoch, issue_token
from daegam.store.memberships import MongoMembershipStore
from daegam.store.oauth_codes import CODE_TTL, PURGE_AFTER, MongoOAuthCodeStore, OAuthCode
from daegam.store.oauth_consent_uses import APPROVE, DENY, Decision, MongoOAuthConsentUseStore

# 17절의 코드 상수. 회수 한도와 남용 방어의 재료라 설정으로 흔들지 않는다. 최근 로그인의 10분은
# `auth/recent.py` 의 `RECENT_AUTH` 다
CONSENT_LIMIT = 10
CONSENT_WINDOW = timedelta(minutes=10)


class ConsentError(ServiceError):
    """동의 화면이 부르는 라우트의 오류. 플랫폼의 오류 모양으로 나간다."""


@dataclass(frozen=True)
class Approver:
    """허용하는 사람 (P53b 6.5절). tv 는 인가 코드와 연결에 남는 값이다. local 은 사용자 문서의
    `token_version`, oidc 는 0 이다. confirm_recent 는 지금(epoch 초)을 받아 최근 로그인을 보고,
    아니면 403 `reauth_required` 를 낸다."""

    sub: str
    tv: int
    confirm_recent: Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ConsentView:
    """inspect 의 재료. 라우터가 응답 모양으로 옮긴다."""

    request: ConsentRequest
    client: OAuthClient
    resource: OAuthResource
    member: bool


def _minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)


def _used() -> ConsentError:
    return ConsentError(409, "request_used", "the consent request has already been answered")


def token_iat_check(token_iat: Any) -> Callable[[float], Awaitable[None]]:
    """local 모드의 최근 인증. 제어면 토큰의 iat 가 10분(시계 여유 30초) 안이어야 한다
    (`auth/recent.py`)."""

    async def confirm(now: float) -> None:
        if not signed_in_recently(token_iat, now):
            raise ConsentError(403, REAUTH_REQUIRED, REAUTH_DETAIL)

    return confirm


class OAuthConsent:
    def __init__(
        self,
        authorizer: OAuthAuthorizer,
        uses: MongoOAuthConsentUseStore,
        codes: MongoOAuthCodeStore,
        memberships: MongoMembershipStore,
    ):
        self.authorizer = authorizer
        self.uses = uses
        self.codes = codes
        self.memberships = memberships

    async def inspect(self, handle: str, sub: str, audit: OAuthAudit) -> ConsentView:
        """동의 화면의 내용. CIMD 문서를 받지 못하면 503 client_metadata_unavailable 이다."""
        request = await self._open(handle, audit)
        resource = await self.authorizer.resource([request.resource])
        client = await self.authorizer.clients.resolve(request.client_id)
        return ConsentView(request, client, resource, await self._member(sub, resource.tenant))

    async def approve(self, handle: str, approver: Approver, audit: OAuthAudit) -> str:
        """허용. 인가 코드를 실은 돌아갈 곳이다. 순서는 모듈 머리글 그대로다."""
        now = self.authorizer.clock()
        request = await self._open(handle, audit)
        if self.authorizer.dormant:
            raise ConsentError(
                503, TEMPORARILY_UNAVAILABLE, "the authorization server issues nothing for now"
            )
        if not is_person_sub(approver.sub):
            raise ConsentError(403, "forbidden", "this subject cannot hold login connections")
        await approver.confirm_recent(now)
        resource = await self.authorizer.resource([request.resource])
        audit.tenant = resource.tenant
        if not await self._member(approver.sub, resource.tenant):
            raise ConsentError(403, "not_a_member", "not a member of the tenant of this resource")
        await self._record(request, approver.sub, APPROVE, now)
        await self._check_limit(request, approver.sub, now)
        code = await self._issue_code(request, approver, resource, from_epoch(now))
        answer = {"code": code, "state": request.state, "iss": self.authorizer.iss}
        return redirect_with(request.redirect_uri, answer)

    async def deny(self, handle: str, sub: str, audit: OAuthAudit) -> str:
        """거절. `error=access_denied` 를 실은 돌아갈 곳이다."""
        request = await self._open(handle, audit)
        await self._record(request, sub, DENY, self.authorizer.clock())
        answer = {"error": ACCESS_DENIED, "state": request.state, "iss": self.authorizer.iss}
        return redirect_with(request.redirect_uri, answer)

    async def _open(self, handle: str, audit: OAuthAudit) -> ConsentRequest:
        request = self.authorizer.open_request(handle)
        if request is None:
            raise ConsentError(404, "not_found", "the consent request is not valid")
        audit.client_ref = client_ref(request.client_id)
        if request.exp <= self.authorizer.clock():
            raise ConsentError(410, "request_expired", "the consent request has expired")
        if await self.uses.used(request.jti):
            raise _used()
        return request

    async def _member(self, sub: str, tenant: str) -> bool:
        return rbac.holds_tenant(await self.memberships.tenants_of(sub), tenant)

    async def _record(
        self, request: ConsentRequest, sub: str, decision: Decision, now: float
    ) -> None:
        """핸들 사용 기록. 기록은 핸들이 만료한 뒤에도 남는다(`KEEP_AFTER_HANDLE`)."""
        recorded = await self.uses.record(
            request.jti,
            sub=sub,
            decision=decision,
            now=from_epoch(now),
            handle_exp=from_epoch(request.exp),
        )
        if not recorded:
            raise _used()

    async def _check_limit(self, request: ConsentRequest, sub: str, now: float) -> None:
        """방금 넣은 기록까지 세어 창 안의 허용이 상한을 넘으면 그 기록을 지우고 429 다."""
        since = from_epoch(now) - CONSENT_WINDOW
        if await self.uses.approvals_since(sub, since) > CONSENT_LIMIT:
            await self.uses.forget(request.jti)
            raise ConsentError(
                429,
                "consent_limit",
                f"at most {CONSENT_LIMIT} approvals per {_minutes(CONSENT_WINDOW)} minutes",
            )

    async def _issue_code(
        self, request: ConsentRequest, approver: Approver, resource: OAuthResource, now: datetime
    ) -> str:
        """인가 코드 (8절). 평문은 돌려주고 sha256 만 저장한다. `tv` 는 local 이면 방금 읽은 사용자
        문서의 것이다."""
        plain, digest = issue_token()
        code = OAuthCode(
            id=digest,
            client_id=request.client_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            sub=approver.sub,
            tv=approver.tv,
            tenant=resource.tenant,
            resource=resource.url,
            scope=request.scope,
            created_at=now,
            expires_at=now + CODE_TTL,
            purge_at=now + CODE_TTL + PURGE_AFTER,
        )
        await self.codes.insert(code)
        return plain
