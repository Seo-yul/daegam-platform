import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listClusters } from "@/api/resources";
import { ListToolbar, Pagination, SearchInput, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateRow } from "@/components/StateBlock";
import { ClusterIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Alert, Card } from "@/components/ui/card";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { useT } from "@/i18n";
import type { DeleteTarget } from "@/lib/deletion";
import { nameSearchText } from "@/lib/names";
import { CLUSTERS_KEY } from "@/lib/queryKeys";

import { useClusterReadOnly } from "./permission";

export function ClusterListPage() {
  const t = useT();
  const text = t.clusters.list;
  // 클러스터는 플랫폼 수준 리소스라 테넌트와 무관하다
  const q = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const readOnly = useClusterReadOnly();

  const view = useListView(q.data ?? [], nameSearchText);

  return (
    <PageWidth data-testid="clusters-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? t.ui.deployment.summary.count(q.data.length) : undefined}
        primary={
          !readOnly ? (
            <Link to="/clusters/new" className={buttonVariants()} data-testid="cluster-new">
              <Plus size={16} aria-hidden />
              {text.register}
            </Link>
          ) : null
        }
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
      </ListToolbar>
      {readOnly ? (
        <Alert variant="info" className="mb-3" data-testid="readonly-notice">
          {t.clusters.readOnly.notice}
        </Alert>
      ) : null}
      <Card>
        <Table data-testid="clusters-table">
          <THead>
            <tr>
              <TH>{text.columns.name}</TH>
              <TH>{text.columns.connection}</TH>
              <TH>{t.clusters.preflight.title}</TH>
              <TH>{t.clusters.preflight.kubernetes}</TH>
              <TH>{t.clusters.preflight.toolhive}</TH>
              <ActionTH />
            </tr>
          </THead>
          <TBody>
            {q.isLoading || q.error || !view.total ? (
              // 못 읽은 것과 하나도 없는 것을 구분한다. "No clusters registered." 는 403 일 때도 같은 문장이었다
              <StateRow
                colSpan={6}
                loading={q.isLoading}
                error={q.error}
                onRetry={() => void q.refetch()}
                illustration={<ClusterIllustration />}
                empty={q.data?.length ? text.noMatch(view.query) : readOnly ? text.emptyReadOnly : text.emptyWritable}
              />
            ) : (
              view.pageItems.map((c) => {
                const pf = c.status.preflight;
                return (
                  <ListTR key={c.id} data-testid={`cluster-row-${c.name}`}>
                    <NameTD to={`/clusters/${c.id}`} item={c}>{c.home ? <Badge variant="info">{text.home}</Badge> : null}</NameTD>
                    <TD>{c.connection.mode}</TD>
                    <TD>
                      {pf ? (
                        <Badge variant={pf.ok ? "ok" : "error"} data-testid={`cluster-preflight-${c.name}`}>
                          {pf.ok ? t.clusters.preflight.ok : t.clusters.preflight.failed}
                        </Badge>
                      ) : (
                        <Badge variant="muted">{t.clusters.preflight.notRun}</Badge>
                      )}
                    </TD>
                    <TD>{pf?.k8s_version ?? "-"}</TD>
                    <TD>{pf?.toolhive_version ?? "-"}</TD>
                    <ActionTD>
                      {/* home 클러스터는 플랫폼이 관리하므로 지울 수 없다. 쓰기 권한이 없으면 아예 보이지 않는다 */}
                      {c.home || readOnly ? null : <RowDeleteButton name={c.name} onClick={() => setTarget({ id: c.id, name: c.name })} />}
                    </ActionTD>
                  </ListTR>
                );
              })
            )}
          </TBody>
        </Table>
        <Pagination view={view} />
      </Card>
      <ResourceDeleteDialog resource="cluster" target={target} onClose={() => setTarget(null)} />
    </PageWidth>
  );
}
