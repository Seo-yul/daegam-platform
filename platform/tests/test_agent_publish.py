"""Agent 의 허브 게시 (P46 6.1절). 테스트 Mongo 가 있어야 한다. header 모드라 요청한 사람은
플랫폼 관리자인 `header` 다.

게시한 Agent 는 허브의 `agent` 항목이다. 항목의 꼴, 게시의 전제 조건, 해제와 Agent 의 해제·삭제,
항목을 쓰는 것(참조), Gateway 의 거절, 전제 조건을 잃은 재배포의 경고를 본다. 참조를 세는 규칙은
tests/test_references.py 가 DB 문서로 본다.
"""

from __future__ import annotations

import copy
import functools
from typing import Any

import pytest

from daegam.auth.principal import Membership, Principal
from daegam.store.hub import AGENT_DESCRIPTION_MAX
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent
from tests.helpers.agents import MINIMAL_SPEC, template_for
from tests.helpers.auth import as_principal
from tests.helpers.consts import AG, GW, HUB
from tests.helpers.docs import agent_doc, agent_refs_spec, insert_docs
from tests.helpers.http import api_client, detail_code, make_app

# 호출을 받는 Agent 의 런타임이 알려야 하는 능력
CALL_FEATURES = "runs,turn_tokens"
BODY = {"description": "Answers billing questions"}


@pytest.fixture
def pub_client(settings, mongo, fake_clusters, fake_secrets):
    # 워커는 끈다. 게시는 실행을 만들지 않는다
    app = make_app(
        settings, fake_clusters, fake_secrets, runs_enabled=True, runs_workers_enabled=False
    )
    with api_client(app) as c:
        yield c


def _budgeted(spec: dict[str, Any], tokens: int | None) -> dict[str, Any]:
    spec["service"].setdefault("budget", {})["session_tokens"] = tokens
    return spec


