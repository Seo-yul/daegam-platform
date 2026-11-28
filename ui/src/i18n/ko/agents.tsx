import type { ReactNode } from "react";

import type { AgentMetrics, MetricsWindow, SleepIneligible, TraceSpan } from "@/api/types";
import type { PiiKind, RunState } from "@/lib/evaluation";
import { EVALUATION_QUEUE_MINUTES } from "@/lib/limits";
import { particle } from "@/lib/particle";
import type { TestChatWait } from "@/pages/agents/TestChatDrawer";

/** 테스트 채팅을 아직 쓸 수 없을 때, 쓸 수 있게 되는 때. */
const testChatOpens = {
  dirty: "저장한 뒤 배포하면",
  draft: "배포 탭에서 배포하면",
  notReady: "Ready 가 되면",
} satisfies Record<TestChatWait, string>;

/** `agent:deploy` 가 있는 사람에게는 저장한 리비전을 미리 보기로 띄울 수 있다고 말한다 (P44b 11절). Ready 를 기다리는 말은 그대로다. */
const testChatPreviewOpens = {
  dirty: "저장하면 여기서 미리 보기로 대화할 수 있습니다.",
  draft: "미리 보기를 시작하거나 배포 탭에서 배포하면 여기서 대화할 수 있습니다.",
} satisfies Partial<Record<TestChatWait, string>>;

/**
 * Agent 의 목록, 상세의 탭과 대화상자, 캔버스 화면의 머리와 안내 (F7b-1). 캔버스 안의 노드와 인스펙터는 canvas 다.
 * 재배포·배포 해제·복제 단추는 shared.detailActions, 배포 단추는 shared.deployDialog, 배포 상태 카드의 말은 detail.deployable 이다.
 */
