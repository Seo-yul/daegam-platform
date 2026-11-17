"""Gateway 응답 (D42). 문서(store Gateway)의 정의 쪽을 현재 리비전으로 펼치고 배포 쪽에
status(services.gateways.status) 를 붙인 것이다. api/gateways.py 의 _out 과 같은 필드다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from daegam.api.schemas import REQUIRED_DEFAULTS, DeleteResponse, RulesNotice
from daegam.api.schemas.mcp_servers import ExposureOut, ExposureStatus
from daegam.services.exposure import ExposureInput
from daegam.services.gateway_status import BadgeCode
from daegam.store.gateways import (
    GatewayBackend,
    GatewayBackendSnapshot,
    GatewayK8s,
    GatewayRevision,
    GatewayRuntime,
    GatewayState,
    GatewaySurface,
)


class GatewayRuntimeOut(GatewayRuntime):
    model_config = REQUIRED_DEFAULTS


class GatewayK8sOut(GatewayK8s):
    model_config = REQUIRED_DEFAULTS


class GatewaySurfaceOut(GatewaySurface):
    model_config = REQUIRED_DEFAULTS


class GatewayBackendSnapshotOut(GatewayBackendSnapshot):
    """`timeout_seconds` 는 렌더하는 값이라 비지 않는다. 캔버스가 Agent 의 도구 타임아웃과
    견준다 (P50 7절). 게시된 백엔드의 규칙 출처(`published_sources`)는 렌더의 사실이라
    내지 않는다."""

    model_config = REQUIRED_DEFAULTS

    timeout_seconds: int
    published_sources: SkipJsonSchema[Any] = Field(default=None, exclude=True)


class GatewayBackendRow(BaseModel):
    """백엔드별 행: Entry phase(설정 검증) + vMCP discoveredBackends(도달 가능성)."""

    model_config = ConfigDict(extra="forbid")

    workload: str
    hub_item_id: str
    hub_item_name: str
    kind: str
    url: str
    entry_phase: str | None
    status: str | None
    consecutive_failures: int | None
    message: str | None


class GatewayCondition(BaseModel):
    """vMCP condition 하나. 필드는 `InstanceCondition` 과 같다 (화면이 같은 표로 그린다)."""

    model_config = ConfigDict(extra="forbid")

    type: str | None
    status: str | None
    reason: str | None
    message: str | None
    last_transition: str | None


class GatewayStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str | None
    url: str | None
    external_url: str | None = None  # 노출했을 때 <scheme>://<host>/mcp
    exposure: ExposureStatus | None = None
    message: str | None
    badge: str | None = None
    # badge 의 코드. 화면은 이것으로 화면 언어의 말을 그리고, 없으면 badge 를 그대로
    # 보인다 (P49 6절)
    badge_code: BadgeCode | None = None
    failure_mode: str | None = None
    backend_count: int | None
    expected_backends: int | None = None
    backends: list[GatewayBackendRow]
    conditions: list[GatewayCondition]
    warnings: list[str] = []
    observed_at: str
    # 배포 쪽을 마지막으로 쓴 시각. 화면이 여기서부터 흐른 시간을 재 진행 배너를 바꾼다 (D43 둘째)
    since: str | None = None
    error: str | None
    generation: int | None = None
    observed_generation: int | None = None


class GatewayOut(BaseModel):
    """정의 쪽은 현재 리비전을 펼친 것이고 revisions 는 번호 목록이다. auth 는 플랫폼 auth_mode 가
    정하는 읽기 전용 인증 경계(oidc | local | anonymous)다. 배포 쪽은 `draft` 에서 비어 있고
    status 도 None 이다. `revision` 과 `deployed_revision` 의 차이가 "r5 저장됨, r2 배포됨" 이다.
    `deployed_backends`·`deployed_conflict_resolution` 은 배포 시점에 해석한 스냅샷이라 정의 쪽
    `backends`·`conflict_resolution` 과 나란히 산다."""

    model_config = REQUIRED_DEFAULTS

    id: str
    tenant: str
    name: str
    display_name: str
    state: GatewayState
    revision: int
    deployed_revision: int | None
    backends: list[GatewayBackend]
    conflict_resolution: str
    failure_mode: str
    authz_policy: str | None
    runtime: GatewayRuntimeOut
    surface: GatewaySurfaceOut
    # code mode (P56 6.2절). 켠 정의도 판정이 거절하면 렌더하지 않고 상태가 경고한다
    code_mode: bool
    auth: str
    revisions: list[int]
    cluster_id: str | None
    k8s: GatewayK8sOut | None
    deployed_backends: list[GatewayBackendSnapshotOut]
    deployed_conflict_resolution: Literal["priority", "prefix"]
    tool_overrides: dict[str, dict[str, str]]
    exposure: ExposureOut
    k8s_uid: str | None
    svc_token_rev: str
    effective_config: dict[str, Any] | None
    overrides: dict[str, Any]
    last_error: str | None
    last_spec_change: str
    status: GatewayStatus | None
    created_at: str
    updated_at: str
    updated_by: str | None


class ConsumerNotice(BaseModel):
    """게시된 Gateway 의 저장이 소유자 Cedar 에 forbid 를 더해, 다음 배포에서 호출이 닫힐 Gateway
    의 이름 (P57 6.2절)."""

    model_config = ConfigDict(extra="forbid")

    closed: list[str]


class GatewaySaved(GatewayOut):
    """POST/PUT/clone 응답에는 검증 경고(tool_names 미확인, priority 모드의 미상 tool_names 등)가
    붙는다. PUT 에는 규칙 저장의 알림(P52 1절)과, 게시된 Gateway 가 forbid 를 더했으면 닫힐 소비자의
    알림(P57 6.2절)도 붙는다."""

    warnings: list[str]
    rules_notice: RulesNotice | None = None
    consumer_notice: ConsumerNotice | None = None


class GatewayDeployed(GatewayOut):
    """deploy/redeploy/PATCH 응답에는 검증 경고(prefix 폴백 등)가 붙는다."""

    warnings: list[str]


class GatewayRevisionOut(GatewayRevision):
    model_config = REQUIRED_DEFAULTS

    runtime: GatewayRuntimeOut
    surface: GatewaySurfaceOut | None


class GatewayBackendReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hub_item_id: str
    name: str
    workload: str
    phase: str | None
    ready: bool
    error: str | None


class GatewayPreviewResponse(BaseModel):
    """`GET /gateways/{id}/revisions/{n}/preview`: apply 없이 렌더링한 문서 묶음
    (MCPGroup, MCPExternalAuthConfig, MCPServerEntry 래퍼들, VirtualMCPServer)."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    namespace: str
    manifest: dict[str, Any]
    documents: list[dict[str, Any]]
    backends: list[GatewayBackendReport]
    conflict_resolution: str
    tool_overrides: dict[str, dict[str, str]]
    warnings: list[str]
    locked: list[str]


class GatewayDeleteResponse(DeleteResponse):
    """삭제 응답. draft 였으면 k8s 는 None 이다."""

    state_before: GatewayState
    k8s: GatewayK8sOut | None


class DeployRequest(BaseModel):
    """`POST /gateways/{id}/deploy`. 정의는 문서에 있으므로 클러스터 쪽만 받는다. exposure 를
    보내지 않으면 문서에 남은 설정(복제가 받은 것)을 쓴다."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str | None = None
    namespace: str | None = None  # 배포 네임스페이스 (허용 목록 안). 기본은 테넌트 기본 (D22)
    exposure: ExposureInput | None = None  # 외부 노출 (docs/guide/exposure.md 2절)


class CloneRequest(BaseModel):
    """`POST /gateways/{id}/clone`. 정의를 베낀 draft 를 만든다. exposure 를 주면 배포 설정으로
    남아 노출용 둘째가 한 호출로 만들어진다 (D42)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    exposure: ExposureInput | None = None
