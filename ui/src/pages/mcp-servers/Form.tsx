import { useMutation, useQueries, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError } from "@/api/client";
import {
  createMcpServer,
  getMcpServer,
  listBackendApis,
  listDataServices,
  listVersions,
  putMcpServer,
  type MCPServerBody,
} from "@/api/resources";
import type { BackendAPI, BackendAPIVersion, Binding, MCPServer, ToolOverride } from "@/api/types";
import { BindingsEditor } from "@/components/BindingsEditor";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors } from "@/components/NameFields";
import { PageWidth } from "@/components/PageWidth";
import { EMPTY_DRAFT, ResourcesEditor, draftFromResources, resourcesFromDraft, type ResourcesDraft } from "@/components/ResourcesEditor";
import { StateBlock } from "@/components/StateBlock";
import { TimeoutInput } from "@/components/TimeoutInput";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { displayName, optionLabel, secondaryName } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { useDirty, useUnsavedGuard } from "@/lib/unsavedGuard";
import { dataServicesKey } from "@/lib/queryKeys";

interface BackendState {
  backendId: string;
  specVersion: string;
  tools: string[];
  overrides: Record<string, ToolOverride>;
  /** 호출 타임아웃(초). 비우면 보내지 않아 서버의 기본값(30초)이 된다 (P50 7절) */
  timeoutSeconds?: number;
}

/** 저장했을 때 실제로 노출되는 도구. 하나도 안 고르면 서버가 스펙 전체를 노출한다. */
function exposedTools(b: BackendState, available: string[]): string[] {
  return b.tools.length ? b.tools : available;
}

/** 프로필 안에서 겹치는 최종 도구 이름 하나와 그것이 어디서 왔는지. */
export interface ToolNameClash {
  name: string;
  uses: { index: number; tool: string }[];
}

/**
 * 오버라이드를 씌운 뒤 겹치는 도구 이름을 찾는다.
 *
 * 서버의 `validate_tools`(platform/daegam/services/mcp_spec.py)와 같은 계산이다. 필요한 값이
 * 전부 폼에 있는데도 이 검사를 서버에 미루면 사용자는 Create 를 누른 뒤에야, 그것도 어느 칸을
 * 고쳐야 하는지 모른 채 422 를 받는다.
 */
export function toolNameClashes(backends: BackendState[], availables: string[][]): ToolNameClash[] {
  const uses = new Map<string, { index: number; tool: string }[]>();
  backends.forEach((b, index) => {
    for (const tool of exposedTools(b, availables[index] ?? [])) {
      const name = b.overrides[tool]?.name?.trim() || tool;
      const list = uses.get(name);
      if (list) list.push({ index, tool });
      else uses.set(name, [{ index, tool }]);
    }
  });
  return [...uses].filter(([, u]) => u.length > 1).map(([name, u]) => ({ name, uses: u }));
}

/** 서버가 422 duplicate_tool_name 에 싣는 conflicts 를 같은 모양으로 푼다. */
function clashesOfError(error: unknown): ToolNameClash[] {
  if (!(error instanceof ApiError) || error.code !== "duplicate_tool_name") return [];
  const conflicts = error.extra<{ name: string; uses?: { backend_index: number; operation_id: string }[] }[]>("conflicts") ?? [];
  return conflicts.map((c) => ({
    name: c.name,
    uses: (c.uses ?? []).map((u) => ({ index: u.backend_index, tool: u.operation_id })),
  }));
}

/** 목록에 다 적기에는 많을 수 있다(도구를 안 고른 백엔드 둘이면 스펙 전체가 겹친다). */
const CLASH_LIST_MAX = 10;

/** 겹치는 이름을 어느 백엔드의 어느 오퍼레이션에서 왔는지까지 적는다. */
function ToolNameClashAlert({ clashes, testId }: { clashes: ToolNameClash[]; testId: string }) {
  const t = useT();
  const text = t.mcpServers.form;
  if (!clashes.length) return null;
  return (
    <Alert variant="error" data-testid={testId}>
      {text.clashes(clashes.length)}
      <ul className="mt-1 list-disc pl-4 text-xs">
        {clashes.slice(0, CLASH_LIST_MAX).map((c) => (
          <li key={c.name}>
            <span className="font-mono">{c.name}</span> {c.uses.map((u) => text.clashUse(u.index + 1, u.tool)).join(", ")}
          </li>
        ))}
        {clashes.length > CLASH_LIST_MAX ? <li>{text.clashMore(clashes.length - CLASH_LIST_MAX)}</li> : null}
      </ul>
    </Alert>
  );
}

