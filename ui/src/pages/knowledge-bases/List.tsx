import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listDataServices, listKnowledgeBases } from "@/api/resources";
import { KnowledgeBaseStateBadge } from "@/components/KnowledgeBadges";
import { ListToolbar, Pagination, SearchInput, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateRow } from "@/components/StateBlock";
import { KnowledgeBaseIllustration } from "@/components/illustrations";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { useT } from "@/i18n";
import { embeddingText } from "@/lib/knowledge";
import { displayName, nameSearchText } from "@/lib/names";
import { dataServicesKey, knowledgeBasesKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

import { KnowledgeBaseDeleteDialog, type KnowledgeBaseDeleteTarget } from "./DeleteDialog";

/** Tenant › Knowledge 목록 (D32). 표시 이름·name·Database·임베딩(provider/model/차원)·문서 수(indexed/documents)·상태 칩·삭제. */
export function KnowledgeBaseListPage() {
  const t = useT();
  const text = t.knowledgeBases.list;
  const tenant = useTenant();
  const q = useQuery({ queryKey: knowledgeBasesKey(tenant), queryFn: listKnowledgeBases });
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });
  const [target, setTarget] = useState<KnowledgeBaseDeleteTarget | null>(null);

  const view = useListView(q.data ?? [], nameSearchText);
  const dsName = (id: string) => {
    const d = dataServices.data?.find((x) => x.id === id);
    return d ? displayName(d) : id;
  };

  return (
    <PageWidth data-testid="knowledge-base-list-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? t.ui.deployment.summary.count(q.data.length) : undefined}
        primary={
          <Link to="/knowledge-bases/new" className={buttonVariants()} data-testid="knowledge-base-new">
            <Plus size={16} aria-hidden />
            {text.create}
          </Link>
        }
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
      </ListToolbar>
      <Card>
        <Table data-testid="knowledge-bases-table">
          <THead>
            <tr>
              <TH>{text.columns.name}</TH>
              <TH>{t.resources.kind["data-service"]}</TH>
              <TH>{text.columns.embedding}</TH>
              <TH>{text.columns.documents}</TH>
              <TH>{text.columns.state}</TH>
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
                illustration={<KnowledgeBaseIllustration />}
                empty={q.data?.length ? t.detail.listControls.noMatch(view.query) : text.empty(tenant)}
              />
            ) : (
              view.pageItems.map((kb) => (
                <ListTR key={kb.id} data-testid={`knowledge-base-row-${kb.name}`}>
                  <NameTD to={`/knowledge-bases/${kb.id}`} item={kb} />
                  <TD>
                    <Link className="underline-offset-2 hover:underline" to={`/data-services/${kb.data_service_id}`}>
                      {dsName(kb.data_service_id)}
                    </Link>
                  </TD>
                  <TD className="font-mono text-xs">{embeddingText(kb)}</TD>
                  <TD data-testid={`knowledge-base-docs-${kb.name}`}>
                    {kb.status.indexed}/{kb.status.documents}
                    {kb.status.failed ? <span className="ml-1 text-xs text-err-fg">{t.shared.failedDocuments(kb.status.failed)}</span> : null}
                  </TD>
                  <TD>
                    <KnowledgeBaseStateBadge kb={kb} testId={`knowledge-base-state-${kb.name}`} />
                    {kb.state === "orphaned_table" ? <span className="ml-2 font-mono text-xs text-muted">{kb.table}</span> : null}
                  </TD>
                  <ActionTD>
                    <RowDeleteButton name={kb.name} onClick={() => setTarget({ id: kb.id, name: kb.name, table: kb.table })} />
                  </ActionTD>
                </ListTR>
              ))
            )}
          </TBody>
        </Table>
        <Pagination view={view} />
      </Card>
      <KnowledgeBaseDeleteDialog target={target} onClose={() => setTarget(null)} />
    </PageWidth>
  );
}
