"""Skill API (D64, P39 3절). GET 은 skill:read, 쓰기는 skill:write 이고 그 뒤에 access 를 판정한다.

판정 순서는 허브와 같다. scope 검사 뒤에 `access` 를 보고 view 가 없으면 404 로 존재를 숨긴다.
규칙은 `SkillService` 와 형식 검사(`services/skill_format.py`)에 있고 라우터는 입력을 받아 넘긴다.
감사는 미들웨어가 핸들러 이름으로 남기고 본문은 남기지 않는다.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Path, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from daegam.api._common import not_null, patched_fields, read_capped, references_out
from daegam.api._download import download_openapi, download_response
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES, DeleteResponse, ErrorResponse
from daegam.api.schemas.references import ReferencesResponse
from daegam.api.schemas.skills import (
    SkillImportedOut,
    SkillOut,
    SkillRevisionMetaOut,
    SkillRevisionOut,
    SkillValidateOut,
)
from daegam.auth.hub_access import Access, permissions_dict, scope_of
from daegam.auth.principal import Principal
from daegam.body_limit import MULTIPART_SLACK
from daegam.names import validate_display_name, validate_name
from daegam.services.skill_format import (
    MAX_FILES,
    MAX_IMPORT_BYTES,
    MAX_REVISION_BYTES,
    RawFiles,
    Upload,
    check_skill,
    is_script,
    read_upload,
)
from daegam.services.skills import SkillService
from daegam.store.skills import Skill, SkillFileMeta, SkillRevisionMeta
from daegam.units import MIB
from daegam.urls import API_PREFIX

router = APIRouter(responses=ERROR_RESPONSES, prefix="/skills", tags=["skills"])
READ = Depends(require_scope("skill:read"))
WRITE = Depends(require_scope("skill:write"))
# 가져오기 경로의 본문 상한. 앱 전체(업로드 상한)보다 작고, 상한 크기의 zip 이 multipart 경계 때문에
# 끊기지 않게 여유를 더한다. 크기 판정(413 too_large)은 라우터가 한다
IMPORT_BODY_LIMIT = (
    re.compile(rf"^{re.escape(API_PREFIX)}/skills/(import|[^/]+/revisions/import)$"),
    MAX_IMPORT_BYTES + MULTIPART_SLACK,
)
IMPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {
        "model": ErrorResponse,
        "description": f"zip 이나 SKILL.md 가 {MAX_IMPORT_BYTES // MIB}MiB 를 넘는다 (too_large)",
    }
}
ARCHIVE_RESPONSES = download_openapi("리비전의 zip(`<name>/SKILL.md …`). 내려받기 헤더로 온다")


def get_service(request: Request) -> SkillService:
    return request.app.state.skill_service


Service = Annotated[SkillService, Depends(get_service)]
RevisionNumber = Annotated[int, Path(ge=1)]


class SkillFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    # 글자 수 상한이다. 리비전 합계의 바이트 상한과 같은 수라서, 이것을 넘는 글은 바이트로도 넘고
    # 검사의 file_too_large 보다 먼저 본문 검증에서 막힌다
    content: str = Field(max_length=MAX_REVISION_BYTES)


# 파일 수는 검사가 too_many_files 로 알릴 수 있게 상한보다 넉넉히 받는다
Files = Annotated[list[SkillFileIn], Field(max_length=2 * MAX_FILES)]
# 리비전 메모의 글자 수 상한이다. 메모는 리비전 탭 목록에 보이고 화면(lib/skills.ts)도 같은 값을
# 쓴다
NOTE_MAX = 500
Note = Annotated[str | None, Field(max_length=NOTE_MAX)]


class SkillCreate(BaseModel):
    """머리말 `name` 이 리소스 이름이고 r1 을 만든다."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None  # 없으면 name (D24)
    files: Files
    note: Note = None


class SkillRevisionCreate(BaseModel):
    """`expect_revision` 은 고치기 시작할 때 본 최신 리비전 번호다. 그사이 다른 리비전이 들어왔으면
    409 revision_conflict 다."""

    model_config = ConfigDict(extra="forbid")

    files: Files
    note: Note = None
    expect_revision: int = Field(ge=1)


class SkillValidate(BaseModel):
    """name 을 주면 그 이름의 Skill 에 넣을 새 리비전으로 본다(머리말 name 이 같아야 한다)."""

    model_config = ConfigDict(extra="forbid")

    files: Files
    name: str | None = None


class SkillPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None


