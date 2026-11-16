"""Spec Generator job API: 넣으면 202, 워커가 처리, GET 으로 진행·결과를 본다.

mode 가 generate 면 input(text·url·examples·partial_spec) 에서 문서를 만들고, refine(D25) 이면
source(Backend API 버전) 의 원본을 서버가 GridFS 에서 읽어 input.json 으로 넣고 프리셋·지시로
설명 필드를 다듬는다. refine 의 결과는 GET 에서 changes(채택 단위)·draft_yaml·renamed 로 본다.

overrides(D37) 는 대상이 문서가 아니라 MCP Server 리비전의 `backends` 조각이다. input.json 이
`{spec, versions}` 이고 결과는 GET 에서 changes 만이다. source 의 모양이 모드를 가르므로
`JobSource`(refine)와 `MCPServerSource`(overrides)를 isinstance 로 구분한다. 응답 스키마도 두 모양을
다 받아야 한다. 한쪽만 받으면 오버라이드 job 하나가 job 목록 전체를 500 으로 만든다.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from daegam import urlfetch
from daegam.api._common import error, not_found, require_secret_store
from daegam.api.deps import BackendAPIs, Files, Settings_, Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES, DeleteResponse
from daegam.api.schemas.spec_generator import SpecJobDetail, SpecJobOut
from daegam.config import Settings
from daegam.pipeline.verdict import compute_verdict
from daegam.services.backend_apis import original_spec
from daegam.services.llm_providers import ProviderError, resolve_api_key
from daegam.services.spec_generator import overrides, refine
from daegam.services.spec_generator.refine import Preset
from daegam.store.base import iso_utc
from daegam.store.settings import LLMProvider
from daegam.store.spec_jobs import MongoSpecJobStore, SpecJob, SpecJobFiles, input_name

router = APIRouter(responses=ERROR_RESPONSES, prefix="/spec-generator", tags=["spec-generator"])


def get_jobs(request: Request) -> MongoSpecJobStore:
    return request.app.state.spec_jobs


def get_job_files(request: Request) -> SpecJobFiles:
    return request.app.state.spec_job_files


Jobs = Annotated[MongoSpecJobStore, Depends(get_jobs)]
JobFiles = Annotated[SpecJobFiles, Depends(get_job_files)]


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Any
    response: Any


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    url: str | None = None
    examples: list[Example] | None = None
    partial_spec: dict[str, Any] | str | None = None

    def is_empty(self) -> bool:
        return not (
            (self.text and self.text.strip())
            or (self.url and self.url.strip())
            or self.examples
            or self.partial_spec
        )


class Hints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    base_url: str | None = None
    style: str | None = None


class JobSource(BaseModel):
    """refine 의 원본: Backend API 버전."""

    model_config = ConfigDict(extra="forbid")

    backend_profile_id: str
    version: int = Field(ge=1)


class MCPServerSource(BaseModel):
    """overrides 의 원본: MCP Server 리비전 (D37)."""

    model_config = ConfigDict(extra="forbid")

    mcp_server_id: str
    revision: int = Field(ge=1)


class JobRequest(BaseModel):
    """generate 는 input 이 필요하고, refine 과 overrides 는 source 와 presets 또는 instruction 이
    필요하다. source 의 모양이 모드를 가른다."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["generate", "refine", "overrides"] = "generate"
    input: JobInput | None = None
    hints: Hints | None = None
    provider: str | None = None
    source: JobSource | MCPServerSource | None = None
    presets: list[Preset] = []
    instruction: str | None = Field(default=None, max_length=refine.INSTRUCTION_MAX_CHARS)


def job_view(
    job: SpecJob, *, draft: Any = None, include_draft: bool = False, **extras: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": job.id,
        "tenant": job.tenant,
        "state": job.state,
        "mode": job.mode,
        "provider": job.provider,
        "source": job.source,
        "presets": job.presets,
        "instruction": job.instruction,
        "hints": job.hints,
        "attempt": job.attempt,
        "iterations": [
            {
                "n": it.n,
                "report_summary": it.report_summary,
                "failed_ids": it.failed_ids,
                "usage": it.usage,
                "note": it.note,
                "batch": it.batch,
                "batch_total": it.batch_total,
                "finished_at": iso_utc(it.finished_at),
            }
            for it in job.iterations
        ],
        "usage_total": job.usage_total,
        "error": job.error,
        "report": job.report,
        "verdict": job.verdict,
        "created_at": iso_utc(job.created_at),
        "updated_at": iso_utc(job.updated_at),
        "finished_at": iso_utc(job.finished_at) if job.finished_at else None,
        "expires_at": iso_utc(job.expires_at) if job.expires_at else None,
    }
    if include_draft:
        body["draft"] = draft
        body.update(extras)
    return body


