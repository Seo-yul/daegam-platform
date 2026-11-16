/**
 * 청지기의 제안을 연 캔버스 (P48 6.5절). 주소의 proposal·session 으로 `GET /assistant/sessions/{id}/proposal` 을 읽어
 * 그 제안이 이 캔버스의 것인지, 닫혔는지(같은 대상의 나중 제안이 기다리는지), 대상 Agent 를 볼 수 없는지를 가른다.
 * 열면 그 세션을 패널에서 고르고 패널을 연다.
 *
 * 주소가 제안을 가리키는 동안 그 세션의 스트림 이벤트를 듣는다(relay.ts). 패널에서 다듬어 새 제안이 오거나 턴이 끝나면
 * 제안을 다시 읽어 닫힌 제안과 새 제안을 알아본다. 읽는 쪽이 스트림을 그만 읽은 때(null)와 패널의 폴링이 턴의 끝을 본
 * 때도 그렇다. 승인한 호출의 결과는 그 턴 안에서만 기다린다. 턴이 끝나면 잊어, 같은 id 로 되풀이된 뒤 호출의 성공을
 * 이 캔버스의 승인으로 읽지 않는다.
 *
 * 결정 둘을 여기서 보낸다. "제안대로 저장" 은 지문을 실은 승인이고, 사람이 직접 저장한 뒤에는 저장한 리비전(새 Agent 면
 * 그 id)을 적은 고정 사유와 `canvas_saved` 로 거절해 청지기가 그 저장에서 이어 가게 한다. 결정이 다시 연 턴은 옆 패널이
 * 그린다. 캔버스에 올리는 일과 사람의 저장은 페이지와 useProposalContent 가 한다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { getAssistantProposal, getAssistantSession } from "@/api/resources";
import type { AssistantPendingState, AssistantProposalView } from "@/api/types";
import {
  PROPOSAL_PARAM,
  buildDecisions,
  canvasSaveReason,
  openAssistantSession,
  proposalIdOf,
  proposalLink,
  proposalTarget,
  savedAgentOf,
  sendAssistantDecisions,
  subscribeAssistantEvents,
} from "@/lib/assistant";
import type { ToolResultEvent } from "@/lib/chatStream";
import { assistantProposalKey } from "@/lib/queryKeys";

export type ProposalState =
  /** 제안을 연 캔버스가 아니다. */
  | { kind: "none" }
  | { kind: "loading" }
  /** 주소의 제안이 기다린다. stale 은 그 뒤에 Agent 의 새 리비전이 저장됐다는 뜻이다. */
  | { kind: "open"; view: AssistantProposalView; stale: boolean }
  /** 주소의 제안은 닫혔다. latest 는 같은 대상의 나중 제안 id 이고 없으면 null 이다. */
  | { kind: "closed"; latest: string | null }
  /** 대상 Agent 를 찾을 수 없거나 볼 수 없다. 아무것도 올리지 않는다. */
  | { kind: "unavailable" }
  | { kind: "error"; error: unknown };

/** 기다리는 제안이 없다는 답. 세션이 사라졌거나 비서가 꺼졌어도 그 제안은 닫힌 것이다. */
const CLOSED_CODES = new Set(["proposal_not_found", "session_not_found", "assistant_disabled"]);

/** 결정의 대상인 턴의 끝. 이것이 오면 제안을 다시 읽는다. */
const TURN_ENDS = new Set(["interrupt", "done", "error"]);

type WaitingCall = AssistantPendingState["calls"][number];

/** 세션을 다시 읽어 이 제안을 기다리는 호출을 찾는다. 결정의 id 와 지문이 거기 있다. 없으면 제안이 닫힌 것이다. */
async function waitingCall(sessionId: string, proposalId: string): Promise<WaitingCall | null> {
  const detail = await getAssistantSession(sessionId);
  if (detail.pending?.kind !== "confirm") return null;
  return detail.pending.calls.find((c) => proposalIdOf(c.args) === proposalId) ?? null;
}

export interface CanvasProposal {
  state: ProposalState;
  /** "제안대로 저장". 승인이 들어가면 제안을 결정한 것으로 보고 표시를 거둔다. */
  approve: () => void;
  approving: boolean;
  approveError: unknown;
  /** 같은 대상의 나중 제안으로 주소를 바꾼다. */
  loadLatest: () => void;
  /** 사람이 이 제안 위에서 직접 저장한 뒤 부른다. 호출을 닫지 못해도 사람의 저장은 그대로다. */
  closeAfterOwnSave: (proposalId: string, saved: { revision: number; agentId?: string }) => void;
}

/**
 * @param target 이 캔버스의 대상(`canvasTarget`). 제안이 이 대상의 것일 때만 열고, 같은 세션의 나중 제안도 이 대상의 것일 때만
 * 불러오자고 한다.
 * @param onApproved 제안대로 저장한 결과. 새 Agent 면 그 id 로 옮기고 바꾸기면 저장된 리비전을 다시 올린다.
 */
