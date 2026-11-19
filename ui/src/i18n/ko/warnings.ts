import type { GateReason } from "@/lib/codeExecution";

import { clusters } from "./clusters";

/** 경고 하나. 표 셀에 들어갈 짧은 이름과 한 문장이다. */
interface WarningText {
  label: string;
  text: string;
}

/** 배포가 멈춘 자동 실행 소스의 수 (P41 3·10절). 서버 문장에서 읽는다(lib/warningText.ts 의 COUNTS_OF). */
export interface PausedSources {
  schedules: number;
  triggers: number;
  broken: number;
}

/** 서버 문장의 수를 받아 문장을 만드는 경고. 수를 읽지 못하면 화면이 서버 원문을 보인다. */
interface CountedWarningText {
  label: string;
  text: (counts: PausedSources) => string;
}

/** 규칙이 Agent 의 Tool 을 숨기는 원인 (P52 7절). 서버 문장의 `cause: <원인>` 이다(services/agent_status.py HIDDEN_CAUSES). */
export type HiddenCause = "not_named" | "item" | "oidc" | "reissue";

/** Code mode 를 끈 까닭 (P56 6.3·6.7절). 서버 문장의 `cause: <까닭>` 이다(services/gateway_code_mode.py CLOSED_CAUSES, CAUSE_TOOLHIVE). */
export type CodeModeCause = "tools" | "unknown_names" | "reserved_name" | "exposed" | "owner_cedar" | "toolhive";

/**
 * 서버 문장의 `(<이름>: <까닭>)` 으로 까닭 문장 하나를 고르는 경고. 코드 실행 게이트(P40 8절)는 `reason:` 을, Code mode(P56 6.3·6.7절)는
 * `cause:` 를 읽는다(어느 이름을 읽는지는 lib/warningText.ts 의 CHOSEN_BY 다). 까닭을 읽지 못하면 화면이 서버 원문을 보인다.
 */
interface ChosenWarningText<K extends string> {
  label: string;
  choices: Record<K, string>;
}

/** 서버 문장의 원인을 받아 한 문장을 더하는 경고. 원인을 읽지 못하면 text 만 보인다. */
interface CausedWarningText {
  label: string;
  text: string;
  causes: Record<HiddenCause, string>;
}

/**
 * 상태 경고. 서버의 `status.warnings` 와 배포·재배포 응답의 `warnings` 를 화면 문장으로 옮긴다(lib/warningText.ts).
 * 문구 규칙은 셋이다. 코드를 문장에 넣지 않는다. 화면 문구이므로 합니다체다. 대시로 설명을 잇지 않는다.
 * label 은 표 셀에 들어가므로 16자를 넘지 않는다.
 */
