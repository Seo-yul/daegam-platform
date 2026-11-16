"""P14 F1a: 비서 도구 표와 OpenAPI 스키마 추출.

표 밖 동작은 전부 제외 목록에 이유와 함께 있어야 하고, 도구 이름은 provider 규칙을 따르며,
직렬화한 도구 정의 전체는 48KB 를 넘지 않는다(P14 의 40KB 에 P15 Knowledge Base 도구 열이
더해졌다). 응답·손 스키마에는 비밀 이름의 필드가 없다(정확 일치).
"""

from __future__ import annotations

import pytest

from daegam.services.agent_changes import PATCH_FIELDS
from daegam.services.assistant.catalog import (
    CATALOG,
    CATALOG_NAME_RE,
    CORE_GROUP,
    OTHER_GROUP,
    RESOURCE_ROUTES,
    TOOL_GROUPS,
    Catalog,
    CatalogError,
    ToolSpec,
    group_of,
)
from daegam.services.assistant.schema import (
    DESCRIPTION_DEPTH,
    ToolDef,
    build_tool_schemas,
    clean,
    resolve_refs,
    serialized_size,
)

# P14 는 40KB 였다. P15(D32) 가 Knowledge Base 도구 열을 더해 44KB 남짓이 됐으므로 상한을 48KB 로
# 올렸고, P39(D64) 가 Skill 읽기 둘과 설정의 skills 절을 더해 48KB 를 넘었으므로 52KB 로 올린다.
# 도구 하나가 평균 500 바이트라 이 상한이 표의 크기를 계속 잡는다. 비서 턴마다의 토큰 값을 묶는
# 상한이라(D56·D57) 올리지 않는다. P39b 의 웹 Tool·네트워크 절과 P38 의 게이트웨이 한도 절은 설정
# 도구에서 뺀다(`hidden_fields`). P38 의 게이트웨이 상태 읽기 하나는 배포 도구 셋이 모델에게만 주는
# 같은 안내를 줄여 자리를 냈다. 승인 카드에 보이는 첫 문장은 그대로다
MAX_TOOL_BYTES = 52 * 1024
# 값이 실리면 안 되는 필드 이름. 부분 일치는 token_id·has_password 같은 이름에 걸리므로 정확 일치다
SECRET_NAMES = {"token", "password", "kubeconfig", "sa_token", "api_key"}


@pytest.fixture(scope="module")
def defs(openapi_spec) -> dict[str, ToolDef]:
    return {d.name: d for d in build_tool_schemas(openapi_spec, CATALOG)}


def _operations(spec) -> set[tuple[str, str]]:
    return {(m.upper(), path) for path, item in spec["paths"].items() for m in item}


# -- 표와 제외 목록 --------------------------------------------------------------------------


def test_every_operation_is_a_tool_or_excluded_exactly_once(openapi_spec):
    tools = {t.key for t in CATALOG.api_tools}
    problems = []
    for method, path in sorted(_operations(openapi_spec)):
        in_table = (method, path) in tools
        excluded = CATALOG.exclusion_reason(method, path) is not None
        if in_table == excluded:
            problems.append(
                f"{method} {path}: {'표와 제외 목록 양쪽' if in_table else '어디에도 없다'}"
            )
    assert not problems, "\n".join(problems)
    # 제외 이유는 비어 있지 않다
    assert all(reason.strip() for reason in CATALOG.excluded.values())


def test_excluded_operations_still_exist(openapi_spec):
    """API 에서 사라진 동작이 제외 목록에 남아 있으면 표가 낡은 것이다."""
    ops = _operations(openapi_spec)
    stale = [k for k in CATALOG.excluded if k not in ops]
    assert stale == []


