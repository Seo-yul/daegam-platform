/**
 * MCP Server 목록 (D42). 옛 MCP Profile 목록과 MCP Instance 목록이 한 표로 합쳐졌다.
 *
 * 문서 하나가 정의와 배포를 함께 갖는다. 그래서 만든 적은 있으나 아직 안 올린 `draft` 도 같은 표에 선다.
 * 점 요약 줄이 미배포를 따로 세는 것은 그 때문이다. 옛 목록에서는 "인스턴스가 없다" 가 곧 "아무것도 없다"
 * 였지만 이제는 정의만 있고 안 올린 상태가 정상적인 한 칸이다.
 */
import { CloudOff, CloudUpload, Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listMcpServers } from "@/api/resources";
import type { MCPServer } from "@/api/types";
import { BackendApiLink } from "@/components/BackendApiLink";
import { BehindBadge } from "@/components/BehindBadge";
import { ClusterLink } from "@/components/ClusterLink";
import { ErrorMessage } from "@/components/ErrorMessage";
import { DeployedOnlyToggle, ListToolbar, Pagination, SearchInput, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { PhaseDot } from "@/components/PhaseBadge";
import { McpServerDeleteWarning, PublishMcpServerDialog, UnpublishMcpServerDialog } from "@/components/PublishSection";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateBadge } from "@/components/StateBadge";
import { StateCountLine } from "@/components/StateCountLine";
import { StateRow } from "@/components/StateBlock";
import { McpServerIllustration } from "@/components/illustrations";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead, UrlTD } from "@/components/ui/table";
import { useT } from "@/i18n";
import { hasScope, useAuthState } from "@/lib/auth";
import type { DeleteTarget } from "@/lib/deletion";
import { countByState, countSummary, isDeployedOrDeploying, shouldPoll } from "@/lib/deployment";
import { nameSearchText } from "@/lib/names";
import { useTenant } from "@/lib/tenant";

