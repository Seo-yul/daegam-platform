"""P50 B2 의 Gateway 갭 (P50 2·4·6·13절).

- G2: 게이트를 지난 클러스터의 vMCP 는 감사 이벤트 셋을 남기고, 지나지 못한 클러스터는 감사 없이
  돌며 그 수를 상태 API 가 센다. MCP 감사 행은 vMCP 가 부른 백엔드(Entry)를 싣는다.
- G4: 이름을 바꾼 툴은 필터에 원래 이름과 새 이름이 함께 들고, 새 이름이 그 백엔드의 툴 이름이나
  이미 광고되는 이름과 겹치면 수를 붙인다. 다시 렌더하기가 배포 스냅샷으로 이름 계획을 다시 세운다.
- G7: 새 Gateway 는 resources·prompts 를 끈 채로 시작하고, 끈 쪽은 Cedar forbid 로 막힌다. 표면을
  정하기 전 리비전은 둘 다 열고 상태에 경고를 단다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from daegam.services.advertised_names import plan_conflicts as plan
from daegam.services.data_plane_auth import KIND_GATEWAY
from daegam.store.gateways import GatewayBackendSnapshot
from tests.helpers.clusters import downgrade_toolhive
from tests.helpers.consts import HUB
from tests.helpers.gateways import (
    gateway_up,
    vmcp_of,
)
from tests.helpers.hub import hub_env
from tests.helpers.workers import raise_epoch, run_data_plane_auth

AUDIT = {"enabled": True, "eventTypes": ["mcp_tool_call", "mcp_resource_read", "mcp_prompt_get"]}


def _clashing(client, fake_clusters, spike_tools: list[str]):
    """calculator 와 spike 가 Add 를 함께 내는 허브 환경. spike 의 툴 이름은 spike_tools 다."""
    _cid, items, _calc, _orders, remote, fake = hub_env(client, fake_clusters)

    async def lister(_client, url, bearer=None):
        return spike_tools

    client.app.state.hub_service.tool_lister = lister
    assert client.post(f"{HUB}/{remote['id']}/refresh-tools").status_code == 200
    backends = [
        {"hub_item_id": items["calculator"]["id"], "tools": ["Add"]},
        {"hub_item_id": remote["id"], "tools": ["Add"]},
    ]
    return backends, fake


# -- G2 감사 --------------------------------------------------------------------------------


def test_the_vmcp_writes_audit_only_where_the_cluster_passes_the_gate(
    client, fake_clusters, mongo, monkeypatch
):
    """오퍼레이터가 0.51.2 인 클러스터의 vMCP 는 감사 이벤트 셋을 남긴다. 판이 모자란 클러스터에서는
    감사 없이 렌더하고, 상태 API 가 그 클러스터에서 감사와 타임아웃을 빼고 도는 Gateway 를 센다."""
    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    gateway = gateway_up(client, items)
    assert vmcp_of(fake, "gw-a")["spec"]["config"]["audit"] == AUDIT
    cluster_id = gateway["cluster_id"]
    assert client.get("/settings/data-plane-auth").json()["fallbacks"] == {}

    downgrade_toolhive(mongo, cluster_id)
    # 판정이 바뀌었으므로 렌더 번호가 달라져 다시 렌더된다 (`render_epoch`)
    assert gateway["id"] in run_data_plane_auth(client)["reapplied"]
    assert "audit" not in vmcp_of(fake, "gw-a")["spec"]["config"]
    # Cedar 는 인라인, 나가는 인증은 발견으로 오늘의 꼴이다 (P50 2절 표의 G8·G5)
    spec = vmcp_of(fake, "gw-a")["spec"]
    assert "authzConfig" in spec["incomingAuth"] and "authzConfigRef" not in spec["incomingAuth"]
    assert spec["outgoingAuth"] == {"source": "discovered"}
    fallbacks = client.get("/settings/data-plane-auth").json()["fallbacks"][cluster_id]
    # 이 Gateway 는 감사·타임아웃·정적 백엔드·Cedar 참조를 뺀 꼴로 돌고, 허브의 remote 항목은 역할
    # 없는 ServiceAccount 없이 돈다 (P50 2절 표)
    gateway_features = ("audit", "timeouts", "static_backends", "authz_ref")
    assert {f: fallbacks[f] for f in gateway_features} == dict.fromkeys(gateway_features, 1)
    assert fallbacks["roleless_sa"] == 1


def test_mcp_audit_rows_name_the_backend_a_gateway_called(client, mongo):
    """vMCP 감사 이벤트의 `metadata.extra.backend_name`(Entry 이름)이 행의 backend 로 나온다. 다른
    프록시의 이벤트에는 없다."""
    ts = datetime(2026, 10, 14, tzinfo=UTC)
    common = {"ts": ts, "tenant": "default", "type": "mcp_tool_call", "tool": "a_add"}
    mongo["mcp_audit_events"].insert_many(
        [
            {
                **common,
                "_id": "via-gateway",
                "metadata": {"extra": {"backend_name": "a-calculator", "duration_ms": 3}},
            },
            {**common, "_id": "direct", "metadata": {"extra": {"duration_ms": 3}}},
        ]
    )
    rows = {r["audit_id"]: r for r in client.get("/audit/mcp").json()}
    assert rows["via-gateway"]["backend"] == "a-calculator"
    assert rows["direct"]["backend"] is None


# -- G4 이름 바꾼 툴 ---------------------------------------------------------------------------


def _snapshot(item: str, tool_names: list[str], tools: list[str]) -> GatewayBackendSnapshot:
    return GatewayBackendSnapshot(
        hub_item_id=item,
        hub_item_name=item,
        kind="remote",
        workload=f"gw-{item}",
        url="http://x",
        tools=tools,
        tool_names=tool_names,
    )


def test_a_rename_target_that_is_taken_gets_a_number():
    """새 이름이 그 백엔드의 다른 툴 이름(고르지 않은 것까지)이거나 이미 광고되는 이름이면 `_2` 부터
    수를 붙인다. 겹치지 않으면 `<항목>_<툴>` 그대로다."""
    calc = _snapshot("calc", ["add", "calc_add"], ["add"])
    math = _snapshot("math", ["add"], [])
    native = _snapshot("other", ["math_add"], [])
    mode, overrides, warnings = plan([calc, math, native], "priority")
    assert mode == "priority" and warnings == []
    assert overrides == {"gw-calc": {"add": "calc_add_2"}, "gw-math": {"add": "math_add_2"}}
    _mode, plain, _warnings = plan([_snapshot("calc", ["add"], []), math], "priority")
    assert plain == {"gw-calc": {"add": "calc_add"}, "gw-math": {"add": "math_add"}}


def test_a_rerender_replans_tool_names_from_the_deployed_snapshot(
    client, fake_clusters, mongo, monkeypatch
):
    """필터가 있는 백엔드의 이름을 바꾼 툴은 필터에 원래 이름과 새 이름이 함께 들어, vMCP 가 바꾼
    뒤의 이름으로 맞춰 봐도 빠지지 않는다. 앞서 세운 이름 계획(새 이름이 그 백엔드의 툴과
    겹친다)은 다시 렌더하기가 배포 스냅샷으로 다시 세워 저장한다."""
    backends, fake = _clashing(client, fake_clusters, ["Add", "spike_Add"])
    inst = gateway_up(client, None, name="x", backends=backends)
    assert inst["tool_overrides"] == {
        "x-calculator": {"Add": "calculator_Add"},
        "x-spike": {"Add": "spike_Add_2"},
    }
    calc = vmcp_of(fake, "gw-x")["spec"]["config"]["aggregation"]["tools"][0]
    assert calc == {
        "workload": "x-calculator",
        "filter": ["Add", "calculator_Add"],
        "overrides": {"Add": {"name": "calculator_Add"}},
    }
    old = {"x-calculator": {"Add": "calculator_Add"}, "x-spike": {"Add": "spike_Add"}}
    mongo["gateways"].update_one({"_id": inst["id"]}, {"$set": {"tool_overrides": old}})
    raise_epoch(monkeypatch, KIND_GATEWAY)
    assert run_data_plane_auth(client)["reapplied"] == [inst["id"]]
    stored = mongo["gateways"].find_one({"_id": inst["id"]})
    assert stored["tool_overrides"]["x-spike"] == {"Add": "spike_Add_2"}
    spike = vmcp_of(fake, "gw-x")["spec"]["config"]["aggregation"]["tools"][1]
    assert spike["filter"] == ["Add", "spike_Add_2"]


# -- G7 표면 ------------------------------------------------------------------------------
