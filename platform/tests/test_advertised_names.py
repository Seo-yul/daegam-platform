"""노출 이름 도우미 (P39b F8). 옮기기 전의 `plan_conflicts`·`advertised_tools`·`advertised_tools_of`
가 내던 값을 그대로 내는지 표로 본다. 표의 기대값은 옮기기 전 함수로 계산한 것이다."""

from __future__ import annotations

from daegam.services.advertised_names import (
    ExposedTool,
    advertised_tools,
    advertised_tools_of,
    gateway_tools,
    plan_conflicts,
    server_tools,
)
from daegam.store.gateways import Gateway, GatewayBackendSnapshot, GatewaySpec
from daegam.store.mcp_servers import BackendSelection, MCPServerRevision, ToolOverride
from tests.helpers.backend_apis import backend_version
from tests.helpers.gateways import backend_snapshot


def _snap(item: str, names: list[str], tools: list[str] | None = None) -> GatewayBackendSnapshot:
    return backend_snapshot(item, "remote", names, tools=tools or [])


def _rev(*backends: BackendSelection) -> MCPServerRevision:
    return MCPServerRevision(revision=1, backends=list(backends))


def _gateway(backends, resolution, overrides) -> Gateway:
    return Gateway(
        tenant="t",
        name="g",
        display_name="g",
        spec=GatewaySpec(backends=[]),
        deployed_backends=backends,
        deployed_conflict_resolution=resolution,
        tool_overrides=overrides,
    )


def test_server_names_follow_the_filter_and_the_overrides():
    versions = {("calc", 1): backend_version(["Add", "Divide", "Sub"], bp="calc", version=1)}
    rev = _rev(
        BackendSelection(
            backend_profile_id="calc",
            spec_version=1,
            tools=["Add", "Divide"],
            tool_overrides={"Add": ToolOverride(name="plus")},
        )
    )
    assert server_tools(rev, versions) == [
        ExposedTool("calc", "Add", "plus"),
        ExposedTool("calc", "Divide", "Divide"),
    ]
    assert advertised_tools_of(rev, versions) == ["plus", "Divide"]


def test_an_unfiltered_backend_exposes_its_version_and_a_missing_version_nothing():
    versions = {("calc", 2): backend_version(["Add", "Sub"], bp="calc", version=2)}
    rev = _rev(
        BackendSelection(backend_profile_id="calc", spec_version=2),
        BackendSelection(backend_profile_id="gone", spec_version=1),
    )
    assert advertised_tools_of(rev, versions) == ["Add", "Sub"]


def test_an_empty_override_name_keeps_the_operation_id():
    versions = {("calc", 1): backend_version(["Add"], bp="calc", version=1)}
    rev = _rev(
        BackendSelection(
            backend_profile_id="calc",
            spec_version=1,
            tool_overrides={"Add": ToolOverride(description="d")},
        )
    )
    assert advertised_tools_of(rev, versions) == ["Add"]


def test_priority_renames_only_the_clashing_tools():
    a = _snap("calculator", ["Add", "Div"])
    b = _snap("spike", ["Add", "Echo"])
    assert plan_conflicts([a, b]) == (
        "priority",
        {"gw-calculator": {"Add": "calculator_Add"}, "gw-spike": {"Add": "spike_Add"}},
        [],
    )


def test_a_rename_skips_names_the_backend_already_has():
    a = _snap("calculator", ["Add", "calculator_Add"], tools=["Add"])
    b = _snap("spike", ["Add"])
    mode, overrides, _ = plan_conflicts([a, b])
    assert mode == "priority"
    assert overrides["gw-calculator"] == {"Add": "calculator_Add_2"}


def test_auto_falls_back_to_prefix_when_a_name_list_is_unknown():
    mode, overrides, warnings = plan_conflicts([_snap("a", ["X"]), _snap("b", [])])
    assert (mode, overrides) == ("prefix", {})
    assert len(warnings) == 1


def test_gateway_names_in_priority_and_prefix_mode():
    a = _snap("calculator", ["Add", "Div"], tools=["Add"])
    b = _snap("spike", ["Add"])
    prio = _gateway([a, b], "priority", {"gw-calculator": {"Add": "calculator_Add"}})
    assert advertised_tools(prio) == ["calculator_Add", "Add"]
    assert gateway_tools([a, b], "priority", {"gw-calculator": {"Add": "calculator_Add"}}) == [
        ExposedTool("gw-calculator", "Add", "calculator_Add"),
        ExposedTool("gw-spike", "Add", "Add"),
    ]
    prefix = _gateway([a, b], "prefix", {})
    assert advertised_tools(prefix) == ["gw-calculator_Add", "gw-spike_Add"]


def test_unknown_names_advertise_the_filter_and_duplicates_once():
    a = _snap("x", [], tools=["Echo"])
    b = _snap("y", ["Echo"])
    doc = _gateway([a, b], "priority", {})
    assert advertised_tools(doc) == ["Echo"]
    assert [t.backend for t in gateway_tools([a, b], "priority", {})] == ["gw-x", "gw-y"]
