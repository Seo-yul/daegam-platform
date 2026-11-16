"""Backend API: 스펙 업로드·버전 조회·접속 정보 수정·삭제와 읽기 토큰 API.

버전 다듬기(D25): `POST /{id}/versions/{n}/minify` 는 규칙(spec2openapi minify)으로 만든 초안의
changes 를 저장 없이 돌려주고, `POST /{id}/versions/{n}/apply-draft` 는 refine job 또는 minify 의
changes 중 채택된 pointer 만 원본 위에 심어 표면 보호·verify 를 거친 뒤 v(n+1) 을 만든다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from spec2openapi import dump_spec
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from daegam import yamlsafe
from daegam.api._common import (
    error,
    not_found,
    not_null,
    or_404,
    patched_fields,
    patched_values,
    read_capped,
    references_out,
)
from daegam.api.deps import (
    BackendAPIs,
    Files,
    Settings_,
    Tenant,
    User,
    authenticate_api_token,
    bearer_token,
    require_scope,
)
from daegam.api.schemas import ERROR_RESPONSES, too_large_response
from daegam.api.schemas.backend_apis import (
    ApplyDraftRequest,
    BackendAPIDeleteResponse,
    BackendAPISummary,
    BackendAPIVersionOut,
    ConvertResponse,
    IssuedToken,
    MinifyOptions,
    MinifyResponse,
    TokenMeta,
    TokenRevokeResponse,
    UploadResponse,
    VerifyResponse,
    VersionSourceOut,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.config import Settings
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.pipeline.verdict import verdict_of
from daegam.services import backend_apis as bp
from daegam.services.spec_generator import minify as minify_svc
from daegam.services.spec_generator import projection, refine, surface
from daegam.store.backend_apis import (
    BackendAPI,
    BackendAPIToken,
    BackendAPIVersion,
    VersionOrigin,
)
from daegam.store.base import iso_utc, issue_token

logger = logging.getLogger("daegam.backend_apis")

router = APIRouter(responses=ERROR_RESPONSES, prefix="/backend-apis", tags=["backend-apis"])


def get_backend_api_service(request: Request) -> bp.BackendAPIService:
    return request.app.state.backend_api_service


BackendAPISvc = Annotated[bp.BackendAPIService, Depends(get_backend_api_service)]

# -- helpers ---------------------------------------------------------------


async def _get_or_404(store: BackendAPIs, tenant: str, api_id: str) -> BackendAPI:
    return or_404(await store.get(api_id, tenant=tenant), "backend api", api_id)


async def _version_or_404(
    store: BackendAPIs, tenant: str, api_id: str, version: int
) -> tuple[BackendAPI, BackendAPIVersion]:
    profile = await _get_or_404(store, tenant, api_id)
    v = await store.get_version(profile.id, version)
    if v is None:
        raise not_found(f"version {version} not found")
    return profile, v


def _profile_out(p: BackendAPI, latest: BackendAPIVersion | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": p.id,
        "tenant": p.tenant,
        "name": p.name,
        "display_name": p.display_name,
        "endpoint": p.endpoint,
        "auth": p.auth,
        "credential_secret": p.credential_secret,
        "soap_headers_secret": p.soap_headers_secret,
        "created_at": iso_utc(p.created_at),
        "updated_at": iso_utc(p.updated_at),
        "latest_version": latest.version if latest else None,
    }
    if latest:
        body.update(
            kind=latest.kind,
            source_format=latest.source_format,
            tool_count=latest.tool_count,
            tool_names=latest.tool_names,
        )
    return body


def _version_out(v: BackendAPIVersion, *, full_report: bool) -> dict[str, Any]:
    """버전 문서 그대로다. 목록·생성 응답만 report 를 {ok, summary} 로 줄인다."""
    body = v.model_dump(mode="json")
    if not full_report:
        body["report"] = {"ok": v.report.get("ok"), "summary": v.report.get("summary")}
    return body


def _upload_out(p: BackendAPI, v: BackendAPIVersion) -> dict[str, Any]:
    report = v.report
    return {
        "id": p.id,
        "name": p.name,
        "display_name": p.display_name,
        "endpoint": p.endpoint,
        "auth": p.auth,
        "credential_secret": p.credential_secret,
        "version": v.version,
        "kind": v.kind,
        "source_format": v.source_format,
        "tool_count": v.tool_count,
        "tool_names": v.tool_names,
        "report": {"ok": report.get("ok"), "summary": report.get("summary")},
    }


# -- pipeline (verify / convert / upload 이 공유) -----------------------------


async def _read_spec_input(request: Request, settings: Settings) -> tuple[bytes, str | None]:
    """verify/convert 의 입력. multipart `file` 또는 json `{text, filename?}`."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = form.get("file")
        if not isinstance(f, StarletteUploadFile):
            raise error(422, "invalid_input", "multipart form must carry a 'file' part")
        content = await f.read(settings.max_upload_bytes + 1)
        return content, f.filename
    if "application/json" in ctype:
        try:
            body = await request.json()
        except ValueError as e:
            raise error(422, "invalid_input", "request body is not valid JSON") from e
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise error(422, "invalid_input", "json body must be {text: str, filename?: str}")
        filename = body.get("filename")
        if filename is not None and not isinstance(filename, str):
            raise error(422, "invalid_input", "filename must be a string")
        return body["text"].encode("utf-8"), filename or None
    raise error(
        422,
        "invalid_input",
        "send multipart/form-data with a 'file' part or application/json with 'text'",
    )


