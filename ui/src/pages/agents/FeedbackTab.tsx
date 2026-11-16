/**
 * Agent 상세의 피드백 탭 (P36 6절). 사용자가 답변마다 남긴 평가를 새것부터 모아 보고 NDJSON 으로 내보낸다.
 *
 * 남의 평가와 의견을 읽는 자리라 남의 대화를 읽는 승인자(`agent:approve`)만 본다. 권한이 없으면 목록을 부르지 않고 그 사실을
 * 말한다. 피드백은 플랫폼에 남으므로 배포 여부와 상관없이 열리고, 런타임이 올라가 있을 때만(`sessionsOpen`) 행이 세션 탭의
 * 그 답변으로 이어진다. 주소는 세션 탭의 `sessionTurnSearch` 가 만들고 세션 탭이 그 대화를 열어 답변으로 내려간다.
 *
 * 머리에는 지난 7일(`FEEDBACK_RECENT_DAYS`)의 평가별 수가 있고 거르개(평가·이유)와 상관없다. 다시 생성으로 바뀐 답변의 평가도
 * 지우지 않고 표시를 달아 남긴다. 내보내기는 지금 거르개의 평가로 지난 90일(`FEEDBACK_EXPORT_MAX_DAYS`)을 받는다. 사람 식별자는
 * 파일에 없다.
 *
 * 대화한 사람이 평가 사례로 쓰는 데 동의한 평가(`evaluation_use`, 다시 생성으로 걷히지 않은 것)의 줄에는 "평가 사례로 복사" 가
 * 선다(P45 8절). 사례를 쓰는 권한(`agent:write`)이 있어야 하고, 누르면 복사 창이 그 턴의 질문과 답을 세트에 넣을 사례로 받는다.
 */
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { exportAgentFeedback, type AgentFeedbackFilter } from "@/api/agentChat";
import type { AgentFeedback, FeedbackRating, FeedbackTag } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { hasScope, isAuthReady, useAuthState } from "@/lib/auth";
import { copyableRating } from "@/lib/evaluation";
import { FEEDBACK_EXPORT_MAX_DAYS, FEEDBACK_RECENT_DAYS, FEEDBACK_TAGS } from "@/lib/feedback";
import { useTenant } from "@/lib/tenant";
import { cn, formatDate } from "@/lib/utils";

import { CopyCaseDialog } from "./EvaluationCaseEditor";
import { sessionTurnSearch } from "./SessionsTab";
import { useAgentFeedbackList } from "./useFeedbackList";

/** 표 칸 수다. 비었거나 불러오는 줄이 표 폭을 채운다. */
const FEEDBACK_COLUMNS = 6;
const APPROVE_SCOPE = "agent:approve";
const WRITE_SCOPE = "agent:write";

export function FeedbackTab({ agentId, sessionsOpen }: { agentId: string; sessionsOpen: boolean }) {
  const t = useT();
  const auth = useAuthState();
  const tenant = useTenant();
  if (!isAuthReady(auth)) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (!hasScope(auth.me, tenant, APPROVE_SCOPE)) {
    return (
      <Card data-testid="feedback-card">
        <CardContent className="py-4 text-sm text-muted" data-testid="feedback-forbidden">
          {t.agents.feedback.forbidden}
        </CardContent>
      </Card>
    );
  }
  return <FeedbackList agentId={agentId} sessionsOpen={sessionsOpen} canCopy={hasScope(auth.me, tenant, WRITE_SCOPE)} />;
}

function FeedbackList({ agentId, sessionsOpen, canCopy }: { agentId: string; sessionsOpen: boolean; canCopy: boolean }) {
  const t = useT();
  const text = t.agents.feedback;
  const rating = t.chat.feedback.rating;
  const navigate = useNavigate();
  const [filter, setFilter] = useState<AgentFeedbackFilter>({});
  const [copying, setCopying] = useState<AgentFeedback | null>(null);
  const list = useAgentFeedbackList(agentId, filter);
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
  const recent = list.data?.pages[0]?.last_7_days;
  const openTurn = sessionsOpen ? (f: AgentFeedback) => void navigate({ search: sessionTurnSearch(f.session_id, f.turn_id) }) : undefined;

  return (
    <Card data-testid="feedback-card">
      <CardHeader className="flex-wrap">
        <CardTitle>{t.agents.tabs.feedback}</CardTitle>
        {recent ? (
          <span className="text-xs text-muted" data-testid="feedback-recent">
            {text.recent(FEEDBACK_RECENT_DAYS, rating.up, recent.up, rating.down, recent.down)}
          </span>
        ) : null}
        <FeedbackFilters filter={filter} onChange={setFilter} />
        <ExportButton agentId={agentId} rating={filter.rating} />
      </CardHeader>
      <ErrorMessage error={list.error} />
      <Table data-testid="feedback-table">
        <THead>
          <tr>
            <TH>{text.columns.time}</TH>
            <TH>{text.columns.user}</TH>
            <TH>{text.columns.rating}</TH>
            <TH>{text.columns.tags}</TH>
            <TH>{text.columns.comment}</TH>
            <TH>
              <span className="sr-only">{t.agents.evaluation.copy.action}</span>
            </TH>
          </tr>
        </THead>
        <TBody>
          {list.isLoading ? (
            <EmptyRow colSpan={FEEDBACK_COLUMNS}>{t.common.loading}</EmptyRow>
          ) : rows.length ? (
            rows.map((f) => <FeedbackRow key={f.id} feedback={f} onOpen={openTurn} onCopy={canCopy && copyableRating(f) ? () => setCopying(f) : undefined} />)
          ) : (
            <EmptyRow colSpan={FEEDBACK_COLUMNS}>{filter.rating || filter.tag ? text.noMatch : text.empty}</EmptyRow>
          )}
        </TBody>
      </Table>
      {list.hasNextPage ? (
        <div className="flex justify-center border-t border-line px-4 py-2">
          <Button size="sm" variant="outline" data-testid="feedback-more" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
            {list.isFetchingNextPage ? text.loadingMore : text.more}
          </Button>
        </div>
      ) : null}
      {copying ? <CopyCaseDialog agentId={agentId} sessionId={copying.session_id} turnId={copying.turn_id} onClose={() => setCopying(null)} /> : null}
    </Card>
  );
}

