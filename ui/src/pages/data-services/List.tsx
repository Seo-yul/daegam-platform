import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listDataServices } from "@/api/resources";
import { ListToolbar, Pagination, SearchInput, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateRow } from "@/components/StateBlock";
import { DatabaseIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { useT } from "@/i18n";
import type { DeleteTarget } from "@/lib/deletion";
import { nameSearchText } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";
import { dataServicesKey } from "@/lib/queryKeys";

export function DataServiceListPage() {
  const t = useT();
  const text = t.dataServices.list;
  const tenant = useTenant();
  const q = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });
  const [target, setTarget] = useState<DeleteTarget | null>(null);

  const view = useListView(q.data ?? [], nameSearchText);

  return (
    <PageWidth data-testid="data-services-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? t.ui.deployment.summary.count(q.data.length) : undefined}
        primary={
          <Link to="/data-services/new" className={buttonVariants()} data-testid="data-service-new">
            <Plus size={16} aria-hidden />
            {text.create}
          </Link>
        }
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
      </ListToolbar>
      <Card>
        <Table data-testid="data-services-table">
          <THead>
            <tr>
              <TH>{text.columns.name}</TH>
              <TH>{text.columns.kind}</TH>
              <TH>{text.columns.host}</TH>
              <TH>{text.columns.database}</TH>
              <TH>{text.columns.verified}</TH>
              <ActionTH />
            </tr>
          </THead>
          <TBody>
            {q.isLoading || q.error || !view.total ? (
              // 못 불러온 것과 하나도 없는 것을 가른다. 오류를 "없습니다" 로 보이면 새로 만들게 된다
              <StateRow
                colSpan={6}
                loading={q.isLoading}
                error={q.error}
                onRetry={() => void q.refetch()}
                illustration={<DatabaseIllustration />}
                empty={q.data?.length ? t.detail.listControls.noMatch(view.query) : text.empty(tenant)}
              />
            ) : (
              view.pageItems.map((d) => (
                <ListTR key={d.id} data-testid={`data-service-row-${d.name}`}>
                  <NameTD to={`/data-services/${d.id}`} item={d} />
                  <TD>
                    <Badge>{d.kind}</Badge>
                  </TD>
                  <TD className="font-mono text-xs">
                    {d.host}:{d.port}
                  </TD>
                  <TD>{d.kind === "postgres" ? (d.database ?? "-") : (d.db ?? 0)}</TD>
                  <TD>
                    {d.verified_at ? (
                      <Badge variant="ok" title={formatDate(t, d.verified_at)}>
                        {t.dataServices.status.verified}
                      </Badge>
                    ) : (
                      <Badge variant="muted">{t.dataServices.status.unverified}</Badge>
                    )}
                  </TD>
                  <ActionTD>
                    <RowDeleteButton name={d.name} onClick={() => setTarget({ id: d.id, name: d.name })} />
                  </ActionTD>
                </ListTR>
              ))
            )}
          </TBody>
        </Table>
        <Pagination view={view} />
      </Card>
      <ResourceDeleteDialog resource="data-service" target={target} onClose={() => setTarget(null)} />
    </PageWidth>
  );
}
