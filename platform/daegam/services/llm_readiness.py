"""게이트웨이 경로를 열어도 되는가 (P38 6절).

Agent 의 경로 결정(`agent_llm.decide`)과 플랫폼 호출자의 길(`llm_endpoint.LlmEndpoints`)이 같은
규칙을 쓴다. 스위치가 켜졌고, 리비전이 한 번 반영됐고, 서명 키가 붙었고, 그 provider 가 반영된
리비전에 있어야 한다. 끈 설치는 상태 문서를 읽지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from daegam.config import Settings
from daegam.services.svc_tokens import SvcTokens
from daegam.store.llm_gateway import MongoLlmGatewayStore

# 경로가 닫힌 까닭. 이 차례가 Agent 경로 표(6절)의 앞 세 행이다
GATEWAY_OFF = "gateway_off"
GATEWAY_NOT_READY = "gateway_not_ready"
NO_SIGNER = "no_signer"


@dataclass(frozen=True)
class GatewayReadiness:
    """한 번 읽은 게이트웨이의 준비. providers_ready 는 반영된 리비전의 provider 이름이다."""

    on: bool
    ready: bool
    signer: bool
    providers_ready: frozenset[str]
    # 반영된 리비전이 채점 호출의 목적 속성을 싣는가 (P45 11절)
    carries_purpose: bool

    def closed(self) -> str | None:
        """경로가 닫힌 까닭 가운데 표의 첫 것. 열렸으면 None 이다."""
        if not self.on:
            return GATEWAY_OFF
        if not self.ready:
            return GATEWAY_NOT_READY
        if not self.signer:
            return NO_SIGNER
        return None

    def opens(self, provider: str) -> bool:
        """그 provider 를 게이트웨이로 불러도 되는가."""
        return self.closed() is None and provider in self.providers_ready

    def opens_evaluator(self, provider: str) -> bool:
        """채점 모델이 그 provider 를 게이트웨이로 불러도 되는가. 반영된 리비전이 목적 속성을
        실어야 수집이 그 기록을 Agent 의 것으로 받는다 (P45 11절)."""
        return self.carries_purpose and self.opens(provider)


async def read_readiness(
    settings: Settings,
    store: MongoLlmGatewayStore | None,
    svc_tokens: SvcTokens | None,
) -> GatewayReadiness:
    """지금의 준비. 상태 문서가 없으면(배선 전, 테스트) 끈 것으로 본다."""
    if not settings.llm_gateway_on or store is None:
        return GatewayReadiness(
            on=False,
            ready=False,
            signer=False,
            providers_ready=frozenset(),
            carries_purpose=False,
        )
    state = await store.get()
    return GatewayReadiness(
        on=True,
        ready=state.ready_at is not None,
        signer=svc_tokens is not None and svc_tokens.available,
        providers_ready=frozenset(state.providers_ready),
        carries_purpose=state.purpose_rev is not None and state.purpose_rev == state.applied_rev,
    )
