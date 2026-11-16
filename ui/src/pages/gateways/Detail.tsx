/**
 * Gateway 상세 (D42). 옛 Gateway Profile 상세와 Gateway Instance 상세가 탭으로 합쳐졌다.
 *
 *  - 개요: vMCP 가 지금 어떤 꼴인지와 백엔드 줄. `draft` 면 아직 볼 것이 없다.
 *  - 정의: 묶은 허브 항목과 충돌·실패 방식·런타임, 리비전 이력, vMCP 미리보기.
 *  - 배포: 클러스터·노출·광고 도구와 허브 게시.
 *  - 메트릭: Tool 호출의 수·결과·지연과 백엔드별 줄 (P51 5절).
 *  - Advanced: 문서 묶음(그룹·Entry·vMCP) 편집과 드리프트.
 *
 * 게시는 MCP Server 와 달리 허브 항목을 따로 만드는 일이라 머리 버튼이 아니라 배포 탭의 카드다.
 * 한 Gateway 를 entry 로도 proxy 로도 여러 번 게시할 수 있기 때문이다.
 */
import { CloudOff } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { ApiError } from "@/api/client";
import {
  cloneGateway,
  deployGateway,
  gatewayDriftAction,
  getGateway,
  getGatewayManifest,
  getGatewayMetrics,
  listClusters,
  listGatewayRevisions,
  listHubItems,
  patchGateway,
  previewGatewayRevision,
  publishGateway,
  putGatewayOverridesYaml,
  redeployGateway,
  undeployGateway,
} from "@/api/resources";
import type { Gateway, GatewayBackendReport, GatewayBackendRow, GatewayPreview, GatewayRevision, HubAccess } from "@/api/types";
import { AccessPolicyEditor } from "@/components/AccessPolicyEditor";
import { isBehind } from "@/components/BehindBadge";
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
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { ClusterLink } from "@/components/ClusterLink";
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
import { queryGate } from "@/components/QueryGate";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { formatResources, isDefaultResources } from "@/components/ResourcesEditor";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { StateBadge, isLiveState } from "@/components/StateBadge";
import { Badge, levelVariant, type BadgeProps } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Select } from "@/components/ui/input";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { TabBar } from "@/components/ui/tabs";
import { HubPublishDeleteWarning, publishedFrom } from "@/pages/gateways/HubPublishDeleteWarning";
import { authBoundary, hasScope, takesPlatformTokens, useAuthState } from "@/lib/auth";
import { isProxyPublished } from "@/lib/hub";
import type { DeleteTarget } from "@/lib/deletion";
import { shouldPoll } from "@/lib/deployment";
import { advertisedTools, degradedBadgeText } from "@/lib/gateway";
import { GatewayToolRules } from "@/components/ToolRulesSection";
import { ruledCount } from "@/lib/toolRules";
import { accessForPublish, canShare, emptyAccess } from "@/lib/hubAccess";
import { displayName, secondaryName } from "@/lib/names";
import { CLUSTERS_KEY, hubItemsKey } from "@/lib/queryKeys";
import { STANDARD_TABS, useTabParam } from "@/lib/tabs";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";
import { describeWarning, missingResource, unshownWarnings, warningText } from "@/lib/warningText";
import { toYaml } from "@/lib/yaml";

/**
 * 상태 경고 `hub_access_revoked: <item>` 의 접두사 (docs/guide/hub-gateway.md 7절). 정책이 좁아져 다음 재배포에서
 * 422 가 될 백엔드다.
 */
export const HUB_ACCESS_REVOKED = "hub_access_revoked:";

/** 경고를 접근 회수 항목과 나머지로 나눈다. 회수 항목은 배지로, 나머지는 Alert 로 보인다. */
export function splitAccessRevoked(warnings: string[]): { revoked: string[]; rest: string[] } {
  const revoked: string[] = [];
  const rest: string[] = [];
  for (const w of warnings) {
    if (w.startsWith(HUB_ACCESS_REVOKED)) revoked.push(w.slice(HUB_ACCESS_REVOKED.length).trim());
    else rest.push(w);
  }
  return { revoked, rest };
}

/** 서버 스키마의 backend status 는 string 이다 (vMCP discoveredBackends). 알려진 상태만 색을 입힌다. */
export function backendStateVariant(state: string | null): BadgeProps["variant"] {
  switch (state) {
    case "ready":
      return "ok";
    case "degraded":
    case "pending":
    case "discovering":
      return "warn";
    case "unavailable":
    case "unauthenticated":
    case "invalid":
    case "missing":
      return "error";
    default:
      return "muted";
  }
}

const TABS = STANDARD_TABS;

