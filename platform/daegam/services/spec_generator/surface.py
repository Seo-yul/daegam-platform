"""표면 보호(surface guard): 초안이 원본과 허용 경로 밖에서 다른지 판정한다 (D25).

비교는 정렬하지 않는다. dict 는 키 순서까지, list 는 위치까지 그대로 본다. spec2openapi 가
`properties` 순서를 XSD sequence 로 보존하고 SOAP 브릿지가 그 순서로 직렬화하기 때문이다.
공통 키의 상대 순서가 바뀌면 그 dict 자리에 `reordered` 가 나오고 어떤 규칙도 이를 허용하지 않는다.

우선순위
1. 경로에 `x-soap*` 또는 `x-s2o` 세그먼트가 있으면 무조건 보호한다. 단 minify 규칙은 루트
   `/x-s2o/minify` 하위를 허용한다(minify 가 기록을 쓰는 자리).
2. 그 밖에서는 허용 leaf 패턴이 보호 subtree 를 이긴다. 예를 들어 파라미터 schema 는 보호
   대상이지만 그 안의 `description` 은 허용이다.

규칙 집합은 `ALLOW_REFINE`(설명 계열), `ALLOW_REFINE_RENAME`(프리셋 `normalize_operation_ids` 일 때
`operationId` changed 까지), `ALLOW_MINIFY`(설명 계열 + minify 기록 + 스키마 안 외부 `x-*` 삭제 +
example 삭제·hoist) 셋이다. `tags` 는 어느 규칙에도 없다.

`ALLOW_OVERRIDES`(D37) 만 대상이 OpenAPI 문서가 아니라 MCP Server 리비전의 `backends` 조각이다.
diff·guard 는 모양을 가리지 않으므로 규칙만 따로 둔다. 우선순위 1 은 여기에도 걸리니, `x-soap` 으로
시작하는 operationId 의 오버라이드는 규칙을 보기 전에 보호로 떨어진다(확률은 낮다).

JSON pointer 는 RFC 6901 이다. 루트는 빈 문자열이고 `~` 는 `~0`, `/` 는 `~1` 로 적는다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["changed", "added", "removed", "reordered"]
Segments = tuple[str, ...]


# -- JSON pointer ------------------------------------------------------------------------


def escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def unescape(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def join(*segments: str | int) -> str:
    return "".join("/" + escape(str(s)) for s in segments)


def segments_of(pointer: str) -> Segments:
    if not pointer:
        return ()
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON pointer: {pointer!r}")
    return tuple(unescape(s) for s in pointer[1:].split("/"))


def _step(node: Any, seg: str) -> Any:
    if isinstance(node, dict):
        if seg not in node:
            raise KeyError(seg)
        return node[seg]
    if isinstance(node, list):
        return node[int(seg)]
    raise KeyError(seg)


def resolve(doc: Any, pointer: str) -> Any:
    """pointer 가 가리키는 값. 없으면 KeyError 다."""
    node = doc
    for seg in segments_of(pointer):
        node = _step(node, seg)
    return node


def set_at(doc: Any, pointer: str, value: Any) -> None:
    """pointer 자리에 값을 둔다. 부모는 있어야 한다. dict 에 새 키면 맨 뒤에 붙는다."""
    segs = segments_of(pointer)
    if not segs:
        raise ValueError("cannot replace the document root")
    parent = doc
    for seg in segs[:-1]:
        parent = _step(parent, seg)
    last = segs[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        idx = int(last)
        if idx == len(parent):
            parent.append(value)
        else:
            parent[idx] = value
    else:
        raise KeyError(last)


def delete_at(doc: Any, pointer: str) -> None:
    segs = segments_of(pointer)
    if not segs:
        raise ValueError("cannot delete the document root")
    parent = doc
    for seg in segs[:-1]:
        parent = _step(parent, seg)
    last = segs[-1]
    if isinstance(parent, dict):
        del parent[last]
    elif isinstance(parent, list):
        del parent[int(last)]
    else:
        raise KeyError(last)


# -- diff ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Diff:
    """원본과 초안이 다른 자리 하나. added 는 before 가, removed 는 after 가 None 이다.
    reordered 는 dict 자리에 나오고 before·after 가 키 목록이다."""

    pointer: str
    kind: Kind
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "pointer": self.pointer,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
        }


def diff(original: Any, draft: Any) -> list[Diff]:
    """정렬 없이 재귀 비교한 차이 목록. 문서 순서대로다. 추가·삭제된 subtree 는 그 키 자리 하나로
    보고한다(하위를 펼치지 않는다)."""
    out: list[Diff] = []
    _walk("", original, draft, out)
    return out


def _same(a: Any, b: Any) -> bool:
    # 1 == True, 1 == 1.0 은 JSON 에서 다른 값이다
    return type(a) is type(b) and a == b


def _walk(pointer: str, a: Any, b: Any, out: list[Diff]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        common_a = [k for k in a if k in b]
        common_b = [k for k in b if k in a]
        if common_a != common_b:
            out.append(Diff(pointer, "reordered", list(a), list(b)))
        for k, v in a.items():
            child = pointer + "/" + escape(str(k))
            if k in b:
                _walk(child, v, b[k], out)
            else:
                out.append(Diff(child, "removed", v, None))
        for k, v in b.items():
            if k not in a:
                out.append(Diff(pointer + "/" + escape(str(k)), "added", None, v))
        return
    if isinstance(a, list) and isinstance(b, list):
        n = min(len(a), len(b))
        for i in range(n):
            _walk(f"{pointer}/{i}", a[i], b[i], out)
        for i in range(n, len(a)):
            out.append(Diff(f"{pointer}/{i}", "removed", a[i], None))
        for i in range(n, len(b)):
            out.append(Diff(f"{pointer}/{i}", "added", None, b[i]))
        return
    if not _same(a, b):
        out.append(Diff(pointer, "changed", a, b))


# -- 규칙 ------------------------------------------------------------------------------

# minify 의 keep 목록. 이 확장은 스키마 안에서도 지울 수 없다 (spec2openapi minify.py 와 같다)
KEEP_EXTENSIONS_EXACT = frozenset(
    {
        "x-soap",
        "x-s2o",
        "x-pattern",
        "x-collectionFormat",
        "x-original-body-name",
        "x-enum-varnames",
        "x-enum-descriptions",
        "x-example",
        "x-examples",
        "x-deprecated-reason",
    }
)
KEEP_EXTENSIONS_PREFIX = ("x-soap-", "x-fastmcp-")

Predicate = Callable[[Segments, Kind], bool]

_CHANGE_KINDS: frozenset[str] = frozenset({"changed", "added", "removed"})


def _pattern(text: str, kinds: frozenset[str] = _CHANGE_KINDS) -> Predicate:
    """`/paths/*/*/summary`, `**/description` 같은 세그먼트 패턴. `*` 는 세그먼트 하나,
    `**` 는 0개 이상이다. kinds 에 든 종류만 허용한다."""
    # 세그먼트를 "/" 로 이어 붙인 문자열에 정규식을 댄다. 세그먼트 안의 "/" 는 미리 치환한다
    tokens = []
    for p in text.strip("/").split("/"):
        if p == "**":
            tokens.append("(?:[^/]+/)*")
        elif p == "*":
            tokens.append("[^/]+/")
        else:
            tokens.append(re.escape(p) + "/")
    compiled = re.compile("^" + "".join(tokens).removesuffix("/") + "$")

    def match(segs: Segments, kind: Kind) -> bool:
        return kind in kinds and compiled.match("/".join(escape(s) for s in segs)) is not None

    return match


_SCHEMA_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "dependentSchemas", "schemas"}
)


def _foreign_extension_removed(segs: Segments, kind: Kind) -> bool:
    """스키마 subtree 안의 외부 `x-*` 키 삭제. keep 목록은 제외. 스키마 subtree 는 경로에 `schema`
    또는 `schemas` 세그먼트가 있는 자리다(components.schemas, 파라미터·미디어 schema). 삭제된 값이
    dict 면 leaf 가 그 아래로 펼쳐지므로, 경로 어딘가의 세그먼트가 스키마 node 의 직접 키인 `x-*`
    이면 된다. `properties` 같은 이름 map 바로 아래의 `x-*` 는 속성 이름이라 해당하지 않는다."""
    if kind != "removed":
        return False
    for i, seg in enumerate(segs):
        if not seg.startswith("x-") or i == 0:
            continue
        if seg in KEEP_EXTENSIONS_EXACT or seg.startswith(KEEP_EXTENSIONS_PREFIX):
            return False
        if segs[i - 1] in _SCHEMA_NAME_MAPS:
            continue
        return any(s in ("schema", "schemas") for s in segs[:i])
    return False


_DESCRIPTION_RULES: tuple[Predicate, ...] = (
    _pattern("**/description"),
    _pattern("/info/description"),
    _pattern("/paths/*/*/summary"),
)
_RENAME_RULE = _pattern("/paths/*/*/operationId", frozenset({"changed"}))
# D37. 대상이 MCP Server 의 backends 조각이다. removed 를 안 받는 것은 `allowed` 가 삭제된
# subtree 를 leaf 로 펼쳐 검사하기 때문이다. name 자리에 removed 를 허용하면 오버라이드 항목
# 통째 삭제까지 같이 열린다. 걷어내기는 사람이 폼에서 한다
_OVERRIDE_KINDS = frozenset({"changed", "added"})
_OVERRIDE_RULES: tuple[Predicate, ...] = (
    _pattern("/backends/*/tool_overrides/*/name", _OVERRIDE_KINDS),
    _pattern("/backends/*/tool_overrides/*/description", _OVERRIDE_KINDS),
)
_MINIFY_RULES: tuple[Predicate, ...] = (
    *_DESCRIPTION_RULES,
    _pattern("**/example", frozenset({"removed"})),
    _pattern("**/examples", frozenset({"removed"})),
    # 파라미터 example 을 schema 로 옮긴 것(hoist)
    _pattern("**/parameters/*/schema/example", frozenset({"added"})),
    _foreign_extension_removed,
)


@dataclass(frozen=True)
class Rules:
    name: str
    allow: tuple[Predicate, ...]
    minify_record: bool = False  # 루트 /x-s2o/minify 하위 허용

    def allows(self, segs: Segments, kind: Kind) -> bool:
        if kind == "reordered":
            return False
        if any(s == "x-s2o" or s.startswith("x-soap") for s in segs):
            return self.minify_record and segs[:2] == ("x-s2o", "minify")
        return any(rule(segs, kind) for rule in self.allow)


ALLOW_REFINE = Rules("refine", _DESCRIPTION_RULES)
ALLOW_REFINE_RENAME = Rules("refine+rename", (*_DESCRIPTION_RULES, _RENAME_RULE))
ALLOW_MINIFY = Rules("minify", _MINIFY_RULES, minify_record=True)
ALLOW_OVERRIDES = Rules("overrides", _OVERRIDE_RULES)


def refine_rules(*, rename: bool) -> Rules:
    return ALLOW_REFINE_RENAME if rename else ALLOW_REFINE


# -- guard -----------------------------------------------------------------------------


def _leaves(segs: Segments, value: Any) -> Iterator[Segments]:
    """subtree 의 leaf 경로들. 빈 dict·list 는 그 자리가 leaf 다."""
    if isinstance(value, dict) and value:
        for k, v in value.items():
            yield from _leaves((*segs, str(k)), v)
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            yield from _leaves((*segs, str(i)), v)
    else:
        yield segs


def allowed(d: Diff, rules: Rules) -> bool:
    """차이 하나가 규칙에 맞는가. 추가·삭제된 subtree 는 모든 leaf 가 허용돼야 한다."""
    segs = segments_of(d.pointer)
    if d.kind == "added":
        return all(rules.allows(leaf, "added") for leaf in _leaves(segs, d.after))
    if d.kind == "removed":
        return all(rules.allows(leaf, "removed") for leaf in _leaves(segs, d.before))
    return rules.allows(segs, d.kind)


Violation = Diff


def guard(original: Any, draft: Any, *, rules: Rules) -> list[Violation]:
    """원본과 초안의 차이 중 규칙이 허용하지 않는 것. 비어 있으면 통과다."""
    return [d for d in diff(original, draft) if not allowed(d, rules)]
