"""P22 F1·F2: 합친 Agent 의 정의·배포 API (D26, D42).

정의와 실행이 한 문서다. 만들면 draft 로 저장만 되고 deploy 를 불러야 클러스터에 뜬다. 여기서는
리비전(layout 만 바뀐 PUT 은 리비전 없음, PUT 의 `expect_revision` 과 layout 을 뺀 PUT, P48 6.7절),
draft 생애(만들기·복제·해제·삭제), 이름 규칙, validate 와 템플릿, D28 전 형식으로 남은 문서의 응답과
배포, 배포 검증 오류, 같은 상태의 저장 경합, 인스턴스 토큰 인증, stub provider 게이트를 든다.
그래프 규칙(oidc 모드의 agent_oauth 포함)은 test_agent_graph_rules.py, 컴파일은
test_agent_compile.py, K8s 렌더·provision 은 test_agent_runtime*.py, 런타임 프록시는
test_agent_runtime_proxy.py 가 본다(여기서는 fake 클러스터에 적용되는 것만 지나간다).
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from daegam.auth.principal import Principal
from daegam.services.agent_graph import check_schema
from daegam.services.agent_templates import templates
from daegam.store.agent_instance_tokens import authenticate_instance_token
from daegam.store.agents import Agent, AgentSpec
from daegam.store.gateways import Gateway, GatewayK8s, GatewayRevision, GatewaySpec
from tests.helpers.agent_runtime import agent_deployment, deployed_agent, token_of
from tests.helpers.agents import (
    agent_body,
    agent_env,
    create_agent,
    deploy_agent,
    deploy_spec,
    node_of,
    seed_tool_gateway,
    template_for,
    template_spec,
)
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, JOBS, LLM_KEY_SECRET, T2
from tests.helpers.http import api_client, detail_code, make_app

# -- API (header 모드) ---------------------------------------------------------------------


def test_create_saves_a_draft_and_deploy_pins_the_revision(client, mongo):
    """만들기는 저장이다. draft 는 클러스터 쪽이 비어 있고 목록에 들되 summary 에는 없다."""
    cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    r = create_agent(client, spec, display_name="상담 봇")
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["state"] == "draft" and a["deployed_revision"] is None and a["revision"] == 1
    assert a["cluster_id"] is None and a["k8s"] is None and a["token_id"] is None
    assert a["status"] is None and a["issues"] == [] and a["revision_created"] is True
    # draft 도 목록에 든다. 고르개 전용 목록은 배포된 것만 본다
    assert [x["id"] for x in client.get(AG).json()] == [a["id"]]
    assert client.get(f"{AG}/summary").json() == []
    assert client.get(f"{AG}/{a['id']}", headers=T2).status_code == 404

    r = deploy_agent(client, a["id"])
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["id"] == a["id"] and d["state"] == "deployed" and d["deployed_revision"] == 1
    assert d["cluster_id"] == cid and d["replicas"] == 1
    assert d["k8s"] == {
        "name": "agent-support",
        "namespace": "daegam-default",
        "token_secret": f"agent-inst-{a['id']}-token",
    }
    assert d["bindings"] == [{"data_service_id": ds["id"], "env_prefix": "DAEGAM_DB_0"}]
    assert d["effective_config"]["namespace"]["name"] == "daegam-default"
    assert d["status"]["phase"] == "Pending"
    assert d["status"]["chat_url"] == f"/api/agents/{a['id']}/chat"
    # 그래프 경고는 없고 fake 클러스터의 preflight 경고(D16)만 붙는다
    assert [w.split(":")[0] for w in d["warnings"]] == ["network_policy_unenforced"]
    # 토큰: sha256 만 저장, 범위는 핀된 리비전이고 참조는 합친 문서 id 다
    tok = mongo["agent_instance_tokens"].find_one({"_id": d["token_id"]})
    assert tok["agent_instance_id"] == a["id"] and tok["revoked_at"] is None
    assert tok["scope"] == [f"agent-profile:{a['id']}:rev:1"]
    assert len(tok["token_sha256"]) == 64
    assert {ix["name"] for ix in mongo["agents"].list_indexes()} >= {
        "uq_tenant_name",
        "ix_cluster",
        "ix_state_updated",
    }
    # 이미 배포된 것에 다시 deploy 는 409 다
    r = deploy_agent(client, a["id"])
    assert detail_code(r) == (409, "already_deployed")


def test_agent_crud_revisions_and_layout(client, mongo):
    cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    layout = templates()[0]["layout"]
    r = create_agent(client, spec, layout=layout, display_name="상담 봇")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["revision"] == 1 and p["revisions"] == [1] and p["revision_created"] is True
    assert p["layout"]["entry"] == {"x": 80, "y": 220}
    assert (
        p["graph"]["nodes"][0]["type"] == "entry"
        and p["service"]["budget"]["session_tokens"] == 200000
    )
    assert client.get(f"{AG}/{p['id']}").json()["revision"] == 1

    # layout 만 바뀐 PUT: 리비전 없음
    moved = {**layout, "entry": {"x": 1, "y": 2}}
    r = client.put(f"{AG}/{p['id']}", json=agent_body(spec, layout=moved, display_name="상담 봇 2"))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1 and r.json()["revision_created"] is False
    assert (
        r.json()["layout"]["entry"] == {"x": 1, "y": 2} and r.json()["display_name"] == "상담 봇 2"
    )
    doc = mongo["agents"].find_one({"_id": p["id"]})
    assert doc["revision"] == 1 and len(doc["revisions"]) == 1 and doc["layout"]["entry"]["x"] == 1
    assert doc["updated_by"] == "header"

    # graph 가 바뀐 PUT: 리비전 2. 리비전 1 은 그대로 남는다
    changed = copy.deepcopy(spec)
    node_of(changed, "entry")["data"]["greeting"] = "반갑습니다."
    r = client.put(f"{AG}/{p['id']}", json=agent_body(changed, layout=moved))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["revision_created"] is True
    assert r.json()["revisions"] == [1, 2]
    revs = client.get(f"{AG}/{p['id']}/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2] and "layout" not in revs[0]
    old = client.get(f"{AG}/{p['id']}/revisions/1").json()
    assert node_of(old, "entry")["data"]["greeting"] == "안녕하세요. 무엇을 도와드릴까요?"
    assert client.get(f"{AG}/{p['id']}/revisions/9").status_code == 404
    # service 만 바뀌어도 리비전이다
    changed["service"]["observability"]["record_bodies"] = True
    assert client.put(f"{AG}/{p['id']}", json=agent_body(changed)).json()["revision"] == 3

    # name 은 바꿀 수 없다. 잘못된 그래프는 저장을 막고 리비전을 만들지 않는다
    r = client.put(f"{AG}/{p['id']}", json=agent_body(changed, name="other"))
    assert detail_code(r) == (422, "name_immutable")
    bad = template_spec(provider="nokey", source_id=gw["id"], ds_id=ds["id"])
    r = client.put(f"{AG}/{p['id']}", json=agent_body(bad))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_graph"
    assert [(i["code"], i["node_id"]) for i in r.json()["detail"]["issues"]] == [
        ("provider_no_key", "model-main")
    ]
    assert client.get(f"{AG}/{p['id']}").json()["revision"] == 3

    # draft 삭제: 클러스터에 아무것도 없었으므로 k8s 는 없고 회수할 토큰도 없다
    r = client.delete(f"{AG}/{p['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": p["id"],
        "deleted": True,
        "state_before": "draft",
        "revoked_tokens": 0,
        "k8s": None,
    }
    # 지운 문서는 남지 않는다. 410 이 아니라 404 다 (D42)
    assert client.get(f"{AG}/{p['id']}").status_code == 404
    assert mongo["agents"].find_one({"_id": p["id"]}) is None


def _without_guardrail(spec: dict[str, Any]) -> dict[str, Any]:
    """템플릿 spec 에서 입력 가드레일 노드와 그 간선을 뺀 사본. 빼도 그래프는 맞다."""
    out = copy.deepcopy(spec)
    out["graph"]["nodes"] = [n for n in out["graph"]["nodes"] if n["id"] != "guardrail-input"]
    out["graph"]["edges"] = [e for e in out["graph"]["edges"] if e["target"] != "guardrail-input"]
    return out


def test_put_saves_only_while_the_latest_revision_is_the_expected_one(client, mongo):
    """`expect_revision` 이 최신 리비전이면 저장하고, 아니면 아무것도 쓰지 않고 409
    revision_conflict 에 지금 번호를 싣는다. 리비전을 만들지 않는 배치만 옮긴 저장도 같은 조건이다.
    없으면 서버가 읽은 리비전에 조건을 걸고, POST 본문은 그 필드를 받지 않는다 (P48 6.7절)."""
    _cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    layout = templates()[0]["layout"]
    p = create_agent(client, spec, layout=layout).json()
    url = f"{AG}/{p['id']}"

    changed = copy.deepcopy(spec)
    node_of(changed, "entry")["data"]["greeting"] = "반갑습니다."
    r = client.put(url, json=agent_body(changed, expect_revision=1))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["revision_created"] is True

    # r1 을 보고 고친 저장은 r2 를 덮지 못한다
    stale = copy.deepcopy(spec)
    node_of(stale, "entry")["data"]["greeting"] = "낡은 인사"
    r = client.put(url, json=agent_body(stale, expect_revision=1))
    assert detail_code(r) == (409, "revision_conflict")
    assert r.json()["detail"]["revision"] == 2
    moved = {**layout, "entry": {"x": 1, "y": 2}}
    r = client.put(url, json=agent_body(changed, layout=moved, expect_revision=1))
    assert detail_code(r) == (409, "revision_conflict")
    assert r.json()["detail"]["revision"] == 2
    doc = mongo["agents"].find_one({"_id": p["id"]})
    assert doc["revision"] == 2 and len(doc["revisions"]) == 2
    assert node_of(doc["spec"], "entry")["data"]["greeting"] == "반갑습니다."
    assert doc["layout"]["entry"] == {"x": 80, "y": 220}

    # 배치만 옮긴 저장도 지금 번호를 대면 지나간다
    r = client.put(url, json=agent_body(changed, layout=moved, expect_revision=2))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["revision_created"] is False
    assert r.json()["layout"]["entry"] == {"x": 1, "y": 2}

    # 번호는 1 부터다. POST 는 그 필드를 모르는 본문이라 422 다
    assert client.put(url, json=agent_body(changed, expect_revision=0)).status_code == 422
    r = create_agent(client, spec, name="other", expect_revision=1)
    assert r.status_code == 422, r.text
    assert mongo["agents"].count_documents({}) == 1

    # 없으면 서버가 읽은 리비전에 조건을 걸고 저장한다
    r = client.put(url, json=agent_body(stale))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3


def test_a_save_landing_between_the_read_and_the_write_is_a_revision_conflict(
    client, mongo, monkeypatch
):
    """PUT 이 문서를 읽은 뒤 쓰기 전에 다른 저장이 먼저 지나가면 조건부 저장이 맞지 않는다.
    `expect_revision` 이 읽은 번호와 같아 앞 검사를 지나도 409 revision_conflict 이고, 본문의
    revision 은 다시 읽은 지금 번호다. 먼저 지나간 저장은 그대로 남는다 (P48 6.7절)."""
    _cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    p = create_agent(client, spec).json()
    svc = client.app.state.agent_service
    real = svc.instances.set_fields
    landed: list[str] = []
    other = copy.deepcopy(spec)
    node_of(other, "entry")["data"]["greeting"] = "먼저 저장한 인사"

    async def other_save_first(doc_id: str, fields: dict[str, Any], **cond: Any) -> bool:
        if not landed:
            landed.append(doc_id)
            fresh = await svc.instances.get(doc_id)
            await svc.save(
                fresh,
                display_name=fresh.display_name,
                spec=AgentSpec.model_validate(other),
                layout=None,
                principal=Principal(sub="other"),
            )
        return await real(doc_id, fields, **cond)

    monkeypatch.setattr(svc.instances, "set_fields", other_save_first)
    mine = copy.deepcopy(spec)
    node_of(mine, "entry")["data"]["greeting"] = "늦게 저장한 인사"
    r = client.put(f"{AG}/{p['id']}", json=agent_body(mine, expect_revision=1))
    assert detail_code(r) == (409, "revision_conflict")
    assert r.json()["detail"]["revision"] == 2
    doc = mongo["agents"].find_one({"_id": p["id"]})
    assert doc["revision"] == 2 and doc["updated_by"] == "other"
    assert node_of(doc["spec"], "entry")["data"]["greeting"] == "먼저 저장한 인사"

    # 기준 번호 없이 보낸 PUT 의 경합도 같은 본문이다
    landed.clear()
    node_of(other, "entry")["data"]["greeting"] = "또 먼저 저장한 인사"
    r = client.put(f"{AG}/{p['id']}", json=agent_body(mine))
    assert detail_code(r) == (409, "revision_conflict")
    assert r.json()["detail"]["revision"] == 3


def test_put_without_layout_keeps_the_stored_positions_of_kept_nodes(client, mongo):
    """layout 을 뺀 PUT 은 저장된 배치 가운데 새 그래프에 남는 노드의 것을 지키고 빠진 노드의
    것만 버린다. 배치를 모르는 호출자(비서의 update_agent)가 캔버스의 배치를 지우지 않고, 리비전을
    만들지 않는 배치만 옮긴 저장도 되돌리지 않는다. 빈 layout 을 보내면 모두 비운다 (P48 6.7절)."""
    _cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    layout = templates()[0]["layout"]
    p = create_agent(client, spec, layout=layout).json()
    url = f"{AG}/{p['id']}"
    moved = {**layout, "entry": {"x": 1, "y": 2}}
    assert client.put(url, json=agent_body(spec, layout=moved)).json()["revision_created"] is False

    r = client.put(url, json=agent_body(_without_guardrail(spec)))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["revision_created"] is True
    kept = {k: v for k, v in moved.items() if k != "guardrail-input"}
    assert r.json()["layout"] == kept
    assert mongo["agents"].find_one({"_id": p["id"]})["layout"] == kept

    # 리비전을 만들지 않는 저장(display_name 만 바꿈)도 배치를 지킨다
    r = client.put(url, json=agent_body(_without_guardrail(spec), display_name="상담 봇"))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["revision_created"] is False
    assert r.json()["layout"] == kept and r.json()["display_name"] == "상담 봇"

    # 다시 더한 노드는 배치 없이 들어온다(캔버스가 격자에 둔다)
    r = client.put(url, json=agent_body(spec))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3 and r.json()["layout"] == kept

    r = client.put(url, json=agent_body(spec, layout={}))
    assert r.status_code == 200, r.text
    assert r.json()["layout"] == {} and r.json()["revision_created"] is False


def test_clone_copies_the_definition_into_a_draft(client, fake_clusters, mongo):
    """복제는 현재 리비전의 정의를 베낀 draft 다. 원본의 배포 쪽은 따라오지 않고, 대신 복제가
    받은 `exposure` 만 배포 설정으로 남아 노출용 둘째가 한 호출로 만들어진다 (D42)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    r = client.post(
        f"{agent.url}/clone",
        json={
            "name": "copy",
            "display_name": "복제본",
            "exposure": {
                "enabled": True,
                "host": "copy.example.com",
                "cors_origins": ["HTTPS://App.Example.com"],
                "allowed_groups": [" support-team ", ""],
            },
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["id"] != agent.id and c["name"] == "copy" and c["display_name"] == "복제본"
    assert c["state"] == "draft" and c["deployed_revision"] is None
    assert c["cluster_id"] is None and c["k8s"] is None and c["token_id"] is None
    assert c["revision"] == 1 and c["graph"] == agent.doc["graph"]
    # 노출은 draft 에 남는다. origin 은 소문자로 정규화하고 빈 그룹은 버린다. 이 단정이 없으면
    # 복제가 exposure 를 통째로 버려도 아무도 모른다
    assert c["exposure"]["enabled"] is True and c["exposure"]["host"] == "copy.example.com"
    assert c["exposure"]["cors_origins"] == ["https://app.example.com"]
    assert c["exposure"]["allowed_groups"] == ["support-team"]
    # 같은 이름으로 또 복제하면 409
    r = client.post(f"{agent.url}/clone", json={"name": "copy"})
    assert detail_code(r) == (409, "name_conflict")


def test_undeploy_returns_to_draft_and_keeps_the_definition(client, fake_clusters, mongo):
    """해제는 클러스터에서만 내린다. 정의와 리비전은 남고 같은 id 로 다시 배포된다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    r = client.post(f"{agent.url}/undeploy")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"] == agent.id and out["state"] == "draft"
    assert out["deployed_revision"] is None and out["k8s"] is None and out["status"] is None
    assert out["revision"] == 1 and out["graph"] == agent.doc["graph"]
    assert agent_deployment(fake) is None
    assert mongo["agent_instance_tokens"].find_one({"_id": agent.doc["token_id"]})["revoked_at"]
    # draft 라 채팅·매니페스트는 409 not_deployed 다
    r = client.post(f"{agent.url}/chat", json={"message": "hi"})
    assert detail_code(r) == (409, "not_deployed")
    assert client.get(f"{agent.url}/manifest").status_code == 409
    # 다시 배포하면 같은 문서가 뜬다
    r = deploy_agent(client, agent.id)
    assert r.status_code == 201, r.text
    assert r.json()["id"] == agent.id and r.json()["deployed_revision"] == 1


def test_agent_name_rules_and_conflicts(client, mongo):
    cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    r = create_agent(client, spec, name="Support_1")
    assert detail_code(r) == (422, "invalid_name")
    r = create_agent(client, spec, name="x" * 33)
    assert detail_code(r) == (422, "invalid_name")
    r = create_agent(client, spec, display_name="  ")
    assert detail_code(r) == (422, "invalid_display_name")
    assert create_agent(client, spec).status_code == 201
    r = create_agent(client, spec)
    assert detail_code(r) == (409, "name_conflict")
    # 다른 테넌트에서는 같은 이름을 쓸 수 있다 (그 테넌트의 소스가 없으니 검증에서 걸린다)
    r = client.post(AG, json=agent_body(spec), headers=T2)
    assert detail_code(r) == (422, "invalid_graph")
    assert "unknown_source" in {i["code"] for i in r.json()["detail"]["issues"]}


def test_deploy_validation_errors(client, mongo):
    cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    p = create_agent(client, spec).json()
    r = deploy_agent(client, p["id"], namespace="elsewhere")
    assert detail_code(r) == (422, "namespace_not_allowed")
    assert deploy_agent(client, "nope").status_code == 404
    # 같은 네임스페이스의 마이그레이션된 Gateway (agent-<name> CR) 와 충돌
    mongo["gateways"].insert_one(
        Gateway(
            tenant="default",
            name="bot",
            spec=GatewaySpec(),
            revisions=[GatewayRevision(revision=1)],
            state="deployed",
            deployed_revision=1,
            cluster_id=cid,
            k8s=GatewayK8s(name="agent-bot", namespace=DEFAULT_NS, group="agent-bot"),
            effective_config={},
        ).to_doc()
    )
    bot = create_agent(client, spec, "bot").json()
    r = deploy_agent(client, bot["id"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "k8s_name_conflict"
    assert r.json()["detail"]["k8s_name"] == "agent-bot"
    # 도구 소스가 다른 클러스터에 있으면 cluster_mismatch
    other = seed_tool_gateway(
        mongo, tenant="default", cluster_id="other-cluster", namespace="ns", name="far"
    )
    far = template_spec(source_id=other["id"], ds_id=ds["id"])
    p2 = create_agent(client, far, "far").json()
    r = deploy_agent(client, p2["id"])
    assert detail_code(r) == (422, "cluster_mismatch")
    assert r.json()["detail"]["node_id"] == "tools-calculator"
    # 저장 뒤 provider 가 사라지면 Deploy 가 막는다
    assert client.patch("/settings", json={"llm_providers": []}).status_code == 200
    r = deploy_agent(client, p["id"])
    assert detail_code(r) == (422, "invalid_graph")
    assert r.json()["detail"]["issues"][0]["code"] == "unknown_provider"


def test_deployment_saves_write_only_their_own_fields(client, fake_clusters, mongo):
    """상태가 같은 채 겹치는 배포 쪽 저장 둘이 서로를 지우지 않는다. 배포 필드를 통째로 쓰면
    낡은 스냅샷을 든 저장(스윕의 토큰 회전, 노출 저장)이 남의 필드를 되돌린다 (D42)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    svc = client.app.state.agent_service
    store = client.app.state.agents

    async def race() -> Agent:
        first = await store.get(agent.id)
        stale = await store.get(agent.id)  # 같은 시점의 둘째 사본
        first.svc_token_rev = "rev-a"
        await svc._save_deployment(
            first, {"svc_token_rev"}, expect_state={"deployed"}, action="rotate"
        )
        stale.replicas = 3
        await svc._save_deployment(stale, {"replicas"}, expect_state={"deployed"}, action="scale")
        return await store.get(agent.id)

    out = client.portal.call(race)
    assert out.replicas == 3
    assert out.svc_token_rev == "rev-a", "낡은 사본의 저장이 남의 배포 필드를 되돌렸다"


def test_edit_states_hold_the_base_advanced_helpers(client, fake_clusters, mongo):
    """Advanced 편집 셋은 기반에 있고 도메인은 `edit_states`·`edit_outside` 두 줄로만 붙잡는다.
    그 선언이 지워지거나 틀리면 해제된 문서에 오는 낡은 쓰기가 409 `not_deployed` 가 아니게 된다.
    공개 메서드로 부르는 회귀가 없으면 그 두 줄을 아무도 지키지 않는다 (D42)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    svc = client.app.state.agent_service
    store = client.app.state.agents

    async def race() -> dict[str, str]:
        stale = await store.get(agent.id)  # 해제 전에 읽은 사본
        await svc.undeploy(await store.get(agent.id))
        errors: dict[str, str] = {}
        attempts = (
            ("overrides", lambda: svc.set_overrides(stale, patch={"spec": {"replicas": 2}})),
            ("reset", lambda: svc.drift_reset(stale)),
            ("absorb", lambda: svc.drift_absorb(stale)),
        )
        for name, call in attempts:
            try:
                await call()
            except Exception as e:  # noqa: BLE001
                errors[name] = getattr(e, "code", type(e).__name__)
        return errors

    assert client.portal.call(race) == {
        "overrides": "not_deployed",
        "reset": "not_deployed",
        "absorb": "not_deployed",
    }


def test_agent_json_requires_instance_token(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    _p2, other = deploy_spec(client, agent.spec(), "other")
    token, other_token = agent.token, token_of(agent.fake, other)
    url = f"{agent.url}/agent.json"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(url, headers=bearer(other_token)).status_code == 401
    assert client.get(f"{AG}/nope/agent.json", headers=bearer(token)).status_code == 401
    # 테넌트 헤더 없이 Bearer 만으로 200. 결과는 스키마를 통과하는 agent.json 이다
    r = client.get(url, headers=bearer(token))
    assert r.status_code == 200, r.text
    doc = r.json()
    assert check_schema(doc) == []
    # 정의와 실행이 한 문서라 두 키의 id 가 같다
    assert doc["profile"] == {"id": agent.id, "name": "support", "revision": 1}
    assert doc["instance"] == {"id": agent.id, "name": "support"}
    assert doc["stub"] is True and doc["main"]["model"]["provider"]["env_key"] is None
    assert doc["tools"][0]["url"] == "http://vmcp-gw-calc.daegam-default.svc.cluster.local:4483/mcp"
    assert doc["tools"][0]["auth"] == {"kind": "none"}  # header 모드
    assert doc["tools"][0]["source"] == {
        "kind": "gateway_instance",
        "id": agent.gw["id"],
        "name": "calc",
    }
    assert doc["memory"] == {"checkpointer": {"kind": "postgres", "dsn_env": "DAEGAM_DB_0_URL"}}
    assert (
        hashlib.sha256(token.encode()).hexdigest()
        == mongo["agent_instance_tokens"].find_one({"_id": agent.doc["token_id"]})["token_sha256"]
    )
    # 범위가 다른 리비전이면 401
    tokens = client.app.state.agent_instance_tokens
    with pytest.raises(Exception) as e:
        client.portal.call(
            lambda: authenticate_instance_token(
                tokens, agent.id, token, scope=f"agent-profile:{agent.id}:rev:2"
            )
        )
    assert getattr(e.value, "status_code", None) == 401
    # 회수된 토큰은 401. 지운 문서도 401 이다
    assert client.delete(agent.url).status_code == 200
    assert client.get(url, headers=bearer(token)).status_code == 401
    assert (
        client.get(f"{AG}/{other['id']}/agent.json", headers=bearer(other_token)).status_code == 200
    )


def test_stub_provider_gate_and_spec_generator_rejection(
    settings, mongo, fake_clusters, fake_secrets, client
):
    """stub 은 DAEGAM_ALLOW_STUB_PROVIDER 배포본만 등록할 수 있고 Spec Generator 는 거부한다."""
    app = make_app(settings, fake_clusters, fake_secrets, allow_stub_provider=False)
    with api_client(app) as c:
        r = c.patch("/settings", json={"llm_providers": [{"name": "s", "kind": "stub"}]})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "stub_provider_disallowed"
        assert r.json()["detail"]["providers"] == ["s"]
    providers = [
        {"name": "s", "kind": "stub"},
        {"name": "oai", "kind": "openai", "api_key_secret": LLM_KEY_SECRET},
    ]
    assert client.patch("/settings", json={"llm_providers": providers}).status_code == 200
    assert client.post("/settings/llm-providers/s/check").json() == {"ok": True, "code": "ok"}
    r = client.post(JOBS, json={"input": {"text": "x"}, "provider": "s"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "provider_not_supported"


# -- oidc 모드 --------------------------------------------------------------------------------


async def _compile_via(svc, agent_id: str) -> dict[str, Any]:
    doc = await svc.agents.get(agent_id)
    return await svc.compile(doc, token_envs=None)
