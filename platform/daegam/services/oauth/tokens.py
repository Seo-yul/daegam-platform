"""MCP 클라이언트의 토큰, 갱신, 회수와 연결 목록 (P33 8절~12절).

`POST /api/oauth/token` 은 두 grant 를 받는다.

- **코드 교환.** 코드는 한 번만 쓴다. `oauth_codes` 의 findOneAndUpdate 한 번이 `used_at` 과 새 연결
  id 를 함께 찍고 그 id 로 연결을 만든다. 이미 쓰인 코드가 다시 오면 코드에 `reused_at` 을 찍고 그때
  찍힌 연결을 `code_reuse` 로 끊는다(OAuth 2.1). 첫 교환이 아직 연결을 넣기 전이라 그 회수가 헛돌 수
  있으므로, 첫 교환은 연결을 넣은 뒤 `reused_at` 을 다시 읽어 찍혔으면 제 연결을 스스로 끊는다.
  클라이언트, `redirect_uri`, PKCE S256, 사람과 소속(`AccessRevoker.holder_lost`, local 은 사용자
  문서이고 oidc 는 사람 기록이다), 리소스의 노출을 본 뒤 연결을 만든다.
- **갱신.** 값은 `<family_id>.<secret>` 이다. 가족을 `family_id` 로 찾고 `client_id` 를 먼저 본 뒤
  비밀을 지금 세대와 바로 앞 세대의 해시에 맞댄다. 지금 세대면 회전하고, 바로 앞 세대가 회전 뒤
  10초 안에 오면 그 요청만 거절하며(`refresh_reuse_grace`), 그 밖은 재사용이라 연결을 끊는다
  (`refresh_reuse`, RFC 9700). 회전하기 전에 접근 정리와 같은 규칙(`AccessRevoker.grant_reason`)과
  리소스의 노출(`resource_gone`)을 다시 보므로 훅이 놓친 계정 사건도 여기서 끊긴다.
- **회수(RFC 7009).** 갱신 토큰이면 그 가족을, 접근 토큰이면 서명을 확인하고 `sid` 의 연결을
  끊는다. 모르는 토큰은 할 일이 없다.

**잠든 인가 서버**(P53b 6.5절)의 토큰 엔드포인트는 코드 교환과 갱신에 503 `temporarily_unavailable`
과 `Retry-After` 로 답한다. 코드를 쓰지도, 갱신 토큰을 회전하지도, 가족을 끊지도 않는다. 회수는
그대로 된다.

접근 토큰은 플랫폼 RSA 키로 서명한 15분짜리 JWT 다. `aud` 는 공유 audience 와 동의한 리소스이고
`groups` 는 연결의 테넌트 고정 그룹과, oidc 모드면 사람 기록의 IdP 그룹 가운데 그 테넌트가 이름 댄
것이다(P53b 6.1절·6.6절). 그룹과 역할은 갱신마다 새로 싣는다. 토큰 요청의 `resource` 가 연결의 것과
다르거나 없으면 무시하고 연결의 리소스로 낸다. 권한이 넓어지지 않고, 첫 로그인의 토큰 요청에 인가
서버 주소를 보내는 클라이언트(Claude Code)를 넘긴다.

오류 설명에는 받은 값을 넣지 않는다. 감사 행이 누가 어느 클라이언트로 불렀는지 말하도록 서비스는
확인한 만큼 `OAuthAudit` 을 채우고 라우터가 그것을 `request.state` 로 옮긴다 (18절). 갱신이 연결을
끊으면 그 사유가 감사의 오류 코드다.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn

from jwt.utils import base64url_encode

from daegam.auth.local import DATA_PLANE_TOKEN_TTL, LocalVerifier
from daegam.auth.oidc import AuthError
from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import tenant_token_claims
from daegam.config import Settings
from daegam.errors import retry_after_header
from daegam.services.access_revocation import AccessRevoker, Holder
from daegam.services.named_groups import NamedGroups
from daegam.services.oauth.audit import OAuthAudit
from daegam.services.oauth.authorize import (
    SCOPE_OFFLINE,
    OAuthAuthorizer,
    OAuthResource,
    granted_scope,
    normalize_resource,
    scope_string,
)
from daegam.services.oauth.clients import AUTHORIZATION_CODE, REFRESH_TOKEN, OAuthClient
from daegam.services.oauth.errors import (
    INVALID_CLIENT,
    INVALID_GRANT,
    INVALID_REQUEST,
    INVALID_SCOPE,
    TEMPORARILY_UNAVAILABLE,
    UNSUPPORTED_GRANT_TYPE,
    OAuthError,
)
from daegam.store.base import hash_token, issue_token, match_token, new_id, utcnow
from daegam.store.oauth_codes import MongoOAuthCodeStore, OAuthCode
from daegam.store.oauth_grants import (
    ACTIVE,
    CLIENT,
    CODE_REUSE,
    EVICTED,
    PURGE_AFTER,
    REFRESH_REUSE,
    RESOURCE_GONE,
    GrantRevokeReason,
    MongoOAuthGrantStore,
    OAuthGrant,
)
from daegam.store.personal_tokens import DELETED
from daegam.store.tenants import default_mcp_group

logger = logging.getLogger("daegam.oauth.tokens")

# 접근 토큰의 수명(초). 회수의 한도가 곧 이 값이라 사람의 데이터 플레인 토큰과 같다 (10절)
ACCESS_TOKEN_SECONDS = DATA_PLANE_TOKEN_TTL
ACCESS_TOKEN_TTL = timedelta(seconds=ACCESS_TOKEN_SECONDS)
# 17절의 코드 상수. 회수 한도와 남용 방어의 재료라 설정으로 흔들지 않는다
REFRESH_TOKEN_TTL = timedelta(days=14)
REUSE_GRACE = timedelta(seconds=10)
MAX_LIVE_GRANTS = 50
# 잠든 인가 서버가 토큰 엔드포인트의 503 에 싣는 Retry-After. 잠은 운영자가 데이터 플레인 발급자를
# 바꿀 때까지라 정해진 끝이 없다. 접근 토큰 한 수명마다 다시 묻게 한다
DORMANT_RETRY_AFTER = timedelta(seconds=DATA_PLANE_TOKEN_TTL)
# 접근 토큰이 반드시 싣는 클레임. 회수가 `sid` 로 연결을 찾는다
ACCESS_TOKEN_CLAIMS = ("exp", "sub", "sid")
TOKEN_TYPE = "Bearer"
# 감사 행의 행동 이름 (18절). 한 라우트가 두 grant 를 받으므로 갈라 적는다
ACTION_CODE = "oauth_token_code"
ACTION_REFRESH = "oauth_token_refresh"
# 유예로 거절한 갱신의 감사 코드. 잦으면 탈취를 의심할 근거다
REFRESH_REUSE_GRACE = "refresh_reuse_grace"
# RFC 7636 4.1. unreserved 문자 43~128자다
_CODE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
# 갱신 토큰의 가족 id. 128비트 난수의 hex 다
FAMILY_ID_BYTES = 16
_FAMILY_ID = re.compile(f"[0-9a-f]{{{FAMILY_ID_BYTES * 2}}}")
_CURRENT = "current"
_PREVIOUS = "previous"


@dataclass(frozen=True)
class _Generation:
    """연결 문서의 두 해시를 `match_token` 이 받는 모양으로 넘기는 어댑터."""

    token_sha256: str
    name: str


def _required(form: Mapping[str, str], name: str) -> str:
    value = form.get(name)
    if not value:
        raise OAuthError(INVALID_REQUEST, f"{name} is required")
    return value


def _invalid_grant(description: str, *, audit_code: str | None = None) -> OAuthError:
    return OAuthError(INVALID_GRANT, description, audit_code=audit_code)


def _pkce_matches(verifier: str, challenge: str) -> bool:
    """BASE64URL(SHA256(verifier)) 가 challenge 와 같은가 (RFC 7636 4.6)."""
    digest = base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(digest, challenge.encode("ascii"))


def _split_refresh(token: str) -> tuple[str, str] | None:
    """갱신 토큰의 (family_id, secret). 그 꼴이 아니면 None 이다."""
    family_id, dot, secret = token.partition(".")
    if not (dot and secret and _FAMILY_ID.fullmatch(family_id)):
        return None
    return family_id, secret


def _generation(grant: OAuthGrant, secret: str) -> str | None:
    """제시한 비밀이 맞는 세대(`_CURRENT`·`_PREVIOUS`). 어느 것과도 맞지 않으면 None 이다."""
    kept = ((grant.refresh_sha256, _CURRENT), (grant.prev_refresh_sha256, _PREVIOUS))
    matched = match_token([_Generation(digest, name) for digest, name in kept if digest], secret)
    return matched.name if matched is not None else None


def _check_live(grant: OAuthGrant | None, now: datetime) -> OAuthGrant:
    """회수되지 않았고 끝나지 않은 연결이어야 한다."""
    if grant is None or grant.status(now) != ACTIVE:
        raise _invalid_grant("the connection has ended")
    return grant


def _narrowed(granted: str, asked: str | None) -> str:
    """갱신의 scope. 없으면 연결의 것이고 원래 것보다 좁힐 수만 있다 (RFC 6749 6절)."""
    if asked is None:
        return granted
    scope = granted_scope(asked)
    if not set(scope.split()) <= set(granted.split()):
        raise OAuthError(INVALID_SCOPE, "the scope cannot be wider than the one granted")
    return scope


def _answered_scope(scope: str, refreshable: bool) -> str:
    """응답의 scope. mcp 와, 갱신 토큰을 냈고 요청에 있었으면 offline_access 다 (9절)."""
    return scope_string(refreshable and SCOPE_OFFLINE in scope.split())


def _note_resource(asked: str | None, bound: str) -> None:
    """토큰 요청의 resource 는 쓰지 않는다. 연결의 것과 다르거나 없으면 로그에 남긴다 (9절)."""
    if asked is None or normalize_resource(asked) != bound:
        state = "missing" if asked is None else "different"
        logger.info("oauth token request resource is %s; issuing for %s", state, bound)


def _access_claims(
    grant: OAuthGrant,
    *,
    audience: str,
    username: str | None,
    role: str | None = None,
    idp_groups: tuple[str, ...] = (),
) -> dict[str, Any]:
    """접근 토큰의 클레임 (10절). 개인 토큰과 같은 테넌트 토큰에 `aud` 로 리소스를 더하고,
    클라이언트와 연결을 싣는다. `client_id` 는 RFC 9068 의 이름이고 값은 client_ref 다. 런타임과
    Cedar 가 보는 `cid` 와 겹치지 않는다. 그룹은 테넌트의 고정 그룹이다 (P53b 6.1절)."""
    claims = tenant_token_claims(
        issuer=grant.iss,
        audience=[audience, grant.resource],
        sub=grant.sub,
        ttl=ACCESS_TOKEN_SECONDS,
        tenant=grant.tenant,
        group=default_mcp_group(grant.tenant),
        username=username,
        role=role,
        idp_groups=idp_groups,
    )
    return {**claims, "client_id": grant.client_ref, "sid": grant.id}


class OAuthTokens:
    """토큰 엔드포인트, RFC 7009 회수, 화면의 연결 목록과 끊기. OAuth 가 켜졌을 때만 있다."""

    def __init__(
        self,
        authorizer: OAuthAuthorizer,
        codes: MongoOAuthCodeStore,
        grants: MongoOAuthGrantStore,
        revoker: AccessRevoker,
        settings: Settings,
        named: NamedGroups,
    ):
        self.authorizer = authorizer
        self.codes = codes
        self.grants = grants
        self.revoker = revoker
        self.settings = settings
        self.named = named

    @property
    def issuer(self) -> InternalIssuer:
        return self.authorizer.clients.issuer

    async def token(self, form: Mapping[str, str], audit: OAuthAudit) -> dict[str, Any]:
        """`POST /oauth/token` 의 응답 본문. grant_type 이 두 갈래를 가른다."""
        grant_type = _required(form, "grant_type")
        actions = {AUTHORIZATION_CODE: ACTION_CODE, REFRESH_TOKEN: ACTION_REFRESH}
        if grant_type not in actions:
            raise OAuthError(
                UNSUPPORTED_GRANT_TYPE, "grant_type must be authorization_code or refresh_token"
            )
        audit.action = actions[grant_type]
        if self.authorizer.dormant:
            raise OAuthError(
                TEMPORARILY_UNAVAILABLE,
                "the authorization server issues no tokens for now",
                status=503,
                headers=retry_after_header(DORMANT_RETRY_AFTER),
            )
        if grant_type == AUTHORIZATION_CODE:
            return await self._exchange(form, audit)
        return await self._refresh(form, audit)

    # -- 코드 교환 (8절·9절) -------------------------------------------------------------

    async def _exchange(self, form: Mapping[str, str], audit: OAuthAudit) -> dict[str, Any]:
        """코드는 교환을 시도하는 순간 쓰인다. 뒤의 검사에 걸려도 다시 쓸 수 없다."""
        code_value, redirect_uri, verifier = (
            _required(form, name) for name in ("code", "redirect_uri", "code_verifier")
        )
        if not _CODE_VERIFIER.fullmatch(verifier):
            raise OAuthError(
                INVALID_REQUEST, "code_verifier must be 43 to 128 unreserved characters"
            )
        client = await self.authorizer.clients.resolve(_required(form, "client_id"))
        audit.client_ref = client.ref
        code = await self._consume(hash_token(code_value), audit)
        bound = (code.client_id, code.redirect_uri) == (client.client_id, redirect_uri)
        if not (bound and _pkce_matches(verifier, code.code_challenge)):
            raise _invalid_grant(
                "the code was not issued for this client, redirect_uri and code_verifier"
            )
        _note_resource(form.get("resource"), code.resource)
        holder, resource = await self._code_holder(code)
        grant, refresh_token = await self._open_grant(code, client, holder, resource)
        return await self._issued(grant, holder, refresh_token, grant.scope)

    async def _consume(self, digest: str, audit: OAuthAudit) -> OAuthCode:
        """코드를 한 번 쓰고 새 연결 id 를 찍는다."""
        now = utcnow()
        code = await self.codes.consume(digest, grant_id=new_id(), now=now)
        if code is None:
            await self._reject_unusable(digest, now, audit)
        audit.sub, audit.tenant = code.sub, code.tenant
        return code

    async def _reject_unusable(self, digest: str, now: datetime, audit: OAuthAudit) -> NoReturn:
        """쓸 수 없는 코드. 이미 쓰인 코드면 재사용을 찍고 그때 찍힌 연결을 끊는다. 그 연결이 아직
        없으면 첫 교환이 `_guard_reuse` 에서 스스로 끊는다."""
        reused = await self.codes.mark_reused(digest, now)
        known = reused or await self.codes.get(digest)
        if known is not None:
            audit.sub, audit.tenant = known.sub, known.tenant
        if reused is None or reused.grant_id is None:
            raise _invalid_grant("the code is not valid or has expired")
        await self.grants.revoke(reused.grant_id, CODE_REUSE)
        raise _invalid_grant("the code has already been used", audit_code=CODE_REUSE)

    async def _code_holder(self, code: OAuthCode) -> tuple[Holder, OAuthResource]:
        """코드를 받은 사람이 아직 그 테넌트를 쓸 수 있고 리소스가 여전히 노출돼 있어야 한다. 사람은
        접근 정리와 같은 규칙으로 본다. (가진 것, 리소스)다."""
        holder = await self.revoker.holder(code.sub, code.tenant)
        lost = self.revoker.holder_lost(code, holder, utcnow())
        if holder.username is None or lost is not None:
            raise _invalid_grant("the person can no longer use this tenant")
        resource = await self._exposed(code.resource, code.tenant)
        if resource is None:
            raise _invalid_grant("the resource is no longer exposed")
        return holder, resource

    async def _open_grant(
        self, code: OAuthCode, client: OAuthClient, holder: Holder, resource: OAuthResource
    ) -> tuple[OAuthGrant, str | None]:
        """연결과, 클라이언트가 갱신을 쓰면 첫 갱신 토큰. 한 사람의 활성 연결이 상한을 넘으면
        마지막 갱신이 가장 오래된 것부터 퇴출한다 (11절)."""
        now = utcnow()
        refreshable = REFRESH_TOKEN in client.grant_types
        plain, digest = issue_token() if refreshable else (None, None)
        grant = self._new_grant(code, client, holder.username or "", resource, digest, now)
        await self.grants.insert(grant)
        await self._guard_reuse(code, grant)
        evicted = await self.grants.beyond(code.sub, now, keep=MAX_LIVE_GRANTS)
        await self.grants.revoke_many(evicted, EVICTED)
        return grant, f"{grant.family_id}.{plain}" if plain else None

    async def _guard_reuse(self, code: OAuthCode, grant: OAuthGrant) -> None:
        """연결을 넣은 뒤 코드의 재사용을 다시 본다. 재사용이 연결을 넣기 전에 왔으면 그쪽의 회수는
        끊을 것이 없었으므로 여기서 끊는다. 넣은 뒤에 왔으면 그쪽이 이미 이 연결을 끊는다."""
        if await self.codes.reused(code.id):
            await self.grants.revoke(grant.id, CODE_REUSE)
            raise _invalid_grant("the code has already been used", audit_code=CODE_REUSE)

    def _new_grant(
        self,
        code: OAuthCode,
        client: OAuthClient,
        username: str,
        resource: OAuthResource,
        refresh_sha256: str | None,
        now: datetime,
    ) -> OAuthGrant:
        """코드가 찍은 연결 id 의 새 연결. 갱신 토큰을 내지 않으면 접근 토큰의 만료에 끝난다."""
        expires = now + timedelta(days=self.settings.oauth_refresh_days)
        ends = min(now + REFRESH_TOKEN_TTL, expires) if refresh_sha256 else now + ACCESS_TOKEN_TTL
        return OAuthGrant(
            id=code.grant_id,
            family_id=secrets.token_hex(FAMILY_ID_BYTES),
            sub=code.sub,
            username=username,
            tv=code.tv,
            client_id=client.client_id,
            client_ref=client.ref,
            client_name=client.name,
            client_kind=client.kind,
            tenant=code.tenant,
            resource=code.resource,
            resource_kind=resource.kind,
            resource_id=resource.id,
            scope=code.scope,
            iss=self.issuer.human_issuer,
            signer_kid=self.issuer.kid,
            refresh_sha256=refresh_sha256,
            refresh_expires_at=ends,
            created_at=now,
            last_refresh_at=now,
            expires_at=expires,
            purge_at=ends + PURGE_AFTER,
        )

    # -- 갱신 (11절) ---------------------------------------------------------------------

    async def _refresh(self, form: Mapping[str, str], audit: OAuthAudit) -> dict[str, Any]:
        """가족이 없으면 invalid_grant 만 내고, client_id 가 다르면 연결을 두고 거절한다."""
        client_id = _required(form, "client_id")
        parts = _split_refresh(_required(form, "refresh_token"))
        grant = await self.grants.by_family(parts[0]) if parts is not None else None
        if grant is None or parts is None:
            raise _invalid_grant("the refresh token is not valid")
        audit.sub, audit.tenant = grant.sub, grant.tenant
        if grant.client_id != client_id:
            raise _invalid_grant("the refresh token was issued to another client")
        audit.client_ref = grant.client_ref
        scope = _narrowed(grant.scope, form.get("scope"))
        _note_resource(form.get("resource"), grant.resource)
        holder, rotated, refresh_token = await self._rotate(grant, parts[1], utcnow())
        return await self._issued(rotated, holder, refresh_token, scope)

    async def _rotate(
        self, grant: OAuthGrant, secret: str, now: datetime
    ) -> tuple[Holder, OAuthGrant, str]:
        """지금 세대의 비밀이면 연결을 다시 보고 회전한다. (가진 것, 회전한 연결, 새 갱신
        토큰)."""
        _check_live(grant, now)
        if _generation(grant, secret) != _CURRENT:
            await self._reject_stale(grant, secret, now)
        holder = await self._grant_holder(grant)
        plain, digest = issue_token()
        ends = min(now + REFRESH_TOKEN_TTL, grant.expires_at)
        rotated = await self.grants.rotate(grant, digest, now=now, refresh_expires_at=ends)
        if rotated is None:
            # 같은 토큰으로 온 다른 요청이 먼저 회전했다. 다시 읽은 가족으로 가른다
            await self._reject_stale(await self.grants.get(grant.id), secret, now)
        return holder, rotated, f"{grant.family_id}.{plain}"

    async def _reject_stale(self, grant: OAuthGrant | None, secret: str, now: datetime) -> NoReturn:
        """지금 세대가 아닌 비밀. 바로 앞 세대가 회전 뒤 10초 안에 오면 동시 갱신으로 보고 그
        요청만 거절한다. 그 밖은 재사용이라 가족을 끊는다 (RFC 9700)."""
        grant = _check_live(grant, now)
        rotated_at = grant.prev_rotated_at
        recent = rotated_at is not None and now - rotated_at <= REUSE_GRACE
        if recent and _generation(grant, secret) == _PREVIOUS:
            raise _invalid_grant(
                "the refresh token was rotated moments ago", audit_code=REFRESH_REUSE_GRACE
            )
        await self.grants.revoke(grant.id, REFRESH_REUSE)
        raise _invalid_grant("the refresh token has already been used", audit_code=REFRESH_REUSE)

    async def _grant_holder(self, grant: OAuthGrant) -> Holder:
        """연결이 아직 접근을 가졌으면 그 사람이 가진 것이다. 접근 정리와 같은 규칙에
        리소스의 노출을 더해 보고, 잃었으면 그 사유로 연결을 끊는다. 사람은 local 모드의 사용자
        문서이거나 oidc 모드의 사람 기록이다 (P53b 6.5절)."""
        holder = await self.revoker.holder(grant.sub, grant.tenant)
        reason = self.revoker.grant_reason(grant, holder, utcnow())
        if reason is None and await self._exposed(grant.resource, grant.tenant) is None:
            reason = RESOURCE_GONE
        if holder.username is None or reason is not None:
            reason = reason or DELETED
            await self.grants.revoke(grant.id, reason)
            raise _invalid_grant("the connection no longer has access", audit_code=reason)
        return holder

    # -- 공통 --------------------------------------------------------------------------

    async def _exposed(self, url: str, tenant: str) -> OAuthResource | None:
        """url 이 여전히 tenant 의 노출한 Gateway·MCP Server 인가. 아니면 None 이다."""
        try:
            resource = await self.authorizer.resource([url])
        except OAuthError:
            return None
        return resource if resource.tenant == tenant else None

    async def _issued(
        self,
        grant: OAuthGrant,
        holder: Holder,
        refresh_token: str | None,
        scope: str,
    ) -> dict[str, Any]:
        """토큰 응답 (9절). 갱신도 그때의 소속 역할과 IdP 그룹을 싣는다. 15분 수명이 역할과 그룹
        변경의 한도다 (P52 3절, P53b 6.6절)."""
        claims = _access_claims(
            grant,
            audience=self.settings.mcp_audience,
            username=holder.username,
            role=holder.role_in(grant.tenant),
            idp_groups=await self.named.carried(grant.tenant, holder.idp_groups),
        )
        body: dict[str, Any] = {
            "access_token": self.issuer.signer.sign(claims),
            "token_type": TOKEN_TYPE,
            "expires_in": ACCESS_TOKEN_SECONDS,
            "scope": _answered_scope(scope, refresh_token is not None),
        }
        if refresh_token is not None:
            body["refresh_token"] = refresh_token
        return body

    # -- 회수 (12절) ---------------------------------------------------------------------

    async def revoke(self, form: Mapping[str, str], audit: OAuthAudit) -> None:
        """`POST /oauth/revoke` (RFC 7009). 모르는 토큰은 할 일이 없고, 토큰이 다른 클라이언트의
        것이면 401 invalid_client 다."""
        client_id = form.get("client_id")
        if not client_id:
            raise OAuthError(INVALID_CLIENT, "client_id is required", status=401)
        grant, reason = await self._revocable(_required(form, "token"))
        if grant is None:
            return
        audit.sub, audit.tenant = grant.sub, grant.tenant
        if grant.client_id != client_id:
            raise OAuthError(INVALID_CLIENT, "the token was issued to another client", status=401)
        audit.client_ref = grant.client_ref
        await self.grants.revoke(grant.id, reason)

    async def _revocable(self, token: str) -> tuple[OAuthGrant | None, GrantRevokeReason]:
        """토큰이 가리키는 연결과 회수 사유. 갱신 토큰이면 가족으로 찾고, 비밀이 지금 세대가 아니면
        재사용이다(11절). 접근 토큰이면 서명과 발급자를 확인하고 `sid` 로 찾는다."""
        parts = _split_refresh(token)
        if parts is not None:
            grant = await self.grants.by_family(parts[0])
            current = grant is not None and _generation(grant, parts[1]) == _CURRENT
            return grant, CLIENT if current else REFRESH_REUSE
        return await self._grant_of_access(token), CLIENT

    async def _grant_of_access(self, token: str) -> OAuthGrant | None:
        """접근 토큰의 연결. 플랫폼 키와 사람 발급자로 서명을 확인한다. 개인 토큰과 사람의 데이터
        플레인 토큰은 `sid` 가 없어 여기서 빠진다."""
        verifier = LocalVerifier(
            self.issuer.signer,
            issuer=self.issuer.human_issuer,
            audience=self.settings.mcp_audience,
            required=ACCESS_TOKEN_CLAIMS,
        )
        try:
            sid = verifier.verify(token)["sid"]
        except AuthError:
            return None
        return await self.grants.get(sid) if isinstance(sid, str) else None

    # -- 화면의 연결 목록과 끊기 (12절) ---------------------------------------------------

    async def list_for(self, sub: str) -> list[OAuthGrant]:
        """sub 의 연결, 새것부터. 끝났거나 끊긴 것도 30일 동안 남는다."""
        return await self.grants.list_for_sub(sub)

    async def owned(self, sub: str, grant_id: str) -> OAuthGrant | None:
        """sub 의 연결. 없거나 남의 것이면 None 이다. 라우터가 둘을 같은 404 로 낸다."""
        grant = await self.grants.get(grant_id)
        return grant if grant is not None and grant.sub == sub else None

    async def disconnect(self, grant: OAuthGrant, reason: GrantRevokeReason) -> None:
        """본인(`user`)이나 관리자(`admin`)가 끊는다. 이미 끊긴 연결이면 할 일이 없다."""
        await self.grants.revoke(grant.id, reason)
