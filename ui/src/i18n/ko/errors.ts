import type { RefKey } from "@/lib/errorText";
import { DOCUMENT_FORMAT_LABEL, EVALUATION_MAX_CASE_RUNS, FEEDBACK_EXPORT_MAX_DAYS, SESSION_TITLE_MAX_CHARS } from "@/lib/limits";

/**
 * 서버 오류 코드의 문장 (D43). lib/errorText.ts 가 코드를 이 표에 태운다.
 *
 * 서버는 `{code, detail}` 을 내는데 detail 은 영어이고 code 는 기계 어휘다. 화면이 그것을
 * `code: detail` 로 이어 붙이면 쓰는 사람은 `not_found: mcp server ... not found` 를 그대로 본다.
 * 여기 표는 코드마다 **무엇이 잘못됐는지와 무엇을 하면 되는지**를 한 문장으로 적어 둔 것이고,
 * 표에 없는 코드는 describeError 가 폴백 문장 뒤에 서버 detail 을 붙여 준다.
 *
 * 문장 규칙은 셋이다. 오류 코드를 문장에 넣지 않는다. 화면 문구이므로 합니다체다.
 * 대시로 설명을 잇지 않는다.
 */
export const errors = {
  /**
   * 오류 코드 → 한국어 문장. 키는 플랫폼이 실제로 내는 코드다
   * (platform/daegam 의 `error(...)`·`ServiceError`·`RenderError`·`PipelineError`).
   */
  codes: {
    // -- 참조·상태 때문에 막히는 것 ------------------------------------------------
    // -- 장기 기억 (D34) ---------------------------------------------------------
    long_term_schema_failed:
      "장기 기억을 둘 스키마를 만들지 못했습니다. Database 계정에 CREATE 권한이 필요합니다. 권한을 준 뒤 다시 배포해 주세요.",
    not_memory_owner:
      "장기 기억은 그 사용자의 것이라 본인이나 테넌트 관리자만 지울 수 있습니다. 본인 계정으로 열거나 테넌트 관리자에게 요청해 주세요.",
    // 내 장기 기억 보기(P36 8절). 런타임이 장기 기억을 쓰지 않거나, 그 사이에 파일이 지워졌다
    long_term_off: "이 Agent 는 장기 기억을 사용하지 않습니다. Agent 정의의 memory 노드에서 장기 기억을 켜고 다시 배포하면 사용할 수 있습니다.",
    memory_not_found: "이 기억 파일을 찾을 수 없습니다. 이미 삭제됐을 수 있으니 목록을 새로 고쳐 주세요.",
    in_use: "다른 리소스가 사용하고 있어 삭제할 수 없습니다. 아래 목록에서 연결을 먼저 해제해 주세요.",
    // Tool 규칙을 컴파일한 Cedar 의 크기 상한 (P52 1절)
    rules_too_large:
      "Tool 규칙을 Cedar 로 바꾼 크기가 한도를 넘습니다. 부를 수 있는 대상이 같은 Tool 을 묶거나 규칙을 둔 Tool 을 줄여 주세요.",
    // Tool 규칙의 저장 검증과 배포 (P52 1절, services/tool_rules.py)
    rule_names_unknown_tool: "규칙에 적은 Tool 이 선택한 Tool 목록에 없습니다. Tool 선택을 확인해 주세요.",
    rule_names_unknown_gateway_tool: "규칙에 적은 Tool 이 이 Gateway 가 제공하는 Tool 에 없습니다. Gateway 의 Tool 을 확인해 주세요.",
    rule_unknown_argument: "조건에 적은 인자가 이 Tool 에 없습니다. 인자 이름을 확인해 주세요.",
    rule_argument_type: "조건의 값이 인자의 형식과 맞지 않습니다. 인자 형식에 맞게 다시 적어 주세요.",
    rule_array_path: "배열 안의 인자에는 조건을 둘 수 없습니다. 배열 밖의 인자를 선택해 주세요.",
    rule_custom_image: "직접 지정한 이미지를 쓰는 서버에는 인자 조건을 둘 수 없습니다. 기본 런타임 이미지로 바꾼 뒤 다시 저장해 주세요.",
    rule_unknown_agent: "규칙에 넣은 Agent 를 이 테넌트에서 찾을 수 없습니다. 목록에서 다시 선택해 주세요.",
    rule_agent_oidc: "Gateway 가 아직 IdP 토큰만 받는 동안에는 Gateway 규칙에 Agent 를 넣을 수 없습니다. 그동안은 테넌트의 Agent 가 모두 같은 클라이언트로 부르기 때문입니다.",
    rules_owner_cedar: "이 Gateway 는 Advanced 탭에 저장한 Cedar 정책을 그대로 쓰고 있어 Tool 규칙을 적용할 수 없습니다. Advanced 탭에서 그 정책을 지운 뒤 다시 저장해 주세요.",
    rules_toolhive_old: "이 클러스터의 ToolHive 가 Tool 규칙을 지원하지 않아 규칙이 있는 서버를 배포할 수 없습니다. 플랫폼 관리자에게 ToolHive 업그레이드를 요청해 주세요.",
    instances_exist: "이 클러스터에 배포된 것이 남아 있어 삭제할 수 없습니다. 먼저 배포를 해제하거나 삭제해 주세요.",
    sandbox_check_running: "이 클러스터의 격리 점검이 이미 진행 중입니다. 점검이 끝난 뒤에 결과를 확인해 주세요.",
    sandbox_check_stale:
      "격리 점검 결과가 그사이 바뀌었거나 통과한 결과가 아니어서 허용할 수 없습니다. 화면을 새로 고쳐 최신 결과를 확인한 뒤 다시 허용해 주세요.",
    // 코드 실행 Agent 의 배포 검사(P40 7절). 게이트가 닫힌 409 sandbox_gate_closed 는 그 까닭의 문장이다(clusters.sandbox.reason)
    sandbox_limit_range:
      "이 네임스페이스의 LimitRange 가 정한 최대 메모리가 작업 공간 하나에 필요한 양보다 작아 코드 실행을 켠 Agent 를 배포할 수 없습니다. 클러스터 관리자에게 LimitRange 의 최대 메모리를 올려 달라고 요청해 주세요.",
    tokens_active:
      '회수하지 않은 읽기 토큰이 있어 삭제할 수 없습니다. 토큰을 먼저 회수하거나 "토큰을 회수하고 삭제" 를 눌러 주세요.',
    published: "허브에 게시돼 있습니다. 허브에서 게시를 먼저 해제해 주세요.",
    not_published: "허브에 게시돼 있지 않습니다. 먼저 게시해 주세요.",
    home_cluster: "home 클러스터는 삭제할 수 없습니다. 플랫폼이 직접 관리하는 클러스터입니다.",
    state_conflict:
      "그 사이에 상태가 바뀌어 이 작업을 할 수 없습니다. 화면을 새로 고쳐 지금 상태를 확인한 뒤에 다시 시도해 주세요.",
    reprice_running: "이미 진행 중인 비용 다시 계산이 있습니다. 끝난 뒤에 다시 시도해 주세요.",
    revision_conflict:
      "그 사이에 이 항목이 바뀌었습니다. 화면을 새로 고쳐 바뀐 내용을 확인한 뒤에 다시 시도해 주세요.",
    deleting: "지금 삭제 중이라 바꿀 수 없습니다. 삭제가 끝난 뒤에 다시 시도해 주세요.",
    in_progress: "배포나 배포 해제 같은 다른 작업이 진행 중이라 지금은 할 수 없습니다. 끝난 뒤에 다시 시도해 주세요.",
    indexing: "문서를 인덱싱하는 중입니다. 인덱싱이 끝난 뒤에 다시 시도해 주세요.",
    not_ready:
      "허브 항목이 아직 준비되지 않아 주소가 없습니다. Ready 가 된 뒤에 다시 시도해 주세요.",
    backends_not_ready:
      "백엔드가 아직 Ready 가 아닙니다. 모든 백엔드가 Ready 가 된 뒤에 다시 만들어 주세요.",

    // -- 없는 것 -------------------------------------------------------------------
    not_found: "찾을 수 없습니다. 이미 지워졌을 수 있으니 목록에서 다시 선택해 주세요.",
    unknown_cluster: "선택한 클러스터를 찾을 수 없습니다. 목록에서 다시 선택해 주세요.",
    unknown_data_service: "선택한 Database 를 찾을 수 없습니다. 목록에서 다시 선택해 주세요.",
    unknown_hub_item: "선택한 허브 항목을 찾을 수 없습니다. 허브 목록에서 다시 선택해 주세요.",
    unknown_backend_api: "선택한 Backend API 를 찾을 수 없습니다. 목록에서 다시 선택해 주세요.",
    unknown_spec_version: "Backend API 에 그 버전이 없습니다. 있는 버전을 선택해 주세요.",
    unknown_mcp_server:
      "연결된 MCP Server 를 찾을 수 없습니다. 그 MCP Server 가 삭제됐는지 확인해 주세요.",
    source_not_found:
      "원본 Backend API 나 버전을 찾을 수 없습니다. 목록에서 다시 선택해 주세요.",
    session_not_found: "세션을 찾을 수 없습니다. 새 대화를 시작해 주세요.",
    secret_missing: "필요한 Secret 이 클러스터에 없습니다. Secret 을 다시 만들거나 이름을 확인해 주세요.",
    instance_token_missing:
      "토큰 Secret 이 없습니다. 다시 배포한 뒤에 시도해 주세요.",

    // -- 이름·중복 -----------------------------------------------------------------
    name_conflict: "같은 이름이 이미 있습니다. 다른 이름을 적어 주세요.",
    name_immutable: "이름은 만든 뒤에 바꿀 수 없습니다. 표시 이름만 바꿀 수 있습니다.",
    invalid_name: "이름이 규칙에 맞지 않습니다. 칸 옆의 이름 규칙에 맞게 고쳐 주세요.",
    invalid_display_name: "표시 이름이 규칙에 맞지 않습니다. 칸 옆의 이름 규칙에 맞게 고쳐 주세요.",
    reserved_name: "'home' 은 플랫폼이 예약한 클러스터 이름입니다. 다른 이름을 적어 주세요.",
    k8s_name_conflict:
      "같은 이름의 쿠버네티스 리소스가 이미 있습니다. 다른 이름을 적어 주세요.",
    host_conflict:
      "이 호스트는 이미 다른 배포에서 사용하고 있습니다. 호스트 하나는 모든 클러스터를 통틀어 한 곳에만 노출할 수 있습니다. 다른 호스트 이름을 적어 주세요.",
    host_reserved: "이 호스트는 플랫폼이 사용하는 예약 호스트입니다. 다른 호스트 이름을 적어 주세요.",
    duplicate_backend: "같은 허브 항목을 백엔드로 두 번 선택했습니다. 하나만 남겨 주세요.",
    duplicate_backend_name: "이름이 같은 허브 항목 두 개를 백엔드로 선택해 워크로드 이름이 겹칩니다. 하나를 빼거나 이름이 다른 허브 항목을 선택해 주세요.",
    duplicate_env_prefix: "바인딩의 env_prefix 가 겹칩니다. 바인딩마다 다른 접두사를 적어 주세요.",
    duplicate_mapping: "같은 그룹과 테넌트 짝이 두 번 있습니다. 중복된 줄을 지워 주세요.",
    duplicate_sub: "같은 사용자가 두 번 있습니다. 중복된 줄을 지워 주세요.",
    duplicate_tools: "선택한 Tool 목록에 같은 Tool 이 두 번 있습니다. 중복을 지워 주세요.",
    duplicate_tool_name:
      "오버라이드를 적용하면 Tool 이름이 겹칩니다. 겹치는 Tool 의 이름을 서로 다르게 바꿔 주세요.",

    // -- 폼 입력 -------------------------------------------------------------------
    empty_patch: "바뀐 것이 없습니다. 값을 하나라도 바꾼 뒤에 저장해 주세요.",
    no_changes: "적용할 변경이 없습니다. 변경 항목을 하나라도 선택한 뒤에 적용해 주세요.",
    invalid_auth:
      "사용할 수 없는 인증 방식입니다. 목록에 있는 방식 중에서 선택해 주세요. wsse 는 SOAP 백엔드에만 사용할 수 있습니다.",
    credential_required:
      "선택한 인증 방식에는 자격 증명 Secret 이 필요합니다. credential_secret 에 Secret 이름을 적어 주세요.",
    invalid_credential_secret:
      "자격 증명 Secret 이름이 규칙에 맞지 않습니다. 쿠버네티스 Secret 이름 규칙대로 적어 주세요.",
    invalid_soap_headers_secret: "SOAP 헤더 Secret 이름이 규칙에 맞지 않습니다. 쿠버네티스 Secret 이름 규칙대로 적어 주세요.",
    invalid_endpoint: "endpoint 가 http(s) 주소가 아닙니다. http 또는 https 로 시작하는 주소를 적어 주세요.",
    invalid_backends:
      "백엔드 지정이 잘못됐습니다. backends 목록에 백엔드를 하나 이상 넣고, 예전 단일 필드는 빼고 보내 주세요.",
    no_backends: "Gateway 에는 백엔드가 최소 하나 필요합니다. 허브에서 백엔드를 선택해 주세요.",
    invalid_scope: "scope 형식이 잘못됐습니다. kind:id 꼴의 문자열 목록으로 적어 주세요.",
    invalid_role: "알 수 없는 역할입니다. 목록에 있는 역할 중에서 선택해 주세요.",
    invalid_tenant: "테넌트 이름이 규칙에 맞지 않습니다. 이름을 확인해 주세요.",
    invalid_tool_name: "Tool 이름이 규칙에 맞지 않습니다. 이름 오버라이드를 규칙에 맞게 고쳐 주세요.",
    unknown_tools:
      "백엔드가 제공하지 않는 Tool 을 선택했습니다. Tool 목록을 새로 고친 뒤에 다시 선택해 주세요.",
    invalid_format: "format 값이 잘못됐습니다. yaml 이나 json 중에서 선택해 주세요.",
    invalid_input:
      "보낸 내용의 형태가 맞지 않습니다. 파일을 선택하거나 본문 텍스트를 넣어 주세요.",
    invalid_request:
      "함께 보낼 수 없는 필드가 섞여 있습니다. 화면 안내대로 하나만 선택해 다시 보내 주세요.",
    invalid_yaml: "YAML 이 올바르지 않습니다. 매핑 하나로 된 YAML 로 고쳐 주세요.",
    invalid_minify_options: "축소 옵션이 잘못됐습니다. 옵션 값을 확인해 주세요.",
    invalid_kind:
      "이 종류의 허브 항목은 직접 만들 수 없습니다. MCP Server 나 Gateway 를 게시하는 방법으로 만들어 주세요.",

    // -- 클러스터·네임스페이스·노출 -------------------------------------------------
    cluster_unreachable:
      "클러스터에 연결하지 못했습니다. 클러스터 상태와 kubeconfig 를 확인한 뒤에 다시 시도해 주세요.",
    // 상태 조회가 클러스터에서 거절당했다. 사용자의 권한이 아니라 플랫폼 계정의 RBAC 문제다 (P71 2.4절)
    cluster_forbidden:
      "클러스터가 플랫폼의 요청을 거부했습니다. 클러스터 관리자에게 플랫폼 계정의 권한을 확인해 달라고 요청해 주세요.",
    cluster_mismatch:
      "선택한 리소스가 서로 다른 클러스터에 있습니다. 같은 클러스터의 리소스로 맞춰 주세요.",
    // -- 플랫폼 웹 Tool (P39b) -----------------------------------------------------------
    platform_item: "플랫폼 Tool 은 삭제하거나 새로고침할 수 없습니다. Platform Settings 에서 켜거나 끌 수 있습니다.",
    platform_tool_disabled: "이 플랫폼 Tool 은 꺼져 있습니다. 관리자에게 Platform Settings 에서 켜 달라고 요청해 주세요.",
    platform_tool_not_exposable:
      "외부에 노출한 Gateway 에는 플랫폼 웹 Tool 을 넣을 수 없습니다. 노출을 끄거나, 관리자에게 Platform Settings 의 '노출한 Gateway 에서도 허용' 을 켜 달라고 요청해 주세요.",
    web_gateway_unknown_names:
      "웹 Tool 을 넣은 Gateway 는 모든 백엔드의 Tool 이름을 알아야 합니다. Tool 이름을 모르는 허브 항목에서 Tool 새로고침을 실행하거나, 그 백엔드의 Tool 을 직접 선택해 주세요.",
    web_backend_unpinned_agents:
      "이 Gateway 를 사용하는 Agent 가운데 Tool 목록이 고정되지 않은 Agent 가 있습니다. 그 Agent 를 먼저 다시 배포한 뒤에 웹 Tool 을 넣어 주세요.",
    web_backend_unknown_pins:
      "이 Gateway 를 사용하는 Agent 가운데 Tool 이름을 알 수 없을 때 배포한 Agent 가 있습니다. 그 Agent 에서 Tool 을 직접 선택하거나, 웹 Tool 없이 Gateway 를 한 번 배포하고 그 Agent 를 다시 배포한 뒤에 웹 Tool 을 넣어 주세요.",
    web_tools_unknown_names:
      "웹 Tool 과 함께 사용하는 Tool 소스의 Tool 이름을 알 수 없습니다. 이 노드에서 Tool 을 직접 선택하거나, 허브 항목에서 Tool 새로고침을 실행해 주세요.",
    domain_is_public_suffix:
      "com·co.kr·github.io 같은 공개 접미사는 도메인 목록에 넣을 수 없습니다. 그 아래의 구체적인 도메인을 적어 주세요.",
    invalid_ca_bundle: "CA bundle 은 PEM 인증서여야 하고 64 KiB 를 넘을 수 없습니다. 인증서 내용을 확인해 주세요.",
    cluster_not_allowed:
      "이 테넌트에 허용되지 않은 클러스터입니다. Tenant Settings 의 허용 목록에 있는 클러스터를 선택하거나 관리자에게 추가를 요청해 주세요.",
    // 권한 게이트(P50 G8·G10)와 테넌트 Secret 을 쓰는 길(P63)이 같은 코드를 낸다. 고치는 길도 같다
    cluster_rights_missing:
      "이 클러스터에는 플랫폼이 테넌트 네임스페이스에서 사용하는 권한(ClusterRole daegam-tenant-objects)이 없습니다. 클러스터 관리자에게 최신 rbac.yaml 을 적용하고 preflight 를 다시 실행해 달라고 요청해 주세요.",
    no_cluster:
      "쓸 클러스터를 정하지 못했습니다. 클러스터를 선택하거나 Tenant Settings 에서 기본 클러스터를 정해 주세요.",
    no_home_cluster:
      "자격 증명을 저장할 home 클러스터가 없습니다. 플랫폼 관리자에게 home 클러스터 등록을 요청해 주세요.",
    namespace_not_allowed:
      "이 테넌트에 허용되지 않은 네임스페이스입니다. Tenant Settings 의 허용 목록에서 선택해 주세요.",
    namespace_no_longer_allowed:
      "이 배포의 네임스페이스가 테넌트의 허용 목록에서 빠져 다시 배포할 수 없습니다. Tenant Settings 의 허용 목록에 다시 추가하거나, 배포를 해제한 뒤 새로 배포해 주세요.",
    namespace_claimed:
      "이 네임스페이스는 다른 테넌트가 쓰고 있습니다. 다른 네임스페이스를 선택해 주세요.",
    ingress_disabled:
      "이 클러스터는 외부 노출을 허용하지 않습니다. 관리자에게 클러스터의 노출 허용을 요청해 주세요.",
    ingress_class_not_allowed:
      "이 IngressClass 는 클러스터의 허용 목록에 없습니다. 허용된 것을 선택하거나 관리자에게 추가를 요청해 주세요.",
    unknown_ingress_class:
      "이 클러스터에 그 IngressClass 가 없습니다. 실제로 있는 IngressClass 를 선택해 주세요.",
    unknown_issuer:
      "이 클러스터에 그 ClusterIssuer 가 없습니다. 있는 ClusterIssuer 를 선택하거나 관리자에게 생성을 요청해 주세요.",
    cert_manager_unavailable:
      "이 클러스터에 cert-manager 가 없어 자동 발급을 사용할 수 없습니다. TLS Secret 을 직접 지정하거나 관리자에게 cert-manager 설치를 요청해 주세요.",
    tls_secret_invalid:
      "TLS Secret 이 없거나 형식이 맞지 않습니다. 같은 네임스페이스의 kubernetes.io/tls Secret 이름을 적어 주세요.",
    invalid_host: "호스트 이름이 규칙에 맞지 않습니다. 소문자 DNS-1123 이름으로 적어 주세요.",
    invalid_cors_origin:
      "CORS 허용 출처 형식이 맞지 않습니다. 경로 없이 https://example.com 꼴로 적어 주세요.",
    unauthenticated_exposure:
      "이대로 노출하면 외부 경로가 인증 없이 열립니다. 외부 노출은 oidc 모드에서만 할 수 있으니 플랫폼 관리자에게 문의해 주세요.",
    redis_required:
      "레플리카가 둘 이상이면 세션 저장소로 Redis 가 필요합니다. Redis 연결을 추가하거나 클러스터 기본 Redis 를 정해 주세요.",
    locked_field: "플랫폼이 관리하는 필드라 바꿀 수 없습니다. 오버라이드에서 빼 주세요.",
    ignored_by_operator:
      "이 필드는 ToolHive proxyrunner 가 덮어써서 반영되지 않습니다. 오버라이드에서 빼 주세요.",
    runtime_timeout: "Agent 런타임이 제때 답하지 않았습니다. 잠시 뒤에 다시 시도해 주세요.",
    runtime_unreachable:
      "Agent 런타임에 연결하지 못했습니다. Agent 가 Ready 인지 확인한 뒤에 다시 시도해 주세요.",
    // 런타임이 도구 서버보다 먼저 떠 아직 도구를 준비하지 못했다(T18). 런타임이 배경에서 다시 읽는다. 플랫폼 경로는 Ready
    // 파드로만 가므로 사람이 볼 일은 드물다
    tools_unavailable: "Agent 가 아직 Tool 을 준비하지 못했습니다. MCP Server·Gateway 가 Ready 인지 확인한 뒤에 다시 시도해 주세요.",
    // 런타임이 Database·Redis 보다 먼저 떠 아직 연결하지 못했다(T22). 다시 연결하다 드러난 접속 거부(T23)도 같은 코드이고
    // 이유는 에이전트 상태 메시지에 있다(T21). 위와 같이 사람이 볼 일은 드물다
    dependencies_unavailable:
      "Agent 가 아직 Database 에 연결하지 못했습니다. Database 가 Ready 인지 확인하고 Agent 상태 메시지에 적힌 이유를 살펴본 뒤에 다시 시도해 주세요.",
    // 배포 전 검사(422)와 init 받기(409), 런타임 기동 검사(503)가 같은 코드를 낸다(D65)
    runtime_image_too_old:
      "이 Agent 가 사용하는 기능을 런타임 이미지가 지원하지 않습니다. 클러스터의 에이전트 이미지를 새 판으로 올린 뒤에 다시 배포해 주세요.",
    redeploy_required:
      "이 Agent 의 그래프에 웹 Tool 이 생겼지만 Tool 목록을 고정하지 않고 배포한 노드가 있습니다. Agent 를 다시 배포해 주세요.",

    // -- 권한·테넌트 ---------------------------------------------------------------
    forbidden: "이 작업을 할 권한이 없습니다. 관리자에게 권한을 요청해 주세요.",
    hub_item_not_consumable:
      "이 허브 항목을 백엔드로 쓸 권한이 없습니다. 항목 소유자에게 사용 권한을 요청해 주세요.",
    // 뒤 판이 쓴 칸을 든 항목의 렌더·매니페스트·접근 정책 저장·복제본 (P55 9절)
    hub_item_newer:
      "이 항목은 더 새 플랫폼 버전에서 만든 것이라 지금 버전으로는 배포하거나 접근 정책을 바꿀 수 없습니다. 플랫폼을 이 항목을 만든 버전 이상으로 올린 뒤 다시 시도해 주세요.",
    // 허브 image 항목의 허용과 거부 (P55 5·6절). 명세의 화면 문구 표 그대로다
    hub_images_unset:
      "이미지 서명 확인 설정이 없어 카탈로그 이미지를 가져올 수 없습니다. 플랫폼 관리자에게 Platform Settings 의 허브 이미지 설정을 요청해 주세요.",
    image_not_admitted: "이 이미지의 서명을 확인할 수 없습니다. 플랫폼 관리자가 사내 레지스트리로 옮기고 서명했는지 확인해 주세요.",
    image_catalog_conflict:
      "이 이미지에 서로 다른 카탈로그 서버를 가리키는 서명이 있어 등록할 수 없습니다. 플랫폼 관리자에게 확인을 요청해 주세요.",
    image_signatures_too_many: "이 이미지에 서명이 너무 많아 모두 확인할 수 없습니다. 플랫폼 관리자에게 확인을 요청해 주세요.",
    image_outside_registry: "허브 이미지는 Platform Settings 에 정한 사내 레지스트리 아래에 있어야 합니다.",
    image_refused: "플랫폼 관리자가 거부한 이미지라 등록할 수 없습니다.",
    refused_image_not_shareable: "플랫폼 관리자가 거부한 이미지의 항목은 다른 테넌트에 더 열 수 없습니다.",
    hub_backend_refused:
      "플랫폼 관리자가 이미지를 거부한 허브 항목은 백엔드로 사용할 수 없습니다. 편집에서 그 백엔드를 빼고 다시 저장해 주세요.",
    registry_unreachable: "사내 레지스트리에 연결하지 못해 이미지를 확인할 수 없습니다. 잠시 뒤 다시 시도해 주세요.",
    registry_credential_refused:
      "사내 레지스트리가 읽기 자격 증명을 거절했습니다. 플랫폼 관리자에게 Platform Settings 의 허브 이미지 자격 증명을 확인해 달라고 요청해 주세요.",
    hub_images_secret_unavailable: "사내 레지스트리의 읽기 자격 증명을 지금 읽을 수 없어 이미지를 확인할 수 없습니다. 잠시 뒤 다시 시도해 주세요.",
    hub_prefix_overlap:
      "허브 이미지 경로가 클러스터의 제품 이미지 경로와 겹쳐 저장할 수 없습니다. 제품 이미지와 겹치지 않는 경로를 적어 주세요.",
    catalog_settings_admin_only:
      "카탈로그와 다른 Args 나 Env 이름은 플랫폼 관리자만 사용할 수 있습니다. 플랫폼 관리자에게 등록을 요청해 주세요.",
    catalog_server_admin_only:
      "이 카탈로그 서버는 지금 카탈로그에 없거나 인자에 채울 값이 있어 플랫폼 관리자만 등록할 수 있습니다. 플랫폼 관리자에게 등록을 요청해 주세요.",
    secret_not_shareable: "Secret env 가 있는 항목은 다른 테넌트에 열 수 없습니다.",
    env_reserved: "플랫폼이나 ToolHive 가 사용하거나 실행할 코드를 바꿀 수 있는 Env 이름이라 사용할 수 없습니다. 다른 이름을 사용해 주세요.",
    env_must_be_secret: "카탈로그가 비밀 값으로 표시한 변수라 Secret env 에 넣어 주세요.",
    env_required_missing: "카탈로그가 꼭 필요하다고 표시한 변수가 빠졌습니다. 그 변수를 Env 나 Secret env 에 넣어 주세요.",
    cross_tenant_unsupported:
      "이 종류의 허브 항목은 다른 테넌트에 열 수 없습니다. 공개 대상에서 다른 테넌트를 빼 주세요.",
    // 외부 MCP 서버 항목의 인증 (P86 6.1절)
    outside_header_reserved:
      "이 헤더 이름은 Gateway 나 MCP 가 직접 정하는 이름이라 사용할 수 없습니다. 다른 헤더 이름을 입력해 주세요.",
    outside_header_value_invalid: "헤더 값에 제어 문자가 있거나 8 KiB 를 넘습니다. 값을 확인한 뒤 다시 입력해 주세요.",
    outside_auth_not_https:
      "인증을 붙이는 허브 항목의 URL 은 https 여야 합니다. 이 테넌트의 네임스페이스에 있는 Service 주소만 예외입니다. https 주소를 입력해 주세요.",
    outside_oauth_unavailable: "사람마다 계정을 연결하는 인증은 아직 사용할 수 없습니다. none 이나 header 를 선택해 주세요.",
    outside_sse_unsupported:
      "SSE 방식으로만 동작하는 외부 MCP 서버는 등록할 수 없습니다. 서버의 streamable-http 주소로 등록해 주세요.",
    entry_address_refused:
      "플랫폼이 이 주소로 연결하지 않습니다. 공개 주소나 이 테넌트의 네임스페이스에 있는 Service 주소를 등록하거나, 사내망 주소라면 관리자에게 허용 목록에 넣어 달라고 요청해 주세요.",
    not_session_owner: "다른 사용자의 세션입니다. 자기 세션에서 다시 시도해 주세요.",
    tenant_mismatch: "다른 테넌트의 세션입니다. 테넌트를 바꾼 뒤에 다시 열어 주세요.",
    agent_oauth_missing:
      "Gateway 를 Tool 로 쓰려면 Tenant Settings 의 agent_oauth 가 필요합니다. Tenant Settings 에서 먼저 채워 주세요.",

    // -- 개인 MCP 토큰 (P32) -------------------------------------------------------
    // oidc 모드에서 오래 가는 자격을 만들 때 마지막 IdP 로그인이 10분보다 오래됐다 (P53b 6.4절)
    reauth_required: "보안을 위해 IdP 에서 다시 로그인해야 합니다. 다시 로그인한 뒤 한 번 더 시도해 주세요.",
    not_a_member: "이 테넌트의 멤버가 아니라 토큰을 받을 수 없습니다. 멤버인 테넌트를 선택해 주세요.",
    token_limit:
      "개인 MCP 토큰 한도를 넘었습니다. 쓰지 않는 토큰을 회수하거나 잠시 뒤에 다시 발급해 주세요.",

    // -- Skill (D64) --------------------------------------------------------------
    // 형식 검사의 줄마다 문장은 lib/skills.ts 의 SKILL_ISSUE_TEXT 이고 여기는 요청 전체의 문장이다
    // 직접 작성의 저장과 가져오기가 같은 코드를 받는다. 문제는 직접 작성에서는 파일 옆에, 가져오기에서는 이 문장 아래에 선다
    invalid_skill: "Skill 파일이 형식에 맞지 않습니다. 표시된 문제를 고친 뒤에 다시 시도해 주세요.",
    skill_unchanged: "마지막 리비전과 내용이 같아 새 리비전을 만들지 않았습니다. 파일을 고친 뒤에 다시 저장해 주세요.",
    skill_limit: "이 테넌트가 가질 수 있는 Skill 수를 넘었습니다. 쓰지 않는 Skill 을 삭제한 뒤에 다시 만들어 주세요.",
    skill_quota_exceeded:
      "이 테넌트의 Skill 리비전 용량을 넘었습니다. 쓰지 않는 Skill 을 삭제하거나 관리자에게 Platform Settings 의 한도를 올려 달라고 요청해 주세요.",
    cross_tenant_manage_unsupported: "manage 권한은 소유 테넌트에만 줄 수 있습니다. manage 의 테넌트 목록에서 다른 테넌트를 빼 주세요.",
    skill_not_consumable:
      "이 Skill 을 Agent 에 붙일 권한이 없습니다. Skill 소유자에게 사용 권한을 요청하거나 Agent 에서 그 Skill 을 빼 주세요.",

    // -- 허브·게이트웨이 -----------------------------------------------------------
    cycle: "백엔드를 따라가면 이 Gateway 자신으로 돌아옵니다. 순환에 든 백엔드를 빼 주세요.",
    published_oidc_unsupported:
      "Gateway 가 아직 IdP 토큰만 받는 동안에는 게시된 Gateway 를 백엔드로 사용할 수 없습니다. 다른 백엔드를 선택하거나, 플랫폼 관리자가 플랫폼 토큰으로 전환한 뒤에 다시 시도해 주세요.",
    published_owner_forbid:
      "이 게시된 Gateway 는 Authz policy (Cedar) 에 forbid 문장이 있어 백엔드로 사용할 수 없습니다. forbid 문장은 다른 Gateway 를 거쳐 오는 사람에게 제대로 적용되지 않기 때문입니다. 원래 Gateway 를 관리하는 사람에게 조건을 Tool 규칙으로 옮겨 달라고 요청하거나 다른 백엔드를 선택해 주세요.",
    published_script_tool:
      "게시된 Gateway 의 execute_tool_script 는 다른 Gateway 에서 선택할 수 없습니다. 스크립트 안의 호출에는 이 Gateway 의 Tool 규칙이 적용되지 않기 때문입니다. 선택에서 execute_tool_script 를 빼 주세요.",
    published_names_unknown:
      "이 게시된 Gateway 가 제공하는 Tool 이름을 알 수 없어 백엔드로 사용할 수 없습니다. 원래 Gateway 의 백엔드에서 Tool 새로고침을 실행하고 그 Gateway 를 다시 배포한 뒤 다시 시도해 주세요.",
    published_proxy_unsupported:
      "proxy 모드로 게시된 Gateway 는 백엔드로 사용할 수 없습니다. 원래 Gateway 를 관리하는 사람에게 entry 모드로도 게시해 달라고 요청한 뒤 그 항목을 선택하거나, 다른 백엔드를 선택해 주세요.",
    tools_list_failed:
      "백엔드에서 Tool 목록을 가져오지 못했습니다. 항목이 Ready 인지 확인한 뒤에 Tool 목록을 새로 고쳐 주세요.",
    // Code mode 를 켠 Gateway 의 저장·미리보기·배포 검사 (P56 6.2절, services/gateway_code_mode.py). toolhive_old 는 배포만 낸다
    code_mode_not_read_only:
      "이 Gateway 가 제공하는 Tool 가운데 읽기 전용이 아닌 Tool 이 있어 Code mode 를 켤 수 없습니다. 그 Tool 을 백엔드의 Tool 선택에서 빼거나 서버나 허브 항목에서 읽기 전용으로 표시해 주세요. 웹 Tool 과 remote·entry 항목의 Tool 은 읽기 전용으로 표시할 수 없으므로 빼야 합니다.",
    code_mode_unknown_names:
      "Tool 이름을 알 수 없는 백엔드가 있어 Code mode 를 켤 수 없습니다. 그 허브 항목에서 Tool 새로고침을 실행하거나 그 백엔드의 Tool 을 직접 선택해 주세요.",
    code_mode_reserved_name:
      "이 Gateway 가 제공하는 Tool 가운데 이름이 execute_tool_script 인 Tool 이 있어 Code mode 를 켤 수 없습니다. 그 백엔드에서 이 Tool 을 뺀 나머지 Tool 을 선택하거나 Conflict resolution 을 prefix 로 바꿔 주세요.",
    code_mode_exposed: "외부에 노출한 Gateway 에서는 Code mode 를 켤 수 없습니다. 노출을 끄거나 Code mode 를 끈 뒤 다시 저장해 주세요.",
    code_mode_owner_cedar:
      "이 Gateway 는 Advanced 탭에 저장한 Cedar 정책을 그대로 사용하고 있어 Code mode 를 켤 수 없습니다. Advanced 탭에서 그 정책을 지운 뒤 다시 저장해 주세요.",
    code_mode_toolhive_old:
      "이 클러스터의 ToolHive 가 Code mode 를 지원하지 않아 Code mode 를 켠 Gateway 를 배포할 수 없습니다. 플랫폼 관리자에게 ToolHive 업그레이드를 요청해 주세요.",

    // -- LLM provider --------------------------------------------------------------
    no_provider:
      "등록된 LLM provider 가 없습니다. 플랫폼 관리자에게 Platform Settings 의 provider 등록을 요청해 주세요.",
    unknown_provider:
      "등록되지 않은 LLM provider 입니다. Platform Settings 에 등록된 provider 를 선택해 주세요.",
    provider_unavailable:
      "청지기가 사용할 LLM provider 가 없습니다. 플랫폼 관리자에게 Platform Settings 의 provider 등록을 요청해 주세요.",
    provider_not_supported:
      "이 provider 는 stub 이라 스펙을 만들 수 없습니다. 실제 provider 를 선택해 주세요.",
    stub_provider_disallowed:
      "이 배포본은 stub provider 를 허용하지 않습니다. 실제 provider 를 등록해 주세요.",
    unknown_kind: "지원하지 않는 provider 종류입니다. 지원하는 종류로 바꿔 주세요.",
    no_secret: "provider 의 키 Secret 을 찾지 못했습니다. Platform Settings 에서 Secret 이름을 확인해 주세요.",
    no_api_key:
      "Secret 안에 API 키가 없습니다. Platform Settings 에서 키를 담은 Secret 을 다시 지정해 주세요.",
    provider_no_key:
      "provider 의 API 키를 읽지 못했습니다. Platform Settings 에서 키 Secret 을 확인해 주세요.",
    no_secret_store:
      "키 Secret 을 읽을 home 클러스터가 없습니다. 플랫폼 관리자에게 home 클러스터 연결을 요청해 주세요.",
    auth: "provider 가 키를 거부했습니다. Platform Settings 에서 키 값이 맞는지 확인해 주세요.",
    connection: "provider 에 연결하지 못했습니다. endpoint 주소와 네트워크를 확인해 주세요.",
    provider_error:
      "provider 가 오류를 돌려줬습니다. 잠시 뒤에 다시 시도하고, 계속되면 provider 설정을 확인해 주세요.",
    // LLM 게이트웨이의 세 코드 (P38 7절). 채팅 error 이벤트, 비서, 스펙 job, Knowledge 문서 오류가 같은 코드를 낸다
    llm_gateway_unreachable:
      "LLM 게이트웨이에 연결하지 못했습니다. 잠시 뒤에 다시 시도하고, 계속되면 플랫폼 관리자에게 게이트웨이 상태를 확인해 달라고 요청해 주세요.",
    llm_gateway_denied: "LLM 게이트웨이가 호출을 거부했습니다. 토큰이 만료됐거나 회수됐을 수 있습니다. 계속되면 플랫폼 관리자에게 문의해 주세요.",
    llm_gateway_rate_limited: "LLM 게이트웨이의 분당 토큰 한도에 도달했습니다. 1분쯤 뒤에 다시 시도해 주세요.",
    // 게이트웨이 경로에서 플랫폼이 호출마다 낼 토큰을 서명하지 못했다 (P38 7절). 다시 보내지 않는다
    no_signer: "플랫폼에 서명 키가 없어 LLM 게이트웨이 토큰을 낼 수 없습니다. 플랫폼 관리자에게 문의해 주세요.",
    // LLM 토큰 교체(P38 5절). 직접 경로의 Agent 는 교체할 토큰이 없다
    llm_token_missing: "이 Agent 는 LLM provider 를 바로 부르고 있어 교체할 LLM 토큰이 없습니다. 게이트웨이 경로로 옮긴 뒤에 다시 시도해 주세요.",
    // LLM provider 가 요청 한도로 429 를 줬다. Spec Generator 가 재시도를 다 쓴 뒤에 남는다
    provider_rate_limited: "LLM provider 의 요청 한도를 넘었습니다. 잠시 뒤에 다시 시도해 주세요.",
    // 에이전트 런타임의 사용자별 분당 요청 한도(429, 공개 API 코드)다. provider 한도와는 다르다
    rate_limited: "짧은 시간에 요청을 너무 많이 보냈습니다. 잠시 뒤에 다시 시도해 주세요.",
    // 크레딧을 다 썼거나 사용 한도에 도달했다(T20). 기다려도 풀리지 않으므로 provider_rate_limited 와 가른다. Spec
    // Generator·비서·Knowledge 인덱서와 Agent 턴이 같은 코드를 낸다. 연결 확인 결과는 CHECK_MESSAGES 가 맡는다
    quota_exhausted:
      "LLM provider 의 크레딧을 다 썼거나 사용 한도에 도달했습니다. provider 콘솔에서 결제 정보와 한도를 확인한 뒤에 다시 시도해 주세요.",
    refusal: "모델이 요청을 거절했습니다. 지시문을 바꿔 다시 시도해 주세요.",
    empty_response: "stub provider 에 준비된 응답이 없습니다. 실제 provider 를 선택해 주세요.",

    // -- 스펙 파이프라인 (Backend API 업로드·다듬기) ----------------------------
    too_large: "파일이 업로드 한도보다 큽니다. 더 작게 나눠서 업로드해 주세요.",
    input_too_large: "입력이 한도보다 큽니다. 더 짧게 줄여 주세요.",
    source_too_large: "다듬을 스펙이 한도보다 큽니다. 스펙을 줄이거나 나눠 주세요.",
    unsupported:
      "WSDL·Swagger 2.0·OpenAPI 3.x 로 알아볼 수 없는 문서입니다. 형식을 확인하고 다시 업로드해 주세요.",
    conversion:
      "스펙을 OpenAPI 로 변환하지 못했습니다. 원본 문서가 올바른 WSDL·Swagger·OpenAPI 인지 확인해 주세요.",
    mixed:
      "한 스펙에 SOAP 오퍼레이션과 REST 오퍼레이션이 섞여 있습니다. 두 스펙으로 나눠서 업로드해 주세요.",
    verify: "스펙 검증이 실패했습니다. 검증 결과에서 실패한 항목을 고쳐 주세요.",
    verify_incomplete: "검증을 일부 건너뛰어 끝내지 못했습니다. 관리자에게 알려 주세요.",
    verdict_error:
      "스펙이 검증을 통과하지 못했습니다. 검증 결과의 오류를 고친 뒤에 다시 저장해 주세요.",
    source_not_verified:
      "원본 버전의 검증에 오류가 있습니다. 검증 오류를 먼저 고친 뒤에 다듬어 주세요.",
    source_mismatch:
      "다듬기 결과의 원본이 지금 버전과 다릅니다. 이 버전에서 다듬기를 다시 실행해 주세요.",
    surface_changed: "바꿀 수 없는 필드가 바뀌었습니다. 그 변경을 빼고 다시 적용해 주세요.",
    unknown_change:
      "목록에 없는 변경 항목을 선택했습니다. 화면을 새로 고쳐 변경 목록을 다시 받아 주세요.",
    empty_input: "입력이 비어 있습니다. 텍스트·URL·예시·부분 스펙 중 하나는 채워 주세요.",
    empty_instruction: "다듬을 내용이 없습니다. 프리셋을 하나 선택하거나 지시문을 적어 주세요.",
    integrity_error: "저장된 데이터가 서로 맞지 않습니다. 관리자에게 알려 주세요.",
    job_active: "작업이 아직 진행 중입니다. 끝난 뒤에 다시 시도해 주세요.",
    job_not_done: "다듬기 작업이 아직 끝나지 않았습니다. 끝난 뒤에 적용해 주세요.",
    job_not_found: "다듬기 작업을 찾을 수 없습니다. 다듬기를 다시 실행해 주세요.",
    job_not_refine: "다듬기로 만든 결과만 적용할 수 있습니다. 다듬기를 실행한 뒤에 적용해 주세요.",
    too_many_jobs:
      "이 테넌트에 진행 중인 작업이 너무 많습니다. 끝난 뒤에 다시 시작해 주세요.",
    url_blocked: "쓸 수 없는 주소입니다. 공개된 http(s) 주소를 적어 주세요.",
    fetch_failed: "주소에서 문서를 받아오지 못했습니다. 주소가 맞는지, 서버가 응답하는지 확인해 주세요.",
    fetch_redirect: "리다이렉트가 너무 많거나 막힌 주소로 갔습니다. 최종 주소를 직접 적어 주세요.",
    fetch_status: "주소의 서버가 오류 상태로 답했습니다. 주소를 확인하고 잠시 뒤에 다시 시도해 주세요.",
    fetch_too_large: "받아온 문서가 업로드 한도보다 큽니다. 더 작은 문서를 쓰거나 필요한 부분만 잘라 주세요.",

    // -- Knowledge -----------------------------------------------------------------
    invalid_data_service_kind:
      "선택한 Database 의 종류가 맞지 않습니다. Knowledge Base 에는 postgres Database 가 필요합니다.",
    data_service_immutable:
      "Database 는 만든 뒤에 바꿀 수 없습니다. 다른 Database 를 쓰려면 Knowledge Base 를 새로 만들어 주세요.",
    embedding_immutable:
      "임베딩 설정은 만든 뒤에 바꿀 수 없습니다. 다른 임베딩을 쓰려면 Knowledge Base 를 새로 만들어 주세요.",
    embedding_not_supported:
      "이 provider 에는 임베딩 API 가 없습니다. 임베딩을 지원하는 provider 를 선택해 주세요.",
    embedding_failed:
      "임베딩을 만들지 못했습니다. provider 키와 모델 이름을 확인한 뒤에 다시 인덱싱해 주세요.",
    dimensions_required:
      "이 임베딩 모델의 차원 수를 플랫폼이 모릅니다. 차원 수를 직접 적어 주세요.",
    pgvector_missing:
      "이 PostgreSQL 에 pgvector 확장이 없습니다. 슈퍼유저에게 CREATE EXTENSION vector 를 먼저 실행해 달라고 요청해 주세요.",
    query_failed: "Database 질의에 실패했습니다. 접속 정보와 계정 권한을 확인해 주세요.",
    too_many_documents:
      "Knowledge Base 의 문서 수 한도를 넘었습니다. 문서를 지운 뒤에 추가해 주세요.",
    // 받는 형식은 lib/limits.ts 의 DOCUMENT_ACCEPT 하나에서 만든다. 손으로 적으면 형식이 늘 때 문구만 남는다
    unsupported_format: `지원하지 않는 문서 형식입니다. ${DOCUMENT_FORMAT_LABEL} 문서로 업로드해 주세요.`,
    // Knowledge Base 의 접속 확인·문서 처리 코드. 예전에는 lib/knowledge.ts 가 따로 표를 가졌는데 같은 코드에
    // 다른 문장이 둘 생겨 여기로 합쳤다(D43). Database 연결 확인 화면(pages/data-services/verifyText.ts)은 그 자리에서
    // 할 일을 말해야 해서 auth_failed·query_failed 를 자기 문장으로 두고, 그 밖의 코드는 이 표로 돌아온다
    db_unreachable:
      "Database 에 연결할 수 없습니다. Host 와 Port, 그리고 플랫폼 네임스페이스에서 나가는 경로(egress)를 확인해 주세요.",
    auth_failed: "Database 인증에 실패했습니다. Database 의 Username 과 New password 를 확인해 주세요.",
    unknown_database: "그 이름의 database 가 서버에 없습니다. Database 칸의 이름을 확인해 주세요.",
    too_many_chunks: "청크 수가 상한을 넘습니다. 문서를 나누거나 limits.max_chunks 를 올려 주세요.",
    table_missing: "벡터 테이블이 Database 에 없습니다. Knowledge Base 를 다시 만들어 주세요.",
    // Knowledge 인덱서와 채팅 첨부의 업로드가 같은 코드를 낸다. 글자가 없는 스캔 문서가 대부분이다
    empty_document: "문서에서 글자를 찾지 못했습니다. 스캔한 문서라면 글자가 든 파일로 바꿔 다시 업로드해 주세요.",
    extract_failed: "문서에서 글을 읽어 내지 못했습니다. 손상되지 않은 파일인지, 글자가 든 파일인지 확인한 뒤 다시 업로드해 주세요.",
    body_missing: "문서 본문이 저장소에 없습니다. 파일을 다시 업로드해 주세요.",
    // 문서 파싱 서비스(daegam-docparse, D61)가 내는 코드. Knowledge 인덱서와 채팅 첨부가 함께 받는다
    encrypted_document: "암호가 걸린 문서라 열 수 없습니다. 암호를 푼 파일로 다시 업로드해 주세요.",
    invalid_archive: "문서 파일의 구조가 올바르지 않습니다. 손상되지 않은 파일인지 확인한 뒤에 다시 업로드해 주세요.",
    archive_too_large: "문서 안의 내용이 너무 커서 읽을 수 없습니다. 문서를 나눠서 업로드해 주세요.",
    extract_timeout: "문서를 읽는 데 30초가 넘게 걸렸습니다. 문서를 나눠서 업로드해 주세요.",
    extract_unavailable: "문서를 읽는 서비스에 연결할 수 없습니다. 잠시 뒤에 다시 시도해 주세요.",
    extract_busy: "문서를 읽는 서비스가 바쁩니다. 잠시 뒤에 다시 시도해 주세요.",
    // 채팅 첨부(P36 9절). 업로드는 플랫폼이, 보낼 때의 검사는 런타임이 한다
    upload_rate_limited: "1분 동안 업로드할 수 있는 파일 수를 넘었습니다. 1분 뒤에 다시 첨부해 주세요.",
    too_many_uploads: "보내지 않은 첨부가 너무 많습니다. 쓰지 않을 첨부를 삭제한 뒤에 다시 첨부해 주세요.",
    upload_busy: "앞서 첨부한 문서를 아직 읽고 있습니다. 잠시 뒤에 다시 첨부해 주세요.",
    unknown_upload: "첨부한 파일을 찾을 수 없습니다. 업로드한 지 1시간이 지나 사라졌을 수 있으니 다시 첨부해 주세요.",
    too_many_attachments: "한 번에 첨부할 수 있는 파일 수를 넘었습니다. 첨부를 줄여 다시 보내 주세요.",
    attachments_too_large: "이 대화에 첨부할 수 있는 크기나 이미지 수를 넘었습니다. 새 대화를 시작해 다시 첨부해 주세요.",
    invalid_attachment: "첨부한 파일을 읽을 수 없습니다. 파일 이름과 형식을 확인한 뒤에 다시 첨부해 주세요.",
    images_unsupported: "이 Agent 의 모델은 이미지를 받지 않습니다. 이미지는 빼고 보내 주세요.",
    attachments_need_filesystem: "이 Agent 는 문서 첨부를 받지 않습니다. Agent 정의에서 파일 Tool 을 켜고 다시 배포하면 사용할 수 있습니다.",
    embedding_dimension_mismatch:
      "임베딩 응답의 차원이 Knowledge Base 의 차원과 다릅니다. 모델을 바꿨다면 Knowledge Base 를 새로 만들어 주세요.",
    url_unreachable:
      "URL 을 받지 못했습니다. 공개 주소(사설·클러스터 주소 제외)이고 10MB 이하인지 확인해 주세요.",

    // -- 비서 ----------------------------------------------------------------------
    assistant_disabled:
      "청지기가 플랫폼 설정에서 꺼져 있습니다. 플랫폼 관리자에게 청지기 활성화를 요청해 주세요.",
    turn_in_progress: "아직 답변이 끝나지 않았습니다. 답변이 끝난 뒤에 보내 주세요.",
    // 에이전트 채팅의 중단 단추(D92). 중지 API 를 받은 런타임이 턴을 멈추고 이 코드로 마감한다. 연결과 떨어져 도는 턴은 연결이
    // 끊겨도 이 코드가 아니다. 옛 이미지는 연결이 끊기면 이 코드로 마감하고, 화면이 끊은 쪽의 문구도 같은 문장이다. 요청을
    // 멈춰도 MCP 서버와 그 뒤 백엔드는 이미 받은 호출을 끝낼 수 있으므로 그 사실을 말한다
    turn_cancelled: "요청을 중단했습니다. 모델 호출은 멈췄고, 이미 보낸 Tool 호출은 Tool 쪽에서 끝났을 수 있습니다.",
    // 파드가 내려가며 턴을 끝까지 잇지 못했다(P78 5절). 파드가 죽어 마감 항목이 없는 턴에 화면이 스스로 붙이기도 한다(4절)
    turn_aborted: "에이전트가 답변을 끝까지 이어 가지 못했습니다. 다시 보내 주세요.",
    // 턴 도중 대화를 시작한 사람이 접근 권한을 잃어 런타임이 턴을 멈췄다(P78 7절). 본인이 읽는 말이라 "사용자의" 를 쓰지 않는다.
    // 한 문장이라 두 문장 규칙의 예외다(test/error-text.test.ts)
    turn_access_lost: "접근 권한을 다시 확인하지 못해 답변을 멈췄습니다.",
    // 턴 위임 토큰(P53 4·6절). 서명이나 위임 기록이 실패해 턴을 시작하지 않았다(503), 런타임이 이 대화의 소유자를 확인하지 못했다(409)
    turn_tokens_unavailable: "사용자 이름으로 Tool 을 부를 토큰을 만들지 못했습니다. 잠시 뒤 다시 보내 주세요.",
    turn_grant_mismatch: "이 대화의 소유자를 확인하지 못해 턴을 시작하지 않았습니다. 문제가 계속되면 플랫폼 관리자에게 알려 주세요.",
    // 중지 API(D65). 화면은 이 답이면 막 끝난 턴의 결과를 보이고(P78 4절), 옛 이미지에서는 연결을 끊으므로 사람이 볼 일은 드물다
    no_turn_in_progress: "지금 진행 중인 답변이 없습니다. 답변이 이미 끝났으니 다음 메시지를 보내 주세요.",
    runtime_feature_missing: "배포된 런타임 이미지가 이 기능을 지원하지 않습니다. 이미지를 올린 뒤 다시 배포해 주세요.",
    // 쓰지 않는 Agent 를 0 으로 줄이기 (P44). 깨우는 중(503, 채팅과 승인 카드는 Retry-After 마다 다시 보낸다), 잠든 Agent 의 읽기,
    // 턴이 리스를 쥐어 재울 수 없음, 잠들 수 없는 Agent (까닭은 배포 탭이 보인다)
    agent_waking: "사용하지 않아 내려 두었던 Agent 의 파드를 다시 띄우는 중입니다. 잠시 뒤에 다시 시도해 주세요.",
    // 게시한 Agent 를 Tool 로 부르는 호출 (P46 6.1·6.2·6.5절)
    agent_source_unpublished: "이 Agent 는 허브에 게시되어 있지 않아 Tool 로 사용할 수 없습니다. 그 Agent 의 상세에서 허브에 게시한 뒤 다시 선택해 주세요.",
    agent_source_no_consume: "이 Agent 항목을 Tool 로 사용할 권한이 없습니다. 항목을 관리하는 사람에게 사용 권한을 요청해 주세요.",
    agent_source_self: "Agent 는 자기 자신을 Tool 로 사용할 수 없습니다. 다른 Agent 를 선택해 주세요.",
    agent_source_runs_unmet: "호출할 Agent 에 memory 노드와 세션 토큰 예산이 있어야 합니다. 그 Agent 의 캔버스와 설정을 확인해 주세요.",
    agent_item_gateway_unsupported: "Agent 항목은 아직 Gateway 의 백엔드로 사용할 수 없습니다. Agent 의 캔버스에서 Tool 로 연결해 주세요.",
    agent_calls_disabled: "이 플랫폼에서 Agent 호출이 꺼져 있습니다. 플랫폼 관리자에게 문의해 주세요.",
    agent_asleep: "이 Agent 는 사용하지 않아 파드를 내려 두었습니다. 깨운 뒤에 다시 시도해 주세요.",
    agent_busy: "Agent 가 답변하는 중이라 지금 파드를 내릴 수 없습니다. 답변이 끝난 뒤 다시 시도해 주세요.",
    agent_sleep_ineligible: "이 Agent 는 0개로 줄일 수 없습니다. 배포 탭에서 그 까닭을 확인해 주세요.",
    // 미리 보기 (P44b 7절, 10절). 테넌트의 자리가 찼다(409), 오늘 시작하고 옮긴 수가 상한이다(429)
    preview_limit: "이 테넌트에서 동시에 열 수 있는 미리 보기 수를 넘었습니다. 다른 미리 보기를 끝낸 뒤 다시 시도해 주세요.",
    preview_start_limit: "오늘 미리 보기를 시작할 수 있는 횟수를 넘었습니다. 내일 다시 시도하거나 배포해서 대화해 주세요.",
    // 플랫폼이 미리 보기를 껐다(409), 열린 미리 보기가 없거나 막 끝났다(404), Agent(미리 보기면 원본)의 하루 토큰 예산을 다 썼다(429)
    preview_disabled: "이 플랫폼에서는 미리 보기를 사용할 수 없습니다. 배포해서 대화해 주세요.",
    preview_not_found: "열린 미리 보기가 없습니다. 미리 보기를 다시 시작해 주세요.",
    daily_budget_exceeded: "이 Agent 가 오늘 사용할 수 있는 토큰을 모두 사용했습니다. 내일 다시 시도해 주세요.",
    // 서비스 어카운트를 끈 설치의 만들기와 발급(409, P53 B)
    service_accounts_off: "이 배포본에서는 서비스 어카운트를 사용하지 않습니다. 다시 사용하려면 플랫폼 관리자에게 문의해 주세요.",
    // 재배포 중이라 아직 새 기능을 모르는 옛 파드가 요청을 받았거나 에이전트가 준비되지 않았다(D61)
    runtime_rolling: "Agent 가 새 버전으로 바뀌는 중이거나 아직 준비되지 않았습니다. 잠시 뒤 다시 시도해 주세요.",
    // 다시 생성(P36). 마지막 턴이 없거나, 한 턴을 하루에 다시 생성할 수 있는 횟수를 다 썼다
    nothing_to_regenerate: "다시 생성할 답변이 없습니다. 메시지를 새로 보내 주세요.",
    max_regenerations: "이 답변은 오늘 다시 생성할 수 있는 횟수를 모두 사용했습니다. 새 메시지로 이어서 질문해 주세요.",
    // 입력 길이(P76). 에이전트의 입력 상한(최대 20만 자)을 넘는 메시지와 거절·응답 결정의 글이다
    max_input_chars: "메시지가 너무 깁니다. 이 Agent 가 받을 수 있는 길이로 줄여 다시 보내 주세요.",
    // 자동 실행(P41). 일정과 트리거를 저장할 때, 지금 실행할 때, 승인 대기 실행을 결정할 때 거절한다
    run_closed: "이 자동 실행은 더 이상 결정을 기다리지 않습니다. 이미 결정되었거나, 중지되었거나, 마감으로 거절되었습니다.",
    runs_unsupported: "이 Agent 의 런타임 이미지는 자동 실행을 지원하지 않습니다. Agent 이미지를 새 판으로 올린 뒤 다시 배포해 주세요.",
    runs_need_memory: "자동 실행에는 memory 노드가 필요합니다. 캔버스에서 memory 노드를 붙인 뒤 다시 시도해 주세요.",
    run_budget_required: "자동 실행에는 세션 토큰 예산이 필요합니다. Agent 설정에서 예산을 정한 뒤 다시 시도해 주세요.",
    runs_need_events: "플랫폼이 Agent 의 이벤트를 받지 않고 있어 자동 실행을 할 수 없습니다. 플랫폼 관리자에게 알려 주세요.",
    trigger_needs_approval:
      "웹훅 트리거를 두려면 이 Agent 가 쓰는 모든 쓰기 Tool 에 승인 규칙이 있어야 합니다. 캔버스에서 승인 규칙을 더한 뒤 다시 시도해 주세요.",
    schedule_too_frequent: "일정은 5분보다 자주 실행할 수 없습니다. 간격을 늘려 주세요.",
    schedule_invalid_cron: "반복 규칙을 읽을 수 없습니다. 다섯 칸 cron 식으로 적어 주세요.",
    run_source_limit: "이 Agent 에 둘 수 있는 일정과 트리거는 각각 20개까지입니다. 쓰지 않는 것을 지운 뒤 다시 시도해 주세요.",
    unknown_time_zone: "알 수 없는 시간대입니다. Asia/Seoul 처럼 IANA 시간대 이름으로 적어 주세요.",
    invalid_expiry: "만료 기간은 1일에서 365일 사이로 정해 주세요.",
    // 대화 이름 바꾸기(P36). 런타임이 공백과 제어 문자를 뺀 뒤 1~100자가 아니면 거절한다
    invalid_title: `대화 이름은 1자 이상 ${SESSION_TITLE_MAX_CHARS}자 이하여야 합니다. 이름을 고쳐 다시 저장해 주세요.`,
    // 대화 삭제(P36). 런타임이 체크포인트를 정한 횟수만큼 지워도 남으면 503 이다. 색인 행이 남아 다시 지우면 이어서 지운다
    session_delete_incomplete: "대화 기록이 많아 한 번에 모두 삭제하지 못했습니다. 다시 삭제해 주세요.",
    // 답변 피드백(P36). 다시 생성으로 바뀐 답변이나 오류로 끝난 답변은 평가할 수 없다
    turn_not_found: "이 답변은 다시 생성됐거나 대화에서 사라져 평가할 수 없습니다. 화면을 새로 고쳐 주세요.",
    feedback_not_found: "이미 취소된 평가입니다. 화면을 새로 고쳐 주세요.",
    // 되돌리기와 평가 (P45 6절~12절). 게이트의 거절 까닭은 배포 대화상자가 agents.evaluation.gate.reasons 로 덧붙인다
    nothing_to_roll_back: "선택한 리비전은 지금 배포된 정의와 같아 되돌릴 것이 없습니다. 다른 리비전을 선택해 주세요.",
    evaluation_required:
      "이 Agent 는 배포 전에 평가를 통과해야 합니다. 이 정의로 평가를 실행해 통과한 뒤 다시 배포하거나, 까닭을 적고 평가 없이 배포해 주세요.",
    evaluation_consent_missing: "대화한 사람이 이 대화를 평가 사례로 사용하는 데 동의하지 않았습니다. 사례를 직접 작성해 주세요.",
    evaluation_case_pii:
      "복사한 질문이나 답변에 이메일 주소, 전화번호, 주민등록번호나 카드 번호로 보이는 내용이 남아 있습니다. 표시한 부분을 지우거나 바꾼 뒤 다시 저장해 주세요.",
    evaluation_run_active: "이 Agent 의 평가가 이미 실행 중입니다. 끝난 뒤 다시 시도해 주세요.",
    evaluation_run_limit: "이 테넌트가 오늘 실행할 수 있는 평가를 모두 실행했습니다. 내일 다시 시도해 주세요.",
    evaluation_set_changed: "확인한 뒤에 평가 세트가 바뀌었습니다. 바뀐 사례를 다시 확인한 뒤 실행해 주세요.",
    evaluation_grader_provider:
      "이 리비전의 모델 노드는 채점 모델의 provider 를 사용하지 않습니다. 이 리비전이 사용하는 provider 의 모델을 채점 모델로 선택해 주세요.",
    evaluation_set_in_gate: "배포 전 평가가 이 세트를 사용하고 있어 지울 수 없습니다. 배포 전 평가를 끄거나 다른 세트로 바꾼 뒤 지워 주세요.",
    evaluations_disabled: "이 플랫폼에서는 평가를 꺼 두었습니다. 플랫폼 관리자에게 문의해 주세요.",
    evaluation_set_full: `평가 세트의 사례 수와 반복 횟수를 곱한 값은 ${EVALUATION_MAX_CASE_RUNS} 이하여야 합니다. 사례를 줄이거나 반복 횟수를 낮춰 주세요.`,
    export_range_too_long: `피드백은 최대 ${FEEDBACK_EXPORT_MAX_DAYS}일 구간까지 내보낼 수 있습니다. 구간을 줄여 다시 내보내 주세요.`,
    export_too_large: "내보낼 피드백이 너무 많습니다. 평가 필터로 범위를 좁혀 다시 내보내 주세요.",
    // 인용 원문(P36 7절). 그 대화에서 인용하지 않았거나 배포된 Knowledge Base 의 문서가 아니면 404 이고, 도구 결과나 답을 가리는
    // 에이전트는 원문을 열지 않는다
    citation_not_found: "이 출처의 원문을 찾을 수 없습니다. 문서가 삭제됐거나 Agent 가 다시 배포됐을 수 있으니 화면을 새로 고쳐 주세요.",
    source_masked: "이 Agent 는 가림 규칙이 있어 원문을 열 수 없습니다. 출처의 문서 이름과 발췌로 내용을 확인해 주세요.",
    // 파일 경로 검사와 크기 상한(P36). 장기 기억 파일(내용 보기)과 세션 파일(다운로드)이 같은 코드를 쓴다. file_too_large 의
    // 이 문장은 세션 파일의 것이고, 기억 서랍은 다운로드가 없고 기억을 삭제할 수 있어 제 문장(chat.memory.tooLarge)을 쓴다
    invalid_path: "파일 경로가 올바르지 않습니다. 목록에서 파일을 다시 선택해 주세요.",
    file_too_large: "파일이 너무 커서 열거나 다운로드할 수 없습니다. Agent 에게 내용을 나눠 저장해 달라고 요청해 주세요.",
    // 작업 공간 파일의 내려받기 거절(P40 6절)
    file_refused_executable:
      "열면 코드가 실행될 수 있는 파일은 내려받을 수 없습니다. 실행 파일, 바로 가기, 디스크 이미지, 매크로나 포함된 개체가 든 문서, 그리고 이런 파일이 든 압축 파일이 해당합니다.",
    // 세션 파일(P36 10절). 다시 생성이 그 파일을 쓴 턴을 버렸거나 경로가 바뀌었다. 서랍과 도구 카드가 같은 문장이고, 서랍은 열
    // 때마다 목록을 새로 읽는다
    file_not_found: "이 파일을 찾을 수 없습니다. 답변을 다시 생성하면서 파일이 사라졌을 수 있으니 파일 서랍을 다시 열어 지금 있는 파일을 확인해 주세요.",
    not_pending: "지금 승인할 것이 없습니다. 화면을 새로 고쳐 주세요.",
    decision_count_mismatch:
      "대기 중인 호출 수와 보낸 결정 수가 다릅니다. 화면을 새로 고쳐 다시 승인해 주세요.",
    decision_mismatch:
      "대기 중인 호출과 보낸 결정이 서로 맞지 않습니다. 화면을 새로 고쳐 다시 승인해 주세요.",
    // 청지기의 Agent 정의 제안에 대한 결정이 본 내용과 다르다(P48 6.1절). 지문이 없거나 그 사이 제안이 바뀌었다
    proposal_changed: "보고 있던 제안이 그 사이에 바뀌었습니다. 새 제안을 확인한 뒤에 다시 결정해 주세요.",
    // 결정과 함께 보낸 기억하기를 플랫폼이 받지 않았다(P43 5절). 그사이 소유자, 규칙, 이유가 바뀌었을 수 있다
    remember_refused: "이 결정은 기억할 수 없습니다. 화면을 새로 고친 뒤 다시 결정해 주세요.",
    // 고치기는 같은 도구의 인자만이다(D65). 카드는 이름을 바꿀 자리가 없으므로 외부 호출자가 볼 문장이다
    edit_name_mismatch:
      "수정한 Tool 이름이 승인 요청의 Tool 과 다릅니다. Tool 이름은 그대로 두고 인자만 고쳐 다시 보내 주세요.",
  } satisfies Record<string, string>,
  /** 표에 없는 코드가 받는 문장. 뒤에 서버 detail 이 따로 붙는다. */
  fallback: "요청을 처리하지 못했습니다.",
  /**
   * 저장 전에 화면이 바뀐 것이 없다고 먼저 알아챘을 때의 안내. 요청을 보내지 않으므로 오류가 아니라 정보다.
   * 서버까지 가서 같은 경우에 걸리면 위 표의 empty_patch 문장이 나온다.
   */
  noChange: "바뀐 것이 없어 저장하지 않았습니다.",
  /**
   * 409 참조 목록의 키 → 화면에 쓰는 리소스 이름.
   *
   * 정의와 배포가 한 리소스라(D42) 옛 Profile·Instance 짝이 하나로 줄었다. `deployed_` 가 붙은 키가
   * 따로 있는 것은 배포가 핀한 리비전이 막는 경우와 현재 정의가 가리키는 경우를 서버가 갈라 보내기
   * 때문이다. 키 목록은 lib/errorText.ts 의 REF_KEYS 이고, 서버가 내는 키가 거기 없으면 화면이 "연결을 먼저 해제해 주세요" 와 빈
   * 목록을 함께 준다.
   */
  refNames: {
    mcp_server_ids: "MCP Server",
    deployed_mcp_server_ids: "배포된 MCP Server",
    gateway_ids: "Gateway",
    agent_ids: "Agent",
    deployed_agent_ids: "배포된 Agent",
    knowledge_base_ids: "Knowledge Base",
    token_ids: "읽기 토큰",
  } satisfies Record<RefKey, string>,
  /**
   * 409 가 id 대신 수로만 싣는 참조. Skill 삭제는 다른 테넌트의 Agent 를 수로만 알린다(D64). 그 테넌트의 이름을 보이지 않기
   * 위해서다.
   */
  refCountNames: {
    other_tenant_agents: "다른 테넌트의 Agent",
  },
  /** 이름을 아는 참조 한 줄. 종류 이름 뒤에 리소스 이름을 잇는다. */
  refNamed: (label: string, name: string): string => `${label} ${name}`,
  /** 이름을 모르는 참조 한 줄. 종류와 개수만 말한다. */
  refCount: (label: string, count: number): string => `${label} ${count}개`,
};
