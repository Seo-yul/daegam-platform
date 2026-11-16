/** 감사 로그 (P7 단위 7): /audit, /audit/mcp. */
import { HttpResponse, type HttpHandler } from "msw";

import type { AuditEvent, MCPAuditEvent } from "@/api/types";

import { NOW, http, record } from "./core";

export const auditEvents: AuditEvent[] = [
  {
    id: "ae-1",
    ts: NOW,
    sub: "dev@example.com",
    subject_type: "user",
    login_username: null,
    client_id: null,
    tenant: "default",
    action: "mcp_instance.create",
    resource: "mcp_instance",
    resource_id: "mi-calc-1",
    path_params: {},
    method: "POST",
    path: "/mcp-instances",
    request_id: "req-abc123",
    status: 201,
    result: "ok",
    error_code: null,
    duration_ms: 412,
    via: null,
    count: null,
  },
  {
    id: "ae-2",
    ts: NOW,
    sub: "dev@example.com",
    subject_type: "user",
    login_username: null,
    client_id: null,
    tenant: "default",
    action: "backend_profile.delete",
    resource: "backend_profile",
    resource_id: "bp-orders",
    path_params: {},
    method: "DELETE",
    path: "/backend-profiles/bp-orders",
    request_id: "req-def456",
    status: 409,
    result: "error",
    error_code: "in_use",
    duration_ms: 12,
    via: null,
    count: null,
  },
];

export const mcpAuditEvents: MCPAuditEvent[] = [
  {
    audit_id: "ma-1",
    ts: NOW,
    type: "mcp_tool_call",
    outcome: "success",
    component: "mcp-calc-1",
    sub: "agent-1",
    user: null,
    client_name: "daegam-agent",
    method: "tools/call",
    tool: "CreateOrder",
    backend: null,
    target: null,
    subjects: null,
    act: null,
    delegation: null,
    source: null,
    metadata: null,
    tenant: "default",
    instance_id: "mi-calc-1",
    cluster_id: "cl-home",
    namespace: "daegam-default",
    pod: "mcp-calc-1-0",
    proxy_kind: "mcpserver",
    collected_at: NOW,
  },
  {
    audit_id: "ma-2",
    ts: NOW,
    type: "mcp_tool_call",
    outcome: "success",
    component: "gw-support",
    sub: "u-alice",
    user: null,
    client_name: "daegam-agent",
    method: "tools/call",
    tool: "CreateOrder",
    backend: "gw-support-calc",
    target: null,
    subjects: null,
    act: "svc:agent:agent-support",
    delegation: null,
    source: null,
    metadata: null,
    tenant: "default",
    instance_id: "mi-calc-1",
    cluster_id: "cl-home",
    namespace: "daegam-default",
    pod: "vmcp-gw-support-0",
    proxy_kind: "virtualmcpserver",
    collected_at: NOW,
  },
];

export const auditHandlers: HttpHandler[] = [
  http.get("/audit", async ({ request }) => {
    const r = await record(request);
    const url = new URL(r.path, "http://localhost");
    const action = url.searchParams.get("action");
    return HttpResponse.json(action ? auditEvents.filter((e) => e.action === action) : auditEvents);
  }),
  http.get("/audit/mcp", async ({ request }) => {
    await record(request);
    return HttpResponse.json(mcpAuditEvents);
  }),
];
