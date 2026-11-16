"""Spec Generator job 응답 (api/spec_generator.py 의 job_view 와 같은 필드)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from daegam.services.spec_generator.projection import ChangeKind


class SpecJobIteration(BaseModel):
    """반복 하나. batch·batch_total 은 refine 에서 이 반복이 다룬 묶음 번호(1부터)와 묶음 수다."""

    model_config = ConfigDict(extra="forbid")

    n: int
    report_summary: dict[str, Any] | None
    failed_ids: list[str]
    usage: dict[str, int]
    note: str | None
    batch: int | None = None
    batch_total: int | None = None
    finished_at: str


class SpecJobSource(BaseModel):
    """refine 의 원본 버전."""

    model_config = ConfigDict(extra="forbid")

    backend_profile_id: str
    version: int


class SpecJobMCPServerSource(BaseModel):
    """overrides 의 원본 리비전 (D37).

    `SpecJobOut.source` 가 세 모양을 다 받아야 한다. 한쪽만 받으면 오버라이드 job 하나가
    `GET /jobs` 응답 검증에서 떨어져 그 테넌트의 job 목록 전체를 500 으로 만든다."""

    model_config = ConfigDict(extra="forbid")

    mcp_server_id: str
    revision: int


class SpecJobLegacyProfileSource(BaseModel):
    """합치기(D42) 전에 저장된 overrides job 의 원본. TTL 로 사라질 때까지 목록에 보인다."""

    model_config = ConfigDict(extra="forbid")

    mcp_profile_id: str
    revision: int


class SpecJobOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant: str
    state: str
    mode: str
    provider: str | None
    source: SpecJobSource | SpecJobMCPServerSource | SpecJobLegacyProfileSource | None
    presets: list[str]
    instruction: str | None
    hints: dict[str, Any]
    attempt: int
    iterations: list[SpecJobIteration]
    usage_total: dict[str, int]
    error: dict[str, Any] | None
    report: dict[str, Any] | None
    verdict: dict[str, Any] | None
    created_at: str
    updated_at: str
    finished_at: str | None
    expires_at: str | None


class SpecChange(BaseModel):
    """채택 단위 하나. pointer 는 RFC 6901 JSON pointer, kind 는 changed|added|removed 다.
    id 는 pointer·kind 에서 결정적으로 나오므로 apply-draft 의 accepted_ids 로 그대로 보낸다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    pointer: str
    kind: ChangeKind
    before: Any = None
    after: Any = None


class RenamedOperation(BaseModel):
    """operationId 가 바뀐 operation. 응답 키는 `from`·`to` 다 (from 은 예약어라 필드는 from_)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class SpecJobDetail(SpecJobOut):
    """`GET /spec-generator/jobs/{id}` 에만 draft(생성 중인 OpenAPI 초안, JSON) 가 붙는다.
    refine 이 done 이면 changes(원본 대비 채택 단위)·draft_yaml·renamed 도 붙는다."""

    draft: Any = None
    changes: list[SpecChange] | None = None
    draft_yaml: str | None = None
    renamed: list[RenamedOperation] | None = None
