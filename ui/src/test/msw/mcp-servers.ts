/**
 * MCP Server 픽스처와 핸들러 (D42).
 *
 * 문서 하나가 정의(backends·runtime·bindings·revisions)와 배포(state·deployed_revision·cluster_id·k8s·
 * status)를 함께 갖는다. 그래서 픽스처도 하나이고, 안 올린 모양은 `draftFixture()` 가 같은 문서에서
 * 배포 쪽을 비운 것이다.
 *
 * 게시는 허브 목(`hubState`)과 상태를 주고받는다. hub.ts 와 서로 import 하지만 모듈 평가 시점에는
 * 상대 값을 읽지 않고 핸들러 안에서만 읽으므로 순환이 문제되지 않는다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { ExposureInput, HubAccessInput, InstanceHubItem, MCPServer, MCPServerRevision, Manifest, ManifestDocument } from "@/api/types";

import { HUB_ACTIONS } from "@/api/types";
import { normalizeAccess } from "@/lib/hubAccess";

import { NOW, apiError, docStore, http, record } from "./core";
import { UNAUTH_WARNING, exposureOf, exposureOptionsFixture, hostInUse } from "./exposure";
import { deriveScope, hubItemFixture, hubState, hubStore } from "./hub";

export const previewCR: Manifest = {
  apiVersion: "toolhive.stacklok.dev/v1alpha1",
  kind: "MCPServer",
  metadata: { name: "mcp-calc", namespace: "daegam-default", labels: { "daegam.devops.ai.kr/tenant": "default" } },
  spec: { image: "daegam-mcp-runtime:0.1.0", transport: "streamable-http", replicas: 2 },
};

/** 배포까지 끝난 MCP Server. 대부분의 테스트가 이 모양에서 출발한다. */
export function mcpServerFixture(over: Partial<MCPServer> = {}): MCPServer {
  return {
    id: "ms-calc",
    tenant: "default",
    name: "calc",
    display_name: "calc",
    state: "deployed",
    revision: 2,
    deployed_revision: 2,
    backends: [
      {
        backend_profile_id: "bp-orders",
        spec_version: 2,
        tools: ["CreateOrder", "GetOrder"],
        tool_overrides: { GetOrder: { name: "get_order", description: null } },
        timeout_seconds: 30,
      },
    ],
    runtime: { image: null, replicas: 2, resources: { limits: { cpu: "500m" } } },
    bindings: [{ data_service_id: "ds-pg", env_prefix: "PG" }],
    revisions: [1, 2],
    cluster_id: "cl-home",
    k8s: { name: "mcp-calc", namespace: "daegam-default", group: "daegam-default", token_secret: "mcp-calc-token" },
    exposure: { enabled: false, host: null, ingress_class: null, tls: { mode: "none", secret_name: null, issuer: null } },
    effective_config: {
      namespace: { name: "daegam-default" },
      images: { runtime_image: "daegam-mcp-runtime:0.1.0", agent_image: "daegam-agent-runtime:0.1.0" },
      replicas: 2,
      resources: { limits: { cpu: "500m" } },
      bindings: [{ data_service_id: "ds-pg", env_prefix: "PG" }],
    },
    spec_token_id: "tok-9",
    spec_token_ids: ["tok-9"],
    k8s_uid: null,
    svc_token_rev: "0",
    overrides: {},
    last_error: null,
    last_spec_change: NOW,
    status: {
      phase: "Pending",
      url: null,
      message: "MCPServer daegam-default/mcp-calc not found",
      conditions: [],
      ready_replicas: null,
      warnings: [],
      observed_at: NOW,
      error: null,
    },
    // 게시는 수동이다 (D23). 게시된 모양은 publishedHubRef 로 덮어쓴다
    hub_item: null,
    created_at: NOW,
    updated_at: NOW,
    updated_by: null,
    ...over,
  };
}

/** 만들기만 하고 아직 안 올린 문서. 배포 쪽이 비어 있다 (D42). */
export function draftFixture(over: Partial<MCPServer> = {}): MCPServer {
  return mcpServerFixture({
    id: "ms-draft",
    name: "draft",
    display_name: "draft",
    state: "draft",
    deployed_revision: null,
    cluster_id: null,
    k8s: null,
    effective_config: null,
    spec_token_id: null,
    spec_token_ids: [],
    status: null,
    ...over,
  });
}

/** 폴링 테스트가 상태를 바꿀 수 있게 mutable 로 둔다. */
export const mcpServerState: { items: MCPServer[] } = { items: [mcpServerFixture()] };

export function resetMcpServerState(): void {
  mcpServerState.items = [mcpServerFixture()];
}