def test_spec_exclusions_are_present():
    """스펙의 제외 표 일곱 줄이 그대로 있다."""
    reasons = CATALOG.excluded
    assert ("PUT", "/api/mcp-servers/{server_id}/overrides") in reasons
    assert ("PUT", "/api/gateways/{gateway_id}/overrides") in reasons
    assert ("PUT", "/api/agents/{agent_id}/overrides") in reasons
    assert ("POST", "/api/clusters") in reasons and (
        "PATCH",
        "/api/clusters/{cluster_id}",
    ) in reasons
    assert ("PUT", "/api/tenants/{tenant}/members") in reasons
    assert CATALOG.exclusion_reason("POST", "/api/assistant/sessions") is not None
    # 개인 MCP 토큰은 평문 토큰과 비밀번호가 모델 문맥에 남으므로 도구가 아니다 (P32 6절)
    for method, path in (
        ("GET", "/api/me/tokens"),
        ("POST", "/api/me/tokens"),
        ("DELETE", "/api/me/tokens/{token_id}"),
        ("DELETE", "/api/users/{user_id}/tokens/{token_id}"),
        # 비서가 동의를 대신 누르면 프롬프트 주입 한 번으로 연결이 생긴다 (P33 1절)
        ("POST", "/api/oauth/consent/inspect"),
        ("POST", "/api/oauth/consent/approve"),
        ("POST", "/api/oauth/consent/deny"),
        # 연결 끊기는 사람이 화면에서 한다 (P33 1절)
        ("GET", "/api/me/oauth-grants"),
        ("DELETE", "/api/me/oauth-grants/{grant_id}"),
        ("DELETE", "/api/users/{user_id}/oauth-grants/{grant_id}"),
    ):
        assert CATALOG.exclusion_reason(method, path) is not None, path
    assert ("POST", "/api/agents/{agent_id}/chat") in reasons
    # 중지도 채팅처럼 데이터 플레인이라 도구가 아니다(D65)
    assert ("POST", "/api/agents/{agent_id}/sessions/{session_id}/cancel") in reasons
    # 사람의 대화를 지우거나 이름을 바꾸는 것도 도구로 열지 않는다(P36)
    assert ("PATCH", "/api/agents/{agent_id}/sessions/{session_id}") in reasons
    assert ("DELETE", "/api/agents/{agent_id}/sessions/{session_id}") in reasons
    assert ("POST", "/api/agents/{agent_id}/sessions/{session_id}/regenerate") in reasons
    assert ("POST", "/api/backend-apis/{api_id}/tokens") in reasons
    assert ("POST", "/api/spec-generator/jobs") in reasons


def test_tool_names_follow_the_provider_rule():
    names = [t.name for t in CATALOG.tools]
    assert len(names) == len(set(names))
    assert all(CATALOG_NAME_RE.match(n) for n in names), names
    for bad in ("Create", "1st", "a.b", "a-b", "x" * 65):
        with pytest.raises(CatalogError, match="이름"):
            Catalog(tools=(ToolSpec(bad, "GET", "/api/me", "read", "x"),), excluded={})


def test_catalog_rejects_a_group_on_an_api_tool():
    """group 은 클라이언트 전용 도구의 묶음이다. API 도구는 경로 앞머리가 묶음을 정한다."""
    spec = ToolSpec("get_thing", "GET", "/api/things", "read", "x", group="agent")
    with pytest.raises(CatalogError, match="group"):
        Catalog(tools=(spec,), excluded={})


def test_catalog_rejects_duplicates_and_overlap_with_exclusions():
    row = ToolSpec("get_me", "GET", "/api/me", "read", "x")
    with pytest.raises(CatalogError, match="두 번"):
        Catalog(tools=(row, ToolSpec("get_me2", "GET", "/api/me", "read", "x")), excluded={})
    with pytest.raises(CatalogError, match="같이"):
        Catalog(tools=(row,), excluded={("GET", "/api/me"): "이유"})
    with pytest.raises(CatalogError, match="resource_kind"):
        Catalog(
            tools=(ToolSpec("t", "GET", "/api/me", "read", "x", resource_kind="nope"),), excluded={}
        )


