"""인바운드 주체(D27). 모든 핸들러는 본문 `user_sub` 대신 `Caller` 하나만 본다.

두 경로가 있다.

- 플랫폼 프록시: `Authorization: Bearer <인스턴스 토큰>`. `sub` 는 본문(POST) 또는 쿼리(GET)의
  `user_sub` 이고 `approver` 는 `X-Daegam-Approver: 1` 이다. POST 본문에 `user_sub` 가 없으면 422.
- 외부 직접(Ingress): `Authorization: Bearer <OIDC 액세스 토큰>`. `sub = claims.sub`,
  `external = True`, `approver = False`(헤더 무시).

외부 토큰의 발급자는 둘까지다(P53b 6.8절). 첫째는 데이터 플레인이 믿는 발급자(`OIDC_ENV`)이고,
둘째는 플랫폼을 믿는 oidc 배포에서 노출한 Agent 가 그 곁에 두는 IdP(`IDP_ENV`, 능력
`two_issuers`)다. 토큰의 `iss` 로 발급자를 고르고 그 발급자의 검증기와 계약(`CallerRules`)만 쓰므로
한 발급자의 클레임이 다른 발급자의 판정을 정하지 못한다. 어느 발급자도 아니면 401 `invalid_token`
이다. 계약의 허용 그룹이 비어 있지 않으면 `groups` 교집합 또는 client id 가 그 계약의 목록에 있어야
하고 아니면 403 `forbidden` 이다. 발급자가 플랫폼인 계약은 client id 로 들이지 않고, `act` 나
`bound_to` 를 실은 위임 토큰을 이 Agent 에 묶인 것만 받으며 나머지는 403 `forbidden` 이다(P53
3.3절).

판정은 인스턴스 토큰(bytes `compare_digest`)을 먼저 보고, JWT 모양(점 두 개)이면 OIDC 로
검증하며, 아니면 401 `unauthorized` 다. 인스턴스 토큰이 아닌 값을 검증기로 보내지 않아 stale
인스턴스 토큰이 `invalid_token` 으로 위장되지 않는다. 발급자가 없으면(header 모드 배포) 프록시
경로만 있다.
인스턴스 토큰 자체가 없는 로컬·테스트 실행은 모든 요청을 프록시 경로로 본다.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request

from runtime.config import ConfigError
from runtime.hitl import APPROVER_HEADER
from runtime.oidc import AuthError, Claims, OIDCVerifier

# 노출한 Agent 에 묶인 위임 토큰의 `bound_to` 다. 이 단계에서는 아무도 내지 않는다 (P53 3.3절)
AGENT_TARGET = "agent:{}"
# 두 env 묶음이 같은 발급자를 가리킬 때의 기동 오류 코드다(`ConfigError`)
DUPLICATE_ISSUER = "duplicate_issuer"
# 플랫폼 서비스(`svc:`)와 서비스 어카운트(`sa:`)의 sub 머리다(P53b 6.5·6.7절). 플랫폼은 이런 sub 의
# 사람 토큰을 내지 않으므로 곁의 IdP 토큰이 이 sub 를 실으면 막는다
SERVICE_SUB_PREFIXES = ("svc:", "sa:")
# 발급자 설정의 클레임 이름 기본값이다. OIDCVerifier 의 기본값과 같다
DEFAULT_GROUPS_CLAIM = "groups"
DEFAULT_CLIENT_ID_CLAIM = "cid"


@dataclass(frozen=True)
class Caller:
    """요청 주체. `sub` 는 세션 소유권·레이트리밋·관측 이벤트가 쓰는 값이다.

    프록시 경로의 GET 은 플랫폼이 쿼리 `user_sub` 로 넘긴 값이고 없으면 빈 문자열이다(플랫폼이
    소유자 검사를 한다). 외부 경로의 `sub` 는 언제나 토큰의 것이다.
    """

    sub: str
    external: bool
    approver: bool


def _error(status: int, code: str, message: str) -> HTTPException:
    headers = {"WWW-Authenticate": f'Bearer error="{code}"'} if status == 401 else None
    return HTTPException(status, detail={"code": code, "message": message}, headers=headers)


def user_sub_required(where: str) -> HTTPException:
    """프록시 경로에 주체가 없는 요청의 422 다. where 는 user_sub 를 싣는 자리(본문, 쿼리)다. POST
    는 여기서 막고, 쿼리가 주체인 소유자 전용 라우트는 `require_owner_sub` 가 막는다."""
    return _error(422, "user_sub_required", f"프록시 경로 {where}에는 user_sub 가 필요하다")


def require_owner_sub(caller: Caller) -> str:
    """쿼리가 주체인 소유자 전용 라우트가 주체를 확인하고 그 값을 받는다(sessions_api.py,
    memories_api.py). GET 은 주체가 없어도 지나오므로(목록은 플랫폼이 거른다) 이 라우트들은 프록시
    경로에 user_sub 가 없으면 422 다."""
    if not caller.sub:
        raise user_sub_required("쿼리")
    return caller.sub


def bearer_token(request: Request) -> str | None:
    """Authorization 헤더의 Bearer 값. 스킴이 다르거나 비어 있으면 None 이다."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2


