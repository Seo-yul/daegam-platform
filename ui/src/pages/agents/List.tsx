/**
 * Agent 목록 (D42). 옛 Agent Profile 목록과 Agent Instance 목록이 하나로 합쳐졌다.
 *
 * Agent 의 정의는 캔버스로 그린 그래프라 목록에서는 요약만 보인다. 카드는 메인 에이전트의 설명으로 무엇을 하는
 * Agent 인지 보이고, 표는 에이전트 수·Tool 소스 수·provider 를 견주게 한다. 나머지는 상세의 정의 탭에서 캔버스로 본다.
 *
 * 카드와 표 두 모양이고 기본은 카드다 (D47). 두 모양이 함께 쓰는 값은 `agentFacts` 한 곳에서 만들고, 모양마다
 * 그중 무엇을 보일지 고른다.
 */
import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listAgents } from "@/api/resources";
import type { Agent } from "@/api/types";
import { BehindBadge } from "@/components/BehindBadge";
import { ClusterLink } from "@/components/ClusterLink";
import { ErrorMessage } from "@/components/ErrorMessage";
import { DeployedOnlyToggle, ListToolbar, Pagination, SearchInput, ViewToggle, useListMode, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { PhaseDot } from "@/components/PhaseBadge";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateBadge } from "@/components/StateBadge";
import { StateBlock, StateRow } from "@/components/StateBlock";
import { StateCountLine } from "@/components/StateCountLine";
import { AgentIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CardGrid, CardTag, CardText, ListCard, editedLine } from "@/components/ui/card-grid";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import { mainAgentDescription, mainModel } from "@/lib/agentGraph";
import { isSleeping } from "@/lib/agentSleep";
import { hasScope, useAuthState } from "@/lib/auth";
import type { DeleteTarget } from "@/lib/deletion";
import { countByState, countSummary, isDeployedOrDeploying, shouldPoll, stateTone } from "@/lib/deployment";
import { kindColor } from "@/lib/kindColor";
import { nameSearchText } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { useUserLabel } from "@/lib/userLabel";
import { describeWarning, phaseText } from "@/lib/warningText";

/** 카드 태그로 보이는 붙은 갈래. 캔버스에 그 노드가 하나라도 있으면 붙은 것이다. */
const PART_TYPES = ["knowledge", "memory", "guardrail"] as const;

/** 그래프 요약: 에이전트 수·Tool 소스 수·메인에 붙은 모델의 provider 와 모델 이름(mainModel)·붙은 갈래. */
function summarize(graph: { nodes: { id: string; type: string; data?: Record<string, unknown> }[]; edges?: { source: string; target: string; kind: string }[] }): {
  agents: number;
  tools: number;
  provider: string | null;
  model: string | null;
  parts: (typeof PART_TYPES)[number][];
} {
  const model = mainModel(graph);
  return {
    agents: graph.nodes.filter((n) => n.type === "agent").length,
    tools: graph.nodes.filter((n) => n.type === "tools").length,
    provider: model?.provider ?? null,
    model: model?.model ?? null,
    parts: PART_TYPES.filter((type) => graph.nodes.some((n) => n.type === type)),
  };
}

/**
 * 카드의 구성 태그. 캔버스를 열지 않아도 무엇이 붙었는지 보인다. 색은 캔버스 노드 색(kindColor)이라 한 번 익히면
 * 두 화면에서 같게 읽힌다. 모델은 이름이 비었으면 provider 를 보인다.
 */
function AgentTags({ name, f }: { name: string; f: ReturnType<typeof agentFacts> }) {
  const t = useT();
  const model = f.model ?? f.provider;
  if (!model && !f.tools && !f.parts.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5" data-testid={`agent-tags-${name}`}>
      {model ? (
        <CardTag accent={kindColor("model")} mono>
          {model}
        </CardTag>
      ) : null}
      {f.tools ? <CardTag accent={kindColor("tools")}>{t.agents.list.toolSources(f.tools)}</CardTag> : null}
      {f.parts.map((part) => (
        <CardTag key={part} accent={kindColor(part)}>
          {t.canvas.nodeLabels[part]}
        </CardTag>
      ))}
    </div>
  );
}

