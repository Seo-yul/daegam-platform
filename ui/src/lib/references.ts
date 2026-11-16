/**
 * 참조 목록의 화면 어휘 (D43). 서버가 주는 `kind` 를 사람이 읽는 이름과 상세 경로로 옮긴다.
 *
 * 이름과 경로는 `lib/deletion.ts` 의 규칙표가 정본이라(D36) 여기서 다시 적지 않는다. 표에 없는
 * 것은 `token` 하나인데, 리소스가 아니라 Backend API 가 발급한 읽기 토큰이라 상세 화면도 링크도
 * 없다. 화면은 그것만 개수로 말한다. 그 이름과 개수 줄은 409 참조 목록(`errors.refNames`, `errors.refCount`)과 같은
 * 말을 쓴다.
 */
import type { Reference, ReferenceKind, PinnedRevisions } from "@/api/types";
import type { Messages } from "@/i18n";
import { PATH_SEGMENT, resourceLabel, type DeletableResource } from "@/lib/deletion";

/** 참조 종류 → 삭제 규칙표의 kind. 허브 항목만 이름이 다르고(`hub-item` ↔ `hub`) 토큰은 없다. */
const RESOURCE: Partial<Record<ReferenceKind, DeletableResource>> = {
  "backend-api": "backend-api",
  "mcp-server": "mcp-server",
  gateway: "gateway",
  agent: "agent",
  "data-service": "data-service",
  "knowledge-base": "knowledge-base",
  "hub-item": "hub",
};

/** 화면에 보이는 종류 이름. */
export function referenceLabel(t: Messages, kind: string): string {
  const resource = RESOURCE[kind as ReferenceKind];
  if (resource) return resourceLabel(t, resource);
  return kind === "token" ? t.errors.refNames.token_ids : kind;
}

/** 그 리소스의 상세 경로. 링크로 갈 곳이 없으면 null 이다. */
export function referencePath(kind: string, id: string): string | null {
  const resource = RESOURCE[kind as ReferenceKind];
  return resource ? `/${PATH_SEGMENT[resource]}/${id}` : null;
}

/**
 * 행 끝의 흐린 줄. Skill 을 핀한 Agent 는 몇 번 리비전을 핀했는지를, 나머지는 정의가 붙드는지 배포본이 붙드는지를
 * 적는다. 재배포 뒤에야 풀리는 참조를 가려 보이기 위해서다.
 */
export function holdText(t: Messages, ref: Pick<Reference, "pins" | "via" | "revision">): string {
  if (ref.pins) return pinsText(t, ref.pins);
  const text = t.ui.references;
  const revision = text.revision[ref.revision];
  // 서버가 주는 via 는 허브 항목 하나다(services/references.py). 모르는 값이면 거친 것을 빼고 붙든 쪽만 적는다
  return ref.via === "hub-item" ? text.viaHubItem(revision) : revision;
}

/** Skill 을 핀한 Agent 의 리비전 ("정의 r3, 배포 r2", D64). 붙들지 않는 쪽은 빠진다. */
function pinsText(t: Messages, pins: PinnedRevisions): string {
  const text = t.ui.references;
  const parts = [pins.current !== null ? text.pinnedCurrent(pins.current) : null, pins.deployed !== null ? text.pinnedDeployed(pins.deployed) : null];
  return parts.filter(Boolean).join(", ");
}

/** 핀 가운데 최신 리비전보다 낮은 것이 있는가. */
export function pinsBehind(pins: PinnedRevisions, latest: number): boolean {
  return [pins.current, pins.deployed].some((n) => n !== null && n < latest);
}

/** 종류별로 묶는다. 서버가 준 차례를 지키므로 종류의 순서도 응답 순서 그대로다. */
export function groupByKind(refs: Reference[]): { kind: string; items: Reference[] }[] {
  const out: { kind: string; items: Reference[] }[] = [];
  for (const ref of refs) {
    const group = out.find((g) => g.kind === ref.kind);
    if (group) group.items.push(ref);
    else out.push({ kind: ref.kind, items: [ref] });
  }
  return out;
}

/** 한 줄 요약 ("MCP Server 2개, Agent 1개"). 대화상자가 목록 앞에 세운다. */
export function summarizeReferences(t: Messages, refs: Reference[]): string {
  return groupByKind(refs)
    .map((g) => t.errors.refCount(referenceLabel(t, g.kind), g.items.length))
    .join(", ");
}

/** id → 이름. 서버 409 가 id 만 실어 오므로 그것을 이름으로 바꿀 때 쓴다 (`lib/errorText.ts`). */
export function referenceNames(refs: Reference[]): ReadonlyMap<string, string> {
  return new Map(refs.map((r) => [r.id, r.display_name || r.name]));
}
