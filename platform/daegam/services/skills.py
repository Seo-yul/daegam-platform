"""Skill: 만들기, 리비전, 복제, 정책, 삭제 (D64, P39 1·2절).

만들고 새 리비전을 넣는 것은 `skill:write` 이고 테넌트 안에서는 저장이 곧 게시다. 접근은 허브
항목과 같은 `access {view, consume, manage}` 와 판정 함수를 빌려 쓴다(`auth/hub_access.py`). 다른
테넌트에는 view·consume 만 열고 manage 는 주인 테넌트 안에 둔다(422
`cross_tenant_manage_unsupported`). 경계를 넓히는 저장은 `skill:share`(platform-admin)다. 다른
테넌트의 Skill 은 `clone` 으로 베껴 고친다.

할당량은 Platform Settings `skills` 이고 테넌트마다 Skill 개수(409 `skill_limit`)와 리비전 본문
합계(409 `skill_quota_exceeded`)다. 현재 리비전이나 배포가 핀한 리비전이 Skill 을 가리키는 Agent 가
있으면 삭제는 409 `in_use` 다. 다른 테넌트의 Agent 는 id 대신 수(`other_tenant_agents`)로 알린다.
"""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta

from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.auth.hub_access import Access, AccessError, Action, can, validate_access, visible
from daegam.auth.principal import Principal
from daegam.errors import ServiceError
from daegam.services.references import SKILL_DELETE, ReferenceService
from daegam.services.skill_format import SKILL_MD, RawFiles, ValidSkill, check_skill, renamed
from daegam.store.access import ACCESS_FIELDS, stamp_access
from daegam.store.base import new_id, utcnow
from daegam.store.settings import MongoPlatformSettingsStore
from daegam.store.skills import (
    MongoSkillStore,
    Skill,
    SkillCloneSource,
    SkillRevision,
    SkillRevisionMeta,
)
from daegam.units import MIB

# 경계를 다른 테넌트로 넓히는 저장에 드는 scope
SHARE_SCOPE = "skill:share"
# 다른 테넌트에 열 수 있는 동작. manage 는 주인 테넌트 안에 둔다
CROSS_TENANT_ACTIONS: tuple[Action, ...] = ("view", "consume")
# 표시 이름 바꾸기가 쓰는 필드
RENAME_FIELDS = ("display_name", "updated_at", "updated_by")
# 다음 번호 리비전이 이보다 오래됐는데도 skills.revision 이 아직 그 번호로 오르지 않았다면, 앞선
# 저장이 리비전을 넣은 뒤 번호를 올리기 전에 멈춘 것이다. 넣기와 올리기 사이는 몇 ms 라서, 이 유예
# 안에서는 저장 중인 다른 요청의 리비전을 지우지 않는다
ORPHAN_GRACE = timedelta(minutes=1)
# 내보내는 zip 항목의 권한 비트(일반 파일 0644)
_ZIP_FILE_MODE = 0o100644 << 16


class SkillError(ServiceError):
    pass