def test_every_real_tool_falls_into_a_named_group():
    """도구 묶음 (D57). 실제 표의 도구는 모두 이름 있는 묶음에 든다. `other` 는 늘 실리므로
    새 경로의 도구가 거기 떨어지면 묶음을 나눈 뜻이 사라진다."""
    groups = {t.name: group_of(t) for t in CATALOG.tools}
    assert OTHER_GROUP not in groups.values()
    assert {g for g in groups.values()} == {CORE_GROUP, *TOOL_GROUPS}
    assert {n for n, g in groups.items() if g == CORE_GROUP} == {
        "navigate",
        "read_guide",
        "ask_user",
        "fetch_url",
        "use_tools",
    }
    assert groups["verify_spec"] == "backend" and groups["list_clusters"] == "cluster"
    assert groups["publish_mcp_server"] == "mcp" and groups["publish_gateway"] == "gateway"
    assert groups["list_my_tenants"] == "settings" and groups["check_llm_provider"] == "settings"
    # use_tools 의 인자는 묶음 이름만 받는다
    use = CATALOG.get("use_tools")
    assert use is not None and use.args_schema is not None
    assert use.args_schema["properties"]["groups"]["items"]["enum"] == list(TOOL_GROUPS)


def test_local_tools_and_risk_table():
    local = {t.name for t in CATALOG.local_tools}
    assert local == {
        "navigate",
        "read_guide",
        "ask_user",
        "fetch_url",
        "use_tools",
        "get_agent_definition",
    }
    # 정의 읽기는 플랫폼을 읽는 클라이언트 전용 도구라 기본 묶음이 아니라 agent 묶음이다 (P48 6.2절)
    assert group_of(CATALOG.get("get_agent_definition")) == "agent"
    assert {group_of(t) for t in CATALOG.local_tools if t.group is None} == {CORE_GROUP}
    assert all(t.method is None and t.path is None and t.args_schema for t in CATALOG.local_tools)
    risk = {t.name: t.risk for t in CATALOG.tools}
    assert risk["list_mcp_servers"] == "read" and risk["verify_spec"] == "read"
    assert risk["preflight_cluster"] == "read" and risk["navigate"] == "read"
    assert risk["create_backend_api"] == "write" and risk["create_data_service"] == "write"
    assert risk["deploy_mcp_server"] == "deploy" and risk["deploy_agent"] == "deploy"
    assert risk["publish_mcp_server"] == "deploy" and risk["redeploy_mcp_server"] == "deploy"
    assert risk["undeploy_mcp_server"] == "deploy" and risk["clone_mcp_server"] == "write"
    assert risk["create_mcp_server"] == "write" and risk["delete_mcp_server"] == "destructive"
    assert risk["update_platform_settings"] == "destructive"
    assert risk["revoke_backend_token"] == "destructive"
    assert all(t.resource_kind in RESOURCE_ROUTES for t in CATALOG.tools if t.resource_kind)
    assert CATALOG.get("create_data_service").secret_fields == ("/password",)
    assert CATALOG.get("update_data_service").secret_fields == ("/password",)


# -- 스키마 추출 ---------------------------------------------------------------------------


def test_serialized_size_is_bounded(defs):
    size = serialized_size(list(defs.values()))
    assert size <= MAX_TOOL_BYTES, f"도구 정의가 {size} 바이트다 (상한 {MAX_TOOL_BYTES})"
    assert len(defs) == len(CATALOG.tools)
    # bind_tools 가 받는 모양이고 parameters 는 언제나 object 스키마다
    for d in defs.values():
        assert set(d.to_dict()) == {"name", "description", "parameters"}
        assert d.parameters["type"] == "object", d.name


