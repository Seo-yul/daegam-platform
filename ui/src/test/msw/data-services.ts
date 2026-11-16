/** Database(Data Service) 픽스처와 핸들러 (/data-services). */
import { HttpResponse, type HttpHandler } from "msw";

import type { DataService } from "@/api/types";

import { NOW, apiError, http, record } from "./core";

export const dsPostgres: DataService = {
  id: "ds-pg",
  tenant: "default",
  name: "orders-db",
  display_name: "Orders DB",
  cluster_id: "cl-home",
  kind: "postgres",
  host: "pg.internal",
  port: 5432,
  database: "orders",
  db: null,
  username: "app",
  secret_name: "ds-ds-pg",
  has_password: true,
  tls: null,
  shipped_tls: null,
  plain_closed: false,
  capabilities: {},
  verified_at: null,
  created_at: NOW,
  updated_at: NOW,
  updated_by: null,
};

export const dsRedis: DataService = {
  id: "ds-redis",
  tenant: "default",
  name: "cache",
  display_name: "cache",
  cluster_id: "cl-home",
  kind: "redis",
  host: "redis.internal",
  port: 6379,
  database: null,
  // 0 이어야 체크포인터로 쓸 수 있다. RediSearch 가 db 0 에서만 인덱스를 만든다 (D33·P17 F5)
  db: 0,
  username: null,
  secret_name: "ds-ds-redis",
  has_password: false,
  tls: null,
  shipped_tls: null,
  plain_closed: false,
  capabilities: { checkpointer: "ready" },
  verified_at: NOW,
  created_at: NOW,
  updated_at: NOW,
  updated_by: null,
};

export const dataServiceHandlers: HttpHandler[] = [
  http.get("/data-services", async ({ request }) => {
    await record(request);
    return HttpResponse.json([dsPostgres, dsRedis]);
  }),
  http.post("/data-services", async ({ request }) => {
    const r = await record(request);
    const body = r.body as Record<string, unknown>;
    return HttpResponse.json(
      { ...dsPostgres, ...body, display_name: body.display_name ?? body.name, id: "ds-new", secret_name: "ds-ds-new", has_password: Boolean(body.password), password: undefined },
      { status: 201 },
    );
  }),
  http.get("/data-services/:id", async ({ request, params }) => {
    await record(request);
    if (params.id === "ds-pg") return HttpResponse.json(dsPostgres);
    if (params.id === "ds-redis") return HttpResponse.json(dsRedis);
    if (params.id === "ds-new") return HttpResponse.json({ ...dsPostgres, id: "ds-new", name: "new-db", display_name: "new-db" });
    return apiError(404, "not_found", `data service ${String(params.id)} not found`);
  }),
  http.patch("/data-services/:id", async ({ request }) => {
    const r = await record(request);
    const body = r.body as Record<string, unknown>;
    return HttpResponse.json({ ...dsPostgres, ...body, password: undefined, verified_at: null });
  }),
  http.delete("/data-services/:id", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ id: params.id, deleted: true, secret_name: "x" });
  }),
  http.get("/data-services/:id/plain-consumers", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ consumers: [] });
  }),
  http.post("/data-services/:id/verify", async ({ request, params }) => {
    await record(request);
    // redis 는 PING 뒤 INFO·MODULE LIST 결과를 `redis` 에 싣는다 (D33)
    if (params.id === "ds-redis") {
      return HttpResponse.json({ id: params.id, ok: true, verified_at: NOW, detail: "redis.internal:6379/0: connected", redis: { version: "8.2.1", modules: ["search", "ReJSON"], checkpointer: "ready" } });
    }
    return HttpResponse.json({ id: params.id, ok: true, verified_at: NOW, detail: "PostgreSQL 16.2" });
  }),
];
