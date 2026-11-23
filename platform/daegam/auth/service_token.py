"""내부 MCP 홉의 서비스 토큰 발급자 (D41).

소비자(vMCP·노출 앞단·에이전트 런타임·플랫폼 자신)가 여는 서버마다 토큰 하나를 낸다. `sub` 는
`svc:` 접두로 사람과 이름 공간을 나누고, `aud` 는 내부 홉 전용 `daegam-internal:<tenant>` 와 그 서버
하나의 `daegam-internal:<tenant>:<서버 id>` 다(P50 9절, 어느 audience 를 싣는지는
`services/svc_tokens.py` `token_audiences` 가 정한다). 내부 MCPServer 는 audience 만 검증하고 그것이
테넌트 격리다. `tenant`·`groups`(테넌트 MCP 그룹)는 감사와 장래의 정책에
쓰려고 싣는다. `tv` 는 없다. 제어면 검증기는 audience 뒤에서 그것으로 한 번 더 거절한다.

local 모드의 에이전트가 Gateway(vMCP)를 부를 때는 audience 가 데이터 플레인 값이고 `iss` 가 사람
발급자인 토큰을 낸다. vMCP 의 daegam-oidc 는 발급자 하나만 믿기 때문이다 (P32 1절). 사람의
`POST /auth/token` 과 같은 모양이고 vMCP 의 THVGroup 정책이 그대로 통과시킨다.

서명기는 기동 뒤 `attach()` 로 붙는다. 키가 홈 클러스터의 Secret 에 있어 `bootstrap_home` 뒤에야
읽을 수 있기 때문이다. 붙기 전의 `mint` 는 RuntimeError 다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from daegam.auth.local import LocalVerifier
from daegam.auth.oidc import AuthError
from daegam.auth.signer import (
    LLM_AUDIENCE,
    PlatformSigner,
    agent_call_audience,
    internal_audience,
    registered_claims,
    search_audience,
)

SERVICE_SUB_PREFIX = "svc:"
# 서비스 어카운트의 토큰이 싣는 `sub` 의 접두사 (P53b 6.7절). `sa:<계정 id>` 이고 사람의 sub 일 수
# 없다. 접근 정리가 이 sub 의 토큰을 사람 대신 계정에 맞댄다
ACCOUNT_SUB_PREFIX = "sa:"
SERVICE_TOKEN_TTL = 365 * 24 * 3600  # 소비자 Secret 에 두는 토큰. 리컨실이 만료 6개월 전에 바꾼다
PLATFORM_CALL_TTL = 300  # 플랫폼 자신의 tools/list 호출마다 새로 낸다
PLATFORM_SUB = "svc:platform"
# LLM 게이트웨이의 Agent 토큰 (P38 5절). 회수 목록을 묶으려고 D41 의 1년보다 짧다. 스윕이 만료
# 30일 전에 다시 낸다
LLM_TOKEN_TTL = 90 * 24 * 3600
LLM_TOKEN_RENEW_DAYS = 30
# 서비스 토큰이 반드시 싣는 클레임. 검증기가 없으면 거절한다
SERVICE_TOKEN_CLAIMS = ("exp", "sub", "tenant")
# 다른 Agent 를 Tool 로 부르는 호출 토큰의 `bound_to` 꼴이다. 노출된 런타임이 받는 `agent:<id>` 와
# 달라 플랫폼의 호출 경로만 받는다
AGENT_CALL_TARGET = "agent-call:{}"
# 호출 토큰이 싣는 턴 위임의 id 다. 호출 경로가 이 id 로 위임을 찾는다
GRANT_ID_CLAIM = "grant_id"


def is_account(sub: str) -> bool:
    """서비스 어카운트의 sub(`sa:<계정 id>`)인가."""
    return sub.startswith(ACCOUNT_SUB_PREFIX)


def account_sub(account_id: str) -> str:
    """서비스 어카운트의 토큰이 싣는 `sub` `sa:<계정 id>`."""
    return f"{ACCOUNT_SUB_PREFIX}{account_id}"


def account_id_of(sub: str) -> str:
    """`account_sub` 를 거꾸로 읽은 계정 id. 서비스 어카운트의 sub 이 아니면 ValueError 다."""
    if not is_account(sub):
        raise ValueError(f"{sub!r} is not a service account subject")
    return sub.removeprefix(ACCOUNT_SUB_PREFIX)


def is_person_sub(sub: str) -> bool:
    """사람의 토큰이 실을 수 있는 sub 인가. 서비스(`svc:`)와 서비스 어카운트(`sa:`)의 접두사로
    시작하는 IdP sub 은 개인 토큰도 로그인 연결도 받지 못한다. 그런 토큰은 경계가 서비스나 계정으로
    읽기 때문이다 (P53b 6.5절)."""
    return bool(sub) and not sub.startswith((SERVICE_SUB_PREFIX, ACCOUNT_SUB_PREFIX))


def service_sub(kind: str, name: str) -> str:
    """`svc:<kind>:<name>`. kind 는 vmcp·expose·agent·dev 다."""
    return f"{SERVICE_SUB_PREFIX}{kind}:{name}"


def service_claims(
    *,
    issuer: str,
    audience: str | list[str],
    sub: str,
    tenant: str,
    groups: list[str],
    ttl: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """extra 는 더 싣는 클레임이다. Agent 토큰의 `agent_id`(P52 3절), Entry 토큰의 규칙 지문
    `rules`(P52 2절), LLM 토큰의 `llm_providers`·`purpose`(P38 5절)다. 값이 비면 싣지 않는다."""
    if not sub.startswith(SERVICE_SUB_PREFIX):
        raise ValueError(f"service token sub must start with {SERVICE_SUB_PREFIX!r}, got {sub!r}")
    if not tenant:
        raise ValueError("service token needs a tenant")
    return {
        **registered_claims(issuer=issuer, audience=audience, sub=sub, ttl=ttl),
        "tenant": tenant,
        "groups": list(groups),
        **{k: v for k, v in (extra or {}).items() if v},
    }


def turn_token_claims(
    *,
    issuer: str,
    audience: str,
    sub: str,
    ttl: int,
    actor: str,
    bound_to: str,
    tenant: str,
    username: str | None,
    agent_id: str | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """턴 위임 토큰의 클레임 (P53 1절). sub 는 사람, `act` 는 그 사람 대신 부르는 Agent 의 서비스
    신원(RFC 8693)과 그 Agent 의 id 이고(P52 3절, Tool 규칙의 위임 절이 그 레코드를 통째로
    견준다), `bound_to` 는 대상 하나다. `groups`·`cid`·`tv`·`scope` 를 싣지 않으므로 그것으로 받는
    경계는 모두 거절하고 자기 대상을 이름으로 적은 경계만 받는다. sub 는 `svc:` 로 시작하지
    않는다. 테넌트 울타리가 그런 sub 를 서비스로 읽기 때문이다. grant_id 는 호출 토큰만 싣는 위임의
    id 다."""
    if sub.startswith(SERVICE_SUB_PREFIX):
        raise ValueError(f"turn token sub must not start with {SERVICE_SUB_PREFIX!r}")
    claims = {
        **registered_claims(issuer=issuer, audience=audience, sub=sub, ttl=ttl),
        "act": {"sub": actor, **({"agent_id": agent_id} if agent_id else {})},
        "bound_to": bound_to,
        "tenant": tenant,
    }
    if username:
        claims["preferred_username"] = username
    if grant_id:
        claims[GRANT_ID_CLAIM] = grant_id
    return claims


def bound_token_claims(
    *, issuer: str, audience: str, sub: str, tenant: str, target: str, rules: str | None, ttl: int
) -> dict[str, Any]:
    """게시된 Gateway 하나에 묶인 소비자 vMCP 의 토큰 클레임 (P57 6.1절). sub 는 소비자 vMCP 의
    서비스 신원, `bound_to` 는 그 게시된 Gateway(`gateway:<id>`)이고, rules 는 소비자가 컴파일한
    게시된 Gateway 의 규칙 지문이다. `groups`·`cid`·`act`·`tv` 를 싣지 않는다. 그래서 테넌트 permit
    과 다른 경계는 이 토큰을 받지 않고, 자기 대상의 위임 permit 만 받는다."""
    if not sub.startswith(SERVICE_SUB_PREFIX):
        raise ValueError(f"bound token sub must start with {SERVICE_SUB_PREFIX!r}, got {sub!r}")
    claims = {
        **registered_claims(issuer=issuer, audience=audience, sub=sub, ttl=ttl),
        "tenant": tenant,
        "bound_to": target,
    }
    if rules:
        claims["rules"] = rules
    return claims


@dataclass(frozen=True)
class TokenPeek:
    """서명을 보지 않고 읽은 헤더·클레임. 리컨실이 갱신 여부를 판단하는 데만 쓴다."""

    kid: str | None
    sub: str | None
    exp: int | None
    jti: str | None
    iss: str | None = None
    groups: tuple[str, ...] = ()
    audiences: tuple[str, ...] = ()  # aud 가 글이든 목록이든 그 값 모두
    tenant: str | None = None
    # Entry 토큰의 규칙 지문과 Agent 토큰의 Agent id (P52 2·3절). 없으면 None 이다
    rules: str | None = None
    agent_id: str | None = None
    # LLM 토큰이 여는 provider slug (P38 5절)
    llm_providers: tuple[str, ...] = ()
    iat: int | None = None
    # 위임 토큰과 묶인 토큰의 대상 (P53 1절, P57 6.1절). 없으면 None 이다
    bound_to: str | None = None
    # groups 클레임이 있는가. 묶인 토큰은 groups 를 싣지 않는다 (P57 6.1절)
    has_groups: bool = False


class InternalIssuer:
    def __init__(self, *, issuer: str, human_issuer: str, mcp_audience: str):
        self.issuer = issuer.rstrip("/")  # 서비스 발급자
        self.human_issuer = human_issuer.rstrip("/")  # 사람 발급자. gateway_token 의 iss 다
        self.mcp_audience = mcp_audience
        self._signer: PlatformSigner | None = None

    def attach(self, signer: PlatformSigner) -> None:
        self._signer = signer

    @property
    def loaded(self) -> bool:
        return self._signer is not None

    @property
    def signer(self) -> PlatformSigner:
        if self._signer is None:
            raise RuntimeError("the platform signing key is not loaded yet")
        return self._signer

    @property
    def kid(self) -> str:
        return self.signer.kid

    def jwks(self) -> dict[str, Any]:
        return self.signer.jwks()

    def mint(
        self,
        *,
        sub: str,
        tenant: str,
        groups: list[str],
        ttl: int = SERVICE_TOKEN_TTL,
        audiences: list[str] | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> str:
        """내부 MCPServer 가 받는 토큰 (`iss` 는 서비스 발급자). audiences 가 없으면 `aud` 는
        `daegam-internal:<tenant>` 하나이고, 둘 이상이면 목록이다 (P50 9절). extra 는
        `service_claims` 의 것이다."""
        aud: str | list[str] = internal_audience(tenant)
        if audiences:
            aud = audiences[0] if len(audiences) == 1 else list(audiences)
        return self._mint(
            self.issuer, aud, sub=sub, tenant=tenant, groups=groups, ttl=ttl, extra=extra
        )

    def mint_gateway(
        self,
        *,
        sub: str,
        tenant: str,
        groups: list[str],
        ttl: int = SERVICE_TOKEN_TTL,
        extra: Mapping[str, str] | None = None,
    ) -> str:
        """vMCP 가 받는 토큰 (`aud: mcp_audience`, `iss` 는 사람 발급자). local 모드의 에이전트
        Gateway 소스용이다."""
        return self._mint(
            self.human_issuer,
            self.mcp_audience,
            sub=sub,
            tenant=tenant,
            groups=groups,
            ttl=ttl,
            extra=extra,
        )

    def mint_bound(self, *, sub: str, tenant: str, target: str, rules: str | None) -> str:
        """게시된 Gateway 에 묶인 소비자 vMCP 의 토큰 (P57 6.1절). `iss` 는 사람 발급자, `aud` 는
        데이터 플레인 값이며 수명은 1년이다(`bound_token_claims`). target 은 `gateway:<id>` 다."""
        return self.signer.sign(
            bound_token_claims(
                issuer=self.human_issuer,
                audience=self.mcp_audience,
                sub=sub,
                tenant=tenant,
                target=target,
                rules=rules,
                ttl=SERVICE_TOKEN_TTL,
            )
        )

    def mint_llm(
        self,
        *,
        sub: str,
        tenant: str,
        providers: list[str],
        ttl: int,
        extra: Mapping[str, str] | None = None,
    ) -> str:
        """LLM 게이트웨이가 받는 토큰 (`aud: daegam-llm`, `iss` 는 서비스 발급자, P38 5절). groups
        는 비운다. 게이트웨이는 읽지 않고, 채우면 요청마다 Tenant Settings 를 읽어야 한다. 테넌트는
        늘 있다. CEL 의 `jwt.tenant` 가 실패하면 테넌트 한도가 말없이 빠지기 때문이다."""
        if not providers:
            raise ValueError("an LLM token opens at least one provider")
        claims = {"llm_providers": sorted(set(providers)), **(extra or {})}
        return self._mint(
            self.issuer, LLM_AUDIENCE, sub=sub, tenant=tenant, groups=[], ttl=ttl, extra=claims
        )

    def _mint(
        self,
        issuer: str,
        audience: str | list[str],
        *,
        sub: str,
        tenant: str,
        groups: list[str],
        ttl: int,
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        return self.signer.sign(
            service_claims(
                issuer=issuer,
                audience=audience,
                sub=sub,
                tenant=tenant,
                groups=groups,
                ttl=ttl,
                extra=extra,
            )
        )

    @staticmethod
    def peek(token: str) -> TokenPeek | None:
        """헤더의 kid 와 exp·jti·sub·iss·aud·groups 를 서명 검증 없이 읽는다. JWT 가 아니면 None."""
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        exp = claims.get("exp")
        iat = claims.get("iat")
        aud = claims.get("aud")
        groups = claims.get("groups")
        providers = claims.get("llm_providers")
        audiences: tuple[str, ...] = ()
        if isinstance(aud, str):
            audiences = (aud,)
        elif isinstance(aud, list):
            audiences = tuple(a for a in aud if isinstance(a, str))
        return TokenPeek(
            kid=header.get("kid"),
            sub=claims.get("sub"),
            exp=int(exp) if isinstance(exp, (int, float)) else None,
            jti=claims.get("jti"),
            iss=claims.get("iss"),
            groups=tuple(str(g) for g in groups) if isinstance(groups, list) else (),
            audiences=audiences,
            tenant=claims.get("tenant") if isinstance(claims.get("tenant"), str) else None,
            rules=claims.get("rules") if isinstance(claims.get("rules"), str) else None,
            agent_id=claims.get("agent_id") if isinstance(claims.get("agent_id"), str) else None,
            llm_providers=tuple(str(p) for p in providers) if isinstance(providers, list) else (),
            iat=int(iat) if isinstance(iat, (int, float)) else None,
            bound_to=claims.get("bound_to") if isinstance(claims.get("bound_to"), str) else None,
            has_groups="groups" in claims,
        )


def claims_current(
    token: str | None,
    *,
    iss: str,
    aud: str,
    groups: list[str],
    llm_providers: list[str] | None = None,
) -> bool:
    """token 의 iss·aud·groups 가 원하는 값과 모두 같은가. 서명과 만료는 보지 않는다. 없거나 JWT 가
    아니면 거짓이다. local 에이전트의 gateway_token 을 provision·인증 재적용·서비스 토큰 스윕이 이
    비교 하나로 보고, 하나라도 다르면 새로 낸다 (P32 8절). llm_providers 를 주면 LLM 토큰이 여는
    provider 도 견준다. 그래서 provider 를 바꾼 뒤의 재배포·재적용·스윕이 같은 비교로 다시 낸다
    (P38 5절)."""
    peek = InternalIssuer.peek(token) if token else None
    wanted = (iss, (aud,), tuple(groups))
    if peek is None or (peek.iss, peek.audiences, peek.groups) != wanted:
        return False
    return llm_providers is None or peek.llm_providers == tuple(sorted(set(llm_providers)))


def verify_gateway_token(token: str, signer: PlatformSigner, *, issuer: str) -> tuple[str, str]:
    """vMCP 가 headerInjection 으로 보낸 검색 Entry 의 서비스 토큰을 검증하고 (테넌트, Gateway 의
    k8s 이름)을 돌려준다 (P39b 12절). 서명 없이 읽은 `tenant` t 로 audience
    `daegam-internal:<t>:web-search` 를 정해 플랫폼 서비스 키로 검증하고, audience 가 그것
    하나뿐인지, 검증된 `tenant` 가 t 이고 `sub` 가 `svc:vmcp:<이름>` 인지 본다. 그 밖은 AuthError
    다. 테넌트 audience 의 토큰(다른 Entry 의 것)은 검색을 열지 못하고, 토큰이 테넌트를 정하므로
    다른 테넌트의 상한·감사로 셀 수 없다."""
    peek = InternalIssuer.peek(token)
    tenant = peek.tenant if peek is not None else None
    if not tenant or peek is None:
        raise AuthError("service token without a tenant")
    audience = search_audience(tenant)
    if list(peek.audiences) != [audience]:
        raise AuthError("not a web search token")
    claims = LocalVerifier(
        signer, issuer=issuer, audience=audience, required=SERVICE_TOKEN_CLAIMS
    ).verify(token)
    prefix = service_sub("vmcp", "")
    sub = claims.get("sub")
    if claims.get("tenant") != tenant or not isinstance(sub, str) or not sub.startswith(prefix):
        raise AuthError("not a Gateway service token")
    name = sub[len(prefix) :]
    if not name:
        raise AuthError("not a Gateway service token")
    return tenant, name


@dataclass(frozen=True)
class AgentCallClaims:
    """검증한 호출 토큰이 말하는 것이다. sub 는 사람, actor_agent_id 는 부르는 Agent 의 id(미리
    보기는 원본의 것), grant_id 는 그 턴의 위임이다."""

    sub: str
    tenant: str
    grant_id: str
    actor_agent_id: str


def verify_agent_call_token(
    token: str, signer: PlatformSigner, *, issuer: str, agent_id: str
) -> AgentCallClaims:
    """호출 경로가 받은 턴 위임 토큰을 검증한다. 서명 없이 읽은 `tenant` 로 그 Agent 의 호출
    audience 를 정해 플랫폼 서비스 키로 검증하고, audience 가 그것 하나뿐인지, `bound_to` 가 그
    Agent 의 호출(`AGENT_CALL_TARGET`)인지, sub 가 사람인지, 위임 id 와 부르는 Agent 의 id 를
    실었는지 본다. 그 밖은 AuthError 다. Gateway, MCP Server, 노출된 Agent 로 가는 턴 토큰과 서비스
    토큰은 audience 와 `bound_to` 가 달라 여기를 지나지 못한다."""
    peek = InternalIssuer.peek(token)
    tenant = peek.tenant if peek is not None else None
    if not tenant or peek is None:
        raise AuthError("not an agent call token")
    audience = agent_call_audience(tenant, agent_id)
    if list(peek.audiences) != [audience]:
        raise AuthError("not a token for this agent's calls")
    claims = LocalVerifier(
        signer, issuer=issuer, audience=audience, required=SERVICE_TOKEN_CLAIMS
    ).verify(token)
    sub, grant_id, act = claims.get("sub"), claims.get(GRANT_ID_CLAIM), claims.get("act")
    actor = act.get("agent_id") if isinstance(act, dict) else None
    if (
        claims.get("tenant") != tenant
        or claims.get("bound_to") != AGENT_CALL_TARGET.format(agent_id)
        or not isinstance(sub, str)
        or not is_person_sub(sub)
        or not isinstance(grant_id, str)
        or not isinstance(actor, str)
    ):
        raise AuthError("not an agent call token")
    return AgentCallClaims(sub=sub, tenant=tenant, grant_id=grant_id, actor_agent_id=actor)
