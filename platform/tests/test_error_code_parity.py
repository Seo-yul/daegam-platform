"""플랫폼과 에이전트 런타임의 오류 코드표가 같은 이름의 코드에 같은 상태를 주는가 (P74 2.2절, D86).

런타임 표는 플랫폼이 import 하지 못하므로 "agent-runtime" 의 runtime/error_codes.py 를 글로 읽는다.
두 표에 함께 있는 코드는 상태 집합이 같거나, 한쪽 집합이 다른 쪽의 기록된 예외(RECORDED) 안에
들어야 한다. 이 테스트는 코드를 하나도 따로 적지 않는다.
"""

from __future__ import annotations

import ast

from daegam.error_codes import RECORDED, STATUS
from tests.helpers.consts import REPO

RUNTIME_REGISTRY = REPO / "agent-runtime" / "runtime" / "error_codes.py"

# None 은 "어떤 상태든" 이다
Statuses = frozenset[int] | None


def _runtime_registry() -> tuple[dict[str, Statuses], set[str]]:
    """(코드 → 상태 집합, 기록된 예외의 코드)."""
    tree = ast.parse(RUNTIME_REGISTRY.read_text(encoding="utf-8"))
    codes: dict[str, Statuses] = {}
    recorded: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id == "STATUS":
            codes.update({k: frozenset({v}) for k, v in ast.literal_eval(node.value).items()})
        elif node.target.id == "RECORDED":
            assert isinstance(node.value, ast.Dict)
            for key, call in zip(node.value.keys, node.value.values, strict=True):
                assert isinstance(key, ast.Constant) and isinstance(call, ast.Call)
                first = call.args[0]
                if isinstance(first, ast.Constant) and first.value is None:
                    codes[key.value] = None
                else:
                    assert isinstance(first, ast.Call)
                    codes[key.value] = frozenset(ast.literal_eval(first.args[0]))
                recorded.add(key.value)
    return codes, recorded


def _platform_registry() -> dict[str, Statuses]:
    codes: dict[str, Statuses] = {k: frozenset({v}) for k, v in STATUS.items()}
    codes.update({k: r.statuses for k, r in RECORDED.items()})
    return codes


def _inside(inner: Statuses, outer: Statuses) -> bool:
    return outer is None or (inner is not None and inner <= outer)


def test_runtime_registry_reads() -> None:
    codes, recorded = _runtime_registry()
    assert len(codes) > 30 and "not_pending" in recorded


def test_shared_codes_agree_on_status() -> None:
    runtime, runtime_recorded = _runtime_registry()
    platform = _platform_registry()
    problems = []
    for code in sorted(set(runtime) & set(platform)):
        theirs, ours = runtime[code], platform[code]
        if theirs == ours:
            continue
        if code in RECORDED and _inside(theirs, ours):
            continue
        if code in runtime_recorded and _inside(ours, theirs):
            continue
        problems.append(f"{code}: 런타임 {theirs}, 플랫폼 {ours}")
    assert not problems, "두 표가 어긋난 코드:\n  " + "\n  ".join(problems)
