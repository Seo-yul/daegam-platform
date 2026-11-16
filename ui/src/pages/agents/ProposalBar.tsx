/**
 * 청지기의 제안을 연 캔버스의 안내 줄 (P48 6.5절). 캔버스 머리 아래에 선다.
 *
 *  - 불러온 제안: 저장하기 전에는 Agent 가 바뀌지 않는다는 말, 그 뒤에 새 리비전이 저장됐으면 어느 저장도 들어가지 않는다는
 *    말, 지우는 노드 목록(캔버스에는 없으므로 여기 적는다)과 "제안대로 저장". 사람이 고친 판은 캔버스의 저장 버튼이 저장한다.
 *  - 불러오기를 물었을 때 사람이 거절했거나 다른 내용을 대신 올렸으면 다시 불러오는 단추만 둔다.
 *  - 닫힌 제안: 그렇다는 말과, 같은 대상의 나중 제안이 기다리면 그것을 불러오는 단추.
 *  - 대상을 볼 수 없으면 그렇다는 말만 하고 아무것도 올리지 않는다.
 */
import { NodeRow } from "@/components/assistant/ProposalSummary";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";

import type { ProposalState } from "./useCanvasProposal";

export interface ProposalBarProps {
  state: ProposalState;
  /** 열린 제안이 캔버스에 올라 있다. */
  shown: boolean;
  /** 열린 제안을 사람이 거절했거나 다른 내용을 대신 올렸다. 다시 불러오는 단추를 보인다. */
  declined: boolean;
  onApprove: () => void;
  approving: boolean;
  approveError: unknown;
  /** 열린 제안을 다시 불러온다. */
  onLoad: () => void;
  /** 같은 대상의 나중 제안을 불러온다. */
  onLoadLatest: () => void;
}

export function ProposalBar({ state, shown, declined, onApprove, approving, approveError, onLoad, onLoadLatest }: ProposalBarProps) {
  const t = useT();
  const text = t.agents.canvasPage.notice;
  switch (state.kind) {
    case "none":
    case "loading":
      return null;
    case "error":
      return <ErrorMessage error={state.error} testId="canvas-proposal-error" />;
    case "unavailable":
      return (
        <Alert variant="warn" data-testid="canvas-proposal" data-state="unavailable">
          {text.proposalUnavailable}
        </Alert>
      );
    case "closed":
      return (
        <Alert variant="info" data-testid="canvas-proposal" data-state="closed" className="flex flex-wrap items-center justify-between gap-2">
          <span>{text.proposalClosed}</span>
          {state.latest ? (
            <Button size="sm" variant="outline" data-testid="canvas-proposal-latest" onClick={onLoadLatest}>
              {text.loadLatestProposal}
            </Button>
          ) : null}
        </Alert>
      );
    case "open":
      if (!shown) {
        return declined ? (
          <Button size="sm" variant="outline" className="self-start" data-testid="canvas-proposal-load" onClick={onLoad}>
            {text.loadLatestProposal}
          </Button>
        ) : null;
      }
      return (
        <div className="flex flex-col gap-2">
          <Alert variant={state.stale ? "warn" : "info"} data-testid="canvas-proposal" data-state={state.stale ? "stale" : "open"} className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <span>{text.proposalLoaded}</span>
              {state.stale && state.view.latest_revision != null ? <span data-testid="canvas-proposal-stale">{text.proposalStale(state.view.latest_revision)}</span> : null}
              {state.view.proposal.changes.nodes.removed.length ? (
                <div className="flex flex-col gap-0.5" data-testid="canvas-proposal-removed">
                  <span className="text-xs font-semibold">{t.canvas.proposalMarks.removedTitle}</span>
                  <ul className="flex flex-wrap gap-x-3 gap-y-0.5">
                    {state.view.proposal.changes.nodes.removed.map((n) => (
                      <NodeRow key={n.id} node={n} testId={`canvas-proposal-removed-${n.id}`} />
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
            <Button size="sm" data-testid="canvas-proposal-approve" disabled={approving} onClick={onApprove}>
              {t.shared.approvalCard.proposal.approve}
            </Button>
          </Alert>
          <ErrorMessage error={approveError} testId="canvas-proposal-approve-error" />
        </div>
      );
  }
}
