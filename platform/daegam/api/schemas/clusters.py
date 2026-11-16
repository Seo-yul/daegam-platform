"""Cluster 응답. 문서(store Cluster) + home + warnings.

요청 본문과도 쓰는 ClusterImages 등에는 REQUIRED_DEFAULTS 를 붙이지 않는다 (schemas/__init__ 2번).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS, DeleteResponse
from daegam.services.sandbox_gate import GateReason
from daegam.store.clusters import (
    Cluster,
    ClusterConnection,
    ClusterGates,
    ClusterSandbox,
    ClusterStatus,
    Preflight,
    RbacCheck,
    SandboxCheck,
    SandboxCheckRun,
    SandboxScheduling,
    SecretRightsCheck,
    StrictApplyCheck,
)


class RbacCheckOut(RbacCheck):
    model_config = REQUIRED_DEFAULTS


class SecretRightsCheckOut(SecretRightsCheck):
    """unbound_namespaces 는 cluster:write 에게만 차 있고 그 밖에는 빈 목록이다(`api/clusters.py`
    `_out`). 수는 unbound 다."""

    model_config = REQUIRED_DEFAULTS


class PreflightOut(Preflight):
    model_config = REQUIRED_DEFAULTS

    rbac: list[RbacCheckOut]
    secret_rights: SecretRightsCheckOut | None = None


class StrictApplyCheckOut(StrictApplyCheck):
    """failures 는 cluster:write 에게만 차 있고 그 밖에는 빈 목록이다(`api/clusters.py` `_out`)."""

    model_config = REQUIRED_DEFAULTS


class ClusterGatesOut(ClusterGates):
    model_config = REQUIRED_DEFAULTS

    strict_apply: StrictApplyCheckOut | None = None


class ClusterStatusOut(ClusterStatus):
    model_config = REQUIRED_DEFAULTS

    preflight: PreflightOut | None = None


class ClusterConnectionOut(ClusterConnection):
    model_config = REQUIRED_DEFAULTS


class SandboxSchedulingOut(SandboxScheduling):
    model_config = REQUIRED_DEFAULTS


class SandboxCheckOut(SandboxCheck):
    model_config = REQUIRED_DEFAULTS


class SandboxCheckRunOut(SandboxCheckRun):
    model_config = REQUIRED_DEFAULTS


class ClusterSandboxOut(ClusterSandbox):
    """샌드박스 이미지·배치·격리 판정과 점검의 마지막 실행 (P40 8절·10절). void_reason 은 판정이
    무효로 읽히는 까닭이고 읽을 때마다 정한다(`verdict_void_reason`). override 는 개발용 우회가 이
    클러스터에 드는가다. 게이트와 지금의 용량은 `GET /clusters/{id}/sandbox` 가 낸다."""

    model_config = REQUIRED_DEFAULTS

    scheduling: SandboxSchedulingOut
    check: SandboxCheckOut
    run: SandboxCheckRunOut
    void_reason: str | None
    override: bool


class ClusterOut(Cluster):
    model_config = REQUIRED_DEFAULTS

    connection: ClusterConnectionOut
    gates: ClusterGatesOut
    status: ClusterStatusOut
    sandbox: ClusterSandboxOut
    home: bool
    warnings: list[str]
    # 환경(DAEGAM_HOME_*)이 기동마다 덮어쓰는 필드 경로(home 만, D54). 화면은 이 필드를 읽기
    # 전용으로 보이고 PATCH 는 다른 값으로 바꾸면 409 env_managed 다
    env_managed: list[str]


class SandboxGateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: bool
    # 닫힌 까닭. 열렸으면 null 이다 (8절)
    reason: GateReason | None


class SandboxNodeOut(BaseModel):
    """들어갈 노드 하나 (7절). 바이트 수다. cap 은 이 노드에 열 수 있는 샌드박스 수이고 used 는 지금
    잡힌 슬롯 수다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    allocatable: int
    others: int
    cap: int
    used: int


class ClusterSandboxStatus(BaseModel):
    """`GET /clusters/{id}/sandbox`. 물을 때 센 게이트와 용량이다 (7절·8절). assumed 는 메모리 한도
    없이 요청이나 LimitRange 기본값으로 센 파드의 수이고, 그 이름(assumed_pods)은 여러 테넌트의
    것이라 cluster:write 에게만 싣는다. shared_nodes 는 마지막 판정이나 스윕이 적은 값이다."""

    model_config = ConfigDict(extra="forbid")

    gate: SandboxGateOut
    capacity: int
    nodes: list[SandboxNodeOut]
    assumed: int
    assumed_pods: list[str]
    shared_nodes: bool | None
    void_reason: str | None
    override: bool
    check_running: bool
    open_sandboxes: int
    run: SandboxCheckRunOut


class SandboxCheckStarted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started: bool


class ClusterDeleteResponse(DeleteResponse):
    secret_name: str


class ClusterPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    preflight: PreflightOut
    warnings: list[str]


class ExposureDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None  # `{name}.<tenant>.<domain>`. 도메인이 없으면 null
    ingress_class: str | None
    tls_mode: str
    issuer: str | None


class ExposureOptions(BaseModel):
    """`GET /clusters/{id}/exposure-options?namespace=`. Secret 은 이름만 나간다 (D22)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    namespaces: list[str]  # 첫 항목이 테넌트 기본 네임스페이스
    namespace: str
    ingress_classes: list[str]
    tls_secrets: list[str]
    cluster_issuers: list[str]
    defaults: ExposureDefaults
    allow_unauthenticated: bool
