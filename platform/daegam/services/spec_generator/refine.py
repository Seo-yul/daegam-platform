"""refine 모드의 프롬프트·프리셋·묶음(batch)·되먹임 대상 (D25).

LLM 에게는 `projection.extract` 가 만든 뷰만 보낸다. 뷰의 직렬화 크기가 `REFINE_VIEW_MAX_BYTES`
(출력 16k 토큰 환산) 를 넘으면 unit(info·operation·schema) 을 `REFINE_BATCH_OPS` 개씩 묶어 순서대로
따로 부른다. 되먹임에는 실패 항목과 그 항목이 가리키는 unit 의 현재 값만 넣는다. 실패 항목을 unit
으로 못 옮기면(예: 응답이 JSON 이 아니었다) 묶음 전체를 다시 넣는다.
"""

from __future__ import annotations

import datetime
import functools
import json
from collections.abc import Callable
from typing import Any, Literal, TypedDict, get_args

from spec2openapi import dump_spec

from daegam import yamlsafe
from daegam.pipeline.convert import iter_operations
from daegam.services.spec_generator import projection, surface
from daegam.services.spec_generator.projection import Unit

REFINE_VIEW_MAX_BYTES = 40 * 1024
REFINE_BATCH_OPS = 25
INSTRUCTION_MAX_CHARS = 200

# 라우터가 JobRequest.presets 를 이 Literal 로 검증한다. 프롬프트 문구 표(PRESET_INSTRUCTIONS)와
# 이름이 어긋나면 instruction_block 이 그 프리셋을 조용히 빼므로 둘을 시험이 맞춰 본다
Preset = Literal[
    "enrich_descriptions",
    "korean_descriptions",
    "fill_parameter_docs",
    "normalize_operation_ids",
]
PRESETS: tuple[str, ...] = get_args(Preset)
RENAME_PRESET = "normalize_operation_ids"

PRESET_INSTRUCTIONS: dict[str, str] = {
    "enrich_descriptions": (
        "Make every summary and description clear and specific for an AI agent that must choose"
        " and call the right tool: what the operation does, when to use it, what it returns."
        " Keep them concise (one to three sentences)."
    ),
    "korean_descriptions": (
        "Write every summary and description in natural Korean. Keep identifiers, parameter"
        " names, schema names and technical terms in their original form."
    ),
    "fill_parameter_docs": (
        "Give every parameter and every schema property whose description is null or empty a"
        " concise description of its meaning, format and constraints as implied by the document."
    ),
    "normalize_operation_ids": (
        "Rename operationId values to a consistent lowerCamelCase verb-noun form (listPets,"
        " getPetById, createOrder) using only [A-Za-z0-9_]. Every operationId must stay unique"
        " across the document. Change the operationId field only for this purpose."
    ),
}

REFINE_SYSTEM_PROMPT = """You improve the documentation fields of an existing OpenAPI document that
is exposed as MCP tools.

You receive a JSON object that contains ONLY the fields you may edit:
- "info": {"description"}
- "operations": {<operationId>: {"summary", "description",
  "parameters": {<name>: <description>}, "operationId"?}}
- "components": {<schemaName>: {"description",
  "properties": {<name>: {"description", ...}}, "items": {...}}}

Rules (strict):
- Change only the VALUES of the fields you were sent. Never add, remove, rename or reorder keys.
- A null value means the field is empty in the document; you may fill it with a string, or leave
  it null.
- components schemas are shared by several operations: describe what the schema is, not one
  caller's use of it.
- Do not invent facts (parameters, behaviour, examples) that the document does not imply.
- Reply with ONE JSON object in exactly the same shape as the input. No prose, no Markdown,
  no code fences.
- When you are given findings, fix every one of them and return the fields you were sent again.
"""

UNKNOWN_PREFIX = "refine.unknown:"
SURFACE_PREFIX = "surface.changed:"
OUTPUT_JSON = "output.json"


class Finding(TypedDict):
    """되먹임 항목 하나. 모듈 셋(loop·refine·surface)을 건너다니는 계약이라 키를 여기 적는다.

    generate 와 refine 이 같은 모양을 쓴다. 만드는 곳은 `loop._findings_from`·`loop.unparsable`
    과 이 파일의 `unknown_finding`·`surface_finding`·`output_json_finding`·`finding_from_id`
    이고, 읽는 곳은 `findings_block`(세 모드의 되먹임 프롬프트)과 `units_for_findings` 다.
    """

    id: str
    """verify 코드이거나 `surface.changed:`·`refine.unknown:` 접두를 단 pointer. job 의
    `failed_ids` 로 저장됐다가 재개 때 `finding_from_id` 가 되살린다."""
    message: str
    """프롬프트에 그대로 실리는 한 줄."""
    location: str
    """문서 안의 자리. 모르면 빈 문자열이라 키는 항상 있다."""


