"""P52 F1: Tool 규칙의 저장 꼴과 저장 검증."""

from __future__ import annotations

import pytest

from daegam.pipeline.convert import tool_details_of
from daegam.pipeline.tool_arguments import arguments_of, tool_arguments
from daegam.services.tool_rules import (
    ToolRuleError,
    check_callers,
    check_cedar_size,
    check_condition,
    server_holds_rules,
    unknown_agents,
    validate_server_rules,
)
from daegam.store.backend_apis import BackendAPIVersion, ToolDetail
from daegam.store.mcp_servers import BackendSelection, MCPServerSpec, ProfileRuntimeSpec
from daegam.store.tool_rules import (
    MAX_RULE_CEDAR_CHARS,
    ArgCondition,
    ServerToolRule,
    ToolCallers,
    normalized_rules,
    opened_keys,
)

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/items/{id}": {
            "get": {
                "operationId": "getItem",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "postItem",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
                    }
                },
                "responses": {"200": {"description": "ok"}},
            },
        }
    },
    "components": {
        "schemas": {
            "Item": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "owner": {"$ref": "#/components/schemas/Owner"},
                },
            },
            "Owner": {
                "type": "object",
                "properties": {"email": {"type": "string", "nullable": True}},
            },
        }
    },
}


def _version(arguments: dict[str, dict[str, str] | None] | None = None) -> BackendAPIVersion:
    details = [
        ToolDetail(operation_id=op, arguments=args)
        for op, args in (arguments or {"getItem": {}, "postItem": {}}).items()
    ]
    return BackendAPIVersion(
        backend_profile_id="b1",
        version=1,
        kind="rest",
        source_format="openapi3",
        source_ext="yaml",
        source_sha256="",
        output_sha256="",
        s2o_version="",
        tool_count=len(details),
        tool_names=[d.operation_id for d in details],
        tool_details=details,
    )


def _spec(rules: dict[str, ServerToolRule], *, tools=(), image=None) -> MCPServerSpec:
    return MCPServerSpec(
        backends=[
            BackendSelection(
                backend_profile_id="b1", spec_version=1, tools=list(tools), tool_rules=rules
            )
        ],
        runtime=ProfileRuntimeSpec(image=image),
    )


def test_fastmcp_arguments_carry_its_names_and_stop_at_arrays():
    """FastMCP 가 겹친 경로 인자에 붙인 이름(`id__path`)과 객체 안의 경로가 그대로이고, 배열은 그
    자리까지만 있다. null 을 함께 받는 형식은 그 형식이다."""
    args = tool_arguments(SPEC)
    assert args[("/items/{id}", "GET")] == {"id": "string", "limit": "integer"}
    assert args[("/items/{id}", "POST")] == {
        "id__path": "string",
        "id": "integer",
        "name": "string",
        "tags": "array",
        "owner": "object",
        "owner.email": "string",
    }
    details = {d["operation_id"]: d["arguments"] for d in tool_details_of(SPEC)}
    assert details["postItem"]["owner.email"] == "string"


def test_a_spec_fastmcp_cannot_read_stores_no_arguments():
    assert tool_arguments({"openapi": "3.0.3", "paths": "nope"}) is None


