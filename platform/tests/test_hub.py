"""P4 F1 MCP Hub: 렌더 스냅샷 3종, 생성·상태·삭제, refresh-tools, internal 자동 등재."""

from __future__ import annotations

import jwt
import pytest

from daegam.auth.principal import Membership, Principal
from daegam.k8s.templates import is_private_endpoint, proxy_mcp_url
from daegam.k8s.templates.common import proxy_resources
from daegam.services.hub import HubError
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import GV, HUB, MS, T2
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import deployed, detail_code
from tests.helpers.hub import SPIKE_URL
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env


def _bootstrap(client):
    """이미지 pull Secret 이 regcred 인 기본 클러스터. image 항목의 MCPServer 가 그것을 싣는다."""
    return default_cluster(client, images={"pull_secret": "regcred"})


def test_is_private_endpoint_and_url_paths():
    assert is_private_endpoint(SPIKE_URL)
    assert is_private_endpoint("http://a.ns.svc:8080")
    assert is_private_endpoint("http://192.168.1.5:8080/mcp")
    assert is_private_endpoint("http://localhost:1/mcp")
    assert not is_private_endpoint("https://mcp.example.com/mcp")
    assert proxy_mcp_url("http://x.svc:8080", "streamable-http") == "http://x.svc:8080/mcp"
    assert proxy_mcp_url("http://x.svc:8080/mcp", "streamable-http") == "http://x.svc:8080/mcp"
    assert proxy_mcp_url("http://x.svc:8080", "sse") == "http://x.svc:8080/sse"


def test_remote_renders_mcpremoteproxy(client, fake_clusters):
    _bootstrap(client)
    r = client.post(HUB, json={"name": "calc-remote", "kind": "remote", "url": SPIKE_URL})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["kind"] == "remote" and item["state"] == "created"
    assert item["k8s"] == {
        "kind": "MCPRemoteProxy",
        "name": "hub-calc-remote",
        "namespace": "daegam-default",
        "group": None,
        "tool_config": None,
    }
    assert item["source"]["transport"] == "streamable-http"
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    cr = fake.get(GV, "MCPRemoteProxy", "hub-calc-remote", "daegam-default")
    # groupRef 없음: 그룹 소속은 Agent 그룹의 Entry 래퍼가 담당한다
    # (같은 그룹의 자기 게시물은 무한 재귀)
    assert cr["spec"] == {
        "remoteUrl": SPIKE_URL,
        "transport": "streamable-http",
        "allowPrivateEndpoint": True,
        "proxyPort": 8080,
        "resources": proxy_resources(),  # D54
        # 원격 호출도 MCP 감사에 남는다
        "audit": {"enabled": True},
        # 역할 없는 ServiceAccount 로 돈다 (P50 10절)
        "serviceAccount": "daegam-remote-proxy",
    }
    assert cr["metadata"]["labels"]["daegam.devops.ai.kr/hub-kind"] == "remote"
    assert cr["metadata"]["labels"]["daegam.devops.ai.kr/id"] == item["id"]
    # 네임스페이스·그룹이 먼저 보장됐다
    assert fake.get(GV, "MCPGroup", "default", "daegam-default") is not None
    # 외부 URL 은 allowPrivateEndpoint false
    r = client.post(
        HUB,
        json={
            "name": "ext",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "transport": "sse",
        },
    )
    cr = fake.get(GV, "MCPRemoteProxy", "hub-ext", "daegam-default")
    assert cr["spec"]["allowPrivateEndpoint"] is False and cr["spec"]["transport"] == "sse"
    # status: CR 에 status 가 없으면 phase None, ready False
    st = client.get(f"{HUB}/{item['id']}").json()["status"]
    assert st["ready"] is False and st["phase"] is None
    fake.set_status(
        GV,
        "MCPRemoteProxy",
        "hub-calc-remote",
        "daegam-default",
        {
            "phase": "Ready",
            "url": "http://mcp-hub-calc-remote-remote-proxy.daegam-default.svc.cluster.local:8080",
        },
    )
    st = client.get(f"{HUB}/{item['id']}").json()["status"]
    assert st["ready"] is True and st["phase"] == "Ready"
    assert (
        st["url"]
        == "http://mcp-hub-calc-remote-remote-proxy.daegam-default.svc.cluster.local:8080/mcp"
    )
    assert [i["name"] for i in client.get(HUB).json()] == ["calc-remote", "ext"]
    assert client.get(HUB, headers=T2).json() == []