def test_path_query_and_body_are_split(defs):
    # 상세는 path 하나
    d = defs["get_mcp_server"]
    assert d.path_params == ("server_id",) and d.query_params == () and d.body_params == {}
    assert d.parameters["required"] == ["server_id"] and d.body_media is None
    # path + query
    d = defs["cluster_exposure_options"]
    assert d.path_params == ("cluster_id",) and d.query_params == ("namespace",)
    assert d.parameters["required"] == ["cluster_id"]
    assert set(d.parameters["properties"]) == {"cluster_id", "namespace"}
    d = defs["delete_backend_api"]
    assert d.query_params == ("force",) and d.parameters["properties"]["force"]["type"] == "boolean"
    # 본문은 평평하게. requestBody 의 required 가 그대로 온다
    d = defs["create_mcp_server"]
    assert d.body_media == "application/json" and d.path_params == ()
    assert {"name", "backends", "backend_profile_id", "runtime", "bindings"} <= set(d.body_params)
    assert set(d.parameters["required"]) == {"name"}
    # path + 본문 (배포는 정의를 가리키고 클러스터 쪽만 받는다)
    d = defs["deploy_mcp_server"]
    assert d.body_media == "application/json" and d.path_params == ("server_id",)
    assert {"cluster_id", "namespace", "exposure"} <= set(d.body_params)
    # 손 스키마(raw 본문). verify_spec 은 스펙 원문을 인자로 받는 read 도구다
    d = defs["verify_spec"]
    assert d.body_params == {"text": "text", "filename": "filename"}
    assert d.parameters["required"] == ["text"]
    # raw 본문이 없는 동작
    d = defs["redeploy_mcp_server"]
    assert d.body_media is None and d.path_params == ("server_id",)
    # 본문이 `RedeployRequest | null` 이라도 object 로 접힌다
    d = defs["redeploy_agent"]
    assert d.body_media == "application/json"
    assert set(d.body_params) == {"replicas", "resources", "exposure", "scale_to_zero"}
    # 비서의 배포 도구는 배포 게이트를 건너뛰지 못한다 (P45 12절)
    for name in ("deploy_agent", "redeploy_agent"):
        assert "skip_evaluation" not in defs[name].body_params, name
        assert "skip_evaluation" not in defs[name].parameters["properties"], name
    # 비서의 Agent 정의는 제안이다 (P48 6.1·6.2절). 바꾸기는 손으로 적은 패치 인자이고 그 이름은
    # Agent 본문의 칸과 겹치지 않는다. 기준 리비전과 배치는 모델이 정하지 않는다(6.7절)
    d = defs["update_agent"]
    assert d.spec.proposal and d.path_params == ("agent_id",)
    assert set(d.body_params) == set(PATCH_FIELDS)
    assert not {"name", "graph", "service", "layout", "expect_revision"} & set(
        d.parameters["properties"]
    )
    assert "전체 교체" not in d.description
    d = defs["create_agent"]
    assert d.spec.proposal and "layout" not in d.parameters["properties"]
    assert {"name", "display_name", "graph", "service"} <= set(d.body_params)
    # 평가 설정은 화면에서만 정한다. 라우트의 "draft 로 저장한다" 는 제안 설명과 어긋나 싣지 않는다
    assert "evaluation" not in d.parameters["properties"]["service"]["properties"]
    assert d.description == CATALOG.get("create_agent").description
    assert not defs["clone_agent"].spec.proposal
    # multipart
    d = defs["create_backend_api"]
    assert d.body_media == "multipart/form-data"
    assert set(d.parameters["required"]) == {"name", "spec_text", "filename"}
    assert d.parameters["properties"]["auth"]["enum"] == ["none", "basic", "wsse"]


def test_description_prefixes_table_text_with_docstring(defs):
    d = defs["create_backend_api"]
    assert d.description.startswith(CATALOG.get("create_backend_api").description)
    assert "새 버전을 만든다" in d.description
    # 한 문장만 따라온다 (docstring 의 둘째 문장은 없다)
    assert "접속 정보 필드만" not in d.description
    assert defs["navigate"].description == CATALOG.get("navigate").description


def test_titles_examples_and_deep_descriptions_are_stripped(defs):
    def walk(schema, depth):
        if isinstance(schema, list):
            for s in schema:
                walk(s, depth)
            return
        if not isinstance(schema, dict):
            return
        assert "title" not in schema and "examples" not in schema and "example" not in schema
        if depth >= DESCRIPTION_DEPTH:
            assert "description" not in schema, schema
        for key, value in schema.items():
            if key in ("properties", "patternProperties"):
                for v in value.values():
                    walk(v, depth + 1)
            elif key in ("items", "additionalProperties", "anyOf", "oneOf", "allOf"):
                walk(value, depth)

    for d in defs.values():
        walk(d.parameters, 0)
    # 깊이 1·2 의 설명은 남는다
    assert "description" in defs["create_backend_api"].parameters["properties"]["name"]