/** 리비전 diff 대상: 번호·시각을 뺀 스펙 부분. */
function specOf(r: GatewayRevision) {
  return {
    backends: r.backends,
    conflict_resolution: r.conflict_resolution,
    failure_mode: r.failure_mode,
    authz_policy: r.authz_policy,
    runtime: r.runtime,
  };
}

export function GatewayDetailPage() {
  const t = useT();
  const text = t.gateways.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  // 목록과 같은 검사다. 한쪽만 걸면 목록에서 안 보이던 버튼이 상세에서 보인다. 훅이므로 조회
  // 결과를 기다리는 early return 보다 위에 둔다
  const { me } = useAuthState();
  const canDeploy = hasScope(me, tenant, "gateway:deploy");
  const navigate = useNavigate();
  const location = useLocation();
  const navState = location.state as { warnings?: string[]; consumersClosed?: string[] } | null;
  const deployWarnings: string[] = navState?.warnings ?? [];
  const consumersClosed: string[] = navState?.consumersClosed ?? [];

  const q = useQuery({
    queryKey: ["gateways", tenant, id],
    queryFn: () => getGateway(id),
    refetchInterval: (query) => (query.state.data ? shouldPoll([query.state.data]) : false),
  });
  const revisions = useQuery({ queryKey: ["gateways", tenant, id, "revisions"], queryFn: () => listGatewayRevisions(id) });
  const hub = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  // 개요와 정의 탭이 같은 이름표를 쓴다
  const hubName = (hid: string) => hub.data?.find((h) => h.id === hid)?.name ?? hid;

  const [tab, setTab] = useTabParam(TABS, "overview");

  const controls = useDeployControls({
    resource: "gateway",
    collection: "gateways",
    id,
    api: { deploy: deployGateway, redeploy: redeployGateway, undeploy: undeployGateway },
  });
  const { deploy, redeploy, invalidate } = controls;
  const [cloning, setCloning] = useState(false);
  const [deletingOpen, setDeletingOpen] = useState(false);

  const missing = missingResource(q.error, q.data);
  if (missing) return <MissingResourceScreen missing={missing} resource={t.resources.kind.gateway} backTo="/gateways" backLabel={text.backToList} />;
  const gate = queryGate(q);
  if (gate) return gate;
  const g = q.data!;
  const st = g.status;
  const live = isLiveState(g.state);
  const busy = controls.busy(g.state);
  // 재배포 응답의 경고는 다음 폴링 응답에 없으므로 mutation 결과에서 읽는다
  const redeployWarnings = redeploy.data?.warnings ?? [];
  const statusWarnings = splitAccessRevoked(st?.warnings ?? []);
  // 배포 응답으로 이미 보인 경고는 상태 쪽에서 뺀다. 안 그러면 방금 배포한 직후 같은 문장이 두 번 선다
  const unshownStatusWarnings = unshownWarnings(statusWarnings.rest, [...deployWarnings, ...redeployWarnings]);

  // 409 backends_not_ready 의 백엔드 보고. 배포 대화상자 안에 표로 세운다
  const notReady: GatewayBackendReport[] | undefined =
    deploy.error instanceof ApiError && deploy.error.code === "backends_not_ready" ? deploy.error.extra<GatewayBackendReport[]>("backends") : undefined;
  const cyclePath: string[] | undefined = deploy.error instanceof ApiError && deploy.error.code === "cycle" ? deploy.error.extra<string[]>("path") : undefined;

  // Monaco 가 든 Advanced 탭만 폭 상한을 끈다 (D44 첫째)
  return (
    <PageWidth full={tab === "advanced"} data-testid="gateway-detail-page" className="flex flex-col gap-4">
      {controls.announcer}
      <PageHeader
        title={displayName(g)}
        name={secondaryName(g)}
        testId="page-title"
        badges={<StateBadge state={g.state} />}
        primary={<DeployPrimaryButton controls={controls} state={g.state} canDeploy={canDeploy} />}
        secondary={
          <>
            <Link to={`/gateways/${g.id}/edit`} className={buttonVariants({ variant: "secondary" })} data-testid="edit-button">
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
        name={g.name}
        description={text.deployBody(g.revision)}
        pending={deploy.isPending}
        error={deploy.error}
        extra={
          <>
            {notReady ? (
              <Alert variant="error" data-testid="backends-not-ready">
                <div className="mb-1 text-sm font-medium">{text.notReady}</div>
                <Table>
                  <THead>
                    <tr>
                      <TH>{t.gateways.columns.backend}</TH>
                      <TH>{t.gateways.columns.state}</TH>
                      <TH>{t.gateways.columns.ready}</TH>
                      <TH>{t.gateways.columns.error}</TH>
                    </tr>
                  </THead>
                  <TBody>
                    {notReady.map((b) => (
                      <TR key={b.hub_item_id} data-testid={`not-ready-${b.name}`}>
                        <TD>{b.name}</TD>
                        <TD>{b.phase ?? "-"}</TD>
                        <TD>
                          <Badge variant={levelVariant(b.ready)}>{b.ready ? t.detail.summaryFields.yes : t.detail.summaryFields.no}</Badge>
                        </TD>
                        <TD className="font-mono text-xs">{b.error ?? "-"}</TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </Alert>
            ) : null}
            {cyclePath ? (
              <Alert variant="error" data-testid="cycle-path">
                {text.cycle(<span className="font-mono text-xs">{cyclePath.join(" → ")}</span>)}
              </Alert>
            ) : null}
          </>
        }
        onSubmit={(body) => deploy.mutate(body)}
        onClose={() => controls.setDeploying(false)}
      />
      <DeployConfirmDialogs
        controls={controls}
        item={g}
        redeployBody={isBehind(g) ? text.redeployBehind(g.deployed_revision, g.revision) : text.redeploySame(g.revision)}
        undeployBody={<p>{text.undeployBody}</p>}
      />
      <CloneDialog
        open={cloning}
        onClose={() => setCloning(false)}
        resource={t.resources.kind.gateway}
        collection="gateways"
        source={g}
        sourceNoun="definition"
        placeholders={{ display: text.clonePlaceholder, name: "support-gw-copy" }}
        previewPrefix="gw"
        exposure={EXPOSURE_KIT}
        clone={(body) => cloneGateway(g.id, body)}
      />
      <ResourceDeleteDialog
        resource="gateway"
        target={deletingOpen ? { id: g.id, name: g.name } : null}
        onClose={() => setDeletingOpen(false)}
        onDeleted={() => navigate("/gateways")}
        extra={<HubPublishDeleteWarning gatewayId={g.id} />}
      />

      {consumersClosed.length ? (
        <Alert variant="warn" data-testid="consumer-notice">
          {t.gateways.consumerNotice.closed(consumersClosed.join(", "))}
        </Alert>
      ) : null}
      {[...deployWarnings, ...redeployWarnings].map((w) => {
        const d = describeWarning(t, w);
        return (
          <Alert key={w} variant="warn" data-testid={`warning-${d.code ?? "other"}`}>
            {d.text}
          </Alert>
        );
      })}
      {unshownStatusWarnings.map((w) => (
        <Alert key={w} variant="warn" data-testid="status-warning">
          {describeWarning(t, w).text}
        </Alert>
      ))}
      {/* 정책이 좁아진 백엔드는 어느 탭에 있든 보여야 한다. 다음 재배포에서 422 가 될 자리다 */}
      {statusWarnings.revoked.length ? (
        <Alert variant="warn" data-testid="hub-access-revoked">
          <div>{text.revokedBackends(warningText(t, "hub_access_revoked") ?? "", statusWarnings.revoked.join(", "))}</div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {statusWarnings.revoked.map((name) => (
              <Badge key={name} variant="error" title={`hub_access_revoked: ${name}`} data-testid={`hub-access-revoked-badge-${name}`}>
                {t.gateways.detail.labels.accessRevoked(name)}
              </Badge>
            ))}
          </div>
        </Alert>
      ) : null}
      <RedeployError controls={controls} />
      <DeploymentAlerts
        stale={Boolean(q.error)}
        lastError={g.last_error}
        status={st}
        item={g}
        live={live}
        canDeploy={canDeploy}
        busy={busy}
        onRedeploy={() => controls.setConfirmingRedeploy(true)}
        onOpenAdvanced={() => setTab("advanced")}
        manifestLabel={t.detail.deploymentAlerts.openManifest}
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
          gateway={g}
          revisions={revisions.data ?? []}
          hubName={hubName}
          onDeploy={() => setTab("deployment")}
        />
      ) : null}
      {tab === "definition" ? (
        <DefinitionTab
          gateway={g}
          revisions={revisions.data ?? []}
          revisionsLoading={revisions.isLoading}
          revisionsError={revisions.error}
          hubName={hubName}
          clusters={(clusters.data ?? []).map((c) => ({ id: c.id, name: c.name }))}
        />
      ) : null}
      {tab === "deployment" ? (
        <DeploymentTab
          item={g}
          canDeploy={canDeploy}
          onDeploy={() => controls.setDeploying(true)}
          onSaved={invalidate}
          emptyBody={t.gateways.deployment.emptyBody(g.revision)}
          hint={t.gateways.deployment.hint}
          saveExposure={(exposure) => patchGateway(g.id, { exposure })}
          lines={
            <>
              <dt className="text-muted">{t.gateways.deployment.resolvedBackends}</dt>
              <dd>{g.deployed_backends.length}</dd>
            </>
          }
        >
          <div className="grid gap-4 lg:grid-cols-2">
            <ToolsCard gateway={g} />
            <PublishCard gateway={g} />
          </div>
        </DeploymentTab>
      ) : null}
      {tab === "metrics" ? <McpMetricsTab queryKey={["gateways", tenant, g.id]} load={(period, tz) => getGatewayMetrics(g.id, period, tz)} /> : null}
      {tab === "advanced" && live ? (
        <ManifestTab
          queryKey={["gateways", tenant, g.id]}
          load={() => getGatewayManifest(g.id)}
          save={(doc, yaml) => {
            // overrides 는 vMCP 에만 얹히므로 다른 문서는 locked ["*"] 로 온다
            if (doc.kind !== "VirtualMCPServer") {
              return Promise.reject(new Error(text.overridesOnlyVmcp));
            }
            return putGatewayOverridesYaml(g.id, yaml);
          }}
          drift={(action) => gatewayDriftAction(g.id, action)}
          note={
            /* 화면에서 유일하게 내부 이름을 한 번 푸는 자리다. 여기서는 K8s 문서를 직접 고치므로 Gateway 와 VirtualMCPServer 가 같은 것임을 말해 준다 */
            <p className="mb-3 text-sm text-muted" data-testid="advanced-note">
              {text.advancedNote}
            </p>
          }
        />
      ) : null}
    </PageWidth>
  );
}

/**
 * 허브 항목을 거치지 않고 원본 MCP Server 로 바로 가는 링크 (D43). 백엔드 줄에서 허브 항목까지
 * 갔다가 다시 한 번 눌러야 하던 두 홉을 한 홉으로 줄인다. internal 항목에만 원본이 있다.
 */
function McpServerLink({ hubItemId, testId }: { hubItemId: string; testId?: string }) {
  const tenant = useTenant();
  const hub = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const instanceId = hub.data?.find((h) => h.id === hubItemId)?.source.instance_id;
  const t = useT();
  if (!instanceId) return null;
  return (
    <Link className="ml-2 text-xs text-muted underline-offset-2 hover:underline" to={`/mcp-servers/${instanceId}`} data-testid={testId}>
      {t.resources.kind["mcp-server"]}
    </Link>
  );
}

/** 백엔드가 고른 도구. 비었으면 허브 항목의 도구 전체다. 개요와 정의 탭이 같이 쓴다. */
function ToolsLabel({ backend }: { backend: Gateway["backends"][number] }) {
  const t = useT();
  const picked = backend.tools?.length ? backend.tools : null;
  return <span className={cn("text-xs text-muted", picked && "font-mono")}>{picked ? picked.join(", ") : t.gateways.detail.allTools}</span>;
}

/** 인증 경계 배지의 색. 익명만 흐리게 둔다. */
function authVariant(auth: Gateway["auth"]): BadgeProps["variant"] {
  return auth !== "anonymous" ? "ok" : "muted";
}

/**
 * 개요: 제품 뷰다 (D44 셋째). MCP Server·Agent 와 같은 뼈대에 백엔드 줄 카드가 하나 더 붙는다.
 * 백엔드가 Gateway 의 본체라 요약 한 줄로는 안 되고, 그렇다고 conditions 처럼 접을 것도 아니다.
 */
function OverviewTab({ gateway, revisions, hubName, onDeploy }: { gateway: Gateway; revisions: GatewayRevision[]; hubName: (id: string) => string; onDeploy: () => void }) {
  const t = useT();
  const text = t.gateways.overview;
  const ruled = gateway.backends.reduce((sum, b) => sum + ruledCount(b.tool_rules ?? {}), 0);
  const st = gateway.status;
  if (!st) {
    return (
      <>
        <Card data-testid="overview-empty">
          <CardContent className="flex flex-col items-start gap-3 py-8">
            <p className="text-sm font-medium text-heading">{t.detail.deployable.notDeployed}</p>
            <p className="text-sm text-muted">{text.deployedSummary(gateway.revision, gateway.backends.length)}</p>
            <Button data-testid="overview-deploy" onClick={onDeploy}>
              {t.detail.connectCard.toDeploy}
            </Button>
          </CardContent>
        </Card>
        <ReferencesCard resource="gateway" id={gateway.id} />
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
              auth={text.auth(
                <Badge variant={authVariant(gateway.auth)} data-testid="address-auth-badge">
                  {gateway.auth}
                </Badge>,
              )}
              badges={
                <div className="flex flex-wrap items-center gap-2">
                  {st.badge ? (
                    <Badge variant="warn" data-testid="status-badge">
                      {degradedBadgeText(t, st)}
                    </Badge>
                  ) : null}
                  <ExposureBadges exposure={gateway.exposure} status={st.exposure} warnings={st.warnings} />
                </div>
              }
            />
            <ConnectCard
              name={gateway.k8s?.name ?? gateway.name}
              displayName={displayName(gateway)}
              tenant={gateway.tenant}
              externalUrl={st.external_url}
              auth={authBoundary(gateway.auth)}
              onDeploy={onDeploy}
            />
            <ReferencesCard resource="gateway" id={gateway.id} />
            <RecentCard
              events={recentEvents({
                createdAt: gateway.created_at,
                revisions,
                lastSpecChange: gateway.last_spec_change,
                deployedRevision: gateway.deployed_revision,
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
              conditions={st.conditions}
              observedAt={st.observed_at}
              deployedRevision={gateway.deployed_revision}
              revision={gateway.revision}
              location={
                <>
                  <ClusterLink id={gateway.cluster_id} />{" "}
                  {gateway.k8s ? (
                    <span className="font-mono text-xs text-muted">
                      {gateway.k8s.namespace}/{gateway.k8s.name} <span className="text-dim">
                        {t.gateways.detail.labels.group} {gateway.k8s.group}
                      </span>
                    </span>
                  ) : null}
                </>
              }
              message={st.message}
              error={st.error}
            />
            <DefinitionSummaryCard
              revision={gateway.revision}
              rows={[
                {
                  label: t.gateways.columns.backends,
                  testId: "status-backend-count",
                  value: (
                    <>
                      {text.alive(st.backend_count ?? "?", st.expected_backends ?? gateway.backends.length)}
                      <ol className="mt-1 flex flex-col gap-0.5">
                        {gateway.backends.map((b) => (
                          <li key={b.hub_item_id}>
                            <Link className="underline-offset-2 hover:underline" to={`/hub/${b.hub_item_id}`}>
                              {hubName(b.hub_item_id)}
                            </Link>{" "}
                            <ToolsLabel backend={b} />
                          </li>
                        ))}
                      </ol>
                      {/* Gateway 자신의 규칙을 둔 Tool 의 수 (P52 7절). 서버와 항목의 규칙은 그 상세가 센다 */}
                      {ruled ? (
                        <div className="mt-1 text-xs text-muted" data-testid="summary-ruled-tools">
                          {t.toolRules.ruledTools(ruled)}
                        </div>
                      ) : null}
                    </>
                  ),
                },
                {
                  label: text.summary.conflict,
                  testId: "summary-conflict",
                  value: gateway.conflict_resolution,
                },
                {
                  label: text.summary.failure,
                  testId: "summary-failure",
                  value: st.failure_mode ?? gateway.failure_mode,
                },
                {
                  label: t.detail.deployable.runtime,
                  testId: "summary-runtime",
                  value: (
                    <>
                      {t.detail.deployable.labels.replicas} {gateway.runtime.replicas}, <span className={cn("text-xs", !isDefaultResources(gateway.runtime.resources) && "font-mono")}>{formatResources(t, gateway.runtime.resources)}</span>
                    </>
                  ),
                },
              ]}
            />
          </>
        }
      />

      <Card data-testid="backend-rows-card">
        <CardHeader>
          <CardTitle>{t.gateways.columns.backends}</CardTitle>
        </CardHeader>
        <Table data-testid="backend-rows">
          <THead>
            <tr>
              <TH>{t.gateways.columns.backend}</TH>
              <TH>{t.gateways.columns.workload}</TH>
              <TH>{t.gateways.columns.kind}</TH>
              <TH>{t.gateways.columns.state}</TH>
              <TH>{t.gateways.columns.failures}</TH>
              <TH>{t.gateways.columns.message}</TH>
            </tr>
          </THead>
          <TBody>
            {st.backends.length ? (
              st.backends.map((b: GatewayBackendRow) => (
                <TR key={b.workload} data-testid={`backend-row-${b.hub_item_name}`}>
                  <TD>
                    <Link className="underline-offset-2 hover:underline" to={`/hub/${b.hub_item_id}`}>
                      {b.hub_item_name}
                    </Link>
                    <McpServerLink hubItemId={b.hub_item_id} testId={`backend-mcp-${b.hub_item_name}`} />
                  </TD>
                  <TD className="font-mono text-xs">{b.workload}</TD>
                  <TD>{b.kind}</TD>
                  <TD>
                    <Badge variant={backendStateVariant(b.status)} data-testid={`backend-state-${b.hub_item_name}`}>
                      {b.status ?? t.detail.phaseBadge.unknown}
                    </Badge>
                    {b.entry_phase ? <span className="ml-1 text-xs text-dim">
                        {t.gateways.detail.labels.entry} {b.entry_phase}
                      </span> : null}
                  </TD>
                  <TD>{b.consecutive_failures ?? "-"}</TD>
                  <TD className="text-xs text-fg-soft">{b.message ?? ""}</TD>
                </TR>
              ))
            ) : (
              <EmptyRow colSpan={6}>{text.noBackendRows}</EmptyRow>
            )}
          </TBody>
        </Table>
      </Card>

      <ConditionsDetails conditions={st.conditions} />
    </>
  );
}

/** 정의: 무엇을 묶었고 이름 충돌을 어떻게 풀기로 했는가. */
function DefinitionTab({
  gateway,
  revisions,
  revisionsLoading,
  revisionsError,
  hubName,
  clusters,
}: {
  gateway: Gateway;
  revisions: GatewayRevision[];
  revisionsLoading: boolean;
  revisionsError: unknown;
  hubName: (id: string) => string;
  clusters: { id: string; name: string }[];
}) {
  const t = useT();
  const text = t.gateways.definition;
  const diffState = useRevisionDiff(revisions, specOf);

  const [previewRev, setPreviewRev] = useState(String(gateway.revision));
  const [previewCluster, setPreviewCluster] = useState("");
  const [preview, setPreview] = useState<GatewayPreview | null>(null);
  const runPreview = useMutation({
    mutationFn: () => previewGatewayRevision(gateway.id, Number(previewRev), previewCluster || null),
    onSuccess: setPreview,
  });
  const overrideCount = preview ? Object.values(preview.tool_overrides).reduce((n, m) => n + Object.keys(m).length, 0) : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="gw-summary">
          <CardHeader>
            <CardTitle>{t.detail.summaryFields.title}</CardTitle>
            <Badge data-testid="gw-revision">r{gateway.revision}</Badge>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
              <dt className="text-muted">{t.detail.summaryFields.id}</dt>
              <dd className="font-mono text-xs">{gateway.id}</dd>
              <dt className="text-muted">{t.gateways.detail.labels.backends}</dt>
              <dd data-testid="gw-backends">
                <ol className="flex flex-col gap-0.5">
                  {gateway.backends.map((b, i) => (
                    <li key={b.hub_item_id}>
                      <span className="mr-1 text-xs text-muted">#{i + 1}</span>
                      <Link className="underline-offset-2 hover:underline" to={`/hub/${b.hub_item_id}`}>
                        {hubName(b.hub_item_id)}
                      </Link>
                      <McpServerLink hubItemId={b.hub_item_id} testId={`definition-mcp-${b.hub_item_id}`} />{" "}
                      <ToolsLabel backend={b} />
                    </li>
                  ))}
                </ol>
              </dd>
              <dt className="text-muted">{t.gateways.detail.labels.conflict}</dt>
              <dd data-testid="gw-conflict">{gateway.conflict_resolution}</dd>
              <dt className="text-muted">{t.gateways.detail.labels.failure}</dt>
              <dd data-testid="gw-failure">{gateway.failure_mode}</dd>
              <dt className="text-muted">{t.gateways.detail.labels.auth}</dt>
              <dd>
                <Badge variant={authVariant(gateway.auth)} data-testid="gw-auth">
                  {gateway.auth}
                </Badge>
                <span className="ml-2 text-xs text-muted">{text.authFixed}</span>
              </dd>
              <dt className="text-muted">{t.gateways.detail.labels.authz}</dt>
              <dd className="font-mono text-xs" data-testid="gw-authz">
                {gateway.authz_policy ?? t.shared.tenantDefault}
              </dd>
              <dt className="text-muted">{t.gateways.detail.labels.runtime}</dt>
              <dd className="font-mono text-xs" data-testid="gw-runtime">
                {t.detail.deployable.labels.replicas} {gateway.runtime.replicas}, {t.detail.deployable.labels.resources} {formatResources(t, gateway.runtime.resources)}
              </dd>
              <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
              <dd>{formatDate(t, gateway.updated_at)}</dd>
            </dl>
          </CardContent>
        </Card>

        <Card data-testid="gw-preview-card">
          <CardHeader>
            <CardTitle>{t.gateways.detail.labels.preview}</CardTitle>
            <PreviewPicker
              revisions={gateway.revisions}
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
                {preview.warnings.map((w) => (
                  <Alert key={w} variant="warn" data-testid="preview-warning">
                    {w}
                  </Alert>
                ))}
                <div className="text-xs text-muted">
                  {t.gateways.detail.labels.namespace}{" "}
                  <span className="font-mono" data-testid="preview-namespace">
                    {preview.namespace}
                  </span>
                  , {t.gateways.detail.labels.conflict}{" "}
                  <Badge variant={preview.conflict_resolution === "priority" ? "ok" : "warn"} data-testid="preview-conflict">
                    {preview.conflict_resolution}
                  </Badge>
                  {overrideCount ? (
                    <span data-testid="preview-overrides">
                      {t.gateways.detail.labels.renames(overrideCount)}
                    </span>
                  ) : null}
                </div>
                <Table data-testid="preview-backends">
                  <THead>
                    <tr>
                      <TH>{t.gateways.columns.backend}</TH>
                      <TH>{t.gateways.columns.workload}</TH>
                      <TH>{t.gateways.columns.state}</TH>
                      <TH>{t.gateways.columns.ready}</TH>
                    </tr>
                  </THead>
                  <TBody>
                    {preview.backends.map((b) => (
                      <TR key={b.hub_item_id}>
                        <TD>{b.name}</TD>
                        <TD className="font-mono text-xs">{b.workload}</TD>
                        <TD>{b.phase ?? "-"}</TD>
                        <TD>
                          <Badge variant={levelVariant(b.ready)}>{b.ready ? t.detail.summaryFields.yes : t.detail.summaryFields.no}</Badge>
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
                <pre className="max-h-96 overflow-auto rounded-md bg-code p-3 font-mono text-xs text-code-fg" data-testid="preview-yaml">
                  {preview.documents.map((d) => toYaml(d)).join("---\n")}
                </pre>
              </>
            ) : (
              <p className="text-sm text-muted">{t.detail.deployable.previewHint}</p>
            )}
          </CardContent>
        </Card>
      </div>

      <GatewayToolRules gateway={gateway} />

      <Card data-testid="gw-revisions-card">
        <CardHeader>
          <CardTitle>{t.gateways.detail.labels.revisions}</CardTitle>
          <RevisionDiffSelects revisions={revisions} state={diffState} />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <ErrorMessage error={revisionsError} />
          <Table data-testid="gw-revisions-table">
            <THead>
              <tr>
                <TH>{t.gateways.columns.revision}</TH>
                <TH>{t.gateways.columns.backends}</TH>
                <TH>{t.gateways.columns.conflict}</TH>
                <TH>{t.gateways.columns.failureMode}</TH>
                <TH>{t.detail.deployable.replicas}</TH>
                <TH>{t.gateways.columns.created}</TH>
              </tr>
            </THead>
            <TBody>
              {revisions.length ? (
                revisions.map((r) => (
                  <TR key={r.revision} data-testid={`gw-revision-row-${r.revision}`} className={r.revision === gateway.revision ? "bg-surface-strong" : ""}>
                    <TD>
                      <RevisionBadges revision={r.revision} current={gateway.revision} deployed={gateway.deployed_revision} />
                    </TD>
                    <TD>{r.backends.length}</TD>
                    <TD>{r.conflict_resolution}</TD>
                    <TD>{r.failure_mode}</TD>
                    <TD>{r.runtime.replicas}</TD>
                    <TD>{formatDate(t, r.created_at)}</TD>
                  </TR>
                ))
              ) : (
                <EmptyRow colSpan={6}>{revisionsLoading ? t.common.loading : t.detail.deployable.noRevisions}</EmptyRow>
              )}
            </TBody>
          </Table>
          <RevisionDiffView state={diffState} />
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * vMCP 가 광고하는 도구. 스냅샷에 도구 이름이 하나도 없으면 카드를 생략한다.
 *
 * 머리말의 충돌 방식은 배포 쪽 값이다. 정의 쪽 `conflict_resolution` 은 `auto` 일 수 있고, 광고 이름을
 * 정하는 것은 배포할 때 풀린 `deployed_conflict_resolution` 이다.
 */
function ToolsCard({ gateway }: { gateway: Gateway }) {
  const t = useT();
  const backends = advertisedTools(gateway);
  const total = backends.reduce((n, b) => n + b.tools.length, 0);
  if (!total) return null;
  return (
    <Card data-testid="tools-card">
      <CardHeader>
        <CardTitle>{t.gateways.detail.labels.tools}</CardTitle>
        <span className="text-xs text-muted" data-testid="tools-count">
          {t.gateways.detail.labels.toolCount(total, gateway.deployed_conflict_resolution)}
        </span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-muted">{t.gateways.deployment.toolsHint}</p>
        {backends.map((b) =>
          b.tools.length ? (
            <div key={b.workload} data-testid={`tools-${b.hub_item_name}`}>
              <div className="mb-1 text-sm">
                <Link className="font-medium underline-offset-2 hover:underline" to={`/hub/${b.hub_item_id}`}>
                  {b.hub_item_name}
                </Link>{" "}
                <span className="font-mono text-xs text-muted">{b.workload}</span>
              </div>
              <ul className="flex flex-wrap gap-1">
                {b.tools.map((tool) => (
                  <li key={tool.original}>
                    <Badge variant="muted" className="font-mono" title={tool.name !== tool.original ? t.gateways.deployment.renamedFrom(tool.original) : undefined} data-testid={`tool-${tool.name}`}>
                      {tool.name}
                      {tool.name !== tool.original ? <span className="ml-1 text-dim">({tool.original})</span> : null}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          ) : null,
        )}
      </CardContent>
    </Card>
  );
}

/**
 * 허브에 게시. published 항목이 이미 있으면 링크와 해제 버튼을 보여 준다. 해제는 DELETE /hub/{id} 다
 * (docs/guide/hub-gateway.md 6절). 이름 두 개의 기본값은 Gateway 의 것이고(D24), 허브 항목 CR 은 `hub-<name>` 이다.
 * 접근 정책은 접힌 편집기다. published 는 다른 테넌트에 열 수 없다 (D23).
 */
function PublishCard({ gateway }: { gateway: Gateway }) {
  const t = useT();
  const text = t.gateways.publish;
  const gatewayId = gateway.id;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const auth = useAuthState();
  const { me } = auth;
  const hub = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const [mode, setMode] = useState<"entry" | "proxy">("entry");
  const [names, setNames] = useState<NameValue>({ displayName: displayName(gateway), name: gateway.name });
  const [access, setAccess] = useState<HubAccess>(emptyAccess());
  const [unpublishing, setUnpublishing] = useState<DeleteTarget | null>(null);
  const publish = useMutation({
    mutationFn: () => {
      const policy = accessForPublish(access);
      const display = names.displayName.trim();
      return publishGateway(gatewayId, { mode, name: names.name.trim() || null, ...(display ? { display_name: display } : {}), ...(policy ? { access: policy } : {}) });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["hub"] }),
  });
  const published = publishedFrom(hub.data ?? [], gatewayId);

  return (
    <Card data-testid="publish-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <Button size="sm" data-testid="publish-button" disabled={publish.isPending} onClick={() => publish.mutate()}>
          {publish.isPending ? t.detail.publishSection.publishing : t.detail.publishSection.publish}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <NameFields testId="publish" placeholders={{ display: t.gateways.placeholder, name: "support-gw" }} value={names} onChange={setNames} preview={{ prefix: "hub" }} required={false} errors={nameFieldErrors(publish.error)} />
        <div className="max-w-xs">
          <Field label={text.mode} htmlFor="publish-mode" hint={text.modeHint}>
            <Select id="publish-mode" aria-label={text.labels.modeSelect} data-testid="publish-mode" value={mode} onChange={(e) => setMode(e.target.value as "entry" | "proxy")}>
              <option value="entry">{t.gateways.detail.labels.entry}</option>
              <option value="proxy">{t.gateways.detail.labels.proxy}</option>
            </Select>
          </Field>
        </div>
        <p className="text-xs text-muted">{text.nameHint}</p>
        {takesPlatformTokens(authBoundary(gateway.auth), auth) ? (
          <p className="text-xs text-muted" data-testid="publish-consumer-hint">
            {text.consumerHint}
          </p>
        ) : null}
        <AccessPolicyEditor value={access} onChange={setAccess} canShare={canShare(me, tenant, "hub:share")} crossTenant={false} collapsible testId="access" />
        {!isNameFieldError(publish.error) ? <ErrorMessage error={publish.error} testId="publish-error" /> : null}
        {published.length ? (
          <ul className="flex flex-col gap-1" data-testid="published-list">
            {published.map((i) => (
              <li key={i.id} className="flex items-center gap-2">
                <Link className="underline-offset-2 hover:underline" to={`/hub/${i.id}`} data-testid={`published-link-${i.name}`}>
                  {i.name}
                </Link>
                <Badge variant={levelVariant(i.status?.ready ?? null)}>{i.status?.phase ?? t.detail.phaseBadge.unknown}</Badge>
                <span className="text-xs text-muted">{isProxyPublished(i) ? t.gateways.detail.labels.proxy : t.gateways.detail.labels.entry}</span>
                {i.permissions.manage ? (
                  <Button size="sm" variant="secondary" data-testid={`unpublish-${i.name}`} onClick={() => setUnpublishing({ id: i.id, name: i.name })}>
                    <CloudOff size={13} />
                    {t.detail.publishSection.unpublish}
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted" data-testid="published-empty">
            {text.empty}
          </p>
        )}
        <ResourceDeleteDialog
          resource="hub"
          target={unpublishing}
          onClose={() => setUnpublishing(null)}
          confirmLabel={t.detail.publishSection.unpublish}
          description={t.detail.publishSection.unpublishPublished}
        />
      </CardContent>
    </Card>
  );
}
