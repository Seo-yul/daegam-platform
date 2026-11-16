"""Tool 규칙의 인자 조건을 지킨다 (P52 4절).

profile.json 의 `rules.<operationId>.conditions` 를 그 Tool 의 입력 스키마(FastMCP 가 지은
`parameters`)와 OpenAPI 라우트에서 풀어 두었다가, 호출마다 두 번 본다. 한 번은 `tools/call` 의
arguments 이고, 한 번은 백엔드로 실제 나가는 HTTP 요청이다(`ToolGuard.outgoing_refusal`). FastMCP 의
요청 짓기는 arguments 를 그대로 옮기지 않기 때문이다. 펼친(explode) 객체 query 인자의 속성이 같은
이름의 query 인자를 덮고, 판에 따라 `type: object` 를 적지 않은 본문의 인자 하나를 본문 전체로
접으며, 클라이언트의 HTTP 머리가 비어 있는 header 인자를 채운다. 그래서 query·header·본문의 조건은
나가는 요청에서 그 자리의 값으로 다시 판정한다. path·cookie 인자는 arguments 만 그 값을 정하므로
arguments 로 판정한다. 어긋나면 그 인자를 이름 댄 영어 Tool 오류이고 백엔드는 부르지 않는다.

- 연산자는 여섯이다(`equals`·`one_of`·`min`·`max`·`max_length`·`prefix`). 정규식은
  없고 배열을 지나는 경로는 받지 않는다. 수는 유한해야 한다(NaN·무한대는 형식이 틀린 값이다).
- 인자가 없으면(null 포함) `optional` 일 때만 지난다. 형식이 틀린 값은 실패다.
- 경로가 풀리지 않는 조건(없는 인자, 배열을 지나는 경로, 연산자에 맞지 않는 형식, 값이 틀린 조건,
  같은 점 경로가 둘인 것, 나가는 요청에서 자리를 정할 수 없는 것)은 그 Tool 을 닫는다. 모든 호출이
  Tool 오류이고, 그 경로는 `initialize` 의 `capabilities.experimental.arg_conditions` 로 알려
  플랫폼이 경고한다.

스키마를 읽는 규칙(`_deref`·`_branches`·`type_of`·`arguments_of` 와 그 상수), 값의 형식(`_fits`)과
연산자마다 받는 형식(`OP_TYPES`)은 플랫폼 `pipeline/tool_arguments.py` 와 `services/tool_rules.py`
의 것과 같은 벌이다. 플랫폼이 저장 때 받아 준 조건을 이 이미지가 같은 뜻으로 풀게 하려는 것이고,
플랫폼의 파리티 시험(`tests/test_condition_copy.py`)이 두 벌을 맞댄다.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl

from runtime.profile import ArgCondition

# 플랫폼 `pipeline/tool_arguments.py` 와 같다 ----------------------------------------------

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


# 플랫폼 `services/tool_rules.py` 의 `_OP_TYPES` 와 같다 ------------------------------------

# 조건 연산자마다 받는 인자 형식
OP_TYPES: dict[str, frozenset[str]] = {
    "equals": frozenset({"string", "integer", "number", "boolean"}),
    "one_of": frozenset({"string", "integer", "number"}),
    "min": frozenset({"integer", "number"}),
    "max": frozenset({"integer", "number"}),
    "max_length": frozenset({"string"}),
    "prefix": frozenset({"string"}),
}

# 여기부터는 이 이미지의 것이다 ---------------------------------------------------------------

# `initialize` 의 `capabilities.experimental` 에 싣는 키와 그 판. 플랫폼이 이 키로 이 이미지가 인자
# 조건을 지키는지 안다(P52 4절)
ARG_CONDITIONS = "arg_conditions"
ARG_CONDITIONS_VERSION = 1


def argument_segments(schema: dict[str, Any]) -> dict[str, list[tuple[str, ...]]]:
    """`arguments_of` 와 같은 길로 돌며 점 경로마다 그 경로를 이루는 이름들이다. 이름에 점이 든
    인자가 있으면 한 점 경로가 둘 이상일 수 있고, 그런 경로는 어느 인자인지 몰라 닫는다."""
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    out: dict[str, str] = {}
    segments: dict[str, list[tuple[str, ...]]] = {}

    def walk(node: dict[str, Any], prefix: str, names: tuple[str, ...], depth: int) -> None:
        props = node.get("properties")
        if not isinstance(props, dict):
            return
        for name, sub in props.items():
            if len(out) >= MAX_PATHS:
                return
            path = f"{prefix}{name}"
            kind, obj = type_of(sub, defs)
            out[path] = kind
            segments.setdefault(path, []).append((*names, name))
            if obj is not None and depth < MAX_DEPTH:
                walk(obj, f"{path}.", (*names, name), depth + 1)

    walk(schema, "", (), 1)
    return segments


def _fits(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    return False


def _condition_fits(cond: ArgCondition, kind: str) -> bool:
    """조건의 값이 연산자와 인자 형식에 맞는가. 플랫폼 `check_condition` 이 저장 때 보는 것과
    같다."""
    value = cond.value
    if cond.op == "one_of":
        return isinstance(value, list) and bool(value) and all(_fits(v, kind) for v in value)
    if cond.op == "max_length":
        return _fits(value, "integer") and value >= 0
    if cond.op in ("min", "max"):
        return _fits(value, "number")
    return _fits(value, kind)


@dataclass(frozen=True)
class Place:
    """조건의 값이 백엔드로 나가는 요청에서 서는 자리다. where 가 query·header·form 이면 key 는 그
    이름 하나이고, json 이면 JSON 본문 안의 경로다."""

    where: Literal["query", "header", "form", "json"]
    key: tuple[str, ...]


def locate(segments: tuple[str, ...], route: Any) -> Place | None | str:
    """조건의 자리다. arguments 만 그 값을 정하는 path·cookie 인자는 None 이고, 자리를 정할 수
    없으면 그 까닭(글)이다. route 는 FastMCP 의 `HTTPRoute` 이고 `parameter_map` 이 인자 이름마다의
    자리와 OpenAPI 이름을 든다."""
    mapping = (getattr(route, "parameter_map", None) or {}).get(segments[0])
    if not isinstance(mapping, Mapping):
        return "the argument has no place in the request"
    location, name, rest = mapping.get("location"), mapping.get("openapi_name"), segments[1:]
    if not isinstance(name, str):
        return "the argument has no place in the request"
    if location in ("path", "cookie"):
        return None if not rest else "nested path or cookie argument"
    if location == "header":
        return Place("header", (name,)) if not rest else "nested header argument"
    if location == "query":
        if not rest:
            return Place("query", (name,))
        if len(rest) > 1:
            return "query object nested deeper than one property"
        param = next(
            (p for p in route.parameters if p.location == "query" and p.name == name), None
        )
        if param is not None and param.explode is False:
            return "query object that is not exploded"
        if param is not None and param.style == "deepObject":
            return Place("query", (f"{name}[{rest[0]}]",))
        # 펼친 form 객체는 속성마다 그 이름의 query 키다
        return Place("query", (rest[0],))
    if location == "body":
        body = getattr(route, "request_body", None)
        media = next(iter(getattr(body, "content_schema", None) or {}), "")
        media = media.split(";")[0].strip().lower()
        if media == "application/json" or "json" in media:
            return Place("json", (name, *rest))
        if media == "application/x-www-form-urlencoded":
            return Place("form", (name,)) if not rest else "nested form field"
        return f"request body of type {media or 'unknown'}"
    return "the argument has no place in the request"


@dataclass(frozen=True)
class Check:
    """풀린 조건 하나다. segments 는 arguments 를 따라 내려갈 이름들이고 kind 는 그 인자의
    형식이다. place 는 나가는 요청에서 다시 볼 자리이고, None 이면 arguments 만 본다."""

    path: str
    segments: tuple[str, ...]
    kind: str
    op: str
    value: Any
    optional: bool
    place: Place | None = None


CLOSED = (
    "This tool is closed by its Tool rules: its argument conditions name arguments it does "
    "not have or that cannot be checked ({paths}). Ask the server's owner to fix the rules."
)


@dataclass(frozen=True)
class ToolGuard:
    """Tool 하나의 조건이다. unresolved 가 있으면 그 Tool 은 닫혀 모든 호출이 오류다."""

    checks: tuple[Check, ...] = ()
    unresolved: tuple[str, ...] = ()

    def refusal(self, arguments: Mapping[str, Any]) -> str | None:
        """arguments 로 이 호출을 거절하는 까닭(영어 한 문장)이다. 지나면 None 이다."""
        if self.unresolved:
            return CLOSED.format(paths=", ".join(self.unresolved))
        for check in self.checks:
            value, broken = _lookup(arguments, check.segments)
            reason = _judge(check, value, None if broken is None else _not_object(broken))
            if reason is not None:
                return f"The Tool rules refused this call: argument {check.path!r} {reason}."
        return None

    def outgoing_refusal(self, request: Any) -> str | None:
        """백엔드로 나가는 요청(httpx2.Request)으로 이 호출을 거절하는 까닭이다. 지나면 None
        이다."""
        if self.unresolved:
            return CLOSED.format(paths=", ".join(self.unresolved))
        for check in self.checks:
            if check.place is None:
                continue
            value, broken = _sent(check.place, check.kind, request)
            reason = _judge(check, value, broken)
            if reason is not None:
                return (
                    f"The Tool rules refused this call: argument {check.path!r} as sent to the "
                    f"backend {reason}."
                )
        return None


def compile_guard(
    conditions: Iterable[ArgCondition], parameters: Any, route: Any = None
) -> ToolGuard:
    """조건을 그 Tool 의 입력 스키마와 라우트에서 푼다. 풀리지 않는 조건의 경로는 unresolved 에
    든다. route 가 없으면(시험) 나가는 요청의 자리를 정하지 않는다."""
    schema = parameters if isinstance(parameters, dict) else {}
    kinds = arguments_of(schema)
    segments = argument_segments(schema)
    checks: list[Check] = []
    unresolved: list[str] = []
    for cond in conditions:
        parts = cond.path.split(".")
        through_array = any(kinds.get(".".join(parts[:i])) == "array" for i in range(1, len(parts)))
        kind = kinds.get(cond.path)
        found = segments.get(cond.path, [])
        place = locate(found[0], route) if route is not None and len(found) == 1 else None
        if (
            through_array
            or kind is None
            or kind not in OP_TYPES[cond.op]
            or len(found) != 1
            or not _condition_fits(cond, kind)
            or isinstance(place, str)
        ):
            if cond.path not in unresolved:
                unresolved.append(cond.path)
            continue
        assert not isinstance(place, str)
        checks.append(Check(cond.path, found[0], kind, cond.op, cond.value, cond.optional, place))
    return ToolGuard(checks=tuple(checks), unresolved=tuple(unresolved))


def closed_guard(paths: Iterable[str]) -> ToolGuard:
    """조건을 풀다 실패한 Tool 의 닫힌 지킴이다. 열린 채 남지 않게 하려는 것이다."""
    return ToolGuard(unresolved=tuple(paths) or ("<conditions>",))


_MISSING = object()
_INTEGER = re.compile(r"-?\d+")
_NUMBER = re.compile(r"-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")
_BOOLEANS = {"true": True, "True": True, "false": False, "False": False}


def _lookup(arguments: Any, segments: tuple[str, ...]) -> tuple[Any, str | None]:
    """(값, 틀린 자리). 값이 없으면 `_MISSING` 이다. 중간 값이 객체가 아니면 그 경로가 틀린
    자리다(맨 앞이면 빈 글이다)."""
    node: Any = arguments
    for i, name in enumerate(segments):
        if node is None:
            return _MISSING, None
        if not isinstance(node, Mapping):
            return _MISSING, ".".join(segments[:i])
        if name not in node:
            return _MISSING, None
        node = node[name]
    return (_MISSING if node is None else node), None


def _from_text(text: str, kind: str) -> Any:
    """query·header·form 의 글을 인자 형식의 값으로 읽는다. 읽지 못하면 그 글 그대로다(형식이 틀린
    값으로 실패한다)."""
    if kind == "integer" and _INTEGER.fullmatch(text):
        return int(text)
    if kind == "number" and _NUMBER.fullmatch(text):
        return float(text)
    if kind == "boolean" and text in _BOOLEANS:
        return _BOOLEANS[text]
    return text


def _sent(place: Place, kind: str, request: Any) -> tuple[Any, str | None]:
    """나가는 요청의 그 자리 값이다. (값, 읽지 못한 까닭). 까닭은 오류 문장의 뒷부분이다."""
    if place.where == "json":
        content = bytes(request.content or b"")
        if not content:
            return _MISSING, None
        try:
            body = json.loads(content)
        except ValueError:
            return _MISSING, "cannot be read because the request body is not JSON"
        value, broken = _lookup(body, place.key)
        if broken == "":
            return _MISSING, "cannot be read because the request body is not an object"
        if broken is not None:
            return _MISSING, _not_object(broken)
        return value, None
    if place.where == "query":
        texts = request.url.params.get_list(place.key[0])
    elif place.where == "header":
        texts = request.headers.get_list(place.key[0])
    else:
        content = bytes(request.content or b"").decode("utf-8", errors="replace")
        texts = [v for k, v in parse_qsl(content, keep_blank_values=True) if k == place.key[0]]
    if not texts:
        return _MISSING, None
    if len(texts) > 1:
        return _MISSING, "is sent more than once"
    return _from_text(texts[0], kind), None


def _not_object(where: str) -> str:
    return f"cannot be read because {where!r} is not an object"


def _judge(check: Check, value: Any, unreadable: str | None) -> str | None:
    """조건 하나가 어긋난 까닭(문장의 뒷부분)이다. 지나면 None 이다. unreadable 은 값을 읽지 못한
    까닭이다."""
    if unreadable is not None:
        return unreadable
    if value is _MISSING:
        return None if check.optional else "is required"
    if not _fits(value, check.kind):
        return f"must be a {check.kind}"
    op, bound = check.op, check.value
    if op == "equals" and value != bound:
        return f"must equal {bound!r}"
    if op == "one_of" and value not in bound:
        return f"must be one of {', '.join(repr(v) for v in bound)}"
    if op == "min" and value < bound:
        return f"must be at least {bound!r}"
    if op == "max" and value > bound:
        return f"must be at most {bound!r}"
    if op == "max_length" and len(value) > bound:
        return f"must be at most {bound} characters long"
    if op == "prefix" and not value.startswith(bound):
        return f"must start with {bound!r}"
    return None
