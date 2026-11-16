"""도구 호출 타임아웃 (P50 7절, G6).

MCP Server 의 backend 는 호출 타임아웃을 갖는다. backend 가 하나면 런타임 env 로, 여럿이면
profile.json 항목으로 나르고, 런타임 기본값이면 env 를 내지 않는다. Gateway 의 백엔드는 vMCP 의
`operational.timeouts.perWorkload` 로 렌더된다. 배포가 기본값을 정하고(내부 항목은 그 서버의 가장 긴
backend 타임아웃에 5초, 나머지는 60초), 값이 없는 스냅샷은 고정값으로 렌더한다. Advanced 편집은 그
자리를 잠그고, 타임아웃을 보내지 않는 PUT 은 저장된 값을 그대로 둔다.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from daegam.services.gateway_timeouts import (
    AGENT_TOOL_TIMEOUT_SECONDS,
    default_timeout,
)
from daegam.store.base import TIMEOUT_MAX_SECONDS
from tests.helpers.consts import DEFAULT_NS, GV, MS
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

TIMEOUT_ENV = "SPEC2OPENAPI_TIMEOUT"


def _per_workload(cr: dict[str, Any]) -> dict[str, str] | None:
    return (cr["spec"]["config"]["operational"].get("timeouts") or {}).get("perWorkload")


def _backends(items, *pairs: tuple[str, int | None]) -> list[dict[str, Any]]:
    """(항목 이름, 타임아웃) 마다 Gateway 백엔드 하나."""
    out = []
    for name, seconds in pairs:
        b: dict[str, Any] = {"hub_item_id": items[name]["id"]}
        if seconds is not None:
            b["timeout_seconds"] = seconds
        out.append(b)
    return out


def _rendered_vmcp(client, gateway_id: str) -> dict[str, Any]:
    state = client.app.state
    doc = client.portal.call(state.gateways.get, gateway_id)
    return client.portal.call(state.gateway_service.render, doc)


# -- MCP Server --------------------------------------------------------------------------------


def test_a_single_backend_renders_its_timeout_only_when_it_is_not_the_default(
    client, fake_clusters
):
    """backend 하나의 타임아웃은 `SPEC2OPENAPI_TIMEOUT` 이고, 런타임 기본값(30초)이면 env 가 없어
    이 필드가 생기기 전과 같은 CR 이다."""
    _cid, bp, _ds, mp = mcp_server_env(client)
    fake = fake_clusters.by_name["edge"]
    first = clone_and_deploy(client, mp, name="calc-1").json()
    env = fake.get(GV, "MCPServer", first["k8s"]["name"], DEFAULT_NS)["spec"].get("env") or []
    assert TIMEOUT_ENV not in {e["name"] for e in env}

    backend = {"backend_profile_id": bp["id"], "spec_version": 1, "timeout_seconds": 90}
    r = client.put(f"{MS}/{mp['id']}", json={"name": mp["name"], "backends": [backend]})
    assert r.status_code == 200, r.text
    assert r.json()["backends"][0]["timeout_seconds"] == 90
    second = clone_and_deploy(client, r.json(), name="calc-2").json()
    env = fake.get(GV, "MCPServer", second["k8s"]["name"], DEFAULT_NS)["spec"]["env"]
    assert {"name": TIMEOUT_ENV, "value": "90"} in env


# -- Gateway -------------------------------------------------------------------------------------


def test_the_internal_default_stops_at_the_upper_bound():
    rev = SimpleNamespace(backends=[SimpleNamespace(timeout_seconds=TIMEOUT_MAX_SECONDS - 2)])
    server = SimpleNamespace(deployed_revision=3, get_revision=lambda n: rev, spec=None)
    assert default_timeout("internal", server) == TIMEOUT_MAX_SECONDS == 300
    assert default_timeout("internal") == 35 and default_timeout("remote") == 60


def test_the_agent_tool_timeout_default_matches_the_agent_runtime():
    """Gateway 의 기본값은 Agent 런타임의 기본 도구 타임아웃과 같아야 한다. 두 패키지는 서로를
    가져오지 못하므로 런타임의 소스를 읽어 맞댄다."""
    source = Path(__file__).parents[2] / "agent-runtime" / "runtime" / "mcp.py"
    found = re.search(r"^DEFAULT_TOOL_TIMEOUT_SECONDS = (\d+)$", source.read_text(), re.M)
    assert found is not None and int(found.group(1)) == AGENT_TOOL_TIMEOUT_SECONDS


# -- 순수 -----------------------------------------------------------------------------------------
