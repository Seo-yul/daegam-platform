"""플랫폼 오류 코드표 (P74 2.2절, D86). 코드 하나는 조건 하나이고 상태 하나다.

`STATUS` 가 플랫폼이 내는 모든 코드의 상태를 한 곳에 둔다. 상태가 둘 이상인 코드는 `RECORDED` 에만
있고, 상태 하나로는 깨지는 동작을 이유로 적는다. 에이전트 런타임의 코드는 런타임의 표
(agent-runtime/runtime/error_codes.py)가 집이고, 테스트(test_error_code_parity.py)가 두 표에 함께
있는 코드의 상태를 맞춰 본다. OAuth 의 `{error, error_description}` 은 RFC 6749 꼴이라 이 표 밖이다.

`STRICT` 는 테스트만 켠다(tests/conftest.py). 켜지면 `ServiceError`·`HTTPException` 처리기가 모든
응답을 표와 맞춰 보고, `status_of` 가 표에 없는 코드나 상태가 여럿인 코드를 찾을 때 멈춘다.
운영에서는 아무것도 보지 않는다.

`holds_reapply` 는 데이터 플레인 재적용이 문서를 막을 코드다(P74 2.5절). 같은 문서는 바뀌기 전까지
다시 해도 같은 오류가 나는 코드이고, 상태가 아니라 이 표시로 가른다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 테스트만 켠다. 켜지면 응답과 조회를 표와 맞춰 본다
STRICT = False

STATUS: dict[str, int] = {
    "account_disabled": 401,
    # 다른 Agent 가 부른 실행을 부른 턴의 사람으로 돌릴 수 없다 (P46 6.4절)
    "acts_as_unavailable": 403,
    # 잠든 Agent 에 런타임을 읽는 요청이다. 읽기는 깨우지 않는다 (P44 8절)
    "agent_asleep": 409,
    # 잠들게 하려는데 턴이 리스를 쥐고 있다 (P44 7절)
    "agent_busy": 409,
    # 호출받는 Agent 가 호출의 상한에 닿았거나 배포 중이다 (P46 6.3절)
    "agent_call_busy": 429,
    # 호출받는 Agent 가 이미 그 호출의 사슬에 있다 (P46 6.5절)
    "agent_call_cycle": 409,
    # 부르는 턴 하나가 시작할 수 있는 호출 수를 넘었다 (P46 6.5절)
    "agent_call_fanout": 429,
    # 사람이 그 Agent 에 보내는 호출이 분당 한도를 넘었다 (P46 6.3절)
    "agent_call_rate_limited": 429,
    # 호출의 사슬이 Agent 셋을 넘게 된다 (P46 6.5절)
    "agent_call_too_deep": 409,
    # Agent 호출이 꺼진 플랫폼에서 Agent 를 허브에 게시하거나 다른 Agent 를 부르려 했다 (P46 6.5절)
    "agent_calls_disabled": 409,
    "agent_client_id_is_ui_client": 422,
    # 게시한 Agent 의 허브 항목은 Gateway 의 백엔드가 되지 못한다 (P46 6.1절)
    "agent_item_gateway_unsupported": 422,
    "agent_oauth_missing": 422,
    "agent_run_limit": 429,
    # 다른 Agent 가 Tool 로 부를 Agent 에 memory 노드나 세션 예산이 없다 (P46 6.1절)
    "agent_source_runs_unmet": 409,
    # 부르려는 Agent 가 허브에 게시돼 있지 않다 (P46 6.1절)
    "agent_source_unpublished": 409,
    # 잠들 수 없는 Agent 다. 까닭은 `ineligible` 칸이다 (P44 6절)
    "agent_sleep_ineligible": 409,
    # 맡긴 작업의 끝난 실행이 있는데 그 답을 아직 읽지 못했다 (P46 6.7절). Retry-After 를 단다
    "agent_task_answer_unread": 503,
    # 맡긴 작업이 아무도 결정하지 않은 승인에서 끝나 다음 실행을 받지 않는다 (P46 6.7절)
    "agent_task_closed": 409,
    # 맡긴 작업이 승인을 기다려 다음 실행을 받지 않는다 (P46 6.7절)
    "agent_task_waiting": 409,
    # 파드를 다시 띄우는 중이다. Retry-After 뒤에 다시 보낸다 (P44 8절)
    "agent_waking": 503,
    "already_deployed": 409,
    "archive_too_large": 422,
    "assistant_disabled": 404,
    "auth_failed": 422,
    "authz_invalid": 422,
    "authz_not_ready": 503,
    "backend_version_mismatch": 422,
    "backends_not_ready": 409,
    # 지금 카탈로그에 없거나 인자에 변수가 있는 카탈로그 서버는 platform-admin 만 등록한다 (P55 5절)
    "catalog_server_admin_only": 403,
    # 카탈로그와 다른 인자나 env 이름은 platform-admin 만 쓴다 (P55 5절)
    "catalog_settings_admin_only": 403,
    "cert_manager_unavailable": 422,
    # Teams 는 사람을 확인하는 인증 모드(local, oidc)에서만 켠다 (P47 6.1절)
    "channels_need_token_auth": 422,
    "citation_not_found": 404,
    "cluster_mismatch": 422,
    "cluster_not_allowed": 422,
    # 문서가 아니라 클러스터의 사실(마지막 preflight)에 기대는 거절이라 재적용을 막지 않는다
    # (P63 2절)
    "cluster_rights_missing": 422,
    "cluster_unreachable": 502,
    # code mode 를 켠 Gateway 의 저장·미리보기·배포·재배포 (P56 6.2절)
    "code_mode_exposed": 422,
    "code_mode_not_read_only": 422,
    "code_mode_owner_cedar": 422,
    "code_mode_reserved_name": 422,
    "code_mode_toolhive_old": 422,
    "code_mode_unknown_names": 422,
    "compile_failed": 500,
    "consent_limit": 429,
    "conversion": 422,
    "credential_required": 422,
    "cross_tenant_consume_unsupported": 422,
    "cross_tenant_manage_unsupported": 422,
    "cross_tenant_unsupported": 422,
    "cross_tenant_view_unsupported": 422,
    "cycle": 422,
    # 미리 보기의 원본이 오늘의 토큰 예산을 다 썼다. 런타임의 같은 코드와 상태다 (P44b 7절)
    "daily_budget_exceeded": 429,
    "data_service_immutable": 422,
    "db_unreachable": 422,
    "decision_count_mismatch": 422,
    "decision_mismatch": 422,
    "deleting": 409,
    "derived_tools": 422,
    "dimensions_required": 422,
    "duplicate_backend": 422,
    "duplicate_backend_name": 422,
    "duplicate_env_prefix": 422,
    "duplicate_mapping": 422,
    "duplicate_sub": 422,
    "domain_is_public_suffix": 422,
    "duplicate_tool_name": 422,
    "duplicate_tools": 422,
    "embedding_immutable": 422,
    "embedding_not_supported": 422,
    "empty_document": 422,
    "empty_input": 422,
    "empty_instruction": 422,
    "empty_patch": 422,
    "encrypted_document": 422,
    # 플랫폼이 entry 항목의 URL 을 직접 부르지 않는다. 공인 주소, 그 테넌트 네임스페이스의 Service,
    # 관리자가 연 사설망만 부른다. reason 이 걸린 주소 규칙의 코드다
    "entry_address_refused": 422,
    "env_managed": 409,
    # 카탈로그가 비밀로 표시한 변수를 env 에 넣었다 (P55 5절)
    "env_must_be_secret": 422,
    # 카탈로그가 꼭 필요하다고 표시한 변수가 빠졌다 (P55 5절)
    "env_required_missing": 422,
    # 플랫폼·ToolHive 가 정하거나 실행할 코드를 바꿀 수 있는 env 이름 (P55 5절)
    "env_reserved": 422,
    # 평가 사례에 개인정보로 보이는 것이 있다. 손으로 쓴 사례는 확인으로 저장한다 (P45 8절)
    "evaluation_case_pii": 422,
    # 대화한 사람이 평가 사례로 쓰는 데 동의하지 않았다 (P45 8절)
    "evaluation_consent_missing": 409,
    # 세트의 사례 수 × 반복 수가 상한을 넘는다 (P45 7절)
    "evaluation_set_full": 422,
    # 세트를 확인한 뒤 바뀌었다. 바뀐 사례를 다시 확인한다 (P45 9절)
    "evaluation_set_changed": 409,
    # 그 Agent 의 평가가 이미 돈다 (P45 9절)
    "evaluation_run_active": 409,
    # 테넌트의 오늘 평가 실행을 다 썼다 (P45 9절)
    "evaluation_run_limit": 429,
    # 플랫폼에서 평가를 껐다 (P45 13절)
    "evaluations_disabled": 409,
    # 이름을 모르는 tools 노드가 있어 평가 미리 보기가 쓰기 Tool 을 잡을 수 없다 (P45 10절)
    "evaluation_tools_unknown": 409,
    # 채점 모델의 provider 가 평가하는 리비전의 모델 자리가 쓰는 provider 가 아니다 (P45 11절)
    "evaluation_grader_provider": 409,
    # 배포 게이트가 새 내용에 지난 평가 실행을 바란다 (P45 12절)
    "evaluation_required": 409,
    # 배포 게이트가 읽는 세트라 지울 수 없다 (P45 12절)
    "evaluation_set_in_gate": 409,
    "export_range_too_long": 422,
    "export_too_large": 422,
    "extract_busy": 429,
    "extract_failed": 422,
    "extract_timeout": 422,
    "extract_unavailable": 503,
    "feedback_not_found": 404,
    "forbidden": 403,
    "home_cluster": 409,
    "hook_message_too_long": 413,
    "hook_rate_limited": 429,
    "hook_secret_unavailable": 503,
    "hook_signature_invalid": 401,
    "hook_template_unresolved": 422,
    "host_conflict": 409,
    "host_reserved": 422,
    # 거부한 digest 를 든 허브 항목을 Gateway 백엔드로 쓴다 (P55 6절). 설정이 바뀌기 전까지 같은
    # 문서는 다시 해도 같다
    "hub_backend_refused": 422,
    # 넣은 허브 이미지 읽기 자격 증명의 관리 Secret 을 읽지 못했다 (P55 6절)
    "hub_images_secret_unavailable": 503,
    # 허브 이미지 설정 전에는 카탈로그 image 를 가져올 수 없다 (P55 6절)
    "hub_images_unset": 422,
    # 뒤 판이 쓴 칸을 든 허브 항목 (P55 9절) 또는 종류를 이 판이 모르는 항목 (P46 A0). 판을
    # 올리기 전까지 같은 문서는 다시 해도 같다
    "hub_item_newer": 422,
    "hub_item_not_consumable": 422,
    # 허브 이미지 접두사가 클러스터의 제품 이미지 접두사와 겹친다 (P55 6절)
    "hub_prefix_overlap": 422,
    "idp_unavailable": 503,
    "ignored_by_operator": 422,
    # 서명이 서로 다른 카탈로그 서버를 이름 댄다 (P55 6절)
    "image_catalog_conflict": 422,
    # 허브 키로 서명하고 허브 주석을 단 서명이 없거나, 준 태그를 든 서명이 없다 (P55 6절)
    "image_not_admitted": 422,
    # 허브 이미지 접두사 밖의 이미지다 (P55 6절)
    "image_outside_registry": 422,
    # platform-admin 이 거부한 digest 로 새 항목을 만든다 (P55 6절)
    "image_refused": 422,
    # referrers 가 상한을 넘어 일부만 보고 정하지 않는다 (P55 6절)
    "image_signatures_too_many": 422,
    "in_progress": 409,
    "in_use": 409,
    "indexing": 409,
    "ingress_class_not_allowed": 422,
    "ingress_disabled": 422,
    # 본문 칸 하나가 상한을 넘은 것이라 2.1절로는 413 이지만 상태를 그대로 둔다 (P74 2.4절)
    "input_too_large": 422,
    "instance_token_missing": 503,
    "instances_exist": 409,
    "integrity_error": 500,
    "invalid_api_key": 422,
    "invalid_archive": 422,
    "invalid_auth": 422,
    "invalid_backends": 422,
    "invalid_cors_origin": 422,
    "invalid_credential_secret": 422,
    "invalid_data_service_kind": 422,
    "invalid_display_name": 422,
    "invalid_endpoint": 422,
    "invalid_expiry": 422,
    "invalid_format": 422,
    "invalid_graph": 422,
    "invalid_host": 422,
    "invalid_input": 422,
    # 본문이 JSON 이 아니거나 너무 깊다 (P37 3절)
    "invalid_json": 422,
    "invalid_kind": 422,
    "invalid_manifest": 422,
    "invalid_minify_options": 422,
    "invalid_name": 422,
    "invalid_purpose": 422,
    "invalid_request": 422,
    "invalid_resources": 422,
    "invalid_role": 422,
    "invalid_scope": 422,
    "invalid_skill": 422,
    "invalid_soap_headers_secret": 422,
    # 라우트 검증보다 먼저 읽는 테넌트 헤더라 400 이다 (P74 2.4절)
    "invalid_tenant": 400,
    "invalid_token": 401,
    "invalid_tool_name": 422,
    "invalid_ca_bundle": 422,
    "invalid_value": 422,
    "invalid_yaml": 422,
    "job_active": 409,
    "job_not_done": 422,
    "job_not_found": 422,
    "job_not_overrides": 422,
    "job_not_refine": 422,
    "jwks_unavailable": 503,
    "k8s_error": 502,
    # deploy() 만 낸다. 재적용에는 나오지 않아 막을 표시가 없다
    "k8s_name_conflict": 409,
    # 상태 충돌이지만 422 를 그대로 둔다 (P74 2.4절)
    "last_admin": 422,
    # LLM 게이트웨이 설정을 렌더하지 못했다. 상태 API 의 까닭이다 (P38 4절)
    "llm_gateway_config_dollar": 422,
    "llm_gateway_config_too_large": 422,
    "llm_gateway_images_missing": 422,
    # 게이트웨이 경로가 아닌 Agent 의 LLM 토큰 교체 (P38 5절)
    "llm_token_missing": 409,
    "locked_field": 422,
    "login_locked": 429,
    "long_term_schema_failed": 422,
    "max_input_chars": 413,
    "mixed": 422,
    "name_conflict": 409,
    "name_immutable": 422,
    # 상태 충돌이지만 422 를 그대로 둔다 (P74 2.4절)
    "namespace_claimed": 422,
    "namespace_no_longer_allowed": 409,
    "namespace_not_allowed": 422,
    "namespace_reserved": 422,
    "no_backends": 422,
    "no_changes": 422,
    "no_cluster": 422,
    "no_home_cluster": 503,
    "no_provider": 422,
    "no_secret_store": 422,
    "not_a_member": 403,
    "not_deployed": 409,
    "not_found": 404,
    "not_member": 403,
    "not_memory_owner": 403,
    "not_pending": 409,
    "not_published": 404,
    "not_ready": 409,
    "not_session_owner": 403,
    # 고른 리비전이 배포된 정의와 같은 내용이다 (P45 6절)
    "nothing_to_roll_back": 409,
    # 인증을 싣는 entry 항목의 URL 이 https 가 아니다. 테넌트 네임스페이스의 Service 만 예외다
    # (P86 6.1절)
    "outside_auth_not_https": 422,
    # Gateway·MCP·추적 전파가 직접 정하는 헤더 이름이다 (P86 6.1절)
    "outside_header_reserved": 422,
    # 헤더 값이 비었거나 제어 문자가 있거나 8 KiB 를 넘는다 (P86 6.1절)
    "outside_header_value_invalid": 422,
    # 사람마다의 OAuth 계정 인증은 P86b 가 만든다
    "outside_oauth_unavailable": 422,
    # SSE 로만 말하는 외부 MCP 서버다. 허브는 streamable HTTP 서버만 받는다 (P54b 15절 3)
    "outside_sse_unsupported": 422,
    "overrides_invalid": 409,
    "password_change_required": 403,
    "payload_too_large": 413,
    "pgvector_missing": 422,
    "platform_item": 422,
    "platform_tool_disabled": 409,
    "platform_tool_not_exposable": 422,
    # 플랫폼이 미리 보기를 껐다 (P44b 10절)
    "preview_disabled": 409,
    # 테넌트의 미리 보기 자리가 모두 찼다 (P44b 10절)
    "preview_limit": 409,
    # 그 사람이 그 Agent 에 연 미리 보기가 없다. 하루 안에 끝났으면 `ended` 가 까닭이다
    "preview_not_found": 404,
    # 오늘 미리 보기를 시작하고 옮긴 수가 상한이다. Retry-After 가 다음 날까지다 (P44b 7절)
    "preview_start_limit": 429,
    # 결정이 본 제안이 기다리는 제안이 아니다 (P48 6.1절). 길이 셋이다. 결정 요청의 HTTP 409 는
    # 지문이 없거나 다를 때이고 화면이 읽는다. 제안 다시 읽기의 HTTP 409 는 기다리는 호출이 그 기준
    # 위에 다시 짜이지 않을 때이고 캔버스가 읽는다. 승인 때 먼저 볼 것이 늘어난 것은 도구 결과이고
    # 모델이 읽는다
    "proposal_changed": 409,
    # 세션에 기다리는 Agent 정의 제안이 없다 (P48 6.1절)
    "proposal_not_found": 404,
    "provider_no_key": 422,
    "provider_not_supported": 422,
    "provider_unavailable": 422,
    "published": 409,
    "published_names_unknown": 422,
    "published_oidc_unsupported": 422,
    "published_owner_forbid": 422,
    "published_proxy_unsupported": 422,
    "published_script_tool": 422,
    "query_failed": 422,
    "reauth_required": 403,
    "redeploy_required": 409,
    "redis_required": 422,
    # 이미지가 거부된 허브 항목의 접근을 넓힌다 (P55 6절)
    "refused_image_not_shareable": 422,
    # 허브 레지스트리가 허브 이미지의 읽기 자격 증명을 거절했다 (P55 6절). 이미지의 거절이 아니다
    "registry_credential_refused": 502,
    # 허브 레지스트리에 닿지 못했거나 5xx 다 (P55 6절)
    "registry_unreachable": 502,
    # 끝나지 않은 비용 다시 계산이 있다 (P37 6절)
    "reprice_running": 409,
    "request_expired": 410,
    "request_used": 409,
    "remember_refused": 422,
    "reserved_name": 422,
    "revision_conflict": 409,
    # Tool 규칙의 저장 검증과 배포 (P52 1절)
    "rule_agent_oidc": 422,
    "rule_argument_type": 422,
    "rule_array_path": 422,
    "rule_custom_image": 422,
    "rule_names_unknown_gateway_tool": 422,
    "rule_names_unknown_tool": 422,
    "rule_unknown_agent": 422,
    "rule_unknown_argument": 422,
    "rules_owner_cedar": 422,
    "rules_too_large": 422,
    "rules_toolhive_old": 422,
    "run_budget_required": 409,
    "run_closed": 409,
    "run_source_limit": 409,
    "runs_need_events": 409,
    "runs_need_memory": 409,
    "runtime_feature_missing": 409,
    "runtime_rolling": 409,
    "runtime_timeout": 504,
    "runtime_unreachable": 502,
    # 코드 실행 샌드박스 (P40). 열기·찾기·닫기는 런타임의 기계 경로이고, 점검과 허용은 Cluster
    # 화면이 부른다. 게이트는 클러스터의 사실이라 문서를 막지 않는다
    "sandbox_check_running": 409,
    "sandbox_check_stale": 409,
    "sandbox_gate_closed": 409,
    "sandbox_limit": 429,
    # 배포 검사다. 네임스페이스 LimitRange 의 최대 메모리나 CPU 가 샌드박스 하나를 담지 못한다 (7절)
    "sandbox_limit_range": 409,
    "sandbox_namespace_policy": 503,
    "sandbox_not_enabled": 409,
    "sandbox_not_found": 404,
    "sandbox_open_failed": 503,
    "sandbox_rate_limited": 429,
    "sandbox_starting": 503,
    "sandbox_unavailable": 503,
    "schedule_invalid_cron": 422,
    "schedule_too_frequent": 422,
    "secret_missing": 409,
    # Secret env 를 든 허브 항목은 다른 테넌트에 열 수 없다 (P55 5절)
    "secret_not_shareable": 422,
    # DAEGAM_SERVICE_ACCOUNTS=off 인 배포본에서 서비스 어카운트를 넓히려 했다 (P53b 6.7절)
    "service_accounts_off": 409,
    "session_not_found": 404,
    "signer_unavailable": 503,
    "skill_limit": 409,
    "skill_not_consumable": 422,
    "skill_quota_exceeded": 409,
    "skill_unchanged": 422,
    "source_masked": 403,
    "source_mismatch": 422,
    "source_not_found": 422,
    "source_not_verified": 422,
    "source_too_large": 422,
    "state_conflict": 409,
    "stub_has_no_key": 422,
    "stub_provider_disallowed": 422,
    "surface_changed": 422,
    # 상태 충돌이지만 422 를 그대로 둔다 (P74 2.4절)
    "svc_token_name_collision": 422,
    "tenant_mismatch": 409,
    # 라우트 검증보다 먼저 읽는 테넌트 헤더라 400 이다 (P74 2.4절)
    "tenant_required": 400,
    # Teams 를 켜려면 App ID, Entra 테넌트 id, Client Secret 이 모두 있어야 한다 (P47 6.1절)
    "teams_not_configured": 422,
    # F0.2 를 반영한 묶음(A2) 전에는 Teams 를 켜지 않는다 (P47 9절)
    "teams_not_ready": 409,
    # 관리 Secret `daegam-channel-teams` 를 읽지 못했다 (P47 6.9절)
    "teams_secret_unavailable": 503,
    # 런타임 이벤트 수집 경로의 되밀기 (P37 3절). 둘 다 Retry-After 를 단다
    "telemetry_busy": 503,
    "telemetry_rate_limited": 429,
    # 잠든 OAuth 인가 서버가 동의 허용에 답한다 (P53b 6.5절)
    "temporarily_unavailable": 503,
    "tls_mode_not_allowed": 422,
    "tls_secret_invalid": 422,
    "token_expired": 401,
    "token_limit": 429,
    "tokens_active": 409,
    "too_large": 413,
    # 개수 상한이지만 422 를 그대로 둔다 (P74 2.4절)
    "too_many_documents": 422,
    "too_many_jobs": 429,
    "too_many_uploads": 429,
    "toolhive_too_old": 422,
    "tools_list_failed": 502,
    "trigger_needs_approval": 409,
    "trigger_paused": 409,
    # 턴 위임의 갱신과 끝내기 경로의 거절과 되밀기 (P53 4절). 되밀기는 Retry-After 를 단다
    "turn_grant_ended": 410,
    "turn_grant_not_found": 404,
    "turn_grant_rate_limited": 429,
    "turn_grant_refused": 403,
    "turn_in_progress": 409,
    "turn_not_found": 404,
    # 규칙이 토큰을 주는 대상이 있는데 만들거나 적지 못해 턴을 시작하지 않았다 (P53 2절)
    "turn_tokens_unavailable": 503,
    "unauthenticated_exposure": 422,
    "unauthorized": 401,
    "unknown_backend_api": 422,
    "unknown_change": 422,
    "unknown_cluster": 422,
    "unknown_data_service": 422,
    "unknown_database": 422,
    "unknown_hub_item": 422,
    "unknown_ingress_class": 422,
    "unknown_issuer": 422,
    "unknown_provider": 422,
    "unknown_revision": 409,
    "unknown_spec_version": 422,
    "unknown_time_zone": 422,
    "unknown_tools": 422,
    "unknown_upload": 422,
    "unsupported": 422,
    "unsupported_format": 422,
    # 게이트웨이 사용량 수집은 OTLP JSON 만 받는다 (P37 8절)
    "unsupported_media_type": 415,
    "upload_busy": 429,
    "upload_rate_limited": 429,
    "url_blocked": 422,
    "url_blocked_host": 422,
    "url_host": 422,
    "url_port": 422,
    "url_private": 422,
    "url_scheme": 422,
    "url_unresolvable": 422,
    "url_userinfo": 422,
    # FastAPI 의 검증 응답(코드 없는 목록)을 감사 기록·비서·화면이 부르는 이름이다
    "validation_error": 422,
    "verdict_error": 422,
    "verify": 422,
    "verify_incomplete": 422,
    "weak_password": 422,
    "web_backend_unknown_pins": 409,
    "web_backend_unpinned_agents": 409,
    "web_gateway_unknown_names": 422,
}


@dataclass(frozen=True)
class Recorded:
    """상태가 둘 이상인 코드. `statuses` 가 None 이면 어떤 상태든 된다."""

    statuses: frozenset[int] | None
    reason: str


RECORDED: dict[str, Recorded] = {
    "invalid_credentials": Recorded(
        frozenset({401, 403}),
        "local 모드의 401 은 화면을 로그아웃시킨다. 개인 토큰 발급에서 비밀번호를 잘못 친 "
        "것은 403 이라야 로그인이 풀리지 않는다",
    ),
    "runtime_error": Recorded(
        None,
        "코드 없이 답한 런타임 응답을 감싼 것이라 상태만이 남은 정보다",
    ),
    "runtime_image_too_old": Recorded(
        frozenset({409, 503}),
        "플랫폼은 409 를 낸다. 503 은 문서보다 오래된 런타임 이미지의 거절을 프록시가 넘긴 것이고, "
        "새 이미지가 옛 이미지가 보내는 것을 바꿀 수 없다",
    ),
}

# 422 가 아니지만 재적용을 막는 코드. 같은 문서로 다시 해도 같은 오류가 난다. not_deployed·
# integrity_error 는 저장된 데이터가 서로 어긋난 것이라 다시 해도 낫지 않는다
_HELD_BEYOND_422 = frozenset(
    {
        # 평가 미리 보기의 소스가 이름을 알리기 전에는 다시 몰아가도 같다 (P45 10절)
        "evaluation_tools_unknown",
        "host_conflict",
        "integrity_error",
        "not_deployed",
        "redeploy_required",
        "runtime_image_too_old",
        "too_large",
        "unknown_revision",
    }
)
# 422 지만 재적용을 막지 않는 코드. 클러스터의 사실이 바뀌면 풀린다
_NOT_HELD_422 = frozenset({"cluster_rights_missing"})


def allowed(status: int, code: str) -> bool:
    """이 상태와 코드의 짝이 표에 맞는가. 표에 없는 코드는 맞지 않는다."""
    if code in RECORDED:
        statuses = RECORDED[code].statuses
        return statuses is None or status in statuses
    return STATUS.get(code) == status


def status_of(code: str, default: int) -> int:
    """다른 예외의 코드를 다시 쌀 때의 상태. 상태가 하나인 코드만 찾는다.

    표에 없으면 `default`(지금까지 쓰던 상태)다. STRICT 에서는 표에 없는 코드와 상태가 여럿인
    코드가 멈춘다. 다시 싸는 쪽이 여럿 중 하나를 우연히 고르지 않게 한다."""
    if code in RECORDED:
        if STRICT:
            raise AssertionError(f"{code} 는 상태가 여럿이라 다시 싸는 쪽이 찾을 수 없다")
        return default
    status = STATUS.get(code)
    if status is None:
        if STRICT:
            raise AssertionError(f"{code} 가 오류 코드표(daegam/error_codes.py)에 없다")
        return default
    return status


def holds_reapply(code: str) -> bool:
    """재적용이 이 코드에서 문서를 막는가(P74 2.5절). 같은 문서는 바뀌기 전까지 다시 해도 같은
    오류가 나는 코드다."""
    if code in _HELD_BEYOND_422:
        return True
    return STATUS.get(code) == 422 and code not in _NOT_HELD_422
