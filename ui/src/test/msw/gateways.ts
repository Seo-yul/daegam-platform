/**
 * Gateway 픽스처와 핸들러 (D42).
 *
 * 정의 쪽 `backends` 는 고른 허브 항목이고, 배포 쪽 `deployed_backends` 는 배포할 때 해석한 Entry·URL 이다.
 * 이름이 겹쳐 서버가 배포 쪽에 `deployed_` 접두를 붙였고 목도 같은 이름을 쓴다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { ExposureInput, Gateway, GatewayRevision, HubAccessInput, HubItem, Manifest } from "@/api/types";

import { HUB_ACTIONS } from "@/api/types";
import { normalizeAccess } from "@/lib/hubAccess";

import { NOW, apiError, docStore, http, record } from "./core";
import { UNAUTH_WARNING, exposureOf, hostInUse } from "./exposure";
import { hubItemFixture, hubState, hubStore } from "./hub";

export const vmcpCR: Manifest = {
  apiVersion: "toolhive.stacklok.dev/v1alpha1",
  kind: "VirtualMCPServer",
  metadata: { name: "gw-support", namespace: "daegam-default" },
  spec: { groupRef: "gw-support", config: { aggregation: { conflictResolution: "priority" } } },
};

/** 배포까지 끝난 Gateway. 백엔드 둘 중 하나가 불가라 Degraded 다. */
export function gatewayFixture(over: Partial<Gateway> = {}): Gateway {
  return {
    id: "gw-support",
    tenant: "default",
    name: "support",
    display_name: "support",
    state: "deployed",
    revision: 2,
    deployed_revision: 2,
    backends: [
      { hub_item_id: "hub-calc", tools: ["Add"], timeout_seconds: 90 },
      { hub_item_id: "hub-weather", tools: [] },
    ],
    conflict_resolution: "auto",
    failure_mode: "best_effort",
    authz_policy: null,
    runtime: { replicas: 1, resources: null },
    surface: { resources: false, prompts: false },
    auth: "anonymous",
    revisions: [1, 2],
    cluster_id: "cl-home",
    k8s: { name: "gw-support", namespace: "daegam-default", group: "gw-support" },
    deployed_backends: [
      { hub_item_id: "hub-calc", hub_item_name: "calc", kind: "internal", workload: "gw-support-calc", url: "http://mcp-calc-proxy.daegam-default.svc:8080/mcp", transport: "streamable-http", tools: ["Add"], tool_names: ["Add", "Divide"], shared_source_id: null, clone_kind: null, clone_name: null, timeout_seconds: 35, server_id: "ms-calc" },
      { hub_item_id: "hub-weather", hub_item_name: "weather", kind: "remote", workload: "gw-support-weather", url: "http://hub-weather.daegam-default.svc:8080/mcp", transport: "streamable-http", tools: [], tool_names: [], shared_source_id: null, clone_kind: null, clone_name: null, timeout_seconds: 60, server_id: null },
    ],
    deployed_conflict_resolution: "priority",
    tool_overrides: {},
    exposure: { enabled: false, host: null, ingress_class: null, tls: { mode: "none", secret_name: null, issuer: null } },
    k8s_uid: "uid-1",
    svc_token_rev: "0",
    effective_config: { namespace: { name: "daegam-default" } },
    overrides: {},
    last_error: null,
    last_spec_change: NOW,
    status: {
      phase: "Degraded",
      url: "http://vmcp-gw-support.daegam-default.svc.cluster.local:4483/mcp",
      external_url: null,
      message: null,
      badge: "일부 백엔드 불가",
      badge_code: "some_backends_down",
      failure_mode: "best_effort",
      backend_count: 1,
      expected_backends: 2,
      backends: [
        { workload: "gw-support-calc", hub_item_id: "hub-calc", hub_item_name: "calc", kind: "internal", url: "http://mcp-calc-proxy.daegam-default.svc:8080/mcp", entry_phase: "Valid", status: "ready", consecutive_failures: null, message: null },
        { workload: "gw-support-weather", hub_item_id: "hub-weather", hub_item_name: "weather", kind: "remote", url: "http://hub-weather.daegam-default.svc:8080/mcp", entry_phase: "Valid", status: "unavailable", consecutive_failures: 3, message: "connection refused" },
      ],
      conditions: [{ type: "Ready", status: "False", reason: "BackendUnavailable", message: "1 of 2 backends unavailable", last_transition: NOW }],
      warnings: [],
      observed_at: NOW,
      error: null,
      generation: 1,
      observed_generation: 1,
    },
    created_at: NOW,
    updated_at: NOW,
    updated_by: null,
    ...over,
  };
}

