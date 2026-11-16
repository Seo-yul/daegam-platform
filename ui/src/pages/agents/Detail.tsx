/**
 * Agent 상세 (D42). 옛 Agent Profile 캔버스와 Agent Instance 상세가 한 화면으로 합쳐졌다.
 *
 * 앞의 셋(개요·정의·배포)은 리소스 넷이 공유하는 뼈대이고, 뒤의 다섯(채팅·세션·승인함·메트릭·이벤트)은
 * Agent 만 갖는 런타임 화면이다. 런타임 탭은 올라가 있어야 볼 것이 있으므로 `draft` 에서는 꺼 둔다.
 *
 * 정의 탭은 캔버스를 그대로 품는다. 캔버스가 곧 Agent 의 정의라서 같은 화면을 두 벌 만들 이유가 없다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import {
  agentDriftAction,
  cloneAgent,
  getAgent,
  getAgentManifest,
  getAuthConfig,
  listDataServices,
  putAgentOverridesYaml,
  redeployAgent,
  undeployAgent,
} from "@/api/resources";
import type { Agent, AgentExposureInput, AgentRedeployBody } from "@/api/types";
import { OverviewSleep, ScaleToZeroField } from "@/components/agents/AgentSleep";
import { EventsTab } from "@/components/agents/EventsTab";
import { LlmRouteSummary } from "@/components/agents/LlmRouteSummary";
import { MetricsTab } from "@/components/agents/MetricsTab";
import { EvaluationSkip, NO_SKIP, skipBody, skipIncomplete, type SkipState } from "@/components/agents/EvaluationSkip";
import { AgentExposureSection, EmptyAllowedGroupsBadge, EMPTY_AGENT_EXPOSURE, agentExposureFormOf, agentExposureInvalid, toAgentExposureInput, type AgentExposureForm } from "@/components/AgentExposureSection";
import { ApprovalInbox } from "@/components/ApprovalInbox";
import { ChatPanel } from "@/components/ChatPanel";
import { CloneDialog, type CloneExposureKit } from "@/components/detail/CloneDialog";
import { DeploymentDraftCard } from "@/components/detail/DeployControls";
import { DeploymentAlerts } from "@/components/detail/DeploymentAlerts";
import { ManifestTab } from "@/components/detail/ManifestTab";
import { MissingResourceScreen } from "@/components/detail/MissingResource";
import { ErrorBody, ErrorMessage } from "@/components/ErrorMessage";
import { ExposureBadges, ExternalUrl } from "@/components/ExposureStatus";
import { ClusterLink } from "@/components/ClusterLink";
import { PageHeader } from "@/components/Layout";
import { DetailActions } from "@/components/DetailActions";
import { AddressCard } from "@/components/overview/AddressCard";
import { ConditionsDetails } from "@/components/overview/ConditionsDetails";
import { DefinitionSummaryCard } from "@/components/overview/DefinitionSummaryCard";
import { OverviewGrid } from "@/components/overview/OverviewGrid";
import { ReadinessCard } from "@/components/overview/ReadinessCard";
import { RecentCard, recentEvents } from "@/components/overview/RecentCard";
import { Tile } from "@/components/overview/Tile";
import { PageWidth } from "@/components/PageWidth";
import { queryGate } from "@/components/QueryGate";
import { ReferencesCard, ReferencesNotice, useReferences } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { formatResources, isDefaultResources } from "@/components/ResourcesEditor";
import { StateBadge, isBusyState, isLiveState, isRedeployableState } from "@/components/StateBadge";
import { AgentSkillsSummary } from "@/components/skills/AttachedSkills";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LiveRegion, useAnnounce } from "@/components/ui/live-region";
import { Modal } from "@/components/ui/modal";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { TabBar, TabCount } from "@/components/ui/tabs";
import { WarningBody } from "@/components/WarningDetail";
import { type Messages, useT } from "@/i18n";
import { graphRemembers, mainHasMemory } from "@/lib/agentGraph";
import { acceptsTurns, savedScaleToZeroBlock, scaleToZeroBlock, windowMinutes } from "@/lib/agentSleep";
import { hasScope, tokenAuth, useAuthState } from "@/lib/auth";
import { PAT_ENV } from "@/lib/connectSnippets";
import { shouldPoll } from "@/lib/deployment";
import { exposureFieldErrors, exposureHostBlank, exposureInvalid, isExposureFieldError } from "@/lib/exposure";
import { displayName, resolveName, secondaryName } from "@/lib/names";
import { AUTH_CONFIG_KEY, dataServicesKey } from "@/lib/queryKeys";
import { referenceNames } from "@/lib/references";
import { useRunsEnabled } from "@/lib/runs";
import { useChatFeatures } from "@/lib/runtimeFeatures";
import { useTabParam } from "@/lib/tabs";
import { useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";
import { describeWarning, missingResource, unshownWarnings, warningCode } from "@/lib/warningText";

import { AgentCanvasPage } from "./Canvas";
import { AgentDeployDialog } from "./DeployDialog";
import { DeployHistoryCard, RevisionsCard } from "./DeploymentRevisions";
import { EvaluationTab } from "./EvaluationTab";
import { endpointTestId, visibleEndpoints } from "./externalEndpoints";
import { FeedbackTab } from "./FeedbackTab";
import { OpenPreviewsCard } from "./OpenPreviewsCard";
import { RunsTab } from "./RunsTab";
import { SessionsTab } from "./SessionsTab";

const TABS = ["overview", "definition", "deployment", "chat", "sessions", "feedback", "evaluation", "approvals", "runs", "metrics", "events", "advanced"] as const;
type Tab = (typeof TABS)[number];
/** 올라가 있어야 볼 것이 있는 탭. `draft` 에서는 꺼 둔다. */
const LIVE_ONLY: ReadonlySet<Tab> = new Set<Tab>(["chat", "sessions", "approvals", "metrics", "events", "advanced"]);

