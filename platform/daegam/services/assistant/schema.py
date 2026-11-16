"""OpenAPI dict 에서 도구 인자 스키마를 뽑는다 (P14 F1a).

도구 하나의 입력은 `{type: object, properties: {<경로>, <쿼리>, <본문 필드>...}, required: [...]}`
로 평평하다. `parameters[].in` 으로 경로·쿼리를 나누고 requestBody 의 `$ref` 는
`components.schemas` 로 재귀 해소한다(순환은 없지만 깊이 상한을 둔다). 본문 필드 이름이
경로·쿼리와 겹치면 `body_` 를 앞에 붙이고 그 대응은 `ToolDef.body_params` 에 남겨 실행기가 되돌린다.

모델 호출마다 도구 정의 전체가 토큰으로 들어가므로 크기를 줄인다. `title`·`examples` 는 전부,
깊이 3 이상의 `description` 은 걷고, `anyOf: [X, null]` 은 X 로 접는다(인자를 빼는 것과 null 은
같다). 표에 있는데 OpenAPI 에 없는 동작은 `CatalogError` 다. 기동 자기 점검이 이것으로 표와 API 의
어긋남을 잡는다.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from daegam.services.assistant.catalog import Catalog, CatalogError, ToolSpec

MAX_REF_DEPTH = 20
# 도구 입력 객체가 깊이 0, 그 properties 가 1, 그 안의 properties 가 2 다. 3 부터 description 을
# 걷는다
DESCRIPTION_DEPTH = 3
STRIP_KEYS = ("title", "examples", "example")
JSON = "application/json"
MULTIPART = "multipart/form-data"


@dataclass(frozen=True)
class ToolDef:
    """모델에 주는 도구 정의 하나와, 실행기가 인자를 요청으로 되돌리는 데 필요한 대응."""

    name: str
    description: str
    parameters: dict[str, Any]
    spec: ToolSpec
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    # 인자 이름 → 본문 필드 이름 (이름이 겹쳐 body_ 를 붙인 경우에만 다르다)
    body_params: dict[str, str] = field(default_factory=dict)
    # application/json | multipart/form-data | None(본문 없음)
    body_media: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """`bind_tools` 가 받는 모양."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def first_sentence(text: str | None) -> str:
    """docstring 의 첫 문장. 첫 문단의 줄을 이어 붙인 뒤 첫 마침표(뒤에 공백 또는 끝)까지다.
    docstring 은 100자에서 줄이 바뀌므로 "첫 줄" 을 글자 그대로 쓰면 문장 조각이 된다."""
    if not text:
        return ""
    paragraph = " ".join(text.strip().split("\n\n", 1)[0].split())
    match = re.match(r".*?\.(?=\s|$)", paragraph)
    return (match.group(0) if match else paragraph)[:200]


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def resolve_refs(schema: Any, components: dict[str, Any], depth: int = 0) -> Any:
    """`$ref` 를 재귀 해소한 깊은 복사본. `allOf: [X]` 와 `anyOf: [X, null]` 은 X 로 접는다."""
    if depth > MAX_REF_DEPTH:
        # depth 는 $ref 홉이 아니라 중첩 단계(properties·items·anyOf 포함)마다 는다. 순환 $ref 는
        # 여기서 잡히고, 정직하게 깊은 스키마도 같은 오류다
        raise CatalogError(
            f"스키마 중첩이 깊이 {MAX_REF_DEPTH} 을 넘는다 (순환 $ref 이거나 지나치게 깊은 스키마)"
        )
    if isinstance(schema, list):
        return [resolve_refs(s, components, depth + 1) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        name = _ref_name(schema["$ref"])
        target = components.get(name)
        if target is None:
            raise CatalogError(f"components.schemas 에 {name!r} 가 없다")
        resolved = resolve_refs(copy.deepcopy(target), components, depth + 1)
        # $ref 옆에 붙은 description·default 는 참조 쪽 설명을 덮는다
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        return {**resolved, **siblings} if isinstance(resolved, dict) else resolved
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("properties", "patternProperties", "$defs"):
            out[key] = {k: resolve_refs(v, components, depth + 1) for k, v in value.items()}
        elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
            out[key] = resolve_refs(value, components, depth + 1)
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            out[key] = [resolve_refs(v, components, depth + 1) for v in value]
        else:
            out[key] = copy.deepcopy(value)
    return _collapse(out)


def _collapse(schema: dict[str, Any]) -> dict[str, Any]:
    """`allOf: [X]` 와 `anyOf: [X, {type: null}]` 을 X 로 접는다. 옆에 붙은 키(description 등)는
    남긴다."""
    for key in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(key)
        if not isinstance(variants, list):
            continue
        real = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
        if len(real) == 1 and isinstance(real[0], dict) and (key == "allOf" or len(variants) == 2):
            rest = {k: v for k, v in schema.items() if k != key}
            return {**real[0], **rest}
    return schema


def clean(schema: Any, depth: int = 0) -> Any:
    """`title`·`examples` 를 전부, 깊이 DESCRIPTION_DEPTH 이상의 `description` 을 걷는다.

    bool 인 `additionalProperties` 도 걷는다. 모델에는 정보가 아니고(모르는 필드는 API 가 422 로
    답해 도구 결과가 된다) 모델마다 하나씩 붙어 있어 부피만 차지한다. 스키마인 것(맵의 값)은
    남긴다. 설명 안의 줄바꿈은 공백 하나로 접는다."""
    if isinstance(schema, list):
        return [clean(s, depth) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in STRIP_KEYS:
            continue
        if key == "additionalProperties" and isinstance(value, bool):
            continue
        if key == "description":
            if depth >= DESCRIPTION_DEPTH:
                continue
            out[key] = " ".join(str(value).split())
        elif key in ("properties", "patternProperties"):
            out[key] = {k: clean(v, depth + 1) for k, v in value.items()}
        elif key in (
            "items",
            "additionalProperties",
            "not",
            "anyOf",
            "oneOf",
            "allOf",
            "prefixItems",
        ):
            out[key] = clean(value, depth)
        else:
            out[key] = value
    return out


def _drop_nested(schema: dict[str, Any], parts: list[str]) -> bool:
    """점으로 이은 경로의 마지막 필드를 뺀다. 배열은 items 로, null 을 함께 받는 anyOf 는 객체나
    배열인 갈래로 내려간다. 경로가 없으면 거짓이다. 비서가 고치지 않는 중첩 필드(P52 의 Tool 규칙)를
    도구 정의에서 빼는 자리다."""
    node: Any = schema
    for i, part in enumerate(parts):
        for _ in range(4):
            if not isinstance(node, dict):
                return False
            options = node.get("anyOf")
            if isinstance(options, list):
                node = next((o for o in options if o.get("type") in ("object", "array")), None)
            elif node.get("type") == "array":
                node = node.get("items")
            else:
                break
        props = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(props, dict) or part not in props:
            return False
        if i == len(parts) - 1:
            del props[part]
            return True
        node = props[part]
    return False


def _body_schema(
    spec: ToolSpec, op: dict[str, Any], components: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """(본문 객체 스키마, media). 손 스키마가 있으면 그것이고, 빈 properties 면 본문이 없다."""
    request_body = op.get("requestBody") or {}
    content = request_body.get("content") or {}
    media = JSON if JSON in content else next(iter(content), None)
    if spec.args_schema is not None:
        schema = copy.deepcopy(spec.args_schema)
        if not schema.get("properties"):
            return None, None
        return schema, media or JSON
    if media is None:
        return None, None
    schema = resolve_refs(content[media].get("schema") or {}, components)
    if not isinstance(schema, dict) or schema.get("type") != "object" or "properties" not in schema:
        raise CatalogError(f"{spec.name}: 본문이 object 가 아닌 동작은 표에 둘 수 없다")
    return schema, media


def _tool_def(spec: ToolSpec, op: dict[str, Any], components: dict[str, Any]) -> ToolDef:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    for p in op.get("parameters") or []:
        name = p["name"]
        where = p.get("in")
        if where not in ("path", "query"):
            continue
        schema = resolve_refs(p.get("schema") or {}, components)
        if p.get("description") and "description" not in schema:
            schema["description"] = p["description"]
        properties[name] = schema
        if where == "path":
            path_params.append(name)
            required.append(name)
        else:
            query_params.append(name)
            if p.get("required"):
                required.append(name)
    body, media = _body_schema(spec, op, components)
    body_params: dict[str, str] = {}
    if body is not None:
        body = copy.deepcopy(body)
        top = {f for f in spec.hidden_fields if "." not in f}
        missing = top - set(body["properties"])
        for path in sorted(set(spec.hidden_fields) - top):
            if not _drop_nested(body, path.split(".")):
                missing.add(path)
        if missing:
            raise CatalogError(f"{spec.name}: 본문에 없는 필드를 뺄 수 없다: {sorted(missing)}")
        body_required = set(body.get("required") or [])
        for prop, sub in body["properties"].items():
            if prop in spec.hidden_fields:
                continue
            arg = prop if prop not in properties else f"body_{prop}"
            properties[arg] = sub
            body_params[arg] = prop
            if prop in body_required:
                required.append(arg)
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    description = spec.description
    # 제안 도구의 라우트 설명("draft 로 저장한다", "전체 교체")은 승인 뒤의 저장을 말하므로 제안을
    # 말하는 표의 설명과 어긋난다. 싣지 않는다 (P48 6.1절)
    doc = "" if spec.proposal else first_sentence(op.get("description"))
    if doc:
        description = f"{description} {doc}"
    return ToolDef(
        name=spec.name,
        description=description,
        parameters=clean(parameters),
        spec=spec,
        path_params=tuple(path_params),
        query_params=tuple(query_params),
        body_params=body_params,
        body_media=media,
    )


def build_tool_schemas(openapi: dict[str, Any], catalog: Catalog) -> list[ToolDef]:
    """표의 도구마다 OpenAPI 에서 인자 스키마를 뽑는다. 표의 동작이 문서에 없으면 CatalogError."""
    components = (openapi.get("components") or {}).get("schemas") or {}
    paths = openapi.get("paths") or {}
    out: list[ToolDef] = []
    for spec in catalog.tools:
        if spec.local:
            assert spec.args_schema is not None  # Catalog 가 검사했다
            out.append(
                ToolDef(
                    name=spec.name,
                    description=spec.description,
                    parameters=clean(copy.deepcopy(spec.args_schema)),
                    spec=spec,
                )
            )
            continue
        method, path = spec.key  # type: ignore[misc]  # local 이 아니면 None 이 아니다
        op = (paths.get(path) or {}).get(method.lower())
        if op is None:
            raise CatalogError(f"표의 동작이 OpenAPI 에 없다: {method} {path} ({spec.name})")
        out.append(_tool_def(spec, op, components))
    return out


def serialized_size(tool_defs: list[ToolDef]) -> int:
    """모델에 넘기는 도구 정의 전체의 JSON 바이트 수."""
    return len(json.dumps([d.to_dict() for d in tool_defs], ensure_ascii=False).encode("utf-8"))


__all__ = [
    "DESCRIPTION_DEPTH",
    "MAX_REF_DEPTH",
    "ToolDef",
    "build_tool_schemas",
    "clean",
    "first_sentence",
    "resolve_refs",
    "serialized_size",
]
