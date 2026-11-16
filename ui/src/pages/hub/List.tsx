/**
 * MCP Hub 목록. 카드와 표 두 모양이고 기본은 카드다 (D47).
 *
 * 두 모양이 함께 쓰는 값은 `hubFacts` 한 곳에서 만들고, 모양마다 그중 무엇을 보일지 고른다. 두 모양에 다 서는 값은
 * testid 도 같아서 어느 모양에서든 같은 것을 찾는다.
 */
import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listHubItems } from "@/api/resources";
import type { HubItem } from "@/api/types";
import { ListToolbar, Pagination, SearchInput, ViewToggle, useListMode, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateBlock, StateRow } from "@/components/StateBlock";
import { ToolList } from "@/components/VerdictPanel";
import { CatalogDialog } from "@/components/hub/CatalogDialog";
import { ClientExportDialog } from "@/components/hub/ClientExportDialog";
import { HubIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CardGrid, ListCard, editedLine } from "@/components/ui/card-grid";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import type { DeleteTarget } from "@/lib/deletion";
import type { StateTone } from "@/lib/deployment";
import { hubKindLabel, hubKindVariant, hubStatusNote, isExternalHubItem } from "@/lib/hub";
import { nameSearchText } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { hubItemsKey } from "@/lib/queryKeys";

/** 허브 항목의 갈래. 상태 배지와 카드의 상태 자리가 이 규칙 하나를 본다. */
export function hubTone(item: HubItem): StateTone {
  if (item.status?.error) return "error";
  if (item.status?.ready) return "ok";
  return item.status?.phase === "Missing" ? "error" : "warn";
}

export function HubStatusBadge({ item, testId }: { item: HubItem; testId?: string }) {
  const t = useT();
  const text = t.hub.list;
  const st = item.status;
  return (
    <Badge variant={hubTone(item)} title={st?.error ?? undefined} data-testid={testId}>
      {st?.error ? text.error : (hubStatusNote(t, st?.phase)?.label ?? st?.phase ?? t.detail.phaseBadge.unknown)}
    </Badge>
  );
}

/** 한 항목의 값. 카드와 표가 여기서 골라 쓴다. */
function hubFacts(t: Messages, item: HubItem) {
  return {
    tone: hubTone(item),
    kind: <Badge variant={hubKindVariant(item.kind)}>{hubKindLabel(t, item.kind)}</Badge>,
    status: <HubStatusBadge item={item} testId={`hub-status-${item.name}`} />,
    // 0 개와 "아직 물어보지 않았다" 는 다르다. 뒤엣것을 "-" 로 적으면 Tool 이 없는 것으로 읽힌다
    tools: item.tool_names.length || (item.tool_names_at ? "0" : t.hub.list.toolsUnknown),
    scope: item.scope,
    // 흐림과 title 만으로는 키보드·터치로 다니는 사람에게 아무 신호도 없다. 뜻을 글자로 적는다
    notConsumable: item.permissions.consume ? null : (
      <Badge variant="muted" data-testid={`hub-not-consumable-${item.name}`}>
        {t.hub.notConsumable.badge}
      </Badge>
    ),
  };
}

/** 카드에서 미리 보이는 Tool 이름의 수. 셋이면 한두 줄에 들고 나머지는 수로 말한다. */
const TOOL_PREVIEW = 3;

/**
 * 카드 몸의 Tool 이름 미리보기. 무엇을 할 수 있는 항목인지가 이름에서 읽힌다. 허브 상세의 Tool 태그(ToolList)와 같은
 * 모양이다. 이름이 없으면(받은 적이 없거나 비었으면) 그리지 않는다. 그 사정은 바닥의 Tool 수가 이미 말한다.
 */
function ToolPreview({ item }: { item: HubItem }) {
  return item.tool_names.length ? <ToolList names={item.tool_names} limit={TOOL_PREVIEW} testId={`hub-tool-preview-${item.name}`} /> : null;
}

