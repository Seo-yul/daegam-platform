"""Agent 의 LLM 게이트웨이 토큰과 그 회수 (P38 5절·6절).

- 발급: 토큰을 Secret 에 쓰기 전에 jti 와 만료를 `Agent.llm_tokens` 에 남긴다. 겹친 발급도 Secret 에
  닿은 토큰을 모두 회수할 수 있다. 쓸 때마다 만료된 기록을 뺀다
- 회수: jti 를 `llm_revocations` 에 넣고 게이트웨이 설정을 곧바로 맞춘다. 해제와 삭제는 살아 있는
  토큰을 모두 회수한다. 새 토큰을 낸 적용(교체, 갱신, provider 를 바꾼 재배포)은 그 앞의 살아 있는
  토큰을 옛 ReplicaSet 의 파드가 다 없어진 뒤에 회수한다(`Agent.llm_cleanup`). 명시적 교체의
  `immediate` 는 새 토큰 말고 살아 있는 토큰을 모두 곧바로 회수한다
- 정리: 게이트웨이 동기화가 틱마다 정리가 남은 Agent 의 Deployment 를 이름으로 읽고 롤아웃이 끝난
  것만 처리한다. 롤아웃이 멈추면 기다린다. 굴리기는 최선일 뿐이고 옛 토큰은 만료까지 살기 때문이다
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool

from daegam.auth.service_token import LLM_TOKEN_TTL, InternalIssuer
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.templates.agent import LLM_TOKEN_KEY, selector_labels
from daegam.k8s.templates.common import rolled_out
from daegam.k8s.templates.svc_token import secret_value
from daegam.services.agent_tokens import llm_revoking, pod_finished, token_lifetime
from daegam.services.instance_status import live_newest_pods
from daegam.services.svc_tokens import SvcTokens
from daegam.store.agents import Agent, LlmCleanup, LlmTokenRecord, MongoAgentStore
from daegam.store.auth_reapply import LIVE_STATES
from daegam.store.base import utcnow
from daegam.store.llm_revocations import MongoLlmRevocationStore

logger = logging.getLogger("daegam.llm_tokens")

# 토큰을 내는 자리. provision(deploying)과 재적용·회전(deployed)이다
MINT_STATES = frozenset({"deploying", *LIVE_STATES})


# 클레임 안에서 도는 정리 한 번. 처리했으면 True 다
Work = Callable[[], Awaitable[bool]]


class LlmTokenError(ServiceError):
    pass


def _at(epoch: int | None) -> datetime:
    return datetime.fromtimestamp(epoch or 0, tz=UTC).replace(tzinfo=None)


def _ready(doc: Agent, generation: int) -> list[LlmCleanup]:
    """그 세대의 롤아웃으로 끝난 정리. 세대를 적지 못한 정리는 어느 롤아웃 뒤든 된다."""
    return [c for c in doc.llm_cleanup if generation >= (c.generation or 0)]


class LlmTokens:
    def __init__(
        self,
        agents: MongoAgentStore,
        revocations: MongoLlmRevocationStore,
        svc_tokens: SvcTokens,
        *,
        client_for_doc: Callable[[Agent], Awaitable[KubeClient]],
        clock: Callable[[], datetime] = utcnow,
    ):
        # 미리 보기의 토큰도 회수하고 정리한다 (P44b 8절)
        self.agents = agents.with_previews()
        self.revocations = revocations
        self.svc_tokens = svc_tokens
        self.client_for_doc = client_for_doc
        self.clock = clock
        # 회수를 적은 뒤 부른다. 게이트웨이가 설정을 곧바로 맞춘다 (`LlmGateway.sync_config`)
        self.on_revoked: Callable[[], Awaitable[None]] | None = None
        # 정리를 문서의 클레임 안에서 돌린다 (`AgentService.claimed_llm_cleanup`). 배선이 붙인다.
        # provision 과 재적용이 쥐는 클레임이라 그 사이의 경로 바뀜과 겹치지 않는다
        self.claimed: Callable[[Agent, Work], Awaitable[bool]] | None = None

    async def mint(self, doc: Agent, providers: list[str]) -> str:
        """그 Agent 의 토큰. 보통 90일이고 미리 보기는 그 끝 + 10분까지이며 원본의 id 와 주체를
        싣는다(P44b 8절·9절). 기록을 먼저 저장하고 평문을 돌려준다. 기록을 남기지 못하면(배포가
        그 사이 바뀌었다) 토큰을 내지 않는다. 기록 없는 토큰은 회수할 수 없기 때문이다."""
        token = self.svc_tokens.mint_llm(
            name=doc.acting_k8s_name,
            tenant=doc.tenant,
            agent_id=doc.acting_id,
            providers=providers,
            ttl=token_lifetime(doc, LLM_TOKEN_TTL, self.clock()),
        )
        peek = InternalIssuer.peek(token)
        assert peek is not None and peek.jti
        record = LlmTokenRecord(jti=peek.jti, iat=_at(peek.iat), exp=_at(peek.exp))
        now = self.clock()
        if not await self.agents.add_llm_token(doc.id, record, now=now, states=MINT_STATES):
            raise LlmTokenError(409, "state_conflict", f"agent {doc.id} left its deployment")
        doc.llm_tokens = [r for r in doc.llm_tokens if r.exp > now] + [record]
        return token

    async def revoke(self, records: list[LlmTokenRecord]) -> None:
        for r in records:
            await self.revocations.add(r.jti, iat=r.iat, exp=r.exp)
        if records and self.on_revoked is not None:
            try:
                await self.on_revoked()
            except Exception as e:  # noqa: BLE001 - 다음 틱이 잇는다
                logger.warning("llm gateway config sync after a revocation failed: %s", e)

    async def revoke_all(self, doc: Agent) -> int:
        """해제와 삭제. 다시 읽은 문서의 살아 있는 토큰을 모두 회수하고 그 기록과 정리를 뺀다.
        파드도 함께 내려간다."""
        fresh = await self.agents.get(doc.id) or doc
        now = self.clock()
        live = [r for r in fresh.llm_tokens if r.exp > now]
        await self.revoke(live)
        if fresh.llm_tokens:
            await self.agents.drop_llm_tokens(doc.id, [r.jti for r in fresh.llm_tokens])
        if fresh.llm_cleanup:
            await self.agents.drop_llm_cleanup(doc.id, [c.id for c in fresh.llm_cleanup])
        doc.llm_tokens, doc.llm_cleanup = [], []
        return len(live)

    def record_of(self, doc: Agent, token: str | None) -> LlmTokenRecord | None:
        peek = InternalIssuer.peek(token) if token else None
        return next((r for r in doc.llm_tokens if peek and r.jti == peek.jti), None)

    async def after_rotation(self, doc: Agent, new_jti: str, *, immediate: bool) -> None:
        """교체 뒤의 옛 토큰. 새 토큰(new_jti) 말고 살아 있는 토큰이 모두 대상이다. 앞서 갱신이나
        provider 를 바꾼 재배포가 바꾼 토큰도 아직 살아 있을 수 있기 때문이다. immediate(유출이
        의심될 때)면 모두 곧바로 회수한다. 곧바로 회수하면 새 파드가 준비될 때까지 옛 파드는 부르지
        못한다. 아니면 다시 렌더한 적용이 롤아웃 뒤의 회수를 남겼다(`AgentLlm.after_apply`). 남기지
        못한 것(그 사이 내려간 Agent)은 곧바로 회수한다. 내려갈 때의 회수가 놓쳤을 수 있다."""
        fresh = await self.agents.get(doc.id) or doc
        now = self.clock()
        others = [r for r in fresh.llm_tokens if r.exp > now and r.jti != new_jti]
        if not immediate:
            pending = llm_revoking(fresh)
            others = [r for r in others if r.jti not in pending]
        await self.revoke(others)

    async def rotate(
        self, doc: Agent, *, immediate: bool, rerender: Callable[[Agent], Awaitable[bool]]
    ) -> None:
        """명시적 교체 (5절). 새 토큰을 내어 파드를 굴리고, 옛 토큰은 `after_rotation` 이 다룬다.
        게이트웨이 경로가 아닌 Agent(Secret 에 llm_token 이 없다)는 409 llm_token_missing 이다.
        rerender 는 서비스 토큰을 모두 새로 내는 다시 렌더다(`rotate_svc_token`). 다른 작업이
        클레임을 쥐어 다시 렌더가 돌지 않았거나 Secret 의 토큰이 그대로면 409 in_progress 이고 아무
        것도 회수하지 않는다. Secret 의 토큰이 아직 옛 파드의 것이기 때문이다. 미리 보기는 건너뛴다.
        그 토큰은 끝 + 10분에 끝나고 아무것도 그것을 갱신하지 않는다 (P44b 8절)."""
        assert doc.k8s is not None
        if doc.preview is not None:
            return
        client = await self.client_for_doc(doc)
        old = self.record_of(doc, await self._held(client, doc))
        # 직접 경로로 옮긴 Agent 의 Secret 에는 정리를 기다리는 옛 토큰이 남아 있을 수 있다
        if old is None or not doc.llm_route.gateway:
            raise LlmTokenError(
                409, "llm_token_missing", f"agent {doc.id} calls its providers directly"
            )
        rendered = await rerender(doc)
        held = await self._held(client, doc)
        now_held = InternalIssuer.peek(held) if held else None
        if not rendered or now_held is None or now_held.jti == old.jti:
            raise LlmTokenError(409, "in_progress", f"agent {doc.id} is busy; try again")
        await self.after_rotation(doc, now_held.jti, immediate=immediate)

    async def _held(self, client: KubeClient, doc: Agent) -> str | None:
        assert doc.k8s is not None
        secret = await run_in_threadpool(
            client.get, "v1", "Secret", doc.k8s.token_secret, doc.k8s.namespace
        )
        return secret_value(secret, LLM_TOKEN_KEY)

    async def holding(self) -> bool:
        """정리가 남은 Agent 가 있는가. 있으면 끈 뒤에도 게이트웨이를 둔다 (6절)."""
        return bool(await self.agents.with_llm_cleanup())

    async def settle(self) -> list[str]:
        """롤아웃이 끝난 Agent 의 정리를 한다. 처리한 Agent id 다."""
        done = []
        for doc in await self.agents.with_llm_cleanup():
            if doc.k8s is not None and await self._settle_one(doc):
                done.append(doc.id)
        return done

    async def _settle_one(self, doc: Agent) -> bool:
        """그 Deployment 의 롤아웃이 끝났고 옛 ReplicaSet 의 파드가 하나도 남지 않았을 때만이다.
        지워지는 옛 파드도 preStop 동안 옛 토큰으로 부른다. 처리는 문서의 클레임 안에서 다시 읽은
        문서와 Deployment 로 한다. 그 사이 경로, 배포(`deploying_since`), Deployment 세대 가운데
        하나라도 바뀌었으면 건너뛴다. 재배포가 경로를 되돌려 다시 만든 복사본이나 새 llm_token 을
        지우지 않기 위해서다."""
        assert doc.k8s is not None
        client = await self.client_for_doc(doc)
        generation = await self._generation(client, doc, rolled=True)
        if generation is None or await self._old_pods_left(client, doc):
            return False
        if not _ready(doc, generation):
            return False
        seen = (doc.llm_route, doc.deploying_since, generation)

        async def work() -> bool:
            fresh = await self.agents.get(doc.id)
            if fresh is None or fresh.k8s is None:
                return False
            now = await self._generation(client, fresh, rolled=False)
            if (fresh.llm_route, fresh.deploying_since, now) != seen:
                return False
            ready = _ready(fresh, generation)
            if not ready:
                return False
            jtis = {j for c in ready for j in c.revoke}
            await self.revoke([r for r in fresh.llm_tokens if r.jti in jtis])
            await self._drop_inputs(client, fresh, ready)
            await self.agents.drop_llm_cleanup(fresh.id, [c.id for c in ready])
            return True

        if self.claimed is None:
            return await work()
        return await self.claimed(doc, work)

    async def _generation(self, client: KubeClient, doc: Agent, *, rolled: bool) -> int | None:
        """그 Deployment 의 세대. rolled 면 롤아웃이 끝나지 않았을 때 None 이다."""
        assert doc.k8s is not None
        dep = await run_in_threadpool(
            client.get, "apps/v1", "Deployment", doc.k8s.name, doc.k8s.namespace
        )
        if rolled and not rolled_out(dep):
            return None
        return int(((dep or {}).get("metadata") or {}).get("generation") or 0)

    async def _drop_inputs(self, client: KubeClient, doc: Agent, ready: list[LlmCleanup]) -> None:
        """옛 경로의 입력을 뺀다 (P38 6절). 게이트웨이 경로였을 때의 `llm_token` 과 직접 경로였을
        때의 키 복사본이다. 그 사이 경로가 되돌아왔으면 지금 템플릿이 그 입력을 읽으므로 둔다."""
        assert doc.k8s is not None
        keys = [] if doc.llm_route.gateway else sorted({k for c in ready for k in c.drop_keys})
        if keys:
            await run_in_threadpool(
                client.drop_secret_keys, doc.k8s.token_secret, doc.k8s.namespace, keys
            )
        secrets = (
            sorted({n for c in ready for n in c.drop_secrets}) if doc.llm_route.gateway else []
        )
        for name in secrets:
            await run_in_threadpool(client.delete, "v1", "Secret", name, doc.k8s.namespace)
        if secrets:
            await self.agents.drop_llm_secrets(doc.id, secrets)

    async def _old_pods_left(self, client: KubeClient, doc: Agent) -> bool:
        """끝나지 않은 파드 가운데 가장 새 템플릿의 살아 있는 파드가 아닌 것이 있는가."""
        assert doc.k8s is not None
        pods = await run_in_threadpool(
            client.list, "v1", "Pod", doc.k8s.namespace, selector_labels(doc)
        )
        running = [p for p in pods if not pod_finished(p)]
        newest = {id(p) for p in live_newest_pods(running)}
        return any(id(p) not in newest for p in running)
