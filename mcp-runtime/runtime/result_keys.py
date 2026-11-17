"""Tool 설명에 붙이는 결과 키 한 줄 (P56 6.9절, F6).

코드 모드의 스크립트는 결과를 읽기 전에 그 키를 알아야 하는데, 모델이 결과의 꼴을 아는 곳은
Tool 설명뿐이다(`mcp-runtime` 은 outputSchema 를 걷고, 런타임은 설명과 입력 스키마만 모델에
넘긴다). 그래서 작업의 JSON 성공 응답 스키마에서 키를 읽어
`Returns {id, status (open or closed), items: [{sku, qty}]}.` 꼴의 한 줄을 만든다. F0.8 이 잰
손글씨 줄과 같은 꼴이다. JSON 성공 응답 스키마가 없거나 키를 하나도 말하지 않으면 줄이 없다.

`runtime/conditions.py` 의 걷기를 쓰지 않는다. 그것은 입력 스키마의 인자 경로를 읽어 `allOf` 를
합치지 않고 `type` 이 없는 노드의 꼴을 `properties`·`items` 로 미루지 않는데, 응답 스키마의 키는
`allOf` 로 합쳐 짓는 일이 흔하기 때문이다. 스키마는 서버 주인이 올린 것이라 걷기는 노드 수
(`MAX_NODES`)와 이미 합친 `allOf` 갈래로 끝을 정하고, 무엇이 잘못되어도 설명을 그대로 둔다
(`with_keys_line`). 키 이름과 enum 값의 줄바꿈과 제어 문자는 공백으로 바꾼다.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

log = logging.getLogger("daegam.mcp-runtime")

# 펼쳐 보이는 깊이. 1 은 맨 위의 키만, 2 는 그 아래 객체와 배열 원소의 키까지다
MAX_DEPTH = 2
# 줄의 글자 수 상한. 넘으면 맨 위의 키만 보이고, 그래도 넘으면 키를 줄인다
MAX_CHARS = 400
# enum 값을 보이는 최대 개수. 넘으면 값을 보이지 않는다
MAX_ENUM = 8
# `$ref` 를 따라가는 최대 횟수
MAX_HOPS = 8
# 줄 하나가 걷는 스키마 노드의 상한. 넘으면 줄을 만들지 않는다
MAX_NODES = 500
# 사람이 바로 읽는 꼴로 바꾸는 format. 나머지는 이름 그대로 보인다
FORMATS = {"date": "YYYY-MM-DD"}
# FastMCP 가 `#/$defs/<이름>` 으로 고쳐 쓴 안쪽 참조의 앞
DEFS_PREFIX = "#/$defs/"


class _OverBudget(Exception):
    """줄 하나가 `MAX_NODES` 를 넘게 걸었다."""


def keys_line(route: Any) -> str | None:
    """작업 하나의 결과 키 줄. 줄이 없으면 None 이다.

    `route` 는 FastMCP 의 `HTTPRoute` 다. 응답 스키마의 맨 위 `$ref` 는 FastMCP 가 이미 풀었고,
    안쪽의 `#/$defs/<이름>` 은 `route.response_schemas` 에서 찾는다."""
    schema = _success_schema(getattr(route, "responses", None) or {})
    if schema is None:
        return None
    defs = getattr(route, "response_schemas", None) or {}
    line = None
    for depth in (MAX_DEPTH, 1):
        try:
            rendered = _Walk(defs, depth).top(schema)
        except _OverBudget:
            return None
        if rendered is None:
            return None
        line = f"Returns {rendered}."
        if len(line) <= MAX_CHARS:
            return line
    return _clip(line) if line else None


def with_keys_line(description: str | None, route: Any) -> str | None:
    """설명 뒤에 결과 키 줄을 붙인다. 작업의 글이 먼저 오고, 줄이 없으면 설명을 그대로 둔다.
    스키마가 무엇이든 서버를 띄우는 길을 막지 않으므로, 줄을 짓다 실패하면 설명을 그대로 둔다."""
    try:
        line = keys_line(route)
    except Exception:  # noqa: BLE001 - 서버 주인의 스키마가 무엇이든 줄 하나 때문에 멈추지 않는다
        log.warning(
            "could not build the result keys line of %s", getattr(route, "operation_id", None)
        )
        return description
    if line is None:
        return description
    return f"{description}\n{line}" if description else line


def _success_schema(responses: dict[str, Any]) -> dict[str, Any] | None:
    """JSON 성공 응답의 스키마. FastMCP 는 성공 응답 가운데 하나만 `route.responses` 에 남기므로
    (200 이 있으면 그것) 그 항목 하나를 읽는다. 2xx 가 아닌 항목은 보지 않는다."""
    for code, response in responses.items():
        if not str(code).startswith("2"):
            continue
        for media, schema in (getattr(response, "content_schema", None) or {}).items():
            if _is_json(media) and isinstance(schema, dict) and schema:
                return schema
    return None


def _is_json(media: str) -> bool:
    base = media.split(";", 1)[0].strip().lower()
    return base == "application/json" or base.endswith("+json")


def _clean(value: Any) -> str:
    """줄에 싣는 이름과 값. 줄바꿈과 제어 문자를 공백으로 바꾸고 공백을 하나로 줄인다. 설명의
    줄이 늘거나 모델에 보이는 글이 지어지지 않게 하려는 것이다."""
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in str(value))
    return " ".join(text.split())


class _Walk:
    """줄 하나를 짓는 걷기. 걸은 노드를 세어 `MAX_NODES` 를 넘으면 멈춘다."""

    def __init__(self, defs: dict[str, Any], depth: int):
        self.defs = defs
        self.depth = depth
        self.left = MAX_NODES

    def _tick(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise _OverBudget

    def top(self, schema: dict[str, Any]) -> str | None:
        """맨 위 스키마의 꼴. 객체면 `{…}`, 객체 배열이면 `a list of {…}` 이다. 키가 없으면 None."""
        schema = self._resolve(schema)
        if self._kind(schema) == "array":
            inner = self._object(self._resolve(schema.get("items")), 1)
            return f"a list of {inner}" if inner else None
        return self._object(schema, 1)

    def _object(self, schema: dict[str, Any], level: int) -> str | None:
        """객체 스키마의 `{키, 키 (값), 키: {…}}`. 속성이 없으면 None 이다."""
        props = self._properties(schema)
        if not props:
            return None
        return "{" + ", ".join(self._key(name, sub, level) for name, sub in props.items()) + "}"

    def _key(self, name: str, schema: Any, level: int) -> str:
        """키 하나. 아래 깊이가 남으면 객체와 객체 배열의 키를 펼치고, 아니면 enum 값이나
        format 을 보인다."""
        self._tick()
        label = _clean(name)
        schema = self._resolve(schema)
        if level < self.depth:
            kind = self._kind(schema)
            if kind == "object":
                inner = self._object(schema, level + 1)
                if inner:
                    return f"{label}: {inner}"
            elif kind == "array":
                inner = self._object(self._resolve(schema.get("items")), level + 1)
                if inner:
                    return f"{label}: [{inner}]"
        note = _note(schema)
        return f"{label} ({note})" if note else label

    def _properties(self, schema: dict[str, Any]) -> dict[str, Any]:
        """객체의 속성. `allOf` 의 갈래를 합치고, null 을 뺀 갈래가 하나인 `anyOf`·`oneOf` 는
        그 갈래를 본다. 이미 합친 스키마(같은 객체)는 다시 걷지 않아, 자기를 부르는 `allOf` 도
        한 번만 합친다."""
        props: dict[str, Any] = {}
        seen: set[int] = set()
        stack = [schema]
        while stack:
            part = self._resolve(stack.pop())
            if id(part) in seen:
                continue
            seen.add(id(part))
            self._tick()
            stack.extend(reversed(_branches(part)))
            own = part.get("properties")
            if isinstance(own, dict):
                for name, sub in own.items():
                    props.setdefault(name, sub)
            subs = part.get("allOf")
            if isinstance(subs, list):
                stack.extend(reversed([s for s in subs if isinstance(s, dict)]))
        return props

    def _kind(self, schema: dict[str, Any]) -> str | None:
        """`type` 이다. `["object", "null"]` 같은 목록은 null 을 뺀 하나이고, 없으면 속성이나
        items 로 미룬다."""
        kind = schema.get("type")
        if isinstance(kind, list):
            rest = [k for k in kind if k != "null"]
            kind = rest[0] if len(rest) == 1 else None
        if kind is None:
            if "properties" in schema or "allOf" in schema:
                return "object"
            if "items" in schema:
                return "array"
            branch = _branches(schema)
            if branch:
                return self._kind(self._resolve(branch[0]))
        return kind if isinstance(kind, str) else None

    def _resolve(self, schema: Any) -> dict[str, Any]:
        """`#/$defs/<이름>` 을 `defs` 에서 푼다. 고리는 `MAX_HOPS` 에서 멈춘다. 스키마가 아닌
        값은 빈 스키마다."""
        for _ in range(MAX_HOPS):
            ref = schema.get("$ref") if isinstance(schema, dict) else None
            if not isinstance(ref, str):
                break
            target = (
                self.defs.get(ref.removeprefix(DEFS_PREFIX))
                if ref.startswith(DEFS_PREFIX)
                else None
            )
            if not isinstance(target, dict):
                return {}
            schema = target
        return schema if isinstance(schema, dict) and "$ref" not in schema else {}


def _branches(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """null 이 아닌 갈래가 하나뿐인 `anyOf`·`oneOf` 의 그 갈래. 갈래가 여럿이면 어느 키를
    보일지 고를 수 없어 비운다."""
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if not isinstance(options, list):
            continue
        rest = [o for o in options if isinstance(o, dict) and o.get("type") != "null"]
        if len(rest) == 1:
            return rest
    return []


def _note(schema: dict[str, Any]) -> str | None:
    """enum 값(`open or closed`) 또는 format. 둘 다 없으면 None 이다."""
    values = schema.get("enum")
    if isinstance(values, list) and 0 < len(values) <= MAX_ENUM:
        shown = [_clean(v) for v in values if v is not None]
        if len(shown) == 1:
            return shown[0]
        if shown:
            return ", ".join(shown[:-1]) + " or " + shown[-1]
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt:
        return FORMATS.get(fmt, _clean(fmt))
    return None


def _clip(line: str) -> str:
    """맨 위의 키만으로도 `MAX_CHARS` 를 넘는 줄. 키를 줄이고 `…` 로 끝을 알린다."""
    head = line[: MAX_CHARS - 5]
    cut = head.rfind(", ")
    head = head[:cut] if cut > 0 else head
    return f"{head}, …}}."
