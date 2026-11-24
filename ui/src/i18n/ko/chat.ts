import type { FeedbackRating, FeedbackTag } from "@/api/types";
import type { ToolCallStatus } from "@/lib/chatStream";
import { particle } from "@/lib/particle";

/** 채팅 전사, 입력칸, 첨부, 서랍 (components/chat). Agent 채팅과 청지기 패널, Agent 상세의 세션·피드백 탭이 같이 쓴다. */
export const chat = {
  /** 입력 칸 아래의 보내기 단추. */
  composer: { busy: "응답 중…", abort: "중단", send: "보내기" },
  /** 채팅 입력칸의 안내. 두 패널이 같은 문장을 쓰고 비서는 앞에 묻는 말을 붙인다(assistant.placeholder). */
  inputHint: "Enter 로 보내고 Shift+Enter 로 줄을 바꿉니다.",
  /**
   * 화면이 스스로 내는 안내 (lib/chatNotice.ts 의 ChatNotice). 오류 자리에 문장 대신 이 키를 두고 그릴 때 문장을 만든다.
   * assistantAborted·editUnsupported·pendingChanged 는 비서 패널의 것이다. 비서의 턴은 요청과 떨어진 태스크라 중단해도 서버에서
   * 이어진다. turnRunning·cancelFailed·stopPending·sessionGone 은 연결과 떨어져 도는 에이전트의 턴을 따라가는 채팅 패널의
   * 것이다(P78 4절).
   */
  notice: {
    attachmentsNotDelivered: "첨부가 전달되지 않았습니다. 파일을 다시 첨부해 보내 주세요.",
    /** 되살린 대화나 스트림이 끊긴 패널에서 턴이 아직 돈다. 화면은 세션을 다시 읽으며 따라간다. */
    turnRunning: "이 대화의 답변이 아직 진행 중입니다. 끝나면 결과가 여기에 나타납니다.",
    /** 중지 요청이 5xx·네트워크 오류·시간 초과로 실패했다. 턴은 계속 돈다. */
    cancelFailed: "답변을 중단하지 못했습니다. 답변은 계속 진행 중이니 중단을 다시 눌러 주세요.",
    /** 중지가 받아들여진 뒤 45초가 지나도 턴이 끝나지 않았다. 파드가 죽은 턴이 리스가 끝나기 전까지 이렇게 보인다. */
    stopPending: "중단을 요청했지만 답변이 아직 끝나지 않았습니다. 잠시 뒤에 다시 확인해 주세요.",
    /** 따라가는 동안 30초 넘게 404 만 받았다. 공유 상태가 없는 파드가 바뀐 때다. */
    sessionGone: "이 대화를 더 이상 불러올 수 없습니다. 새 대화를 시작해 주세요.",
    assistantAborted: "요청을 중단했습니다. 서버 쪽 처리는 끝날 때까지 이어집니다.",
    editUnsupported: "청지기는 인자 수정(edit)을 지원하지 않습니다. 거부한 뒤 채팅으로 바꿔 달라고 말해 주세요.",
    pendingChanged: "보류 중인 호출이 바뀌었습니다. 세션을 다시 불러온 뒤 결정해 주세요.",
    /** 잠든 Agent 를 깨우는 동안 채팅과 승인 카드가 보낼 것을 쥐고 기다린다 (P44 8절). Retry-After 마다 다시 보낸다. */
    waking: "Agent 의 파드를 다시 띄우는 중입니다. 파드가 뜨면 이 메시지를 바로 보냅니다.",
    wakingDecision: "Agent 의 파드를 다시 띄우는 중입니다. 파드가 뜨면 이 결정을 바로 보냅니다.",
  },
  /**
   * 턴이 도는 대화의 배지. 채팅 패널의 세션 줄, 홈 챗의 대화 목록, Agent 의 세션 탭이 쓴다(P78 4절). 세션 탭의 배지는 마지막으로
   * 읽은 때의 상태이고 읽기 전용이라 중단 단추가 없다. 비서의 assistant.running 과 같은 말이다.
   */
  runningBadge: "진행 중",
  /** 429 의 오류 한 줄에 Retry-After 를 붙인 것. text 는 오류 문장이고 seconds 는 응답이 준 초다. */
  retryAfter: (text: string, seconds: string): string => `${text} (${seconds}초 뒤에 다시 시도해 주세요)`,
  /** 전사 항목 하나(ChatItemView). 인사말 라벨은 shared.chatPanel.greeting 이다. */
  item: {
    editMessage: "메시지 수정",
    /** 말풍선 복사 단추가 문장 가운데 넣는 명사. */
    copyMessage: "메시지",
    copyAnswer: "답변",
    subAgent: (name: string): string => `서브 에이전트 ${name}`,
    /** 턴 마감 줄의 단추. 그 턴의 실행 단계를 여는 창이다(agents.trace). */
    trace: "트레이스 보기",
    /** 턴 마감 줄의 배지. usage 의 cached 는 캐시된 입력 토큰이고 0 이면 적지 않는다. */
    turn: (turn: number): string => `턴 ${turn}`,
    usage: (total: number, input: number, cached: number, output: number): string =>
      `토큰 ${total} (입력 ${input}${cached ? ` · 캐시 ${cached}` : ""} · 출력 ${output})`,
    toolCalls: (count: number): string => `Tool ${count}`,
    /** 가드레일 항목. position 과 rule 은 서버가 실은 값이고 없으면 괄호를 뺀다. */
    guardrail: (message: string, position?: string, rule?: string): string =>
      `가드레일${position ? ` (${position}${rule ? ` · ${rule}` : ""})` : ""}: ${message}`,
    /** 서버가 가드레일 안내문을 싣지 않았거나 런타임이 기본 문장을 채웠을 때(lib/serverLiterals.ts guardrailMessage). */
    guardrailDefault: "가드레일에 걸렸습니다.",
    /** 예비 모델 알림. agent 는 서브에이전트의 전환일 때 그 이름이다. */
    fallback: (to: string, agent?: string): string =>
      `${agent ? `서브 에이전트 ${agent}의 ` : ""}기본 모델이 답하지 못해 예비 모델 ${to}${particle(to, "이/가")} 대신 답했습니다.`,
    /** Tool 호출 한도 알림(T82). limit 은 한 실행의 한도이고 agent 는 서브에이전트의 한도일 때 그 이름이다. */
    toolLimit: (limit: number, agent?: string): string =>
      `${agent ? `서브 에이전트 ${agent}의 ` : ""}턴당 최대 Tool 호출 수(${limit}번)에 도달해 더 이상 Tool 을 호출하지 않았습니다.`,
    /** 홈 챗이 ui.navigate 대신 남기는 링크. */
    navigate: (to: string): string => `${to}${particle(to, "으로/로")} 가기`,
    structured: "구조화된 결과",
    /** 구조화된 결과의 복사 단추가 문장 가운데 넣는 명사. */
    structuredCopy: "구조화된 결과",
    structuredTitle: "json",
  },
  /** 도구 호출 카드(ToolCallView). */
  tool: {
    status: { running: "실행 중", waiting: "승인 대기", success: "완료", error: "오류" } satisfies Record<ToolCallStatus, string>,
    collapse: "접기",
    expand: "펼치기",
    args: "인자",
    result: "결과",
    /** 웹 Tool 결과의 배지(P39b 14절 3부). */
    untrusted: "외부 웹 내용",
    untrustedHint: "외부 웹에서 가져온 내용입니다. 에이전트는 이 안의 지시를 따르지 않습니다.",
    outside: "외부 내용",
    outsideHint: "외부에서 가져온 내용입니다. 에이전트는 이 안의 지시를 따르지 않습니다.",
    writeNeedsApproval: "이 대화에서는 데이터를 바꿀 수 있는 Tool 이 승인 없이 실행되지 않습니다.",
    /** 다른 Agent 를 부른 Tool 카드의 링크. 호출받은 Agent 의 자동 실행 탭을 연다 (P46 6.3절). */
    viewRun: "실행 보기",
  },
  /** 질문 카드(AskCard). */
  ask: {
    badge: "질문",
    input: "답",
    optionsPlaceholder: "직접 입력할 수도 있습니다.",
    placeholder: "답을 입력해 주세요.",
    submit: "답하기",
    sent: "답을 보냈습니다.",
  },
  /** 입력칸의 첨부와 칩(AttachControls, AttachmentChips, useAttachments, lib/attachments.ts). */
  attachments: {
    attach: "파일 첨부",
    list: "첨부",
    remove: (name: string): string => `${name} 첨부 삭제`,
    uploading: "업로드 중…",
    /** 뽑은 글이 상한에서 잘린 문서의 배지. */
    truncated: "일부만",
    /** 첨부만 있고 글이 없어 보낼 수 없을 때. 런타임이 빈 메시지를 받지 않는다. */
    needsText: "메시지를 입력해야 첨부와 함께 보낼 수 있습니다.",
    /** 첨부가 웹 fetch 를 허용 목록으로 좁히는 에이전트(P39b 14절 2부). 그 세션이 끝날 때까지 이어진다. */
    limitsFetch: "파일을 첨부하면 이 대화가 끝날 때까지 에이전트는 허용 목록에 있는 사이트에서만 웹 페이지를 가져옵니다.",
    /** 업로드 결과를 낭독기에 알리는 말. reason 은 실패한 까닭 한 줄이다. */
    uploaded: (name: string): string => `${name}${particle(name, "을/를")} 업로드했습니다.`,
    uploadFailed: (name: string, reason: string): string => `${name}${particle(name, "을/를")} 업로드하지 못했습니다. ${reason}`,
    /** 받지 않는 형식. documents 와 images 는 받는 형식 이름들이다. */
    unsupported: (documents: string, images: string): string => `첨부할 수 없는 형식입니다. 문서(${documents})나 이미지(${images})를 첨부해 주세요.`,
    /** 이미지를 풀지 못했다. 손상된 파일과 브라우저가 풀기에 너무 큰 이미지를 가를 수 없어 둘 다 말한다. */
    imageUnreadable: "이미지를 열 수 없습니다. 손상된 파일이거나 너무 큰 이미지일 수 있으니 다른 이미지로 다시 첨부해 주세요.",
    /** 풀었지만 줄여 굽지 못했다. 브라우저의 캔버스가 실패한 드문 경우다. */
    imageNotResized: "이미지를 줄이지 못했습니다. 잠시 뒤에 다시 첨부해 주세요.",
    /** 상한을 넘는 파일. limit 은 "2.0 MB" 처럼 적은 상한이다. */
    imageTooLarge: (limit: string): string => `이미지는 ${limit}까지 첨부할 수 있습니다. 더 작은 이미지로 다시 첨부해 주세요.`,
    documentTooLarge: (limit: string): string => `문서는 ${limit}까지 첨부할 수 있습니다. 문서를 나눠서 다시 첨부해 주세요.`,
    /** 한 턴 상한을 넘어 첨부하지 않은 파일의 안내. */
    overLimit: (maxFiles: number, maxImages: number, dropped: number): string =>
      `한 번에 파일 ${maxFiles}개, 그 가운데 이미지 ${maxImages}개까지 첨부할 수 있어 ${dropped}개는 첨부하지 않았습니다.`,
    /** 칩의 사실 조각. lib 가 " · " 로 잇는다. count 는 화면 언어로 적은 글자 수다. */
    pages: (pages: number): string => `${pages}쪽`,
    sheets: (sheets: number): string => `시트 ${sheets}개`,
    chars: (count: string): string => `${count}자`,
  },
  /** 답의 인용 번호 칩과 출처 줄, 작은 창(Citations). */
  citations: {
    untitled: "제목 없는 문서",
    sources: "출처",
    chip: (n: number, title: string): string => `출처 ${n}: ${title}`,
    popover: (n: number): string => `출처 ${n}`,
    open: "원문 열기",
    download: "원문 다운로드",
    /** 작은 창의 한 줄 조각. 컴포넌트가 " · " 로 잇는다. n 은 1 부터 센 조각 차례, score 는 소수 둘째 자리까지 적은 관련도다. */
    chunk: (n: number): string => `${n}번째 조각`,
    score: (score: string): string => `관련도 ${score}`,
  },
  /** 다운로드 단추(DownloadButton)의 기본 글자. */
  download: "다운로드",
  /** 답변 피드백(FeedbackButtons, lib/feedback.ts). Agent 상세의 피드백 탭도 평가와 이유의 이름을 쓴다. */
  feedback: {
    rating: { up: "좋아요", down: "아쉬워요" } satisfies Record<FeedbackRating, string>,
    /** 아쉬운 이유. 보이는 차례는 lib/feedback.ts 의 FEEDBACK_TAGS 다. */
    tags: {
      inaccurate: "사실과 다릅니다",
      unhelpful: "도움이 되지 않습니다",
      incomplete: "답변이 충분하지 않습니다",
      unsafe: "안전하지 않습니다",
      other: "기타",
    } satisfies Record<FeedbackTag, string>,
    /** 평가 옆의 단추이자 좋아요 의견 창의 제목. */
    comment: "의견 남기기",
    downTitle: "어떤 점이 아쉬웠나요?",
    tagsLegend: "아쉬운 점 (여러 개 선택할 수 있습니다)",
    commentLabel: "의견",
    /** 의견 칸 아래의 안내. 승인 권한이 있는 사람이 읽으므로 개인정보를 적지 않게 한다. */
    commentHint: "자세히 적어 주시면 에이전트를 개선하는 데 활용합니다. 이 에이전트의 승인 권한이 있는 사람이 볼 수 있으니 개인정보는 적지 마세요.",
    sending: "보내는 중…",
    /** 다시 생성으로 바뀐 턴의 평가에 붙는 표시. */
    superseded: "다시 생성된 답변",
    /**
     * 평가 사례로 쓰는 동의 (P45 8절). 의견 창의 스위치와 그 안내다. 개인정보 보호법이 동의에 요구하는 항목(복사하는 것, 읽는 사람,
     * 남는 기간, 거부해도 되는 것)을 안내에 적는다. days 는 좋아요나 아쉬워요가 마지막으로 바뀐 뒤 남는 날 수다.
     */
    evaluationConsent: "이 질문과 답변을 에이전트 평가 사례로 사용하는 데 동의합니다",
    evaluationConsentHint: (days: number): string =>
      `동의하면 이 테넌트에서 developer 이상의 role 을 가진 사람과 플랫폼 관리자가 이 질문과 답변을 평가 사례로 복사해 볼 수 있습니다. 앞선 질문은 복사하지 않습니다. 동의를 철회하거나 좋아요나 아쉬워요를 취소하면 복사한 사례와 그 평가 결과도 지워집니다. 그대로 두어도 좋아요나 아쉬워요를 마지막으로 바꾼 날부터 ${days}일이 지나면 지워집니다. 채점하는 동안 채점 모델의 provider 에 이미 보낸 내용은 지울 수 없습니다. 동의하지 않아도 채팅과 의견에는 아무 영향이 없습니다.`,
  },
  /** 기억·세션 파일 서랍의 미리 보기 칸(FileText). 목록을 읽는 동안의 줄은 shell.sessionList.loading 이다. */
  files: {
    notText: "텍스트 파일이 아니어서 내용을 표시할 수 없습니다.",
    loadingContent: "내용을 불러오는 중입니다…",
    /** 코드 실행 Agent 의 작업 공간 파일 묶음(P40 6절). 파일 Tool 도 작업 공간에 쓰므로 누가 만든 것인지 가리지 않는다. */
    madeInWorkspace: "작업 공간에서 만든 파일",
  },
  /** 이 에이전트가 기억하는 내 파일(MemoryDrawer). 행의 삭제 단추는 resources.delete.rowLabel 이다. */
  memory: {
    title: "이 에이전트가 기억하는 것",
    notice:
      "에이전트가 대화하면서 남긴 내 기억입니다. 다른 사용자의 화면에는 보이지 않지만, 이 에이전트의 승인 권한이 있는 사람은 대화 기록에서 에이전트가 기억에 쓴 내용을 볼 수 있습니다. 삭제하면 다음 대화부터 에이전트가 그 내용을 알지 못합니다.",
    /** 내용 상한을 넘는 기억 파일. 오류 사전의 file_too_large 는 다운로드할 수 있는 세션 파일의 문장이라 기억은 제 문장을 쓴다. */
    tooLarge: "기억 파일이 너무 커서 열 수 없습니다. 필요 없는 기억이면 목록에서 삭제해 주세요.",
    /** 보존 기간 한 줄. 만료가 없으면 retentionForever 다. */
    retention: (days: number): string => `기억은 마지막으로 바뀐 날부터 ${days}일 동안 남습니다.`,
    retentionForever: "기억은 직접 삭제할 때까지 남습니다.",
    deleteAll: "모두 삭제",
    deleteTitle: "이 기억을 삭제할까요?",
    deleteBody: (name: string): string => `${name} 파일이 지워지고 되돌릴 수 없습니다. 다음 대화부터 에이전트가 이 내용을 알지 못합니다.`,
    deleteAllTitle: "기억을 모두 삭제할까요?",
    deleteAllBody: "이 에이전트가 기억하는 내 파일이 모두 지워지고 되돌릴 수 없습니다. 다음 대화부터 에이전트는 처음 만난 것처럼 대화합니다.",
    empty: "아직 기억한 내용이 없습니다. 에이전트가 대화하면서 기억할 내용을 남기면 여기에 보입니다.",
    content: "기억 파일 내용",
    hint: "목록에서 파일을 선택하면 내용이 여기에 보입니다.",
  },
  /** 이 대화의 파일 서랍(SessionFilesDrawer). name 은 화면에 보이는 파일 이름이다. */
  sessionFiles: {
    open: "파일",
    title: "이 대화의 파일",
    uploaded: "업로드한 파일",
    agent: "Agent 가 만든 파일",
    agentNotice: "에이전트가 파일 Tool 로 쓴 텍스트 파일입니다.",
    closed: "대화가 닫혀 파일이 지워졌습니다.",
    uploadedEmpty: "이 대화에 업로드한 파일이 없습니다.",
    agentEmpty: "Agent 가 만든 파일이 없습니다.",
    download: (name: string): string => `${name} 다운로드`,
    preview: "미리 보기",
    previewLabel: (name: string): string => `${name} 미리 보기`,
    content: "파일 내용",
    hint: "목록에서 미리 보기를 선택하면 내용이 여기에 보입니다.",
  },
  /** 대화 이름을 행 안에서 바꾸는 입력칸(SessionTitle). */
  sessionTitle: {
    label: "대화 이름",
    empty: "이름을 한 글자 이상 입력해 주세요. 바꾸지 않으려면 Esc 키를 누르세요.",
  },
  /** "생각하는 중…" 줄(ThinkingIndicator). seconds 는 그 줄이 선 뒤 흐른 초다. */
  thinking: {
    label: "생각하는 중…",
    seconds: (seconds: number): string => `${seconds}초`,
  },
  /** 마지막 턴의 다시 생성과 메시지 수정(TurnActions). */
  turnActions: {
    regenerate: "다시 생성",
    /** 도구를 실행한 턴을 다시 생성하기 전에 묻는 말. */
    confirmTitle: "답변을 다시 생성할까요?",
    confirmBody: "지난 답변을 만들 때 에이전트가 Tool 을 사용했습니다. 다시 생성하면 Tool 이 다시 실행될 수 있고, 이미 실행된 작업은 취소되지 않습니다.",
    editInput: "수정할 메시지",
  },
  /** 입력칸 아래 줄의 상대와 모델 pill(pills.tsx). */
  pills: {
    agent: "대화 상대",
    model: "모델",
    defaultModel: "기본 모델",
    /** Agent 의 모델은 읽기 전용이다. 손을 올리면 보이는 말이다. */
    modelReadonly: "모델은 Agent 의 정의가 정합니다. 캔버스에서 바꿉니다.",
  },
  /** 대화의 수명(sessionLife.ts, D46). ttl 은 화면 언어로 적은 세션 TTL 이고 days 는 기록 보존 날 수다. */
  sessionLife: {
    /** 숫자를 모를 때. 성질은 그대로 말한다. */
    plain: "대화는 닫히면 이어 쓸 수 없고 기록은 남습니다.",
    deletedOnClose: (ttl: string): string => `대화는 마지막 메시지 뒤 ${ttl}이 지나면 닫히고 기록도 함께 지워집니다.`,
    retained: (ttl: string, days: number): string => `대화는 마지막 메시지 뒤 ${ttl}이 지나면 닫혀 이어 쓸 수 없고, 기록은 ${days}일 동안 남습니다.`,
    /** 닫힌 대화를 열었을 때 입력칸 자리에 오는 문장. */
    closed: "이 대화는 닫혀 이어 쓸 수 없습니다. 새 대화로 이어 가 주세요.",
    closedAfter: (ttl: string): string => `이 대화는 마지막 메시지 뒤 ${ttl}이 지나 닫혔습니다. 새 대화로 이어 가 주세요.`,
  },
};
