/**
 * 비서가 쓰기 도구를 성공시키면 열린 목록을 새로 고친다 (D31). 도구 이름이 create·update·delete·deploy·
 * redeploy·publish·unpublish 로 시작하면 `lib/deletion.ts` 의 규칙표로 그 리소스 kind 의 쿼리 키를
 * 무효화한다. `resource` 이벤트는 링크 카드일 뿐이라 무효화하지 않는다.
 */
import type { QueryClient } from "@tanstack/react-query";

import { DELETE_RULES, type DeletableResource } from "@/lib/deletion";
import type { ChatEvent, ToolResultEvent } from "@/lib/chatStream";

const WRITE_TOOL_RE = /^(create|update|delete|deploy|redeploy|undeploy|clone|publish|unpublish)(_|$)/;

/**
 * 쓰기 도구인지, 그렇다면 어느 리소스 kind 를 건드리는지. 읽기 도구면 null, 쓰기인데 kind 를 못 고르면 빈 목록(전체 무효화)이다.
 * 이름의 `mcp_server` 같은 밑줄 꼴이 규칙표 kind 와 맞는다. 정의와 배포가 한 리소스라(D42) 배포 도구가 따로
 * 무효화할 kind 가 없다.
 */
export function writeToolKinds(name: string): DeletableResource[] | null {
  if (!WRITE_TOOL_RE.test(name)) return null;
  return (Object.keys(DELETE_RULES) as DeletableResource[]).filter((k) => name.includes(k.replace(/-/g, "_")));
}

/** kind 들의 규칙표 키를 무효화한다. kind 가 하나도 없으면 전부 무효화한다(무엇이 바뀌었는지 모르므로). */
export async function invalidateKinds(queryClient: QueryClient, kinds: DeletableResource[]): Promise<void> {
  if (!kinds.length) {
    await queryClient.invalidateQueries();
    return;
  }
  const keys = new Set(kinds.flatMap((k) => DELETE_RULES[k].invalidate));
  await Promise.all([...keys].map((key) => queryClient.invalidateQueries({ queryKey: [key] })));
}

/** 스트림 이벤트가 성공한 쓰기 도구 결과면 무효화한다. 무효화했으면 true. 실패한 결과(status=error)는 아무것도 바꾸지 않았으니 건너뛴다. */
export async function invalidateForEvent(queryClient: QueryClient, ev: ChatEvent): Promise<boolean> {
  if (ev.event === "tool_result") {
    const d = ev.data as ToolResultEvent;
    if (d.status === "error") return false;
    const kinds = writeToolKinds(d.name);
    if (!kinds) return false;
    await invalidateKinds(queryClient, kinds);
    return true;
  }
  return false;
}
