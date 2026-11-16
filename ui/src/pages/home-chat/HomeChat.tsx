/**
 * 홈 챗 (D35, D45). 첫 화면이고 에이전트와 대화하는 자리다.
 *
 * 화면은 두 상태다. 고른 상대와 나눌 대화가 아직 없으면 **시작 화면**(`HomeStart`. 가운데 인사와 입력칸
 * 하나, 빠른 시작 칩)이고, 첫 메시지를 보내거나 왼쪽 목록에서 대화를 고르면 **대화 화면**이다. 시작
 * 화면에서 쓴 말은 `initialMessage` 로 그대로 넘어가 첫 턴이 된다(D45 다섯째).
 *
 * 상대와 모델은 입력칸 안에서 고른다(D45 둘째). 상대 pill 의 첫 항목은 청지기(플랫폼 비서, D31)이고 그
 * 아래가 이 테넌트의 Agent 다. 둘은 **누구로서 도는지**가 달라서(청지기는 내 권한으로 플랫폼 API 를,
 * 에이전트는 자기 자격으로 MCP 를) 아래 한 줄이 그것을 말한다. 모델은 청지기면 등록된 provider 중에서
 * 고르고 Agent 면 그 정의가 정한 값을 읽기 전용으로 보인다. Agent 의 모델은 캔버스가 정한다.
 *
 * 두 채팅을 한 부품으로 합치지 않는다(D35). 전사 복원·폴링·세션 목록·삭제·결정 종류가 전부 다르다. 에이전트 세션 목록은
 * `useAgentSessionList` 가 쥔다. 런타임이 `session_admin` 을 알리면 제목으로 검색하고 이름을 바꾸며 지운다(P36 4절). 지운
 * 대화가 열려 있었으면 시작 화면으로 돌아간다. 도는 리비전이 장기 기억을 쓰고 런타임이 `memory_files` 를 알리면 목록 아래에
 * "이 에이전트가 기억하는 것" 단추가 서고 내 기억 서랍을 연다(P36 8절).
 * 대화 화면의 안쪽은 `AssistantPanel(variant="home")` 과 `ChatPanel` 로 갈라 두고, 입력칸(`Composer`)만
 * 같은 부품을 각자 꽂는다. 세션은 첫 메시지에서 만든다. 화면을 여는 것만으로 세션이 생기면 폭증한다.
 *
 * 쓰지 않아 잠든 Agent(phase Sleeping)도 고르개에 서고 대화를 받는다(P44 8절). 사람이 고르개에서 잠든 Agent 를 고르면 한 번
 * 깨운다(`POST /wake`). 마지막 고른 것을 되살릴 때와 타이머로는 깨우지 않는다. 잠들었거나 깨우는 중인 동안에는 대화 목록과 기억
 * 서랍이 런타임을 읽지 않고 휴면 줄과 깨우기 단추를 보이며, 고른 Agent 의 상태를 폴링해 그 둘을 떠나면 다시 읽는다.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { deleteAssistantSession, getAgent, listAgentSummaries } from "@/api/resources";
import type { Agent, AgentSummary } from "@/api/types";
import { SleepingNotice, useWakeAgent } from "@/components/agents/AgentSleep";
import { AssistantPanel } from "@/components/assistant/AssistantPanel";
import { ChatPanel } from "@/components/ChatPanel";
import { PhaseBadge } from "@/components/PhaseBadge";
import { CounterpartBadge } from "@/components/icons/brand";
import { AgentPill, MemoryDrawer, ModelLabel, ModelPill, sessionLifeHint } from "@/components/chat";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";
import { mainModel } from "@/lib/agentGraph";
import { acceptsTurns, isSleeping, pausesRuntimeReads } from "@/lib/agentSleep";
import { setAssistantProvider, setAssistantSessionId, useAssistantConfigState, useAssistantProvider, useAssistantSessionId, useAssistantSessions } from "@/lib/assistant";
import { useAuthState } from "@/lib/auth";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { useNarrowViewport } from "@/lib/media";
import { agentSummariesKey, assistantSessionsKey, homeAgentDetailKey } from "@/lib/queryKeys";
import { RUNTIME_MEMORY_FILES, useChatFeatures } from "@/lib/runtimeFeatures";
import { assistantSessionTitle } from "@/lib/serverLiterals";
import { homeChatAgentKey, readItem, writeItem } from "@/lib/storage";
import { useTenant } from "@/lib/tenant";
import { cn, formatDate } from "@/lib/utils";

import { HomeStart } from "./HomeStart";
import { SessionList, type SessionRow } from "./SessionList";
import { useAgentSessionList } from "./useAgentSessionList";

/** 고르개의 청지기 항목 값. 인스턴스 id 와 섞이지 않게 uuid 가 아닌 것을 쓴다. */
export const ASSISTANT_VALUE = "assistant";

