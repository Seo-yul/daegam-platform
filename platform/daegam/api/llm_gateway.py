"""LLM 게이트웨이의 상태 (P38 11절).

`GET /api/settings/llm-gateway` 는 테넌트를 가로질러 세므로 platform-admin(`settings:write`)만
부른다. 값은 동기화(`services/llm_gateway.py`)가 리컨실 틱마다 적은 상태 문서와 설치 env 다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from daegam.api._agent_proxy import get_agent_or_404
from daegam.api.deps import Tenant, require_scope
from daegam.api.schemas import ERROR_RESPONSES, REQUIRED_DEFAULTS
from daegam.store.base import UtcDatetime

router = APIRouter(responses=ERROR_RESPONSES, prefix="/settings", tags=["settings"])
agents_router = APIRouter(responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"])


class LlmGatewayPodOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    name: str
    rev: str | None


class LlmGatewayProviderOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    name: str
    slug: str
    # 반영된 리비전에 이 provider 가 있다. 게이트웨이 경로가 이 provider 를 연다
    ready: bool
    key_synced_at: UtcDatetime | None
    route_url: str
    # 렌더하지 않은 까닭(주소 검사, 키 없음)
    rejected: str | None


class LlmGatewayAgentsOut(BaseModel):
    """배포된 Agent 의 경로 수 (P38 11절). direct 는 지금 결정의 까닭마다의 수, moving 은
    게이트웨이로 결정됐지만 아직 다시 렌더되지 않은 수, cleanups_pending 은 롤아웃 뒤의 정리가 남은
    수다."""

    model_config = REQUIRED_DEFAULTS

    gateway: int
    direct: dict[str, int]
    moving: int
    cleanups_pending: int


class LlmGatewayStatusOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    switch: str
    url: str
    namespace: str
    # 예약한 게이트웨이 네임스페이스를 쓰던 테넌트
    clashing_tenants: list[str]
    activated_at: UtcDatetime | None
    ready_at: UtcDatetime | None
    desired_rev: str | None
    applied_rev: str | None
    reason: str | None
    warnings: list[str]
    pods: list[LlmGatewayPodOut]
    providers: list[LlmGatewayProviderOut]
    limits_rev: str | None
    revocations: int
    revocations_pending: int
    # 수집기의 수집 토큰 만료와 P37 수집 경로가 마지막으로 받은 때 (8절)
    ingest_token_expires_at: UtcDatetime | None
    ingest_received_at: UtcDatetime | None
    synced_at: UtcDatetime | None
    agents: LlmGatewayAgentsOut


@router.get(
    "/llm-gateway",
    response_model=LlmGatewayStatusOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def get_llm_gateway(request: Request) -> dict[str, Any]:
    """platform-admin 만. 테넌트를 가로질러 센다. 스위치·주소·네임스페이스와 그 점검, 켠 시각,
    원하는·반영된 리비전과 파드마다의 리비전, provider 마다의 키 동기화와 반영, 한도 설정의 판,
    회수 목록의 크기, 수집 토큰의 만료와 마지막 수신, 경로마다의 Agent 수다."""
    status = await request.app.state.llm_gateway.status()
    status["clashing_tenants"] = await request.app.state.tenancy.tenants_on_namespace(
        status["namespace"]
    )
    status["agents"] = await request.app.state.agent_service.llm.counts()
    return status


class LlmTokenRotateIn(BaseModel):
    # 유출이 의심되면 옛 토큰을 곧바로 회수한다. 새 파드가 준비될 때까지 옛 파드는 부르지 못한다
    immediate: bool = False


class LlmTokenRotateOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    agent_id: str
    immediate: bool


@agents_router.post(
    "/{agent_id}/llm-token/rotate",
    response_model=LlmTokenRotateOut,
    dependencies=[Depends(require_scope("agent:deploy"))],
)
async def rotate_llm_token(
    request: Request, tenant: Tenant, agent_id: str, body: LlmTokenRotateIn | None = None
) -> dict[str, Any]:
    """Agent 의 LLM 게이트웨이 토큰을 새로 내고 파드를 굴린다 (P38 5절). 옛 토큰은 옛
    ReplicaSet 의 파드가 다 없어진 뒤에 회수하고, `immediate` 면 곧바로 회수한다. 게이트웨이
    경로가 아닌 Agent 는 409 llm_token_missing 이다."""
    immediate = body is not None and body.immediate
    doc = await get_agent_or_404(request.app.state.agents, tenant, agent_id)
    service = request.app.state.agent_service
    await service.agent_tokens.llm.rotate(
        doc, immediate=immediate, rerender=service.rotate_svc_token
    )
    return {"agent_id": doc.id, "immediate": immediate}
