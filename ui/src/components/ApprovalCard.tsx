/**
 * 승인 카드 (HITL). `interrupt` 이벤트나 `GET /pending` 의 action_requests 를 보이고 사람이 결정한다.
 * 결정은 요청마다 하나씩이고 개수가 action_requests 와 같아야 한다 (deepagents 규칙). 요청이 하나면 approve·reject
 * 는 바로 보내고 edit(인자 JSON 편집)·respond(메시지) 는 입력 뒤 보낸다. 여럿이면 줄마다 고르고 아래 버튼으로 한 번에 보낸다.
 *
 * 비서가 Agent 정의 제안의 인자에 넣는 예약 칸(`_proposal`, P48 6.1절)은 인자로 보이지 않는다. 제안 호출이면 인자 대신
 * 플랫폼이 비교한 제안 덩어리를 그리고(ProposalSummary, 6.4절), 승인 단추는 "제안대로 저장" 이며 거부 사유 칸이 늘 열려
 * 고칠 점을 적은 거부로 다시 제안을 받는다(6.6절).
 *
 * 이유가 `outside` 인 요청은 그 이유를 보인다(P43 4절). 플랫폼이 요청마다 기억할 수 있다고 알린 선택(`remember_options`)은
 * "다음부터 묻지 않고" 단추로 보이고, 그 결정에 `remember` 를 싣는다(P43 5절). 알리지 않았으면 단추가 없다.
 */
import { useState } from "react";

import type { AgentActionRequest, AgentDecision, AgentDecisionType, AgentPending, RememberChoice } from "@/api/types";
import { ProposalSummary } from "@/components/assistant/ProposalSummary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { statusCardClass } from "@/components/ui/card";
import { Textarea } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { visibleArgs } from "@/lib/assistant";

const DECISION_VARIANTS: Record<AgentDecisionType, "default" | "secondary" | "destructive" | "outline"> = { approve: "default", edit: "secondary", reject: "secondary", respond: "outline" };

/**
 * LangChain HITL 이 설명을 주지 않은 도구에 붙이는 영어 기본 문구다("Tool execution requires approval" 뒤에 이름과
 * 인자). 이름과 인자는 카드가 따로 보이므로 이 문구는 감춘다.
 */
const HITL_DEFAULT_DESCRIPTION = /^Tool execution requires approval/;

/** 결정을 만들지 못한 까닭. 문장은 그릴 때 사전에서 고르고, JSON 오류는 파서의 말(detail)을 싣는다. */
type DraftError = { code: Exclude<keyof Messages["shared"]["approvalCard"]["error"], "json"> } | { code: "json"; detail: string };

interface Draft {
  type: AgentDecisionType | null;
  /** approve·reject 를 다음부터 묻지 않게 기억하는가. */
  remember: boolean;
  argsText: string;
  message: string;
  error: DraftError | null;
}

const REMEMBER_LABEL = { approve: "rememberApprove", reject: "rememberReject" } as const satisfies Record<RememberChoice, keyof Messages["shared"]["approvalCard"]>;

function draftErrorText(t: Messages, error: DraftError): string {
  const text = t.shared.approvalCard.error;
  return error.code === "json" ? text.json(error.detail) : text[error.code];
}

function allowedFor(pending: AgentPending, req: AgentActionRequest): AgentDecisionType[] {
  return pending.review_configs.find((c) => c.action_name === req.name)?.allowed_decisions ?? ["approve", "reject"];
}

/**
 * 다른 요청이 같은 Tool 에 반대로 기억하려는가. 한 결정 안에서 같은 Tool 의 승인과 거부를 함께 기억하면 플랫폼이 거절하므로
 * (422 remember_refused) 반대쪽 단추를 막는다.
 */
function rememberConflicts(pending: AgentPending, drafts: Draft[], i: number, choice: RememberChoice): boolean {
  const name = pending.action_requests[i].name;
  return drafts.some((d, j) => j !== i && d.remember && d.type !== choice && pending.action_requests[j].name === name);
}

