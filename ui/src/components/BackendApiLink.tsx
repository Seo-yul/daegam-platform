/**
 * Backend API 이름이자 링크 (D44 넷째). MCP Server 목록의 백엔드 칸이 쓴다.
 *
 * 그 칸은 지금까지 개수뿐이었다. "2" 만 보고는 어느 API 를 감싼 서버인지 알 수 없어 상세를 열어야
 * 했다. 목록 응답을 `ClusterLink` 와 같은 꼴로 캐시에서 읽으므로 행마다 왕복이 늘지 않는다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listBackendApis } from "@/api/resources";
import { useT } from "@/i18n";
import { resolveName } from "@/lib/names";
import { useTenant } from "@/lib/tenant";

export function BackendApiLink({ ids, testId }: { ids: string[]; testId?: string }) {
  const t = useT();
  const tenant = useTenant();
  const q = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis, enabled: ids.length > 0 });
  if (!ids.length) {
    return (
      <span className="text-dim" data-testid={testId}>
        -
      </span>
    );
  }
  // 아직 목록을 못 받았으면 잠깐 비워 두고, 받았는데 없으면 지워진 것이다
  const nameOf = (id: string) => resolveName(t, q.data, id);
  return (
    // 여럿을 다 늘어놓으면 52px 한 줄을 넘는다. 첫 이름만 링크로 두고 나머지는 수로 접되 title 에 다 적는다
    <span className="inline-flex items-center gap-1.5" data-testid={testId} title={ids.map(nameOf).join(", ")}>
      <Link className="truncate underline-offset-2 hover:underline" to={`/backend-apis/${ids[0]}`}>
        {nameOf(ids[0])}
      </Link>
      {ids.length > 1 ? <span className="text-muted">{t.shared.backendApiLink.more(ids.length - 1)}</span> : null}
    </span>
  );
}