/** 만들기만 하고 아직 안 올린 Gateway. 배포 쪽이 비어 있다 (D42). */
export function gatewayDraftFixture(over: Partial<Gateway> = {}): Gateway {
  return gatewayFixture({
    id: "gw-draft",
    name: "draft",
    display_name: "draft",
    state: "draft",
    deployed_revision: null,
    cluster_id: null,
    k8s: null,
    deployed_backends: [],
    effective_config: null,
    status: null,
    ...over,
  });
}

/** http 로 노출된 Gateway. 주소 할당, 무인증 경고. */
export function exposedGatewayFixture(over: Partial<Gateway> = {}): Gateway {
  const base = gatewayFixture();
  return gatewayFixture({
    exposure: { enabled: true, host: "gw-support.e2e.local", ingress_class: "traefik", tls: { mode: "none", secret_name: null, issuer: null } },
    status: {
      ...base.status!,
      phase: "Ready",
      badge: null,
      external_url: "http://gw-support.e2e.local/mcp",
      exposure: { front_phase: null, address_assigned: true, tls_secret_present: null, certificate_ready: null },
      warnings: [UNAUTH_WARNING],
    },
    ...over,
  });
}

/** local 모드에서 노출한 Gateway (P32 연결 카드). 인증 경계가 local 이고 외부 주소가 있어 무인증 경고가 없다. */
export function localGatewayFixture(over: Partial<Gateway> = {}): Gateway {
  const base = exposedGatewayFixture();
  return exposedGatewayFixture({ auth: "local", status: { ...base.status!, warnings: [] }, ...over });
}

export const gatewayRevisions: GatewayRevision[] = [
  { revision: 1, created_at: NOW, backends: [{ hub_item_id: "hub-calc", tools: [] }], conflict_resolution: "priority", failure_mode: "best_effort", authz_policy: null, runtime: { replicas: 1, resources: null }, surface: { resources: false, prompts: false } },
  { revision: 2, created_at: NOW, backends: gatewayFixture().backends, conflict_resolution: "auto", failure_mode: "best_effort", authz_policy: null, runtime: { replicas: 1, resources: null }, surface: { resources: false, prompts: false } },
];

/** 삭제·배포가 반영되는 목록이다. */
export const gatewayState: { items: Gateway[] } = { items: [gatewayFixture()] };

export function resetGatewayState(): void {
  gatewayState.items = [gatewayFixture()];
}

const gatewayStore = docStore(gatewayState, "gateway");

/** Gateway 저장의 backends[] 검사 (5절). view 가 없으면 unknown_hub_item, consume 이 없으면 hub_item_not_consumable. */
function notConsumable(body: Record<string, unknown>): Response | null {
  const backends = (body.backends as { hub_item_id: string }[] | undefined) ?? [];
  for (const b of backends) {
    const item = hubStore.find(b.hub_item_id);
    if (!item || !item.permissions.view) return apiError(422, "unknown_hub_item", `hub item ${b.hub_item_id} not found`, { hub_item_id: b.hub_item_id });
    if (!item.permissions.consume) return apiError(422, "hub_item_not_consumable", `hub item ${item.name} cannot be consumed`, { hub_item_id: item.id });
  }
  return null;
}

