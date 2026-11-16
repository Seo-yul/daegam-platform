"""OpenAPI 스냅샷: 모든 경로가 응답 스키마($ref)를 갖고 오류가 ErrorResponse 로 문서화된다.
`make ui-types` 가 이 문서로 ui/src/api/schema.d.ts 를 만든다."""

from __future__ import annotations

from daegam.urls import API_PREFIX

# 응답 본문이 JSON 모델이 아닌 경로(initContainer 용 파일, text/yaml, 순수 dict)
UNTYPED = {
    ("get", "/healthz"),
    ("get", f"{API_PREFIX}/backend-apis/{{api_id}}/versions/{{version}}/openapi.yaml"),
    # 변환된 OpenAPI 문서 자체(dict). UI 문서 뷰어가 받는다
    ("get", f"{API_PREFIX}/backend-apis/{{api_id}}/versions/{{version}}/spec"),
    ("get", f"{API_PREFIX}/mcp-servers/{{server_id}}/revisions/{{n}}/profile.json"),
    # 런타임 설정 agent.json (agent-runtime/schema/agent.schema.json 이 계약이다)
    ("get", f"{API_PREFIX}/agents/{{agent_id}}/agent.json"),
    # 런타임 SSE 프록시 (text/event-stream)
    ("post", f"{API_PREFIX}/agents/{{agent_id}}/chat"),
    ("post", f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}/decisions"),
    ("post", f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}/regenerate"),
    # 미리 보기의 같은 SSE 프록시 (P44b 6절)
    ("post", f"{API_PREFIX}/agents/{{agent_id}}/preview/chat"),
    ("post", f"{API_PREFIX}/agents/{{agent_id}}/preview/sessions/{{session_id}}/decisions"),
    ("post", f"{API_PREFIX}/agents/{{agent_id}}/preview/sessions/{{session_id}}/regenerate"),
    # 비서 SSE (D31). 이벤트 계약은 docs/guide/assistant.md 9절이다
    ("post", f"{API_PREFIX}/assistant/sessions/{{session_id}}/turns"),
    ("post", f"{API_PREFIX}/assistant/sessions/{{session_id}}/decisions"),
    # local 로그아웃 (D40). 토큰에 상태가 없어 본문 없이 204 다
    ("post", f"{API_PREFIX}/auth/logout"),
    # 다운로드 본문(P36). 피드백 NDJSON, 인용 원문과 세션 파일의 바이트이고 헤더는
    # api/_download.py 가 정한다
    ("get", f"{API_PREFIX}/agents/{{agent_id}}/feedback/export"),
    ("get", f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}/citations/{{n}}/source"),
    ("get", f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}/files/{{path}}"),
    (
        "get",
        f"{API_PREFIX}/agents/{{agent_id}}/preview/sessions/{{session_id}}/citations/{{n}}/source",
    ),
    ("get", f"{API_PREFIX}/agents/{{agent_id}}/preview/sessions/{{session_id}}/files/{{path}}"),
    # Skill 리비전의 zip 내보내기(D64)
    ("get", f"{API_PREFIX}/skills/{{skill_id}}/revisions/{{n}}/archive"),
    # Teams 앱 패키지의 zip(P47 6.1절)
    ("get", f"{API_PREFIX}/settings/channels/teams/app-package"),
    # 평가 거두기(P36)는 본문 없이 204 다
    (
        "delete",
        f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}/turns/{{turn_id}}/feedback",
    ),
    # 보내지 않은 첨부 업로드 지우기(P36)는 본문 없이 204 다
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/uploads/{{upload_id}}"),
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/preview/uploads/{{upload_id}}"),
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/schedules/{{schedule_id}}"),
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/triggers/{{trigger_id}}"),
    # 기억한 승인 선택 지우기(P43)는 본문 없이 204 다
    ("delete", f"{API_PREFIX}/me/approval-choices/{{choice_id}}"),
    # 평가 세트와 사례 지우기(P45)는 본문 없이 204 다
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/evaluation-sets/{{set_id}}"),
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/evaluation-sets/{{set_id}}/cases/{{case_id}}"),
    ("delete", f"{API_PREFIX}/agents/{{agent_id}}/evaluation-sets/{{set_id}}/runs/{{run_id}}"),
}