/** 복제 대화상자에 넘기는 Agent 의 노출 폼. 공용 폼에 CORS·허용 그룹이 더 붙는다. */
const AGENT_EXPOSURE_KIT: CloneExposureKit<AgentExposureForm, AgentExposureInput> = {
  empty: EMPTY_AGENT_EXPOSURE,
  toInput: toAgentExposureInput,
  invalid: agentExposureInvalid,
  Section: AgentExposureSection,
};

export function AgentDetailPage() {
  const t = useT();
  const text = t.agents.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  // 목록과 같은 검사다. 한쪽만 걸면 목록에서 안 보이던 버튼이 상세에서 보인다. 훅이므로 조회
  // 결과를 기다리는 early return 보다 위에 둔다
  const { me } = useAuthState();
  const canDeploy = hasScope(me, tenant, "agent:deploy");
  // 이벤트 목록은 다른 사람의 user_sub 를 보이므로 감사 읽기 권한이 있어야 탭이 선다 (P37 12절)
  const canAudit = hasScope(me, tenant, "audit:read");
  // 평가 탭은 남의 대화를 읽는 승인자(agent:approve)의 것이다 (P45 7절). 되돌리기는 저장과 배포를 함께 하므로 agent:write 도 있어야 한다
  const canApprove = hasScope(me, tenant, "agent:approve");
  const canWrite = hasScope(me, tenant, "agent:write");
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const answeredWarnings: string[] = (location.state as { warnings?: string[] } | null)?.warnings ?? [];

  const q = useQuery({
    queryKey: ["agents", tenant, id],
    queryFn: () => getAgent(id),
    refetchInterval: (query) => (query.state.data ? shouldPoll([query.state.data]) : false),
  });
  // 채팅과 세션 탭이 런타임 능력을 가르는 값이다. 훅이므로 early return 보다 위에 둔다
  const features = useChatFeatures(q.data?.status);
  // bindings 는 Database id 만 들고 있다. 화면에는 이름과 링크를 보인다 (D43 넷째)
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });

  // 자동 실행이 꺼진 플랫폼은 실행 라우트가 404 라 그 탭이 없다 (P41 12절)
  const runsOn = useRunsEnabled();
  // 감사 읽기가 없으면 이벤트 탭이 없다. 주소의 ?tab= 과 탭 줄이 같은 목록을 본다
  const visibleTabs = TABS.filter((k) => (k !== "events" || canAudit) && (k !== "runs" || runsOn) && (k !== "evaluation" || canApprove));
  const [tab, setTab] = useTabParam(visibleTabs, "overview");
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["agents"] });

  const [deploying, setDeploying] = useState(false);
  const [redeploying, setRedeploying] = useState(false);
  const [confirmingUndeploy, setConfirmingUndeploy] = useState(false);
  const [cloning, setCloning] = useState(false);
  const [deletingOpen, setDeletingOpen] = useState(false);
  // 배포 해제가 409 로 막히면 그 응답의 id 를 이 목록에서 이름으로 바꿔 보인다 (D43)
  const undeployNames = referenceNames(useReferences("agent", id, confirmingUndeploy).data ?? []);

  // 대화상자가 닫히는 것 말고는 아무 일도 없는 것처럼 보이는 자리다. 낭독기에게는 여기서 말한다
  const { message: announcement, count: announceCount, announce } = useAnnounce();

  const redeploy = useMutation({
    mutationFn: (body: AgentRedeployBody) => redeployAgent(id, body),
    onSuccess: async () => {
      setRedeploying(false);
      announce(t.ui.deployment.started.redeploy);
      await invalidate();
    },
  });
  const undeploy = useMutation({
    mutationFn: () => undeployAgent(id),
    onSuccess: async () => {
      setConfirmingUndeploy(false);
      announce(t.ui.deployment.started.undeploy);
      await invalidate();
    },
  });

  const missing = missingResource(q.error, q.data);
  if (missing) return <MissingResourceScreen missing={missing} resource={t.resources.kind.agent} backTo="/agents" backLabel={text.back} />;
  const gate = queryGate(q);
  if (gate) return gate;
  const a = q.data!;
  const st = a.status;
  const live = isLiveState(a.state);
  const busy = isBusyState(a.state) || redeploy.isPending || undeploy.isPending;
  // 잠든 Agent 도 대화를 받는다. 보낸 턴이 깨우고 파드가 뜨기를 기다린다 (P44 8절)
  const ready = acceptsTurns(st?.phase);
  // 배포 응답의 "런타임 기능 미확인" 은 init 이 기능을 알리기 전의 말이다. 상태에 runtime 이 오면(알렸으면) 더 서 있지
  // 않는다. 그대로 두면 Ready 가 된 뒤에도 배너가 남는다 (D65)
  const reported = Boolean(st?.runtime);
  const settled = (warnings: string[]) =>
    reported ? warnings.filter((w) => warningCode(w) !== "runtime_features_unknown") : warnings;
  const deployWarnings = settled(answeredWarnings);
  // 재배포 응답의 경고는 다음 폴링 응답에 없으므로 mutation 결과에서 읽는다
  const redeployWarnings = settled(redeploy.data?.warnings ?? []);
  // 배포·재배포 응답으로 이미 보인 경고는 상태 쪽에서 뺀다. 안 그러면 같은 문장이 두 번 세 번 선다
  const statusWarnings = unshownWarnings(st?.warnings ?? [], [...deployWarnings, ...redeployWarnings]);

  // 머리에 남는 것은 주 동작 하나와 편집뿐이고 나머지는 더보기 메뉴다 (D44 둘째)
  // 캔버스가 든 정의 탭과 Monaco 가 든 Advanced 탭은 넓을수록 잘 보이므로 폭 상한을 끈다 (D44 첫째)
  //
  // 정의 탭은 거기서 한 걸음 더 간다. 캔버스가 곧 Agent 의 정의라 그 탭에 있는 동안은 만들기 화면과
  // 같은 자리여야 한다(D42). 그래서 탭 줄만 남기고 제목·동작·배너를 접어 세로를 캔버스에 준다.
  // 사이드바와 비서를 접는 것은 껍데기가 한다(Layout.isCanvasPath). 다른 탭으로 가면 다 돌아온다.
  const canvasOnly = tab === "definition";
  // 앞의 넷과 Advanced 는 다른 상세와 같은 탭이고, 승인함은 그 화면의 제목을 그대로 쓴다
  const tabLabel: Record<Tab, string> = { ...t.ui.tabs.standard, ...t.agents.tabs, approvals: t.shared.approvalInbox.title };
  return (
    <PageWidth
      full={tab === "definition" || tab === "advanced"}
      data-testid="agent-detail-page"
      // 정의 탭은 캔버스가 본문 패널의 남은 높이를 다 받도록 높이 사슬을 잇는다
      className={cn("flex flex-col gap-4", canvasOnly && "min-h-0 flex-1")}
    >
      <LiveRegion key={announceCount} message={announcement} testId="deploy-announce" />
      {canvasOnly ? null : (
        <>
          <PageHeader
            title={displayName(a)}
            name={secondaryName(a)}
            testId="page-title"
            badges={<StateBadge state={a.state} />}
            primary={
              canDeploy ? (
                a.state === "draft" ? (
                  <Button data-testid="deploy-button" disabled={busy} onClick={() => setDeploying(true)}>
                    {t.shared.deployDialog.submit}
                  </Button>
                ) : isRedeployableState(a.state) ? (
                  <Button data-testid="redeploy-button" disabled={busy} onClick={() => setRedeploying(true)}>
                    {redeploy.isPending ? t.shared.detailActions.redeploying : t.shared.detailActions.redeploy}
                  </Button>
                ) : null
              ) : null
            }
            secondary={
              /* Agent 의 편집은 캔버스라 따로 화면이 없다. 정의 탭이 그 자리이므로 같은 곳으로 보내는
                 버튼을 따로 두지 않는다 (D42) */
              <DetailActions
                busy={busy}
                onUndeploy={live && canDeploy ? () => setConfirmingUndeploy(true) : undefined}
                onClone={() => setCloning(true)}
                onDelete={() => setDeletingOpen(true)}
              />
            }
          />
          {deployWarnings.map((w) => {
            const d = describeWarning(t, w);
            return (
              <Alert key={w} variant="warn" data-testid={`warning-${d.code ?? "other"}`}>
                <WarningBody d={d} />
              </Alert>
            );
          })}
          {unshownWarnings(redeployWarnings, deployWarnings).map((w) => (
            <Alert key={w} variant="warn" data-testid="redeploy-warning">
              <WarningBody d={describeWarning(t, w)} />
            </Alert>
          ))}
          {statusWarnings.map((w) => (
            <Alert key={w} variant="warn" data-testid="status-warning">
              <WarningBody d={describeWarning(t, w)} />
            </Alert>
          ))}
          {!redeploying ? <ErrorMessage error={redeploy.error} testId="redeploy-error" /> : null}
          <DeploymentAlerts
            stale={Boolean(q.error)}
            lastError={a.last_error}
            status={st}
            item={a}
            live={live}
            canDeploy={canDeploy}
            busy={busy}
            onRedeploy={() => setRedeploying(true)}
            onOpenAdvanced={() => setTab("advanced")}
            manifestLabel={t.detail.deploymentAlerts.openManifest}
            subject="graph"
          />
        </>
      )}

      <TabBar
        tabs={visibleTabs}
        value={tab}
        onChange={setTab}
        label={(key) => (
          <>
            {tabLabel[key]}
            {key === "approvals" && st?.pending_approvals ? (
              <TabCount>{st.pending_approvals}</TabCount>
            ) : null}
          </>
        )}
        disabled={(key) => LIVE_ONLY.has(key) && !live}
        reason={live ? null : text.liveOnlyTabs}
      />

      {tab === "overview" ? (
        <OverviewTab
          agent={a}
          // 목록이 아직 오지 않았으면 "…", 온 뒤에도 없으면 삭제된 항목이다
          dataServiceName={(dsId) => resolveName(t, dataServices.data, dsId)}
          onDeploy={() => setTab("deployment")}
          canDeploy={canDeploy}
        />
      ) : null}
      {tab === "overview" && a.exposure.enabled ? <ExternalCallCard agent={a} /> : null}
      {/* 그 Agent 에 열린 미리 보기 (P44b 11절). 배포하지 않은 Agent 도 미리 보기를 띄우므로 개요의 두 모양 모두 아래에 둔다 */}
      {tab === "overview" && canDeploy ? <OpenPreviewsCard agentId={a.id} /> : null}
      {/* 캔버스가 Agent 의 정의다. 같은 라우트 안에 있으므로 useParams 로 같은 id 를 읽는다 */}
      {/* Agent 마다 새로 선다. 같은 탭에서 다른 Agent 로 옮겨도 앞 Agent 의 편집 상태(올린 제안의 기준 리비전 등)가 남지 않는다 */}
      {tab === "definition" ? <AgentCanvasPage key={a.id} embedded /> : null}
      {tab === "deployment" ? (
        <AgentDeploymentTab agent={a} canDeploy={canDeploy} canWrite={canWrite} canApprove={canApprove} onDeploy={() => setDeploying(true)} onRedeploy={() => setRedeploying(true)} />
      ) : null}
      {/* 탭을 바꿔도 언마운트하지 않는다. 대화는 패널의 로컬 state 라 언마운트되면 사라지고,
          상태를 보러 개요 탭에 다녀온 사람이 방금 하던 대화를 잃는다 */}
      {live ? (
        <div hidden={tab !== "chat"} data-testid="chat-tabpanel">
          <Card data-testid="chat-card">
            <CardHeader>
              <CardTitle>{t.agents.tabs.chat}</CardTitle>
              <span className="text-xs text-muted">{ready ? text.chatReady : text.chatNotReady}</span>
            </CardHeader>
            <ChatPanel instanceId={a.id} greeting={st?.greeting} ephemeral disabled={!ready} features={features} traces />
          </Card>
        </div>
      ) : null}
      {tab === "sessions" && live ? <SessionsTab agentId={a.id} features={features} phase={st?.phase} /> : null}
      {tab === "feedback" ? <FeedbackTab agentId={a.id} sessionsOpen={live} /> : null}
      {tab === "evaluation" && canApprove ? <EvaluationTab agent={a} canWrite={canWrite} canDeploy={canDeploy} /> : null}
      {tab === "approvals" && live ? <ApprovalInbox instanceId={a.id} phase={st?.phase} savedPending={st?.pending_approvals} /> : null}
      {tab === "runs" && runsOn ? <RunsTab agentId={a.id} live={live} /> : null}
      {/* 머리에 이미 선 경고는 메트릭이 다시 세우지 않는다. 런타임과 상태가 같은 말을 할 때가 있다 */}
      {tab === "metrics" && live ? <MetricsTab agentId={a.id} shownWarnings={st?.warnings ?? []} /> : null}
      {tab === "events" && live && canAudit ? <EventsTab agentId={a.id} /> : null}
      {tab === "advanced" && live ? (
        <ManifestTab
          queryKey={["agents", tenant, a.id]}
          load={() => getAgentManifest(a.id)}
          save={(_doc, yaml) => putAgentOverridesYaml(a.id, yaml)}
          drift={(action) => agentDriftAction(a.id, action)}
        />
      ) : null}

      {/* 대화상자는 모두 포털로 뜨므로 놓는 자리는 화면에 영향이 없다. 머리를 접는 정의 탭에서도 살아 있게 접는 덩어리 밖에 둔다 */}
      <AgentDeployDialog agent={a} open={deploying} onClose={() => setDeploying(false)} onStarted={() => announce(t.ui.deployment.started.deploy)} />
      <RedeployDialog open={redeploying} agent={a} pending={redeploy.isPending} error={redeploy.error} onSubmit={(body) => redeploy.mutate(body)} onClose={() => setRedeploying(false)} />
      <ConfirmDialog
        open={confirmingUndeploy}
        title={t.shared.detailActions.undeploy}
        description={
          <>
            <p className="font-medium text-heading">{displayName(a)}</p>
            <p>{t.agents.undeployBody}</p>
            <ReferencesNotice resource="agent" id={a.id} open={confirmingUndeploy} testId="undeploy-references" />
          </>
        }
        confirmLabel={t.shared.detailActions.undeploy}
        pendingLabel={t.shared.detailActions.undeploying}
        pending={undeploy.isPending}
        error={undeploy.error ? <ErrorBody error={undeploy.error} names={undeployNames} /> : null}
        onConfirm={() => undeploy.mutate()}
        onCancel={() => setConfirmingUndeploy(false)}
      />
      <CloneDialog
        open={cloning}
        onClose={() => setCloning(false)}
        resource={t.resources.kind.agent}
        collection="agents"
        source={a}
        sourceNoun="graph"
        placeholders={text.clonePlaceholders}
        previewPrefix="agent"
        exposure={AGENT_EXPOSURE_KIT}
        clone={(body) => cloneAgent(a.id, body)}
      />
      <ResourceDeleteDialog resource="agent" target={deletingOpen ? { id: a.id, name: a.name } : null} onClose={() => setDeletingOpen(false)} onDeleted={() => navigate("/agents")} />
    </PageWidth>
  );
}

