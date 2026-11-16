"""Agent 파드가 뜰 때 읽는 입력 (P72 2.1절, P35 12·13절). provider 키 복사본 Secret 과, 렌더가
읽은 키 env 이름의 표·토큰 env 표·설정 지문의 기록이다. `AgentPodInputs` 는 서비스가 생성자에서
만들고 저장과 이름 묶음을 콜러블로 넘긴다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.templates import render_llm_secret
from daegam.k8s.templates.agent import llm_secret_name_for
from daegam.services.clusters import ClusterService
from daegam.services.llm_providers import ProviderError, resolve_api_key
from daegam.store.agents import Agent
from daegam.store.base import hash_token
from daegam.store.settings import LLMProvider

# provision 이 Deployment 를 적용하기 바로 전에 문서에 남기는, 파드가 뜰 때 읽는 입력의 기록.
# 키 env 이름의 표(P35 12절), 설정 지문(13절), 턴 drain 결정(P78 5절)이다
POD_INPUT_RECORDS: frozenset[str] = frozenset(
    {"llm_env", "config_digest", "mcp_token_tables", "turn_drain_seconds"}
)


class AgentPodInputs:
    """provider 키를 복사하고 파드 입력의 기록을 남긴다."""

    def __init__(
        self,
        *,
        cluster_service: ClusterService,
        settings: Settings,
        k8s: Callable[[Agent], Any],
        save_deployment: Callable[..., Awaitable[None]],
        error_cls: type[ServiceError],
    ):
        self.cluster_service = cluster_service
        self.settings = settings
        self.k8s = k8s
        self.save_deployment = save_deployment
        self.error_cls = error_cls

    async def copy_llm_secrets(
        self,
        doc: Agent,
        ctx: Any,
        client: KubeClient,
        *,
        expect_state: Collection[str] = frozenset({"deploying"}),
    ) -> dict[str, str]:
        """그래프가 쓰는 provider 마다 키를 읽어 복사본을 SSA 로 쓴다. stub 은 키가 없다. 더는 쓰지
        않는 복사본(폴백을 뺀 경우 등)은 지운다. 목록은 문서 llm_secrets 에 남는다. 돌려주는 것은
        설정 지문에 드는 {복사본 Secret 이름: 키 sha256} 이다 (P35 13절). 키 평문은 여기서만
        보고 문서에는 해시도 남기지 않는다. 직접 경로의 묶기만 부른다(`AgentLlm.bind`). 재적용이
        직접 경로로 옮길 때는 expect_state 가 살아 있는 상태다."""
        k8s = self.k8s(doc)
        secret_store = self.cluster_service.secret_store()
        # 적용하기 전에 쓸 이름을 옛 이름과 합쳐 먼저 남긴다. 적용과 저장 사이에 삭제가 끼어도
        # 삭제가 이름으로 모든 복사본을 찾는다 (P66 2.5절)
        wanted = self.llm_secret_names(doc, ctx.providers)
        ahead = list(dict.fromkeys([*doc.llm_secrets, *wanted]))
        if ahead != doc.llm_secrets:
            doc.llm_secrets = ahead
            await self.save_deployment(
                doc, {"llm_secrets"}, expect_state=expect_state, action="copy provider keys"
            )
        keys: dict[str, str] = {}
        for p in ctx.providers:
            if p.is_stub:
                continue
            try:
                # Secret 읽기는 동기 K8s 호출이고 403 이면 잠깐씩 다시 하므로 스레드풀에서 읽는다
                key = await run_in_threadpool(
                    resolve_api_key, p, secret_store, namespace=self.settings.namespace
                )
            except ProviderError as e:
                raise self.error_cls(422, "provider_no_key", f"provider {p.name!r}: {e}") from e
            rendered = render_llm_secret(doc, p.name, key)
            await run_in_threadpool(client.apply, rendered)
            keys[rendered["metadata"]["name"]] = hash_token(key)
        names = list(keys)
        for stale in doc.llm_secrets:
            if stale not in names:
                await run_in_threadpool(client.delete, "v1", "Secret", stale, k8s.namespace)
        if names != doc.llm_secrets:
            doc.llm_secrets = names
            await self.save_deployment(
                doc, {"llm_secrets"}, expect_state=expect_state, action="copy provider keys"
            )
        return keys

    def llm_secret_names(self, doc: Agent, providers: Sequence[LLMProvider]) -> list[str]:
        """그래프의 provider 마다의 키 복사본 이름. stub 은 키가 없어 빠진다."""
        name = self.k8s(doc).name
        return [llm_secret_name_for(name, p.name) for p in providers if not p.is_stub]

    def has_llm_copies(self, doc: Agent, providers: Sequence[LLMProvider]) -> bool:
        """문서의 llm_secrets 가 그래프의 provider 마다의 복사본을 모두 적고 있는가."""
        return set(self.llm_secret_names(doc, providers)) <= set(doc.llm_secrets)

    async def record(
        self,
        doc: Agent,
        before: dict[str, Any],
        *,
        expect_state: Collection[str] = frozenset({"deploying"}),
    ) -> None:
        """렌더가 읽은 키 env 이름의 표(P35 12절), 토큰 env 표(P50 9절), 설정 지문(13절)과 턴 drain
        결정(P78 5절)을 문서에 남긴다. init 때의 컴파일이 같은 표를 읽어야 하므로 Deployment 를
        적용하기 바로 전에 쓴다. 그 전에 provision 이 실패하면 옛 템플릿으로 다시 뜨는 파드가 옛
        표의 컴파일을 계속 받는다. `before` 는 문서에 있던 값이고 같으면 쓰지 않는다. 상태가
        expect_state 밖이라 쓰지 못하면 409 state_conflict 로 멈춰 그 뒤의 Secret 과 Deployment 를
        건드리지 않는다."""
        changed = {name for name, old in before.items() if getattr(doc, name) != old}
        if changed:
            await self.save_deployment(
                doc, changed, expect_state=expect_state, action="record pod inputs"
            )
