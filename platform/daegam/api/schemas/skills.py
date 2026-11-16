"""Skill 응답 (api/skills.py 의 _out 과 같은 필드, D64)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.api.schemas.hub import AccessOut, PermissionsOut
from daegam.store.skills import (
    Skill,
    SkillFile,
    SkillFileMeta,
    SkillFrontmatter,
    SkillLatest,
    SkillRevisionMeta,
)


class SkillFileMetaOut(SkillFileMeta):
    model_config = REQUIRED_DEFAULTS

    # 스크립트로 보는 파일(`scripts/` 아래나 .py·.sh·.js·.ts). 받되 P40 전에는 돌지 않는 글이다
    script: bool


class SkillFileOut(SkillFile):
    model_config = REQUIRED_DEFAULTS

    script: bool


class SkillLatestOut(SkillLatest):
    model_config = REQUIRED_DEFAULTS

    files: list[SkillFileMetaOut]  # type: ignore[assignment]


class SkillOut(Skill):
    """목록과 상세. permissions 는 요청 주체의 판정이고 scope 는 access 에서 파생한 값이다."""

    model_config = REQUIRED_DEFAULTS

    latest: SkillLatestOut
    access: AccessOut
    permissions: PermissionsOut
    scope: Literal["tenant", "shared"]


class SkillImportedOut(SkillOut):
    """가져오기. 건너뛴 zip 항목(`__MACOSX/`·`.DS_Store`)을 적는다."""

    skipped: list[str]


class SkillFrontmatterOut(SkillFrontmatter):
    model_config = REQUIRED_DEFAULTS


class SkillIssueOut(BaseModel):
    """검사 결과 한 줄. path 가 null 이면 Skill 전체에 걸린 것이다."""

    model_config = ConfigDict(extra="forbid")

    path: str | None
    code: str
    message: str
    severity: Literal["error", "warning"]


class SkillValidateOut(BaseModel):
    """저장 없이 한 검사. 오류가 있으면 frontmatter·digest 가 null 이다."""

    model_config = ConfigDict(extra="forbid")

    frontmatter: SkillFrontmatterOut | None
    issues: list[SkillIssueOut]
    digest: str | None
    bytes: int


class SkillRevisionMetaOut(SkillRevisionMeta):
    """리비전 목록. 파일 본문은 없다."""

    model_config = REQUIRED_DEFAULTS

    frontmatter: SkillFrontmatterOut
    files: list[SkillFileMetaOut]  # type: ignore[assignment]


class SkillRevisionOut(SkillRevisionMetaOut):
    """리비전 하나. 파일 본문까지 있다."""

    files: list[SkillFileOut]  # type: ignore[assignment]


class SkillBundleFileOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class SkillBundleOut(BaseModel):
    """init 이 받는 Skill 리비전 하나다(D64). `agent.schema.json` 의 `$defs/skill_bundle` 과
    모양이 같다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    id: str
    revision: int
    digest: str
    files: list[SkillBundleFileOut]
