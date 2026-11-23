"""렌더 번호의 골든 테스트 (P50 1절).

종류마다 같은 문서를 렌더해 지문을 `GOLDEN` 과 견준다. 렌더가 바뀌었는데 `RENDER_EPOCHS` 의 그 종류
번호가 그대로면 살아 있는 문서가 새 렌더를 받지 못하므로 실패한다. 렌더를 바꾼 커밋은 그 종류의
번호를 올리고 `GOLDEN` 의 (번호, 지문)을 함께 고친다. 문서 id 는 처음 나온 순서의 자리표로 바꾸고,
토큰에서 나온 어노테이션과 클러스터가 정하는 uid(ownerReferences 의 것)는 지문에 넣지 않는다.

서버 문서(MCP Server, image 허브 항목)는 자기 audience 로 옮기기 전의 것을 렌더한다 (P50 9절,
`_before_switch`). 새로 만든 서버는 처음부터 옮겨 가 있지만, 살아 있는 문서가 옮겨 가는 것은 렌더
번호가 아니라 그 문서의 판(`own_audience_rev`)이 다시 렌더하기 때문이다.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest

from daegam.config.resolve import EffectiveConfig
from daegam.k8s.templates.agent import CONFIG_SHA_ANNOTATION
from daegam.k8s.templates.svc_token import SVC_TOKEN_REV_ANNOTATION
from daegam.services.data_plane_auth import (
    KIND_AGENT,
    KIND_CLONE,
    KIND_GATEWAY,
    KIND_HUB_ITEM,
    KIND_MCP_SERVER,
    RENDER_EPOCHS,
)
from daegam.services.hub import item_from_clone_source
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import BP, GW, HUB, MS
from tests.helpers.exposure import front_and_gateway
from tests.helpers.hub import SPIKE_URL, other_tenant_gateway, shared_env, shared_image_item
from tests.helpers.mcp_servers import two_backend_server

# 종류마다의 (렌더 번호, 지문). 렌더를 바꾼 커밋이 번호와 지문을 함께 고친다. agent 의 지문은 P78
# 에서 번호 없이 바뀌었다. 고정 문서는 이 테스트가 배포해 provision 이 남긴 턴 drain 결정을 갖지만,
# 살아 있는 문서는 다음 provision 전까지 결정이 없어 렌더가 그대로이기 때문이다 (P78 5절).
# 다섯 지문 모두 제품 이름을 바꿀 때(P80) 번호 없이 바뀌었다. 옛 설치의 문서는 새 설치로 넘어오지
# 않는다 (P80 2·3절)
GOLDEN: dict[str, tuple[int, str]] = {
    KIND_MCP_SERVER: (6, "52d8697d724aff82"),
    KIND_GATEWAY: (3, "433f0bb4c6397530"),
    KIND_AGENT: (5, "d88bdfb68dceff83"),
    KIND_HUB_ITEM: (3, "e2e9ebe0740774b3"),
    KIND_CLONE: (3, "81dc8cfebb3f71dd"),
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# 토큰에서 나와 렌더할 때마다 달라지는 어노테이션
_VOLATILE = frozenset({SVC_TOKEN_REV_ANNOTATION, CONFIG_SHA_ANNOTATION})


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "uid":
                continue
            if key == "annotations" and isinstance(item, dict):
                item = {k: v for k, v in item.items() if k not in _VOLATILE}
            out[key] = _strip(item)
        return out
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def fingerprint(docs: list[dict[str, Any]]) -> str:
    """렌더한 문서 묶음의 지문. id 는 처음 나온 순서의 자리표다."""
    text = json.dumps(_strip(docs), sort_keys=True)
    seen: dict[str, str] = {}
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<id{len(seen)}>"), text)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _before_switch(doc: Any) -> Any:
    """자기 audience 로 옮겨 가기 전의 서버 문서 (P50 9절)."""
    doc.audience_switch = None
    return doc


def _mcp_server(client, fake_clusters, _mongo) -> list[dict[str, Any]]:
    """노출한 MCP Server 의 테넌트 울타리 설정·MCPServer 와 노출 앞단, 그리고 그 정의를 backend
    둘로 고쳐 배포한 MCP Server 의 울타리 설정·MCPServer. 둘째 backend 는 basic 과 자격증명
    Secret 을 가져 backend 순번의 auth env 와 자격증명 자리가 지문에 들어간다."""
    portal, state = client.portal, client.app.state
    service = state.mcp_server_service
    front, _gateway, _fake = front_and_gateway(client, fake_clusters)
    server = _before_switch(portal.call(state.mcp_servers.get, front["id"]))
    effective = EffectiveConfig.model_validate(server.effective_config)
    docs = portal.call(service.render_docs, server)
    docs += portal.call(service._expose_docs, server, effective)
    definition = next(d for d in client.get(MS).json() if d["name"] == "calc-mcp")
    calc = client.get(BP).json()[0]
    combo, _pets = two_backend_server(
        client, definition, calc, auth="basic", credential_secret="pets-creds"
    )
    combo_doc = _before_switch(portal.call(state.mcp_servers.get, combo["id"]))
    return docs + portal.call(service.render_docs, combo_doc)


def _gateway(client, fake_clusters, _mongo) -> list[dict[str, Any]]:
    """그 MCP Server 를 백엔드로 쓰는 노출한 Gateway 의 그룹·Entry·vMCP 와 노출 객체."""
    portal, state = client.portal, client.app.state
    _front, gateway, _fake = front_and_gateway(client, fake_clusters)
    exposure = {"exposure": {"enabled": True, "host": "gw.e2e.local"}}
    r = client.patch(f"{GW}/{gateway['id']}", json=exposure)
    assert r.status_code == 200, r.text
    doc = portal.call(state.gateways.get, gateway["id"])
    effective = EffectiveConfig.model_validate(doc.effective_config)
    docs = portal.call(state.gateway_service.render_docs, doc)
    return docs + state.gateway_service._expose_docs(doc, effective)


def _agent(client, fake_clusters, mongo) -> list[dict[str, Any]]:
    """템플릿 그래프 Agent 의 Deployment."""
    portal, state = client.portal, client.app.state
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    doc = portal.call(state.agents.get, agent.doc["id"])
    ctx = portal.call(state.agent_service._context, doc)
    return [state.agent_service._render_deployment(doc, ctx)]


def _hub_item(client, _fake_clusters, _mongo) -> list[dict[str, Any]]:
    """CR 을 가진 허브 항목 둘(image·remote)의 CR."""
    portal, state = client.portal, client.app.state
    default_cluster(client)
    bodies = (
        {"name": "fetch", "kind": "image", "image": "ghcr.io/x/fetch:1", "port": 8000},
        {"name": "calc-remote", "kind": "remote", "url": SPIKE_URL},
    )
    docs: list[dict[str, Any]] = []
    for body in bodies:
        r = client.post(HUB, json=body)
        assert r.status_code == 201, r.text
        item = _before_switch(portal.call(state.hub_service.store.get_unscoped, r.json()["id"]))
        docs += portal.call(state.hub_service.render, item)
    return docs


def _clone(client, fake_clusters, mongo) -> list[dict[str, Any]]:
    """다른 테넌트가 쓰는 shared remote 항목과 image 항목의 복제본 CR. 이름에는 원본 id 의 앞자리가
    들어가 렌더마다 달라지므로 고정한 이름으로 렌더한다."""
    portal, state = client.portal, client.app.state
    _cid, _items, remote, fake = shared_env(client, fake_clusters)
    image = shared_image_item(client, fake)
    docs: list[dict[str, Any]] = []
    for i, shared in enumerate((remote, image)):
        _profile, r = other_tenant_gateway(client, shared, name=f"gw-{i}")
        assert r.status_code == 201, r.text
        record = mongo["shared_clones"].find_one({"source_item_id": shared["id"]})
        docs += portal.call(
            lambda record=record, i=i: state.hub_service.shared_clone_docs(
                item_from_clone_source(record["source"]),
                tenant=record["tenant"],
                namespace=record["namespace"],
                name=f"shared-golden-{i}",
            )
        )
    return docs


# 종류마다 고정 문서를 배포한 뒤 재적용이 다시 렌더하는 문서 묶음을 내는 함수
RENDERS = {
    KIND_MCP_SERVER: _mcp_server,
    KIND_GATEWAY: _gateway,
    KIND_AGENT: _agent,
    KIND_HUB_ITEM: _hub_item,
    KIND_CLONE: _clone,
}


def test_every_kind_has_a_golden_rendering():
    assert set(GOLDEN) == set(RENDERS) == set(RENDER_EPOCHS)


@pytest.mark.parametrize("kind", ["agent", "gateway", "mcp_server"])
def test_a_changed_rendering_raises_its_epoch(kind, client, fake_clusters, mongo):
    """렌더가 바뀌면 번호를 올린다. 번호를 올렸으면 지문도 새 렌더의 것으로 고친다."""
    epoch, digest = GOLDEN[kind]
    got = fingerprint(RENDERS[kind](client, fake_clusters, mongo))
    assert RENDER_EPOCHS[kind] == epoch, (
        f"RENDER_EPOCHS[{kind!r}] 가 {RENDER_EPOCHS[kind]} 이다. GOLDEN[{kind!r}] 을 "
        f"({RENDER_EPOCHS[kind]}, {got!r}) 로 고친다"
    )
    assert got == digest, (
        f"{kind} 의 렌더가 바뀌었다(지문 {got}). 살아 있는 문서에 반영하려면 "
        f"RENDER_EPOCHS[{kind!r}] 를 올리고 GOLDEN[{kind!r}] 을 (새 번호, {got!r}) 로 고친다"
    )
