/**
 * Agent 미리 보기의 읽기와 동작 (P44b 6절, 11절). 테스트 채팅 서랍은 내 미리 보기(`useMyPreview`)를, 개요는 그 Agent 에 열린 미리
 * 보기 모두(`useOpenPreviews`)를 본다. 시작이나 끝은 두 쿼리의 앞머리(`agentPreviewsKey`)를 무효화해 서랍과 개요가 함께 따라온다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { endAgentPreview, endMyPreview, getMyPreview, listAgentPreviews, startMyPreview, type MyPreview } from "@/api/agentPreview";
import { retryable } from "@/components/chat";
import { PREVIEWS_POLL_MS, previewPollMs } from "@/lib/agentPreview";
import { agentPreviewsKey, myAgentPreviewKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/**
 * 내 미리 보기와 그 시작(옮기기 포함)과 끝. `agentId` 가 null 이면 읽지 않는다. 파드가 뜨거나 끝나는 동안은 짧게, 뜬 뒤에는 길게
 * 다시 읽는다. 읽기가 다시 해도 소용없는 거절(403 등)로 실패하면 쥐고 있던 미리 보기를 버린다. 범위를 잃은 사람이 끝난 미리 보기와
 * 대화하는 것처럼 보이지 않게 한다. `loaded` 는 첫 읽기가 끝났는가다. 그 전에는 열린 미리 보기가 있는지 모른다.
 */
export function useMyPreview(agentId: string | null) {
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const key = myAgentPreviewKey(tenant, agentId ?? "");
  const query = useQuery({
    queryKey: key,
    queryFn: () => getMyPreview(agentId as string),
    enabled: agentId !== null,
    refetchInterval: (q) => previewPollMs(q.state.data?.preview),
  });
  const lost = query.error !== null && !retryable(query.error);

  /** 답을 쿼리에 바로 넣고 개요의 목록도 다시 읽게 한다. */
  const settle = async (next: MyPreview) => {
    queryClient.setQueryData<MyPreview>(key, next);
    await queryClient.invalidateQueries({ queryKey: agentPreviewsKey(tenant, agentId ?? ""), exact: true });
  };
  const start = useMutation({
    mutationFn: () => startMyPreview(agentId as string),
    onSuccess: (preview) => settle({ preview, ended: null }),
  });
  const end = useMutation({
    mutationFn: () => endMyPreview(agentId as string),
    onSuccess: () => settle({ preview: null, ended: null }),
  });
  return {
    preview: lost ? null : (query.data?.preview ?? null),
    ended: query.data?.ended ?? null,
    loaded: query.isSuccess,
    error: query.error,
    refetch: () => void query.refetch(),
    start,
    end,
  };
}

/** 그 Agent 에 열린 미리 보기 모두와 그 하나를 끝내기. */
export function useOpenPreviews(agentId: string) {
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: agentPreviewsKey(tenant, agentId),
    queryFn: () => listAgentPreviews(agentId),
    refetchInterval: PREVIEWS_POLL_MS,
  });
  const end = useMutation({
    mutationFn: (previewId: string) => endAgentPreview(agentId, previewId),
    // 끝낸 것이 내 미리 보기일 수 있으므로 서랍의 쿼리까지 앞머리로 다시 읽는다
    onSuccess: () => queryClient.invalidateQueries({ queryKey: agentPreviewsKey(tenant, agentId) }),
  });
  return { previews: query.data ?? [], error: query.error ?? end.error, end };
}
