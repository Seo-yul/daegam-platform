"""계층 방향을 지킨다: api → services → store·k8s, 그 반대는 없다.

거꾸로 가는 import 가 하나 생기면 아래 계층을 위 계층 없이 쓸 수 없게 된다. 실제로 그런 곳이
있었다. 옛 MCP 인스턴스 서비스가 `api/deps.py` 의 `hash_token` 을 가져다 쓰느라 그 서비스를
import 하려면 FastAPI 의존 그래프 전체가 딸려 왔다.

예외는 없다. 렌더된 CR 이 담는 플랫폼 API 접두사(`API_PREFIX`)는 코어인 `daegam/urls.py` 에 있다.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.source import imported_modules, parse_python

DAEGAM = Path(__file__).resolve().parents[1] / "daegam"
# 아래 계층 → 그 계층이 import 하면 안 되는 위 계층
FORBIDDEN = {
    "services": ("daegam.api",),
    "store": ("daegam.api", "daegam.services"),
    "k8s": ("daegam.api", "daegam.services"),
    "auth": ("daegam.api", "daegam.services"),
    "config": ("daegam.api", "daegam.services"),
    # 남의 문서를 여는 문(D61). documents/ 는 daegam-docparse 와 그 하위 프로세스가 돌리는 코드라
    # 플랫폼 서비스와 저장소가 딸려 오면 안 된다
    "documents": ("daegam.api", "daegam.services", "daegam.store"),
    "xmlsafe.py": ("daegam.api", "daegam.services", "daegam.store"),
    "zipsafe.py": ("daegam.api", "daegam.services", "daegam.store"),
    "request_body.py": ("daegam.api", "daegam.services", "daegam.store"),
    "regex_safety.py": ("daegam.api", "daegam.services", "daegam.store"),
    "contracts.py": ("daegam.api", "daegam.services", "daegam.store"),
    "units.py": ("daegam.api", "daegam.services", "daegam.store"),
    # 공개 URL fetch 의 주소 규칙과 그 위의 URL 판정. 설정·저장소·렌더가 쓰는 코어다 (D30).
    # urlfetch 는 웹 Tool 의 바이트 사본이라 daegam 을 아예 import 하지 않는다
    # (tests/test_urlfetch_copy.py)
    "urlfetch.py": ("daegam.api", "daegam.services", "daegam.store"),
    "urls.py": ("daegam.api", "daegam.services", "daegam.store"),
}


def _imports(path: Path) -> set[str]:
    return imported_modules(parse_python(path))


def test_lower_layers_do_not_import_upper_layers() -> None:
    problems: list[str] = []
    for layer, forbidden in FORBIDDEN.items():
        root = DAEGAM / layer
        for f in [root] if root.is_file() else sorted(root.rglob("*.py")):
            rel = f.relative_to(DAEGAM).as_posix()
            for module in sorted(_imports(f)):
                for bad in forbidden:
                    if module == bad or module.startswith(f"{bad}."):
                        problems.append(f"{rel} -> {module}")
    assert not problems, (
        "계층을 거꾸로 올라가는 import 가 있다. 공용이면 아래 계층(store/base.py, names.py,"
        " errors.py)으로 내린다:\n  " + "\n  ".join(problems)
    )


def test_test_doubles_live_with_the_tests() -> None:
    """테스트 대역과 그 검사는 플랫폼 꾸러미가 아니라 tests/helpers 에 있다(P75, D87). 꾸러미 전체가
    이미지에 들어가므로 여기 두면 대역이 운영 코드와 함께 실린다."""
    fakes = [
        f"{f.relative_to(DAEGAM).as_posix()}: {node.name}"
        for f in sorted(DAEGAM.rglob("*.py"))
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef) and node.name.startswith("Fake")
    ]
    assert fakes == [], "테스트 대역은 platform/tests/helpers 로 옮긴다"
    guard = DAEGAM / "loop_guard.py"
    assert not guard.exists(), "loop_guard 는 테스트 대역의 검사라 tests/helpers 에 둔다"


def test_lease_lives_in_store_so_every_layer_can_use_it() -> None:
    """이름 단위 `Lease` 는 reconcile·감사 수집기·비서 셋이 쓰는 Mongo 원시 기능이라 store 에 있다
    (D30 "공통은 아래 계층에"). services 가 다른 services 를 거쳐 가져오면 그 서비스에 묶인다."""
    assert (DAEGAM / "store/leases.py").is_file(), "Lease 는 store/leases.py 에 있어야 한다"
    users = (
        "services/reconcile.py",
        "services/audit_collector.py",
        "services/assistant/service.py",
        "wiring.py",
    )
    for rel in users:
        text = (DAEGAM / rel).read_text(encoding="utf-8")
        if "Lease" not in text:
            continue
        assert "from daegam.store.leases import" in text, (
            f"{rel} 은 store 에서 Lease 를 가져와야 한다"
        )
        assert "from daegam.services.reconcile import Lease" not in text