export const gatewayHandlers: HttpHandler[] = [
  http.get("/gateways", async ({ request }) => {
    await record(request);
    return HttpResponse.json(gatewayState.items);
  }),
  // 만들기는 저장이다. 올리는 것은 배포가 따로 한다 (D42)
  http.post("/gateways", async ({ request }) => {
    const r = await record(request);
    const body = r.body as Record<string, unknown>;
    const blocked = notConsumable(body);
    if (blocked) return blocked;
    const made = gatewayDraftFixture({ ...body, id: "gw-new", display_name: (body.display_name as string) ?? (body.name as string), revision: 1, revisions: [1] });
    gatewayState.items = [...gatewayState.items, made];
    return HttpResponse.json({ ...made, warnings: ["hub item weather has no tool_names; conflict planning falls back to prefix mode"] }, { status: 201 });
  }),
  http.get("/gateways/:id", async ({ request, params }) => {
    await record(request);
    const g = gatewayStore.findOr404(params.id);
    return g instanceof Response ? g : HttpResponse.json(g);
  }),
  http.put("/gateways/:id", async ({ request, params }) => {
    const r = await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    const body = r.body as Record<string, unknown>;
    const blocked = notConsumable(body);
    if (blocked) return blocked;
    // 갱신은 name 이 저장값과 같아야 한다 (D24, 422 name_immutable)
    if (body.name !== g.name) return apiError(422, "name_immutable", "name cannot be changed after creation");
    return HttpResponse.json({ ...gatewayStore.replace({ ...g, ...body, display_name: (body.display_name as string) ?? g.display_name, revision: g.revision + 1, revisions: [...g.revisions, g.revision + 1] }), warnings: [] });
  }),
  http.delete("/gateways/:id", async ({ request, params }) => {
    await record(request);
    if (hubState.items.some((i) => i.kind === "published" && i.source.gateway_instance_id === params.id)) {
      return apiError(409, "published", "unpublish the hub item first");
    }
    gatewayStore.remove(params.id);
    return HttpResponse.json({ id: params.id, deleted: true, deleted_at: NOW });
  }),

  // -- 배포 동작 (D42) ---------------------------------------------------------------
  http.post("/gateways/:id/deploy", async ({ request, params }) => {
    const r = await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    if (g.state !== "draft") return apiError(409, "state_conflict", `gateway is ${g.state}, not draft`);
    const body = (r.body ?? {}) as { cluster_id?: string | null; namespace?: string | null; exposure?: ExposureInput | null };
    if (g.name === "not-ready") {
      return apiError(409, "backends_not_ready", "all backends must be Ready before deploying the vMCP", {
        backends: [
          { hub_item_id: "hub-calc", name: "calc", workload: "gw-not-ready-calc", phase: "Ready", ready: true, error: null },
          { hub_item_id: "hub-weather", name: "weather", workload: "gw-not-ready-weather", phase: "Pending", ready: false, error: "unreachable: ConnectError: refused" },
        ],
      });
    }
    if (g.name === "loop") {
      return apiError(422, "cycle", "gateway would consume its own publication", { path: ["gw-support", "hub-pub-bot", "gw-support"] });
    }
    const taken = hostInUse(body.exposure);
    if (taken) return taken;
    const exp = exposureOf(body.exposure, `gw-${g.name}`);
    const base = gatewayFixture();
    const next = gatewayStore.replace({
      ...g,
      state: "deploying",
      deployed_revision: g.revision,
      cluster_id: body.cluster_id ?? "cl-home",
      k8s: { ...base.k8s!, name: `gw-${g.name}`, namespace: body.namespace ?? "daegam-default" },
      deployed_backends: base.deployed_backends,
      effective_config: base.effective_config,
      exposure: exp.exposure,
      status: {
        ...base.status!,
        external_url: exp.external_url,
        exposure: exp.exposure.enabled ? { front_phase: null, address_assigned: false, tls_secret_present: null, certificate_ready: null } : null,
        warnings: exp.exposure.enabled ? [UNAUTH_WARNING] : [],
      },
    });
    return HttpResponse.json({ ...next, warnings: ["network_policy_unenforced", ...(exp.exposure.enabled ? [UNAUTH_WARNING] : [])] });
  }),
  http.post("/gateways/:id/redeploy", async ({ request, params }) => {
    await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    return HttpResponse.json({ ...gatewayStore.replace({ ...g, deployed_revision: g.revision, status: { ...g.status!, phase: "Rolling" } }), warnings: [] });
  }),
  http.post("/gateways/:id/undeploy", async ({ request, params }) => {
    await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    return HttpResponse.json(gatewayStore.replace({ ...g, state: "draft", deployed_revision: null, cluster_id: null, k8s: null, deployed_backends: [], effective_config: null, status: null }));
  }),
  http.post("/gateways/:id/clone", async ({ request, params }) => {
    const r = await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    const body = r.body as { name: string; display_name?: string | null };
    if (gatewayState.items.some((x) => x.name === body.name)) return apiError(409, "name_conflict", `gateway '${body.name}' already exists`);
    const made = gatewayDraftFixture({ id: "gw-clone", name: body.name, display_name: body.display_name || body.name, backends: g.backends, runtime: g.runtime, revision: 1, revisions: [1] });
    gatewayState.items = [...gatewayState.items, made];
    return HttpResponse.json(made, { status: 201 });
  }),
  // 노출만 바꾸는 PATCH 는 리비전을 올리지 않고 재배포만 한다 (8절)
  http.patch("/gateways/:id", async ({ request, params }) => {
    const r = await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    const body = r.body as { exposure?: ExposureInput | null };
    const taken = hostInUse(body.exposure);
    if (taken) return taken;
    const exp = exposureOf(body.exposure, g.k8s!.name);
    const next = gatewayStore.replace({
      ...g,
      exposure: exp.exposure,
      status: {
        ...g.status!,
        phase: "Rolling",
        external_url: exp.external_url,
        exposure: exp.exposure.enabled ? { front_phase: null, address_assigned: false, tls_secret_present: null, certificate_ready: null } : null,
        warnings: exp.exposure.enabled ? [UNAUTH_WARNING] : [],
      },
    });
    return HttpResponse.json({ ...next, warnings: exp.exposure.enabled ? [UNAUTH_WARNING] : [] });
  }),

  // -- 리비전·미리보기 ----------------------------------------------------------------
  http.get("/gateways/:id/revisions", async ({ request }) => {
    await record(request);
    return HttpResponse.json(gatewayRevisions);
  }),
  http.get("/gateways/:id/revisions/:n/preview", async ({ request, params }) => {
    const r = await record(request);
    const url = new URL(r.path, "http://localhost");
    if (url.searchParams.get("cluster_id") === "nope") return apiError(422, "unknown_cluster", "cluster nope not found");
    // r1 은 calc 만이라 priority, r2 는 weather(tool_names 미상)가 있어 prefix 로 떨어진다
    const priority = Number(params.n) === 1;
    return HttpResponse.json({
      cluster_id: url.searchParams.get("cluster_id") ?? "cl-home",
      namespace: "daegam-default",
      manifest: vmcpCR,
      documents: [
        { apiVersion: "toolhive.stacklok.dev/v1alpha1", kind: "MCPGroup", metadata: { name: "gw-support" } },
        { apiVersion: "toolhive.stacklok.dev/v1alpha1", kind: "MCPServerEntry", metadata: { name: "gw-support-calc" } },
        vmcpCR,
      ],
      backends: [
        { hub_item_id: "hub-calc", name: "calc", workload: "gw-support-calc", phase: "Ready", ready: true, error: null },
        { hub_item_id: "hub-weather", name: "weather", workload: "gw-support-weather", phase: "Pending", ready: false, error: null },
      ],
      conflict_resolution: priority ? "priority" : "prefix",
      tool_overrides: priority ? {} : { "gw-support-calc": { Add: "gw-support-calc_Add" } },
      warnings: priority ? [] : ["some backends have no tool_names; falling back to prefix mode (tool names become <workload>_<tool>)"],
      locked: ["spec.groupRef"],
    });
  }),

  // -- Advanced·게시 ------------------------------------------------------------------
  http.get("/gateways/:id/manifest", async ({ request }) => {
    await record(request);
    return HttpResponse.json({
      documents: [
        { kind: "MCPGroup", name: "gw-support", namespace: "daegam-default", rendered: { kind: "MCPGroup" }, overrides: {}, final: { kind: "MCPGroup" }, live: null, drift: null, drift_patch: null, drift_managers: [], locked: ["*"], ignored_by_operator: [], error: null },
        { kind: "MCPServerEntry", name: "gw-support-calc", namespace: "daegam-default", rendered: { kind: "MCPServerEntry" }, overrides: {}, final: { kind: "MCPServerEntry" }, live: null, drift: null, drift_patch: null, drift_managers: [], locked: ["*"], ignored_by_operator: [], error: null },
        { kind: "VirtualMCPServer", name: "gw-support", namespace: "daegam-default", rendered: vmcpCR, overrides: {}, final: vmcpCR, live: vmcpCR, drift: [], drift_patch: {}, drift_managers: [], locked: ["spec.groupRef", "metadata.name"], ignored_by_operator: [], error: null },
      ],
    });
  }),
  http.put("/gateways/:id/overrides", async ({ request }) => {
    const r = await record(request);
    const text = (r.body as { yaml?: string }).yaml ?? "";
    const m = /replicas: (\d+)/.exec(text);
    const final = { ...vmcpCR, spec: { ...(vmcpCR.spec as object), replicas: m ? Number(m[1]) : 1 } };
    return HttpResponse.json({ overrides: { spec: { replicas: m ? Number(m[1]) : 1 } }, final, diff: ["spec.replicas"], validation: "schema_only" });
  }),
  http.post("/gateways/:id/drift/:action", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ documents: [] });
  }),
  http.post("/gateways/:id/publish", async ({ request, params }) => {
    const r = await record(request);
    const g = gatewayStore.findOr404(params.id);
    if (g instanceof Response) return g;
    const body = r.body as { mode: string; name?: string | null; display_name?: string | null; access?: HubAccessInput | null };
    // 서버 규칙 (D24): 이름을 비우면 문서의 name 그대로, display_name 을 비우면 그 display_name
    const name = body.name?.trim() || g.name;
    const access = normalizeAccess(body.access);
    const crossed = HUB_ACTIONS.find((a) => access[a].tenants.some((t) => t !== "default"));
    if (crossed) return apiError(422, "cross_tenant_unsupported", "published items cannot be opened to other tenants", { action: crossed, kind: "published" });
    const item: HubItem = {
      ...hubItemFixture(),
      id: `hub-pub-${name}`,
      name,
      display_name: body.display_name?.trim() || g.display_name || g.name,
      kind: "published",
      access,
      source: { url: "http://vmcp-gw-support.daegam-default.svc.cluster.local:4483/mcp", transport: "streamable-http", image: null, port: null, instance_id: null, mcp_profile_id: null, tools_filter: null, gateway_instance_id: String(params.id) },
      k8s: { kind: body.mode === "proxy" ? "MCPRemoteProxy" : "VirtualMCPServer", name: "vmcp-gw-support", namespace: "daegam-default", group: null, tool_config: null },
      tool_names: ["Add"],
      status: { phase: "Valid", ready: true, url: "http://vmcp-gw-support.daegam-default.svc.cluster.local:4483/mcp", message: null, observed_at: NOW, error: null },
    };
    hubState.items = [...hubState.items.filter((i) => i.id !== item.id), item];
    return HttpResponse.json(item, { status: 201 });
  }),
];
