import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { NavLink } from "react-router";

import { listAudit, listMcpAudit } from "@/api/resources";
import type { AuditFilters, MCPAuditFilters } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { StateRow } from "@/components/StateBlock";
import { Badge, levelVariant } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { FilterForm, LIMIT_MAX, LIMIT_MIN, LIMIT_STEP, TruncationNotice } from "@/components/FilterForm";
import { tabClass } from "@/components/ui/tabs";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { openAssistantSession, useAssistantConfig, useAssistantSessions } from "@/lib/assistant";
import { useTenant } from "@/lib/tenant";
import { useUserLabel } from "@/lib/userLabel";
import { formatDate } from "@/lib/utils";
import { useAdminAccess } from "@/pages/admin/permission";

function Tabs() {
  const text = useT().audit.tabs;
  const cls = ({ isActive }: { isActive: boolean }) => tabClass(isActive);
  return (
    <div className="mb-4 flex gap-0.5 border-b border-line">
      <NavLink to="/audit" end className={cls} data-testid="audit-tab-api">
        {text.api}
      </NavLink>
      <NavLink to="/audit/mcp" className={cls} data-testid="audit-tab-mcp">
        {text.mcp}
      </NavLink>
    </div>
  );
}

/** 필터 초기값이 role 에 따라 다르므로 GET /me 가 끝난 뒤 그린다. */
export function AuditPage() {
  const t = useT();
  const { ready, isPlatformAdmin: admin } = useAdminAccess();
  const tenant = useTenant();
  if (!ready) return <p className="text-sm text-muted">{t.common.loading}</p>;
  // 테넌트를 바꾸면 필터를 다시 잡는다. 옛 테넌트가 필터에 남으면 서버가 403 으로 막는다
  return <AuditInner key={tenant} admin={admin} />;
}

/** 청지기가 대신 부른 호출의 표식. `assistant/<session_id>` 다 (D31). */
export const ASSISTANT_VIA_PREFIX = "assistant/";

/** 폼 값(via 글자 + "청지기가 한 것만" 체크) → 쿼리의 via. 체크만 하면 접두(assistant/)로, 세션 id 만 적으면 접두를 붙인다. */
export function viaFilterValue(via: string, assistantOnly: boolean): string {
  const v = via.trim();
  if (!assistantOnly) return v;
  if (!v) return ASSISTANT_VIA_PREFIX;
  return v.startsWith(ASSISTANT_VIA_PREFIX) ? v : `${ASSISTANT_VIA_PREFIX}${v}`;
}

type AuditFormValues = AuditFilters & { assistant_only?: string };

/** via 셀. 청지기 세션이면 세션 id 를 보이고, 내 세션 목록에 있으면 패널에서 그 세션을 여는 버튼이다. 없는 세션이면 링크가 없다. */
function ViaCell({ via, sessionIds, tenant }: { via: string | null | undefined; sessionIds: Set<string>; tenant: string }) {
  const t = useT();
  if (!via) return <span>-</span>;
  const sid = via.startsWith(ASSISTANT_VIA_PREFIX) ? via.slice(ASSISTANT_VIA_PREFIX.length) : null;
  if (!sid) return <span className="font-mono text-xs">{via}</span>;
  return (
    <span className="flex items-center gap-1.5">
      <Badge variant="accent">{t.shell.names.assistant}</Badge>
      {sessionIds.has(sid) ? (
        <button type="button" className="font-mono text-xs text-link underline-offset-2 hover:text-link-hover hover:underline" data-testid={`audit-via-open-${sid}`} title={t.audit.openSession} onClick={() => openAssistantSession(tenant, sid)}>
          {sid}
        </button>
      ) : (
        <span className="font-mono text-xs text-muted" data-testid={`audit-via-${sid}`}>
          {sid}
        </span>
      )}
    </span>
  );
}

