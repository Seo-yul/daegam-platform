"""평문 포트가 닫힌 Redis (P61 5절 5단계, B4). 닫힌 Database 와 클러스터 기본 Redis 는 TLS
끝점으로만 렌더되고, TLS 를 모르는 소비자는 배포가 막는다. Agent 는 `redis_tls` 를 모르는
이미지면 409 runtime_image_too_old, MCP Server 는 세션 저장소를 TLS 로 둘 수 없는 클러스터면 그
게이트의 422 다. 닫힌 표시는 플랫폼이 쓰고 API 본문으로는 쓰지 않는다."""

from __future__ import annotations

from types import SimpleNamespace

from daegam.k8s.templates.agent import render_agent_deployment
from daegam.k8s.templates.common import tls_endpoint
from daegam.services.agent_features import (
    FEATURES_HEADER,
    IMAGE_HEADER,
    REDIS_TLS,
    AgentFeatures,
    deploy_check,
    requiring_redis_tls,
)
from daegam.services.mcp_servers import SessionOptions, _closed_session_refusal
from daegam.store.agents import AgentRuntime
from daegam.store.clusters import DefaultRedis
from daegam.store.data_services import RedisTls
from daegam.store.tenants import Binding
from tests.helpers.agent_runtime import deployed_agent, env_map, minimal_agent_doc
from tests.helpers.agents import template_spec
from tests.helpers.clusters import record_toolhive
from tests.helpers.consts import AG, DEFAULT_NS, GV, IMAGE
from tests.helpers.data_services import TLS_PORT, tls_database
from tests.helpers.docs import cluster_doc, data_service_doc, effective_config
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env, replicated_server
from tests.helpers.tls import ca_pem


def _closed(mongo, ds_id: str) -> None:
    mongo["data_services"].update_one({"_id": ds_id}, {"$set": {"plain_closed": True}})


# -- 렌더 ---------------------------------------------------------------------------------------


def test_a_closed_plain_port_renders_the_tls_endpoint_whatever_the_caller_allows():
    tls = RedisTls(port=TLS_PORT, ca_secret="ds-r-ca")
    open_ds = data_service_doc("mem", kind="redis", tls=tls)
    closed = data_service_doc("mem", kind="redis", tls=tls, plain_closed=True)
    assert tls_endpoint(open_ds, False) is None
    assert tls_endpoint(closed, False) == tls
    # TLS 끝점이 없는 기록과 postgres 는 닫힌 표시와 상관없다
    assert tls_endpoint(data_service_doc("mem", kind="redis", plain_closed=True), False) is None
    assert tls_endpoint(data_service_doc(plain_closed=True), True) is None


def test_an_agent_of_any_image_gets_the_tls_endpoint_of_a_closed_redis():
    ds = data_service_doc(
        "mem",
        kind="redis",
        id="ds-r",
        host="redis.svc",
        db=0,
        secret_name="ds-ds-r",
        tls=RedisTls(port=TLS_PORT, ca_secret="ds-ds-r-ca"),
        plain_closed=True,
    )
    effective = effective_config(
        bindings=[Binding(data_service_id="ds-r", env_prefix="DAEGAM_DB_0")]
    )
    dep = render_agent_deployment(
        minimal_agent_doc(),
        effective,
        providers=[],
        data_services={"ds-r": ds},
        agent_oauth=None,
        redis_tls=False,
        client_id_claim="cid",
        groups_claim=None,
    )
    env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
    assert env["DAEGAM_DB_0_PORT"] == str(TLS_PORT) and env["DAEGAM_DB_0_TLS"] == "1"
    assert env["DAEGAM_DB_0_URL"] == f"rediss://redis.svc:{TLS_PORT}/0"


# -- Agent: redis_tls 를 요구한다 ---------------------------------------------------------------


def test_a_closed_binding_requires_redis_tls_of_a_known_image():
    """배포 전 검사는 이미지의 기록이 있을 때만 판정한다. 닫힌 Redis 를 바인딩한 에이전트는
    `redis_tls` 가 빠진 기록에서 missing 이고, 그 이름은 문서 기능이 아니다."""
    doc = minimal_agent_doc()
    wanted = requiring_redis_tls(AgentFeatures())
    assert wanted.required == {REDIS_TLS}
    check, warnings = deploy_check(doc, IMAGE, wanted)
    assert check is None and [w.code for w in warnings] == ["runtime_features_unknown"]
    doc.runtime = AgentRuntime(image=IMAGE, features=["cancel"], reported_at=doc.created_at)
    check, _ = deploy_check(doc, IMAGE, wanted)
    assert check is not None and check.missing == (REDIS_TLS,)
    doc.runtime = AgentRuntime(image=IMAGE, features=[REDIS_TLS], reported_at=doc.created_at)
    check, _ = deploy_check(doc, IMAGE, wanted)
    assert check is not None and check.missing == ()


