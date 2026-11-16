/** Cluster 픽스처와 핸들러 (/clusters, exposure-options, preflight). */
import { HttpResponse, type HttpHandler } from "msw";

import type { Cluster, ClusterSandboxStatus } from "@/api/types";

import { NOW, apiError, http, record } from "./core";
import { exposureOptionsFixture } from "./exposure";

export const clusterHome: Cluster = {
  id: "cl-home",
  name: "home",
  display_name: "home",
  home: true,
  connection: { mode: "kubecontext", secret_name: null, api_url: null, kubecontext: "orbstack" },
  platform_internal_url: "http://host.internal:9000",
  images: { pull_policy: "IfNotPresent" },
  namespaces: { labels: {} },
  // make deploy-local 의 home 과 같은 모양 (P8 F4): Traefik, 무인증 노출 허용, 플랫폼 host 예약
  ingress: {
    enabled: true,
    allow_unauthenticated: true,
    class_name: "traefik",
    allowed_classes: [],
    domain: "apps.local",
    tls: "none",
    default_issuer: null,
    annotations: null,
    controller_namespace: "traefik",
    controller_ip_blocks: [],
    reserved_hosts: ["daegam.com"],
  },
  scheduling: {},
  default_redis: null,
  shipped_redis_tls: null,
  recorded_toolhive_version: null,
  gates: {
    tenant_object_rights: true,
    // P40 10절: 샌드박스 권한과 PriorityClass 가 있다
    sandbox_rights: true,
    usage_metrics: false,
    strict_apply: { ok: true, checked_at: NOW, checked: 2, refused: 0, kinds: ["MCPServer"], failures: [] },
  },
  status: {
    preflight: {
      ok: true,
      checked_at: NOW,
      k8s_version: "v1.33.1",
      toolhive_version: "0.51.2",
      rbac: [{ group: "toolhive.stacklok.dev", resource: "mcpservers", verb: "create", name: null, namespace: null, allowed: true }],
      missing: [],
      network_policy: "enforced",
      policy_controller: "calico-node",
      // P63: 이 릴리스의 RBAC 파일이고 관리 네임스페이스 둘에 모두 RoleBinding 이 있다
      secret_rights: { cluster_wide: false, role: true, writable: true, namespaces: 2, unbound: 0, unbound_namespaces: [] },
      shipped_redis_cert_expires_at: null,
    },
    metadata_skipped: [],
  },
  // 코드 실행 샌드박스 (P40 10절). 격리 점검을 하지 않은 클러스터의 기본값이다
  sandbox: {
    image: null,
    scheduling: { node_selector: null, tolerations: null },
    check: {
      passed: false,
      userns: null,
      network: null,
      targets: [],
      shared_nodes: null,
      capacity: null,
      placement: null,
      image: null,
      image_id: null,
      seen_image_id: null,
      k8s_version: null,
      policy_controller: null,
      checked_at: null,
      accepted_by: null,
      accepted_at: null,
      voided_at: null,
      void_reason: null,
    },
    run: { started_at: null, finished_at: null, by: null, error: null },
    void_reason: null,
    override: false,
  },
  block_metadata: false,
  warnings: [],
  // make deploy-local 의 ConfigMap 이 정하는 DAEGAM_HOME_INGRESS_* 와 기동마다 쓰는 주소 (D54)
  env_managed: [
    "ingress.allow_unauthenticated",
    "ingress.class_name",
    "ingress.controller_namespace",
    "ingress.enabled",
    "ingress.reserved_hosts",
    "platform_internal_url",
  ],
  created_at: NOW,
  updated_at: NOW,
  updated_by: null,
};

export const clusterEdge: Cluster = {
  ...clusterHome,
  id: "cl-edge",
  name: "edge",
  display_name: "edge",
  home: false,
  connection: { mode: "kubeconfig", secret_name: "cluster-cl-edge", api_url: null, kubecontext: null },
  gates: { tenant_object_rights: false, sandbox_rights: false, usage_metrics: null, strict_apply: null },
  status: {
    preflight: {
      ok: false,
      checked_at: NOW,
      k8s_version: "v1.31.0",
      toolhive_version: null,
      rbac: [],
      missing: ["toolhive-operator"],
      network_policy: "unknown",
      policy_controller: null,
      secret_rights: null,
      shipped_redis_cert_expires_at: null,
    },
    metadata_skipped: [],
  },
  warnings: ["network_policy_unenforced"],
  env_managed: [],
};

/** 노출을 막아 둔 클러스터 (ingress.enabled: false). 노출 섹션의 비활성 사유 픽스처다. */
export const clusterLocked: Cluster = {
  ...clusterHome,
  id: "cl-locked",
  name: "locked",
  display_name: "locked",
  home: false,
  connection: { mode: "kubeconfig", secret_name: "cluster-cl-locked", api_url: null, kubecontext: null },
  ingress: { enabled: false, allow_unauthenticated: false, tls: "none" },
};

