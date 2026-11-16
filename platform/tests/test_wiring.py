"""배선 자기 점검 (P72 2.4절, D84).

만들기 전에 있는 의존은 생성자 인자라 빠지면 기동이 TypeError 로 멈춘다. 만든 뒤에 붙이는 훅은
그 클래스가 `late_hooks` 로 적고 `_check_wiring` 이 그 선언을 읽는다. 손으로 적은 목록이 코드를
따라가지 못해 훅 넷이 검사에서 빠져 있던 자리다.
"""

from __future__ import annotations

import ast
import typing
from types import SimpleNamespace
from typing import Any

import pytest

from daegam.services.clusters import ClusterService
from daegam.services.hub import HubService
from daegam.wiring import Services, _check_wiring
from tests.helpers.consts import REPO
from tests.helpers.source import parse_python

WIRING = REPO / "platform" / "daegam" / "wiring.py"


def _assistant(tool_defs: Any = ("tool",), app: Any = "app") -> SimpleNamespace:
    return SimpleNamespace(tool_defs=list(tool_defs), executor=SimpleNamespace(app=app))


class _Cluster:
    late_hooks = ClusterService.late_hooks

    def __init__(self, **hooks: Any) -> None:
        for name in self.late_hooks:
            setattr(self, name, hooks.get(name, object()))


def test_a_missing_declared_hook_stops_the_start() -> None:
    """선언한 훅이 하나라도 비면 기동에서 막는다. 클러스터 삭제 가드가 비면 남은 배포가 있는
    클러스터가 지워진다."""
    _check_wiring(SimpleNamespace(cluster_service=_Cluster(), assistant=_assistant()))
    broken = SimpleNamespace(
        cluster_service=_Cluster(instance_checker=None), assistant=_assistant()
    )
    with pytest.raises(RuntimeError, match="cluster_service.instance_checker"):
        _check_wiring(broken)


def test_the_assistant_without_tools_or_app_stops_the_start() -> None:
    """비서는 도구 표가 비어 있거나 실행기가 앱을 못 받으면 조용히 도구 없는 채팅이 된다."""
    with pytest.raises(RuntimeError, match="assistant.tool_defs"):
        _check_wiring(SimpleNamespace(assistant=_assistant(tool_defs=())))
    with pytest.raises(RuntimeError, match="assistant.executor.app"):
        _check_wiring(SimpleNamespace(assistant=_assistant(app=None)))


def _owner(node: ast.expr, hints: dict[str, Any]) -> Any:
    """대입 대상의 주인이 `Services` 의 칸이면 그 타입이다. `cluster_service.x` 처럼 칸 이름의 지역
    변수와 `services.cluster_service.x` 를 함께 본다. 그 밖(app.state 등)은 None 이다."""
    if isinstance(node, ast.Name):
        return hints.get(node.id)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "services"
    ):
        return hints.get(node.attr)
    return None


def _late_assignments(fn: ast.FunctionDef) -> list[tuple[ast.expr, str]]:
    """함수 안에서 객체 속성에 대입하는 자리. `a.b = …`, `a.b: T = …`, `setattr(a, "b", …)` 다."""
    found: list[tuple[ast.expr, str]] = []
    for node in ast.walk(fn):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
        ):
            found.append((node.args[0], str(node.args[1].value)))
        for target in targets:
            if isinstance(target, ast.Attribute):
                found.append((target.value, target.attr))
    return found


def test_build_services_assigns_only_declared_hooks() -> None:
    """서비스를 만든 뒤 대입하는 속성은 그 클래스가 `late_hooks` 로 적은 것뿐이다. 선언하지 않고
    붙이면 `_check_wiring` 이 그 훅을 보지 못하므로 여기서 막는다. 만들기 전에 있는 의존은 생성자
    인자로 넘긴다. `build_services` 와 그 도우미(`_token_services`, `_oauth_services`), 앱에 붙이는
    `attach` 를 보고, 일반 대입·주석 붙은 대입·`setattr` 을 모두 센다."""
    functions = {"build_services", "_token_services", "_oauth_services", "attach"}
    found = {
        node.name: node
        for node in ast.walk(parse_python(WIRING))
        if isinstance(node, ast.FunctionDef) and node.name in functions
    }
    assert set(found) == functions, f"wiring.py 에서 찾지 못한 함수: {functions - set(found)}"
    hints = typing.get_type_hints(Services)
    seen = {
        attr
        for node in found.values()
        for target, attr in _late_assignments(node)
        if _owner(target, hints) is not None
    }
    # 검사가 비어 돌지 않는다. 선언한 훅의 대입은 찾아야 한다
    assert ClusterService.late_hooks <= seen
    assert HubService.late_hooks <= seen
    undeclared = [
        f"{fn}: {ast.unparse(target)}.{attr}"
        for fn, node in found.items()
        for target, attr in _late_assignments(node)
        if (owner := _owner(target, hints)) is not None
        and attr not in getattr(owner, "late_hooks", frozenset())
    ]
    assert not undeclared, f"선언하지 않은 사후 대입: {undeclared}"