export function HubListPage() {
  const t = useT();
  const text = t.hub.list;
  const tenant = useTenant();
  const q = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [mode, setMode] = useListMode("hub");

  const view = useListView(q.data ?? [], nameSearchText);

  // 못 불러온 것과 하나도 없는 것을 가른다. 오류를 "없습니다" 로 보이면 새로 만들게 된다
  const blocked = q.isLoading || Boolean(q.error) || !view.total;
  const state = {
    loading: q.isLoading,
    error: q.error,
    onRetry: () => void q.refetch(),
    illustration: <HubIllustration />,
    empty: q.data?.length
      ? t.detail.listControls.noMatch(view.query)
      : text.empty(
          tenant,
          <Link className="font-medium text-fg underline-offset-2 hover:underline" to="/mcp-servers" data-testid="goto-instances">
            {t.resources.kind["mcp-server"]}
          </Link>,
        ),
  };
  const canDelete = (i: HubItem) => isExternalHubItem(i.kind) && i.permissions.manage;
  const notConsumable = (i: HubItem) => (i.permissions.consume ? undefined : t.hub.notConsumable.title);

  return (
    <PageWidth data-testid="hub-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? t.ui.deployment.summary.count(q.data.length) : undefined}
        primary={
          <Link to="/hub/new" className={buttonVariants()} data-testid="hub-new">
            <Plus size={16} aria-hidden />
            {text.register}
          </Link>
        }
        // 사람의 MCP 클라이언트가 읽을 설정 파일을 이 목록에서 만든다 (P55a 2절)
        secondary={
          <>
            {/* 공개 카탈로그의 서버를 허브 항목으로 가져온다 (P55 8절) */}
            <Button variant="secondary" data-testid="hub-import-catalog" onClick={() => setImporting(true)}>
              {text.importCatalog}
            </Button>
            <Button variant="secondary" data-testid="hub-export-clients" disabled={!q.data?.length} onClick={() => setExporting(true)}>
              {text.exportClients}
            </Button>
          </>
        }
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
        <ViewToggle mode={mode} onChange={setMode} />
      </ListToolbar>
      {mode === "card" ? (
        <>
          {blocked ? (
            <StateBlock {...state} />
          ) : (
            <CardGrid testId="hub-cards">
              {view.pageItems.map((i) => {
                const f = hubFacts(t, i);
                return (
                  // consume 이 없는 카드는 보이되 흐리다. Gateway 의 백엔드로 고를 수 없기 때문이다
                  // (docs/guide/hub-gateway.md 7절)
                  <ListCard
                    key={i.id}
                    to={`/hub/${i.id}`}
                    item={i}
                    data-testid={`hub-row-${i.name}`}
                    data-consumable={i.permissions.consume ? "true" : "false"}
                    title={notConsumable(i)}
                    className={i.permissions.consume ? undefined : "opacity-50"}
                    badge={f.notConsumable}
                    byline={[text.scope(<span data-testid={`hub-scope-${i.name}`}>{f.scope}</span>), editedLine(t, null, i.updated_at)]}
                    // 허브 항목을 알아보게 하는 것은 들어 있는 Tool 이다. 이름을 보면 무엇을 할 수 있는지 안다
                    body={<ToolPreview item={i} />}
                    note={i.status?.error ? <span className="text-err-fg">{i.status.error}</span> : null}
                    footer={
                      <>
                        {f.kind}
                        <span>
                          {text.tool}{" "}
                          <span className="font-medium text-heading" data-testid={`hub-tools-${i.name}`}>
                            {f.tools}
                          </span>
                        </span>
                      </>
                    }
                    // 정상(Ready)이면 배지를 숨기고 낭독기에만 남긴다
                    status={f.status}
                    quietStatus={f.tone === "ok"}
                    menu={canDelete(i) ? [{ label: t.common.delete, danger: true, testId: `row-delete-${i.name}`, onClick: () => setTarget({ id: i.id, name: i.name }) }] : []}
                  />
                );
              })}
            </CardGrid>
          )}
          <div className="mt-2">
            <Pagination view={view} />
          </div>
        </>
      ) : (
        <Card>
          <Table data-testid="hub-table">
            <THead>
              <tr>
                <TH>{text.columns.name}</TH>
                <TH>{text.columns.kind}</TH>
                <TH>{text.columns.status}</TH>
                <TH>{text.columns.tools}</TH>
                <TH>{text.columns.scope}</TH>
                <ActionTH />
              </tr>
            </THead>
            <TBody>
              {blocked ? (
                <StateRow colSpan={6} {...state} />
              ) : (
                view.pageItems.map((i) => {
                  const f = hubFacts(t, i);
                  return (
                    <ListTR
                      key={i.id}
                      data-testid={`hub-row-${i.name}`}
                      data-consumable={i.permissions.consume ? "true" : "false"}
                      title={notConsumable(i)}
                      className={i.permissions.consume ? undefined : "opacity-50"}
                    >
                      <NameTD to={`/hub/${i.id}`} item={i}>
                        {f.notConsumable}
                      </NameTD>
                      <TD>{f.kind}</TD>
                      <TD>{f.status}</TD>
                      <TD data-testid={`hub-tools-${i.name}`}>{f.tools}</TD>
                      <TD data-testid={`hub-scope-${i.name}`}>{f.scope}</TD>
                      <ActionTD>
                        {canDelete(i) ? <RowDeleteButton name={i.name} onClick={() => setTarget({ id: i.id, name: i.name })} /> : null}
                      </ActionTD>
                    </ListTR>
                  );
                })
              )}
            </TBody>
          </Table>
          <Pagination view={view} />
        </Card>
      )}
      <ResourceDeleteDialog resource="hub" target={target} onClose={() => setTarget(null)} />
      <ClientExportDialog open={exporting} onClose={() => setExporting(false)} items={q.data ?? []} />
      <CatalogDialog open={importing} onClose={() => setImporting(false)} />
    </PageWidth>
  );
}