export const warnings = {
  /**
   * 경고 코드 → 짧은 이름과 한 문장.
   *
   * 코드는 서버에서 전수로 훑은 것이다. preflight(`k8s/preflight.py`), 노출 검증(`services/exposure.py`),
   * Gateway 접근 회수(`services/gateways.py`), MCP Server 배포(`services/mcp_servers.py`), Agent 런타임과
   * 그래프 분석(`services/agents.py`, `services/agent_graph.py`) 다섯이 낸다.
   */
  codes: {
    // -- 클러스터 준비 상태 (MCP·Gateway·Agent 공통) --------------------------------
    network_policy_unenforced: {
      label: "정책 집행 안 됨",
      text: "이 클러스터에는 NetworkPolicy 를 집행하는 컨트롤러가 없어 플랫폼이 만드는 NetworkPolicy 가 아무것도 막지 않습니다. 플랫폼은 이것을 보호 수단으로 보지 않으므로 배포는 그대로 됩니다. 이 엔드포인트는 집행 여부와 상관없이 클러스터 안에서 무인증으로 열려 있습니다. 클러스터 관리자에게 Calico·Cilium 같은 집행 컨트롤러 설치를 요청하고, 그때까지는 민감한 백엔드를 이 클러스터에 올리지 말아 주세요.",
    },

    // 메타데이터 차단을 켰는데 정책 컨트롤러가 확인된 것이 아니어서 egress 정책을 두지 않은 클러스터 (T75)
    metadata_block_unsupported: {
      label: "메타데이터 차단 안 됨",
      text: "이 클러스터는 인스턴스 메타데이터 주소를 막도록 설정했지만, NetworkPolicy 컨트롤러가 확인된 컨트롤러(kube-router, Calico, Antrea)가 아니어서 플랫폼이 egress 정책을 두지 않았습니다. 다른 컨트롤러에서는 이 정책이 파드의 API 서버 접속까지 막을 수 있기 때문입니다. 노드 설정이나 컨트롤러의 자체 정책으로 그 주소를 막아 주세요.",
    },
    // 관리자의 egress 정책이 있어 메타데이터 차단을 두지 않은 네임스페이스 (T75). {namespaces} 는 Cluster 화면이 채운다
    metadata_block_skipped: {
      label: "일부 네임스페이스 차단 안 됨",
      text: "다음 네임스페이스에는 플랫폼이 만들지 않은 egress 정책이 있어 메타데이터 차단 정책을 두지 않았습니다. NetworkPolicy 는 허용을 더하기만 하므로 차단 정책을 더하면 그 네임스페이스의 기존 제한이 풀리기 때문입니다. 그 네임스페이스의 정책에서 직접 주소를 막아 주세요: {namespaces}",
    },

    // -- 외부 노출 (MCP·Gateway) ----------------------------------------------------
    unauthenticated_exposure: {
      label: "무인증 노출",
      text: "플랫폼이 header 모드로 동작하고 있어 이 엔드포인트가 인증 없이 외부에 열립니다. 클러스터의 무인증 노출 허용을 끄거나 플랫폼을 oidc 모드로 바꿔 주세요.",
    },

    // 사용자 이미지 서버가 TLS 끝점을 가진 Redis 를 바인딩한 배포 (P61 3절)
    redis_tls_binding: {
      label: "Redis TLS 바인딩",
      text: "바인딩한 Redis 에 TLS 포트가 있어 이 바인딩의 주소는 TLS 포트를 사용하는 rediss:// 주소입니다. 이 MCP Server 는 직접 지정한 이미지로 실행되므로, 이미지가 바인딩 환경 변수의 TLS 값과 CA 파일 경로를 읽어 TLS 로 접속하는지 확인해 주세요.",
    },

    // -- Gateway ---------------------------------------------------------------------
    // 턴 위임 토큰(P53 7절). 위임이 켜진 local 모드에서만 나온다. owner_cedar 는 Agent 가 이 Gateway 를 Agent 이름으로 부르는 까닭이고, forbid 는 사용자 이름으로 부른 호출이 거절될 수 있다는 경고다
    turn_tokens_owner_cedar: {
      label: "사용자 전달 막음",
      text: "이 Gateway 는 Advanced 탭에 저장한 Cedar 정책(ConfigMap 이나 엔터티가 든 정책)을 그대로 쓰고 있어, 플랫폼이 Agent 의 호출을 이 Gateway 에 묶을 수 없습니다. 그래서 이 클러스터의 Agent 는 모든 Gateway 의 Tool 을 사용자가 아니라 Agent 의 이름으로 부릅니다. 이 Gateway 를 외부에 노출했다면 모든 클러스터의 Agent 가 그렇습니다. 규칙을 Authz policy (Cedar) 칸에 다시 적고 Advanced 탭에서 그 정책을 지워 주세요.",
    },
    // oidc 모드의 Gateway (P53b 6.6·6.9절). 소유자 Cedar 가 IdP 그룹이나 클라이언트 이름으로 가린다. 발급자를 플랫폼으로 바꾸기 전에도 나온다
    idp_names_in_policy: {
      label: "IdP 이름 확인",
      text: "이 Gateway 의 Authz policy (Cedar) 칸이 IdP 그룹이나 클라이언트의 이름으로 호출자를 가립니다. 플랫폼 토큰에는 IdP 그룹이 테넌트 이름을 붙인 모양으로만 실리고 클라이언트 id 는 실리지 않으므로, 플랫폼 토큰으로 전환하면 이 조건이 맞지 않습니다. 조건을 Tool 규칙의 역할이나 그룹으로 옮겨 주세요.",
    },
    turn_tokens_authz_forbid: {
      label: "forbid 정책 확인",
      text: "이 Gateway 의 Authz policy (Cedar) 칸에 forbid 정책이 있습니다. Agent 가 사용자 이름으로 Tool 을 부를 때 쓰는 토큰에는 그룹이 없으므로, 그 정책이 그룹으로 사용자를 가린다면 Agent 의 호출이 거절됩니다. 그룹 대신 테넌트(claim_tenant)로 조건을 적어 주세요.",
    },
    // Tool 규칙이 굴러가는 동안 (P52 2절). 앞의 규칙이 서는 자리와 지문을 아직 싣지 않은 Gateway 다
    rules_pending: {
      label: "규칙 반영 대기",
      text: "이 Gateway 가 아직 새 파드로 바뀌는 중이라, 바뀐 Tool 규칙은 모든 파드가 바뀐 뒤에 적용됩니다. 그때까지는 앞의 규칙이 적용됩니다.",
    },
    rules_gateways_behind: {
      label: "Gateway 반영 중",
      text: "이 서버를 쓰는 Gateway 가운데 바뀐 Tool 규칙을 아직 반영하지 못한 것이 있어, 그 Gateway 를 거친 규칙 있는 Tool 은 잠시 부를 수 없습니다. 플랫폼이 곧 다시 적용합니다.",
    },
    rules_front_rolling: {
      label: "앞단 반영 중",
      text: "노출 앞단이 아직 새 파드로 바뀌는 중이라, 앞단을 거친 호출에는 앞의 Tool 규칙이 적용됩니다. 모든 파드가 바뀌면 새 규칙이 적용됩니다.",
    },
    // 서버 컨테이너가 Tool 의 입력 스키마에서 풀지 못한 인자 조건 (P52 4절). 그 Tool 은 모든 호출을 거절한다
    conditions_unresolved: {
      label: "인자 조건 확인 필요",
      text: "조건에 적은 인자를 서버가 Tool 에서 찾지 못해, 그 Tool 은 모든 호출을 거절합니다. Tool 규칙에서 인자 이름을 확인해 주세요.",
    },
    // Tool 규칙의 경고 (P52 7절). services/tool_rules.py 의 RULE_WARNINGS 와 Agent 상태(services/agent_status.py)가 낸다
    rules_not_enforced: {
      label: "규칙 미적용",
      text: "header 인증 모드에서는 Tool 규칙의 부를 수 있는 대상이 적용되지 않습니다. 이 제한이 필요하면 플랫폼 관리자에게 토큰 인증 모드를 요청해 주세요.",
    },
    conditions_closed: {
      label: "조건 미집행",
      text: "이 서버의 모든 파드가 인자 조건을 지원하는 이미지로 도는지 아직 확인하지 못해, 조건이 있는 Tool 을 닫아 두었습니다. 확인이 끝나면 열립니다. 옛 이미지라면 클러스터의 런타임 이미지를 새 판으로 올린 뒤 다시 배포해 주세요.",
    },
    rules_toolhive_old: {
      label: "규칙 미지원 클러스터",
      text: "이 클러스터의 ToolHive 가 Tool 규칙을 지원하지 않아 규칙이 있는 Tool 을 닫아 두었습니다. 플랫폼 관리자에게 ToolHive 업그레이드를 요청해 주세요.",
    },
    rules_owner_cedar_closed: {
      label: "규칙 반영 불가",
      text: "이 Gateway 는 Advanced 탭에 저장한 Cedar 정책을 그대로 쓰고 있어 서버의 Tool 규칙을 반영하지 못하고, 규칙이 있는 Tool 을 부를 수 없습니다. Advanced 탭에서 그 정책을 지워 주세요.",
    },
    rules_digest_behind: {
      label: "규칙 반영 중",
      text: "이 Gateway 가 백엔드의 바뀐 Tool 규칙을 아직 반영하지 못해, 규칙이 있는 Tool 을 잠시 부를 수 없습니다. 플랫폼이 곧 다시 적용합니다.",
    },
    // 게시된 Gateway 를 백엔드로 쓸 때의 경고 (P57 6.4절). 뒤의 셋은 걸린 백엔드 이름을 원문 끝의 괄호에 싣는다
    published_owner_forbid: {
      label: "백엔드 사용 차단",
      text: "이 Gateway 의 Authz policy (Cedar) 에 forbid 문장이 있어, 이 Gateway 를 백엔드로 사용하는 Gateway 를 거쳐 오는 호출을 모두 거절합니다. forbid 문장의 조건을 Tool 규칙으로 옮겨 주세요.",
    },
    published_backend_closed: {
      label: "백엔드 호출 거부",
      text: "백엔드로 사용하는 게시된 Gateway 가 이 Gateway 의 호출을 거절합니다. 그 Gateway 나 그 아래 게시된 Gateway 의 Authz policy (Cedar) 에 forbid 문장이 있거나, 그 Gateway 가 아직 이 호출을 받도록 렌더되지 않았습니다. 그 Gateway 를 관리하는 사람에게 확인을 요청해 주세요.",
    },
    published_consumer_pending: {
      label: "백엔드 연결 대기",
      text: "백엔드로 사용하는 게시된 Gateway 가 아직 이 Gateway 를 받도록 다시 렌더되지 않아, 그 Gateway 의 Tool 을 잠시 부를 수 없습니다. 플랫폼이 곧 다시 적용합니다. 그 사이 이 Gateway 를 Tool 소스로 사용하는 Agent 를 배포하면, 그 Agent 는 다시 배포할 때까지 그 Tool 을 보지 못합니다.",
    },
    published_names_unknown: {
      label: "Tool 이름 미확인",
      text: "백엔드로 사용하는 게시된 Gateway 의 Tool 이름을 배포할 때 알 수 없어 그 Gateway 를 부르지 않습니다. 원래 Gateway 의 백엔드에서 Tool 새로고침을 실행하고 이 Gateway 를 다시 배포해 주세요.",
    },
    // Gateway 의 문장이다. 허브 항목은 hubCodes 의 문장을 쓴다
    rules_tool_missing: {
      label: "규칙의 Tool 없음",
      text: "규칙을 둔 Tool 가운데 백엔드가 더 이상 제공하지 않는 것이 있습니다. 백엔드가 Tool 이름을 바꿨다면 규칙을 새 이름으로 고친 뒤 Gateway 를 다시 배포해 주세요.",
    },
    // Code mode 를 켠 Gateway 의 렌더 (P56 6.3·6.7절, services/gateway_code_mode.py). 까닭은 서버 문장의 `cause: <까닭>` 이다
    code_mode_closed: {
      label: "Code mode 꺼짐",
      choices: {
        tools: "이 Gateway 가 제공하는 Tool 가운데 읽기 전용이 아닌 Tool 이 있어 Code mode 를 껐습니다. 그 Tool 을 백엔드의 Tool 선택에서 뺀 뒤 다시 배포하거나, 서버나 허브 항목에서 읽기 전용으로 표시해 주세요.",
        toolhive: "이 클러스터의 ToolHive 가 Code mode 를 지원하지 않아 Code mode 를 껐습니다. 플랫폼 관리자에게 ToolHive 업그레이드를 요청해 주세요.",
        unknown_names:
          "Tool 이름을 알 수 없는 백엔드가 있어 Code mode 를 껐습니다. 그 허브 항목에서 Tool 새로고침을 실행하거나 그 백엔드의 Tool 을 직접 선택한 뒤 다시 배포해 주세요.",
        reserved_name:
          "이 Gateway 가 제공하는 Tool 가운데 이름이 execute_tool_script 인 Tool 이 있어 Code mode 를 껐습니다. 그 백엔드에서 이 Tool 을 뺀 나머지 Tool 을 선택하거나 Conflict resolution 을 prefix 로 바꾼 뒤 다시 배포해 주세요.",
        exposed: "외부에 노출한 Gateway 라서 Code mode 를 껐습니다. 노출을 끄거나 Code mode 를 끈 뒤 다시 저장해 주세요.",
        owner_cedar: "이 Gateway 는 Advanced 탭에 저장한 Cedar 정책을 그대로 사용하고 있어 Code mode 를 껐습니다. Advanced 탭에서 그 정책을 지운 뒤 다시 저장해 주세요.",
      },
    },
    code_mode_tools_hidden: {
      label: "Code mode 로 숨김",
      text: "서버가 읽기 전용이라고 알려 주지 않는 Tool 이 있어, Code mode 를 켠 이 Gateway 에서는 그 Tool 이 보이지 않고 호출도 거절됩니다. MCP Server 의 Tool 이면 클러스터의 런타임 이미지를 새 판으로 올린 뒤 그 서버를 다시 배포하고, 허브 항목의 Tool 이면 그 Tool 을 백엔드의 Tool 선택에서 빼 주세요.",
    },
    // 원인은 서버 문장의 `cause: <원인>` 에서 읽는다(lib/warningText.ts causeIn)
    agent_tool_hidden: {
      label: "Tool 숨김",
      text: "이 Agent 가 선택한 Tool 가운데 규칙이 이 Agent 를 허용하지 않는 것이 있어 Agent 가 그 Tool 을 볼 수 없습니다.",
      causes: {
        not_named: "원인: 서버나 Gateway 의 규칙에 이 Agent 가 없습니다(규칙에 더해 주세요).",
        item: "원인: 허브 항목의 규칙입니다(항목 규칙은 Agent 를 허용하지 않습니다).",
        oidc: "원인: Gateway 가 아직 IdP 토큰만 받습니다(플랫폼 토큰으로 전환하기 전에는 Agent 를 규칙에 넣을 수 없습니다).",
        reissue: "원인: 이 Agent 의 토큰을 새로 발급하는 중입니다(다시 뜨면 열립니다).",
      },
    },
    runtime_tool_refusals: {
      label: "런타임 업데이트 필요",
      text: "이 Agent 의 런타임 이미지는 거절된 Tool 호출을 처리하지 못해, 규칙이 있는 Tool 이 거절되면 그 대화가 실패합니다. 클러스터의 에이전트 이미지를 새 판으로 올린 뒤 다시 배포해 주세요.",
    },
    tool_marks_changed: {
      label: "읽기 전용 표시 바뀜",
      text: "이 Agent 가 쓰는 서버나 허브 항목에서 Tool 의 읽기 전용 표시가 바뀌었습니다. 이 Agent 를 다시 배포해야 승인 설정에 반영됩니다.",
    },
    // 외부 MCP 서버 항목의 헤더 값 (P86 6.3·6.4절)
    outside_header_refused: {
      label: "헤더 값 거절됨",
      text: "외부 서버가 플랫폼의 마지막 호출에서 이 허브 항목의 헤더 값을 거절했습니다. 값을 확인한 뒤 헤더 값 바꾸기로 다시 저장해 주세요.",
    },
    // SSE 로만 말하는 외부 서버로 그 전에 만든 허브 항목 (services/hub.py OUTSIDE_SSE_UNSUPPORTED)
    outside_sse_unsupported: {
      label: "SSE 서버 미지원",
      text: "이 허브 항목의 서버는 SSE 방식으로만 동작해 플랫폼이 Tool 을 읽지 못하고, 이 항목을 사용하는 Gateway 를 배포할 수 없습니다. 서버의 streamable-http 주소로 새 항목을 만들어 주세요.",
    },
    outside_header_missing: {
      label: "헤더 값 없음",
      text: "백엔드로 사용하는 허브 항목의 헤더 값을 찾지 못해 헤더 없이 부릅니다. 허브 항목을 관리하는 사람에게 헤더 값을 다시 저장해 달라고 요청해 주세요.",
    },
    // 허브 image 항목의 허용과 거부 (P55 6절). 명세의 화면 문구 표 그대로다
    hub_image_unverified: {
      label: "서명 확인 안 됨",
      text: "이 항목의 이미지는 서명을 확인하지 않고 등록됐습니다. 사내 레지스트리로 옮겨 서명한 이미지로 새 항목을 만들어 주세요.",
    },
    hub_image_refused: {
      label: "거부된 이미지",
      text: "플랫폼 관리자가 이 항목의 이미지를 거부했습니다. 지금은 그대로 동작하지만 이 항목을 사용하는 Gateway 는 다음 재배포에서 거부됩니다. 새 이미지로 항목을 다시 만들어 주세요.",
    },
    hub_backend_refused: {
      label: "거부된 백엔드 이미지",
      text: "이 Gateway 의 백엔드 가운데 플랫폼 관리자가 이미지를 거부한 허브 항목이 있습니다. 지금은 그대로 동작하지만 다음 재배포에서 거부됩니다. 편집에서 그 백엔드를 빼고 새 리비전으로 저장해 주세요.",
    },
    hub_image_key_revoked: {
      label: "서명 키 회수됨",
      text: "이 항목의 이미지를 허용한 서명 키가 회수됐습니다. 새 키로 서명한 이미지로 항목을 다시 만들어 주세요.",
    },
    hub_access_revoked: {
      label: "허브 접근 회수됨",
      text: "허브 항목의 접근 정책이 좁아져 이 Gateway 가 더는 선택할 수 없는 백엔드가 있습니다. 지금은 그대로 동작하지만 다음 재배포에서 거부됩니다. 항목을 게시한 테넌트에 접근 허용을 요청하거나, 편집에서 그 백엔드를 빼고 새 리비전으로 저장해 주세요.",
    },

    // -- Agent ---------------------------------------------------------------------------
    skill_access_revoked: {
      label: "Skill 접근 회수됨",
      text: "Skill 의 접근 정책이 좁아져 이 Agent 가 더는 붙일 수 없는 Skill 이 있습니다. 지금은 그대로 동작하지만 다음 저장과 재배포에서 거부됩니다. Skill 소유자에게 사용 권한을 요청하거나, 캔버스에서 그 Skill 을 빼고 새 리비전으로 저장해 주세요.",
    },

    // -- Agent 런타임 ------------------------------------------------------------------
    no_shared_state: {
      label: "세션이 공유되지 않음",
      text: "그래프에 memory 노드가 없어 세션이 파드 안에만 있습니다. 파드를 재시작하면 사라지고 레플리카끼리 공유되지도 않습니다. memory 노드를 추가하고 Database 를 연결하면 세션이 보존됩니다.",
    },
    pending_sessions_revision_changed: {
      label: "승인 대기 세션 있음",
      text: "승인을 기다리던 세션이 있습니다. 재배포하면 새 리비전의 그래프에서 이어집니다.",
    },
    // 런타임 기능 기록 (D65). 배포·재배포 응답과 status.warnings 양쪽에 온다
    runtime_features_unknown: {
      label: "런타임 기능 미확인",
      text: "이 이미지가 어떤 기능을 지원하는지 아직 알리지 않았습니다. 파드가 뜰 때 init 컨테이너가 확인하므로 기다린 뒤 상태를 봐 주세요.",
    },
    // 플랫폼이 평가를 꺼서 배포 전 평가 없이 배포했다 (P45 12절). 배포·재배포·되돌리기 응답에 온다
    evaluation_gate_off: {
      label: "배포 전 평가 꺼짐",
      text: "플랫폼에서 평가를 꺼 두어 배포 전 평가 없이 배포했습니다. 평가를 다시 켜면 다음 배포부터 평가를 확인합니다.",
    },
    runtime_feature_dropped: {
      label: "기능 일부 제외됨",
      text: "런타임 이미지가 지원하지 않는 기능이 있어 그 기능을 빼고 배포했습니다. 그 기능을 사용하려면 클러스터의 에이전트 이미지를 새 판으로 올린 뒤에 다시 배포해 주세요.",
    },
    // 턴 위임 토큰(P53 7절). 런타임 이미지가 turn_tokens 를 알리지 않아 모든 Tool 을 Agent 의 이름으로 부른다
    // 뒤 판이 더한 종류의 소스를 가리키는 tools 노드를 컴파일이 뺐다 (P46 A0). 노드 이름은 끝의 괄호에서 읽는다
    tools_source_newer: {
      label: "알 수 없는 Tool 소스",
      text: "더 새 플랫폼 버전에서 연결한 Tool 소스가 있어 그 노드를 빼고 Agent 를 실행합니다. 플랫폼을 그 버전 이상으로 올리면 그 Tool 을 다시 사용합니다.",
    },
    turn_tokens_unsupported: {
      label: "사용자 전달 미지원",
      text: "이 Agent 의 런타임 이미지는 대화를 시작한 사용자를 Tool 호출에 싣지 못해, 모든 Tool 을 Agent 의 이름으로 부릅니다. 클러스터의 에이전트 이미지를 새 판으로 올린 뒤 다시 배포해 주세요.",
    },
    runtime_features_invalid: {
      label: "기능 목록 형식 오류",
      text: "init 컨테이너가 보낸 기능 목록의 형식이 맞지 않아 무시했습니다. Agent 는 그대로 동작하지만 기능 판정에는 빈 목록을 사용합니다. 이미지가 정상 버전인지 확인해 주세요.",
    },
    // 런타임이 그래프를 컴파일할 때 남기는 경고. /metrics 를 거쳐 status.warnings 에 온다
    builtin_tool_name: {
      label: "내장 Tool 과 이름이 겹침",
      text: "연결한 Tool 소스가 런타임 내장 Tool(파일 Tool, task 등)과 같은 이름의 Tool 을 내놓아, 이 Agent 는 그 소스의 Tool 대신 내장 Tool 을 사용합니다. 소스에서 Tool 이름을 바꾸거나 Gateway 의 prefix 모드로 연결해 주세요.",
    },
    correlation_hook_missing: {
      label: "LLM 상관 헤더 없음",
      text: "이 Agent 가 쓰는 모델의 SDK 클라이언트에 상관 헤더(traceparent)를 붙이지 못했습니다. 대화는 그대로 동작하지만, LLM Gateway 나 provider 쪽 호출 기록을 이 Agent 의 트레이스와 같은 id 로 이어 볼 수 없습니다. 런타임 이미지의 문제이므로 플랫폼 관리자에게 알려 주세요.",
    },
    // 배포 때의 Tool 핀 (P39b 14절 5부)
    tools_behind: {
      label: "새 Tool 있음",
      text: "배포한 뒤에 Tool 소스에 새 Tool 이 생겼습니다. 다시 배포하면 추가됩니다.",
    },
    tools_missing: {
      label: "사라진 Tool 있음",
      text: "배포할 때 고정한 Tool 가운데 Tool 소스에서 사라진 것이 있습니다. 그 Tool 은 호출되지 않습니다. 소스를 확인하거나 다시 배포해 주세요.",
    },
    platform_tool_exposed: {
      label: "웹 Tool 노출 허용 꺼짐",
      text: "외부에 노출한 이 Gateway 가 플랫폼 웹 Tool 을 사용하지만 Platform Settings 에서 노출한 Gateway 사용을 허용하지 않았습니다. 지금은 그대로 동작하지만 다음 재배포는 거절됩니다. 노출을 끄거나, 관리자에게 '노출한 Gateway 에서도 허용' 을 켜 달라고 요청해 주세요.",
    },
    pinned_agents_keep_tools: {
      label: "고정한 Tool 유지",
      text: "이 Gateway 를 사용하는 Agent 는 다시 배포하기 전까지 배포 당시 고정한 Tool 을 그대로 사용합니다.",
    },
    web_backend_pinned_agents: {
      label: "Agent 재배포 필요",
      text: "이 Gateway 를 사용하는 Agent 는 다시 배포해야 웹 Tool 을 사용할 수 있습니다.",
    },
    // 바깥 내용 표시의 검사 (P43 2·4·8절)
    outside_mark_off: {
      label: "외부 내용 표시 꺼짐",
      text: "외부 서비스의 Tool 에서 외부 내용 표시를 껐습니다. 그 Tool 의 결과에 섞인 지시가 쓰기 Tool 을 승인 없이 부를 수 있습니다.",
    },
    read_only_by_canvas: {
      label: "캔버스 읽기 전용 표시",
      text: "서버가 읽기 전용 여부를 알려 주지 않는 Tool 을 캔버스에서 읽기 전용으로 표시했습니다. 그 Tool 이 데이터를 바꾸고 승인 설정이 없다면 외부 내용이 들어온 대화에서도 승인 없이 실행됩니다.",
    },
    outside_writes_refused: {
      label: "외부 내용이 오면 쓰기 거절",
      text: "이 그래프에는 memory 노드가 없어 승인을 받을 수 없습니다. 대화에 외부 내용이 들어오면 데이터를 바꿀 수 있는 Tool 은 실행하지 않습니다. 승인을 받으려면 memory 노드를 추가해 주세요.",
    },
    // 웹 Tool 이 있는 그래프의 검사 (P39b 14절 2·4부)
    web_fetch_allowlist_empty: {
      label: "허용 도메인 없음",
      text: "허용 도메인이 비어 있어, 데이터 Tool 이 있는 그래프나 파일을 첨부한 대화에서는 web_fetch 가 모두 거절됩니다.",
    },

    // -- Agent 그래프 분석 (배포·재배포 응답) --------------------------------------------
    unattached_node: {
      label: "떨어진 노드",
      text: "entry 에서 연결이 이어지지 않는 노드가 있어 무시됩니다. 캔버스에서 연결을 확인해 주세요.",
    },
    unsupported_param: {
      label: "무시되는 설정값",
      text: "선택한 provider 가 지원하지 않는 model 설정값이 있어 무시됩니다. 캔버스의 모델 노드에서 그 값을 빼거나, 그 값을 지원하는 provider 를 선택해 주세요.",
    },
    unknown_node_keys: {
      label: "알 수 없는 노드 설정",
      text: "tools 노드에 이 플랫폼 버전이 알지 못하는 설정이 있어 무시됩니다. allow_domains 처럼 설정 이름이 맞는지 확인해 주세요.",
    },
    knowledge_base_empty: {
      label: "빈 Knowledge Base",
      text: "knowledge 노드가 가리키는 Knowledge Base 에 인덱싱된 문서가 없어 검색 결과가 나오지 않습니다. 문서를 업로드해 인덱싱해 주세요.",
    },
    knowledge_base_failed: {
      label: "인덱싱 실패 문서",
      text: "knowledge 노드가 가리키는 Knowledge Base 에 인덱싱하지 못한 문서가 있습니다. Knowledge Base 에서 실패한 문서를 확인해 주세요.",
    },
    // 배포가 자동 실행의 일정과 트리거를 멈췄다 (P41 3·10절). 다른 사람에게 묶인 것과 전제 조건이 맞지 않는 것이다
    runs_paused_by_deploy: {
      label: "자동 실행 멈춤",
      text: ({ schedules, triggers, broken }: PausedSources): string =>
        `이 배포로 다른 사람의 이름으로 도는 일정 ${schedules}개와 트리거 ${triggers}개, 설정이 맞지 않는 ${broken}개가 멈춥니다.`,
    },
    // 코드 실행을 켠 Agent 의 클러스터 게이트가 닫혔다 (P40 8절). 문장은 그 까닭의 문장이다
    sandbox_gate_closed: {
      label: "코드 실행 불가",
      choices: clusters.sandbox.reason,
    },
    sandbox_override: {
      label: "점검 없이 허용",
      text: "개발용 설정으로 점검 없이 코드 실행을 허용하고 있습니다. 운영 환경에서는 이 설정을 쓰지 마세요.",
    },
  } satisfies Record<string, WarningText | CountedWarningText | CausedWarningText | ChosenWarningText<GateReason> | ChosenWarningText<CodeModeCause>>,

  /** 허브 항목 상세가 codes 대신 쓰는 문장. 같은 코드를 Gateway 도 내지만 고치는 법이 다르다 (P52 7절). */
  hubCodes: {
    rules_tool_missing: {
      label: "규칙의 Tool 없음",
      text: "규칙을 둔 Tool 가운데 이미지가 더 이상 제공하지 않는 것이 있습니다. 이미지가 Tool 이름을 바꿨다면 규칙을 새 이름으로 고쳐 저장해 주세요.",
    },
  } satisfies Record<string, WarningText>,

  /**
   * 코드 없이 영어 문장만 오는 Gateway 경고(lib/warningText.ts 의 PROSE_WARNINGS). 서버가 고른 고치는 법(fixHint)을
   * 잇는 문장은 그것을 받는 함수다.
   */
  prose: {
    prefixFallback: {
      label: "prefix 모드로 전환",
      text: (hint: string): string =>
        `Tool 이름을 모르는 백엔드가 있어 prefix 모드로 전환됩니다. Tool 이름 앞에 워크로드 이름이 붙어 <workload>_<tool> 꼴이 됩니다. 원래 이름을 쓰려면 ${hint}`,
    },
    priorityConflict: {
      label: "이름 충돌 위험",
      text: (hint: string): string =>
        `priority 모드인데 Tool 이름을 모르는 백엔드가 있습니다. 이름이 겹쳐도 바꿔 줄 수 없고 먼저 등록된 워크로드가 경고 없이 적용됩니다. prefix 모드로 바꾸거나, ${hint}`,
    },
    filterUnchecked: {
      label: "Tool 필터 미검증",
      text: (hint: string): string => `허브 항목의 Tool 이름을 몰라 선택한 Tool 필터가 맞는지 확인하지 못했습니다. ${hint}`,
    },
    tooManyBackends: {
      label: "백엔드 많음",
      text: "백엔드가 20개를 넘습니다. 세션마다 모든 백엔드에 tools/list 를 부르므로 Gateway 를 나누는 것을 검토해 주세요.",
    },
  },

  /**
   * 도구 이름을 모를 때 무엇을 하면 되는지. 서버가 백엔드 종류를 보고 이미 갈라 적어 주므로
   * (`services/gateways.unknown_names_hint`) 화면은 그 판단을 다시 하지 않고 문장에서 읽어 낸다.
   * 파생 항목(internal·published)에는 도구 새로고침이 없어 422 다 (D49).
   */
  fixHint: {
    refresh: "허브 항목에서 Tool 새로고침을 실행해 주세요.",
    redeploy: "원본을 다시 배포해 주세요. internal·published 항목에는 새로고침이 없습니다.",
    both: "허브 항목에서 Tool 새로고침을 실행하거나, internal·published 항목이면 원본을 다시 배포해 주세요.",
  },
};