/** Chat 입구 노드가 정한 세션 TTL(초)이다. 없으면(옛 정의) null 이다. */
function sessionTtlSeconds(agent: Agent | undefined): number | null {
  const node = agent?.graph?.nodes?.find((n) => n.type === "entry");
  const value = (node?.data as { session_ttl_seconds?: number } | undefined)?.session_ttl_seconds;
  return typeof value === "number" ? value : null;
}

/** Agent 정의가 정한 모델 한 줄. 메인 에이전트에 붙은 모델(mainModel)이고, 없거나 비었으면 null 이다. */
function agentModelText(agent: Agent | undefined): string | null {
  const m = agent?.graph ? mainModel(agent.graph) : null;
  return [m?.provider, m?.model].filter(Boolean).join(" · ") || null;
}

export function HomeChatPage() {
  const t = useT();
  const text = t.shell.homeChat;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const location = useLocation();
  const navigate = useNavigate();
  const { config: assistant, settled: assistantSettled } = useAssistantConfigState();
  const auth = useAuthState();
  // 좁은 폭에서는 목록과 대화가 나란히 못 선다. 목록을 접고 버튼으로 펴며, 고르면 다시 접는다(실측)
  const narrow = useNarrowViewport();
  const [listOpen, setListOpen] = useState(false);

  const instances = useQuery<AgentSummary[]>({
    queryKey: agentSummariesKey(tenant),
    queryFn: listAgentSummaries,
  });
  // 대화를 받는 것(Ready, 잠든 것과 깨우는 중인 것)이나 아직 모르는 것을 올린다. `phase` 는 상태 조회가 한 번이라도 돈 뒤에
  // 채워지므로 배포만 하고 상세를 안 연 인스턴스는 null 이다. 그것을 감추면 배포해 둔 에이전트가 첫 화면에서
  // 사라진다(실측). 모르면 보여 주고, 고른 뒤에 그 하나만 상세로 확인한다(D35 F2)
  const ready = useMemo(() => (instances.data ?? []).filter((i) => acceptsTurns(i.phase) || i.phase == null), [instances.data]);

  // 마지막으로 고른 상대를 테넌트마다 기억한다. 첫 화면이라 새로고침할 때마다 청지기로 돌아가면
  // 에이전트와 이어 가던 사람이 매번 다시 골라야 한다
  const [picked, setPicked] = useState<string | null>(() => readItem(homeChatAgentKey(tenant)));
  // 사람이 고르개에서 고른 에이전트와 그 때다. 고른 뒤에 읽은 상세가 잠들었다고 답하면 한 번 깨우고 비운다(P44 8절). 캐시에
  // 남은 앞의 상세로는 가르지 않는다. 되살린 고름은 여기 오지 않는다
  const wakeOnPick = useRef<{ id: string; at: number } | null>(null);
  const pick = (v: string) => {
    setPicked(v);
    writeItem(homeChatAgentKey(tenant), v);
    if (v === ASSISTANT_VALUE) {
      wakeOnPick.current = null;
      return;
    }
    wakeOnPick.current = { id: v, at: Date.now() };
    // 캐시가 아직 신선해도 고른 뒤의 상태를 읽는다
    void queryClient.invalidateQueries({ queryKey: homeAgentDetailKey(tenant, v) });
  };
  // 테넌트를 바꾸면 목록이 바뀐다. 고른 에이전트가 그 테넌트에 없으면 기본으로 돌아간다
  const fallback = assistant.enabled ? ASSISTANT_VALUE : (ready[0]?.id ?? null);
  const selected = picked && (picked === ASSISTANT_VALUE ? assistant.enabled : ready.some((i) => i.id === picked)) ? picked : fallback;
  useEffect(() => {
    setPicked(readItem(homeChatAgentKey(tenant)));
  }, [tenant]);

  const onAssistant = selected === ASSISTANT_VALUE;
  const instance = onAssistant ? null : (ready.find((i) => i.id === selected) ?? null);

  // 청지기 세션은 저장소(테넌트별 현재 세션)가 쥐고 있어 패널과 목록이 같은 값을 본다
  const assistantSessionId = useAssistantSessionId(tenant);
  const assistantSessions = useAssistantSessions(tenant, assistant.enabled && onAssistant);
  // 에이전트 세션은 그 인스턴스의 것이다. 상대를 바꾸면 목록도 세션도 바뀐다
  const [agentSessionId, setAgentSessionId] = useState<string | null>(null);
  // 고른 하나만 상세로 지금 상태를 읽는다. 목록은 마지막으로 알려진 값이라 Ready 가 아닐 수 있다
  const detail = useQuery<Agent>({
    queryKey: homeAgentDetailKey(tenant, instance?.id ?? ""),
    queryFn: () => getAgent(instance!.id),
    enabled: Boolean(instance),
    // 잠들었거나 깨우는 중이면 상태만 폴링한다. 상태 읽기는 깨우지 않고, 그 둘을 떠나면 목록이 다시 읽힌다
    refetchInterval: (query) => (pausesRuntimeReads(query.state.data?.status?.phase) ? DEPLOY_POLL_MS : false),
  });
  const phase = detail.data?.status?.phase ?? null;
  // 상세가 오기 전에는 목록의 마지막 phase 로 가른다. 잠든 Agent 의 목록을 상세보다 먼저 읽지 않는다
  const paused = pausesRuntimeReads(detail.data ? phase : instance?.phase);
  // 고른 에이전트가 잠들었으면 한 번 깨운다. 깨우기 단추와 같은 길이라 답을 받으면 상태를 다시 읽는다. 실패해도 첫 메시지가
  // 깨우므로 따로 보이지 않는다
  const { mutate: wakeAgent } = useWakeAgent();
  useEffect(() => {
    const target = wakeOnPick.current;
    if (!target || detail.data?.id !== target.id || detail.dataUpdatedAt < target.at) return;
    wakeOnPick.current = null;
    if (isSleeping(detail.data.status?.phase)) wakeAgent(target.id);
  }, [detail.data, detail.dataUpdatedAt, wakeAgent]);
  const features = useChatFeatures(detail.data?.status);
  const agentSessions = useAgentSessionList({
    tenant,
    agentId: instance?.id ?? null,
    features,
    talking: Boolean(agentSessionId),
    paused,
    onDeleted: (id) => {
      if (id === agentSessionId) newSession();
    },
  });
  // 잠든 에이전트도 대화를 받는다. 보낸 턴이 깨우고 파드가 뜨기를 기다린다
  const notReady = Boolean(instance) && detail.isSuccess && !acceptsTurns(phase);
  // 도는 리비전이 장기 기억을 쓰고 런타임이 기억 보기를 알릴 때만 서랍 단추가 선다. 저장만 한 정의로 가르면 재배포 전에
  // 단추가 사라지거나 404 long_term_off 만 받는 단추가 선다
  const memories = instance && features.has(RUNTIME_MEMORY_FILES) && features.longTermMemory ? <MemoryDrawer agentId={instance.id} userSub={auth.me?.sub ?? null} phase={phase} /> : null;
  // 대화의 수명을 말하는 두 값이다 (D46). 세션 TTL 은 정의의 Chat 입구 노드가, 기록 보존 기간은
  // 플랫폼 설정이 정하고 상태 응답에 실려 온다. 하나라도 못 읽으면 안내문이 숫자를 뺀다
  const ttlSeconds = sessionTtlSeconds(detail.data);
  const retentionDays = detail.data?.status?.transcript_retention_days ?? null;

  /*
   * 시작 화면과 대화 화면을 가르는 값. 고른 상대의 세션이 있으면 대화 화면이고, 없더라도 방금 첫
   * 메시지를 보냈으면 대화 화면이다. Agent 세션 id 는 첫 턴의 done 이 와야 잡히므로 세션만 보고
   * 정하면 대답이 흐르는 동안 화면이 시작 화면으로 남는다.
   */
  const [draft, setDraft] = useState("");
  const [started, setStarted] = useState(false);
  const [firstMessage, setFirstMessage] = useState<string | null>(null);
  const sessionId = onAssistant ? assistantSessionId : agentSessionId;
  // 청지기 세션은 저장소가, 에이전트 세션은 이 화면의 상태가 쥔다
  const setSessionId = (id: string | null) => (onAssistant ? setAssistantSessionId(tenant, id) : setAgentSessionId(id));
  const talking = Boolean(sessionId) || started;

  /*
   * 첫 메시지는 한 번만 넘긴다. 패널이 마운트하며 자기 ref 에 담고(자식 효과가 부모 효과보다 먼저
   * 돈다) 그 뒤에 여기서 비우므로, 좁은 폭에서 목록을 폈다 접어 패널이 다시 서도 같은 말이 두 번
   * 가지 않는다.
   */
  useEffect(() => {
    if (firstMessage !== null) setFirstMessage(null);
  }, [firstMessage]);

  /*
   * 사이드바의 "새 대화" 는 지금 화면과 같은 경로로 오므로 라우팅이 일어나지 않는다. 그래서 Layout 이
   * `state.fresh` 로 알리고 여기서 받아 시작 화면으로 되돌린다. 받은 즉시 state 를 지워야 뒤로가기나
   * 새로고침이 같은 신호를 다시 읽지 않는다.
   */
  useEffect(() => {
    if (!(location.state as { fresh?: boolean } | null)?.fresh) return;
    navigate(location.pathname, { replace: true, state: null });
    newSession();
    // 방문(location.key)마다 한 번이다. newSession 은 함수 선언이라 여기서 부를 수 있다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);

  /** 시작 화면으로 되돌린다. 상대를 바꾸거나 "새 대화" 를 누른 자리다. */
  const restart = useCallback(() => {
    setStarted(false);
    setFirstMessage(null);
    setDraft("");
  }, []);

  // 상대를 바꾸면 그 상대의 대화로 갈아탄다. 흐르던 첫 메시지는 이 상대의 것이 아니다
  useEffect(() => {
    setAgentSessionId(null);
    restart();
  }, [selected, restart]);

  const rows: SessionRow[] = onAssistant
    ? (assistantSessions.data ?? []).map((s) => ({
        id: s.id,
        title: assistantSessionTitle(t, s.title),
        detail: [formatDate(t, s.updated_at), s.provider].filter(Boolean).join(" · "),
      }))
    : agentSessions.rows;

  /** 새 대화로 간다. 목록의 + 와 닫힌 대화의 버튼이 같이 쓴다. */
  function newSession(): void {
    setSessionId(null);
    restart();
    setListOpen(false);
  }

  async function removeAssistantSession(id: string): Promise<void> {
    await deleteAssistantSession(id).catch(() => undefined);
    if (id === assistantSessionId) setAssistantSessionId(tenant, null);
    await queryClient.invalidateQueries({ queryKey: assistantSessionsKey(tenant) });
  }

  // 상대 pill 과 모델 chip 은 두 화면이 같은 것을 쓴다. 시작 화면에서는 카드 안에, 대화 화면에서는
  // 패널의 입력칸 안에 꽂힌다
  const pill = (
    <>
      <AgentPill value={selected ?? ""} onChange={pick} disabled={instances.isLoading && !ready.length} assistantValue={ASSISTANT_VALUE}>
        {assistant.enabled ? <option value={ASSISTANT_VALUE}>{text.assistantOption}</option> : null}
        {ready.map((i) => (
          <option key={i.id} value={i.id}>
            {i.display_name || i.name}
          </option>
        ))}
      </AgentPill>
      {instance ? <PhaseBadge phase={phase ?? instance.phase} testId="home-agent-phase" /> : null}
      {assistantSettled && !assistant.enabled ? (
        <Badge variant="muted" data-testid="home-assistant-off">
          {text.assistantOff}
        </Badge>
      ) : null}
    </>
  );

  const startProvider = useAssistantProvider(tenant);
  const agentModel = agentModelText(detail.data);
  // 청지기의 모델은 시작 화면에서 고르고, 대화 화면에서는 비서 패널이 자기 입력칸 안에 같은 chip 을
  // 세운다(그 대화에 붙은 provider 를 알기 때문이다). Agent 는 정의가 정한 값이라 읽기 전용이다
  const startModel = onAssistant ? (
    (assistant.providers ?? []).length ? (
      // 고르지 않았으면 설정의 기본을 이름으로 보인다. 어느 모델과 이야기하는지가 이 결정의 요지다
      <ModelPill value={startProvider ?? assistant.provider ?? ""} providers={assistant.providers ?? []} onChange={(name) => setAssistantProvider(tenant, name)} />
    ) : null
  ) : agentModel ? (
    <ModelLabel text={agentModel} />
  ) : null;

  const actorNote = (
    <p className="text-xs text-muted" data-testid="home-actor-note">
      {onAssistant ? text.assistantNote : text.agentNote}
    </p>
  );

  // (4) 둘 다 없다. 갓 설치한 플랫폼의 기본 경로라 여기가 제일 중요하다
  const noBody = !assistant.enabled && !ready.length && !instances.isLoading;

  return (
    // 껍데기가 홈 챗에서는 본문 여백을 두지 않으므로(Layout 의 MAIN) 대화 목록 열이 패널 가장자리에 붙는다
    <div className="flex min-h-0 flex-1 flex-col" data-testid="home-chat-page">
      {narrow && !noBody ? (
        <div className="border-b border-line px-3 py-2">
          <Button variant="secondary" size="sm" data-testid="home-toggle-sessions" aria-expanded={listOpen} onClick={() => setListOpen((v) => !v)}>
            {listOpen ? text.closeList : text.openList(rows.length)}
          </Button>
        </div>
      ) : null}
      {noBody ? (
        <HomeStart
          me={auth.me}
          value={draft}
          onChange={setDraft}
          onSubmit={() => undefined}
          pill={null}
          model={null}
          quickStarts={false}
          onQuickStart={() => undefined}
          empty={<EmptyHome loadFailed={instances.isError} />}
        />
      ) : (
        <div className="flex min-h-0 flex-1">
          {/* 대화 목록은 두 화면에 다 남는다. 시작 화면에서도 옅은 바탕의 조용한 열로 옆에 선다 (D45 첫째, P81 6절) */}
          {!narrow || listOpen ? (
            <div className={cn("flex min-h-0 flex-col bg-surface-strong", narrow ? "flex-1" : "w-[268px] shrink-0 border-r border-line")}>
              <SessionList
                // 상대를 바꾸면 검색칸과 행 안 입력칸을 비운다
                key={selected ?? ""}
                rows={rows}
                selected={sessionId}
                onSelect={(id) => {
                  setSessionId(id);
                  restart();
                  setStarted(true);
                  setListOpen(false);
                }}
                onNew={newSession}
                actions={onAssistant ? { kind: "trash", onDelete: (id) => void removeAssistantSession(id) } : agentSessions.manage}
                loading={onAssistant ? assistantSessions.isLoading : agentSessions.loading}
                empty={onAssistant ? text.empty.assistant : agentSessions.query ? text.empty.search : text.empty.agent}
                note={onAssistant ? text.assistantSessionHint : sessionLifeHint(t, ttlSeconds, retentionDays)}
                footer={memories}
                notice={!onAssistant && instance && paused ? <SleepingNotice agentId={instance.id} text={t.agents.sleepingList} phase={phase} className="mx-0.5 text-xs" testId="home-sessions-sleeping" /> : undefined}
              />
            </div>
          ) : null}
          {narrow && listOpen ? null : talking ? (
            /* 대화 화면. 안쪽은 상대에 따라 갈리고 입력칸만 같은 부품이다 (D35) */
            <div className="flex min-h-0 min-w-0 flex-1 flex-col">
              {onAssistant ? (
                <AssistantPanel variant="home" composerLeft={pill} initialMessage={firstMessage} />
              ) : instance ? (
                <div className="flex min-h-0 flex-1 flex-col">
                  {notReady ? <AgentNotReadyAlert instanceId={instance.id} phase={phase} className="m-3" /> : null}
                  <ChatPanel
                    key={instance.id}
                    instanceId={instance.id}
                    sessionId={agentSessionId}
                    onSessionChange={(sid) => {
                      // 첫 턴이 세션을 만든다. 그때 왼쪽 목록에 그 대화가 없으면 방금 한 말이 어디에도 안 보인다.
                      // 런타임의 색인 쓰기와 done 이 앞뒤를 다투므로 바로 한 번, 잠시 뒤 한 번 더 읽는다
                      if (sid !== agentSessionId) {
                        void agentSessions.invalidate();
                        window.setTimeout(() => void agentSessions.invalidate(), 2_000);
                      }
                      setAgentSessionId(sid);
                    }}
                    ephemeral={false}
                    disabled={notReady}
                    features={features}
                    traces
                    showSessionBar={false}
                    sessionTtlSeconds={ttlSeconds}
                    onNewSession={newSession}
                    compact
                    composerLeft={pill}
                    composerModel={startModel}
                    initialMessage={firstMessage}
                  />
                </div>
              ) : null}
              <div className="border-t border-line px-5 py-2">{actorNote}</div>
            </div>
          ) : (
            <HomeStart
              me={auth.me}
              badge={<CounterpartBadge kind={onAssistant ? "assistant" : "agent"} size={52} />}
              value={draft}
              onChange={setDraft}
              onSubmit={() => {
                const message = draft.trim();
                if (!message || notReady) return;
                setDraft("");
                setFirstMessage(message);
                setStarted(true);
              }}
              disabled={notReady}
              pill={pill}
              model={startModel}
              // 빠른 시작 칩은 청지기가 잘하는 일이다. 상대가 Agent 면 감춘다 (D45 넷째)
              quickStarts={onAssistant}
              onQuickStart={setDraft}
              note={actorNote}
              notice={notReady && instance ? <AgentNotReadyAlert instanceId={instance.id} phase={phase} className="w-full max-w-[740px]" /> : null}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** 고른 에이전트가 Ready 가 아니라 대화할 수 없다는 안내. 시작 화면과 대화 화면이 같은 문장을 쓴다. */
function AgentNotReadyAlert({ instanceId, phase, className }: { instanceId: string; phase: string | null; className: string }) {
  const t = useT();
  const text = t.shell.homeChat;
  const detail = (
    <Link className="underline underline-offset-2" to={`/agents/${instanceId}`}>
      {text.detailLink}
    </Link>
  );
  return (
    <Alert variant="warn" className={className} data-testid="home-agent-not-ready">
      {text.notReady(phase, detail)}
    </Alert>
  );
}

/** (4) 청지기도 인스턴스도 없을 때. 무엇을 만들면 되는지 말하고 그리로 보낸다. */
function EmptyHome({ loadFailed }: { loadFailed: boolean }) {
  const t = useT();
  const text = t.shell.homeChat.emptyHome;
  return (
    <div className="flex w-full max-w-[740px] flex-col gap-3 text-sm" data-testid="home-empty">
      <p className="text-center text-base font-semibold text-heading">{text.title}</p>
      {loadFailed ? (
        <p className="text-warn" data-testid="home-empty-error">
          {text.loadFailed}
        </p>
      ) : null}
      <p className="text-muted">{text.lead}</p>
      <ol className="flex list-decimal flex-col gap-2 pl-5">
        <li>
          {text.agent(
            <Link className="text-accent underline-offset-2 hover:underline" to="/agents" data-testid="home-empty-agent-link">
              {text.agentLink}
            </Link>,
          )}
        </li>
        <li>
          {text.assistant(
            <Link className="text-accent underline-offset-2 hover:underline" to="/admin/settings" data-testid="home-empty-assistant-link">
              {text.assistantLink}
            </Link>,
          )}
        </li>
      </ol>
    </div>
  );
}