function AuditInner({ admin }: { admin: boolean }) {
  const t = useT();
  const text = t.audit;
  const userLabel = useUserLabel();
  const tenant = useTenant();
  // platform-admin 은 tenant 를 안 보내면 전체를 본다. 그 외는 헤더의 테넌트만 볼 수 있다.
  const initial: AuditFormValues = { tenant: admin ? "" : tenant, since: "", until: "", action: "", sub: "", resource: "", via: "", assistant_only: "", limit: LIMIT_STEP };
  const [filters, setFilters] = useState<AuditFormValues>(initial);
  // Limit 을 밖에서 올리면(더 보기) 칸도 따라와야 하므로 폼을 다시 태운다
  const [formKey, setFormKey] = useState(0);
  const limit = Number(filters.limit) || LIMIT_STEP;
  const q = useQuery({ queryKey: ["audit", tenant, filters], queryFn: () => listAudit(filters) });
  // via 의 세션 링크는 내 세션 목록에 있는 것만 단다. 비서가 꺼져 있으면 목록을 부르지 않는다
  const assistant = useAssistantConfig();
  const sessions = useAssistantSessions(tenant, assistant.enabled);
  const sessionIds = new Set((sessions.data ?? []).map((s) => s.id));

  return (
    <PageWidth data-testid="audit-page">
      <PageHeader title={t.audit.page.title} testId="page-title" />
      <Tabs />
      <FilterForm
        key={formKey}
        values={filters}
        defaults={initial}
        onApply={(f) => {
          const { assistant_only, via, ...rest } = f;
          setFilters({ ...rest, via: viaFilterValue(via ?? "", Boolean(assistant_only)) });
        }}
        fields={[
          { key: "tenant", label: "Tenant", placeholder: admin ? "(all)" : tenant },
          { key: "since", label: "Since", type: "datetime-local" },
          { key: "until", label: "Until", type: "datetime-local" },
          { key: "action", label: "Action", placeholder: "deploy_server" },
          { key: "sub", label: "Subject" },
          { key: "resource", label: "Resource", placeholder: "mcp-servers" },
          { key: "via", label: "Via", placeholder: "assistant/<session>" },
          { key: "limit", label: "Limit", type: "number", min: LIMIT_MIN, max: LIMIT_MAX },
        ]}
        checks={[{ key: "assistant_only", label: text.assistantOnly, title: text.assistantOnlyTitle }]}
      />
      <Card>
        <Table data-testid="audit-table">
          <THead>
            <tr>
              <TH className="whitespace-nowrap">{text.columns.time}</TH>
              <TH className="whitespace-nowrap">{text.columns.subject}</TH>
              <TH className="whitespace-nowrap">{text.columns.via}</TH>
              <TH className="whitespace-nowrap">{text.columns.tenant}</TH>
              <TH className="whitespace-nowrap">{text.columns.action}</TH>
              <TH className="whitespace-nowrap">{text.columns.resource}</TH>
              <TH className="whitespace-nowrap">{text.columns.request}</TH>
              <TH className="whitespace-nowrap">{text.columns.result}</TH>
              <TH className="whitespace-nowrap">{text.columns.requestId}</TH>
            </tr>
          </THead>
          <TBody>
            {q.isLoading || q.error || !q.data?.length ? (
              // 403·500 도 "No events." 라고 적혀 있었다. 못 읽은 것과 없는 것은 다른 말을 한다
              <StateRow
                colSpan={9}
                loading={q.isLoading}
                error={q.error}
                onRetry={() => void q.refetch()}
                empty={text.empty}
              />
            ) : (
              q.data.map((e) => (
                <TR key={e.id} data-testid={`audit-row-${e.id}`}>
                  <TD className="whitespace-nowrap text-xs">{formatDate(t, e.ts)}</TD>
                  {/* local 모드의 sub 는 사용자 id 다. 아이디로 보이고 원래 값(필터에 넣는 값)은 제목으로 둔다 */}
                  {/* 이름은 짧아 줄을 바꾸지 않는다. 긴 id 가 든 리소스 칸에 밀려 한 글자씩 세로로 쌓였다 */}
                  <TD className="whitespace-nowrap font-mono text-xs" title={e.sub ?? undefined}>
                    {e.sub ? userLabel(e.sub) : "-"}
                    {e.subject_type !== "user" ? <Badge variant="muted" className="ml-1">{e.subject_type}</Badge> : null}
                  </TD>
                  <TD data-testid={`audit-via-cell-${e.id}`}>
                    <ViaCell via={e.via} sessionIds={sessionIds} tenant={tenant} />
                  </TD>
                  <TD>{e.tenant ?? "-"}</TD>
                  <TD className="font-mono text-xs">{e.action}</TD>
                  <TD className="font-mono text-xs">
                    {e.resource ?? "-"}
                    {e.resource_id ? <span className="text-muted">/{e.resource_id}</span> : null}
                  </TD>
                  <TD className="font-mono text-xs">
                    {e.method} {e.path}
                    {e.duration_ms !== null ? <span className="ml-1 text-dim">{t.audit.page.duration(e.duration_ms)}</span> : null}
                  </TD>
                  <TD>
                    <Badge variant={levelVariant(e.result === "ok" || e.status < 400)}>{e.status}</Badge>
                    {e.error_code ? <span className="ml-1 font-mono text-xs text-err-fg">{e.error_code}</span> : null}
                  </TD>
                  <TD className="font-mono text-[11px] text-muted" data-testid="audit-request-id">
                    {e.request_id}
                  </TD>
                </TR>
              ))
            )}
          </TBody>
        </Table>
        <TruncationNotice
          shown={q.data?.length ?? 0}
          limit={limit}
          onMore={(next) => {
            setFilters({ ...filters, limit: next });
            setFormKey((k) => k + 1);
          }}
        />
      </Card>
    </PageWidth>
  );
}