class SkillService:
    def __init__(
        self,
        store: MongoSkillStore,
        settings: MongoPlatformSettingsStore,
        references: ReferenceService,
    ):
        self.store = store
        self.settings = settings
        self.references = references

    # -- 판정 ------------------------------------------------------------------------

    async def visible(self, principal: Principal, tenant: str) -> list[Skill]:
        return visible(principal, tenant, await self.store.list_candidates(tenant))

    async def get_visible(self, principal: Principal, tenant: str, skill_id: str) -> Skill:
        """없거나 view 가 없으면 404 다. 존재를 드러내지 않는다."""
        skill = await self.store.get_unscoped(skill_id)
        if skill is None or not can(principal, tenant, skill, "view"):
            raise SkillError(404, "not_found", f"skill {skill_id} not found")
        return skill

    @staticmethod
    def require(principal: Principal, tenant: str, skill: Skill, action: Action) -> None:
        if not can(principal, tenant, skill, action):
            raise SkillError(
                403, "forbidden", f"{action} permission on this skill required", action=action
            )

    # -- 만들기와 리비전 ------------------------------------------------------------------

    async def create(
        self,
        tenant: str,
        files: RawFiles,
        *,
        display_name: str | None,
        note: str | None,
        principal: Principal,
        name: str | None = None,
        cloned_from: SkillCloneSource | None = None,
    ) -> Skill:
        """머리말 name 이 리소스 이름이고 r1 을 만든다. name 을 주면 머리말이 그것과 같아야 한다.
        같은 이름이 있으면 409 `name_conflict` 다."""
        valid = await _checked(files, name=name)
        name = valid.frontmatter.name
        if await self.store.get_by_name(tenant, name) is not None:
            raise _name_conflict(name)
        await self._check_quota(tenant, adding=valid.bytes, new_skill=True)
        revision = _revision(
            new_id(), tenant, 1, valid, note=note, by=principal.sub, cloned_from=cloned_from
        )
        skill = Skill(
            id=revision.skill_id,
            tenant=tenant,
            name=name,
            display_name=display_name or name,
            latest=revision.latest(),
            created_at=revision.created_at,
            updated_at=revision.created_at,
            updated_by=principal.sub,
        )
        # 리비전부터 넣어 Skill 이 리비전 없이 보이는 순간이 없게 한다
        await self.store.add_revision(revision)
        try:
            await self.store.create(skill)
        except Exception as e:
            await self._drop_unowned(revision)
            if isinstance(e, DuplicateKeyError):
                raise _name_conflict(name) from e
            raise
        return skill

    async def add_revision(
        self,
        skill: Skill,
        files: RawFiles,
        *,
        expect_revision: int,
        note: str | None,
        principal: Principal,
    ) -> Skill:
        """새 번호를 넣고 `skills.revision` 을 조건부로 올린다. 어긋나면 409 `revision_conflict`,
        내용이 마지막 리비전과 같으면 422 `skill_unchanged` 다."""
        if expect_revision != skill.revision:
            raise _revision_conflict(skill.revision)
        valid = await _checked(files, name=skill.name)
        if valid.digest == skill.latest.digest:
            raise SkillError(
                422, "skill_unchanged", "the files are the same as the latest revision"
            )
        await self._check_quota(skill.tenant, adding=valid.bytes)
        revision = _revision(
            skill.id, skill.tenant, expect_revision + 1, valid, note=note, by=principal.sub
        )
        await self._insert_next(revision, expect=expect_revision)
        if not await self.store.advance(skill, revision, expect=expect_revision):
            await self.store.delete_revision(revision.id)
            raise _revision_conflict(skill.revision)
        return await self._reloaded(skill)

    async def clone(
        self,
        source: Skill,
        tenant: str,
        *,
        name: str,
        display_name: str | None,
        revision: int | None,
        principal: Principal,
    ) -> Skill:
        """source 의 리비전(없으면 최신)을 tenant 에 새 Skill 로 베낀다. SKILL.md 머리말의 name 을
        새 이름으로 바꾸고 r1 의 `cloned_from` 에 출처를 남긴다."""
        rev = await self.revision(source, revision or source.revision)
        files = [
            (f.path, renamed(f.content, name) if f.path == SKILL_MD else f.content)
            for f in rev.files
        ]
        return await self.create(
            tenant,
            files,
            display_name=display_name,
            note=None,
            principal=principal,
            name=name,
            cloned_from=SkillCloneSource(
                tenant=source.tenant, name=source.name, revision=rev.revision
            ),
        )

    async def rename(self, skill: Skill, display_name: str, principal: Principal) -> Skill:
        skill.display_name = display_name
        skill.updated_at = utcnow()
        skill.updated_by = principal.sub
        await self.store.set_fields(skill, RENAME_FIELDS)
        return skill

    async def set_access(self, skill: Skill, access: Access, principal: Principal) -> Skill:
        """정책 저장. 경계 확장은 `skill:share`, 그 밖은 manage 만으로 된다(manage 는 호출부 몫).
        manage 에 다른 테넌트를 넣으면 422 `cross_tenant_manage_unsupported` 다."""
        try:
            validated = validate_access(
                access,
                current=skill.access,
                owner_tenant=skill.tenant,
                cross_tenant=CROSS_TENANT_ACTIONS,
                share_scope=SHARE_SCOPE,
                principal=principal,
            )
        except AccessError as e:
            raise SkillError(e.status, e.code, e.detail, **e.extra) from e
        stamp_access(skill, validated, principal)
        await self.store.set_fields(skill, ACCESS_FIELDS)
        return skill

    # -- 읽기 -------------------------------------------------------------------------

    async def revisions(self, skill: Skill) -> list[SkillRevisionMeta]:
        """새것부터. 파일 본문은 없다. 최신보다 큰 번호(저장 중이거나 멈춘 저장이 남긴 것)는
        뺀다."""
        return [
            r for r in await self.store.list_revisions(skill.id) if r.revision <= skill.revision
        ]

    async def revision(self, skill: Skill, n: int) -> SkillRevision:
        rev = await self.store.get_revision(skill.id, n) if n <= skill.revision else None
        if rev is None:
            raise SkillError(404, "not_found", f"skill {skill.id} has no revision {n}")
        return rev

    async def archive(self, skill: Skill, n: int) -> tuple[bytes, str]:
        """리비전 n 의 zip(`<name>/SKILL.md …`)과 파일 이름. 같은 리비전은 같은 바이트다."""
        rev = await self.revision(skill, n)
        stamp = rev.created_at.timetuple()[:6]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in rev.files:
                info = zipfile.ZipInfo(f"{skill.name}/{f.path}", date_time=stamp)
                info.external_attr = _ZIP_FILE_MODE
                zf.writestr(info, f.content, compress_type=zipfile.ZIP_DEFLATED)
        return buf.getvalue(), f"{skill.name}-r{rev.revision}.zip"

    # -- 삭제 -------------------------------------------------------------------------

    async def delete(self, skill: Skill) -> None:
        """현재·배포 리비전으로 이 Skill 을 핀한 Agent 가 있으면 409 `in_use` 다. 다른 테넌트의
        Agent 도 막되 수로만 알린다."""
        everyone = await self.references.used_by("skill", skill.id, tenant=None)
        if everyone:
            mine = await self.references.used_by("skill", skill.id, tenant=skill.tenant)
            raise SkillError(
                409,
                "in_use",
                "skill is pinned by agents",
                **{field.key: field.ids(mine) for field in SKILL_DELETE},
                other_tenant_agents=len(everyone) - len(mine),
            )
        await self.store.delete(skill.id)

    # -- 내부 -------------------------------------------------------------------------

    async def _check_quota(self, tenant: str, *, adding: int, new_skill: bool = False) -> None:
        limits = (await self.settings.get()).skills
        if new_skill and await self.store.count(tenant) >= limits.max_per_tenant:
            raise SkillError(
                409,
                "skill_limit",
                f"a tenant holds at most {limits.max_per_tenant} skills",
                limit=limits.max_per_tenant,
            )
        used = await self.store.revision_bytes(tenant)
        quota = limits.tenant_quota_mb * MIB
        if used + adding > quota:
            raise SkillError(
                409,
                "skill_quota_exceeded",
                f"skill revisions of this tenant would take {used + adding} bytes; "
                f"quota is {limits.tenant_quota_mb} MiB",
                used_bytes=used,
                quota_bytes=quota,
            )

    async def _insert_next(self, revision: SkillRevision, *, expect: int) -> None:
        """번호 expect+1 을 넣는다. 그 번호가 이미 있는데 Skill 이 아직 expect 이고 그 리비전이
        `ORPHAN_GRACE` 보다 오래됐으면, 지난 저장이 넣고 올리기 전에 멈춘 것이라 거두고 한 번 다시
        넣는다. 남겨 두면 그 Skill 에 새 리비전을 영영 넣지 못한다. Skill 이 정말 앞서 갔거나 다른
        요청이 막 넣은 것이면 409 `revision_conflict` 다."""
        for retry in (False, True):
            try:
                await self.store.add_revision(revision)
                return
            except DuplicateKeyError as e:
                current = await self.store.get_unscoped(revision.skill_id)
                latest = current.revision if current is not None else expect
                if retry or latest != expect or not await self._drop_stale(revision):
                    raise _revision_conflict(latest) from e

    async def _drop_stale(self, revision: SkillRevision) -> bool:
        before = utcnow() - ORPHAN_GRACE
        return await self.store.delete_stale_revision(
            revision.skill_id, revision.revision, before=before
        )

    async def _drop_unowned(self, revision: SkillRevision) -> None:
        """Skill 을 만들지 못했으면 먼저 넣은 리비전을 거둔다. 남기면 보이지 않는 채로 할당량에 계속
        잡힌다. 결과를 모르는 실패(시간 초과)도 있어 Skill 이 정말 없을 때만 지운다."""
        if await self.store.get_unscoped(revision.skill_id) is None:
            await self.store.delete_revision(revision.id)

    async def _reloaded(self, skill: Skill) -> Skill:
        return await self.store.get_unscoped(skill.id) or skill


async def _checked(files: RawFiles, *, name: str | None) -> ValidSkill:
    """형식 검사는 글 길이에 비례해 CPU 를 쓰므로 이벤트 루프 밖에서 돈다."""
    checked = await run_in_threadpool(check_skill, files, name=name)
    return checked.valid()


def _revision(
    skill_id: str,
    tenant: str,
    n: int,
    valid: ValidSkill,
    *,
    note: str | None,
    by: str,
    cloned_from: SkillCloneSource | None = None,
) -> SkillRevision:
    return SkillRevision(
        skill_id=skill_id,
        tenant=tenant,
        revision=n,
        files=list(valid.files),
        frontmatter=valid.frontmatter,
        digest=valid.digest,
        bytes=valid.bytes,
        note=note,
        cloned_from=cloned_from,
        created_by=by,
    )


def _name_conflict(name: str) -> SkillError:
    return SkillError(409, "name_conflict", f"skill {name!r} already exists")


def _revision_conflict(current: int) -> SkillError:
    return SkillError(
        409,
        "revision_conflict",
        "another revision was saved first; reload and try again",
        revision=current,
    )
