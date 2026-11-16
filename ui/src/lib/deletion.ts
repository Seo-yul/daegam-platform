/**
 * 리소스 삭제 규칙. 목록의 휴지통과 상세의 Delete 버튼이 같은 표를 본다.
 *
 * 위험도: 삭제하면 실행 중인 워크로드가 사라질 수 있는 것(MCP Server·Gateway·Agent·Cluster)은
 * 이름을 다시 입력해야 한다. 정의와 배포가 한 리소스라(D42) `draft` 도 같은 규칙을 탄다.
 * 나머지는 단순 확인이다. 서버가 참조 관계로 거부(409)하면 대화상자 안에 이유를 보여 주고 닫지 않는다.
 */
import {
  deleteAgent,
  deleteBackendApi,
  deleteCluster,
  deleteDataService,
  deleteGateway,
  deleteHubItem,
  deleteKnowledgeBase,
  deleteMcpServer,
} from "@/api/resources";
import { deleteSkill } from "@/api/skills";
import type { Messages } from "@/i18n";
import { ownValue } from "@/lib/utils";

/** 리소스 넷이 정의와 배포를 함께 갖는다 (D42). 옛 Profile·Instance 짝이 하나로 줄고 Skill(D64)이 더해져 kind 는 아홉이다. */
export type DeletableResource =
  | "backend-api"
  | "mcp-server"
  | "gateway"
  | "agent"
  | "data-service"
  | "knowledge-base"
  | "cluster"
  | "hub"
  | "skill";

/** 삭제 규칙. 종류 이름과 설명 문장은 사전(resources.kind, resources.delete.description)에 있다. */
export interface DeleteRule {
  /** 이름을 다시 입력해야 하는 위험 리소스. */
  requireName: boolean;
  /** 성공 뒤 무효화할 react-query 키 접두사. */
  invalidate: string[];
  /** 상세에서 삭제한 뒤 돌아갈 목록 경로. */
  listPath: string;
  /** MCP Server·Gateway·Agent 는 서버의 답을 돌려준다. deleted 가 거짓이면 다른 작업이 이어서 지운다 (P66). */
  remove: (id: string, opts?: { force?: boolean }) => Promise<{ deleted: boolean } | void>;
}

export const DELETE_RULES: Record<DeletableResource, DeleteRule> = {
  "backend-api": {
    requireName: false,
    invalidate: ["backend-apis"],
    listPath: "/backend-apis",
    remove: (id, opts) => deleteBackendApi(id, opts?.force ?? false),
  },
  "mcp-server": {
    requireName: true,
    invalidate: ["mcp-servers", "hub"],
    listPath: "/mcp-servers",
    remove: (id) => deleteMcpServer(id),
  },
  gateway: {
    requireName: true,
    invalidate: ["gateways", "hub"],
    listPath: "/gateways",
    remove: (id) => deleteGateway(id),
  },
  agent: {
    requireName: true,
    invalidate: ["agents"],
    listPath: "/agents",
    remove: (id) => deleteAgent(id),
  },
  "data-service": {
    requireName: false,
    invalidate: ["data-services"],
    listPath: "/data-services",
    remove: (id) => deleteDataService(id),
  },
  "knowledge-base": {
    requireName: false,
    invalidate: ["knowledge-bases"],
    listPath: "/knowledge-bases",
    // 테이블을 못 지우면 서버는 200 으로 deleted: false(table_not_dropped)를 주고 리소스를 orphaned_table 로
    // 남긴다. 그것을 성공으로 넘기지 않게 여기서 던진다. 화면은 이 규칙 대신 KnowledgeBaseDeleteDialog 가
    // 응답을 직접 보고 이유를 말하므로, knowledge-base 에는 ResourceDeleteDialog 를 쓰지 않는다
    remove: async (id) => {
      const r = await deleteKnowledgeBase(id);
      if (!r.deleted) throw new Error(r.warning ?? "table_not_dropped");
    },
  },
  cluster: {
    requireName: true,
    invalidate: ["clusters"],
    listPath: "/clusters",
    remove: (id) => deleteCluster(id),
  },
  hub: {
    requireName: false,
    invalidate: ["hub"],
    listPath: "/hub",
    remove: (id) => deleteHubItem(id),
  },
  skill: {
    requireName: false,
    invalidate: ["skills"],
    listPath: "/skills",
    remove: (id) => deleteSkill(id),
  },
};

/** 화면에 보이는 종류 이름. 규칙표에 없는 kind 는 받은 그대로다. */
export function resourceLabel(t: Messages, kind: string): string {
  return ownValue(t.resources.kind, kind) ?? kind;
}

/**
 * 삭제 확인 대화상자의 제목. action 은 확인 단추의 말(삭제, 게시 해제)이고 기본은 삭제다. 공용 대화상자와 Knowledge Base
 * 대화상자가 같이 쓴다.
 */
export function deleteTitle(t: Messages, resource: DeletableResource, action: string = t.common.delete): string {
  return t.resources.delete.title(t.resources.kind[resource], action);
}

/**
 * kind → 목록 경로의 첫 조각. `listPath` 에서 유도하므로 표가 한 벌뿐이다. 이 조각은 목록 경로이면서
 * 그 리소스의 react-query 키 첫 조각이기도 하다(`invalidate` 와 같다). 비서가 경로에서 문맥을 알아낼 때 이것을 뒤집어 쓴다.
 */
export const PATH_SEGMENT = Object.fromEntries(
  Object.entries(DELETE_RULES).map(([kind, rule]) => [kind, rule.listPath.slice(1)]),
) as Record<DeletableResource, string>;

/** 삭제 대상. 목록 행이나 상세 화면이 대화상자에 넘긴다. */
export interface DeleteTarget {
  id: string;
  name: string;
}
