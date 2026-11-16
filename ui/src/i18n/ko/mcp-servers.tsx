import type { ReactNode } from "react";

import type { OverridesPreset } from "@/api/types";
import { particle } from "@/lib/particle";

/** MCP Server 목록·상세·폼과 오버라이드 제안 (D42, D37). 게시와 해제 단추의 말은 detail.publishSection 이다. */
export const mcpServers = {
  /** 목록과 상세가 같이 쓰는 말. */
  backends: "백엔드",
  revision: "리비전",
  bindings: "바인딩",
  /** /mcp-servers. */
  list: {
    create: "MCP Server 만들기",
    published: "허브 게시",
    columns: { name: "이름", state: "상태", cluster: "클러스터", hub: "허브" },
    publish: "게시",
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 MCP Server 가 없습니다.`,
    noDeployed: `배포된 MCP Server 가 없습니다. "배포된 것만" 을 끄면 아직 올리지 않은 것도 보입니다.`,
    /** link 는 Backend API 등록 화면으로 가는 링크이고 글자는 emptyLink 다. */
    empty: (tenant: string, link: ReactNode): ReactNode => (
      <>
        테넌트 {tenant}에 MCP Server 가 없습니다.{" "}{link}하고 그 Tool 을 선택해 만듭니다.
      </>
    ),
    emptyLink: "Backend API 를 먼저 등록",
    title: "MCP Servers",
    search: "Search MCP servers…",
    url: "URL",
  },
  /**
   * /mcp-servers/:id. 재배포·편집·배포 해제 단추는 shared.detailActions, 배포는 shared.deployDialog.submit 이고, Gateway 와
   * 같이 쓰는 탭의 말은 detail.deployable 이다.
   */
  detail: {
    back: "MCP Server 목록으로 돌아가기",
    deployTitle: "MCP Server 배포",
    deployBody: (revision: number): string => `정의 r${revision}${particle(`r${revision}`, "으로/로")} 클러스터 리소스를 올립니다. 올라간 뒤에도 정의는 그대로 남아 다시 고칠 수 있습니다.`,
    /** 돌고 있는 리비전이 정의보다 뒤처졌을 때의 재배포 확인. */
    redeployBehind: (deployed: number, revision: number): string =>
      `지금 r${deployed}${particle(`r${deployed}`, "으로/로")} 돌고 있습니다. 재배포하면 최신 정의 r${revision}${particle(`r${revision}`, "으로/로")} 올라가므로 이미지와 리소스, 바인딩이 바뀔 수 있습니다. 파드가 새로 뜨는 동안 잠시 끊깁니다.`,
    redeployBody: (revision: number): string => `파드를 다시 만듭니다. 정의는 r${revision} 그대로입니다. 새 파드가 뜨는 동안 잠시 끊깁니다.`,
    undeployBody: "파드와 클러스터 리소스를 내립니다. 정의와 리비전 이력은 그대로 남아 다시 배포할 수 있습니다.",
    undeployPublished: "허브에 게시돼 있습니다. 내리면 그 항목을 쓰는 Gateway 가 이 백엔드를 잃습니다.",
    /** 화면 언어를 따르는 복제 이름 예시. */
    clonePlaceholders: { display: "주문 MCP 복제본", name: "orders-mcp-copy" },
    manifestLink: "Advanced 탭에서 클러스터 리소스 확인하기",
    /** 개요 탭. 배포 탭으로 가는 단추는 detail.connectCard.toDeploy 다. */
    saved: (revision: number): string =>
      `정의 r${revision}${particle(String(revision), "이/가")} 저장돼 있습니다. 배포하면 클러스터 리소스가 생기고 여기에 파드 상태와 주소가 나옵니다.`,
    innerHint: "플랫폼이 발급한 서비스 토큰이 있어야 부를 수 있습니다.",
    authExposed: "클러스터 안 주소는 서비스 토큰으로, 외부 주소는 앞단 프록시가 확인하는 인증으로 부릅니다.",
    authInternal: "서비스 토큰으로만 부를 수 있습니다. 외부에는 열지 않았습니다.",
    /** 고른 Tool 이 없어 스펙의 Tool 을 모두 내보낼 때. 수를 모르면 null 이다. */
    allTools: (total: number | null): string => `${total ?? "?"}개 전부`,
    /** 정의 탭. */
    allSpecTools: "(스펙의 Tool 전체)",
    /** 리비전 표의 열. 레플리카 열은 detail.deployable.replicas 다. */
    revisionColumns: { backendVersion: "백엔드 버전", image: "이미지", created: "생성" },
    /** 배포 탭. */
    deployHint: (revision: number): string => `클러스터와 네임스페이스, 외부 노출을 정하면 정의 r${revision}${particle(`r${revision}`, "으로/로")} 올립니다.`,
    deploymentHint: "배포할 때 정한 자리와, 그때 해석된 값입니다.",
    /** 상세의 칸 이름. 쿠버네티스 필드 이름이라 두 언어 모두 영어다. */
    labels: {
      image: "image",
      backends: "backends",
      tools: "tools",
      runtime: "runtime",
      bindings: "bindings",
      preview: "MCP Server preview",
      cluster: "cluster",
      namespace: "namespace",
      lockedPaths: (count: number): string => `잠긴 경로 (${count})`,
      revisions: "Revisions",
      effective: "effective",
    },
  },
  /**
   * /mcp-servers/new 와 /mcp-servers/:id/edit. 고르개의 빈 선택지는 shared.exposureSection.select 이고, 저장 단추는
   * detail.deployable.saveRevision 과 shared.create 다.
   */
  form: {
    /** 오버라이드를 씌운 뒤 겹치는 Tool 이름. */
    clashes: (count: number): string =>
      `오버라이드를 적용하면 Tool 이름이 이 MCP Server 안에서 겹칩니다(${count}개). 겹치는 쪽에 다른 override name 을 적어 주세요.`,
    /** 겹친 이름이 온 자리. index 는 1 부터 센 백엔드 순서다. 호출부가 ", " 로 잇는다. */
    clashUse: (index: number, tool: string): string => `Backend API #${index} 의 ${tool}`,
    clashMore: (count: number): string => `그 밖에 ${count}개`,
    /** backend 마다의 호출 타임아웃 (P50 7절). 비우면 서버 기본값 30초다. */
    timeout: "타임아웃(초)",
    timeoutHint: "이 Backend API 를 부를 때 기다리는 최대 시간. 기본 30초, 최대 300초.",
    versionHint: "이 MCP Server 가 고정할 Backend API 의 버전입니다. 백엔드에 새 버전이 생겨도 따라가지 않습니다.",
    noBackends: "등록된 Backend API 가 없습니다. 스펙을 먼저 올려 Backend API 를 만들어 주세요.",
    /** names 는 ", " 로 이은 Tool 이름이다. */
    dropped: (count: number, names: string): string =>
      `새 버전에 없는 Tool ${count}개(${names})를 선택에서 뺐습니다. 나머지 선택과 오버라이드는 그대로입니다.`,
    allExposed: (count: number): string => `(선택한 Tool 이 없어 ${count}개 전체가 노출됩니다)`,
    selectFound: "검색 결과 전체 선택",
    selectAll: "전체 선택",
    clearFound: "검색 결과 선택 해제",
    clearAll: "전체 해제",
    hiddenPicked: (count: number): string => `검색에 걸리지 않은 선택이 ${count}개 더 있습니다.`,
    noMatch: "맞는 Tool 이 없습니다.",
    pickVersion: "스펙 버전을 먼저 선택합니다.",
    overrideName: "override name (MCP Tool 이름으로 노출)",
    switchTitle: "다른 Backend API 로 바꿉니다",
    switchBody: (tools: number, overrides: number): string =>
      `선택한 Tool ${tools}개와 오버라이드 ${overrides}개가 사라집니다. Tool 은 백엔드마다 다르므로 옮겨 담을 수 없습니다.`,
    switchConfirm: "바꾸기",
    backendsRequired: "백엔드마다 Backend API 와 spec version 을 선택해 주세요.",
    revisionNotice: (revision: number): string => `저장하면 리비전 r${revision}${particle(`r${revision}`, "이/가")} 만들어집니다. 배포된 것은 재배포하기 전까지 이전 리비전을 사용합니다.`,
    /** 화면 언어를 따르는 이름 예시. */
    placeholders: { display: "주문 조회 Tool", name: "orders-tools" },
    addBackend: "백엔드 추가",
    uniqueNote: "오버라이드 후 Tool 이름은 이 MCP Server 안에서 서로 달라야 합니다. 겹치면 저장할 수 없습니다.",
    imageHint: "비우면 클러스터·플랫폼 기본 이미지를 사용합니다.",
    clashTitle: "Tool 이름이 겹칩니다. 위의 목록을 보고 override name 을 바꿔 주세요.",
    versionsPending: "백엔드의 버전 목록을 불러오는 중입니다. 잠시 뒤에 저장할 수 있습니다.",
    specVersion: "Spec version",
    versionOption: (version: number, kind: string, count: number): string => `v${version} (${kind}, Tool ${count}개)`,
    tools: "Tools",
    searchTools: "Tool 검색",
    overrideDescription: "덮어쓸 설명",
    title: "New MCP Server",
    source: "Source",
    runtime: "Runtime",
    image: "Image",
    replicas: "Replicas",
    bindings: "Bindings",
  },
  /** /mcp-servers/:id/suggest (D37). 단추와 프리셋 이름은 backendApis.specJob 이다. */
  suggest: {
    title: "오버라이드 제안",
    presetHint: {
      enrich_descriptions: "에이전트가 Tool 을 선택할 수 있게 설명을 채웁니다. 이름은 원본이 불분명할 때만 바꿉니다.",
      korean_descriptions: "설명을 한국어로 작성합니다. Tool 이름은 에이전트가 부르는 식별자라 원형 그대로 둡니다.",
    } satisfies Record<OverridesPreset, string>,
    subtitle: "LLM 이 Tool 이름과 설명을 제안합니다. 채택한 것만 새 리비전에 들어갑니다.",
    instructionPlaceholder: "예: 동사로 시작하는 이름을 사용합니다.",
    notice: "시작한 제안은 중간에 멈출 수 없습니다. 이 주소를 그대로 열면 새로고침하거나 다녀와도 이어서 봅니다.",
    deployNotice:
      "적용은 배포가 아닙니다. 돌고 있는 것은 지금 핀한 리비전을 계속 사용합니다. 다음 재배포 때 이 이름이 함께 나갑니다. 이름이 바뀌면 허브 게시 목록도 따라 바뀝니다.",
    unaccepted: "채택하지 않은 항목은 지금 그대로 남습니다.",
    apply: (revision: number): string => `채택해 r${revision} 만들기`,
    empty: "왼쪽에서 프리셋을 선택하고 실행하면 제안이 여기 나타납니다.",
    llmJob: "LLM job",
  },
};