class SkillClone(BaseModel):
    """consume 이 있으면 다른 테넌트의 Skill 도 자기 테넌트에 베낀다. revision 이 없으면
    최신이다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    revision: int | None = Field(default=None, ge=1)


def _files(body: list[SkillFileIn]) -> RawFiles:
    return [(f.path, f.content) for f in body]


def _file_meta(f: SkillFileMeta) -> dict[str, Any]:
    return {**f.model_dump(mode="json"), "script": is_script(f.path)}


def _out(skill: Skill, *, principal: Principal, tenant: str) -> dict[str, Any]:
    d = skill.model_dump(mode="json")
    d["latest"]["files"] = [_file_meta(f) for f in skill.latest.files]
    d["permissions"] = permissions_dict(principal, tenant, skill)
    d["scope"] = scope_of(skill)
    return d


def _revision_out(rev: SkillRevisionMeta) -> dict[str, Any]:
    """리비전 응답. 목록의 것은 파일 본문이 없고 하나의 것은 있다."""
    d = rev.model_dump(mode="json")
    d["files"] = [_file_meta(f) for f in rev.files]
    return d


async def _read_upload(file: UploadFile) -> Upload:
    """zip 풀기와 검사는 CPU 를 쓰므로 이벤트 루프 밖에서 돈다."""
    return await run_in_threadpool(read_upload, await read_capped(file, MAX_IMPORT_BYTES))


# -- 목록·만들기 ------------------------------------------------------------------------


@router.get("", response_model=list[SkillOut], dependencies=[READ])
async def list_skills(tenant: Tenant, service: Service, user: User) -> list[dict[str, Any]]:
    """view 로 거른 목록. 다른 테넌트가 연 Skill 도 들고 `tenant` 가 주인 테넌트다."""
    return [_out(s, principal=user, tenant=tenant) for s in await service.visible(user, tenant)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SkillOut, dependencies=[WRITE])
async def create_skill(
    tenant: Tenant, service: Service, user: User, body: SkillCreate
) -> dict[str, Any]:
    """파일로 Skill 을 만든다. 형식이 틀리면 422 invalid_skill 이고 issues 에 모두 싣는다."""
    skill = await service.create(
        tenant,
        _files(body.files),
        display_name=_display_name(body.display_name),
        note=body.note,
        principal=user,
    )
    return _out(skill, principal=user, tenant=tenant)


@router.post(
    "/import",
    status_code=status.HTTP_201_CREATED,
    response_model=SkillImportedOut,
    responses=IMPORT_RESPONSES,
    dependencies=[WRITE],
)
async def import_skill(
    tenant: Tenant,
    service: Service,
    user: User,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """zip 이나 SKILL.md 하나로 Skill 을 만든다. 모든 항목이 한 폴더 아래 있으면 그 폴더를
    벗긴다."""
    upload = await _read_upload(file)
    skill = await service.create(
        tenant,
        upload.files,
        display_name=_display_name(display_name),
        note=None,
        principal=user,
    )
    return {**_out(skill, principal=user, tenant=tenant), "skipped": list(upload.skipped)}


@router.post("/validate", response_model=SkillValidateOut, dependencies=[WRITE])
async def validate_skill(body: SkillValidate) -> dict[str, Any]:
    """저장 없이 검사한다. 오류와 경고(스크립트, 모르는 머리말 키)를 모두 싣는다."""
    checked = await run_in_threadpool(check_skill, _files(body.files), name=body.name)
    return {
        "frontmatter": checked.frontmatter.model_dump() if checked.frontmatter else None,
        "issues": [i.to_dict() for i in checked.issues],
        "digest": checked.digest,
        "bytes": checked.bytes,
    }


# -- 하나 -----------------------------------------------------------------------------


@router.get("/{skill_id}", response_model=SkillOut, dependencies=[READ])
async def get_skill(tenant: Tenant, service: Service, user: User, skill_id: str) -> dict[str, Any]:
    """상세와 최신 리비전의 파일 목록."""
    skill = await service.get_visible(user, tenant, skill_id)
    return _out(skill, principal=user, tenant=tenant)


@router.patch("/{skill_id}", response_model=SkillOut, dependencies=[WRITE])
async def update_skill(
    tenant: Tenant, service: Service, user: User, skill_id: str, patch: SkillPatch
) -> dict[str, Any]:
    """표시 이름만 바꾼다. manage 가 없으면 403 이다."""
    skill = await service.get_visible(user, tenant, skill_id)
    service.require(user, tenant, skill, "manage")
    patched_fields(patch)
    value = not_null(patch.display_name, "display_name", code="invalid_display_name")
    skill = await service.rename(skill, validate_display_name(value, default=skill.name), user)
    return _out(skill, principal=user, tenant=tenant)


@router.delete("/{skill_id}", response_model=DeleteResponse, dependencies=[WRITE])
async def delete_skill(
    tenant: Tenant, service: Service, user: User, skill_id: str
) -> dict[str, Any]:
    """Skill 과 모든 리비전을 지운다. 핀한 Agent 가 있으면 409 in_use 다."""
    skill = await service.get_visible(user, tenant, skill_id)
    service.require(user, tenant, skill, "manage")
    await service.delete(skill)
    return {"id": skill.id, "deleted": True}


@router.get("/{skill_id}/references", response_model=ReferencesResponse, dependencies=[READ])
async def skill_references(
    tenant: Tenant, service: Service, user: User, skill_id: str, request: Request
) -> dict[str, Any]:
    """이 Skill 의 참조 (D43). 핀한 Agent 이고 행마다 `pins {current, deployed}` 가 있다."""
    skill = await service.get_visible(user, tenant, skill_id)
    return await references_out(request, "skill", skill.id, tenant=tenant)


@router.put("/{skill_id}/access", response_model=SkillOut, dependencies=[WRITE])
async def set_skill_access(
    tenant: Tenant, service: Service, user: User, skill_id: str, body: Access
) -> dict[str, Any]:
    """정책 저장. manage 가 없으면 403, 경계를 넓히면 skill:share(platform-admin), manage 에 다른
    테넌트를 넣으면 422 cross_tenant_manage_unsupported 다."""
    skill = await service.get_visible(user, tenant, skill_id)
    service.require(user, tenant, skill, "manage")
    skill = await service.set_access(skill, body, user)
    return _out(skill, principal=user, tenant=tenant)


@router.post(
    "/{skill_id}/clone",
    status_code=status.HTTP_201_CREATED,
    response_model=SkillOut,
    dependencies=[WRITE],
)
async def clone_skill(
    tenant: Tenant, service: Service, user: User, skill_id: str, body: SkillClone
) -> dict[str, Any]:
    """요청 테넌트에 새 Skill 로 베낀다. consume 이 없으면 403 이다."""
    source = await service.get_visible(user, tenant, skill_id)
    service.require(user, tenant, source, "consume")
    skill = await service.clone(
        source,
        tenant,
        name=validate_name(body.name),
        display_name=_display_name(body.display_name),
        revision=body.revision,
        principal=user,
    )
    return _out(skill, principal=user, tenant=tenant)


# -- 리비전 ----------------------------------------------------------------------------


@router.get("/{skill_id}/revisions", response_model=list[SkillRevisionMetaOut], dependencies=[READ])
async def list_skill_revisions(
    tenant: Tenant, service: Service, user: User, skill_id: str
) -> list[dict[str, Any]]:
    """새것부터. 파일 본문은 없다."""
    skill = await service.get_visible(user, tenant, skill_id)
    return [_revision_out(r) for r in await service.revisions(skill)]


@router.get("/{skill_id}/revisions/{n}", response_model=SkillRevisionOut, dependencies=[READ])
async def get_skill_revision(
    tenant: Tenant, service: Service, user: User, skill_id: str, n: RevisionNumber
) -> dict[str, Any]:
    """리비전 하나와 파일 본문."""
    skill = await service.get_visible(user, tenant, skill_id)
    return _revision_out(await service.revision(skill, n))


@router.get(
    "/{skill_id}/revisions/{n}/archive",
    response_class=Response,
    responses=ARCHIVE_RESPONSES,
    dependencies=[READ],
)
async def export_skill_revision(
    tenant: Tenant, service: Service, user: User, skill_id: str, n: RevisionNumber
) -> Response:
    """리비전을 zip 으로 내보낸다. 테넌트가 쓴 글을 플랫폼 origin 에서 내리므로 attachment 와
    nosniff 를 단다."""
    skill = await service.get_visible(user, tenant, skill_id)
    body, name = await service.archive(skill, n)
    return download_response(body, name)


@router.post(
    "/{skill_id}/revisions",
    status_code=status.HTTP_201_CREATED,
    response_model=SkillOut,
    dependencies=[WRITE],
)
async def add_skill_revision(
    tenant: Tenant, service: Service, user: User, skill_id: str, body: SkillRevisionCreate
) -> dict[str, Any]:
    """새 리비전을 넣는다. manage 가 없으면 403, 최신이 expect_revision 이 아니면 409
    revision_conflict, 마지막 리비전과 같으면 422 skill_unchanged 다."""
    skill = await service.get_visible(user, tenant, skill_id)
    service.require(user, tenant, skill, "manage")
    skill = await service.add_revision(
        skill,
        _files(body.files),
        expect_revision=body.expect_revision,
        note=body.note,
        principal=user,
    )
    return _out(skill, principal=user, tenant=tenant)


@router.post(
    "/{skill_id}/revisions/import",
    status_code=status.HTTP_201_CREATED,
    response_model=SkillImportedOut,
    responses=IMPORT_RESPONSES,
    dependencies=[WRITE],
)
async def import_skill_revision(
    tenant: Tenant,
    service: Service,
    user: User,
    skill_id: str,
    file: Annotated[UploadFile, File()],
    expect_revision: Annotated[int, Form(ge=1)],
) -> dict[str, Any]:
    """zip 이나 SKILL.md 하나로 새 리비전을 넣는다. 규칙은 JSON 으로 넣는 것과 같다."""
    skill = await service.get_visible(user, tenant, skill_id)
    service.require(user, tenant, skill, "manage")
    upload = await _read_upload(file)
    skill = await service.add_revision(
        skill, upload.files, expect_revision=expect_revision, note=None, principal=user
    )
    return {**_out(skill, principal=user, tenant=tenant), "skipped": list(upload.skipped)}


def _display_name(value: str | None) -> str | None:
    """보냈으면 D24 규칙으로 본다. 없으면 서비스가 name 으로 채운다."""
    return None if value is None else validate_display_name(value, default="")
