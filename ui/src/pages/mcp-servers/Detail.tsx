/**
 * MCP Server 상세 (D42). 옛 MCP Profile 상세와 MCP Instance 상세가 탭으로 합쳐졌다.
 *
 *  - 개요: 지금 클러스터가 어떤 꼴인지. `draft` 면 아직 볼 것이 없다고 말하고 배포 탭으로 보낸다.
 *  - 정의: 백엔드·도구·런타임·바인딩과 리비전 이력, CR 미리보기.
 *  - 배포: 클러스터·네임스페이스·노출과 배포 동작. 정의가 배포보다 앞서 있으면 여기서 알린다.
 *  - 메트릭: Tool 호출의 수·결과·지연 (P51 5절). 내린 문서도 남은 시간 묶음을 보인다.
 *  - Advanced: 매니페스트 편집과 드리프트. 올라가 있을 때만 뜻이 있다.
 *
 * 문서 상태(state)와 CR phase 는 다른 축이라 머리에 둘을 나란히 둔다. `deployed` 인데 phase 가 Failed 인
 * 경우가 있고, 그 둘을 한 칸으로 뭉치면 무엇이 잘못됐는지 못 읽는다.
 */
import { CloudOff, CloudUpload } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import {
  cloneMcpServer,
  deployMcpServer,
  driftAction,
  getMcpServer,
  getMcpServerManifest,
  getMcpServerMetrics,
  listBackendApis,
  listClusters,
  listDataServices,
  listMcpRevisions,
  patchMcpServer,
  previewRevision,
  putOverridesYaml,
  redeployMcpServer,
  undeployMcpServer,
} from "@/api/resources";
import type { MCPServer, MCPServerRevision, PreviewResponse } from "@/api/types";
import { isBehind } from "@/components/BehindBadge";
import { ClusterLink } from "@/components/ClusterLink";
import { DeployDialog } from "@/components/DeployDialog";
import { DeployConfirmDialogs, DeployPrimaryButton, DeploymentTab, RedeployError, useDeployControls } from "@/components/detail/DeployControls";
import { CloneDialog, EXPOSURE_KIT } from "@/components/detail/CloneDialog";
import { DeploymentAlerts } from "@/components/detail/DeploymentAlerts";
import { ManifestTab } from "@/components/detail/ManifestTab";
import { MissingResourceScreen } from "@/components/detail/MissingResource";
import { McpMetricsTab } from "@/components/mcp/McpMetricsTab";
import { DetailActions } from "@/components/DetailActions";
import { ErrorMessage } from "@/components/ErrorMessage";
import { ExposureBadges } from "@/components/ExposureStatus";
import { PageHeader } from "@/components/Layout";
import { AddressCard } from "@/components/overview/AddressCard";
import { ConditionsDetails } from "@/components/overview/ConditionsDetails";
import { ConnectCard } from "@/components/overview/ConnectCard";
import { DefinitionSummaryCard } from "@/components/overview/DefinitionSummaryCard";
import { OverviewGrid } from "@/components/overview/OverviewGrid";
import { PreviewPicker, RevisionDiffSelects, useRevisionDiff } from "@/components/overview/RevisionDiff";
import { RevisionBadges, RevisionDiffView } from "@/components/overview/RevisionDiffView";
import { ReadinessCard } from "@/components/overview/ReadinessCard";
import { RecentCard, recentEvents } from "@/components/overview/RecentCard";
import { PageWidth } from "@/components/PageWidth";
import { McpServerDeleteWarning, PublishMcpServerDialog, PublishedBadge, UnpublishMcpServerDialog, usePublishRights } from "@/components/PublishSection";
import { queryGate } from "@/components/QueryGate";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { formatResources, isDefaultResources } from "@/components/ResourcesEditor";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { StateBadge, isLiveState } from "@/components/StateBadge";
import { ServerToolRules } from "@/components/ToolRulesSection";
import { ToolList } from "@/components/VerdictPanel";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { TabBar } from "@/components/ui/tabs";
import { authBoundary, hasScope, useAuthState } from "@/lib/auth";
import { shouldPoll } from "@/lib/deployment";
import { displayName, secondaryName } from "@/lib/names";
import { CLUSTERS_KEY, dataServicesKey } from "@/lib/queryKeys";
import { STANDARD_TABS, useTabParam } from "@/lib/tabs";
import { useTenant } from "@/lib/tenant";
import { ruledCount } from "@/lib/toolRules";
import { formatDate } from "@/lib/utils";
import { describeWarning, missingResource, unshownWarnings } from "@/lib/warningText";
import { toYaml } from "@/lib/yaml";

