/**
 * 클러스터 이름이자 링크 (D43 넷째). 클러스터를 말하는 다섯 화면이 같은 조각을 쓴다.
 *
 * 지금까지 이 자리는 전부 텍스트 잎이었다. 배포가 어디 있는지는 보이는데 거기로 갈 길이 없었다.
 * 목록은 화면마다 이미 받고 있으므로 같은 키(`CLUSTERS_KEY`)로 캐시를 읽는다. 왕복이 늘지 않는다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listClusters } from "@/api/resources";
import { useT } from "@/i18n";
import { displayName, resolveName } from "@/lib/names";
import { CLUSTERS_KEY } from "@/lib/queryKeys";

export function ClusterLink({ id, testId = "cluster-link" }: { id?: string | null; testId?: string }) {
  const t = useT();
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters, enabled: Boolean(id) });
  if (!id) return <>-</>;
  const cluster = clusters.data?.find((c) => c.id === id);
  // 아직 목록을 못 받았거나 받았는데 없으면(지워졌다) 링크 없이 그 사정만 적는다
  if (!cluster) return <span data-testid={testId}>{resolveName(t, clusters.data, id)}</span>;
  return (
    <Link className="underline-offset-2 hover:underline" to={`/clusters/${id}`} data-testid={testId}>
      {displayName(cluster)}
    </Link>
  );
}
