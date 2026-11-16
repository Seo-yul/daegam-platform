/**
 * 승인 카드의 제안 몫 (P48 6.4절). 청지기의 Agent 정의 제안을 플랫폼이 기준 리비전과 비교한 덩어리 그대로 그린다.
 * 패널의 카드와 홈 챗의 카드가 같은 ApprovalCard 를 거쳐 이것을 쓰고, 스스로 비교하지 않는다.
 *
 * 차례: 제목, 배포된 리비전 안내, 저장하기 전에 확인할 점, 노드 수 요약, 바뀌는 것 목록(노드·간선·service·표시 이름),
 * 바뀌는 글의 줄 비교, 검증 경고, "캔버스에서 보기". 청지기의 말은 카드 밖 말풍선에 남는다.
 */
import { Link } from "react-router";

import type { AgentProposal, ProposalNode } from "@/api/types";
import type { ProposalMark } from "@/components/canvas/types";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { useT } from "@/i18n";
import { issueText } from "@/lib/agentGraph";
import { attentionText, edgeKindLabel, nodeTypeLabel, proposalCounts, proposalHref, proposalTitle, textFieldLabel } from "@/lib/assistant";
import { cn } from "@/lib/utils";

/** 표시의 색. 스펙 비교(`SpecDiff.tsx` `KIND_VARIANT`)와 같이 더한 것은 ok, 바꾼 것은 info 다. 캔버스의 노드 칩도 같다. */
export const MARK_VARIANT: Record<ProposalMark, BadgeProps["variant"]> = { added: "ok", changed: "info" };

/** 바뀌는 노드 한 줄. 카드의 목록과 캔버스 안내 줄의 지우는 노드 목록이 같이 쓴다. */
export function NodeRow({ node, mark, testId }: { node: ProposalNode; mark?: ProposalMark; testId: string }) {
  const t = useT();
  return (
    <li className="flex flex-wrap items-center gap-1.5 text-xs" data-testid={testId}>
      {mark ? <Badge variant={MARK_VARIANT[mark]}>{t.canvas.proposalMarks[mark]}</Badge> : null}
      <span className="text-muted">{nodeTypeLabel(t, node.type)}</span>
      <span className="font-mono text-heading">{node.id}</span>
      {node.name && node.name !== node.id ? <span className="text-fg">{node.name}</span> : null}
      {node.fields?.length ? <span className="font-mono text-[11px] text-muted">{node.fields.join(", ")}</span> : null}
    </li>
  );
}

