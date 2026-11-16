import { useMutation, useQueryClient } from "@tanstack/react-query";

import { pauseMySchedule, pauseMyTrigger } from "@/api/resources";
import type { BoundSchedule, BoundTrigger } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { SourceStateCell } from "@/components/runs/shared";
import { StateRow } from "@/components/StateBlock";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { MY_RUNS_AS_ME_KEY } from "@/lib/queryKeys";
import { useRunsAsMe } from "@/lib/runs";
import { formatDate } from "@/lib/utils";

const COLUMNS = 6;

/** 한 줄. 일정과 트리거가 한 표에 서고, 무엇인지는 일정의 반복 규칙이나 트리거의 이름으로 보인다. */
type Row = { kind: "schedules"; source: BoundSchedule } | { kind: "triggers"; source: BoundTrigger };

/**
 * /account/runs-as-me: 나를 대신하는 자동 실행 (P41 6절, R9). 모든 테넌트에서 나에게 묶여 도는 일정과 트리거를 보이고 멈춘다.
 * 지금 role 과 상관없이 멈출 수 있고, 다시 시작은 그 Agent 의 자동 실행 탭에서 한다. 지운 Agent 의 것은 이름 대신 id 다.
 */
export function RunsAsMePage() {
  const t = useT();
  const text = t.account.runsAsMe;
  const queryClient = useQueryClient();
  const q = useRunsAsMe();
  const pause = useMutation({
    mutationFn: async (row: Row): Promise<void> => {
      await (row.kind === "schedules" ? pauseMySchedule(row.source.id) : pauseMyTrigger(row.source.id));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MY_RUNS_AS_ME_KEY }),
  });
  const rows: Row[] = [
    ...(q.data?.schedules ?? []).map((source): Row => ({ kind: "schedules", source })),
    ...(q.data?.triggers ?? []).map((source): Row => ({ kind: "triggers", source })),
  ];

  return (
    <PageWidth data-testid="runs-as-me-page" className="flex flex-col gap-4">
      <PageHeader title={text.title} testId="page-title" />
      <Card>
        <CardContent className="flex flex-col gap-3">
          <ErrorMessage error={pause.error} testId="runs-as-me-pause-error" />
          <Table data-testid="runs-as-me-table">
            <THead>
              <tr>
                <TH>{t.account.issue.tenant}</TH>
                <TH>{t.resources.kind.agent}</TH>
                <TH>{t.runs.history.columns.source}</TH>
                <TH>{t.account.tokenTable.columns.expiry}</TH>
                <TH>{t.account.tokenTable.columns.status}</TH>
                <TH />
              </tr>
            </THead>
            <TBody>
              {rows.map((row) => {
                const s = row.source;
                const testId = `runs-as-me-${s.id}`;
                return (
                  <TR key={`${row.kind}-${s.id}`} data-testid={testId}>
                    <TD className="text-xs">{s.tenant}</TD>
                    <TD className={s.agent_name ? undefined : "font-mono text-xs"}>{s.agent_name ?? s.agent_id}</TD>
                    <TD className="text-xs">
                      {row.kind === "schedules" ? (
                        <>
                          {t.runs.schedules.title} <code className="font-mono">{row.source.cron}</code>
                        </>
                      ) : (
                        <>
                          {t.runs.source.webhook} {row.source.name}
                        </>
                      )}
                    </TD>
                    <TD className="text-xs whitespace-nowrap">{formatDate(t, s.expires_at)}</TD>
                    <TD>
                      <SourceStateCell source={s} testId={testId} />
                    </TD>
                    <TD className="text-right">
                      {!s.paused ? (
                        <Button size="sm" variant="outline" data-testid={`${testId}-pause`} disabled={pause.isPending} onClick={() => pause.mutate(row)}>
                          {t.runs.pause}
                        </Button>
                      ) : null}
                    </TD>
                  </TR>
                );
              })}
              {!rows.length ? <StateRow colSpan={COLUMNS} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} /> : null}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </PageWidth>
  );
}