def test_entry_keeps_url_without_cr(client, fake_clusters):
    _bootstrap(client)
    r = client.post(HUB, json={"name": "calc-entry", "kind": "entry", "url": SPIKE_URL})
    assert detail_code(r) == (422, "invalid_value")  # transport 필수
    r = client.post(
        HUB,
        json={
            "name": "calc-entry",
            "kind": "entry",
            "url": SPIKE_URL,
            "transport": "streamable-http",
        },
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert (
        item["k8s"]["kind"] is None
        and item["k8s"]["name"] == "hub-calc-entry"
        and item["state"] == "created"
    )
    fake = fake_clusters.by_name["edge"]
    assert fake.find("MCPServerEntry") == [] and fake.find("MCPRemoteProxy") == []
    # CR 이 없으니 URL 만 있으면 ready, url 은 원래 remoteUrl
    st = client.get(f"{HUB}/{item['id']}").json()["status"]
    assert st["ready"] is True and st["phase"] == "Valid" and st["url"] == SPIKE_URL
    assert client.get(f"{HUB}/{item['id']}/manifest").json()["documents"] == []
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200


def test_image_renders_mcpserver_with_toolconfig(client, fake_clusters):
    _bootstrap(client)
    r = client.post(
        HUB,
        json={
            "name": "fetch",
            "kind": "image",
            "image": "ghcr.io/x/fetch:1",
            "port": 8000,
            "tools_filter": ["fetch", "search"],
        },
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["k8s"]["kind"] == "MCPServer" and item["k8s"]["tool_config"] == "hub-fetch-tools"
    assert item["tool_names"] == ["fetch", "search"]
    fake = fake_clusters.by_name["edge"]
    tc = fake.get(GV, "MCPToolConfig", "hub-fetch-tools", "daegam-default")
    assert tc["spec"] == {"toolsFilter": ["fetch", "search"]}
    cr = fake.get(GV, "MCPServer", "hub-fetch", "daegam-default")
    assert cr["spec"]["image"] == "ghcr.io/x/fetch:1" and cr["spec"]["mcpPort"] == 8000
    assert cr["spec"]["toolConfigRef"] == {"name": "hub-fetch-tools"} and cr["spec"][
        "groupRef"
    ] == {"name": "default"}
    assert cr["spec"]["podTemplateSpec"]["spec"]["imagePullSecrets"] == [{"name": "regcred"}]
    # 허브 image 항목도 MCP Server 와 같다. spec.resources 는 ToolHive 가 프록시에 넣으므로
    # 고정값이고, 클러스터 기본 자원이 없으니 MCP 컨테이너에는 자원이 없다 (D54)
    assert cr["spec"]["resources"] == proxy_resources()
    assert "resources" not in cr["spec"]["podTemplateSpec"]["spec"]["containers"][0]
    # 테넌트 울타리 설정이 서버보다 먼저 받아들여지고 서버가 참조한다 (P50 11절)
    kinds = [o["kind"] for o in fake.applied]
    assert kinds.index("MCPToolConfig") < kinds.index("MCPAuthzConfig") < kinds.index("MCPServer")
    assert cr["spec"]["authzConfigRef"] == {"name": "hub-fetch-authz"}
    m = client.get(f"{HUB}/{item['id']}/manifest").json()
    kinds = [d["kind"] for d in m["documents"]]
    assert kinds == ["MCPToolConfig", "MCPAuthzConfig", "MCPServer"]
    # 삭제는 역순으로 모두 지운다
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert fake.get(GV, "MCPServer", "hub-fetch", "daegam-default") is None
    assert fake.get(GV, "MCPAuthzConfig", "hub-fetch-authz", "daegam-default") is None
    assert fake.get(GV, "MCPToolConfig", "hub-fetch-tools", "daegam-default") is None
    assert client.get(f"{HUB}/{item['id']}").status_code == 404


def test_validation_and_conflicts(client):
    _bootstrap(client)
    assert (
        client.post(HUB, json={"name": "a", "kind": "remote", "url": SPIKE_URL}).status_code == 201
    )
    r = client.post(HUB, json={"name": "a", "kind": "remote", "url": SPIKE_URL})
    assert detail_code(r) == (409, "name_conflict")
    # http(s) 이고 호스트가 있으며 공백이 없어야 한다. 뒤의 둘은 예전 접두어 검사가 통과시켰다
    for i, url in enumerate(("ftp://x", "http://:80/mcp", "https://h st/mcp")):
        r = client.post(HUB, json={"name": f"b{i}", "kind": "remote", "url": url})
        assert detail_code(r) == (422, "invalid_value"), url
    r = client.post(HUB, json={"name": "c", "kind": "image"})
    assert r.status_code == 422
    r = client.post(HUB, json={"name": "d", "kind": "internal", "url": SPIKE_URL})
    assert r.status_code == 422
    r = client.post(HUB, json={"name": "bad name", "kind": "remote", "url": SPIKE_URL})
    assert detail_code(r) == (422, "invalid_name")
    r = client.post(
        HUB, json={"name": "e", "kind": "remote", "url": SPIKE_URL, "cluster_id": "nope"}
    )
    assert detail_code(r) == (422, "unknown_cluster")


def test_refresh_tools_uses_proxy_url(client, fake_clusters, mongo):
    """새로 고침은 프록시 주소로 묻는다. 도구 목록을 받는 사이 저장된 접근 정책을 되돌리지 않는다
    (P68 2절)."""
    _bootstrap(client)
    item = client.post(HUB, json={"name": "calc-remote", "kind": "remote", "url": SPIKE_URL}).json()
    fake = fake_clusters.by_name["edge"]
    calls = []

    async def lister(_client, url, bearer=None):
        calls.append(url)
        return ["Add", "Divide"]

    client.app.state.hub_service.tool_lister = lister
    # URL 이 아직 없으면 409
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert detail_code(r) == (409, "not_ready")
    fake.set_status(
        GV,
        "MCPRemoteProxy",
        "hub-calc-remote",
        "daegam-default",
        {
            "phase": "Ready",
            "url": "http://mcp-hub-calc-remote-remote-proxy.daegam-default.svc.cluster.local:8080",
        },
    )
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert r.status_code == 200, r.text
    assert r.json()["tool_names"] == ["Add", "Divide"] and r.json()["tool_names_at"]
    assert calls == [
        "http://mcp-hub-calc-remote-remote-proxy.daegam-default.svc.cluster.local:8080/mcp"
    ]
    assert client.get(f"{HUB}/{item['id']}").json()["tool_names"] == ["Add", "Divide"]

    async def broken(_client, url, bearer=None):
        raise ConnectionError("refused")

    client.app.state.hub_service.tool_lister = broken
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert detail_code(r) == (502, "tools_list_failed")

    svc = client.app.state.hub_service

    async def racing(_client, url, bearer=None):
        fresh = await svc.store.get_unscoped(item["id"])
        fresh.access_updated_by = "admin-b"
        assert await svc.store.save_access(fresh)
        return ["Add"]

    svc.tool_lister = racing
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert r.status_code == 200, r.text
    doc = mongo["hub_items"].find_one({"_id": item["id"]})
    assert doc["tool_names"] == ["Add"] and doc["seq"] == 1
    assert doc["access_updated_by"] == "admin-b"


def test_tools_endpoint_lists_schemas_live_and_never_stores_them(client, fake_clusters, mongo):
    """D48: 도구 탭이 부르는 GET /hub/{id}/tools 는 그때그때 물어보고 아무것도 쓰지 않는다.
    못 물어보면 200 에 listed=false 와 까닭이다. 화면이 저장된 이름으로 떨어지기 때문이다.
    읽기인데도 플랫폼이 밖으로 나가는 호출이라 성공해도 감사에 남기고, 다른 읽기는 남기지 않는다.
    판정은 라우팅 전이라 경로 문자열로 하는데, `/tools` 로 끝나는지만 보면 이름이 tools 인 다른
    리소스 조회까지 걸린다."""
    from daegam.audit.middleware import _audited_read
    from daegam.mcp_client import ToolSchema, ToolsResult

    _bootstrap(client)
    item = client.post(HUB, json={"name": "calc-remote", "kind": "remote", "url": SPIKE_URL}).json()
    mongo["audit_events"].delete_many({})

    # URL 이 아직 없으면 오류가 아니라 까닭이다
    r = client.get(f"{HUB}/{item['id']}/tools")
    assert r.status_code == 200, r.text
    assert r.json()["listed"] is False and "no URL yet" in r.json()["reason"]
    assert r.json()["tools"] == []

    fake = fake_clusters.by_name["edge"]
    fake.set_status(
        GV,
        "MCPRemoteProxy",
        "hub-calc-remote",
        "daegam-default",
        {
            "phase": "Ready",
            "url": "http://mcp-hub-calc-remote-remote-proxy.daegam-default.svc.cluster.local:8080",
        },
    )

    async def lister(_client, _url, bearer=None):
        return ToolsResult(
            server="calc",
            protocol_version="1.0",
            tools=["Add"],
            tool_schemas=[
                ToolSchema(
                    name="Add",
                    title="두 수 더하기",
                    description="a 와 b 를 더한다",
                    input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                    output_schema=True,
                )
            ],
        )

    client.app.state.hub_service.schema_lister = lister
    body = client.get(f"{HUB}/{item['id']}/tools").json()
    assert body["listed"] is True and body["reason"] is None
    assert body["tools"] == [
        {
            "name": "Add",
            "title": "두 수 더하기",
            "description": "a 와 b 를 더한다",
            "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}},
            "schema_omitted": False,
            "output_schema": True,
        }
    ]
    # 저장하지 않는다. 문서의 tool_names 는 refresh-tools 만 쓴다
    assert client.get(f"{HUB}/{item['id']}").json()["tool_names"] == []
    assert "tools" not in client.get(HUB).json()[0]

    async def broken(_client, _url, bearer=None):
        raise ConnectionError("refused")

    client.app.state.hub_service.schema_lister = broken
    body = client.get(f"{HUB}/{item['id']}/tools").json()
    assert body["listed"] is False and body["reason"] == "ConnectionError: refused"

    rows = list(mongo["audit_events"].find())
    assert [(r["method"], r["resource"], r["resource_id"]) for r in rows] == [
        ("GET", "hub", item["id"])
    ] * 3
    # 지금 `/tools` 로 끝나는 라우트는 이것 하나뿐이라 접미사 규칙으로도 통과한다. 남길 읽기가
    # 늘어날 때 옆 리소스까지 끌려 들어가지 않도록 모양 자체를 못박아 둔다
    assert _audited_read(f"/api/hub/{item['id']}/tools")
    assert not _audited_read("/api/agents/a1/tools")
    assert not _audited_read("/api/hub/h1/tools/extra")
    assert not _audited_read("/api/hub/h1/refresh-tools")


def test_tool_schemas_are_capped_without_losing_any_name():
    """상한은 이름과 설명을 지키고 스키마만 덜어낸다. 도구가 통째로 사라지면 개수가 tool_names 와
    어긋나 사람이 목록을 못 믿는다."""
    from daegam.mcp_client import ToolSchema
    from daegam.services.hub import TOOL_DESC_MAX, TOOL_SCHEMA_MAX_BYTES, _capped, _json_bytes

    huge = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(2000)}}
    assert len(_json_bytes(huge)) > TOOL_SCHEMA_MAX_BYTES
    out, truncated = _capped(
        [
            ToolSchema(name="big", description="d" * (TOOL_DESC_MAX + 500), input_schema=huge),
            ToolSchema(name="small", input_schema={"type": "object"}),
        ]
    )
    assert [d["name"] for d in out] == ["big", "small"]
    assert truncated is False
    assert out[0]["input_schema"] is None and out[0]["schema_omitted"] is True
    assert len(out[0]["description"]) == TOOL_DESC_MAX + 1  # 자른 표시 한 글자
    assert out[1]["input_schema"] == {"type": "object"} and out[1]["schema_omitted"] is False


