"""Tool 마다 FastMCP 가 짓는 입력 스키마의 인자 목록 (P52 1절).

인자 조건의 경로는 MCP Tool 의 입력 스키마에서 FastMCP 의 이름으로 풀려야 한다. `mcp-runtime` 이
같은 판의 FastMCP(`fastmcp==4.1.2`)로 Tool 을 지으므로, 변환할 때 그 파서
(`parse_openapi_to_http_routes` 의 `flat_param_schema`)로 인자를 한 번 읽어 Backend API 버전에
둔다. 이름이 겹친 경로 인자는 `id__path` 처럼 FastMCP 가 붙인 이름이다.

저장하는 것은 스키마 전체가 아니라 점으로 이은 경로마다의 형식이다. 배열은 그 자리까지만 적고
안으로 내려가지 않는다(배열 안의 경로에는 조건을 둘 수 없다). 형식은 `string`·
`integer`·`number`·`boolean`·`object`·`array`·`unknown` 이고, null 을 함께 받는 형식은 그 형식이며
갈래가 여럿인 것은 `unknown` 이다.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("daegam.pipeline")

# 경로의 깊이와 Tool 하나의 경로 수 상한. 그 너머의 인자에는 조건을 둘 수 없다
MAX_DEPTH = 6
MAX_PATHS = 300
SCALARS = frozenset({"string", "integer", "number", "boolean"})
UNKNOWN = "unknown"


def _deref(node: Any, defs: dict[str, Any]) -> Any:
    for _ in range(16):
        if not isinstance(node, dict) or "$ref" not in node:
            return node
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            return None
        node = defs.get(ref.removeprefix("#/$defs/"))
    return None


def _branches(node: dict[str, Any], defs: dict[str, Any]) -> list[dict[str, Any]]:
    """null 이 아닌 갈래들. anyOf·oneOf 가 없으면 node 하나다."""
    for key in ("anyOf", "oneOf"):
        options = node.get(key)
        if isinstance(options, list):
            out = []
            for opt in options:
                opt = _deref(opt, defs)
                if isinstance(opt, dict) and opt.get("type") != "null":
                    out.append(opt)
            return out
    return [node]


def type_of(node: Any, defs: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """(형식, 객체면 그 스키마). 갈래가 여럿이거나 읽지 못하면 unknown 이다."""
    node = _deref(node, defs)
    if not isinstance(node, dict):
        return UNKNOWN, None
    branches = _branches(node, defs)
    if len(branches) != 1:
        return UNKNOWN, None
    node = branches[0]
    raw = node.get("type")
    kinds = [raw] if isinstance(raw, str) else [t for t in raw or [] if t != "null"]
    if not kinds and isinstance(node.get("properties"), dict):
        kinds = ["object"]
    if len(kinds) != 1 or not isinstance(kinds[0], str):
        return UNKNOWN, None
    kind = kinds[0]
    if kind == "object":
        return kind, node
    return (kind if kind in SCALARS or kind == "array" else UNKNOWN), None


def arguments_of(schema: dict[str, Any]) -> dict[str, str]:
    """입력 스키마 하나의 {점 경로: 형식}. 배열 안으로는 내려가지 않는다."""
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    out: dict[str, str] = {}

    def walk(node: dict[str, Any], prefix: str, depth: int) -> None:
        props = node.get("properties")
        if not isinstance(props, dict):
            return
        for name, sub in props.items():
            if len(out) >= MAX_PATHS:
                return
            path = f"{prefix}{name}"
            kind, obj = type_of(sub, defs)
            out[path] = kind
            if obj is not None and depth < MAX_DEPTH:
                walk(obj, f"{path}.", depth + 1)

    walk(schema, "", 1)
    return out


def tool_arguments(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, str]] | None:
    """(path, 대문자 메서드) 마다의 인자. FastMCP 가 스펙을 읽지 못하면 None 이다. 그 버전에는
    인자 조건을 둘 수 없다."""
    try:
        from fastmcp.utilities.openapi import parse_openapi_to_http_routes

        routes = parse_openapi_to_http_routes(spec)
    except Exception:  # noqa: BLE001 — 인자 목록은 덤이라 변환을 막지 않는다
        logger.warning("could not read tool input schemas with FastMCP", exc_info=True)
        return None
    return {
        (route.path, route.method.upper()): arguments_of(route.flat_param_schema or {})
        for route in routes
    }