def test_an_agent_on_a_closed_redis_starts_on_tls_and_refuses_an_old_image(
    client, fake_clusters, mongo
):
    """닫힌 Redis 를 바인딩한 Agent 는 첫 배포부터 TLS 끝점이다. `redis_tls` 를 알리지 않은 init 은
    409 runtime_image_too_old(missing redis_tls)이고, 그 기록이 남은 뒤의 재배포도 같은 409 다.
    알리는 이미지는 받는다."""
    made: dict = {}

    def spec(gw, _ds):
        ds = tls_database(client, "mem")
        mongo["data_services"].update_one(
            {"_id": ds["id"]},
            {"$set": {"plain_closed": True, "capabilities": {"checkpointer": "ready"}}},
        )
        made["ds"] = ds
        return template_spec(source_id=gw["id"], ds_id=ds["id"])

    agent = deployed_agent(client, fake_clusters, mongo, spec=spec, runtime=False)
    dep = agent.fake.get("apps/v1", "Deployment", agent.doc["k8s"]["name"], DEFAULT_NS)
    env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
    assert env["DAEGAM_DB_0_TLS"] == "1" and env["DAEGAM_DB_0_PORT"] == str(TLS_PORT)
    stored = mongo["agents"].find_one({"_id": agent.doc["id"]})
    assert stored["redis_tls_bindings"] == [made["ds"]["id"]]

    old = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: "cancel"}
    r = client.get(f"{AG}/{agent.doc['id']}/agent.json", headers=old)
    assert detail_code(r) == (409, "runtime_image_too_old")
    assert r.json()["detail"]["missing"] == [REDIS_TLS]
    r = client.post(f"{AG}/{agent.doc['id']}/redeploy", json={})
    assert detail_code(r) == (409, "runtime_image_too_old")

    new = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: f"cancel,{REDIS_TLS}"}
    assert client.get(f"{AG}/{agent.doc['id']}/agent.json", headers=new).status_code == 200


# -- MCP Server: 세션 저장소 -------------------------------------------------------------------


def test_session_storage_on_a_closed_redis_needs_the_session_tls_gate(client, fake_clusters, mongo):
    """복제 서버의 세션 저장소가 평문 포트가 닫힌 Redis 바인딩에 놓이면 0.52 아래 클러스터는 그
    게이트의 422 다. 0.52 이상은 TLS 끝점이다. 클러스터 기본 Redis 도 같은 판단
    (`_closed_session_refusal`)을 지나고, 그 갈래는 아래의 DB 없는 시험과 test_redis_tls.py 가
    본다."""
    cid, bp, _ds, ms = mcp_server_env(client)
    ds = tls_database(client, "secure")
    _closed(mongo, ds["id"])
    replicated_server(
        client, bp, ms, bindings=[{"data_service_id": ds["id"], "env_prefix": "CACHE"}]
    )
    record_toolhive(mongo, cid, "0.51.2")
    r = clone_and_deploy(client, ms, name="calc-1")
    assert detail_code(r) == (422, "toolhive_too_old")
    assert r.json()["detail"]["feature"] == "session_tls"

    record_toolhive(mongo, cid, "0.52.0")
    r = clone_and_deploy(client, ms, name="calc-2")
    assert r.status_code == 201, r.text
    cr = fake_clusters.by_name["edge"].get(GV, "MCPServer", "mcp-calc-2", DEFAULT_NS)
    storage = cr["spec"]["sessionStorage"]
    assert storage["address"] == f"redis.svc:{TLS_PORT}" and "tls" in storage


# -- 닫힌 표시는 플랫폼의 것이다 ---------------------------------------------------------------


def test_a_closed_default_redis_without_its_ca_in_tenant_namespaces_names_the_rights():
    """세션 저장소를 TLS 로 렌더할 수 있는 판이어도 기본 Redis 의 CA 를 테넌트 네임스페이스에 둘 수
    없으면 권한의 422 본문이다. has 는 클러스터의 ToolHive 판이다."""
    cluster = cluster_doc()
    cluster.status.preflight = None
    cluster.recorded_toolhive_version = "0.53.0"
    default = DefaultRedis(
        address="redis.shared:6379", tls={"port": TLS_PORT, "ca": ca_pem()}, plain_closed=True
    )
    effective = effective_config(replicas=2, default_redis=default)
    session = SessionOptions(session_tls=True, default_redis_ca=False)
    rev = SimpleNamespace(bindings=[])
    refusal = _closed_session_refusal(cluster, rev, {}, effective, session)
    assert refusal is not None and refusal["code"] == "cluster_rights_missing"
    assert refusal["found"] == "ToolHive 0.53.0 without the daegam-tenant-objects rights"
    ready = SessionOptions(session_tls=True, default_redis_ca=True)
    assert _closed_session_refusal(cluster, rev, {}, effective, ready) is None
