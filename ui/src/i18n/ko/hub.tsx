import type { ReactNode } from "react";

import type { HubAction } from "@/api/types";
import { particle } from "@/lib/particle";

/** 이름 목록 뒤의 주격 조사. 조사는 끝 이름이 정한다. */
const subject = (names: string): string => `${names}${particle(names, "이/가")}`;

/** MCP Hub 목록·상세·등록과 접근 정책(lib/hubAccess), Tool 스키마 목록(components/hub). */
export const hub = {
  /** consume 이 없는 항목. 목록의 행과 카드가 흐린 까닭을 말한다. */
  notConsumable: { title: "백엔드로 선택할 수 없음", badge: "consume 권한 없음" },
  /** 접근 정책의 문장 (docs/guide/hub-gateway.md 7절). Skill 은 동작의 뜻과 테넌트를 부르는 말을 따로 넘긴다. */
  access: {
    /** 정책 문장이 항목을 가진 테넌트를 부르는 말. */
    owner: "게시한 테넌트",
    actions: {
      view: { title: "view", hint: "목록·상세에 보입니다" },
      consume: { title: "consume", hint: "Gateway 의 백엔드로 선택할 수 있습니다" },
      manage: { title: "manage", hint: "게시 해제·정책 변경·Tool 새로고침" },
    } satisfies Record<HubAction, { title: string; hint: string }>,
    /** scope 를 풀어 쓴 말. owner 는 항목을 가진 테넌트를 부르는 말이다. */
    scope: {
      shared: "다른 테넌트에도 열려 있음",
      tenant: (owner: string): string => `${owner} 안에서만 보임`,
    },
    /** header 항목의 접근 정책 편집기 위 안내 (P86 6.2절). 다른 테넌트에 열면 사본이 그 네임스페이스로 간다. */
    headerNotice:
      "다른 테넌트에 열면 그 테넌트의 Gateway 네임스페이스마다 헤더 값의 사본이 들어갑니다. 다른 클러스터에 있는 네임스페이스도 같고, 그 네임스페이스를 관리하는 사람은 값을 읽을 수 있습니다.",
    /** 편집 중인 정책이 저장된 정책에서 테넌트를 뺄 때 함께 보인다. */
    headerRemoved: "테넌트를 빼면 곧 그 테넌트의 사본을 지웁니다. 그 전에 값을 읽었을 수 있으니 헤더 값도 바꾸는 것이 안전합니다.",
    /** 한 동작의 주체를 읽기 전용으로 적는 조각. lib 가 " · " 로 잇는다. 모든 테넌트는 shared.accessPolicyEditor.allTenants 다. */
    subjects: {
      /** 테넌트를 비운 동작의 주체. 영어는 줄 맨 앞이라 첫 글자를 대문자로 바꾸고 한국어는 그대로 쓴다. */
      owner: (owner: string): string => owner,
      tenants: (tenants: string): string => `테넌트 ${tenants}`,
      minRole: (role: string): string => `${role} 이상`,
      roles: (roles: string): string => `role ${roles} 이상`,
      groups: (groups: string): string => `그룹 ${groups}`,
    },
  },
  /**
   * 플랫폼이 붙이는 항목의 phase (P55 5절). 열쇠가 status.phase 이고, label 은 상태 배지, text 는 상세의 안내다. 여기 없는 phase 는
   * 클러스터 리소스의 값이라 그대로 보인다.
   */
  status: {
    not_answering: {
      label: "응답 없음",
      text: "백엔드 파드는 실행 중이지만 이 항목이 Tool 목록 요청에 응답하지 않습니다. 플랫폼이 프록시 파드를 다시 만드는 중이며, 그동안 이 항목의 호출은 실패합니다. 계속되면 백엔드 파드의 로그를 확인해 주세요.",
    },
  } as Record<string, { label: string; text: string }>,
  /** /hub. 검색어와 맞는 것이 없을 때는 detail.listControls.noMatch 다. */
  list: {
    register: "외부 MCP 등록",
    /** 카탈로그 대화상자를 여는 단추 (P55 8절). */
    importCatalog: "카탈로그에서 가져오기",
    /** 클라이언트 설정 내보내기 대화상자를 여는 단추 (P55a 2절). */
    exportClients: "클라이언트 설정 내보내기",
    /** 0 개와 아직 물어보지 않은 것을 가른다. */
    toolsUnknown: "아직 모름",
    /** link 는 MCP Server 목록으로 가는 링크다. */
    empty: (tenant: string, link: ReactNode): ReactNode => (
      <>
        테넌트 {tenant}에 허브 항목이 없습니다.{" "}{link}의 "허브에 게시" 로 올리거나 외부 MCP 를 등록합니다.
      </>
    ),
    /** 카드 머리의 흐린 줄. scope 는 꾸민 서버 값이다. */
    scope: (scope: ReactNode): ReactNode => <>범위 {scope}</>,
    columns: { name: "이름", kind: "종류", status: "상태", tools: "Tool", scope: "범위" },
    error: "error",
    title: "MCP Hub",
    search: "Search hub…",
    tool: "Tool",
  },
  /**
   * /hub/:id. 개요 탭의 이름은 ui.tabs.standard.overview 이고, 게시 해제 확인 문구는 detail.publishSection 의
   * unpublishInternal 과 unpublishPublished 다. refresh 는 Gateway 폼의 Tool 없음 안내가 이 단추를 짚는 링크 글자로도 쓴다.
   */
  detail: {
    tabs: { tools: "Tools", access: "접근 정책", manifest: "매니페스트" },
    /** 지워진 항목의 북마크를 따라왔을 때 화면이 부르는 리소스 이름. */
    notFound: "허브 항목",
    back: "MCP Hub 로 돌아가기",
    manageRequired: "manage 권한이 필요합니다",
    /** 플랫폼 웹 Tool 항목(P39b 13절). 주인 테넌트가 없어 소유자 칸에도 이 이름을 적는다. */
    platform: "플랫폼",
    platformHint: "플랫폼이 관리하는 Tool 입니다. Platform Settings 에서 켜고 끄며, Gateway 에 넣어 사용합니다.",
    refreshing: "새로고침 중…",
    refresh: "Tool 새로고침",
    gotoInstance: "MCP Server 로 이동",
    noK8s: "(클러스터 리소스 없음)",
    /** 사람이 MCP 클라이언트에 붙여 넣는 주소. 이름표이자 복사 단추가 문장 가운데 넣는 명사다. */
    mcpUrl: "MCP 주소",
    notExposed: "외부로 노출되어 있지 않아 클러스터 내부에서만 부를 수 있습니다.",
    /** remote·entry 의 등록한 주소를 서버가 클러스터 밖에서 닿는 주소로 가르지 못했을 때 (P55a 1절). */
    notOutside: "플랫폼이 클러스터 밖에서 닿는 주소로 확인하지 못해 클라이언트 설정에 넣지 않습니다. 등록한 주소는 source url 에 있습니다.",
    /** 복사 단추가 문장 가운데 넣는 명사. */
    backendUrlCopy: "backend url",
    manifestReadOnly: "읽기 전용입니다. 게시 항목은 직접 편집할 수 없습니다.",
    noManifest: "클러스터 리소스 문서가 없습니다.",
    /** Summary·Status 카드의 칸 이름. API 필드 이름이라 두 언어 모두 영어다. */
    labels: {
      tenant: "tenant",
      scope: "scope",
      sourceUrl: "source url",
      image: "image",
      origin: "origin",
      status: "Status",
      ready: "ready",
      backendUrl: "backend url",
      message: "message",
      error: "error",
      observed: "observed",
      manifest: "Manifest",
      cluster: "cluster",
      /** entry 항목의 인증 (P86 6.4절). API 필드 이름이다. */
      auth: "auth",
      headerName: "header name",
      headerSet: "set",
      check: "check",
      /** 카탈로그 출처와 image 항목의 컨테이너, 허용 기록 (P55 8절). API 필드 이름이다. */
      catalog: "catalog",
      digest: "digest",
      upstream: "upstream",
      run: "run",
      admittedAt: "admitted at",
      args: "args",
      env: "env",
      secretEnv: "secret env",
      stdio: "stdio",
    },
    /** 허용 기록 카드의 제목 (P55 8절). */
    admissionTitle: "허용 기록",
    /** 허용 기록의 provenance (P55 6절). verified 는 upstream 서명을 확인한 것이고 approved 는 출처 없이 승인한 것이다. */
    admission: { verified: "서명 확인됨", approved: "출처 확인 없이 승인됨" },
    /** Secret env 이름마다 값이 있는지. 값은 화면에 다시 보이지 않는다. */
    secretSet: { set: "설정됨", unset: "비어 있음" },
    /** Secret env 값 바꾸기 (P55 5절). 만든 사람과 tenant-admin 만 연다(`permissions.replace_secret_env`). */
    replaceSecrets: "Secret 값 바꾸기",
    replaceSecretsHint: "새 값을 넣은 이름만 바뀌고, 비워 둔 이름은 지금 값을 그대로 사용합니다. 값을 바꾸면 이 항목의 파드가 다시 시작됩니다.",
    /** 아직 저장된 값이 하나도 없을 때 replaceSecretsHint 대신. 그때는 Secret 이 없어 서버가 모든 이름의 값을 받아야 만든다. */
    replaceSecretsAllRequired: "아직 저장된 값이 없어 모든 이름의 값을 입력해야 합니다.",
    replaceSecretsSave: "바꾸기",
    replacedBy: (who: string, when: string): string => `${who}${particle(who, "이/가")} ${when} 에 Secret 값을 바꿨습니다.`,
    /** header 항목의 값 바꾸기(P86 6.1절)와 마지막 확인의 답(6.3절). 카드 제목은 new.labels.auth, 대화상자의 값 칸은 new.labels.headerValue 와 new.headerValueHint 다. */
    auth: {
      replaceHeader: "헤더 값 바꾸기",
      checkRefused: "저장했지만 외부 서버가 이 헤더 값을 거절했습니다. 값이 맞는지 확인한 뒤 다시 바꿔 주세요.",
      checkUnreachable: "저장했지만 외부 서버에 연결하지 못해 헤더 값을 확인하지 못했습니다.",
    },
  },
  /**
   * 클라이언트 설정 내보내기 대화상자 (P55a 2절, 8절). 토큰·로그인 갈래와 인증 없음 경고는 연결 카드의 문장
   * (detail.connectCard)을 그대로 쓴다. 파일 경로 제목은 말이 아니라 lib 에 있다.
   */
  export: {
    title: "클라이언트 설정 내보내기",
    client: "클라이언트",
    items: "내보낼 항목",
    /** 고를 수 없는 항목의 흐린 까닭. 연결 카드와 같은 말이다. */
    noAddress: "외부 주소가 없습니다",
    private: "사설망 주소",
    privateHint: "사설망 주소는 VPN 처럼 사내망에 닿는 곳에서만 부를 수 있습니다. 선택하면 그대로 내보냅니다.",
    urlCredentials: "주소에 자격 증명이 들어 있을 수 있어 내보내지 않습니다. 상세에서 직접 복사해 주세요.",
    /** Codex 를 골랐을 때 SSE 직접 연결 항목의 흐린 까닭. Codex 는 streamable HTTP 만 읽는다. */
    sseCodex: "Codex 는 SSE 서버를 읽지 못해 내보내지 않습니다.",
    direct: "직접 연결",
    directHint: "직접 연결 항목은 클라이언트가 외부 서버를 바로 부릅니다. 플랫폼의 감사 기록이 남지 않고, 로그인은 그 서버의 방식을 따릅니다. 주소는 등록된 그대로 내보냅니다.",
    tokenHint: (tenant: string): string => `이 파일은 테넌트 ${tenant} 의 항목만 담고 토큰은 적지 않습니다. 토큰을 넣는 방법은 아래 클라이언트 안내를 따라 주세요.`,
    /** 플랫폼 토큰을 받는 경계의 클라이언트 안내. link 는 내 MCP 토큰 화면으로 가는 링크이고 env 는 테넌트의 토큰 env 다. */
    tokenSteps: (link: ReactNode, env: string): ReactNode => (
      <>
        {link} 화면에서 이 테넌트의 토큰을 발급해 주세요. 그다음 클라이언트를 시작할 셸에서 아래 명령으로 {env}에 토큰을 넣어 주세요.
      </>
    ),
    allowlist: "회사 전체 허용 목록 (플랫폼 관리자)",
    allowlistHint:
      "Claude Code 관리 설정에 합칠 allowedMcpServers 값입니다. 모든 테넌트의 모든 멤버에게 열린 직접 연결 항목과 클러스터의 노출 도메인만 허용하므로, 사람들이 스스로 추가한 다른 MCP 서버는 막힙니다. 다만 각자의 설정에서 허용 목록을 넓히면 그 서버도 열립니다. 노출 도메인 밖의 주소는 들어가지 않습니다.",
    /** names 는 패턴으로 적지 못해 허용 목록에서 빠진 클러스터와 항목의 이름을 ", " 로 이은 것이다. */
    allowlistSkipped: (names: string): string =>
      `허용 목록에 넣지 못한 것이 있습니다: ${names}. 경로에 * 가 든 주소와, 점이 없거나 DNS 이름이 아닌 노출 도메인은 패턴으로 적을 수 없으니 관리 설정에 직접 넣어 주세요.`,
    exclusive: "회사 전체 고정 목록",
    exclusiveHint: "이 파일을 배포하면 사람들은 목록 밖의 서버를 추가할 수 없습니다. 허브의 Gateway 와 MCP Server 도 추가할 수 없게 됩니다.",
    download: "내려받기",
  },
  /**
   * 카탈로그 대화상자 (P55 7·8절). 이 플랫폼 판과 함께 실린 스냅숏을 보이고, 고르면 /hub/new 를 채워 연다. tier 는 카탈로그
   * 값(Official, Community)이 열쇠이고 모르는 값은 그대로 보인다.
   */
  catalog: {
    title: "MCP 카탈로그",
    hint: "ToolHive 가 공개한 MCP 서버 목록입니다. 이 플랫폼 버전과 함께 들어온 목록이라 최신 서버가 빠져 있을 수 있습니다.",
    search: "이름이나 설명으로 찾기…",
    kinds: { all: "전체", image: "image", remote: "remote" },
    empty: "카탈로그가 비어 있습니다.",
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 서버가 없습니다.`,
    tier: { Official: "공식", Community: "커뮤니티" } as Record<string, string>,
    provenance: { available: "출처 확인 가능", none: "출처 정보 없음" },
    platforms: "아키텍처",
    sseOnlyHint: "SSE 방식으로만 동작하는 서버라 허브에 등록할 수 없습니다.",
    needsLogin: "로그인 필요",
    needsLoginHint: "사람마다 로그인해야 사용하는 서버라 아직 허브에 등록할 수 없습니다.",
    needsCredentials: "자격 증명 필요",
    needsCredentialsHint: "주소나 헤더에 자격 증명을 넣어야 하는 서버라 아직 허브에 등록할 수 없습니다.",
    /** 비밀 헤더 하나만 필요한 원격 (P86 A3). 등록 폼이 entry 항목의 header 인증으로 열린다. */
    headerImportHint: (header: string): string => `이 서버는 ${header} 헤더에 API 키를 넣어야 합니다. 등록할 때 Auth 를 header 로 선택하고 그 값을 입력해 주세요.`,
    admissionOff: "이미지 서명 확인이 설정되지 않아 카탈로그 이미지는 등록할 수 없습니다.",
  },
  /** 상세의 Tools 탭 (D48). */
  tools: {
    count: (count: number): string => `${count}개`,
    countLive: (count: number): string => `${count}개 · 방금 업데이트`,
    /** readAt 은 화면 언어로 적은 마지막 조회 시각이고, 모르면 null 이다. */
    countStored: (count: number, readAt: string | null): string => `${count}개${readAt ? ` · ${readAt} 에 읽음` : ""}`,
    needConsume: "Tool 이 무엇을 받는지 보려면 consume 권한이 필요합니다. 지금은 이름만 보입니다.",
    loading: "서버에 Tool 을 물어보는 중입니다…",
    notListed: "지금은 서버에 물어볼 수 없어 마지막으로 저장된 이름만 보입니다.",
    notListedReason: "서버가 준 까닭",
    truncated: (shown: number): string => `Tool 이 너무 많아 앞의 ${shown}개만 보입니다. 나머지는 이 화면에 담기지 않습니다.`,
    /** 저장된 이름과 방금 본 이름의 차이. 이름은 ", " 로 이은 목록이다. */
    mismatch: {
      storedOnly: (names: string): string => `저장된 쪽에만 ${subject(names)} 있습니다.`,
      liveOnly: (names: string): string => `서버에만 ${subject(names)} 있습니다.`,
      both: (stored: string, live: string): string => `저장된 쪽에만 ${subject(stored)} 있고 서버에만 ${subject(live)} 있습니다.`,
    },
    /** mismatch 는 위의 차이 문장이다. 고칠 길은 뒤에 따로 선다. */
    stale: (mismatch: string): string => `저장된 목록이 지금과 다릅니다. ${mismatch} Gateway 를 만들 때는 저장된 쪽을 사용합니다.`,
    /** 파생 항목에는 Tool 새로고침이 없어 원본을 짚는다. origin 은 원본 종류 이름(MCP Server, Gateway)이다. */
    staleFromOrigin: (origin: string): string => `이름은 이 항목을 게시한 ${origin} 에서 옵니다. 그 ${origin} 상세에서 재배포하면 맞춰집니다.`,
    staleRefresh: "Tool 새로고침으로 맞춰 주세요.",
    staleNeedManage: "맞추려면 manage 권한이 필요합니다.",
    unknown: "Tool 목록이 아직 없습니다.",
    unknownFromOrigin: (origin: string): string =>
      `이름은 이 항목을 게시한 ${origin} 에서 옵니다. 그 ${origin} 상세에서 재배포하면 Gateway 에서 Tool 을 선택할 수 있습니다.`,
    unknownRefresh: "Tool 새로고침을 먼저 실행하면 Gateway 에서 Tool 을 선택할 수 있습니다.",
    title: "Tools",
  },
  /** 도구 목록(ToolSchemaList, D48). 필수 표시는 detail.openApiDocs.required 다. */
  toolSchemas: {
    search: "Tool 이름으로 찾기…",
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 Tool 이 없습니다.`,
    outputSchema: "정해진 출력",
    noDescription: "서버가 이 Tool 에 설명을 달지 않았습니다.",
    schemaOmitted: "입력 스키마가 너무 커서 싣지 않았습니다. 서버에서 직접 확인해 주세요.",
    input: "입력",
    noArgs: "받는 인자가 없습니다.",
  },
  /** /hub/new. 클러스터 칸은 shared.deployDialog.clusterHint 와 shared.tenantDefault 를 쓴다. */
  new: {
    /** 화면 언어를 따르는 예시. 표시 이름과 Name 이 서로 대응되는 한 쌍이다. */
    placeholders: { display: "결제 MCP", name: "payments-mcp" },
    kindHint: "remote: MCPRemoteProxy 로 감쌉니다 / entry: URL 만 등록합니다 / image: 컨테이너로 배포합니다",
    urlHint: "원격 MCP 서버의 주소입니다. 플랫폼이 이 주소로 tools/list 를 불러 Tool 목록을 채웁니다.",
    imageHint: "MCP 서버 컨테이너 이미지입니다. 다른 테넌트가 참조하면 그 네임스페이스로 복제됩니다.",
    portHint: "비우면 8080 을 사용합니다.",
    toolsFilterHint: "쉼표로 구분합니다. 비우면 전체 Tool 을 노출합니다.",
    transportHint: { entry: "entry 는 필수입니다.", other: "비우면 streamable-http 를 사용합니다." },
    registering: "등록 중…",
    register: "등록",
    title: "Register external MCP",
    kind: "Kind",
    kinds: { remote: "remote", entry: "entry", image: "image" },
    cluster: "Cluster",
    url: "URL",
    image: "Image",
    port: "Container port (mcpPort)",
    toolsFilter: "Tools filter",
    toolsFilterPlaceholder: "Add, Divide",
    transport: "Transport",
    transports: { "streamable-http": "streamable-http", sse: "sse" },
    /**
     * entry 항목의 인증 칸 (P86 6.1절)과 Env 줄의 이름·값 칸 (P55 5절). API 필드와 값이라 두 언어 모두 영어다. oauth 는 P86b 가
     * 더한다.
     */
    labels: { auth: "Auth", headerName: "Header Name", headerValue: "Header Value", name: "Name", value: "Value" },
    headerNamePlaceholder: "Authorization",
    auths: { none: "none", header: "header" },
    authHint: "none: 인증 없이 부릅니다 / header: 모든 호출에 같은 헤더(API 키 등)를 붙입니다. 등록한 뒤에는 바꿀 수 없습니다.",
    headerValueHint: "저장한 뒤에는 값을 다시 볼 수 없습니다. 이 허브 항목을 백엔드로 사용하는 Gateway 마다 그 네임스페이스에 값의 사본을 둡니다.",
    /** 카탈로그에서 연 폼의 머리 줄 (P55 8절). name 은 카탈로그 서버의 제목이다. */
    fromCatalog: (name: string): string => `카탈로그에서 가져온 값입니다: ${name}`,
    /** 카탈로그 이미지의 미러 안내. 플랫폼 관리자는 미러 작업의 입력과 복사 단추를, 다른 멤버는 한 줄을 본다. */
    mirrorAdmin: "이 이미지를 사용하려면 허브 미러 작업을 아래 값으로 실행해 사내 레지스트리로 옮겨 주세요. 이미 옮겼다면 그대로 등록하면 됩니다.",
    mirrorMember: "카탈로그 이미지는 플랫폼 관리자가 사내 레지스트리로 옮긴 뒤에 등록할 수 있습니다. 아직 옮기지 않았다면 등록이 거절됩니다.",
    /** 미러 입력 칸의 복사 단추가 문장 가운데 넣는 명사 ("미러 작업 값 복사"). */
    mirrorCopy: "미러 작업 값",
    /** image 항목의 컨테이너 (P55 5절). 칸 이름은 API 이름이라 두 언어 모두 영어다. */
    stdio: "stdio",
    stdioHint: "컨테이너가 표준 입출력으로 MCP 메시지를 주고받으면 선택해 주세요. ToolHive 가 이를 HTTP 로 바꿔 Gateway 에 내보냅니다.",
    args: "Args",
    env: "Env",
    envHint: "값은 이 항목을 볼 수 있는 모든 사람에게 보입니다. 비밀 값은 Secret env 에 넣어 주세요.",
    secretEnv: "Secret env",
    secretEnvHint: "값은 이 항목만 사용하는 Secret 에 저장하고 화면에 다시 보이지 않습니다. Secret env 가 있는 항목은 다른 테넌트에 열 수 없습니다.",
  },
};