@router.post(
    "/verify",
    response_model=VerifyResponse,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def verify_spec(_tenant: Tenant, settings: Settings_, request: Request) -> dict[str, Any]:
    """저장하지 않고 감지·변환·verify 만 돌려 판정을 준다. 깨진 입력도 200 에 verdict error 다."""
    content, filename = await _read_spec_input(request, settings)
    insp = await bp.inspect_spec(content, filename, settings)
    verdict = verdict_of(insp)
    return {
        "source_format": insp.source_format,
        "kind": insp.kind,
        "mixed": insp.mixed,
        "report": insp.report,
        "tool_names": insp.tool_names,
        "tool_details": insp.tool_details,
        "verdict": verdict.to_dict(),
    }


@router.post(
    "/convert",
    response_model=ConvertResponse,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def convert_spec(
    _tenant: Tenant, settings: Settings_, request: Request, format: str = "yaml"
) -> Response:
    """변환된 OpenAPI 3.x 를 text/yaml 로 돌려준다 (헤더 X-Daegam-Verdict).
    `?format=json` 이면 {yaml, report, verdict}. 판정이 error 면 422 verdict_error."""
    if format not in ("yaml", "json"):
        raise error(422, "invalid_format", "format must be yaml or json")
    content, filename = await _read_spec_input(request, settings)
    insp = await bp.inspect_spec(content, filename, settings)
    verdict = verdict_of(insp)
    if verdict.level == "error":
        raise error(
            422, "verdict_error", "spec did not pass verification", verdict=verdict.to_dict()
        )
    assert insp.spec is not None
    yaml_text = dump_spec(insp.spec, fmt="yaml")
    headers = {"X-Daegam-Verdict": verdict.level}
    if format == "json":
        return JSONResponse(
            content={"yaml": yaml_text, "report": insp.report, "verdict": verdict.to_dict()},
            headers=headers,
        )
    return Response(content=yaml_text, media_type="text/yaml; charset=utf-8", headers=headers)


# -- profiles ------------------------------------------------------------------


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("backend:write"))],
    responses=too_large_response("스펙이 업로드 상한(max_upload_bytes)을 넘는다"),
)
async def upload_spec(
    tenant: Tenant,
    user: User,
    service: BackendAPISvc,
    settings: Settings_,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()],
    display_name: Annotated[str | None, Form()] = None,
    endpoint: Annotated[str | None, Form()] = None,
    auth: Annotated[str | None, Form()] = None,
    credential_secret: Annotated[str | None, Form()] = None,
    soap_headers_secret: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """스펙을 올려 새 버전을 만든다. 프로필이 없으면 접속 정보와 함께 만들고,
    있으면 명시된 접속 정보 필드만 덮어쓴다. display_name 은 없으면 name 이다 (D24)."""
    name = validate_name(name)
    display_v = validate_display_name(display_name, default=name)
    # 접속 정보는 본문을 읽기 전에 검사한다. 둘 다 틀리면 예전처럼 422 가 413 보다 먼저다
    connection = bp.UploadConnection.of(endpoint, auth, credential_secret, soap_headers_secret)
    up = bp.BackendAPIUpload(
        name=name,
        display_name=display_v,
        display_name_sent=display_name is not None,
        connection=connection,
        content=await read_capped(file, settings.max_upload_bytes),
        filename=file.filename,
    )
    profile, version, created = await service.upload(tenant, up, user_sub=user.sub)
    return JSONResponse(status_code=201 if created else 200, content=_upload_out(profile, version))