const TABS = STANDARD_TABS;

function specOf(r: MCPServerRevision) {
  const { revision: _r, created_at: _c, ...spec } = r;
  return spec;
}

export function McpServerDetailPage() {
  const t = useT();
  const text = t.mcpServers.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  // 목록과 같은 검사다. 한쪽만 걸면 목록에서 안 보이던 버튼이 상세에서 보인다. 훅이므로 조회
  // 결과를 기다리는 early return 보다 위에 둔다
  const { me } = useAuthState();
  const canDeploy = hasScope(me, tenant, "mcp:deploy");
  const navigate = useNavigate();
  const location = useLocation();
  const deployWarnings: string[] = (location.state as { warnings?: string[] } | null)?.warnings ?? [];

  const q = useQuery({
    queryKey: ["mcp-servers", tenant, id],
    queryFn: () => getMcpServer(id),
    // 안 올린 문서는 클러스터에서 올 소식이 없다
    refetchInterval: (query) => (query.state.data ? shouldPoll([query.state.data]) : false),
  });
  const revisions = useQuery({ queryKey: ["mcp-servers", tenant, id, "revisions"], queryFn: () => listMcpRevisions(id) });
  const backends = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis });
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  // 개요와 정의 탭이 같은 이름표를 쓴다
  const backendName = (bid: string) => backends.data?.find((b) => b.id === bid)?.name;
  const backendToolCount = (bid: string) => backends.data?.find((b) => b.id === bid)?.tool_count ?? null;
  const dsName = (dsId: string) => dataServices.data?.find((d) => d.id === dsId)?.name ?? dsId;

  const [tab, setTab] = useTabParam(TABS, "overview");
  const controls = useDeployControls({
    resource: "mcp-server",
    collection: "mcp-servers",
    id,
    api: { deploy: deployMcpServer, redeploy: redeployMcpServer, undeploy: undeployMcpServer },
  });
  const { deploy, invalidate } = controls;
  const [cloning, setCloning] = useState(false);
  const [deletingOpen, setDeletingOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [unpublishing, setUnpublishing] = useState(false);

  const rights = usePublishRights(q.data);

  const missing = missingResource(q.error, q.data);
  if (missing) return <MissingResourceScreen missing={missing} resource={t.resources.kind["mcp-server"]} backTo="/mcp-servers" backLabel={text.back} />;
  const gate = queryGate(q);
  if (gate) return gate;
  const s = q.data!;
  const st = s.status;
  const live = isLiveState(s.state);
  const busy = controls.busy(s.state);
  // 재배포 응답에는 경고가 없다(api/mcp_servers.py redeploy). 배포 응답으로 이미 보인 것만 상태 쪽에서 뺀다
  const statusWarnings = unshownWarnings(st?.warnings ?? [], deployWarnings);

  // 머리에 남는 것은 주 동작 하나와 편집, 그리고 게시다 (D44 둘째). 게시를 더보기에서 꺼낸 것은
  // 이것이 "만든 것을 남이 쓰게 하는" 단계라 배포 다음으로 자주 누르기 때문이다. 게시는 수동이고
  // (D23) 올라가 있어야 가리킬 엔드포인트가 생긴다
  const publishAction = s.hub_item
    ? rights.canUnpublish
      ? { label: t.detail.publishSection.unpublish, icon: <CloudOff size={14} />, testId: "unpublish-button", onClick: () => setUnpublishing(true) }
      : null
    : s.state === "deployed" && rights.canPublish
      ? { label: t.detail.publishSection.publishTitle, icon: <CloudUpload size={14} />, testId: "publish-button", onClick: () => setPublishing(true) }
      : null;

  // Monaco 가 든 Advanced 탭만 폭 상한을 끈다 (D44 첫째)
  return (
    <PageWidth full={tab === "advanced"} data-testid="mcp-server-detail-page" className="flex flex-col gap-4">
      {controls.announcer}
      <PageHeader
        title={displayName(s)}
        name={secondaryName(s)}
        testId="page-title"
        badges={
          <>
            <StateBadge state={s.state} />
            <PublishedBadge server={s} />
          </>
        }
        primary={<DeployPrimaryButton controls={controls} state={s.state} canDeploy={canDeploy} />}
        secondary={
          <>
            {publishAction ? (
              <Button variant="secondary" data-testid={publishAction.testId} disabled={busy} onClick={publishAction.onClick}>
                {publishAction.icon}
                {publishAction.label}
              </Button>
            ) : null}
            <Link to={`/mcp-servers/${s.id}/suggest`} className={buttonVariants({ variant: "secondary" })} data-testid="suggest-button">
              {t.mcpServers.suggest.title}
            </Link>
            <Link to={`/mcp-servers/${s.id}/edit`} className={buttonVariants({ variant: "secondary" })} data-testid="edit-button">
              {t.shared.detailActions.editRevision}
            </Link>
            <DetailActions
              busy={busy}
              onUndeploy={live && canDeploy ? () => controls.setConfirmingUndeploy(true) : undefined}
              onClone={() => setCloning(true)}
              onDelete={() => setDeletingOpen(true)}
            />
          </>
        }
      />

      <DeployDialog
        open={controls.deploying}
        title={text.deployTitle}
        name={s.name}
        description={text.deployBody(s.revision)}
        pending={deploy.isPending}
        error={deploy.error}
        onSubmit={(body) => deploy.mutate(body)}
        onClose={() => controls.setDeploying(false)}
      />
      <DeployConfirmDialogs
        controls={controls}
        item={s}
        redeployBody={isBehind(s) ? text.redeployBehind(s.deployed_revision, s.revision) : text.redeployBody(s.revision)}
        undeployBody={
          <>
            <p>{text.undeployBody}</p>
            {s.hub_item ? <p className="mt-1 text-warn-fg">{text.undeployPublished}</p> : null}
          </>
        }
      />
      <CloneDialog
        open={cloning}
        onClose={() => setCloning(false)}
        resource={t.resources.kind["mcp-server"]}
        collection="mcp-servers"
        source={s}
        sourceNoun="definition"
        placeholders={text.clonePlaceholders}
        previewPrefix="mcp"
        exposure={EXPOSURE_KIT}
        clone={(body) => cloneMcpServer(s.id, body)}
      />
      <ResourceDeleteDialog
        resource="mcp-server"
        target={deletingOpen ? { id: s.id, name: s.name } : null}
        onClose={() => setDeletingOpen(false)}
        onDeleted={() => navigate("/mcp-servers")}
        extra={<McpServerDeleteWarning server={s} />}
      />
      <PublishMcpServerDialog server={s} open={publishing} onClose={() => setPublishing(false)} />
      <UnpublishMcpServerDialog server={s} open={unpublishing} onClose={() => setUnpublishing(false)} />

      {deployWarnings.map((w) => {
        const d = describeWarning(t, w);
        return (
          <Alert key={w} variant="warn" data-testid={`warning-${d.code ?? "other"}`}>
            {d.text}
          </Alert>
        );
      })}
      {/* 배포 응답으로 이미 보인 경고는 뺀다. 안 그러면 방금 배포한 직후 같은 문장이 두 번 선다 */}
      {statusWarnings.map((w) => (
        <Alert key={w} variant="warn" data-testid="status-warning">
          {describeWarning(t, w).text}
        </Alert>
      ))}
      <RedeployError controls={controls} />
      <DeploymentAlerts
        stale={Boolean(q.error)}
        lastError={s.last_error}
        status={st}
        item={s}
        live={live}
        canDeploy={canDeploy}
        busy={busy}
        onRedeploy={() => controls.setConfirmingRedeploy(true)}
        onOpenAdvanced={() => setTab("advanced")}
        manifestLabel={text.manifestLink}
        subject="definition"
      />

      <TabBar
        tabs={TABS}
        value={tab}
        onChange={setTab}
        label={(key) => t.ui.tabs.standard[key]}
        disabled={(key) => key === "advanced" && !live}
        reason={live ? null : t.detail.deployable.advancedAfterDeploy}
      />

      {tab === "overview" ? (
        <OverviewTab
          server={s}
          revisions={revisions.data ?? []}
          backendName={backendName}
          backendToolCount={backendToolCount}
          dsName={dsName}
          onDeploy={() => setTab("deployment")}
        />
      ) : null}
      {tab === "definition" ? (
        <DefinitionTab
          server={s}
          revisions={revisions.data ?? []}
          revisionsLoading={revisions.isLoading}
          revisionsError={revisions.error}
          backendName={backendName}
          dsName={dsName}
          clusters={(clusters.data ?? []).map((c) => ({ id: c.id, name: c.name }))}
        />
      ) : null}
      {tab === "deployment" ? (
        <DeploymentTab
          item={s}
          canDeploy={canDeploy}
          onDeploy={() => controls.setDeploying(true)}
          onSaved={invalidate}
          emptyBody={text.deployHint(s.revision)}
          hint={text.deploymentHint}
          saveExposure={(exposure) => patchMcpServer(s.id, { exposure })}
          lines={<EffectiveLine server={s} />}
        />
      ) : null}
      {tab === "metrics" ? <McpMetricsTab queryKey={["mcp-servers", tenant, s.id]} load={(period, tz) => getMcpServerMetrics(s.id, period, tz)} /> : null}
      {tab === "advanced" && live ? (
        <ManifestTab
          queryKey={["mcp-servers", tenant, s.id]}
          load={() => getMcpServerManifest(s.id)}
          save={(_doc, yaml) => putOverridesYaml(s.id, yaml)}
          drift={(action) => driftAction(s.id, action)}
        />
      ) : null}
    </PageWidth>
  );
}

