"""MCP Server 응답 (D42). 문서(store MCPServer)의 정의 쪽을 현재 리비전으로 펼치고 배포 쪽에
status(services.mcp_servers.status) 를 붙인 것이다. 인스턴스 셋이 함께 쓰는 상태·매니페스트 모델도
여기 있다."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from daegam.api.schemas import REQUIRED_DEFAULTS, DeleteResponse, RulesNotice
from daegam.api.schemas.hub import HubItemRef
from daegam.services.exposure import ExposureInput
from daegam.store.exposure import Exposure
from daegam.store.mcp_servers import (
    InstanceK8s,
    MCPServerRevision,
    MCPServerState,
    ProfileRuntimeSpec,
    ToolOverride,
)
from daegam.store.tenants import Binding
from daegam.store.tool_rules import ServerToolRule


class InstanceK8sOut(InstanceK8s):
    model_config = REQUIRED_DEFAULTS


class InstanceCondition(BaseModel):
    """CR·Deployment condition 하나. last_transition 은 이 조건이 지금 값으로 바뀐 시각이고
    화면의 "떠 있은 지" 가 Ready 조건의 그것에서 나온다 (D44 셋째). 오퍼레이터가 안 채우면
    None 이다."""

    model_config = ConfigDict(extra="forbid")

    type: str | None
    status: str | None
    reason: str | None
    message: str | None
    last_transition: str | None


class ExposureStatus(BaseModel):
    """노출 상태 (docs/guide/exposure.md 6절). 앞단 phase 는 MCP Server 에만 있다 (Gateway 는
    vMCP phase 가 곧 앞단)."""

    model_config = ConfigDict(extra="forbid")

    front_phase: str | None
    address_assigned: bool | None
    tls_secret_present: bool | None
    certificate_ready: bool | None


class InstanceStatus(BaseModel):
    """CR status 매핑. generation·observed_generation 은 CR 이 있을 때만 들어간다."""

    model_config = ConfigDict(extra="forbid")

    phase: str | None
    url: str | None
    external_url: str | None = None  # 노출했을 때 <scheme>://<host>/mcp
    exposure: ExposureStatus | None = None
    message: str | None
    conditions: list[InstanceCondition]
    ready_replicas: int | None
    warnings: list[str] = []
    observed_at: str
    # 배포 쪽을 마지막으로 쓴 시각. 화면이 여기서부터 흐른 시간을 재 진행 배너를 바꾼다 (D43 둘째)
    since: str | None = None
    error: str | None
    generation: int | None = None
    observed_generation: int | None = None


class ExposureOut(Exposure):
    model_config = REQUIRED_DEFAULTS


class BackendSelectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_profile_id: str
    spec_version: int
    tools: list[str]
    tool_overrides: dict[str, ToolOverride]
    timeout_seconds: int
    # operationId 마다의 Tool 규칙 (P52 1절). 규칙이 없으면 빈다
    tool_rules: dict[str, ServerToolRule] = Field(default_factory=dict)


class MCPServerOut(BaseModel):
    """정의 쪽은 현재 리비전을 펼친 것이고 revisions 는 번호 목록이다. 배포 쪽은 `draft` 에서
    비어 있고 status 도 None 이다. `revision` 과 `deployed_revision` 의 차이가 "r5 저장됨,
    r2 배포됨" 이다."""

    model_config = REQUIRED_DEFAULTS

    id: str
    tenant: str
    name: str
    display_name: str
    state: MCPServerState
    revision: int
    deployed_revision: int | None
    backends: list[BackendSelectionOut]
    runtime: ProfileRuntimeSpec
    bindings: list[Binding]
    revisions: list[int]
    cluster_id: str | None
    k8s: InstanceK8sOut | None
    exposure: ExposureOut
    effective_config: dict[str, Any] | None
    spec_token_id: str | None
    spec_token_ids: list[str]
    k8s_uid: str | None
    svc_token_rev: str
    overrides: dict[str, Any]
    last_error: str | None
    last_spec_change: str
    status: InstanceStatus | None
    hub_item: HubItemRef | None  # 허브에 게시돼 있으면 그 항목 (docs/guide/hub-gateway.md 6절)
    created_at: str
    updated_at: str
    updated_by: str | None


class MCPServerDeployed(MCPServerOut):
    """deploy 응답에는 클러스터 preflight 경고(network_policy_unenforced 등)가 붙는다. 재배포와
    Basic 편집의 응답에는 그 배포가 새로 낸 경고(사용자 이미지의 Redis 바인딩이 TLS 로 바뀐
    `redis_tls_binding`, P61)가 붙는다."""

    warnings: list[str]


class MCPServerSaved(MCPServerOut):
    """PUT 의 답. 정의에 더해 규칙 저장의 알림이 붙는다."""

    rules_notice: RulesNotice


class MCPServerRevisionOut(MCPServerRevision):
    model_config = REQUIRED_DEFAULTS


class MCPServerDeleteResponse(DeleteResponse):
    """삭제 응답. draft 였으면 k8s 는 None 이다."""

    state_before: MCPServerState
    revoked_token_ids: list[str]
    k8s: InstanceK8sOut | None


class DeployRequest(BaseModel):
    """`POST /mcp-servers/{id}/deploy`. 정의는 문서에 있으므로 클러스터 쪽만 받는다. exposure 를
    보내지 않으면 문서에 남은 설정(복제가 받은 것)을 쓴다."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str | None = None
    namespace: str | None = None  # 배포 네임스페이스 (허용 목록 안). 기본은 테넌트 기본 (D22)
    exposure: ExposureInput | None = None  # 외부 노출 (docs/guide/exposure.md 2절)


class CloneRequest(BaseModel):
    """`POST /mcp-servers/{id}/clone`. 정의를 베낀 draft 를 만든다. exposure 를 주면 배포 설정으로
    남아 노출용 둘째가 한 호출로 만들어진다 (D42)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    exposure: ExposureInput | None = None


class ApplyDraftRequest(BaseModel):
    """`POST /mcp-servers/{id}/apply-draft`: overrides job 의 제안을 채택한다 (D37).

    보내는 것은 채택할 변경의 id 뿐이다. changes 는 서버가 원본과 초안으로 다시 계산하므로
    모르는 id 는 422 unknown_change 이고 고른 것이 없으면 422 no_changes 다.
    accepted_ids 가 비어 있으면 변경 전부를 채택한다."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    accepted_ids: list[str] = Field(default_factory=list)


class PreviewResponse(BaseModel):
    """`POST /mcp-servers/{id}/revisions/{n}/preview`: apply 없이 렌더링한 MCPServer CR."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    namespace: str
    effective_config: dict[str, Any]
    manifest: dict[str, Any]
    locked: list[str]


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    name: str
    namespace: str
    rendered: dict[str, Any]
    overrides: dict[str, Any]
    final: dict[str, Any]
    live: dict[str, Any] | None
    drift: list[str] | None
    drift_patch: dict[str, Any] | None
    drift_managers: list[str]
    locked: list[str]
    ignored_by_operator: list[str]
    error: str | None


class ManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[ManifestDocument]


class OverridesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, Any]
    final: dict[str, Any]
    diff: list[str]
    validation: str
    note: str


class DriftAbsorbResponse(OverridesResponse):
    """drift/absorb 에 드리프트가 있었을 때: overrides 저장 결과 + 갱신된 manifest 문서."""

    documents: list[ManifestDocument]