/**
 * `GET /clusters/{id}/sandbox` 의 픽스처 (P40 7절·8절). home 은 권한이 있고 점검하지 않아 `unchecked` 로 닫혔으며, 노드 하나에
 * 작업 공간 셋이 들어가고 메모리 한도 없는 파드 둘을 요청으로 셌다. 그 밖은 권한이 없어 `rights` 다.
 */
export function clusterSandboxFixture(over: Partial<ClusterSandboxStatus> = {}): ClusterSandboxStatus {
  return {
    gate: { open: false, reason: "unchecked" },
    capacity: 3,
    nodes: [{ name: "n1", allocatable: 16 * 1024 ** 3, others: 4 * 1024 ** 3, cap: 3, used: 0 }],
    assumed: 2,
    assumed_pods: ["daegam-default/a", "daegam-default/b"],
    shared_nodes: null,
    void_reason: null,
    override: false,
    check_running: false,
    open_sandboxes: 0,
    run: { started_at: null, finished_at: null, by: null, error: null },
    ...over,
  };
}

export const clusterHandlers: HttpHandler[] = [
  http.get("/clusters/:id/sandbox", async ({ request, params }) => {
    await record(request);
    if (params.id === "cl-home") return HttpResponse.json(clusterSandboxFixture());
    return HttpResponse.json(clusterSandboxFixture({ gate: { open: false, reason: "rights" }, capacity: 0, nodes: [], assumed: 0, assumed_pods: [] }));
  }),
  http.post("/clusters/:id/sandbox/check", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ started: true }, { status: 202 });
  }),
  http.post("/clusters/:id/sandbox/accept", async ({ request }) => {
    await record(request);
    return HttpResponse.json(clusterHome);
  }),
  http.get("/clusters", async ({ request }) => {
    await record(request);
    return HttpResponse.json([clusterHome, clusterEdge, clusterLocked]);
  }),
  http.post("/clusters", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { name: string; display_name?: string };
    return HttpResponse.json({ ...clusterEdge, id: "cl-new", name: body.name, display_name: body.display_name ?? body.name }, { status: 201 });
  }),
  http.get("/clusters/:id", async ({ request, params }) => {
    await record(request);
    if (params.id === "cl-home") return HttpResponse.json(clusterHome);
    if (params.id === "cl-edge") return HttpResponse.json(clusterEdge);
    if (params.id === "cl-locked") return HttpResponse.json(clusterLocked);
    if (params.id === "cl-new") return HttpResponse.json({ ...clusterEdge, id: "cl-new", name: "new", display_name: "new" });
    return apiError(404, "not_found", `cluster ${String(params.id)} not found`);
  }),
  // 노출 옵션: home 은 정상, edge 는 secrets list 권한 없음(403), locked 는 노출 비활성
  http.get("/clusters/:id/exposure-options", async ({ request, params }) => {
    const r = await record(request);
    const ns = new URL(r.path, "http://localhost").searchParams.get("namespace");
    if (params.id === "cl-home") {
      if (ns && !exposureOptionsFixture().namespaces.includes(ns)) {
        return apiError(422, "namespace_not_allowed", `namespace ${ns} is not allowed for tenant default`);
      }
      // 두 번째 네임스페이스에는 TLS Secret 이 없다
      return HttpResponse.json(exposureOptionsFixture(ns === "daegam-default-edge" ? { namespace: ns, tls_secrets: [] } : {}));
    }
    if (params.id === "cl-edge") {
      return apiError(403, "forbidden", "the cluster client may not secrets:list@daegam-default; apply deploy rbac.yaml and retry", {
        missing: ["secrets:list@daegam-default"],
      });
    }
    if (params.id === "cl-locked") {
      return HttpResponse.json(exposureOptionsFixture({ enabled: false, allow_unauthenticated: false, defaults: { host: null, ingress_class: null, tls_mode: "none", issuer: null } }));
    }
    return apiError(404, "not_found", `cluster ${String(params.id)} not found`);
  }),
  http.patch("/clusters/:id", async ({ request }) => {
    const r = await record(request);
    return HttpResponse.json({ ...clusterEdge, ...(r.body as object) });
  }),
  http.delete("/clusters/:id", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ id: params.id, deleted: true, secret_name: "x" });
  }),
  http.post("/clusters/:id/preflight", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({
      id: params.id,
      name: "edge",
      preflight: { ...clusterHome.status.preflight, k8s_version: "v1.34.0" },
    });
  }),
];