@router.get(
    "",
    response_model=list[BackendAPISummary],
    response_model_exclude_unset=True,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def list_backend_apis(tenant: Tenant, store: BackendAPIs) -> list[dict[str, Any]]:
    profiles = await store.list(tenant)
    latest = await store.latest_versions([p.id for p in profiles])
    return [_profile_out(p, latest.get(p.id)) for p in profiles]


@router.get(
    "/{api_id}",
    response_model=BackendAPISummary,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def get_backend_api(tenant: Tenant, store: BackendAPIs, api_id: str) -> dict[str, Any]:
    profile = await _get_or_404(store, tenant, api_id)
    return _profile_out(profile, await store.latest_version(profile.id))


class ProfilePatch(BaseModel):
    """PATCH 본문. 보낸 필드만 바뀐다. endpoint·credential_secret은 null로 지울 수 있다.
    name 은 저장값과 같아야 한다 (422 name_immutable, D24)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    display_name: str | None = None
    endpoint: str | None = None
    auth: str | None = None
    credential_secret: str | None = None
    soap_headers_secret: str | None = None


@router.patch(
    "/{api_id}",
    response_model=BackendAPISummary,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def patch_backend_api(
    tenant: Tenant,
    store: BackendAPIs,
    service: BackendAPISvc,
    api_id: str,
    patch: ProfilePatch,
    user: User,
) -> dict[str, Any]:
    profile = await _get_or_404(store, tenant, api_id)
    sent = patched_fields(patch)
    if "name" in sent:
        # name 은 생성 뒤 바꿀 수 없다 (D24). 같은 값이면 통과, 다르면 422 name_immutable
        check_name_unchanged(not_null(patch.name, "name", code="invalid_name"), profile.name)
    changes = patched_values(patch, sent)
    if "display_name" in sent:
        changes["display_name"] = validate_display_name(
            not_null(patch.display_name, "display_name", code="invalid_display_name"),
            default=profile.name,
        )
    profile = await service.update(profile, changes, user_sub=user.sub)
    return _profile_out(profile, await store.latest_version(profile.id))


@router.get(
    "/{api_id}/versions",
    response_model=list[BackendAPIVersionOut],
    dependencies=[Depends(require_scope("backend:read"))],
)
async def list_versions(tenant: Tenant, store: BackendAPIs, api_id: str) -> list[dict[str, Any]]:
    profile = await _get_or_404(store, tenant, api_id)
    return [_version_out(v, full_report=False) for v in await store.list_versions(profile.id)]


@router.get(
    "/{api_id}/versions/{version}",
    response_model=BackendAPIVersionOut,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def get_version(
    tenant: Tenant, store: BackendAPIs, api_id: str, version: int
) -> dict[str, Any]:
    _, v = await _version_or_404(store, tenant, api_id, version)
    return _version_out(v, full_report=True)


@router.get(
    "/{api_id}/references",
    response_model=ReferencesResponse,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def backend_api_references(
    tenant: Tenant, store: BackendAPIs, api_id: str, request: Request
) -> dict[str, Any]:
    """이 Backend API 의 참조 (D43). MCP Server 와 살아 있는 읽기 토큰이다."""
    profile = await _get_or_404(store, tenant, api_id)
    return await references_out(request, "backend-api", profile.id, tenant=tenant)


@router.delete(
    "/{api_id}",
    response_model=BackendAPIDeleteResponse,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def delete_backend_api(
    tenant: Tenant,
    store: BackendAPIs,
    service: BackendAPISvc,
    api_id: str,
    force: bool = False,
) -> dict[str, Any]:
    profile = await _get_or_404(store, tenant, api_id)
    revoked = await service.delete(profile, force=force)
    return {"id": profile.id, "deleted": True, "revoked_token_ids": revoked}


# -- tokens ----------------------------------------------------------------


async def _read_token_request(request: Request) -> tuple[str | None, list[str]]:
    """토큰 발급 본문(json 또는 form)에서 label 과 scope 를 읽는다. scope 는 form 이면 쉼표 구분."""
    ctype = request.headers.get("content-type", "")
    label: Any = None
    scope: Any = None
    if "application/json" in ctype:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            label = body.get("label")
            scope = body.get("scope")
    elif "form" in ctype:
        form = await request.form()
        label = form.get("label")
        raw = form.get("scope")
        scope = [s for s in str(raw).split(",")] if raw else None
    if label is not None:
        label = str(label).strip() or None
    scopes: list[str] = []
    if scope is not None:
        if isinstance(scope, str):
            scope = [scope]
        if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
            raise error(422, "invalid_scope", "scope must be a list of strings")
        for s in scope:
            s = s.strip()
            if not s:
                continue
            if not re.fullmatch(r"^[a-z][a-z0-9-]*:[A-Za-z0-9._:-]+$", s):
                raise error(422, "invalid_scope", f"bad scope {s!r} (expected kind:id)")
            if s not in scopes:
                scopes.append(s)
    return label, scopes


def _token_out(t: BackendAPIToken) -> dict[str, Any]:
    return {
        "token_id": t.id,
        "backend_profile_id": t.backend_profile_id,
        "label": t.label,
        "scope": t.scope,
        "created_at": iso_utc(t.created_at),
        "revoked_at": iso_utc(t.revoked_at) if t.revoked_at else None,
        "active": t.active,
    }


@router.post(
    "/{api_id}/tokens",
    response_model=IssuedToken,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def create_token(
    tenant: Tenant, store: BackendAPIs, api_id: str, request: Request
) -> dict[str, Any]:
    profile = await _get_or_404(store, tenant, api_id)
    label, scope = await _read_token_request(request)
    token, digest = issue_token()
    tok = await store.create_token(profile.id, digest, label, scope)
    # token은 여기서 한 번만 노출된다. DB에는 sha256만 남는다.
    return {
        "token_id": tok.id,
        "token": token,
        "backend_profile_id": profile.id,
        "label": tok.label,
        "scope": tok.scope,
    }


@router.get(
    "/{api_id}/tokens",
    response_model=list[TokenMeta],
    dependencies=[Depends(require_scope("backend:read"))],
)
async def list_tokens(tenant: Tenant, store: BackendAPIs, api_id: str) -> list[dict[str, Any]]:
    profile = await _get_or_404(store, tenant, api_id)
    return [_token_out(t) for t in await store.list_tokens(profile.id)]


@router.delete(
    "/{api_id}/tokens/{token_id}",
    response_model=TokenRevokeResponse,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def revoke_token(
    tenant: Tenant, store: BackendAPIs, api_id: str, token_id: str
) -> dict[str, Any]:
    profile = await _get_or_404(store, tenant, api_id)
    if not await store.revoke_token(profile.id, token_id):
        raise not_found(f"token {token_id} not found")
    return {"token_id": token_id, "revoked": True}


@router.get("/{api_id}/versions/{version}/openapi.yaml")
async def read_openapi_yaml(
    store: BackendAPIs, files: Files, api_id: str, version: int, request: Request
) -> Response:
    """initContainer용. 테넌트 헤더를 요구하지 않고 Bearer 토큰만 본다."""
    presented = bearer_token(request)
    await authenticate_api_token(store, api_id, presented)
    v = await store.get_version(api_id, version)
    if v is None:
        raise not_found(f"version {version} not found")
    body = await bp.verified_spec_body(files, v)
    return Response(content=body, media_type="text/yaml; charset=utf-8")


@router.get(
    "/{api_id}/versions/{version}/spec",
    response_model=dict[str, Any],
    dependencies=[Depends(require_scope("backend:read"))],
)
async def read_spec(
    tenant: Tenant, store: BackendAPIs, files: Files, api_id: str, version: int
) -> dict[str, Any]:
    """UI 문서 뷰어용. 변환된 OpenAPI 3.x 문서를 JSON 으로 돌려준다.
    인증은 다른 GET 과 같다(사용자 스코프 + 테넌트). initContainer 는 openapi.yaml 을 쓴다."""
    profile, v = await _version_or_404(store, tenant, api_id, version)
    body = await bp.verified_spec_body(files, v)
    doc = yamlsafe.safe_load(body)
    if not isinstance(doc, dict):
        logger.error("spec body is not a mapping: profile=%s version=%s", profile.id, v.version)
        raise error(500, "integrity_error", f"version {v.version} body is not a document")
    return doc


@router.get(
    "/{api_id}/versions/{version}/source",
    response_model=VersionSourceOut,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def read_source(
    tenant: Tenant, store: BackendAPIs, files: Files, api_id: str, version: int
) -> dict[str, Any]:
    """올린 그대로의 원본 문서. 편집 화면이 "등록된 문서" 로 보여 준다.

    `/spec` 은 변환된 OpenAPI 라 WSDL 로 올린 백엔드에서는 사람이 올린 것과 다른 문서가 나온다.
    무엇이 등록돼 있는지 묻는 자리에는 변환물이 아니라 원본이 맞다.
    """
    profile, v = await _version_or_404(store, tenant, api_id, version)
    # 변환본을 읽는 `/spec` 의 `verified_spec_body` 와 같은 검사다
    body = await bp.verified_source_body(files, v)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        # 스펙은 텍스트다. 못 읽는 바이트가 나오면 저장이 깨진 것이라 조용히 넘기지 않는다
        logger.error("source body is not utf-8: profile=%s version=%s", profile.id, v.version)
        raise error(
            500, "integrity_error", f"version {v.version} source body is not utf-8"
        ) from None
    return {"source_ext": v.source_ext, "source_format": v.source_format, "text": text}


# -- 다듬기 (D25) ---------------------------------------------------------------------


def _minify_opts(o: MinifyOptions) -> minify_svc.MinifyOptions:
    """요청 스키마를 서비스 옵션으로. 스키마를 아는 쪽이 라우터라 여기 남는다."""
    return minify_svc.MinifyOptions(
        enrich=tuple(o.enrich),
        max_description=o.max_description,
        drop_value_examples=o.drop_value_examples,
    )


@router.post(
    "/{api_id}/versions/{version}/minify",
    response_model=MinifyResponse,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def minify_version(
    tenant: Tenant,
    store: BackendAPIs,
    files: Files,
    api_id: str,
    version: int,
    body: MinifyOptions,
) -> dict[str, Any]:
    """규칙(spec2openapi minify_for_mcp)으로 만든 초안의 changes 와 YAML 을 즉시 돌려준다. 저장하지
    않는다. 저장은 apply-draft 에 같은 옵션과 채택할 id 를 보내서 한다."""
    _profile, v = await _version_or_404(store, tenant, api_id, version)
    original = await bp.original_spec(files, v)
    draft, changes = await run_in_threadpool(bp.run_minify, original, _minify_opts(body))
    return {
        "changes": [c.to_dict() for c in changes],
        "draft_yaml": dump_spec(draft, fmt="yaml"),
    }


async def _refine_changes(
    request: Request,
    tenant: str,
    profile: BackendAPI,
    version: int,
    original: dict[str, Any],
    job_id: str,
) -> tuple[list[projection.Change], surface.Rules, VersionOrigin]:
    """apply-draft 의 refine 갈래. job 이 이 버전을 다듬은 done 상태의 refine job 인지 보고,
    그 초안의 changes 와 표면 규칙, 새 버전의 출처를 돌려준다."""
    job = await request.app.state.spec_jobs.get(job_id, tenant=tenant)
    if job is None:
        raise error(422, "job_not_found", f"spec job {job_id} not found")
    if job.mode != "refine":
        raise error(422, "job_not_refine", f"spec job {job.id} is mode {job.mode}")
    if job.state != "done" or not job.draft_ref:
        raise error(422, "job_not_done", f"spec job {job.id} is {job.state}")
    if job.source != {"backend_profile_id": profile.id, "version": version}:
        raise error(
            422, "source_mismatch", f"spec job {job.id} refined {job.source}, not v{version}"
        )
    job_files = request.app.state.spec_job_files
    raw_in = await job_files.get(job.input_ref)
    raw_draft = await job_files.get(job.draft_ref)
    if raw_in is None or raw_draft is None:
        raise error(422, "job_not_found", f"spec job {job.id} files are gone")
    if surface.diff(original, json.loads(raw_in.decode("utf-8"))):
        raise error(422, "source_mismatch", f"spec job {job.id} input differs from v{version}")
    draft = json.loads(raw_draft.decode("utf-8"))
    rules = surface.refine_rules(rename=refine.RENAME_PRESET in job.presets)
    origin = VersionOrigin(
        kind="refine", from_version=version, job_id=job.id, presets=list(job.presets)
    )
    return projection.changes(original, draft), rules, origin


@router.post(
    "/{api_id}/versions/{version}/apply-draft",
    response_model=BackendAPIVersionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("backend:write"))],
    responses=too_large_response("다듬은 초안이 업로드 상한(max_upload_bytes)을 넘는다"),
)
async def apply_draft(
    tenant: Tenant,
    store: BackendAPIs,
    files: Files,
    service: BackendAPISvc,
    settings: Settings_,
    request: Request,
    api_id: str,
    version: int,
    body: ApplyDraftRequest,
) -> JSONResponse:
    """refine job(`job_id`) 또는 minify(`minify` 옵션) 의 changes 중 `accepted_ids`(비면 전부) 를
    원본 v{version} 위에 심어 표면 보호·verify 를 거친 뒤 새 버전을 만든다. 결과 본문의 sha256 이
    최신 버전의 source_sha256 과 같으면 200 에 그 버전을 돌려주고 새로 만들지 않는다."""
    if (body.job_id is None) == (body.minify is None):
        raise error(422, "invalid_request", "send exactly one of job_id or minify")
    profile, v = await _version_or_404(store, tenant, api_id, version)
    original = await bp.original_spec(files, v)

    if body.job_id is not None:
        all_changes, rules, origin = await _refine_changes(
            request, tenant, profile, version, original, body.job_id
        )
    else:
        assert body.minify is not None
        _draft, all_changes = await run_in_threadpool(
            bp.run_minify, original, _minify_opts(body.minify)
        )
        rules = surface.ALLOW_MINIFY
        origin = VersionOrigin(kind="minify", from_version=version)

    accepted = bp.select_changes(all_changes, body.accepted_ids)
    if not accepted:
        raise error(422, "no_changes", "nothing to apply")
    result_doc = projection.apply_changes(original, accepted)
    bp.check_surface(original, result_doc, rules)

    content = dump_spec(result_doc, fmt="yaml").encode("utf-8")
    latest = await store.latest_version(profile.id)
    if latest and latest.source_sha256 == hashlib.sha256(content).hexdigest():
        return JSONResponse(status_code=200, content=_version_out(latest, full_report=False))
    result = await bp.inspect_and_accept(content, "draft.yaml", settings)
    new = await service.add_version(
        profile, content, "draft.yaml", result, seen=latest, origin=origin
    )
    return JSONResponse(status_code=201, content=_version_out(new, full_report=False))