EXPECTED_SCHEMAS = {
    "ErrorResponse",
    "ErrorDetail",
    "DeleteResponse",
    "VersionInfo",
    "BackendAPISummary",
    "BackendAPIVersionOut",
    "UploadResponse",
    "VerifyResponse",
    "ConvertResponse",
    "TokenMeta",
    "IssuedToken",
    "DataServiceOut",
    "DataServiceCreated",
    "DataServiceVerifyResponse",
    "MCPServerOut",
    "MCPServerRevisionOut",
    "PreviewResponse",
    "MCPServerDeployed",
    "InstanceStatus",
    "ManifestResponse",
    "OverridesResponse",
    "MCPServerDeleteResponse",
    "ClusterOut",
    "ClusterPreflightResponse",
    "PreflightOut",
    "PlatformSettingsOut",
    "TenantSettingsOut",
    "TenantMembership",
    "MembershipOut",
    "GroupMapping",
    "AuthConfig",
    "MeOut",
    "SpecJobOut",
    "SpecJobDetail",
    "HubItemWithStatus",
    "HubStatus",
    "GatewayOut",
    "GatewaySaved",
    "GatewayDeployed",
    "GatewayRevisionOut",
    "GatewayPreviewResponse",
    "GatewayStatus",
    "GatewayBackendRow",
    "GatewayDeleteResponse",
    "AgentOut",
    "AgentSaved",
    "AgentDeployed",
    "AgentRevisionOut",
    "AgentValidateResponse",
    "AgentIssueOut",
    "AgentTemplateOut",
    "AgentSummary",
    "AgentStatus",
    "AgentDeleteResponse",
    "AgentMemoriesDeleteResponse",
    "AgentChatBody",
    "AgentUploadOut",
    "AgentDecisionsBody",
    "AgentSessionsOut",
    "AgentSessionOut",
    "AgentPendingOut",
    "AgentMetricsOut",
    "AgentEventOut",
    "AssistantConfigOut",
    "AssistantSessionListOut",
    "AssistantSessionCreated",
    "AssistantSessionOut",
    "AssistantTurnBody",
    "AssistantDecisionsBody",
    "SkillOut",
    "SkillImportedOut",
    "SkillValidateOut",
    "SkillRevisionMetaOut",
    "SkillRevisionOut",
}


def _success(op: dict) -> dict:
    codes = [c for c in op["responses"] if c.startswith("2")]
    assert len(codes) == 1, op.get("operationId")
    return op["responses"][codes[0]]


def _has_ref(schema: dict) -> bool:
    if "$ref" in schema:
        return True
    if schema.get("type") == "array":
        return "$ref" in schema.get("items", {})
    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants:
        return all("$ref" in v for v in variants)
    return False


def test_every_operation_has_a_typed_response(openapi_spec):
    untyped = set()
    for path, ops in openapi_spec["paths"].items():
        for method, op in ops.items():
            content = _success(op).get("content") or {}
            schema = (content.get("application/json") or {}).get("schema") or {}
            if not _has_ref(schema):
                untyped.add((method, path))
    assert untyped == UNTYPED


def test_errors_are_documented_as_error_response(openapi_spec):
    for path, ops in openapi_spec["paths"].items():
        if path in ("/healthz", f"{API_PREFIX}/version"):
            continue
        for method, op in ops.items():
            for code in ("401", "403", "404", "422"):
                resp = op["responses"].get(code)
                assert resp is not None, (method, path, code)
                ref = resp["content"]["application/json"]["schema"]["$ref"]
                assert ref == "#/components/schemas/ErrorResponse", (method, path, code)


def test_response_components_exist(openapi_spec):
    schemas = openapi_spec["components"]["schemas"]
    missing = EXPECTED_SCHEMAS - set(schemas)
    assert not missing, missing
    # 오류 본문 형태 {detail: {code, detail, ...}}
    detail = schemas["ErrorDetail"]
    assert set(detail["required"]) == {"code", "detail"}
    assert detail.get("additionalProperties", True) is not False


def test_agent_metrics_fields_are_in_the_schema(openapi_spec):
    """런타임 몫의 모델은 `extra="allow"` 라 적지 않은 칸도 지나가지만 OpenAPI 에는 없다. 화면이
    읽는 칸은 모델에 적어 `make ui-types` 가 덮는다(P35). 파드 하나의 값인 `pod` 는 없다(P37)."""
    schemas = openapi_spec["components"]["schemas"]
    assert {"runtime", "tools", "turns", "telemetry"} <= set(
        schemas["AgentMetricsOut"]["properties"]
    )
    assert "pod" not in schemas["AgentMetricsOut"]["properties"]
    assert "protocols" in schemas["AgentMetricsToolsOut"]["properties"]
    assert "features" in schemas["AgentMetricsRuntimeOut"]["properties"]


