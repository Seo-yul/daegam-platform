"""overrides 모드: MCP Server 의 tool_overrides 이름·설명을 LLM 이 제안한다 (D37).

원본은 정의 리비전의 `backends` 조각이고 초안도 같은 모양이다. 둘 다 dict 이므로 `surface` 와
`projection` 의 뷰 조립(`view_of`)·투영(`apply_leaves`)·`changes`·`apply_changes` 가 그대로 돈다.
여기서 따로 적는 것은 leaf 목록과 판정이다.
`loop.check_doc` 은 초안을 언제나 OpenAPI 로 검사하므로 여기서 쓰면 job 이 영영 done 이 되지 않는다.

뷰는 **고칠 수 있는 자리만** 담는다.

    {"backends": {"<i>": {"<operationId>": {"name": str|null, "description": str|null}}}}

원본 summary·description 은 뷰에 넣지 않고 프롬프트의 참고 블록으로 따로 보낸다. 한 dict 에 섞으면
모델이 참고 자리를 고쳤을 때 투영이 그것을 버리고 finding 을 내서, 고칠 것이 없는데도 되먹임이 돈다.

**키가 `(backend index, operationId)` 다.** 프로필의 backend 둘이 같은 operationId 를 가질 수 있기
때문이다(`platform/tests/test_mcp_servers.py` 의 multi backend 충돌 시험). 평면 map 으로 만들면 그
둘이 한 칸으로 합쳐져 어느 backend 에 심을지 정할 수 없다.
"""

from __future__ import annotations

import copy
from typing import Any

from daegam.services.mcp_spec import (
    ProfileValidationError,
    backend_final_names,
    validate_tools,
)
from daegam.services.spec_generator import projection, surface
from daegam.services.spec_generator.projection import Applied, Leaf
from daegam.services.spec_generator.refine import (
    OUTPUT_JSON,
    SURFACE_PREFIX,
    Finding,
    dumps,
    findings_block,
    instruction_block,
    output_json_finding,
)
from daegam.services.toolnames import FASTMCP_NAME_MAX
from daegam.store.backend_apis import BackendAPIVersion
from daegam.store.mcp_servers import MCPServerSpec

UNKNOWN_PREFIX = "overrides.unknown:"
NAME_PREFIX = "overrides."

# refine 의 같은 이름 프리셋과 뜻은 같고 어휘가 다르다. 저쪽 문구는 OpenAPI 문서를 가리켜서
# ("every summary and description", "schema names") 여기서 쓰면 뷰에 없는 것을 고치라고 시킨다
PRESET_INSTRUCTIONS: dict[str, str] = {
    "enrich_descriptions": (
        "Write a description that helps an AI agent pick the right tool: what it does, when to"
        " use it, what it returns. Keep it to one or two sentences. Give a tool a name override"
        " only when the original operationId is unclear or inconsistent with its siblings."
    ),
    "korean_descriptions": (
        "Write every description in natural Korean. Tool names stay in their original form"
        " because they are identifiers the agent calls."
    ),
}
PRESETS: tuple[str, ...] = tuple(PRESET_INSTRUCTIONS)

OVERRIDES_SYSTEM_PROMPT = f"""You rename and describe MCP tools for one MCP Server.

An MCP Server exposes operations from one or more backends. Each tool may carry an override that
changes the name and the description that the agent sees. The underlying API is not changed.

You receive a JSON object that contains ONLY the override fields you may edit:
  {{"backends": {{"<backend index>": {{"<operationId>": {{"name", "description"}}}}}}}}

Rules (strict):
- Change only the VALUES of the fields you were sent. Never add, remove, rename or reorder keys.
- A null value means there is no override today. You may fill it with a string, or leave it null
  to keep the original. Leaving it null is a valid answer.
- A tool name must match [A-Za-z0-9_] and be at most {FASTMCP_NAME_MAX} characters. It must have
  no leading, trailing or doubled underscores, because the server would otherwise expose a
  different name than the one you chose.
- Tool names must be unique across the WHOLE profile, counting every backend and every tool that
  has no override. Two backends can hold the same operationId; that is why the keys are nested
  under a backend index.
- Do not invent operations. Only the operationIds you were sent exist.
- Reply with ONE JSON object in exactly the same shape as the input. No prose, no Markdown,
  no code fences.
- When you are given findings, fix every one of them and return the fields you were sent again.
"""


