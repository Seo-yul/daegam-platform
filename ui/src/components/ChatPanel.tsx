/**
 * Agent 채팅 패널 (D26). `POST /api/agents/{id}/chat` 의 SSE 를 fetch + ReadableStream 으로 읽어
 * 토큰을 흘려 보이고, 도구 호출·결과는 접이식으로, `interrupt` 는 인라인 승인 카드로 보인다. 승인 결정은
 * `POST /sessions/{sid}/decisions` 로 보내고 같은 스트림 형식으로 이어 받는다. `done` 은 토큰 사용량 배지다.
 * 세션은 첫 응답의 session_id 로 잡히고 이후 턴이 그 세션에 붙는다. 캔버스의 "테스트 채팅" 서랍도 이 패널을 쓴다.
 * 바깥에서 세션을 주면(`sessionId`) 그 세션의 저장된 전사를 `GET /sessions/{sid}` 로 되살린다(D35 F1). 새로고침해도
 * 대화가 원문 그대로 남는 자리이고, 보류 중이면 승인 카드도 다시 세운다.
 * `ephemeral` 은 필수 prop 이라 부르는 쪽이 장기 기억에 남길 대화인지 아닌지를 매번 밝힌다(D34). 테스트 채팅 둘은 true 다.
 * 전사 항목의 모델·접기·그리기(ChatItem·applyChatEvent·Bubble·ChatItemView·ToolCallView)는 components/chat/ 에, 스트림 루프는
 * lib/chatTurn.ts 에 있고 플랫폼 비서(AssistantPanel, D31)와 같이 쓴다. 여기에는 입력 폼과 세션 상태만 남는다.
 * 전사와 입력칸은 가운데 열(폭 상한 820px)에 둔다. 양 끝으로 벌어진 말풍선은 넓은 화면에서 읽기 어렵다 (D46).
 * 닫힌 대화(`session.status === "closed"`)를 열면 전사는 그대로 보이고 입력칸 자리에 닫힌 까닭과 새 대화 버튼이 온다.
 * 런타임이 아는 것은 `features`(lib/runtimeFeatures.ts 의 `useChatFeatures`) 하나로 받는다.
 * 턴은 연결과 떨어져 돈다(P78, D92). 응답 머리 `X-Daegam-Detached-Turn` 이 있으면 런타임은 연결이 닫혀도 턴을 끝까지 잇는다.
 * 그래서 패널은 스트림을 여는 순간 `X-Daegam-Session-Id` 를 제 세션으로 잡고, 마감(done·error·interrupt) 없이 끊긴 스트림은
 * 오류 대신 세션을 읽어 따라간다. 그 읽기가 실패해도 따라가기가 다시 읽는다. 턴이 도는 동안에는 플랫폼이 준 간격으로 가벼운
 * 읽기(`view=progress`, chat/useFollowTurn.ts)를 되풀이해 저장된 항목과 "진행 중" 배지, 중단 단추를 보이고, 끝나면 전체를 한 번 읽어
 * 승인 카드를 다시 세운다. 마감 항목 없이 끝난 턴(파드가 죽었다)에는 turn_aborted 줄을 화면에만 붙인다. 다른 대화로 옮기면 읽기와
 * 따라가기를 닫을 뿐 턴을 멈추지 않는다.
 * 중단은 중지 API 하나다. `X-Daegam-Turn-Id` 의 시도 id 를 실어 그 시도만 멈추고, 202 뒤 45초가 지나도 턴이 돌면 기다리라는
 * 안내를, 409 no_turn_in_progress 면 끝난 결과를, 5xx·네트워크 오류·시간 초과면 다시 누르라는 안내를, 다시 눌러도 소용없는
 * 거절이면 그 까닭을 보인다. 연결을 닫는 것은 중단이 아니다.
 * 머리가 없는 옛 이미지는 D65 의 두 길 그대로다. `cancel` 을 알렸으면 중지 API 를 부르고 스트림이 `turn_cancelled` 로 끝나기를
 * 잠시 기다린 뒤 읽기를 닫고, 기능이 없거나 답이 202 가 아니면 곧바로 연결을 끊는다.
 * 세션 줄은 id 대신 제목을 보인다(P36 3절). 되살린 세션은 색인의 제목이고, 이 패널에서 연 세션은 런타임과 같은 규칙으로
 * 첫 메시지에서 만든 제목(lib/sessionTitle.ts 의 titleFromMessage)이다. 제목이 없는 옛 세션은 id 그대로다.
 * 런타임이 `regenerate` 를 알렸으면 마지막 답변을 다시 생성하거나 마지막 메시지를 수정해 보낸다(P36 5절, chat/TurnActions.tsx).
 * 보내면서 마지막 사용자 말 뒤를 걷어 낸 화면을 먼저 보이고, 스트림을 열기 전에 거절되면 입력 가드레일의 차단까지 포함해
 * 걷기 전으로 되돌린 뒤 까닭을 보인다. 보낸 뒤에는 사라지는 단추 대신 아래 입력칸이 포커스를 받는다.
 * 잠든 Agent 에 보낸 턴(메시지, 다시 생성, 결정)은 플랫폼이 깨우는 동안 503 agent_waking 으로 답한다(P44 8절). 패널은 보낼 것을 쥔 채
 * 기다림 줄을 세우고 Retry-After 마다 다시 보내며(lib/agentSleep.ts untilAwake), 5분을 넘기면 마지막 오류를 보인다. 기다리는 동안의
 * 중단 단추는 기다림만 멈추고 보낼 것을 되돌린다. 메시지는 입력칸으로, 첨부는 칩으로, 결정은 다시 고를 수 있는 카드로 돌아간다.
 * 끝난 답변마다 턴 마감 줄에 엄지 둘이 선다(P36 6절, chat/FeedbackButtons.tsx). 되살린 세션은 내 평가로 눌린 상태를 되살린다.
 * 답의 인용 번호 `[n]` 은 전사 전체의 출처로 만든 표에 있으면 번호 칩이 되고, 칩의 작은 창에서 원문을 열거나 다운로드한다
 * (P36 7절, chat/Citations.tsx).
 * 턴이 도는데 흐르는 말풍선도 도는 도구 카드도 없으면 전사 끝에 "생각하는 중…" 이 선다(P36 11절, chat/ThinkingIndicator.tsx).
 * 런타임이 `attachments` 를 알렸고 문서나 이미지를 받으면 입력칸에서 파일을 첨부한다(P36 9절, chat/useAttachments.ts). 끌어 놓을
 * 자리는 채팅 열 전체다. 보낼 때는 업로드 id 만 싣고 그 칩은 보내는 동안 삭제할 수 없다. 스트림이 열리면 칩을 비우며 보낸 말풍선
 * 아래에 칩이 남는다. 열기 전에 거절되면 칩이 남고, 보낸 말풍선을 걷고 쓴 글을 입력칸에 되살려 다시 보내게 한다. 입력 가드레일의
 * 차단만은 정한 안내문의 답이라 보낸 말과 함께 전사에 남는다. 첨부는 대화에 묶여 대화를 바꾸면 보내지 않은 첨부를 버린다.
 * `done.attachments_received` 가 보낸 수와 다르면 첨부가 전달되지 않았다고 알린다.
 * 런타임이 `session_files` 를 알렸고 세션이 있으면 채팅 열 위의 "파일" 단추가 이 대화의 파일 서랍을 열고, 성공한 파일 쓰기 도구
 * 카드에 다운로드가 선다(P36 10절, chat/SessionFilesDrawer.tsx).
 * 패널이 부르는 라우트는 Agent id 가 아니라 채팅 라우트의 바탕(`ChatBase`)에 걸려 있다. `preview` 면 부르는 사람의 미리 보기
 * (`/agents/{id}/preview` 아래 같은 이름의 라우트)와 대화하고, 피드백과 트레이스 라우트가 없어 엄지와 트레이스 단추가 없다(P44b 6절).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { DETACHED_TURN_HEADER, SESSION_ID_HEADER, TURN_ID_HEADER, openAgentRegenerate } from "@/api/agentChat";
import { ApiError, chatBase } from "@/api/client";
import { cancelAgentTurn, getAgentPending, getAgentSession, openAgentChat, openAgentDecisions } from "@/api/resources";
import type { AgentDecision } from "@/api/types";
import {
  ATTACHMENTS_NOT_DELIVERED,
  Bubble,
  CANCEL_FAILED,
  ChatItemView,
  CitationScope,
  Composer,
  ComposerChips,
  MessageEditor,
  RegenerateButton,
  RegenerateConfirm,
  STOP_PENDING,
  SessionFilesBar,
  SessionFilesScope,
  TURN_ABORTED_ITEM,
  TURN_RUNNING,
  ThinkingIndicator,
  WAKING,
  WAKING_DECISION,
  ToolSafetyScope,
  closedSessionNotice,
  composerAttach,
  describeChatError,
  guardrailBlocked,
  notFound,
  resolveInterruptAt,
  retryable,
  rewindLastTurn,
  showsThinking,
  turnUnended,
  useAttachments,
  useFileDrop,
  useFollowTurn,
  useLastTurnActions,
  useSendOnce,
  useStickToBottom,
  useTurnFeedback,
  withRememberOptions,
  type ChatItem,
  type ChatNotice,
  type LastTurn,
} from "@/components/chat";
import { TraceDrawer, type TraceTarget } from "@/components/trace/TraceDrawer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { LiveRegion } from "@/components/ui/live-region";
import { useT } from "@/i18n";
import { itemsFromTranscript, reopenPending } from "@/lib/assistant";
import { untilAwake } from "@/lib/agentSleep";
import { attachmentsLost } from "@/lib/attachments";
import { consumeChatStream } from "@/lib/chatTurn";
import { RUNTIME_CANCEL, RUNTIME_REGENERATE, RUNTIME_TOOL_SAFETY, type ChatFeatures } from "@/lib/runtimeFeatures";
import { titleFromMessage } from "@/lib/sessionTitle";
import { cn, isAbortError } from "@/lib/utils";

/** 옛 이미지에서 중지 API 가 202 로 답한 뒤 스트림이 `turn_cancelled` 로 끝나기를 기다리는 상한(ms). 넘기면 읽기를 닫는다. */
export const CANCEL_WAIT_MS = 5_000;
/** 중지가 받아들여진 뒤 이만큼 지나도 턴이 돌면 기다리라는 안내를 보인다(ms). 그래프 닫기와 마감, 읽기 한 번의 몫이다 (P78 4절). */
export const STOP_PENDING_MS = 45_000;

