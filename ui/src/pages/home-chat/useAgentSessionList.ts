/**
 * 홈 챗 대화 목록의 에이전트 몫 (D35 F4, P36 4절). 목록 조회와 제목 검색, 이름 바꾸기·삭제를 한 곳에 묶는다.
 *
 * 런타임이 `session_admin` 을 알리지 않으면 검색어를 보내지 않고 `manage` 도 없다. 목록은 지금처럼 메뉴 없는 행이다.
 * 상대를 바꾸면 검색어를 비운다. 대화 중에는 첫 턴의 색인 쓰기를 놓치지 않게 목록을 느슨하게 다시 읽는다. 고른 에이전트가
 * 잠들었거나 깨우는 중이면(`paused`) 읽지 않는다. 읽기는 깨우지 않으므로 화면이 휴면 줄을 보이고 상태가 바뀌면 다시 읽는다 (P44 8절).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { deleteAgentSession, renameAgentSession } from "@/api/agentChat";
import { listAgentSessions } from "@/api/resources";
import type { AgentSession } from "@/api/types";
import { type Messages, useT } from "@/i18n";
import { agentSessionsKey } from "@/lib/queryKeys";
import { RUNTIME_SESSION_ADMIN, type ChatFeatures } from "@/lib/runtimeFeatures";
import { formatDate } from "@/lib/utils";

import type { SessionManage, SessionRow } from "./SessionList";

/** 대화 중에 목록을 다시 읽는 간격(ms)이다. */
const TALKING_REFETCH_MS = 10_000;

export interface AgentSessionList {
  rows: SessionRow[];
  loading: boolean;
  /** 서버에 보낸 제목 검색어. 검색을 끄거나 기능이 없으면 빈 글이다. */
  query: string;
  /** 런타임이 `session_admin` 을 알렸을 때만 있다. */
  manage: SessionManage | undefined;
  /** 이 에이전트의 모든 목록(검색어마다 하나)을 다시 읽게 한다. */
  invalidate: () => Promise<void>;
}

export function useAgentSessionList({
  tenant,
  agentId,
  features,
  talking,
  paused = false,
  onDeleted,
}: {
  tenant: string;
  /** 고른 에이전트. 없으면(청지기거나 아직 못 고름) 읽지 않는다. */
  agentId: string | null;
  features: ChatFeatures;
  /** 대화 중인가. 그동안 목록을 느슨하게 다시 읽는다. */
  talking: boolean;
  /** 고른 에이전트가 잠들었거나 깨우는 중이라 런타임을 읽지 않는다. */
  paused?: boolean;
  /** 지운 뒤 부른다. 열려 있던 대화를 지웠으면 위가 시작 화면으로 돌아간다. */
  onDeleted: (id: string) => void;
}): AgentSessionList {
  const t = useT();
  const queryClient = useQueryClient();
  // 검색어는 그것을 쓴 에이전트와 함께 쥔다. 상대를 바꾸면 다시 그릴 필요 없이 빈 검색어가 된다
  const [search, setSearch] = useState<{ agentId: string | null; q: string }>({ agentId, q: "" });
  const admin = agentId !== null && features.has(RUNTIME_SESSION_ADMIN);
  const query = admin && search.agentId === agentId ? search.q : "";

  const list = useQuery<AgentSession[]>({
    queryKey: agentSessionsKey(tenant, agentId ?? "", { q: query }),
    queryFn: () => listAgentSessions(agentId!, undefined, query),
    enabled: agentId !== null && !paused,
    // 첫 턴의 done 직후 한 번만 다시 읽으면 런타임이 세션을 색인하기 전에 읽어 목록이 빈 채로 남을 수 있다
    // (stub 처럼 답이 즉시 오는 에이전트에서 실측). 대화 중에는 느슨하게 다시 읽는다
    refetchInterval: talking ? TALKING_REFETCH_MS : false,
  });

  const invalidate = useCallback(() => queryClient.invalidateQueries({ queryKey: agentSessionsKey(tenant, agentId ?? "") }), [agentId, queryClient, tenant]);
  // 검색칸은 이 함수가 바뀌면 디바운스를 다시 건다. 다시 그릴 때마다 새로 만들지 않는다
  const onSearch = useCallback((q: string) => setSearch({ agentId, q }), [agentId]);

  const manage: SessionManage | undefined =
    admin && agentId
      ? {
          kind: "manage",
          onSearch,
          onRename: async (id, title) => {
            await renameAgentSession(agentId, id, title);
            await invalidate();
          },
          onDelete: async (id) => {
            await deleteAgentSession(agentId, id);
            onDeleted(id);
            await invalidate();
          },
        }
      : undefined;

  return { rows: (list.data ?? []).map((s) => agentRow(t, s)), loading: list.isLoading, query, manage, invalidate };
}

function agentRow(t: Messages, s: AgentSession): SessionRow {
  const at = s.last_turn_at ?? s.created_at;
  return {
    id: s.session_id,
    title: s.title ?? "",
    detail: t.shell.sessionList.agentDetail(s.turns, at ? formatDate(t, at) : null, s.status === "pending"),
    closed: s.status === "closed",
    running: Boolean(s.running),
  };
}