export function useCanvasProposal({ tenant, target, onApproved }: { tenant: string; target: string; onApproved: (saved: { id: string; revision: number }) => void }): CanvasProposal {
  const location = useLocation();
  const [, setSearch] = useSearchParams();
  const queryClient = useQueryClient();
  const link = useMemo(() => proposalLink(location.search), [location.search]);
  const sessionId = link?.sessionId ?? null;
  // 결정했거나 사람이 직접 저장한 제안. 그 뒤에는 표시와 두 저장을 거둔다
  const [handled, setHandled] = useState<string | null>(null);
  // 승인을 보낸 호출. 그 성공 결과에서 저장된 것을 안다
  const approvedCall = useRef<string | null>(null);
  const onApprovedRef = useRef(onApproved);
  onApprovedRef.current = onApproved;

  // 캔버스와 그 대화를 함께 보게 그 세션을 패널에서 고르고 연다
  useEffect(() => {
    if (sessionId) openAssistantSession(tenant, sessionId);
  }, [tenant, sessionId]);

  const query = useQuery({
    queryKey: assistantProposalKey(tenant, sessionId ?? "", link?.proposalId ?? ""),
    queryFn: () => getAssistantProposal(sessionId as string),
    enabled: Boolean(sessionId),
    retry: false,
  });

  // 그 세션의 스트림을 듣는다. 패널에서 다듬은 새 제안, 결정이 다시 연 턴의 끝, 끝까지 읽지 못한 스트림 뒤에 제안을 다시 읽는다
  useEffect(() => {
    if (!sessionId) return;
    return subscribeAssistantEvents((sid, ev) => {
      if (sid !== sessionId) return;
      if (ev?.event === "tool_result") {
        const d = ev.data as ToolResultEvent;
        const saved = d.id === approvedCall.current && d.status === "success" ? savedAgentOf(d.content) : null;
        if (saved) {
          approvedCall.current = null;
          onApprovedRef.current(saved);
        }
        return;
      }
      if (ev === null || TURN_ENDS.has(ev.event)) {
        // 결정한 턴이 끝났다. 그 뒤 같은 id 의 호출(provider 가 id 를 되풀이한다)이 패널 카드에서 승인돼도 캔버스는 다시 올리지 않는다
        approvedCall.current = null;
        void queryClient.invalidateQueries({ queryKey: assistantProposalKey(tenant, sid) });
      }
    });
  }, [queryClient, tenant, sessionId]);

  const state = useMemo<ProposalState>(() => {
    if (!link) return { kind: "none" };
    if (query.error) {
      const e = query.error;
      if (e instanceof ApiError && e.status === 404 && CLOSED_CODES.has(e.code)) return handled === link.proposalId ? { kind: "none" } : { kind: "closed", latest: null };
      if (e instanceof ApiError && (e.status === 403 || e.status === 404)) return { kind: "unavailable" };
      return { kind: "error", error: e };
    }
    const view = query.data;
    if (!view) return { kind: "loading" };
    const ours = proposalTarget(view.proposal) === target;
    // 주소의 제안이 다른 대상의 것이다(다른 Agent 의 캔버스에 붙인 주소). 이 캔버스에서는 볼 수 없는 제안이다
    if (view.proposal_id === link.proposalId && !ours) return { kind: "unavailable" };
    if (view.proposal_id !== link.proposalId) {
      const latest = ours ? view.proposal_id : null;
      return handled === link.proposalId && !latest ? { kind: "none" } : { kind: "closed", latest };
    }
    if (handled === link.proposalId) return { kind: "none" };
    const stale = view.kind === "update" && view.base_revision != null && view.latest_revision != null && view.latest_revision !== view.base_revision;
    return { kind: "open", view, stale };
  }, [link, query.error, query.data, target, handled]);

  const approveMutation = useMutation({
    mutationFn: async () => {
      if (!link) return;
      const call = await waitingCall(link.sessionId, link.proposalId);
      if (!call) {
        // 그 사이 닫혔다. 다시 읽으면 닫힌 제안으로 보인다
        await query.refetch();
        return;
      }
      approvedCall.current = call.id;
      await sendAssistantDecisions(queryClient, link.sessionId, { decisions: buildDecisions([{ type: "approve" }], [call.id], [call]) });
      setHandled(link.proposalId);
    },
    onError: () => {
      approvedCall.current = null;
      // 409 proposal_changed 처럼 그 사이 제안이 바뀌었을 수 있다. 다시 읽어 지금 상태를 보인다
      void query.refetch();
    },
  });

  const closeAfterOwnSave = useCallback(
    (proposalId: string, saved: { revision: number; agentId?: string }) => {
      if (!sessionId) return;
      setHandled(proposalId);
      void (async () => {
        try {
          const call = await waitingCall(sessionId, proposalId);
          if (!call) return;
          const decisions = buildDecisions([{ type: "reject", message: canvasSaveReason(saved) }], [call.id], [call]).map((d) => ({ ...d, canvas_saved: true }));
          await sendAssistantDecisions(queryClient, sessionId, { decisions });
        } catch {
          // 사람의 저장은 그대로다. 호출이 열린 채 남으면 나중 승인은 기준 리비전이 지나 409 revision_conflict 로 아무것도
          // 저장하지 않는다 (P48 6.5절). 대화는 옆 패널에서 이어 간다
        }
      })();
    },
    [queryClient, sessionId],
  );

  const latest = state.kind === "closed" ? state.latest : null;
  const loadLatest = useCallback(() => {
    if (!latest) return;
    setSearch(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(PROPOSAL_PARAM, latest);
        return next;
      },
      { replace: true },
    );
  }, [latest, setSearch]);

  return { state, approve: () => approveMutation.mutate(), approving: approveMutation.isPending, approveError: approveMutation.error, loadLatest, closeAfterOwnSave };
}