def test_body_field_colliding_with_path_param_gets_prefix():
    openapi = {
        "paths": {
            "/api/things/{name}": {
                "put": {
                    "parameters": [
                        {
                            "name": "name",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Body"}}
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Body": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "size": {"type": "integer"}},
                    "required": ["name"],
                }
            }
        },
    }
    catalog = Catalog(
        tools=(ToolSpec("put_thing", "PUT", "/api/things/{name}", "write", "x"),), excluded={}
    )
    (d,) = build_tool_schemas(openapi, catalog)
    assert d.path_params == ("name",) and d.body_params == {"body_name": "name", "size": "size"}
    assert d.parameters["required"] == ["name", "body_name"]


def test_hidden_fields_stay_out_of_the_tool(openapi_spec, defs):
    """설정 도구는 사람이 화면에서 정하는 웹 Tool·네트워크 절을 인자로 싣지 않는다. 본문에 없는
    필드를 빼라고 하면 표가 API 와 어긋난 것이라 CatalogError 다."""
    params = defs["update_platform_settings"].parameters["properties"]
    assert not {"web_tools", "network", "default_web_tools_image"} & set(params)
    assert {"llm_providers", "skills"} <= set(params)
    spec = ToolSpec("x", "PATCH", "/api/settings", "write", "x", hidden_fields=("nope",))
    with pytest.raises(CatalogError, match="뺄 수 없다"):
        build_tool_schemas(openapi_spec, Catalog(tools=(spec,), excluded={}))


def test_missing_operation_fails_loudly(openapi_spec):
    catalog = Catalog(tools=(ToolSpec("nope", "GET", "/api/nope", "read", "x"),), excluded={})
    with pytest.raises(CatalogError, match="OpenAPI 에 없다"):
        build_tool_schemas(openapi_spec, catalog)
    catalog = Catalog(tools=(ToolSpec("nope", "DELETE", "/api/me", "read", "x"),), excluded={})
    with pytest.raises(CatalogError, match="DELETE /api/me"):
        build_tool_schemas(openapi_spec, catalog)


def test_ref_resolution_depth_limit_and_collapse():
    components = {"A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}}}
    components["B"] = {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "b"}
    out = resolve_refs({"$ref": "#/components/schemas/A"}, components)
    assert out["properties"]["b"] == {"type": "string", "description": "b"}
    assert resolve_refs({"allOf": [{"type": "integer"}], "description": "n"}, {}) == {
        "type": "integer",
        "description": "n",
    }
    loop = {"L": {"type": "object", "properties": {"next": {"$ref": "#/components/schemas/L"}}}}
    with pytest.raises(CatalogError, match="깊이"):
        resolve_refs({"$ref": "#/components/schemas/L"}, loop)
    with pytest.raises(CatalogError, match="없다"):
        resolve_refs({"$ref": "#/components/schemas/Missing"}, {})
    assert clean({"title": "T", "type": "string", "description": "d"}) == {
        "type": "string",
        "description": "d",
    }


# -- 비밀 이름 ----------------------------------------------------------------------------


def _property_names(schema, components, acc: set[str], depth=0) -> None:
    if depth > 40 or not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _property_names(components[schema["$ref"].rsplit("/", 1)[1]], components, acc, depth + 1)
        return
    for name, sub in (schema.get("properties") or {}).items():
        acc.add(name)
        _property_names(sub, components, acc, depth + 1)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            _property_names(schema[key], components, acc, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            _property_names(sub, components, acc, depth + 1)


def test_no_secret_named_field_in_success_responses_or_hand_schemas(openapi_spec, defs):
    components = openapi_spec["components"]["schemas"]
    for t in CATALOG.api_tools:
        method, path = t.key
        op = openapi_spec["paths"][path][method.lower()]
        for code, response in op["responses"].items():
            if not code.startswith("2"):
                continue
            schema = ((response.get("content") or {}).get("application/json") or {}).get("schema")
            names: set[str] = set()
            _property_names(schema or {}, components, names)
            assert not names & SECRET_NAMES, (t.name, names & SECRET_NAMES)
        if t.args_schema is not None:
            names = set()
            _property_names(t.args_schema, {}, names)
            assert not names & SECRET_NAMES, (t.name, names & SECRET_NAMES)
    # 요청 스키마에 있는 password 는 secret_fields 로 거부된다 (test_assistant_tools)
    assert "password" in defs["create_data_service"].parameters["properties"]
