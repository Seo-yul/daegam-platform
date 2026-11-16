import type { InstancePhase, ResourceState, SleepPhase } from "@/api/types";
import type { DeletableResource } from "@/lib/deletion";

/** 진행 배너 끝의 폴링 안내. */
const polling = (seconds: number): string => `${seconds}초마다 상태를 다시 확인하므로 새로 고치지 않아도 됩니다.`;

/** 예상 시간을 넘긴 진행 배너. what 은 무엇이 늦는지 말하는 마침표 없는 문장이다. */
const slow = (what: string): string =>
  `예상보다 오래 걸립니다. ${what}. 이미지 이름과 클러스터 여유를 확인하고, 자세한 이유는 클러스터에서 파드 이벤트(kubectl describe pod)를 봐 주세요.`;

/** 리소스 종류, 단계(phase), 상태 배지, 삭제 규칙. */
export const resources = {
  /** 화면에 보이는 종류 이름. 메뉴 이름과 같고 두 언어 모두 영어다(D36). */
  kind: {
    "backend-api": "Backend API",
    "mcp-server": "MCP Server",
    gateway: "Gateway",
    agent: "Agent",
    "data-service": "Database",
    "knowledge-base": "Knowledge Base",
    cluster: "Cluster",
    hub: "Hub item",
    skill: "Skill",
  } satisfies Record<DeletableResource, string>,
  /** 문서 상태 칩(StateBadge). 플랫폼이 무엇을 하는 중인지 말한다. */
  state: {
    draft: "미배포",
    deploying: "배포 중",
    deployed: "배포됨",
    failed: "배포 실패",
    undeploying: "배포 해제 중",
    deleting: "삭제 중",
  } satisfies Record<ResourceState, string>,
  /** CR phase. 클러스터가 실제로 어떤 꼴인지 말한다(lib/warningText.ts). */
  phase: {
    /** phase 배지 옆에 덧붙이는 사람 말. Degraded 는 Gateway 가 낸다. */
    label: {
      Pending: "준비 중",
      Rolling: "교체 중",
      Ready: "정상",
      Degraded: "일부 사용 불가",
      Failed: "실패",
      Missing: "없음",
      Deleted: "삭제됨",
      /** Agent 만 갖는 쉬는 phase (P44 10절). */
      Sleeping: "휴면",
      Waking: "깨우는 중",
    } satisfies Record<InstancePhase | SleepPhase | "Degraded", string>,
    /** 진행 배너. 폴링 주기(초)를 받는다. 처음부터 예상 시간을 말한다. */
    progress: {
      Pending: (pollSeconds: number): string => `배포하는 중입니다. 파드는 보통 30초 안에 뜹니다. ${polling(pollSeconds)}`,
      Rolling: (pollSeconds: number): string => `새 파드로 교체하는 중입니다. 보통 1분 안에 끝나고 그동안 잠시 끊길 수 있습니다. ${polling(pollSeconds)}`,
      /** 잠든 Agent 를 다시 띄우는 중 (P44 8절). */
      Waking: (pollSeconds: number): string => `사용하지 않아 내려 두었던 파드를 다시 띄우는 중입니다. 파드가 뜨면 기다리던 요청에 차례로 답합니다. ${polling(pollSeconds)}`,
    },
    /** 예상 시간을 넘긴 진행 배너. 기다리라는 말을 거두고 어디를 보라고 말한다. 넘긴 시간(분)을 받는다. */
    slow: {
      Pending: (minutes: number): string => slow(`파드가 ${minutes}분이 넘도록 뜨지 않았습니다`),
      Rolling: (minutes: number): string => slow(`새 파드가 ${minutes}분이 넘도록 준비되지 않았습니다`),
      Waking: (minutes: number): string => slow(`파드가 ${minutes}분이 넘도록 다시 뜨지 않았습니다`),
    },
    /** 배포가 주저앉았을 때 다음에 할 일. */
    failure: {
      Failed: "배포에 실패했습니다. 아래 error 와 conditions 에서 원인을 확인하고, 이미지 이름과 리소스 요청을 고친 뒤 다시 배포해 주세요.",
      Missing: "클러스터에서 이 리소스를 찾지 못했습니다. 클러스터 연결을 확인하고 다시 배포해 주세요.",
    },
  },
  /** 삭제 확인 대화상자(ResourceDeleteDialog)와 목록 행의 휴지통. */
  delete: {
    /** 대화상자 제목. kind 는 종류 이름이고 action 은 확인 단추의 말(삭제, 게시 해제)이다. */
    title: (kind: string, action: string): string => `${kind} ${action}`,
    /** 무엇이 같이 사라지는지, 또는 어떤 경우 삭제가 막히는지 한 줄. */
    description: {
      "backend-api": "버전 이력과 발급한 토큰이 함께 삭제됩니다. 이 Backend API 를 쓰는 MCP Server 가 있으면 삭제할 수 없습니다.",
      "mcp-server":
        "리비전 이력이 함께 삭제됩니다. 배포된 상태면 실행 중인 파드와 클러스터 리소스가, MCP Hub 에 게시한 항목이 있으면 그 항목이 함께 사라집니다.",
      gateway:
        "리비전 이력이 함께 삭제됩니다. 배포된 상태면 Gateway 와 그룹·Entry 클러스터 리소스가 함께 사라지고, MCP Hub 에 게시한 항목이 있으면 먼저 게시를 해제합니다.",
      agent:
        "리비전 이력이 함께 삭제됩니다. 배포된 상태면 파드와 Deployment·Service, 발급한 토큰, 복사된 provider Secret 이 함께 사라지고 진행 중인 세션과 보류 승인도 사라집니다.",
      "data-service": "저장된 접속 정보와 Secret 이 함께 삭제됩니다. 이 Database 를 쓰는 MCP Server 가 있으면 삭제할 수 없습니다.",
      "knowledge-base":
        "문서와 벡터 테이블이 함께 삭제됩니다. 이 Knowledge Base 를 쓰는 Agent 가 있거나 인덱싱 중인 문서가 있으면 삭제할 수 없습니다.",
      cluster: "등록된 접속 정보(Secret)가 함께 삭제됩니다. 이 Cluster 에 배포된 MCP Server 가 있으면 삭제할 수 없습니다.",
      hub: "이 항목을 쓰는 Gateway 가 있으면 삭제할 수 없습니다. manage 권한이 필요합니다.",
      skill: "모든 리비전이 함께 삭제됩니다. 이 Skill 을 붙인 Agent 가 하나라도 있으면 삭제할 수 없습니다. 다른 테넌트의 Agent 도 포함됩니다.",
    } satisfies Record<DeletableResource, string>,
    /** Backend API 의 읽기 토큰이 살아 있어 거부됐을 때의 강제 삭제 단추. */
    force: "토큰을 회수하고 삭제",
    /** 다른 작업이 문서를 쥐고 있어 서버가 상태만 "삭제 중" 으로 옮겼을 때 낭독기에게 하는 말 (P66). */
    continuing: "진행 중인 다른 작업이 끝나면 이어서 삭제합니다.",
    /** 목록 행의 휴지통. 아이콘만 있어 낭독기가 읽는 이름이다. */
    rowLabel: (name: string): string => `${name} 삭제`,
  },
};