export const agents = {
  /** 리비전. 목록의 열, 개요의 타일, 캔버스 머리의 리비전 셀렉트가 같이 쓴다. */
  revision: "리비전",
  /** 상세에서 Agent 만 갖는 탭과 그 카드의 제목. 앞의 넷과 Advanced 는 ui.tabs.standard, 승인함은 shared.approvalInbox.title 이다. */
  tabs: { chat: "채팅", sessions: "세션", feedback: "피드백", evaluation: "평가", runs: "자동 실행", metrics: "메트릭", events: "이벤트" },
  /** /agents. */
  list: {
    create: "Agent 만들기",
    /** 상태 줄 끝의 승인 대기 수. */
    pendingApprovals: "승인 대기",
    /** 카드의 태그. */
    toolSources: (count: number): string => `Tool 소스 ${count}`,
    pendingSessions: (count: number): string => `승인 대기 세션 ${count}개`,
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 Agent 가 없습니다.`,
    noDeployed: `배포된 Agent 가 없습니다. "배포된 것만" 을 끄면 아직 올리지 않은 것도 보입니다.`,
    /** link 는 템플릿으로 새 Agent 를 여는 링크이고 글자는 emptyLink 다. */
    empty: (tenant: string, link: ReactNode): ReactNode => (
      <>
        테넌트 {tenant} 에 Agent 가 없습니다.{" "}{link}할 수 있습니다.
      </>
    ),
    emptyLink: "템플릿으로 시작",
    /** 카드 메뉴의 배포 항목 풀이. */
    deployHint: "클러스터를 선택하고 올립니다",
    /** 표의 머리. 리비전 열은 agents.revision 이다. agents 는 그래프의 에이전트 노드 수다. */
    columns: { name: "이름", state: "상태", agents: "에이전트", tools: "Tool 소스", cluster: "클러스터", sessions: "세션", pending: "대기", warnings: "경고" },
    title: "Agents",
    search: "Search agents…",
    provider: "Provider",
  },
  /** /agents/:id 의 머리와 탭 줄, 채팅 탭. */
  detail: {
    back: "Agent 목록으로 돌아가기",
    /** 배포 전에 런타임 탭을 잠근 까닭. */
    liveOnlyTabs: "흐린 탭은 배포한 뒤에 볼 수 있습니다.",
    chatReady: "플랫폼 프록시로 런타임과 SSE 로 대화합니다.",
    chatNotReady: "Ready 가 되면 대화할 수 있습니다.",
    /** 잠든 Agent 의 개요 (P44 10절). time 은 잠든 시각이다. */
    sleepingNote: (time: string): string =>
      `${time}부터 사용하지 않아 파드를 내려 두었습니다. 다음 대화가 오면 다시 띄웁니다. 활성 세션과 보류 승인은 내려 두기 직전의 값입니다.`,
    /** 잠든 Agent 를 깨우는 단추와 그 진행 중 이름. 개요, 런타임 탭, 홈 챗이 같이 쓴다. */
    wake: "깨우기",
    waking: "깨우는 중…",
    /** agent:deploy 가 개요에서 지금 재우는 단추와 그 진행 중 이름. */
    sleep: "지금 내려 두기",
    sleeping: "내려 두는 중…",
    /** 화면 언어를 따르는 복제 이름 예시. */
    clonePlaceholders: { display: "고객지원 에이전트 복제본", name: "support-agent-copy" },
    /** 개요의 열린 미리 보기 카드 (P44b 11절). agent:deploy 가 있는 사람에게만 보인다. name 은 시작한 사람, time 은 끝나는 시각이다. */
    previews: {
      title: "열린 미리 보기",
      row: (name: string, revision: number, time: string): string => `${name} 님의 r${revision} 미리 보기, ${time}에 끝납니다`,
      /** 평가 실행이 띄운 미리 보기 (P45 9절). 끝내면 그 실행이 취소된다. */
      evaluationRow: (name: string, revision: number, time: string): string => `${name} 님의 r${revision} 평가 실행, ${time}에 끝납니다`,
      /** 평가 실행의 미리 보기를 끝내기 전에 묻는 창에 그 줄과 함께 선다. */
      endEvaluation: "미리 보기를 끝내면 이 평가 실행도 취소합니다.",
      end: "끝내기",
    },
    /** 허브 게시 (P46 6.1절). 단추, 대화상자의 첫 줄, Tool 설명 칸, 호출을 믿는 선택, 답이 가는 곳의 안내다. 게시와 해제 단추의 말은 detail.publishSection 이다. */
    publish: {
      action: "허브에 게시",
      hint: "같은 테넌트의 다른 Agent 가 이 Agent 를 Tool 로 부를 수 있게 허브에 게시합니다. 호출마다 부른 사람의 권한으로 이 Agent 를 실행하고 이 Agent 의 예산과 자동 실행 한도를 사용합니다.",
      description: { label: "Tool 설명", hint: "부르는 Agent 의 모델이 이 설명을 읽고 언제 부를지 정합니다. 이 Agent 가 하는 일과 받을 요청을 작성해 주세요." },
      trusted: { label: "호출 내용을 사람이 쓴 것으로 다루기", hint: "끄면 호출마다 외부 내용으로 다루므로 이 Agent 의 쓰기 Tool 은 승인을 기다립니다." },
      answerNotice: "답은 부른 Agent 의 모델과 대화 기록으로 넘어갑니다. 그 Agent 는 다른 LLM provider 를 사용할 수 있습니다.",
    },
  },
  /** 잠든 Agent 의 세션 탭, 홈 챗 대화 목록, 기억 서랍. 런타임을 읽지 않고 이 줄과 깨우기 단추를 보인다 (P44 8절). */
  sleepingList: "이 Agent 는 사용하지 않아 파드를 내려 두었습니다. 깨우면 목록을 불러옵니다.",
  /** 개요 탭. 빈 카드의 제목은 detail.deployable.notDeployed, 배포 탭으로 가는 단추는 detail.connectCard.toDeploy 다. */
  overview: {
    saved: (revision: number): string =>
      `그래프 r${revision}${particle(String(revision), "이/가")} 저장돼 있습니다. 배포하면 런타임이 뜨고 여기에 세션과 승인 대기 수가 나옵니다.`,
    chatAddress: "플랫폼 채팅 주소",
    chatAddressHint: "플랫폼이 프록시로 런타임에 붙습니다. 채팅 탭이 이 주소를 사용합니다.",
    /** groups 는 ", " 로 이은 허용 그룹이고 비었으면 괄호를 뺀다. */
    authExposed: (groups: string): string => `외부 주소는 런타임이 OIDC 액세스 토큰을 직접 검증합니다${groups ? ` (허용 그룹 ${groups})` : ""}.`,
    authInternal: "플랫폼을 거쳐서만 부를 수 있습니다. 외부에는 열지 않았습니다.",
    /** 준비 상태 카드 안의 타일. 리비전 타일은 agents.revision 이다. */
    tiles: { sessions: "활성 세션", pending: "보류 승인", warnings: "경고" },
    /** 정의 요약 카드의 줄. 바인딩 줄은 mcpServers.bindings 다. */
    summary: { models: "모델", tools: "Tool 소스", memory: "기억·지식" },
    /** 모델 노드가 provider 도 모델도 정하지 않았을 때. */
    unsetModel: "(미설정)",
    /** 요약 줄의 조각. 화면이 ", " 로 잇는다. */
    mcpServers: (count: number): string => `MCP Server ${count}개`,
    gateways: (count: number): string => `Gateway ${count}개`,
    checkpointerAndMemory: "체크포인터, 장기 기억",
    checkpointer: "체크포인터",
    knowledgeBases: (count: number): string => `Knowledge Base ${count}개`,
    /** 정의 요약 카드의 LLM 경로 줄 (P38 6절, 11절). 직접 경로인 이유는 reasons 가 코드로 고른다. */
    llmRoute: {
      label: "LLM 경로",
      gateway: "LLM 게이트웨이",
      direct: "provider 직접 호출",
      reasons: {
        gateway_off: "게이트웨이가 꺼져 있습니다.",
        gateway_not_ready: "게이트웨이에 반영된 설정이 아직 없습니다.",
        no_signer: "플랫폼에 서명 키가 없어 토큰을 낼 수 없습니다.",
        remote_cluster: "home 클러스터가 아닌 클러스터에 배포돼 있습니다.",
        stub_only: "stub provider 만 사용합니다.",
        provider_not_ready: "사용하는 provider 가운데 게이트웨이에 반영되지 않은 것이 있습니다.",
        runtime_features_unknown: "런타임 이미지가 아직 기능을 알리지 않았습니다. 파드가 뜬 뒤 다음 재적용에서 옮깁니다.",
        runtime_image_too_old: "런타임 이미지가 LLM 게이트웨이를 지원하지 않습니다. 이미지를 올려 다시 배포해 주세요.",
      } satisfies Record<string, string>,
      rotate: "LLM 토큰 교체",
      immediate: "옛 토큰을 곧바로 회수",
      rotateHint:
        "새 토큰으로 파드를 다시 띄우고, 옛 토큰은 옛 파드가 모두 내려간 뒤에 회수합니다. 토큰 유출이 의심되면 곧바로 회수를 선택해 주세요. 그러면 새 파드가 준비될 때까지 옛 파드는 LLM 을 부르지 못합니다.",
      rotated: "새 토큰으로 파드를 다시 띄우고 있습니다.",
      rotatedImmediate: "새 토큰으로 파드를 다시 띄우고 옛 토큰을 곧바로 회수했습니다.",
    },
  },
  /** 배포 탭. 배포된 리비전 줄은 detail.deployable.deployedRevision, 외부 노출 줄은 shared.exposureSection.enable 이다. */
  deployment: {
    emptyBody: (revision: number): string => `클러스터와 replicas, 외부 노출을 정하면 그래프 r${revision}${particle(`r${revision}`, "으로/로")} 런타임을 올립니다.`,
    hostUnset: "(host 미정)",
    off: "꺼짐",
    hint: "replicas·리소스·노출은 재배포 대화상자에서 함께 고칩니다. 그래프는 정의 탭에서 고치고 재배포하면 적용됩니다.",
    /** 리비전 표 (P45 6절). 카드 제목은 agents.revision 이다. */
    revisions: { current: "현재", deployed: "배포됨" },
    /** 되돌리기 (P45 6절). n 은 되돌릴 리비전, next 는 새로 만들 리비전이다. */
    rollback: {
      action: "이 리비전으로 되돌리기",
      title: (revision: number): string => `r${revision}${particle(`r${revision}`, "으로/로")} 되돌리기`,
      body: (revision: number, next: number): string =>
        `r${revision}의 정의를 새 리비전 r${next}${particle(`r${next}`, "으로/로")} 저장하고 배포합니다. 런타임 이미지와 provider 설정, Tool 목록은 현재 상태를 따르므로 r${revision}${particle(`r${revision}`, "이/가")} 배포됐던 때와 다르게 동작할 수 있습니다.`,
      /** 그 내용이 마지막으로 배포된 기록 줄. time 은 그때의 시각, image 는 그때의 런타임 이미지다. */
      lastDeployed: (time: string, image: string): string => `이 정의는 ${time}에 ${image} 이미지로 마지막으로 배포됐습니다.`,
      submit: "되돌리기",
      submitting: "되돌리는 중…",
      /** 422 invalid_graph. 되돌릴 정의가 지금 플랫폼에서 검증되지 않아 아무것도 바꾸지 않았다. */
      invalidGraph: (revision: number): string =>
        `r${revision}의 정의가 지금 플랫폼의 검증을 통과하지 못해 되돌리지 않았습니다. 정의 탭에서 r${revision}${particle(`r${revision}`, "을/를")} 불러와 고친 뒤 저장하고 배포해 주세요.`,
    },
    /** 배포 기록 카드 (P45 6절). 리비전 열은 agents.revision 이다. */
    history: {
      title: "배포 기록",
      empty: "배포 기록이 없습니다.",
      columns: { time: "시각", action: "동작", image: "이미지", by: "배포한 사람", outcome: "결과", evaluation: "평가" },
      actions: {
        deploy: "배포",
        redeploy: "재배포",
        rollback: (from: number): string => `r${from}${particle(`r${from}`, "으로/로")} 되돌림`,
      },
      /** 배포가 끝난 꼴. 모르는 값은 그대로 보인다. */
      outcomes: { pending: "진행 중", deployed: "배포됨", failed: "실패" } as Record<string, string>,
      /** 게이트가 받아들인 배포. rate 는 그 실행의 통과율(%)이고, 그 실행을 읽지 못하면 passedUnknown 이다. */
      passed: (rate: number): string => `평가를 통과함 (${rate}%)`,
      passedUnknown: "평가를 통과함",
      skipped: (reason: string): string => `평가 없이 배포함: ${reason}`,
    },
  },
  /** 노출을 켠 Agent 의 외부 호출 카드. 토큰 발급 화면의 링크 글자는 account.tokens.title 이다. */
  external: {
    title: "외부 호출",
    /** audience 는 플랫폼의 mcp_audience 이고 없으면 괄호를 뺀다. */
    lead: (audience: string | null): string =>
      `플랫폼을 거치지 않고 Ingress 로 런타임을 직접 부릅니다. 인증은 OIDC 액세스 토큰${audience ? ` (aud: ${audience})` : ""}입니다.`,
    noUrl: "(주소가 잡히면 status.external_url 에 나옵니다)",
    columns: { method: "메서드", path: "경로", description: "설명" },
    /** 복사 단추가 문장 가운데 넣는 명사. */
    curlCopy: "curl 예시",
    /** curl 예시가 보내는 첫 메시지. 사람이 복사해 가는 글이라 화면 언어를 따른다. */
    curlMessage: "안녕하세요",
    /** env 는 토큰을 넣을 env 이름이고 link 는 토큰 발급 화면으로 가는 링크다. */
    patNote: (env: string, link: ReactNode): ReactNode => (
      <>
        {env} 에 넣을 개인 MCP 토큰은{" "}{link}에서 발급해 주세요.
      </>
    ),
    eventSource: "브라우저에서는 EventSource 를 쓸 수 없고 fetch 로 스트림을 읽습니다. EventSource 는 POST 와 Authorization 헤더를 보낼 수 없기 때문입니다.",
    noOrigins: "없음. 브라우저 origin 이 없으므로 CORS 헤더를 내지 않습니다.",
    baseUrl: "base url",
    allowedGroups: "allowed groups",
    corsOrigins: "cors origins",
  },
  /** 외부 경로 표의 설명 (externalEndpoints.ts). 값은 lib 의 상한을 받는다. */
  endpoints: {
    chat: "한 턴을 보내고 SSE 로 받습니다. 본문은 {session_id?, message} 이고 세션 소유자는 토큰의 sub 입니다.",
    chatAttachments:
      '본문에 attachments 를 더해 문서 글({kind: "text", name, format, text})과 이미지({kind: "image", name, media_type, data})를 붙입니다. data 는 base64 이고 개수·크기 상한을 넘으면 413·422 입니다. done 의 attachments_received 가 받아 저장한 수입니다.',
    sessions: "토큰의 sub 가 소유한 세션 목록입니다. 다른 사용자의 세션은 보이지 않습니다.",
    /** maxChars 는 세션 제목의 상한이다. */
    searchSessions: (maxChars: number): string => `제목에 q 가 들어간 세션만 돌려줍니다. q 는 1~${maxChars}자이고 대소문자를 가리지 않습니다.`,
    session: "세션 상세입니다. 소유자만 볼 수 있습니다.",
    renameSession: (maxChars: number): string =>
      `세션 이름을 바꿉니다. 본문은 {title} 이고 공백과 제어 문자를 뺀 1~${maxChars}자입니다. 소유자만 부를 수 있습니다.`,
    deleteSession: "세션을 지웁니다. 대화와 체크포인트가 사라지고 장기 기억은 남습니다. 답변을 만드는 중이면 409 입니다. 소유자만 부를 수 있습니다.",
    pending: "보류 중인 승인 요청입니다. 소유자만 볼 수 있습니다.",
    decisions: "승인 결정으로 턴을 재개합니다. 본문은 {decisions: [...]} 이고 응답은 /chat 과 같은 SSE 입니다. 소유자만 부를 수 있습니다.",
    regenerate: "마지막 답변을 다시 생성합니다. 본문 {message?} 를 주면 마지막 질문을 그 글로 바꿔 보내고, 응답은 /chat 과 같은 SSE 입니다. 소유자만 부를 수 있습니다.",
    files: "그 대화에 업로드한 파일(origin: upload)과 에이전트가 파일 Tool 로 쓴 파일(origin: agent)의 목록입니다. 소유자만 볼 수 있습니다.",
    /** maxSize 는 "1.0 MB" 처럼 적은 파일 상한이다. */
    file: (maxSize: string): string =>
      `파일 하나를 다운로드합니다. path 는 목록의 경로에서 앞의 / 를 뗀 것이고 ${maxSize}를 넘는 파일은 413 입니다. 응답은 언제나 Content-Disposition: attachment 입니다. 소유자만 받을 수 있습니다.`,
    memories: "토큰 sub 의 장기 기억 파일 목록입니다. Agent 가 그 사람과 대화하며 남긴 것이고, 장기 기억을 사용하지 않는 Agent 는 404 입니다.",
    /** root 는 기억 파일의 뿌리 경로이고 maxSize 는 적은 파일 상한이다. */
    memoryFile: (root: string, maxSize: string): string => `기억 파일 하나의 내용입니다. key 는 ${root} 아래의 경로이고 ${maxSize}를 넘는 파일은 413 입니다.`,
    deleteMemoryFile: "기억 파일 하나를 지웁니다. 같은 경로로 시작하는 다른 파일은 남습니다.",
  },
  /** Agent 배포 대화상자. 클러스터 칸의 풀이와 단추는 shared.deployDialog, 클러스터의 빈 선택지는 shared.tenantDefault 다. */
  deployDialog: {
    title: "Agent 배포",
    /** name 은 꾸민 표시 이름이다. */
    body: (name: ReactNode, revision: number): ReactNode => (
      <>
        {name}의 정의 r{revision}{particle(`r${revision}`, "으로/로")} 런타임을 올립니다. provider 키 Secret 이 대상 네임스페이스로 복사됩니다.
      </>
    ),
    replicasHint: "2 이상이면 memory 노드가 있어야 세션·카운터가 파드 사이에 공유됩니다.",
    /** name 은 꾸민 클러스터 리소스 이름이고 namespace 는 꾸민 네임스페이스이며 정하지 않았으면 null 이다. */
    preview: (name: ReactNode, namespace: ReactNode): ReactNode => (
      <>
        Deployment·Service 이름: {name}
        {namespace}
      </>
    ),
    nameConflict: "이 네임스페이스에 같은 이름이 이미 있습니다. 다른 네임스페이스를 선택하거나 Agent 이름을 바꿔 주세요.",
    /** 배포 대화상자의 칸 이름. scaleToZero 는 API 칸 이름이라 두 언어 모두 영어다. */
    labels: {
      replicas: "Replicas",
      scaleToZero: "Scale to zero",
    },
    /** 쓰지 않는 Agent 를 0 으로 줄이는 스위치의 풀이 (P44 6절). minutes 는 설치의 쉬는 창(분)이다. */
    scaleToZeroHint: (minutes: number): string =>
      `켜면 ${minutes}분 동안 대화와 자동 실행이 없을 때 파드를 0개로 줄이고, 다음 대화나 자동 실행이 오면 다시 띄웁니다. 다시 뜨는 동안에는 첫 답변이 늦어집니다.`,
    /** 설치의 쉬는 창이 0 이라 스스로 잠들지 않을 때의 풀이. */
    scaleToZeroHintManual: `켜면 "지금 내려 두기" 로 파드를 0개로 줄일 수 있고, 다음 대화나 자동 실행이 오면 다시 띄웁니다. 이 배포본에는 자동으로 줄이는 기준 시간이 없어 직접 내려 둘 때만 줄입니다.`,
    /** 상태가 없어(첫 배포) 쉬는 창을 모를 때의 풀이. */
    scaleToZeroHintUnknown: "켜면 플랫폼이 정한 시간 동안 대화와 자동 실행이 없을 때 파드를 0개로 줄이고, 다음 대화나 자동 실행이 오면 다시 띄웁니다. 다시 뜨는 동안에는 첫 답변이 늦어집니다.",
    /** 스위치가 켜져 있어도 0 으로 줄이지 않는 까닭. agent_off 는 스위치 자체라 문장이 없다. */
    scaleToZeroOff: {
      no_memory: "memory 노드가 없는 Agent 는 파드를 내리면 대화와 승인 대기가 사라지므로 0개로 줄이지 않습니다.",
      exposed: "외부에 노출한 Agent 는 Ingress 가 파드로 바로 요청을 보내므로 0개로 줄이지 않습니다.",
      runtime_image: "이 Agent 의 런타임 이미지는 0개로 줄이기를 지원하지 않습니다. 최신 이미지로 재배포해 주세요.",
      replicas_override: "Advanced 설정이 레플리카 수를 정하고 있어 0개로 줄이지 않습니다. 0개로 줄이려면 Advanced 탭에서 spec.replicas 를 지워 주세요.",
      setting_off: "플랫폼 설정에서 0개로 줄이기를 꺼 두었습니다.",
    } satisfies Record<Exclude<SleepIneligible, "agent_off">, string>,
  },
  /** 재배포 대화상자. 제목과 단추는 shared.detailActions, 외부 노출 제목은 shared.exposureSection.enable 이다. */
  redeploy: {
    body: (revision: number): string => `최신 정의 r${revision}${particle(`r${revision}`, "으로/로")} 올리고 provider Secret 을 다시 복사합니다. 보류 중인 세션은 새 리비전에서 이어집니다.`,
    exposureChanged: "노출 설정이 바뀌어 Ingress 를 다시 만듭니다.",
    exposureKept: "노출 설정은 그대로 둡니다.",
    /** 재배포 단추가 꺼진 까닭. */
    hostRequired: "Host 는 필수입니다. 이 Agent 를 부를 주소를 적어야 재배포할 수 있습니다.",
    hostInvalid: "Host 가 규칙에 맞지 않습니다. 소문자 영숫자와 하이픈, 점만 쓸 수 있습니다.",
    exposureInvalid: "외부 노출 설정에 고칠 것이 남아 있어 재배포할 수 없습니다.",
  },
  /** 배포 해제 확인. 제목과 단추는 shared.detailActions 다. */
  undeployBody:
    "파드와 Deployment·Service, 발급한 토큰, 복사된 provider Secret 을 내립니다. 진행 중인 세션과 보류 승인은 사라집니다. 그래프와 리비전 이력은 남아 다시 배포할 수 있습니다.",
  /** 메트릭 탭. */
  metrics: {
    /** 기간 고르개의 이름은 detail.windowSelect.label 이다. 턴·호출·비용의 수가 이 기간 안의 것이다. */
    window: { "1h": "최근 1시간", "24h": "최근 24시간" } satisfies Record<MetricsWindow, string>,
    /** 턴 결과 이름. 키는 서버의 칸 이름이고 보이는 차례는 화면이 정한다. 멈춘 턴은 "중지" 다. */
    turnResult: {
      done: "완료",
      pending: "승인 대기",
      turn_failed: "실패",
      turn_timeout: "시간 초과",
      quota_exhausted: "할당량 소진",
      budget_exceeded: "예산 초과",
      cancelled: "중지",
    } satisfies Record<keyof AgentMetrics["turns"], string>,
    sessions: "세션 (활성/보류/닫힘)",
    turns: "완료한 턴",
    tokens: "토큰 (입력+출력)",
    latency: "지연 p95 (ms)",
    cost: "비용",
    unpriced: (count: number): string => `가격 규칙이 없는 호출 ${count}건은 비용에 포함하지 않았습니다.`,
    turnResults: "턴 결과",
    errorRate: "오류율",
    latencyStats: "지연 평균 / p50 / 최대",
    latencyValues: (avg: string, p50: string, max: string): string => `${avg} / ${p50} / ${max} ms`,
    rejected: "턴 시작 전 거부",
    noRejected: "거부한 요청이 없습니다.",
    modelTitle: "모델 호출",
    retries: "재시도",
    fallbacks: "예비 모델로 전환",
    model: "모델",
    /** 모델 호출과 Tool 실행 카드의 표와 합계는 shared.callsCard 다. */
    toolTitle: "Tool 실행",
    toolLoading: "Tool 상태",
    boundTools: "묶인 Tool",
    /** count 는 화면 언어로 적은 수이고 모르면 "-" 다. */
    toolCount: (count: string): string => `${count}개`,
    loadedAt: "불러온 시각",
    lastLoadError: (error: string): string => `마지막으로 불러오지 못한 오류: ${error}`,
    dailyBudget: "일당 토큰 예산",
    usedOfLimit: "쓴 양 / 한도",
    budgetWindow: "집계 기간",
    noDailyBudget: "일당 예산을 걸지 않았습니다.",
    costDaily: "일별 비용 (최근 30일)",
    telemetryTitle: "이벤트 수집",
    telemetryMode: { push: "Agent 가 직접 보냄", log: "파드 로그에서 읽음", none: "받은 이벤트 없음" } satisfies Record<AgentMetrics["telemetry"]["mode"], string>,
    redeployToPush: "이 Agent 를 다시 배포하면 파드 로그를 거치지 않고 이벤트를 바로 보냅니다.",
    podColumns: { pod: "파드", lastAt: "마지막 수신 시각", received: "받은 수", refused: "거부된 수", dropped: "버린 수" },
    noPods: "이 기간에 이벤트를 보낸 파드가 없습니다.",
    /** 파드의 큐가 넘쳐 버린 이벤트 수. 값은 화면 언어로 적은 수다. */
    dropped: (cost: string, other: string): string => `큐가 가득 차서 버린 이벤트는 비용 이벤트 ${cost}건, 그 밖의 이벤트 ${other}건입니다.`,
    /** reason 은 오류 코드의 문장이다. */
    runtimeError: (reason: string): string => `런타임에 연결할 수 없어 세션과 Tool 상태를 표시할 수 없습니다. ${reason}`,
  },
  /** 턴의 트레이스 창과 그 단계 그림. 채팅의 턴 마감 줄과 세션 탭의 턴 표가 연다. */
  trace: {
    title: "트레이스",
    none: "이 턴에는 트레이스가 없습니다.",
    run: (n: number): string => `실행 ${n}`,
    resumed: "승인 뒤 재개",
    /** 같은 턴의 두 실행 사이. gap 은 formatDuration(exact) 로 적은 시간이다. */
    approvalWait: (gap: string): string => `승인 대기 ${gap}`,
    /** turn_end 가 아직 오지 않은 실행의 결과. 나머지 결과 이름은 metrics.turnResult 다. */
    running: "실행 중",
    /** 턴이 막 끝나 트레이스가 아직 저장소에 없다. 창이 몇 번 다시 읽는다. */
    collecting: "트레이스를 모으는 중입니다. 잠시 뒤에 다시 표시합니다.",
    timeline: "실행 단계",
    kind: { run: "실행", model: "모델", tool: "Tool", subagent: "서브 에이전트", embeddings: "임베딩" } satisfies Record<TraceSpan["kind"], string>,
    flag: { error: "오류", retry: "재시도", fallback: "예비 모델", cache: "캐시 적중", gateway: "LLM 게이트웨이" },
    cost: (usd: string): string => `비용 ${usd}`,
    /** 턴이 Tool 로 부른 다른 Agent 의 실행들 (P46 6.5절). task 는 답을 기다리지 않고 맡긴 작업의 실행이다. */
    calls: { title: "Agent 호출", task: "작업", tokens: (count: string): string => `토큰 ${count}` },
    attributes: "속성",
    body: "본문",
    points: "그 밖의 이벤트",
    /** 상한에 걸린 트레이스. spans 는 단계 수 상한이고(limit 은 화면 언어로 적은 수), events 는 기록 수 상한이다. */
    truncated: {
      spans: (limit: string): string => `단계가 ${limit}개를 넘어 처음 ${limit}개만 표시합니다.`,
      events: "기록이 많아 이 실행의 앞부분만 표시합니다.",
    },
  },
  /** 이벤트 탭. 카드 제목은 agents.tabs.events 다. audit:read 가 없으면 탭이 없다. */
  events: {
    hint: "런타임이 보낸 이벤트, 수집기가 파드 로그에서 읽은 이벤트, 플랫폼이 남긴 답변 피드백 이벤트입니다. 본문은 record_bodies 가 켜져 있을 때만 남고, 이 Agent 의 승인 권한이 있는 사람만 볼 수 있습니다.",
    columns: { time: "시각", event: "이벤트", session: "세션", turn: "턴", status: "상태", subject: "주체", tokens: "토큰", body: "본문" },
    empty: "이벤트가 없습니다.",
    filters: { event: "이벤트", session: "세션 ID", trace: "트레이스 ID", all: "모든 이벤트", apply: "적용" },
    /** 이벤트 이름. 모르는 이름은 그대로 보인다. */
    names: {
      turn_start: "턴 시작",
      turn_end: "턴 종료",
      model_call: "모델 호출",
      tool_exec: "Tool 실행",
      model_retry: "모델 재시도",
      model_fallback: "예비 모델로 전환",
      interrupt: "승인 요청",
      decision: "승인 결정",
      guardrail: "가드레일",
      budget: "예산 초과",
      rate_limited: "분당 한도 초과",
      web_guard: "웹 Tool 호출 거절",
      injection_check: "판정 모델이 검사하지 못한 Tool 결과",
      tool_limit: "턴당 최대 Tool 호출 수 도달",
      rejected: "턴 시작 전 거부",
      feedback: "답변 피드백",
      session_deleted: "세션 삭제",
      runtime_start: "런타임 시작",
      tools_refreshed: "Tool 목록 새로 고침",
      session_expired: "세션 만료",
      memory_deleted: "장기 기억 삭제",
    },
    showBody: "본문 보기",
  },
  /** 세션 탭. 카드 제목은 agents.tabs.sessions, 상태 필터의 전체는 detail.stateCountLine.total 이다. */
  sessions: {
    search: "세션 검색",
    statusFilter: "상태 필터",
    columns: { session: "세션", title: "제목", user: "사용자", status: "상태", turns: "턴", tokens: "토큰", lastTurn: "마지막 턴" },
    untitled: "제목 없음",
    noMatch: "제목에 검색어가 들어간 세션이 없습니다.",
    empty: "세션이 없습니다.",
    detailTitle: "세션 상세",
    pick: "세션을 선택하면 대화와 토큰, 보류 상태가 보입니다.",
    /** 세션 상세 머리의 배지. */
    turns: (count: number): string => `${count}턴`,
    input: (count: number): string => `입력 ${count}`,
    output: (count: number): string => `출력 ${count}`,
    noMessages: "남은 대화 기록이 없습니다.",
    decideInInbox: "결정은 승인함 탭에서 합니다.",
    states: { active: "active", pending: "pending", closed: "closed" },
    turnsTitle: "턴 기록",
    turnColumns: { turn: "턴", runs: "실행 수", status: "결과", start: "시작 시각", tokens: "토큰", cost: "비용" },
    noTurns: "이 세션의 턴 기록이 없습니다.",
    regenerated: "다시 생성됨",
    openTrace: "트레이스 보기",
  },
  /** 피드백 탭. 카드 제목은 agents.tabs.feedback, 평가와 이유의 이름은 chat.feedback, 내보내기 단추는 shared.export 다. */
  feedback: {
    forbidden: "피드백은 이 Agent 의 승인 권한이 있는 사람만 볼 수 있습니다.",
    /** 머리의 지난 며칠 평가 수. up 과 down 은 평가 이름이다. */
    recent: (days: number, up: string, upCount: number, down: string, downCount: number): string =>
      `지난 ${days}일 · ${up} ${upCount} · ${down} ${downCount}`,
    columns: { time: "시각", user: "사용자", rating: "평가", tags: "이유", comment: "의견" },
    noMatch: "조건에 맞는 피드백이 없습니다.",
    empty: "아직 피드백이 없습니다.",
    loadingMore: "불러오는 중…",
    more: "더 보기",
    ratingFilter: "평가 필터",
    allRatings: "평가 전체",
    tagFilter: "이유 필터",
    allTags: "이유 전체",
    exportHint: (days: number): string => `지난 ${days}일의 피드백을 NDJSON 파일로 다운로드합니다. 평가 필터만 적용되고 이유 필터는 적용되지 않습니다.`,
    exporting: "내보내는 중…",
  },
  /**
   * 평가 탭과 배포 전 평가 (P45 7절~12절). 승인 권한(agent:approve)이 있는 사람만 탭을 본다. 저장·만들기·추가·제거 단추는 shared,
   * 취소·삭제는 common, 리비전은 agents.revision 이다.
   */
  evaluation: {
    setsEmpty: "평가 세트가 없습니다. 세트를 만들고 사례를 추가해 주세요.",
    /** 게이트가 채점 기준을 세지 않는 동안(DAEGAM_EVALUATION_GATE_CRITERIA 가 꺼져 있다) 기준의 판단 옆에 선다. */
    criteriaAdvisory: "채점 기준의 판단은 참고용입니다. 배포 전 평가는 확인 항목만 봅니다.",
    sets: {
      title: "평가 세트",
      create: "세트 만들기",
      edit: "세트 편집",
      caseCount: (count: number): string => `사례 ${count}건`,
      repeats: (count: number): string => `반복 ${count}회`,
      noGrader: "채점 모델 없음",
    },
    /** 세트 편집 창. max 는 사례 수 × 반복 수의 상한이다. */
    set: {
      createTitle: "평가 세트 만들기",
      editTitle: "평가 세트 편집",
      name: "이름",
      grader: "채점 모델",
      graderHint: "이 Agent 의 모델 노드가 사용하는 provider 의 모델만 선택할 수 있습니다. 채점 기준이 있는 사례가 하나라도 있으면 채점 모델을 선택해야 합니다.",
      repeats: "반복 횟수",
      repeatsHint: (max: number): string => `사례마다 이 횟수만큼 실행하고 절반보다 많이 통과해야 통과로 봅니다. 사례 수와 반복 횟수를 곱한 값은 ${max} 이하여야 합니다.`,
      deleteTitle: "평가 세트 삭제",
      deleteBody: (name: string): string => `${name} 세트와 그 사례를 모두 지웁니다. 지운 세트는 되살릴 수 없습니다.`,
    },
    /** 사례의 목록과 편집기. */
    case: {
      messages: "질문",
      criteria: "채점 기준",
      checks: "확인 항목",
      criteriaHint: "답변이 지켜야 할 것을 한 문장씩 적어 주세요. 채점 모델이 문장마다 통과 여부를 판단합니다.",
      toolResults: "쓰기 Tool 의 가상 결과",
      toolResultsHint: "평가 실행은 쓰기 Tool 을 실제로 부르지 않습니다. 여기 적은 결과를 그 Tool 의 답으로 돌려주고, 결과를 적지 않은 Tool 의 호출은 거부합니다.",
      piiWarning: "이 사례에 이메일 주소, 전화번호, 주민등록번호나 카드 번호로 보이는 내용이 있습니다. 실제 개인정보가 아니면 아래 확인란을 선택한 뒤 저장해 주세요.",
      piiAcknowledge: "개인정보가 아님을 확인했습니다",
      changedDuringRun: "실행하는 동안 바뀌거나 지워져서 실행하지 않았고 결과에도 포함하지 않았습니다.",
      add: "사례 추가",
      addTitle: "사례 추가",
      editTitle: "사례 편집",
      edit: "편집",
      empty: "사례가 없습니다. 사례를 추가해 주세요.",
      /** max 는 사례 하나의 질문 수 상한이다. */
      messagesHint: (max: number): string => `새 대화 하나에서 위에서부터 차례로 보냅니다. 질문은 ${max}개까지입니다.`,
      reference: "참조 답변",
      referenceGood: "좋은 답변의 예입니다",
      expectDone: "답변이 끝까지 나왔는지 확인합니다",
      /** 결과의 확인 항목 줄에서 `done` 확인의 이름이다. */
      doneKind: "답변이 끝까지 나왔는지",
      checkKind: "종류",
      checkKinds: {
        tool_called: "Tool 을 불렀는지",
        tool_not_called: "Tool 을 부르지 않았는지",
        answer_contains: "답변에 들어 있는지",
        answer_not_contains: "답변에 들어 있지 않은지",
        answer_matches: "답변이 정규식에 맞는지",
      },
      toolName: "Tool 이름",
      checkArgs: "인자에 들어 있어야 할 값 (JSON)",
      checkText: "문구",
      toolResult: "돌려줄 결과",
      /** 인자 칸이 JSON 객체가 아니다. n 은 1부터 센 확인 항목의 차례다. */
      argsInvalid: (n: number): string => `${n}번째 확인 항목의 인자는 JSON 객체여야 합니다. 고친 뒤 다시 저장해 주세요.`,
      columns: { question: "질문", origin: "출처", updatedBy: "마지막으로 바꾼 사람" },
      /** 개인정보 검사가 짚은 칸의 이름. n 은 1부터 센 차례이고 name 은 가상 결과의 Tool 이름이다. */
      places: {
        message: (n: number): string => `질문 ${n}`,
        reference: "참조 답변",
        criterion: (n: number): string => `채점 기준 ${n}`,
        checkTool: (n: number): string => `확인 항목 ${n}의 Tool 이름`,
        checkText: (n: number): string => `확인 항목 ${n}의 문구`,
        checkArgs: (n: number): string => `확인 항목 ${n}의 인자`,
        toolResultName: (n: number): string => `가상 결과의 Tool 이름 ${n}`,
        toolResult: (name: string): string => `${name} 의 가상 결과`,
      },
      copied: "대화에서 복사",
      handWritten: "직접 작성",
      deleteTitle: "사례 삭제",
      deleteBody: "이 사례와 그 실행 결과를 지웁니다. 지운 사례는 되살릴 수 없습니다.",
      /** 422 evaluation_case_pii 가 짚은 개인정보의 종류. 칸의 자리(messages[0] 등)와 함께 보인다. */
      piiKinds: {
        email: "이메일 주소",
        phone: "전화번호",
        id_number: "주민등록번호나 외국인등록번호",
        card_number: "카드 번호",
        masked: "이 Agent 의 가드레일에 적은 PII 정규식에 맞는 내용",
      } satisfies Record<PiiKind, string>,
    },
    /** 대화에서 사례로 복사 (P45 8절). 피드백 탭과 세션 탭이 연다. */
    copy: {
      action: "평가 사례로 복사",
      hint: "좋아요나 아쉬워요를 받은 질문과 그 답변만 사례로 복사합니다. 이메일 주소와 전화번호 같은 개인정보는 [phone] 처럼 바꿔 두었습니다. 저장하기 전에 이름과 사번처럼 바뀌지 않은 개인정보를 지워 주세요. 대화한 사람이 동의를 철회하면 이 사례와 그 평가 결과도 지워집니다.",
      set: "평가 세트",
      noSets: "평가 세트가 없습니다. 평가 탭에서 세트를 먼저 만들어 주세요.",
      /** summary 는 replacedCount 를 ", " 로 이은 것이다. */
      replaced: (summary: string): string => `${summary}${particle(summary, "을/를")} 바꿔 두었습니다.`,
      /** 복사의 404 turn_not_found. 다시 생성으로 걷혔거나 대화 기록의 보관 기간이 지난 턴이다. */
      turnGone: "이 답변은 다시 생성됐거나 대화 기록의 보관 기간이 지나 사례로 복사할 수 없습니다. 사례를 직접 작성해 주세요.",
      replacedCount: (kind: string, count: number): string => `${kind} ${count}건`,
    },
    /** 실행 (P45 9절). */
    run: {
      title: "평가 실행 기록",
      start: (revision: number): string => `r${revision} 평가 실행`,
      note: "평가 실행은 미리 보기 파드 하나를 띄워 사례마다 새 대화를 엽니다. 읽기 Tool 은 실행한 사람의 권한으로 실제 시스템을 읽고, 쓰기 Tool 은 부르지 않습니다. 답변과 Tool 결과는 실행한 사람만 볼 수 있고, 다른 사람은 통과 여부만 볼 수 있습니다.",
      reviewChanged: (count: number): string =>
        `내가 마지막으로 실행한 뒤에 다른 사람이 작성하거나 바꾼 사례가 ${count}건 있습니다. 사례는 내 권한으로 실행되므로 내용을 확인한 뒤 실행해 주세요.`,
      contentHidden: (name: string): string => `답변과 Tool 결과는 이 평가를 실행한 ${name} 님만 볼 수 있습니다. 그 사람의 권한으로 읽은 내용이기 때문입니다.`,
      headerMode: "이 플랫폼은 Tool 을 Agent 의 권한으로 부르므로 평가 실행도 Agent 의 권한으로 실제 시스템을 읽습니다.",
      summary: (total: number, passed: number, rate: number, worse: number): string =>
        `사례 ${total}건 가운데 ${passed}건 통과 (${rate}%), 배포된 리비전보다 나빠진 사례 ${worse}건`,
      noBaseline: (count: number): string => `배포된 리비전에서 평가한 결과가 없는 사례 ${count}건은 나빠졌는지 판단하지 못했습니다.`,
      /** 실행 목록의 결과 칸. 나빠진 사례는 결과 창이 기준선과 견줘 보인다. */
      passRate: (total: number, passed: number, rate: number): string => `사례 ${total}건 가운데 ${passed}건 통과 (${rate}%)`,
      states: {
        queued: "대기 중",
        starting: "준비 중",
        running: "실행 중",
        grading: "채점 중",
        finished: "완료",
        error: "오류",
        cancelled: "취소됨",
      } satisfies Record<RunState, string>,
      /** 실행이 오류나 취소로 끝난 까닭 (P45 13절). 모르는 까닭은 오류 사전에서 찾는다. */
      errors: {
        evaluation_tools_unknown:
          "Tool 이름을 알 수 없는 Tool 소스가 있어 평가를 실행하지 않았습니다. 이름을 모르면 쓰기 Tool 을 막을 수 없기 때문입니다. 그 소스에서 사용할 Tool 을 선택한 뒤 다시 실행해 주세요.",
        acts_as_unavailable:
          "실행한 사람의 권한을 다시 확인하지 못해 평가를 멈췄습니다. 평가는 Agent 의 권한으로 대신 진행하지 않습니다. 다시 로그인한 뒤 실행해 주세요.",
        invalid_graph: "평가할 리비전의 정의가 지금 플랫폼의 검증을 통과하지 못해 평가를 실행하지 않았습니다. 정의 탭에서 문제를 고쳐 저장한 뒤 다시 실행해 주세요.",
        runtime_image_too_old:
          "런타임 이미지가 평가 실행에 필요한 기능을 지원하지 않아 평가를 실행하지 않았습니다. 클러스터의 에이전트 이미지를 새 판으로 올린 뒤 다시 실행해 주세요.",
        preview_limit: `${EVALUATION_QUEUE_MINUTES}분 동안 미리 보기 자리가 나지 않아 평가를 실행하지 않았습니다. 다른 미리 보기를 끝낸 뒤 다시 실행해 주세요.`,
        preview_start_limit: "오늘 미리 보기를 시작할 수 있는 횟수를 모두 사용해서 평가를 실행하지 않았습니다. 내일 다시 실행해 주세요.",
        token_limit: "이 실행이 사용할 수 있는 토큰을 모두 사용해서 실행을 멈췄고, 실행한 사례까지만 채점했습니다. 사례를 줄이거나 세트를 나눠 다시 실행해 주세요.",
        daily_budget: "이 Agent 가 오늘 사용할 수 있는 토큰을 모두 사용해서 실행을 멈췄고, 실행한 사례까지만 채점했습니다. 내일 다시 실행해 주세요.",
        preview_ended: "평가 미리 보기가 끝나서 실행을 멈췄고, 실행한 사례까지만 채점했습니다. 다시 실행해 주세요.",
        case_not_started: "사례를 시작하지 못해서 실행을 멈췄고, 실행한 사례까지만 채점했습니다. 잠시 뒤 다시 실행해 주세요.",
        evaluation_grader_provider:
          "채점 모델을 사용할 수 없어 채점하지 못했습니다. 채점 모델의 provider 가 Platform Settings 에서 지워졌거나 키가 없거나, 이 리비전이 사용하지 않는 provider 이거나, 이 플랫폼에서 허용하지 않는 stub provider 일 수 있습니다. 이 리비전이 사용하는 provider 의 모델을 채점 모델로 선택한 뒤 다시 실행해 주세요.",
        evaluations_disabled: "플랫폼에서 평가를 꺼 두어 실행을 취소했습니다. 플랫폼 관리자가 평가를 다시 켠 뒤 실행해 주세요.",
      } as Record<string, string>,
      empty: "아직 실행한 평가가 없습니다.",
      columns: { started: "실행한 시각", by: "실행한 사람", state: "상태", result: "결과" },
      /** 도는 실행의 진행. done 은 돈 사례와 반복의 수, total 은 그 차례의 크기다. */
      progress: (done: number, total: number): string => `${total}개 가운데 ${done}개 실행`,
      /** 채점 중인 실행의 진행. done 은 채점을 마친 판정의 수, total 은 채점 기준이 있는 판정의 수다. */
      gradingProgress: (done: number, total: number): string => `${total}개 가운데 ${done}개 채점`,
      cancel: "평가 실행 취소",
      cancelledBy: (name: string): string => `${name} 님이 취소했습니다.`,
      open: "결과 보기",
      /** 실행 하나의 결과 창 제목. */
      detailTitle: (revision: number): string => `r${revision} 평가 결과`,
      repeat: "반복",
      passed: "통과",
      failed: "실패",
      graderError: "채점 오류",
      worse: "나빠짐",
      answer: "답변",
      toolCalls: "Tool 호출",
      /** 실행이 기록한 사례가 지금은 없다. */
      deletedCase: "지운 사례",
    },
    /** Tool 호출이 답한 꼴 (P45 10절). */
    call: { ran: "실행함", simulated: "가상 결과를 돌려줌", rejected: "거부함", refused: "런타임이 거절함" } as Record<string, string>,
    /** 배포 전 평가 (P45 12절). 게이트 카드와 배포·재배포·되돌리기 대화상자가 쓴다. */
    gate: {
      title: "배포 전 평가",
      hint: (days: number): string =>
        `켜면 이 세트의 평가를 통과한 정의만 배포할 수 있습니다. 통과율이 기준 이상이고 배포된 리비전보다 나빠진 사례가 없어야 하며, 평가 결과는 ${days}일 동안 유효합니다.`,
      on: "켜짐",
      minPassRate: "최소 통과율 (%)",
      noWorseCases: "배포된 리비전보다 나빠진 사례가 없어야 합니다",
      /** min 은 나빠진 사례를 막는 게이트가 받는 세트의 반복 수 하한이다. */
      noWorseCasesHint: (min: number): string => `반복 횟수가 ${min} 이상인 세트에서만 사용할 수 있습니다.`,
      maxAgeDays: "평가 결과의 유효 기간 (일)",
      turnOff: "끄기",
      skip: "평가 없이 배포",
      skipReason: "평가 없이 배포하는 까닭",
      skipHint: "까닭과 배포한 사람은 배포 기록에 남습니다.",
      /** 409 evaluation_required 의 까닭 (P45 12절). */
      reasons: {
        no_run: "이 정의를 평가한 결과가 없습니다.",
        stale: (days: number): string => `평가 결과가 ${days}일보다 오래됐거나 그 뒤에 세트가 바뀌었습니다.`,
        nothing_counted: "배포 전 평가에 반영할 수 있는 사례가 없습니다. 사례에 확인 항목을 추가해 주세요.",
        pass_rate: (rate: number, min: number): string => `통과율 ${rate}%가 기준 ${min}%에 미치지 못합니다.`,
        worse_cases: (count: number): string => `배포된 리비전보다 나빠진 사례가 ${count}건 있습니다.`,
        baseline_missing: (count: number): string => `배포된 리비전에서 평가한 결과가 없는 사례가 ${count}건 있습니다. 배포된 리비전도 이 세트로 평가해 주세요.`,
      },
      /** 거절에 실린 그 정의의 가장 새 실행. state 는 실행 상태의 이름이다. */
      latestRun: (state: string): string => `이 정의를 마지막으로 평가한 실행은 "${state}" 상태입니다.`,
      runDeployed: "배포된 리비전 평가 실행",
    },
  },
  /** 캔버스 아래의 테스트 채팅 서랍. */
  testChat: {
    title: "테스트 채팅",
    /** 저장한 적 없는 캔버스. preview 는 그 사람이 미리 보기를 띄울 수 있는가(agent:deploy)다. */
    unsaved: (preview: boolean): string => (preview ? "저장하면 미리 보기로 대화할 수 있습니다." : "저장하고 배포한 뒤에 쓸 수 있습니다."),
    /** revision 은 배포된 리비전이고 아직 모르면 null 이다. 그때는 번호 없이 적는다. */
    talkingTo: (revision: number | null): string => `배포된 r${revision ?? ""}${particle(`r${revision ?? ""}`, "과/와")} 대화합니다`,
    behind: (revision: number): string => `편집 중인 r${revision}${particle(`r${revision}`, "은/는")} 아직 반영되지 않았습니다`,
    /** 아직 대화할 수 없을 때. when 은 무엇을 하면 쓸 수 있는지만 고르고, preview 는 그 사람이 미리 보기를 띄울 수 있는가다. */
    waiting: (when: TestChatWait, preview: boolean): string =>
      preview && when !== "notReady" ? testChatPreviewOpens[when] : `${testChatOpens[when]} 여기서 바로 대화할 수 있습니다.`,
    /** 미리 보기 (P44b 11절). revision 은 저장한 최신 리비전이거나 미리 보기가 도는 리비전이다. */
    previewStart: (revision: number): string => `r${revision} 미리 보기`,
    previewStarting: "미리 보기를 준비하는 중입니다. 파드가 뜨면 대화할 수 있습니다.",
    previewTalking: (revision: number): string =>
      `미리 보기 r${revision}${particle(`r${revision}`, "과/와")} 대화합니다. 장기 기억을 사용하지 않고, 미리 보기를 끝내면 이 대화도 사라집니다.`,
    previewTools: "미리 보기도 배포된 Agent 처럼 실제 Tool 을 부르므로 쓰기 Tool 은 실제 시스템을 바꿉니다.",
    previewMove: "저장한 최신 리비전으로 미리 보기를 바꾸면 파드를 다시 띄우므로 지금 대화는 사라집니다.",
    previewEnd: "미리 보기 끝내기",
    previewEndedIdle: (minutes: number): string => `미리 보기를 ${minutes}분 동안 사용하지 않아 끝냈습니다. 다시 시작해 주세요.`,
    previewEndedMax: (hours: number): string => `미리 보기는 처음 시작하고 ${hours}시간이 지나면 끝납니다. 다시 시작해 주세요.`,
    previewEndedByOther: "다른 사람이 이 미리 보기를 끝냈습니다. 다시 시작해 주세요.",
    previewEndedAccess: "이 테넌트에서 Agent 를 배포할 권한이 없어져 미리 보기를 끝냈습니다.",
    previewEndedDisabled: "플랫폼에서 미리 보기를 꺼서 이 미리 보기를 끝냈습니다.",
    /** 배포가 실패했거나 파드가 Failed 로 읽히는 미리 보기의 상태 줄. */
    previewFailed: "미리 보기를 시작하지 못했습니다. 미리 보기를 끝낸 뒤 다시 시작해 주세요.",
  },
  /** 캔버스 맨 위 줄(CanvasHeader). 저장 단추는 shared.save 와 shared.saving, 배포 단추는 shared.deployDialog.submit 이다. */
  canvasHeader: {
    noNodes: "캔버스에 노드가 없습니다. 팔레트에서 먼저 노드를 놓아 주세요.",
    revisionOption: (revision: number, current: boolean): string => `r${revision}${current ? " (현재)" : ""}`,
    /** 한 번도 저장하지 않은 캔버스. */
    neverSaved: "미저장",
    /** 저장한 뒤로 바뀐 것이 있다. */
    dirty: "저장되지 않음",
    validationIdle: "노드를 놓으면 검증합니다",
    contextLoading: "참조 목록을 불러오는 중입니다. provider·MCP·Database 확인은 그동안 보류합니다.",
    undo: "되돌리기",
    undoTitle: "되돌리기 (⌘Z)",
    redo: "다시 하기",
    redoTitle: "다시 하기 (⇧⌘Z)",
    autoLayout: "자동 배치",
    validating: "검증 중입니다.",
    validatingButton: "검증 중…",
    validate: "검증",
    saveTitle: "저장 (⌘S)",
    /** 꺼진 단추의 접근 이름. action 은 단추의 이름(autoLayout, validate, shared.save)이고 reason 은 꺼진 까닭이다. */
    blocked: (action: string, reason: string): string => `${action}. ${reason}`,
    /** 배포 단추가 꺼진 까닭(title)과 그 접근 이름. new 는 저장한 적 없는 캔버스, dirty 는 저장하지 않은 변경이다. */
    deployReason: { new: "먼저 저장해 주세요.", dirty: "저장하지 않은 변경이 있습니다. 저장한 뒤에 배포할 수 있습니다." },
    deployBlocked: { new: "배포. 먼저 저장해 주세요.", dirty: "배포. 저장하지 않은 변경이 있습니다. 저장한 뒤에 배포할 수 있습니다." },
    back: "Agents",
  },
  /** 캔버스 화면(Canvas.tsx). 노드와 인스펙터의 글자는 canvas 다. */
  canvasPage: {
    /** 저장하지 않고 나가려 할 때 묻는 말. 무엇이 사라지는지 먼저 적는다. */
    leave: "저장하지 않은 변경이 있습니다. 나가면 이 캔버스의 노드·간선·설정이 사라집니다. 나갈까요?",
    replaceWithTemplate: "캔버스를 템플릿으로 바꿉니다. 지금 내용은 사라집니다. 계속할까요?",
    loadRevision: (revision: number): string => `저장하지 않은 변경이 있습니다. r${revision}${particle(`r${revision}`, "을/를")} 불러오면 지금 내용은 사라지고 되돌릴 수 없습니다. 계속할까요?`,
    loadProposal: "저장하지 않은 변경이 있습니다. 제안을 불러오면 지금 내용은 사라지고 되돌릴 수 없습니다. 계속할까요?",
    /** 불러온 청지기의 제안을 캔버스에서 고친 뒤 "제안대로 저장" 을 누를 때 묻는 말 (P48 6.5절). */
    approveOverEdits:
      "캔버스에서 제안을 고친 내용이 있습니다. 제안대로 저장하면 고친 내용은 사라집니다. 고친 내용까지 저장하려면 취소한 뒤 저장을 눌러 주세요. 제안대로 저장할까요?",
    /** 캔버스 위 안내 줄. */
    notice: {
      template: (name: string): string => `템플릿 "${name}"${particle(name, "을/를")} 불러왔습니다. provider·MCP·Database 를 선택하면 저장할 수 있습니다.`,
      created: (revision: number): string => `저장했습니다. 리비전 r${revision}${particle(`r${revision}`, "을/를")} 만들었습니다. 배포 탭에서 올리면 테스트 채팅을 쓸 수 있습니다.`,
      revisionCreated: (revision: number): string => `리비전 r${revision}${particle(`r${revision}`, "을/를")} 만들었습니다. 배포된 것에 적용하려면 재배포해야 합니다.`,
      layoutOnly: "배치만 바뀌어 리비전 없이 저장했습니다.",
      revisionLoaded: (revision: number): string => `r${revision}${particle(`r${revision}`, "을/를")} 불러왔습니다. 저장하면 이 내용으로 새 리비전이 생깁니다.`,
      nameRequired: "이름을 정해야 저장할 수 있습니다. 오른쪽 패널의 Name 칸을 채워 주세요.",
      /**
       * 청지기의 제안을 연 캔버스 (P48 6.5절). latest 는 제안 뒤에 저장된 최신 리비전 번호다. loadLatestProposal 은 같은 대상의
       * 나중 제안을 불러오는 단추이고, 사람이 불러오기를 거절한 제안을 다시 불러오는 단추도 같은 말이다.
       */
      proposalLoaded: "청지기의 제안을 불러왔습니다. 저장하기 전에는 Agent 가 바뀌지 않습니다.",
      proposalStale: (latest: number): string =>
        `이 제안을 만든 뒤에 r${latest}${particle(`r${latest}`, "이/가")} 저장됐습니다. 지금 내용으로는 저장할 수 없습니다. 제안대로 저장하면 청지기가 r${latest}${particle(`r${latest}`, "을/를")} 기준으로 다시 제안합니다.`,
      proposalClosed: "이 제안은 이미 결정됐거나 새 메시지로 닫혔습니다.",
      loadLatestProposal: "최신 제안 불러오기",
      proposalUnavailable: "이 Agent 를 찾을 수 없거나 볼 권한이 없어 제안을 불러올 수 없습니다.",
    },
    /** 저장이 꺼진 이유. 이름 칸은 노드를 고르면 인스펙터에서 사라지므로 어디를 채워야 하는지 자리까지 적는다. */
    saveDisabled: {
      saving: "저장 중입니다.",
      noName: "이름을 정해야 저장할 수 있습니다. 캔버스 빈 곳을 눌러 패널을 엽니다.",
      noNodes: "Chat 트리거·에이전트·모델 노드가 있어야 저장할 수 있습니다.",
      errors: (count: number): string => `오류 ${count}건을 고쳐야 저장할 수 있습니다. 검증 배지를 눌러 어디인지 봅니다.`,
      unchanged: "저장할 변경이 없습니다.",
    },
    newTitle: "새 Agent",
    /** lists 는 못 읽은 목록의 화면 이름이고 "·" 로 잇는다. */
    contextError: (lists: readonly string[]): string =>
      `참조 목록(${lists.join("·")})을 읽지 못해 그 목록이 필요한 확인을 건너뜁니다. 새로고침하면 다시 읽습니다. 저장할 때 서버가 이 검사를 다시 합니다.`,
    /** 빈 캔버스의 안내 상자. */
    empty: {
      title: "Chat 트리거·에이전트·모델 노드가 있어야 저장할 수 있습니다.",
      body: "에이전트마다 모델 노드가 하나씩 붙습니다. 왼쪽 팔레트에서 끌어다 놓거나 눌러서 추가합니다.",
      templates: "템플릿으로 한 번에 시작합니다",
    },
    /** 못 읽은 목록 이름 가운데 영어 화면 이름(Platform Settings, Gateway 등)이 아닌 것. */
    skillRevisions: "Skill 리비전",
  },
};
