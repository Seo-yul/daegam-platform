/**
 * 플랫폼 비서 (D31, phase-14 F2). 자리가 둘이다.
 *
 *  - `variant="drawer"`(기본) 는 Layout 의 세 번째 열(오른쪽 400px)이라 경로가 바뀌어도 남는다.
 *  - `variant="home"` 은 홈 챗 안에서 기본 대화 상대로 산다(D35). 바깥 틀·닫기·따라가기가 없고,
 *    `ui.navigate` 는 화면을 옮기는 대신 전사에 링크 항목으로 남는다. 홈 챗에서 옮기면 청지기가
 *    자기가 사는 화면을 닫고 스트림이 끊기기 때문이다. 전사와 입력칸은 가운데 열(폭 상한 820px)에
 *    둔다(D46). 오른쪽 서랍은 이미 400px 라 그대로다.
 *
 * 아래는 두 자리에 공통이다.
 *
 *  - 세션은 테넌트별로 하나를 기억한다(lib/assistant.ts). 다시 열면 `GET /assistant/sessions/{id}` 로 전사를 복원하고
 *    `state=running` 이면 3초마다 다시 읽는다(턴은 요청과 분리된 태스크라 다른 탭·다른 파드에서도 이어진다).
 *  - 턴은 `POST /turns` 의 SSE 다. 스트림 루프는 lib/chatTurn.ts 의 공용 함수이고 Agent 채팅과 같은 ChatItem 어휘로 그린다.
 *    `interrupt confirm` 은 ApprovalCard, `interrupt question` 은 AskCard(답이 다음 턴의 메시지가 된다), `resource` 는 링크 카드다.
 *  - 세션 생성이나 턴 열기가 실패하면(409 turn_in_progress 등) 입력을 되돌리고 서버에 가지 않은 말풍선은 남기지 않는다.
 *  - `ui.navigate` 는 "따라가기" 토글이 켜져 있을 때만 화면을 옮기고, 쓰기 도구 결과가 오면 그 리소스의 쿼리 키를 무효화한다.
 *  - 승인 결정의 id 는 interrupt data 에 없으면 세션을 다시 읽어 `pending.calls[i].id` 로 맞춘다.
 *  - 보내기·중단은 같은 버튼이 라벨과 동작만 바꿔 포커스가 남는다. 패널을 열면 입력에 포커스가 가고 질문에 답한 뒤에도 입력으로 돌아온다.
 *  - 루트에 `nokey` 를 달아 캔버스 위에서 Delete·Backspace 가 노드를 지우지 않게 한다.
 *  - 캔버스의 제안 보기(P48 6.5절)가 보낸 결정의 스트림은 이 패널이 받아 지금 대화에 이어 그린다(lib/assistant/relay.ts).
 *    스트림 이벤트는 듣는 쪽(캔버스)에도 건넨다. 턴의 문맥에는 캔버스에 저장하지 않은 변경이 있는지도 싣는다.
 */
