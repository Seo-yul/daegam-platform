/**
 * Agent 의 LLM 경로 (P38 6절, 11절). Agent 개요의 정의 요약 카드 한 줄이다.
 *
 * 경로는 배포가 정한 값(`llm_route`)이고 직접 경로면 그 까닭을 보인다. 게이트웨이 경로에서 배포 권한이 있으면 "LLM 토큰 교체" 가
 * 있다. 새 토큰으로 파드를 굴리고 옛 토큰은 옛 파드가 모두 내려간 뒤에 회수하며, 곧바로 회수를 선택하면 그 자리에서 회수한다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { rotateLlmToken } from "@/api/resources";
import type { Agent } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { ownValue } from "@/lib/utils";

export function LlmRouteSummary({ agent, canRotate }: { agent: Agent; canRotate: boolean }) {
  const t = useT();
  const text = t.agents.overview.llmRoute;
  const route = agent.llm_route;
  if (route.route === "gateway") {
    return (
      <div className="flex flex-col gap-1.5">
        <Badge variant="accent" className="w-fit" data-testid="llm-route">
          {text.gateway}
        </Badge>
        {canRotate ? <RotateToken agentId={agent.id} /> : null}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span data-testid="llm-route">{text.direct}</span>
      {route.reason ? (
        <span className="text-xs text-muted" data-testid="llm-route-reason">
          {ownValue(text.reasons, route.reason) ?? route.reason}
        </span>
      ) : null}
    </div>
  );
}

function RotateToken({ agentId }: { agentId: string }) {
  const t = useT();
  const text = t.agents.overview.llmRoute;
  const queryClient = useQueryClient();
  const [immediate, setImmediate] = useState(false);
  const rotate = useMutation({
    mutationFn: () => rotateLlmToken(agentId, immediate),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" title={text.rotateHint} disabled={rotate.isPending} data-testid="llm-token-rotate" onClick={() => rotate.mutate()}>
          {text.rotate}
        </Button>
        <label className="flex items-center gap-1.5 text-xs">
          <input type="checkbox" data-testid="llm-token-immediate" checked={immediate} onChange={(e) => setImmediate(e.target.checked)} />
          {text.immediate}
        </label>
      </div>
      <p className="text-[11px] text-muted">{text.rotateHint}</p>
      {rotate.isSuccess ? (
        <span className="text-xs text-ok-fg" data-testid="llm-token-rotated">
          {rotate.data.immediate ? text.rotatedImmediate : text.rotated}
        </span>
      ) : null}
      <ErrorMessage error={rotate.error} testId="llm-token-rotate-error" />
    </div>
  );
}