function buildDecision(req: AgentActionRequest, d: Draft): { decision?: AgentDecision; error?: DraftError } {
  switch (d.type) {
    case "approve":
      return { decision: { type: "approve", ...(d.remember ? { remember: "approve" } : {}) } };
    case "reject":
      return { decision: { type: "reject", ...(d.message.trim() ? { message: d.message.trim() } : {}), ...(d.remember ? { remember: "reject" } : {}) } };
    case "respond":
      if (!d.message.trim()) return { error: { code: "messageRequired" } };
      return { decision: { type: "respond", message: d.message.trim() } };
    case "edit": {
      try {
        const args: unknown = JSON.parse(d.argsText);
        if (!args || typeof args !== "object" || Array.isArray(args)) return { error: { code: "argsNotObject" } };
        return { decision: { type: "edit", edited_action: { name: req.name, args: args as Record<string, unknown> } } };
      } catch (e) {
        return { error: { code: "json", detail: (e as Error).message } };
      }
    }
    default:
      return { error: { code: "noDecision" } };
  }
}

export function ApprovalCard({ pending, onSubmit, disabled = false, resolved = false, testId = "approval-card" }: { pending: AgentPending; onSubmit: (decisions: AgentDecision[]) => void; disabled?: boolean; resolved?: boolean; testId?: string }) {
  const t = useT();
  const text = t.shared.approvalCard;
  const [drafts, setDrafts] = useState<Draft[]>(() => pending.action_requests.map((r) => ({ type: null, remember: false, argsText: JSON.stringify(visibleArgs(r.args), null, 2), message: "", error: null })));
  const single = pending.action_requests.length === 1;

  function setDraft(i: number, patch: Partial<Draft>) {
    setDrafts((prev) => prev.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  }

  function submit(all: Draft[]) {
    const decisions: AgentDecision[] = [];
    const next = [...all];
    let failed = false;
    pending.action_requests.forEach((req, i) => {
      const r = buildDecision(req, all[i]);
      if (r.decision) decisions.push(r.decision);
      else {
        failed = true;
        next[i] = { ...all[i], error: r.error ?? null };
      }
    });
    if (failed) {
      setDrafts(next);
      return;
    }
    onSubmit(decisions);
  }

  function choose(i: number, type: AgentDecisionType, remember = false) {
    const next = drafts.map((d, j) => (j === i ? { ...d, type, remember, error: null } : d));
    setDrafts(next);
    // 요청 하나에 approve·reject 는 바로 보낸다. edit·respond 는 입력이 필요하다
    if (single && (type === "approve" || type === "reject")) submit(next);
  }

  const ready = drafts.every((d) => d.type !== null);
  // 요청마다 기억할 수 있는 선택이다. 플랫폼이 그 요청이 받는 결정과 겹친 것만 알린다
  const remember: RememberChoice[][] = pending.action_requests.map((_, i) => pending.remember_options?.[i] ?? []);
  const rememberDays = pending.remember_days;
  const offersRemember = !resolved && rememberDays !== undefined && remember.some((choices) => choices.length > 0);

  return (
    <div
      className={statusCardClass("warn", resolved)}
      data-testid={testId}
      data-resolved={resolved ? "true" : undefined}
    >
      <div className="flex items-center gap-2">
        {resolved ? (
          <>
            <Badge variant="muted">{text.decided}</Badge>
            <span className="text-sm font-semibold text-heading">{text.decidedCount(pending.action_requests.length)}</span>
          </>
        ) : (
          <>
            <Badge variant="warn">{text.needed}</Badge>
            <span className="text-sm font-semibold text-heading">{text.waitingCount(pending.action_requests.length)}</span>
          </>
        )}
        <span className="ml-auto font-mono text-[10.5px] text-muted">{pending.session_id}</span>
      </div>
      {pending.action_requests.map((req, i) => {
        const d = drafts[i];
        const allowed = allowedFor(pending, req);
        const suffix = single ? "" : `-${i}`;
        const proposal = req.proposal;
        // 제안은 고칠 점을 적어 거부하는 것이 다듬는 길이라(P48 6.6절) 사유 칸을 단추보다 먼저 늘 연다
        const messageOpen = !resolved && (proposal ? allowed.includes("reject") : (d.type === "respond" || d.type === "reject") && !(single && d.type === "reject"));
        // 사유 칸은 하나다. 제안이면 단추 앞에 고칠 점을, 아니면 고른 결정 아래에 메시지나 거부 사유를 받는다
        const placeholder = proposal ? text.proposal.rejectPlaceholder : d.type === "respond" ? text.respondPlaceholder : text.rejectPlaceholder;
        const message = <Textarea aria-label={text.message} data-testid={`decision-message${suffix}`} rows={2} className="font-sans" placeholder={placeholder} value={d.message} onChange={(e) => setDraft(i, { message: e.target.value, error: null })} />;
        return (
          <div key={i} className="flex flex-col gap-2 rounded-tile border border-line bg-surface-strong p-3" data-testid={`approval-request-${i}`}>
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-semibold text-heading">{req.name}</span>
              {req.description && !HITL_DEFAULT_DESCRIPTION.test(req.description) ? <span className="text-xs text-muted">{req.description}</span> : null}
            </div>
            {req.reason === "outside" ? (
              <div className="flex flex-col gap-0.5" data-testid={`approval-reason-${i}`}>
                <span className="text-xs font-semibold text-warn-fg">{text.outsideReason}</span>
                <span className="text-xs text-muted">{text.newChatHint}</span>
              </div>
            ) : null}
            {req.files?.length ? (
              <div className="flex flex-col gap-1 rounded-item border border-line bg-surface px-3 py-2" data-testid={`approval-files-${i}`}>
                <span className="text-xs font-semibold text-warn-fg">{text.externalFiles}</span>
                {req.files.map((f) => (
                  <span key={f.name} className="truncate font-mono text-[11.5px] text-muted" title={f.url ?? undefined}>
                    {f.name}
                    {f.url ? ` ← ${f.url}` : ""}
                  </span>
                ))}
              </div>
            ) : null}
            {proposal ? (
              <ProposalSummary proposal={proposal} sessionId={pending.session_id} resolved={resolved} testId={`approval-proposal-${i}`} />
            ) : (
              <pre className="max-h-40 overflow-auto rounded-tile border border-line bg-sunken px-3 py-2 font-mono text-[12px] text-heading" data-testid={`approval-args-${i}`}>
                {JSON.stringify(visibleArgs(req.args), null, 2)}
              </pre>
            )}
            {messageOpen && proposal ? message : null}
            {!resolved ? (
              <div className="flex flex-wrap gap-2">
                {allowed.map((choice) => {
                  const pressed = d.type === choice && !d.remember;
                  return (
                    <Button key={choice} size="sm" variant={pressed ? "default" : DECISION_VARIANTS[choice]} data-testid={`decision-${choice}${suffix}`} disabled={disabled} aria-pressed={pressed} onClick={() => choose(i, choice)}>
                      {proposal && choice === "approve" ? text.proposal.approve : text.decision[choice]}
                    </Button>
                  );
                })}
                {remember[i].map((choice) => {
                  const pressed = d.type === choice && d.remember;
                  return (
                    <Button key={`remember-${choice}`} size="sm" variant={pressed ? "default" : "outline"} data-testid={`decision-remember-${choice}${suffix}`} disabled={disabled || rememberConflicts(pending, drafts, i, choice)} aria-pressed={pressed} onClick={() => choose(i, choice, true)}>
                      {text[REMEMBER_LABEL[choice]]}
                    </Button>
                  );
                })}
              </div>
            ) : null}
            {!resolved && d.type === "edit" ? (
              <div className="flex flex-col gap-1">
                <Textarea aria-label={text.editArgs} data-testid={`decision-edit-args${suffix}`} rows={5} value={d.argsText} onChange={(e) => setDraft(i, { argsText: e.target.value, error: null })} />
                <p className="text-xs text-muted">{text.editHint}</p>
              </div>
            ) : null}
            {messageOpen && !proposal ? message : null}
            {d.error ? (
              <p className="text-xs text-err-fg" data-testid={`decision-error${suffix}`}>
                {draftErrorText(t, d.error)}
              </p>
            ) : null}
          </div>
        );
      })}
      {offersRemember ? (
        <p className="text-xs text-muted" data-testid="approval-remember-hint">
          {text.rememberHint(rememberDays)}
        </p>
      ) : null}
      {!resolved && (!single || drafts[0]?.type === "edit" || drafts[0]?.type === "respond") ? (
        <div className="flex justify-end">
          <Button size="sm" data-testid="decision-submit" disabled={disabled || !ready} onClick={() => submit(drafts)}>
            {text.submit}
          </Button>
        </div>
      ) : null}
      {resolved ? <p className="text-xs text-muted">{text.sent}</p> : null}
    </div>
  );
}