async def _check_provider_ready(
    request: Request, settings: Settings, provider: LLMProvider
) -> None:
    """job 을 받기 전에 키 경로를 확인한다(D25). Secret 저장소(home 클러스터)가 없으면 422
    no_secret_store, Secret 이나 그 안의 키가 없으면 422 provider_no_key. 메시지에는 provider
    이름과 Secret 이름만 싣고 키 값은 절대 싣지 않는다."""
    secret_store = require_secret_store(request, provider.name, verb="read")
    try:
        # Secret 조회는 동기 K8s 호출이라 스레드풀에서. 값은 버리고 있는지만 본다
        await run_in_threadpool(
            resolve_api_key, provider, secret_store, namespace=settings.namespace
        )
    except ProviderError as e:
        raise error(
            422,
            "provider_no_key",
            e.detail,
            provider=provider.name,
            secret=provider.api_key_secret,
        ) from e


# -- 사전 검사 (refine) ------------------------------------------------------------------


async def _load_refine_source(
    tenant: str,
    store: Any,
    files: Any,
    settings: Settings,
    source: JobSource,
    presets: list[str],
) -> dict[str, Any]:
    """refine 원본을 읽고 검사한다. 422 source_not_found(프로필·버전 없음), source_not_verified
    (원본 verdict 에 error. warn 은 통과), source_too_large(뷰가 spec_job_input_max_bytes 초과)."""
    profile = await store.get(source.backend_profile_id, tenant=tenant)
    if profile is None:
        raise error(422, "source_not_found", f"backend api {source.backend_profile_id} not found")
    v = await store.get_version(profile.id, source.version)
    if v is None:
        raise error(
            422, "source_not_found", f"version {source.version} of {profile.name} not found"
        )
    verdict = compute_verdict(v.source_format, v.report, False)
    if verdict.level == "error":
        raise error(
            422,
            "source_not_verified",
            f"version {v.version} of {profile.name} has verify errors: "
            + ", ".join(sorted(i.code for i in verdict.items if i.level == "error")),
            verdict=verdict.to_dict(),
        )
    original = await original_spec(files, v)
    size = refine.view_bytes(original, rename=refine.RENAME_PRESET in presets)
    limit = settings.spec_job_input_max_bytes
    if size > limit:
        raise error(
            422,
            "source_too_large",
            f"refine view of version {v.version} is {size} bytes; limit is {limit}",
            size=size,
            limit=limit,
        )
    return original


async def _load_overrides_source(
    tenant: str,
    profiles: Any,
    backend: Any,
    settings: Settings,
    source: MCPServerSource,
) -> dict[str, Any]:
    """overrides 원본을 읽는다 (D37). 422 source_not_found(프로필·리비전·핀한 backend 버전 없음),
    source_too_large(뷰가 spec_job_input_max_bytes 초과).

    `input.json` 은 `{spec, versions}` 다. 낡은 채택 비교는 `spec` 만 본다. 버전 문서를 통째로 담는
    것은 runner 에 Backend API 저장소를 배선하지 않으려는 것이다. 버전은 불변이라 반복마다 다시
    읽을 이유가 없고, 다른 파드가 재개해도 같은 재료로 같은 판정을 낸다."""
    profile = await profiles.get(source.mcp_server_id, tenant=tenant)
    if profile is None:
        raise error(422, "source_not_found", f"mcp server {source.mcp_server_id} not found")
    rev = profile.get_revision(source.revision)
    if rev is None:
        raise error(
            422, "source_not_found", f"revision {source.revision} of {profile.name} not found"
        )
    versions = []
    for sel in rev.backends:
        bp = await backend.get(sel.backend_profile_id, tenant=tenant)
        if bp is None:
            raise error(422, "source_not_found", f"backend api {sel.backend_profile_id} not found")
        v = await backend.get_version(bp.id, sel.spec_version)
        if v is None:
            raise error(
                422,
                "source_not_found",
                f"backend api {bp.name} has no version {sel.spec_version}",
            )
        versions.append(v)
    spec = overrides.spec_of(rev)
    view = overrides.extract(overrides.fragment(spec), [list(v.tool_names) for v in versions])
    size = len(refine.dumps(view).encode("utf-8"))
    limit = settings.spec_job_input_max_bytes
    if size > limit:
        raise error(
            422,
            "source_too_large",
            f"override view of revision {source.revision} is {size} bytes; limit is {limit}",
            size=size,
            limit=limit,
        )
    return {"spec": spec, "versions": [v.model_dump(mode="json") for v in versions]}


