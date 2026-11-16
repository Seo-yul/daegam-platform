import type { RuleOperator } from "@/lib/toolRules";

/**
 * Tool 규칙 (P52 7절). MCP Server 와 Gateway 의 정의 탭, 허브 image 항목의 Tools 탭이 같은 카드(components/ToolRules.tsx)로
 * 그린다.
 */
export const toolRules = {
  title: "Tool 규칙",
  callers: "부를 수 있는 대상",
  callersHint:
    "비워 두면 이 테넌트의 모든 멤버와 Agent 가 부를 수 있습니다. 역할, 그룹(oidc 모드), Agent 를 하나라도 선택하면 선택한 대상만 부를 수 있고, 역할은 그 역할 이상의 멤버를 뜻합니다.",
  agentHint:
    "여기서 선택한 Agent 는 그 Agent 를 쓸 수 있는 모든 사람을 위해 이 Tool 을 부를 수 있습니다. 역할과 그룹은 이 Tool 을 직접 부르는 사람에게만 적용됩니다. 역할이나 그룹만 선택하면 어떤 Agent 도 이 Tool 을 부를 수 없습니다.",
  namespaceHint: "이 테넌트에서 이미지를 배포할 수 있는 멤버는 이 규칙을 거치지 않고 서버 컨테이너를 부를 수 있습니다. 인자 조건은 그래도 적용됩니다.",
  itemHint: "항목의 규칙에는 역할만 선택할 수 있어, 규칙을 두면 Agent 는 이 Tool 을 부를 수 없습니다.",
  readOnly: "읽기 전용",
  readOnlyHint:
    "외부에 데이터를 쓰지 않는 Tool 만 읽기 전용으로 표시해 주세요. Agent 의 승인 설정이 이 표시를 기본값으로 쓰고, 외부 내용이 들어온 대화에서는 읽기 전용이 아닌 Tool 은 승인 없이 실행되지 않습니다. 바꾼 표시는 각 Agent 가 다시 배포될 때 반영됩니다.",
  conditions: "인자 조건",
  conditionsHint: "조건에 맞지 않는 호출은 백엔드에 보내지 않고 Tool 오류로 거절합니다. 이 조건은 모든 호출자에게 똑같이 적용됩니다.",
  operators: {
    equals: "같음",
    one_of: "다음 중 하나",
    min: "최솟값",
    max: "최댓값",
    max_length: "최대 글자 수",
    prefix: "시작 문자열",
  } satisfies Record<RuleOperator, string>,
  optional: "인자가 없으면 통과",
  gatewayHint: "이 규칙은 이 Gateway 를 거친 호출에만 적용되고, 서버의 Tool 규칙에 더해 적용됩니다. 서버가 막은 호출은 여기서 열 수 없습니다.",
  gatewayPinHint: "규칙을 둔 백엔드는 이 Gateway 를 배포할 때의 Tool 만 제공합니다. 백엔드에 새로 생기거나 이름이 바뀐 Tool 은 Gateway 를 다시 배포해야 나타납니다.",
  itemPinHint: "규칙이 있는 항목은 규칙을 저장할 때의 Tool 만 제공합니다. 이미지를 바꿔 새로 생기거나 이름이 바뀐 Tool 은 규칙을 다시 저장해야 나타납니다.",
  deletedAgent: "삭제된 Agent",
  opensNotice: "부를 수 있는 대상을 모두 지웠습니다. 저장하면 이 Tool 은 이 테넌트의 모든 멤버와 Agent 에게 열립니다.",
  /** 개요와 카드 머리의 수. */
  ruledTools: (count: number): string => `규칙이 있는 Tool ${count}개`,
  // -- 화면 문구 표 밖에서 더한 문구 ------------------------------------------------------------
  roles: "역할",
  groups: "그룹",
  groupsHint: "여러 그룹은 쉼표로 구분합니다.",
  agents: "Agent",
  noAgents: "이 테넌트에 Agent 가 없습니다.",
  argument: "인자",
  operator: "연산자",
  value: "값",
  valuesHint: "'다음 중 하나' 조건의 값은 쉼표로 구분합니다.",
  addCondition: "조건 추가",
  removeCondition: "조건 삭제",
  noArguments: "이 Backend API 버전에는 인자 목록이 없어 조건을 둘 수 없습니다. 스펙을 다시 업로드하면 조건을 둘 수 있습니다.",
  /** 규칙은 저장돼 있는데 지금 제공하는 Tool 목록에 없는 줄의 배지. 서버가 그 키의 저장을 거절한다. */
  notOffered: "제공하지 않음",
  noTools: "규칙을 둘 Tool 을 아직 알 수 없습니다. Tool 목록을 먼저 확인해 주세요.",
  editRequired: "규칙을 바꾸려면 이 리소스를 고칠 권한이 필요합니다.",
  save: "규칙 저장",
  saving: "저장 중…",
  saved: "규칙을 저장했습니다.",
  /** 저장의 답(rules_notice.opened). 저장 전의 opensNotice 와 짝이다. */
  openedSaved: (tools: string): string => `부를 수 있는 대상을 모두 지워 다음 Tool 은 이 테넌트의 모든 멤버와 Agent 에게 열립니다: ${tools}`,
  /** 저장의 답(rules_notice.rolling_agents). 이미 id 를 실은 토큰을 든 Agent 는 다시 뜨지 않으므로 "수 있습니다" 다. */
  rollingSaved: (agents: string): string => `Agent 를 구별하는 정보를 토큰에 더하려고 다음 Agent 가 한 번씩 다시 뜰 수 있습니다: ${agents}`,
};
