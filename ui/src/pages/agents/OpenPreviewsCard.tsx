/**
 * 개요의 열린 미리 보기 카드 (P44b 11절). `agent:deploy` 가 있는 사람에게 그 Agent 에 열린 미리 보기마다 시작한 사람, 리비전, 끝나는
 * 시각과 끝내기 단추를 보인다. 미리 보기는 원본으로서 쓰기 Tool 을 부르므로 누구의 것이든 끝낼 수 있고, 다른 사람의 것을 끝내면 그
 * 사람의 서랍이 다른 사람이 끝냈다고 말한다. 끝내면 그 사람의 대화가 사라지므로 그 줄을 보이며 먼저 묻는다. 열린 것이 없으면 카드가
 * 없다. 끝나는 시각은 지금까지의 활동으로 끝날 때(`idle_ends_at`, 처음 시작 두 시간 뒤를 넘지 않는다)이고, 미리 보기는 두 시간 안에
 * 끝나므로 시각만 적는다.
 *
 * 평가 실행이 띄운 미리 보기(`run`, P45 9절)는 그 실행의 것이라고 적는다. 서랍과 미리 보기 라우트는 그것에 닿지 않고 이 카드에만
 * 보이며, 끝내면 서버가 그 실행을 취소한다. 그래서 그 줄을 끝내기 전에 묻는 창이 실행도 취소된다고 말한다.
 */
import { useState } from "react";

import type { AgentPreview } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useT } from "@/i18n";
import { useUserLabel } from "@/lib/userLabel";
import { formatClock } from "@/lib/utils";

import { useOpenPreviews } from "./usePreviews";

export function OpenPreviewsCard({ agentId }: { agentId: string }) {
  const t = useT();
  const text = t.agents.detail.previews;
  const userLabel = useUserLabel();
  const { previews, error, end } = useOpenPreviews(agentId);
  // 끝내기를 묻는 미리 보기다
  const [ending, setEnding] = useState<AgentPreview | null>(null);
  if (!previews.length && !error) return null;
  const row = (p: AgentPreview) => (p.run ? text.evaluationRow : text.row)(userLabel(p.owner), p.revision, formatClock(p.idle_ends_at));
  return (
    <Card data-testid="open-previews">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <ErrorMessage error={error} testId="open-previews-error" />
        <ul className="flex flex-col gap-2">
          {previews.map((p) => (
            <li key={p.id} className="flex flex-wrap items-center justify-between gap-3 text-sm" data-testid={`open-preview-${p.id}`}>
              <span data-testid={`open-preview-row-${p.id}`}>{row(p)}</span>
              <Button size="sm" variant="outline" data-testid={`open-preview-end-${p.id}`} disabled={end.isPending && end.variables === p.id} onClick={() => setEnding(p)}>
                {text.end}
              </Button>
            </li>
          ))}
        </ul>
      </CardContent>
      <ConfirmDialog
        open={ending !== null}
        title={t.agents.testChat.previewEnd}
        description={
          ending ? (
            <>
              <p>{row(ending)}</p>
              {ending.run ? <p data-testid="open-preview-end-evaluation">{text.endEvaluation}</p> : null}
            </>
          ) : null
        }
        confirmLabel={text.end}
        onConfirm={() => {
          if (ending) end.mutate(ending.id);
          setEnding(null);
        }}
        onCancel={() => setEnding(null)}
      />
    </Card>
  );
}
