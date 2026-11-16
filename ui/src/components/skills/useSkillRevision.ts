/**
 * Skill 리비전 하나의 본문 (D64). 상세의 개요·파일 탭, 리비전 차이, 새 리비전 화면이 같은 키로 읽는다.
 *
 * 쓴 리비전은 바뀌지 않으므로(고치는 API 가 없다) 한 번 받은 본문을 다시 묻지 않는다.
 */
import { useQuery } from "@tanstack/react-query";

import { getSkillRevision } from "@/api/skills";
import { skillRevisionKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

export function useSkillRevision(id: string, n: number | undefined, enabled = true) {
  const tenant = useTenant();
  return useQuery({
    queryKey: skillRevisionKey(tenant, id, n ?? 0),
    queryFn: () => getSkillRevision(id, n ?? 0),
    enabled: enabled && n !== undefined,
    staleTime: Infinity,
  });
}
