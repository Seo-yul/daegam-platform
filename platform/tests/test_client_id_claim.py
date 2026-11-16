"""oidc 모드의 Agent 정책이 IdP 의 client id 클레임을 읽는다.

플랫폼 검증기는 client id 를 `DAEGAM_OIDC_CLIENT_ID_CLAIM` 으로 읽는다(Keycloak 은 azp). Gateway 의
vMCP, 노출한 MCP Server 의 앞단, 노출 Agent 의 런타임도 같은 이름을 읽어야 Agent 의 client
credentials 토큰이 통과한다. 이 모듈은 `oidc_settings` 를 그 클레임과 UI client id 로 바꿔
렌더를 보고, UI client id 를 Agent client id 로 두는 저장을 거절하는지 본다.
"""

from __future__ import annotations

import dataclasses

import pytest

from daegam.config import Settings
from tests.helpers.agent_runtime import agent_deployment, env_map
from tests.helpers.agents import TOKEN_URL, agent_body, seed_tool_gateway, template_for
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import ACME_NS, AG
from tests.helpers.data_services import created_database
from tests.helpers.exposure import INGRESS, cluster_secret, ingress_class
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import detail_code

UI_CLIENT = "daegam-ui"


@pytest.fixture
def oidc_settings(oidc_settings: Settings) -> Settings:
    return dataclasses.replace(oidc_settings, oidc_client_id_claim="azp", oidc_client_id=UI_CLIENT)


def _tenant(client, h, client_ids: list[str]) -> None:
    r = client.patch(
        "/tenants/acme/settings",
        json={
            "mcp_group": "okta-acme",
            "agent_client_ids": client_ids,
            "agent_oauth": {
                "client_id": "agent-1",
                "secret_name": "agent-oauth",
                "token_url": TOKEN_URL,
                "scope": "mcp",
            },
        },
        headers=h,
    )
    assert r.status_code == 200, r.text


def _exposure_cluster(client, h, fake_clusters) -> tuple[FakeClusterClient, str]:
    fake = fake_clusters.by_name.setdefault("edge", FakeClusterClient())
    ingress_class(fake, "traefik")
    cluster_secret(fake, ACME_NS, "site-tls", tls=True)
    cid = default_cluster(client, headers=h, ingress={**INGRESS, "allow_unauthenticated": False})
    return fake, cid


def test_the_exposed_agent_gets_the_idp_client_id_claim(
    oidc_client, oidc_headers, mongo, fake_clusters
):
    h = oidc_headers
    fake, cid = _exposure_cluster(oidc_client, h, fake_clusters)
    r = oidc_client.patch(
        "/settings", json={"llm_providers": [{"name": "local-stub", "kind": "stub"}]}, headers=h
    )
    assert r.status_code == 200, r.text
    _tenant(oidc_client, h, ["agent-1"])
    ds = created_database(oidc_client, headers=h)
    gw = seed_tool_gateway(mongo, tenant="acme", cluster_id=cid, namespace=ACME_NS)
    r = oidc_client.post(AG, json=agent_body(template_for(gw, ds), "bot"), headers=h)
    assert r.status_code == 201, r.text
    r = oidc_client.post(
        f"{AG}/{r.json()['id']}/deploy",
        json={
            "exposure": {
                "enabled": True,
                "host": "bot.acme.example",
                "tls": {"mode": "secret", "secret_name": "site-tls"},
            }
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    container = agent_deployment(fake, "agent-bot", ACME_NS)["spec"]["template"]["spec"][
        "containers"
    ][0]
    env = env_map(container)
    assert env["DAEGAM_OIDC_CLIENT_ID_CLAIM"] == "azp"
    assert env["DAEGAM_AGENT_CLIENT_IDS"] == "agent-1"


def test_the_ui_client_id_cannot_be_an_agent_client_id(oidc_client, oidc_headers):
    """UI 의 client id 는 모든 사람의 로그인 토큰이 싣는다. Agent client id 로 두면 client id 정책이
    그룹과 상관없이 모든 사람을 받는다."""
    r = oidc_client.patch(
        "/tenants/acme/settings",
        json={"agent_client_ids": ["agent-1", UI_CLIENT]},
        headers=oidc_headers,
    )
    assert detail_code(r) == (422, "agent_client_id_is_ui_client")
