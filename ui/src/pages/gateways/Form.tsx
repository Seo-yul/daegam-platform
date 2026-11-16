import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { createGateway, getGateway, listHubItems, putGateway, type GatewayBody } from "@/api/resources";
import type { ConflictResolution, FailureMode, Gateway, GatewaySaved, HubItem } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { PageWidth } from "@/components/PageWidth";
import { EMPTY_DRAFT, ResourcesEditor, draftFromResources, resourcesFromDraft, type ResourcesDraft } from "@/components/ResourcesEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TimeoutInput } from "@/components/TimeoutInput";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { authBoundary, idpDataPlane, tokenAuth, useAuthState } from "@/lib/auth";
import { displayName, secondaryName } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { useDirty, useUnsavedGuard } from "@/lib/unsavedGuard";
import { cn } from "@/lib/utils";

import { CODE_MODE_TOOL, hubKindVariant, isPlatformHubItem, isProxyPublished, selectableTools } from "@/lib/hub";
import { describeCode } from "@/lib/errorText";
import { hubItemsKey } from "@/lib/queryKeys";

interface BackendSel {
  hub_item_id: string;
  tools: string[];
  /** 호출 타임아웃(초). 비우면 배포가 정한다 (P50 7절) */
  timeout_seconds?: number | null;
}

/** Gateway 가 백엔드의 resources·prompts 를 내보이는가 (P50 13절). */
interface SurfaceSel {
  resources: boolean;
  prompts: boolean;
}

/** 폼 한 벌. 편집은 저장된 Gateway 에서 채우고, 저장하지 않은 입력 판정은 이것 통째로 본다. */
interface FormState {
  names: NameValue;
  backends: BackendSel[];
  conflict: ConflictResolution;
  failureMode: FailureMode;
  authz: string;
  replicas: string;
  resources: ResourcesDraft;
  surface: SurfaceSel;
}

// 새 Gateway 는 둘 다 끈 채로 시작한다(서버의 POST 기본값과 같다)
const EMPTY: FormState = {
  names: { displayName: "", name: "" },
  backends: [],
  conflict: "auto",
  failureMode: "best_effort",
  authz: "",
  replicas: "1",
  resources: EMPTY_DRAFT,
  surface: { resources: false, prompts: false },
};

/** 인가 정책 칸의 보기. Cedar 문장이라 번역하지 않는다. */
const CEDAR_EXAMPLE = 'permit(principal, action == Action::"call_tool", resource);';

function fromGateway(p: Gateway): FormState {
  return {
    names: { displayName: displayName(p), name: p.name },
    backends: p.backends.map((b) => ({ hub_item_id: b.hub_item_id, tools: b.tools ?? [], timeout_seconds: b.timeout_seconds })),
    conflict: p.conflict_resolution as ConflictResolution,
    failureMode: p.failure_mode as FailureMode,
    authz: p.authz_policy ?? "",
    replicas: String(p.runtime.replicas ?? 1),
    resources: draftFromResources(p.runtime.resources),
    // 표면을 정하기 전 리비전(null)은 둘 다 열려 있다. 저장하면 그대로 정한 것이 된다
    surface: p.surface ? { resources: p.surface.resources, prompts: p.surface.prompts } : { resources: true, prompts: true },
  };
}

/** 게시된 백엔드 하나를 가리키는 저장 오류 (P57 6.1·6.2절). 답의 `hub_item_id` 가 그 항목이다. */
const BACKEND_ERROR_CODES: ReadonlySet<string> = new Set([
  "published_oidc_unsupported",
  "published_proxy_unsupported",
  "published_owner_forbid",
  "published_script_tool",
  "published_names_unknown",
]);