def test_tool_schemas_total_stays_under_the_cap_even_if_names_alone_overflow():
    """이름에는 길이 제한이 없어 스키마를 다 버려도 상한을 넘길 수 있다. 그때 상한을 놓아 버리면
    남의 서버가 우리 응답 크기를 정하게 되므로 잘라내고 잘랐다고 말한다."""
    from daegam.mcp_client import ToolSchema
    from daegam.services.hub import TOOL_SCHEMAS_MAX_BYTES, _capped, _json_bytes

    many = [ToolSchema(name="n" * 4000, input_schema={"type": "object"}) for _ in range(200)]
    out, truncated = _capped(many)
    assert truncated is True
    assert 0 < len(out) < len(many)
    assert len(_json_bytes(out)) <= TOOL_SCHEMAS_MAX_BYTES


def test_the_measured_size_is_the_real_size():
    """상한 계산이 `_json_bytes(out)` 와 어긋나면 "상한이 걸린다" 는 말 자체가 헛말이 된다.
    배열 원소 사이는 콤마 하나가 아니라 ", " 두 바이트다."""
    from daegam.mcp_client import ToolSchema
    from daegam.services.hub import _SEP, _capped, _json_bytes

    for n in (0, 1, 2, 17):
        out, _ = _capped(
            [ToolSchema(name=f"t{i}", input_schema={"type": "object"}) for i in range(n)]
        )
        measured = sum(len(_json_bytes(d)) for d in out) + _SEP * max(len(out) - 1, 0) + 2
        assert measured == len(_json_bytes(out)), n