# -- 원본 조각 -------------------------------------------------------------------------


def spec_of(revision: Any) -> dict[str, Any]:
    """리비전에서 `MCPServerSpec` 필드만 뽑는다. `revision`·`created_at` 은 리비전마다 달라
    그대로 두면 낡은 채택 비교가 늘 실패한다 (화면의 `Detail.tsx` specOf 와 같은 일이다)."""
    data = revision.model_dump(mode="json")
    for meta in ("revision", "created_at"):
        data.pop(meta, None)
    return data


def fragment(spec: dict[str, Any]) -> dict[str, Any]:
    """비교와 투영의 대상. `backends` 만이다. runtime·bindings 는 제안이 건드리지 않으므로
    job 이 도는 사이 그쪽이 바뀌어도 채택을 막지 않는다 (D37)."""
    return {"backends": copy.deepcopy(spec.get("backends") or [])}


def result(original: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """done 된 overrides job 의 결과 필드. GET 때 계산한다.

    refine 과 달리 `draft_yaml`·`renamed` 는 없다. 대상이 OpenAPI 문서가 아니라 프로필 조각이라
    YAML 로 보여 줄 것이 아니고, operationId 는 바뀌지 않기 때문이다."""
    return {"changes": [c.to_dict() for c in projection.changes(original, draft)]}


def exposed_ops(sel: dict[str, Any], tool_names: list[str]) -> list[str]:
    """이 backend 가 실제로 노출하는 operation. `tools` 가 비면 전부다(서버의 `exposed` 와 같다).
    유일성이 계산되는 집합이자 실제로 나가는 집합이라 뷰도 이것이어야 한다."""
    tools = sel.get("tools") or []
    return list(tools) if tools else list(tool_names)


# -- 뷰 -------------------------------------------------------------------------------


def _guard_escapes(op: str) -> bool:
    """표면 보호가 규칙을 보기 전에 보호로 떨어뜨리는 operationId.

    `Rules.allows` 의 첫 갈래는 경로 세그먼트 중 하나라도 `x-s2o` 이거나 `x-soap` 으로 시작하면
    규칙을 보지 않고 거부한다(`surface.py`). 오버라이드 pointer 에서는 그 세그먼트 자리가
    operationId 라, 그런 이름의 툴을 뷰에 담으면 LLM 이 무엇을 답하든 표면 위반이 된다. 되먹임
    문구가 "protected field" 라 모델이 고칠 방법도 없어 job 이 no_progress 로 접힌다. 아예 뺀다.
    """
    return op == "x-s2o" or op.startswith("x-soap")


def leaves_of(frag: dict[str, Any], names: list[list[str]]) -> list[Leaf]:
    """고칠 수 있는 leaf 전부. names[i] 는 backend i 가 핀한 버전의 tool_names 다. keys 는 뷰 안
    경로 `(backends, i, operationId, name|description)` 이고 pointer 는 조각의 JSON pointer 다."""
    out: list[Leaf] = []
    for i, sel in enumerate(frag.get("backends") or []):
        overrides = sel.get("tool_overrides") or {}
        for op in exposed_ops(sel, names[i] if i < len(names) else []):
            if _guard_escapes(op):
                continue
            ov = overrides.get(op) or {}
            base = surface.join("backends", i, "tool_overrides", op)
            for field in ("name", "description"):
                value = ov.get(field)
                out.append(
                    Leaf(
                        ("backends", str(i), op, field),
                        f"{base}/{field}",
                        value if isinstance(value, str) else None,
                    )
                )
    return out


def extract(frag: dict[str, Any], names: list[list[str]]) -> dict[str, Any]:
    """고칠 수 있는 자리만 뽑은 뷰."""
    return projection.view_of(leaves_of(frag, names))


def context_of(
    frag: dict[str, Any], names: list[list[str]], details: list[dict[str, dict[str, Any]]]
) -> dict[str, Any]:
    """뷰와 같은 키로 묶은 참고 블록. 원본 summary·description 이다. 고치는 자리가 아니다."""
    out: dict[str, Any] = {}
    for i, sel in enumerate(frag.get("backends") or []):
        by_op = details[i] if i < len(details) else {}
        entries: dict[str, Any] = {}
        for op in exposed_ops(sel, names[i] if i < len(names) else []):
            d = by_op.get(op) or {}
            entries[op] = {"summary": d.get("summary"), "description": d.get("description")}
        if entries:
            out[str(i)] = entries
    return {"backends": out}


def details_by_op(versions: list[BackendAPIVersion]) -> list[dict[str, dict[str, Any]]]:
    return [
        {
            d.operation_id: {"summary": d.summary, "description": d.description}
            for d in v.tool_details
        }
        for v in versions
    ]


def tool_names_of(versions: list[BackendAPIVersion]) -> list[list[str]]:
    return [list(v.tool_names) for v in versions]


def versions_from(inp: dict[str, Any]) -> list[BackendAPIVersion]:
    """job 생성 때 담아 둔 버전 문서들.

    runner 에 Backend API 저장소를 배선하지 않으려고 `input.json` 에 싣는다 (D37). 버전은
    불변이라 반복마다 다시 읽을 이유가 없고, 다른 파드가 리스를 물려받아 재개해도 같은 재료로 같은
    판정을 낸다."""
    raw = inp.get("versions")
    if not isinstance(raw, list):
        raise ValueError("overrides input has no versions")
    return [BackendAPIVersion.model_validate(v) for v in raw]


# -- 투영 -----------------------------------------------------------------------------


def apply(frag: dict[str, Any], view: Any, names: list[list[str]]) -> Applied:
    """뷰의 문자열 값을 조각 deep copy 위에 심는다.

    버리는 것이 넷인데 처리가 둘로 갈린다(D37). 원본에 없는 경로와 문자열이 아닌 값은 `unknown` 에
    적어 되먹이고, `None` 과 빈 문자열은 조용히 건너뛴다. 뒤의 둘은 잘못된 답이 아니라 "이 툴은
    그대로 두겠다" 는 정당한 답이고, 되먹이면 모델이 다음에도 같은 답을 내 no_progress 로 접힌다.
    이 규칙은 refine 과 같아서 `projection.apply_leaves` 한 곳에 있다."""
    return projection.apply_leaves(frag, view, leaves_of(frag, names), before_set=_set_override)


def _set_override(draft: dict[str, Any], leaf: Leaf) -> None:
    """`set_at` 은 부모가 있어야 하므로 오버라이드 항목이 없던 operation 은 빈 dict 를 먼저 만든다.
    그 자리가 통째로 `added` 라서 채택 단위도 항목 하나가 된다 (D37)."""
    _backends, index, op, _field = leaf.keys
    sel = draft["backends"][int(index)]
    overrides = sel.setdefault("tool_overrides", {})
    if op not in overrides:
        overrides[op] = {}


# -- 판정 -----------------------------------------------------------------------------


def unknown_finding(pointer: str) -> Finding:
    return {
        "id": UNKNOWN_PREFIX + pointer,
        "message": "this field was not sent to you; do not add keys or non-string values",
        "location": pointer,
    }


def override_surface_finding(v: surface.Violation) -> Finding:
    """refine 의 문구는 "only descriptions may change" 라 이름도 바꾸는 이 모드에 맞지 않는다."""
    return {
        "id": SURFACE_PREFIX + v.pointer,
        "message": f"protected field {v.kind}; only tool_overrides name and description may change",
        "location": v.pointer,
    }


def _name_finding(e: ProfileValidationError) -> Finding:
    """`ProfileValidationError` 하나를 되먹임 항목으로.

    대상이 실리는 자리가 코드마다 다르다. `invalid_tool_name` 은 `operation` 하나이고
    `unknown_tools`·`duplicate_tools` 는 `unknown` 목록이다. id 에 대상을 실어야 두 반복의 오류가
    같은지 다른지를 no_progress 판정이 가릴 수 있다."""
    index = e.extra.get("backend_index")
    ops = [str(o) for o in (e.extra.get("unknown") or [])]
    where = str(e.extra.get("operation") or e.extra.get("name") or "")
    if not where and ops:
        where = ",".join(sorted(ops))
    location = ""
    if index is not None:
        if where and not ops:
            location = surface.join("backends", int(index), "tool_overrides", where)
        elif len(ops) == 1:
            location = surface.join("backends", int(index), "tool_overrides", ops[0])
        else:
            location = surface.join("backends", int(index))
    return {
        "id": f"{NAME_PREFIX}{e.code}:{index}/{where}",
        "message": e.detail,
        "location": location,
    }


def check_names(draft: dict[str, Any], versions: list[BackendAPIVersion]) -> list[Finding]:
    """이름 규칙 전부를 건다. 나오는 코드는 unknown_tools·duplicate_tools·invalid_tool_name·
    duplicate_tool_name 넷이다.

    backend 별 검사를 직접 도는 것은 `validate_tools` 가 첫 문제에서 바로 던지기 때문이다. 그대로
    부르면 되먹임이 한 번에 하나씩만 나간다. 프로필 전체 겹침만 `validate_tools` 에 맡기고, 그쪽은
    conflicts 로 한꺼번에 준다."""
    try:
        spec = MCPServerSpec.model_validate(draft)
    except Exception as e:  # pydantic ValidationError. finding 으로 바꿔야 판정이 안 죽는다
        return [{"id": f"{NAME_PREFIX}invalid_shape", "message": str(e)[:500], "location": ""}]

    out: list[Finding] = []
    for i, sel in enumerate(spec.backends):
        if i >= len(versions):
            continue
        try:
            backend_final_names(sel, versions[i], index=i)
        except ProfileValidationError as e:
            out.append(_name_finding(e))
    if out:
        return out
    try:
        validate_tools(spec, list(versions))
    except ProfileValidationError as e:
        conflicts = e.extra.get("conflicts") or []
        if not conflicts:
            return [_name_finding(e)]
        return [
            {
                "id": f"{NAME_PREFIX}{e.code}:{c.get('name')}",
                "message": f"tool name {c.get('name')!r} is used by "
                + ", ".join(
                    f"backend {u.get('backend_index')} {u.get('operation_id')}"
                    for u in c.get("uses") or []
                )
                + ". give all but one a different override name",
                "location": "",
            }
            for c in conflicts
        ]
    return out


def findings_for(
    original: dict[str, Any],
    draft: dict[str, Any],
    versions: list[BackendAPIVersion],
    *,
    extra: list[Finding],
) -> list[Finding]:
    """초안 하나의 되먹임 항목 전부. 표면 위반이 먼저, 그다음이 이름 규칙이다."""
    out = list(extra)
    out.extend(
        override_surface_finding(v)
        for v in surface.guard(original, draft, rules=surface.ALLOW_OVERRIDES)
    )
    out.extend(check_names(draft, versions))
    return out


def finding_from_id(failed_id: str) -> Finding | None:
    """저장된 failed_ids 에서 되살린다. 표면·이름 항목은 초안을 다시 판정하면 나오므로 응답·투영
    단계의 것만 만든다 (refine 의 같은 이름 함수와 짝이고, 재개 때 `loop` 가 부른다)."""
    if failed_id.startswith(UNKNOWN_PREFIX):
        return unknown_finding(failed_id[len(UNKNOWN_PREFIX) :])
    if failed_id == OUTPUT_JSON:
        return output_json_finding()
    return None


# -- 프롬프트 --------------------------------------------------------------------------


def _instruction_block(presets: list[str], instruction: str | None) -> str:
    return instruction_block(PRESET_INSTRUCTIONS, presets, instruction)


def build_first_prompt(
    view: dict[str, Any],
    context: dict[str, Any],
    *,
    presets: list[str],
    instruction: str | None,
) -> str:
    return (
        "Name and describe the tools of this MCP Server.\n\n"
        + _instruction_block(presets, instruction)
        + "Original names and descriptions from the backend (read-only, for reference):\n<<<\n"
        + dumps(context)
        + "\n>>>\n\nOverride fields you may edit (JSON):\n<<<\n"
        + dumps(view)
        + "\n>>>\n\nReply with the override JSON object only, same shape, same keys."
    )


def build_feedback_prompt(findings: list[Finding], view: dict[str, Any]) -> str:
    return (
        "Your previous answer did not pass validation.\n\n"
        "Findings to fix (every one of them):\n"
        + findings_block(findings)
        + "\n\nCurrent override values (JSON):\n<<<\n"
        + dumps(view)
        + "\n>>>\n\nReply with the override JSON object only, same shape, same keys."
        " Do not add keys."
    )