import { useQueryClient } from "@tanstack/react-query";
import { PanelRightClose, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router";

import { ApiError } from "@/api/client";
import { createAssistantSession, deleteAssistantSession, getAssistantSession, openAssistantDecisions, openAssistantTurn } from "@/api/resources";
import type { AgentDecision, AssistantDecisionsBody } from "@/api/types";
import { ASSISTANT_ABORTED, ChatItemView, Composer, ModelPill, describeChatError, resolveInterruptAt, resolveInterrupts, useSendOnce, useStickToBottom, type ChatItem } from "@/components/chat";
import { CounterpartBadge } from "@/components/icons/brand";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import {
  ASSISTANT_POLL_MS,
  buildContext,
  consumeAssistantAutoFocus,
  buildDecisions,
  emitAssistantEvent,
  isCanvasUnsaved,
  registerStreamReceiver,
  relayEvent,
  resolveDecisionIds,
  setAssistantFollow,
  setAssistantProvider,
  setAssistantSessionId,
  transcriptToItems,
  useAssistantConfig,
  useAssistantFollow,
  useAssistantProvider,
  useAssistantSessionId,
  useAssistantSessions,
} from "@/lib/assistant";
import { consumeChatStream } from "@/lib/chatTurn";
import { assistantSessionsKey } from "@/lib/queryKeys";
import { assistantSessionTitle } from "@/lib/serverLiterals";
import { useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";

/** 세션 제목의 기본값. 첫 메시지의 앞부분이다. */
const TITLE_MAX = 40;

export type AssistantVariant = "drawer" | "home";

export interface AssistantPanelProps {
  onClose?: () => void;
  variant?: AssistantVariant;
  /** 입력칸 아래 줄 왼쪽. 홈 챗이 상대 pill 을 여기 꽂는다 (D45). */
  composerLeft?: ReactNode;
  /**
   * 마운트하자마자 보낼 첫 메시지. 홈의 시작 화면에서 쓴 말이 그대로 첫 턴이 된다 (D45 다섯째).
   * 한 번만 보내고 잊는다.
   */
  initialMessage?: string | null;
}

export function AssistantPanel({ onClose, variant = "drawer", composerLeft, initialMessage = null }: AssistantPanelProps) {
  const t = useT();
  const text = t.assistant;
  const home = variant === "home";
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const sessionId = useAssistantSessionId(tenant);
  const follow = useAssistantFollow();
  const sessions = useAssistantSessions(tenant);
  const config = useAssistantConfig();
  const picked = useAssistantProvider(tenant);

  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState(false);
  const runningRef = useRef(running);
  runningRef.current = running;
  const [loading, setLoading] = useState(false);
  // 잡은 오류나 화면이 낸 안내(ChatNotice)다. 문장은 그릴 때 describeChatError 로 만든다. 언어를 바꾸면 그 언어로 다시 그려진다
  const [error, setError] = useState<unknown>(null);
  const abortRef = useRef<AbortController | null>(null);
  const { scrollerRef, onScroll } = useStickToBottom();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pathRef = useRef(location.pathname);
  pathRef.current = location.pathname;
  const followRef = useRef(follow);
  followRef.current = follow;
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;
  /** 이 패널이 방금 만든 세션. 저장소 변경으로 복원 효과가 돌 때 스트리밍 중인 전사를 덮어쓰지 않게 한 번 건너뛴다. */
  const skipRestoreRef = useRef<string | null>(null);
  /** 캔버스가 넘긴 스트림을 받을 때마다 는다. 그 전에 떠난 전사 읽기가 흐르는 항목을 덮어쓰지 않게 그 답을 버린다. */
  const handOffRef = useRef(0);

  /** 입력으로 포커스를 돌린다. 질문 카드에 답한 뒤 다음 말을 바로 이어 쓰게 한다. */
  const focusInput = useCallback(() => {
    inputRef.current?.focus();
  }, []);

  /*
   * 열릴 때의 포커스. 사용자가 토글을 눌러 연 경우와 홈 챗(이 화면의 본론이 대화다)에만 준다.
   * 페이지를 열 때 저장된 상태로 서랍이 서면서 포커스를 가져가면 키보드로 오는 사람이 첫 Tab 에서
   * 본문 건너뛰기 링크 대신 오른쪽 패널 안에 떨어진다(실측). `autoFocus` 는 그 구분을 못 한다.
   */
  useEffect(() => {
    if (home || consumeAssistantAutoFocus()) focusInput();
    // 마운트 때 한 번이다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(
    async (id: string): Promise<void> => {
      const handOffs = handOffRef.current;
      try {
        const detail = await getAssistantSession(id);
        // 그 사이 세션이 바뀌었거나 넘겨받은 스트림을 그리기 시작했으면 버린다
        if (sessionRef.current !== id || handOffRef.current !== handOffs) return;
        setItems(transcriptToItems(detail));
        // 다른 곳에서 돌던 턴(끊긴 스트림 뒤의 폴링)이 끝났다. 듣는 쪽(캔버스)에 스트림 끝처럼 알린다
        if (runningRef.current && detail.state !== "running") emitAssistantEvent(id, null);
        setRunning(detail.state === "running");
      } catch (e) {
        if (sessionRef.current !== id) return;
        setRunning(false);
        if (e instanceof ApiError && e.status === 404) {
          // 사라진 세션(TTL 7일·다른 곳에서 삭제)은 잊는다
          setAssistantSessionId(tenant, null);
          setItems([]);
        } else {
          // load 가 사전에 기대면 언어를 바꿀 때 세션을 다시 읽으며 스트림을 끊는다. 그래서 문장 대신 잡은 오류를 두고 그릴 때 문장을 만든다
          setError(e);
        }
      }
    },
    [tenant],
  );

  // 세션이 바뀌면(테넌트 전환·드롭다운·감사 화면의 링크) 전사를 다시 읽는다
  useEffect(() => {
    if (skipRestoreRef.current !== null && skipRestoreRef.current === sessionId) {
      skipRestoreRef.current = null;
      return;
    }
    abortRef.current?.abort();
    setError(null);
    setRunning(false);
    if (!sessionId) {
      setItems([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    void load(sessionId).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, load]);

  // 다른 곳에서 턴이 도는 동안(state=running) 전사를 폴링한다
  useEffect(() => {
    if (!running || !sessionId || busy) return;
    const timer = window.setInterval(() => void load(sessionId), ASSISTANT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, sessionId, busy, load]);

  useEffect(() => () => abortRef.current?.abort(), []);

  /** 열린 SSE 를 끝까지 읽는다. sid 는 이 스트림의 세션이다(새 세션의 첫 턴은 저장소가 갱신되기 전이라 ref 를 믿지 않는다). */
  const consume = useCallback(
    async (sid: string, res: Response) => {
      const controller = new AbortController();
      abortRef.current = controller;
      setBusy(true);
      setError(null);
      const result = await consumeChatStream(res, {
        signal: controller.signal,
        update: setItems,
        hooks: {
          onNavigate: (to) => {
            // 홈 챗에서는 옮기지 않고 전사에 링크를 남긴다(D35). 옮기면 이 화면이 사라져 스트림이 끊긴다
            if (home) setItems((prev) => [...prev, { kind: "navigate", to }]);
            else if (followRef.current) navigate(to);
          },
        },
        onEvent: (ev) => relayEvent(queryClient, sid, ev),
      });
      setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
      // 스트림을 그만 읽었다. 턴의 끝을 받았든(그 이벤트는 이미 건넸다) 받지 못했든(중단, 세션 바꾸기, 끊김, 끝 이벤트 없이 닫힘)
      // 듣는 쪽(캔버스)은 다시 읽어 따라간다
      emitAssistantEvent(sid, null);
      if (result.status === "error") setError(result.error);
      // 세션을 바꾸느라 끊은 스트림의 중단은 오류가 아니다. 사용자가 중단한 것이면 안내하고, 서버의 턴은 이어지므로
      // 세션을 다시 읽어 running 이면 폴링으로 따라간다. 문구는 에이전트 채팅의 것과 다르다. 비서의 턴은 끊겨도 이어진다
      if (result.status === "aborted" && sessionRef.current === sid) {
        setError(ASSISTANT_ABORTED);
        void load(sid);
      }
      void queryClient.invalidateQueries({ queryKey: assistantSessionsKey(tenant) });
    },
    [load, navigate, queryClient, tenant],
  );

  /** 세션이 없으면 만들고 턴을 연다. 실패하면 오류만 보이고 null 이다. */
  async function openTurn(text: string): Promise<{ sid: string; res: Response } | null> {
    try {
      let sid = sessionRef.current;
      if (!sid) {
        const created = await createAssistantSession(text.slice(0, TITLE_MAX));
        sid = created.id;
        skipRestoreRef.current = sid;
        setAssistantSessionId(tenant, sid);
        void queryClient.invalidateQueries({ queryKey: assistantSessionsKey(tenant) });
      }
      // 고른 provider 는 그 대화에 붙는다 (D45). 고르지 않았으면 서버가 설정의 기본을 쓴다
      const body = { message: text, context: buildContext(pathRef.current, queryClient, isCanvasUnsaved()), ...(provider ? { provider } : {}) };
      const res = await openAssistantTurn(sid, body);
      return { sid, res };
    } catch (e) {
      setError(e);
      return null;
    }
  }

  async function send(message: string) {
    const text = message.trim();
    if (!text || busy || loading) return;
    setError(null);
    const opened = await openTurn(text);
    if (!opened) {
      // 서버에 가지 않았다. 입력을 되돌리고 말풍선은 남기지 않는다(그 사이 새로 쓴 글이 있으면 그것을 둔다)
      setInput((cur) => cur || text);
      return;
    }
    // 결정 없이 새 메시지를 보내면 서버가 보류를 superseded 로 닫는다. 카드도 닫는다
    setItems((prev) => [...resolveInterrupts(prev), { kind: "user", text }]);
    await consume(opened.sid, opened.res);
  }

  // 캔버스가 연 결정 스트림. 지금 보고 있는 세션의 것만 받아 열린 카드를 닫고 이어 그린다. 등록은 한 번이고 판단은 늘 최신 상태로 한다
  const receiveRef = useRef<(sid: string, res: Response) => boolean>(() => false);
  receiveRef.current = (sid, res) => {
    if (sid !== sessionRef.current) return false;
    handOffRef.current += 1;
    setItems((prev) => resolveInterrupts(prev));
    void consume(sid, res);
    return true;
  };
  useEffect(() => registerStreamReceiver((sid, res) => receiveRef.current(sid, res)), []);

  async function decide(index: number, decisions: AgentDecision[]) {
    const item = items[index];
    const sid = sessionRef.current;
    if (!item || item.kind !== "interrupt" || !sid || busy) return;
    setError(null);
    let res: Response;
    try {
      const ids = await resolveDecisionIds(sid, item.pending);
      const body: AssistantDecisionsBody = { decisions: buildDecisions(decisions, ids, item.pending.action_requests) };
      res = await openAssistantDecisions(sid, body);
    } catch (e) {
      // 카드는 열린 채 둔다. 다시 결정할 수 있다
      setError(e);
      return;
    }
    setItems((prev) => resolveInterruptAt(prev, index));
    await consume(sid, res);
  }

  function answer(text: string) {
    focusInput();
    void send(text);
  }

  function submit() {
    if (busy || loading || !input.trim()) return;
    const text = input;
    setInput("");
    void send(text);
  }

  // 홈의 시작 화면에서 쓴 첫 메시지 (D45 다섯째)
  useSendOnce(initialMessage, send);

  function newSession() {
    abortRef.current?.abort();
    setError(null);
    setAssistantSessionId(tenant, null);
  }

  async function removeSession() {
    const sid = sessionRef.current;
    if (!sid) return;
    abortRef.current?.abort();
    try {
      await deleteAssistantSession(sid);
    } catch (e) {
      setError(e);
      return;
    }
    setAssistantSessionId(tenant, null);
    void queryClient.invalidateQueries({ queryKey: assistantSessionsKey(tenant) });
  }

  const list = sessions.data ?? [];
  const pendingOpen = items.some((it) => it.kind === "interrupt" && !it.resolved);
  // 모델 chip 의 값. 이 대화에 붙은 것이 먼저이고(D45 셋째) 그 위에 방금 고른 것이 온다.
  // 대화가 없으면 테넌트에 기억해 둔 것, 그것도 없으면 설정의 기본이다
  const sessionProvider = useMemo(() => list.find((s) => s.id === sessionId)?.provider ?? null, [list, sessionId]);
  const [override, setOverride] = useState<string | null>(null);
  useEffect(() => setOverride(null), [sessionId]);
  const provider = override ?? sessionProvider ?? picked ?? config.provider ?? "";
  const providers = config.providers ?? [];

  // 홈 챗에서는 바깥 틀이 없다. 부모(홈 챗의 가운데 칸)를 채우고 머리줄 대신 위의 고르개가 상대를 말한다
  const Root = home ? "div" : "aside";
  return (
    <Root
      className={home ? "nokey flex min-h-0 flex-1 flex-col" : "nokey my-2 mr-2 flex h-[calc(100vh-1rem)] w-[384px] shrink-0 flex-col overflow-hidden rounded-panel border border-line bg-surface shadow-hairline"}
      data-testid="assistant-panel"
      data-variant={variant}
      data-session-id={sessionId ?? undefined}
      data-follow={follow ? "true" : "false"}
      aria-label={t.shell.names.assistant}
    >
      {/* 홈 챗에는 머리줄이 없다. class 로 감추면 닫기 버튼이 DOM 에 남아 탭으로 닿는다 */}
      {home ? null : (
      <div className="flex h-[52px] shrink-0 items-center gap-2.5 border-b border-line pl-4 pr-2.5">
        <CounterpartBadge kind="assistant" size={26} />
        <div className="font-display text-[15px] font-bold tracking-tight text-heading" data-testid="assistant-wordmark">
          {t.shell.names.assistant}
        </div>
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-muted" title={text.followTitle}>
          <input type="checkbox" className="size-3.5 accent-primary" data-testid="assistant-follow" checked={follow} onChange={(e) => setAssistantFollow(e.target.checked)} />
          {text.follow}
        </label>
        <button
          type="button"
          data-testid="assistant-close"
          aria-label={t.shell.assistantToggle.close}
          title={text.closeTitle}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-item text-dim hover:bg-hover hover:text-fg"
          onClick={onClose}
        >
          <PanelRightClose size={16} aria-hidden />
        </button>
      </div>
      )}
      {/* 홈 챗에서는 왼쪽 대화 목록이 세션을 쥔다(D35 F4). 여기와 거기에 고르개가 둘이면 헷갈린다 */}
      {home ? null : (
      <div className="flex items-center gap-1 border-b border-line px-3 py-2">
        <Select
          aria-label={t.shared.chatPanel.session}
          data-testid="assistant-session"
          className="h-8 min-w-0 flex-1 text-[13px]"
          value={sessionId ?? ""}
          onChange={(e) => {
            abortRef.current?.abort();
            setAssistantSessionId(tenant, e.target.value || null);
          }}
        >
          <option value="">{t.shared.chatPanel.newChat}</option>
          {list.map((s) => (
            <option key={s.id} value={s.id}>
              {assistantSessionTitle(t, s.title) || s.id}
            </option>
          ))}
        </Select>
        <Button size="sm" variant="ghost" data-testid="assistant-new-session" aria-label={t.shared.chatPanel.newChat} title={t.shell.sessionList.newTitle} disabled={!sessionId && !items.length} onClick={newSession}>
          <Plus size={14} aria-hidden />
        </Button>
        <Button size="sm" variant="ghost" data-testid="assistant-delete-session" aria-label={text.deleteSession} title={t.shell.sessionList.trashTitle} disabled={!sessionId || busy} onClick={() => void removeSession()}>
          <Trash2 size={14} aria-hidden />
        </Button>
      </div>
      )}
      {/* 홈 챗에서는 전사와 입력칸이 가운데 열이다 (D46). 서랍은 폭이 이미 좁아 감싸기만 한다 */}
      <div className={home ? "mx-auto flex min-h-0 w-full max-w-[820px] flex-1 flex-col" : "flex min-h-0 flex-1 flex-col"} data-testid="assistant-column">
      <div ref={scrollerRef} onScroll={onScroll} className={cn("flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto", home ? "px-5 py-5" : "px-4 py-4")} data-testid="assistant-transcript">
        {loading ? (
          <p className="text-xs text-muted" data-testid="assistant-loading">
            {text.loading}
          </p>
        ) : null}
        {running ? (
          <div className="flex items-center gap-2 text-[11px] text-muted" data-testid="assistant-running">
            <Badge variant="info">{text.running}</Badge>
            {text.runningElsewhere(ASSISTANT_POLL_MS / 1000)}
          </div>
        ) : null}
        {!items.length && !loading ? (
          <p className="text-xs text-muted" data-testid="assistant-empty">
            {text.empty}
          </p>
        ) : null}
        {items.map((it, i) => (
          <ChatItemView key={i} index={i} item={it} onDecide={(decisions) => void decide(i, decisions)} onAnswer={answer} busy={busy || loading} />
        ))}
      </div>
      {error !== null ? (
        <Alert variant="error" className="mx-3 mb-2" data-testid="assistant-error">
          {describeChatError(t, error)}
        </Alert>
      ) : null}
      <Composer
        value={input}
        onChange={setInput}
        onSubmit={submit}
        busy={busy}
        onAbort={() => abortRef.current?.abort()}
        disabled={loading}
        placeholder={pendingOpen ? text.pendingPlaceholder : text.placeholder(t.chat.inputHint)}
        left={composerLeft}
        model={
          providers.length ? (
            <ModelPill
              value={provider}
              providers={providers}
              onChange={(name) => {
                setOverride(name);
                setAssistantProvider(tenant, name);
              }}
            />
          ) : null
        }
        inputRef={inputRef}
        inputLabel={text.input}
        inputTestId="assistant-input"
        sendTestId="assistant-send"
        abortTestId="assistant-abort"
        formTestId="assistant-form"
      />
      </div>
    </Root>
  );
}