def _require_source_mode(body: JobRequest, source_type: type[BaseModel], shape: str) -> Any:
    """refine·overrides 공통 사전 검사. input 이 아니라 source 를 받고, 프리셋이나 지시가 하나는
    있어야 한다. 통과하면 source 를 돌려준다. 문구의 모드 이름은 body.mode 다."""
    mode = body.mode
    if body.input is not None and not body.input.is_empty():
        raise error(422, "invalid_request", f"{mode} takes source, not input")
    if not isinstance(body.source, source_type):
        raise error(422, "invalid_request", f"{mode} needs source {shape}")
    if not body.presets and not (body.instruction and body.instruction.strip()):
        raise error(422, "empty_instruction", f"{mode} needs at least one preset or instruction")
    return body.source


async def _prepare_generate(body: JobRequest, settings: Settings) -> bytes:
    """generate 의 input.json 본문. 입력 크기와 URL 을 먼저 검사한다."""
    if body.source is not None or body.presets or body.instruction:
        raise error(422, "invalid_request", "source, presets and instruction are for mode refine")
    if body.input is None or body.input.is_empty():
        raise error(422, "empty_input", "input needs text, url, examples or partial_spec")
    raw = json.dumps(body.input.model_dump(exclude_none=True), ensure_ascii=False).encode("utf-8")
    if len(raw) > settings.spec_job_input_max_bytes:
        raise error(
            422,
            "input_too_large",
            f"input is {len(raw)} bytes; limit is {settings.spec_job_input_max_bytes}",
        )
    if body.input.url:
        try:
            # 이름 풀기는 이벤트 루프 밖에서, 시한 안에 한다 (P69)
            await urlfetch.check_url_async(body.input.url)
        except urlfetch.BlockedURL as e:
            raise error(422, "url_blocked", e.detail, reason=e.code) from e
    return raw


async def _prepare_refine(
    body: JobRequest, tenant: str, store: Any, spec_files: Any, settings: Settings
) -> bytes:
    """refine 의 input.json 본문. 원본 OpenAPI 문서다."""
    source = _require_source_mode(body, JobSource, "{backend_profile_id, version}")
    original = await _load_refine_source(
        tenant, store, spec_files, settings, source, list(body.presets)
    )
    return json.dumps(original, ensure_ascii=False).encode("utf-8")


async def _prepare_overrides(
    body: JobRequest, tenant: str, request: Request, store: Any, settings: Settings
) -> bytes:
    """overrides 의 input.json 본문. `{spec, versions}` 다 (D37)."""
    source = _require_source_mode(body, MCPServerSource, "{mcp_server_id, revision}")
    unknown = [p for p in body.presets if p not in overrides.PRESETS]
    if unknown:
        raise error(
            422,
            "invalid_request",
            f"mode overrides takes presets {', '.join(overrides.PRESETS)}",
            unknown=unknown,
        )
    inp = await _load_overrides_source(
        tenant, request.app.state.mcp_servers, store, settings, source
    )
    return json.dumps(inp, ensure_ascii=False).encode("utf-8")