def test_ambiguous_branches_are_unknown():
    schema = {
        "type": "object",
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "b": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    assert arguments_of(schema) == {"a": "unknown", "b": "string"}


POST_ARGS = {
    "id__path": "string",
    "id": "integer",
    "name": "string",
    "tags": "array",
    "owner": "object",
    "owner.email": "string",
}


@pytest.mark.parametrize(
    ("cond", "code"),
    [
        (ArgCondition(path="nope", op="equals", value="x"), "rule_unknown_argument"),
        (ArgCondition(path="tags.0", op="equals", value="x"), "rule_array_path"),
        (ArgCondition(path="tags", op="one_of", value=["x"]), "rule_array_path"),
        (ArgCondition(path="owner", op="equals", value="x"), "rule_argument_type"),
        (ArgCondition(path="name", op="min", value=1), "rule_argument_type"),
        (ArgCondition(path="id", op="max_length", value=3), "rule_argument_type"),
        (ArgCondition(path="id", op="equals", value="1"), "rule_argument_type"),
        (ArgCondition(path="id", op="equals", value=True), "rule_argument_type"),
        (ArgCondition(path="name", op="one_of", value=[]), "rule_argument_type"),
        (ArgCondition(path="name", op="max_length", value=-1), "rule_argument_type"),
    ],
)
def test_conditions_that_do_not_resolve_or_fit_are_refused(cond, code):
    with pytest.raises(ToolRuleError) as e:
        check_condition(cond, POST_ARGS)
    assert (e.value.status, e.value.code) == (422, code)


@pytest.mark.parametrize(
    "cond",
    [
        ArgCondition(path="owner.email", op="prefix", value="ops@"),
        ArgCondition(path="id", op="min", value=1),
        ArgCondition(path="id", op="max", value=2.5),
        ArgCondition(path="name", op="one_of", value=["a", "b"]),
        ArgCondition(path="name", op="max_length", value=10, optional=True),
        ArgCondition(path="id__path", op="equals", value="x"),
    ],
)
def test_conditions_that_resolve_pass(cond):
    check_condition(cond, POST_ARGS)


def test_a_version_without_arguments_takes_no_conditions():
    with pytest.raises(ToolRuleError) as e:
        check_condition(ArgCondition(path="id", op="equals", value=1), None)
    assert e.value.code == "rule_unknown_argument"


def test_server_rule_keys_must_be_selected_tools():
    rule = ServerToolRule(callers=ToolCallers(roles=["developer"]))
    validate_server_rules(_spec({"getItem": rule}), [_version()], oidc=False)
    with pytest.raises(ToolRuleError) as e:
        validate_server_rules(
            _spec({"postItem": rule}, tools=["getItem"]), [_version()], oidc=False
        )
    assert e.value.code == "rule_names_unknown_tool" and e.value.extra["unknown"] == ["postItem"]


def test_a_custom_image_cannot_hold_conditions_but_can_hold_callers():
    callers = ServerToolRule(callers=ToolCallers(roles=["viewer"]))
    validate_server_rules(_spec({"getItem": callers}, image="me/x:1"), [_version()], oidc=False)
    cond = ServerToolRule(conditions=[ArgCondition(path="limit", op="max", value=10)])
    version = _version({"getItem": {"limit": "integer"}})
    validate_server_rules(_spec({"getItem": cond}), [version], oidc=False)
    with pytest.raises(ToolRuleError) as e:
        validate_server_rules(_spec({"getItem": cond}, image="me/x:1"), [version], oidc=False)
    assert e.value.code == "rule_custom_image"


def test_groups_only_in_oidc_mode_and_never_the_role_prefix():
    callers = ToolCallers(groups=["eng"])
    with pytest.raises(ToolRuleError):
        check_callers(callers, oidc=False, where="t")
    check_callers(callers, oidc=True, where="t")
    with pytest.raises(ToolRuleError):
        check_callers(ToolCallers(groups=["daegam-role:acme:developer"]), oidc=True, where="t")
    with pytest.raises(ToolRuleError):
        check_callers(ToolCallers(agents=["a1"]), oidc=True, where="t", roles_only=True)


def test_only_added_agent_ids_must_exist():
    """지운 Agent 의 id 가 이미 저장돼 있으면 그대로 둔다. 이 저장이 더한 id 만 본다."""
    before = [ServerToolRule(callers=ToolCallers(agents=["gone"]))]
    keeps = [ServerToolRule(callers=ToolCallers(agents=["gone"]))]
    assert unknown_agents(keeps, before, existing=[]) == []
    adds = [ServerToolRule(callers=ToolCallers(agents=["gone", "a2", "a3"]))]
    assert unknown_agents(adds, before, existing=["a2"]) == ["a3"]


def test_normalized_rules_drop_blank_rules_and_sort_callers():
    rules = normalized_rules(
        {
            "b": ServerToolRule(),
            "a": ServerToolRule(
                callers=ToolCallers(roles=["tenant-admin", "viewer", "viewer"], agents=["z", "y"])
            ),
            "c": ServerToolRule(read_only=True),
        }
    )
    assert list(rules) == ["a", "c"]
    assert rules["a"].callers.roles == ["viewer", "tenant-admin"]
    assert rules["a"].callers.agents == ["y", "z"]


def test_emptying_callers_opens_the_tool():
    before = {
        "a": ServerToolRule(callers=ToolCallers(roles=["developer"])),
        "b": ServerToolRule(callers=ToolCallers(roles=["developer"])),
        "c": ServerToolRule(read_only=True),
    }
    after = {"b": ServerToolRule(callers=ToolCallers(roles=["viewer"])), "c": ServerToolRule()}
    assert opened_keys(before, after) == ["a"]


def test_read_only_marks_alone_are_not_rules():
    assert not server_holds_rules(_spec({"getItem": ServerToolRule(read_only=True)}))
    assert server_holds_rules(
        _spec({"getItem": ServerToolRule(conditions=[ArgCondition(path="x", op="equals")])})
    )


def test_a_spec_without_rules_dumps_as_before():
    """규칙이 없는 리비전은 이 칸이 생기기 전과 같은 문서다."""
    assert "tool_rules" not in _spec({}).model_dump()["backends"][0]


def test_condition_values_and_compiled_cedar_are_capped():
    long = "x" * 257
    for cond in (
        ArgCondition(path="name", op="equals", value=long),
        ArgCondition(path="name", op="prefix", value=long),
        ArgCondition(path="name", op="one_of", value=["a", long]),
    ):
        with pytest.raises(ToolRuleError) as e:
            check_condition(cond, POST_ARGS)
        assert e.value.code == "invalid_value"
    check_cedar_size(["x" * MAX_RULE_CEDAR_CHARS])
    with pytest.raises(ToolRuleError) as e:
        check_cedar_size(["x" * MAX_RULE_CEDAR_CHARS, "y"], object="vMCP")
    assert (e.value.status, e.value.code) == (422, "rules_too_large")
    assert e.value.extra["object"] == "vMCP"
