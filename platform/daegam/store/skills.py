"""Skill 과 그 리비전 (D64, P39 1절).

Skill 은 테넌트 리소스이고 정의와 리비전만 있다. 클러스터에 뜨는 것이 없어 상태도 배포도 없다.
`skills` 문서는 이름·표시 이름·접근 정책과 최신 리비전의 요약(`latest`)이고, 파일 본문은
`skill_revisions` 의 리비전 문서 안에 있다. 리비전은 512 KiB 까지라 문서 상한(16 MiB)에 넉넉하고,
GridFS 에 두면 지울 때 두 곳을 맞춰야 한다. 리비전을 고치는 길은 없어 쓴 리비전은 바뀌지 않는다.

새 리비전은 번호를 먼저 넣고(`uq_skill_revision` 이 같은 번호의 두 번째를 막는다)
`skills.revision` 을 조건부로 올린다(`advance`). 둘 중 하나가 지면 부르는 쪽이 409
`revision_conflict` 를 낸다. 넣고 올리기 전에 멈춘 저장이 남긴 번호는 다음 저장이 오래된 것만
거둔다(`delete_stale_revision`). 조회는 `access` 를 판정하지 않으므로 주체가 있는 경로는
`SkillService.visible`·`get_visible` 을 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.auth.hub_access import Access
from daegam.store.access import boundary_candidates, create_access_indexes
from daegam.store.base import Document, UtcDatetime, new_id, utcnow

SKILLS = "skills"
SKILL_REVISIONS = "skill_revisions"


class SkillFrontmatter(BaseModel):
    """SKILL.md 머리말의 아는 키. `allowed-tools` 는 `allowed_tools` 로 옮겨 둔다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    # 표시뿐이다. 도구와 승인은 캔버스의 tools 노드가 정한다
    allowed_tools: list[str] = Field(default_factory=list)


class SkillFileMeta(BaseModel):
    """본문 없는 파일 요약. sha256 은 저장한 글(UTF-8)의 것이라 리비전 사이에 바뀐 파일을 가린다."""

    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int
    sha256: str


class SkillFile(SkillFileMeta):
    """리비전 안의 파일. content 는 저장할 때 맞춘 글(첫 BOM 을 떼고 줄 끝을 LF 로)이다."""

    content: str


class SkillLatest(BaseModel):
    """최신 리비전의 요약. 목록이 리비전 문서를 읽지 않고 그린다."""

    model_config = ConfigDict(extra="forbid")

    description: str
    digest: str
    bytes: int
    files: list[SkillFileMeta]


