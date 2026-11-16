"""잠든 Agent 의 렌더와 레플리카 패치 (P44 8절·9절, D103).

렌더는 문서의 쉬는 상태가 레플리카 수를 정한다. asleep 이면 0 이고 그 밖은 배포 설정이다. 준비
검사는 첫 1초부터 2초마다이고 실패 문턱이 15 라 깨운 파드가 곧 Service 에 들면서도 저장소가 잠깐
흔들려 빠지기까지는 지금처럼 30초쯤 걸린다. 레플리카 패치는 `spec.replicas` 하나만 바꾼다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.k8s.templates.agent import (
    READY_PROBE_DELAY_SECONDS,
    READY_PROBE_FAILURES,
    READY_PROBE_PERIOD_SECONDS,
    render_agent_deployment,
    rendered_replicas,
)
from daegam.store.agents import AgentSleep
from tests.helpers.agent_runtime import minimal_agent_doc
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.docs import effective_config
from tests.helpers.fake_cluster import FakeClusterClient


def _render(state: str, replicas: int = 3) -> dict[str, Any]:
    return render_agent_deployment(
        minimal_agent_doc(sleep=AgentSleep(state=state)),
        effective_config(replicas=replicas),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )


@pytest.mark.parametrize(
    ("state", "replicas"), [("awake", 3), ("draining", 3), ("waking", 3), ("asleep", 0)]
)
def test_the_sleep_state_decides_the_rendered_replicas(state, replicas):
    """잠든 문서만 0 이다. 렌더가 그 상태를 읽으므로 provision·재적용·렌더 번호 어느 렌더도 잠든
    Agent 를 깨우지 않는다 (9절)."""
    assert _render(state)["spec"]["replicas"] == replicas
    assert rendered_replicas(state, effective_config(replicas=3)) == replicas


def test_a_sleeping_render_changes_only_the_count():
    """잠든 렌더와 깨어 있는 렌더는 레플리카 수만 다르다. 잠든 동안의 재적용은 템플릿만 바꾸고 다음
    깨우기가 그 템플릿으로 뜬다."""
    asleep, awake = _render("asleep"), _render("awake")
    asleep["spec"].pop("replicas")
    awake["spec"].pop("replicas")
    assert asleep == awake


def test_the_readiness_probe_is_fast_with_a_high_threshold():
    """첫 1초부터 2초마다 보고 15번 실패해야 Service 에서 빠진다 (8절, D103 의 고르지 않은 것)."""
    (container,) = _render("awake")["spec"]["template"]["spec"]["containers"]
    probe = container["readinessProbe"]
    assert probe["httpGet"] == {"path": "/healthz?probe=ready", "port": 8080}
    assert (probe["initialDelaySeconds"], probe["periodSeconds"]) == (1, 2)
    assert probe["failureThreshold"] == 15
    assert READY_PROBE_PERIOD_SECONDS * READY_PROBE_FAILURES == 30
    assert READY_PROBE_DELAY_SECONDS == 1


def test_the_replica_patch_changes_one_field():
    """패치는 `spec.replicas` 만 바꾸고 세대를 돌려준다. 같은 값이면 세대가 그대로다. 없는
    Deployment 는 None 이다 (9절)."""
    fake = FakeClusterClient()
    dep = _render("awake")
    dep["metadata"]["generation"] = 4
    fake.objects[("apps/v1", "Deployment", DEFAULT_NS, "agent-bot")] = {
        **dep,
        "metadata": {**dep["metadata"], "namespace": DEFAULT_NS, "name": "agent-bot"},
    }
    before = fake.get("apps/v1", "Deployment", "agent-bot", DEFAULT_NS)
    assert fake.scale_deployment("agent-bot", DEFAULT_NS, 0) == 5
    assert fake.scale_deployment("agent-bot", DEFAULT_NS, 0) == 5
    after = fake.get("apps/v1", "Deployment", "agent-bot", DEFAULT_NS)
    assert after["spec"].pop("replicas") == 0 and before["spec"].pop("replicas") == 3
    assert after["spec"] == before["spec"]
    assert fake.scale_deployment("missing", DEFAULT_NS, 1) is None
