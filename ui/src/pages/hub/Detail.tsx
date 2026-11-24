import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { getHubItem, getHubManifest, getHubTools, refreshHubTools, setHubAccess } from "@/api/resources";
import type { HubAccess, HubItem, HubTool, HubTools } from "@/api/types";
import { AccessPolicyCard } from "@/components/AccessPolicyCard";
import { ClusterLink } from "@/components/ClusterLink";
import { ErrorMessage } from "@/components/ErrorMessage";
import { LastErrorAlert } from "@/components/InstanceStatus";
import { PageHeader } from "@/components/Layout";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { ToolList } from "@/components/VerdictPanel";
import { HeaderAuthCard } from "@/components/hub/HeaderAuthCard";
import { AdmissionCard, ImageContainerCard } from "@/components/hub/ImageItemCards";
import { ToolSchemaList } from "@/components/hub/ToolSchemaList";
import { ItemToolRules } from "@/components/ToolRulesSection";
import { CopyButton } from "@/components/ui/copy-button";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { queryGate } from "@/components/QueryGate";
import { type Messages, useT } from "@/i18n";
import { derivedHubSource, hubKindLabel, hubPollInterval, hubToolNames, isAgentHubItem, isDerivedHubItem, isPlatformHubItem } from "@/lib/hub";
import { TabBar } from "@/components/ui/tabs";
import { useTabParam } from "@/lib/tabs";
import { useAuthState } from "@/lib/auth";
import { canShare, narrowed, normalizeAccess, scopeHint } from "@/lib/hubAccess";
import { displayName, secondaryName } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";
import { describeWarning, missingResource, warningCode } from "@/lib/warningText";
import { toYaml } from "@/lib/yaml";

import { hubKindVariant, hubStatusNote } from "@/lib/hub";
import { HubStatusBadge } from "./List";
import { hubItemKey, hubItemsKey } from "@/lib/queryKeys";

const TABS = ["overview", "tools", "access", "manifest"] as const;
type Tab = (typeof TABS)[number];

/** 탭 이름. 개요는 다른 상세와 같은 말을 쓴다. */
function tabLabel(t: Messages, tab: Tab): string {
  return tab === "overview" ? t.ui.tabs.standard.overview : t.hub.detail.tabs[tab];
}