export function McpAuditPage() {
  const t = useT();
  const { ready, isPlatformAdmin: admin } = useAdminAccess();
  const tenant = useTenant();
  if (!ready) return <p className="text-sm text-muted">{t.common.loading}</p>;
  return <McpAuditInner key={tenant} admin={admin} />;
}

function McpAuditInner({ admin }: { admin: boolean }) {
  const t = useT();
  const text = t.audit;
  const userLabel = useUserLabel();
  const tenant = useTenant();
  const initial: MCPAuditFilters = { tenant: admin ? "" : tenant, since: "", until: "", tool: "", sub: "", type: "", outcome: "", limit: LIMIT_STEP };
  const [filters, setFilters] = useState<MCPAuditFilters>(initial);
  const [formKey, setFormKey] = useState(0);
  const limit = Number(filters.limit) || LIMIT_STEP;
  const q = useQuery({ queryKey: ["audit-mcp", tenant, filters], queryFn: () => listMcpAudit(filters) });

  return (
    <PageWidth data-testid="audit-mcp-page">
      <PageHeader title={t.audit.page.title} testId="page-title" />
      <Tabs />
      <FilterForm
        key={formKey}
        values={filters}
        defaults={initial}
        onApply={setFilters}
        fields={[
          { key: "tenant", label: "Tenant", placeholder: admin ? "(all)" : tenant },
          { key: "since", label: "Since", type: "datetime-local" },
          { key: "until", label: "Until", type: "datetime-local" },
          { key: "tool", label: "Tool" },
          { key: "sub", label: "Subject" },
          { key: "type", label: "Type", placeholder: "mcp_tool_call" },
          { key: "outcome", label: "Outcome", placeholder: "success" },
          { key: "limit", label: "Limit", type: "number", min: LIMIT_MIN, max: LIMIT_MAX },
        ]}
      />
      <Card>
        <Table data-testid="audit-mcp-table">
          <THead>
            <tr>
              <TH className="whitespace-nowrap">{text.columns.time}</TH>
              <TH className="whitespace-nowrap">{text.columns.type}</TH>
              <TH className="whitespace-nowrap">{text.columns.result}</TH>
              <TH className="whitespace-nowrap">{t.shared.columns.tool}</TH>
              <TH className="whitespace-nowrap">{text.columns.backend}</TH>
              <TH className="whitespace-nowrap">{text.columns.subject}</TH>
              <TH className="whitespace-nowrap">{text.columns.via}</TH>
              <TH className="whitespace-nowrap">{text.columns.resource}</TH>
              <TH className="whitespace-nowrap">{text.columns.component}</TH>
              <TH className="whitespace-nowrap">{text.columns.auditId}</TH>
            </tr>
          </THead>
          <TBody>
            {q.isLoading || q.error || !q.data?.length ? (
              <StateRow
                colSpan={10}
                loading={q.isLoading}
                error={q.error}
                onRetry={() => void q.refetch()}
                empty={text.mcpEmpty}
              />
            ) : (
              q.data.map((e) => (
                <TR key={e.audit_id} data-testid={`audit-mcp-row-${e.audit_id}`}>
                  <TD className="whitespace-nowrap text-xs">{formatDate(t, e.ts)}</TD>
                  <TD className="font-mono text-xs">{e.type ?? "-"}</TD>
                  <TD>
                    <Badge variant={e.outcome === "success" ? "ok" : e.outcome ? "error" : "muted"}>{e.outcome ?? "-"}</Badge>
                  </TD>
                  <TD className="font-mono text-xs">{e.tool ?? "-"}</TD>
                  <TD className="font-mono text-xs" data-testid={`audit-mcp-backend-${e.audit_id}`}>{e.backend ?? "-"}</TD>
                  <TD className="font-mono text-xs" title={e.sub ?? e.user ?? undefined}>{e.sub ? userLabel(e.sub) : (e.user ?? "-")}</TD>
                  {/* 위임한 호출의 Agent 다. 사람은 주체 칸에 남고 위임 사슬의 바깥 행위자가 여기 온다 (P53 7절) */}
                  <TD className="font-mono text-xs" data-testid={`audit-mcp-via-${e.audit_id}`}>{e.act ?? "-"}</TD>
                  <TD className="font-mono text-xs">
                    {e.instance_id ?? "-"}
                    {e.namespace ? <span className="text-muted"> {e.namespace}</span> : null}
                  </TD>
                  <TD className="text-xs">{e.component ?? "-"}</TD>
                  <TD className="font-mono text-[11px] text-muted">{e.audit_id}</TD>
                </TR>
              ))
            )}
          </TBody>
        </Table>
        <TruncationNotice
          shown={q.data?.length ?? 0}
          limit={limit}
          onMore={(next) => {
            setFilters({ ...filters, limit: next });
            setFormKey((k) => k + 1);
          }}
        />
      </Card>
    </PageWidth>
  );
}