def _callable_spec(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """memory 노드와 세션 예산이 있는 템플릿이다."""
    return _budgeted(template_for(gw, ds), 20_000)


def _without_budget(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    return _budgeted(template_for(gw, ds), None)


def _without_memory(_gw: dict[str, Any], _ds: dict[str, Any]) -> dict[str, Any]:
    """memory 노드 없이도 배포되는 최소 그래프에 세션 예산만 둔 것이다."""
    return _budgeted(copy.deepcopy(MINIMAL_SPEC), 20_000)


def _agent(
    client, fake_clusters, mongo, *, spec=_callable_spec, features: str | None = CALL_FEATURES
) -> DeployedAgent:
    return deployed_agent(client, fake_clusters, mongo, spec=spec, runtime=False, features=features)


def _publish(client, agent: DeployedAgent, **body: Any):
    return client.post(f"{agent.url}/publish", json={**BODY, **body})


def _published(client, agent: DeployedAgent, **body: Any) -> dict[str, Any]:
    r = _publish(client, agent, **body)
    assert r.status_code == 201, r.text
    return r.json()


# -- 게시와 항목의 꼴 ---------------------------------------------------------------------------


def test_publishing_makes_an_agent_item_that_points_at_the_agent(pub_client, mongo, fake_clusters):
    """항목은 Agent 의 id 와 Deployment 이름만 든다. MCP Server 의 칸과 CR 종류가 없어 MCP Server
    를 찾는 조회와 CR 을 가진 항목의 조회에 걸리지 않고, 렌더할 것도 없다. 두 번째 게시는 200
    으로 같은 항목이다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    assert pub_client.get(HUB).json() == []

    item = _published(pub_client, agent)
    assert (item["kind"], item["name"], item["display_name"]) == ("agent", "support", "support")
    assert item["source"]["agent_id"] == agent.id and item["source"]["instance_id"] is None
    assert item["cluster_id"] == agent.cluster_id
    assert item["k8s"]["kind"] is None
    assert (item["k8s"]["name"], item["k8s"]["namespace"]) == (
        agent.doc["k8s"]["name"],
        agent.doc["k8s"]["namespace"],
    )
    assert item["description"] == BODY["description"] and item["scope"] == "tenant"
    assert item["tool_names"] == [] and item["status"]["url"] is None

    stored = mongo["hub_items"].find_one({"_id": item["id"]})
    assert stored["source"]["agent_id"] == agent.id
    assert stored["source"].get("instance_id") is None and stored["k8s"]["kind"] is None
    assert "calls_trusted" not in stored

    r = _publish(pub_client, agent, name="other", description="Changed")
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["description"]) == (item["id"], BODY["description"])
    assert [i["id"] for i in pub_client.get(HUB).json()] == [item["id"]]

    hub = pub_client.app.state.hub_service
    assert pub_client.portal.call(hub.store.get_by_instance, agent.id) is None
    assert pub_client.portal.call(hub.store.owning_cr_on_cluster, agent.cluster_id) == []
    assert pub_client.get(f"{HUB}/{item['id']}/manifest").json() == {"documents": []}


def test_the_owner_names_the_item_and_trusts_calls(pub_client, mongo, fake_clusters):
    """이름과 표시 이름, 호출을 믿는지를 게시할 때 정한다. 쥔 이름은 409, 틀린 이름은 422 다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    entry = {"name": "taken", "kind": "entry", "url": "https://mcp.example/mcp"}
    r = pub_client.post(HUB, json={**entry, "transport": "streamable-http"})
    assert r.status_code == 201, r.text
    assert "description" not in mongo["hub_items"].find_one({"name": "taken"})

    assert detail_code(_publish(pub_client, agent, name="taken")) == (409, "name_conflict")
    assert detail_code(_publish(pub_client, agent, name="bad name")) == (422, "invalid_name")
    item = _published(pub_client, agent, name="billing", display_name="Billing", calls_trusted=True)
    assert (item["name"], item["display_name"], item["calls_trusted"]) == (
        "billing",
        "Billing",
        True,
    )


def test_an_agent_has_one_item_even_when_two_publishes_race(pub_client, mongo, fake_clusters):
    """겹친 두 게시 가운데 늦은 쪽은 저장에서 지고 먼저 들어간 항목을 돌려준다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    first = _published(pub_client, agent)
    service = pub_client.app.state.agent_service
    store = service.publishing.hub.store
    doc = pub_client.portal.call(service.agents.get, agent.id)
    found, calls = store.get_by_agent, []

    async def misses_first(agent_id: str):
        """늦은 게시가 항목이 생기기 전에 읽은 것처럼 첫 조회만 비운다."""
        calls.append(agent_id)
        return None if len(calls) == 1 else await found(agent_id)

    store.get_by_agent = misses_first
    admin = Principal(sub="header", memberships=[Membership(tenant="default", role="tenant-admin")])
    late = functools.partial(
        service.publishing.hub.publish_agent, doc, name="late", description="x", principal=admin
    )
    item, created = pub_client.portal.call(late)
    assert (item.id, created) == (first["id"], False)
    assert mongo["hub_items"].count_documents({}) == 1


def test_publishing_needs_both_scopes_and_unpublishing_the_item_manage(
    pub_client, mongo, fake_clusters
):
    """viewer 는 게시하지 못한다. 해제는 항목의 manage 를 따른다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    with as_principal(pub_client, "vera", "viewer"):
        assert _publish(pub_client, agent).status_code == 403
    item = _published(pub_client, agent, access={"manage": {"roles": ["tenant-admin"]}})
    with as_principal(pub_client, "dev", "developer"):
        for path in (f"{agent.url}/publish", f"{HUB}/{item['id']}"):
            r = pub_client.delete(path)
            assert detail_code(r) == (403, "forbidden") and r.json()["detail"]["action"] == "manage"
    with as_principal(pub_client, "ada", "tenant-admin"):
        assert pub_client.delete(f"{agent.url}/publish").status_code == 200


@pytest.mark.parametrize(
    "description", ["", "   ", "x" * (AGENT_DESCRIPTION_MAX + 1)], ids=["empty", "blank", "long"]
)
def test_the_tool_description_is_required_and_bounded(
    pub_client, mongo, fake_clusters, description
):
    agent = _agent(pub_client, fake_clusters, mongo)
    r = _publish(pub_client, agent, description=description)
    assert r.status_code == 422, r.text
    assert pub_client.get(HUB).json() == []


def test_an_agent_item_stays_in_its_tenant(pub_client, mongo, fake_clusters):
    """게시 본문으로도 접근 정책 저장으로도 다른 테넌트에 열지 못한다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    r = _publish(pub_client, agent, access={"consume": {"tenants": ["other"]}})
    assert detail_code(r) == (422, "cross_tenant_unsupported")
    assert r.json()["detail"]["kind"] == "agent"

    item = _published(pub_client, agent, access={"consume": {"roles": ["tenant-admin"]}})
    assert item["access"]["consume"]["roles"] == ["tenant-admin"]
    assert item["access_updated_by"] == "header"
    r = pub_client.put(f"{HUB}/{item['id']}/access", json={"view": {"tenants": ["*"]}})
    assert detail_code(r) == (422, "cross_tenant_unsupported")


def test_the_item_reports_the_agent_status_and_lists_no_tools(pub_client, mongo, fake_clusters):
    """상태는 Agent 의 phase 다. Tool 목록을 새로 받아 올 곳은 없다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    item = _published(pub_client, agent)
    phase = pub_client.get(agent.url).json()["status"]["phase"]
    assert phase is not None
    for got in (pub_client.get(f"{HUB}/{item['id']}").json(), pub_client.get(HUB).json()[0]):
        assert got["status"]["phase"] == phase and got["status"]["error"] is None

    r = pub_client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert detail_code(r) == (422, "derived_tools")
    assert pub_client.get(f"{HUB}/{item['id']}/tools").json()["listed"] is False


# -- 전제 조건 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "features", "expected"),
    [
        (_without_budget, CALL_FEATURES, "agent_source_runs_unmet"),
        (_without_memory, CALL_FEATURES, "agent_source_runs_unmet"),
        (_callable_spec, None, "runtime_feature_missing"),
        (_callable_spec, "runs", "runtime_feature_missing"),
        (_callable_spec, "turn_tokens", "runtime_feature_missing"),
    ],
    ids=["no-budget", "no-memory", "no-record", "no-turn-tokens", "no-runs"],
)
def test_only_an_agent_that_can_be_called_is_published(
    pub_client, mongo, fake_clusters, spec, features, expected
):
    agent = _agent(pub_client, fake_clusters, mongo, spec=spec, features=features)
    assert detail_code(_publish(pub_client, agent)) == (409, expected)
    assert mongo["hub_items"].count_documents({}) == 0


def test_a_draft_agent_is_not_published(pub_client, mongo, fake_clusters):
    agent = _agent(pub_client, fake_clusters, mongo)
    assert pub_client.post(f"{agent.url}/undeploy").status_code == 200
    assert detail_code(_publish(pub_client, agent)) == (409, "not_deployed")
    assert detail_code(pub_client.post(f"{AG}/nope/publish", json=BODY)) == (404, "not_found")


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"runs_enabled": False}, "agent_calls_disabled"),
        ({"runs_enabled": True, "telemetry_ingest_enabled": False}, "runs_need_events"),
    ],
    ids=["runs-off", "ingest-off"],
)
def test_publishing_follows_the_platform_switches(
    settings, mongo, fake_clusters, fake_secrets, over, expected
):
    app = make_app(settings, fake_clusters, fake_secrets, runs_workers_enabled=False, **over)
    with api_client(app) as client:
        agent = _agent(client, fake_clusters, mongo)
        assert detail_code(_publish(client, agent)) == (409, expected)