export function McpServerListPage() {
  const t = useT();
  const text = t.mcpServers.list;
  const tenant = useTenant();
  const q = useQuery({
    queryKey: ["mcp-servers", tenant],
    queryFn: listMcpServers,
    // 전부 `draft` 면 클러스터에서 올 소식이 없다. 그때까지 5초마다 다시 묻지 않는다
    refetchInterval: (query) => shouldPoll(query.state.data ?? []),
  });
  const counts = countByState(q.data ?? []);
  const published = (q.data ?? []).filter((s) => s.hub_item).length;
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const [publishing, setPublishing] = useState<MCPServer | null>(null);
  const [unpublishing, setUnpublishing] = useState<MCPServer | null>(null);
  const { me } = useAuthState();
  const canPublish = hasScope(me, tenant, "hub:write");
  const canDeploy = hasScope(me, tenant, "mcp:deploy");
  const [deployedOnly, setDeployedOnly] = useState(false);

  const view = useListView(
    (q.data ?? []).filter((s) => !deployedOnly || isDeployedOrDeploying(s.state)),
    nameSearchText,
  );
  const deleting = target ? q.data?.find((s) => s.id === target.id) : undefined;

  return (
    <PageWidth data-testid="mcp-servers-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? countSummary(t, counts) : undefined}
        primary={
          <Link to="/mcp-servers/new" className={buttonVariants()} data-testid="mcp-server-new">
            <Plus size={16} aria-hidden />
            {text.create}
          </Link>
        }
      />
      <StateCountLine counts={counts} prefix="ms" extra={[{ label: text.published, value: published, testId: "ms-count-published" }]} />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
        <DeployedOnlyToggle value={deployedOnly} onChange={setDeployedOnly} />
      </ListToolbar>
      {/* 첫 조회가 실패한 것은 표 안의 StateRow 가 말한다. 여기 배너는 이미 받아 둔 목록을 보이는 중에 폴링이 끊긴 경우다 */}
      {q.data ? <ErrorMessage error={q.error} /> : null}
      <Card>
        <Table data-testid="mcp-servers-table">
          <THead>
            <tr>
              <TH>{text.columns.name}</TH>
              <TH>{text.columns.state}</TH>
              <TH>{t.mcpServers.revision}</TH>
              <TH>{t.mcpServers.backends}</TH>
              <TH>{text.columns.cluster}</TH>
              <TH>{text.url}</TH>
              <TH>{text.columns.hub}</TH>
              <ActionTH className="w-28" />
            </tr>
          </THead>
          <TBody>
            {q.isLoading || (q.error && !q.data) || !view.total ? (
              // 못 불러온 것과 비어 있는 것을 가른다. 조회에 실패했는데 "없습니다" 라고 하면 이미 만들어 둔 사람이 같은 것을 또 만든다
              <StateRow
                colSpan={8}
                loading={q.isLoading}
                error={q.data ? undefined : q.error}
                onRetry={() => void q.refetch()}
                illustration={<McpServerIllustration />}
                empty={
                  view.query
                    ? text.noMatch(view.query)
                    : deployedOnly
                      ? text.noDeployed
                      : text.empty(
                          tenant,
                          <Link className="font-medium text-fg underline-offset-2 hover:underline" to="/backend-apis" data-testid="goto-backend-apis">
                            {text.emptyLink}
                          </Link>,
                        )
                }
              />
            ) : (
              view.pageItems.map((s) => (
                <ListTR key={s.id} data-testid={`mcp-server-row-${s.name}`}>
                  <NameTD to={`/mcp-servers/${s.id}`} item={s} />
                  <TD>
                    <span className="inline-flex items-center gap-1.5">
                      <StateBadge state={s.state} testId={`mcp-server-state-${s.name}`} />
                      {/* 문서 상태와 CR phase 는 다른 축이다. 올려놨는데 파드가 죽어 있는 경우를 한 칸에서 읽는다.
                          위 요약 줄이 같은 색으로 세고 있어 낱말은 겹치므로 색 점만 둔다 */}
                      {s.status ? <PhaseDot phase={s.status.phase} testId={`mcp-server-phase-${s.name}`} /> : null}
                    </span>
                    {s.status?.error ? <div className="mt-0.5 text-xs text-err-fg">{s.status.error}</div> : null}
                  </TD>
                  <TD>
                    r{s.revision}
                    <BehindBadge item={s} testId={`mcp-server-behind-${s.name}`} />
                  </TD>
                  <TD>
                    <BackendApiLink ids={s.backends.map((b) => b.backend_profile_id)} testId={`mcp-server-backends-${s.name}`} />
                  </TD>
                  <TD>
                    <ClusterLink id={s.cluster_id} testId={`mcp-server-cluster-${s.name}`} />
                  </TD>
                  <UrlTD url={s.status?.url} testId={`mcp-server-url-${s.name}`} copyTestId={`copy-url-${s.name}`} />
                  {/* 버튼의 낱말이 지금 상태를 말한다. "게시 해제" 가 보이면 게시된 것이라 배지가 따로 없어도 된다 */}
                  <TD data-testid={`mcp-server-hub-${s.name}`}>
                    {s.hub_item ? (
                      <span className="inline-flex items-center gap-2">
                        <Link className="truncate text-xs underline-offset-2 hover:underline" to={`/hub/${s.hub_item.id}`} data-testid={`hub-link-${s.name}`}>
                          {s.hub_item.name}
                        </Link>
                        {s.hub_item.permissions.manage ? (
                          <button type="button" className={buttonVariants({ variant: "secondary", size: "sm" })} data-testid={`unpublish-button-${s.name}`} onClick={() => setUnpublishing(s)}>
                            <CloudOff size={13} />
                            {t.detail.publishSection.unpublish}
                          </button>
                        ) : null}
                      </span>
                    ) : s.state === "deployed" && canPublish ? (
                      <button type="button" className={buttonVariants({ variant: "secondary", size: "sm" })} data-testid={`publish-button-${s.name}`} onClick={() => setPublishing(s)}>
                        <CloudUpload size={13} />
                        {text.publish}
                      </button>
                    ) : (
                      // 배포해야 가리킬 엔드포인트가 생긴다. 안 올린 것에 게시 버튼을 두면 눌러 보고 나서야 안다
                      <span className="text-xs text-dim">-</span>
                    )}
                  </TD>
                  <ActionTD className="space-x-1">
                    {s.state === "draft" && canDeploy ? (
                      <Link to={`/mcp-servers/${s.id}?tab=deployment`} className={buttonVariants({ variant: "outline", size: "sm" })} data-testid={`deploy-${s.name}`}>
                        {t.shared.deployDialog.submit}
                      </Link>
                    ) : null}
                    <RowDeleteButton name={s.name} onClick={() => setTarget({ id: s.id, name: s.name })} />
                  </ActionTD>
                </ListTR>
              ))
            )}
          </TBody>
        </Table>
        <Pagination view={view} />
      </Card>
      <ResourceDeleteDialog resource="mcp-server" target={target} onClose={() => setTarget(null)} extra={<McpServerDeleteWarning server={deleting} />} />
      <PublishMcpServerDialog server={publishing} open={publishing !== null} onClose={() => setPublishing(null)} />
      <UnpublishMcpServerDialog server={unpublishing} open={unpublishing !== null} onClose={() => setUnpublishing(null)} />
    </PageWidth>
  );
}
