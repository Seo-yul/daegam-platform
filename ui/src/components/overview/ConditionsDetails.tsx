/**
 * 접힌 "클러스터 상태 자세히" (D44 셋째). K8s conditions 표가 개요의 첫 화면이던 것을 여기로 내린다.
 *
 * 전부 정상이면 요약 한 줄만 보이고, 문제인 condition 이 하나라도 있으면 기본으로 펼쳐 둔다. 접혀 있어도 표는
 * 문서에 남으므로 브라우저 찾기와 낭독기 둘 다 닿는다.
 *
 * 색은 `lib/conditions.ts` 가 정한다. 쿠버네티스 condition 에는 극성이 있어 False 를 일률적으로 빨갛게
 * 칠하면 멀쩡한 인스턴스가 실패로 보인다.
 */
import type { InstanceCondition } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import { conditionTone } from "@/lib/conditions";
import { formatDate } from "@/lib/utils";

/** 요약 줄. 접힌 채로도 이것만 읽으면 볼 필요가 있는지 알 수 있다. */
export function conditionsSummary(t: Messages, conditions: InstanceCondition[]): string {
  const text = t.detail.conditionsDetails;
  // False 를 세지 않고 극성을 아는 판정을 센다. StdioReplicaCapped=False 처럼 "해당 없음" 인 False 가 흔하다
  const problems = conditions.filter((c) => conditionTone(c.type, c.status) === "error").length;
  return problems ? text.problems(problems) : text.allOk(conditions.length);
}

export function ConditionsDetails({ conditions, testId = "conditions-details" }: { conditions: InstanceCondition[]; testId?: string }) {
  const t = useT();
  const text = t.detail.conditionsDetails;
  if (!conditions.length) return null;
  const failed = conditions.some((c) => conditionTone(c.type, c.status) === "error");
  return (
    <details className="surface rounded-card overflow-hidden" data-testid={testId} open={failed}>
      <summary className="cursor-pointer px-5 py-3.5 text-sm text-muted marker:text-dim hover:text-heading focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none">
        {text.summary(conditionsSummary(t, conditions))}
      </summary>
      <div className="border-t border-line-strong">
        <Table data-testid="conditions-table">
          <THead>
            <tr>
              <TH>{text.columns.type}</TH>
              <TH>{text.columns.status}</TH>
              <TH>{text.columns.reason}</TH>
              <TH>{text.columns.message}</TH>
              <TH>{text.columns.changed}</TH>
            </tr>
          </THead>
          <TBody>
            {conditions.map((c, i) => (
              <TR key={`${c.type ?? "?"}-${i}`} data-testid={`condition-${c.type ?? i}`}>
                <TD>{c.type}</TD>
                <TD>
                  <Badge variant={conditionTone(c.type, c.status)}>{c.status}</Badge>
                </TD>
                <TD>{c.reason ?? ""}</TD>
                <TD className="text-fg-soft">{c.message ?? ""}</TD>
                <TD className="text-xs text-muted whitespace-nowrap">{c.last_transition ? formatDate(t, c.last_transition) : ""}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>
    </details>
  );
}
