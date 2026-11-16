"""플랫폼 패키지에서 오류 코드를 내는 곳을 AST 로 읽는다 (P74 2.2절). test_error_codes.py 가 쓴다.

읽는 꼴은 이렇다.

- 상태와 코드의 짝: `error`, `ServiceError` 와 그 자손, `_error`·`error` 속성, `HTTPException`,
  `FormatRefused`, 자손 생성자의 `super().__init__` 가 (글자 상태, 글자 코드 또는 모듈 상수)를
  받는다. `HTTPException` 은 둘째 인자나
  `detail=` 가 `{"code": ...}` 인 dict 여도 읽는다.
- 그 밖의 호출도 첫 인자가 400~599 의 글자 상태이고 둘째가 코드면 짝으로 읽는다(`error_cls(422,
  ...)` 처럼 오류 클래스를 변수로 받는 곳).
- 상태가 정해진 도우미의 `code=`: `not_null`(422), `forbidden`(403), `unauthorized`(401),
  `_require` 의 `outside=`(409). 코드를 주지 않으면 그 기본값이다.
- 코드만 지니는 예외(`CODE_ONLY`)의 첫 인자나 `code=`. 상태는 다시 싸는 쪽이 표에서 찾는다.
- 클래스 속성 `status`·`code` 를 지니는 예외(`GraphError`, `CompileError`).
- OIDC `AuthError` 의 `code=`·`status=`. 빠진 쪽은 기본값(401, `invalid_token`)이다.
- 상태가 변수인 곳(`LocalAuthError(status, ...)`)은 코드만 읽고 상태는 None 이다.

OAuth 의 `OAuthError` 는 본문이 RFC 6749 꼴이라 읽지 않는다(P74 범위 밖).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "daegam"

PAIR_NAMES = frozenset({"error", "_error", "HTTPException", "FormatRefused", "ServiceError"})
FIXED_HELPERS: dict[str, tuple[int, str, str]] = {
    # 이름: (상태, 코드를 받는 키워드, 기본 코드)
    "not_null": (422, "code", "invalid_value"),
    "forbidden": (403, "code", "forbidden"),
    "unauthorized": (401, "code", "unauthorized"),
    "_require": (409, "outside", "already_deployed"),
}
CODE_ONLY = frozenset(
    {
        "RenderError",
        "MergeError",
        "ExtractError",
        "BlockedURL",
        "ProfileValidationError",
        "KubeconfigRejected",
        "PipelineError",
    }
)
# OIDC 검증 실패의 기본 (상태, 코드). `code=`·`status=` 로 바꿔 준다
AUTH_DEFAULT = (401, "invalid_token")
SKIPPED = frozenset({"OAuthError"})


@dataclass(frozen=True)
class Raise:
    where: str
    status: int | None
    code: str


def _trees() -> list[tuple[Path, ast.Module]]:
    return [(f, ast.parse(f.read_text(encoding="utf-8"))) for f in sorted(PACKAGE.rglob("*.py"))]


def _constants(trees: list[tuple[Path, ast.Module]]) -> dict[str, str]:
    """모듈 수준 `NAME = "글자"`. 같은 이름이 다른 값으로 둘이면 읽지 않는다."""
    seen: dict[str, set[str]] = {}
    for _path, tree in trees:
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


def _service_errors(trees: list[tuple[Path, ast.Module]]) -> frozenset[str]:
    """`ServiceError` 와 그 자손의 클래스 이름."""
    bases: dict[str, set[str]] = {}
    for _path, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases[node.name] = {ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases}
    names = {"ServiceError"}
    grew = True
    while grew:
        grew = False
        for name, parents in bases.items():
            if name not in names and parents & names:
                names.add(name)
                grew = True
    return frozenset(names - SKIPPED)


def _literal(node: ast.expr | None, constants: dict[str, str]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _dict_code(node: ast.expr | None, constants: dict[str, str]) -> object:
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "code":
                return _literal(value, constants)
    return None


def scan() -> list[Raise]:
    trees = _trees()
    constants = _constants(trees)
    pair_names = PAIR_NAMES | _service_errors(trees)
    found: list[Raise] = []

    def add(path: Path, call: ast.Call, status: object, code: object) -> None:
        if isinstance(code, str):
            where = f"{path.relative_to(PACKAGE)}:{call.lineno}"
            found.append(Raise(where, status if isinstance(status, int) else None, code))

    for path, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # 클래스 속성으로 상태와 코드를 지니는 예외(GraphError·CompileError)
                attrs = {
                    s.targets[0].id: _literal(s.value, constants)
                    for s in node.body
                    if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name)
                }
                if isinstance(attrs.get("status"), int) and isinstance(attrs.get("code"), str):
                    where = f"{path.relative_to(PACKAGE)}:{node.lineno}"
                    found.append(Raise(where, attrs["status"], attrs["code"]))  # type: ignore[arg-type]
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = ast.unparse(call.func).rsplit(".", 1)[-1]
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            args = call.args
            first = _literal(args[0], constants) if args else None
            looks_like_pair = isinstance(first, int) and 400 <= first <= 599 and len(args) > 1
            is_super = ast.unparse(call.func) == "super().__init__"
            if name in pair_names or looks_like_pair or is_super:
                if args and isinstance(_literal(args[0], constants), str):
                    # 그래프 검사의 `error(code, message)` 처럼 본문 안의 항목 코드다 (범위 밖)
                    continue
                status = _literal(args[0], constants) if args else None
                second = args[1] if len(args) > 1 else None
                code = _literal(second, constants) or _dict_code(second, constants)
                code = code or _dict_code(kw.get("detail"), constants)
                add(path, call, status, code)
            elif name in FIXED_HELPERS:
                status, key, default = FIXED_HELPERS[name]
                add(path, call, status, _literal(kw[key], constants) if key in kw else default)
            elif name in CODE_ONLY:
                first = args[0] if args else kw.get("code")
                add(path, call, None, _literal(first, constants))
            elif name == "AuthError":
                code = _literal(kw["code"], constants) if "code" in kw else AUTH_DEFAULT[1]
                status = _literal(kw["status"], constants) if "status" in kw else AUTH_DEFAULT[0]
                add(path, call, status, code)
    return found
