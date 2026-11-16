"""렌더 번호로 살아 있는 문서를 다시 렌더하기 (P50 1절).

번호가 오르면 그 종류의 살아 있는 문서가 모두 다시 렌더되고, 인증 절반이 다른 문서가 먼저 가며,
번호만 다른 문서와 원본이 바뀐 공유 복제본은 한 네임스페이스에서 몇 개씩만 간다. 네임스페이스
객체를 맞추지 못한 네임스페이스만 기다리고, 상태 API 는 종류마다의 수와 막힌 이유를 보인다.
Agent 는 새 파드가 받을 컴파일이 어긋나면 굴리지 않고 막힌다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import get_args

from pydantic import BaseModel

from daegam.api.schemas.tenancy import ReapplyKindName
from daegam.services import data_plane_auth as dpa
from daegam.services.agents import AgentError
from daegam.services.budget import Budget
from daegam.services.data_plane_auth import (
    KIND_AGENT,
    KIND_CLONE,
    KIND_GATEWAY,
    KIND_MCP_SERVER,
    RENDER_EPOCHS,
    DataPlaneAuth,
    _Target,
    _Tick,
)
from daegam.services.tenancy import INTERNAL_OIDC_CONFIG_NAME, TOOLHIVE_API
from daegam.store.auth_reapply import (
    AuthBlocked,
    ClusterAuthState,
    ConsumerAuthState,
    is_stale_mark,
    rendered_epoch,
)
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.exposure import front_and_gateway
from tests.helpers.workers import raise_epoch, run_data_plane_auth

# -- 순서와 속도 (순수) ---------------------------------------------------------------------------


class _K8s(BaseModel):
    namespace: str


class _Doc(ConsumerAuthState):
    id: str
    k8s: _K8s
    cluster_id: str = "c1"
    last_spec_change: datetime | None = None


def _target(
    doc_id: str,
    ns: str,
    calls: list[str],
    *,
    auth_behind: bool = False,
    cluster: str = "c1",
    kind: str = KIND_GATEWAY,
) -> _Target:
    async def reapply_auth(doc):
        calls.append(doc.id)
        return True

    service = SimpleNamespace(reapply_auth=reapply_auth, auth_kind=kind, profile_kind=kind)
    doc = _Doc(
        id=doc_id,
        k8s=_K8s(namespace=ns),
        cluster_id=cluster,
        auth_rev="old" if auth_behind else "want",
    )
    return _Target(service, doc, "want", 1)


def _auth() -> DataPlaneAuth:
    none = SimpleNamespace()
    kinds = ("gateways", "mcp_servers", "agents", "hub_items", "shared_clones")
    return DataPlaneAuth(none, none, **dict.fromkeys(kinds, none))


def _tick() -> _Tick:
    return _Tick(now=utcnow(), install_id="i", tenant_settings=None)


def _run(targets: list[_Target], tick: _Tick) -> None:
    asyncio.run(_auth()._reapply(targets, tick))


def test_auth_changes_go_first_and_only_epochs_meet_the_namespace_cap():
    """인증 절반이 다른 문서가 먼저 다시 렌더된다. 번호만 다른 문서는 한 네임스페이스에서
    `REAPPLY_PER_NAMESPACE` 개까지이고, 인증 절반이 다른 문서는 그 수에 막히지 않는다."""
    calls: list[str] = []
    epochs = [_target(f"e{i}", "a", calls) for i in range(4)]
    auth = [_target(f"auth{i}", "a", calls, auth_behind=True) for i in range(4)]
    other = [_target("other", "b", calls)]
    _run([*epochs, *auth, *other], _tick())
    assert calls[:4] == ["auth0", "auth1", "auth2", "auth3"]
    # 네임스페이스 a 는 인증 넷으로 이미 셋을 넘겼으므로 번호만 다른 문서는 가지 않는다
    assert calls[4:] == ["other"]
    rendered = {c.doc.id: (c.doc.auth_rev, c.doc.render_epoch) for c in [*auth, *other]}
    assert set(rendered.values()) == {("want", 1)}


def test_a_changed_clone_source_waits_for_the_namespace_cap():
    """공유 복제본의 판 첫 절반은 원본 정의의 판이라, 원본이 바뀐 복제본은 인증 변경처럼 먼저 가지
    않고 번호만 다른 문서처럼 한 네임스페이스에서 `REAPPLY_PER_NAMESPACE` 개까지만 간다."""
    calls: list[str] = []
    clones = [_target(f"c{i}", "a", calls, auth_behind=True, kind=KIND_CLONE) for i in range(4)]
    revoke = _target("revoke", "a", calls, auth_behind=True)
    _run([*clones, revoke], _tick())
    assert calls == ["revoke", "c0", "c1"]


def test_auth_changes_on_one_cluster_go_before_epoch_work_on_another():
    """한 틱의 몫은 클러스터를 가리지 않고 인증 절반이 다른 문서부터 쓴다. 큰 클러스터의 렌더 번호
    일거리가 다른 클러스터의 회수를 밀어내지 않는다."""
    calls: list[str] = []
    busy = [_target(f"a{i}", f"ns{i}", calls, cluster="a") for i in range(40)]
    revoke = _target("b-revoke", "ns", calls, auth_behind=True, cluster="b")
    _run([*busy, revoke], _tick())
    assert calls[0] == "b-revoke" and len(calls) == 10


def test_a_block_record_keeps_a_string_revision_for_the_previous_release():
    """인증 입력을 쓰지 않는 문서의 막힘도 인증 절반을 빈 문자열로 적어, 문자열만 읽는 이전
    릴리스의 모델이 그 문서를 읽을 수 있다. 막힘은 그대로 유지된다."""
    doc = _Doc(id="d", k8s=_K8s(namespace="a"))
    doc.auth_blocked = AuthBlocked(rev="", code="locked_field", epoch=1)
    target = _Target(SimpleNamespace(), doc, None, 1)
    assert target.blocked

    class PreviousAuthBlocked(BaseModel):
        rev: str
        code: str

    PreviousAuthBlocked.model_validate(doc.auth_blocked.model_dump())


def test_epoch_rerenders_stop_at_the_namespace_cap_and_the_time_budget():
    calls: list[str] = []
    _run([_target(f"e{i}", "a", calls) for i in range(5)], _tick())
    assert calls == ["e0", "e1", "e2"]
    late = _tick()
    late.clock = Budget(-1)
    calls.clear()
    _run([_target("e9", "b", calls)], late)
    assert calls == []


def test_a_cluster_state_admits_namespaces_whose_objects_converged():
    state = ClusterAuthState(cluster_id="c1", drifted=["a"])
    assert not state.admits("a") and state.admits("b")
    state.unreachable = True
    assert not state.admits("b")


# -- 리컨실 단계에서 ---------------------------------------------------------------------------


def test_a_namespace_whose_objects_failed_waits_and_the_others_go(
    client, fake_clusters, mongo, monkeypatch
):
    """한 네임스페이스의 OIDC 설정을 다시 보장하지 못하면 그 네임스페이스의 문서만 기다린다.
    다른 네임스페이스의 문서는 같은 틱에 다시 렌더된다."""
    _front, gateway, fake = front_and_gateway(client, fake_clusters)
    other = {**mongo["gateways"].find_one({"_id": gateway["id"]}), "_id": "gw-other"}
    other["name"] = "other"
    other["k8s"] = {**other["k8s"], "namespace": "daegam-other"}
    mongo["gateways"].insert_one(other)
    fake.delete(TOOLHIVE_API, "MCPOIDCConfig", INTERNAL_OIDC_CONFIG_NAME, DEFAULT_NS)
    ensure = dpa.ensure_oidc_configs

    def failing(client_, tenant, ns, *args, **kw):
        if ns == DEFAULT_NS:
            raise RuntimeError("quota exceeded")
        return ensure(client_, tenant, ns, *args, **kw)

    monkeypatch.setattr(dpa, "ensure_oidc_configs", failing)
    raise_epoch(monkeypatch, KIND_GATEWAY)
    now = utcnow()
    result = run_data_plane_auth(client, now=now)
    assert result["reapplied"] == ["gw-other"]
    assert f"cluster:{gateway['cluster_id']}" in result["failed"]
    monkeypatch.setattr(dpa, "ensure_oidc_configs", ensure)
    later = run_data_plane_auth(client, now=now + timedelta(minutes=6))
    assert later["reapplied"] == [gateway["id"]]


def test_an_agent_whose_new_pod_would_not_compile_is_blocked_not_rolled(
    client, fake_clusters, mongo, monkeypatch
):
    """새 파드가 뜰 때 받을 컴파일이 어긋나면(provider 삭제 등) 재렌더가 Deployment 를 적용하기
    전에 422 로 멈추고, 그 문서는 막혀 재배포를 기다린다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    service = client.app.state.agent_service

    async def broken(doc, *, allowed=None, token_envs=None):
        raise AgentError(422, "invalid_graph", "provider local-stub is gone")

    monkeypatch.setattr(service, "compile", broken)
    raise_epoch(monkeypatch, KIND_AGENT)
    before = [d for d in agent.fake.applied if d["kind"] == "Deployment"]
    assert run_data_plane_auth(client)["blocked"] == [agent.doc["id"]]
    after = [d for d in agent.fake.applied if d["kind"] == "Deployment"]
    assert after == before
    stored = mongo["agents"].find_one({"_id": agent.doc["id"]})
    assert stored["auth_blocked"]["code"] == "invalid_graph"


