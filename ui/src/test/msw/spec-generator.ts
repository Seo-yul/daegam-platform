/**
 * Spec Generator job 픽스처와 핸들러 (/spec-generator/jobs). refine 모드(D25)와 overrides 모드(D37) job 을 흉내 낸다.
 * job 은 테스트가 그 상태의 `finish()` 를 부를 때까지 running 이고(refine 은 반복 1, 실패 항목 있음), 그 뒤로는 done 이다
 * (refine 은 반복 2, changes·renamed).
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { RefineJob, SpecChange } from "@/api/types";

import { NOW, apiError, http, record } from "./core";

/** Petstore 축약본(petstore.ts) 기준. NewPet 은 createPet 이 직접, listPets·getPet 이 Pet(allOf) 을 거쳐 참조한다. */
export const refineChanges: SpecChange[] = [
  { id: "r1", pointer: "/paths/~1pets/get/summary", kind: "changed", before: "List all pets", after: "List every pet in the store" },
  { id: "r2", pointer: "/paths/~1pets/get/operationId", kind: "changed", before: "listPets", after: "list_pets" },
  { id: "r3", pointer: "/paths/~1pets/post/description", kind: "added", before: null, after: "Creates a pet and returns it." },
  { id: "r4", pointer: "/components/schemas/NewPet/properties/name/description", kind: "changed", before: "Pet name", after: "The pet's display name" },
];

export const refineDraftYaml = "openapi: 3.0.3\ninfo:\n  title: Petstore\n# (refine draft; 화면은 클라이언트가 pointer 를 심어 다시 만든다)\n";

export const refineJobState = {
  finished: false,
  presets: [] as string[],
  /**
   * running 을 끝낸다. 테스트가 running 화면을 본 뒤에 부른다. 폴링 횟수로 끊으면 머신이 바쁠 때
   * running 구간이 화면에 잡히기 전에 지나가 간헐적으로 깨진다(실측).
   */
  finish() {
    this.finished = true;
  },
};

/**
 * 오버라이드 제안 job 의 채택 단위 (D37). 두 모양을 다 담는다. 이미 오버라이드가 있던 GetOrder 는
 * leaf 하나이고, 없던 CreateOrder 는 항목 통째가 한 단위다.
 */
export const overrideChanges: SpecChange[] = [
  { id: "o1", pointer: "/backends/0/tool_overrides/GetOrder/name", kind: "changed", before: "get_order", after: "getOrder" },
  { id: "o2", pointer: "/backends/0/tool_overrides/CreateOrder", kind: "added", before: null, after: { name: "createOrder", description: "주문을 만든다." } },
];

export const overridesJobState = {
  finished: false,
  presets: [] as string[],
  finish() {
    this.finished = true;
  },
};

export function resetRefineJobState(): void {
  refineJobState.finished = false;
  refineJobState.presets = [];
}

export function resetOverridesJobState(): void {
  overridesJobState.finished = false;
  overridesJobState.presets = [];
}

const JOB_ID = "job-refine-1";
const OVERRIDES_JOB_ID = "job-overrides-1";

function baseJob(state: RefineJob["state"]): RefineJob {
  return {
    id: JOB_ID,
    tenant: "default",
    state,
    provider: "openai-main",
    hints: {},
    attempt: 1,
    iterations: [],
    usage_total: {},
    error: null,
    report: null,
    verdict: null,
    created_at: NOW,
    updated_at: NOW,
    finished_at: null,
    expires_at: null,
    mode: "refine",
    source: { backend_profile_id: "bp-orders", version: 2 },
    presets: refineJobState.presets,
    instruction: null,
  };
}

/** overrides job (D37). report 는 없고 verdict 는 findings 유무만 담은 빈 판정이다. */
function overridesJob(state: RefineJob["state"]): RefineJob {
  return {
    ...baseJob(state),
    id: OVERRIDES_JOB_ID,
    mode: "overrides",
    source: { mcp_server_id: "ms-calc", revision: 2 },
    presets: overridesJobState.presets,
  };
}

export const specGeneratorHandlers: HttpHandler[] = [
  http.post("/spec-generator/jobs", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { mode?: string; provider?: string; presets?: string[] };
    if (body.provider === "no-key") {
      return apiError(422, "provider_no_key", "llm provider 'no-key' has no api key in secret 'daegam-llm-openai'");
    }
    if (body.mode === "overrides") {
      overridesJobState.finished = false;
      overridesJobState.presets = body.presets ?? [];
      return HttpResponse.json(overridesJob("queued"), { status: 202 });
    }
    if (body.mode !== "refine") return apiError(422, "empty_input", "input needs text, url, examples or partial_spec");
    refineJobState.finished = false;
    refineJobState.presets = body.presets ?? [];
    return HttpResponse.json(baseJob("queued"), { status: 202 });
  }),
  http.get("/spec-generator/jobs/:id", async ({ request, params }) => {
    await record(request);
    if (params.id === OVERRIDES_JOB_ID) {
      const it = { n: 1, report_summary: null, failed_ids: [], usage: { input_tokens: 900, output_tokens: 300 }, note: null, finished_at: NOW };
      if (!overridesJobState.finished) return HttpResponse.json({ ...overridesJob("running"), iterations: [] });
      return HttpResponse.json({
        ...overridesJob("done"),
        iterations: [it],
        usage_total: { input_tokens: 900, output_tokens: 300 },
        verdict: { level: "ok", items: [] },
        finished_at: NOW,
        changes: overrideChanges,
      });
    }
    if (params.id !== JOB_ID) return apiError(404, "not_found", `spec job ${String(params.id)} not found`);
    const first = {
      n: 1,
      report_summary: { pass: 19, warn: 1, fail: 1, skip: 0 },
      failed_ids: ["tool-description.present", "surface.changed:/paths/~1pets/get/tags"],
      usage: { input_tokens: 1200, output_tokens: 400 },
      note: null,
      finished_at: NOW,
    };
    if (!refineJobState.finished) {
      return HttpResponse.json({ ...baseJob("running"), iterations: [first], usage_total: { input_tokens: 1200, output_tokens: 400 } });
    }
    const renaming = refineJobState.presets.includes("normalize_operation_ids");
    const changes = renaming ? refineChanges : refineChanges.filter((c) => c.id !== "r2");
    return HttpResponse.json({
      ...baseJob("done"),
      iterations: [first, { n: 2, report_summary: { pass: 21, warn: 0, fail: 0, skip: 0 }, failed_ids: [], usage: { input_tokens: 1300, output_tokens: 500 }, note: null, finished_at: NOW }],
      usage_total: { input_tokens: 2500, output_tokens: 900 },
      report: { ok: true, complete: true },
      verdict: { level: "ok", items: [] },
      finished_at: NOW,
      changes,
      draft_yaml: refineDraftYaml,
      renamed: renaming ? [{ from: "listPets", to: "list_pets" }] : [],
    });
  }),
];