# -- 원본 -----------------------------------------------------------------------------


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def spec_from_yaml(body: bytes) -> dict[str, Any]:
    """저장된 openapi.yaml 을 JSON 호환 dict 로. 키 순서는 그대로다. YAML 타임스탬프는 문자열로."""
    doc = yamlsafe.safe_load(body)
    if not isinstance(doc, dict):
        raise ValueError("spec body is not a mapping")
    return json.loads(json.dumps(doc, ensure_ascii=False, default=_json_default))


def dumps(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1)


def result(original: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """done 된 refine job 의 결과 필드 {changes, draft_yaml, renamed}. GET 때 계산한다."""
    return {
        "changes": [c.to_dict() for c in projection.changes(original, draft)],
        "draft_yaml": dump_spec(draft, fmt="yaml"),
        "renamed": projection.renamed(original, draft),
    }


# -- 뷰·묶음 ---------------------------------------------------------------------------


def view_bytes(spec: dict[str, Any], *, rename: bool) -> int:
    return len(dumps(projection.extract(spec, rename=rename)).encode("utf-8"))


def split_batches(
    spec: dict[str, Any],
    *,
    rename: bool,
    max_bytes: int = REFINE_VIEW_MAX_BYTES,
    batch_ops: int = REFINE_BATCH_OPS,
) -> list[list[Unit]]:
    """뷰가 max_bytes 안이면 묶음 하나(unit 전부). 넘으면 unit 을 batch_ops 개씩 순서대로 묶는다."""
    units = projection.units_of(spec)
    if view_bytes(spec, rename=rename) <= max_bytes:
        return [units]
    size = max(1, batch_ops)
    return [units[i : i + size] for i in range(0, len(units), size)] or [units]


# -- 프롬프트 --------------------------------------------------------------------------


def instruction_block(
    preset_instructions: dict[str, str], presets: list[str], instruction: str | None
) -> str:
    """프리셋과 자유 지시 한 줄을 프롬프트 머리의 Instructions 블록으로.

    다듬기(D25)와 오버라이드 제안(D37)이 프리셋 표만 다르고 나머지는 같아 여기 한 번만 적는다.
    자유 지시의 상한도 여기서만 건다. 두 벌로 두었을 때 한쪽이 상한을 빠뜨린 적이 있다. 라우터가
    `JobRequest` 에서 같은 상한을 먼저 걸지만(`api/spec_generator`), 프롬프트를 만드는 자리가
    스스로 지키지 않으면 그 검사를 지나는 길이 하나 생기는 날 조용히 넘어간다."""
    lines = [preset_instructions[p] for p in presets if p in preset_instructions]
    if instruction and instruction.strip():
        lines.append(instruction.strip()[:INSTRUCTION_MAX_CHARS])
    if not lines:
        return ""
    return "Instructions:\n" + "\n".join(f"- {line}" for line in lines) + "\n\n"


def _instruction_block(presets: list[str], instruction: str | None) -> str:
    return instruction_block(PRESET_INSTRUCTIONS, presets, instruction)


def build_first_prompt(
    view: dict[str, Any],
    *,
    presets: list[str],
    instruction: str | None,
    batch: tuple[int, int] = (1, 1),
) -> str:
    k, m = batch
    part = f" (part {k} of {m}; other parts are handled separately)" if m > 1 else ""
    return (
        f"Improve the documentation fields below{part}.\n\n"
        + _instruction_block(presets, instruction)
        + "Fields (JSON):\n<<<\n"
        + dumps(view)
        + "\n>>>\n\nReply with the JSON object only, same shape, same keys."
    )


def findings_block(findings: list[Finding]) -> str:
    """되먹임 항목을 `- [id] at location: message` 줄로. 세 모드(generate·refine·overrides)의
    되먹임 프롬프트가 같은 줄 모양을 LLM 에 보내도록 여기 한 번만 적는다."""
    lines = []
    for f in findings:
        loc = f" at {f['location']}" if f["location"] else ""
        lines.append(f"- [{f['id']}]{loc}: {f['message']}")
    return "\n".join(lines)


def build_feedback_prompt(findings: list[Finding], view: dict[str, Any]) -> str:
    return (
        "Your previous answer did not pass verification.\n\n"
        "Findings to fix (every one of them):\n"
        + findings_block(findings)
        + "\n\nCurrent values of the fields to fix (JSON):\n<<<\n"
        + dumps(view)
        + "\n>>>\n\nReply with the JSON object only, same shape, same keys. Do not add keys."
    )


# -- 실패 항목 -------------------------------------------------------------------------


def unknown_finding(pointer: str) -> Finding:
    return {
        "id": UNKNOWN_PREFIX + pointer,
        "message": "this key does not exist in the document; do not add keys",
        "location": pointer,
    }


def surface_finding(v: surface.Violation) -> Finding:
    return {
        "id": SURFACE_PREFIX + v.pointer,
        "message": f"protected field {v.kind}; only descriptions may change",
        "location": v.pointer,
    }


def output_json_finding(reason: str = "") -> Finding:
    """응답이 JSON 객체가 아니었다는 항목. 이유는 저장하지 않으므로 재개 때는 이유 없이 되살린다."""
    message = "response was not a JSON object"
    return {
        "id": OUTPUT_JSON,
        "message": f"{message}: {reason}" if reason else message,
        "location": "",
    }


def finding_from_id(failed_id: str) -> Finding | None:
    """저장된 failed_ids 에서 되먹임 항목을 되살린다. verify 항목은 초안을 다시 검사하면 나오므로
    여기서는 응답·투영 단계의 것만 만든다."""
    if failed_id.startswith(UNKNOWN_PREFIX):
        return unknown_finding(failed_id[len(UNKNOWN_PREFIX) :])
    if failed_id == OUTPUT_JSON:
        return output_json_finding()
    return None


OpIndex = dict[str, tuple[str, str]]


def _op_index(spec: dict[str, Any]) -> OpIndex:
    """뷰 키 → (path, method). `projection.op_keys` 를 거꾸로 본 것이고, 키가 겹치면 문서 순서로
    첫 operation 이다."""
    index: OpIndex = {}
    for pm, key in projection.op_keys(spec).items():
        index.setdefault(key, pm)
    return index


def _unit_of_pointer(segs: tuple[str, ...], op_index: Callable[[], OpIndex]) -> Unit | None:
    """문서 pointer(surface) 또는 뷰 pointer(refine.unknown) 가 속한 unit."""
    if not segs:
        return None
    if segs[0] == "info":
        return ("info",)
    if segs[0] == "paths" and len(segs) >= 3:
        return ("op", segs[1], segs[2])
    if segs[0] == "components" and len(segs) >= 3 and segs[1] == "schemas":
        return ("schema", segs[2])
    if segs[0] == "components" and len(segs) >= 2:
        return ("schema", segs[1])
    if segs[0] == "operations" and len(segs) >= 2:
        found = op_index().get(segs[1])
        return ("op", *found) if found is not None else None
    return None


def _unit_of_location(spec: dict[str, Any], location: str) -> Unit | None:
    """verify 항목의 location(`paths.{path}.{method}[...]`, `components.schemas.{name}[...]`)."""
    for path, method, _op in iter_operations(spec):
        prefix = f"paths.{path}.{method}"
        if location == prefix or location.startswith(prefix + "."):
            return ("op", path, method)
    schemas = (spec.get("components") or {}).get("schemas")
    if isinstance(schemas, dict):
        for name in schemas:
            prefix = f"components.schemas.{name}"
            if location == prefix or location.startswith(prefix + "."):
                return ("schema", str(name))
    return None


def units_for_findings(
    spec: dict[str, Any], findings: list[Finding], batch: list[Unit]
) -> list[Unit]:
    """되먹임에 넣을 unit. 하나라도 unit 으로 못 옮기는 항목이 있으면 묶음 전체다."""
    # 뷰 키 색인은 `/operations/...` 항목을 처음 만났을 때 한 번만 만든다
    op_index = functools.cache(lambda: _op_index(spec))
    out: list[Unit] = []
    for f in findings:
        fid = f["id"]
        unit: Unit | None = None
        if fid.startswith(SURFACE_PREFIX) or fid.startswith(UNKNOWN_PREFIX):
            pointer = fid.split(":", 1)[1]
            try:
                unit = _unit_of_pointer(surface.segments_of(pointer), op_index)
            except ValueError:
                unit = None
        elif f["location"]:
            unit = _unit_of_location(spec, f["location"])
        if unit is None:
            return list(batch)
        if unit not in out:
            out.append(unit)
    return out or list(batch)
