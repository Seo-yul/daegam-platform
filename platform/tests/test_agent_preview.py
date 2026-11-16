"""미리 보기의 시작, 옮기기, 자리, 하루 시작 수, 일일 예산, 끝 (P44b 5절·7절·10절, D107).

미리 보기는 원본의 최신 리비전을 같은 번호로 memory 노드 없이 돈다. 공유 상태 두 규칙을 보지 않아
승인과 분당 한도가 있는 그래프도 파드 하나로 배포되고 agent.json 의 `memory` 는 null 이다. 한
사람이 Agent 하나에 하나를 열고, 다시 시작하면 그 자리에서 옮기며 끝은 그대로다. 테넌트 자리와 하루
시작 수가 상한이다. 끝은 Agent 삭제이고 까닭을 남기며 자리를 놓는다.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import timedelta

import pytest

from daegam.services import agent_graph
from daegam.services.agent_features import (
    IN_POD_STATE,
    KNOWN,
    TOOL_SAFETY,
)
from daegam.services.agent_preview import (
    PREVIEW_IDLE,
    STARTS_PER_DAY,
    PreviewError,
    due_reason,
    last_activity,
    preview_spec,
)
from daegam.store.agents import AgentRevision
from daegam.store.base import utcnow
from daegam.store.memberships import Membership
from daegam.store.personal_tokens import DELETED, ROLE_CHANGED
from daegam.store.usage import TokenUsage, UsageProvider, UsageRecord, UsageSource
from tests.helpers.agent_runtime import agent_deployment, deployed_agent, token_of
from tests.helpers.agent_sleep import in_app
from tests.helpers.agents import (
    agent_body,
    agent_env,
    create_agent,
    issues_of,
    make_ctx,
    node_of,
    template_for,
    template_spec,
    without_memory,
)
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS
from tests.helpers.http import api_client, make_app
from tests.helpers.previews import (
    HEADER_SUB,
    agent_json,
    preview_service,
    source_doc,
    start_preview,
)


def _stored(mongo, preview_id: str) -> dict | None:
    return mongo["agents"].find_one({"_id": preview_id})


def test_in_pod_state_skips_the_two_shared_state_rules():
    """승인과 분당 한도가 있는 그래프에서 memory 노드를 빼면 `memory_required` 다. 미리 보기의
    검증은 그 규칙과 `long_term_needs_memory` 를 보지 않는다."""
    spec = without_memory(template_spec())
    assert "memory_required" in {i["code"] for i in issues_of(spec)}
    ctx = dataclasses.replace(make_ctx(), in_pod_state=True)
    assert "memory_required" not in {i["code"] for i in issues_of(spec, ctx)}


def test_the_preview_spec_drops_memory_nodes_and_their_edges():
    rev = AgentRevision(revision=4, **template_spec())
    out = preview_spec(rev)
    assert out.revision == 4 and out.graph.node("memory") is None
    assert all(e.kind != "memory" for e in out.graph.edges)
    assert len(out.graph.nodes) == len(rev.graph.nodes) - 1
    # 원본은 그대로다
    assert rev.graph.node("memory") is not None


def test_a_preview_runs_the_latest_saved_revision_in_one_pod(client, fake_clusters, mongo):
    """원본의 최신 리비전(배포한 것과 다르다)을 같은 번호로, memory 없이, 원본의 클러스터와
    네임스페이스에 파드 하나로 띄운다. 노출도 잠들기도 없다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    changed = copy.deepcopy(agent.spec())
    node_of(changed, "entry")["data"]["greeting"] = "미리 보기 인사"
    assert client.put(agent.url, json=agent_body(changed)).status_code == 200
    preview = start_preview(client, agent.id)
    assert preview.preview is not None and preview.state == "deployed"
    assert preview.name == preview.id and len(preview.id) == 36
    assert preview.display_name == "support" and preview.revision == 2
    assert preview.deployed_revision == 2 and preview.spec.graph.node("memory") is None
    assert preview.cluster_id == agent.cluster_id and preview.k8s is not None
    assert preview.k8s.namespace == DEFAULT_NS and preview.k8s.name == f"agent-{preview.id}"
    assert preview.replicas == 1 and preview.scale_to_zero is False and preview.bindings == []
    assert not preview.exposure.enabled
    ends = preview.preview.ends_at - preview.preview.started_at
    assert ends == timedelta(hours=2)
    # 원본은 그대로 r1 을 배포하고 있다
    stored = _stored(mongo, agent.id)
    assert stored["deployed_revision"] == 1 and "preview" not in stored
    # 파드가 받는 agent.json 은 memory 가 null 이고 새 인사를 싣는다
    r = client.get(f"{AG}/{preview.id}/agent.json", headers=bearer(token_of(agent.fake, preview)))
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["memory"] is None and doc["entry"]["greeting"] == "미리 보기 인사"
    assert agent_graph.check_schema(doc) == []
    assert agent_deployment(agent.fake, preview.k8s.name)["spec"]["replicas"] == 1
    # 자리 하나를 쥐었다
    (slot,) = list(mongo["agent_preview_slots"].find())
    assert slot["preview_id"] == preview.id and slot["tenant"] == "default"


