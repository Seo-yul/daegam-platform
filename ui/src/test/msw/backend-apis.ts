/**
 * Backend API 픽스처와 핸들러 (/backend-apis, verify·convert, versions, tokens, minify·apply-draft).
 * bp-pets 의 버전 목록은 apply-draft 가 늘리므로 상태(backendApiState)다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { ApplyDraftBody, BackendAPI, BackendAPIToken, BackendAPIVersion, SpecChange, VerifyResponse } from "@/api/types";

import { specPetstore } from "../petstore";
import { NOW, apiError, http, record } from "./core";
import { refineChanges } from "./spec-generator";

// display_name (D24): 표시 이름이 name 과 다른 픽스처(orders, orders-db)는 목록의 보조 이름 표시를 검사하는 데 쓴다.
export const profileOrders: BackendAPI = {
  id: "bp-orders",
  tenant: "default",
  name: "orders",
  display_name: "Orders SOAP",
  endpoint: "https://orders.example.com",
  auth: "bearer",
  credential_secret: "orders-cred",
  soap_headers_secret: null,
  created_at: NOW,
  updated_at: NOW,
  latest_version: 2,
  kind: "soap",
  source_format: "wsdl",
  tool_count: 3,
  tool_names: ["CreateOrder", "GetOrder", "CancelOrder"],
};

export const profileEmpty: BackendAPI = {
  id: "bp-empty",
  tenant: "default",
  name: "empty",
  display_name: "empty",
  endpoint: null,
  auth: "none",
  credential_secret: null,
  soap_headers_secret: null,
  created_at: NOW,
  updated_at: NOW,
  latest_version: null,
};

/** REST(Swagger 2.0 → 3.0) 프로필. 리포트에 warn·fail 이 없어 Verify report 가 기본으로 접힌다. */
export const profilePets: BackendAPI = {
  id: "bp-pets",
  tenant: "default",
  name: "pets",
  display_name: "pets",
  endpoint: "https://petstore.example.com/v1",
  auth: "none",
  credential_secret: null,
  soap_headers_secret: null,
  created_at: NOW,
  updated_at: NOW,
  latest_version: 1,
  kind: "rest",
  source_format: "swagger2",
  tool_count: 4,
  tool_names: ["listPets", "createPet", "getPet", "deletePet"],
};

export const petsVersion: BackendAPIVersion = {
  backend_profile_id: "bp-pets",
  version: 1,
  kind: "rest",
  source_format: "swagger2",
  source_ext: "json",
  source_sha256: "sha-src-pets",
  output_sha256: "sha-out-pets",
  s2o_version: "0.6.0",
  tool_count: 4,
  tool_names: ["listPets", "createPet", "getPet", "deletePet"],
  tool_details: [
    { operation_id: "listPets", summary: "List all pets", description: null },
    { operation_id: "createPet", summary: "Create a pet", description: null },
    { operation_id: "getPet", summary: "Info for a specific pet", description: null },
    { operation_id: "deletePet", summary: "Delete a pet", description: null },
  ],
  created_at: NOW,
  report: {
    ok: true,
    complete: true,
    summary: { pass: 4, warn: 0, fail: 0, skip: 0 },
    results: [
      { id: "document.openapi3", status: "pass", message: "openapi 3.0.3" },
      { id: "tool-name.present", status: "pass", message: null },
      { id: "tool-description.present", status: "pass", message: null },
      { id: "x-soap.mixed-rest", status: "pass", message: null },
    ],
  },
};

/** bp-pets 의 버전 목록. apply-draft 가 v2 를 붙인다. */
export const backendApiState: { petsVersions: BackendAPIVersion[] } = { petsVersions: [petsVersion] };

export function resetBackendApiState(): void {
  backendApiState.petsVersions = [petsVersion];
}

/**
 * 규칙 정리(minify) 픽스처. Petstore 축약본 기준으로 kind 셋을 다 담았다: 설명 접기(added), 설명 정리(changed),
 * 스키마의 외부 x-* 와 값 예시 삭제(removed), 루트 x-s2o.minify 기록(added, 부모 없음).
 */