function FeedbackFilters({ filter, onChange }: { filter: AgentFeedbackFilter; onChange: (next: AgentFeedbackFilter) => void }) {
  const t = useT();
  const text = t.agents.feedback;
  const labels = t.chat.feedback;
  return (
    <div className="ml-auto flex gap-2">
      <Select aria-label={text.ratingFilter} data-testid="feedback-rating" className="h-8 w-32 text-xs" value={filter.rating ?? ""} onChange={(e) => onChange({ ...filter, rating: (e.target.value || undefined) as FeedbackRating | undefined })}>
        <option value="">{text.allRatings}</option>
        <option value="up">{labels.rating.up}</option>
        <option value="down">{labels.rating.down}</option>
      </Select>
      <Select aria-label={text.tagFilter} data-testid="feedback-tag" className="h-8 w-48 text-xs" value={filter.tag ?? ""} onChange={(e) => onChange({ ...filter, tag: (e.target.value || undefined) as FeedbackTag | undefined })}>
        <option value="">{text.allTags}</option>
        {FEEDBACK_TAGS.map((tag) => (
          <option key={tag} value={tag}>
            {labels.tags[tag]}
          </option>
        ))}
      </Select>
    </div>
  );
}

/** 지금 거르개의 평가로 지난 90일을 받는다. 서버의 내보내기는 이유로 거르지 않는다. 실패하면 단추 옆에 오류를 보인다. */
function ExportButton({ agentId, rating }: { agentId: string; rating?: FeedbackRating }) {
  const t = useT();
  const text = t.agents.feedback;
  const run = useMutation({ mutationFn: () => exportAgentFeedback(agentId, { rating }) });
  return (
    <>
      <Button size="sm" variant="outline" data-testid="feedback-export" title={text.exportHint(FEEDBACK_EXPORT_MAX_DAYS)} disabled={run.isPending} onClick={() => run.mutate()}>
        {run.isPending ? text.exporting : t.shared.export}
      </Button>
      {run.error ? (
        <div className="basis-full">
          <ErrorMessage error={run.error} testId="feedback-export-error" />
        </div>
      ) : null}
    </>
  );
}

function FeedbackRow({ feedback: f, onOpen, onCopy }: { feedback: AgentFeedback; onOpen?: (f: AgentFeedback) => void; onCopy?: () => void }) {
  const t = useT();
  return (
    <TR data-testid={`feedback-row-${f.id}`} className={cn(onOpen && "cursor-pointer")} onClick={onOpen ? () => onOpen(f) : undefined}>
      <TD className="text-xs text-muted">
        {onOpen ? (
          <button
            type="button"
            className="text-left underline-offset-2 hover:underline"
            data-testid={`feedback-open-${f.id}`}
            onClick={(e) => {
              // 행도 같은 일을 하므로 두 번 옮기지 않게 여기서 멈춘다
              e.stopPropagation();
              onOpen(f);
            }}
          >
            {formatDate(t, f.created_at)}
          </button>
        ) : (
          formatDate(t, f.created_at)
        )}
      </TD>
      <TD className="text-xs">{f.user_sub}</TD>
      <TD>
        <span className="flex flex-wrap items-center gap-1">
          <Badge variant={f.rating === "up" ? "ok" : "warn"}>{t.chat.feedback.rating[f.rating]}</Badge>
          {f.superseded ? <Badge variant="muted">{t.chat.feedback.superseded}</Badge> : null}
        </span>
      </TD>
      <TD className="text-xs">{f.tags.map((tag) => t.chat.feedback.tags[tag]).join(", ") || "-"}</TD>
      <TD className="max-w-80 text-xs break-words whitespace-pre-wrap">{f.comment ?? <span className="text-muted">-</span>}</TD>
      <TD className="text-right">
        {onCopy ? (
          <Button
            size="sm"
            variant="outline"
            data-testid={`feedback-copy-${f.id}`}
            onClick={(e) => {
              // 행은 세션 탭으로 옮기므로 여기서 멈춘다
              e.stopPropagation();
              onCopy();
            }}
          >
            {t.agents.evaluation.copy.action}
          </Button>
        ) : null}
      </TD>
    </TR>
  );
}
