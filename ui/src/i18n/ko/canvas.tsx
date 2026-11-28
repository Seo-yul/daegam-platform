import type { ReactNode } from "react";

import type { ToolEffect } from "@/api/types";

import { clashNames } from "@/lib/clashNames";
import type { AgentData, EdgeKind, GuardrailData, IssueAt, McpSourceKind, NodeType, ProviderRole, RedisCheckpointerState } from "@/lib/agentGraph";
import { MAX_SKILLS_DISTINCT_BYTES, MAX_SKILLS_EXPANDED_BYTES, MAX_SKILLS_PER_AGENT, MAX_SKILLS_PER_GRAPH } from "@/lib/agentSkills";
import { SANDBOX_COMMAND_MAX_SECONDS } from "@/lib/codeExecution";
import { particle } from "@/lib/particle";
import { MIB, formatMib } from "@/lib/utils";

import { skills } from "./skills";

/** 노드 종류의 이름. 팔레트, 카드, 인스펙터 제목, 이슈 상세와 Agent 목록의 구성 태그가 쓴다. */
const nodeLabels = {
  entry: "Chat 트리거",
  agent: "에이전트",
  model: "모델",
  tools: "MCP",
  memory: "메모리",
  guardrail: "가드레일",
  knowledge: "지식",
} satisfies Record<NodeType, string>;

/** 노드 이름 뒤에 조사를 둔다. 한글 이름에는 붙이고, 영어 이름(MCP)에는 띄운다(ui.md 3절). */
const withJosa = (label: string, josa: string): string => (/[A-Za-z]$/u.test(label) ? `${label} ${josa}` : `${label}${josa}`);

/** 간선 종류의 이름. 범례, 포트 힌트, 간선 패널과 이슈 상세가 쓴다. */
const edgeLabels = {
  entry: "입구",
  model: "모델",
  tools: "Tool",
  delegate: "서브 에이전트",
  memory: "메모리",
  guardrail: "가드레일",
  knowledge: "지식",
} satisfies Record<EdgeKind, string>;

/** 상세에 적는 provider 의 낱말. 모델 노드의 provider 와 폴백 provider 는 같은 규칙을 따르고 상세의 낱말만 다르다. */
const providerRole = { provider: "provider", fallback: "폴백 provider" } satisfies Record<ProviderRole, string>;

/**
 * redis memory 의 redis_modules_missing 상세. 키는 verify 가 남긴 `capabilities.checkpointer` 이고 없으면 verify 전이다. 서버가
 * 내는 영어(lib/agentGraph.ts 의 REDIS_CHECKPOINTER_DETAIL) 대신 무엇이 모자란지와 무엇을 하면 되는지를 함께 적는다.
 */
const redisCheckpointerHint = {
  none: "Database 화면에서 연결 확인을 먼저 눌러 주세요. 체크포인터는 RediSearch·RedisJSON 을 갖춘 Redis 8 이 필요합니다.",
  modules_missing: "RediSearch·RedisJSON 이 없습니다. Redis 8 로 올리거나 두 모듈을 켠 Redis 를 선택해 주세요.",
  unknown: "연결 확인에 사용한 계정이 FT._LIST 를 실행하지 못했습니다. 계정에 그 권한을 주거나 다른 계정으로 연결 확인을 다시 눌러 주세요.",
} satisfies Record<RedisCheckpointerState, string>;

