/**
 * Agent 상세의 세션 탭 (D26·D35). 세션 색인 목록과 고른 세션의 상세다.
 *
 * 상세는 런타임의 원문 전사(`transcript.items`)를 채팅 복원과 같은 부품(itemsFromTranscript·ChatItemView)으로 그린다.
 * 승인자는 남의 세션도 보므로 읽기 전용이다. 결정 콜백을 넘기지 않아 카드가 비활성이고, 보류 중이면 채팅 복원처럼
 * 살아 있는 보류로 카드를 다시 세운 뒤 결정은 승인함 탭에서 한다고 적는다.
 *
 * 목록에는 세션 제목 칸이 있고, 런타임이 `session_admin` 을 알리면 홈 챗과 같은 제목 검색칸이 선다(P36 4절). 남의
 * 세션이 보이는 탭이라 이름 바꾸기와 삭제는 두지 않고, 같은 까닭으로 답변 피드백의 엄지와 다시 생성 단추도 넘기지 않는다.
 * 답의 인용 번호 칩은 채팅과 같다(P36 7절). 원문 다운로드는 세션 상세처럼 승인자도 받으므로 런타임이 `citations` 를 알리면
 * 칩의 작은 창에 선다.
 *
 * 상세 아래에는 그 세션의 턴 기록(턴 시도마다 실행, 결과, 토큰, 비용)이 서고, 줄마다 그 턴의 트레이스 창을 연다(P37 12절).
 * 소유자가 아니고 승인 권한도 없으면 서버가 403 이라 표 자리에 그 까닭이 선다.
 *
 * 피드백 탭의 행은 주소의 `session`·`turn`(`sessionTurnSearch`)으로 이 탭을 연다(P36 6절). 그 세션을 먼저 고르고 상세에서
 * 그 답변의 턴 마감 줄을 강조해 보이는 자리로 내린다. 두 쿼리는 처음 한 번 읽고 주소에서 지운다.
 *
 * 턴 기록의 줄 가운데 대화한 사람이 평가 사례로 쓰는 데 동의한 평가가 있는 턴에는 "평가 사례로 복사" 가 선다(P45 8절). 동의는 피드백
 * 탭과 같은 평가 목록(첫 쪽, 피드백 탭이 더 불러온 쪽까지)에서 읽고, 승인 권한과 사례를 쓰는 권한(`agent:write`)이 함께 있어야 한다.
 *
 * 상태가 Sleeping·Waking 인 동안에는 목록과 세션 상세를 읽지 않는다(P44 8절). 목록 자리에 휴면 줄과 깨우기 단추가 서고, 상태가 그
 * 둘을 떠나면 다시 읽는다.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router";

import { ApiError, chatBase } from "@/api/client";
import { getAgentPending, getAgentSession, listAgentSessions, listAgentTurns } from "@/api/resources";
import type { AgentPending, AgentSessionDetail, AgentSessionStatus, AgentTurn } from "@/api/types";
import { SleepingNotice } from "@/components/agents/AgentSleep";
import { ChatItemView, CitationScope, SessionSearch, type ChatItem } from "@/components/chat";
import { ErrorMessage } from "@/components/ErrorMessage";
import { queryGate } from "@/components/QueryGate";
import { TraceDrawer, type TraceTarget } from "@/components/trace/TraceDrawer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { itemsFromTranscript, reopenPending } from "@/lib/assistant";
import { runStatusName } from "@/lib/agentEvents";
import { pausesRuntimeReads } from "@/lib/agentSleep";
import { hasScope, useAuthState } from "@/lib/auth";
import { copyableRating } from "@/lib/evaluation";
import { agentSessionsKey, agentTurnsKey } from "@/lib/queryKeys";
import { RUNTIME_SESSION_ADMIN, type ChatFeatures } from "@/lib/runtimeFeatures";
import { useTenant } from "@/lib/tenant";
import { formatUsd, tokensOf } from "@/lib/usage";
import { cn, formatDate, formatNumber } from "@/lib/utils";

import { CopyCaseDialog } from "./EvaluationCaseEditor";
import { useAgentFeedbackList } from "./useFeedbackList";

/** 목록 칸 수다. 비었거나 불러오는 줄이 표 폭을 채운다. */
const SESSION_COLUMNS = 7;
/** 동의를 찾는 평가 목록의 거르개. 피드백 탭의 거르개 없는 목록과 같은 키다. */
const NO_FILTER = {};
/** 다른 탭이 이 탭의 한 대화와 그 답변을 여는 주소 쿼리 이름이다. */
const SESSION_PARAM = "session";
const TURN_PARAM = "turn";

