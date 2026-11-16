"""파드가 내려갈 때 Agent 런타임이 도는 턴을 기다리는 시간의 렌더다 (P78 5절).

provision 마다 플랫폼 설정 `DAEGAM_AGENT_TURN_DRAIN_SECONDS`(기본 120초)를 문서에 남기고, 렌더가 그
결정을 env `DAEGAM_TURN_DRAIN_SECONDS` 와 grace(drain + 60초)로 싣는다. 결정이 없는 옛 문서는 둘 다
싣지 않아 렌더가 그대로다. Advanced overrides 가 grace 를 정하면 그 grace 를 두고 drain 을 그 안에
맞춘다. 저장하는 순간에도, 편집기의 최종본을 그대로 저장해도 같다. 렌더만 하는 다시 적용은 남긴
결정을 쓰고, 설정이 바뀐 것은 다음 provision 이 가져간다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.k8s.merge import apply_overrides
from daegam.k8s.templates.agent import (
    TURN_DRAIN_ENV,
    TURN_SETTLE_SECONDS,
    fit_turn_drain,
    render_agent_deployment,
)
from tests.helpers.agent_runtime import (
    env_map,
    minimal_agent_doc,
)
from tests.helpers.docs import effective_config
from tests.helpers.source import AGENT_RUNTIME, py_constant

GRACE = "terminationGracePeriodSeconds"


def _grace_patch(grace: Any) -> dict[str, Any]:
    return {"spec": {"template": {"spec": {GRACE: grace}}}}


def _render(**kw: Any) -> dict[str, Any]:
    return render_agent_deployment(
        minimal_agent_doc(**kw),
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )


def _drain_and_grace(deployment: dict[str, Any]) -> tuple[str | None, int | None]:
    pod = deployment["spec"]["template"]["spec"]
    return env_map(pod["containers"][0]).get(TURN_DRAIN_ENV), pod.get(GRACE)


def test_a_document_without_the_decision_renders_no_drain():
    """provision 전 문서(draft)의 렌더는 drain env 도 grace 도 없다. overrides 가 grace 를 정해도 그
    값만 얹히고 env 는 생기지 않는다."""
    rendered = _render()
    assert _drain_and_grace(rendered) == (None, None)
    final = fit_turn_drain(apply_overrides(rendered, _grace_patch(90)))
    assert _drain_and_grace(final) == (None, 90)


@pytest.mark.parametrize(
    ("overrides", "drain", "grace"),
    [
        pytest.param({}, "120", 180, id="no-override"),
        pytest.param(_grace_patch(600), "540", 600, id="above-the-default"),
        pytest.param(_grace_patch(90), "30", 90, id="below-the-default"),
        pytest.param(_grace_patch(60), "0", 60, id="sixty-means-no-drain"),
        pytest.param(_grace_patch(20), "0", 20, id="below-the-overhead"),
        pytest.param(_grace_patch(None), "0", None, id="removed-grace"),
    ],
)
def test_the_drain_fits_the_grace(overrides: dict[str, Any], drain: str, grace: int | None):
    """grace 는 drain 에 preStop 5초, 마감 몫 45초, 여유 10초를 더한 것이다. 저장된 overrides 가
    grace 를 정하면 렌더가 drain 을 그 안에 맞추고(지운 grace 는 쿠버네티스 기본 30초다), 최종본의
    grace 는 overrides 의 값이다. 그래서 drain 도중에 SIGKILL 되지 않는다."""
    rendered = _render(turn_drain_seconds=120, overrides=overrides)
    assert _drain_and_grace(rendered) == (drain, 180)
    final = fit_turn_drain(apply_overrides(rendered, overrides))
    assert _drain_and_grace(final) == (drain, grace)


def test_the_drain_env_and_settle_time_match_the_runtime() -> None:
    """런타임 drain.py 의 env 이름과 마감 몫이 플랫폼의 것과 같다. 마감 몫이 어긋나면 grace 가 짧아
    런타임이 턴을 마감하는 도중에 파드가 강제 종료된다."""
    drain = AGENT_RUNTIME / "drain.py"
    assert py_constant(drain, "DRAIN_ENV") == TURN_DRAIN_ENV
    assert py_constant(drain, "SETTLE_SECONDS") == TURN_SETTLE_SECONDS