export function ProposalSummary({ proposal, sessionId, resolved = false, testId = "proposal" }: { proposal: AgentProposal; sessionId: string; resolved?: boolean; testId?: string }) {
  const t = useT();
  const text = t.shared.approvalCard.proposal;
  const { changes, attention, warnings } = proposal;
  const counts = proposalCounts(proposal);
  // 결정한 카드의 제안은 이미 닫혔다. 열면 캔버스가 "닫힌 제안" 이라고만 말하므로 링크를 두지 않는다
  const href = !resolved && sessionId ? proposalHref(sessionId, proposal) : null;
  const cut = changes.texts.some((d) => d.cut);
  return (
    <div className="flex flex-col gap-2" data-testid={testId} data-proposal-id={proposal.proposal_id} data-kind={proposal.kind}>
      <span className="text-sm font-semibold text-heading" data-testid={`${testId}-title`}>
        {proposalTitle(t, proposal)}
      </span>
      {proposal.deployed_revision !== null ? (
        <p className="text-xs text-muted" data-testid={`${testId}-deployed`}>
          {text.deployed(proposal.deployed_revision)}
        </p>
      ) : null}
      {/* 먼저 볼 것이 맨 앞이다. 사람은 카드를 끝까지 읽지 않고 승인하기도 한다 (P48 12절) */}
      {attention.length ? (
        <div className="flex flex-col gap-1 rounded-item border border-warn-line bg-warn-soft px-3 py-2" data-testid={`${testId}-attention`}>
          <span className="text-xs font-semibold text-warn-fg">{text.attentionTitle}</span>
          <ul className="flex list-disc flex-col gap-0.5 pl-4 text-xs text-warn-fg">
            {attention.map((m, k) => (
              <li key={k} data-code={m.code}>
                {attentionText(t, m)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="text-xs text-fg" data-testid={`${testId}-summary`}>
        {text.summary(counts.added, counts.changed, counts.removed)}
      </p>
      <ul className="flex flex-col gap-1" data-testid={`${testId}-changes`}>
        {changes.nodes.added.map((n) => (
          <NodeRow key={`a-${n.id}`} node={n} mark="added" testId={`${testId}-added-${n.id}`} />
        ))}
        {changes.nodes.changed.map((n) => (
          <NodeRow key={`c-${n.id}`} node={n} mark="changed" testId={`${testId}-changed-${n.id}`} />
        ))}
        {changes.edges.added.map((e) => (
          <li key={`ea-${e.source}-${e.target}-${e.kind}`} className="font-mono text-[11px] text-ok-fg">
            + {edgeKindLabel(t, e.kind)} {e.source} → {e.target}
          </li>
        ))}
        {changes.edges.removed.map((e) => (
          <li key={`er-${e.source}-${e.target}-${e.kind}`} className="font-mono text-[11px] text-err-fg">
            − {edgeKindLabel(t, e.kind)} {e.source} → {e.target}
          </li>
        ))}
        {changes.service.length ? (
          <li className="flex flex-wrap gap-1" data-testid={`${testId}-service`}>
            {changes.service.map((f) => (
              <code key={f} className="rounded-badge border border-line bg-surface px-1.5 font-mono text-[11px] text-fg">{`service.${f}`}</code>
            ))}
          </li>
        ) : null}
        {changes.display_name ? (
          <li className="text-xs" data-testid={`${testId}-display-name`}>
            <span className="text-muted">{t.detail.nameFields.labels.display}</span> {changes.display_name.before ?? ""} → {changes.display_name.after ?? ""}
          </li>
        ) : null}
      </ul>
      {changes.nodes.removed.length ? (
        <div className="flex flex-col gap-1" data-testid={`${testId}-removed`}>
          <span className="text-xs font-semibold text-err-fg">{t.canvas.proposalMarks.removedTitle}</span>
          <ul className="flex flex-col gap-1">
            {changes.nodes.removed.map((n) => (
              <NodeRow key={`r-${n.id}`} node={n} testId={`${testId}-removed-${n.id}`} />
            ))}
          </ul>
        </div>
      ) : null}
      {changes.texts.length ? (
        <div className="flex flex-col gap-1.5" data-testid={`${testId}-texts`}>
          <span className="text-xs font-semibold text-heading">{text.textChanges}</span>
          {changes.texts.map((d) => (
            <div key={`${d.node_id}-${d.field}`} className="flex flex-col gap-0.5" data-testid={`${testId}-text-${d.node_id}-${d.field}`}>
              <span className="text-[11px] text-muted">
                <span className="font-mono">{d.node_id}</span> · {textFieldLabel(t, d.field)}
              </span>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-tile border border-line bg-sunken px-2.5 py-1.5 font-mono text-[11.5px]">
                {d.lines.map((l, k) => (
                  <span key={k} className={cn("block", l.op === "+" ? "text-ok-fg" : "text-err-fg")}>
                    {l.op} {l.text}
                  </span>
                ))}
              </pre>
            </div>
          ))}
          {cut ? (
            <p className="text-xs text-muted" data-testid={`${testId}-text-cut`}>
              {text.textCut}
            </p>
          ) : null}
        </div>
      ) : null}
      {warnings.length ? (
        <ul className="flex flex-col gap-0.5 text-xs text-warn-fg" data-testid={`${testId}-warnings`}>
          {warnings.map((w, k) => (
            <li key={k}>{issueText(t, { code: w.code, level: "warning", node_id: w.node_id, edge_id: null, ...(w.message ? { message: w.message } : {}) })}</li>
          ))}
        </ul>
      ) : null}
      {href ? (
        // 같은 캔버스에서 다른 제안을 여는 링크면 떠나기 확인을 건너뛴다. 캔버스가 고친 것이 있을 때 스스로 묻는다(unsavedGuard.ts)
        <Link to={href} className={cn(buttonVariants({ variant: "outline", size: "sm" }), "self-start")} data-testid={`${testId}-open-canvas`} data-unsaved-guard="same-path">
          {text.openCanvas}
        </Link>
      ) : null}
    </div>
  );
}