/** 걸어 둔 예약을 지운다. */
function clearTimer(ref: { current: number | null }): void {
  if (ref.current === null) return;
  window.clearTimeout(ref.current);
  ref.current = null;
}

export interface ChatPanelProps {
  instanceId: string;
  /**
   * 그 Agent 에서 부르는 사람이 연 미리 보기와 대화한다(P44b 6절). 패널은 Agent id 와 이것으로 채팅 라우트의 바탕을 고른다
   * (`/agents/{id}` 나 `/agents/{id}/preview`). 미리 보기에는 답변 피드백과 트레이스 라우트가 없어 엄지와 트레이스 단추가 없다.
   */
  preview?: boolean;
  /**
   * 첫 말 전에 보이는 인사말. 배포 상태의 `status.greeting`(핀된 리비전 입구의 값)이지 편집 중인 정의가 아니다 (D65).
   * 첫 턴이 시작되면 런타임이 같은 글을 `greeting` 이벤트로 내 전사의 첫 항목이 되므로 이 말풍선은 물러난다.
   */
  greeting?: string | null;
  /** 이어갈 세션. 비우면 첫 턴에서 새 세션이 생긴다. */
  sessionId?: string | null;
  onSessionChange?: (sessionId: string) => void;
  /**
   * 이 대화의 턴에 장기 기억 저장소를 붙이지 않는다(D34). 만드는 사람이 던져 보는 테스트 채팅은 true 여야
   * 기억을 더럽히지 않는다. 기본값을 두지 않은 것은 부르는 쪽이 매번 어느 쪽인지 밝히라는 뜻이다.
   */
  ephemeral: boolean;
  /** 서랍처럼 낮은 높이로 쓸 때. */
  compact?: boolean;
  /**
   * 세션 id 와 "새 세션" 을 보이는 머리줄. 홈 챗은 왼쪽 대화 목록이 세션을 쥐므로 끈다(D35 F4).
   * 한 화면에 세션 고르개가 둘이면 어느 쪽이 진짜인지 알 수 없다.
   */
  showSessionBar?: boolean;
  disabled?: boolean;
  /** 입력칸 아래 줄 왼쪽. 홈 챗이 상대 pill 을 여기 꽂는다 (D45). */
  composerLeft?: ReactNode;
  /** 입력칸 아래 줄 오른쪽. 홈 챗이 읽기 전용 모델 chip 을 여기 꽂는다. Agent 상세의 채팅 탭은 비운다. */
  composerModel?: ReactNode;
  /**
   * 마운트하자마자 보낼 첫 메시지. 홈의 시작 화면에서 쓴 말이 그대로 첫 턴이 된다 (D45 다섯째).
   * 한 번만 보내고 잊는다.
   */
  initialMessage?: string | null;
  /** Chat 입구 노드의 세션 TTL(초). 닫힌 대화의 안내문이 이 값으로 까닭을 적는다 (D46). */
  sessionTtlSeconds?: number | null;
  /** 닫힌 대화에서 "새 대화" 를 누른 자리. 세션을 쥔 쪽(홈 챗의 목록)이 준다. */
  onNewSession?: () => void;
  /**
   * 배포된 런타임이 아는 것(`useChatFeatures`). `cancel` 이 있으면 중단이 중지 API 를 불러 런타임이 마감한 마지막
   * 이벤트까지 받고, 없으면(첫 배포, 옛 이미지) 연결을 끊어 멈춘다 (D65). `regenerate` 가 있으면 마지막 턴에 다시
   * 생성·메시지 수정 단추가 선다 (P36).
   */
  features: ChatFeatures;
  /** 턴 마감 줄에 트레이스 단추를 세우고 그 창을 연다(P37 12절). Agent 상세의 채팅과 홈 챗이 켠다. */
  traces?: boolean;
  /** 오류 줄에 선 오류. 미리 보기 서랍은 끝난 미리 보기가 답한 404 면 미리 보기를 곧바로 다시 읽어 끝난 까닭을 보인다 (P44b). */
  onError?: (error: unknown) => void;
}