def test_paths_snapshot(openapi_spec):
    typed = {
        (m, p) for p, ops in openapi_spec["paths"].items() for m in ops if (m, p) not in UNTYPED
    }
    assert typed >= {
        ("get", f"{API_PREFIX}/version"),
        ("get", f"{API_PREFIX}/auth/config"),
        ("get", f"{API_PREFIX}/me"),
        ("get", f"{API_PREFIX}/tenants"),
        ("get", f"{API_PREFIX}/tenants/{{tenant}}/settings"),
        ("get", f"{API_PREFIX}/tenants/{{tenant}}/members"),
        ("get", f"{API_PREFIX}/group-mappings"),
        ("get", f"{API_PREFIX}/backend-apis"),
        ("post", f"{API_PREFIX}/backend-apis/verify"),
        ("get", f"{API_PREFIX}/data-services"),
        ("get", f"{API_PREFIX}/mcp-servers"),
        ("post", f"{API_PREFIX}/mcp-servers/{{server_id}}/deploy"),
        ("post", f"{API_PREFIX}/mcp-servers/{{server_id}}/undeploy"),
        ("post", f"{API_PREFIX}/mcp-servers/{{server_id}}/clone"),
        ("get", f"{API_PREFIX}/mcp-servers/{{server_id}}/manifest"),
        ("get", f"{API_PREFIX}/clusters"),
        ("post", f"{API_PREFIX}/clusters/{{cluster_id}}/preflight"),
        ("get", f"{API_PREFIX}/settings"),
        ("get", f"{API_PREFIX}/spec-generator/jobs"),
        ("get", f"{API_PREFIX}/hub"),
        ("post", f"{API_PREFIX}/hub/{{item_id}}/refresh-tools"),
        ("get", f"{API_PREFIX}/gateways"),
        ("get", f"{API_PREFIX}/gateways/{{gateway_id}}/revisions/{{n}}/preview"),
        ("post", f"{API_PREFIX}/gateways/{{gateway_id}}/deploy"),
        ("post", f"{API_PREFIX}/gateways/{{gateway_id}}/redeploy"),
        ("post", f"{API_PREFIX}/gateways/{{gateway_id}}/undeploy"),
        ("post", f"{API_PREFIX}/gateways/{{gateway_id}}/clone"),
        ("post", f"{API_PREFIX}/gateways/{{gateway_id}}/publish"),
        ("get", f"{API_PREFIX}/gateways/{{gateway_id}}/manifest"),
        ("put", f"{API_PREFIX}/gateways/{{gateway_id}}/overrides"),
        ("post", f"{API_PREFIX}/gateways/{{gateway_id}}/drift/{{action}}"),
        # Agent (D26, D42: 정의와 배포가 한 리소스다)
        ("get", f"{API_PREFIX}/agents"),
        ("get", f"{API_PREFIX}/agents/templates"),
        ("post", f"{API_PREFIX}/agents/validate"),
        ("get", f"{API_PREFIX}/agents/summary"),
        ("put", f"{API_PREFIX}/agents/{{agent_id}}"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/revisions/{{n}}"),
        ("post", f"{API_PREFIX}/agents/{{agent_id}}/clone"),
        ("post", f"{API_PREFIX}/agents/{{agent_id}}/deploy"),
        ("post", f"{API_PREFIX}/agents/{{agent_id}}/redeploy"),
        ("post", f"{API_PREFIX}/agents/{{agent_id}}/undeploy"),
        ("delete", f"{API_PREFIX}/agents/{{agent_id}}"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/manifest"),
        ("put", f"{API_PREFIX}/agents/{{agent_id}}/overrides"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/sessions"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/sessions/{{session_id}}/pending"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/metrics"),
        ("get", f"{API_PREFIX}/agents/{{agent_id}}/events"),
    }


def test_api_paths_live_under_the_api_prefix(openapi_spec):
    """UI 가 루트를 쓰므로 문서화된 경로는 probe(/healthz) 하나만 빼고 전부 /api 아래다."""
    outside = {p for p in openapi_spec["paths"] if not p.startswith(f"{API_PREFIX}/")}
    assert outside == {"/healthz"}