interface FormState {
  displayName: string;
  name: string;
  backends: BackendState[];
  image: string;
  replicas: string;
  resources: ResourcesDraft;
  bindings: Binding[];
}

const EMPTY_BACKEND: BackendState = { backendId: "", specVersion: "", tools: [], overrides: {} };

function fromProfile(p: MCPServer): FormState {
  return {
    displayName: displayName(p),
    name: p.name,
    backends: p.backends.map((b) => ({
      backendId: b.backend_profile_id,
      specVersion: String(b.spec_version),
      tools: b.tools,
      overrides: b.tool_overrides,
      timeoutSeconds: b.timeout_seconds,
    })),
    image: p.runtime.image ?? "",
    replicas: String(p.runtime.replicas ?? 1),
    resources: draftFromResources(p.runtime.resources),
    bindings: p.bindings,
  };
}

const EMPTY: FormState = {
  displayName: "",
  name: "",
  backends: [EMPTY_BACKEND],
  image: "",
  replicas: "1",
  resources: EMPTY_DRAFT,
  bindings: [],
};

/** 첫 backend 섹션은 기존 data-testid 그대로, 추가 섹션은 -<i> 접미사를 붙인다. */
function tid(base: string, index: number): string {
  return index === 0 ? base : `${base}-${index}`;
}