export const minifyChanges: SpecChange[] = [
  { id: "m1", pointer: "/paths/~1pets/get/description", kind: "added", before: null, after: "Errors: default: unexpected error." },
  {
    id: "m2",
    pointer: "/paths/~1pets~1{petId}/get/description",
    kind: "changed",
    before: "Returns the pet.\n\nThe owner is included when known.",
    after: "Returns the pet. The owner is included when known.",
  },
  { id: "m3", pointer: "/components/schemas/Error/x-internal-id", kind: "removed", before: "err-1", after: null },
  { id: "m4", pointer: "/components/schemas/Error/properties/code/example", kind: "removed", before: 500, after: null },
  { id: "m5", pointer: "/x-s2o/minify", kind: "added", before: null, after: { enrich: ["errors"], max_description: null, drop_value_examples: false } },
];

export const minifyDraftYaml = "openapi: 3.0.3\ninfo:\n  title: Petstore\n# (minify draft; 화면은 클라이언트가 pointer 를 심어 다시 만든다)\n";

export const versions: BackendAPIVersion[] = [1, 2].map((n) => ({
  backend_profile_id: "bp-orders",
  version: n,
  kind: "soap",
  source_format: "wsdl",
  source_ext: "wsdl",
  source_sha256: `sha-src-${n}`,
  output_sha256: `sha-out-${n}`,
  s2o_version: "0.6.0",
  tool_count: 3,
  tool_names: ["CreateOrder", "GetOrder", "CancelOrder"],
  tool_details: [
    { operation_id: "CreateOrder", summary: "Creates a new order", description: null },
    { operation_id: "GetOrder", summary: "Fetches one order by id", description: "Returns the full order document" },
    { operation_id: "CancelOrder", summary: null, description: "Cancels an order" },
  ],
  created_at: NOW,
  report: { ok: true, summary: { pass: 20, warn: 1 } },
}));

export const versionDetail: BackendAPIVersion = {
  ...versions[1],
  report: {
    ok: true,
    complete: true,
    // 실제 리포트처럼 CHECK_LEVELS 의 21개 체크가 모두 있다. 테스트가 id 집합을 사전과 대조한다.
    summary: { pass: 18, warn: 1, fail: 0, skip: 2 },
    results: [
      { id: "document.mapping", status: "pass", message: null },
      { id: "document.openapi3", status: "pass", message: "openapi 3.0.3" },
      { id: "document.has-paths", status: "pass", message: "3 paths" },
      { id: "document.has-operations", status: "pass", message: "3 operations" },
      { id: "tool-name.present", status: "pass", message: null },
      { id: "tool-name.safe", status: "pass", message: null },
      { id: "tool-name.normalized", status: "pass", message: null },
      { id: "tool-name.unique", status: "pass", message: null },
      { id: "tool-name.normalization-collision", status: "pass", message: null },
      {
        id: "tool-description.present",
        status: "warn",
        message: "tool GetOrder has no description",
        location: "paths./GetOrder.post",
        refs: [{ id: "mcp-tools", url: "https://modelcontextprotocol.io/specification/latest/server/tools" }],
      },
      { id: "x-soap.version", status: "pass", message: "1.1" },
      { id: "x-soap.endpoint", status: "pass", message: "https://orders.example.com/soap" },
      { id: "x-soap.input-element", status: "pass", message: null },
      { id: "x-soap.output-element", status: "pass", message: null },
      { id: "x-soap.refs", status: "pass", message: null },
      { id: "x-soap.choice", status: "pass", message: null },
      { id: "x-soap.substitution", status: "pass", message: null },
      { id: "x-soap.mixed-rest", status: "pass", message: null },
      { id: "openapi.schema-valid", status: "pass", message: null },
      { id: "fastmcp.roundtrip", status: "skip", message: "fastmcp not installed" },
      { id: "fastmcp.tool-materialized", status: "skip", message: "fastmcp not installed" },
    ],
  },
};

export const verifyOk: VerifyResponse = {
  source_format: "wsdl",
  kind: "soap",
  mixed: false,
  report: { ok: true, complete: true, results: [] },
  tool_names: ["Add", "Divide"],
  tool_details: [
    { operation_id: "Add", summary: "Adds two integers", description: null },
    { operation_id: "Divide", summary: null, description: "Divides two integers" },
  ],
  verdict: {
    level: "warn",
    items: [
      { level: "warn", code: "tool-description.present", message: "tool Add has no description", count: 2, args: {} },
      { level: "info", code: "converted-from-wsdl", message: "converted from WSDL", count: 1, args: {} },
    ],
  },
};