const mcpServerStore = docStore(mcpServerState, "mcp server");

/** ms-calc 가 hub-calc 로 게시돼 있고 gw-support 가 참조하는 상태의 hub_item. */
export function publishedHubRef(over: Partial<InstanceHubItem> = {}): InstanceHubItem {
  return { id: "hub-calc", name: "calc", display_name: "calc", permissions: { view: true, consume: true, manage: true }, gateway_ids: ["gw-support"], ...over };
}

/** TLS Secret 으로 노출된 MCP Server. 앞단 프록시 Ready, 주소 할당, header 모드 무인증 경고. */
export function exposedMcpServerFixture(over: Partial<MCPServer> = {}): MCPServer {
  const base = mcpServerFixture();
  return mcpServerFixture({
    exposure: { enabled: true, host: "calc.default.apps.local", ingress_class: "traefik", tls: { mode: "secret", secret_name: "calc-tls", issuer: null } },
    status: {
      ...base.status!,
      phase: "Ready",
      url: "http://mcp-calc-proxy.daegam-default.svc:8080/mcp",
      external_url: "https://calc.default.apps.local/mcp",
      exposure: { front_phase: "Ready", address_assigned: true, tls_secret_present: true, certificate_ready: null },
      message: null,
      ready_replicas: 2,
      warnings: [UNAUTH_WARNING],
    },
    ...over,
  });
}

export const mcpRevisions: MCPServerRevision[] = [
  {
    revision: 1,
    created_at: NOW,
    backends: [{ backend_profile_id: "bp-orders", spec_version: 2, tools: ["CreateOrder"], tool_overrides: {}, timeout_seconds: 30 }],
    runtime: { image: null, replicas: 1, resources: null },
    bindings: [],
  },
  {
    revision: 2,
    created_at: NOW,
    backends: [
      {
        backend_profile_id: "bp-orders",
        spec_version: 2,
        tools: ["CreateOrder", "GetOrder"],
        tool_overrides: { GetOrder: { name: "get_order", description: null } },
        timeout_seconds: 30,
      },
    ],
    runtime: { image: null, replicas: 2, resources: { limits: { cpu: "500m" } } },
    bindings: [{ data_service_id: "ds-pg", env_prefix: "PG" }],
  },
];

export const manifestDoc: ManifestDocument = {
  kind: "MCPServer",
  name: "mcp-calc",
  namespace: "daegam-default",
  rendered: previewCR,
  overrides: {},
  final: previewCR,
  live: { ...previewCR, spec: { ...(previewCR.spec as object), replicas: 3 } },
  drift: ["spec.replicas"],
  drift_patch: { spec: { replicas: 3 } },
  drift_managers: ["kubectl-edit"],
  locked: ["spec.image", "metadata.labels[daegam.devops.ai.kr/*]"],
  ignored_by_operator: ["spec.podTemplateSpec.spec.containers[name=mcp].args"],
  error: null,
};