/**
 * 개요: 제품 뷰다 (D44 셋째). MCP Server·Gateway 와 같은 뼈대이고 세션·승인 타일만 준비 상태 카드
 * 안에 더 붙는다. 제일 오래 기다리는 화면이라 진짜 진척을 낼 숫자가 거기 있어야 한다 (D43 둘째).
 */
function OverviewTab({
  agent,
  dataServiceName,
  onDeploy,
  canDeploy,
}: {
  agent: Agent;
  dataServiceName: (id: string) => string;
  onDeploy: () => void;
  canDeploy: boolean;
}) {
  const t = useT();
  const text = t.agents.overview;
  const st = agent.status;
  if (!st) {
    return (
      <>
        <Card data-testid="overview-empty">
          <CardContent className="flex flex-col items-start gap-3 py-8">
            <p className="text-sm font-medium text-heading">{t.detail.deployable.notDeployed}</p>
            <p className="text-sm text-muted">{text.saved(agent.revision)}</p>
            <Button data-testid="overview-deploy" onClick={onDeploy}>
              {t.detail.connectCard.toDeploy}
            </Button>
          </CardContent>
        </Card>
        <ReferencesCard resource="agent" id={agent.id} />
      </>
    );
  }
  const graph = summarizeGraph(t, agent);
  return (
    <>
      <OverviewGrid
        left={
          <>
            <AddressCard
              url={st.chat_url}
              innerLabel={text.chatAddress}
              innerHint={text.chatAddressHint}
              externalUrl={st.external_url}
              onExpose={onDeploy}
              urlTestId="status-chat-url"
              copyTestId="copy-chat-url"
              externalTestId="external-url"
              externalCopyTestId="external-url-copy"
              auth={agent.exposure.enabled ? text.authExposed(agent.exposure.allowed_groups.join(", ")) : text.authInternal}
              /* 앞단 프록시가 없으므로 front 배지는 없다 (D27) */
              badges={<ExposureBadges exposure={agent.exposure} status={st.exposure} warnings={st.warnings} />}
            />
            <ReferencesCard resource="agent" id={agent.id} />
            <RecentCard
              events={recentEvents({
                createdAt: agent.created_at,
                lastSpecChange: agent.last_spec_change,
                deployedRevision: agent.deployed_revision,
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
              wanted={agent.replicas}
              conditions={st.conditions}
              observedAt={st.observed_at}
              deployedRevision={agent.deployed_revision}
              revision={agent.revision}
              location={
                <>
                  <ClusterLink id={agent.cluster_id} />{" "}
                  {agent.k8s ? (
                    <span className="font-mono text-xs text-muted">
                      {agent.k8s.namespace}/{agent.k8s.name}
                    </span>
                  ) : null}
                </>
              }
              message={st.message}
              error={st.error}
            >
              {/* 잠들었으면 아래 타일의 수가 그때의 값이라는 안내가 타일보다 먼저 온다 (P44 10절) */}
              <OverviewSleep agent={agent} canDeploy={canDeploy} />
              <div className="grid grid-cols-2 gap-3">
                <Tile label={text.tiles.sessions} value={st.active_sessions ?? "-"} testId="status-sessions" />
                <Tile label={text.tiles.pending} value={st.pending_approvals ?? "-"} tone={st.pending_approvals ? "warn" : undefined} testId="status-pending" />
                <Tile label={text.tiles.warnings} value={st.warnings.length} tone={st.warnings.length ? "warn" : undefined} testId="status-warnings" />
                <Tile label={t.agents.revision} value={`r${agent.deployed_revision ?? agent.revision}`} testId="status-revision-tile" />
              </div>
            </ReadinessCard>
            <DefinitionSummaryCard
              revision={agent.revision}
              rows={[
                {
                  label: text.summary.models,
                  testId: "summary-models",
                  value: (
                    <>
                      {graph.models.length ? graph.models.join(", ") : "-"}
                      {agent.llm_secrets.length ? <div className="font-mono text-xs text-muted">{agent.llm_secrets.join(", ")}</div> : null}
                    </>
                  ),
                },
                // LLM 경로와 직접 경로의 까닭, 게이트웨이 경로의 토큰 교체 (P38 11절)
                {
                  label: text.llmRoute.label,
                  testId: "summary-llm-route",
                  value: <LlmRouteSummary agent={agent} canRotate={canDeploy && isLiveState(agent.state)} />,
                },
                { label: text.summary.tools, testId: "summary-tools", value: graph.tools },
                { label: text.summary.memory, testId: "summary-memory", value: graph.memory },
                { label: "Skills", testId: "summary-skills", value: <AgentSkillsSummary graph={agent.graph} /> },
                {
                  label: "replicas",
                  testId: "summary-replicas",
                  value: (
                    <>
                      {agent.replicas} <span className={cn("text-xs text-muted", !isDefaultResources(agent.resources) && "font-mono")}>{formatResources(t, agent.resources)}</span>
                    </>
                  ),
                },
                {
                  label: t.mcpServers.bindings,
                  testId: "status-bindings",
                  value: agent.bindings.length ? (
                    <ul className="flex flex-col gap-0.5">
                      {agent.bindings.map((b) => (
                        <li key={b.env_prefix}>
                          <span className="font-mono text-xs">{b.env_prefix}</span>{" "}
                          <Link className="underline-offset-2 hover:underline" to={`/data-services/${b.data_service_id}`} data-testid={`binding-${b.env_prefix}`}>
                            {dataServiceName(b.data_service_id)}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    "-"
                  ),
                },
              ]}
            />
          </>
        }
      />
      <ConditionsDetails conditions={st.conditions ?? []} />
    </>
  );
}

/**
 * 그래프를 개요 세 줄로 줄인다. 자세한 것은 정의 탭의 캔버스에 그대로 있으므로 여기는 "무엇으로
 * 만들어졌나" 만 말한다. 노드 `data` 는 API 에서 자유 객체라 읽을 키만 좁혀 본다.
 */
function summarizeGraph(t: Messages, agent: Agent): { models: string[]; tools: string; memory: string } {
  const text = t.agents.overview;
  const nodes = agent.graph.nodes;
  const models = [
    ...new Set(
      nodes
        .filter((n) => n.type === "model")
        .map((n) => {
          const d = (n.data ?? {}) as { provider?: string; model?: string | null };
          return [d.provider, d.model].filter(Boolean).join(" / ") || text.unsetModel;
        }),
    ),
  ];
  const tools = nodes.filter((n) => n.type === "tools");
  const kinds = tools.map((n) => ((n.data ?? {}) as { source?: { kind?: string } }).source?.kind);
  const gateways = kinds.filter((k) => k === "gateway_instance").length;
  const servers = kinds.filter((k) => k === "mcp_instance").length;
  const parts: string[] = [];
  if (servers) parts.push(text.mcpServers(servers));
  if (gateways) parts.push(text.gateways(gateways));
  const memory = nodes.filter((n) => n.type === "memory");
  const knowledge = nodes.filter((n) => n.type === "knowledge");
  const memoryParts: string[] = [];
  if (memory.length) {
    memoryParts.push(graphRemembers(agent.graph) ? text.checkpointerAndMemory : text.checkpointer);
  }
  if (knowledge.length) memoryParts.push(text.knowledgeBases(knowledge.length));
  return { models, tools: parts.length ? parts.join(", ") : "-", memory: memoryParts.length ? memoryParts.join(", ") : "-" };
}

/**
 * 배포: 어디에 어떻게 올렸는가. 안 올렸으면 여기서 올린다. 아래에 리비전 표(되돌리기)와 배포 기록이 선다 (P45 6절). 배포를 해제한
 * Agent 도 기록이 남으므로 초안이어도 기록이 있으면 기록 카드를 보인다.
 */
function AgentDeploymentTab({
  agent,
  canDeploy,
  canWrite,
  canApprove,
  onDeploy,
  onRedeploy,
}: {
  agent: Agent;
  canDeploy: boolean;
  canWrite: boolean;
  canApprove: boolean;
  onDeploy: () => void;
  onRedeploy: () => void;
}) {
  const t = useT();
  const text = t.agents.deployment;
  const history = agent.deploy_history.length ? <DeployHistoryCard agent={agent} canReadRuns={canApprove} /> : null;
  if (agent.state === "draft") {
    return (
      <>
        <DeploymentDraftCard canDeploy={canDeploy} onDeploy={onDeploy} body={text.emptyBody(agent.revision)} />
        {history}
      </>
    );
  }
  return (
    <>
      <Card data-testid="deployment-card">
        <CardHeader>
          <CardTitle>{t.ui.tabs.standard.deployment}</CardTitle>
          {canDeploy ? (
            <Button size="sm" variant="secondary" data-testid="deployment-redeploy" onClick={onRedeploy}>
              {t.shared.detailActions.redeploy}
            </Button>
          ) : null}
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
            <dt className="text-muted">{t.detail.deployable.labels.cluster}</dt>
            <dd data-testid="deployment-cluster">
              <ClusterLink id={agent.cluster_id} testId="deployment-cluster-link" />
            </dd>
            <dt className="text-muted">{t.detail.deployable.labels.namespace}</dt>
            <dd className="font-mono text-xs">{agent.k8s ? `${agent.k8s.namespace}/${agent.k8s.name}` : "-"}</dd>
            <dt className="text-muted">{t.detail.deployable.deployedRevision}</dt>
            <dd>r{agent.deployed_revision ?? "?"}</dd>
            <dt className="text-muted">{t.detail.deployable.labels.replicas}</dt>
            <dd>
              {agent.replicas} <span className="text-xs text-muted">
                {t.detail.deployable.labels.resources} {formatResources(t, agent.resources)}
              </span>
            </dd>
            <dt className="text-muted">{t.shared.exposureSection.enable}</dt>
            <dd>{agent.exposure.enabled ? (agent.exposure.host ?? text.hostUnset) : text.off}</dd>
          </dl>
          {/* 지금 도는 리비전으로 서버가 판정한 까닭이다. 스위치 자체(agent_off)는 꺼진 체크로 보인다 (P44 6절) */}
          <div className="mt-3">
            <ScaleToZeroField
              checked={agent.scale_to_zero}
              block={savedScaleToZeroBlock(agent.status)}
              minutes={windowMinutes(agent.status)}
              testId="deployment-scale-to-zero"
            />
          </div>
          <p className="mt-3 text-xs text-muted">{text.hint}</p>
        </CardContent>
      </Card>
      <RevisionsCard agent={agent} canRollBack={canDeploy && canWrite && isRedeployableState(agent.state)} />
      {history}
    </>
  );
}

/**
 * curl 예시. 토큰은 env `DAEGAM_PAT` 에서 읽고(연결 카드와 같은 이름) -N 으로 SSE 버퍼링을 끈다. message 는 보낼 첫 메시지이고
 * 사람이 복사해 가는 글이라 화면 언어를 따른다.
 */
function curlExample(baseUrl: string, message: string): string {
  return [`curl -N ${baseUrl}/chat \\`, `  -H "Authorization: Bearer $${PAT_ENV}" \\`, `  -H "Content-Type: application/json" \\`, `  -d '{"message": "${message}"}'`].join("\n");
}

/** 외부 호출 카드. 노출을 켠 Agent 에만 보이고 external_url·엔드포인트 표·curl 예시·인가 범위를 적는다. */
function ExternalCallCard({ agent }: { agent: Agent }) {
  const t = useT();
  const text = t.agents.external;
  const authConfig = useQuery({ queryKey: AUTH_CONFIG_KEY, queryFn: getAuthConfig });
  // 개인 MCP 토큰을 내는 모드(local, oidc)면 그 화면으로 가는 안내를 단다 (P53b 6.5절)
  const minted = tokenAuth(useAuthState().mode);
  const base = agent.status?.external_url ?? `${agent.exposure.tls.mode === "none" ? "http" : "https"}://${agent.exposure.host ?? "<host>"}`;
  const curl = curlExample(base, text.curlMessage);
  const groups = agent.exposure.allowed_groups;
  const origins = agent.exposure.cors_origins;
  const audience = authConfig.data?.mcp_audience ?? null;
  return (
    <Card data-testid="external-call-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted">{text.lead(audience)}</span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted">{text.baseUrl}</span>
          <ExternalUrl url={agent.status?.external_url} testId="external-call-url" copyTestId="external-call-url-copy" />
          {!agent.status?.external_url ? <span className="text-xs text-muted">{text.noUrl}</span> : null}
        </div>
        <Table data-testid="external-endpoints">
          <THead>
            <tr>
              <TH>{text.columns.method}</TH>
              <TH>{text.columns.path}</TH>
              <TH>{text.columns.description}</TH>
            </tr>
          </THead>
          <TBody>
            {visibleEndpoints(agent.status).map((e) => (
              <TR key={`${e.method} ${e.path}`} data-testid={endpointTestId(e)}>
                <TD>
                  <Badge variant={e.method === "GET" ? "muted" : "accent"}>{e.method}</Badge>
                </TD>
                <TD className="font-mono text-xs">{e.path}</TD>
                <TD className="text-xs text-muted">{e.description(t.agents.endpoints)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
        <CodeSnippet
          title="curl"
          code={curl}
          copyLabel={text.curlCopy}
          testId="external-curl"
          copyTestId="external-curl-copy"
          note={
            minted ? (
              <span data-testid="external-pat-note">
                {text.patNote(
                  PAT_ENV,
                  <Link to="/account/tokens" className="text-link underline underline-offset-2 hover:text-link-hover" data-testid="external-pat-link">
                    {t.account.tokens.title}
                  </Link>,
                )}
              </span>
            ) : null
          }
        />
        <Alert variant="info" data-testid="external-eventsource-note">
          {text.eventSource}
        </Alert>
        <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
          <dt className="text-muted">{text.allowedGroups}</dt>
          <dd className="flex flex-wrap items-center gap-1" data-testid="external-allowed-groups">
            {groups.length ? (
              groups.map((g) => (
                <Badge key={g} variant="muted" className="font-mono">
                  {g}
                </Badge>
              ))
            ) : (
              <EmptyAllowedGroupsBadge testId="external-allowed-groups-empty" />
            )}
          </dd>
          <dt className="text-muted">{text.corsOrigins}</dt>
          <dd className="flex flex-wrap items-center gap-1" data-testid="external-cors-origins">
            {origins.length ? (
              origins.map((o) => (
                <Badge key={o} variant="muted" className="font-mono">
                  {o}
                </Badge>
              ))
            ) : (
              <span className="text-xs text-muted">{text.noOrigins}</span>
            )}
          </dd>
        </dl>
      </CardContent>
    </Card>
  );
}

/**
 * 재배포 대화상자. 최신 정의로 올리고, 노출 설정을 바꿨으면 본문 `exposure` 로 같이 보낸다.
 * 바꾸지 않았으면 본문을 비워 저장된 스냅샷을 유지한다 (AgentRedeployBody).
 */
function RedeployDialog({
  open,
  agent,
  pending,
  error,
  onSubmit,
  onClose,
}: {
  open: boolean;
  agent: Agent;
  pending: boolean;
  error: unknown;
  onSubmit: (body: AgentRedeployBody) => void;
  onClose: () => void;
}) {
  const t = useT();
  const text = t.agents.redeploy;
  const namespace = agent.k8s?.namespace ?? "";
  const [form, setForm] = useState<AgentExposureForm>(() => agentExposureFormOf(agent.exposure, namespace));
  const [scaleToZero, setScaleToZero] = useState(agent.scale_to_zero);
  // 배포 게이트가 막은 뒤에 평가 없이 배포하는 까닭 (P45 12절)
  const [skip, setSkip] = useState<SkipState>(NO_SKIP);
  // 열 때마다 저장된 스냅샷으로 되돌린다
  useEffect(() => {
    if (open) {
      setForm(agentExposureFormOf(agent.exposure, namespace));
      setScaleToZero(agent.scale_to_zero);
      setSkip(NO_SKIP);
    }
  }, [open, agent.exposure, agent.scale_to_zero, namespace]);
  const saved = toAgentExposureInput(agentExposureFormOf(agent.exposure, namespace));
  const next = toAgentExposureInput(form);
  const changed = JSON.stringify(next) !== JSON.stringify(saved);
  const hostBlank = exposureHostBlank(form);
  const hostInvalid = exposureInvalid(form);
  const blocked = pending || hostBlank || hostInvalid || agentExposureInvalid(form);
  const skipping = skipIncomplete(skip);

  return (
    <Modal open={open} title={t.shared.detailActions.redeploy} testId="redeploy-dialog" closeTestId="redeploy-close" onClose={() => (pending ? undefined : onClose())}>
      <div className="flex flex-col gap-3 px-5 py-4">
        <p className="text-sm text-muted">{text.body(agent.revision)}</p>
        <div className="flex flex-col gap-3 rounded-card border border-line px-4 py-3" data-testid="redeploy-exposure">
          <span className="text-sm font-semibold text-heading">{t.shared.exposureSection.enable}</span>
          <AgentExposureSection clusterId={agent.cluster_id} name={agent.name} value={form} onChange={setForm} errors={exposureFieldErrors(error)} mode="edit" />
          <p className="text-xs text-muted">{changed ? text.exposureChanged : text.exposureKept}</p>
        </div>
        {/* 재배포는 최신 정의를 지금의 이미지로 올리므로 memory 노드는 정의에서, 노출은 위 입력에서 읽는다 (P44 6절) */}
        <ScaleToZeroField
          checked={scaleToZero}
          onChange={setScaleToZero}
          block={scaleToZeroBlock({ remembers: mainHasMemory(agent.graph), exposed: form.enabled, status: agent.status })}
          minutes={windowMinutes(agent.status)}
          testId="redeploy-scale-to-zero"
        />
        {!isExposureFieldError(error) ? <ErrorMessage error={error} testId="redeploy-error" /> : null}
        <EvaluationSkip agent={agent} error={error} value={skip} onChange={setSkip} onLeave={onClose} testId="redeploy-skip" />
        {/* 버튼이 왜 죽어 있는지 적는다. 노출을 켠 채 Host 가 비면 재배포 자체가 막힌다 */}
        {!pending && blocked ? (
          <p className="text-xs text-err-fg" data-testid="redeploy-blocked">
            {hostBlank ? text.hostRequired : hostInvalid ? text.hostInvalid : text.exposureInvalid}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="outline" data-testid="redeploy-cancel" disabled={pending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button
            data-testid="redeploy-submit"
            disabled={blocked || skipping}
            // 바꾸지 않은 것은 보내지 않는다. 서버가 저장된 노출과 스위치를 그대로 둔다
            onClick={() =>
              onSubmit({ ...(changed ? { exposure: next } : {}), ...(scaleToZero !== agent.scale_to_zero ? { scale_to_zero: scaleToZero } : {}), ...skipBody(skip) })
            }
          >
            {pending ? t.shared.detailActions.redeploying : t.shared.detailActions.redeploy}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
