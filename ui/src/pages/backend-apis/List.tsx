import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listBackendApis } from "@/api/resources";
import { ErrorMessage } from "@/components/ErrorMessage";
import { ListToolbar, Pagination, SearchInput, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateRow } from "@/components/StateBlock";
import { BackendApiIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { useT } from "@/i18n";
import type { DeleteTarget } from "@/lib/deletion";
import { nameSearchText } from "@/lib/names";
import { useTenant } from "@/lib/tenant";

export function BackendApiListPage() {
  const t = useT();
  const text = t.backendApis.list;
  const tenant = useTenant();
  const q = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis });
  const [target, setTarget] = useState<DeleteTarget | null>(null);

  const view = useListView(q.data ?? [], nameSearchText);

  return (
    <PageWidth data-testid="backend-apis-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? t.ui.deployment.summary.count(q.data.length) : undefined}
        primary={
          <Link to="/backend-apis/new" className={buttonVariants()} data-testid="backend-api-new">
            <Plus size={16} aria-hidden />
            {text.create}
          </Link>
        }
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
      </ListToolbar>
      {/* 앞서 받아 둔 것이 있으면 표는 그대로 두고 폴링이 끊겼다는 것만 위에 붙인다. 처음부터 못
          받았으면 표 안의 StateRow 가 "다시 시도" 와 함께 말한다 */}
      {q.data ? <ErrorMessage error={q.error} /> : null}
      <Card>
        <Table data-testid="backend-apis-table">
          <THead>
            <tr>
              <TH>{text.columns.name}</TH>
              <TH>{t.backendApis.kind}</TH>
              <TH>{t.backendApis.latestVersion}</TH>
              <TH>{text.columns.tools}</TH>
              <TH>{text.columns.endpoint}</TH>
              <TH>{text.columns.auth}</TH>
              <ActionTH />
            </tr>
          </THead>
          <TBody>
            {q.isLoading || (q.error && !q.data) || !view.total ? (
              // 못 불러온 것과 비어 있는 것을 가른다. 조회에 실패했는데 "없습니다" 라고 하면 이미 만들어 둔 사람이 같은 것을 또 만든다
              <StateRow
                colSpan={7}
                loading={q.isLoading}
                error={q.data ? undefined : q.error}
                onRetry={() => void q.refetch()}
                illustration={<BackendApiIllustration />}
                empty={view.query ? text.noMatch(view.query) : text.empty(tenant)}
                emptyAction={
                  view.query ? null : (
                    <Link to="/backend-apis/new" className={buttonVariants({ size: "sm" })} data-testid="empty-backend-api-new">
                      {text.emptyNew}
                    </Link>
                  )
                }
              />
            ) : (
              view.pageItems.map((p) => (
                <ListTR key={p.id} data-testid={`backend-api-row-${p.name}`}>
                  <NameTD to={`/backend-apis/${p.id}`} item={p} />
                  <TD>{p.kind ? <Badge>{p.kind}</Badge> : <span className="text-dim">-</span>}</TD>
                  <TD>{p.latest_version ?? "-"}</TD>
                  <TD>{p.tool_count ?? "-"}</TD>
                  <TD className="max-w-xs truncate text-muted">{p.endpoint ?? "-"}</TD>
                  <TD>{p.auth}</TD>
                  <ActionTD>
                    <RowDeleteButton name={p.name} onClick={() => setTarget({ id: p.id, name: p.name })} />
                  </ActionTD>
                </ListTR>
              ))
            )}
          </TBody>
        </Table>
        <Pagination view={view} />
      </Card>
      <ResourceDeleteDialog resource="backend-api" target={target} onClose={() => setTarget(null)} />
    </PageWidth>
  );
}