def test_capping_many_tools_does_not_blow_up_quadratically():
    """고칠 때마다 전체를 다시 직렬화하던 판은 600개에서 7초가 걸렸다. 크기는 한 번씩만 잰다."""
    import time

    from daegam.mcp_client import ToolSchema
    from daegam.services.hub import TOOL_SCHEMAS_MAX_BYTES, _capped, _json_bytes

    schema = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(60)}}
    tools = [ToolSchema(name=f"t{i}", input_schema=schema) for i in range(600)]
    started = time.monotonic()
    out, truncated = _capped(tools)
    assert time.monotonic() - started < 1.0
    assert len(out) == 600 and truncated is False
    assert len(_json_bytes(out)) <= TOOL_SCHEMAS_MAX_BYTES


def test_internal_items_follow_mcp_servers(client, fake_clusters, mongo):
    """D23: 배포해도 허브에 없다. publish 로 생기고, 재배포로 tool_names 가 바뀌며, 참조 중에는
    unpublish·삭제가 409 이고, 참조가 풀린 뒤 삭제로 사라진다."""
    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    assert inst["hub_item"] is None
    assert client.get(HUB).json() == []  # 자동 등재 없음
    assert client.get(f"{MS}/{inst['id']}").json()["hub_item"] is None
    # 게시돼 있지 않으면 해제는 404
    r = client.delete(f"{MS}/{inst['id']}/publish")
    assert detail_code(r) == (404, "not_published")

    r = client.post(f"{MS}/{inst['id']}/publish", json={})
    assert r.status_code == 201, r.text
    it = r.json()
    assert it["kind"] == "internal" and it["name"] == "calc-1" and it["state"] == "created"
    assert it["source"]["instance_id"] == inst["id"]
    assert it["source"]["mcp_profile_id"] == inst["id"]  # 합친 문서에서는 같은 값이다 (D42)
    assert it["k8s"] == {
        "kind": "MCPServer",
        "name": "mcp-calc-1",
        "namespace": "daegam-default",
        "group": "default",
        "tool_config": None,
    }
    assert it["permissions"] == {
        "view": True,
        "consume": True,
        "manage": True,
        "replace_header": False,
        "replace_secret_env": False,
    }
    assert (
        client.app.state.hub_service.backend_url(
            __import__("daegam.store.hub", fromlist=["HubItem"]).HubItem.model_validate(
                {**it, "status": {}}
            )
        )
        == "http://mcp-mcp-calc-1-proxy.daegam-default.svc.cluster.local:8080/mcp"
    )
    assert it["tool_names"] == ["Add"]  # 프로필 tools
    # fake CR 에 status 가 없다. internal 은 MCP Server 화면과 같은 phase 다 (T55)
    assert it["status"]["phase"] == "Pending"
    assert [i["id"] for i in client.get(HUB).json()] == [it["id"]]
    # 멱등: 다시 게시하면 200 으로 같은 항목
    r = client.post(f"{MS}/{inst['id']}/publish", json={"name": "other-name"})
    assert r.status_code == 200 and r.json()["id"] == it["id"] and r.json()["name"] == "calc-1"
    # 인스턴스 응답에 게시 요약이 붙는다
    ref = client.get(f"{MS}/{inst['id']}").json()["hub_item"]
    assert ref == {
        "id": it["id"],
        "name": "calc-1",
        "display_name": "calc-1",
        "permissions": {"view": True, "consume": True, "manage": True},
        "gateway_ids": [],
    }
    assert deployed(client, MS)[0]["hub_item"]["id"] == it["id"]
    # 리비전 변경 + redeploy → tool_names 갱신 (오버라이드 이름 반영). 정의는 배포된 문서 자신의 것
    client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
            "tool_overrides": {"Add": {"name": "add_numbers"}},
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    assert client.post(f"{MS}/{inst['id']}/redeploy").status_code == 200
    assert client.get(f"{HUB}/{it['id']}").json()["tool_names"] == ["add_numbers", "Divide"]
    # 빈 tools 면 백엔드 버전 전체
    client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    client.post(f"{MS}/{inst['id']}/redeploy")
    assert client.get(f"{HUB}/{it['id']}").json()["tool_names"] == ["Add", "Divide"]
    # 참조 중이면 해제 409 (DELETE /hub/{id} 와 같다). 인스턴스 응답이 그 프로필을 가리킨다
    fake_clusters.by_name["edge"].set_status(
        GV,
        "MCPServer",
        "mcp-calc-1",
        "daegam-default",
        {"phase": "Ready", "url": "http://mcp-mcp-calc-1-proxy.daegam-default.svc:8080/mcp"},
    )
    r = client.post("/gateways", json={"name": "gw", "backends": [{"hub_item_id": it["id"]}]})
    assert r.status_code == 201, r.text
    gp = r.json()["id"]
    r = client.delete(f"{MS}/{inst['id']}/publish")
    assert detail_code(r) == (409, "in_use")
    assert r.json()["detail"]["gateway_ids"] == [gp]
    r = client.delete(f"{HUB}/{it['id']}")
    assert detail_code(r) == (409, "in_use")
    assert client.get(f"{MS}/{inst['id']}").json()["hub_item"]["gateway_ids"] == [gp]
    # 참조 중이면 삭제도 409 in_use (D42 의 in_use 가드). 해제는 게시 중이라 409 published
    r = client.delete(f"{MS}/{inst['id']}")
    assert detail_code(r) == (409, "in_use")
    assert r.json()["detail"]["gateway_ids"] == [gp]
    r = client.post(f"{MS}/{inst['id']}/undeploy")
    assert detail_code(r) == (409, "published")
    # 참조를 지우면 삭제가 게시 항목까지 따라 지운다 (9절)
    assert client.delete(f"/gateways/{gp}").status_code == 200
    assert client.delete(f"{MS}/{inst['id']}").status_code == 200
    assert client.get(HUB).json() == []
    assert mongo["hub_items"].count_documents({}) == 0


