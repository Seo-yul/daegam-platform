"""MCP Server 정의의 검증과 profile.json 렌더링. 순수 함수만 있다."""

from __future__ import annotations

from typing import Any

from daegam.services.advertised_names import exposed_pairs
from daegam.services.toolnames import InvalidToolName, validate_tool_name
from daegam.store.backend_apis import BackendAPI, BackendAPIVersion
from daegam.store.mcp_servers import BackendSelection, MCPServerRevision, MCPServerSpec


class ProfileValidationError(Exception):
    def __init__(self, code: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, **self.extra}


def backend_final_names(
    sel: BackendSelection, version: BackendAPIVersion, *, index: int = 0
) -> list[tuple[str, str]]:
    """backend 하나의 tools ⊆ tool_names, 오버라이드 키·이름 규칙을 검증하고
    (노출 이름, operationId) 목록을 돌려준다. 이름 중복 검사는 validate_tools 가
    정의 전체에서 한다."""
    ctx = {"backend_profile_id": sel.backend_profile_id, "backend_index": index}
    available = list(version.tool_names)
    avail_set = set(available)
    unknown = [t for t in sel.tools if t not in avail_set]
    if unknown:
        raise ProfileValidationError(
            "unknown_tools",
            f"tools not in backend api version {version.version}: {', '.join(unknown)}",
            unknown=unknown,
            available=sorted(avail_set),
            **ctx,
        )
    if len(set(sel.tools)) != len(sel.tools):
        raise ProfileValidationError("duplicate_tools", "tools contains duplicates", **ctx)
    unknown_ov = [op for op in sel.tool_overrides if op not in avail_set]
    if unknown_ov:
        raise ProfileValidationError(
            "unknown_tools",
            f"tool_overrides refer to unknown operations: {', '.join(unknown_ov)}",
            unknown=unknown_ov,
            available=sorted(avail_set),
            **ctx,
        )
    # 이름 오버라이드는 노출 여부와 관계없이 전부 규칙을 지켜야 한다. 여럿이 틀렸으면 오버라이드
    # 순서로 첫 것을 알린다
    for op, ov in sel.tool_overrides.items():
        if ov.name is not None:
            try:
                validate_tool_name(ov.name)
            except InvalidToolName as e:
                raise ProfileValidationError(
                    "invalid_tool_name", str(e), operation=op, **ctx
                ) from e
    return exposed_pairs(sel, available)


def validate_tools(spec: MCPServerSpec, versions: list[BackendAPIVersion]) -> None:
    """backend 별 검증에 더해, 오버라이드 적용 후 최종 MCP 툴 이름이 정의 전체에서
    유일한지 확인한다. 충돌하면 duplicate_tool_name 에 충돌 목록(conflicts)을 담는다.

    backend 와 핀한 버전은 부르는 쪽이 짝지어 넘긴다. 어긋나면 assert 가 아니라 이 오류로 말한다.
    `python -O` 로 돌면 assert 가 사라져 아래 zip(strict=True) 의 ValueError 가 되는데, 그것은
    ProfileValidationError 만 잡는 자리(스펙 제안 판정)를 통째로 죽인다."""
    if len(spec.backends) != len(versions):
        raise ProfileValidationError(
            "backend_version_mismatch",
            f"backend count {len(spec.backends)} does not match "
            f"pinned version count {len(versions)}",
        )
    uses: dict[str, list[dict[str, Any]]] = {}
    for i, (sel, version) in enumerate(zip(spec.backends, versions, strict=True)):
        for name, op in backend_final_names(sel, version, index=i):
            uses.setdefault(name, []).append(
                {
                    "backend_profile_id": sel.backend_profile_id,
                    "backend_index": i,
                    "operation_id": op,
                }
            )
    conflicts = [{"name": n, "uses": u} for n, u in uses.items() if len(u) > 1]
    if conflicts:
        names = ", ".join(c["name"] for c in conflicts)
        raise ProfileValidationError(
            "duplicate_tool_name",
            f"tool names must be unique across the profile after overrides: {names}",
            conflicts=conflicts,
        )


def profile_rules(sel: BackendSelection) -> dict[str, Any]:
    """backend 하나의 profile.json `rules` 다 (P52 4절). 읽기 전용 표시나 인자 조건이 있는
    operationId 만 싣는다. 누가 부를지의 규칙은 Cedar 가 지키므로 싣지 않는다. 그래서 그런 규칙이
    없는 리비전의 profile.json 은 이 칸이 생기기 전과 같다."""
    out: dict[str, Any] = {}
    for op, rule in sorted(sel.tool_rules.items()):
        if rule.read_only is None and not rule.conditions:
            continue
        out[op] = {
            "read_only": rule.read_only,
            "conditions": [c.model_dump(mode="json") for c in rule.conditions],
        }
    return out


def _backend_entry(
    sel: BackendSelection, backend_api: BackendAPI, version: BackendAPIVersion
) -> dict[str, Any]:
    soap = version.kind == "soap"
    if not soap and backend_api.auth == "wsse":
        raise ProfileValidationError(
            "invalid_auth", "auth=wsse is only valid for SOAP backends (WS-Security)"
        )
    entry: dict[str, Any] = {
        "tools": list(sel.tools),
        "tool_overrides": {
            op: {k: v for k, v in ov.model_dump().items() if v is not None}
            for op, ov in sel.tool_overrides.items()
        },
        "endpoint": backend_api.endpoint,
        "auth_kind": backend_api.auth,
        "soap": soap,
    }
    rules = profile_rules(sel)
    if rules:
        entry["rules"] = rules
    return entry


def render_profile_json(
    rev: MCPServerRevision | MCPServerSpec,
    backends: list[tuple[BackendAPI, BackendAPIVersion]],
) -> dict[str, Any]:
    """initContainer 가 받아가는 profile.json. 비밀은 없다.

    backend 가 하나면 예전 평면 형식 그대로다 (구 mcp-runtime 이미지와 호환).
    둘 이상이면 {"backends": [...]} 형식이고, 항목마다 `spec` 이 profile.json 과
    같은 디렉터리의 스펙 파일 이름(openapi-<i>.yaml)이고 `timeout_seconds` 가 그 backend 의 호출
    타임아웃이다. 그 칸을 모르는 옛 런타임 이미지는 무시하고 기본값으로 부른다. 읽기 전용 표시와
    인자 조건이 있으면 그 backend 의 `rules` 가 실린다(`profile_rules`, P52 4절). 옛 이미지는 그
    칸을 버리므로 조건을 지키지 않고, 그래서 조건이 있는 Tool 은 그 서버의 모든 파드가 조건을
    지킨다고 알 때까지 Cedar 가 닫는다(`services/condition_records.py`)."""
    assert len(rev.backends) == len(backends)
    entries = [
        _backend_entry(sel, bp, version)
        for sel, (bp, version) in zip(rev.backends, backends, strict=True)
    ]
    if len(entries) == 1:
        return entries[0]
    for i, (entry, sel) in enumerate(zip(entries, rev.backends, strict=True)):
        entry["spec"] = f"openapi-{i}.yaml"
        # backend 마다의 호출 타임아웃 (P50 7절). backend 하나면 env 가 나른다
        entry["timeout_seconds"] = sel.timeout_seconds
    return {"backends": entries}


def scope_for(server_id: str) -> str:
    """스펙 읽기 토큰의 scope. 이름은 옛 것을 그대로 두고 값은 MCP Server 의 id 다."""
    return f"mcp-profile:{server_id}"