def token_presented(request: Request, token: bytes | None) -> bool:
    """요청이 올바른 인스턴스 토큰을 냈는가. 비교는 bytes 라 비 ASCII 헤더에도 예외가 없다. healthz
    는 자원을 열기 전(T22)에도 불려 Authenticator 대신 이것을 쓴다."""
    if token is None:
        return False
    presented = bearer_token(request)
    if presented is None:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), token)


def split_csv(raw: str | None) -> tuple[str, ...]:
    """콤마로 나눈 env 값. 빈 조각은 버린다."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# -- 발급자와 그 계약 ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerRules:
    """발급자 하나의 토큰 가운데 누가 이 Agent 를 부를 수 있는가(D27, P53b 6.8절).

    allowed_groups 가 비어 있지 않으면 groups 교집합 또는 client id 가 agent_client_ids 에 있어야
    한다. 비어 있으면 그 발급자가 audience 로 서명한 주체 전부다. delegation_target 이
    있으면(발급자가 플랫폼) 위임 토큰은 그 값에 묶인 것만 받는다. 없으면 위임 토큰을 따로 보지
    않는다. sub 가 refused_sub_prefixes 의 하나로 시작하는 토큰은 받지 않는다."""

    allowed_groups: frozenset[str] = frozenset()
    agent_client_ids: frozenset[str] = frozenset()
    delegation_target: str | None = None
    refused_sub_prefixes: tuple[str, ...] = ()

    def caller(self, claims: Claims) -> Caller:
        if claims.sub.startswith(self.refused_sub_prefixes):
            raise _error(403, "forbidden", "플랫폼 서비스나 서비스 어카운트의 sub 를 실은 토큰이다")
        if self.delegation_target is not None and _foreign_delegation(
            claims.raw, self.delegation_target
        ):
            raise _error(403, "forbidden", "이 Agent 에 묶이지 않은 위임 토큰이다")
        if self.allowed_groups:
            in_group = bool(self.allowed_groups.intersection(claims.groups))
            trusted_client = claims.client_id in self.agent_client_ids
            if not in_group and not trusted_client:
                raise _error(403, "forbidden", "이 에이전트를 부를 수 있는 그룹이 아니다")
        return Caller(sub=claims.sub, external=True, approver=False)


@dataclass(frozen=True)
class TrustedIssuer:
    """외부 토큰을 받는 발급자 하나다. 검증기가 발급자·audience·서명 키·클레임 이름을 보고 rules 가
    인가를 본다."""

    verifier: OIDCVerifier
    rules: CallerRules


@dataclass(frozen=True)
class IssuerEnv:
    """발급자 하나의 env 이름이다."""

    issuer: str
    jwks_url: str
    insecure_http: str
    groups_claim: str
    client_id_claim: str
    allowed_groups: str
    agent_client_ids: str


# 데이터 플레인이 믿는 발급자다. local 모드와 플랫폼을 믿는 oidc 배포에서는 플랫폼(JWKS 주소가
# 있다), 그 밖의 oidc 배포에서는 IdP 다
OIDC_ENV = IssuerEnv(
    issuer="DAEGAM_OIDC_ISSUER",
    jwks_url="DAEGAM_OIDC_JWKS_URL",
    insecure_http="DAEGAM_OIDC_INSECURE_HTTP",
    groups_claim="DAEGAM_OIDC_GROUPS_CLAIM",
    client_id_claim="DAEGAM_OIDC_CLIENT_ID_CLAIM",
    allowed_groups="DAEGAM_ALLOWED_GROUPS",
    agent_client_ids="DAEGAM_AGENT_CLIENT_IDS",
)
# 플랫폼을 믿는 oidc 배포에서 노출한 Agent 가 곁에 두는 IdP 다(능력 two_issuers, P53b 6.8절). IdP
# 토큰은 IdP 만 믿는 oidc 배포와 같이 IdP 의 클레임 이름, 날 그룹, agent_client_ids 로 판정하고 위임
# 토큰을 묶지 않는다
IDP_ENV = IssuerEnv(
    issuer="DAEGAM_IDP_ISSUER",
    jwks_url="DAEGAM_IDP_JWKS_URL",
    insecure_http="DAEGAM_IDP_INSECURE_HTTP",
    groups_claim="DAEGAM_IDP_GROUPS_CLAIM",
    client_id_claim="DAEGAM_IDP_CLIENT_ID_CLAIM",
    allowed_groups="DAEGAM_IDP_ALLOWED_GROUPS",
    agent_client_ids="DAEGAM_IDP_AGENT_CLIENT_IDS",
)


@dataclass(frozen=True)
class IssuerSettings:
    """외부 토큰의 발급자 하나와 그 계약의 설정이다. audience 는 두 발급자가 같은 데이터 플레인의
    것이라 Settings 가 하나만 든다."""

    issuer: str
    # 있으면 discovery 없이 여기서 JWKS 를 받는다. 발급자가 플랫폼이면 플랫폼이 클러스터 안 주소를
    # 내려 준다(P32 1절)
    jwks_url: str | None = None
    # 평문 http 발급자·JWKS 허용. 클러스터 안 주소 전용이고 플랫폼이 발급자마다 판단해 내려 준다
    insecure_http: bool = False
    # 그룹과 client id 를 읽는 클레임. 플랫폼이 발급자에 맞춰 내려 준다(Keycloak 은 azp)
    groups_claim: str = DEFAULT_GROUPS_CLAIM
    client_id_claim: str = DEFAULT_CLIENT_ID_CLAIM
    allowed_groups: tuple[str, ...] = ()
    agent_client_ids: tuple[str, ...] = ()
    # 발급자가 플랫폼인가(`read_issuers`). 플랫폼의 토큰은 client id 로 들이지 않고(P53b 6.8절) 위임
    # 토큰은 이 Agent 에 묶인 것만 받는다(P53 3.3절)
    is_platform: bool = False
    # 플랫폼 곁의 IdP 인가(`read_issuers`). 그 토큰의 sub 가 서비스 어카운트나 플랫폼 서비스의
    # 것이면 막는다. IdP 주체가 이 Agent 에서 서비스 어카운트의 세션과 기억을 갖지 못하게 한다
    beside_platform: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str], names: IssuerEnv) -> IssuerSettings | None:
        """names 의 env 를 읽는다. 발급자가 비면 None 이다."""
        issuer = env.get(names.issuer) or None
        if issuer is None:
            return None
        return cls(
            issuer=issuer,
            jwks_url=env.get(names.jwks_url) or None,
            insecure_http=env.get(names.insecure_http, "").lower() == "true",
            groups_claim=env.get(names.groups_claim) or DEFAULT_GROUPS_CLAIM,
            client_id_claim=env.get(names.client_id_claim) or DEFAULT_CLIENT_ID_CLAIM,
            allowed_groups=split_csv(env.get(names.allowed_groups)),
            agent_client_ids=split_csv(env.get(names.agent_client_ids)),
        )

    def trusted(
        self,
        audience: str,
        *,
        delegation_target: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> TrustedIssuer:
        """이 발급자의 검증기와 계약이다. JWKS 는 첫 외부 토큰에서 게으르게 받는다. 검증기는 부르는
        쪽이 닫는다. 발급자가 플랫폼이면 client id 를 버리고 delegation_target 에 위임 토큰을
        묶는다. 플랫폼이 낸 OAuth 토큰도 client id 를 싣기 때문에 그 값이 그룹 검사를 건너뛰게
        하지 않으려는 것이다."""
        verifier = OIDCVerifier(
            self.issuer,
            audience,
            http_client=http_client,
            groups_claim=self.groups_claim,
            client_id_claim=self.client_id_claim,
            insecure_http=self.insecure_http,
            jwks_url=self.jwks_url,
        )
        rules = CallerRules(
            allowed_groups=frozenset(self.allowed_groups),
            agent_client_ids=frozenset() if self.is_platform else frozenset(self.agent_client_ids),
            delegation_target=delegation_target if self.is_platform else None,
            refused_sub_prefixes=SERVICE_SUB_PREFIXES if self.beside_platform else (),
        )
        return TrustedIssuer(verifier, rules)


def read_issuers(env: Mapping[str, str]) -> tuple[IssuerSettings | None, IssuerSettings | None]:
    """두 env 묶음(데이터 플레인이 믿는 발급자, 곁의 IdP)을 읽는다. 첫 발급자는 JWKS 주소가 있거나
    곁에 IdP 가 있으면 플랫폼이다. 플랫폼은 자기에게만 클러스터 안 JWKS 주소를 내려 주고, IdP 를
    곁에 두는 것은 첫 발급자가 플랫폼일 때뿐이다. 둘 가운데 하나만 맞아도 플랫폼으로 보아, JWKS
    주소가 빠진 렌더에서도 위임 토큰 검사가 열린 쪽으로 풀리지 않게 한다. 곁의 IdP 는 플랫폼이
    아니고 서비스의 sub 를 막는다."""
    oidc = IssuerSettings.from_env(env, OIDC_ENV)
    idp = IssuerSettings.from_env(env, IDP_ENV)
    if oidc is not None and (oidc.jwks_url is not None or idp is not None):
        oidc = replace(oidc, is_platform=True)
    if idp is not None:
        idp = replace(idp, beside_platform=True)
    return oidc, idp


class Authenticator:
    def __init__(
        self, *, instance_token: str | None, issuers: Iterable[TrustedIssuer] = ()
    ) -> None:
        self._token = instance_token.encode("utf-8") if instance_token else None
        # 발급자(검증기가 비교하는 값)마다 하나다. 같은 발급자의 계약 둘은 어느 것이 판정할지
        # 정할 수 없으므로 이름 있는 설정 오류로 기동을 막는다
        self._issuers: dict[str, TrustedIssuer] = {}
        for trusted in issuers:
            name = trusted.verifier.issuer
            if name in self._issuers:
                raise ConfigError(
                    DUPLICATE_ISSUER, f"DAEGAM_OIDC_ISSUER 와 DAEGAM_IDP_ISSUER 가 같다: {name}"
                )
            self._issuers[name] = trusted

    async def caller(self, request: Request) -> Caller:
        if token_presented(request, self._token):
            return await self._proxy_caller(request)
        presented = bearer_token(request)
        if presented is not None and looks_like_jwt(presented) and self._issuers:
            return await self._external_caller(presented)
        if self._token is None:
            # 인스턴스 토큰이 없는 실행(로컬·테스트)은 보호할 것이 없다
            return await self._proxy_caller(request)
        raise _error(401, "unauthorized", "인스턴스 토큰 또는 OIDC 액세스 토큰이 필요하다")

    async def _external_caller(self, token: str) -> Caller:
        """토큰의 `iss` 가 가리키는 발급자의 검증기로 검증하고 그 계약으로 판정한다. `iss` 는 서명을
        보기 전에 읽으므로 고르는 데만 쓰고, 고른 검증기가 `iss`·서명·audience 를 다시 본다. 다른
        발급자의 키로 서명한 토큰은 그 검증기의 키로 풀리지 않아 401 이다. 모르는 발급자의 토큰은
        어느 JWKS 도 받지 않고 끝난다."""
        issuer = _unverified_issuer(token)
        trusted = self._issuers.get(issuer) if issuer else None
        if trusted is None:
            raise _error(401, "invalid_token", "issuer mismatch")
        try:
            claims = await trusted.verifier.verify(token)
        except AuthError as e:
            raise _error(e.status, e.code, e.detail) from e
        return trusted.rules.caller(claims)

    async def _proxy_caller(self, request: Request) -> Caller:
        approver = request.headers.get(APPROVER_HEADER, "").strip() == "1"
        if request.method == "POST":
            sub = await _body_user_sub(request)
            if not sub:
                raise user_sub_required("본문")
        else:
            sub = (request.query_params.get("user_sub") or "").strip()
        return Caller(sub=sub, external=False, approver=approver)


def _unverified_issuer(token: str) -> str | None:
    """서명을 보기 전에 읽은 `iss`. 문자열이 아니거나 없으면 None 이고, 꼴이 틀린 토큰은 401
    이다."""
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise _error(401, "invalid_token", "malformed token") from e
    issuer = payload.get("iss")
    return issuer if isinstance(issuer, str) else None


def _foreign_delegation(raw: dict[str, Any], target: str) -> bool:
    """위임 토큰(`act` 나 `bound_to` 를 실은 것)인데 target 에 묶이지 않았는가."""
    return ("act" in raw or "bound_to" in raw) and raw.get("bound_to") != target


async def _body_user_sub(request: Request) -> str | None:
    """본문 JSON 의 user_sub. 본문이 JSON 객체가 아니면 None 이고 그 모양 오류는 pydantic 이 낸다.
    Starlette 가 본문을 캐시하므로 뒤의 모델 파싱이 다시 읽어도 된다."""
    try:
        raw = await request.body()
        data: Any = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    sub = data.get("user_sub")
    return sub.strip() if isinstance(sub, str) else None


async def get_caller(request: Request) -> Caller:
    auth: Authenticator = request.app.state.runtime.auth
    return await auth.caller(request)


CallerDep = Annotated[Caller, Depends(get_caller)]
