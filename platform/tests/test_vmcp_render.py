"""Gateway vMCP 렌더의 사람 쪽 인증 (D22). oidc 모드의 incomingAuth(그룹·client id 정책과 덧붙인
authz_policy), OAuth 인가 서버가 켜진 local 의 mcp scope (D59) 다.

incomingAuth 빌더(`vmcp_incoming_auth`)와 미리보기 API 를 함께 본다. header 모드의 anonymous 와
permit-all 정책, outgoingAuth 는 test_gateways.py 의
test_preview_from_draft_renders_group_entries_vmcp 가 본다.
"""

from __future__ import annotations

from types import SimpleNamespace

from daegam.auth.cedar import client_id_policy, group_policy
from daegam.k8s.labels import LABEL_PROFILE_REVISION, LABEL_RESOURCE
from daegam.k8s.templates import oidc_config_ref, vmcp_incoming_auth
from daegam.k8s.templates.vmcp import (
    LABEL_GATEWAY_PROFILE,
    render_entry,
    render_group,
    render_vmcp,
    vmcp_policies,
)
from daegam.store.clusters import ClusterScheduling
from tests.helpers.auth import admin_headers
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import GW, HUB, TENANT_HEADER
from tests.helpers.hub import SPIKE_URL

EXTRA = 'permit(principal, action == Action::"call_tool", resource);'


def test_incoming_auth_snapshot():
    ia = vmcp_incoming_auth(
        oidc_ref=oidc_config_ref("daegam-mcp"),
        policies=vmcp_policies(
            tenant_group="daegam-acme",
            agent_client_ids=["agent-1", "agent-2"],
            client_id_claim="cid",
        ),
        groups_claim=None,
    )
    assert ia == {
        "type": "oidc",
        "oidcConfigRef": {"name": "daegam-oidc", "audience": "daegam-mcp"},
        "authzConfig": {
            "type": "inline",
            "inline": {
                "policies": [
                    group_policy("daegam-acme"),
                    client_id_policy("agent-1"),
                    client_id_policy("agent-2"),
                ]
            },
        },
    }
    # authz_policy 오버라이드는 목록 끝에 붙는다. 참조는 받은 그대로다
    ref = oidc_config_ref("other-aud", "https://gw.example/mcp", ("mcp",))
    policies = vmcp_policies(
        tenant_group="g", agent_client_ids=[], extra_policy=f" {EXTRA} ", client_id_claim="cid"
    )
    ia = vmcp_incoming_auth(oidc_ref=ref, policies=policies, groups_claim=None)
    assert ia["oidcConfigRef"] == {
        "name": "daegam-oidc",
        "audience": "other-aud",
        "resourceUrl": "https://gw.example/mcp",
        "scopes": ["mcp"],
    }
    assert ia["authzConfig"]["inline"]["policies"] == [group_policy("g"), EXTRA]


def test_oauth_advertises_the_mcp_scope_on_the_vmcp_ref(oauth_client, fake_clusters, mongo):
    """OAuth 인가 서버가 켜진 local 에서는 vMCP 의 사람 참조가 `scopes: ["mcp"]` 를 싣는다. PRM 의
    scopes_supported 와 401 의 scope 가 이것이다. 꺼진 모드의 참조에는 위 oidc 테스트들처럼 없다
    (P33 14절)."""
    c = oauth_client
    c.headers.update({**admin_headers(c, mongo), TENANT_HEADER: "default"})
    default_cluster(c)
    hub = c.post(HUB, json={"name": "spike", "kind": "remote", "url": SPIKE_URL}).json()
    r = c.post(GW, json={"name": "bot", "backends": [{"hub_item_id": hub["id"]}]})
    assert r.status_code == 201, r.text
    spec = c.get(f"{GW}/{r.json()['id']}/revisions/1/preview").json()["manifest"]["spec"]
    assert spec["incomingAuth"]["oidcConfigRef"] == {
        "name": "daegam-oidc",
        "audience": "daegam-mcp",
        "scopes": ["mcp"],
    }


def test_the_exposure_front_renders_with_its_own_resource_and_no_gateway_labels():
    """노출한 MCP Server 의 vMCP 앞단은 Gateway 의 렌더를 자기 resource 값으로 쓴다. Entry 에는 허브
    항목 라벨이 없고 vMCP 에는 Gateway 정의 라벨이 없으며 리비전 라벨은 있다 (P54 5절)."""
    group = render_group(
        tenant="t",
        instance_id="s",
        name="front-mcp-a",
        namespace="ns",
        description="d",
        resource="mcp-instance-front",
    )
    entry = render_entry(
        tenant="t",
        instance_id="s",
        name="front-mcp-a",
        namespace="ns",
        group="front-mcp-a",
        url="http://mcp-mcp-a-proxy.ns.svc.cluster.local:8080/mcp",
        transport="streamable-http",
        hub_item_id=None,
        resource="mcp-instance-front",
    )
    vmcp = render_vmcp(
        tenant="t",
        instance_id="s",
        profile_id=None,
        revision=4,
        name="front-mcp-a",
        namespace="ns",
        group="front-mcp-a",
        backends=[{"workload": "front-mcp-a"}],
        failure_mode="best_effort",
        # pod_placement 가 읽는 두 칸만 있다
        effective=SimpleNamespace(  # type: ignore[arg-type]
            images=SimpleNamespace(pull_secret=None), scheduling=ClusterScheduling()
        ),
        resource="mcp-instance-front",
    )
    for doc in (group, entry, vmcp):
        assert doc["metadata"]["labels"][LABEL_RESOURCE] == "mcp-instance-front"
    assert "daegam.devops.ai.kr/hub-item" not in entry["metadata"]["labels"]
    assert LABEL_GATEWAY_PROFILE not in vmcp["metadata"]["labels"]
    template = vmcp["spec"]["podTemplateSpec"]["metadata"]["labels"]
    assert template[LABEL_PROFILE_REVISION] == "4" and LABEL_GATEWAY_PROFILE not in template