/** 세션 탭을 열어 그 세션을 고르고 그 답변으로 내려가는 주소 쿼리다. */
export function sessionTurnSearch(sessionId: string, turnId: string): string {
  return new URLSearchParams({ tab: "sessions", [SESSION_PARAM]: sessionId, [TURN_PARAM]: turnId }).toString();
}

export function SessionsTab({ agentId, features, phase = null }: { agentId: string; features: ChatFeatures; /** 상태의 phase. Sleeping·Waking 이면 런타임을 읽지 않는다. */ phase?: string | null }) {
  const t = useT();
  const text = t.agents.sessions;
  const tenant = useTenant();
  const searchable = features.has(RUNTIME_SESSION_ADMIN);
  const [status, setStatus] = useState<"" | AgentSessionStatus>("");
  const [query, setQuery] = useState("");
  const linked = useLinkedTurn();
  const [selected, setSelected] = useState<string | null>(linked.session);
  const paused = pausesRuntimeReads(phase);
  // 검색어는 런타임이 session_admin 을 알렸을 때만 보낸다. 없으면 플랫폼이 409 로 막는다
  const q = searchable ? query : "";
  const list = useQuery({
    queryKey: agentSessionsKey(tenant, agentId, { status, q }),
    queryFn: () => listAgentSessions(agentId, status || undefined, q),
    enabled: !paused,
  });
  const rows = list.data ?? [];
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_28rem]">
      <Card data-testid="sessions-card">
        <CardHeader>
          <CardTitle>{t.agents.tabs.sessions}</CardTitle>
          {searchable ? <SessionSearch onSearch={setQuery} placeholder={text.search} testId="sessions-search" className="ml-auto w-44" /> : null}
          <Select aria-label={text.statusFilter} data-testid="sessions-status" className="h-8 w-36 text-xs" value={status} onChange={(e) => setStatus(e.target.value as "" | AgentSessionStatus)}>
            <option value="">{t.detail.stateCountLine.total}</option>
            <option value="active">{text.states.active}</option>
            <option value="pending">{text.states.pending}</option>
            <option value="closed">{text.states.closed}</option>
          </Select>
        </CardHeader>
        {paused ? null : <ErrorMessage error={list.error} />}
        {paused ? (
          <SleepingNotice agentId={agentId} text={t.agents.sleepingList} phase={phase} className="m-4" testId="sessions-sleeping" />
        ) : (
          <Table data-testid="sessions-table">
            <THead>
              <tr>
                <TH>{text.columns.session}</TH>
                <TH>{text.columns.title}</TH>
                <TH>{text.columns.user}</TH>
                <TH>{text.columns.status}</TH>
                <TH>{text.columns.turns}</TH>
                <TH>{text.columns.tokens}</TH>
                <TH>{text.columns.lastTurn}</TH>
              </tr>
            </THead>
            <TBody>
              {list.isLoading ? (
                <EmptyRow colSpan={SESSION_COLUMNS}>{t.common.loading}</EmptyRow>
              ) : rows.length ? (
                rows.map((s) => (
                  <TR key={s.session_id} data-testid={`session-row-${s.session_id}`} className={cn("cursor-pointer", selected === s.session_id && "bg-hover")} onClick={() => setSelected(s.session_id)}>
                    <TD className="font-mono text-xs">{s.session_id}</TD>
                    <TD className="max-w-48 truncate text-xs" title={s.title ?? undefined}>
                      {s.title || <span className="text-muted">{text.untitled}</span>}
                    </TD>
                    <TD className="text-xs">{s.user_sub}</TD>
                    <TD>
                      <span className="flex flex-wrap items-center gap-1">
                        <Badge variant={s.status === "pending" ? "warn" : s.status === "active" ? "ok" : "muted"}>{s.status}</Badge>
                        {/* 마지막으로 읽은 때의 상태다. 이 탭은 읽기 전용이라 중단은 홈 챗에서 한다 (P78 4절) */}
                        {s.running ? (
                          <Badge variant="info" data-testid={`session-running-${s.session_id}`}>
                            {t.chat.runningBadge}
                          </Badge>
                        ) : null}
                      </span>
                    </TD>
                    <TD>{s.turns}</TD>
                    <TD>{formatNumber(t, tokensOf(s.usage))}</TD>
                    <TD className="text-xs text-muted">{formatDate(t, s.last_turn_at)}</TD>
                  </TR>
                ))
              ) : (
                <EmptyRow colSpan={SESSION_COLUMNS}>{q ? text.noMatch : text.empty}</EmptyRow>
              )}
            </TBody>
          </Table>
        )}
      </Card>
      <SessionDetailCard agentId={agentId} sessionId={selected} focusTurn={selected === linked.session ? linked.turn : null} features={features} paused={paused} />
    </div>
  );
}

