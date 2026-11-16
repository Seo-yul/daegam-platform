import type { AgentDecisionType } from "@/api/types";
import type { RefineMode } from "@/components/GenerateMenu";
import { particle } from "@/lib/particle";

/** 여러 영역이 그리는 공용 컴포넌트 가운데 이름이 A~K 로 시작하는 것 (F3a). 키는 컴포넌트 이름이다. */
export const shared = {
  /** 저장 단추가 저장하는 동안의 말. 접근 정책, 오버라이드, 노출 카드가 같이 쓴다. */
  saving: "저장 중…",
  /**
   * 저장을 마친 뒤의 알림. Backend API 접속 정보, Database 와 Knowledge Base 편집, Cluster 의 두 편집 카드, Group Mappings,
   * Platform Settings, Tenant Settings 가 같이 쓴다.
   */
  saved: "저장했습니다.",
  /** 저장 단추. Backend API 만들기, Database 만들기와 편집, Knowledge Base 편집이 같이 쓴다. */
  save: "저장",
  /** 새로 만드는 폼의 만들기 단추. MCP Server, Gateway, Knowledge Base, Skill 폼과 사용자 추가가 같이 쓴다. */
  create: "만들기",
  /** 편집 폼을 저장한 값으로 되돌리는 단추. Platform Settings, Tenant Settings, Cluster 의 두 편집 카드가 같이 쓴다. */
  revert: "되돌리기",
  /** 고친 칸만 보내는 저장 단추. Platform Settings 와 Tenant Settings 가 같이 쓴다. */
  saveChanges: "변경 저장",
  /** 표에서 줄 하나를 빼는 단추. Group Mappings, provider 표, Tenant Settings 의 Members 가 같이 쓴다. */
  remove: "제거",
  /** 클러스터나 네임스페이스를 비워 테넌트 기본값을 쓰는 선택지. 배포 대화상자, 노출 섹션, 리비전 미리보기 고르개가 같이 쓴다. */
  tenantDefault: "(테넌트 기본값)",
  /** Ingress class 를 비워 두거나 resources 를 적지 않아 클러스터 기본값을 쓸 때. 노출 섹션과 resources 편집기가 같이 쓴다. */
  clusterDefault: "(클러스터 기본값)",
  /** 경로마다 이전 값과 이후 값을 견주는 표의 열. Advanced 편집기와 리비전 차이 표가 같이 쓴다. */
  diffColumns: { path: "경로", before: "이전", after: "이후" },
  /** 아직 고르지 않은 셀렉트의 빈 선택지. Knowledge Base 만들기와 캔버스 인스펙터의 셀렉트가 같이 쓴다. */
  select: "(선택)",
  /** 입력한 것을 목록에 더하는 단추. Knowledge Base 의 URL 추가와 캔버스 MCP 노드의 허용 Tool 이 같이 쓴다. */
  add: "추가",
  /** 파일로 받는 단추. Skill 상세의 제목 줄과 리비전 탭, Agent 의 피드백 탭이 같이 쓴다. */
  export: "내보내기",
  /** 색인이 실패한 문서 수. 색인된 문서 수 뒤에 붙는다. Knowledge Base 상세와 캔버스의 지식 패널이 같이 쓴다. */
  failedDocuments: (count: number): string => `· 실패 ${count}`,
  /** 마지막으로 바꾼 사람. 수정 시각 뒤에 붙는다. */
  byUser: (user: string): string => `by ${user}`,
  /** 편집 화면의 제목. 쪽 제목이라 두 언어 모두 영어다(ui.md 3절). */
  editTitle: (name: string): string => `Edit ${name}`,
  /** 비워 두면 기본값을 쓰는 선택지. 뒤에 붙일 때는 앞에 공백을 둔다. */
  defaultValue: "(기본값)",
  /** 허브 항목과 Skill 상세의 접근 정책 카드. hint 는 scope 를 풀어 쓴 말이다. */
  accessPolicyCard: {
    preview: "이대로 저장하면",
    saved: (scope: string, hint: string): string => `정책을 저장했습니다. scope ${scope}, ${hint}.`,
    save: "정책 저장",
    manageRequired: "정책을 바꾸려면 manage 권한이 필요합니다.",
    title: "Access policy",
    updated: "updated",
  },
  /** 접근 정책 편집기. owner 는 항목을 가진 테넌트를 부르는 말이다(게시한 테넌트, 소유 테넌트). */
  accessPolicyEditor: {
    allTenants: "모든 테넌트",
    ownerOnly: (owner: string): string => `${owner}만`,
    allTenantsCheck: "모든 테넌트 (*)",
    ownerFixed: (owner: string): string => `${owner} 안에서만 열 수 있습니다.`,
    defaultRole: (role: string): string => `(기본: ${role})`,
    groupPlaceholder: "그룹 이름",
    hint: "비우면 view 는 viewer 이상, consume·manage 는 developer 이상입니다. role 과 그룹은 둘 중 하나만 맞아도 됩니다.",
    crossTenant: (owner: string): string => `이 종류의 항목은 다른 테넌트에 열 수 없습니다. 테넌트는 ${owner}${particle(owner, "으로/로")} 고정됩니다.`,
    share: (scope: string): string => `다른 테넌트에 열려면 ${scope} 권한이 필요합니다. 테넌트 추가와 * 는 platform-admin 만 할 수 있습니다.`,
    collapsed: "비우면 기본 정책입니다",
    tenants: "Tenants",
    minRole: "Min role",
    oktaGroups: "Okta groups",
  },
  /** Advanced 탭의 YAML 편집기. path 는 잠기거나 덮어쓰이는 필드 경로다. */
  advancedEditor: {
    noDocuments: "문서가 없습니다.",
    notMapping: "최상위가 매핑 하나여야 합니다",
    /** 편집기 줄 옆 표시에 손을 올리면 보이는 말. */
    mark: {
      locked: (path: string): string => `플랫폼이 관리합니다: ${path}`,
      ignored: (path: string): string => `오퍼레이터가 덮어씁니다: ${path}`,
      error: (path: string): string => `오류: ${path}`,
    },
    /** 보기 탭. 어긋난 곳의 수를 모르면(실제 객체를 못 읽음) null 이다. */
    view: {
      edit: "편집 (최종본)",
      overrides: (count: number): string => `오버라이드 차이 (${count})`,
      drift: (count: number | null): string => `어긋난 곳 (${count ?? "?"})`,
    },
    dirty: "저장 안 함",
    liveUnreadable: "실제 객체 못 읽음",
    discard: "버리기",
    save: "오버라이드 저장",
    touchedLocked: "플랫폼이 관리하는 줄을 고쳤습니다. 이대로 저장하면 서버가 거부합니다.",
    saved: (paths: number): string => `저장했습니다. 오버라이드 경로 ${paths}개.`,
    validation: (result: string): string => `검증: ${result}`,
    overridesTitle: "플랫폼이 그린 것 → 오버라이드를 얹은 최종본",
    unreadable: (detail: string): string => `편집기 내용을 읽지 못했습니다: ${detail}`,
    noOverrides: "오버라이드가 없어 최종 결과가 플랫폼이 그린 클러스터 리소스와 같습니다.",
    noLive: "실제 객체가 없습니다. 클러스터 리소스가 아직 만들어지지 않았습니다.",
    managers: (managers: string): string => `이 리소스를 함께 고치는 다른 주체가 있습니다: ${managers}`,
    resetting: "되돌리는 중…",
    reset: "플랫폼 값으로 되돌리기",
    absorbing: "가져오는 중…",
    absorb: "오버라이드로 가져오기",
    driftTitle: "최종본 → 클러스터의 실제 객체",
    driftUnknown: "실제 객체를 읽지 못해 어긋난 곳을 알 수 없습니다.",
    noDrift: "어긋난 곳이 없습니다.",
    locked: { title: "플랫폼이 관리하는 값", hint: "여기를 바꾸면 서버가 거부합니다 (422 locked_field)." },
    ignored: {
      title: "오퍼레이터가 덮어쓰는 값",
      hint: "ToolHive proxyrunner 가 항상 다른 값으로 바꿉니다. 조용히 무시되는 대신 서버가 거부합니다 (422 ignored_by_operator).",
    },
    currentOverrides: "지금 걸린 오버라이드",
    none: "없습니다",
    notInDocument: "이 문서에는 없습니다.",
    absent: (count: number): string => `이 문서에 없는 경로 ${count}개`,
    yamlPrefix: "YAML:",
    serverPaths: (count: number): string => `서버가 관리하는 경로 (${count})`,
  },
  /** Agent 의 외부 노출 섹션. */
  agentExposureSection: {
    oidcOnly: "외부 노출은 oidc 모드에서만 켤 수 있습니다. 플랫폼이 header 인증 모드라 런타임이 외부 토큰을 검증할 수 없습니다.",
    /** allowed_groups 를 비웠을 때 누가 부르는가. local 은 테넌트 멤버이고 그 밖은 인증된 모두다. */
    allUsers: "인증된 모든 사용자가 이 Agent 를 부를 수 있습니다.",
    tenantMembers: "이 테넌트의 멤버만 부를 수 있습니다.",
    corsHint: "브라우저에서 직접 부르는 origin 을 줄마다 하나씩 적습니다 (예: https://app.example.com). 비우면 CORS 헤더를 내지 않습니다.",
    corsInvalid: (origins: string): string => `origin 은 http(s)://host[:port] 형식이어야 합니다: ${origins}`,
    /** emptyNote 는 비웠을 때 누가 부르는가의 문장(allUsers, tenantMembers)이다. */
    groupsHint: (emptyNote: string): string =>
      `비우면 ${emptyNote} 그룹을 적으면 그 그룹에 속한 사용자와 Tenant Settings 의 agent_client_ids 에 있는 클라이언트만 통과합니다.`,
    groupsPlaceholder: "그룹 이름을 적고 Enter",
    /** 외부 노출 칸 이름. */
    labels: {
      cors: "CORS origins",
      groups: "Allowed groups",
    },
  },
  /** Backend API 버전의 OpenAPI 문서 팝업. */
  apiDocsDialog: {
    title: (name: string): string => `${name} API 문서`,
    noVersion: "아직 저장된 버전이 없습니다.",
    loading: "문서를 불러오는 중입니다…",
    loadingViewer: "뷰어를 불러오는 중입니다…",
  },
  /** 승인 카드 (HITL). */
  approvalCard: {
    decision: { approve: "승인", edit: "수정 후 승인", reject: "거부", respond: "메시지로 답하기" } satisfies Record<AgentDecisionType, string>,
    /** 결정을 만들지 못한 까닭. detail 은 JSON 파서의 말이다. */
    error: {
      messageRequired: "메시지를 입력해 주세요.",
      argsNotObject: "인자는 JSON 객체여야 합니다.",
      json: (detail: string): string => `JSON 오류: ${detail}`,
      noDecision: "결정을 선택해 주세요.",
    },
    decided: "결정함",
    decidedCount: (count: number): string => `${count}개 Tool 호출을 결정했습니다.`,
    needed: "승인 필요",
    waitingCount: (count: number): string => `${count}개 Tool 호출이 결정을 기다립니다.`,
    externalFiles: "외부에서 가져온 문서를 사용합니다.",
    editArgs: "수정한 인자 (JSON)",
    editHint: "인자를 고친 뒤 승인합니다. Tool 이름은 바꿀 수 없습니다.",
    message: "메시지",
    respondPlaceholder: "Tool 을 실행하지 않고 모델에게 전할 말",
    rejectPlaceholder: "거부 사유 (선택)",
    submit: "결정 보내기",
    sent: "결정을 보냈습니다.",
    /** 요청의 이유가 `outside` 일 때 (P43 4절). 아래 줄은 그 표시가 이 대화의 것임을 알린다. */
    outsideReason: "이 대화에 외부 내용이 들어와 승인이 필요합니다.",
    newChatHint: "새 대화는 외부 내용 없이 시작합니다.",
    /** 결정과 함께 기억하는 선택 (P43 5절). 세션 소유자가 기억할 수 있는 요청에만 보인다. */
    rememberApprove: "다음부터 묻지 않고 승인",
    rememberReject: "다음부터 묻지 않고 거부",
    rememberHint: (days: number): string => `기억한 선택은 ${days}일 동안 이 Agent 와의 대화에 적용되고 계정 화면에서 지울 수 있습니다. 대화에 외부 내용이 들어오면 다시 승인을 요청합니다.`,
    /**
     * 청지기의 Agent 정의 제안 (P48 6.4·6.5절). 카드는 플랫폼이 비교한 덩어리를 그리기만 한다. base 는 기준 리비전 번호다.
     * attention 은 저장하기 전에 먼저 볼 변경의 문장이고 키가 플랫폼의 code 다. 값은 플랫폼이 문서에서 읽은 이름이다.
     * textChanges 는 캔버스 인스펙터의 앞·뒤 상자 제목도 되고, approve 는 캔버스 안내 줄의 단추 이름도 된다.
     */
    proposal: {
      titleNew: (name: string): string => `새 Agent 제안: ${name}`,
      titleUpdate: (name: string, base: number): string => `${name} 변경 제안 (r${base} 기준)`,
      summary: (added: number, changed: number, removed: number): string => `노드 ${added}개 추가, ${changed}개 변경, ${removed}개 삭제`,
      deployed: (deployed: number): string => `지금 배포된 리비전은 r${deployed}입니다. 저장한 뒤 다시 배포해야 실행 중인 Agent 에 반영됩니다.`,
      attentionTitle: "저장하기 전에 확인할 점",
      attention: {
        system_prompt_changed: (agent: string): string => `${agent}의 시스템 프롬프트나 설명을 새로 쓰거나 바꿉니다`,
        tool_source_added: (source: string): string => `Tool 소스 ${source}${particle(source, "을/를")} 연결합니다`,
        write_without_approval: (tool: string): string => `쓰기 Tool ${tool}${particle(tool, "이/가")} 승인 없이 실행됩니다`,
        approval_removed: (tool: string): string => `${tool}의 승인을 없앱니다`,
        guardrail_weakened: (position: string): string => `${position} 가드레일을 없애거나 완화합니다`,
        web_reach_widened: "웹 fetch 가 갈 수 있는 도메인을 넓힙니다",
        rate_limit_removed: "사용자별 호출 한도를 없애거나 늘립니다",
        knowledge_attached: (knowledge: string): string => `지식 ${knowledge}${particle(knowledge, "을/를")} 붙입니다. 이 Agent 와 대화하는 사람은 모두 그 문서를 검색할 수 있습니다`,
        code_execution: "코드 실행을 켜거나 실행 전 승인을 없앱니다",
        model_changed: (agent: string, model: string): string => `${agent}의 모델을 ${model}${particle(model, "으로/로")} 바꿉니다`,
        service_changed: "예산이나 대화 기록 설정을 바꿉니다",
        skill_attached: (skill: string): string => `Skill ${skill}${particle(skill, "을/를")} 붙입니다. 청지기는 Skill 본문을 읽지 않았습니다`,
        memory_changed: "장기 기억을 켜거나 보관 기간을 늘립니다",
      },
      textChanges: "바뀌는 글",
      textCut: "바뀐 곳이 많아 일부만 보입니다. 캔버스에서 전체를 볼 수 있습니다.",
      openCanvas: "캔버스에서 보기",
      approve: "제안대로 저장",
      rejectPlaceholder: "고칠 점 (선택)",
    },
  },
  /** Agent 상세의 승인함. */
  approvalInbox: {
    title: "승인함",
    count: (pending: number, seconds: number): string => `보류 ${pending}건 (${seconds}초마다 갱신)`,
    empty: "보류 중인 승인이 없습니다.",
    pick: "왼쪽에서 세션을 선택하면 승인 요청이 보입니다.",
    forbidden: "이 세션의 소유자이거나 agent:approve 권한이 있어야 결정할 수 있습니다.",
    /** 결정을 보낸 뒤 재개 스트림의 상태 배지. 그 옆의 닫기 단추는 ui.modal.close 다. */
    outcome: { busy: "재개 중", failed: "실패", done: "재개됨" },
    /** 결과가 아직 없는 Tool 호출 줄의 끝. */
    running: "실행 중",
    turnDone: (turn: number, tokens: number): string => `턴 ${turn} 완료 · 토큰 ${tokens}`,
    interrupted: "다시 승인이 필요합니다. 목록에서 세션을 다시 선택해 주세요.",
    pending: "pending",
    tool: "Tool",
    /** 자동 실행이 기다리는 승인의 마감 (P41 7절). 승인자에게만 보이는 실행 줄이다. */
    runDeadline: (time: string): string => `${time}까지 결정하지 않으면 거절됩니다.`,
    /** 잠든 Agent 의 승인함 (P44 8절). 목록을 읽지 않고 잠들 때 남긴 승인 대기 수를 보인다. */
    sleeping: (count: number): string =>
      `이 Agent 는 사용하지 않아 파드를 내려 두었습니다. 승인 대기 ${count}건은 내려 두기 직전의 값이고, 깨우면 목록을 불러옵니다.`,
  },
  /** MCP Server 목록의 Backend API 칸. 첫 이름 뒤에 나머지 수를 적는다. */
  backendApiLink: { more: (count: number): string => `외 ${count}개` },
  /** Database 바인딩 편집. */
  bindingsEditor: {
    empty: "바인딩이 없습니다.",
    remove: "제거",
    add: "바인딩 추가",
    dataService: "Database",
    selectDataService: "(Database 선택)",
    envPrefix: "환경 변수 접두어",
  },
  /** 메트릭 탭의 호출 카드(components/metrics/CallsCard). Agent 의 모델 호출·Tool 실행과 MCP Server·Gateway 의 Tool 별·백엔드별 호출이 같이 쓴다. */
  callsCard: {
    calls: "호출",
    errors: "오류",
    p95: "p95 (ms)",
    /** 카드 머리의 합계. 값은 화면 언어로 적은 수이고 모르면 "-" 다. */
    callTotals: (calls: string, errors: string, p95: string): string => `호출 ${calls}건 · 오류 ${errors}건 · p95 ${p95} ms`,
    /** 표가 빌 때. MCP Server·Gateway 의 메트릭 탭은 기간에 호출이 하나도 없을 때도 이 문장을 쓴다. */
    noCalls: "이 기간에 호출이 없습니다.",
  },
  /** Agent 채팅 패널의 세션 줄, 빈 전사, 입력칸. */
  chatPanel: {
    session: "세션",
    newOnFirstMessage: "(첫 메시지에서 새로 만듭니다)",
    stop: "중단",
    newSession: "새 세션",
    restoring: "지난 대화를 불러오는 중입니다…",
    greeting: "인사말",
    empty: "메시지를 보내면 대화가 시작됩니다. Tool 호출과 승인 요청이 여기에 보입니다.",
    newChat: "새 대화",
    notReady: "Agent 가 Ready 가 아닙니다.",
    input: "메시지",
  },
  /** 쌓인 막대(components/charts/DailyBars)의 칸마다 손을 올리면 보이는 말. 사용량 화면과 메트릭 탭이 같이 쓴다. */
  dailyBars: {
    part: (day: string, name: string, value: string): string => `${day} · ${name}: ${value}`,
  },
  /** MCP Server 와 Gateway 의 배포 대화상자. */
  deployDialog: {
    clusterHint: "비우면 테넌트 기본 클러스터를 사용합니다.",
    deploying: "배포 중…",
    submit: "배포",
    exposureLegend: "Namespace and exposure",
    /** 배포 대화상자의 칸 이름. */
    labels: {
      cluster: "Cluster",
    },
  },
  /**
   * 상세 제목 줄의 공통 동작과 그 진행 중 문구. 삭제는 common.delete 다. 재배포와 편집은 배포하는 리소스(MCP Server, Gateway)
   * 상세가 같이 쓰고 Agent 상세도 쓸 것이다(F7b).
   */
  detailActions: {
    undeploy: "배포 해제",
    undeploying: "내리는 중…",
    clone: "복제",
    redeploy: "재배포",
    redeploying: "재배포 중…",
    editRevision: "편집(새 리비전)",
  },
  /** 화면 하나가 그리다 터졌을 때 본문 자리의 문구. */
  errorBoundary: {
    title: "이 화면을 그리지 못했습니다.",
    help: "다른 화면은 그대로 사용할 수 있습니다. 다시 그리기를 눌러도 같으면 이 문구와 함께 알려 주세요.",
    retry: "다시 그리기",
  },
  /** 상세 배포 탭의 노출 설정 카드. */
  exposureCard: {
    hint: "저장하면 Ingress·앞단을 다시 만들고 재배포됩니다.",
    saved: "저장했습니다. 재배포됩니다.",
    save: "저장하고 재배포",
    title: "Exposure",
  },
  /** 외부 노출 섹션. 배포 대화상자와 상세 Basic 탭이 같이 쓴다. */
  exposureSection: {
    /** 노출 토글을 잠근 까닭. */
    disabled: {
      cluster: "이 클러스터는 외부 노출을 허용하지 않습니다 (Cluster ingress.enabled 가 false 입니다).",
      unauthenticated:
        "header 인증 모드에서는 인증 없이 노출되므로 이 클러스터에서는 막혀 있습니다 (Cluster ingress.allow_unauthenticated 가 false 입니다).",
    },
    namespaceFixed: "배포 네임스페이스는 생성 뒤 바꿀 수 없습니다.",
    namespaceHint: "배포와 Ingress·TLS Secret 이 놓이는 네임스페이스입니다. Tenant Settings 의 허용 목록에서 선택합니다.",
    clusterFirst: "(클러스터를 먼저 선택해 주세요)",
    loading: "노출 옵션을 불러오는 중입니다…",
    /** missing 은 모자란 권한을 쉼표로 이은 것이다. */
    forbidden: (missing: string): string =>
      `이 클러스터에서 TLS Secret 목록을 볼 권한이 없습니다 (${missing}). 최신 rbac.yaml 을 적용하면 Secret 모드를 선택할 수 있습니다. 지금은 TLS 없음 또는 cert-manager 만 선택할 수 있습니다.`,
    enable: "외부 노출",
    enableHint: "Ingress 로 클러스터 밖에 엽니다.",
    unauthenticated: "인증 없이 외부에 노출됩니다. 플랫폼이 header 인증 모드라 이 주소에는 누구나 접근할 수 있습니다.",
    classHint: "Ingress 컨트롤러를 선택하는 이름입니다. 클러스터가 허용한 것만 보입니다.",
    hostHint: "소문자 DNS-1123 호스트명입니다. 같은 클러스터의 다른 배포와 겹칠 수 없습니다.",
    hostInvalid: "소문자 영숫자와 하이픈, 점만 쓸 수 있습니다.",
    tlsHint: {
      edge: "이 클러스터는 Ingress 앞에서 TLS 를 끝냅니다. 주소는 https 이고 다른 방식은 선택할 수 없습니다.",
      choose: "없음이면 http 로 열고, Secret 이면 그 이름의 인증서를, cert-manager 면 ClusterIssuer 가 발급한 인증서를 사용합니다.",
    },
    tlsEdge: "앞단에서 종단 (https)",
    tlsNone: "없음 (http)",
    noSecrets: "이 네임스페이스에 type=kubernetes.io/tls Secret 이 없습니다.",
    secretsForbidden: "Secret 목록을 볼 수 없어 Secret 모드는 선택할 수 없습니다.",
    noIssuers: "클러스터에 cert-manager ClusterIssuer 가 없습니다.",
    secretHint: "배포 네임스페이스의 type=kubernetes.io/tls Secret 입니다. 이름만 조회하고 내용은 읽지 않습니다.",
    select: "(선택해 주세요)",
    issuerHint: "인증서 Secret 은 플랫폼이 <k8s.name>-tls 로 이름 짓고 cert-manager 가 만듭니다.",
    issuerDefault: (issuer: string): string => `(클러스터 기본값: ${issuer})`,
    tlsNote: "TLS 를 선택하면 https 로 노출됩니다.",
    tlsSecretOption: "TLS Secret 사용 (https)",
    certManagerOption: "cert-manager 사용 (https)",
    /** 외부 노출 섹션의 칸 이름. */
    labels: {
      namespace: "Namespace",
      ingressClass: "IngressClass",
      host: "Host",
      tls: "TLS",
      tlsSecret: "TLS Secret",
      issuer: "ClusterIssuer",
    },
  },
  /** 상세의 노출 상태 배지와 external_url 줄. 세 갈래 배지는 참, 거짓, 모름(아직 못 읽음)의 말이다. */
  exposureStatus: {
    front: (phase: string): string => `앞단 ${phase}`,
    frontPublishes: "앞단이 공개",
    address: { yes: "주소 할당됨", no: "주소 대기", unknown: "주소 ?" },
    tlsSecret: { yes: "TLS Secret 있음", no: "TLS Secret 없음", unknown: "TLS Secret ?" },
    certificate: { yes: "인증서 준비됨", no: "인증서 대기", unknown: "인증서 ?" },
    unauthenticated: "무인증 노출",
    notExposed: "외부 노출 안 함",
    /** 복사 단추 문장 가운데 들어가는 명사. */
    copyLabel: "외부 주소",
  },
  /** Backend API 버전 행의 다듬기 메뉴. Refine 화면의 제목도 mode 를 쓴다. */
  generateMenu: {
    mode: { minify: "규칙으로 정리", llm: "LLM 으로 다듬기" } satisfies Record<RefineMode, string>,
    hint: { minify: "spec2openapi 규칙으로 즉시 정리합니다.", llm: "등록된 LLM provider 로 설명을 다듬습니다." } satisfies Record<RefineMode, string>,
    label: "다듬기",
    title: (version: number): string => `v${version} 의 설명을 규칙 또는 LLM 으로 다듬어 새 버전을 만듭니다.`,
  },
  /** 상세 셋이 같이 쓰는 상태 배너. */
  instanceStatus: {
    stale: (seconds: number): string => `연결이 끊겨 마지막 상태를 표시하고 있습니다. ${seconds}초마다 다시 시도합니다.`,
    unreachable: "클러스터에서 지금 상태를 읽지 못했습니다. 아래는 마지막으로 알려진 값입니다.",
    lastError: (text: string): string => `마지막 오류: ${text}`,
  },
  /** Knowledge Base 상태 배지에 손을 올리면 보이는 말. */
  knowledgeBadges: {
    orphanedTable: (table: string): string => `벡터 테이블(${table})이 Database 에 남아 있습니다. 다시 삭제하면 같은 순서로 다시 시도합니다.`,
    /** at 은 화면 언어로 적은 삭제 시작 시각이다. */
    deletingSince: (at: string): string => `삭제 시작 ${at}`,
  },
  /** 여러 표가 같이 쓰는 열 이름. */
  columns: {
    tool: "Tool",
    tools: "Tool",
  },
};