/**
 * 개요: 제품 뷰다 (D44 셋째). 알고 싶은 순서대로 카드 다섯이고 K8s conditions 는 접힌 상세로 내려간다.
 * 안 올렸으면 볼 것이 없다고 말하고 배포 탭으로 보낸다.
 */
function OverviewTab({
  server,
  revisions,
  backendName,
  backendToolCount,
  dsName,
  onDeploy,
}: {
  server: MCPServer;
  revisions: MCPServerRevision[];
  backendName: (id: string) => string | undefined;
  backendToolCount: (id: string) => number | null;
  dsName: (id: string) => string;
  onDeploy: () => void;
}) {
  const t = useT();
  const text = t.mcpServers.detail;
  const authMode = useAuthState().mode;
  const st = server.status;
  if (!st) {
    return (
      <>
        <Card data-testid="overview-empty">
          <CardContent className="flex flex-col items-start gap-3 py-8">
            <p className="text-sm font-medium text-heading">{t.detail.deployable.notDeployed}</p>
            <p className="text-sm text-muted">{text.saved(server.revision)}</p>
            <Button data-testid="overview-deploy" onClick={onDeploy}>
              {t.detail.connectCard.toDeploy}
            </Button>
          </CardContent>
        </Card>
        <ReferencesCard resource="mcp-server" id={server.id} />
      </>
    );
  }
  return (
    <>
      <OverviewGrid
        left={
          <>
            <AddressCard
              url={st.url}
              innerLabel={t.detail.deployable.innerEndpoint}
              innerHint={text.innerHint}
              externalUrl={st.external_url}
              onExpose={onDeploy}
              auth={server.exposure.enabled ? text.authExposed : text.authInternal}
              badges={<ExposureBadges exposure={server.exposure} status={st.exposure} warnings={st.warnings} front />}
            />
            {server.exposure.enabled ? (
              // MCP Server 응답에는 인증 경계 칸이 없다. 앞단 프록시는 플랫폼 인증 모드를 그대로 따른다
              <ConnectCard
                name={server.k8s?.name ?? server.name}
                displayName={displayName(server)}
                tenant={server.tenant}
                externalUrl={st.external_url}
                auth={authBoundary(authMode)}
                onDeploy={onDeploy}
              />
            ) : null}
            <ReferencesCard resource="mcp-server" id={server.id} />
            <RecentCard
              events={recentEvents({
                createdAt: server.created_at,
                revisions,
                lastSpecChange: server.last_spec_change,
                deployedRevision: server.deployed_revision,
                since: st.since,
                observedAt: st.observed_at,
              })}
            />
          </>
        }
        right={
          <>
            <ReadinessCard
              phase={st.phase}
              ready={st.ready_replicas}
              wanted={server.runtime.replicas}
              conditions={st.conditions}
              observedAt={st.observed_at}
              deployedRevision={server.deployed_revision}
              revision={server.revision}
              location={
                <>
                  <ClusterLink id={server.cluster_id} />{" "}
                  {server.k8s ? (
                    <span className="font-mono text-xs text-muted">
                      {server.k8s.namespace}/{server.k8s.name}
                    </span>
                  ) : null}
                </>
              }
              message={st.message}
              error={st.error}
            />
            <DefinitionSummaryCard
              revision={server.revision}
              rows={[
                {
                  label: t.mcpServers.backends,
                  testId: "summary-backends",
                  value: server.backends.length ? (
                    <ul className="flex flex-col gap-0.5">
                      {server.backends.map((b, i) => (
                        <li key={`${b.backend_profile_id}-${i}`}>
                          <BackendLabel backend={b} backendName={backendName} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    "-"
                  ),
                },
                {
                  label: "Tool",
                  testId: "summary-tools",
                  value: <ToolSummary server={server} backendToolCount={backendToolCount} />,
                },
                {
                  label: t.detail.deployable.runtime,
                  testId: "summary-runtime",
                  value: (
                    <>
                      {t.detail.deployable.labels.replicas} {server.runtime.replicas}, <span className={cn("text-xs", !isDefaultResources(server.runtime.resources) && "font-mono")}>{formatResources(t, server.runtime.resources)}</span>
                    </>
                  ),
                },
                {
                  label: t.mcpServers.bindings,
                  testId: "summary-bindings",
                  value: <BindingLines bindings={server.bindings} dsName={dsName} />,
                },
              ]}
            />
          </>
        }
      />
      <ConditionsDetails conditions={st.conditions} />
    </>
  );
}

/** 백엔드 한 줄: Backend API 링크(지워졌으면 그 표시)와 스펙 버전. 개요와 정의 탭이 같이 쓴다. */
function BackendLabel({ backend, backendName }: { backend: MCPServer["backends"][number]; backendName: (id: string) => string | undefined }) {
  const t = useT();
  const name = backendName(backend.backend_profile_id);
  return (
    <>
      {name ? (
        <Link className="underline-offset-2 hover:underline" to={`/backend-apis/${backend.backend_profile_id}`}>
          {name}
        </Link>
      ) : (
        t.ui.names.deleted
      )}{" "}
      <span className="text-muted">v{backend.spec_version}</span>
    </>
  );
}

/** 바인딩 줄들: `PREFIX → Database 이름`. 없으면 "-" 다. 개요와 정의 탭이 같이 쓴다. */
function BindingLines({ bindings, dsName }: { bindings: MCPServer["bindings"]; dsName: (id: string) => string }) {
  if (!bindings.length) return "-";
  return bindings.map((b) => (
    <div key={b.env_prefix}>
      <span className="font-mono text-xs">{b.env_prefix}</span> → {dsName(b.data_service_id)}
    </div>
  ));
}

/** 백엔드 요약에서 이름으로 보이는 Tool 의 수. 나머지는 수로 센다. */
const SUMMARY_TOOLS = 6;

/** "도구 n / m". 고른 것이 없으면 스펙의 도구를 전부 내보낸다는 뜻이라 총 개수만 적는다. */
function ToolSummary({ server, backendToolCount }: { server: MCPServer; backendToolCount: (id: string) => number | null }) {
  const t = useT();
  const picked = server.backends.reduce((sum, b) => sum + b.tools.length, 0);
  const total = server.backends.reduce((sum, b) => sum + (backendToolCount(b.backend_profile_id) ?? 0), 0);
  const names = server.backends.flatMap((b) => b.tools.map((tool) => b.tool_overrides[tool]?.name || tool));
  // 누가 부를지의 대상이나 인자 조건을 둔 Tool 의 수 (P52 7절). 읽기 전용 표시만 둔 것은 세지 않는다
  const ruled = server.backends.reduce((sum, b) => sum + ruledCount(b.tool_rules ?? {}), 0);
  return (
    <>
      <span>{picked ? `${picked} / ${total || "?"}` : t.mcpServers.detail.allTools(total || null)}</span>
      {ruled ? (
        <div className="text-xs text-muted" data-testid="summary-ruled-tools">
          {t.toolRules.ruledTools(ruled)}
        </div>
      ) : null}
      {names.length ? (
        <div className="mt-1">
          <ToolList names={names} limit={SUMMARY_TOOLS} testId="mcp-server-tool-summary" />
        </div>
      ) : null}
    </>
  );
}

/** 정의: 무엇을 노출하기로 했는가. 리비전 이력과 그 사이의 diff, 올리면 생길 CR 미리보기. */
function DefinitionTab({
  server,
  revisions,
  revisionsLoading,
  revisionsError,
  backendName,
  dsName,
  clusters,
}: {
  server: MCPServer;
  revisions: MCPServerRevision[];
  revisionsLoading: boolean;
  revisionsError: unknown;
  backendName: (id: string) => string | undefined;
  dsName: (id: string) => string;
  clusters: { id: string; name: string }[];
}) {
  const t = useT();
  const columns = t.mcpServers.detail.revisionColumns;
  const diffState = useRevisionDiff(revisions, specOf);

  const [previewRev, setPreviewRev] = useState(String(server.revision));
  const [previewCluster, setPreviewCluster] = useState("");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const runPreview = useMutation({
    mutationFn: () => previewRevision(server.id, Number(previewRev), previewCluster || null),
    onSuccess: setPreview,
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="ms-summary">
          <CardHeader>
            <CardTitle>{t.detail.summaryFields.title}</CardTitle>
            <Badge data-testid="ms-revision">r{server.revision}</Badge>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
              <dt className="text-muted">{t.detail.summaryFields.id}</dt>
              <dd className="font-mono text-xs">{server.id}</dd>
              <dt className="text-muted">{t.mcpServers.detail.labels.backends}</dt>
              <dd data-testid="ms-backend" className="flex flex-col gap-0.5">
                {server.backends.map((b, i) => (
                  <span key={`${b.backend_profile_id}-${i}`}>
                    <BackendLabel backend={b} backendName={backendName} />
                  </span>
                ))}
              </dd>
              <dt className="text-muted">{t.mcpServers.detail.labels.tools}</dt>
              <dd data-testid="ms-tools" className="flex flex-col gap-1">
                {server.backends.map((b, i) => (
                  <div key={`tools-${b.backend_profile_id}-${i}`}>
                    {server.backends.length > 1 ? <span className="mr-1 text-xs text-muted">{backendName(b.backend_profile_id) ?? t.ui.names.deleted}:</span> : null}
                    {b.tools.length ? (
                      <ul className="inline-flex flex-wrap gap-1">
                        {b.tools.map((tool) => (
                          <li key={tool}>
                            <Badge variant="muted" className="font-mono">
                              {b.tool_overrides[tool]?.name ? `${tool} → ${b.tool_overrides[tool].name}` : tool}
                            </Badge>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span className="text-dim">{t.mcpServers.detail.allSpecTools}</span>
                    )}
                  </div>
                ))}
              </dd>
              <dt className="text-muted">{t.mcpServers.detail.labels.runtime}</dt>
              <dd data-testid="ms-runtime">
                {t.mcpServers.detail.labels.image} {server.runtime.image ?? <span className="text-dim">{t.shared.defaultValue}</span>}, {t.detail.deployable.labels.replicas} {server.runtime.replicas},{" "}
                {t.detail.deployable.labels.resources}{" "}
                <span className={cn("text-xs", !isDefaultResources(server.runtime.resources) && "font-mono")}>{formatResources(t, server.runtime.resources)}</span>
              </dd>
              <dt className="text-muted">{t.mcpServers.detail.labels.bindings}</dt>
              <dd data-testid="ms-bindings">
                <BindingLines bindings={server.bindings} dsName={dsName} />
              </dd>
              <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
              <dd>{formatDate(t, server.updated_at)}</dd>
            </dl>
          </CardContent>
        </Card>

        <Card data-testid="preview-card">
          <CardHeader>
            <CardTitle>{t.mcpServers.detail.labels.preview}</CardTitle>
            <PreviewPicker
              revisions={server.revisions}
              clusters={clusters}
              revision={previewRev}
              onRevision={setPreviewRev}
              cluster={previewCluster}
              onCluster={setPreviewCluster}
              pending={runPreview.isPending}
              onRun={() => runPreview.mutate()}
            />
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <ErrorMessage error={runPreview.error} testId="preview-error" />
            {preview ? (
              <>
                <div className="text-xs text-muted">
                  {t.mcpServers.detail.labels.cluster} <span className="font-mono">{clusters.find((c) => c.id === preview.cluster_id)?.name ?? preview.cluster_id}</span>,{" "}
                  {t.mcpServers.detail.labels.namespace}{" "}
                  <span className="font-mono" data-testid="preview-namespace">
                    {preview.namespace}
                  </span>
                </div>
                <pre className="max-h-96 overflow-auto rounded-md bg-code p-3 font-mono text-xs text-code-fg" data-testid="preview-yaml">
                  {toYaml(preview.manifest)}
                </pre>
                <details className="text-xs text-muted">
                  <summary>{t.mcpServers.detail.labels.lockedPaths(preview.locked.length)}</summary>
                  <ul className="mt-1 list-disc pl-4 font-mono">
                    {preview.locked.map((l) => (
                      <li key={l}>{l}</li>
                    ))}
                  </ul>
                </details>
              </>
            ) : (
              <p className="text-sm text-muted">{t.detail.deployable.previewHint}</p>
            )}
          </CardContent>
        </Card>
      </div>

      <ServerToolRules server={server} />

      <Card data-testid="revisions-card">
        <CardHeader>
          <CardTitle>{t.mcpServers.detail.labels.revisions}</CardTitle>
          <RevisionDiffSelects revisions={revisions} state={diffState} />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <ErrorMessage error={revisionsError} />
          <Table data-testid="revisions-table">
            <THead>
              <tr>
                <TH>{t.mcpServers.revision}</TH>
                {/* 값이 v2 인데 무엇의 버전인지 "스펙" 만으로는 알 수 없다. 정의 요약은 "백엔드 orders v2" 다 */}
                <TH>{columns.backendVersion}</TH>
                {/* Backend API 상세·MCP Hub 상세와 같은 말을 쓴다 */}
                <TH>{t.shared.columns.tools}</TH>
                <TH>{columns.image}</TH>
                <TH>{t.detail.deployable.replicas}</TH>
                <TH>{t.mcpServers.bindings}</TH>
                <TH>{columns.created}</TH>
              </tr>
            </THead>
            <TBody>
              {revisions.length ? (
                revisions.map((r) => (
                  <TR key={r.revision} data-testid={`revision-row-${r.revision}`} className={r.revision === server.revision ? "bg-surface-strong" : ""}>
                    <TD>
                      <RevisionBadges revision={r.revision} current={server.revision} deployed={server.deployed_revision} />
                    </TD>
                    <TD>{r.backends.map((b) => `v${b.spec_version}`).join(", ")}</TD>
                    <TD>{r.backends.reduce((n, b) => n + (b.tools?.length ?? 0), 0)}</TD>
                    <TD className="font-mono text-xs">{r.runtime.image ?? "-"}</TD>
                    <TD>{r.runtime.replicas}</TD>
                    <TD>{r.bindings.length}</TD>
                    <TD>{formatDate(t, r.created_at)}</TD>
                  </TR>
                ))
              ) : (
                <EmptyRow colSpan={7}>{revisionsLoading ? t.common.loading : t.detail.deployable.noRevisions}</EmptyRow>
              )}
            </TBody>
          </Table>
          <RevisionDiffView state={diffState} />
        </CardContent>
      </Card>
    </div>
  );
}

/** 배포 탭의 effective 줄. 서버가 해소한 이미지·레플리카·자원이다. */
function EffectiveLine({ server }: { server: MCPServer }) {
  const t = useT();
  const effective = server.effective_config as { images?: { runtime_image?: string }; replicas?: number; resources?: unknown } | null;
  if (!effective) return null;
  return (
    <>
      <dt className="text-muted">{t.mcpServers.detail.labels.effective}</dt>
      <dd className="text-xs text-muted" data-testid="effective-line">
        {t.mcpServers.detail.labels.image} <span className="font-mono">{effective.images?.runtime_image ?? "?"}</span>, {t.detail.deployable.labels.replicas} {effective.replicas ?? "?"},{" "}
        {t.detail.deployable.labels.resources}{" "}
        <span data-testid="effective-resources">{formatResources(t, effective.resources)}</span>
      </dd>
    </>
  );
}
