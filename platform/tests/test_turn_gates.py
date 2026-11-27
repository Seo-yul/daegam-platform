"""턴 위임 토큰의 게이트 (P53 3.5절): 렌더 번호를 푸는 꼴, 판 검사, 묶임을 싣는가, 클러스터마다의
Gateway 게이트 판정. 판정은 저장된 문서만 읽고, 노출한 Gateway 와 노출 Agent 는 모든 클러스터의
게이트를 닫는다. 묶임은 local 모드의 렌더가 번호에 단 표시로만 읽는다."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from daegam.auth.cedar import holds_forbid
from daegam.k8s.gates import Feature, RenderStamp, pack_epoch, unpack_epoch
from daegam.services.data_plane_auth import (
    KIND_GATEWAY,
    REAPPLY_SECONDS_PER_TICK,
    RENDER_EPOCHS,
    missed_gates,
    render_epoch,
)
from daegam.services.reconcile import CLUSTER_CHECK_SECONDS_PER_TICK, DRIVE_SECONDS_PER_TICK
from daegam.services.turn_gates import (
    HOLD_EXPOSED_AGENT_GROUPS,
    HOLD_EXPOSED_TOOLHIVE_TOO_OLD,
    HOLD_NO_BINDING,
    HOLD_OWNER_CEDAR,
    TICK_BUDGET_SECONDS,
    TurnGates,
    audience_applied,
    carries_binding,
    judge,
    live_epochs,
    lowest_epoch,
    owner_cedar,
    version_ok,
)
from daegam.store.auth_reapply import (
    KIND_AGENT,
    STALE_EPOCH,
    AudienceSwitch,
    ClusterTurnGates,
    DeployRender,
    is_stale_epoch,
    stale_mark,
)
from daegam.store.base import utcnow
from daegam.store.clusters import ClusterStatus, Preflight
from tests.helpers.docs import cluster_doc

NOW = utcnow()
# local 모드가 렌더한 Gateway 번호 3 (묶임의 표시를 단다)과 다른 모드가 렌더한 같은 번호
BOUND = pack_epoch(3, bound=True)
UNBOUND = 3
CONFIGMAP = {
    "spec": {"incomingAuth": {"authzConfig": {"type": "configMap", "configMap": {"name": "cedar"}}}}
}


def _cluster(name: str = "home", version: str | None = "0.53.0"):
    return cluster_doc(
        name, status=ClusterStatus(preflight=Preflight(ok=True, toolhive_version=version))
    )


def _doc(
    epoch: int = BOUND,
    *,
    state: str = "deployed",
    exposed: bool = False,
    overrides: dict[str, Any] | None = None,
    record: DeployRender | None = None,
    since=NOW,
    cluster_id: str = "c1",
    doc_id: str = "gw1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=doc_id,
        name=doc_id,
        tenant="acme",
        cluster_id=cluster_id,
        state=state,
        render_epoch=epoch,
        exposure=SimpleNamespace(enabled=exposed),
        overrides=overrides or {},
        deploy_render=record,
        deploying_since=since,
    )


def _packed(base: int, missing: frozenset[Feature], *, bound: bool = False) -> int:
    """base 번 판의 렌더가 게이트 missing 을 빠뜨린 번호. `render_epoch` 와 같은 꼴이다."""
    return pack_epoch(base, missing, bound=bound)


@pytest.mark.parametrize(
    ("version", "ok"),
    [("0.53.0", True), ("v0.54.1", True), ("0.52.9", False), (None, False)],
)
def test_the_version_check_needs_toolhive_0_53(version, ok):
    assert version_ok(_cluster(version=version)) is ok


MISSING = frozenset({Feature.AUDIT, Feature.AUTHZ_REF})


@pytest.mark.parametrize("base", [0, 1, 2, 3, 7])
@pytest.mark.parametrize("missing", [frozenset(), MISSING], ids=["passing", "missing"])
@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_a_packed_epoch_unpacks_to_its_base_gates_and_binding(base, missing, bound):
    epoch = pack_epoch(base, missing, bound=bound)
    assert epoch != STALE_EPOCH and (epoch < STALE_EPOCH) == (bound or bool(missing))
    assert unpack_epoch(epoch) == RenderStamp(base, missing, bound)
    assert unpack_epoch(stale_mark(epoch)) == RenderStamp(base, missing, bound)


def test_render_epoch_marks_only_local_gateway_renders_as_bound():
    passing = frozenset(Feature)
    now = RENDER_EPOCHS[KIND_GATEWAY]
    assert render_epoch(KIND_GATEWAY, passing, binds_delegation=True) == pack_epoch(now, bound=True)
    assert render_epoch(KIND_GATEWAY, passing) == now
    assert render_epoch(KIND_AGENT, passing, binds_delegation=True) == RENDER_EPOCHS[KIND_AGENT]


def test_only_a_bound_gateway_render_of_epoch_3_carries_the_binding():
    assert carries_binding(_doc(BOUND))
    assert carries_binding(_doc(_packed(3, frozenset({Feature.AUDIT}), bound=True)))
    # 다른 모드의 렌더와 번호 2 의 렌더는 묶임이 없다
    assert not carries_binding(_doc(UNBOUND))
    assert not carries_binding(_doc(_packed(3, frozenset({Feature.AUDIT}))))
    assert not carries_binding(_doc(2))
    assert not carries_binding(_doc(_packed(2, frozenset({Feature.AUDIT}))))


@pytest.mark.parametrize(
    "replaced", [BOUND, UNBOUND, 1, 0, _packed(3, frozenset({Feature.TIMEOUTS}), bound=True)]
)
def test_the_stale_mark_reads_back_the_replaced_epoch(replaced):
    mark = stale_mark(replaced)
    assert is_stale_epoch(mark) and stale_mark(mark) == mark
    assert unpack_epoch(mark) == unpack_epoch(replaced)
    assert carries_binding(_doc(mark)) is carries_binding(_doc(replaced))


def test_a_plain_stale_epoch_does_not_carry_the_binding():
    assert unpack_epoch(STALE_EPOCH) is None
    assert not carries_binding(_doc(STALE_EPOCH))
    assert not carries_binding(_doc(stale_mark(STALE_EPOCH)))
    assert lowest_epoch([BOUND, STALE_EPOCH, 2]) == STALE_EPOCH
    # 같은 번호면 묶임이 없는 렌더가 낮다
    assert lowest_epoch([BOUND, UNBOUND]) == UNBOUND


def test_a_first_deploy_in_progress_or_failed_holds_no_gate():
    fresh = DeployRender(since=NOW)
    assert live_epochs(KIND_GATEWAY, _doc(0, state="deploying", record=fresh)) == []
    assert carries_binding(_doc(0, state="deploying", record=fresh))
    applied = DeployRender(since=NOW, epoch=BOUND)
    assert carries_binding(_doc(0, state="failed", record=applied))


@pytest.mark.parametrize("state", ["deploying", "failed"])
@pytest.mark.parametrize("record", ["other-deployment", "none"])
def test_an_in_flight_render_without_this_deployments_record_reads_as_unknown(state, record):
    """되돌린 옛 코드가 다시 배포하면 이 판의 기록을 다른 배포의 것으로 만들고 자기 렌더(묶임
    없음)를 적용한다. 적용한 뒤 실패하면 상태와 까닭만 저장해 적힌 번호는 이 판의 것으로 남는다.
    그래서 기록이 맞지 않으면 무엇이 도는지 모른다 (P53 8절)."""
    found = DeployRender(since=NOW, epoch=BOUND) if record == "other-deployment" else None
    doc = _doc(BOUND, state=state, record=found, since=NOW + timedelta(seconds=5))
    assert live_epochs(KIND_GATEWAY, doc) == [STALE_EPOCH]
    assert not carries_binding(doc)


@pytest.mark.parametrize("state", ["deploying", "failed"])
def test_an_in_flight_agent_without_a_record_reads_its_stored_epoch(state):
    """Agent 번호 1 이후의 렌더는 모두 allowed_groups 를 채운다. 그래서 1.0.0 이 남긴 failed 노출
    Agent 처럼 기록이 없는 Agent 는 적힌 번호로 읽고 모든 클러스터의 게이트를 붙들지 않는다."""
    c1, c2 = _cluster("one"), _cluster("two")
    agent = _doc(2, state=state, exposed=True, cluster_id=c1.id, doc_id="ag1")
    assert live_epochs(KIND_AGENT, agent) == [2]
    assert _verdicts([c1, c2], {c1.id: [(KIND_AGENT, agent)]})[c2.id].gateway_gate_open
    voided = _doc(
        2,
        state=state,
        exposed=True,
        record=DeployRender(since=NOW, epoch=2),
        since=NOW + timedelta(seconds=5),
        cluster_id=c1.id,
        doc_id="ag1",
    )
    assert live_epochs(KIND_AGENT, voided) == [2]


def test_a_redeploy_keeps_the_render_it_replaced_until_it_finishes():
    record = DeployRender(since=NOW, epoch=BOUND, prior=1)
    assert not carries_binding(_doc(1, state="failed", record=record))
    assert not carries_binding(
        _doc(UNBOUND, state="failed", record=DeployRender(since=NOW, epoch=BOUND, prior=UNBOUND))
    )
    assert carries_binding(
        _doc(BOUND, state="failed", record=DeployRender(since=NOW, epoch=BOUND, prior=BOUND))
    )


@pytest.mark.parametrize("state", ["undeploying", "deleting", "deployed"])
def test_other_deploy_states_read_the_stored_epoch(state):
    record = DeployRender(since=NOW, epoch=BOUND)
    assert not carries_binding(_doc(1, state=state, record=record))
    assert carries_binding(_doc(BOUND, state=state, record=DeployRender(since=NOW, prior=1)))
    assert carries_binding(_doc(BOUND, state=state, record=None))


@pytest.mark.parametrize("state", ["undeploying", "deleting"])
def test_leaving_with_a_voided_record_reads_as_unknown(state):
    """되돌린 옛 코드의 재배포가 기록을 다른 배포의 것으로 만든 뒤 실패한 Gateway 를 내리는 동안에도
    그 코드의 렌더(묶임 없음)가 돌 수 있다. 적힌 번호는 이 판의 것으로 남아 있다."""
    voided = DeployRender(since=NOW, epoch=BOUND)
    doc = _doc(BOUND, state=state, record=voided, since=NOW + timedelta(seconds=5))
    assert live_epochs(KIND_GATEWAY, doc) == [STALE_EPOCH]
    assert not carries_binding(doc)
    # 배포를 마친 문서는 옛 코드가 성공해 적은 번호로 읽는다
    assert carries_binding(
        _doc(BOUND, state="deployed", record=voided, since=NOW + timedelta(seconds=5))
    )


@pytest.mark.parametrize("state", ["undeploying", "deleting"])
def test_leaving_after_a_failed_first_deploy_reads_that_deployments_record(state):
    """첫 배포가 마치지 못한 문서(적힌 번호 0)를 내리는 동안에는 그 배포가 적용한 렌더만 돈다."""
    record = DeployRender(since=NOW, epoch=BOUND)
    assert carries_binding(_doc(0, state=state, record=record))
    other = _doc(0, state=state, record=record, since=NOW + timedelta(seconds=5))
    assert not carries_binding(other)


def test_owner_cedar_is_any_stored_authz_config():
    assert owner_cedar(_doc(overrides=CONFIGMAP))
    policies = {"type": "inline", "inline": {"policies": ["permit(principal, action, resource);"]}}
    assert owner_cedar(_doc(overrides={"spec": {"incomingAuth": {"authzConfig": policies}}}))
    assert not owner_cedar(_doc())


def test_audience_applied_reads_the_switch():
    assert not audience_applied(SimpleNamespace(audience_switch=None))
    switched = AudienceSwitch(switched_at=NOW, reason="new_server")
    assert not audience_applied(SimpleNamespace(audience_switch=switched))
    applied = switched.model_copy(update={"applied_at": NOW})
    assert audience_applied(SimpleNamespace(audience_switch=applied))


def _verdicts(clusters, docs, *, trusts_platform=True):
    verdicts = judge(clusters, docs, trusts_platform=trusts_platform, now=NOW)
    return {g.cluster_id: g for g in verdicts}


def test_a_gateway_without_the_binding_closes_its_cluster_only():
    c1, c2 = _cluster("one"), _cluster("two")
    docs = {
        c1.id: [(KIND_GATEWAY, _doc(2, cluster_id=c1.id))],
        c2.id: [(KIND_GATEWAY, _doc(BOUND, cluster_id=c2.id))],
    }
    verdicts = _verdicts([c1, c2], docs)
    assert not verdicts[c1.id].gateway_gate_open
    assert [h.reason for h in verdicts[c1.id].holders] == [HOLD_NO_BINDING]
    assert verdicts[c2.id].gateway_gate_open


def test_a_failed_gateway_at_base_1_closes_its_cluster_and_an_exposed_one_every_cluster():
    c1, c2 = _cluster("one"), _cluster("two")
    failed = _doc(1, state="failed", cluster_id=c1.id)
    assert not _verdicts([c1, c2], {c1.id: [(KIND_GATEWAY, failed)]})[c1.id].gateway_gate_open
    assert _verdicts([c1, c2], {c1.id: [(KIND_GATEWAY, failed)]})[c2.id].gateway_gate_open
    exposed = _doc(1, state="failed", exposed=True, cluster_id=c1.id)
    verdicts = _verdicts([c1, c2], {c1.id: [(KIND_GATEWAY, exposed)]})
    assert not verdicts[c1.id].gateway_gate_open
    assert not verdicts[c2.id].gateway_gate_open


def test_owner_cedar_closes_the_gate_and_exposed_reaches_every_cluster():
    c1, c2 = _cluster("one"), _cluster("two")
    own = _doc(BOUND, overrides=CONFIGMAP, cluster_id=c1.id)
    verdicts = _verdicts([c1, c2], {c1.id: [(KIND_GATEWAY, own)]})
    assert [h.reason for h in verdicts[c1.id].holders] == [HOLD_OWNER_CEDAR]
    assert verdicts[c2.id].gateway_gate_open
    exposed = _doc(BOUND, overrides=CONFIGMAP, exposed=True, cluster_id=c1.id)
    verdicts = _verdicts([c1, c2], {c1.id: [(KIND_GATEWAY, exposed)]})
    assert not verdicts[c2.id].gateway_gate_open


def test_an_exposed_gateway_on_a_cluster_below_0_53_closes_every_cluster():
    old, new = _cluster("old", version="0.52.0"), _cluster("new")
    exposed = _doc(BOUND, exposed=True, cluster_id=old.id)
    verdicts = _verdicts([old, new], {old.id: [(KIND_GATEWAY, exposed)]})
    assert not verdicts[old.id].version_ok
    assert [h.reason for h in verdicts[new.id].holders] == [HOLD_EXPOSED_TOOLHIVE_TOO_OLD]
    unexposed = _doc(BOUND, cluster_id=old.id)
    assert _verdicts([old, new], {old.id: [(KIND_GATEWAY, unexposed)]})[new.id].gateway_gate_open


@pytest.mark.parametrize("state", ["deployed", "undeploying", "deleting", "failed"])
def test_an_exposed_agent_rendered_before_its_allowed_groups_closes_every_cluster(state):
    c1, c2 = _cluster("one"), _cluster("two")
    agent = _doc(0, state=state, exposed=True, cluster_id=c1.id, doc_id="ag1")
    verdicts = _verdicts([c1, c2], {c1.id: [(KIND_AGENT, agent)]})
    assert [h.reason for h in verdicts[c2.id].holders] == [HOLD_EXPOSED_AGENT_GROUPS]
    # failed 문서는 그 배포의 기록으로 읽는다. 다른 상태는 적힌 번호다
    record = DeployRender(since=NOW, epoch=1, prior=1)
    rendered = _doc(1, state=state, exposed=True, record=record, cluster_id=c1.id, doc_id="ag1")
    assert _verdicts([c1, c2], {c1.id: [(KIND_AGENT, rendered)]})[c2.id].gateway_gate_open


def test_a_failed_gateway_rendered_in_another_mode_holds_the_gate_in_local_mode():
    """oidc 모드에서 이 판이 배포했다가 실패한 Gateway 는 그 배포의 기록에 묶임 없는 번호 3 을
    남긴다. local 로 바꿔도 재적용이 failed 문서를 다시 렌더하지 않으므로 그 Gateway 가 게이트를
    닫는다."""
    c1 = _cluster("one")
    record = DeployRender(since=NOW, epoch=UNBOUND)
    failed = _doc(0, state="failed", record=record, cluster_id=c1.id)
    assert live_epochs(KIND_GATEWAY, failed) == [UNBOUND]
    verdicts = _verdicts([c1], {c1.id: [(KIND_GATEWAY, failed)]})
    assert not verdicts[c1.id].gateway_gate_open
    assert [h.reason for h in verdicts[c1.id].holders] == [HOLD_NO_BINDING]


def test_outside_local_mode_the_gate_is_closed_and_counts_nothing():
    c1 = _cluster("one")
    docs = {c1.id: [(KIND_GATEWAY, _doc(2, cluster_id=c1.id))]}
    verdict = _verdicts([c1], docs, trusts_platform=False)[c1.id]
    assert (verdict.gateway_gate_open, verdict.holders, verdict.local) == (False, [], False)
    assert _verdicts([c1], {c1.id: []})[c1.id].local


def test_a_gateways_first_deploy_holds_no_gate_and_a_retry_under_another_deployment_does():
    c1 = _cluster("one")
    first = _doc(0, state="deploying", record=DeployRender(since=NOW), cluster_id=c1.id)
    assert _verdicts([c1], {c1.id: [(KIND_GATEWAY, first)]})[c1.id].gateway_gate_open
    retried = _doc(
        0,
        state="deploying",
        record=DeployRender(since=NOW, epoch=BOUND),
        since=NOW + timedelta(seconds=1),
        cluster_id=c1.id,
    )
    assert not _verdicts([c1], {c1.id: [(KIND_GATEWAY, retried)]})[c1.id].gateway_gate_open


class _Store:
    def __init__(self, gates: list[ClusterTurnGates]):
        self.gates = gates

    async def turn_gates(self) -> list[ClusterTurnGates]:
        return self.gates


LOCAL = SimpleNamespace(auth_mode="local", data_plane_trusts_platform=True, reconcile_interval=60)


def test_a_verdict_older_than_three_intervals_and_a_tick_is_not_fresh():
    verdict = ClusterTurnGates(
        cluster_id="c1", version_ok=True, gateway_gate_open=True, verdict_at=NOW, local=True
    )
    gates = TurnGates(_Store([verdict]), LOCAL)  # type: ignore[arg-type]
    found = asyncio.run(gates.cluster("c1"))
    assert found == verdict
    assert asyncio.run(gates.cluster("c2")) is None
    assert gates.fresh(found, NOW + timedelta(seconds=259))
    assert not gates.fresh(found, NOW + timedelta(seconds=260))
    assert not gates.fresh(None, NOW)


def test_a_verdict_stays_fresh_across_two_ticks_that_spend_their_budgets():
    """판정은 재적용 단계가 문서를 읽은 때로 적힌다. 그 틱의 재렌더(30초), 간격(60초), 다음 틱의
    멈춘 문서와 클러스터 점검(50초), 그 틱의 재렌더(30초)를 지나 다음 판정이 적히기 직전까지
    젊다."""
    verdict = ClusterTurnGates(
        cluster_id="c1", version_ok=True, gateway_gate_open=True, verdict_at=NOW, local=True
    )
    gates = TurnGates(_Store([verdict]), LOCAL)  # type: ignore[arg-type]
    next_write = timedelta(seconds=30 + 60 + 50 + 30)
    assert gates.fresh(verdict, NOW + next_write)


def test_the_tick_budget_is_the_sum_of_the_budgets_of_a_tick():
    assert TICK_BUDGET_SECONDS == (
        DRIVE_SECONDS_PER_TICK + CLUSTER_CHECK_SECONDS_PER_TICK + REAPPLY_SECONDS_PER_TICK
    )


def test_a_verdict_judged_in_another_mode_is_not_fresh():
    """IdP 를 믿는 oidc 모드의 틱이 적은 판정은 묶임을 보지 않았다. local 로 바꾸거나 발급자를
    플랫폼으로 바꾼 뒤 첫 틱 전에는 쓰지 않는다. 플랫폼을 믿는 판정도 IdP 로 되돌린 뒤에는 쓰지
    않는다 (P53b 6.2절)."""
    verdict = ClusterTurnGates(
        cluster_id="c1", version_ok=True, gateway_gate_open=True, verdict_at=NOW, local=False
    )
    assert not TurnGates(_Store([verdict]), LOCAL).fresh(verdict, NOW)  # type: ignore[arg-type]
    oidc = SimpleNamespace(
        auth_mode="oidc", data_plane_trusts_platform=False, reconcile_interval=60
    )
    assert TurnGates(_Store([verdict]), oidc).fresh(verdict, NOW)  # type: ignore[arg-type]
    platform = SimpleNamespace(
        auth_mode="oidc", data_plane_trusts_platform=True, reconcile_interval=60
    )
    assert not TurnGates(_Store([verdict]), platform).fresh(verdict, NOW)  # type: ignore[arg-type]
    trusted = verdict.model_copy(update={"local": True})
    assert TurnGates(_Store([trusted]), platform).fresh(trusted, NOW)  # type: ignore[arg-type]
    assert not TurnGates(_Store([trusted]), oidc).fresh(trusted, NOW)  # type: ignore[arg-type]


def test_readers_of_the_stale_mark_read_the_replaced_render():
    """다시 렌더 표시가 붙은 Gateway 는 바꾼 렌더로 도므로 메트릭이 보는 빠진 게이트도 그 렌더의
    것이다. 옛 코드의 STALE_EPOCH 는 모른다."""
    for bound in (False, True):
        packed = _packed(RENDER_EPOCHS[KIND_GATEWAY], frozenset({Feature.AUDIT}), bound=bound)
        assert missed_gates(KIND_GATEWAY, stale_mark(packed)) == frozenset({Feature.AUDIT})
        current = pack_epoch(RENDER_EPOCHS[KIND_GATEWAY], bound=bound)
        assert missed_gates(KIND_GATEWAY, current) == frozenset()
        assert missed_gates(KIND_GATEWAY, stale_mark(current)) == frozenset()
    current = RENDER_EPOCHS[KIND_GATEWAY]
    assert missed_gates(KIND_GATEWAY, 2) is None and missed_gates(KIND_GATEWAY, 0) is None
    assert missed_gates(KIND_GATEWAY, STALE_EPOCH) is None
    assert is_stale_epoch(STALE_EPOCH) and is_stale_epoch(stale_mark(current))
    assert not is_stale_epoch(packed) and not is_stale_epoch(current)


@pytest.mark.parametrize(
    ("policy", "holds"),
    [
        ('forbid(principal, action, resource) unless { principal in THVGroup::"ops" };', True),
        ('@id("x")\nforbid (principal, action, resource);', True),
        ("permit(principal, action, resource);", False),
        ("// forbid(principal, action, resource);\npermit(principal, action, resource);", False),
        ("/* forbid(principal) */ permit(principal, action, resource);", False),
        (None, False),
    ],
)
def test_holds_forbid_is_a_text_test_outside_comments(policy, holds):
    assert holds_forbid(policy) is holds