# -- 해제, Agent 의 해제와 삭제 ------------------------------------------------------------------


def test_unpublishing_from_either_side_removes_the_item(pub_client, mongo, fake_clusters):
    """Agent 쪽 해제와 `DELETE /hub/{id}` 가 같은 일을 하고, 감사 action 도 같다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    r = pub_client.delete(f"{agent.url}/publish")
    assert detail_code(r) == (404, "not_published")

    item = _published(pub_client, agent)
    r = pub_client.delete(f"{agent.url}/publish")
    assert r.status_code == 200 and r.json() == {"id": item["id"], "deleted": True}
    assert pub_client.get(f"{HUB}/{item['id']}").status_code == 404

    again = _published(pub_client, agent)
    assert again["id"] != item["id"]
    assert pub_client.delete(f"{HUB}/{again['id']}").status_code == 200
    assert pub_client.get(HUB).json() == []

    rows = mongo["audit_events"].find({"action": {"$in": ["publish_agent", "unpublish_agent"]}})
    assert sorted((r["action"], r["resource"], r["status"]) for r in rows) == [
        ("publish_agent", "agents", 201),
        ("publish_agent", "agents", 201),
        ("unpublish_agent", "agents", 200),
        ("unpublish_agent", "hub", 200),
    ]


def test_a_published_agent_is_not_undeployed_and_its_delete_takes_the_item(
    pub_client, mongo, fake_clusters
):
    agent = _agent(pub_client, fake_clusters, mongo)
    item = _published(pub_client, agent)
    assert detail_code(pub_client.post(f"{agent.url}/undeploy")) == (409, "published")
    assert pub_client.get(agent.url).json()["state"] == "deployed"

    assert pub_client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert pub_client.post(f"{agent.url}/undeploy").status_code == 200

    other = _publish_again(pub_client, agent)
    r = pub_client.delete(agent.url)
    assert r.status_code == 200 and r.json()["deleted"] is True, r.text
    assert mongo["hub_items"].find_one({"_id": other["id"]}) is None


def _publish_again(client, agent: DeployedAgent) -> dict[str, Any]:
    """내린 Agent 를 다시 배포하고 능력을 알린 뒤 게시한다."""
    from daegam.services.agent_features import FEATURES_HEADER, IMAGE_HEADER
    from tests.helpers.consts import IMAGE

    assert client.post(f"{agent.url}/deploy", json={}).status_code == 201
    headers = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: CALL_FEATURES}
    assert client.get(f"{agent.url}/agent.json", headers=headers).status_code == 200
    return _published(client, agent)


def test_an_item_whose_agent_is_gone_reads_missing_and_is_unpublished(
    pub_client, mongo, fake_clusters
):
    """앞 판이 게시한 Agent 를 지우면 항목만 남는다. 이 판은 그 항목을 `Missing` 으로 보이고
    해제한다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    item = _published(pub_client, agent)
    mongo["agents"].delete_one({"_id": agent.id})

    status = pub_client.get(f"{HUB}/{item['id']}").json()["status"]
    assert (status["phase"], status["ready"]) == ("Missing", False)
    assert pub_client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert mongo["hub_items"].count_documents({}) == 0


