/**
 * Agent 상세의 이벤트 탭 (P37 12절). 런타임이 보냈거나 수집기가 파드 로그에서 읽은 `agent_events` 를 최신순으로 보인다.
 * 다른 사람의 `user_sub` 가 보이므로 audit:read 가 있어야 탭이 선다(Detail.tsx). 답변 피드백과 본문은 서버가 agent:approve
 * 에게만 준다. 본문은 남이 쓴 글이라 글로만 그린다.
 *
 * 이벤트 이름·세션 id·trace id 로 거른다. 거르기 폼과 잘림 표시는 감사 화면의 것이다(components/FilterForm.tsx). 이름은
 * 사전의 agents.events.names 이고 모르는 이름은 그대로 보인다.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { listAgentEvents } from "@/api/resources";
import type { AgentEvent, AgentEventFilter } from "@/api/types";
import { FilterForm, LIMIT_STEP, TruncationNotice } from "@/components/FilterForm";
import { queryGate } from "@/components/QueryGate";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { JsonReveal } from "@/components/ui/json-reveal";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { eventName } from "@/lib/agentEvents";
import { useTenant } from "@/lib/tenant";
import { tokensOf } from "@/lib/usage";
import { formatDate, formatNumber } from "@/lib/utils";

type EventFormValues = { event?: string; session_id?: string; trace_id?: string; limit?: number };

const DEFAULTS: EventFormValues = { event: "", session_id: "", trace_id: "", limit: LIMIT_STEP };

/** 폼의 값을 질의로. 빈 칸은 cleanQuery 가 뺀다. */
function filterOf(f: EventFormValues): AgentEventFilter {
  return { event: f.event || undefined, session_id: f.session_id?.trim() || undefined, trace_id: f.trace_id?.trim() || undefined, limit: Number(f.limit) || LIMIT_STEP };
}

export function EventsTab({ agentId }: { agentId: string }) {
  const t = useT();
  const text = t.agents.events;
  const tenant = useTenant();
  const [values, setValues] = useState<EventFormValues>(DEFAULTS);
  // 더 보기로 limit 을 올리면 폼도 다시 태운다
  const [formKey, setFormKey] = useState(0);
  const filter = filterOf(values);
  const q = useQuery({ queryKey: ["agents", tenant, agentId, "events", filter], queryFn: () => listAgentEvents(agentId, filter) });
  const gate = queryGate(q, { testId: "events-error", keepStale: false });
  const names = Object.keys(text.names).map((name) => ({ value: name, label: eventName(t, name) }));
  return (
    <Card data-testid="events-card">
      <CardHeader>
        <CardTitle>{t.agents.tabs.events}</CardTitle>
        <span className="text-xs text-muted">{text.hint}</span>
      </CardHeader>
      <div className="px-4">
        <FilterForm
          key={formKey}
          testId="events-filters"
          testPrefix="events"
          values={values}
          defaults={DEFAULTS}
          onApply={setValues}
          fields={[
            { key: "event", label: text.filters.event, options: [{ value: "", label: text.filters.all }, ...names], className: "w-48" },
            { key: "session_id", label: text.filters.session, className: "w-48 font-mono" },
            { key: "trace_id", label: text.filters.trace, className: "w-72 font-mono" },
          ]}
        />
      </div>
      {gate ?? (
        <>
          <EventsTable rows={q.data ?? []} />
          <TruncationNotice
            testId="events"
            shown={q.data?.length ?? 0}
            limit={filter.limit ?? LIMIT_STEP}
            onMore={(next) => {
              setValues({ ...values, limit: next });
              setFormKey((k) => k + 1);
            }}
          />
        </>
      )}
    </Card>
  );
}

function EventsTable({ rows }: { rows: AgentEvent[] }) {
  const t = useT();
  const text = t.agents.events;
  return (
    <Table data-testid="events-table">
      <THead>
        <tr>
          <TH>{text.columns.time}</TH>
          <TH>{text.columns.event}</TH>
          <TH>{text.columns.session}</TH>
          <TH>{text.columns.turn}</TH>
          <TH>{t.shared.columns.tool}</TH>
          <TH>{text.columns.status}</TH>
          <TH>{text.columns.subject}</TH>
          <TH>{text.columns.tokens}</TH>
          <TH>{text.columns.body}</TH>
        </tr>
      </THead>
      <TBody>
        {rows.length ? (
          rows.map((e, i) => (
            <TR key={e.id} data-testid={`event-row-${i}`}>
              <TD className="text-xs text-muted">{formatDate(t, e.ts)}</TD>
              <TD className="text-xs" title={e.event}>
                {eventName(t, e.event)}
              </TD>
              <TD className="font-mono text-xs">{e.session_id ?? "-"}</TD>
              <TD>{e.turn ?? "-"}</TD>
              <TD className="font-mono text-xs">{e.tool ?? "-"}</TD>
              <TD>{e.status ?? "-"}</TD>
              <TD className="text-xs">{e.user_sub ?? "-"}</TD>
              {/* 턴 본문은 위가 아니라 data 안이다 */}
              <TD>{e.data?.usage ? formatNumber(t, tokensOf(e.data.usage)) : "-"}</TD>
              <TD>{e.body ? <JsonReveal summary={text.showBody} value={e.body} testId={`event-row-${i}-body`} /> : "-"}</TD>
            </TR>
          ))
        ) : (
          <EmptyRow colSpan={9}>{text.empty}</EmptyRow>
        )}
      </TBody>
    </Table>
  );
}
