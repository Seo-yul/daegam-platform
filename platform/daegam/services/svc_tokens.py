"""서비스 토큰 발급 창구 (D41). 소비자 넷(vMCP·노출 앞단·에이전트·플랫폼 자신)이 여기서 받는다.

내부 서버에 가는 토큰(`mint`, `platform_token`)의 `groups` 는 테넌트의 MCP 그룹(`human_mcp_group`)
이다. local 은 daegam-<tenant> 고정이고 그 밖의 모드는 Tenant Settings mcp_group, 없으면
daegam-<tenant> 다. 내부 MCPServer 는 audience 와 테넌트 울타리만 보므로 그 그룹은 감사 로그에만
쓰이고, 바뀌지 않아 토큰이 다시 나지 않는다. 사람 경계(vMCP, 노출 앞단)에 가는 토큰(`mint_gateway`,
`platform_gateway_token`)은 모든 모드에서 고정 그룹 daegam-<tenant> 를 싣는다 (P53b 6.1절). 저장된
mcp_group 이 다른 테넌트의 고정 그룹을 적어도 그 토큰이 그 테넌트의 Gateway 를 열지 않는다.

audience 는 `token_audiences` 하나가 정한다 (P50 9절). 소비자가 서버 하나에 내는 토큰은 테넌트
audience 와 그 서버 자신의 audience 를 함께 싣는다. 내부 서버는 그 서버를 부르는 소비자가 모두 이
토큰으로 굴러갔거나 7일을 기다린 뒤 자기 audience 하나만 검증하고, 그 전에는 테넌트 audience 를
검증한다(`services/data_plane_auth.py`). 새로 만든 서버는 처음부터 자기 audience 만 받는다.
배포, 회전, 갱신 스윕, 다시 렌더하기가 모두 이 함수를 쓴다(`audiences_due`). 서버를 모르는
토큰(Agent 의 옛 단일 토큰, 서버를 찾지 못한 Gateway Entry 의 토큰)은 테넌트 audience 하나다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from starlette.concurrency import run_in_threadpool

from daegam.auth.cedar import GATEWAY_TARGET
from daegam.auth.service_token import (
    LLM_TOKEN_TTL,
    PLATFORM_CALL_TTL,
    PLATFORM_SUB,
    SERVICE_TOKEN_TTL,
    InternalIssuer,
    claims_current,
    service_sub,
)
from daegam.auth.signer import (
    FETCH_SERVER_ID,
    SEARCH_SERVER_ID,
    fetch_audience,
    internal_audience,
    search_audience,
    server_audience,
)
from daegam.config import Settings
from daegam.k8s.client import KubeClient
from daegam.k8s.templates.svc_token import existing_svc_token
from daegam.store.base import from_epoch, naive_utc
from daegam.store.tenants import MongoTenantSettingsStore, default_mcp_group, human_mcp_group

# 서비스 토큰을 다시 낼 때. 만료가 이 안이면 리컨실 스윕이 새로 낸다 (D41)
RENEW_BEFORE = timedelta(days=180)


def token_audiences(tenant: str, server_id: str | None) -> list[str]:
    """소비자가 서버 하나에 내는 토큰이 실어야 하는 audience (P50 9절). 테넌트 audience 와 그 서버
    자신의 것이다. 서버를 모르면(Agent 의 옛 단일 토큰) 테넌트 audience 하나다. 플랫폼 fetch 복제본
    (`FETCH_SERVER_ID`)은 fetch audience 하나뿐이고 플랫폼 검색(`SEARCH_SERVER_ID`)은 search
    audience 하나뿐이다. 그 토큰은 다른 내부 서버가 모두 거절한다 (P39b 12·13절)."""
    if server_id == FETCH_SERVER_ID:
        return [fetch_audience(tenant)]
    if server_id == SEARCH_SERVER_ID:
        return [search_audience(tenant)]
    tenant_audience = internal_audience(tenant)
    if server_id is None:
        return [tenant_audience]
    return [tenant_audience, server_audience(tenant, server_id)]


def gateway_groups(tenant: str) -> list[str]:
    """플랫폼이 사람 경계(vMCP, 노출 앞단)에 내는 토큰의 groups. 모든 모드에서 테넌트의 고정
    그룹 하나다 (P53b 6.1절). 저장된 mcp_group 을 싣지 않는다. 테넌트 관리자가 그 칸에 다른
    테넌트의 고정 그룹을 적어도 플랫폼의 토큰이 그 테넌트의 경계를 열지 않게 한다."""
    return [default_mcp_group(tenant)]


def audiences_due(token: str | None, tenant: str, server_id: str | None) -> bool:
    """토큰의 audience 가 지금 원하는 것과 다른가. 원하는 audience 가 하나라도 빠졌거나, 그것과
    물러나는 테넌트 audience 밖의 audience 를 실으면 참이다. 없거나 JWT 가 아니어도 참이다."""
    peek = InternalIssuer.peek(token) if token else None
    if peek is None:
        return True
    have = set(peek.audiences)
    wanted = set(token_audiences(tenant, server_id))
    return not wanted <= have or bool(have - wanted - {internal_audience(tenant)})


def _expires_within(exp: int, now: datetime, renew_before: timedelta) -> bool:
    """만료(epoch 초)가 now 에서 renew_before 안인가. now 는 naive 든 aware 든 UTC 로 본다."""
    return from_epoch(exp) < naive_utc(now) + renew_before


def token_due(
    issuer: InternalIssuer, token: str | None, now: datetime, renew_before: timedelta
) -> bool:
    """다시 낼 때인가. 없음·JWT 아님·서명 키 변경·발급자 주소 변경·만료가 renew_before 안이면
    참이다. 발급자 주소를 바꾸면 검증기는 다음 쓰기 경로에서 새 주소를 보는데 토큰의 iss 는 옛
    주소라 401 이 된다. 서비스 토큰 스윕(만료 6개월 전)과 LLM 토큰(30일 전, P38 5절)이 쓴다."""
    if not token:
        return True
    peek = InternalIssuer.peek(token)
    if peek is None or peek.kid != issuer.kid or peek.exp is None or peek.iss != issuer.issuer:
        return True
    return _expires_within(peek.exp, now, renew_before)


def bound_current(
    issuer: InternalIssuer,
    token: str | None,
    *,
    sub: str,
    tenant: str,
    gateway_id: str,
    rules: str | None,
    now: datetime,
    renew_before: timedelta = RENEW_BEFORE,
) -> bool:
    """게시된 Gateway(문서 id gateway_id)에 묶인 토큰이 지금 낼 것과 같은가 (P57 6.1절). 배포, 다시
    렌더하기, 스윕이 함께 쓴다. 서명 키의 `kid`, 사람 발급자, `aud` 가 데이터 플레인 값 하나,
    `groups` 없음, sub, tenant, `bound_to`, 규칙 지문(rules), 그리고 만료가 renew_before 밖이다.
    서비스 토큰의 검사(`token_due`)는 서비스 발급자를, Agent 의 gateway_token 검사는 고정 그룹을
    원하므로 이 토큰에 맞지 않는다."""
    peek = InternalIssuer.peek(token) if token else None
    if peek is None or peek.exp is None:
        return False
    wanted = (
        issuer.kid,
        issuer.human_issuer,
        (issuer.mcp_audience,),
        sub,
        tenant,
        GATEWAY_TARGET.format(gateway_id),
        rules,
    )
    have = (peek.kid, peek.iss, peek.audiences, peek.sub, peek.tenant, peek.bound_to, peek.rules)
    if have != wanted or peek.has_groups:
        return False
    return not _expires_within(peek.exp, now, renew_before)


class SvcTokens:
    def __init__(
        self,
        issuer: InternalIssuer,
        tenant_settings: MongoTenantSettingsStore,
        settings: Settings,
    ):
        self.issuer = issuer
        self.tenant_settings = tenant_settings
        self.settings = settings

    @property
    def available(self) -> bool:
        """서명 키가 붙었는가. home 클러스터가 없는 배포본은 토큰을 못 낸다."""
        return self.issuer.loaded

    async def groups(self, tenant: str) -> list[str]:
        ts = await self.tenant_settings.get(tenant)
        return [human_mcp_group(self.settings, ts)]

    async def mint(
        self,
        *,
        kind: str,
        name: str,
        tenant: str,
        server_id: str | None = None,
        ttl: int = SERVICE_TOKEN_TTL,
        rules: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """`svc:<kind>:<name>` 이 서버 server_id 에 내는 내부 홉 토큰 (`token_audiences`). rules 는
        그 서버의 규칙 지문(Gateway 의 Entry 토큰, P52 2절), agent_id 는 Agent 의 id(P52 3절)다."""
        return self.issuer.mint(
            sub=service_sub(kind, name),
            tenant=tenant,
            groups=await self.groups(tenant),
            ttl=ttl,
            audiences=token_audiences(tenant, server_id),
            extra={"rules": rules or "", "agent_id": agent_id or ""},
        )

    async def mint_gateway(
        self,
        *,
        name: str,
        tenant: str,
        ttl: int = SERVICE_TOKEN_TTL,
        agent_id: str | None = None,
    ) -> str:
        """데이터 플레인이 플랫폼을 믿는 모드의 에이전트가 Gateway(vMCP)를 부를 토큰 (`aud:
        mcp_audience`). 그룹은 고정 그룹이다(`gateway_groups`). agent_id 는 Tool 규칙의 Agent 절이
        보는 Agent 의 id 다 (P52 3절)."""
        return self.issuer.mint_gateway(
            sub=service_sub("agent", name),
            tenant=tenant,
            groups=gateway_groups(tenant),
            ttl=ttl,
            extra={"agent_id": agent_id or ""},
        )

    async def gateway_token_current(self, token: str | None, tenant: str) -> bool:
        """gateway_token 이 지금 `mint_gateway` 가 낼 iss·aud·groups 를 싣는가. iss 는 사람
        발급자라 공개 주소가 바뀌면 옛 iss 의 토큰을 새로 낸다 (P32 8절). 저장된 mcp_group 을 싣고
        나간 토큰도 새로 낸다 (P53b 6.1절)."""
        return claims_current(
            token,
            iss=self.issuer.human_issuer,
            aud=self.issuer.mcp_audience,
            groups=gateway_groups(tenant),
        )

    async def platform_token(self, tenant: str, server_id: str | None = None) -> str:
        """플랫폼 자신이 서버 server_id 에 부를 토큰(tools/list, 검증기 점검). 호출마다 새로 내고
        짧다. 서버를 모르는 호출(테넌트 audience 로 검증하는 검증기의 점검)은 테넌트 audience
        하나다."""
        return self.issuer.mint(
            sub=PLATFORM_SUB,
            tenant=tenant,
            groups=await self.groups(tenant),
            ttl=PLATFORM_CALL_TTL,
            audiences=token_audiences(tenant, server_id),
        )

    def mint_llm(
        self,
        *,
        name: str,
        tenant: str,
        agent_id: str,
        providers: list[str],
        ttl: int = LLM_TOKEN_TTL,
    ) -> str:
        """Agent 런타임이 LLM 게이트웨이를 부를 토큰 (P38 5절). 보통 90일이고 미리 보기는 그 끝 +
        10분까지다(P44b 8절). providers 는 그래프가 쓰는 provider 의 slug 다. groups 는 비운다."""
        return self.issuer.mint_llm(
            sub=service_sub("agent", name),
            tenant=tenant,
            providers=providers,
            ttl=ttl,
            extra={"agent_id": agent_id},
        )

    def platform_llm_bearer(
        self, tenant: str, purpose: str, provider: str, agent_id: str | None = None
    ) -> str:
        """플랫폼의 비서·스펙 생성기·Knowledge 인덱서·채점 모델이 요청마다 받는 5분 토큰 (P38
        5절). 한 작업이 300초 호출을 몇 번이고 다시 하므로 모델을 만들 때 한 번 낸 토큰은 작업보다
        먼저 끝난다. 그래서 요청마다 낸다. 채점 모델은 원본 Agent 의 id 를 함께 싣는다 (P45
        11절)."""
        extra = {"purpose": purpose}
        if agent_id is not None:
            extra["agent_id"] = agent_id
        return self.issuer.mint_llm(
            sub=PLATFORM_SUB,
            tenant=tenant,
            providers=[provider],
            ttl=PLATFORM_CALL_TTL,
            extra=extra,
        )

    @property
    def mints_gateway_tokens(self) -> bool:
        """Gateway(vMCP)가 받는 토큰을 낼 수 있는가. vMCP 는 데이터 플레인이 믿는 사람 발급자의
        토큰만 받으므로 플랫폼을 믿는 모드(local, platform 인 oidc)뿐이다. IdP 를 믿는 oidc 의
        vMCP 는 IdP 토큰만 받고 header 모드는 토큰을 보지 않는다 (P53b 6.1절)."""
        return self.settings.data_plane_trusts_platform

    async def platform_gateway_token(self, tenant: str) -> str:
        """플랫폼 자신이 Gateway(vMCP)와 노출 앞단에 tools/list 할 토큰. `platform_token` 처럼
        호출마다 새로 내고 짧다. groups 가 고정 그룹이라 vMCP 의 Cedar(THVGroup)를 통과한다."""
        return self.issuer.mint_gateway(
            sub=PLATFORM_SUB, tenant=tenant, groups=gateway_groups(tenant), ttl=PLATFORM_CALL_TTL
        )

    async def ensure_bound(
        self,
        client: KubeClient,
        *,
        secret_name: str,
        namespace: str,
        name: str,
        tenant: str,
        gateway_id: str,
        rules: str | None,
        now: datetime,
        fresh: bool = False,
    ) -> str:
        """vMCP name 이 게시된 Gateway(문서 id gateway_id)에 묶여 싣는 토큰 (P57 6.1절). Secret 의
        것이 지금 낼 것과 같으면(`bound_current`) 그대로 쓴다. 그래서 할 일이 없는 스윕은 소비자를
        굴리지 않는다. fresh 면 새로 낸다(서비스 토큰 회전)."""
        sub = service_sub("vmcp", name)
        if not fresh:
            existing = await run_in_threadpool(existing_svc_token, client, secret_name, namespace)
            if bound_current(
                self.issuer,
                existing,
                sub=sub,
                tenant=tenant,
                gateway_id=gateway_id,
                rules=rules,
                now=now,
            ):
                assert existing is not None
                return existing
        return self.issuer.mint_bound(
            sub=sub, tenant=tenant, target=GATEWAY_TARGET.format(gateway_id), rules=rules
        )

    async def ensure(
        self,
        client: KubeClient,
        *,
        secret_name: str,
        namespace: str,
        kind: str,
        name: str,
        tenant: str,
        server_id: str | None = None,
        fresh: bool = False,
        rules: str | None = None,
    ) -> str:
        """소비자 Secret 에 토큰이 있고 audience 와 규칙 지문(rules)이 지금 원하는 것이면 그것을,
        아니면 새로 낸 것을 준다. 재배포마다 토큰이 바뀌면 파드가 든 것과 Secret 의 것이 갈라져
        리컨실의 만료 판단이 틀어진다. fresh 면 Secret 을 보지 않고 새로 낸다(서비스 토큰 회전)."""
        existing = None
        if not fresh:
            existing = await run_in_threadpool(existing_svc_token, client, secret_name, namespace)
        if existing and not audiences_due(existing, tenant, server_id):
            peek = InternalIssuer.peek(existing)
            if peek is not None and peek.rules == rules:
                return existing
        return await self.mint(
            kind=kind, name=name, tenant=tenant, server_id=server_id, rules=rules
        )
