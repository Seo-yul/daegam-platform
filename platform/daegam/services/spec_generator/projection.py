"""투영(projection): LLM 에게는 허용 필드만 보내고, 돌려받은 값을 원본 deep copy 위에 심는다 (D25).

뷰(RefineView)의 모양은 operation 단위 사전이다.

    {
      "info": {"description": str|null},
      "operations": {
        "<operationId>": {
          "operationId": str,            # 프리셋 normalize_operation_ids 일 때만
          "summary": str|null,
          "description": str|null,
          "parameters": {"<name>": str|null}   # 인라인 파라미터의 description
        }
      },
      "components": {
        "<SchemaName>": {"description": str|null, "properties": {"<p>": <node>}, "items": <node>}
      }
    }

스키마 node 는 `{description, properties?, items?}` 로 재귀한다. 값이 null 이면 원본에 그 설명이
없다는 뜻이고 LLM 은 문자열로 채울 수 있다. `apply` 는 문자열 값만 심고 null·빈 문자열은 그대로
둔다. 원본에 없는 키(새 operation, 새 파라미터, 새 속성)는 버리고 그 뷰 경로를 `unknown` 으로
돌려준다. 구조·키 순서·`x-soap`·`x-s2o` 는 LLM 손을 거치지 않는다.

operation 은 (path, method) 로 식별하고 뷰 키는 그 시점의 operationId 다(없으면 "METHOD path").
묶음(batch)은 unit 목록이다. unit 은 ("info",), ("op", path, method), ("schema", name) 이다.
"""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from daegam.pipeline.convert import iter_operations
from daegam.services.spec_generator import surface

Unit = tuple[str, ...]
RefineView = dict[str, Any]


@dataclass(frozen=True)
class Leaf:
    """뷰의 leaf 하나. keys 는 뷰 안 경로, pointer 는 원본 문서의 JSON pointer 다."""

    keys: tuple[str, ...]
    pointer: str
    value: Any


def _op_key(path: str, method: str, op: dict[str, Any]) -> str:
    op_id = op.get("operationId")
    if isinstance(op_id, str) and op_id:
        return op_id
    return f"{method.upper()} {path}"


def op_keys(spec: dict[str, Any]) -> dict[tuple[str, str], str]:
    """(path, method) → 뷰 키. operationId 가 겹치면(되먹임 중간 상태) `id (METHOD path)` 로 구분해
    LLM 이 어느 operation 을 고칠지 가리킬 수 있게 한다."""
    ops = list(iter_operations(spec))
    keys = [_op_key(path, method, op) for path, method, op in ops]
    dup = {k for k, c in Counter(keys).items() if c > 1}
    return {
        (path, method): f"{key} ({method.upper()} {path})" if key in dup else key
        for (path, method, _op), key in zip(ops, keys, strict=True)
    }


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def units_of(spec: dict[str, Any]) -> list[Unit]:
    """뷰를 이루는 unit 을 문서 순서대로. info, operation 들, components.schemas 들."""
    units: list[Unit] = [("info",)]
    for path, method, _op in iter_operations(spec):
        units.append(("op", path, method))
    schemas = (spec.get("components") or {}).get("schemas")
    if isinstance(schemas, dict):
        for name in schemas:
            units.append(("schema", str(name)))
    return units


def _param_keys(params: list[Any]) -> list[tuple[int, str]]:
    """인라인 파라미터의 (index, 뷰 키). 이름이 겹치면 `name@in` 으로 구분한다."""
    names = [p.get("name") for p in params if isinstance(p, dict) and "$ref" not in p]
    dup = {n for n, c in Counter(names).items() if c > 1}
    out: list[tuple[int, str]] = []
    for i, p in enumerate(params):
        if not isinstance(p, dict) or "$ref" in p or not isinstance(p.get("name"), str):
            continue
        key = p["name"]
        if key in dup:
            key = f"{key}@{p.get('in', '?')}"
        out.append((i, key))
    return out