export const mcpServerHandlers: HttpHandler[] = [
  http.get("/mcp-servers", async ({ request }) => {
    await record(request);
    return HttpResponse.json(mcpServerState.items);
  }),
  // 만들기는 저장이다. 올리는 것은 배포가 따로 한다 (D42)
  http.post("/mcp-servers", async ({ request }) => {
    const r = await record(request);
    const body = r.body as Record<string, unknown>;
    const made = draftFixture({ ...body, id: "ms-new", display_name: (body.display_name as string) ?? (body.name as string), revision: 1, revisions: [1] });
    mcpServerState.items = [...mcpServerState.items, made];
    return HttpResponse.json(made, { status: 201 });
  }),
  http.get("/mcp-servers/:id", async ({ request, params }) => {
    await record(request);
    const s = mcpServerStore.findOr404(params.id);
    return s instanceof Response ? s : HttpResponse.json(s);
  }),
  http.put("/mcp-servers/:id", async ({ request, params }) => {
    const r = await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    const body = r.body as Record<string, unknown>;
    // 갱신은 name 이 저장값과 같아야 한다 (D24, 422 name_immutable)
    if (body.name !== s.name) return apiError(422, "name_immutable", "name cannot be changed after creation");
    return HttpResponse.json(mcpServerStore.replace({ ...s, ...body, display_name: (body.display_name as string) ?? s.display_name, revision: s.revision + 1, revisions: [...s.revisions, s.revision + 1] }));
  }),
  http.delete("/mcp-servers/:id", async ({ request, params }) => {
    await record(request);
    // 게시 항목은 문서를 따라 사라진다 (9절)
    hubState.items = hubState.items.filter((i) => i.source.instance_id !== params.id);
    mcpServerStore.remove(params.id);
    return HttpResponse.json({ id: params.id, deleted: true, deleted_at: NOW, revoked_token_ids: ["tok-9"] });
  }),

  // -- 배포 동작 (D42) ---------------------------------------------------------------
  http.post("/mcp-servers/:id/deploy", async ({ request, params }) => {
    const r = await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    if (s.state !== "draft") return apiError(409, "state_conflict", `mcp server is ${s.state}, not draft`);
    const body = (r.body ?? {}) as { cluster_id?: string | null; namespace?: string | null; exposure?: ExposureInput | null };
    // 서버 검증(8절)의 대표 코드. host 가 taken 이면 host_conflict, 네임스페이스가 허용 목록 밖이면 namespace_not_allowed
    const taken = hostInUse(body.exposure);
    if (taken) return taken;
    if (body.namespace && !exposureOptionsFixture().namespaces.includes(body.namespace)) {
      return apiError(422, "namespace_not_allowed", `namespace ${body.namespace} is not allowed for tenant default`);
    }
    const ns = body.namespace ?? "daegam-default";
    const exp = exposureOf(body.exposure, s.name);
    const base = mcpServerFixture();
    const next = mcpServerStore.replace({
      ...s,
      state: "deploying",
      deployed_revision: s.revision,
      cluster_id: body.cluster_id ?? "cl-home",
      k8s: { ...base.k8s!, name: `mcp-${s.name}`, namespace: ns },
      exposure: exp.exposure,
      effective_config: base.effective_config,
      status: {
        ...base.status!,
        external_url: exp.external_url,
        exposure: exp.exposure.enabled ? { front_phase: "Pending", address_assigned: false, tls_secret_present: exp.exposure.tls.mode === "secret" ? true : null, certificate_ready: null } : null,
        warnings: exp.exposure.enabled ? [UNAUTH_WARNING] : [],
      },
    });
    return HttpResponse.json({ ...next, warnings: ["network_policy_unenforced", ...(exp.exposure.enabled ? [UNAUTH_WARNING] : [])] });
  }),
  http.post("/mcp-servers/:id/redeploy", async ({ request, params }) => {
    await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    return HttpResponse.json(mcpServerStore.replace({ ...s, deployed_revision: s.revision, status: { ...s.status!, phase: "Rolling" } }));
  }),
  // 배포 해제. 정의와 리비전 이력은 남고 배포 쪽만 비운다 (D42)
  http.post("/mcp-servers/:id/undeploy", async ({ request, params }) => {
    await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    return HttpResponse.json(mcpServerStore.replace({ ...s, state: "draft", deployed_revision: null, cluster_id: null, k8s: null, effective_config: null, status: null }));
  }),
  // 복제. 정의만 베낀 draft 를 만든다
  http.post("/mcp-servers/:id/clone", async ({ request, params }) => {
    const r = await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    const body = r.body as { name: string; display_name?: string | null };
    if (mcpServerState.items.some((x) => x.name === body.name)) return apiError(409, "name_conflict", `mcp server '${body.name}' already exists`);
    const made = draftFixture({ id: "ms-clone", name: body.name, display_name: body.display_name || body.name, backends: s.backends, runtime: s.runtime, bindings: s.bindings, revision: 1, revisions: [1] });
    mcpServerState.items = [...mcpServerState.items, made];
    return HttpResponse.json(made, { status: 201 });
  }),

  http.patch("/mcp-servers/:id", async ({ request, params }) => {
    const r = await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    const body = r.body as { exposure?: ExposureInput | null };
    const taken = hostInUse(body.exposure);
    if (taken) return taken;
    const exp = exposureOf(body.exposure, s.name);
    return HttpResponse.json(
      mcpServerStore.replace({
        ...s,
        exposure: exp.exposure,
        status: {
          ...s.status!,
          phase: "Rolling",
          external_url: exp.external_url,
          exposure: exp.exposure.enabled ? { front_phase: "Pending", address_assigned: false, tls_secret_present: null, certificate_ready: null } : null,
          warnings: exp.exposure.enabled ? [UNAUTH_WARNING] : [],
        },
      }),
    );
  }),

  // -- 리비전·미리보기 ----------------------------------------------------------------
  http.get("/mcp-servers/:id/revisions", async ({ request }) => {
    await record(request);
    return HttpResponse.json(mcpRevisions);
  }),
  http.post("/mcp-servers/:id/revisions/:n/preview", async ({ request, params }) => {
    const r = await record(request);
    const body = (r.body ?? {}) as { cluster_id?: string | null };
    const clusterId = body.cluster_id ?? new URL(r.path, "http://localhost").searchParams.get("cluster_id");
    if (clusterId === "nope") return apiError(422, "unknown_cluster", "cluster nope not found");
    if (params.n === "9") return apiError(404, "not_found", "revision 9 not found");
    return HttpResponse.json({
      cluster_id: clusterId ?? "cl-home",
      namespace: "daegam-default",
      effective_config: {},
      manifest: { ...previewCR, spec: { ...(previewCR.spec as object), replicas: Number(params.n) } },
      locked: ["spec.image"],
    });
  }),
  // 제안 채택 (D37). 고른 id 만 심어 새 리비전을 만든다
  http.post("/mcp-servers/:id/apply-draft", async ({ request, params }) => {
    const r = await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    const body = r.body as { job_id?: string; accepted_ids?: string[] };
    if (body.job_id !== "job-overrides-1") return apiError(422, "job_not_found", `spec job ${String(body.job_id)} not found`);
    const ids = body.accepted_ids ?? [];
    if (ids.length && ids.some((i) => i !== "o1" && i !== "o2")) {
      return apiError(422, "unknown_change", "1 accepted id(s) are not in the change list");
    }
    const take = (id: string) => ids.length === 0 || ids.includes(id);
    const tool_overrides: Record<string, { name: string | null; description: string | null }> = {
      GetOrder: { name: take("o1") ? "getOrder" : "get_order", description: null },
    };
    if (take("o2")) tool_overrides.CreateOrder = { name: "createOrder", description: "주문을 만든다." };
    return HttpResponse.json(mcpServerStore.replace({ ...s, revision: 3, revisions: [1, 2, 3], backends: [{ ...s.backends[0], tool_overrides }] }), { status: 201 });
  }),

  // -- 허브 게시 (9절, D23) ------------------------------------------------------------
  http.post("/mcp-servers/:id/publish", async ({ request, params }) => {
    const r = await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    const body = (r.body ?? {}) as { name?: string | null; display_name?: string | null; access?: HubAccessInput | null };
    if (s.hub_item) {
      const existing = hubStore.find(s.hub_item.id);
      return HttpResponse.json(existing ?? hubItemFixture({ id: s.hub_item.id, name: s.hub_item.name }));
    }
    const name = body.name?.trim() || s.name;
    if (hubState.items.some((i) => i.name === name)) return apiError(409, "name_conflict", `hub item ${name} already exists`);
    const access = normalizeAccess(body.access);
    const crossed = HUB_ACTIONS.find((a) => access[a].tenants.some((t) => t !== s.tenant));
    if (crossed) return apiError(422, "cross_tenant_unsupported", "internal items cannot be opened to other tenants", { action: crossed, kind: "internal" });
    const item = hubItemFixture({
      id: `hub-inst-${name}`,
      name,
      // 허브 항목 display_name 의 기본값은 문서의 display_name 이다 (D24)
      display_name: body.display_name?.trim() || s.display_name || s.name,
      // 허브 출처 필드 이름은 서버가 그대로 두었다. 가리키는 것은 배포된 워크로드 하나다
      source: { ...hubItemFixture().source, instance_id: s.id, mcp_profile_id: s.id },
      k8s: { kind: "MCPServer", name: s.k8s!.name, namespace: s.k8s!.namespace, group: s.k8s!.group, tool_config: null },
      access,
      scope: deriveScope(access, s.tenant),
      access_updated_by: "header-admin",
      access_updated_at: NOW,
    });
    hubState.items = [...hubState.items, item];
    mcpServerStore.replace({ ...s, hub_item: { id: item.id, name: item.name, display_name: item.display_name, permissions: item.permissions, gateway_ids: [] } });
    return HttpResponse.json(item, { status: 201 });
  }),
  http.delete("/mcp-servers/:id/publish", async ({ request, params }) => {
    await record(request);
    const s = mcpServerStore.findOr404(params.id);
    if (s instanceof Response) return s;
    if (!s.hub_item) return apiError(404, "not_published", "mcp server is not published");
    if (!s.hub_item.permissions.manage) return apiError(403, "forbidden", "manage is required", { action: "manage" });
    if (s.hub_item.gateway_ids.length) {
      return apiError(409, "in_use", "hub item is used by gateways", { gateway_ids: s.hub_item.gateway_ids });
    }
    hubStore.remove(s.hub_item.id);
    const removed = s.hub_item.id;
    mcpServerStore.replace({ ...s, hub_item: null });
    return HttpResponse.json({ id: removed, deleted: true });
  }),
  http.get("/mcp-servers/:id/manifest", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ documents: [manifestDoc] });
  }),
];
