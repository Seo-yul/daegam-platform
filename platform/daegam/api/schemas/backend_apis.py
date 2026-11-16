"""Backend API 응답 (api/backend_apis.py 의 _profile_out·_version_out·_upload_out·
_token_out 과 같은 필드)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from daegam.api.schemas import DeleteResponse
from daegam.api.schemas.spec_generator import SpecChange


class ReportSummary(BaseModel):
    """verify 보고서 요약 {ok, summary}."""

    model_config = ConfigDict(extra="allow")

    ok: bool | None = None
    summary: dict[str, Any] | None = None


class BackendAPISummary(BaseModel):
    """목록·단건·PATCH 응답. kind 이하 4개는 버전이 있을 때만 들어간다(없으면 키가 없다)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant: str
    name: str
    display_name: str
    endpoint: str | None
    auth: str
    credential_secret: str | None
    # soap:header 값을 담은 Secret 의 이름뿐이다. 값은 돌려주지 않는다 (T60)
    soap_headers_secret: str | None
    created_at: str
    updated_at: str
    latest_version: int | None
    kind: str | None = None
    source_format: str | None = None
    tool_count: int | None = None
    tool_names: list[str] | None = None


class ToolDetail(BaseModel):
    """Tool 별 원본 메타. 예전 버전 문서에는 없어서 목록이 비어 있을 수 있다."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    summary: str | None = None
    description: str | None = None
    # OpenAPI 메서드(대문자). 변환 스펙에서 읽지 못했으면 null 이다(P39b 14절 4부)
    method: str | None = None
    # FastMCP 입력 스키마의 {점 경로: 형식}. 인자 조건이 고를 인자다(P52 1절). 없으면 null 이다
    arguments: dict[str, str] | None = None


class VersionOrigin(BaseModel):
    """apply-draft 로 만든 버전의 출처. kind 는 refine|minify, job_id 는 refine 일 때만 있다."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    from_version: int
    job_id: str | None = None
    presets: list[str] = []


class BackendAPIVersionOut(BaseModel):
    """버전 메타. 목록에서는 report 가 {ok, summary} 요약, 단건에서는 전체 보고서다.
    origin 은 다듬기로 만든 버전에만 있다(업로드는 null)."""

    model_config = ConfigDict(extra="forbid")

    backend_profile_id: str
    version: int
    kind: str
    source_format: str
    source_ext: str
    source_sha256: str
    output_sha256: str
    s2o_version: str
    tool_count: int
    tool_names: list[str]
    tool_details: list[ToolDetail]
    origin: VersionOrigin | None = None
    created_at: str
    report: dict[str, Any]


class VersionSourceOut(BaseModel):
    """올린 그대로의 원본 문서. source_ext 는 wsdl|yaml|json 같은 확장자다."""

    model_config = ConfigDict(extra="forbid")

    source_ext: str
    source_format: str
    text: str


class MinifyOptions(BaseModel):
    """spec2openapi minify_for_mcp 옵션. enrich 는 errors|examples."""

    model_config = ConfigDict(extra="forbid")

    enrich: list[Literal["errors", "examples"]] = []
    max_description: int | None = Field(default=None, ge=1)
    drop_value_examples: bool = False


class MinifyResponse(BaseModel):
    """`POST /{id}/versions/{n}/minify`. 저장하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    changes: list[SpecChange]
    draft_yaml: str


class ApplyDraftRequest(BaseModel):
    """`POST /{id}/versions/{n}/apply-draft`. job_id(refine 결과) 와 minify(옵션) 중 하나.
    accepted_ids 가 비어 있으면 변경 전부를 채택한다."""

    model_config = ConfigDict(extra="forbid")

    job_id: str | None = None
    minify: MinifyOptions | None = None
    accepted_ids: list[str] = []


class UploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    display_name: str
    endpoint: str | None
    auth: str
    credential_secret: str | None
    version: int
    kind: str
    source_format: str
    tool_count: int
    tool_names: list[str]
    report: ReportSummary


class VerdictItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str
    code: str
    message: str
    count: int
    # message 에 든 값(pipeline/verdict.py VerdictItem.args). 화면은 code 와 이것으로
    # 화면 언어의 문장을 만든다
    args: dict[str, str | list[str]] = {}


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str
    items: list[VerdictItem]


class VerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_format: str | None
    kind: str | None
    mixed: bool
    report: dict[str, Any] | None
    tool_names: list[str]
    tool_details: list[ToolDetail]
    verdict: Verdict


class ConvertResponse(BaseModel):
    """`POST /convert?format=json`. 기본(yaml)은 text/yaml 본문이다."""

    model_config = ConfigDict(extra="forbid")

    yaml: str
    report: dict[str, Any] | None
    verdict: Verdict


class BackendAPIDeleteResponse(DeleteResponse):
    revoked_token_ids: list[str]


class TokenMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str
    backend_profile_id: str
    label: str | None
    scope: list[str]
    created_at: str
    revoked_at: str | None
    active: bool


class IssuedToken(BaseModel):
    """토큰 값은 발급 응답에서 한 번만 노출된다."""

    model_config = ConfigDict(extra="forbid")

    token_id: str
    token: str
    backend_profile_id: str
    label: str | None
    scope: list[str]


class TokenRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str
    revoked: bool
