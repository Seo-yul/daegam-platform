"""파리티 테스트가 화면(TS)과 런타임의 사본을 읽는다.

런타임 꾸러미(deepagents 등)는 플랫폼에 없으므로 런타임 파일은 import 하지 않고 글자와 구문 트리로
읽는다. 표준 라이브러리만 쓰는 파일은 `runtime_module` 로 통째로 싣는다.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from daegam.services import agent_graph, agent_skills
from tests.helpers.consts import REPO

AGENT_RUNTIME = REPO / "agent-runtime" / "runtime"
MCP_RUNTIME = REPO / "mcp-runtime" / "runtime"
WEB_TOOLS = REPO / "web-tools" / "webtools"
# 코드 실행 작업 공간 이미지의 구성요소 디렉터리 (P40 F1)
SANDBOX_IMAGE = REPO / "sandbox"
UI_LIB = REPO / "ui" / "src" / "lib"


def read_source(path: Path) -> str:
    assert path.is_file(), f"{path} 가 없다"
    return path.read_text(encoding="utf-8")


def assert_byte_copy(platform: Path, runtime: Path) -> None:
    """런타임 파일 runtime 이 플랫폼 파일 platform 의 바이트 동일 복사본이고, 플랫폼 쪽이
    daegam.* 을 끌어오지 않는다(그래야 복사본이 런타임에서도 돈다). 두 디렉터리가 lock 과 이미지
    컨텍스트를 따로 가져 공용 패키지 대신 복사본을 둔다. 어긋나면 한쪽을 다른 쪽에 그대로
    복사한다."""
    assert runtime.is_file(), f"{runtime} 가 없다"
    assert platform.read_bytes() == runtime.read_bytes(), (
        f"{platform} 와 {runtime} 가 다르다. 한쪽을 다른 쪽에 그대로 복사한다"
    )
    text = read_source(platform)
    assert "from daegam" not in text and "import daegam" not in text, (
        f"{platform} 가 daegam.* 을 끌어오므로 런타임 복사본이 돌지 못한다"
    )


def parse_python(path: Path) -> ast.Module:
    return ast.parse(read_source(path))


def imported_modules(tree: ast.AST, *, strings: bool = False) -> set[str]:
    """tree 가 import 하는 모듈 이름들(`import a, b.c` 면 a 와 b.c, `from d.e import f` 면 d.e).
    상대 import 는 넣지 않는다. strings 면 글자 상수도 모두 넣는다. importlib 에 이름으로 넘긴
    모듈(`importlib.import_module("a")`)까지 보려는 검사가 쓴다. 주석과 설명 글은 보지 않는다."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
        elif strings and isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def runtime_module(path: Path) -> Any:
    """파일 하나를 그대로 싣는다. 표준 라이브러리만 쓰는 모듈이라 런타임 꾸러미 없이 실린다.
    dataclass 가 문자열 주석을 풀 때 sys.modules 에서 모듈을 찾으므로 거기 올린다."""
    spec = importlib.util.spec_from_file_location(f"runtime_{path.stem}_copy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assigned_names(node: ast.stmt) -> list[str]:
    """최상위 문장이 값을 대입하는 이름이다(`A = ...`, `A: T = ...`). 다른 문장이면 비어 있다."""
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
        return [node.target.id]
    return []


def find_assignment(module: ast.Module, name: str) -> ast.Assign | ast.AnnAssign:
    """모듈 맨 위에서 name 에 값을 대입하는 문장이다."""
    for node in module.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and name in _assigned_names(node):
            return node
    raise AssertionError(f"{name} 을 찾지 못했다")


def find_function(module: ast.Module, name: str) -> ast.FunctionDef:
    """모듈 맨 위의 함수 정의다."""
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 을 찾지 못했다")


def exec_excerpt(
    path: Path,
    scope: dict[str, Any],
    *,
    functions: Collection[str] = (),
    constants: Collection[str] | Callable[[str], bool] = (),
) -> dict[str, Any]:
    """최상위 함수 `functions` 와 `constants` 가 고른 최상위 대입만 scope 위에서 실행해 그
    이름공간을 낸다. 런타임 꾸러미를 끌어오는 모듈에서 표 함수와 상수만 떼어 플랫폼 사본과 같은
    입력으로 돌린다. constants 는 이름 묶음이거나 이름을 받는 술어(`str.isupper`)다. 이름으로 준
    함수·상수가 파일에 없으면 실패한다."""
    wanted = constants if callable(constants) else set(constants).__contains__
    body = [
        node
        for node in parse_python(path).body
        if (isinstance(node, ast.FunctionDef) and node.name in functions)
        or any(wanted(name) for name in _assigned_names(node))
    ]
    namespace = dict(scope)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    named = [*functions, *([] if callable(constants) else constants)]
    missing = [name for name in named if name not in namespace]
    assert not missing, f"{path} 에서 {missing} 를 찾지 못했다"
    return namespace


def _imported_constant(path: Path, node: ast.ImportFrom, name: str) -> tuple[Path, str] | None:
    """`from runtime.attachments import MIB` 가 name 을 들여오면 그 모듈 파일과 원래 이름이다."""
    if node.level or not node.module or not node.module.startswith("runtime."):
        return None
    for alias in node.names:
        if (alias.asname or alias.name) == name:
            return path.parent.parent / f"{node.module.replace('.', '/')}.py", alias.name
    return None


def py_constant(path: Path, name: str) -> Any:
    """런타임 모듈 맨 위의 `NAME = <식>` 값이다. 모듈을 import 하지 않고 그 식만 푼다. 식이 같은
    모듈의 다른 상수(`2 * MIB`)를 쓰면 그 상수도 같은 방법으로 풀고, 다른 런타임 모듈에서
    들여온 상수(`from runtime.attachments import MIB`)는 그 모듈에서 푼다."""
    for node in parse_python(path).body:
        if isinstance(node, ast.ImportFrom) and (found := _imported_constant(path, node, name)):
            return py_constant(*found)
        if isinstance(node, ast.Assign | ast.AnnAssign) and name in _assigned_names(node):
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            scope = {n: py_constant(path, n) for n in names}
            code = compile(ast.Expression(node.value), str(path), "eval")
            return eval(code, {"__builtins__": {}, **scope})
    raise AssertionError(f"{path} 에서 {name} 를 찾지 못했다")


def ts_number(path: Path, name: str) -> int:
    """`export const NAME = 100;` 의 값이다."""
    m = re.search(rf"export const {name}\s*=\s*([0-9_]+)\s*;", read_source(path))
    assert m, f"{path} 에서 {name} 를 찾지 못했다"
    return int(m.group(1).replace("_", ""))


def ts_string(path: Path, name: str) -> str:
    """`export const NAME = "value";` 의 값이다."""
    m = re.search(rf'export const {name}\s*=\s*"([^"]*)"\s*;', read_source(path))
    assert m, f"{path} 에서 {name} 를 찾지 못했다"
    return m.group(1)


def ts_object(path: Path, name: str, entry: re.Pattern[str]) -> dict[str, str]:
    """`export const NAME ... = { ... }` 의 한 단계 객체에서 entry 가 잡은 (키, 값) 이다."""
    m = re.search(rf"export const {name}\b[^=]*=\s*\{{(.*?)\}}", read_source(path), re.DOTALL)
    assert m, f"{path} 에서 {name} 를 찾지 못했다"
    return dict(entry.findall(m.group(1)))


# 그래프 검사 이슈를 내는 호출. 코드는 첫 인자의 글자다
ISSUE_CALL = re.compile(r"\ba\.(?:error|warn)\(")
ISSUE_CODE = re.compile(r'\ba\.(?:error|warn)\(\s*"([a-z][a-z0-9_]*)"')
# Skill 검사 결과를 만드는 호출. 코드는 첫 인자의 상수 이름이다
FINDING_CALL = re.compile(r"(?<!class )\bFinding\(")
FINDING_CODE = re.compile(r"(?<!class )\bFinding\(\s*([A-Z][A-Z0-9_]*)\b")


def graph_issue_codes() -> set[str]:
    """서버 그래프 검사(`agent_graph.py`·`agent_skills.py`)가 내는 이슈 code 전부. 소스에서 읽고,
    코드를 글자나 상수로 넘기지 않는 호출이 생기면 실패해 추출을 넓히게 한다."""
    source = Path(agent_graph.__file__).read_text(encoding="utf-8")
    codes = ISSUE_CODE.findall(source)
    assert codes, "agent_graph.py 에서 이슈 호출을 찾지 못했다. 호출 모양이 바뀌면 추출을 고친다"
    assert len(codes) == len(ISSUE_CALL.findall(source)), "코드가 글자가 아닌 이슈 호출이 있다"
    return set(codes) | _finding_codes()


def _finding_codes() -> set[str]:
    source = Path(agent_skills.__file__).read_text(encoding="utf-8")
    names = FINDING_CODE.findall(source)
    assert names, (
        "agent_skills.py 에서 Finding 호출을 찾지 못했다. 호출 모양이 바뀌면 추출을 고친다"
    )
    assert len(names) == len(FINDING_CALL.findall(source)), "코드가 상수가 아닌 Finding 호출이 있다"
    return {getattr(agent_skills, name) for name in names}