@router.post(
    "/jobs",
    response_model=SpecJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def create_job(
    tenant: Tenant,
    settings: Settings_,
    jobs: Jobs,
    files: JobFiles,
    store: BackendAPIs,
    spec_files: Files,
    request: Request,
    body: JobRequest,
    user: User,
) -> JSONResponse:
    if body.mode == "refine":
        raw = await _prepare_refine(body, tenant, store, spec_files, settings)
    elif body.mode == "overrides":
        raw = await _prepare_overrides(body, tenant, request, store, settings)
    else:
        raw = await _prepare_generate(body, settings)

    platform_settings = await request.app.state.platform_settings.get()
    provider = platform_settings.provider(body.provider)
    if provider is None:
        if body.provider:
            raise error(
                422, "unknown_provider", f"llm provider {body.provider!r} is not configured"
            )
        raise error(
            422, "no_provider", "no llm_providers configured in platform settings (PATCH /settings)"
        )
    if provider.is_stub:
        # stub 은 Agent 실증용이다 (D25). 스펙을 만들 모델이 없다
        raise error(
            422,
            "provider_not_supported",
            f"llm provider {provider.name!r} is a stub and cannot generate specs",
            provider=provider.name,
        )
    await _check_provider_ready(request, settings, provider)

    if await jobs.count_active(tenant) >= settings.spec_job_max_active_per_tenant:
        raise error(
            429,
            "too_many_jobs",
            f"tenant already has {settings.spec_job_max_active_per_tenant} active jobs",
        )

    job = SpecJob(
        tenant=tenant,
        created_by=user.sub,
        provider=provider.name,
        mode=body.mode,
        source=body.source.model_dump() if body.source else None,
        presets=list(body.presets),
        instruction=(body.instruction or "").strip() or None,
        input_ref="",
        input_size=len(raw),
        hints=body.hints.model_dump(exclude_none=True) if body.hints else {},
    )
    job.input_ref = await files.put(input_name(job.id), raw, job_id=job.id, role="input")
    await jobs.create(job)
    return JSONResponse(status_code=202, content=job_view(job))


@router.get(
    "/jobs", response_model=list[SpecJobOut], dependencies=[Depends(require_scope("backend:read"))]
)
async def list_jobs(tenant: Tenant, jobs: Jobs) -> list[dict[str, Any]]:
    return [job_view(j) for j in await jobs.list(tenant)]


@router.get(
    "/jobs/{job_id}",
    response_model=SpecJobDetail,
    dependencies=[Depends(require_scope("backend:read"))],
)
async def get_job(tenant: Tenant, jobs: Jobs, files: JobFiles, job_id: str) -> dict[str, Any]:
    job = await jobs.get(job_id, tenant=tenant)
    if job is None:
        raise not_found(f"spec job {job_id} not found")
    draft: Any = None
    if job.draft_ref:
        raw = await files.get(job.draft_ref)
        if raw is not None:
            try:
                draft = json.loads(raw.decode("utf-8"))
            except ValueError:
                draft = raw.decode("utf-8", errors="replace")
    extras: dict[str, Any] = {}
    if job.mode in ("refine", "overrides") and job.state == "done" and isinstance(draft, dict):
        # 원본은 job 생성 때 넣은 input.json 이다. changes 는 저장하지 않고 여기서 계산한다
        raw_in = await files.get(job.input_ref)
        if raw_in is not None:
            inp = json.loads(raw_in.decode("utf-8"))
            if job.mode == "refine":
                extras = refine.result(inp, draft)
            else:
                # input.json 은 {spec, versions} 라 원본 조각은 spec 쪽이다 (D37)
                extras = overrides.result(overrides.fragment(inp.get("spec") or {}), draft)
    return job_view(job, draft=draft, include_draft=True, **extras)


@router.delete(
    "/jobs/{job_id}",
    response_model=DeleteResponse,
    dependencies=[Depends(require_scope("backend:write"))],
)
async def delete_job(tenant: Tenant, jobs: Jobs, files: JobFiles, job_id: str) -> dict[str, Any]:
    """끝난 job 만 지운다. 진행 중이면 409."""
    job = await jobs.get(job_id, tenant=tenant)
    if job is None:
        raise not_found(f"spec job {job_id} not found")
    if job.active:
        raise error(409, "job_active", f"job is {job.state}; wait until it finishes")
    await jobs.delete(job.id)
    await files.delete_job(job.id)
    return {"id": job.id, "deleted": True}
