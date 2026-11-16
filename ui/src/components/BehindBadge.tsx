/**
 * 정의가 배포보다 앞서 있는가 (D42). 정의만 고쳐 놓고 재배포를 안 한 상태다.
 *
 * 목록 셋과 상세 셋이 같은 비교식과 같은 배지를 각자 적고 있었다. `StateBadge` 의 판정들처럼 배지와
 * 판정을 한 파일에 둔다.
 */
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";

/** 배포된 적이 있고 그 리비전이 지금 정의와 다르다. 안 올린 `draft` 는 뒤처진 것이 아니다. 참이면 배포된 리비전이 있다. */
export function isBehind<T extends { revision: number; deployed_revision: number | null }>(item: T): item is T & { deployed_revision: number } {
  return item.deployed_revision !== null && item.deployed_revision !== item.revision;
}

/** 목록 리비전 칸의 `배포 rN` 배지. 뒤처지지 않았으면 아무것도 그리지 않는다. */
export function BehindBadge({ item, testId }: { item: { revision: number; deployed_revision: number | null }; testId: string }) {
  const t = useT();
  if (!isBehind(item)) return null;
  return (
    <Badge variant="warn" className="ml-1.5" data-testid={testId}>
      {t.ui.references.pinnedDeployed(item.deployed_revision)}
    </Badge>
  );
}