def _schema_leaves(node: Any, keys: tuple[str, ...], pointer: str) -> list[Leaf]:
    if not isinstance(node, dict) or "$ref" in node:
        return []
    out = [
        Leaf(
            (*keys, "description"), pointer + "/description", _text_or_none(node.get("description"))
        )
    ]
    props = node.get("properties")
    if isinstance(props, dict):
        for name, child in props.items():
            out.extend(
                _schema_leaves(
                    child,
                    (*keys, "properties", str(name)),
                    pointer + "/properties/" + surface.escape(str(name)),
                )
            )
    items = node.get("items")
    if isinstance(items, dict):
        out.extend(_schema_leaves(items, (*keys, "items"), pointer + "/items"))
    return out


def leaves_of(
    spec: dict[str, Any], *, rename: bool = False, units: list[Unit] | None = None
) -> list[Leaf]:
    """허용 필드 leaf 목록. units 를 주면 그 unit 만."""
    wanted = set(units) if units is not None else None
    out: list[Leaf] = []
    if wanted is None or ("info",) in wanted:
        info = spec.get("info")
        if isinstance(info, dict):
            out.append(
                Leaf(
                    ("info", "description"),
                    "/info/description",
                    _text_or_none(info.get("description")),
                )
            )
    keys = op_keys(spec)
    for path, method, op in iter_operations(spec):
        if wanted is not None and ("op", path, method) not in wanted:
            continue
        key = keys[(path, method)]
        base = surface.join("paths", path, method)
        if rename:
            out.append(
                Leaf(
                    ("operations", key, "operationId"), base + "/operationId", op.get("operationId")
                )
            )
        out.append(
            Leaf(
                ("operations", key, "summary"), base + "/summary", _text_or_none(op.get("summary"))
            )
        )
        out.append(
            Leaf(
                ("operations", key, "description"),
                base + "/description",
                _text_or_none(op.get("description")),
            )
        )
        params = op.get("parameters")
        if isinstance(params, list):
            for i, pkey in _param_keys(params):
                out.append(
                    Leaf(
                        ("operations", key, "parameters", pkey),
                        f"{base}/parameters/{i}/description",
                        _text_or_none(params[i].get("description")),
                    )
                )
    schemas = (spec.get("components") or {}).get("schemas")
    if isinstance(schemas, dict):
        for name, node in schemas.items():
            if wanted is not None and ("schema", str(name)) not in wanted:
                continue
            out.extend(
                _schema_leaves(
                    node,
                    ("components", str(name)),
                    "/components/schemas/" + surface.escape(str(name)),
                )
            )
    return out


def view_of(leaves: Iterable[Leaf]) -> dict[str, Any]:
    """leaf 들을 keys 경로대로 중첩한 뷰. 키 순서는 leaf 순서다. overrides 뷰도 이것으로 만든다."""
    view: dict[str, Any] = {}
    for leaf in leaves:
        node = view
        for k in leaf.keys[:-1]:
            node = node.setdefault(k, {})
        node[leaf.keys[-1]] = leaf.value
    return view


def extract(
    spec: dict[str, Any], *, rename: bool = False, units: list[Unit] | None = None
) -> RefineView:
    """원본에서 허용 필드만 뽑은 뷰. 키 순서는 문서 순서다."""
    return view_of(leaves_of(spec, rename=rename, units=units))


@dataclass(frozen=True)
class Applied:
    draft: dict[str, Any]
    unknown: list[str]  # 원본에 없는 뷰 경로이거나 문자열이 아닌 값 (JSON pointer 모양, 뷰 기준)