def test_publish_and_unpublish_round_trip(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    # 이름을 주고 게시, 충돌은 409, 잘못된 이름은 422
    assert client.post(HUB, json={"name": "taken", "kind": "remote", "url": SPIKE_URL}).status_code
    r = client.post(f"{MS}/{inst['id']}/publish", json={"name": "taken"})
    assert detail_code(r) == (409, "name_conflict")
    r = client.post(f"{MS}/{inst['id']}/publish", json={"name": "bad name"})
    assert detail_code(r) == (422, "invalid_name")
    r = client.post(f"{MS}/{inst['id']}/publish", json={"name": "calc-pub"})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["name"] == "calc-pub"
    # 해제 = 허브 항목 삭제 (internal 도 지울 수 있다)
    r = client.delete(f"{MS}/{inst['id']}/publish")
    assert r.status_code == 200 and r.json() == {"id": item["id"], "deleted": True}
    assert client.get(f"{HUB}/{item['id']}").status_code == 404
    assert client.get(f"{MS}/{inst['id']}").json()["hub_item"] is None
    # 재배포는 게시돼 있지 않으면 허브에 손대지 않는다
    assert client.post(f"{MS}/{inst['id']}/redeploy").status_code == 200
    assert [i["name"] for i in client.get(HUB).json()] == ["taken"]
    # 다시 게시하면 새 항목, DELETE /hub/{id} 로도 해제된다
    item = client.post(f"{MS}/{inst['id']}/publish", json={}).json()
    assert item["name"] == "calc-1"
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert client.get(f"{MS}/{inst['id']}").json()["hub_item"] is None


def test_listing_tools_carries_a_platform_service_token_for_our_servers(client, fake_clusters):
    """internal·image 항목의 MCPServer 는 서비스 토큰을 요구한다 (D41). 플랫폼은 호출마다 짧은
    svc:platform 토큰을 낸다.

    D49 로 `internal` 의 refresh-tools 가 막히면서 이 검사가 도구 탭의 라이브 조회
    (`GET /hub/{id}/tools`, D48)로 옮겨 왔다. 토큰을 내는 자리(`_bearer_for`)는 둘이 함께 쓴다.
    """
    from daegam.mcp_client import ToolSchema, ToolsResult

    _cid, _bp, _ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    it = client.post(f"{MS}/{inst['id']}/publish", json={}).json()
    fake = fake_clusters.by_name["edge"]
    seen: list[str | None] = []

    async def lister(_client, url, headers=None):
        seen.append((headers or {}).get("Authorization", "").removeprefix("Bearer ") or None)
        return ToolsResult(
            server="s", protocol_version="1.0", tools=["Add"], tool_schemas=[ToolSchema(name="Add")]
        )

    client.app.state.hub_service.schema_lister = lister
    fake.set_status(
        GV,
        "MCPServer",
        inst["k8s"]["name"],
        inst["k8s"]["namespace"],
        {"phase": "Ready", "url": f"http://mcp-{inst['k8s']['name']}-proxy.svc:8080/mcp"},
    )
    r = client.get(f"{HUB}/{it['id']}/tools")
    assert r.status_code == 200 and r.json()["listed"] is True, r.text
    assert seen and seen[-1]
    claims = jwt.decode(seen[-1], options={"verify_signature": False})
    # 그 MCP Server 하나의 audience 를 테넌트 audience 와 함께 싣는다 (P50 9절)
    wanted = ["daegam-internal:default", f"daegam-internal:default:{inst['id']}"]
    assert claims["sub"] == "svc:platform" and claims["aud"] == wanted
    assert claims["exp"] - claims["iat"] <= 300


def _ready_remote(client, fake_clusters):
    """Ready 인 remote 항목 하나."""
    _bootstrap(client)
    item = client.post(HUB, json={"name": "calc-remote", "kind": "remote", "url": SPIKE_URL}).json()
    fake_clusters.by_name["edge"].set_status(
        GV,
        "MCPRemoteProxy",
        "hub-calc-remote",
        "daegam-default",
        {"phase": "Ready", "url": "http://proxy.daegam-default.svc.cluster.local:8080"},
    )
    return item


def test_access_saves_check_the_seq_the_server_read(client, fake_clusters, mongo):
    """그사이 다른 저장이 지나갔으면 409 revision_conflict, 삭제 중이면 409 deleting 이다."""
    item = _ready_remote(client, fake_clusters)
    svc = client.app.state.hub_service
    stale = client.portal.call(svc.store.get_unscoped, item["id"])
    assert client.put(f"{HUB}/{item['id']}/access", json={}).status_code == 200
    with pytest.raises(HubError) as e:
        admin = Principal(sub="u", memberships=[Membership(tenant="default", role="admin")])
        client.portal.call(svc.set_access, stale, stale.access, admin)
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    mongo["hub_items"].update_one({"_id": item["id"]}, {"$set": {"state": "deleting"}})
    r = client.put(f"{HUB}/{item['id']}/access", json={})
    assert detail_code(r) == (409, "deleting")


def test_a_refresh_or_apply_after_a_delete_started_leaves_deleting(client, fake_clusters, mongo):
    """삭제가 시작된 뒤 끝난 적용·새로 고침은 created 를 쓰지 않는다. deleting 에 남은 항목은 다시
    DELETE 하면 전이 없이 마저 지워진다 (P68 2절)."""
    item = _ready_remote(client, fake_clusters)
    svc = client.app.state.hub_service
    copy = client.portal.call(svc.store.get_unscoped, item["id"])

    async def lister(_client, url, bearer=None):
        mongo["hub_items"].update_one({"_id": item["id"]}, {"$set": {"state": "deleting"}})
        return ["Add"]

    svc.tool_lister = lister
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert detail_code(r) == (409, "deleting")
    client.portal.call(svc.apply, copy)
    assert mongo["hub_items"].find_one({"_id": item["id"]})["state"] == "deleting"
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert mongo["hub_items"].find_one({"_id": item["id"]}) is None


def test_every_hub_item_names_a_refused_read_the_same_way(client, fake_clusters):
    """읽기 거절은 종류와 상관없이 MCP Server·Gateway 상태와 같은 `cluster_forbidden: 권한` 이다."""
    mcp_server_env(client)
    r = client.post(HUB, json={"name": "spike", "kind": "remote", "url": SPIKE_URL})
    assert r.status_code == 201, r.text
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    fake.denied.add(("toolhive.stacklok.dev", "mcpremoteproxies", "get"))
    st = client.get(f"{HUB}/{r.json()['id']}").json()["status"]
    assert st["error"].startswith("cluster_forbidden: mcpremoteproxies:get")
