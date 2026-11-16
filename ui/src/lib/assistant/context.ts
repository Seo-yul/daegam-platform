/**
 * 턴의 `context{route, resource?, unsaved_edits?}` (D31, P48 6.5절). 현재 경로에서 `/mcp-servers/:id` 꼴을 kind·id 로 풀고
 * 이름은 react-query 캐시에서 찾는다. 경로 조각 표의 정본은 `lib/deletion.ts` 의 PATH_SEGMENT 다.
 */
import type { QueryClient } from "@tanstack/react-query";

import type { AssistantContext } from "@/api/types";
import { PATH_SEGMENT, type DeletableResource } from "@/lib/deletion";

/** 목록 경로의 첫 조각 → 리소스 kind. 정본은 deletion.ts 의 `PATH_SEGMENT` 이고 이것은 그것을 뒤집은 것이다. */
const PATH_KINDS = Object.fromEntries(Object.entries(PATH_SEGMENT).map(([k, p]) => [p, k])) as Record<string, DeletableResource>;

/** `/mcp-servers/:id`, `/gateways/:id?tab=deployment` 꼴에서 kind·id 를 푼다. `new` 와 목록 경로는 리소스가 아니다. */
export function resourceFromPath(pathname: string): { kind: DeletableResource; id: string } | null {
  const [seg, id] = pathname.split("/").filter(Boolean);
  if (!seg || !id || id === "new") return null;
  const kind = PATH_KINDS[seg];
  if (!kind) return null;
  try {
    return { kind, id: decodeURIComponent(id) };
  } catch {
    return { kind, id };
  }
}

function nameOf(o: unknown, id: string): string | null {
  if (!o || typeof o !== "object") return null;
  const r = o as { id?: unknown; display_name?: unknown; name?: unknown };
  if (r.id !== id) return null;
  if (typeof r.display_name === "string" && r.display_name) return r.display_name;
  if (typeof r.name === "string" && r.name) return r.name;
  return null;
}

/** 리소스 이름을 react-query 캐시에서 찾는다. 목록·상세 어느 쿼리든 같은 id 의 객체가 있으면 display_name 또는 name 이다. */
export function findResourceName(queryClient: QueryClient, kind: DeletableResource, id: string): string | null {
  for (const [, data] of queryClient.getQueriesData<unknown>({ queryKey: [PATH_SEGMENT[kind]] })) {
    if (Array.isArray(data)) {
      for (const o of data) {
        const n = nameOf(o, id);
        if (n) return n;
      }
    } else {
      const n = nameOf(data, id);
      if (n) return n;
    }
  }
  return null;
}

/**
 * 턴에 실어 보내는 문맥. 이름을 못 찾으면 id 를 이름으로 보낸다(계약의 name 은 필수다). unsaved_edits 는 캔버스에 저장하지
 * 않은 변경이 있다는 표시이고(P48 6.5절) 참일 때만 싣는다. 이 칸을 모르는 앞 판의 플랫폼은 문맥의 모르는 칸을 422 로 막으므로
 * 늘 실으면 모든 턴이 막힌다.
 */
export function buildContext(pathname: string, queryClient?: QueryClient, unsavedEdits = false): AssistantContext {
  const unsaved = unsavedEdits ? { unsaved_edits: true } : {};
  const r = resourceFromPath(pathname);
  if (!r) return { route: pathname, ...unsaved };
  const name = queryClient ? findResourceName(queryClient, r.kind, r.id) : null;
  return { route: pathname, resource: { kind: r.kind, id: r.id, name: name ?? r.id }, ...unsaved };
}
