"""Agent 의 인스턴스 토큰과 그 Secret (P72 2.1절, D41, P50 9절).

인스턴스 토큰은 init 이 agent.json 을 받고 플랫폼과 런타임이 서로를 부르는 비밀이다. 같은
Secret 에 런타임이 MCP Server·Gateway 를 부를 서비스 토큰도 산다. MCP Server 소스마다의 토큰은 파드
템플릿마다의 토큰 env 표(`McpTokenTable`)가 정한다. `AgentTokens` 는 서비스가 생성자에서 만들고
저장·가드·클라이언트를 콜러블로 넘긴다(`agent_runtime.AgentRuntimeProxy` 와 같은 꼴).

토큰의 수명과 다시 낼 때는 문서가 정한다(P44b 8절, `token_lifetime`·`renew_before`). 미리 보기의
서비스·Gateway·LLM 토큰은 미리 보기의 끝 + 10분에 끝나고 그 전에 다시 낼 때가 오지 않는다.
그래서 provision·재적용·렌더 번호·회전의 다시 쓰기가 열린 미리 보기의 토큰을 새로 내지 않고
`svc_token_rev` 도 그대로라 파드가 굴러가지 않는다. 새로 내는 토큰은 모두 `acting_id` 와
`acting_k8s_name` 을 싣는다. 미리 보기는 원본의 것이라 Tool 규칙이 원본으로 대한다 (9절).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timedelta
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.auth.service_token import (
    LLM_TOKEN_RENEW_DAYS,
    SERVICE_TOKEN_TTL,
    InternalIssuer,
    claims_current,
)
from daegam.auth.signer import LLM_AUDIENCE
from daegam.errors import ServiceError
from daegam.k8s.client import ClusterUnreachable, KubeClient
from daegam.k8s.templates import render_agent_token_secret
from daegam.k8s.templates.agent import (
    GATEWAY_TOKEN_KEY,
    INIT_CONTAINER,
    LLM_TOKEN_KEY,
    MCP_TOKEN_KEY,
    MCP_TOKEN_TABLE_ENV,
    TOKEN_KEY,
    mcp_token_table,
    selector_labels,
)
from daegam.k8s.templates.svc_token import secret_value, svc_token_rev
from daegam.services import agent_graph
from daegam.services.claims import checkpoint, note_issued
from daegam.services.svc_tokens import SvcTokens, audiences_due, token_due
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, scope_for
from daegam.store.agents import Agent, AgentRevision, McpTokenTable, MongoAgentStore
from daegam.store.base import fields_of, hash_token, issue_token, utcnow

logger = logging.getLogger("daegam.agents")

# 문서가 기억하는 토큰 env 표의 상한 (P50 9절). 보통은 지금 템플릿과 바로 앞 템플릿의 둘이고,
# 롤아웃이 끝나기 전에 소스를 거듭 바꾸면 그 사이 템플릿들의 표가 더해진다
TOKEN_TABLES_MAX = 8


def source_envs(doc: Agent) -> dict[str, str]:
    """Secret 에 둘 MCP Server 토큰 키(env 이름 → 서버 id). 지금 템플릿과 바로 앞 템플릿의 표의
    것이다. 앞 템플릿의 파드가 다 없어지면 그 표가 빠진다 (P50 9절, `prune_token_tables`)."""
    out: dict[str, str] = {}
    for table in doc.mcp_token_tables:
        for server_id, env in table.envs.items():
            out.setdefault(env, server_id)
    return out


def current_envs(doc: Agent) -> dict[str, str] | None:
    """지금 템플릿의 토큰 env 표. 표가 생기기 전 문서는 None 이라 옛 단일 토큰이다."""
    table = doc.current_token_table
    return dict(table.envs) if table is not None else None


def asking_envs(doc: Agent, digest: str | None) -> dict[str, str] | None:
    """init 이 보낸 표 지문의 토큰 env 표 (P50 9절). 지문이 없으면 표가 생기기 전 템플릿의 파드라
    None(옛 단일 토큰)이다. 문서가 기억하지 않는 지문(두 템플릿 전의 것)도 None 이다. 옛 단일 토큰의
    env 는 모든 템플릿에 있으므로 그 파드도 뜬다."""
    if not digest:
        return None
    for table in doc.mcp_token_tables:
        if table.digest == digest:
            return dict(table.envs)
    logger.warning("agent %s: init sent an unknown token table %s", doc.id, digest)
    return None


def llm_revoking(doc: Agent) -> frozenset[str]:
    """롤아웃 뒤에 회수할 LLM 토큰의 jti (P38 6절). Secret 의 토큰이 여기 있으면 게이트웨이 경로가
    새 토큰을 낸다. 직접 경로로 옮긴 직후 다시 게이트웨이 경로로 돌아온 Agent 다."""
    return frozenset(j for c in doc.llm_cleanup for j in c.revoke)


def stamp_svc_tokens(
    doc: Agent,
    mcp_token: str | None,
    gateway_token: str | None,
    source_tokens: dict[str, str],
    llm_token: str | None = None,
) -> None:
    """토큰의 판과 자기 audience 를 실은 서버 목록을 문서에 적는다. 판은 지금 템플릿이 env 로
    받는 토큰들(옛 단일 토큰, gateway_token, 지금 표의 소스 토큰, llm_token)로 정한다 (P50 9절,
    P38 5절). 없는 토큰은 판에 들지 않으므로 llm_token 이 없는 Agent 의 판은 그대로다."""
    current = sorted((current_envs(doc) or {}).items())
    doc.svc_token_rev = svc_token_rev(
        mcp_token, gateway_token, *(source_tokens.get(env) for _sid, env in current), llm_token
    )
    doc.svc_token_servers = [sid for sid, env in current if source_tokens.get(env)]


def carries_id(token: str | None, doc: Agent) -> bool:
    """토큰이 이 Agent 가 규칙에 쓰는 id(`acting_id`)를 `agent_id` 로 싣는가 (P52 3절, P44b 5절).
    없는 토큰은 거짓이다."""
    peek = InternalIssuer.peek(token) if token else None
    return peek is not None and peek.agent_id == doc.acting_id


def token_lifetime(doc: Agent, default: int, now: datetime) -> int:
    """이 문서에 새로 내는 토큰의 수명(초). 미리 보기는 끝 + 10분까지이고(P44b 8절, 끝난 뒤에도
    1초), 나머지는 default 다."""
    if doc.preview is None:
        return default
    return max(1, int((doc.preview.credentials_end - now).total_seconds()))


def renew_before(doc: Agent, default: timedelta) -> timedelta:
    """만료가 이만큼 안으로 들어오면 다시 낸다. 미리 보기는 그 끝 전에 다시 낼 때가 오지 않으므로
    0 이다 (P44b 8절). 나머지는 default 다."""
    return timedelta(0) if doc.preview is not None else default


def pod_finished(pod: dict[str, Any]) -> bool:
    """끝난 파드(Succeeded·Failed, 축출된 파드 포함)는 컨테이너를 다시 띄우지 않는다."""
    return (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed")


def pod_token_table(pod: dict[str, Any]) -> str | None:
    """파드의 init 이 보내는 토큰 env 표 지문. 표가 생기기 전 템플릿의 파드는 None 이다."""
    for container in (pod.get("spec") or {}).get("initContainers") or []:
        if container.get("name") != INIT_CONTAINER:
            continue
        for env in container.get("env") or []:
            if env.get("name") == MCP_TOKEN_TABLE_ENV:
                return env.get("value")
    return None


class AgentTokens:
    """인스턴스 토큰을 내고 다시 쓰고, 토큰 Secret 을 쓰고 읽고, 토큰 env 표를 맞추고 거둔다."""

    def __init__(
        self,
        *,
        tokens: MongoAgentInstanceTokenStore,
        agents: MongoAgentStore,
        svc_tokens: SvcTokens,
        k8s: Callable[[Agent], Any],
        pinned: Callable[[Agent], AgentRevision],
        client_for_doc: Callable[[Agent], Awaitable[KubeClient]],
        save_deployment: Callable[..., Awaitable[None]],
        forget_token: Callable[[str], None],
        live_states: Collection[str],
        error_cls: type[ServiceError],
        named: Callable[[Agent], Awaitable[bool]] | None = None,
    ):
        self.tokens = tokens
        self.agents = agents
        self.svc_tokens = svc_tokens
        self.k8s = k8s
        self.pinned = pinned
        self.client_for_doc = client_for_doc
        self.save_deployment = save_deployment
        self.forget_token = forget_token
        self.live_states = live_states
        self.error_cls = error_cls
        # Tool 규칙이 이 Agent 를 이름 대는가 (P52 3절). 그러면 agent_id 를 싣지 않은 서비스 토큰을
        # 새로 낸다. 없으면 아무도 이름 대지 않는다
        self.named = named
        # LLM 게이트웨이 토큰의 발급 (P38 5절). 배선이 붙인다
        self.llm: Any = None
        # Agent 의 그래프가 쓰는 provider slug. 게이트웨이 경로의 결정(F7)이 붙인다
        self.llm_wanted: Callable[[Agent], list[str] | None] | None = None

    async def ensure_secret(
        self, doc: Agent, ctx: Any, client: KubeClient, token: str | None
    ) -> str | None:
        """인스턴스 토큰 Secret 을 맞추고 설정 지문을 걸 인스턴스 토큰을 돌려준다. 방금 낸
        평문이거나 그대로 쓰는 Secret 의 것이다 (P35 13절).

        평문(`token`)이 오면 그것을 쓰고, 아니면 Secret 이 있고 토큰이 살아 있으며 핀된 리비전을
        읽을 수 있을 때만 건너뛴다 (재실행 안전). 서비스 토큰(D41)도 같은 Secret 에 산다. 그 키가
        빠진 옛 Secret 은 인스턴스 토큰 평문을 모르므로 다시 발급해 통째로 쓴다. 원하는
        iss·aud·groups 와 다른 gateway_token 은 빠진 것으로 본다 (P32 8절)."""
        existing = await self.token_secret(doc, client)
        mcp_token = secret_value(existing, MCP_TOKEN_KEY)
        gateway_token = await self.kept_gateway_token(doc, ctx, existing)
        source_tokens = self.kept_source_tokens(doc, existing)
        mcp_token, gateway_token, source_tokens = await self.keep_named(
            doc, mcp_token, gateway_token, source_tokens
        )
        svc_missing = (
            mcp_token is None
            or (ctx.gateway_token and gateway_token is None)
            or len(source_tokens) < len(source_envs(doc))
        )
        llm_token, llm_providers = self.kept_llm_token(
            doc, existing, wanted=self.wanted_llm(doc), revoking=llm_revoking(doc)
        )
        svc_missing = svc_missing or (llm_providers is not None and llm_token is None)
        held = secret_value(existing, TOKEN_KEY)
        current = await self.instance_current(doc, held)
        if token is None and svc_missing and existing is not None and current:
            # 서비스 토큰 키만 빠진 옛 Secret. 인스턴스 토큰 평문은 Secret 에 있으니 그것을 다시 써
            # 넣는다. 다시 발급하면 굴러가는 사이 플랫폼이 런타임을 부르는 호출이 401 인 창이 생긴다
            token = held
        if token is None and (existing is None or svc_missing or not current):
            token = await self.issue(doc, ctx.rev.revision)
            await self.save_deployment(
                doc, {"token_id"}, expect_state={"deploying"}, action="issue token"
            )
        if token is not None:
            await self.write_secret(
                doc,
                ctx,
                client,
                token,
                mcp_token=mcp_token,
                gateway_token=gateway_token,
                source_tokens=source_tokens,
                llm_token=llm_token,
                llm_providers=llm_providers,
            )
        else:
            stamp_svc_tokens(doc, mcp_token, gateway_token, source_tokens, llm_token)
        return token or secret_value(existing, TOKEN_KEY)

    def wanted_llm(self, doc: Agent) -> list[str] | None:
        """그 Agent 의 토큰이 열어야 할 provider slug. 게이트웨이 경로의 결정(F7)이 붙이기 전에는
        None 이라 Secret 에 든 토큰의 provider 를 그대로 쓴다."""
        return self.llm_wanted(doc) if self.llm_wanted is not None else None

    def kept_llm_token(
        self,
        doc: Agent,
        secret: dict[str, Any] | None,
        *,
        wanted: list[str] | None,
        rotate: bool = False,
        revoking: Collection[str] = (),
    ) -> tuple[str | None, list[str] | None]:
        """Secret 의 llm_token 과 다시 낼 때의 provider slug. wanted 는 그래프가 쓰는 provider 이고,
        든 토큰의 `llm_providers` 가 그와 다르면 다시 낸다(`claims_current`). 그래야 뺀 provider 가
        재배포·교체·갱신을 지나 열린 채 남지 않는다. 교체할 때이거나 만료가 다시 낼 때 안이거나
        (`renew_before`, 미리 보기가 아니면 30일) 그 jti 가 롤아웃 뒤의 회수(revoking)를 기다리면
        다시 낸다. 옛 토큰은 그 롤아웃이 끝난 뒤에 회수한다 (P38 5절, `AgentLlm.after_apply`). 직접
        경로(wanted 가 빈 목록)이거나 토큰도 wanted 도 없으면 (None, None) 이다. SSA 는 stringData
        로 넣은 키를 다음 적용에서 빼도 남기므로 옛 토큰은 정리가 뺄 때까지 Secret 에 있다."""
        token = secret_value(secret, LLM_TOKEN_KEY)
        peek = InternalIssuer.peek(token) if token else None
        held = list(peek.llm_providers) if peek is not None else []
        providers = wanted if wanted is not None else (held or None)
        if not providers:
            return None, None
        issuer = self.svc_tokens.issuer
        renew = renew_before(doc, timedelta(days=LLM_TOKEN_RENEW_DAYS))
        current = token is not None and claims_current(
            token, iss=issuer.issuer, aud=LLM_AUDIENCE, groups=[], llm_providers=providers
        )
        revoked = peek is not None and peek.jti in revoking
        if rotate or revoked or not current or token_due(issuer, token, utcnow(), renew):
            return None, providers
        return token, providers

    async def keep_named(
        self,
        doc: Agent,
        mcp_token: str | None,
        gateway_token: str | None,
        source_tokens: dict[str, str],
    ) -> tuple[str | None, str | None, dict[str, str]]:
        """규칙이 이름 댄 Agent 는 자기 id 를 실은 서비스 토큰만 그대로 쓴다. Agent 절이 그 id 를
        보기 때문이다. 이 기능 전에 낸 토큰만 빠지므로 그 Agent 가 한 번 굴러간다 (P52 3절).
        이름 대지 않은 Agent 는 그대로다."""
        if self.named is None or not await self.named(doc):
            return mcp_token, gateway_token, source_tokens
        return (
            mcp_token if carries_id(mcp_token, doc) else None,
            gateway_token if carries_id(gateway_token, doc) else None,
            {env: t for env, t in source_tokens.items() if carries_id(t, doc)},
        )

    async def token_secret(self, doc: Agent, client: KubeClient) -> dict[str, Any] | None:
        """대상 클러스터의 인스턴스 토큰 Secret. 없으면 None 이다."""
        k8s = self.k8s(doc)
        return await run_in_threadpool(client.get, "v1", "Secret", k8s.token_secret, k8s.namespace)

    async def secret_token(self, doc: Agent, client: KubeClient) -> str | None:
        """토큰 Secret 의 인스턴스 토큰이다. Secret 이 없거나 키가 비면 None 이다. 런타임 프록시가
        이 읽기 위에 캐시와 503 을 얹는다."""
        return secret_value(await self.token_secret(doc, client), TOKEN_KEY)

    async def known_token(self, doc: Agent) -> str | None:
        """Secret 에 있는 인스턴스 토큰이다. 적용하지 않는 렌더(`render`)가 설정 지문을 provision 과
        같은 값으로 다시 걸 때 쓴다. Secret 이 없거나(provision 전, 지워진 것) 클러스터에 연결할
        수 없으면 None 이라 지문은 "0" 이고, 클러스터 오류는 그 렌더를 쓰는 쪽(드리프트 비교·적용)이
        제 자리에서 낸다."""
        try:
            return await self.secret_token(doc, await self.client_for_doc(doc))
        except (self.error_cls, ClusterUnreachable):
            return None

    async def kept_gateway_token(
        self, doc: Agent, ctx: Any, secret: dict[str, Any] | None
    ) -> str | None:
        """Secret 의 gateway_token 가운데 다시 써도 되는 것. Gateway 소스를 쓰지 않거나 iss·aud·
        groups 가 지금 원하는 값과 다르면 None 이라 부르는 쪽이 새로 낸다 (P32 8절)."""
        if not ctx.gateway_token:
            return None
        token = secret_value(secret, GATEWAY_TOKEN_KEY)
        current = await self.svc_tokens.gateway_token_current(token, doc.tenant)
        return token if current else None

    async def issue(self, doc: Agent, revision: int) -> str:
        """인스턴스 토큰을 새로 낸다. 옛 것은 회수하고 핀된 리비전의 scope 로 만든 뒤 `token_id` 를
        채우고 평문을 돌려준다. 저장은 부르는 쪽이 자기 상태 조건으로 한다. 회수와 발급 앞에서
        클레임을 확인해 클레임을 넘긴 실행이 새 홀더의 토큰을 회수하지 않게 한다 (P66 2.2절)."""
        await checkpoint()
        await self.tokens.revoke_for_instance(doc.id)
        token, digest = issue_token()
        tok = await self.tokens.create(
            instance_id=doc.id,
            profile_id=doc.id,
            token_sha256=digest,
            scope=[scope_for(doc.id, revision)],
        )
        note_issued(tok.id)
        doc.token_id = tok.id
        return token

    async def rewrite_secret(
        self,
        doc: Agent,
        ctx: Any,
        client: KubeClient,
        token: str,
        existing: dict[str, Any] | None,
        *,
        rotate: bool,
    ) -> None:
        """재적용과 회전의 토큰 Secret (`AgentService._apply_consumer`). 회전은 서비스 토큰을 모두
        새로 낸다. 재적용은 Secret 의 것을 다시 쓰되 빠졌거나 audience 가 그 서버에 원하는 것과 다른
        소스 토큰(P50 9절), 원하는 값과 다른 gateway_token, 원하는 provider 를 열지 않거나 회수를
        기다리는 llm_token(P38 5절·6절)만 새로 낸다. 미리 보기는 회전도 재적용처럼 다룬다. 그 토큰은
        끝 + 10분까지라 다시 낼 까닭이 없다 (P44b 8절)."""
        rotate = rotate and doc.preview is None
        mcp_token = None if rotate else secret_value(existing, MCP_TOKEN_KEY)
        gateway_token = None if rotate else await self.kept_gateway_token(doc, ctx, existing)
        source_tokens = {} if rotate else self.kept_source_tokens(doc, existing)
        llm_token, llm_providers = self.kept_llm_token(
            doc, existing, wanted=self.wanted_llm(doc), rotate=rotate, revoking=llm_revoking(doc)
        )
        mcp_token, gateway_token, source_tokens = await self.keep_named(
            doc, mcp_token, gateway_token, source_tokens
        )
        await self.write_secret(
            doc,
            ctx,
            client,
            token,
            mcp_token=mcp_token,
            gateway_token=gateway_token,
            source_tokens=source_tokens,
            llm_token=llm_token,
            llm_providers=llm_providers,
        )

    async def write_secret(
        self,
        doc: Agent,
        ctx: Any,
        client: KubeClient,
        token: str,
        *,
        mcp_token: str | None,
        gateway_token: str | None,
        source_tokens: dict[str, str],
        llm_token: str | None = None,
        llm_providers: list[str] | None = None,
    ) -> None:
        """토큰 Secret 을 통째로 다시 쓴다. 빠진 서비스 토큰(D41)은 새로 내고, 토큰 캐시를 비우고
        `svc_token_rev` 를 맞춘다. 있던 서비스 토큰을 넘기면 그대로 다시 쓴다. MCP Server 소스마다의
        토큰은 문서가 기억하는 표들(지금 템플릿과 파드가 남았을 수 있는 앞 템플릿들)이 정하고(P50
        9절), 옛 단일 토큰은 테넌트 audience 하나로 남는다. svc_tokens 배선은 부르는 쪽이 먼저
        본다."""
        # 서비스 토큰을 내기 전에 클레임을 확인한다 (P66 2.2절)
        await checkpoint()
        # 새로 내는 토큰은 모두 규칙이 보는 id 와 주체를 싣는다 (P52 3절, P44b 9절). 수명은 문서가
        # 정한다 (P44b 8절)
        name, agent_id = doc.acting_k8s_name, doc.acting_id
        ttl = token_lifetime(doc, SERVICE_TOKEN_TTL, utcnow())
        if mcp_token is None:
            mcp_token = await self.svc_tokens.mint(
                kind="agent", name=name, tenant=doc.tenant, ttl=ttl, agent_id=agent_id
            )
        if ctx.gateway_token and gateway_token is None:
            gateway_token = await self.svc_tokens.mint_gateway(
                name=name, tenant=doc.tenant, ttl=ttl, agent_id=agent_id
            )
        for env, server_id in source_envs(doc).items():
            if env not in source_tokens:
                source_tokens[env] = await self.svc_tokens.mint(
                    kind="agent",
                    name=name,
                    tenant=doc.tenant,
                    server_id=server_id,
                    ttl=ttl,
                    agent_id=agent_id,
                )
        if llm_providers is not None and llm_token is None and self.llm is not None:
            # 게이트웨이 경로의 토큰은 기록을 먼저 남긴다 (P38 5절)
            llm_token = await self.llm.mint(doc, llm_providers)
        await run_in_threadpool(
            client.apply,
            render_agent_token_secret(
                doc,
                token,
                mcp_token=mcp_token,
                gateway_token=gateway_token,
                source_tokens=source_tokens,
                llm_token=llm_token,
            ),
        )
        self.forget_token(doc.id)
        stamp_svc_tokens(doc, mcp_token, gateway_token, source_tokens, llm_token)

    async def tables(self, doc: Agent) -> list[McpTokenTable]:
        """파드 템플릿마다의 토큰 env 표 (P50 9절). 지금 템플릿의 표가 앞이고, 그 뒤는 파드가 아직
        남았을 수 있는 앞 템플릿들의 표다. 지금 표는 핀한 리비전의 MCP Server 소스로 정하므로
        소스가 같으면 표도 같아 그대로다. 소스가 바뀌면 새 표가 앞에 오고, 앞 표들은 그 파드가 다
        없어진 뒤에 스윕이 거둔다(`prune_token_tables`). `TOKEN_TABLES_MAX` 를 넘는 가장 오랜 표는
        버린다."""
        table = mcp_token_table(agent_graph.mcp_server_sources(self.pinned(doc)))
        current = doc.current_token_table
        if current is not None and current.digest == table.digest:
            return doc.mcp_token_tables
        older = [t for t in doc.mcp_token_tables if t.digest != table.digest]
        return [table, *older][:TOKEN_TABLES_MAX]

    def kept_source_tokens(self, doc: Agent, secret: dict[str, Any] | None) -> dict[str, str]:
        """Secret 의 MCP Server 토큰 가운데 다시 써도 되는 것(env → 토큰). audience 가 그 서버에
        원하는 것과 다르면 빠져 부르는 쪽이 새로 낸다 (P50 9절)."""
        kept: dict[str, str] = {}
        for env, server_id in source_envs(doc).items():
            token = secret_value(secret, env)
            if token and not audiences_due(token, doc.tenant, server_id):
                kept[env] = token
        return kept

    async def prune_tables(self, doc: Agent, client: KubeClient) -> bool:
        """앞 템플릿들의 토큰 env 표를 그 파드가 다 없어졌으면 거둔다 (P50 9절). 끝나지 않은 이
        Agent 의 파드가 모두 지금 표의 지문을 들 때만이다. secretKeyRef 는 키가 빠지면 파드가 뜨지
        못하기 때문이다. 문서에서 앞 표들을 빼고 Secret 에서 앞 표들에만 있던 키를 이름으로 뺀다.
        뺄 키는 읽은 문서의 표로 정하고 문서는 읽은 배포가 그대로일 때만 고치므로, 그 사이 재배포가
        낸 키는 남는다. 서비스 토큰 스윕이 부른다. 거뒀으면 참이다."""
        current = doc.current_token_table
        older = [t for t in doc.mcp_token_tables if t is not current]
        if current is None or not older or doc.k8s is None:
            return False
        pods = await run_in_threadpool(
            client.list, "v1", "Pod", doc.k8s.namespace, selector_labels(doc)
        )
        if any(pod_token_table(p) != current.digest for p in pods if not pod_finished(p)):
            return False
        stale = set().union(*(t.envs.values() for t in older)) - set(current.envs.values())
        doc.mcp_token_tables = [current]
        saved = await self.agents.set_fields(
            doc.id,
            fields_of(doc, {"mcp_token_tables"}),
            expect_state=self.live_states,
            expect_deployment=doc.deploying_since,
        )
        if not saved:
            return False
        if stale:
            await run_in_threadpool(
                client.drop_secret_keys, doc.k8s.token_secret, doc.k8s.namespace, sorted(stale)
            )
        return True

    async def instance_current(self, doc: Agent, token: str | None) -> bool:
        """Secret 의 인스턴스 토큰을 다시 써도 되는가 (P66 2.7절). Secret 의 토큰이 문서가 가리키는
        기록(`token_id`)의 해시와 같고, 그 기록이 살아 있으며 핀된 리비전의 scope 를 가져야 한다.
        문서의 기록만 보면 두 실행이 겹치거나 저장과 Secret 쓰기 사이에 멈춘 뒤 Secret 에 남은
        회수된 토큰을 영영 다시 쓴다. 토큰을 다시 쓰는 자리(provision 의 Secret 맞추기, 재렌더)와
        서비스 토큰 스윕이 모두 이것을 부른다."""
        if not token or not doc.token_id or doc.deployed_revision is None:
            return False
        tok = await self.tokens.get(doc.token_id)
        return (
            tok is not None
            and tok.active
            and tok.token_sha256 == hash_token(token)
            and scope_for(doc.id, doc.deployed_revision) in tok.scope
        )