/**
 * 피드백 탭이 주소에 실어 보낸 세션과 답변을 처음 한 번 읽고 주소에서 지운다. 남겨 두면 탭을 옮겨도 주소에 따라다니다가 이 탭으로
 * 돌아올 때 그 대화를 다시 고른다. 지울 때는 탭 이동처럼(lib/tabs.ts) 화면이 들고 있는 state 를 남긴다.
 */
function useLinkedTurn(): { session: string | null; turn: string | null } {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const [linked] = useState(() => ({ session: params.get(SESSION_PARAM), turn: params.get(TURN_PARAM) }));
  useEffect(() => {
    if (!params.has(SESSION_PARAM) && !params.has(TURN_PARAM)) return;
    const next = new URLSearchParams(params);
    next.delete(SESSION_PARAM);
    next.delete(TURN_PARAM);
    setParams(next, { replace: true, state: location.state });
  }, [params, setParams, location.state]);
  return linked;
}

function SessionDetailCard({ agentId, sessionId, focusTurn, features, paused }: { agentId: string; sessionId: string | null; focusTurn: string | null; features: ChatFeatures; paused: boolean }) {
  const t = useT();
  const text = t.agents.sessions;
  const tenant = useTenant();
  const detail = useQuery({ queryKey: ["agents", tenant, agentId, "session", sessionId], queryFn: () => getAgentSession(chatBase(agentId), sessionId as string), enabled: Boolean(sessionId) && !paused });
  // 상세의 pending 은 참·거짓뿐이라 승인 카드에 보일 요청 내용은 따로 받는다.
  const pending = useQuery({
    queryKey: ["agents", tenant, agentId, "pending", sessionId],
    queryFn: () => getAgentPending(chatBase(agentId), sessionId as string),
    enabled: Boolean(sessionId) && Boolean(detail.data?.pending) && !paused,
  });
  return (
    <Card data-testid="session-detail">
      <CardHeader>
        <CardTitle>{text.detailTitle}</CardTitle>
        {sessionId ? <span className="font-mono text-xs text-muted">{sessionId}</span> : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {!sessionId ? <p className="text-sm text-muted">{text.pick}</p> : null}
        {detail.isLoading ? <p className="text-sm text-muted">{t.common.loading}</p> : null}
        <ErrorMessage error={detail.error} />
        {detail.data ? <SessionConversation agentId={agentId} detail={detail.data} pending={pending.data} focusTurn={focusTurn} features={features} /> : null}
        {detail.data && sessionId ? <TurnTable agentId={agentId} sessionId={sessionId} /> : null}
      </CardContent>
    </Card>
  );
}

/** 세션의 턴 기록. 줄마다 그 턴의 실행들을 트레이스 창으로 열고, 동의한 평가가 있는 턴은 평가 사례로 복사한다. */
function TurnTable({ agentId, sessionId }: { agentId: string; sessionId: string }) {
  const t = useT();
  const text = t.agents.sessions;
  const tenant = useTenant();
  const [target, setTarget] = useState<TraceTarget | null>(null);
  const copyTurns = useCopyableTurns(agentId, sessionId);
  const [copying, setCopying] = useState<string | null>(null);
  const turns = useQuery({ queryKey: agentTurnsKey(tenant, agentId, sessionId), queryFn: () => listAgentTurns(agentId, sessionId) });
  // 404 는 소유자를 아직 정할 수 없는 것(첫 turn_start 가 오기 전)이라 턴이 없다고 보인다
  const missing = turns.error instanceof ApiError && turns.error.status === 404;
  const gate = missing ? null : queryGate(turns, { testId: "session-turns-error" });
  return (
    <div className="flex flex-col gap-2" data-testid="session-turns">
      <span className="text-xs font-medium text-muted">{text.turnsTitle}</span>
      {gate}
      {missing || (!gate && turns.data) ? (
        <Table>
          <THead>
            <tr>
              <TH>{text.turnColumns.turn}</TH>
              <TH>{text.turnColumns.runs}</TH>
              <TH>{text.turnColumns.status}</TH>
              <TH>{text.turnColumns.start}</TH>
              <TH>{text.turnColumns.tokens}</TH>
              <TH>{text.turnColumns.cost}</TH>
              <TH>
                <span className="sr-only">{text.openTrace}</span>
              </TH>
              {copyTurns ? (
                <TH>
                  <span className="sr-only">{t.agents.evaluation.copy.action}</span>
                </TH>
              ) : null}
            </tr>
          </THead>
          <TBody>
            {turns.data?.length ? (
              turns.data.map((turn) => (
                <TurnRow
                  key={turn.turn_id}
                  turn={turn}
                  onOpen={() => setTarget({ sessionId, turnId: turn.turn_id })}
                  copy={copyTurns ? (copyTurns.has(turn.turn_id) ? () => setCopying(turn.turn_id) : null) : undefined}
                />
              ))
            ) : (
              <EmptyRow colSpan={copyTurns ? 8 : 7}>{text.noTurns}</EmptyRow>
            )}
          </TBody>
        </Table>
      ) : null}
      <TraceDrawer agentId={agentId} target={target} onClose={() => setTarget(null)} />
      {copying ? <CopyCaseDialog agentId={agentId} sessionId={sessionId} turnId={copying} onClose={() => setCopying(null)} /> : null}
    </div>
  );
}

/**
 * 그 세션에서 평가 사례로 복사할 수 있는 턴의 id. 승인 권한과 사례를 쓰는 권한이 없으면 null 이고 칸이 서지 않는다. 피드백 탭과 같은
 * 키의 목록이라 그 탭이 불러온 쪽도 함께 읽는다.
 */
function useCopyableTurns(agentId: string, sessionId: string): ReadonlySet<string> | null {
  const tenant = useTenant();
  const { me } = useAuthState();
  const allowed = hasScope(me, tenant, "agent:approve") && hasScope(me, tenant, "agent:write");
  const list = useAgentFeedbackList(agentId, NO_FILTER, { enabled: allowed });
  return useMemo(() => {
    if (!allowed) return null;
    const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
    return new Set(rows.filter((f) => f.session_id === sessionId && copyableRating(f)).map((f) => f.turn_id));
  }, [allowed, list.data, sessionId]);
}

/** copy 는 칸이 있으면 그 턴의 복사(동의한 평가가 없으면 null), 칸이 없으면 undefined 다. */
function TurnRow({ turn, onOpen, copy }: { turn: AgentTurn; onOpen: () => void; copy?: (() => void) | null }) {
  const t = useT();
  const text = t.agents.sessions;
  const last = turn.runs.at(-1);
  return (
    <TR data-testid={`session-turn-${turn.turn_id}`}>
      <TD>
        {turn.turn ?? "-"}
        {turn.regenerated ? (
          <Badge variant="muted" className="ml-1.5">
            {text.regenerated}
          </Badge>
        ) : null}
      </TD>
      <TD>{turn.runs.length}</TD>
      <TD>{runStatusName(t, last?.status)}</TD>
      <TD className="text-xs text-muted">{formatDate(t, turn.runs[0]?.start)}</TD>
      <TD>{formatNumber(t, tokensOf(turn.usage))}</TD>
      <TD>{turn.cost_usd == null ? "-" : formatUsd(t, turn.cost_usd)}</TD>
      <TD>
        <Button size="sm" variant="ghost" data-testid={`session-turn-${turn.turn_id}-trace`} onClick={onOpen}>
          {text.openTrace}
        </Button>
      </TD>
      {copy !== undefined ? (
        <TD>
          {copy ? (
            <Button size="sm" variant="outline" data-testid={`session-turn-${turn.turn_id}-copy`} onClick={copy}>
              {t.agents.evaluation.copy.action}
            </Button>
          ) : null}
        </TD>
      ) : null}
    </TR>
  );
}

function SessionConversation({
  agentId,
  detail,
  pending,
  focusTurn,
  features,
}: {
  agentId: string;
  detail: AgentSessionDetail;
  pending: AgentPending | undefined;
  focusTurn: string | null;
  features: ChatFeatures;
}) {
  const t = useT();
  const text = t.agents.sessions;
  const { session } = detail;
  // 번호 표가 출처를 든 카드 객체로 다시 만들지를 정하므로 상세가 그대로면 같은 항목을 쓴다
  const items = useMemo(() => sessionItems(detail, pending), [detail, pending]);
  const chat = useMemo(() => chatBase(agentId), [agentId]);
  const focusAt = focusTurn ? items.findIndex((it) => it.kind === "done" && it.turn_id === focusTurn) : -1;
  const focusRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // jsdom 에는 scrollIntoView 가 없다
    focusRef.current?.scrollIntoView?.({ block: "center" });
  }, [focusAt]);
  return (
    <>
      <div className="flex flex-wrap gap-2 text-xs text-muted">
        <Badge variant="muted">{text.turns(session.turns)}</Badge>
        <Badge variant="muted">{text.input(session.usage.input_tokens)}</Badge>
        <Badge variant="muted">{text.output(session.usage.output_tokens)}</Badge>
        <Badge variant={session.status === "pending" ? "warn" : "muted"}>{session.status}</Badge>
      </div>
      <div className="flex flex-col gap-2" data-testid="session-messages">
        <CitationScope items={items} chat={chat} sessionId={session.session_id} features={features}>
          {items.length ? (
            items.map((it, i) =>
              i === focusAt ? (
                <div key={i} ref={focusRef} className="rounded-chip border border-accent-line bg-accent-soft px-1" data-testid="session-focused-turn">
                  <ChatItemView index={i} item={it} />
                </div>
              ) : (
                <ChatItemView key={i} index={i} item={it} />
              ),
            )
          ) : (
            <p className="text-sm text-muted">{text.noMessages}</p>
          )}
        </CitationScope>
      </div>
      {detail.pending ? (
        <p className="text-xs text-muted" data-testid="session-pending-note">
          {text.decideInInbox}
        </p>
      ) : null}
    </>
  );
}

/**
 * 세션 상세를 채팅 항목으로 옮긴다. 전사를 채팅 복원과 같은 규칙으로 되살리고, 보류 중이면 살아 있는 보류로 마지막 승인
 * 카드를 다시 세운다(ChatPanel 의 복원과 같다).
 */
function sessionItems(detail: AgentSessionDetail, pending: AgentPending | undefined): ChatItem[] {
  const items = itemsFromTranscript(detail.transcript?.items);
  return detail.pending && pending ? reopenPending(items, pending) : items;
}
