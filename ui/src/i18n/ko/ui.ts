import type { ReferenceRevision } from "@/api/types";
import { particle } from "@/lib/particle";
import type { StandardTab } from "@/lib/tabs";

/** 기본 UI 부품과 공용 lib (F3c). 키는 문구를 그리는 파일 이름을 따른다. */
export const ui = {
  /** 목록의 카드 모양(components/ui/card-grid.tsx). */
  cardGrid: {
    /** 머리의 흐린 줄에서 언제 고쳤는지. ago 는 "3분 전" 같은 말이다. */
    edited: (ago: string): string => `${ago} 수정`,
    /** 몸의 설명 줄이 비었을 때. 빈칸으로 두면 카드가 덜 그려진 것처럼 보인다. */
    noDescription: "설명이 없습니다",
    /** 오른쪽 위 더보기. name 은 카드의 표시 이름이다. */
    menu: (name: string): string => `${name} 동작`,
    menuTrigger: (name: string): string => `${name} 동작 더보기`,
  },
  /** 복사할 수 있는 코드 칸(components/ui/code-snippet.tsx). 제목이 없을 때 복사 단추가 문장 가운데 넣는 명사다. */
  codeSnippet: { copyLabel: "코드" },
  /**
   * 상한을 넘어 올리지 않은 파일 (lib/files.ts fileTooLargeText). size 는 파일 크기, limit 은 "문서 하나는 10MB" 처럼 상한을
   * 말하는 앞부분이다. 덧붙일 처방은 부르는 쪽이 문장 뒤에 잇는다.
   */
  files: {
    tooLarge: (name: string, size: string, limit: string): string => `"${name}"${particle(name, "은/는")} ${size} 입니다. ${limit} 까지라 올리지 않았습니다.`,
  },
  /** 라벨·입력·도움말 한 벌(components/ui/input.tsx). 필수 칸은 낭독기만 라벨 뒤에 읽는다. */
  input: { required: "(필수)" },
  /** 모달의 닫기 단추(components/ui/modal.tsx). 승인함이 결과를 닫는 단추도 같은 말을 쓴다. */
  modal: { close: "닫기" },
  /** 표(components/ui/table.tsx). 헤더 없는 액션 열의 이름은 낭독기만 읽는다. */
  table: { actions: "작업" },
  /** 아직 없는 화면과 라우트 밖 경로(pages/Placeholder.tsx). */
  placeholder: {
    pending: "곧 제공됩니다.",
    notFound: "페이지를 찾을 수 없습니다.",
  },
  /** 배포 상태 (lib/deployment.ts). */
  deployment: {
    /**
     * 배포 동작이 시작됐을 때 낭독기에게 하는 말 (D43). 셋 다 대화상자가 닫히는 것으로 끝나 눈으로 보지 않으면 아무 일도
     * 없는 것과 같다. 끝나는 것은 파드가 뜬 뒤이고 그것은 배너가 따로 말하므로 시작했다고만 한다.
     */
    started: {
      deploy: "배포를 시작했습니다.",
      redeploy: "재배포를 시작했습니다.",
      undeploy: "배포 해제를 시작했습니다.",
    },
    /** 제목 옆 한 줄 요약의 조각 (D44 넷째). lib 가 개수 뒤에 나쁜 쪽부터 한마디를 " · " 로 잇는다. */
    summary: {
      count: (total: number): string => `${total}개`,
      failed: (count: number): string => `Failed ${count}`,
      progressing: (count: number): string => `Progressing ${count}`,
      allReady: "모두 Ready",
      allDraft: "모두 미배포",
      mixed: (ready: number, draft: number): string => `Ready ${ready} · 미배포 ${draft}`,
    },
  },
  /** 리소스 이름 규칙 (lib/names.ts, D24). 숫자는 lib 의 NAME_MAX·DISPLAY_NAME_MAX 와 같다. */
  names: {
    /** Name 칸의 규칙 힌트. 잘못 입력했을 때의 안내와 같은 문장이다. */
    nameRule: "소문자 영문, 숫자, 하이픈(-)만 쓸 수 있고 하이픈은 양끝이나 연속으로 올 수 없습니다. 32자 이내입니다.",
    displayNameRule: "화면에 보이는 이름입니다. 한글도 사용할 수 있고 80자 이내입니다. 비우면 Name 을 그대로 사용합니다.",
    nameRequired: "이름을 입력해 주세요.",
    nameTooLong: (length: number): string => `32자 이내로 입력해 주세요. 지금 ${length}자입니다.`,
    displayNameBlank: "표시 이름은 공백만으로 채울 수 없습니다.",
    displayNameTooLong: (length: number): string => `표시 이름은 80자 이내로 입력해 주세요. 지금 ${length}자입니다.`,
    /**
     * 다른 리소스를 가리키는 자리에서 그 이름을 못 찾았을 때 (D43 넷째). 지워졌거나 볼 권한이 없는 것이고, 어느 쪽이든
     * 사람에게 UUID 를 보이지 않는다.
     */
    deleted: "(삭제된 항목)",
  },
  /** 스펙 다듬기 변경 묶음 (lib/pointer.ts). 스키마 묶음의 부제는 그것을 참조하는 operation 수다. */
  pointer: { schemaRefs: (count: number): string => `operation ${count}개가 참조` },
  /** OpenRouter 업체 선호의 한 줄 요약 조각 (lib/providers.ts, T19). 접힌 칸의 제목이고 lib 가 " · " 로 잇는다. */
  providers: {
    routing: {
      none: "업체 선호 없음",
      /** 목록 밖 업체로 넘어가지 않는 순서. order 는 "a → b" 다. */
      only: (order: string): string => `${order}만`,
      noFallback: "다른 업체로 넘어가지 않음",
      requireParameters: "요청 인자를 모두 지원하는 업체만",
    },
  },
  /** 참조 목록 (lib/references.ts, D43). */
  references: {
    /** 정의가 붙드는가 배포본이 붙드는가. 재배포 뒤에야 풀리는 참조를 가려 보인다. */
    revision: {
      current: "정의가 참조",
      deployed: "배포본이 참조",
      both: "정의와 배포본이 참조",
    } satisfies Record<ReferenceRevision, string>,
    /** 허브 항목을 거친 간접 참조. revision 은 위의 문구다. */
    viaHubItem: (revision: string): string => `허브 항목을 거쳐 ${revision}`,
    /**
     * Skill 을 핀한 Agent 의 리비전 (D64). 붙들지 않는 쪽은 빠지고 lib 가 ", " 로 잇는다. 정의가 배포보다 앞선 문서의 배지
     * (BehindBadge)도 배포된 리비전을 같은 말로 적으므로 pinnedDeployed 를 쓴다.
     */
    pinnedCurrent: (revision: number): string => `정의 r${revision}`,
    pinnedDeployed: (revision: number): string => `배포 r${revision}`,
  },
  /** MCP Server·Gateway 상세의 탭 (lib/tabs.ts). */
  tabs: {
    standard: {
      overview: "개요",
      definition: "정의",
      deployment: "배포",
      metrics: "메트릭",
      advanced: "Advanced",
    } satisfies Record<StandardTab, string>,
  },
  /** 저장하지 않은 편집을 두고 떠날 때 (lib/unsavedGuard.ts). 편집 폼이 함께 쓰고, 캔버스는 잃는 것이 달라 자기 문구를 쓴다. */
  unsavedGuard: { confirm: "저장하지 않은 입력이 있습니다. 나가면 지금 적은 것이 사라집니다. 나갈까요?" },
  /** 선택 목록(Select). */
  select: {
    noOptions: "(항목 없음)",
  },
};
