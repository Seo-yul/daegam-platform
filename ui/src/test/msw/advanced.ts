/** Advanced 편집 (P7 단위 4): MCP Server 의 overrides / drift. mcp-servers 핸들러보다 앞에 등록된다. */
import { HttpResponse, type HttpHandler } from "msw";

import type { Manifest, ManifestDocument } from "@/api/types";

import { apiError, http, record } from "./core";
import { manifestDoc, previewCR } from "./mcp-servers";

/** manifest 응답도 mutable 로 두어 drift reset/absorb 뒤 바뀐 문서를 돌려줄 수 있게 한다. */
export const manifestState: { doc: ManifestDocument } = { doc: manifestDoc };

export function resetManifestState(): void {
  manifestState.doc = manifestDoc;
}

export const advancedHandlers: HttpHandler[] = [
  http.get("/mcp-servers/:id/manifest", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ documents: [manifestState.doc] });
  }),
  http.put("/mcp-servers/:id/overrides", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { yaml?: string; patch?: unknown; manifest?: unknown };
    const text = body.yaml ?? "";
    if (text.includes("image: hacked")) {
      return apiError(422, "locked_field", "spec.image is managed by the platform", { path: "spec.image" });
    }
    if (text.includes("args:")) {
      return apiError(422, "ignored_by_operator", "operator overwrites this field", {
        path: "spec.podTemplateSpec.spec.containers[name=mcp].args",
      });
    }
    if (text.includes("replicas: 99")) {
      return apiError(422, "invalid_manifest", "spec.replicas: must be <= 10");
    }
    const m = /replicas: (\d+)/.exec(text);
    const replicas = m ? Number(m[1]) : 2;
    const final = { ...previewCR, spec: { ...(previewCR.spec as object), replicas } };
    manifestState.doc = { ...manifestState.doc, overrides: { spec: { replicas } }, final, drift: [], drift_managers: [] };
    return HttpResponse.json({
      overrides: { spec: { replicas } },
      final,
      diff: ["spec.replicas"],
      validation: "schema_only",
      note: "CRD schema and CEL rules only; operator validation shows up in status.conditions after apply",
    });
  }),
  http.post("/mcp-servers/:id/drift/:action", async ({ request, params }) => {
    await record(request);
    if (params.action === "reset") {
      manifestState.doc = { ...manifestState.doc, live: manifestState.doc.final, drift: [], drift_managers: [] };
    } else {
      const live = manifestState.doc.live as Manifest;
      manifestState.doc = { ...manifestState.doc, final: live, overrides: { spec: { replicas: 3 } }, drift: [], drift_managers: [] };
    }
    return HttpResponse.json({ documents: [manifestState.doc] });
  }),
];