export function HubDetailPage() {
  const t = useT();
  const text = t.hub.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { me } = useAuthState();

  const [tab, setTab] = useTabParam(TABS, "overview");
  const q = useQuery({ queryKey: hubItemKey(tenant, id), queryFn: () => getHubItem(id), refetchInterval: (query) => hubPollInterval(query.state.data) });
  // 매니페스트는 그 탭을 열 때만 부른다. 렌더가 클러스터를 한 번 밟으므로 개요만 보는 사람에게는 낭비다
  const manifest = useQuery({
    queryKey: [...hubItemKey(tenant, id), "manifest"],
    queryFn: () => getHubManifest(id),
    enabled: tab === "manifest",
  });
  // 도구도 같다. 이 호출은 클러스터를 밟는 데다 감사에도 남으므로 탭을 열 때만 한 번이다.
  // staleTime 을 두는 것은 탭을 오갈 때마다 다시 나가지 않게 하려는 것이다
  const tools = useQuery({
    queryKey: [...hubItemKey(tenant, id), "tools"],
    queryFn: () => getHubTools(id),
    // 게시한 Agent 의 항목은 MCP 를 말하지 않아 물어볼 서버가 없다 (P46 6.2절)
    enabled: tab === "tools" && Boolean(q.data?.permissions.consume) && !(q.data && isAgentHubItem(q.data.kind)),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["hub"] });
  const refresh = useMutation({ mutationFn: () => refreshHubTools(id), onSuccess: invalidate });
  const [deleting, setDeleting] = useState(false);

  // 내려간 게시 항목의 북마크를 따라온 사람에게 빨간 줄 하나만 주지 않는다
  if (missingResource(q.error, q.data)) {
    return <NotFoundPage resource={text.notFound} backTo="/hub" backLabel={text.back} />;
  }
  const gate = queryGate(q);
  if (gate) return gate;
  const item = q.data!;
  // 이름표의 수는 저장된 것이 아니라 방금 물어본 것이다. image 종류는 등록 때의 tools_filter 가 그대로
  // tool_names 라 새로고침 전에는 실제와 다르다. 한 화면이 서로 다른 수를 말하면 안 된다
  const liveTools = tools.data?.listed ? tools.data.tools : null;
  const manage = item.permissions.manage;
  // internal·published 의 도구 이름은 우리 리비전에서 계산한다 (D49). 라이브 값을 저장할 자리가
  // 없으므로 새로고침이 할 일이 없다. 라이브는 도구 탭이 저장 없이 보인다 (D48)
  // 같은 갈래를 두 곳에서 따로 적으면 종류가 하나 늘 때 한쪽만 고치게 된다 (서버는 store/hub.DERIVED_KINDS)
  const derived = isDerivedHubItem(item.kind);
  const platform = isPlatformHubItem(item.kind);
  // 게시한 Agent 의 항목은 MCP 를 말하지 않아 받아 올 Tool 목록이 없다 (P46 6.2절, 서버 422 derived_tools)
  const agent = isAgentHubItem(item.kind);
  const canRefresh = manage && !derived && !platform && !agent && (item.kind !== "entry" || Boolean(item.source.url));
  const published = item.kind === "published";
  const internal = item.kind === "internal";
  // internal·published·agent 는 원본의 게시 항목이라 지우는 것이 곧 게시 해제다 (docs/guide/hub-gateway.md 6절)
  const unpublishLabel = published || internal || agent ? t.detail.publishSection.unpublish : undefined;
  const phaseNote = hubStatusNote(t, item.status?.phase);
  // Secret env 가 있는 항목은 다른 테넌트에 열 수 없고(422 secret_not_shareable), 이미지가 거부된 항목은 더 열 수 없다(422
  // refused_image_not_shareable). 앞의 것은 테넌트 칸을 잠그고, 뒤의 것은 좁히기만 되게 넓히기를 막는다 (P55 5·6절)
  const secretEnvItem = (item.source.secret_env?.length ?? 0) > 0;
  const refusedImage = (item.status?.warnings ?? []).some((w) => warningCode(w) === "hub_image_refused");

  return (
    <PageWidth data-testid="hub-detail-page" className="flex flex-col gap-4">
      <PageHeader
        title={displayName(item)}
        name={secondaryName(item)}
        testId="page-title"
        actions={
          <>
            {derived || platform || agent ? null : (
              <Button variant="secondary" data-testid="refresh-tools-button" disabled={!canRefresh || refresh.isPending} title={manage ? undefined : text.manageRequired} onClick={() => refresh.mutate()}>
                {refresh.isPending ? text.refreshing : text.refresh}
              </Button>
            )}
            {internal ? (
              <Link to={`/mcp-servers/${item.source.instance_id ?? ""}`} className={buttonVariants({ variant: "secondary" })} data-testid="goto-instance-button">
                {text.gotoInstance}
              </Link>
            ) : null}
            {agent && item.source.agent_id ? (
              <Link to={`/agents/${item.source.agent_id}`} className={buttonVariants({ variant: "secondary" })} data-testid="goto-agent-button">
                {t.canvas.openResource(t.resources.kind.agent)}
              </Link>
            ) : null}
            {manage && !platform ? (
              <Button variant="danger" data-testid="delete-button" onClick={() => setDeleting(true)}>
                {unpublishLabel ?? t.common.delete}
              </Button>
            ) : null}
          </>
        }
      />
      <ResourceDeleteDialog
        resource="hub"
        target={deleting ? { id: item.id, name: item.name } : null}
        onClose={() => setDeleting(false)}
        onDeleted={() => navigate("/hub")}
        confirmLabel={unpublishLabel}
        // agent 항목은 그것을 Tool 로 쓰는 Agent 가 있어도 해제된다. 그 Agent 들은 아래의 참조 안내가 보인다
        description={published ? t.detail.publishSection.unpublishPublished : internal ? t.detail.publishSection.unpublishInternal : agent ? "" : undefined}
      />
      <ErrorMessage error={refresh.error} testId="refresh-error" />
      <LastErrorAlert lastError={item.last_error} testId="hub-last-error" />
      {/* 플랫폼이 붙인 phase 의 안내 (P55 5절). stdio 항목이 Ready 인데 답하지 않는 동안이다 */}
      {phaseNote ? (
        <Alert variant="warn" data-testid="hub-status-phase">
          {phaseNote.text}
        </Alert>
      ) : null}
      {/* 항목의 Tool 규칙 경고 (P52 7절)와 이미지 경고 (P55 6절). 같은 코드를 Gateway 도 내지만 항목의 고치는 법을 쓴다 */}
      {(item.status?.warnings ?? []).map((w) => (
        <Alert key={w} variant="warn" data-testid="hub-status-warning">
          {describeWarning(t, w, "hub").text}
        </Alert>
      ))}

      <TabBar
        tabs={TABS}
        value={tab}
        onChange={setTab}
        label={(key) =>
          key === "tools" ? (
            <>
              {tabLabel(t, key)} <span className="text-muted">{liveTools?.length ?? hubToolNames(item).length}</span>
            </>
          ) : (
            tabLabel(t, key)
          )
        }
      />

      {tab === "overview" ? (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
          <Card data-testid="hub-summary">
            <CardHeader>
              <CardTitle>{t.detail.summaryFields.title}</CardTitle>
              <Badge variant={hubKindVariant(item.kind)} data-testid="hub-kind" title={platform ? text.platformHint : undefined}>
                {hubKindLabel(t, item.kind)}
              </Badge>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
                <dt className="text-muted">{t.detail.summaryFields.id}</dt>
                <dd className="font-mono text-xs">{item.id}</dd>
                <dt className="text-muted">{text.labels.tenant}</dt>
                <dd>{item.tenant || (platform ? text.platform : "")}</dd>
                <dt className="text-muted">{text.labels.scope}</dt>
                <dd data-testid="hub-scope">
                  {item.scope}
                  <span className="ml-2 text-xs text-muted">{scopeHint(t, item.scope)}</span>
                </dd>
                <dt className="text-muted">{text.labels.cluster}</dt>
                <dd>
                  <ClusterLink id={item.cluster_id} />
                </dd>
                <dt className="text-muted">k8s</dt>
                <dd className="font-mono text-xs">
                  {item.k8s.kind ?? text.noK8s} {item.k8s.namespace}/{item.k8s.name}
                </dd>
                {item.source.url ? (
                  <>
                    <dt className="text-muted">{text.labels.sourceUrl}</dt>
                    <dd className="break-all font-mono text-xs">{item.source.url}</dd>
                  </>
                ) : null}
                {item.source.image ? (
                  <>
                    <dt className="text-muted">{text.labels.image}</dt>
                    <dd className="font-mono text-xs">
                      {item.source.image}
                      {item.source.port ? `:${item.source.port}` : ""}
                    </dd>
                  </>
                ) : null}
                {/* 카탈로그에서 온 항목은 종류와 상관없이 그 서버와 스냅숏의 릴리스를 보인다 (P55 5·7절) */}
                {item.source.catalog ? (
                  <>
                    <dt className="text-muted">{text.labels.catalog}</dt>
                    <dd className="font-mono text-xs" data-testid="hub-catalog-origin">
                      {item.source.catalog.server}@{item.source.catalog.release}
                    </dd>
                  </>
                ) : null}
                {item.source.instance_id ? (
                  <>
                    <dt className="text-muted">{text.labels.origin}</dt>
                    <dd>
                      <Link className="underline-offset-2 hover:underline" to={`/mcp-servers/${item.source.instance_id}`} data-testid="origin-instance-link">
                        {t.resources.kind["mcp-server"]}
                      </Link>
                    </dd>
                  </>
                ) : null}
                {item.source.gateway_instance_id ? (
                  <>
                    <dt className="text-muted">{text.labels.origin}</dt>
                    <dd>
                      <Link className="underline-offset-2 hover:underline" to={`/gateways/${item.source.gateway_instance_id}`} data-testid="origin-gateway-link">
                        {t.resources.kind.gateway}
                      </Link>
                    </dd>
                  </>
                ) : null}
                <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
                <dd>{formatDate(t, item.updated_at)}</dd>
              </dl>
            </CardContent>
          </Card>

          <Card data-testid="hub-status-card">
            <CardHeader>
              <CardTitle>{text.labels.status}</CardTitle>
              <HubStatusBadge item={item} testId="hub-status" />
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
                <dt className="text-muted">{text.labels.ready}</dt>
                <dd data-testid="hub-ready">{item.status?.ready ? t.detail.summaryFields.yes : t.detail.summaryFields.no}</dd>
                {/* 사람이 MCP 클라이언트에 붙여 넣는 주소다. 아래 backend url 은 Gateway 가 묶을 때
                    쓰는 클러스터 안 주소라 밖에서는 닿지 않는다 */}
                {/* 플랫폼 웹 Tool 은 Gateway 에 넣어 쓰므로 붙여 넣을 주소가 없다 (P39b 15절) */}
                {platform ? null : (
                  <>
                    <dt className="text-muted">{text.mcpUrl}</dt>
                    <dd className="min-w-0" data-testid="hub-mcp-url">
                      {item.status?.external_url ? (
                        <span className="flex items-start gap-1.5">
                          <span className="min-w-0 break-all font-mono text-xs">{item.status.external_url}</span>
                          <CopyButton value={item.status.external_url} label={text.mcpUrl} testId="copy-mcp-url" />
                        </span>
                      ) : (
                        // 등록한 주소가 있어도 서버가 밖에서 닿는다고 확인하지 못한 것은 클라이언트 설정에 넣지 않는다 (P55a 1절)
                        <span className="text-xs text-muted" data-testid={item.status?.address_class ? "hub-mcp-url-not-outside" : undefined}>
                          {item.status?.address_class ? text.notOutside : text.notExposed}
                        </span>
                      )}
                    </dd>
                  </>
                )}
                <dt className="text-muted">{text.labels.backendUrl}</dt>
                <dd className="min-w-0" data-testid="hub-backend-url">
                  {item.status?.url ? (
                    <span className="flex items-start gap-1.5">
                      <span className="min-w-0 break-all font-mono text-xs">{item.status.url}</span>
                      <CopyButton value={item.status.url} label={text.backendUrlCopy} testId="copy-backend-url" />
                    </span>
                  ) : (
                    "-"
                  )}
                </dd>
                {item.status?.message ? (
                  <>
                    <dt className="text-muted">{text.labels.message}</dt>
                    <dd>{item.status.message}</dd>
                  </>
                ) : null}
                {item.status?.error ? (
                  <>
                    <dt className="text-muted">{text.labels.error}</dt>
                    <dd className="text-err-fg">{item.status.error}</dd>
                  </>
                ) : null}
                <dt className="text-muted">{text.labels.observed}</dt>
                <dd>{formatDate(t, item.status?.observed_at)}</dd>
              </dl>
            </CardContent>
          </Card>
          </div>

          {/* image 항목의 컨테이너와 허브 키의 허용 기록 (P55 8절) */}
          {item.kind === "image" ? (
            <div className={item.source.admission ? "grid gap-4 lg:grid-cols-2" : undefined}>
              <ImageContainerCard item={item} />
              <AdmissionCard item={item} />
            </div>
          ) : null}
          <HeaderAuthCard item={item} />
          <ReferencesCard resource="hub" id={item.id} />
        </>
      ) : null}

      {tab === "tools" && agent ? <AgentToolCard item={item} /> : null}
      {tab === "tools" && !agent ? (
        <>
          <ToolsCard item={item} q={tools} />
          <ItemToolRules item={item} />
        </>
      ) : null}

      {tab === "access" ? (
        <AccessPolicyCard
          item={item}
          testId="hub"
          save={(access) => setHubAccess(item.id, access)}
          onSaved={async (updated) => {
            queryClient.setQueryData(hubItemKey(tenant, item.id), updated);
            await queryClient.invalidateQueries({ queryKey: hubItemsKey(tenant) });
          }}
          // internal·published·agent 는 다른 테넌트에 열 수 없다 (docs/guide/hub-gateway.md 8절, 422 cross_tenant_unsupported)
          editor={{ canShare: canShare(me, tenant, "hub:share") && !refusedImage, crossTenant: !isDerivedHubItem(item.kind) && !agent && !secretEnvItem }}
          notice={
            item.auth?.kind === "header"
              ? (draft) => <HeaderCopyNotice saved={item.access} draft={draft} owner={item.tenant} />
              : secretEnvItem || refusedImage
                ? () => (
                    <Alert variant="info" data-testid="access-image-notice">
                      {secretEnvItem ? t.errors.codes.secret_not_shareable : t.errors.codes.refused_image_not_shareable}
                    </Alert>
                  )
                : undefined
          }
        />
      ) : null}

      {tab === "manifest" ? (
        <Card data-testid="hub-manifest-card">
          <CardHeader>
            <CardTitle>{text.labels.manifest}</CardTitle>
            <span className="text-xs text-muted">{text.manifestReadOnly}</span>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <ErrorMessage error={manifest.error} />
            {manifest.data?.documents.length ? (
              manifest.data.documents.map((d) => (
                <div key={`${d.kind}/${d.name}`}>
                  <div className="mb-1 font-mono text-xs text-muted">
                    {d.kind} {d.name}
                  </div>
                  <pre className="max-h-80 overflow-auto rounded-md bg-code p-3 font-mono text-xs text-code-fg" data-testid={`hub-manifest-${d.kind}`}>
                    {toYaml(d.rendered)}
                  </pre>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted" data-testid="hub-manifest-empty">
                {manifest.isLoading ? t.common.loading : text.noManifest}
              </p>
            )}
          </CardContent>
        </Card>
      ) : null}
    </PageWidth>
  );
}

/**
 * header 항목의 접근 정책 안내 (P86 6.2절). 다른 테넌트에 열면 사본이 그 Gateway 네임스페이스로 간다. 편집 중인 정책이 저장된 것에서
 * 테넌트를 빼면 지워지기 전에 값을 읽었을 수 있다고 함께 말한다.
 */
function HeaderCopyNotice({ saved, draft, owner }: { saved: HubAccess; draft: HubAccess; owner: string }) {
  const t = useT();
  const removed = narrowed(normalizeAccess(saved), draft, owner, "consume");
  return (
    <Alert variant="warn" data-testid="access-header-notice">
      {t.hub.access.headerNotice}
      {removed ? (
        <span className="mt-1 block" data-testid="access-header-removed">
          {t.hub.access.headerRemoved}
        </span>
      ) : null}
    </Alert>
  );
}

/**
 * 저장된 이름과 방금 본 이름의 차이를 사람이 읽는 한 문장으로. 없으면 빈 문자열이다.
 *
 * 양쪽 모두를 말해야 한다. 서버에만 있는 것은 Gateway 에서 고를 수 없고, 저장된 쪽에만 있는 것은
 * 고를 수 있지만 실제로 부르면 없는 도구다.
 */
function missingNames(t: Messages, live: HubTool[], stored: string[]): string {
  const text = t.hub.tools.mismatch;
  const liveNames = new Set(live.map((tool) => tool.name));
  const storedNames = new Set(stored);
  const onlyLive = [...liveNames].filter((n) => !storedNames.has(n)).join(", ");
  const onlyStored = [...storedNames].filter((n) => !liveNames.has(n)).join(", ");
  // 어느 쪽에만 있는지에 따라 문장이 셋이다. 문장을 이어 붙이고 고쳐 쓰지 않는다
  if (onlyStored && onlyLive) return text.both(onlyStored, onlyLive);
  if (onlyStored) return text.storedOnly(onlyStored);
  return onlyLive ? text.liveOnly(onlyLive) : "";
}

/**
 * 게시한 Agent 의 항목의 도구 탭 (P46 6.2절). Tool 은 항목 이름에서 나온 하나이고 설명은 게시할 때 적은 Tool 설명이다. 부르는
 * Agent 의 모델이 그 설명을 읽는다.
 */
function AgentToolCard({ item }: { item: HubItem }) {
  const text = useT().hub.tools;
  const names = hubToolNames(item);
  return (
    <Card data-testid="hub-tools-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted">{text.count(names.length)}</span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ToolList names={names} />
        {item.description ? (
          <p className="text-sm whitespace-pre-wrap text-fg-soft" data-testid="hub-agent-description">
            {item.description}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * 도구 탭 (D48). 이 항목이 내놓는 도구가 무엇을 받는지 보인다.
 *
 * 값은 저장된 것이 아니라 지금 서버에 물어본 것이다. `tool_names` 를 쓰는 다섯 자리 중 라이브 조회를
 * 거치는 것은 도구 새로고침 하나뿐이라, 스키마를 저장하면 재배포 한 번에 옛것이 되어 같은 화면이 서로
 * 다른 것을 보인다. 못 물어보면 저장된 이름 칩으로 떨어지고 까닭만 한 줄 적는다. 도구가 무엇인지 못
 * 보는 것은 불편이지 오류가 아니라서 화면을 빨간 줄로 덮지 않는다.
 */
function ToolsCard({ item, q }: { item: HubItem; q: UseQueryResult<HubTools> }) {
  const t = useT();
  const text = t.hub.tools;
  const listed = q.data?.listed ? q.data.tools : null;
  // 파생 항목(internal·published)의 이름은 원본에서 온다 (D49). published 의 원본은 Gateway 라 종류대로 말한다
  const origin = derivedHubSource(t, item.kind);
  // 저장된 목록이 방금 본 것과 다르면 그 사실을 말한다. Gateway 만들기 화면은 저장된 쪽을 내주므로
  // 이것을 모르면 여기서 본 도구를 거기서 못 고른다.
  //
  // 개수가 아니라 이름을 견준다. 재배포가 성공해도 허브 사본 갱신은 따로 도는데 그것이 실패하면
  // 경고 한 줄만 로그에 남고 넘어간다(`services/mcp_servers._hub_sync`). 그때 가장 흔한 모양이
  // 오버라이드로 이름만 바뀌는 것이라 개수만 보면 바로 그 경우를 놓친다
  // 잘린 목록으로 견주면 잘려 나간 도구가 전부 "저장된 쪽에만 있다" 로 잡힌다. 그것은 어긋남이
  // 아니라 우리가 덜 보여 준 것이고, 새로고침으로 고칠 수도 없다
  const staleNames = listed === null || q.data?.truncated ? "" : missingNames(t, listed, item.tool_names);
  const stale = staleNames.length > 0;

  if (!item.permissions.consume) {
    return (
      <Card data-testid="hub-tools-card">
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
          <span className="text-xs text-muted">{text.count(item.tool_names.length)}</span>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {/* 이 조회는 플랫폼이 밖으로 나가는 호출이라 consume 이 있어야 한다 (D48) */}
          <p className="text-xs text-muted" data-testid="tools-need-consume">
            {text.needConsume}
          </p>
          {item.tool_names.length ? <ToolList names={item.tool_names} /> : null}
        </CardContent>
      </Card>
    );
  }

  const readAt = item.tool_names_at ? formatDate(t, item.tool_names_at) : null;
  return (
    <Card data-testid="hub-tools-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted">{listed ? text.countLive(listed.length) : text.countStored(item.tool_names.length, readAt)}</span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {q.isLoading ? (
          <p className="text-sm text-muted" data-testid="tools-loading">
            {text.loading}
          </p>
        ) : null}
        {/* 조회 자체가 실패하면 q.data 가 없어 아래 갈래가 모두 조용하다. 그러면 "도구가 없다" 로
            보이는데 실은 우리가 못 물어본 것이다. 서로 다른 두 사실은 다르게 말한다 */}
        <ErrorMessage error={q.error} testId="tools-error" />
        {/* reason 은 서버가 준 영문 예외 문자열이라 사람에게 그대로 던지지 않는다 (D36). 우리 말이
            먼저 오고 원문은 무엇을 물어볼지 아는 사람을 위해 접어 둔다 */}
        {q.data && !q.data.listed ? (
          <div className="text-xs text-warn-fg" data-testid="tools-not-listed">
            {text.notListed}
            {q.data.reason ? (
              <details className="mt-1">
                <summary className="cursor-pointer text-muted">{text.notListedReason}</summary>
                <p className="mt-0.5 font-mono break-all text-muted" data-testid="tools-not-listed-reason">
                  {q.data.reason}
                </p>
              </details>
            ) : null}
          </div>
        ) : null}
        {/* 응답 상한에 걸려 잘린 목록을 전부인 줄 알면 Gateway 에서 없는 도구를 찾게 된다 */}
        {q.data?.truncated ? (
          <p className="text-xs text-warn-fg" data-testid="tools-truncated">
            {text.truncated(listed?.length ?? 0)}
          </p>
        ) : null}
        {stale ? (
          <p className="text-xs text-warn-fg" data-testid="tools-stale">
            {text.stale(staleNames)}{" "}
            {/* 파생 항목(internal·published)에는 도구 새로고침이 없다. 이름이 원본에서 오기
                때문인데(D49), 그 사실을 모르면 있지도 않은 버튼을 찾게 된다 */}
            {origin ? text.staleFromOrigin(origin) : item.permissions.manage ? text.staleRefresh : text.staleNeedManage}
          </p>
        ) : null}
        {listed?.length ? (
          <ToolSchemaList tools={listed} />
        ) : item.tool_names.length ? (
          <ToolList names={item.tool_names} />
        ) : q.isLoading ? null : (
          <p className="text-sm text-warn-fg" data-testid="tools-unknown">
            {text.unknown}{" "}
            {/* 파생 항목에는 새로고침 버튼이 없다 (D49). tools-stale 과 같은 갈래다 */}
            {origin ? text.unknownFromOrigin(origin) : text.unknownRefresh}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
