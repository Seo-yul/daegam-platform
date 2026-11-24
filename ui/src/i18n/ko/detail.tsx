import type { ReactNode } from "react";

import type { SpecChangeKind } from "@/api/types";
import type { QuantityProblem } from "@/components/ResourcesEditor";
import type { RecentKind } from "@/components/overview/RecentCard";
import type { ConnectMode } from "@/lib/connectSnippets";
import { particle } from "@/lib/particle";

/** 여러 영역이 그리는 공용 컴포넌트 가운데 이름이 L~Z 로 시작하는 것과 detail·illustrations·markdown·overview (F3b). */
export const detail = {
  /** 변경 수와 채택 수. 스펙 다듬기(SpecDiff)와 오버라이드 제안(OverrideChanges)이 같이 쓴다. */
  changeCount: (changes: number, accepted: number): string => `변경 ${changes}개 · 채택 ${accepted}개`,
  /** 리비전이 담는 것. Agent 는 캔버스 그래프이고 나머지는 정의다. 배포 상태 배너와 복제 대화상자가 id 로 고른다. */
  revisionSubject: { graph: "그래프", definition: "정의" },
  /** 목록의 툴바와 페이지 바(ListControls). */
  listControls: {
    deployedOnly: "배포된 것만",
    viewToggle: "목록 모양",
    cardView: "카드로 보기",
    tableView: "표로 보기",
    /** 낭독기만 읽는 페이지 위치. */
    pageInfo: (total: number, start: number, end: number): string => `${total}개 중 ${start}번째부터 ${end}번째까지 보입니다.`,
    /** 검색어와 맞는 것이 없을 때. 종류를 말하지 않는 목록(MCP Hub, Database, Knowledge Base)이 같이 쓴다. */
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 것이 없습니다.`,
    previousPage: "이전 페이지",
    nextPage: "다음 페이지",
  },
  /** 리소스 폼의 이름 두 칸(NameFields). */
  nameFields: {
    /** 기본 예시. Backend API 가 가장 흔한 REST API 등록이라 그것을 쓴다. 표시 이름과 Name 이 서로 대응되는 한 쌍이다. */
    placeholders: { display: "주문 API", name: "orders-api" },
    immutable: "이름은 생성 뒤 바꿀 수 없습니다.",
    /** 클러스터에 생길 리소스 이름의 미리보기. name 은 고정폭으로 꾸민 이름이다. */
    preview: (name: ReactNode): ReactNode => <>클러스터 리소스 이름: {name}</>,
    /** 표시 이름과 이름 칸. */
    labels: {
      display: "Display name",
      name: "Name",
    },
  },
  /** 없는 리소스를 열었을 때의 화면(NotFoundPage). resource 는 화면에 쓰는 리소스 이름이다("MCP Server"). */
  notFoundPage: {
    title: "찾을 수 없습니다",
    body: (resource: string): string => `요청한 ${resource}${particle(resource, "이/가")} 없습니다. 이미 삭제됐거나 주소가 잘못됐을 수 있습니다.`,
    back: "목록으로 돌아가기",
  },
  /** 410 으로 지워진 것을 알게 된 상세(MissingResourceScreen). date 는 화면 언어로 적은 삭제 시각이다. */
  missingResource: {
    gone: (resource: string, date: string): string => `이 ${resource}${particle(resource, "은/는")} ${date}에 삭제됐습니다.`,
  },
  /** API 문서 뷰어(OpenApiDocs). 오퍼레이션 목록과 고른 오퍼레이션의 파라미터·본문·응답·SOAP 계약이다. */
  openApiDocs: {
    operations: "오퍼레이션 목록",
    search: "오퍼레이션 검색",
    searchPlaceholder: "검색 (이름·경로·요약)",
    noMatch: "일치하는 오퍼레이션이 없습니다.",
    noOperations: "이 문서에는 오퍼레이션이 없습니다.",
    toolName: "이 값이 MCP Tool 이름이 됩니다.",
    parameters: "파라미터",
    /** 파라미터 표와 스키마 표의 열. type 은 본문 형식 옆의 이름표로도 쓴다. */
    columns: { name: "이름", in: "위치", type: "형", description: "설명", property: "속성" },
    noParameters: "파라미터가 없습니다.",
    requestBody: "요청 본문",
    noBodySchema: "본문 스키마가 없습니다.",
    responses: "응답",
    noResponses: "응답 정의가 없습니다.",
    soap: "SOAP 계약",
    required: "필수",
    circular: "(순환)",
    circularHint: "위에서 펼친 스키마가 다시 나옵니다.",
    openapi: "OpenAPI",
    server: "server",
    deprecated: "deprecated",
    operationId: "operationId",
    requiredBadge: "required",
    soapFields: { soapAction: "soapAction", soapVersion: "soapVersion", style: "style", endpoint: "endpoint", inputElement: "input element", outputElement: "output element" },
  },
  /** 도구 오버라이드 제안의 변경 목록(OverrideChanges, D37). */
  overrideChanges: {
    field: { name: "노출 이름", description: "설명" } satisfies Record<"name" | "description", string>,
    empty: "바꿀 것이 없습니다. 모델이 지금 이름과 설명을 그대로 두기로 했습니다.",
    title: "제안",
    clearAll: "전체 해제",
    selectAll: "전체 선택",
    wholeNote: "이 Tool 에는 아직 오버라이드가 없어서 이름과 설명이 함께 들어갑니다.",
    /** 오버라이드가 없던 칸의 이전 값. */
    none: "없음",
    backend: "Backend",
  },
  /** CR phase 의 점과 배지(PhaseBadge). 낭독기만 읽는 상태 문장이다. */
  phaseBadge: {
    unknown: "알 수 없음",
    dot: (phase: string): string => `상태: ${phase}`,
    live: (phase: string): string => `상태: ${phase}`,
  },
  /** OpenRouter provider 의 업체 선호 칸(ProviderRoutingFields, T19). */
  providerRouting: {
    order: "먼저 쓸 업체 (예: moonshotai, deepinfra)",
    noFallback: "목록 밖 업체로 넘어가지 않음",
    requireParameters: "Tool 같은 요청 인자를 모두 지원하는 업체만",
    quantizations: "허용할 양자화 (예: fp8, bf16)",
    /** 업체 선호 칸의 낭독 이름. */
    labels: {
      order: "업체 우선순위",
      quantizations: "허용할 양자화",
    },
  },
  /** MCP Server 의 허브 게시와 해제(PublishSection). */
  publishSection: {
    /** 삭제 확인 대화상자의 참조 경고. 문장 아래에 Gateway 링크 목록이 붙는다. */
    deleteWarning: (count: number): string => `허브 게시 항목이 함께 사라지고, 참조 중인 Gateway ${count}개가 Degraded 상태가 됩니다.`,
    publishTitle: "허브에 게시",
    /** 게시 대화상자의 첫 줄. name 은 조사를 고르는 표시 이름이고 target 은 그 이름을 꾸민 요소다. */
    lead: (name: string, target: ReactNode): ReactNode => (
      <>MCP Hub 에 {target}{particle(name, "을/를")} internal 항목으로 올립니다. Gateway 가 백엔드로 선택할 수 있게 됩니다.</>
    ),
    placeholders: { display: "주문 MCP", name: "orders-mcp" },
    nameHint: "Name 을 비우면 MCP Server 이름을 사용합니다. 같은 테넌트의 다른 허브 항목과 이름이 겹치면 게시할 수 없습니다.",
    publishing: "게시 중…",
    publish: "게시",
    unpublish: "게시 해제",
    unpublishing: "게시 해제 중…",
    /** internal 항목의 게시 해제 확인 문구. 이 대화상자와 허브 항목 상세가 같이 쓴다. */
    unpublishInternal: "이 항목이 MCP Hub 에서 내려갑니다. MCP Server 자체는 그대로 남습니다. 이 항목을 사용하는 Gateway 가 있으면 게시를 해제할 수 없습니다.",
    /**
     * published 항목의 게시 해제 확인 문구. Gateway 상세의 게시 카드와 허브 항목 상세가 같이 쓴다. 서버는 이 항목을 쓰는
     * Gateway 가 있으면 409 `in_use` 로 거부한다 (services/hub.py delete).
     */
    unpublishAgent: "이 항목이 MCP Hub 에서 내려갑니다. Agent 자체는 그대로 남습니다. 이 Agent 를 Tool 로 사용하는 Agent 의 저장과 배포, 호출은 그때부터 거절됩니다.",
    unpublishPublished: "MCP Hub에서 내려갑니다. Gateway 자체는 그대로 남습니다. 이 항목을 쓰는 다른 Gateway가 있으면 해제할 수 없습니다.",
    /** 409 in_use 로 해제가 막혔을 때. 문장 아래에 Gateway 링크 목록이 붙는다. */
    inUse: "먼저 아래 Gateway 에서 이 항목을 빼야 합니다.",
    published: "Published",
  },
  /** "참조" 카드와 대화상자 안의 참조 목록(ReferencesCard, D43). */
  referencesCard: {
    title: "참조",
    empty: "참조가 없습니다",
    /** 대화상자의 목록 앞 문장. summary 는 종류와 개수다. */
    notice: (summary: string): string => `참조가 있습니다. ${summary}.`,
    /** 읽기 토큰은 갈 곳이 없어 개수만 적는다. */
    tokens: (count: number): string => `${count}개`,
  },
  /** 런타임 resources 편집기(ResourcesEditor). 비었을 때의 요약은 shared.clusterDefault 다. */
  resourcesEditor: {
    hint: "비우면 클러스터 기본값을 사용합니다.",
    jsonMode: "JSON 으로 편집",
    jsonError: "JSON 오류를 고친 뒤 폼으로 돌아갈 수 있습니다.",
    /** 폼에 담을 수 없는 키가 있을 때. keys 는 ", " 로 이은 키 경로다. */
    jsonOnly: (keys: string): string => `폼에 담을 수 없는 키가 있어 JSON 으로 편집합니다: ${keys}`,
    /** 폼이 받지 못하는 quantity 의 안내. */
    problem: {
      cpu: "CPU 는 코어 수(예: 0.5, 1) 또는 정수 밀리코어(예: 500m)로 적습니다.",
      milli: "밀리코어(m)는 정수로 적습니다. 코어 단위 소수는 m 없이 적습니다(예: 0.5).",
      memoryUnit: "단위는 K8s 표기(Ki/Mi/Gi/Ti 또는 k/M/G/T)로 적습니다. 대문자 K 와 B 접미사는 사용할 수 없습니다.",
      memory: "메모리는 바이트 수 또는 Ki/Mi/Gi/Ti, k/M/G/T 단위로 적습니다(예: 256Mi, 1Gi).",
    } satisfies Record<QuantityProblem, string>,
    /** limits 가 requests 보다 작을 때. kind 는 cpu 나 memory 이고 두 값은 적은 그대로다. */
    limitBelowRequest: (kind: string, limit: string, request: string): string =>
      `${kind} limits(${limit}) 가 requests(${request}) 보다 작습니다. 클러스터가 파드 생성을 거부할 수 있습니다.`,
    cpu: "CPU",
    memory: "Memory",
    requests: "Requests",
    limits: "Limits",
  },
  /** 스펙 다듬기 결과의 좌우 diff 와 채택 목록(SpecDiff, D25). */
  specDiff: {
    kind: { changed: "변경", added: "추가", removed: "삭제" } satisfies Record<SpecChangeKind, string>,
    /** 값이 없는 쪽의 미리보기. */
    none: "(없음)",
    summary: (total: number, changed: number): string => `operation ${total}개 중 ${changed}개의 설명이 바뀌었습니다. 호출 표면은 바뀌지 않았습니다.`,
    renamed: "operationId 가 바뀝니다. 이 백엔드를 참조하는 MCP Server 는 다시 배포하기 전에 확인해 주세요.",
    checkingProfiles: "참조하는 MCP Server 를 확인하는 중입니다…",
    /** links 는 ", " 로 이은 MCP Server 링크들이다. */
    profiles: (links: ReactNode): ReactNode => (
      <>
        참조하는 MCP Server:{" "}
        {links}
      </>
    ),
    noProfiles: "이 백엔드를 참조하는 MCP Server 는 없습니다.",
    changeList: "변경 목록",
    acceptAll: "전체 채택",
    acceptNone: "전체 해제",
    noChanges: "바뀐 항목이 없습니다.",
    jump: "이동",
    splitNote: "왼쪽 원본 · 오른쪽 채택 적용",
    inlineNote: "지운 줄과 더한 줄을 한 칸에 이어서 봅니다",
    tooNarrow: "칸이 좁아 좌우로 나눌 수 없습니다",
    inlineView: "인라인으로 보기",
    splitView: "좌우로 보기",
  },
  /** LLM 스펙 작업 화면의 공용 조각(SpecJobForm). */
  specJobForm: {
    defaultProvider: (name: string): string => `기본값은 ${name}입니다.`,
    loadingProviders: "불러오는 중입니다…",
    noProviderOption: "provider 없음",
    /** provider 고르개의 한 줄. 기본값인지와 키가 있는지는 표시만 고른다. */
    providerOption: (name: string, isDefault: boolean, hasKey: boolean): string => `${name}${isDefault ? " (기본)" : ""}${hasKey ? "" : " · 키 없음"}`,
    /** 등록된 provider 가 없을 때. link 는 Platform Settings 로 가는 링크다. */
    noProvider: (link: ReactNode): ReactNode => (
      <>
        등록된 provider 가 없습니다. API 키를 담은 Secret 으로 provider 를 등록해야 LLM 기능을 쓸 수 있습니다.{" "}
        {link}
      </>
    ),
    settingsLink: "Platform Settings 로 이동",
    instruction: "자유 지시 (선택)",
    instructionHint: (length: number, max: number): string => `한 줄, ${length}/${max}자`,
    iterations: (done: number, max: number): string => `반복 ${done}/${max}`,
    /** count 는 화면 언어로 적은 토큰 수다. */
    tokens: (count: string): string => `토큰 ${count}`,
    /** provider 선택 칸 이름. */
    labels: {
      provider: "Provider",
    },
  },
  /** 로딩·오류·빈 상태(StateBlock). 로딩 문구는 common.loading 이다. */
  stateBlock: { retry: "다시 시도" },
  /** 배포 상태 목록의 점 요약 줄(StateCountLine). 미배포 칸은 resources.state.draft 다. */
  stateCountLine: { total: "전체" },
  /** 테넌트 선택기(TenantSelector). */
  tenantSelector: {
    invalid: "이름은 영숫자로 시작하고 [A-Za-z0-9._-] 64자 이내여야 합니다.",
    use: "사용",
    add: "테넌트 직접 입력",
    addHint: "목록에 없는 테넌트를 직접 입력합니다.",
    newTenant: "새 테넌트",
    namePlaceholder: "테넌트 이름",
    /** 테넌트 선택 칸의 낭독 이름. */
    labels: {
      select: "tenant",
    },
  },
  /** 메트릭 탭의 기간 고르개(components/metrics/WindowSelect). 기간의 이름은 탭마다 자기 사전에 있다. */
  windowSelect: { label: "기간" },
  /** Advanced 탭(ManifestTab, D42). */
  manifestTab: {
    loading: "매니페스트를 불러오는 중입니다…",
    editorLoading: "편집기를 불러오는 중입니다…",
  },
  /** 빈 상태의 선 그림(illustrations). 낭독기가 읽는 그림 설명이다. */
  illustrations: {
    backendApi: "문서 하나에서 화살표가 밖으로 나가는 선 그림",
    mcpServer: "Tool 표식 셋이 담긴 상자 선 그림",
    gateway: "선 셋이 한 노드로 모여 하나로 나가는 선 그림",
    agent: "작은 그래프가 담긴 말풍선 선 그림",
    database: "원기둥 모양 데이터베이스 선 그림",
    knowledgeBase: "돋보기로 들여다보는 겹친 문서 선 그림",
    skill: "체크 표시가 붙은 절차서 선 그림",
    cluster: "고리로 이어진 노드 셋 선 그림",
    hub: "MCP 마크",
  },
  /** 마크다운 본문(MarkdownBody, D61). */
  markdown: {
    /** 각주 머리. 낭독기만 읽는다. */
    footnotes: "각주",
    /** 그리지 않는 이미지 대신 보이는 글. 설명(alt)이 없으면 낱말 하나다. */
    image: (alt: string | undefined): string => (alt ? `이미지: ${alt}` : "이미지"),
    /** 코드 블록의 복사 단추가 문장 가운데 넣는 명사. */
    copyCode: "코드",
  },
  /** 개요 탭의 "주소" 카드(AddressCard, D44). */
  addressCard: {
    title: "주소",
    inner: "클러스터 안 주소",
    /** 복사 단추가 문장 가운데 넣는 명사. */
    innerCopy: "클러스터 안 주소",
    external: "외부 주소",
    expose: "배포 탭에서 켜기",
    auth: "인증 방식",
  },
  /** 접힌 "클러스터 상태 자세히"(ConditionsDetails). */
  conditionsDetails: {
    /** 접힌 머리. detail 은 아래의 problems 나 allOk 다. */
    summary: (detail: string): string => `클러스터 상태 자세히 · ${detail}`,
    problems: (count: number): string => `conditions 주의 ${count}개`,
    allOk: (count: number): string => `conditions ${count}개 전부 정상`,
    columns: { type: "종류", status: "상태", reason: "이유", message: "메시지", changed: "바뀐 때" },
  },
  /** "연결" 카드(ConnectCard, P32 10절, P33 16절). */
  connectCard: {
    title: "연결",
    mode: { login: "로그인으로 연결", token: "토큰으로 연결" } satisfies Record<ConnectMode, string>,
    noAddress: "외부 주소가 없어 클러스터 밖의 MCP 클라이언트가 호출할 수 없습니다.",
    toDeploy: "배포 탭으로",
    login: "설정에는 주소만 넣습니다. 처음 부를 때 클라이언트가 브라우저로 로그인과 연결 허용 화면을 엽니다.",
    loginFallback: "클라이언트가 로그인을 시작하지 못하면 토큰으로 연결해 주세요.",
    /** 발급 폼의 이름 기본값. name 은 리소스의 표시 이름이다. */
    defaultLabel: (name: string): string => `${name} 연결`,
    /** env 는 토큰을 담을 환경 변수 이름이다. */
    oidc: (env: string): string =>
      `이 배포본은 IdP 가 발급한 액세스 토큰으로 부릅니다. 플랫폼이 발급한 토큰은 받지 않습니다. IdP 에서 받은 토큰을 ${env}에 넣어 주세요. IdP 가 MCP 클라이언트 등록을 받으면 클라이언트가 스스로 IdP 로그인을 열 수도 있습니다.`,
    anonymous: "인증 없이 열려 있어 이 주소에 접속할 수 있으면 누구나 Tool 을 호출할 수 있습니다.",
    readPatTitle: "토큰을 env 로 두기",
    /** 복사 단추가 문장 가운데 넣는 명사. */
    readPatCopy: "명령",
    readPatNote: "토큰을 명령줄에 쓰면 셸 기록에 남습니다. 이 명령은 입력을 화면에 보이지 않고 받습니다.",
    scope: (tenant: string): string => `이 토큰은 ${tenant}의 Gateway, 노출한 MCP Server, 노출한 Agent 를 부를 수 있습니다.`,
    notMember: "이 테넌트의 멤버만 토큰을 받을 수 있습니다. 멤버 계정으로 로그인해 주세요.",
    /** 조각의 복사 단추가 문장 가운데 넣는 명사. client 는 클라이언트 이름이다. */
    copySettings: (client: string): string => `${client} 설정`,
    /**
     * 클라이언트별 설정 조각의 제목과 곁의 안내(lib/connectSnippets). 파일 경로 제목은 말이 아니라 lib 에 있다. 갈래(token,
     * login)마다 안내가 다르고 anonymous 는 안내가 없다. vscodeInput 은 VS Code 가 토큰을 물을 때 보이는 설명이라 설정 파일에
     * 적히고, 조각을 만드는 순간의 화면 언어를 따른다.
     */
    snippets: {
      commandLine: "명령줄",
      projectJson: "프로젝트 .mcp.json",
      initialize: "initialize 요청",
      vscodeInput: "대감 MCP 토큰",
      claude: {
        token: {
          cli: "셸이 토큰 값을 펼쳐 Claude Code 설정에 그대로 적습니다. 설정에 토큰을 남기지 않으려면 아래 .mcp.json 을 써 주세요.",
          json: "Claude Code 가 실행할 때 env 를 펼칩니다.",
        },
        login: { cli: "추가한 뒤 Claude Code 세션에서 /mcp 로 로그인합니다.", json: "Claude Code 세션에서 /mcp 로 로그인합니다." },
      },
      cursorLogin: "처음 쓸 때 Cursor 가 브라우저 로그인을 엽니다.",
      vscode: { token: "처음 쓸 때 VS Code 가 토큰을 묻습니다.", login: "처음 쓸 때 VS Code 가 브라우저 로그인을 엽니다." },
      codexLogin: "codex mcp login 이 브라우저 로그인을 엽니다.",
    },
  },
  /** "정의 요약" 카드(DefinitionSummaryCard). */
  definitionSummaryCard: { title: "정의 요약" },
  /** "준비 상태" 카드(ReadinessCard). */
  readinessCard: {
    title: "준비 상태",
    readyPods: "준비된 파드",
    uptime: "가동 시간",
    /** date 는 Ready 가 된 시각이다. */
    since: (date: string): string => `${date}부터`,
    observed: "확인 시각",
    pollEvery: (seconds: number): string => `${seconds}초마다 확인`,
    cluster: "클러스터",
    revision: "리비전",
    notDeployed: "아직 배포하지 않았습니다",
    /** 돌고 있는 리비전이 저장된 정의와 다를 때. */
    savedRevision: (revision: number): string => `저장된 정의는 r${revision}입니다`,
    message: "메시지",
    error: "오류",
  },
  /** "최근 기록" 카드(RecentCard). 줄은 문장이 아니라 낱말이다. */
  recentCard: {
    title: "최근 기록",
    empty: "아직 기록이 없습니다.",
    event: {
      created: "생성",
      saved: (revision: number): string => `정의 r${revision} 저장`,
      edited: "정의 수정",
      deployed: (revision: number): string => `r${revision} 배포`,
      deployChanged: "배포 변경",
      observed: "상태 확인",
    } satisfies Record<RecentKind, string | ((revision: number) => string)>,
  },
  /** 미리보기 고르개(RevisionDiff 의 PreviewPicker). 클러스터를 비운 선택지는 shared.tenantDefault 다. */
  revisionDiff: {
    previewing: "만드는 중…",
    preview: "미리보기",
    diff: "diff",
    from: "기준 리비전",
    to: "비교할 리비전",
    previewRevision: "미리 볼 리비전",
    previewCluster: "미리 볼 클러스터",
  },
  /** 리비전 차이 표(RevisionDiffView). 표의 열은 shared.diffColumns 다. */
  revisionDiffView: {
    same: "다른 곳이 없습니다.",
    summary: (from: number, to: number, changes: number): string => `r${from} → r${to}: 변경 ${changes}개`,
    current: "current",
    deployed: "deployed",
  },
  /**
   * 배포하는 리소스(MCP Server, Gateway) 상세와 폼이 같이 쓰는 말. Agent 상세도 쓴다. 재배포·편집 단추는
   * shared.detailActions 다.
   */
  deployable: {
    /** 개요 탭과 배포 탭의 빈 카드 제목. */
    notDeployed: "아직 배포하지 않았습니다.",
    /** 배포 전에 Advanced 탭을 잠근 까닭. */
    advancedAfterDeploy: "Advanced 는 배포한 뒤에 문서를 편집할 수 있습니다.",
    /** 정의 탭의 미리보기 카드. */
    previewHint: "배포하면 생길 클러스터 리소스를 미리 확인합니다.",
    noRevisions: "리비전이 없습니다.",
    deployedRevision: "배포된 리비전",
    /** 개요 탭 주소 카드의 안 주소 이름표. */
    innerEndpoint: "클러스터 안 엔드포인트",
    /** 정의 요약 카드의 줄 이름. */
    runtime: "런타임",
    /** 리비전 표의 열. */
    replicas: "레플리카",
    /** 폼의 Replicas 칸. */
    replicasHint: "파드 수입니다. 1 이상 10 이하입니다.",
    /** 편집 폼의 저장 단추. 새로 만들 때는 shared.create 다. */
    saveRevision: "새 리비전으로 저장",
    /** 배포 정보의 칸 이름. 쿠버네티스 필드 이름이라 두 언어 모두 영어다. */
    labels: {
      cluster: "cluster",
      namespace: "namespace",
      replicas: "replicas",
      resources: "resources",
    },
  },
  /** 복제 대화상자(CloneDialog, D42). Agent·MCP Server·Gateway·Skill 상세가 같이 쓴다. 복제 단추는 shared.detailActions.clone 이다. */
  cloneDialog: {
    /** resource 는 화면에 쓰는 리소스 이름이다("MCP Server"). */
    title: (resource: string): string => `${resource} 복제`,
    /** 복제본의 기본 표시 이름. 사람이 고쳐 쓰는 값이라 대화상자를 연 때의 언어로 적는다. */
    copyName: (name: string): string => `${name} 복제본`,
    /** subject 는 revisionSubject 의 말이다. */
    body: (subject: string, revision: number, resource: string): string =>
      `지금 ${subject} r${revision}${particle(`r${revision}`, "을/를")} 복사하여 새 ${resource}${particle(resource, "을/를")} 미배포 상태로 만듭니다. 리비전 이력과 원본의 배포 상태는 따라오지 않습니다.`,
    cloning: "복제 중…",
    exposure: "외부 노출 (선택)",
  },
  /** 상세 머리의 배포 상태 배너(DeploymentAlerts, D43). 재배포 단추는 shared.detailActions.redeploy 다. */
  deploymentAlerts: {
    /** 정의가 돌고 있는 리비전보다 앞설 때. subject 는 revisionSubject 의 말이다. */
    behind: (revision: number, deployed: number, subject: string): string =>
      `정의는 r${revision} 인데 돌고 있는 것은 r${deployed} 입니다. 재배포해야 바뀐 ${subject}가 적용됩니다.`,
    /** 실패 배너에서 Advanced 탭으로 가는 링크. Gateway 와 Agent 상세가 넘긴다. MCP Server 의 것은 mcpServers.detail.manifestLink 다. */
    openManifest: "Advanced 탭에서 클러스터 리소스 확인하기",
  },
  /** 상세의 Summary 카드와 표가 같이 쓰는 칸 이름과 예·아니오. API 필드 이름이라 두 언어 모두 영어다. */
  summaryFields: {
    title: "Summary",
    id: "id",
    updated: "updated",
    yes: "yes",
    no: "no",
  },
};