/** Agent 캔버스와 그래프 검사 (D26, D28). 팔레트, 노드 카드, 인스펙터, 검증 배지와 검증 이슈의 문장이다. */
export const canvas = {
  nodeLabels,
  edgeLabels,
  /** 아직 고르지 않은 참조. 카드 칩과 인스펙터가 쓴다. */
  unset: "미선택",
  databaseUnset: "Database 미선택",
  knowledgeBaseUnset: "Knowledge Base 미선택",
  providerUnset: "provider 미선택",
  /** 이름이 빈 에이전트와 모델 노드의 카드 제목. */
  unnamed: "(이름 없음)",
  /** 켜면 공유 카운터를 둘 memory 노드가 필요한 설정의 도움말. 분당 요청 한도와 일당 토큰 예산이 쓴다. */
  needsMemory: "켜면 memory 노드가 필요합니다 (공유 카운터).",
  /** 인스펙터에서 고른 리소스를 새 탭으로 여는 링크. kind 는 리소스 종류 이름이다(Database, Gateway). */
  open: "열기",
  openResource: (kind: string): string => `이 ${kind} 열기`,
  /** 인스펙터 탭. tools 는 에이전트와 MCP 노드의 Tool 탭이다. 메모리와 가드레일 탭은 nodeLabels 다. */
  tabs: { basic: "기본", limits: "한도", service: "서비스", prompt: "프롬프트", tools: "Tool" },
  /** 청지기의 제안을 불러온 캔버스의 노드 표시와 지우는 노드 목록의 제목 (P48 6.5절). 승인 카드의 바뀌는 것 목록도 쓴다. */
  proposalMarks: { added: "추가", changed: "변경", removedTitle: "삭제되는 노드" },
  /** 왼쪽 팔레트. hints 는 노드 버튼 아래의 한 줄이다. */
  palette: {
    nodes: "노드",
    hints: {
      entry: "세션 TTL, 턴 수, 사용자별 분당 한도, 인사말",
      agent: "메인 하나와 서브 여럿. 프롬프트·실행 한도·응답 형식",
      model: "provider·모델, 출력 토큰, 추론 강도, 폴백. 에이전트마다 하나",
      tools: "Gateway 또는 MCP Server 의 Tool, 승인·타임아웃",
      memory: "Postgres 또는 Redis 8 체크포인터, 세션 색인, 공유 카운터, 사용자별 장기 기억",
      guardrail: "최대 길이, 차단 패턴, PII 마스킹, 판정 모델",
      knowledge: "Knowledge Base 검색 Tool. 에이전트마다 여럿",
    } satisfies Record<NodeType, string>,
    addHint: "끌어다 놓거나 눌러서 추가합니다.",
    templates: "템플릿",
    templatesLoading: "템플릿을 불러오는 중입니다.",
    templatesError: "템플릿을 불러오지 못했습니다. 새로고침하거나 위 팔레트에서 노드를 직접 놓아 주세요.",
    templatesEmpty: "사용할 수 있는 템플릿이 없습니다. 위 팔레트에서 노드를 직접 놓아 주세요.",
  },
  /** 노드 카드의 포트, 검증 표식과 칩. */
  node: {
    port: (kind: EdgeKind): string => `${edgeLabels[kind]} 포트입니다. 끌어서 다른 노드 위에 놓으면 연결됩니다.`,
    errors: (count: number): string => `오류 ${count}건`,
    warnings: (count: number): string => `경고 ${count}건`,
    turns: (count: number): string => `${count}턴`,
    perMinute: (count: number): string => `${count}/분`,
    role: { main: "메인 에이전트", sub: "서브 에이전트" } satisfies Record<AgentData["role"], string>,
    noModel: "모델 없음",
    /** 에이전트에 이은 MCP 노드의 수. */
    tools: (count: number): string => `Tool ${count}`,
    knowledge: (count: number): string => `지식 ${count}`,
    /** 에이전트에 붙인 Skill 의 수. */
    skills: (count: number): string => `Skills ${count}`,
    subs: (count: number): string => `서브 ${count}`,
    fallback: (provider: string): string => `폴백 ${provider}`,
    /** 폴백을 켰지만 provider 를 아직 고르지 않았을 때. */
    fallbackUnset: "폴백 미선택",
    sharedBy: (count: number): string => `공유 ${count}`,
    agents: (count: number): string => `에이전트 ${count}`,
    sourceUnset: "소스 미선택",
    allowed: (count: number): string => `허용 ${count}`,
    allTools: "전체 Tool",
    timeouts: (count: number): string => `타임아웃 ${count}`,
    checkpointerOf: (kind: string): string => `${kind} 체크포인터`,
    checkpointer: "체크포인터",
    sessionIndex: "세션 색인",
    sharedCounter: "공유 카운터",
    /** retention 은 keepForever 이거나 common.time.days 다. */
    longTerm: (retention: string): string => `장기 기억 · ${retention}`,
    keepForever: "계속 보관",
    guardrail: (position: string): string => `${position} 가드레일`,
    rules: (count: number): string => `규칙 ${count}`,
    documents: (indexed: number, total: number): string => `문서 ${indexed}/${total}`,
    unknownKnowledgeBase: "알 수 없는 Knowledge Base",
    ttl: (seconds: number): string => `TTL ${seconds}초`,
    hitl: "HITL",
    json: "JSON",
    effort: "effort",
    temp: "temp",
  },
  /** 캔버스 왼쪽 아래의 간선 색 범례와 선택 방법. 삭제는 common.delete 다. */
  legend: { areaSelect: "끌어 영역 선택", multiSelect: "여러 개 선택" },
  /**
   * 끌고 있는 연결을 이을 수 없는 까닭(lib/agentGraph.ts 의 connectionProblem). 카드 아래와 툴팁에 보인다. 조사가 붙는
   * 자리라 메모리와 가드레일은 nodeLabels 를 끼우지 않고 종류별로 적는다("메모리 은" 이 되지 않게).
   */
  connection: {
    self: "자기 자신에게는 연결할 수 없습니다.",
    wrongPort: (kind: EdgeKind, target: NodeType): string => `${edgeLabels[kind]} 포트는 ${withJosa(nodeLabels[target], "에")} 연결할 수 없습니다.`,
    notAllowed: (source: NodeType, target: NodeType): string => `${withJosa(nodeLabels[source], "에서")} ${withJosa(nodeLabels[target], particle(nodeLabels[target], "으로/로"))}는 연결할 수 없습니다.`,
    already: "이미 연결돼 있습니다.",
    entryToSub: "입구는 메인 에이전트에만 연결합니다.",
    entryOnce: "입구는 하나의 에이전트에만 연결합니다.",
    delegateFromSub: "위임은 메인 에이전트에서만 나갑니다.",
    delegateToMain: "위임 대상은 서브 에이전트여야 합니다.",
    memoryToSub: "메모리는 메인 에이전트에만 붙습니다.",
    guardrailToSub: "가드레일은 메인 에이전트에만 붙습니다.",
    oneModel: "모델은 하나만 붙일 수 있습니다.",
    oneMemory: "메모리는 하나만 붙일 수 있습니다.",
    nodeMissing: "노드를 찾을 수 없습니다.",
  },
  /** 검증 배지와 펼친 이슈 목록. 이슈의 오류·경고 배지는 skills.severity 다. */
  validation: {
    summary: (errors: number, warnings: number): string =>
      errors || warnings ? `${errors ? `오류 ${errors}` : ""}${errors && warnings ? " · " : ""}${warnings ? `경고 ${warnings}` : ""}` : "검증 통과",
    source: { server: "서버", client: "로컬" } satisfies Record<"server" | "client", string>,
    count: (count: number): string => `이슈 ${count}건입니다. 누르면 그 노드로 이동합니다.`,
    passed: "저장·배포 규칙을 모두 통과했습니다.",
    serverHint: "서버 검증은 상단의 검증 버튼으로 실행합니다.",
  },
  /** 아무것도 고르지 않았을 때의 Agent 패널. 이름 칸은 NameFields 다. */
  profile: {
    displayPlaceholder: "고객지원 에이전트",
    selectHint: "노드를 선택하면 그 노드의 설정이, 간선을 선택하면 간선 정보가 여기 보입니다.",
    sessionTokens: "세션당 토큰 예산",
    sessionTokensHint: "비우면 한도가 없습니다. 넘으면 429 로 턴을 거부합니다.",
    dailyTokens: "일당 토큰 예산",
    recordBodies: "본문 기록",
    recordBodiesHint: "관측 이벤트에 메시지·Tool 인자·결과 본문을 싣습니다. 기본은 끔입니다.",
    heading: "Agent",
  },
  /** 간선을 고른 패널. */
  edge: {
    title: (kind: EdgeKind): string => `${edgeLabels[kind]} 간선`,
    delete: "간선 삭제",
    from: "시작",
    to: "끝",
  },
  /** 여러 노드 패널의 이름과 설명 칸. */
  fields: { name: "이름", description: "설명" },
  /** Chat 트리거 패널. 인사말 칸 이름은 shared.chatPanel.greeting 이다. */
  entry: {
    greetingHint: "세션이 시작될 때 먼저 보이는 문장입니다. 비우면 없습니다.",
    sessionTtl: "세션 TTL (초)",
    sessionTtlHint: "마지막 턴 뒤 이 시간이 지나면 세션을 닫습니다.",
    maxTurns: "세션당 최대 턴",
    rateLimit: "사용자별 분당 요청 한도",
    perMinute: "분당 요청",
    maxConcurrent: "동시 세션 수",
    maxInput: "입력 최대 길이 (자)",
    maxInputHint: "넘으면 413 max_input_chars 로 거부합니다.",
  },
  /** 에이전트 패널. */
  agent: {
    subNotice: "서브 에이전트의 응답 언어와 응답 형식은 시스템 프롬프트에 적습니다. 컨텍스트 압축은 메인의 압축 정책을 따릅니다.",
    role: "역할",
    roleHint: "메인은 하나뿐입니다. 서브는 메인이 위임하는 task Tool 이 됩니다.",
    nameHint: "소문자 영문·숫자·하이픈으로 작성합니다. 서브 에이전트 이름은 task Tool 의 인자입니다.",
    descriptionHint: {
      main: "무엇을 맡는 에이전트인지 적습니다.",
      sub: "메인 에이전트가 task Tool 설명으로 읽습니다. 서브는 필수입니다.",
    } satisfies Record<AgentData["role"], string>,
    language: "응답 언어",
    languageHint: "예: ko, en. 비우면 지정하지 않습니다.",
    systemPrompt: "시스템 프롬프트",
    responseFormat: "응답 형식",
    editorLoading: "편집기를 불러오는 중입니다.",
    schemaHint: "응답이 이 JSON schema 를 따르도록 모델에 요구합니다.",
    /** 응답 형식 편집기의 원문을 스키마로 읽지 못한 까닭. detail 은 JSON 파서의 말이고, error 는 json 이나 notObject 의 문장이다. */
    schemaError: {
      json: (detail: string): string => `JSON 오류: ${detail}.`,
      notObject: "JSON 객체여야 합니다.",
      reverted: (error: string): string => `${error} 기본 스키마로 되돌렸습니다.`,
    },
    filesystem: "내장 파일 Tool 노출",
    filesystemHint: "deepagents 의 ls·read_file·write_file·edit_file·delete·glob·grep 을 세션 state 파일시스템에 엽니다. 기본은 노출입니다. Skills 와 장기 기억은 이 Tool 이 있어야 합니다.",
    maxToolCalls: "턴당 최대 Tool 호출",
    turnTimeout: "턴 타임아웃 (초)",
    retries: "재시도",
    tools: "연결된 MCP",
    toolsEmpty: "캔버스에서 이 에이전트의 오른쪽 Tool 포트를 MCP 노드로 이으면 그 Tool 을 사용합니다.",
    knowledge: "연결된 지식 (Knowledge Base 검색 Tool)",
    knowledgeEmpty: "오른쪽 가운데 지식 포트에서 지식 노드로 이으면 그 Knowledge Base 를 검색 Tool 로 사용합니다. 여럿을 붙일 수 있습니다.",
    delegates: "위임하는 서브 에이전트",
    delegatesEmpty: "오른쪽 아래 위임 포트에서 서브 에이전트 노드로 이으면 task Tool 로 위임합니다.",
    checkpointer: "세션 상태 체크포인터",
    checkpointerEmpty:
      "위 왼쪽 메모리 포트에서 메모리 노드로 이으면 세션 상태·색인·공유 카운터를 Postgres 나 Redis 8 에 둡니다. 승인·분당 한도·일당 예산을 쓰려면 필요합니다.",
    guardrails: "붙은 가드레일",
    guardrailsEmpty: "아래 가드레일 포트에서 가드레일 노드로 이으면 입력·출력·Tool 호출을 검사합니다.",
    guardrailRow: (position: string, rules: number): string => `${position} · 규칙 ${rules}`,
    /** 기본 탭의 붙은 모델. 둘 이상 붙었을 때의 문장은 connection.oneModel 이다. */
    model: "붙은 모델",
    noModel: "모델 노드가 없습니다 (model_count). 이 에이전트를 선택한 채 팔레트에서 모델을 추가하거나, 위 오른쪽 모델 포트에서 모델 노드로 이어 주세요.",
    modelHint: "provider·모델·추론 강도는 모델 노드를 선택해 바꿉니다.",
    formatText: "text",
    formatSchema: "JSON schema",
  },
  /** 메인 에이전트 Tool 탭의 코드 실행 칸(P40 9절). 게이트가 닫힌 까닭은 clusters.sandbox.reason 의 문장이다. */
  codeExecution: {
    label: "코드 실행",
    hint: "켜면 Agent 가 대화마다 격리된 작업 공간에서 Python 과 셸 명령을 실행하고 파일을 만듭니다. 파일 Tool 도 이 작업 공간을 사용합니다. 작업 공간은 네트워크에 연결되지 않고, 30분 동안 사용하지 않거나 대화가 닫히면 그 안의 파일과 함께 지워집니다.",
    approval: "명령 실행 전 승인",
    filesystemReason: "코드 실행은 메인 에이전트의 내장 파일 Tool 이 켜져 있어야 쓸 수 있습니다.",
  },
  /** 에이전트 프롬프트 탭의 고급 접기. 컨텍스트 압축(SummarizationMiddleware)의 발동 기준과 보관량이다. */
  compaction: {
    /** defaults 는 기본값을 적은 작은 글씨 요소(compaction.defaults)다. */
    summary: (defaults: ReactNode): ReactNode => <>고급: 컨텍스트 압축 {defaults}</>,
    defaults: (value: string): string => `(기본 ${value})`,
    help: "SummarizationMiddleware 의 발동 기준과 보관량입니다. 요약은 끌 수 없습니다. 컨텍스트가 넘쳐 실패하는 것보다 요약하는 편이 낫기 때문입니다. 컨텍스트 창이 큰 모델은 요약 한 번이 턴 타임아웃을 넘길 수 있습니다. 발동 기준을 토큰 수로 두거나 턴 타임아웃을 늘려 주세요.",
    trigger: "발동 기준",
    triggerKinds: { fraction: "컨텍스트 비율 (fraction)", tokens: "토큰 수 (tokens)", messages: "메시지 수 (messages)" },
    value: "값",
    keep: "보관량",
    keepKinds: { fraction: "비율 (fraction)", messages: "메시지 수 (messages)" },
  },
  /** 모델 노드 패널. 모델 칸 이름은 nodeLabels.model 이다. */
  model: {
    nameHint: "캔버스에서 이 모델을 부르는 이름입니다.",
    providerHint: "Platform Settings 에 등록한 LLM provider 입니다.",
    modelHint: "비우면 provider 의 기본 모델입니다.",
    maxOutputTokens: "출력 토큰 상한",
    effort: "추론 강도",
    effortHintOpenai: "openai 계열은 max 를 받지 않습니다.",
    effortHintGlm: "GLM 모델은 glm-5.3 이 low, high, max 를, glm-5.2 가 none 부터 max 까지를 받습니다. 받지 않는 값은 가장 가까운 값으로 바꿔 보내고, 다른 GLM 모델에는 추론 강도를 보내지 않습니다.",
    effortHint:
      "off 는 끌 수 있는 모델(Claude 3·4.x·Opus 5·Sonnet 5·Haiku 5.5)에서만 적용되고 그 밖의 모델은 low 로 갑니다. 4.5 세대 이하는 추론 인자를 보내지 않고 4.6 세대의 xhigh 는 high 로 갑니다.",
    temperatureHint: "비우면 provider 기본값입니다.",
    /** saved 는 저장된 temperature 값이 있는가다. */
    temperatureNote: (saved: boolean): string => `temperature 는 openai 계열 provider 에서만 적용됩니다.${saved ? " 저장된 값은 무시되고 경고가 붙습니다." : ""}`,
    fallback: "폴백 provider·model",
    fallbackHint: "이 모델이 실패하면 폴백 모델로 다시 시도합니다. 키 Secret 도 함께 복사됩니다.",
    fallbackProvider: "폴백 provider",
    fallbackModel: "폴백 모델",
    users: "이 모델을 쓰는 에이전트",
    usersEmpty: "에이전트의 위 오른쪽 모델 포트에서 이 노드로 이으면 그 에이전트가 이 모델을 사용합니다. 여러 에이전트가 한 모델을 공유할 수 있습니다.",
    /** 모델 패널의 칸 이름. */
    labels: {
      provider: "Provider",
      temperature: "temperature",
    },
  },
  /** MCP 노드 패널. 승인 결정 종류(approve 등)와 Tool 이름은 그대로 보인다. */
  tools: {
    approval: "승인",
    timeouts: "타임아웃",
    sourceKind: "소스 종류",
    source: "소스",
    sourceHint: {
      gateway_instance: "Gateway 를 부릅니다. 배포된 것만 선택할 수 있습니다.",
      mcp_instance: "MCP Server 의 프록시를 부릅니다. 배포된 것만 선택할 수 있습니다.",
    } satisfies Record<McpSourceKind, string>,
    // Gateway 가 아직 IdP 토큰만 받는 oidc 모드의 Gateway 소스 안내다(P53b 6.1절). 그동안 Agent 는 agent_oauth 로 부른다
    sourceHintIdpGateway: "Gateway 를 부릅니다. oidc 모드면 Tenant Settings 의 agent_oauth 가 필요합니다. 배포된 것만 선택할 수 있습니다.",
    allow: "허용 Tool",
    allowCount: (count: number): string => `${count}개만 노출합니다.`,
    allowAll: "아무것도 선택하지 않으면 배포할 때 소스에 있던 Tool 을 모두 노출합니다. 그 뒤 소스에 추가된 Tool 은 다시 배포해야 노출됩니다.",
    /** Tool 효과 배지(P39b 14절 4부). 검사(validate)를 한 뒤에 보인다. */
    effect: { read: "읽기", write: "쓰기", web: "웹" } satisfies Record<ToolEffect, string>,
    effectsUnknown: "검증을 실행하면 Tool 마다 읽기·쓰기·웹 효과가 보입니다.",
    readOnly: "읽기 전용 표시",
    readOnlyConfirm: "선택한 Tool 이 모두 외부에 데이터를 쓰지 않는다는 것을 확인했습니다",
    readOnlyApply: "읽기 전용으로 표시",
    readOnlyClear: "표시 해제",
    readOnlyHintOutside:
      "remote·entry 항목처럼 서버가 읽기 전용 여부를 알려 주지 않는 Tool 만 표시할 수 있습니다. 표시한 Tool 은 승인 설정이 없으면 외부 내용이 들어온 대화에서도 승인 없이 실행되므로, 외부에 데이터를 쓰지 않는 Tool 만 표시해 주세요.",
    readOnlyIgnored: "이 Tool 은 서버나 허브 항목이 정한 읽기·쓰기 구분을 따르므로 캔버스의 표시는 적용되지 않습니다.",
    outside: "외부 내용을 가져오는 Tool",
    outsideHint:
      "결과에 메일, 웹 페이지, 문서처럼 외부에서 온 글이 섞일 수 있는 Tool 을 선택해 주세요. image·remote·entry 항목의 Tool 은 처음부터 선택되어 있습니다. 그 결과를 읽은 대화에서는 데이터를 바꿀 수 있는 Tool 이 승인 없이 실행되지 않습니다.",
    outsideApproval: "외부 내용이 들어오면 승인",
    outsideApprovalHint: "대화에 외부 내용이 들어오면 승인 설정이 없는 쓰기 Tool 도 승인을 받아야 실행합니다. 읽기 Tool 로 표시되지 않은 Tool 은 모두 쓰기 Tool 로 다룹니다.",
    outsideRefused: "외부 내용이 들어오면 거절",
    outsideRefusedHint: "이 그래프에는 memory 노드가 없어 대화에 외부 내용이 들어오면 이 Tool 을 실행하지 않습니다.",
    rememberable: "승인 기억 켜기",
    rememberableHint:
      "켜면 사용자가 이 Tool 의 승인을 기억해 다음부터 묻지 않게 할 수 있습니다. 대화에 외부 내용이 있으면 다시 승인을 요청합니다. 승인 설정이나 이 값을 바꿔 배포하면 기억한 선택은 지워집니다.",
    allowDomains: "허용 도메인",
    allowDomainsHint: "web_fetch 가 접속할 수 있는 도메인입니다. 쉼표로 구분합니다. 데이터 Tool 이 있는 그래프나 파일을 첨부한 대화에서는 이 목록에 있는 사이트에서만 가져오고, 목록이 비어 있으면 모두 거절합니다. 다른 사람이 요청 기록을 볼 수 있는 사이트(단축 URL, 붙여넣기 서비스, 설문 응답 서비스)는 넣지 마세요.",
    blockDomains: "차단 도메인",
    blockDomainsHint: "web_fetch 가 접속하지 않는 도메인입니다. 쉼표로 구분합니다.",
    webHint: "web_search 와 web_fetch 는 플랫폼 Tool 입니다. Gateway 에 넣어 사용합니다.",
    webHintLink: "웹 Tool 로 Gateway 만들기",
    /** code mode Gateway 의 execute_tool_script 안내 (P56 6.5절). 검사가 그 Tool 을 스크립트로 본 노드에 선다. */
    codeModeHint:
      "execute_tool_script 는 스크립트 안에서 이 Gateway 가 이 Agent 에게 허용하는 모든 Tool 을 부릅니다. 이 Agent 의 어느 노드에서든 이 Gateway 의 Tool 에 승인을 하나라도 설정했으면 스크립트도 승인을 받고, 스크립트 결과는 외부 내용으로 다룹니다.",
    notInSource: "소스에 없음",
    listUnknown: "소스의 Tool 목록을 알 수 없습니다. 이름을 직접 적어 주세요.",
    pickSource: "소스를 선택하면 Tool 목록이 나옵니다.",
    extraPlaceholder: "Tool 이름을 직접 추가",
    approvalHelp: "Tool 마다 사람이 선택할 수 있는 결정 종류입니다. 하나라도 켜면 memory 노드가 필요합니다 (체크포인터).",
    allowFirst: "허용 Tool 을 먼저 정해 주세요.",
    timeoutsHelp: "Tool 호출마다 기다릴 최대 초입니다. 비우면 런타임 기본값입니다.",
    timeoutLabel: (tool: string): string => `${tool} 타임아웃(초)`,
    timeoutDefault: "기본",
    /** 게시한 Agent 를 Tool 로 부르는 소스 (P46 6.2절). group 은 소스 종류의 선택지, subtitle 은 노드 카드의 둘째 줄이다. */
    sources: { agent: { group: "Agent", subtitle: "Agent 호출" } },
    /** Agent 소스의 Tool 은 효과와 외부 내용 표시를 바꿀 수 없다 (P46 6.6절). */
    agentMarks:
      "이 Tool 은 항상 쓰기 Tool 로 다루고 결과는 외부 내용으로 표시합니다. 호출받은 Agent 가 자기 Tool 로 데이터를 바꿀 수 있고 그 답에 외부 내용이 들어 있을 수 있기 때문입니다.",
    /** 부르는 대화의 글이 가는 곳. provider 는 호출받은 Agent 의 메인 모델 provider, recording 은 그 Agent 의 본문 기록이 켜졌는지다. */
    agentDataFlow: (provider: string, recording: string): string =>
      `이 대화의 내용은 호출받은 Agent 의 LLM provider(${provider})로 보내고, 그 Agent 의 본문 기록 설정(${recording})을 따릅니다.`,
    /** `agentDataFlow` 의 recording 자리에 서는 말. */
    agentRecording: { on: "켜짐", off: "꺼짐" },
    /** Agent 소스의 타임아웃 탭. */
    agentTimeout: "기다리는 시간을 넘기면 호출을 멈추고 호출받은 Agent 의 실행도 취소합니다.",
    /** Agent 가 이 Tool 을 Gateway 보다 먼저 끊는다 (P50 7절). agent·gateway 는 초다. */
    timeoutShorter: (tool: string, agent: number, gateway: number): string =>
      `${tool} 의 타임아웃(${agent}초)이 Gateway 가 이 Tool 을 기다리는 시간(${gateway}초)보다 짧습니다. 느린 호출은 Agent 가 먼저 끊습니다.`,
  },
  /** 메모리 노드 패널. 장기 기억 절(D34)을 켤 수 없으면 그 까닭을 적는다. agent 는 메인 에이전트 이름이다. */
  memory: {
    noMain: "메인 에이전트가 없어 아직 켤 수 없습니다. 팔레트에서 에이전트를 추가하고 그 에이전트의 위 왼쪽 메모리 포트에서 이 노드로 이어 주세요.",
    notAttached: (agent: string): string => `이 메모리가 메인 에이전트 '${agent}' 에 붙어 있지 않아 켤 수 없습니다. 그 에이전트의 위 왼쪽 메모리 포트에서 이 노드로 이어 주세요.`,
    filesystemOff: (agent: string): string =>
      `메인 에이전트 '${agent}' 의 내장 파일 Tool 이 꺼져 있어 켤 수 없습니다. 그 에이전트 노드를 선택하고 Tool 탭에서 '내장 파일 Tool 노출' 을 켜 주세요. 에이전트는 그 Tool 로 /memories/ 의 파일을 읽고 작성합니다.`,
    databaseHint: "Postgres 또는 Redis 8(RediSearch·RedisJSON) 입니다. 세션 체크포인터와 색인, 공유 카운터를 여기에 둡니다. 배포할 때 Secret 참조로 바인딩됩니다.",
    noStores: "이 테넌트에 Postgres 나 Redis Database 가 없습니다. Tenant › Database 에서 먼저 등록해 주세요.",
    longTerm: "장기 기억",
    longTermVsKnowledge: "지식(Knowledge)은 여럿이 함께 보도록 선택해 둔 자료를 읽기만 하는 검색이고, 장기 기억은 사용자마다 따로 쌓이며 에이전트가 스스로 작성합니다.",
    longTermEnable: "장기 기억 켜기",
    longTermHint: "세션이 끝나도 남습니다. 에이전트가 /memories/ 아래에 파일로 직접 작성합니다. 경로는 고정입니다.",
    retention: "보관 기간 (일)",
    retentionPlaceholder: "안 지움",
    retentionHint: "마지막으로 쓴 뒤 이만큼 지난 기억을 지웁니다. 하루 이상이어야 합니다.",
    keptForever: "보관 기간이 비어 있어 기억을 지우지 않습니다. 사용자나 에이전트가 지울 때까지 남습니다.",
    retentionNote: (days: number): string => `${days}일 동안 손대지 않은 기억을 지웁니다.`,
    longTermScope: "이 에이전트가 사용자마다 따로 기억합니다. 다른 사용자에게는 보이지 않습니다.",
    checkpointer: "checkpointer",
    unverified: "unverified",
    /** 기억 패널의 칸 이름. */
    labels: {
      database: "Database",
    },
  },
  /** 가드레일 노드 패널과 카드. 규칙 종류와 PII 종류는 그대로 보인다. */
  guardrail: {
    positions: { input: "입력", output: "출력", tool: "Tool" } satisfies Record<GuardrailData["position"], string>,
    violations: { block: "차단", flag: "표시" } satisfies Record<GuardrailData["on_violation"], string>,
    position: "위치",
    positionHint: "입력은 사용자 메시지를, 출력은 모델 답을, Tool 은 호출 인자와 결과를 검사합니다. 출력에 가리기나 막기가 있으면 답이 메시지 단위로 한 번에 나옵니다.",
    onViolation: "위반 시",
    onViolationHint: "차단은 안내문으로 답하고 턴을 멈춥니다. 표시는 이벤트만 남깁니다.",
    rules: "규칙",
    addRule: "규칙 추가",
    ruleKind: "규칙 종류",
    maxLength: "최대 길이",
    denyPattern: "차단 정규식",
    regex: "정규식",
    piiRegex: "PII 정규식",
    message: "안내문",
    messageHint: "차단할 때 사용자에게 보이는 문장입니다.",
    /** injection_check 규칙(P43 7절). 판정 모델 셀렉트의 줄은 모델 노드의 이름이고, max_chars·max_calls 는 칸 이름 그대로 보인다. */
    judgeModel: "판정 모델",
    judgeModelHint: "작고 빠른 모델을 선택해 주세요. 판정 모델은 외부 내용이 담긴 Tool 결과마다 한 번씩 추론 없이 호출됩니다.",
    injectionHint: "외부 내용을 판정 모델이 먼저 읽고 숨은 지시가 있는지 검사합니다. 판정 모델의 호출은 이 Agent 의 사용량에 기록됩니다.",
    injectionLimitsHint: "max_chars 보다 긴 결과는 앞과 뒤만 검사합니다. 한 턴에 max_calls 번을 넘으면 나머지 결과는 검사하지 않고 외부 내용으로만 다룹니다.",
  },
  /** 지식 노드 패널 (D32). Knowledge Base 셀렉트의 줄, Tool 이름과 설명, top_k 와 min_score 의 도움말이다. */
  knowledge: {
    baseHint: "이 에이전트가 검색할 Knowledge Base 입니다. 배포할 때 벡터 테이블의 Database 와 임베딩 provider 키가 함께 바인딩됩니다.",
    option: (name: string, model: string, indexed: number, total: number): string => `${name} (${model} · 문서 ${indexed}/${total})`,
    empty: "이 테넌트에 Knowledge Base 가 없습니다. Tenant › Knowledge Base 에서 먼저 만들어 주세요.",
    toolName: "Tool 이름",
    toolNameHint: "영문·숫자·밑줄 64자 이내입니다. 같은 에이전트에 붙은 다른 지식 노드와 달라야 하고 런타임 내장 이름(ls, read_file, task 등)은 쓸 수 없습니다.",
    descriptionHint: "모델이 언제 이 Tool 을 부를지 판단하는 문장입니다. 비우면 Knowledge Base 이름으로 기본 문장을 만듭니다.",
    topKHint: "한 번에 돌려주는 구절 수입니다 (1..20).",
    minScoreHint: "비우면 거르지 않습니다 (0..1).",
    outside: "외부 내용으로 다루기",
    outsideHint: "이 Knowledge Base 의 검색 결과를 외부 내용으로 다룹니다. 검토를 거친 문서만 있는 Knowledge Base 라면 끌 수 있습니다.",
    /** 지식 패널의 칸 이름. */
    labels: {
      base: "Knowledge Base",
    },
  },
  /** 에이전트 패널 Tool 탭의 Skills 절 (D64). name 은 Skill 의 표시 이름이다. */
  skills: {
    full: (max: number): string => `에이전트 하나에는 Skill 을 ${max}개까지 붙일 수 있습니다.`,
    none: "붙일 수 있는 Skill 이 없습니다. Skills 화면에서 먼저 만들어 주세요.",
    loading: "Skill 목록을 불러오는 중입니다.",
    consumeOnly: "사용 권한(consume)이 있는 Skill 만 보입니다. 붙이면 지금의 최신 리비전으로 고정됩니다.",
    intro: "에이전트는 요청에 맞는 Skill 의 SKILL.md 를 읽고 그 절차를 따릅니다. 새 리비전은 '최신으로' 를 누르고 저장한 뒤 재배포해야 반영됩니다.",
    notConsumable: "사용 권한 없음",
    upgrade: "최신으로",
    upgradeLabel: (name: string): string => `${name} 최신으로`,
    detachLabel: (name: string): string => `${name} 빼기`,
    empty: "붙인 Skill 이 없습니다.",
    attach: "Skill 붙이기",
    pick: "Skill 선택",
    withScripts: "스크립트 포함",
    heading: "Skills",
  },
  /**
   * 그래프 검사 이슈 (lib/agentGraph.ts 의 issueText). `text` 는 code 별 문장으로 무엇이 잘못됐는지와 무엇을 하면 되는지를 적고,
   * 캔버스 검사기가 낸 이슈면 뒤에 `detail`(그래프 검사)이나 `skillDetail`(Skill 검사, lib/agentSkills.ts)의 상세가 붙는다.
   * 서버 `POST /validate` 와 저장 응답의 영어 이슈도 같은 code 로 text 를 찾는다. code 는 서버와 같아야 하므로 절대 바꾸지
   * 않는다. at 을 받는 문장은 이슈가 노드에 붙었는지 간선에 붙었는지로 갈린다.
   */
  issues: {
    text: {
      duplicate_node_id: "같은 id 를 쓰는 노드가 둘 있습니다. 한쪽 노드를 지워 주세요.",
      duplicate_edge_id: "같은 id 를 쓰는 간선이 둘 있습니다. 한쪽 간선을 지워 주세요.",
      invalid_node_data: "이 노드의 설정 값이 올바르지 않습니다.",
      entry_count: (at: IssueAt): string =>
        at.node_id ? "Chat 트리거 노드는 하나만 둘 수 있습니다. 남는 트리거 노드를 지워 주세요." : "Chat 트리거 노드가 하나 있어야 합니다. 팔레트에서 Chat 트리거를 추가해 주세요.",
      main_agent_count: (at: IssueAt): string =>
        at.node_id ? "메인 에이전트는 하나만 둘 수 있습니다. 이 에이전트의 역할을 서브로 바꾸거나 지워 주세요." : "역할이 메인인 에이전트가 하나 있어야 합니다. 팔레트에서 에이전트를 추가해 주세요.",
      memory_count: (at: IssueAt): string =>
        at.edge_id ? "메인 에이전트에는 메모리를 하나만 붙일 수 있습니다. 남는 메모리 간선을 지워 주세요." : "메모리 노드는 하나만 둘 수 있습니다. 남는 메모리 노드를 지워 주세요.",
      model_count: (at: IssueAt): string =>
        at.edge_id ? "에이전트에는 모델을 하나만 붙일 수 있습니다. 남는 모델 간선을 지워 주세요." : "이 에이전트에 모델 노드가 없습니다. 카드 위 오른쪽 모델 포트에서 모델 노드로 이어 주세요.",
      dangling_edge: "이 간선이 없는 노드를 가리킵니다. 간선을 지우거나 그 노드를 다시 만들어 주세요.",
      invalid_edge_kind: "이 간선은 연결 규칙에 맞지 않습니다. 간선을 지우고 맞는 포트끼리 이어 주세요.",
      delegation_cycle: "위임이 돌고 돌아 자기에게 돌아옵니다. 이 간선을 지워 주세요.",
      unreachable_agent: "이 에이전트는 Chat 트리거와 이어져 있지 않아 실행되지 않습니다.",
      unattached_node: "이 노드가 아무 곳에도 붙어 있지 않아 무시됩니다. 포트로 이어 주거나 지워 주세요.",
      unknown_judge_model: "판정 모델을 정하지 못했습니다. 오른쪽 패널에서 이 그래프의 모델 노드를 판정 모델로 선택해 주세요.",
      judge_count: "판정 모델 규칙(injection_check)은 그래프에 하나만 둘 수 있습니다. 남는 규칙을 지워 주세요.",
      unknown_provider: "Platform Settings 에서 provider 를 찾지 못했습니다. 등록된 provider 를 선택하거나 Platform Settings 에 먼저 등록해 주세요.",
      provider_no_key: "provider 에 API 키 Secret 이 없습니다. Platform Settings 에서 api_key_secret 을 채워 주세요.",
      unsupported_param: "anthropic provider 는 temperature 를 쓰지 않습니다. 값을 비워 두어도 됩니다.",
      unknown_node_keys: "이 노드에 이 플랫폼 버전이 알지 못하는 설정이 있어 무시됩니다. allow_domains 처럼 설정 이름이 맞는지 확인해 주세요.",
      duplicate_agent_name: "에이전트 이름이 다른 에이전트와 겹칩니다. 오른쪽 패널에서 다른 이름을 지어 주세요.",
      unknown_source: "MCP 소스를 정하지 못했습니다. 오른쪽 패널에서 이 테넌트의 Gateway 나 MCP Server 를 선택해 주세요.",
      source_not_deployed: "이 Tool 소스는 아직 배포되지 않았습니다. 그 MCP Server 나 Gateway 를 먼저 배포하거나, 배포된 다른 소스를 선택해 주세요.",
      agent_oauth_missing:
        "플랫폼이 oidc 모드라 Gateway 소스에는 Tenant Settings 의 agent_oauth(client credentials)가 있어야 합니다. Tenant Settings 에서 먼저 채워 주세요.",
      agent_client_id_not_allowed: "agent_oauth 의 client_id 가 Tenant Settings 의 agent_client_ids 에 없어 Gateway 가 거절합니다. 그 목록에 client_id 를 추가해 주세요.",
      unknown_data_service: "체크포인터로 쓸 Database 를 정하지 못했습니다. 오른쪽 패널에서 Postgres 나 Redis Database 를 선택해 주세요.",
      invalid_data_service_kind: "이 Database 는 체크포인터로 쓸 수 없습니다. Postgres 나 Redis 를 선택해 주세요.",
      redis_modules_missing: "이 Redis 는 아직 체크포인터로 쓸 수 없습니다. Database 화면에서 상태를 확인해 주세요.",
      memory_required: "메모리 노드가 있어야 합니다. 팔레트에서 메모리를 추가해 메인 에이전트에 이어 주세요.",
      long_term_needs_memory: "장기 기억을 켰지만 기억을 둘 저장소가 없습니다. 메인 에이전트의 위 왼쪽 메모리 포트에서 이 메모리 노드로 이어 주세요.",
      long_term_filesystem_disabled:
        "장기 기억을 켜려면 메인 에이전트의 내장 파일 Tool 이 필요합니다. 에이전트 노드를 선택하고 Tool 탭에서 '내장 파일 Tool 노출' 을 켜 주세요. 에이전트는 그 Tool 로 /memories/ 의 파일을 읽고 작성합니다.",
      code_execution_filesystem_disabled: "코드 실행을 켜려면 메인 에이전트의 내장 파일 Tool 이 필요합니다. 에이전트 노드를 선택하고 Tool 탭에서 '내장 파일 Tool 노출' 을 켜 주세요.",
      code_execution_turn_timeout_short: `메인 에이전트의 턴 타임아웃이 명령 하나의 최대 실행 시간(${SANDBOX_COMMAND_MAX_SECONDS}초)보다 짧아 긴 명령은 턴 타임아웃에서 멈춥니다. Tool 탭에서 턴 타임아웃을 ${SANDBOX_COMMAND_MAX_SECONDS}초 이상으로 늘려 주세요.`,
      redis_db_not_zero:
        "이 Redis Database 는 DB 번호가 0 이 아니라 쓸 수 없습니다. RediSearch 가 db 0 에서만 인덱스를 만들기 때문입니다. DB 번호가 0 인 Database 를 선택하거나 Database 화면에서 DB 번호를 0 으로 바꿔 주세요.",
      reserved_tool_name: "이 Tool 이름은 런타임 내장 Tool 과 겹칩니다. 오른쪽 패널에서 다른 이름을 지어 주세요.",
      unknown_knowledge_base: "Knowledge Base 를 정하지 못했습니다. 오른쪽 패널에서 이 테넌트의 Knowledge Base 를 선택해 주세요.",
      embedding_not_supported:
        "이 Knowledge Base 의 임베딩 provider 가 임베딩을 지원하지 않는 kind 로 바뀌었습니다. Platform Settings 에서 kind 를 되돌리거나 다른 Knowledge Base 를 선택해 주세요.",
      knowledge_base_deleting: "이 Knowledge Base 는 삭제 중입니다. 다른 Knowledge Base 를 선택해 주세요.",
      knowledge_base_empty: "이 Knowledge Base 에 인덱싱된 문서가 아직 없어 검색 결과가 나오지 않습니다. Knowledge Base 화면에서 문서를 업로드해 주세요.",
      knowledge_base_failed: "이 Knowledge Base 에 인덱싱이 실패한 문서가 있습니다. Knowledge Base 화면에서 무엇이 실패했는지 확인해 주세요.",
      duplicate_tool_name: "같은 에이전트에 붙은 다른 지식 노드와 Tool 이름이 같습니다. 오른쪽 패널에서 Tool 이름을 다르게 지어 주세요.",
      builtin_tool_name: (at: IssueAt): string =>
        `이 Tool 소스에 내장 Tool 과 이름이 같은 Tool 이 있어, 해당 에이전트는 소스의 Tool 대신 내장 Tool 을 사용합니다. 소스에서 Tool 이름을 바꾸거나 prefix 모드인 Gateway 를 거쳐 연결해 주세요. 겹치는 Tool: ${clashNames(at.data)}`,
      skills_filesystem_disabled:
        "Skill 을 붙였지만 메인 에이전트의 내장 파일 Tool 이 꺼져 있어 에이전트가 Skill 을 읽지 못합니다. 메인 에이전트 노드를 선택하고 Tool 탭에서 '내장 파일 Tool 노출' 을 켜거나 Skill 을 빼 주세요.",
      too_many_skills: (at: IssueAt): string =>
        at.node_id
          ? `에이전트 하나에는 Skill 을 ${MAX_SKILLS_PER_AGENT}개까지 붙일 수 있습니다. Skill 설명이 모든 모델 호출에 실리기 때문입니다. 남는 Skill 을 빼 주세요.`
          : `그래프 전체에는 서로 다른 Skill 을 ${MAX_SKILLS_PER_GRAPH}개까지 붙일 수 있습니다. 남는 Skill 을 빼 주세요.`,
      skill_revision_mismatch:
        "같은 Skill 을 에이전트마다 다른 리비전으로 고정했습니다. 낮은 리비전을 고정한 에이전트에서 그 Skill 의 '최신으로' 를 눌러 리비전을 하나로 맞춰 주세요.",
      skill_name_conflict: "이름이 같은 Skill 둘을 함께 붙였습니다. 파일을 두는 폴더가 겹치므로 하나를 빼거나 다른 이름으로 복제해 붙여 주세요.",
      unknown_skill: "Skill 이나 그 리비전을 찾지 못했습니다. 삭제됐거나 이 테넌트에 공개되지 않은 Skill 입니다. 에이전트에서 그 Skill 을 빼 주세요.",
      skills_too_large: `붙인 Skill 의 파일이 너무 큽니다. 서로 다른 Skill 의 합은 ${MAX_SKILLS_DISTINCT_BYTES / MIB} MiB, 에이전트마다 쓰는 합은 ${MAX_SKILLS_EXPANDED_BYTES / MIB} MiB 까지입니다. 붙인 Skill 을 줄여 주세요.`,
      /** Skill 화면의 검사와 같은 code 이고 같은 문장이다. */
      skill_scripts_inert: skills.issues.skill_scripts_inert,
      skill_behind: "고정한 리비전보다 새 리비전이 있습니다. 새 리비전을 쓰려면 '최신으로' 를 누르고 저장한 뒤 재배포해 주세요.",
      // code mode Gateway 의 스크립트를 든 tools 노드 (P56 6.5절, platform/daegam/services/agent_code_mode.py)
      code_mode_partial_selection:
        "execute_tool_script 는 이 Gateway 의 모든 Tool 을 부를 수 있어, Tool 을 일부만 선택한 노드에서는 사용할 수 없습니다. 선택을 모두 비워 전체 Tool 을 사용하거나 선택에서 execute_tool_script 를 빼 주세요.",
      code_mode_duplicate_script:
        "한 에이전트에는 execute_tool_script 를 Gateway 하나에서만 붙일 수 있습니다. 승인 설정과 Tool 이름이 Gateway 를 구별하지 않기 때문입니다. 다른 Gateway 의 노드에서는 execute_tool_script 를 뺀 Tool 을 선택해 주세요.",
    } satisfies Record<string, string | ((at: IssueAt) => string)>,
    providerRole,
    redisCheckpointerHint,
    /** 그래프 검사의 상세. 이슈마다 다른 이름, 값, 이유를 인자로 받는다. 노드와 간선 종류, Redis 상태, provider 자리는 id 로 받는다. */
    detail: {
      nodeData: (error: string): string => `(${error})`,
      subAgentDescription: "서브 에이전트에는 설명이 필요합니다. 메인 에이전트가 그 설명을 읽고 언제 넘길지 정합니다.",
      missingNode: (node: string): string => `(없는 노드: ${node})`,
      edgeBetween: (source: NodeType, target: NodeType, kind: EdgeKind): string => `(${nodeLabels[source]} 노드에서 ${nodeLabels[target]} 노드로 가는 ${edgeLabels[kind]} 간선입니다.)`,
      entryToSub: "(입구 간선은 메인 에이전트로만 갑니다.)",
      selfDelegation: "(에이전트가 자기 자신에게 위임합니다.)",
      delegationFromSub: "(위임은 메인 에이전트에서만 나갑니다. 서브 에이전트는 위임하지 못합니다.)",
      delegationToMain: "(위임 대상은 서브 에이전트여야 합니다.)",
      attachedToSub: "(메모리·가드레일은 입구나 메인 에이전트에만 붙습니다.)",
      delegationReturn: (node: string): string => `(위임이 돌아오는 노드: ${node})`,
      unreachableMain: "(Chat 트리거 노드를 이 에이전트에 이어 주세요.)",
      unreachableSub: "(메인 에이전트의 위임 포트에서 이 에이전트로 이어 주세요.)",
      agentNameTaken: (node: string): string => `(같은 이름을 먼저 쓴 노드: ${node})`,
      judgeModelMissing: (node: string): string => `(이 그래프에서 찾지 못한 모델 노드: ${node})`,
      judgeModelUnset: "(판정 모델을 아직 선택하지 않았습니다.)",
      providerMissing: (role: ProviderRole, name: string): string => `(찾지 못한 ${providerRole[role]}: ${name})`,
      providerUnset: (role: ProviderRole): string => `(${providerRole[role]} 를 아직 선택하지 않았습니다.)`,
      providerNoKey: (role: ProviderRole, name: string): string => `(키가 없는 ${providerRole[role]}: ${name})`,
      providerName: (role: ProviderRole, name: string): string => `(${providerRole[role]}: ${name})`,
      sourceMissing: (kind: string, id: string): string => `(이 테넌트에서 찾지 못한 ${kind}: ${id})`,
      sourceUnset: (kind: string): string => `(${kind} 를 아직 선택하지 않았습니다.)`,
      sourceState: (kind: string, id: string, state: string): string => `(${kind}: ${id}, 지금 상태: ${state})`,
      clientIdNotAllowed: (clientId: string): string => `(허용 목록에 없는 client_id: ${clientId})`,
      databaseMissing: (id: string): string => `(이 테넌트에서 찾지 못한 Database: ${id})`,
      databaseKind: (name: string, kind: string): string => `(선택한 Database: ${name}, 종류: ${kind})`,
      redisCheckpointer: (state: RedisCheckpointerState, name: string): string => `${redisCheckpointerHint[state]} (Database: ${name})`,
      redisDb: (name: string, db: number): string => `(Database: ${name}, db: ${db})`,
      filesystemOff: (agent: string): string => `(파일 Tool 이 꺼진 에이전트: ${agent})`,
      memoryReasons: (reasons: readonly string[]): string => `(체크포인터와 공유 카운터가 필요한 곳: ${reasons.join(", ")})`,
      reservedToolName: (name: string): string => `(내장 Tool 과 겹치는 이름: ${name})`,
      knowledgeBaseMissing: (id: string): string => `(이 테넌트에서 찾지 못한 Knowledge Base: ${id})`,
      knowledgeBaseDeleting: (name: string): string => `(지워지는 중인 Knowledge Base: ${name})`,
      embeddingProviderMissing: (kb: string, provider: string): string => `(Knowledge Base ${kb}의 임베딩 provider 를 찾지 못했습니다: ${provider})`,
      embeddingKind: (kb: string, provider: string, kind: string): string => `(Knowledge Base ${kb}의 임베딩 provider ${provider}${particle(provider, "은/는")} ${kind}입니다.)`,
      embeddingNoKey: (kb: string, provider: string): string => `(Knowledge Base ${kb}의 임베딩 provider 에 키가 없습니다: ${provider})`,
      knowledgeBase: (name: string): string => `(Knowledge Base: ${name})`,
      knowledgeBaseFailed: (name: string, failed: number): string => `(Knowledge Base: ${name}, 실패 ${failed}건)`,
      toolNameTaken: (name: string, node: string): string => `(겹치는 Tool 이름: ${name}, 먼저 쓴 노드: ${node})`,
    },
    /** Skill 검사(lib/agentSkills.ts 의 skillFindings)의 상세. skill 과 name 은 Skill 의 이름이고 node 는 노드 id 다. */
    skillDetail: {
      skillsAttached: (count: number): string => `(붙인 Skill: ${count}개)`,
      skillsDistinct: (count: number): string => `(서로 다른 Skill: ${count}개)`,
      skillsDistinctBytes: (bytes: number): string => `(서로 다른 Skill 의 합: ${formatMib(bytes)})`,
      skillsExpandedBytes: (bytes: number): string => `(에이전트마다 쓰는 합: ${formatMib(bytes)})`,
      skillRevisions: (skill: string, revision: number, node: string, nodeRevision: number): string => `(Skill ${skill}: 이 에이전트 r${revision}, ${node} r${nodeRevision})`,
      skillMissing: (skill: string, revision: number): string => `(찾지 못한 Skill: ${skill} r${revision})`,
      skillNameTaken: (name: string): string => `(겹치는 이름: ${name})`,
      skill: (name: string): string => `(Skill: ${name})`,
      skillBehind: (name: string, pinned: number, latest: number): string => `(Skill: ${name}, 고정 r${pinned}, 최신 r${latest})`,
    },
  },
};