def test_a_rerender_that_raced_a_redeploy_is_done_again_on_the_next_tick(
    client, fake_clusters, mongo, monkeypatch
):
    """다시 렌더하는 사이 재배포가 끝나면(`deploying_since` 가 바뀌면) 그 렌더를 저장하지 않고 렌더
    번호를 다시 렌더 표시로 적어, 다음 틱이 새 배포의 스냅숏으로 다시 렌더한다. 표시는 저장된 번호를
    담는다 (P53 3.5절)."""
    _front, gateway, _fake = front_and_gateway(client, fake_clusters)
    service = client.app.state.gateway_service
    apply = service._apply_consumer

    async def racing(doc, client_, *, rotate):
        await apply(doc, client_, rotate=rotate)
        mongo["gateways"].update_one({"_id": doc.id}, {"$set": {"deploying_since": utcnow()}})

    monkeypatch.setattr(service, "_apply_consumer", racing)
    before = mongo["gateways"].find_one({"_id": gateway["id"]})["render_epoch"]
    epoch = raise_epoch(monkeypatch, KIND_GATEWAY)
    assert run_data_plane_auth(client)["reapplied"] == []
    marked = mongo["gateways"].find_one({"_id": gateway["id"]})["render_epoch"]
    assert is_stale_mark(marked) and rendered_epoch(marked) == before
    monkeypatch.setattr(service, "_apply_consumer", apply)
    assert run_data_plane_auth(client)["reapplied"] == [gateway["id"]]
    assert mongo["gateways"].find_one({"_id": gateway["id"]})["render_epoch"] == epoch


def test_the_kind_names_of_the_status_api_are_the_epoch_kinds():
    assert set(get_args(ReapplyKindName)) == set(RENDER_EPOCHS)
    assert KIND_MCP_SERVER in RENDER_EPOCHS