export function ChatPanel({ instanceId, preview = false, greeting, sessionId: initialSessionId = null, onSessionChange, ephemeral, compact = false, disabled = false, showSessionBar = true, composerLeft, composerModel, initialMessage = null, sessionTtlSeconds = null, onNewSession, features, traces = false, onError }: ChatPanelProps) {
  const t = useT();
  const text = t.shared.chatPanel;
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [traceTarget, setTraceTarget] = useState<TraceTarget | null>(null);
  // 세션 줄에 보이는 제목이다. 세션이 바뀌면 비우고 되살린 색인 줄이나 첫 메시지로 채운다
  const [title, setTitle] = useState<string | null>(null);
  // 잡은 오류나 화면이 낸 안내(ChatNotice)다. 문장은 그릴 때 describeChatError 로 만든다. 언어를 바꾸면 그 언어로 다시 그려진다
  const [sendError, setSendError] = useState<unknown>(null);
  // 세션 TTL 이 지나 닫힌 대화인가 (D46). 전사는 그대로 읽히고 새 턴만 막힌다
  const [closed, setClosed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  // 도는 턴의 세션 id 다. 첫 턴은 done 전에는 상태에 없어 응답 머리에서 읽는다. 중지 API 가 이 값으로 부른다
  const turnSessionRef = useRef<string | null>(null);
  // 중지 API 가 202 로 답한 뒤 읽기를 닫을 예약이다. 스트림이 먼저 끝나면 지운다
  const cancelTimerRef = useRef<number | null>(null);
  const { scrollerRef, onScroll } = useStickToBottom();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 채팅 라우트의 바탕. 아래 콜백과 효과가 이것에 기대므로 Agent 와 미리 보기 여부가 바뀔 때만 새로 만든다
  const chat = useMemo(() => chatBase(instanceId, preview), [instanceId, preview]);
  const attach = useAttachments(chat, features);

  const [restoring, setRestoring] = useState(false);
  // 연결과 떨어져 도는 턴을 세션 읽기로 따라가는 세션이다(P78 4절). 그동안 중단 단추와 "진행 중" 배지가 서고 입력칸이 잠긴다
  const [following, setFollowing] = useState<string | null>(null);
  // 오류가 아닌 안내 줄이다(턴이 아직 돈다, 중단을 기다린다). 문장은 그릴 때 describeChatError 로 만든다
  const [notice, setNotice] = useState<ChatNotice | null>(null);
  // 패널이 보이는 대화다. 바깥이 준 세션이 다른 대화인지, 이 패널이 연 대화를 위가 돌려준 것인지 가른다. 처음에는 아무것도 보이지
  // 않으므로 null 이고, 대화를 바꾸거나(resetSession) 패널이 세션을 잡을 때(adopt) 바뀐다
  const shownSessionRef = useRef<string | null>(null);
  // 도는 턴의 시도 id 다. 응답 머리나 세션 줄에서 온다. 중단이 이것을 실어 낡은 화면이 다음 턴을 멈추지 않게 한다
  const turnIdRef = useRef<string | null>(null);
  // 지금 읽는 스트림이 연결과 떨어진 턴인가(응답 머리). 아니면 옛 이미지라 연결을 끊는 것이 중단이다
  const detachedRef = useRef(false);
  // 스트림마다 하나씩 올리는 번호다. 다른 대화로 옮기면 올려서 앞 스트림이 새 대화의 전사와 상태를 건드리지 못하게 한다
  const streamRef = useRef(0);
  // 중지가 받아들여진 뒤 45초를 재는 예약이다
  const stopTimerRef = useRef<number | null>(null);
  // 턴이 도는가(스트림을 읽거나 따라가는 중). 예약된 안내가 그사이 끝난 턴에 서지 않게 본다
  const runningRef = useRef(false);
  const running = busy || following !== null;
  runningRef.current = running;
  const { clear: clearAttachments } = attach;
  // 오류 줄이 서면 위에 알린다. 처리기는 그릴 때마다 바뀌어도 되고 늘 마지막 것이 불린다
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  useEffect(() => {
    if (sendError !== null) onErrorRef.current?.(sendError);
  }, [sendError]);

  /** 세션을 바꾼다. 세션 줄의 제목, 전사, 오류와 안내, 닫힘 표시를 비운다. 새 세션(null)이면 빈 화면이다. */
  const resetSession = useCallback((next: string | null = null) => {
    shownSessionRef.current = next;
    setSessionId(next);
    setTitle(null);
    setItems([]);
    setSendError(null);
    setNotice(null);
    setClosed(false);
  }, []);

  /** 턴이 끝났다. 중단의 45초 예약, 턴이 돈다는 두 안내, 중단을 다시 누르라는 안내를 걷는다. */
  const turnOver = useCallback(() => {
    clearTimer(stopTimerRef);
    setNotice((n) => (n === STOP_PENDING || n === TURN_RUNNING ? null : n));
    setSendError((e: unknown) => (e === CANCEL_FAILED ? null : e));
  }, []);

  /** 읽던 스트림의 자리를 비운다. 대화를 떠날 때와 스트림이 끝날 때 같다. */
  const releaseStream = useCallback(() => {
    abortRef.current = null;
    turnSessionRef.current = null;
    detachedRef.current = false;
    clearTimer(cancelTimerRef);
    setBusy(false);
  }, []);

  /** 그 대화의 도는 턴을 따라간다. 턴이 돈다는 안내를 세우고 세션 읽기를 되풀이한다. */
  const follow = useCallback((sid: string) => {
    setNotice((n) => n ?? TURN_RUNNING);
    setFollowing(sid);
  }, []);

  /** 따라가기를 멈추고 까닭을 보인다. 대화를 잃었거나 다시 읽어도 소용없는 거절이다. */
  const stopFollowing = useCallback(
    (error: unknown) => {
      setFollowing(null);
      turnOver();
      setSendError(error);
    },
    [turnOver],
  );

  /** 보이던 대화를 떠난다. 열린 읽기를 닫고 따라가기를 멈출 뿐 연결과 떨어진 턴은 그대로 돈다. 옛 이미지의 턴은 연결이 닫혀 멈춘다(D65). */
  const leave = useCallback(() => {
    streamRef.current += 1;
    abortRef.current?.abort();
    releaseStream();
    // 앞 대화의 시도 id 로 새 대화의 턴을 멈추려 하지 않게 비운다
    turnIdRef.current = null;
    setFollowing(null);
    turnOver();
  }, [releaseStream, turnOver]);

  /** 이 패널이 연 턴의 세션을 제 대화로 잡고 위에 알린다. 위가 같은 세션을 돌려줘도 다시 그리지 않는다. */
  const adopt = useCallback(
    (sid: string) => {
      if (shownSessionRef.current === sid) return;
      shownSessionRef.current = sid;
      setSessionId(sid);
      onSessionChange?.(sid);
    },
    [onSessionChange],
  );

  // 스트림의 interrupt 에는 기억할 수 있는 선택이 없다. 플랫폼의 GET /pending 이 세션 소유자에게만 싣는다(P43 5절). 읽지 못하면
  // 카드는 기억하기 없이 그대로다
  const offerRemember = useCallback(
    async (sid: string) => {
      const pending = await getAgentPending(chat, sid).catch(() => null);
      if (pending && shownSessionRef.current === sid) setItems((prev) => withRememberOptions(prev, pending));
    },
    [chat],
  );

  /**
   * 그 세션을 전체로 읽어 보인다. 턴이 돌면 저장된 항목과 안내를 보이고 따라가며 true 를 돌려준다. 끝났으면 보류의 승인 카드를
   * 다시 세우고, `unended` 면 마감 항목 없이 끝난 턴(파드가 죽었다) 아래에 turn_aborted 줄을 화면에만 붙인다. 그사이 다른 대화로
   * 옮겼으면 버리고 false 다.
   */
  const showSession = useCallback(
    async (sid: string, unended: boolean): Promise<boolean> => {
      const detail = await getAgentSession(chat, sid);
      if (shownSessionRef.current !== sid) return false;
      setClosed(detail.session?.status === "closed");
      setTitle(detail.session?.title ?? null);
      const restored = itemsFromTranscript(detail.transcript?.items);
      if (detail.session?.running) {
        // 도는 재개의 승인 카드는 그 재개가 끝난 뒤에 다시 선다
        setItems(restored);
        turnIdRef.current = detail.session.turn_id ?? turnIdRef.current;
        follow(sid);
        return true;
      }
      // 보류 중이면 승인 카드가 다시 서야 새로고침한 사람이 결정할 수 있다
      const pending = detail.pending ? await getAgentPending(chat, sid).catch(() => null) : null;
      if (shownSessionRef.current !== sid) return false;
      const shown = pending ? reopenPending(restored, pending) : restored;
      setItems(unended && turnUnended(shown) ? [...shown, TURN_ABORTED_ITEM] : shown);
      setFollowing(null);
      turnOver();
      return false;
    },
    [chat, follow, turnOver],
  );

  // 바깥이 준 대화로 옮기면 저장된 전사로 다시 그린다. 이 패널이 연 대화를 위가 돌려준 것이면 그대로 둔다. 다시 그리면 흐르던
  // 항목을 잃는다. 오래된 런타임에는 transcript 가 없어 빈 배열이 되고, 그때도 새 턴은 그대로 쌓인다
  useEffect(() => {
    if (initialSessionId !== null && initialSessionId === shownSessionRef.current) return;
    leave();
    clearAttachments();
    resetSession(initialSessionId);
    if (!initialSessionId) return;
    let alive = true;
    setRestoring(true);
    void showSession(initialSessionId, false)
      .catch((e: unknown) => {
        // 이 효과가 사전에 기대면 언어를 바꿀 때 전사를 다시 읽으며 첨부와 입력을 비운다. 그래서 문장 대신 잡은 오류를 두고 그릴 때 문장을 만든다
        if (alive && shownSessionRef.current === initialSessionId) setSendError(e);
      })
      .finally(() => {
        if (alive) setRestoring(false);
      });
    return () => {
      alive = false;
    };
  }, [chat, clearAttachments, initialSessionId, leave, resetSession, showSession]);

  // 도는 턴을 따라간다(chat/useFollowTurn.ts). 돌면 저장된 항목과 시도 id 를 바꿔 보이고, 끝나면 전체를 한 번 읽는다
  useFollowTurn(chat, following, {
    onProgress: (next, turnId) => {
      setItems(next);
      if (turnId) turnIdRef.current = turnId;
    },
    onEnded: (sid) => showSession(sid, true),
    onStop: stopFollowing,
  });

  /** 새 대화. 보내지 않은 첨부를 버리고 빈 화면으로 간다. 도는 턴은 그대로 둔다. */
  const startOver = useCallback(() => {
    leave();
    clearAttachments();
    resetSession();
  }, [clearAttachments, leave, resetSession]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      clearTimer(cancelTimerRef);
      clearTimer(stopTimerRef);
    },
    [],
  );

  /**
   * 옛 이미지의 중단(D65). 런타임이 `cancel` 을 알면 중지 API 를 불러 마지막 이벤트까지 받고, 그 답이 202 가 아니거나 기다림을 넘기면
   * 읽기를 닫는다. 기능이 없거나 이미 중지를 기다리는 중에 다시 누르면 곧바로 끊는다.
   */
  const abortAttached = useCallback(() => {
    const controller = abortRef.current;
    if (!controller || controller.signal.aborted) return;
    const sid = turnSessionRef.current;
    if (!features.has(RUNTIME_CANCEL) || !sid || cancelTimerRef.current !== null) {
      controller.abort();
      return;
    }
    cancelAgentTurn(chat, sid)
      .then(() => {
        // 답이 오기 전에 스트림이 끝났으면 예약할 것이 없다
        if (abortRef.current !== controller) return;
        cancelTimerRef.current = window.setTimeout(() => controller.abort(), CANCEL_WAIT_MS);
      })
      .catch(() => controller.abort());
  }, [chat, features]);

  /**
   * 연결과 떨어진 턴의 중단. 시도 id 를 실은 중지 API 하나로 멈춘다. 202 면 스트림이나 다음 읽기가 turn_cancelled 를 보이고, 45초가
   * 지나도 돌면 기다리라는 안내를 보인다. 409 no_turn_in_progress 면 턴이 막 끝난 것이라 결과를 보인다. 5xx·네트워크 오류·시간
   * 초과면 턴이 그대로 도니 다시 누르라고 하고, 다시 눌러도 소용없는 거절은 그 까닭을 보이며 배포가 내려갔거나 대화가 없으면
   * 따라가기도 멈춘다.
   */
  const stopDetached = useCallback(() => {
    const sid = following ?? turnSessionRef.current;
    if (!sid) return;
    setSendError(null);
    setNotice((n) => (n === STOP_PENDING ? null : n));
    cancelAgentTurn(chat, sid, turnIdRef.current)
      .then(() => {
        // 그사이 턴이 끝났거나 다른 대화로 옮겼으면 기다릴 것이 없다
        if (!runningRef.current || shownSessionRef.current !== sid) return;
        clearTimer(stopTimerRef);
        stopTimerRef.current = window.setTimeout(() => {
          stopTimerRef.current = null;
          if (runningRef.current && shownSessionRef.current === sid) setNotice(STOP_PENDING);
        }, STOP_PENDING_MS);
      })
      .catch((e: unknown) => {
        if (shownSessionRef.current !== sid) return;
        if (e instanceof ApiError && e.code === "no_turn_in_progress") {
          // 턴이 막 끝났다. 스트림이 열려 있으면 그 마감이 곧 오고, 따라가는 중이면 결과를 읽어 보인다
          if (abortRef.current === null) void showSession(sid, true).catch((err: unknown) => setSendError(err));
          return;
        }
        if (retryable(e)) {
          setSendError(CANCEL_FAILED);
          return;
        }
        if (notFound(e) || (e instanceof ApiError && e.code === "not_deployed")) {
          stopFollowing(e);
          return;
        }
        setSendError(e);
      });
  }, [chat, following, showSession, stopFollowing]);

  /** 중단 단추. 연결과 떨어진 턴이면 중지 API, 옛 이미지면 D65 의 두 길이다. */
  const abort = useCallback(() => {
    if (following === null && !detachedRef.current) abortAttached();
    else stopDetached();
  }, [abortAttached, following, stopDetached]);

  /** 깨우는 동안 다시 보내는 열기(P44 8절). 처음 기다릴 때 기다림 줄(notice)을 세우고, 이 턴의 중단 신호가 기다림을 끝낸다. */
  const openWaking = useCallback(
    (call: () => Promise<Response>, signal: AbortSignal, notice: ChatNotice) => untilAwake(call, { signal, onWaiting: () => setNotice(notice) }),
    [],
  );

  /**
   * 스트림을 열고 끝까지 읽는다. `rollback` 을 주면 스트림을 열기 전에 거절될 때 전사를 그 모습으로 되돌리고, 입력 가드레일의
   * 차단도 전사에 남기지 않고 오류 줄로 보인다. 되돌린 전사 끝에 가드레일 항목을 붙이면 마지막 턴이 사라진다. `sent` 는 이 턴에
   * 실은 첨부 수이고 런타임이 받은 수와 다르면 알린다. 연결과 떨어진 턴이 마감 없이 끊기면 오류 대신 세션을 읽어 따라간다.
   * open 은 이 턴의 중단 신호와 기다림 줄을 받아 깨우는 동안 다시 보낸다(`openWaking`). 기다림을 멈추면 오류 줄 없이 끝난다.
   */
  const consume = useCallback(
    async (open: (signal: AbortSignal) => Promise<Response>, knownSessionId: string | null, { rollback, sent = 0 }: { rollback?: ChatItem[]; sent?: number } = {}) => {
      const controller = new AbortController();
      abortRef.current = controller;
      const stream = ++streamRef.current;
      // 다른 대화로 옮긴 뒤에는 이 스트림이 전사와 상태를 건드리지 않는다
      const current = () => streamRef.current === stream;
      setBusy(true);
      setSendError(null);
      setNotice(null);
      // 턴을 마감하는 이벤트(done·error·interrupt)를 받았는가
      let ended = false;
      // interrupt 로 멈춘 세션이다. 그 카드에 기억할 수 있는 선택을 따로 읽어 입힌다
      let interrupted: string | null = null;
      try {
        const res = await open(controller.signal)
          .catch((e: unknown) => {
            if (rollback && current()) setItems(rollback);
            throw e;
          })
          .finally(() => {
            // 기다림 줄은 스트림이 열리거나 거절되면 물러난다
            if (current()) setNotice((n) => (n === WAKING || n === WAKING_DECISION ? null : n));
          });
        if (!current()) {
          // 그사이 다른 대화로 옮겼다. 읽지 않을 본문을 닫아 연결을 붙잡지 않는다
          void res.body?.cancel();
          return;
        }
        const sid = res.headers.get(SESSION_ID_HEADER) ?? knownSessionId;
        turnSessionRef.current = sid;
        detachedRef.current = res.headers.has(DETACHED_TURN_HEADER);
        turnIdRef.current = res.headers.get(TURN_ID_HEADER);
        // 연결과 떨어진 턴은 스트림이 끊겨도 돈다. 첫 턴의 세션을 지금 잡아야 끊긴 뒤에도 따라가고 위의 목록도 그 대화를 보인다
        if (detachedRef.current && sid) adopt(sid);
        const result = await consumeChatStream(res, {
          signal: controller.signal,
          update: (fn) => {
            if (current()) setItems(fn);
          },
          onEvent: (ev) => {
            if (!current()) return;
            if (attachmentsLost(sent, ev)) setSendError(ATTACHMENTS_NOT_DELIVERED);
            if (ev.event === "done" || ev.event === "error" || ev.event === "interrupt") ended = true;
            const eventSid = (ev.data as { session_id?: string } | null)?.session_id;
            if ((ev.event === "done" || ev.event === "interrupt") && eventSid) adopt(eventSid);
            if (ev.event === "interrupt") interrupted = eventSid || sid;
          },
        });
        if (!current()) return;
        if (interrupted && features.has(RUNTIME_TOOL_SAFETY)) void offerRemember(interrupted);
        if (detachedRef.current && !ended && sid) {
          // 마감 없이 끊겼다. 턴은 서버에서 이어질 수 있으므로 오류 대신 세션을 읽어 돌면 따라가고 끝났으면 결과를 보인다. 그 읽기도
          // 실패하면(연결이 아직 끊겼다, 5xx, 그 세션을 모르는 다른 파드) 따라가기가 다시 읽는다
          await showSession(sid, true).catch((e: unknown) => {
            if (!retryable(e) && !notFound(e)) throw e;
            if (current() && shownSessionRef.current === sid) follow(sid);
          });
        } else if (result.status !== "done") {
          setSendError(result.error);
        }
      } catch (e) {
        if (!current()) return;
        // 깨우는 동안 기다리던 사람이 중단했다. 스트림을 열기 전의 AbortError 는 그것뿐이다(열기는 signal 을 받지 않는다).
        // 보낼 것은 open 이 이미 되돌렸고 오류는 없다
        if (isAbortError(e)) return;
        // 입력 가드레일은 스트림을 열기 전에 422 guardrail_blocked 로 막는다(런타임 routes). 오류가 아니라 정한
        // 안내문으로 답한 것이므로 전사에 가드레일 항목으로 남긴다. 스트림 안의 guardrail 이벤트(출력·flag)와 같은 모양이다.
        // 되돌린 전사(rollback)는 걷기 전 모습 그대로여야 하므로 같은 안내문을 오류 줄로 보인다
        if (guardrailBlocked(e) && !rollback) {
          setItems((prev) => [...prev, { kind: "guardrail", action: "block", message: e.detail, position: "input", rule: e.extra<string>("rule") }]);
        } else {
          setSendError(e);
        }
      } finally {
        if (current()) {
          releaseStream();
          if (ended) turnOver();
        }
      }
    },
    [adopt, features, follow, offerRemember, releaseStream, showSession, turnOver],
  );

  const { take, settle } = attach;
  const send = useCallback(
    (draft: string) => {
      const message = draft.trim();
      if (!message || running || disabled) return;
      if (!sessionId) setTitle(titleFromMessage(message));
      const { ids, facts } = take();
      const sent: ChatItem = { kind: "user", text: message, ...(ids.length ? { attachments: facts } : {}) };
      setItems((prev) => [...prev, sent]);
      const open = async (signal: AbortSignal) => {
        // consume 이 이 스트림의 번호를 올린 뒤에 부른다. 기다리는 동안 다른 대화로 옮기면 번호가 바뀐다
        const stream = streamRef.current;
        try {
          // 잠든 Agent 를 깨우는 동안에는 같은 메시지와 첨부를 쥐고 다시 보낸다(P44 8절). 플랫폼은 업로드를 쓰기 전에 답한다
          const res = await openWaking(() => openAgentChat(chat, { session_id: sessionId, message, ephemeral, ...(ids.length ? { attachments: ids } : {}) }), signal, WAKING);
          // 런타임이 첨부를 세션에 받았고 플랫폼은 업로드를 지웠다
          settle(ids, true);
          return res;
        } catch (e) {
          // 입구에서 거절됐거나 깨우는 동안의 기다림을 멈췄다. 칩을 놓아 다시 보내게 한다. 가드레일의 차단은 consume 이 보낸 말
          // 뒤에 안내문을 남기고, 그 밖의 거절은 보내기 전으로 되돌려 쓴 글을 입력칸에 되살린다. 그사이 새로 쓴 글이 있으면 그것을 둔다
          settle(ids, false);
          // 그사이 다른 대화로 옮겼으면 새 대화의 전사와 입력칸에 앞 대화의 글을 넣지 않는다
          if (!guardrailBlocked(e) && streamRef.current === stream) {
            setItems((prev) => prev.filter((it) => it !== sent));
            setInput((typed) => typed || draft);
          }
          throw e;
        }
      };
      void consume(open, sessionId, { sent: ids.length });
    },
    [chat, consume, disabled, ephemeral, openWaking, running, sessionId, settle, take],
  );

  // 홈의 시작 화면에서 쓴 첫 메시지 (D45 다섯째)
  useSendOnce(initialMessage, send);

  const regenerate = useCallback(
    (turn: LastTurn, message?: string) => {
      if (!sessionId) return;
      setItems(rewindLastTurn(items, turn, message));
      // 누른 단추나 입력칸은 보내는 동안 사라진다. 포커스가 body 로 떨어지지 않게 아래 입력칸으로 옮긴다
      inputRef.current?.focus();
      void consume((signal) => openWaking(() => openAgentRegenerate(chat, sessionId, { message, ephemeral }), signal, WAKING), sessionId, { rollback: items });
    },
    [chat, consume, ephemeral, items, openWaking, sessionId],
  );
  const feedback = useTurnFeedback(preview ? null : instanceId, sessionId, ephemeral);
  const canRegenerate = features.has(RUNTIME_REGENERATE) && Boolean(sessionId) && !running && !closed && !disabled && !restoring;
  const lastTurnActions = useLastTurnActions(items, canRegenerate, regenerate);
  const thinking = running && showsThinking(items);
  // 파일은 채팅 열 어디에 놓아도 받는다. 입력칸이 잠겼거나 닫힌 대화면 받지 않고 브라우저가 파일을 여는 것만 막는다
  const drop = useFileDrop(attach.enabled && !closed && !disabled && !restoring ? attach.addFiles : undefined);

  function decide(index: number, decisions: AgentDecision[]) {
    const item = items[index];
    if (!item || item.kind !== "interrupt") return;
    const sid = item.pending.session_id || sessionId;
    if (!sid) return;
    setItems((prev) => resolveInterruptAt(prev, index));
    const open = (signal: AbortSignal) =>
      openWaking(() => openAgentDecisions(chat, sid, { decisions, ephemeral }), signal, WAKING_DECISION).catch((e: unknown) => {
        // 깨우는 동안의 기다림을 멈췄으면 결정은 가지 않았다. 카드를 다시 고를 수 있게 연다
        if (isAbortError(e)) setItems((prev) => resolveInterruptAt(prev, index, false));
        throw e;
      });
    void consume(open, sid);
  }

  return (
    <div className={cn("flex min-h-0 flex-col", compact ? "h-full" : "h-[34rem]")} data-testid="chat-panel" data-session-id={sessionId ?? undefined}>
      {showSessionBar ? (
      <div className="flex h-11 items-center gap-2 border-b border-line px-4 text-[12.5px] text-muted">
        <span>{text.session}</span>
        {/* 제목 없는 세션은 id 라 고정폭이고, 아직 세션이 없을 때의 안내는 문장이라 본문 글꼴이다 */}
        <span className={cn("min-w-0 truncate text-fg", sessionId && !title && "font-mono")} data-testid="chat-session" title={sessionId ?? undefined}>
          {sessionId ? (title ?? sessionId) : text.newOnFirstMessage}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {following !== null ? (
            <Badge variant="info" data-testid="chat-running">
              {t.chat.runningBadge}
            </Badge>
          ) : null}
          {running ? (
            <Button size="sm" variant="ghost" data-testid="chat-abort" onClick={abort}>
              {text.stop}
            </Button>
          ) : (
            <Button size="sm" variant="ghost" data-testid="chat-new-session" disabled={!sessionId && !items.length} onClick={startOver}>
              {text.newSession}
            </Button>
          )}
        </span>
      </div>
      ) : null}
      {/* 전사와 입력칸은 가운데 열이다 (D46). 폭 상한 820px 를 넘으면 말풍선이 양 끝으로 벌어진다 */}
      <SessionFilesScope chat={chat} sessionId={sessionId} closed={closed} features={features}>
      <ToolSafetyScope on={features.has(RUNTIME_TOOL_SAFETY)}>
      <div
        className={cn("mx-auto flex min-h-0 w-full max-w-[820px] flex-1 flex-col rounded-card", drop.over && "ring-2 ring-inset ring-accent")}
        data-testid="chat-column"
        data-drop-over={drop.over || undefined}
        {...drop.props}
      >
      <SessionFilesBar />
      <div ref={scrollerRef} onScroll={onScroll} className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-5 py-5" data-testid="chat-transcript">
        {restoring ? (
          <p className="text-xs text-muted" data-testid="chat-restoring">
            {text.restoring}
          </p>
        ) : null}
        {greeting && !items.length && !restoring ? <Bubble role="assistant" text={greeting} label={text.greeting} testId="chat-greeting" /> : null}
        {!items.length && !greeting && !restoring ? <p className="text-xs text-muted">{text.empty}</p> : null}
        <CitationScope items={items} chat={chat} sessionId={sessionId} features={features}>
          {items.map((it, i) =>
            lastTurnActions.editing && it.kind === "user" && i === lastTurnActions.turn?.userAt ? (
              <MessageEditor key={i} text={it.text} attachments={it.attachments} onSubmit={lastTurnActions.request} onCancel={lastTurnActions.cancelEdit} />
            ) : (
              <ChatItemView
                key={i}
                index={i}
                item={it}
                onDecide={(decisions) => decide(i, decisions)}
                busy={running}
                onEdit={i === lastTurnActions.turn?.userAt ? lastTurnActions.startEdit : undefined}
                editRef={lastTurnActions.editButtonRef}
                feedback={feedback}
                onOpenTrace={traces && !preview ? (ids) => setTraceTarget({ sessionId, ...ids }) : undefined}
              />
            ),
          )}
        </CitationScope>
        {thinking ? <ThinkingIndicator /> : null}
        {lastTurnActions.turn && !lastTurnActions.editing ? <RegenerateButton onClick={() => lastTurnActions.request()} /> : null}
        <RegenerateConfirm {...lastTurnActions.confirm} />
      </div>
      {/* 섰다 사라지는 줄의 role="status" 는 낭독기가 놓칠 수 있어 늘 있는 알림 줄이 "생각하는 중" 을 알린다 */}
      <LiveRegion message={thinking ? t.chat.thinking.label : ""} testId="chat-thinking-live" />
      {notice !== null ? (
        <Alert variant="info" className="mx-4 mb-2" data-testid="chat-notice">
          {describeChatError(t, notice)}
        </Alert>
      ) : null}
      {sendError !== null ? (
        <Alert variant="error" className="mx-4 mb-2" data-testid="chat-error">
          {describeChatError(t, sendError)}
        </Alert>
      ) : null}
      {closed ? (
        /* 닫힌 대화는 읽기 전용이다 (D46). 입력칸 자리에 닫힌 까닭과 새 대화로 가는 길이 온다 */
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3 text-xs text-muted" data-testid="chat-closed">
          <span>{closedSessionNotice(t, sessionTtlSeconds)}</span>
          <Button
            size="sm"
            variant="secondary"
            className="ml-auto"
            data-testid="chat-closed-new-session"
            onClick={onNewSession ?? startOver}
          >
            {text.newChat}
          </Button>
        </div>
      ) : (
      <Composer
        value={input}
        onChange={setInput}
        onSubmit={() => {
          const draft = input;
          setInput("");
          send(draft);
        }}
        busy={running}
        // 세션 줄이 없는 홈 챗은 입력칸의 단추가 중단이다. 되살린 대화에서 도는 턴도 여기서 멈춘다
        onAbort={showSessionBar ? undefined : abort}
        abortTestId="chat-abort"
        disabled={disabled || restoring}
        placeholder={disabled ? text.notReady : t.chat.inputHint}
        rows={compact ? 1 : 2}
        inputRef={inputRef}
        left={composerLeft}
        model={composerModel}
        attach={composerAttach(attach)}
        chips={attach.enabled ? <ComposerChips attach={attach} needsText={!input.trim()} inputRef={inputRef} limitsFetch={features.attachmentsLimitFetch} /> : null}
        inputLabel={text.input}
        inputTestId="chat-input"
        sendTestId="chat-send"
        formTestId="chat-form"
      />
      )}
      </div>
      </ToolSafetyScope>
      </SessionFilesScope>
      {traces && !preview ? <TraceDrawer agentId={instanceId} target={traceTarget} onClose={() => setTraceTarget(null)} /> : null}
    </div>
  );
}
