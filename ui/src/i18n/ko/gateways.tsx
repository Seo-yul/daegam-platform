import type { ReactNode } from "react";

import type { GatewayStatus } from "@/api/types";
import { particle } from "@/lib/particle";

/** Gateway 의 목록, 폼, 상세와 삭제 경고 (F6). */
export const gateways = {
  /** 이름 칸의 표시 이름 예시. 폼과 허브 게시 카드가 같이 쓴다. */
  placeholder: "고객지원 게이트웨이",
  /**
   * 표의 머리. backend 는 줄 하나가 백엔드 하나인 표(배포 대화상자의 준비 표, 개요의 백엔드 줄, 미리보기)이고 backends 는
   * 개수나 묶음을 적는 자리(목록, 리비전 표, 정의 요약, 백엔드 카드 제목)다. 리비전 표의 레플리카 열은 detail.deployable.replicas 다.
   */
  columns: {
    name: "이름",
    state: "상태",
    revision: "리비전",
    backend: "백엔드",
    backends: "백엔드",
    conflict: "충돌",
    location: "클러스터 / 네임스페이스",
    ready: "준비",
    error: "오류",
    workload: "워크로드",
    kind: "종류",
    failures: "실패",
    message: "메시지",
    failureMode: "실패 방식",
    created: "생성",
  },
  /** Degraded 배지(GatewayStatus.badge_code). 서버는 같은 한국어를 badge 에도 적는다(platform/daegam/services/gateways.py DEGRADED_BADGES). */
  degradedBadge: {
    all_backends_down: "모든 백엔드 불가",
    some_backends_down: "일부 백엔드 불가",
  } satisfies Record<NonNullable<GatewayStatus["badge_code"]>, string>,
  /** /gateways. */
  list: {
    create: "Gateway 만들기",
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 Gateway 가 없습니다.`,
    noneDeployed: '배포된 Gateway 가 없습니다. "배포된 것만" 을 끄면 아직 올리지 않은 것도 보입니다.',
    /** link 는 허브로 가는 링크이고 글자는 emptyLink 다. */
    empty: (tenant: string, link: ReactNode): ReactNode => (
      <>
        테넌트 {tenant} 에 Gateway 가 없습니다. 연결 항목이 먼저 있어야 합니다.{" "}
        {link}
        해 주세요.
      </>
    ),
    emptyLink: "MCP Server 를 게시하거나 외부 MCP 를 등록",
    title: "Gateways",
    search: "Search gateways…",
    url: "URL",
  },
  /** 새로 만들기와 편집(새 리비전) 폼. 저장 단추와 Replicas 칸의 풀이는 detail.deployable 과 shared.create 다. */
  form: {
    revisionNotice: (revision: number): string => `저장하면 리비전 r${revision}${particle(`r${revision}`, "이/가")} 만들어집니다.`,
    backendsHint: "선택한 순서가 우선순위입니다. Tool 을 비우면 그 항목의 Tool 전체를 광고합니다.",
    noHubItems: "허브에 항목이 없습니다. MCP Server 를 만들거나 외부 MCP 를 등록해 주세요.",
    /** 데이터 플레인이 IdP 토큰만 받는 동안 게시된 Gateway 를 막는 까닭 (P57 6.1절). */
    oidcBlocked: "Gateway 가 아직 IdP 토큰만 받는 동안에는 게시된 Gateway 를 백엔드로 사용할 수 없습니다. 플랫폼 관리자가 플랫폼 토큰으로 전환하면 선택할 수 있습니다.",
    /** 토큰 인증에서 proxy 모드로 게시된 Gateway 를 막는 까닭 (P57 6.1절, 422 published_proxy_unsupported). */
    proxyBlocked: "proxy 모드로 게시된 Gateway 는 백엔드로 사용할 수 없습니다. 원래 Gateway 를 entry 모드로 게시한 항목을 선택해 주세요.",
    /** 토큰 인증에서 Tool 이름을 모르는 게시된 Gateway 를 막는 까닭 (P57 6.2절, 422 published_names_unknown). */
    publishedNamesUnknown: "이 게시된 Gateway 의 Tool 이름을 알 수 없어 선택할 수 없습니다. 원래 Gateway 의 백엔드에서 Tool 새로고침을 실행하고 그 Gateway 를 다시 배포해 주세요.",
    notConsumable: "이 항목을 백엔드로 선택할 권한이 없습니다.",
    /** 422 hub_item_not_consumable. name 은 항목 이름이고, 목록에 없으면 id 다. */
    notConsumableError: (name: string): string => `${name}: 이 항목을 백엔드로 선택할 권한이 없습니다. 항목의 접근 정책(consume)을 확인해 주세요.`,
    /**
     * 백엔드 하나를 가리키는 저장 오류(422 published_*). name 은 걸린 허브 항목 이름이고 text 는 그 코드의 문장이다. source 는
     * published_owner_forbid 의 원래 Gateway 이름이고, 없으면 빈 문자열이다.
     */
    backendError: (name: string, text: string, source: string): string =>
      source ? `${name}: ${text} 원래 Gateway 는 ${source} 입니다.` : `${name}: ${text}`,
    toolsPick: "tools (비우면 전체)",
    /** link 는 허브 항목 상세로 가는 링크이고 글자는 그 상세의 단추 이름(hub.detail.refresh)이다. */
    noTools: (link: ReactNode): ReactNode => <>이 항목은 Tool 목록이 없습니다. {link}을 먼저 실행해 주세요. 지금은 전체 Tool 이 포함됩니다.</>,
    conflictHint:
      "auto: 모든 백엔드의 Tool 목록을 알면 priority, 하나라도 모르면 prefix / priority: 순서대로 우선, 충돌 Tool 만 <item>_<tool> / prefix: 모든 Tool 을 <workload>_<tool>",
    /** names 는 Tool 목록을 모르는 항목 이름을 ", " 로 이은 것이다. */
    unknownTools: (names: string): string =>
      `${names}의 Tool 목록을 몰라 충돌 Tool 의 이름을 바꿀 수 없습니다. 충돌하면 앞선 백엔드가 경고 없이 적용됩니다. Tool 새로고침을 실행하거나 prefix 모드로 바꿔 주세요.`,
    failureHint: "지금 ToolHive 는 두 값 모두 남은 백엔드로 계속 서빙합니다. best_effort 이면 백엔드가 빠졌을 때 '일부 백엔드 불가' 배지를 띄웁니다.",
    authzHint: "테넌트 기본 정책에 더해지는 Cedar 정책 한 줄입니다. 비우면 기본 정책만 적용됩니다.",
    runtimeHint: "Gateway 파드의 replicas 와 리소스 요청·제한입니다.",
    /** 백엔드마다의 호출 타임아웃 (P50 7절). 비우면 배포가 정한다. */
    timeout: "타임아웃(초)",
    timeoutHint:
      "Gateway 가 이 백엔드의 Tool 호출을 기다리는 최대 시간. 비우면 배포할 때 정합니다. 내부 MCP Server 는 그 서버의 가장 긴 백엔드 타임아웃에 5초를 더하고, 그 밖은 60초입니다.",
    /** Gateway 가 백엔드의 resources·prompts 를 내보이는가 (P50 13절). */
    surfaceResources: "resources 공개",
    surfacePrompts: "prompts 공개",
    surfaceHint: "켜면 백엔드의 resources(또는 prompts)를 이 Gateway 의 목록에 보이고 읽게 합니다. 끄면 목록에서 빠지고 읽기는 거절됩니다.",
    title: "New Gateway",
    backendsTitle: "Backends (MCP Hub 에서 선택)",
    toolCount: (count: number): string => `Tool ${count}개`,
    aggregation: "Aggregation",
    auth: "auth",
    conflict: "Conflict resolution",
    conflicts: { auto: "auto", priority: "priority", prefix: "prefix" },
    failure: "Failure mode",
    failFast: "fail",
    authz: "Authz policy (Cedar)",
    runtime: "Runtime",
    replicas: "Replicas",
    selected: (count: number): string => `selected ${count}`,
  },
  /**
   * 상세의 머리, 대화상자, 경고, Advanced 탭. 재배포·편집·배포 해제 단추는 shared.detailActions 이고, MCP Server 와 같이
   * 쓰는 탭의 말은 detail.deployable 이다.
   */
  detail: {
    backToList: "Gateway 목록으로 돌아가기",
    deployTitle: "Gateway 배포",
    deployBody: (revision: number): string =>
      `정의 r${revision}${particle(`r${revision}`, "으로/로")} Gateway 와 그룹·Entry 클러스터 리소스를 올립니다. 백엔드 허브 항목이 모두 Ready 여야 합니다.`,
    /** 409 backends_not_ready. 문장 아래에 백엔드 표가 붙는다. */
    notReady: "모든 백엔드가 Ready 상태여야 배포할 수 있습니다.",
    /** 409 cycle. path 는 " → " 로 이은 게시 항목 경로를 꾸민 요소다. */
    cycle: (path: ReactNode): ReactNode => <>게시 항목을 따라가면 자기 자신에게 돌아옵니다: {path}</>,
    /** 재배포 확인. 돌고 있는 리비전이 정의보다 뒤처졌을 때와 같을 때. */
    redeployBehind: (deployed: number, revision: number): string =>
      `지금 r${deployed}${particle(`r${deployed}`, "으로/로")} 돌고 있습니다. 재배포하면 최신 정의 r${revision}${particle(`r${revision}`, "으로/로")} 올라가므로 묶여 있는 백엔드가 바뀔 수 있습니다. Gateway 파드가 새로 뜨는 동안 잠시 끊깁니다.`,
    redeploySame: (revision: number): string => `Gateway 파드를 다시 만듭니다. 정의는 r${revision} 그대로입니다. 새 파드가 뜨는 동안 잠시 끊깁니다.`,
    undeployBody: "Gateway 와 그룹·Entry 클러스터 리소스를 내립니다. 정의와 리비전 이력은 그대로 남아 다시 배포할 수 있습니다. 다른 테넌트가 공유한 허브 항목의 복제본은 바로 삭제하지 않고, 사용하는 Gateway 가 없어지면 잠시 뒤 정리합니다.",
    clonePlaceholder: "고객지원 게이트웨이 복제본",
    /** 정책이 좁아진 백엔드. warning 은 hub_access_revoked 경고 문장이고 names 는 ", " 로 이은 항목 이름이다. */
    revokedBackends: (warning: string, names: string): string => `${warning} 막힌 백엔드는 ${names} 입니다.`,
    overridesOnlyVmcp: "오버라이드는 VirtualMCPServer 문서에만 저장할 수 있습니다.",
    /** 화면에서 유일하게 Gateway 의 내부 이름(vMCP)을 푸는 자리다. */
    advancedNote: "Gateway(vMCP) 와 그룹·Entry 문서를 함께 보여 줍니다. 오버라이드는 VirtualMCPServer 문서에만 저장됩니다.",
    /** 백엔드가 고른 Tool 이 없을 때. 개요와 정의 탭이 같이 쓴다. */
    allTools: "(Tool 전체)",
    /** 상세의 칸 이름과 상태 말. */
    labels: {
      group: "group",
      entry: "entry",
      proxy: "proxy",
      backends: "backends",
      conflict: "conflict resolution",
      failure: "failure mode",
      auth: "auth",
      authz: "authz policy",
      runtime: "runtime",
      preview: "Gateway preview",
      namespace: "namespace",
      revisions: "Revisions",
      tools: "Tools",
      accessRevoked: (name: string): string => `접근 회수됨: ${name}`,
      renames: (count: number): string => `, Tool 이름 변경 ${count}개`,
      toolCount: (total: number, conflict: string): string => `Tool ${total}개 · 충돌 해결 ${conflict}`,
    },
  },
  /** 개요 탭. */
  overview: {
    deployedSummary: (revision: number, backends: number): string =>
      `정의 r${revision}${particle(String(revision), "이/가")} 저장돼 있고 백엔드 ${backends}개를 묶어 뒀습니다. 배포하면 Gateway 가 올라가고 여기에 엔드포인트와 백엔드 상태가 나옵니다.`,
    innerHint: "이 하나로 백엔드 여럿의 Tool 을 부릅니다.",
    /** badge 는 인증 경계 값을 적은 배지다. */
    auth: (badge: ReactNode): ReactNode => (
      <>
        인증 경계는{" "}
        {badge}{" "}
        입니다. 플랫폼 auth_mode 가 정하고 여기서 바꿀 수 없습니다.
      </>
    ),
    /** 정의 요약 카드의 줄 이름. 백엔드 줄은 columns.backends 이고 런타임 줄은 detail.deployable.runtime 이다. */
    summary: { conflict: "이름 충돌", failure: "백엔드 실패" },
    /** 살아 있는 백엔드 수. 상태를 아직 못 읽었으면 alive 는 "?" 다. 문장 아래에 백엔드 목록이 붙는다. */
    alive: (alive: number | string, expected: number): string => `${alive}/${expected} 살아 있습니다`,
    noBackendRows: "아직 백엔드 줄이 없습니다.",
  },
  /** 정의 탭. */
  definition: {
    authFixed: "플랫폼 auth_mode 가 정합니다. 여기서 바꿀 수 없습니다.",
  },
  /** 배포 탭과 그 안의 광고 Tool 카드. */
  deployment: {
    emptyBody: (revision: number): string => `클러스터와 네임스페이스, 외부 노출을 정하면 정의 r${revision}${particle(`r${revision}`, "으로/로")} Gateway 를 올립니다.`,
    hint: "배포할 때 정한 자리와, 그때 해석된 백엔드입니다.",
    resolvedBackends: "해석된 백엔드",
    toolsHint: "배포 시점 스냅샷에서 계산한 광고 Tool 입니다. 실제 목록은 엔드포인트에 tools/list 를 호출해 확인합니다.",
    /** 이름이 바뀐 광고 Tool 배지의 title. original 은 바뀌기 전 이름이다. */
    renamedFrom: (original: string): string => `${original}에서 이름을 바꿈`,
  },
  /** 게시된 Gateway 의 저장이 forbid 문장을 더해 닫힐 소비자 Gateway (P57 6.2절, 저장 답의 consumer_notice). */
  consumerNotice: {
    closed: (names: string): string =>
      `다시 배포하면 이 Gateway 를 백엔드로 사용하는 Gateway(${names})의 호출을 모두 거절합니다. Authz policy (Cedar) 에 forbid 문장이 있기 때문입니다.`,
  },
  /** 배포 탭의 허브 게시 카드와 삭제 대화상자의 게시 경고. 게시 해제 확인 문구는 detail.publishSection.unpublishPublished 다. */
  publish: {
    modeHint: "entry: URL 만 게시합니다 / proxy: MCPRemoteProxy 로 감쌉니다.",
    nameHint: "Name 을 비우면 Gateway 이름을 사용합니다. proxy 는 별도 정책과 감사를 적용합니다. 같은 이름으로 다시 게시하면 그대로입니다.",
    empty: "아직 게시된 항목이 없습니다.",
    /** 게시된 Gateway 의 삭제 경고. 문장 아래에 막고 있는 허브 항목 링크가 붙는다. */
    deleteBlocked: "이 Gateway 가 MCP Hub 에 게시돼 있어 그대로는 지울 수 없습니다. 아래 항목의 게시를 먼저 해제해 주세요.",
    title: "허브에 게시",
    mode: "Mode",
    /** 토큰 인증에서 다른 Gateway 가 이 Gateway 를 백엔드로 쓰면 지켜지는 것 (P57 6.2절). */
    consumerHint:
      "다른 Gateway 가 이 Gateway 를 백엔드로 사용하면 그 Gateway 가 이 Gateway 의 Tool 규칙을 함께 지킵니다. Authz policy (Cedar) 에 forbid 문장이 있는 Gateway 는 백엔드로 사용할 수 없습니다.",
    /** 게시 방식 선택 칸의 낭독 이름. */
    labels: {
      modeSelect: "게시 방식",
    },
  },
};
