/**
 * Gateway 목록 (D42). 옛 Gateway Profile 목록과 Gateway Instance 목록이 한 표로 합쳐졌다.
 *
 * Gateway 는 vMCP 다. 여러 허브 항목을 하나의 MCP 엔드포인트로 묶어 외부 클라이언트에 낸다. 그래서
 * 백엔드 칸이 "몇 개를 묶었고 그중 몇 개가 살아 있는가" 를 함께 보인다.
 */
import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listGateways } from "@/api/resources";
import { BehindBadge } from "@/components/BehindBadge";
import { ClusterLink } from "@/components/ClusterLink";
import { ErrorMessage } from "@/components/ErrorMessage";
import { DeployedOnlyToggle, ListToolbar, Pagination, SearchInput, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { PhaseDot } from "@/components/PhaseBadge";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateBadge } from "@/components/StateBadge";
import { StateCountLine } from "@/components/StateCountLine";
import { StateRow } from "@/components/StateBlock";
import { GatewayIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead, UrlTD } from "@/components/ui/table";
import { HubPublishDeleteWarning } from "@/pages/gateways/HubPublishDeleteWarning";
import { useT } from "@/i18n";
import { hasScope, useAuthState } from "@/lib/auth";
import type { DeleteTarget } from "@/lib/deletion";
import { countByState, countSummary, isDeployedOrDeploying, shouldPoll } from "@/lib/deployment";
import { degradedBadgeText } from "@/lib/gateway";
import { nameSearchText } from "@/lib/names";
import { useTenant } from "@/lib/tenant";

export function GatewayListPage() {
  const t = useT();
  const text = t.gateways.list;
  const tenant = useTenant();
  const q = useQuery({
    queryKey: ["gateways", tenant],
    queryFn: listGateways,
    refetchInterval: (query) => shouldPoll(query.state.data ?? []),
  });
  const counts = countByState(q.data ?? []);
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const { me } = useAuthState();
  const canDeploy = hasScope(me, tenant, "gateway:deploy");
  const [deployedOnly, setDeployedOnly] = useState(false);

  const view = useListView(
    (q.data ?? []).filter((g) => !deployedOnly || isDeployedOrDeploying(g.state)),
    nameSearchText,
  );

  return (
    <PageWidth data-testid="gateways-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? countSummary(t, counts) : undefined}
        primary={
          <Link to="/gateways/new" className={buttonVariants()} data-testid="gateway-new">
            <Plus size={16} aria-hidden />
            {text.create}
          </Link>
        }
      />
      <StateCountLine counts={counts} prefix="gw" />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
        <DeployedOnlyToggle value={deployedOnly} onChange={setDeployedOnly} />
      </ListToolbar>
      {q.data ? <ErrorMessage error={q.error} /> : null}
      <Card>
        <Table data-testid="gateways-table">
          <THead>
            <tr>
              <TH>{t.gateways.columns.name}</TH>
              <TH>{t.gateways.columns.state}</TH>
              <TH>{t.gateways.columns.revision}</TH>
              <TH>{t.gateways.columns.backends}</TH>
              <TH>{t.gateways.columns.conflict}</TH>
              <TH>{t.gateways.columns.location}</TH>
              <TH>{text.url}</TH>
              <ActionTH className="w-28" />
            </tr>
          </THead>
          <TBody>
            {q.isLoading || (q.error && !q.data) || !view.total ? (
              <StateRow
                colSpan={8}
                loading={q.isLoading}
                error={q.data ? undefined : q.error}
                onRetry={() => void q.refetch()}
                illustration={<GatewayIllustration />}
                empty={
                  view.query
                    ? text.noMatch(view.query)
                    : deployedOnly
                      ? text.noneDeployed
                      : text.empty(
                          tenant,
                          <Link className="font-medium text-fg underline-offset-2 hover:underline" to="/hub" data-testid="goto-hub">
                            {text.emptyLink}
                          </Link>,
                        )
                }
              />
            ) : (
              view.pageItems.map((g) => (
                <ListTR key={g.id} data-testid={`gateway-row-${g.name}`}>
                  <NameTD to={`/gateways/${g.id}`} item={g} />
                  <TD>
                    <span className="inline-flex items-center gap-1.5">
                      <StateBadge state={g.state} testId={`gateway-state-${g.name}`} />
                      {g.status ? <PhaseDot phase={g.status.phase} testId={`gateway-phase-${g.name}`} /> : null}
                      {g.status?.badge ? (
                        <Badge variant="warn" data-testid={`gateway-badge-${g.name}`}>
                          {degradedBadgeText(t, g.status)}
                        </Badge>
                      ) : null}
                    </span>
                  </TD>
                  <TD>
                    r{g.revision}
                    <BehindBadge item={g} testId={`gateway-behind-${g.name}`} />
                  </TD>
                  {/* 묶은 개수(정의)와 살아 있는 개수(상태)를 함께 본다. 안 올렸으면 정의 개수만 뜻이 있다 */}
                  <TD data-testid={`gateway-backends-${g.name}`}>
                    {g.status ? `${g.status.backend_count ?? "?"}/${g.status.expected_backends ?? g.backends.length}` : g.backends.length}
                  </TD>
                  <TD>{g.conflict_resolution}</TD>
                  <TD>
                    <ClusterLink id={g.cluster_id} testId={`gateway-cluster-${g.name}`} />
                    {g.k8s ? <div className="truncate font-mono text-xs text-muted">{g.k8s.namespace}</div> : null}
                  </TD>
                  <UrlTD url={g.status?.url} testId={`gateway-url-${g.name}`} copyTestId={`copy-url-${g.name}`} />
                  <ActionTD className="space-x-1">
                    {g.state === "draft" && canDeploy ? (
                      <Link to={`/gateways/${g.id}?tab=deployment`} className={buttonVariants({ variant: "outline", size: "sm" })} data-testid={`deploy-${g.name}`}>
                        {t.shared.deployDialog.submit}
                      </Link>
                    ) : null}
                    <RowDeleteButton name={g.name} onClick={() => setTarget({ id: g.id, name: g.name })} />
                  </ActionTD>
                </ListTR>
              ))
            )}
          </TBody>
        </Table>
        <Pagination view={view} />
      </Card>
      <ResourceDeleteDialog resource="gateway" target={target} onClose={() => setTarget(null)} extra={<HubPublishDeleteWarning gatewayId={target?.id} />} />
    </PageWidth>
  );
}