def view_paths(node: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """뷰의 leaf 경로 전부(값이 dict 가 아닌 자리). 빈 dict 는 아무것도 아니다."""
    if isinstance(node, dict):
        out: list[tuple[str, ...]] = []
        for k, v in node.items():
            out.extend(view_paths(v, (*prefix, str(k))))
        return out
    return [prefix]


def apply_leaves(
    doc: dict[str, Any],
    view: Any,
    leaves: Iterable[Leaf],
    *,
    before_set: Callable[[dict[str, Any], Leaf], None] | None = None,
) -> Applied:
    """뷰의 문자열 값을 doc deep copy 위의 leaf 자리에 심는다. refine 과 overrides 가 같이 쓴다.

    leaves 에 없는 뷰 경로와 문자열이 아닌 값은 버리고 unknown 에 적는다. None 과 빈 문자열은
    "그대로 두겠다" 는 답이라 조용히 건너뛴다(D25·D37). before_set 은 값을 심기 직전에 부른다.
    overrides 가 없던 오버라이드 항목의 빈 dict 를 먼저 만드는 데 쓴다."""
    draft = copy.deepcopy(doc)
    if not isinstance(view, dict):
        return Applied(draft, ["/"])
    known = {leaf.keys: leaf for leaf in leaves}
    unknown: list[str] = []
    for keys in view_paths(view):
        leaf = known.get(keys)
        pointer = surface.join(*keys)
        if leaf is None:
            unknown.append(pointer)
            continue
        value = view
        for k in keys:
            value = value[k]
        if value is None or value == "":
            continue  # 비워 두면 원본 그대로
        if not isinstance(value, str):
            unknown.append(pointer)
            continue
        if value != leaf.value:
            if before_set is not None:
                before_set(draft, leaf)
            surface.set_at(draft, leaf.pointer, value)
    return Applied(draft, unknown)


def apply(spec: dict[str, Any], view: Any, *, rename: bool = False) -> Applied:
    """뷰의 문자열 값을 원본 deep copy 위에 심는다. 뷰에는 있는데 원본에 없는 경로, 문자열이 아닌
    값은 버리고 unknown 에 적는다. 묶음 밖의 키라도 원본에 있으면 받는다."""
    return apply_leaves(spec, view, leaves_of(spec, rename=rename))


# -- 변경 목록 --------------------------------------------------------------------------


ChangeKind = Literal["changed", "added", "removed"]
"""채택 단위의 종류. 표면 차이의 `reordered` 는 어떤 규칙도 허용하지 않으므로 채택 단위가
되지 않는다."""


@dataclass(frozen=True)
class Change:
    """채택 단위 하나. id 는 pointer·kind 에서 결정적으로 나오므로 다시 계산해도 같다."""

    id: str
    pointer: str
    kind: ChangeKind
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pointer": self.pointer,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
        }


def change_id(pointer: str, kind: str) -> str:
    return hashlib.sha1(f"{kind}:{pointer}".encode()).hexdigest()[:12]


def changes(original: dict[str, Any], draft: dict[str, Any]) -> list[Change]:
    """원본과 초안의 차이를 채택 단위 목록으로. 문서 순서다.

    `reordered` 는 표면 검사가 먼저 막는다. 여기까지 왔으면 검사를 건너뛴 것이라 오류로 멈춘다
    (apply_changes 와 같은 규칙). 타입 검사기가 게이트에서 돌지 않으므로 코드로 지킨다 (P73 2절)."""
    out: list[Change] = []
    for d in surface.diff(original, draft):
        if d.kind == "reordered":
            raise ValueError(f"reordered keys at {d.pointer!r} cannot be a change")
        out.append(Change(change_id(d.pointer, d.kind), d.pointer, d.kind, d.before, d.after))
    return out


def apply_changes(original: dict[str, Any], accepted: list[Change]) -> dict[str, Any]:
    """채택한 변경만 원본 deep copy 위에 심는다."""
    out = copy.deepcopy(original)
    for c in accepted:
        if c.kind == "removed":
            surface.delete_at(out, c.pointer)
        elif c.kind in ("changed", "added"):
            surface.set_at(out, c.pointer, copy.deepcopy(c.after))
        else:
            raise ValueError(f"change {c.id} ({c.kind}) cannot be applied")
    return out


def renamed(original: dict[str, Any], draft: dict[str, Any]) -> list[dict[str, str]]:
    """operationId 가 바뀐 operation 들 [{from, to}]. (path, method) 로 짝을 맞춘다."""
    before = {(p, m): op.get("operationId") for p, m, op in iter_operations(original)}
    out: list[dict[str, str]] = []
    for p, m, op in iter_operations(draft):
        old = before.get((p, m))
        new = op.get("operationId")
        if isinstance(old, str) and isinstance(new, str) and old != new:
            out.append({"from": old, "to": new})
    return out
