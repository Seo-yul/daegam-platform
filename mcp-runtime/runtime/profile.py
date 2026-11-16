"""profile.json 모델. 플랫폼(MCP Server 리비전)이 렌더링해 initContainer 가 받아온다.

비밀은 들어 있지 않다. 자격증명은 SPEC2OPENAPI_USERNAME/PASSWORD 환경변수로만 온다.

이 판부터 모델은 모르는 칸을 거절한다(P52 4절). 플랫폼이 이 이미지가 모르는 칸(새 규칙 같은 것)을
실었는데 조용히 버리면 그 규칙이 지켜지지 않은 채 서버가 뜬다. 거절하면 컨테이너가 뜨지 않는다.
`rules` 는 operationId 마다의 Tool 규칙이고(읽기 전용 표시와 인자 조건), 평면 profile 은 최상위에,
backends 형식은 항목마다 든다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# FastMCP 4.0.x OpenAPIProvider._generate_default_name 은 mcp_names 로 받은 이름에도
# _slugify(구분자는 '_' 로, [^A-Za-z0-9_] 제거, '_' 연속 축약, 양끝 '_' 제거)를 적용하고
# 56자에서 자른다. 오버라이드 이름이 그 과정에서 바뀌면 사용자가 지정한 이름과
# 노출되는 이름이 달라지므로, 정규화를 통과해도 "변하지 않는" 이름만 받는다.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
FASTMCP_NAME_MAX = 56


class ProfileError(ValueError):
    """profile.json 이 잘못됐을 때 낸다. status 는 플랫폼 API 의 422 에 대응한다."""

    status = 422

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.status} {self.code}] {self.message}"


def fastmcp_normalize(name: str) -> str:
    """FastMCP 4.0.x provider._slugify + 56자 절단을 그대로 흉내 낸다."""
    slug = re.sub(r"[\s\-\.]+", "_", name)
    slug = re.sub(r"[^a-zA-Z0-9_]", "", slug)
    slug = re.sub(r"_+", "_", slug)
    slug = slug.strip("_")
    return slug[:FASTMCP_NAME_MAX]


def validate_tool_name(name: str) -> str:
    if not TOOL_NAME_RE.fullmatch(name):
        raise ValueError(f"tool name {name!r} must match [A-Za-z0-9_] and be 1..64 chars")
    normalized = fastmcp_normalize(name)
    if normalized != name:
        raise ValueError(
            f"tool name {name!r} would be exposed by FastMCP as {normalized!r} "
            f"(no leading/trailing/double underscores, max {FASTMCP_NAME_MAX} chars)"
        )
    return name


# 인자 조건의 연산자다 (P52 4절). 정규식은 없다. 플랫폼 `store/tool_rules.py`
# `ConditionOp` 와 같다
ConditionOp = Literal["equals", "one_of", "min", "max", "max_length", "prefix"]


class ArgCondition(BaseModel):
    """인자 조건 하나다. path 는 FastMCP 입력 스키마의 이름을 점으로 이은 경로이고, optional 이
    아니면 인자가 없을 때 실패한다. 경로가 스키마에서 풀리는지는 서버를 지을 때 본다
    (`runtime/conditions.py`)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    op: ConditionOp
    value: Any = None
    optional: bool = False


class ToolRule(BaseModel):
    """operationId 하나의 Tool 규칙이다. read_only 가 None 이면 메서드가 정한다(GET·HEAD 는 읽기).
    누가 부를지의 규칙은 여기 없다. 그것은 ToolHive 의 Cedar 가 지킨다."""

    model_config = ConfigDict(extra="forbid")

    read_only: bool | None = None
    conditions: list[ArgCondition] = Field(default_factory=list)


class ToolOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_tool_name(v)


class ToolSelection(BaseModel):
    """backend 하나의 툴 선택·오버라이드·접속 정보와 Tool 규칙이다."""

    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list)
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    endpoint: str | None = None
    auth_kind: Literal["none", "basic", "wsse"] = "none"
    soap: bool = False
    # operationId 마다의 Tool 규칙 (P52 4절)
    rules: dict[str, ToolRule] = Field(default_factory=dict)

    @field_validator("tools")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for t in v:
            if not t:
                raise ValueError("tools entries must be non-empty operationIds")
            seen.setdefault(t, None)
        return list(seen)

    @property
    def mcp_names(self) -> dict[str, str]:
        return {op: ov.name for op, ov in self.tool_overrides.items() if ov.name}

    @property
    def descriptions(self) -> dict[str, str]:
        return {op: ov.description for op, ov in self.tool_overrides.items() if ov.description}


class BackendEntry(ToolSelection):
    """backends 형식 profile.json 의 항목 하나다. `spec` 은 profile.json 과 같은
    디렉터리에 있는 OpenAPI 파일 이름이다 (플랫폼이 openapi-<i>.yaml 로 렌더링).
    `timeout_seconds` 는 그 backend 를 부르는 호출의 타임아웃이고, 없으면(그 칸을 모르는 옛
    플랫폼) spec2openapi 의 기본값이다."""

    spec: str
    timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("spec")
    @classmethod
    def _spec_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("spec must be a non-empty file name")
        return v


class ProfileConfig(ToolSelection):
    """profile.json 이다. 없으면 기본값이라 스펙의 모든 operation 을 노출한다.

    backends 가 비어 있지 않으면 다중 backend 형식이고, 평면 필드(tools 등)는
    쓰이지 않는다. backend 가 하나뿐인 프로필은 플랫폼이 예전 평면 형식으로
    렌더링하므로 기존 경로 그대로 동작한다."""

    backends: list[BackendEntry] = Field(default_factory=list)


def parse_profile(data: Any) -> ProfileConfig:
    try:
        return ProfileConfig.model_validate(data)
    except ValidationError as e:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise ProfileError("invalid_profile", errors) from e


def load_profile(path: str | Path) -> ProfileConfig:
    """path 가 없으면 기본 ProfileConfig 다 (전부 노출)."""
    p = Path(path)
    if not p.exists():
        return ProfileConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProfileError("invalid_profile", f"cannot read {p}: {e}") from e
    return parse_profile(data)
