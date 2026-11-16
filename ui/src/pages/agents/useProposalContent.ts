/**
 * 캔버스에 올린 청지기의 제안 (P48 6.5절). 열린 제안을 기준 위의 저장하지 않은 편집으로 올리고, 무엇을 올렸는지 기억한다.
 *
 * 올린 내용은 제안이 닫히거나 결정된 뒤에도 캔버스에 남으므로 기억도 그 내용이 바뀔 때까지 남는다. 그래서 사람의 저장은
 * 제안의 상태와 상관없이 올린 제안의 기준 리비전(`base`)을 `expect_revision` 으로 싣고, 그 사이 다른 저장이 있었으면 409 다.
 * `edited` 는 올린 제안 위에서 사람이 고쳤는가이다(배치는 빼고 본다). 기준 위의 변경이 아니라 이것으로 "제안대로 저장" 과
 * 다음 제안을 불러오기 전에 묻고, 청지기에게 저장하지 않은 변경이 있다고 알린다.
 *
 * 올리는 일(그리기)은 페이지가 `draw` 로 하고 그린 내용의 열쇠를 돌려준다. 열쇠는 페이지의 `contentKey` 와 같은 규칙이다.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { getAgentRevision } from "@/api/resources";
import type { Agent, AssistantProposalView } from "@/api/types";
import type { ProposalText } from "@/components/canvas/Inspector";
import type { ProposalMark } from "@/components/canvas/types";
import { messages } from "@/i18n";
import { nodeText, proposalNodeMarks } from "@/lib/assistant";

interface Loaded {
  id: string;
  /** 올린 캔버스의 Agent. 새 Agent 캔버스면 undefined 다. 다른 Agent 의 캔버스에서는 올린 것이 없는 것으로 본다. */
  agentId: string | undefined;
  /** 올린 내용의 열쇠(그래프·service·이름, 배치 빼고). */
  key: string;
  /** 바꾸기의 기준 리비전. 만들기는 null 이다. */
  base: number | null;
}

export interface ProposalContentInput {
  tenant: string;
  /** 저장된 Agent 의 id. 새 Agent 캔버스면 undefined 다. */
  agentId: string | undefined;
  agent: Agent | undefined;
  /** 주소의 제안이 기다리면 그 제안이다. */
  open: AssistantProposalView | null;
  /** 제안을 얹을 바탕(저장된 Agent 나 빈 새 캔버스)을 올렸는가. */
  ready: boolean;
  /** 지금 캔버스 내용의 열쇠. */
  contentKey: string;
  /** 기준 위에 저장하지 않은 변경이 있는가(떠나기 확인과 같은 판단). */
  unsaved: boolean;
  selectedNodeId: string | null;
  /** 제안을 캔버스에 그리고 그린 내용의 열쇠를 돌려준다. */
  draw: (v: AssistantProposalView) => string;
}

export interface ProposalContent {
  /** 열린 제안이 캔버스에 올라 있으면 그 제안이다. 표시와 "제안대로 저장" 이 이것을 본다. */
  shown: AssistantProposalView | null;
  /** 캔버스에 올린 제안의 id. 내용이 바뀌면 null 이다. */
  loadedId: string | null;
  /** 올린 제안의 기준 리비전. 사람의 저장이 `expect_revision` 으로 싣는다. */
  base: number | null;
  /** 올린 제안 위에서 사람이 고쳤는가. */
  edited: boolean;
  /** 사람이 불러오기를 거절했거나 다른 내용을 대신 올려 열린 제안이 캔버스에 없다. 다시 불러오는 단추를 보인다. */
  declined: boolean;
  /** 열린 제안을 다시 불러온다(고친 것이 있으면 묻는다). */
  load: () => void;
  /** 리비전이나 템플릿을 대신 올렸다. 기억을 지우고 다시 불러오는 단추를 남긴다. */
  setAside: () => void;
  /** 저장했거나 저장된 리비전을 다시 올렸다. 기억을 지운다. */
  forget: () => void;
  marks: ReadonlyMap<string, ProposalMark> | undefined;
  /** 고른 노드에서 제안이 바꾸는 글의 앞(기준 리비전)과 뒤(짜 맞춘 정의) 전체. */
  texts: ProposalText[] | undefined;
}

export function useProposalContent({ tenant, agentId, agent, open, ready, contentKey, unsaved, selectedNodeId, draw }: ProposalContentInput): ProposalContent {
  const [stored, setLoaded] = useState<Loaded | null>(null);
  // 다른 Agent 의 캔버스로 옮겼으면 앞 캔버스에 올린 제안은 이 캔버스의 것이 아니다. 그 기준 리비전을 싣거나 고친 것으로 세지 않는다
  const loaded = stored?.agentId === agentId ? stored : null;
  const [declined, setDeclined] = useState<string | null>(null);
  const edited = loaded !== null && contentKey !== loaded.key;
  const shown = open && loaded?.id === open.proposal_id ? open : null;

  /** 올리기 전에 묻는다. 올린 제안이 있으면 그 위에서 고친 것이, 없으면 기준 위의 변경이 사라지기 때문이다. */
  function apply(v: AssistantProposalView) {
    if ((loaded ? edited : unsaved) && !window.confirm(messages().agents.canvasPage.loadProposal)) {
      setDeclined(v.proposal_id);
      return;
    }
    setLoaded({ id: v.proposal_id, agentId, key: draw(v), base: v.kind === "update" ? (v.base_revision ?? null) : null });
    setDeclined(null);
  }

  useEffect(() => {
    if (!open || !ready || loaded?.id === open.proposal_id || declined === open.proposal_id) return;
    apply(open);
    // 열린 제안이 바뀌거나 바탕이 준비될 때만 본다. apply 는 그때의 상태로 묻는다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ready, loaded?.id, declined]);

  // 바뀌는 글의 앞. 기준이 지금 리비전이면 이미 읽은 Agent 를 쓰고, 그 사이 새 리비전이 저장됐으면 기준을 읽는다. 리비전은
  // 바뀌지 않으므로 다시 읽지 않는다
  const baseRevision = shown?.kind === "update" ? shown.base_revision : null;
  const baseIsLatest = baseRevision != null && agent?.revision === baseRevision;
  const baseQuery = useQuery({
    queryKey: ["agents", tenant, agentId, "revision", baseRevision],
    queryFn: () => getAgentRevision(agentId as string, baseRevision as number),
    enabled: baseRevision != null && !baseIsLatest && Boolean(agentId),
    staleTime: Infinity,
  });
  const baseGraph = baseIsLatest ? agent?.graph : baseQuery.data?.graph;

  const marks = useMemo(() => (shown ? proposalNodeMarks(shown.proposal) : undefined), [shown]);
  const texts = useMemo(() => {
    if (!shown || !selectedNodeId) return undefined;
    const fields = shown.proposal.changes.texts.filter((d) => d.node_id === selectedNodeId).map((d) => d.field);
    return fields.map((field) => ({ field, before: nodeText(baseGraph, selectedNodeId, field), after: nodeText(shown.graph, selectedNodeId, field) }));
  }, [shown, selectedNodeId, baseGraph]);

  return {
    shown,
    loadedId: loaded?.id ?? null,
    base: loaded?.base ?? null,
    edited,
    declined: open !== null && declined === open.proposal_id,
    load: () => {
      if (!open) return;
      setDeclined(null);
      apply(open);
    },
    setAside: () => {
      if (!loaded) return;
      if (open) setDeclined(open.proposal_id);
      setLoaded(null);
    },
    forget: () => setLoaded(null),
    marks,
    texts,
  };
}
