"""오류 코드표(runtime/error_codes.py)와 실제로 내는 곳이 맞는가 (P74 2.2절, D86).

runtime/ 의 모든 파일을 AST 로 훑어 상태와 코드가 글로 적힌 짝을 모은다. 읽는 꼴은 이렇다.

- `error`·`_error`·`HTTPException` 의 (상태, 코드). `_reject`·`rejected` 는 첫 인자 뒤의 (상태,
  코드)다.
- `HTTPException(상태, detail={"code": ...})`.
- 코드만 지니는 예외 `HitlError` 의 첫 인자. 상태는 다시 싸는 쪽이 표에서 찾는다.
- `AuthError(..., code=, status=)`. 빠진 쪽은 그 기본값(`invalid_token`, 401)이다.

코드는 글자이거나 모듈의 글자 상수다. 상태가 변수인 곳은 코드만 표에 있으면 되고, 상태는
conftest 가 켜는 처리기 검사가 본다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from runtime import error_codes
from runtime.error_codes import RECORDED, STATUS, allowed, status_of

RUNTIME_DIR = Path(error_codes.__file__).parent
PAIR_CALLS = frozenset({"error", "_error", "HTTPException"})
# 첫 인자(상태 객체) 뒤에 (상태, 코드)가 오는 거절 함수다
REJECT_CALLS = frozenset({"_reject", "rejected"})
CODE_ONLY = frozenset({"HitlError"})
AUTH_DEFAULTS = {"code": "invalid_token", "status": 401}
# 글로 적힌 짝 없이 변수로만 나가는 코드. `error` 는 코드 없는 HTTPException 을 감싼 이름이고,
# `runtime_image_too_old` 는 ConfigError 의 코드로 healthz 와 RefusingMiddleware 를 지난다
REACHED_BY_VARIABLE = frozenset({"error", "runtime_image_too_old"})


def _constants(trees: list[ast.Module]) -> dict[str, str]:
    """모듈 수준 `NAME = "글자"`. 같은 이름이 다른 값으로 둘이면 읽지 않는다."""
    seen: dict[str, set[str]] = {}
    for tree in trees:
        for node in tree.body:
            target = (
                node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
            )
            if (
                isinstance(node, ast.Assign | ast.AnnAssign)
                and isinstance(target, ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                seen.setdefault(target.id, set()).add(node.value.value)
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def _literal(node: ast.expr | None, constants: dict[str, str]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _detail_code(call: ast.Call, constants: dict[str, str]) -> object:
    for kw in call.keywords:
        if kw.arg == "detail" and isinstance(kw.value, ast.Dict):
            for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "code":
                    return _literal(value, constants)
    return None


def _scan() -> list[tuple[str, int | None, str]]:
    """(위치, 상태 또는 None, 코드) 목록."""
    files = sorted(RUNTIME_DIR.rglob("*.py"))
    trees = [ast.parse(f.read_text(encoding="utf-8")) for f in files]
    constants = _constants(trees)
    found: list[tuple[str, int | None, str]] = []
    for path, tree in zip(files, trees, strict=True):
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = ast.unparse(call.func).rsplit(".", 1)[-1]
            where = f"{path.relative_to(RUNTIME_DIR)}:{call.lineno}"
            args = call.args[1:] if name in REJECT_CALLS else call.args
            if name in PAIR_CALLS or name in REJECT_CALLS:
                status = _literal(args[0], constants) if args else None
                code = _literal(args[1], constants) if len(args) > 1 else None
                code = code if code is not None else _detail_code(call, constants)
                if isinstance(code, str):
                    found.append((where, status if isinstance(status, int) else None, code))
            elif name in CODE_ONLY and call.args:
                code = _literal(call.args[0], constants)
                if isinstance(code, str):
                    found.append((where, None, code))
            elif name == "AuthError":
                kw = {k.arg: _literal(k.value, constants) for k in call.keywords}
                code = kw.get("code", AUTH_DEFAULTS["code"])
                status = kw.get("status", AUTH_DEFAULTS["status"])
                if isinstance(code, str):
                    found.append((where, status if isinstance(status, int) else None, code))
    return found


def test_every_raised_code_matches_the_registry() -> None:
    found = _scan()
    assert len(found) > 60, f"{len(found)} 곳밖에 못 읽었다. 읽는 꼴이 코드와 어긋났는지 본다"
    problems = []
    for where, status, code in found:
        if code not in STATUS and code not in RECORDED:
            problems.append(f"{where} {code}: 표에 없다")
        elif status is not None and not allowed(status, code):
            problems.append(f"{where} {status} {code}: 표와 다르다")
    assert not problems, "오류 코드표와 어긋난 곳:\n  " + "\n  ".join(problems)


def test_registry_has_no_unused_code() -> None:
    """표에만 있고 아무도 내지 않는 코드가 없다. 변수로만 나가는 코드는 뺀다."""
    raised = {code for _where, _status, code in _scan()}
    unused = sorted((set(STATUS) | set(RECORDED)) - raised - REACHED_BY_VARIABLE)
    assert not unused, f"내는 곳이 없는 코드: {unused}"


def test_recorded_codes_have_one_home() -> None:
    assert not set(STATUS) & set(RECORDED)
    assert all(r.reason for r in RECORDED.values())


def test_status_of_refuses_unknown_and_recorded_codes_in_tests() -> None:
    assert status_of("runtime_image_too_old", 500) == 503
    with pytest.raises(AssertionError, match="없다"):
        status_of("no_such_code", 422)
    with pytest.raises(AssertionError, match="여럿"):
        status_of("not_pending", 409)


def test_status_of_falls_back_outside_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(error_codes, "STRICT", False)
    assert status_of("no_such_code", 422) == 422
    assert status_of("not_pending", 409) == 409
