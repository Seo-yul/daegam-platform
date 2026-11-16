/**
 * "정의 요약" 카드 (D44 셋째). 지금 도는 것이 무엇으로 만들어졌는지 몇 줄이다.
 *
 * 줄의 내용은 도메인마다 달라서(MCP Server 는 백엔드·도구·런타임·바인딩, Gateway 는 백엔드 수와 충돌·
 * 실패 규칙, Agent 는 모델·도구 출처·기억) 페이지가 줄을 만들어 넘기고 카드는 모양만 쥔다. 자세한 것은
 * 정의 탭에 그대로 있고 여기는 "다섯 줄 안에 읽히는가" 만 본다.
 */
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";

export interface SummaryRow {
  label: string;
  value: ReactNode;
  testId?: string;
}

export function DefinitionSummaryCard({
  revision,
  rows,
  footer,
  testId = "definition-summary",
}: {
  /** 저장된 최신 리비전. 머리에 배지로 선다. */
  revision?: number;
  rows: SummaryRow[];
  /** 줄 아래에 더 붙일 것. */
  footer?: ReactNode;
  testId?: string;
}) {
  const t = useT();
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{t.detail.definitionSummaryCard.title}</CardTitle>
        {revision !== undefined ? <Badge variant="muted">r{revision}</Badge> : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="grid grid-cols-[6.5rem_1fr] gap-y-1.5 text-sm">
          {rows.map((r) => (
            <div key={r.label} className="contents">
              <dt className="text-muted">{r.label}</dt>
              <dd data-testid={r.testId}>{r.value}</dd>
            </div>
          ))}
        </dl>
        {footer}
      </CardContent>
    </Card>
  );
}