/** 한 Agent 의 값. 카드와 표가 여기서 골라 쓴다. */
function agentFacts(t: Messages, a: Agent) {
  const sum = summarize(a.graph);
  const warnings = a.status?.warnings ?? [];
  const sleeping = isSleeping(a.status?.phase);
  return {
    tone: stateTone(a),
    // 정상이면 카드가 상태를 낭독기에만 남긴다. 잠든 Agent 는 정상이지만 파드가 없다는 것을 보인다 (P44 10절)
    quiet: stateTone(a) === "ok" && !sleeping,
    // 배포 상태 배지와 phase 점. 두 모양이 같은 짝으로 상태를 말한다. 잠들었으면 그 이름을 덧붙인다
    status: (
      <>
        <StateBadge state={a.state} testId={`agent-state-${a.name}`} />
        {a.status ? <PhaseDot phase={a.status.phase} testId={`agent-phase-${a.name}`} /> : null}
        {sleeping ? (
          <Badge variant="muted" data-testid={`agent-sleeping-${a.name}`}>
            {phaseText(t, a.status?.phase)}
          </Badge>
        ) : null}
      </>
    ),
    error: a.status?.error,
    revision: (
      <>
        r{a.revision}
        <BehindBadge item={a} testId={`agent-behind-${a.name}`} />
      </>
    ),
    description: mainAgentDescription(a.graph),
    agents: sum.agents,
    tools: sum.tools,
    parts: sum.parts,
    provider: sum.provider,
    model: sum.model,
    cluster: <ClusterLink id={a.cluster_id} testId={`agent-cluster-${a.name}`} />,
    sessions: a.status?.active_sessions ?? "-",
    pendingCount: a.status?.pending_approvals ?? 0,
    pending: a.status?.pending_approvals ? <Badge variant="warn">{a.status.pending_approvals}</Badge> : (a.status?.pending_approvals ?? "-"),
    // 짧은 이름만 보이고 한 문장 설명은 title 로 준다. 코드를 그대로 찍으면 급한 것인지 알 수 없다
    warningLabels: warnings.length ? warnings.map((w) => describeWarning(t, w).label).join(", ") : null,
    warningsTitle: warnings.length ? warnings.map((w) => describeWarning(t, w).text).join("\n") : undefined,
  };
}

/** 카드의 손볼 일 줄. 있을 때만 그린다. 승인 대기는 사람이 채팅에서 승인·거절해야 이어지는 세션이다. */
function AgentNote({ name, f }: { name: string; f: ReturnType<typeof agentFacts> }) {
  const t = useT();
  if (!f.error && !f.warningLabels && !f.pendingCount) return null;
  return (
    <div className="flex flex-col gap-0.5">
      {f.error ? <span className="text-err-fg">{f.error}</span> : null}
      {f.warningLabels ? <span className="text-warn-fg">{f.warningLabels}</span> : null}
      {f.pendingCount ? (
        <span className="text-warn-fg" data-testid={`agent-pending-${name}`}>
          {t.agents.list.pendingSessions(f.pendingCount)}
        </span>
      ) : null}
    </div>
  );
}

