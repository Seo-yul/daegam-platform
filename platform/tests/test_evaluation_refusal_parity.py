"""P45 9절: 평가 실행기가 '런타임이 거절함' 으로 가르는 코드의 사본 파리티.

원본은 런타임이 Tool 을 부르지 않고 `guards.refusal_result` 로 낸 거절이다. 그 거절을 내는 곳은
`agent-runtime/runtime/tool_safety.py` 의 `MESSAGES`(Tool 이 돈 뒤 결과만 막는 `outside_mark_failed`
는 빼고)와 `agent-runtime/runtime/web_guard.py` 의 `Refusal` 이다. 플랫폼의 사본은
`services/evaluation/runner.py` `RUNTIME_REFUSALS` 이고, 어긋나면 실행기가 런타임의 거절을 '실행함'
으로 적거나 Tool 의 결과를 거절로 적는다. 런타임 쪽이 원본이므로 플랫폼의 사본을 맞춘다.
"""

from __future__ import annotations

import re

from daegam.services.evaluation.runner import RUNTIME_REFUSALS
from tests.helpers.source import AGENT_RUNTIME, read_source

FIX = "런타임의 거절 코드가 원본이다. runner.py RUNTIME_REFUSALS 의 사본을 그 값에 맞춘다"


def _consts(source: str) -> dict[str, str]:
    return dict(re.findall(r'^([A-Z][A-Z_]*) = "([a-z_]+)"$', source, re.MULTILINE))


def _tool_safety_codes() -> set[str]:
    source = read_source(AGENT_RUNTIME / "tool_safety.py")
    consts = _consts(source)
    block = re.search(r"^MESSAGES = \{\n(.*?)^\}", source, re.MULTILINE | re.DOTALL)
    assert block, "tool_safety.py 에서 MESSAGES 를 찾지 못했다"
    names = re.findall(r"^ {4}([A-Z][A-Z_]*):", block.group(1), re.MULTILINE)
    assert names, "tool_safety.py MESSAGES 에 키가 없다"
    return {consts[n] for n in names} - {consts["OUTSIDE_MARK_FAILED"]}


def _web_guard_codes() -> set[str]:
    source = read_source(AGENT_RUNTIME / "web_guard.py")
    consts = _consts(source)
    names = set(re.findall(r"Refusal\(\s*([A-Z][A-Z_]*)", source))
    assert names, "web_guard.py 에서 Refusal 을 찾지 못했다"
    return {consts[n] for n in names}


def test_the_runner_knows_every_refusal_of_the_runtime() -> None:
    assert _tool_safety_codes() | _web_guard_codes() == RUNTIME_REFUSALS, FIX


def test_no_other_runtime_module_sends_a_refusal() -> None:
    callers = {
        path.name
        for path in AGENT_RUNTIME.glob("*.py")
        if path.name != "guards.py" and "refusal_result(" in read_source(path)
    }
    assert callers == {"tool_safety.py", "web_guard.py"}, (
        "런타임에 refusal_result 를 부르는 모듈이 늘었다. "
        "그 거절 코드를 이 파리티와 RUNTIME_REFUSALS 에 더한다"
    )
