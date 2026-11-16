"""P53: 턴 위임의 런타임 사본 파리티 (D91).

결정이 결정자의 입력을 싣는지 가르는 규칙(`holds_input`), 턴 답의 두 헤더 이름, 런타임이 `/end` 에
싣는 끝난 까닭은 런타임 `grant_gate.py`·`turn.py`·`turn_grants.py` 가 원본이다. 플랫폼
`services/turn_person.py`·`store/turn_grants.py` 가 사본을 둔다. 어긋나면 플랫폼이 승인자의 입력을
소유자의 이름으로 열거나, 받아들인 턴의 위임을 not_accepted 로 끝내거나, 런타임의 끝을 422 로
거절한다. 런타임 쪽이 원본이므로 플랫폼의 사본을 그 값에 맞춘다.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, get_args

from daegam.services import turn_person
from daegam.services.agent_graph import SOURCE_KINDS
from daegam.services.turn_grants import BOUND_TO
from daegam.store.turn_grants import RuntimeEndReason
from tests.helpers.source import AGENT_RUNTIME, exec_excerpt, py_constant

GRANT_GATE = AGENT_RUNTIME / "grant_gate.py"
TURN = AGENT_RUNTIME / "turn.py"
TURN_GRANTS = AGENT_RUNTIME / "turn_grants.py"

DECISIONS: list[list[dict[str, Any]]] = [
    [],
    [{"type": "approve"}],
    [{"type": "reject"}],
    [{"type": "reject", "message": None}],
    [{"type": "reject", "message": ""}],
    [{"type": "reject", "message": "아니다"}],
    [{"type": "edit", "args": {"a": 1}}],
    [{"type": "respond", "message": "이렇게 하라"}],
    [{"type": "approve"}, {"type": "edit", "args": {}}],
    [{"type": "approve"}, {"type": "reject"}],
    [{"type": "later_kind"}],
]


def test_holds_input_decides_like_the_runtime():
    runtime = exec_excerpt(
        GRANT_GATE,
        {"Sequence": Sequence, "Decision": object},
        functions=("holds_input",),
        constants=("_NO_INPUT_TYPES",),
    )["holds_input"]
    for decisions in DECISIONS:
        objects = [SimpleNamespace(type=d["type"], message=d.get("message")) for d in decisions]
        assert turn_person.holds_input(decisions) == runtime(objects), decisions


def test_turn_answer_headers_match_the_runtime():
    assert turn_person.SESSION_HEADER == py_constant(TURN, "SESSION_HEADER").lower()
    assert turn_person.TURN_GRANT_HEADER == py_constant(TURN_GRANTS, "TURN_GRANT_HEADER").lower()


def test_end_reasons_match_the_runtime():
    found = exec_excerpt(TURN_GRANTS, {}, constants=lambda name: name.startswith("END_"))
    runtime = {v for k, v in found.items() if k.startswith("END_") and isinstance(v, str)}
    assert set(get_args(RuntimeEndReason)) == runtime


def test_renewal_knows_exactly_the_source_kinds_the_compile_knows():
    """갱신은 `BOUND_TO` 에 없는 종류의 대상을 뒤 판의 것으로 보고 기록에 둔다 (P46 A0). 컴파일이
    아는 소스 종류와 같아야 이 판이 토큰을 주는 대상을 갱신이 빠뜨리지 않는다."""
    assert set(BOUND_TO) == SOURCE_KINDS