export function AgentListPage() {
  const t = useT();
  const text = t.agents.list;
  const tenant = useTenant();
  const userLabel = useUserLabel();
  const q = useQuery({
    queryKey: ["agents", tenant],
    queryFn: listAgents,
    refetchInterval: (query) => shouldPoll(query.state.data ?? []),
  });
  const counts = countByState(q.data ?? []);
  const pending = (q.data ?? []).reduce((n, a) => n + (a.status?.pending_approvals ?? 0), 0);
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const { me } = useAuthState();
  const canDeploy = hasScope(me, tenant, "agent:deploy");
  const [deployedOnly, setDeployedOnly] = useState(false);
  const [mode, setMode] = useListMode("agents");

  const view = useListView(
    (q.data ?? []).filter((a) => !deployedOnly || isDeployedOrDeploying(a.state)),
    nameSearchText,
  );

  const blocked = q.isLoading || Boolean(q.error && !q.data) || !view.total;
  const state = {
    loading: q.isLoading,
    error: q.data ? undefined : q.error,
    onRetry: () => void q.refetch(),
    illustration: <AgentIllustration />,
    empty: view.query
      ? text.noMatch(view.query)
      : deployedOnly
        ? text.noDeployed
        : text.empty(
            tenant,
            <Link className="font-medium text-fg underline-offset-2 hover:underline" to="/agents/new?template=support-agent" data-testid="goto-template">
              {text.emptyLink}
            </Link>,
          ),
  };
  const deployLink = (a: Agent) =>
    a.state === "draft" && canDeploy ? (
      <Link to={`/agents/${a.id}?tab=deployment`} className={buttonVariants({ variant: "outline", size: "sm" })} data-testid={`deploy-${a.name}`}>
        {t.shared.deployDialog.submit}
      </Link>
    ) : null;

  return (
    <PageWidth data-testid="agents-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? countSummary(t, counts) : undefined}
        primary={
          <Link to="/agents/new" className={buttonVariants()} data-testid="agent-new">
            <Plus size={16} aria-hidden />
            {text.create}
          </Link>
        }
      />
      <StateCountLine
        counts={counts}
        prefix="ag"
        // 대기 중인 승인은 사람이 손을 대야 풀리므로 있을 때만 호박색으로 센다
        extra={[{ label: text.pendingApprovals, value: pending, dot: pending ? "warn" : "muted", testId: "ag-count-pending" }]}
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
        <DeployedOnlyToggle value={deployedOnly} onChange={setDeployedOnly} />
        <ViewToggle mode={mode} onChange={setMode} />
      </ListToolbar>
      {q.data ? <ErrorMessage error={q.error} /> : null}
      {mode === "card" ? (
        <>
          {blocked ? (
            <StateBlock {...state} />
          ) : (
            <CardGrid testId="agent-cards">
              {view.pageItems.map((a) => {
                const f = agentFacts(t, a);
                return (
                  <ListCard
                    key={a.id}
                    to={`/agents/${a.id}`}
                    item={a}
                    data-testid={`agent-row-${a.name}`}
                    byline={[editedLine(t, userLabel(a.updated_by, ""), a.updated_at)]}
                    // 설명은 한 줄이고, 무엇이 붙었는지를 태그로 보인다. Agent 는 설명만으로 구별되지 않는 일이 많다
                    body={
                      <div className="flex flex-col gap-2.5">
                        <CardText text={f.description} testId={`agent-description-${a.name}`} />
                        <AgentTags name={a.name} f={f} />
                      </div>
                    }
                    note={<AgentNote name={a.name} f={f} />}
                    footer={<span className="flex items-center gap-1">{f.revision}</span>}
                    // 정상(배포·Ready)이면 배지를 숨기고 낭독기에만 남긴다. 문제가 있는 카드와 잠든 카드만 상태를 말한다
                    status={f.status}
                    quietStatus={f.quiet}
                    menu={[
                      ...(a.state === "draft" && canDeploy
                        ? [{ label: t.shared.deployDialog.submit, hint: text.deployHint, to: `/agents/${a.id}?tab=deployment`, testId: `deploy-${a.name}` }]
                        : []),
                      { label: t.common.delete, danger: true, testId: `row-delete-${a.name}`, onClick: () => setTarget({ id: a.id, name: a.name }) },
                    ]}
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
          <Table data-testid="agents-table">
            <THead>
              <tr>
                <TH>{text.columns.name}</TH>
                <TH>{text.columns.state}</TH>
                <TH>{t.agents.revision}</TH>
                <TH>{text.columns.agents}</TH>
                <TH>{text.columns.tools}</TH>
                <TH>{text.provider}</TH>
                <TH>{text.columns.cluster}</TH>
                <TH>{text.columns.sessions}</TH>
                <TH>{text.columns.pending}</TH>
                <TH>{text.columns.warnings}</TH>
                <ActionTH className="w-28" />
              </tr>
            </THead>
            <TBody>
              {blocked ? (
                <StateRow colSpan={11} {...state} />
              ) : (
                view.pageItems.map((a) => {
                  const f = agentFacts(t, a);
                  return (
                    <ListTR key={a.id} data-testid={`agent-row-${a.name}`}>
                      <NameTD to={`/agents/${a.id}`} item={a} />
                      <TD>
                        <span className="inline-flex items-center gap-1.5">{f.status}</span>
                        {f.error ? <div className="mt-0.5 text-xs text-err-fg">{f.error}</div> : null}
                      </TD>
                      <TD>{f.revision}</TD>
                      <TD>{f.agents}</TD>
                      <TD>{f.tools}</TD>
                      <TD className="font-mono text-xs">{f.provider ?? "-"}</TD>
                      <TD>{f.cluster}</TD>
                      <TD>{f.sessions}</TD>
                      <TD data-testid={`agent-pending-${a.name}`}>{f.pending}</TD>
                      <TD className="text-xs text-warn-fg" title={f.warningsTitle}>
                        {f.warningLabels ?? <span className="text-dim">-</span>}
                      </TD>
                      <ActionTD className="space-x-1">
                        {deployLink(a)}
                        <RowDeleteButton name={a.name} onClick={() => setTarget({ id: a.id, name: a.name })} />
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
      <ResourceDeleteDialog resource="agent" target={target} onClose={() => setTarget(null)} />
    </PageWidth>
  );
}
