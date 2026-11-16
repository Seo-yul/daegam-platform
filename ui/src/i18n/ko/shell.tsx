import type { ReactNode } from "react";

import type { NavGroupId } from "@/components/Layout";
import type { QuickStartId } from "@/pages/home-chat/HomeStart";

/** 사이드바, 머리, 홈, 홈 챗. 사이드바의 "새 대화" 와 목록의 새 대화 단추는 shared.chatPanel.newChat 이다. */
export const shell = {
  /** 왼쪽 사이드바와 본문 건너뛰기(components/Layout.tsx). */
  sidebar: {
    /** 그룹 이름. 키가 그룹 id 이고, 접힌 그룹은 이름이 아니라 id 로 저장한다(STORAGE_KEYS.navCollapsed). */
    groups: { Tool: "Tool", Agent: "Agent", Admin: "관리" } satisfies Record<NavGroupId, string>,
    skipToMain: "본문으로 건너뛰기",
    /** 브랜드 마크. 홈으로 가는 길이다. */
    home: "대감 홈으로",
    homeTitle: "홈으로",
    expand: "사이드바 펼치기",
    collapse: "사이드바 접기",
  },
  /** 계정 메뉴의 테마 단추 (P81 5절). */
  theme: {
    toLight: "라이트 테마로 바꾸기",
    toDark: "다크 테마로 바꾸기",
    dark: "Dark",
    light: "Light",
    label: "Theme",
  },
  /** 계정 메뉴의 플랫폼 API 문서 단추 (P59, P81 5절). */
  apiDocs: "API 문서",
  /** 제품과 비서의 이름 (P80). product 는 사이드바 글자 로고와 탭 제목, API 문서 창 제목이, assistant 는 머리 줄 토글과 비서 패널, Audit 배지가 쓴다. */
  names: { product: "대감", assistant: "청지기" },
  /** 머리의 청지기 패널 토글. */
  assistantToggle: {
    open: "청지기 패널 열기",
    close: "청지기 패널 닫기",
    openTitle: "청지기에게 채팅으로 플랫폼 작업을 맡깁니다.",
    closeTitle: "청지기 패널을 닫습니다.",
  },
  /** 화면 머리(PageHeader). 제목이 글자가 아니고 name 도 없을 때 `···` 메뉴 이름(ui.cardGrid.menu)에 넣는 말이다. */
  pageHeader: { thisResource: "이 리소스" },
  /** 홈 챗(pages/home-chat/HomeChat.tsx). */
  homeChat: {
    /** 상대 고르개의 청지기 항목과, 비서가 꺼졌을 때 그 옆에 다는 배지. */
    assistantOption: "청지기 (플랫폼 비서)",
    assistantOff: "청지기 꺼짐",
    /** 입력칸 아래 한 줄. 고른 상대가 누구로서 도는지를 말한다(D35). */
    assistantNote: "청지기는 로그인한 사용자의 권한으로 플랫폼을 조작합니다. 사용자가 못 하는 일은 청지기도 못 합니다.",
    agentNote: "이 Agent 는 자기 자격으로 Tool 을 부릅니다. Tool 호출에 사용자의 신원은 실리지 않습니다.",
    /** 좁은 폭에서 대화 목록을 접고 펴는 단추. count 는 목록의 대화 수다. */
    closeList: "대화 목록 닫기",
    openList: (count: number): string => `대화 목록 (${count})`,
    /** 대화 목록이 비었을 때. 청지기의 목록, 에이전트 목록의 검색 결과, 에이전트 목록이다. */
    empty: {
      assistant: "아직 대화가 없습니다. 무엇이든 물어보세요.",
      search: "제목에 검색어가 들어간 대화가 없습니다.",
      agent: "이 Agent 와 나눈 대화가 아직 없습니다.",
    },
    /** 청지기 대화 목록 아래의 성질 설명. 에이전트 목록은 세션 수명 안내(sessionLifeHint)가 온다. */
    assistantSessionHint: "청지기와의 대화는 테넌트마다 이어집니다. 지우면 감사 기록만 남습니다.",
    /** 고른 에이전트가 Ready 가 아니라 대화할 수 없을 때. phase 를 모르면 null 이고, detail 은 detailLink 를 단 상세 링크다. */
    notReady: (phase: string | null, detail: ReactNode): ReactNode => (
      <>
        이 Agent 는 지금 {phase ?? "알 수 없는"} 상태라 대화할 수 없습니다.{" "}
        {detail}에서 상태와 이유를 볼 수 있습니다.
      </>
    ),
    detailLink: "상세",
    /** 대화할 상대가 없을 때(청지기도 Agent 도 없음). 무엇을 만들면 되는지 말한다. link 는 그 길로 가는 링크다. */
    emptyHome: {
      title: "아직 대화할 상대가 없습니다",
      loadFailed: "Agent 목록을 읽지 못했습니다. 권한이나 연결을 확인한 뒤 새로고침해 주세요. 아래 길은 그대로 사용할 수 있습니다.",
      lead: "두 가지 길이 있습니다. 어느 쪽이든 여기로 돌아오면 대화할 수 있습니다.",
      agentLink: "Agent 를 만들어 배포합니다",
      agent: (link: ReactNode): ReactNode => <>{link}. 템플릿 "단일 상담 에이전트" 로 시작하면 provider·MCP·Database 만 선택하면 됩니다.</>,
      assistantLink: "Platform Settings 에서 청지기를 켭니다",
      assistant: (link: ReactNode): ReactNode => (
        <>{link}. 켜면 "Petstore 스펙으로 MCP Server 를 만들어 줘" 처럼 말로 시킬 수 있습니다. LLM provider 가 하나 필요합니다.</>
      ),
    },
  },
  /** 홈의 시작 화면(HomeStart). */
  homeStart: {
    /** 빠른 시작 칩 넷(D45 넷째). 칩의 글이 곧 입력칸에 채워 청지기에게 보내는 말이라 화면 언어를 따른다. */
    quickStarts: {
      mcp: "Petstore 스펙으로 MCP Server 를 만들어 주세요",
      status: "지금 배포된 것들의 상태를 알려 주세요",
      hub: "허브에 어떤 Tool 이 있는지 보여 주세요",
      agent: "상담 에이전트를 만들어 주세요",
    } satisfies Record<QuickStartId, string>,
    placeholder: "무엇을 도와드릴까요? Enter 로 보내고 Shift+Enter 로 줄을 바꿉니다.",
    /** 이름을 아는 사람에게 하는 인사. 모르면 ask 가 인사이고, ask 는 입력칸의 이름이기도 하다. */
    greeting: (name: string): string => `${name}님, 안녕하세요`,
    ask: "무엇을 도와드릴까요?",
  },
  /** 홈 챗의 왼쪽 대화 목록(SessionList, useAgentSessionList). 지우기 단추는 common.delete 다. */
  sessionList: {
    heading: "대화",
    newTitle: "새 대화를 시작합니다.",
    search: "대화 검색",
    loading: "목록을 불러오는 중입니다…",
    /** 닫힌 대화(D46)의 배지. */
    closed: "닫힘",
    rename: "이름 바꾸기",
    /** 청지기 행의 휴지통 단추. 확인 없이 지운다. name 은 대화의 제목이다. */
    trash: (name: string): string => `${name} 대화 삭제`,
    trashTitle: "이 대화를 삭제합니다. 감사 기록은 남습니다.",
    /** 에이전트 대화의 삭제 확인. */
    deleteTitle: "이 대화를 삭제할까요?",
    deleteBody: "대화와 업로드한 파일, Agent 가 만든 파일이 지워지고 되돌릴 수 없습니다. 장기 기억과 감사 기록은 남습니다.",
    deleting: "삭제 중…",
    /** 턴이 도는 대화를 지우려 할 때(409 turn_in_progress). 공용 문장은 다시 보내라고 하므로 삭제에 맞춰 따로 둔다(P78 4절). */
    deleteRunning: "답변이 진행 중인 대화는 삭제할 수 없습니다. 답변을 중단한 뒤에 삭제해 주세요.",
    /** 에이전트 대화 행의 제목 아래 한 줄. at 은 화면 언어로 적은 마지막 턴 시각이고, pending 이면 승인을 기다리는 대화다. */
    agentDetail: (turns: number, at: string | null, pending: boolean): string => `턴 ${turns}${at ? ` · ${at}` : ""}${pending ? " · 승인 대기" : ""}`,
  },
  /** 계정 메뉴의 언어 줄 (P81 5절). */
  languageSwitch: {
    label: "Language",
  },
  /** 계정 메뉴 아래의 빌드 표시 (P81 5절). */
  versionBadge: {
    release: "release",
    api: "api",
    ui: "ui",
    stale: (build: string): string => `(서버: ${build})`,
    staleTitle: (build: string): string => `서버가 제공하는 UI 빌드는 ${build} 입니다`,
  },
};