export function GatewayFormPage() {
  const t = useT();
  const text = t.gateways.form;
  const { id } = useParams();
  const editing = Boolean(id);
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const auth = useAuthState();
  const oidc = tokenAuth(auth.mode);
  // 데이터 플레인이 IdP 토큰만 받는 동안은 게시된 Gateway 를 백엔드로 쓸 수 없다 (P57 6.1절)
  const idp = idpDataPlane(auth);

  const existing = useQuery({
    queryKey: ["gateways", tenant, id],
    queryFn: () => getGateway(id as string),
    enabled: editing,
  });
  const hub = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });

  const [form, setForm] = useState<FormState>(EMPTY);
  const set = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));
  // 저장을 막은 resources 입력. 오류 문장은 그릴 때 이것으로 다시 만들어 언어를 바꿔도 따라간다
  const [badResources, setBadResources] = useState<ResourcesDraft | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (existing.data && !loaded) {
      setForm(fromGateway(existing.data));
      setLoaded(true);
    }
  }, [existing.data, loaded]);
  const { names, backends, conflict, failureMode, authz, replicas, resources, surface } = form;
  // 플랫폼 웹 Tool 을 맨 앞에 둔다 (P39b 15절). 캔버스의 안내가 `?backend=<이름>` 으로 미리 골라 보낸다
  const pickerItems = useMemo(() => [...(hub.data ?? [])].sort((a, b) => Number(isPlatformHubItem(b.kind)) - Number(isPlatformHubItem(a.kind))), [hub.data]);
  const [params] = useSearchParams();
  const [preselected, setPreselected] = useState(false);
  useEffect(() => {
    if (editing || preselected || !hub.data) return;
    const wanted = params.getAll("backend");
    const picked = hub.data.filter((item) => isPlatformHubItem(item.kind) && wanted.includes(item.name) && item.permissions.consume !== false);
    if (picked.length) setForm((f) => ({ ...f, backends: [...f.backends, ...picked.map((item) => ({ hub_item_id: item.id, tools: [] }))] }));
    setPreselected(true);
  }, [editing, preselected, hub.data, params]);

  const save = useMutation({
    mutationFn: (body: GatewayBody): Promise<Gateway | GatewaySaved> => (editing ? putGateway(id as string, body) : createGateway(body)),
    onSuccess: async (p) => {
      await queryClient.invalidateQueries({ queryKey: ["gateways"] });
      // 게시된 Gateway 의 저장이 forbid 문장을 더했으면 닫힐 소비자를 상세가 알린다 (P57 6.2절)
      const closed = "consumer_notice" in p ? (p.consumer_notice?.closed ?? []) : [];
      navigate(`/gateways/${p.id}`, { state: { warnings: p.warnings ?? [], consumersClosed: closed } });
    },
  });

  // 저장하지 않은 입력을 두고 나가려 하면 묻는다 (D43 다섯째). 저장이 끝나면 지킬 것이 없다
  const dirty = useDirty(JSON.stringify(form), !editing || loaded);
  const confirmLeave = useUnsavedGuard(dirty && !save.isPending && !save.isSuccess);

  function toggleBackend(item: HubItem, on: boolean) {
    set({ backends: on ? [...backends, { hub_item_id: item.id, tools: [] }] : backends.filter((b) => b.hub_item_id !== item.id) });
  }
  function setTimeoutOf(hubItemId: string, seconds: number | undefined) {
    set({ backends: backends.map((b) => (b.hub_item_id === hubItemId ? { ...b, timeout_seconds: seconds ?? null } : b)) });
  }
  function toggleTool(hubItemId: string, tool: string, on: boolean) {
    set({ backends: backends.map((b) => (b.hub_item_id === hubItemId ? { ...b, tools: on ? [...b.tools, tool] : b.tools.filter((name) => name !== tool) } : b)) });
  }

  // 명시 priority 인데 tool_names 를 모르는 백엔드가 있으면 충돌 도구를 이름 바꿀 수 없어 첫 워크로드가 조용히 이긴다.
  const unknownTools = backends
    .map((b) => hub.data?.find((h) => h.id === b.hub_item_id))
    .filter((h): h is HubItem => Boolean(h) && (h as HubItem).tool_names.length === 0)
    .map((h) => h.name);

  // 토큰 인증에서 게시된 Gateway 의 execute_tool_script 는 저장하지 않는다 (422 published_script_tool). 앞에서 저장한 선택에 남아
  // 있어도 화면에 보이지 않으므로 뺀다
  function savedTools(b: (typeof backends)[number]): string[] {
    const published = oidc && hub.data?.find((h) => h.id === b.hub_item_id)?.kind === "published";
    return published ? b.tools.filter((name) => name !== CODE_MODE_TOOL) : b.tools;
  }

  function submit() {
    const res = resourcesFromDraft(t, resources);
    if (res.error) {
      setBadResources(resources);
      return;
    }
    setBadResources(null);
    // 편집은 저장된 name 을 그대로 보낸다 (name_immutable). display_name 은 비우면 서버가 name 으로 채운다
    save.mutate({
      name: names.name.trim(),
      display_name: names.displayName.trim() || undefined,
      backends: backends.map((b) => ({ hub_item_id: b.hub_item_id, tools: savedTools(b), timeout_seconds: b.timeout_seconds })),
      conflict_resolution: conflict,
      failure_mode: failureMode,
      authz_policy: authz.trim() || null,
      runtime: { replicas: Number(replicas) || 1, resources: res.value },
      surface,
    });
  }

  // 422 hub_item_not_consumable: 항목 이름으로 보인다 (docs/guide/hub-gateway.md 7절). 목록에 없으면 id 그대로다
  const notConsumableId = save.error instanceof ApiError && save.error.code === "hub_item_not_consumable" ? save.error.extra<string>("hub_item_id") : undefined;
  const notConsumableName = notConsumableId ? (hub.data?.find((h) => h.id === notConsumableId)?.name ?? notConsumableId) : undefined;
  // 게시된 백엔드 하나를 가리키는 422 는 그 항목의 이름과 함께 보인다. published_owner_forbid 는 원래 Gateway 도 이름 댄다
  // (docs/guide/hub-gateway.md 3절)
  const backendFailure = save.error instanceof ApiError && BACKEND_ERROR_CODES.has(save.error.code) ? save.error : null;
  const backendFailureId = backendFailure?.extra<string>("hub_item_id");
  const backendFailureText = backendFailure
    ? text.backendError(
        hub.data?.find((h) => h.id === backendFailureId)?.name ?? backendFailureId ?? "",
        describeCode(t, backendFailure.code).text,
        backendFailure.extra<string>("gateway") ?? "",
      )
    : null;

  const resourcesError = badResources ? resourcesFromDraft(t, badResources).error : null;

  if (editing && existing.isLoading) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (editing && existing.error) return <ErrorMessage error={existing.error} />;

  return (
    <PageWidth data-testid="gateway-form-page">
      <PageHeader title={editing ? t.shared.editTitle(existing.data ? displayName(existing.data) : "") : text.title} name={existing.data ? secondaryName(existing.data) : undefined} testId="page-title" />
      {editing ? (
        <Alert variant="info" className="mb-4" data-testid="revision-notice">
          {text.revisionNotice((existing.data?.revision ?? 0) + 1)}
        </Alert>
      ) : null}
      <form
        className="grid gap-4 lg:grid-cols-2"
        data-testid="gateway-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Card>
          <CardHeader>
            <CardTitle>{text.backendsTitle}</CardTitle>
            <span className="text-xs text-muted">{text.selected(backends.length)}</span>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <NameFields testId="gp" placeholders={{ display: t.gateways.placeholder, name: "support-gateway" }} value={names} onChange={(next) => set({ names: next })} readOnlyName={editing} errors={nameFieldErrors(save.error)} />
            <p className="text-xs text-muted">{text.backendsHint}</p>
            <ul className="divide-y divide-line-soft rounded-md border border-line" data-testid="backend-list">
              {hub.data?.length ? null : (
                <li className="px-3 py-3 text-sm text-muted">{hub.isLoading ? t.common.loading : text.noHubItems}</li>
              )}
              {pickerItems.map((item) => {
                const idx = backends.findIndex((b) => b.hub_item_id === item.id);
                const sel = idx >= 0 ? backends[idx] : undefined;
                // IdP 토큰만 받는 동안의 게시된 Gateway 와, 토큰 인증의 proxy 모드 게시는 백엔드로 넣을 수 없다
                // (docs/guide/hub-gateway.md 3절, 422 published_oidc_unsupported·published_proxy_unsupported)
                const idpBlocked = idp && item.kind === "published";
                const proxyBlocked = oidc && !idp && isProxyPublished(item);
                const tools = selectableTools(item, oidc);
                // 토큰 인증의 게시된 Gateway 는 고정할 이름이 있어야 한다 (422 published_names_unknown)
                const namesBlocked = oidc && !idp && !proxyBlocked && item.kind === "published" && !tools.length;
                const blocked = idpBlocked || proxyBlocked || namesBlocked;
                // consume 이 없는 항목은 보이되 고를 수 없다
                // (docs/guide/hub-gateway.md 7절, 422 hub_item_not_consumable)
                const notConsumable = item.permissions.consume === false;
                return (
                  <li key={item.id} className={cn("px-3 py-2", notConsumable && "opacity-60")}>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        data-testid={`backend-${item.name}`}
                        checked={Boolean(sel)}
                        disabled={blocked || notConsumable}
                        onChange={(e) => toggleBackend(item, e.target.checked)}
                      />
                      {/* 사람이 붙인 이름이 먼저다. K8s 이름은 같은 표시 이름을 가르는 데 쓴다 */}
                      <span className="font-medium">{displayName(item)}</span>
                      {secondaryName(item) ? <span className="font-mono text-xs text-muted">{secondaryName(item)}</span> : null}
                      <Badge variant={hubKindVariant(item.kind)}>{item.kind}</Badge>
                      <span className="text-xs text-muted">{text.toolCount(tools.length)}</span>
                      {sel ? (
                        <Badge variant="info" data-testid={`backend-${item.name}-priority`}>
                          #{idx + 1}
                        </Badge>
                      ) : null}
                    </label>
                    {idpBlocked ? (
                      <p className="mt-1 pl-6 text-xs text-muted" data-testid={`backend-${item.name}-oidc-blocked`}>
                        {text.oidcBlocked}
                      </p>
                    ) : proxyBlocked ? (
                      <p className="mt-1 pl-6 text-xs text-muted" data-testid={`backend-${item.name}-proxy-blocked`}>
                        {text.proxyBlocked}
                      </p>
                    ) : namesBlocked ? (
                      <p className="mt-1 pl-6 text-xs text-muted" data-testid={`backend-${item.name}-names-unknown`}>
                        {text.publishedNamesUnknown}
                      </p>
                    ) : notConsumable ? (
                      <p className="mt-1 pl-6 text-xs text-muted" data-testid={`backend-${item.name}-not-consumable`}>
                        {text.notConsumable}
                      </p>
                    ) : null}
                    {sel ? (
                      <div className="mt-1 pl-6">
                        <Field label={text.timeout} hint={text.timeoutHint} htmlFor={`backend-${item.name}-timeout`}>
                          <TimeoutInput id={`backend-${item.name}-timeout`} value={sel.timeout_seconds} onChange={(seconds) => setTimeoutOf(item.id, seconds)} />
                        </Field>
                      </div>
                    ) : null}
                    {sel ? (
                      tools.length ? (
                        <div className="mt-1 pl-6">
                          <div className="mb-1 text-xs text-muted">{text.toolsPick}</div>
                          <ul className="flex flex-wrap gap-2">
                            {tools.map((tool) => (
                              <li key={tool}>
                                <label className="flex items-center gap-1 font-mono text-xs">
                                  <input
                                    type="checkbox"
                                    data-testid={`backend-${item.name}-tool-${tool}`}
                                    checked={sel.tools.includes(tool)}
                                    onChange={(e) => toggleTool(item.id, tool, e.target.checked)}
                                  />
                                  {tool}
                                </label>
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : (
                        <p className="mt-1 pl-6 text-xs text-warn-fg" data-testid={`backend-${item.name}-no-tools`}>
                          {text.noTools(
                            <Link className="underline" to={`/hub/${item.id}`}>
                              {t.hub.detail.refresh}
                            </Link>,
                          )}
                        </p>
                      )
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>{text.aggregation}</CardTitle>
              <span className="flex items-center gap-1 text-xs text-muted">
                {text.auth}
                <Badge variant={oidc ? "ok" : "muted"} data-testid="gp-auth">
                  {authBoundary(auth.mode)}
                </Badge>
              </span>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <Field label={text.conflict} htmlFor="gp-conflict" hint={text.conflictHint}>
                <Select id="gp-conflict" data-testid="gp-conflict" value={conflict} onChange={(e) => set({ conflict: e.target.value as ConflictResolution })}>
                  <option value="auto">{text.conflicts.auto}</option>
                  <option value="priority">{text.conflicts.priority}</option>
                  <option value="prefix">{text.conflicts.prefix}</option>
                </Select>
              </Field>
              {conflict === "priority" && unknownTools.length ? (
                <Alert variant="warn" data-testid="gp-conflict-warning">
                  {text.unknownTools(unknownTools.join(", "))}
                </Alert>
              ) : null}
              <Field label={text.failure} htmlFor="gp-failure" hint={text.failureHint}>
                <Select id="gp-failure" data-testid="gp-failure" value={failureMode} onChange={(e) => set({ failureMode: e.target.value as FailureMode })}>
                  <option value="best_effort">best_effort</option>
                  <option value="fail">{text.failFast}</option>
                </Select>
              </Field>
              <div className="flex flex-col gap-1">
                {(["resources", "prompts"] as const).map((key) => (
                  <label key={key} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      data-testid={`gp-surface-${key}`}
                      checked={surface[key]}
                      onChange={(e) => set({ surface: { ...surface, [key]: e.target.checked } })}
                    />
                    <span>{key === "resources" ? text.surfaceResources : text.surfacePrompts}</span>
                  </label>
                ))}
                <span className="text-xs text-muted">{text.surfaceHint}</span>
              </div>
              <Field label={text.authz} htmlFor="gp-authz" hint={text.authzHint}>
                <Input id="gp-authz" data-testid="gp-authz" className="font-mono text-xs" placeholder={CEDAR_EXAMPLE} value={authz} onChange={(e) => set({ authz: e.target.value })} />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{text.runtime}</CardTitle>
              <span className="text-xs text-muted">{text.runtimeHint}</span>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="w-24">
                <Field label={text.replicas} hint={t.detail.deployable.replicasHint} htmlFor="gp-replicas">
                  <Input id="gp-replicas" data-testid="gp-replicas" type="number" min={1} max={10} value={replicas} onChange={(e) => set({ replicas: e.target.value })} />
                </Field>
              </div>
              <ResourcesEditor testId="gp" value={resources} onChange={(next) => set({ resources: next })} />
            </CardContent>
          </Card>

          {resourcesError ? (
            <Alert variant="error" data-testid="form-error">
              {`resources: ${resourcesError}`}
            </Alert>
          ) : null}
          {notConsumableName ? (
            <Alert variant="error" data-testid="not-consumable-error">
              {text.notConsumableError(notConsumableName)}
            </Alert>
          ) : backendFailureText ? (
            <Alert variant="error" data-testid="backend-error">
              {backendFailureText}
            </Alert>
          ) : !isNameFieldError(save.error) ? (
            <ErrorMessage error={save.error} testId="save-error" />
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              onClick={() => {
                if (confirmLeave()) navigate(editing ? `/gateways/${id}` : "/gateways");
              }}
            >
              {t.common.cancel}
            </Button>
            <Button type="submit" data-testid="save-button" disabled={save.isPending || backends.length === 0}>
              {save.isPending ? t.shared.saving : editing ? t.detail.deployable.saveRevision : t.shared.create}
            </Button>
          </div>
        </div>
      </form>
    </PageWidth>
  );
}