export const verifyBroken: VerifyResponse = {
  source_format: "unsupported",
  kind: null,
  mixed: false,
  report: null,
  tool_names: [],
  tool_details: [],
  verdict: {
    level: "error",
    items: [{ level: "error", code: "unsupported-format", message: "could not recognise the document", count: 1, args: {} }],
  },
};

/** 발급·회수가 반영되는 토큰 목록이다. */
export const backendTokenState: { items: BackendAPIToken[] } = { items: [initialToken()] };

function initialToken(): BackendAPIToken {
  return {
    token_id: "tok-1",
    backend_profile_id: "bp-orders",
    label: "ci",
    scope: [],
    created_at: NOW,
    revoked_at: null,
    active: true,
  };
}

export function resetBackendTokenState(): void {
  backendTokenState.items = [initialToken()];
}

export const backendApiHandlers: HttpHandler[] = [
  http.get("/backend-apis", async ({ request }) => {
    await record(request);
    return HttpResponse.json([profileOrders, profileEmpty, profilePets]);
  }),
  http.post("/backend-apis/verify", async ({ request }) => {
    const r = await record(request);
    const text =
      typeof r.body === "object" && r.body && "text" in r.body
        ? String((r.body as { text: string }).text)
        : String((r.body as { file?: { text: string } })?.file?.text ?? "");
    return HttpResponse.json(text.includes("broken") ? verifyBroken : verifyOk);
  }),
  http.post("/backend-apis/convert", async ({ request }) => {
    const r = await record(request);
    const text =
      typeof r.body === "object" && r.body && "text" in r.body
        ? String((r.body as { text: string }).text)
        : String((r.body as { file?: { text: string } })?.file?.text ?? "");
    if (text.includes("broken")) {
      return apiError(422, "verdict_error", "spec did not pass verification", { verdict: verifyBroken.verdict });
    }
    return HttpResponse.json(
      { yaml: "openapi: 3.0.3\ninfo:\n  title: calculator\n", report: verifyOk.report, verdict: verifyOk.verdict },
      { headers: { "X-Daegam-Verdict": "warn" } },
    );
  }),
  http.post("/backend-apis", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { name?: string; display_name?: string; file?: { name: string } };
    return HttpResponse.json(
      {
        id: "bp-new",
        name: body.name,
        display_name: body.display_name ?? body.name,
        endpoint: null,
        auth: "none",
        credential_secret: null,
        version: 1,
        kind: "soap",
        source_format: "wsdl",
        tool_count: 2,
        tool_names: ["Add", "Divide"],
        report: { ok: true },
      },
      { status: 201 },
    );
  }),
  http.get("/backend-apis/:id", async ({ request, params }) => {
    await record(request);
    if (params.id === "bp-orders") return HttpResponse.json(profileOrders);
    if (params.id === "bp-empty") return HttpResponse.json(profileEmpty);
    if (params.id === "bp-pets") {
      const latest = backendApiState.petsVersions[backendApiState.petsVersions.length - 1];
      return HttpResponse.json({ ...profilePets, latest_version: latest.version });
    }
    if (params.id === "bp-new") return HttpResponse.json({ ...profileEmpty, id: "bp-new", name: "calc", display_name: "calc" });
    return apiError(404, "not_found", `backend api ${String(params.id)} not found`);
  }),
  http.patch("/backend-apis/:id", async ({ request }) => {
    const r = await record(request);
    return HttpResponse.json({ ...profileOrders, ...(r.body as object) });
  }),
  http.delete("/backend-apis/:id", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ id: params.id, deleted: true, revoked_token_ids: [] });
  }),
  http.get("/backend-apis/:id/versions", async ({ request, params }) => {
    await record(request);
    if (params.id === "bp-pets") return HttpResponse.json(backendApiState.petsVersions);
    return HttpResponse.json(params.id === "bp-orders" ? versions : []);
  }),
  // 규칙 정리 (D25). 저장 없이 changes 와 초안 YAML 을 즉시 준다. 옵션은 요청 기록으로 검사한다.
  http.post("/backend-apis/:id/versions/:n/minify", async ({ request, params }) => {
    await record(request);
    if (params.id === "bp-empty") return apiError(404, "not_found", `version ${String(params.n)} not found`);
    return HttpResponse.json({ changes: minifyChanges, draft_yaml: minifyDraftYaml });
  }),
  // 초안 적용 (D25). 모르는 accepted_ids 는 422 필드 오류, 아니면 v(n+1) 을 만든다 (bp-pets 는 목록에도 붙는다).
  http.post("/backend-apis/:id/versions/:n/apply-draft", async ({ request, params }) => {
    const r = await record(request);
    const body = r.body as ApplyDraftBody;
    const known = new Set([...minifyChanges, ...refineChanges].map((c) => c.id));
    const unknown = body.accepted_ids.filter((id) => !known.has(id));
    if (unknown.length) return apiError(422, "unknown_change_id", `unknown change ids: ${unknown.join(", ")}`, { accepted_ids: unknown });
    const from = Number(params.n);
    const version = from + 1;
    const origin = { kind: "job_id" in body ? "refine" : "minify", from_version: from, job_id: "job_id" in body ? body.job_id : null, presets: [] };
    if (params.id === "bp-pets") {
      backendApiState.petsVersions = [...backendApiState.petsVersions, { ...petsVersion, version, output_sha256: `sha-out-pets-${version}`, origin } as BackendAPIVersion];
    }
    const profile = params.id === "bp-pets" ? profilePets : profileOrders;
    return HttpResponse.json(
      {
        id: params.id,
        name: profile.name,
        display_name: profile.display_name ?? profile.name,
        endpoint: profile.endpoint,
        auth: profile.auth,
        credential_secret: profile.credential_secret,
        version,
        kind: profile.kind,
        source_format: profile.source_format,
        tool_count: profile.tool_count,
        tool_names: profile.tool_names,
        report: { ok: true },
        origin,
      },
      { status: 201 },
    );
  }),
  http.get("/backend-apis/:id/versions/:n", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json(params.id === "bp-pets" ? petsVersion : versionDetail);
  }),
  // 변환된 OpenAPI 문서(JSON). 어느 프로필이든 Petstore 축약본을 준다 (bp-orders 도 뷰어 검사용)
  http.get("/backend-apis/:id/versions/:n/spec", async ({ request, params }) => {
    await record(request);
    if (params.id === "bp-empty") return apiError(404, "not_found", `version ${String(params.n)} not found`);
    return HttpResponse.json(specPetstore);
  }),
  // 올린 그대로의 원본. 편집 화면의 "등록된 문서" 가 읽는다. bp-orders 는 WSDL 로 올린 것이다
  http.get("/backend-apis/:id/versions/:n/source", async ({ request, params }) => {
    await record(request);
    if (params.id === "bp-empty") return apiError(404, "not_found", `version ${String(params.n)} not found`);
    // 한 줄로 밀어 넣은 WSDL 이다. "보기 좋게" 가 이런 문서를 읽을 수 있게 한다
    return HttpResponse.json({ source_ext: "wsdl", source_format: "wsdl", text: '<definitions name="Calculator"><portType name="Calc"><operation name="Add"/></portType></definitions>' });
  }),
  http.get("/backend-apis/:id/tokens", async ({ request }) => {
    await record(request);
    return HttpResponse.json(backendTokenState.items);
  }),
  http.post("/backend-apis/:id/tokens", async ({ request, params }) => {
    const r = await record(request);
    const label = (r.body as { label?: string | null } | null)?.label ?? null;
    return HttpResponse.json(
      { token_id: "tok-2", token: "secret-token-value", backend_profile_id: params.id, label, scope: [] },
      { status: 201 },
    );
  }),
  http.delete("/backend-apis/:id/tokens/:tokenId", async ({ request, params }) => {
    await record(request);
    // 회수를 실제로 반영한다. 고정 응답이면 "요청은 갔다" 밖에 못 보고, 목록을 다시 읽지 않는
    // 회귀를 놓친다
    backendTokenState.items = backendTokenState.items.map((t) =>
      t.token_id === params.tokenId ? { ...t, revoked_at: NOW, active: false } : t,
    );
    return HttpResponse.json({ token_id: params.tokenId, revoked: true });
  }),
];