def _features_of(client, fake, doc, names: frozenset[str]) -> list[str]:
    """doc 의 파드가 names 를 알리며 받은 agent.json 의 features 다."""
    r = agent_json(client, fake, doc, names)
    assert r.status_code == 200, r.text
    return r.json().get("features", [])


def _plain(spec: dict) -> dict:
    """memory 노드가 없어도 저장되는 spec 이다. 공유 상태를 요구하는 승인과 분당 한도를 뺀다."""
    out = without_memory(spec)
    node_of(out, "tools-calculator")["data"]["approval"] = {}
    node_of(out, "entry")["data"]["rate_limit"] = None
    return out


def test_a_preview_asks_only_where_its_source_would(client, fake_clusters, mongo):
    """원본의 리비전에 memory 노드가 있으면 미리 보기의 agent.json 이 문서 기능 `in_pod_state` 를
    실어 런타임이 memory 없이도 승인을 묻는다. 원본의 것은 싣지 않는다. memory 노드가 없는
    리비전으로 옮기면 그 이름이 빠져 배포한 원본처럼 거절한다 (7절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    assert {IN_POD_STATE, TOOL_SAFETY} <= set(_features_of(client, agent.fake, preview, KNOWN))
    source = _features_of(client, agent.fake, source_doc(client, agent.id), KNOWN)
    assert TOOL_SAFETY in source and IN_POD_STATE not in source
    assert client.put(agent.url, json=agent_body(_plain(agent.spec()))).status_code == 200
    moved = start_preview(client, agent.id)
    assert moved.id == preview.id and moved.deployed_revision == 2
    features = _features_of(client, agent.fake, moved, KNOWN)
    assert TOOL_SAFETY in features and IN_POD_STATE not in features


def test_a_preview_of_a_source_without_memory_refuses_like_its_source(client, fake_clusters, mongo):
    _cid, ds, gw = agent_env(client, mongo)
    draft = create_agent(client, _plain(template_for(gw, ds)))
    assert draft.status_code == 201, draft.text
    preview = start_preview(client, draft.json()["id"])
    fake = fake_clusters.by_name["edge"]
    features = _features_of(client, fake, preview, KNOWN)
    assert TOOL_SAFETY in features and IN_POD_STATE not in features


def test_an_image_without_in_pod_state_runs_the_preview_with_a_warning(
    client, fake_clusters, mongo
):
    """그 이름을 모르는 이미지에는 빼고, init 받기가 남긴 `runtime_feature_dropped` 를 미리 보기의
    응답이 `warnings` 로 싣는다. 그 미리 보기는 승인을 거절하며 돈다 (7절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    assert client.get(f"{agent.url}/preview").json()["warnings"] == []
    old = _features_of(client, agent.fake, preview, KNOWN - {IN_POD_STATE})
    assert TOOL_SAFETY in old and IN_POD_STATE not in old
    [warning] = client.get(f"{agent.url}/preview").json()["warnings"]
    assert warning.startswith("runtime_feature_dropped: ") and IN_POD_STATE in warning


def test_a_draft_is_previewed_where_a_deploy_would_go(client, fake_clusters, mongo):
    cid, ds, gw = agent_env(client, mongo)
    draft = create_agent(client, template_for(gw, ds)).json()
    preview = start_preview(client, draft["id"])
    assert preview.state == "deployed" and preview.cluster_id == cid
    assert preview.k8s is not None and preview.k8s.namespace == DEFAULT_NS
    assert _stored(mongo, draft["id"])["state"] == "draft"


def test_starting_again_moves_the_preview_in_place_and_keeps_its_end(client, fake_clusters, mongo):
    """같은 리비전이면 그대로이고 세지 않는다. 원본을 고친 뒤면 같은 문서를 새 리비전으로 옮기고
    파드가 굴러간다. 끝은 처음 시작의 것이다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    first = start_preview(client, agent.id)
    again = start_preview(client, agent.id)
    assert again.id == first.id and again.revision == first.revision
    assert mongo["agent_preview_starts"].find_one()["count"] == 1
    changed = copy.deepcopy(agent.spec())
    node_of(changed, "entry")["data"]["greeting"] = "바뀐 인사"
    assert client.put(agent.url, json=agent_body(changed)).status_code == 200
    moved = start_preview(client, agent.id)
    assert moved.id == first.id and moved.revision == 2 and moved.deployed_revision == 2
    assert moved.preview == first.preview
    assert {r.revision for r in moved.revisions} == {1, 2}
    assert mongo["agent_preview_starts"].find_one()["count"] == 2
    assert mongo["agents"].count_documents({"preview.source": agent.id}) == 1


def test_each_person_holds_one_preview_and_the_tenant_holds_a_few(
    settings, mongo, fake_clusters, fake_secrets
):
    """자리 수가 테넌트의 상한이다. 찬 뒤의 시작은 409 `preview_limit` 이고, 끝내면 자리가 난다."""
    app = make_app(
        dataclasses.replace(settings, agent_previews_per_tenant=2), fake_clusters, fake_secrets
    )
    with api_client(app) as client:
        agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
        a = start_preview(client, agent.id, "dev-a")
        b = start_preview(client, agent.id, "dev-b")
        assert a.id != b.id
        with pytest.raises(PreviewError) as e:
            start_preview(client, agent.id, "dev-c")
        assert (e.value.status, e.value.code) == (409, "preview_limit")
        # 시작하지 못한 미리 보기는 남지 않는다
        assert mongo["agents"].count_documents({"preview.source": agent.id}) == 2
        assert in_app(client, preview_service(client).end, a, "ended", by="dev-a")
        c = start_preview(client, agent.id, "dev-c")
        assert c.state == "deployed"
        assert {s["preview_id"] for s in mongo["agent_preview_slots"].find()} == {b.id, c.id}


def test_a_person_starts_or_moves_twenty_previews_a_day(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    start_preview(client, agent.id)
    mongo["agent_preview_starts"].update_many({}, {"$set": {"count": STARTS_PER_DAY}})
    changed = copy.deepcopy(agent.spec())
    node_of(changed, "entry")["data"]["greeting"] = "스물한 번째"
    assert client.put(agent.url, json=agent_body(changed)).status_code == 200
    with pytest.raises(PreviewError) as e:
        start_preview(client, agent.id)
    assert (e.value.status, e.value.code) == (429, "preview_start_limit")
    assert 0 < int(e.value.headers["Retry-After"]) <= 86400
    # 다른 사람은 따로 센다
    assert start_preview(client, agent.id, "dev-b").state == "deployed"


def test_the_daily_budget_counts_the_source_ledger_with_the_preview_rows(
    client, fake_clusters, mongo
):
    """핀한 리비전의 daily_tokens 에 원본의 오늘 원장 합계(미리 보기의 행 포함)가 닿으면 429 다."""

    def with_budget(gw, ds):
        spec = template_for(gw, ds)
        spec["service"]["budget"]["daily_tokens"] = 1000
        return spec

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=with_budget)
    preview = start_preview(client, agent.id)
    previews = preview_service(client)
    in_app(client, previews.check_budget, preview)

    def row(rid: str, tokens: int, preview_id: str | None = None, days_ago: int = 0) -> None:
        record = UsageRecord(
            id=rid,
            ts=utcnow() - timedelta(days=days_ago),
            writer="runtime",
            route="direct",
            tenant="default",
            source=UsageSource(kind="agent", id=agent.id, preview=preview_id),
            provider=UsageProvider(kind="openai"),
            model="m",
            usage=TokenUsage(input_tokens=tokens, output_tokens=tokens),
        )
        mongo["usage_records"].insert_one(record.to_doc())

    row("r-old", 5000, days_ago=2)
    row("r-1", 300)
    in_app(client, previews.check_budget, preview)
    row("r-2", 200, preview_id=preview.id)
    with pytest.raises(PreviewError) as e:
        in_app(client, previews.check_budget, preview)
    assert (e.value.status, e.value.code) == (429, "daily_budget_exceeded")
    assert e.value.extra["used"] == 1000 and "Retry-After" in e.value.headers


def test_ending_a_preview_deletes_it_frees_its_slot_and_records_why(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    assert preview.k8s is not None
    assert agent_deployment(agent.fake, preview.k8s.name) is not None
    previews = preview_service(client)
    assert in_app(client, previews.end, preview, "ended_by_other", by="dev-b")
    assert _stored(mongo, preview.id) is None
    assert agent_deployment(agent.fake, preview.k8s.name) is None
    assert mongo["agent_preview_slots"].count_documents({}) == 0
    tokens = list(mongo["agent_instance_tokens"].find({"agent_instance_id": preview.id}))
    assert tokens and all(t["revoked_at"] is not None for t in tokens)
    source = source_doc(client, agent.id)
    with pytest.raises(PreviewError) as e:
        in_app(client, previews.require_mine, source, HEADER_SUB)
    assert (e.value.status, e.value.code) == (404, "preview_not_found")
    assert e.value.extra["ended"]["reason"] == "ended_by_other"
    assert e.value.extra["ended"]["by"] == "dev-b"
    # 다시 시작하면 앞의 끝은 지워진다
    start_preview(client, agent.id)
    assert mongo["agent_preview_ends"].count_documents({}) == 0


def test_a_preview_is_due_after_its_idle_window_or_its_two_hours(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    assert preview.preview is not None
    start = preview.preview.started_at
    # 배포가 적은 활동이 창의 시작이다
    base = last_activity(preview)
    assert start <= base < start + timedelta(minutes=1)
    assert due_reason(preview, base + PREVIEW_IDLE - timedelta(seconds=1), enabled=True) is None
    assert due_reason(preview, base + PREVIEW_IDLE, enabled=True) == "idle"
    busy = preview.model_copy(deep=True)
    busy.sleep.activity = start + timedelta(minutes=100)
    assert due_reason(busy, start + timedelta(minutes=110), enabled=True) is None
    assert due_reason(busy, start + timedelta(hours=2), enabled=True) == "max_age"
    assert due_reason(preview, start, enabled=False) == "disabled"


def _tick(client) -> None:
    worker = client.app.state.agent_preview_worker
    client.portal.call(worker.tick)


def test_the_worker_ends_idle_previews_and_those_whose_source_is_gone(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    idle = start_preview(client, agent.id, "dev-a")
    fresh = start_preview(client, agent.id, "dev-b")
    old = utcnow() - PREVIEW_IDLE - timedelta(minutes=1)
    mongo["agents"].update_one(
        {"_id": idle.id}, {"$set": {"preview.started_at": old, "sleep.activity": old}}
    )
    _tick(client)
    assert _stored(mongo, idle.id) is None and _stored(mongo, fresh.id) is not None
    assert mongo["agent_preview_ends"].find_one({"preview_id": idle.id})["reason"] == "idle"
    # 원본 문서가 사라지면(지우기가 미리 보기를 끝내지 못한 뒤) 워커가 끝낸다
    mongo["agents"].delete_one({"_id": agent.id})
    _tick(client)
    assert _stored(mongo, fresh.id) is None
    end = mongo["agent_preview_ends"].find_one({"preview_id": fresh.id})
    assert end["reason"] == "source_deleted"


def test_turning_previews_off_ends_them_all_within_a_tick(
    settings, mongo, fake_clusters, fake_secrets
):
    app = make_app(settings, fake_clusters, fake_secrets)
    with api_client(app) as client:
        agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
        preview = start_preview(client, agent.id)
        client.app.state.agent_previews.settings = dataclasses.replace(
            settings, agent_previews_enabled=False
        )
        _tick(client)
        assert _stored(mongo, preview.id) is None
        with pytest.raises(PreviewError) as e:
            start_preview(client, agent.id)
        assert (e.value.status, e.value.code) == (409, "preview_disabled")


def test_deleting_the_source_ends_its_previews(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    assert client.delete(agent.url).status_code == 200
    assert _stored(mongo, preview.id) is None
    end = mongo["agent_preview_ends"].find_one({"preview_id": preview.id})
    assert end["reason"] == "source_deleted"


def test_a_person_who_loses_access_or_agent_deploy_loses_their_previews(
    client, fake_clusters, mongo
):
    """회수 훅이 그 사람의 미리 보기를 끝낸다. 역할이 낮아졌어도 `agent:deploy` 가 남으면
    그대로다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    kept = start_preview(client, agent.id, "dev-a")
    lost = start_preview(client, agent.id, "dev-b")
    gone = start_preview(client, agent.id, "dev-c")
    for sub, role in (("dev-a", "developer"), ("dev-b", "viewer")):
        row = Membership(sub=sub, tenant="default", role=role).model_dump()
        mongo["memberships"].insert_one(row)
    # 역할을 낮춘 멤버 교체가 부르는 회수 훅이다
    revoker = client.app.state.membership_service.revoker
    for sub in ("dev-a", "dev-b"):
        in_app(client, revoker.revoke_tokens_for, sub, ROLE_CHANGED, by="admin", tenant="default")
    assert _stored(mongo, kept.id) is not None and _stored(mongo, lost.id) is None
    # 소속이나 계정을 잃은 사람의 훅이다
    in_app(client, revoker.revoke_for, "dev-c", DELETED, by="admin")
    assert _stored(mongo, gone.id) is None
    reasons = {e["preview_id"]: e["reason"] for e in mongo["agent_preview_ends"].find()}
    assert reasons == {lost.id: "access_lost", gone.id: "access_lost"}


def test_the_worker_frees_slots_left_by_a_failed_start(client, fake_clusters, mongo):
    """미리 보기가 없는 자리는 시작의 틈(`SLOT_GRACE`)이 지나면 놓는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    now = utcnow()
    for n, ghost, taken in ((1, "ghost-old", now - timedelta(minutes=11)), (2, "ghost-new", now)):
        slot = {"tenant": "default", "n": n, "preview_id": ghost, "taken_at": taken}
        mongo["agent_preview_slots"].insert_one({"_id": f"default:{n}", **slot})
    _tick(client)
    held = {s["preview_id"] for s in mongo["agent_preview_slots"].find()}
    assert held == {preview.id, "ghost-new"}


def test_previews_of_a_source_never_sleep(client, fake_clusters, mongo):
    """쉬기 워커는 미리 보기를 보지 않는다 (P44 와 함께, 7절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    sleep = client.app.state.agent_service.sleep
    found = client.portal.call(
        lambda: sleep.agents.sleep_candidates(
            idle_before=utcnow() + timedelta(days=1),
            stale_before=utcnow(),
            capability="scale_to_zero",
        )
    )
    assert preview.id not in {d.id for d in found}


def test_a_person_finds_only_their_own_preview(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    mine = start_preview(client, agent.id, "dev-a")
    source = source_doc(client, agent.id)
    previews = preview_service(client)
    assert in_app(client, previews.require_mine, source, "dev-a").id == mine.id
    with pytest.raises(PreviewError):
        in_app(client, previews.require_mine, source, "dev-b")
    assert [p.id for p in in_app(client, previews.of_source, source)] == [mine.id]


def test_a_preview_past_its_end_is_refused_before_the_worker_ends_it(client, fake_clusters, mongo):
    """워커가 아직 끝내지 않았어도 끝날 때가 지난 미리 보기는 그 사람에게 404 이고 까닭을 싣는다.
    그 뒤로는 턴도 턴 토큰도 나지 않는다. 이벤트 수집도 그 자격의 끝 뒤에는 받지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    source = source_doc(client, agent.id)
    previews = preview_service(client)
    started = utcnow() - timedelta(hours=2, minutes=11)
    mongo["agents"].update_one(
        {"_id": preview.id},
        {"$set": {"preview.started_at": started, "preview.ends_at": started + timedelta(hours=2)}},
    )
    with pytest.raises(PreviewError) as e:
        in_app(client, previews.require_mine, source, HEADER_SUB)
    assert (e.value.status, e.value.code) == (404, "preview_not_found")
    assert e.value.extra["ended"]["reason"] == "max_age"
    chat = client.post(f"{AG}/{agent.id}/preview/chat", json={"message": "x"})
    assert chat.status_code == 404
    # 수집 경로는 그 토큰을 받지 않는다
    from tests.helpers.telemetry import EVENTS, envelope, run_event

    body = envelope(preview.deployed_revision, [run_event("turn_start", 1.0)])
    refused = client.post(EVENTS, json=body, headers=bearer(token_of(agent.fake, preview)))
    assert refused.status_code == 401
    # 다른 사람의 목록에서는 아직 보이고 끝낼 수 있다
    assert in_app(client, previews.require_of_source, source, preview.id).id == preview.id


def test_turn_tokens_of_a_preview_end_with_its_credentials(client, fake_clusters, mongo):
    """턴 토큰의 수명은 위임의 마감과 미리 보기 자격의 끝(끝 + 10분)을 넘지 않는다."""
    from daegam.store.turn_grants import GrantTarget, TurnGrant

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    assert preview.preview is not None
    now = utcnow()
    grant = TurnGrant(
        id="g-1",
        secret_sha256="x",
        agent_id=preview.id,
        tenant="default",
        sub="person-1",
        trigger="chat",
        targets=[GrantTarget(kind="gateway_instance", id="gw")],
        opened_at=now,
        deadline=now + timedelta(hours=1),
        purge_at=now + timedelta(days=1),
    )
    grants = client.app.state.turn_grants
    near = preview.preview.credentials_end - timedelta(seconds=60)
    assert grants._ttl(grant, near, preview) <= 60
    assert grants._ttl(grant, preview.preview.credentials_end, preview) <= 0
    source = source_doc(client, agent.id)
    assert grants._ttl(grant, now, source) == client.app.state.settings.turn_token_ttl_seconds


def test_the_worker_reads_a_preview_again_before_ending_it(client, fake_clusters, mongo):
    """틱이 고른 뒤 턴이 와 활동이 앞으로 갔으면 끝내지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    old = utcnow() - PREVIEW_IDLE - timedelta(minutes=1)
    mongo["agents"].update_one(
        {"_id": preview.id}, {"$set": {"preview.started_at": old, "sleep.activity": old}}
    )
    previews = preview_service(client)
    (picked,) = in_app(client, previews.due, utcnow())
    mongo["agents"].update_one({"_id": preview.id}, {"$set": {"sleep.activity": utcnow()}})
    assert in_app(client, previews.end_if_due, picked, utcnow()) is False
    assert _stored(mongo, preview.id) is not None


def test_a_start_that_stopped_before_its_deploy_is_started_again(client, fake_clusters, mongo):
    """시작이 문서를 넣고 배포 전에 멈췄으면(요청 취소 등) 남은 draft 는 다음 시작이 지우고 새로
    연다. 다음 시작이 그것을 그대로 돌려주지 않는다."""
    from daegam.services.agent_preview import _new_preview

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    source = source_doc(client, agent.id)
    stuck = _new_preview(source, HEADER_SUB, utcnow(), preview_spec(source.current))
    in_app(client, client.app.state.agents.create, stuck)
    mongo["agent_preview_slots"].insert_one(
        {
            "_id": "default:0",
            "tenant": "default",
            "n": 0,
            "preview_id": stuck.id,
            "taken_at": utcnow(),
        }
    )
    fresh = start_preview(client, agent.id)
    assert fresh.id != stuck.id and fresh.state == "deployed"
    assert _stored(mongo, stuck.id) is None
    assert [s["preview_id"] for s in mongo["agent_preview_slots"].find()] == [fresh.id]


def test_starts_that_meet_an_ending_or_a_racing_preview_answer_in_progress(
    client, fake_clusters, mongo, monkeypatch
):
    """앞의 미리 보기가 지우는 중이면 409 `in_progress` 다. 같은 사람의 두 시작이 겹쳐 진 쪽은
    409 이고 그 자리를 곧바로 놓는다. 배포 검증에서 멈춘 시작은 문서를 지우고 자리를 놓는다."""
    from daegam.services.agents import AgentError

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    previews = preview_service(client)
    mongo["agents"].update_one({"_id": preview.id}, {"$set": {"state": "deleting"}})
    with pytest.raises(PreviewError) as e:
        start_preview(client, agent.id)
    assert (e.value.status, e.value.code) == (409, "in_progress")
    mongo["agents"].update_one({"_id": preview.id}, {"$set": {"state": "deployed"}})

    async def nothing(_source, _sub):
        return None

    monkeypatch.setattr(previews, "mine", nothing)
    with pytest.raises(PreviewError) as e:
        start_preview(client, agent.id)
    assert (e.value.status, e.value.code) == (409, "in_progress")
    assert [s["preview_id"] for s in mongo["agent_preview_slots"].find()] == [preview.id]
    monkeypatch.undo()

    async def refused(*_args, **_kw):
        raise AgentError(422, "invalid_graph", "refused for the test")

    monkeypatch.setattr(client.app.state.agent_service, "deploy", refused)
    with pytest.raises(AgentError):
        start_preview(client, agent.id, "dev-b")
    assert mongo["agents"].count_documents({"preview.owner": "dev-b"}) == 0
    assert [s["preview_id"] for s in mongo["agent_preview_slots"].find()] == [preview.id]


def test_a_move_whose_redeploy_failed_moves_again_on_the_next_start(
    client, fake_clusters, mongo, monkeypatch
):
    """옮기기는 리비전을 들인 뒤 재배포한다. 재배포가 실패하면 문서는 새 리비전이고 파드는 앞의
    리비전이므로 다음 시작이 그대로 돌려주지 않고 다시 옮긴다."""
    from daegam.services.agents import AgentError

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    first = start_preview(client, agent.id)
    changed = copy.deepcopy(agent.spec())
    node_of(changed, "entry")["data"]["greeting"] = "바뀐 인사"
    assert client.put(agent.url, json=agent_body(changed)).status_code == 200

    async def busy(*_args, **_kw):
        raise AgentError(409, "in_progress", "claimed for the test")

    service = client.app.state.agent_service
    monkeypatch.setattr(service, "redeploy", busy)
    with pytest.raises(AgentError):
        start_preview(client, agent.id)
    stuck = _stored(mongo, first.id)
    assert (stuck["revision"], stuck["deployed_revision"]) == (2, 1)
    monkeypatch.undo()

    moved = start_preview(client, agent.id)
    assert moved.id == first.id and moved.deployed_revision == 2


def test_a_preview_remembers_no_approval_choices(client, fake_clusters, mongo):
    """미리 보기는 기억하지 않으므로 승인 카드가 기억할 선택을 내지 않고 결정의 `remember` 는
    거절한다 (7절)."""
    from daegam.services.agent_features import TOOL_SAFETY
    from daegam.services.approval_choices import ApprovalChoiceService
    from daegam.store.agents import AgentRuntime

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    image = preview.snapshot_image or "img"
    reported = AgentRuntime(image=image, features=[TOOL_SAFETY])
    preview.runtime = reported
    preview.effective_config = {
        **(preview.effective_config or {}),
        "images": {"agent_image": image},
    }
    assert preview.image_reports(TOOL_SAFETY)
    assert ApprovalChoiceService._supported_rules(preview) is None
    source = preview.model_copy(update={"preview": None})
    assert ApprovalChoiceService._supported_rules(source) is not None


def test_sandbox_events_of_a_preview_are_recorded_under_the_source(client, fake_clusters, mongo):
    from daegam.services.telemetry.normalize import SANDBOX_OPENED
    from daegam.store.sandboxes import SandboxRecord

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    preview = start_preview(client, agent.id)
    rec = SandboxRecord(
        agent_id=preview.id,
        tenant="default",
        cluster_id=agent.cluster_id,
        namespace=DEFAULT_NS,
        session_id="s1",
        node="n1",
        pod="p1",
    )
    in_app(client, client.app.state.sandboxes._event, rec, SANDBOX_OPENED)
    (row,) = list(mongo["agent_events"].find({"event": SANDBOX_OPENED}))
    assert (row["instance_id"], row["preview_id"]) == (agent.id, preview.id)