class Skill(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str  # D24. 생성 뒤 바뀌지 않고 SKILL.md 머리말의 name 과 같다
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    revision: int = 1
    latest: SkillLatest
    access: Access = Field(default_factory=Access)
    access_updated_by: str | None = None
    access_updated_at: UtcDatetime | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None


class SkillCloneSource(BaseModel):
    """복제로 만든 리비전의 출처. 화면이 "acme/demo r3 에서 복제" 로 그린다."""

    model_config = ConfigDict(extra="forbid")

    tenant: str
    name: str
    revision: int


class SkillRevisionMeta(Document):
    """파일 본문을 뺀 리비전. 리비전 목록이 이것으로 읽는다."""

    id: str = Field(default_factory=new_id)
    skill_id: str
    tenant: str  # 할당량을 세는 테넌트. 주인 테넌트다
    revision: int
    files: list[SkillFileMeta]
    frontmatter: SkillFrontmatter
    digest: str
    bytes: int
    note: str | None = None
    # 복제로 만든 r1 에만 있다
    cloned_from: SkillCloneSource | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    created_by: str | None = None


class SkillRevision(SkillRevisionMeta):
    files: list[SkillFile]

    def latest(self) -> SkillLatest:
        """이 리비전을 최신으로 둘 때의 요약."""
        return SkillLatest(
            description=self.frontmatter.description,
            digest=self.digest,
            bytes=self.bytes,
            files=[SkillFileMeta(path=f.path, bytes=f.bytes, sha256=f.sha256) for f in self.files],
        )


class MongoSkillStore:
    def __init__(self, db: AsyncDatabase):
        self.skills = db[SKILLS]
        self.revisions = db[SKILL_REVISIONS]

    async def init_schema(self) -> None:
        await self.skills.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )
        await create_access_indexes(self.skills)
        await self.revisions.create_index(
            [("skill_id", ASCENDING), ("revision", ASCENDING)],
            unique=True,
            name="uq_skill_revision",
        )
        await self.revisions.create_index([("tenant", ASCENDING)], name="ix_tenant")

    # -- Skill ---------------------------------------------------------------------

    async def create(self, skill: Skill) -> Skill:
        await self.skills.insert_one(skill.to_doc())
        return skill

    async def get_unscoped(self, skill_id: str) -> Skill | None:
        """판정 없는 조회. 주체가 있는 경로는 SkillService.get_visible 을 쓴다."""
        doc = await self.skills.find_one({"_id": skill_id})
        return Skill.from_doc(doc) if doc else None

    async def get_by_name(self, tenant: str, name: str) -> Skill | None:
        doc = await self.skills.find_one({"tenant": tenant, "name": name})
        return Skill.from_doc(doc) if doc else None

    async def get_many_unscoped(self, ids: list[str]) -> dict[str, Skill]:
        """판정 없는 일괄 조회. Agent 가 핀한 Skill 을 검증·상태 경고가 읽는다."""
        if not ids:
            return {}
        cursor = self.skills.find({"_id": {"$in": ids}})
        return {d["_id"]: Skill.from_doc(d) for d in await cursor.to_list()}

    async def list_candidates(self, tenant: str) -> list[Skill]:
        """tenant 에 보일 수 있는 후보. view 판정은 SkillService.visible 이 한다."""
        cursor = self.skills.find({"$or": boundary_candidates(tenant)}).sort(
            [("created_at", ASCENDING), ("_id", ASCENDING)]
        )
        return [Skill.from_doc(d) for d in await cursor.to_list()]

    async def count(self, tenant: str) -> int:
        return await self.skills.count_documents({"tenant": tenant})

    async def set_fields(self, skill: Skill, fields: Iterable[str]) -> None:
        """skill 의 이 필드만 쓴다. 표시 이름 바꾸기와 정책 저장이 겹쳐도 서로를 옛 값으로 되돌리지
        않게 동작마다 제 필드만 쓴다. 리비전은 `advance` 만 올린다."""
        doc = skill.to_doc()
        await self.skills.update_one({"_id": skill.id}, {"$set": {f: doc[f] for f in fields}})

    async def advance(self, skill: Skill, revision: SkillRevision, *, expect: int) -> bool:
        """`skills.revision` 이 아직 expect 일 때만 최신을 revision 으로 올린다. 졌으면 False 다."""
        res = await self.skills.update_one(
            {"_id": skill.id, "revision": expect},
            {
                "$set": {
                    "revision": revision.revision,
                    "latest": revision.latest().model_dump(),
                    "updated_at": revision.created_at,
                    "updated_by": revision.created_by,
                }
            },
        )
        return res.modified_count == 1

    async def delete(self, skill_id: str) -> None:
        """리비전부터 지운다. 도중에 멈춰도 Skill 문서가 남아 다시 DELETE 할 수 있다."""
        await self.revisions.delete_many({"skill_id": skill_id})
        await self.skills.delete_one({"_id": skill_id})

    # -- 리비전 ----------------------------------------------------------------------

    async def add_revision(self, revision: SkillRevision) -> SkillRevision:
        """같은 (skill_id, revision) 이 있으면 pymongo DuplicateKeyError 가 그대로 올라온다."""
        await self.revisions.insert_one(revision.to_doc())
        return revision

    async def delete_revision(self, revision_id: str) -> None:
        """올리기에 진 리비전이나, Skill 을 만들지 못해 주인이 없는 첫 리비전(`_drop_unowned`)을
        거둔다."""
        await self.revisions.delete_one({"_id": revision_id})

    async def delete_stale_revision(
        self, skill_id: str, revision: int, *, before: datetime
    ) -> bool:
        """before 보다 먼저 넣은 그 번호의 리비전을 지운다. 막 넣은 것은 남긴다. 지웠으면
        True 다."""
        res = await self.revisions.delete_one(
            {"skill_id": skill_id, "revision": revision, "created_at": {"$lt": before}}
        )
        return res.deleted_count == 1

    async def get_revision(self, skill_id: str, revision: int) -> SkillRevision | None:
        doc = await self.revisions.find_one({"skill_id": skill_id, "revision": revision})
        return SkillRevision.from_doc(doc) if doc else None

    async def pinned_metas(self, pins: list[tuple[str, int]]) -> list[SkillRevisionMeta]:
        """핀 (skill_id, revision) 들의 리비전. 파일 본문은 읽지 않는다. 없는 핀은 빠진다."""
        if not pins:
            return []
        cursor = self.revisions.find(_pins_filter(pins), projection={"files.content": 0})
        return [SkillRevisionMeta.from_doc(d) for d in await cursor.to_list()]

    async def pinned_named(self, pins: list[tuple[str, int]], name: str) -> SkillRevision | None:
        """핀 가운데 머리말 name 이 그것인 리비전 하나. 파일 본문까지 읽는다(init 의 Skill 받기)."""
        if not pins:
            return None
        doc = await self.revisions.find_one({**_pins_filter(pins), "frontmatter.name": name})
        return SkillRevision.from_doc(doc) if doc else None

    async def list_revisions(self, skill_id: str) -> list[SkillRevisionMeta]:
        """새것부터. 파일 본문은 읽지 않는다."""
        cursor = self.revisions.find({"skill_id": skill_id}, projection={"files.content": 0}).sort(
            "revision", DESCENDING
        )
        return [SkillRevisionMeta.from_doc(d) for d in await cursor.to_list()]

    async def revision_bytes(self, tenant: str) -> int:
        """테넌트의 리비전 본문 합계. 할당량(`skills.tenant_quota_mb`)이 센다."""
        cursor = await self.revisions.aggregate(
            [{"$match": {"tenant": tenant}}, {"$group": {"_id": None, "bytes": {"$sum": "$bytes"}}}]
        )
        rows = await cursor.to_list()
        return int(rows[0]["bytes"]) if rows else 0


def _pins_filter(pins: list[tuple[str, int]]) -> dict[str, Any]:
    """핀 목록을 고르는 질의. `uq_skill_revision` 이 받친다."""
    return {"$or": [{"skill_id": skill_id, "revision": n} for skill_id, n in pins]}
