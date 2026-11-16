"""서버마다의 audience (P50 9절, G9 1단계).

소비자가 서버 하나에 내는 토큰은 테넌트 audience 와 그 서버 자신의 audience 를 함께 싣는다.
Gateway 의 vMCP 는 토큰을 받는 Entry 마다, 노출 앞단은 하나, Agent 는 MCP Server 소스마다 토큰을
든다. 서버 id 는 MCP Server 문서, 허브 image 항목, 공유 복제본 기록의 id 다. 상태 API 는 아직 그
토큰을 적용하지 않은 소비자를 서버마다 보인다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.auth.signer import internal_audience, server_audience
from daegam.k8s.templates import svc_token
from daegam.k8s.templates.agent import (
    MCP_TOKEN_ENV,
    MCP_TOKEN_TABLE_ENV,
    MCP_TOKEN_TABLE_HEADER,
    mcp_token_env,
    mcp_token_table,
)
from daegam.services.gateways import GatewayError
from tests.helpers.agent_runtime import agent_container, agent_deployment, env_map, token_of
from tests.helpers.agents import (
    agent_body,
    agent_env,
    deploy_spec,
    seed_tool_server,
    template_for,
    with_tool_server,
)
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, GW, OTHER
from tests.helpers.gateways import create_gateway, gateway_up
from tests.helpers.hub import hub_env
from tests.helpers.svc_tokens import aud_of, entry_object_name

TENANT_AUD = internal_audience("default")
LEGACY_AUTH = {"kind": "bearer", "secret_env": MCP_TOKEN_ENV}


def _opens(tenant: str, server_id: str) -> list[str]:
    """서버 server_id 를 여는 1단계 토큰의 audience."""
    return [internal_audience(tenant), server_audience(tenant, server_id)]


def _secret_token(fake, name: str, namespace: str = DEFAULT_NS) -> str:
    return fake.get("v1", "Secret", name, namespace)["stringData"]["token"]


def _calc_id(items: dict[str, Any]) -> str:
    """`hub_env` 의 calculator 항목(internal) id."""
    return items["calculator"]["id"]


def _rerender(client, store: str, service: str, doc_id: str) -> bool:
    """소비자 하나를 지금 설정으로 다시 렌더한다(인증 재적용과 같은 길, P32 8절)."""
    state = client.app.state
    doc = client.portal.call(getattr(state, store).get, doc_id)
    return client.portal.call(getattr(state, service).reapply_auth, doc)


# -- Gateway ---------------------------------------------------------------------------------


def test_a_rerender_refuses_a_server_of_another_tenant(client, fake_clusters, mongo):
    """스냅샷을 채우다 다른 테넌트의 서버를 만나면 토큰을 내지 않고 422 다."""
    _cid, items, _calc, _orders, _remote, _fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="gw-a", backends=[{"hub_item_id": _calc_id(items)}])
    mongo["gateways"].update_one(
        {"_id": inst["id"]}, {"$set": {"deployed_backends.0.server_id": None}}
    )
    mongo["hub_items"].update_one({"_id": _calc_id(items)}, {"$set": {"tenant": OTHER}})
    with pytest.raises(GatewayError) as e:
        _rerender(client, "gateways", "gateway_service", inst["id"])
    assert (e.value.status, e.value.code) == (422, "cross_tenant_unsupported")


# -- 노출 앞단과 상태 -------------------------------------------------------------------------


def test_entry_token_names_refuse_a_collision(client, fake_clusters, monkeypatch):
    """Entry 토큰 객체 이름은 Entry 이름 해시 12자를 붙인다. 두 Entry 가 같은 이름이 되면 한 토큰이
    두 서버에 가게 되므로 렌더하지 않고 422 다."""
    name = entry_object_name("gw-a", "a-calculator")
    assert name.startswith("svc-gw-a-") and len(name.rsplit("-", 1)[1]) == 12
    _cid, items, _calc, _orders, _remote, _fake = hub_env(client, fake_clusters)
    draft = create_gateway(client, items, name="a")
    assert draft.status_code == 201, draft.text
    monkeypatch.setattr(svc_token, "stable_hash", lambda _value, length: "0" * length)
    r = client.get(f"{GW}/{draft.json()['id']}/revisions/1/preview")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "svc_token_name_collision"


# -- Agent -----------------------------------------------------------------------------------


def _tool_auth(client, agent_id: str, token: str, digest: str | None = None) -> dict[str, Any]:
    """init 이 받는 agent.json 의 첫 도구 소스 인증. digest 는 init 이 보내는 토큰 표 지문이다."""
    headers = bearer(token)
    if digest is not None:
        headers[MCP_TOKEN_TABLE_HEADER] = digest
    r = client.get(f"{AG}/{agent_id}/agent.json", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["tools"][0]["auth"]


def test_an_agent_holds_one_token_per_mcp_server_source(client, fake_clusters, mongo):
    """MCP Server 소스마다 토큰 하나가 `DAEGAM_MCP_TOKEN_<id>` 로 내려가고 그 서버를 연다. init 은
    자기 템플릿의 표 지문을 보내고 init 때의 컴파일이 그 표로 env 를 고른다. 소스를 바꿔 다시
    배포하면 새 표가 앞에 오고, 앞 템플릿의 표와 키는 남는다."""
    cid, ds, gw = agent_env(client, mongo)
    s1 = seed_tool_server(mongo, cluster_id=cid, name="orders")
    s2 = seed_tool_server(mongo, cluster_id=cid, name="billing")
    _p, inst = deploy_spec(client, with_tool_server(template_for(gw, ds), s1))
    fake = fake_clusters.by_name["edge"]
    t1, t2 = mcp_token_table([s1]), mcp_token_table([s2])
    env1, env2 = mcp_token_env(s1), mcp_token_env(s2)
    assert t1.envs == {s1: env1} and env1 == f"DAEGAM_MCP_TOKEN_{s1.replace('-', '').upper()}"
    saved = mongo["agents"].find_one({"_id": inst["id"]})
    assert saved["mcp_token_tables"] == [t1.model_dump()] and saved["svc_token_servers"] == [s1]
    secret_name = inst["k8s"]["token_secret"]
    secret = fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]
    assert aud_of(secret[env1]) == _opens("default", s1)
    # 표가 생기기 전 파드가 쓰던 옛 단일 토큰은 테넌트 audience 하나로 남는다
    assert aud_of(secret["mcp_token"]) == TENANT_AUD
    ref = {"secretKeyRef": {"name": secret_name, "key": env1}}
    assert env_map(agent_container(fake))[env1] == ref
    init = agent_deployment(fake)["spec"]["template"]["spec"]["initContainers"][0]
    assert {"name": MCP_TOKEN_TABLE_ENV, "value": t1.digest} in init["env"]
    token = token_of(fake, inst)
    assert _tool_auth(client, inst["id"], token, t1.digest) == {
        "kind": "bearer",
        "secret_env": env1,
    }
    # 표를 모르는 파드(지문 없음)와 문서가 기억하지 않는 지문은 옛 단일 토큰이다
    assert _tool_auth(client, inst["id"], token) == LEGACY_AUTH
    assert _tool_auth(client, inst["id"], token, "0" * 16) == LEGACY_AUTH

    spec = with_tool_server(template_for(gw, ds), s2)
    assert client.put(f"{AG}/{inst['id']}", json=agent_body(spec)).status_code == 200
    r = client.post(f"{AG}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    saved = mongo["agents"].find_one({"_id": inst["id"]})
    assert saved["mcp_token_tables"] == [t2.model_dump(), t1.model_dump()]
    assert saved["svc_token_servers"] == [s2]
    secret = fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]
    assert aud_of(secret[env2]) == _opens("default", s2) and env1 in secret
    env = env_map(agent_container(fake))
    assert env2 in env and env1 not in env
    token = token_of(fake, inst)
    assert _tool_auth(client, inst["id"], token, t2.digest) == {
        "kind": "bearer",
        "secret_env": env2,
    }
    # 앞 템플릿의 파드는 자기 표로 컴파일하고 그 표에 없는 소스는 옛 단일 토큰이다
    assert _tool_auth(client, inst["id"], token, t1.digest) == LEGACY_AUTH

    # 해제는 표와 서버 목록을 비운다
    assert client.post(f"{AG}/{inst['id']}/undeploy").status_code == 200
    saved = mongo["agents"].find_one({"_id": inst["id"]})
    assert saved["mcp_token_tables"] == [] and saved["svc_token_servers"] == []