function BackendSection({
  index,
  value,
  backends,
  versions,
  clashes,
  onChange,
  onRemove,
}: {
  index: number;
  value: BackendState;
  backends: UseQueryResult<BackendAPI[]>;
  versions: UseQueryResult<BackendAPIVersion[]>;
  /** 이 프로필 안에서 겹치는 최종 도구 이름. 겹치는 칸에 aria-invalid 를 준다. */
  clashes: ToolNameClash[];
  onChange: (patch: Partial<BackendState>) => void;
  onRemove: (() => void) | null;
}) {
  const t = useT();
  const text = t.mcpServers.form;
  const backendList = backends.data ?? [];
  const version = versions.data?.find((v) => String(v.version) === value.specVersion);
  const available = version?.tool_names ?? [];
  const details = useMemo(() => {
    const map: Record<string, { summary: string | null; description: string | null }> = {};
    for (const d of version?.tool_details ?? []) {
      map[d.operation_id] = { summary: d.summary ?? null, description: d.description ?? null };
    }
    return map;
  }, [version]);

  const [search, setSearch] = useState("");
  const searching = search.trim().length > 0;
  const visible = useMemo(
    () => available.filter((tool) => tool.toLowerCase().includes(search.trim().toLowerCase())),
    [available, search],
  );
  // 검색으로 목록에서 사라진 선택. 400개가 선택된 줄 모르고 저장하는 일을 막는다
  const hiddenPicked = value.tools.filter((tool) => available.includes(tool) && !visible.includes(tool)).length;

  // 버전을 바꿀 때 새 버전에 없어서 선택에서 빠진 도구. 다음 버전 변경까지만 보인다
  const [dropped, setDropped] = useState<string[]>([]);
  // 백엔드 자체를 바꾸면 고른 것이 전부 사라지므로 한 번 묻는다
  const [pendingBackend, setPendingBackend] = useState<string | null>(null);
  const picked = value.tools.length + Object.keys(value.overrides).length;

  /** 버전을 옮길 때 새 버전에도 있는 도구와 그 오버라이드는 남긴다. */
  function changeVersion(next: string) {
    const nextAvailable = versions.data?.find((v) => String(v.version) === next)?.tool_names ?? [];
    const kept = value.tools.filter((tool) => nextAvailable.includes(tool));
    const overrides: Record<string, ToolOverride> = {};
    for (const [tool, ov] of Object.entries(value.overrides)) if (nextAvailable.includes(tool)) overrides[tool] = ov;
    setDropped(value.tools.filter((tool) => !nextAvailable.includes(tool)));
    onChange({ specVersion: next, tools: kept, overrides });
  }

  function applyBackend(next: string) {
    setDropped([]);
    setPendingBackend(null);
    onChange({ backendId: next, specVersion: "", tools: [], overrides: {} });
  }

  function toggleTool(name: string, on: boolean) {
    onChange({ tools: on ? [...value.tools, name] : value.tools.filter((tool) => tool !== name) });
  }
  function setOverride(tool: string, patch: Partial<ToolOverride>) {
    const cur = { ...(value.overrides[tool] ?? {}), ...patch };
    const next = { ...value.overrides };
    if (!cur.name && !cur.description) delete next[tool];
    else next[tool] = { name: cur.name || null, description: cur.description || null };
    onChange({ overrides: next });
  }
  /** 이 도구의 최종 이름이 프로필 안에서 겹치는가. */
  function clashing(tool: string): boolean {
    return clashes.some((c) => c.uses.some((u) => u.index === index && u.tool === tool));
  }

  return (
    <div
      className="flex flex-col gap-3 rounded-md border border-line p-3"
      data-testid={tid("backend-section", index)}
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label={`Backend API #${index + 1}`} htmlFor={tid("mp-backend", index)}>
          <Select
            id={tid("mp-backend", index)}
            data-testid={tid("mp-backend", index)}
            value={value.backendId}
            onChange={(e) => (picked ? setPendingBackend(e.target.value) : applyBackend(e.target.value))}
          >
            <option value="">{t.shared.exposureSection.select}</option>
            {backendList.map((b) => (
              <option key={b.id} value={b.id}>
                {optionLabel(b)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={text.specVersion} hint={text.versionHint} htmlFor={tid("mp-version", index)}>
          <Select
            id={tid("mp-version", index)}
            data-testid={tid("mp-version", index)}
            value={value.specVersion}
            disabled={!value.backendId}
            onChange={(e) => changeVersion(e.target.value)}
          >
            <option value="">{t.shared.exposureSection.select}</option>
            {versions.data?.map((v) => (
              <option key={v.version} value={String(v.version)}>
                {text.versionOption(v.version, v.kind, v.tool_count)}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <Field label={text.timeout} hint={text.timeoutHint} htmlFor={tid("mp-timeout", index)}>
        <TimeoutInput id={tid("mp-timeout", index)} placeholder="30" value={value.timeoutSeconds} onChange={(seconds) => onChange({ timeoutSeconds: seconds })} />
      </Field>

      {/* 못 읽은 것과 하나도 없는 것을 구분해서 말한다. 처음 쓰는 사람은 Backend API 가 먼저라는 것을 여기서 배운다 */}
      <StateBlock
        loading={backends.isLoading}
        error={backends.error}
        onRetry={() => void backends.refetch()}
        empty={backendList.length === 0 ? text.noBackends : undefined}
        emptyAction={
          <Link className="font-medium underline underline-offset-2" to="/backend-apis/new">
            {t.backendApis.list.create}
          </Link>
        }
      />
      {value.backendId && versions.error ? <ErrorMessage error={versions.error} testId={tid("mp-versions-error", index)} /> : null}
      {dropped.length ? (
        <Alert variant="warn" data-testid={tid("tools-dropped", index)}>
          {text.dropped(dropped.length, dropped.join(", "))}
        </Alert>
      ) : null}

      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">
          {text.tools}{" "}
          {/* 서버는 빈 목록을 "전체 노출" 로 읽는다(services/mcp_spec.py 의 exposed). 화면이 "0/40" 이라고만
              말하면 위험한 도구를 빼려고 None 을 누른 사람이 정반대를 저장하게 된다. Gateway 폼과 같은 문장을 쓴다. */}
          {value.tools.length === 0 ? (
            <span className="text-warn-fg" data-testid={tid("tools-all-warning", index)}>
              {text.allExposed(available.length)}
            </span>
          ) : (
            <span className="text-muted">
              ({value.tools.length}/{available.length})
            </span>
          )}
        </span>
        <div className="flex gap-1">
          {/* 검색 중이면 눈에 보이는 것만 다룬다. 12개를 걸러 놓고 All 을 눌렀는데 400개가 선택되면 안 된다 */}
          <Button
            size="sm"
            variant="ghost"
            data-testid={tid("tools-all", index)}
            onClick={() => onChange({ tools: searching ? [...new Set([...value.tools, ...visible])] : [...available] })}
          >
            {searching ? text.selectFound : text.selectAll}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            data-testid={tid("tools-none", index)}
            onClick={() => onChange({ tools: searching ? value.tools.filter((tool) => !visible.includes(tool)) : [] })}
          >
            {searching ? text.clearFound : text.clearAll}
          </Button>
          {onRemove ? (
            <Button
              size="sm"
              variant="ghost"
              data-testid={`backend-remove-${index}`}
              onClick={onRemove}
            >
              {t.shared.bindingsEditor.remove}
            </Button>
          ) : null}
        </div>
      </div>
      <Input
        data-testid={tid("tools-search", index)}
        placeholder={text.searchTools}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      {hiddenPicked ? (
        <p className="text-xs text-muted" data-testid={tid("tools-hidden-picked", index)}>
          {text.hiddenPicked(hiddenPicked)}
        </p>
      ) : null}
      <ul
        className="max-h-64 divide-y divide-line-soft overflow-y-auto rounded-md border border-line"
        data-testid={tid("tools-list", index)}
      >
        {visible.length === 0 ? (
          <li className="px-3 py-2 text-xs text-muted">
            {available.length ? text.noMatch : text.pickVersion}
          </li>
        ) : null}
        {visible.map((tool) => {
          const on = value.tools.includes(tool);
          const detail = details[tool];
          const original = detail?.summary || detail?.description || null;
          return (
            <li key={tool} className="px-3 py-1.5">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  data-testid={tid(`tool-${tool}`, index)}
                  checked={on}
                  onChange={(e) => toggleTool(tool, e.target.checked)}
                />
                <span className="font-mono text-xs">{tool}</span>
              </label>
              {original ? (
                <p
                  className="pl-6 text-xs text-muted"
                  data-testid={tid(`tool-desc-${tool}`, index)}
                  title={detail?.description ?? undefined}
                >
                  {original}
                </p>
              ) : null}
              {on ? (
                <div className="mt-1 grid grid-cols-2 gap-2 pl-6">
                  <Input
                    aria-label={`override name ${tool}`}
                    aria-invalid={clashing(tool) || undefined}
                    data-testid={tid(`override-name-${tool}`, index)}
                    className="h-7 text-xs"
                    placeholder={text.overrideName}
                    value={value.overrides[tool]?.name ?? ""}
                    onChange={(e) => setOverride(tool, { name: e.target.value })}
                  />
                  <Input
                    aria-label={`override description ${tool}`}
                    data-testid={tid(`override-desc-${tool}`, index)}
                    className="h-7 text-xs"
                    placeholder={text.overrideDescription}
                    value={value.overrides[tool]?.description ?? ""}
                    onChange={(e) => setOverride(tool, { description: e.target.value })}
                  />
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
      <ConfirmDialog
        open={pendingBackend !== null}
        title={text.switchTitle}
        description={text.switchBody(value.tools.length, Object.keys(value.overrides).length)}
        confirmLabel={text.switchConfirm}
        onConfirm={() => applyBackend(pendingBackend ?? "")}
        onCancel={() => setPendingBackend(null)}
      />
    </div>
  );
}

/**
 * 저장을 막은 폼의 문제. 문장은 그릴 때 만들어 언어를 바꾸면 따라 바뀐다. 리소스 문제는 저장을 누른 때의 입력을 들고
 * 있다가 그 입력을 다시 검사해 문장을 얻는다.
 */
type FormProblem = { code: "resources"; draft: ResourcesDraft } | { code: "backends" };

function formProblemText(t: Messages, problem: FormProblem): string {
  if (problem.code === "backends") return t.mcpServers.form.backendsRequired;
  return `resources: ${resourcesFromDraft(t, problem.draft).error ?? ""}`;
}

/** /mcp-servers/new 와 /mcp-servers/:id/edit. 편집은 PUT 이고 새 리비전이 된다. */
export function McpServerFormPage() {
  const t = useT();
  const text = t.mcpServers.form;
  const { id } = useParams();
  const editing = Boolean(id);
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const existing = useQuery({
    queryKey: ["mcp-servers", tenant, id],
    queryFn: () => getMcpServer(id as string),
    enabled: editing,
  });
  const backends = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis });
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });

  const [form, setForm] = useState<FormState>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (existing.data && !loaded) {
      setForm(fromProfile(existing.data));
      setLoaded(true);
    }
  }, [existing.data, loaded]);
  // 버전 목록은 여기서 읽는다. 이름 충돌 검사가 백엔드 전부의 tool_names 를 한자리에서 봐야 하기 때문이다
  const versionQueries = useQueries({
    queries: form.backends.map((b) => ({
      queryKey: ["backend-apis", tenant, b.backendId, "versions"],
      queryFn: () => listVersions(b.backendId),
      enabled: Boolean(b.backendId),
    })),
  });
  const availables = form.backends.map(
    (b, i) => versionQueries[i]?.data?.find((v) => String(v.version) === b.specVersion)?.tool_names ?? [],
  );
  const clashes = toolNameClashes(form.backends, availables);
  // 버전 목록이 아직 안 왔으면 availables 가 비어 있어 이름 겹침 검사가 헛돈다. 그동안은 저장을 막는다
  const versionsPending = form.backends.some((b, i) => Boolean(b.backendId) && (versionQueries[i]?.isPending ?? false));
  const set = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));
  const setBackend = (index: number, patch: Partial<BackendState>) =>
    setForm((f) => ({
      ...f,
      backends: f.backends.map((b, i) => (i === index ? { ...b, ...patch } : b)),
    }));

  const [formError, setFormError] = useState<FormProblem | null>(null);
  const save = useMutation({
    mutationFn: (body: MCPServerBody) => (editing ? putMcpServer(id as string, body) : createMcpServer(body)),
    onSuccess: async (p) => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      navigate(`/mcp-servers/${p.id}`);
    },
  });

  // 저장하지 않은 입력을 두고 나가려 하면 묻는다 (D43 다섯째). 저장이 끝나면 지킬 것이 없다
  const dirty = useDirty(JSON.stringify(form), !editing || loaded);
  const confirmLeave = useUnsavedGuard(dirty && !save.isPending && !save.isSuccess);

  function submit() {
    const res = resourcesFromDraft(t, form.resources);
    if (res.error) {
      setFormError({ code: "resources", draft: form.resources });
      return;
    }
    if (form.backends.some((b) => !b.backendId || !b.specVersion)) {
      setFormError({ code: "backends" });
      return;
    }
    setFormError(null);
    // 노출되는 도구의 오버라이드만 보낸다. `tools` 가 비면 "전부 노출" 이라는 뜻이므로
    // b.tools 만 돌면 그런 프로필의 오버라이드가 저장할 때 통째로 사라진다. 제안을 채택한
    // 결과가 정확히 그 모양이라(D37) 이 자리를 exposedTools 로 돌아야 한다.
    // 버전 목록을 못 읽었으면 노출 집합을 모르는 것이라 거르지 않고 그대로 보낸다. 걸렀다가는
    // 같은 유실이 되살아나고, 키가 낡았으면 서버의 backend_final_names 가 422 로 말해 준다
    const body = form.backends.map((b, i) => {
      const available = availables[i] ?? [];
      const exposed = b.tools.length || available.length ? new Set(exposedTools(b, available)) : null;
      const overrides: Record<string, ToolOverride> = {};
      for (const [tool, o] of Object.entries(b.overrides)) {
        if (exposed === null || exposed.has(tool)) overrides[tool] = o;
      }
      return {
        backend_profile_id: b.backendId,
        spec_version: Number(b.specVersion),
        tools: b.tools,
        tool_overrides: overrides,
        ...(b.timeoutSeconds === undefined ? {} : { timeout_seconds: b.timeoutSeconds }),
      };
    });
    // 편집은 저장된 name 을 그대로 보낸다 (name_immutable). display_name 은 비우면 서버가 name 으로 채운다
    save.mutate({
      name: form.name.trim(),
      display_name: form.displayName.trim() || undefined,
      backends: body,
      runtime: { image: form.image.trim() || null, replicas: Number(form.replicas) || 1, resources: res.value },
      bindings: form.bindings,
    });
  }

  if (editing && existing.isLoading) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (editing && existing.error) return <ErrorMessage error={existing.error} />;

  return (
    <PageWidth data-testid="mcp-server-form-page">
      <PageHeader title={editing ? t.shared.editTitle(existing.data ? displayName(existing.data) : "") : text.title} name={existing.data ? secondaryName(existing.data) : undefined} testId="page-title" />
      {editing ? (
        <Alert variant="info" className="mb-4" data-testid="revision-notice">
          {text.revisionNotice((existing.data?.revision ?? 0) + 1)}
        </Alert>
      ) : null}
      <form
        className="grid gap-4 lg:grid-cols-2"
        data-testid="mcp-server-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Card>
          <CardHeader>
            <CardTitle>{text.source}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <NameFields
              placeholders={text.placeholders}
              testId="mp"
              value={{ displayName: form.displayName, name: form.name }}
              onChange={(v) => set({ displayName: v.displayName, name: v.name })}
              readOnlyName={editing}
              errors={nameFieldErrors(save.error)}
            />
            {form.backends.map((b, i) => (
              <BackendSection
                key={i}
                index={i}
                value={b}
                backends={backends}
                versions={versionQueries[i] as UseQueryResult<BackendAPIVersion[]>}
                clashes={clashes}
                onChange={(patch) => setBackend(i, patch)}
                onRemove={
                  form.backends.length > 1
                    ? () => set({ backends: form.backends.filter((_, j) => j !== i) })
                    : null
                }
              />
            ))}
            <div>
              <Button
                size="sm"
                variant="outline"
                data-testid="backend-add"
                onClick={() => set({ backends: [...form.backends, { ...EMPTY_BACKEND }] })}
              >
                {text.addBackend}
              </Button>
              <p className="mt-1 text-xs text-muted">
                {text.uniqueNote}
              </p>
            </div>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>{text.runtime}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="grid grid-cols-[1fr_6rem] gap-3">
                <Field label={text.image} htmlFor="mp-image" hint={text.imageHint}>
                  <Input id="mp-image" data-testid="mp-image" value={form.image} onChange={(e) => set({ image: e.target.value })} />
                </Field>
                <Field label={text.replicas} hint={t.detail.deployable.replicasHint} htmlFor="mp-replicas">
                  <Input id="mp-replicas" data-testid="mp-replicas" type="number" min={1} max={10} value={form.replicas} onChange={(e) => set({ replicas: e.target.value })} />
                </Field>
              </div>
              <ResourcesEditor testId="mp" value={form.resources} onChange={(r) => set({ resources: r })} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{text.bindings}</CardTitle>
            </CardHeader>
            <CardContent>
              <BindingsEditor value={form.bindings} onChange={(b) => set({ bindings: b })} dataServices={dataServices.data ?? []} />
            </CardContent>
          </Card>

          {formError ? (
            <Alert variant="error" data-testid="form-error">
              {formProblemText(t, formError)}
            </Alert>
          ) : null}
          <ToolNameClashAlert clashes={clashes} testId="tool-name-clash" />
          {!isNameFieldError(save.error) ? <ErrorMessage error={save.error} testId="save-error" /> : null}
          {/* 폼이 못 잡은 충돌(예: 아직 안 읽힌 버전)은 서버가 conflicts 로 알려 준다. 같은 문장으로 그린다 */}
          {clashes.length === 0 ? <ToolNameClashAlert clashes={clashesOfError(save.error)} testId="tool-name-clash-server" /> : null}
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              onClick={() => {
                if (confirmLeave()) navigate(editing ? `/mcp-servers/${id}` : "/mcp-servers");
              }}
            >
              {t.common.cancel}
            </Button>
            <Button
              type="submit"
              data-testid="save-button"
              disabled={save.isPending || versionsPending || clashes.length > 0}
              title={
                clashes.length
                  ? text.clashTitle
                  : versionsPending
                    ? text.versionsPending
                    : undefined
              }
            >
              {save.isPending ? t.shared.saving : editing ? t.detail.deployable.saveRevision : t.shared.create}
            </Button>
          </div>
        </div>
      </form>
    </PageWidth>
  );
}
