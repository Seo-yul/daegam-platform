/**
 * `GET /{리소스}/{id}/references` (D43). 아홉 경로가 같은 모양을 답하므로 핸들러도 한 표에서 만든다.
 *
 * 기본은 빈 목록이다. 참조가 있는 화면을 보는 테스트만 `referencesState` 에 넣는다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { Reference } from "@/api/types";

import { http, record } from "./core";

/** `"<경로 조각>/<id>"` → 그 리소스를 쓰는 것. 예: `"mcp-servers/ms-calc"`. */
export const referencesState: Record<string, Reference[]> = {};

export function resetReferencesState(): void {
  for (const key of Object.keys(referencesState)) delete referencesState[key];
}

/** 항목 하나. 안 준 칸은 이 기본값이다. */
export function reference(kind: Reference["kind"], id: string, over: Partial<Reference> = {}): Reference {
  return {
    kind,
    id,
    name: id,
    display_name: id,
    state: null,
    revision: "current",
    via: null,
    pins: null,
    ...over,
  };
}

const SEGMENTS = ["backend-apis", "mcp-servers", "gateways", "agents", "data-services", "knowledge-bases", "hub", "clusters", "skills"] as const;

export const referenceHandlers: HttpHandler[] = SEGMENTS.map((segment) =>
  http.get(`/${segment}/:id/references`, async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ used_by: referencesState[`${segment}/${String(params.id)}`] ?? [] });
  }),
);