# -- 참조와 Gateway ----------------------------------------------------------------------------


def test_agents_that_use_the_item_are_listed_and_do_not_hold_it(pub_client, mongo, fake_clusters):
    """항목의 참조는 그것을 Tool 로 쓰는 Agent 이고, Agent 의 참조는 그 항목과 그것을 거쳐 쓰는
    Agent 다. 쓰는 Agent 가 있어도 주인은 게시를 거둔다."""
    agent = _agent(pub_client, fake_clusters, mongo)
    item = _published(pub_client, agent)
    uses = agent_refs_spec(source=("agent", item["id"]))
    caller = agent_doc("ag-caller", spec=uses, tenant="default", state="deployed")
    insert_docs(mongo, agents=[caller])

    refs = pub_client.get(f"{HUB}/{item['id']}/references").json()["used_by"]
    assert [(r["kind"], r["id"], r["revision"], r["via"]) for r in refs] == [
        ("agent", "ag-caller", "both", None)
    ]
    refs = pub_client.get(f"{agent.url}/references").json()["used_by"]
    assert [(r["kind"], r["id"], r["revision"], r["via"]) for r in refs] == [
        ("hub-item", item["id"], "deployed", None),
        ("agent", "ag-caller", "both", "hub-item"),
    ]

    assert pub_client.delete(f"{agent.url}/publish").status_code == 200
    assert pub_client.get(f"{agent.url}/references").json()["used_by"] == []


def test_a_gateway_does_not_take_an_agent_item_at_save_or_deploy(pub_client, mongo, fake_clusters):
    agent = _agent(pub_client, fake_clusters, mongo)
    item = _published(pub_client, agent)
    backends = [{"hub_item_id": item["id"]}]

    r = pub_client.post(GW, json={"name": "gw", "backends": backends})
    assert detail_code(r) == (422, "agent_item_gateway_unsupported"), r.text
    assert r.json()["detail"]["hub_item_id"] == item["id"]

    # 앞 판이 저장한 정의는 배포에서 멈춘다
    entry = {"name": "outside", "kind": "entry", "url": "https://mcp.example/mcp"}
    other = pub_client.post(HUB, json={**entry, "transport": "streamable-http"}).json()
    r = pub_client.post(GW, json={"name": "gw", "backends": [{"hub_item_id": other["id"]}]})
    assert r.status_code == 201, r.text
    gateway = r.json()
    mongo["gateways"].update_one(
        {"_id": gateway["id"]}, {"$set": {"revisions.$[].backends.0.hub_item_id": item["id"]}}
    )
    r = pub_client.post(f"{GW}/{gateway['id']}/deploy", json={})
    assert detail_code(r) == (422, "agent_item_gateway_unsupported"), r.text


# -- 재배포 ------------------------------------------------------------------------------------


def _saved(client, agent: DeployedAgent, spec: dict[str, Any]) -> None:
    r = client.put(agent.url, json={"name": "support", **spec})
    assert r.status_code == 200, r.text


def test_a_redeploy_that_drops_the_prerequisites_warns_only_when_published(
    pub_client, mongo, fake_clusters
):
    agent = _agent(pub_client, fake_clusters, mongo)
    broken = _budgeted(agent.spec(), None)

    _saved(pub_client, agent, broken)
    r = pub_client.post(f"{agent.url}/redeploy", json={})
    assert r.status_code == 200, r.text
    assert not [w for w in r.json()["warnings"] if w.startswith("agent_calls_broken_by_deploy")]

    _saved(pub_client, agent, agent.spec())
    assert pub_client.post(f"{agent.url}/redeploy", json={}).status_code == 200
    _published(pub_client, agent)
    r = pub_client.post(f"{agent.url}/redeploy", json={})
    assert not [w for w in r.json()["warnings"] if w.startswith("agent_calls_broken_by_deploy")]

    _saved(pub_client, agent, broken)
    r = pub_client.post(f"{agent.url}/redeploy", json={})
    assert r.status_code == 200, r.text
    assert [w.split(":")[0] for w in r.json()["warnings"]].count(
        "agent_calls_broken_by_deploy"
    ) == 1
