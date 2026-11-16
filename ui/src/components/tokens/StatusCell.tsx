/** 목록의 상태 배지와 그 곁의 한 줄. 개인 토큰 목록(`TokenTable`)과 OAuth 연결 목록(`GrantList`)이 같이 쓴다. */
import { Badge } from "@/components/ui/badge";
import type { StatusBadge } from "@/lib/personalTokens";

export function StatusCell({ badge, aside, testId }: { badge: StatusBadge; aside: string | null; testId: string }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid={testId}>
      <Badge variant={badge.variant}>{badge.label}</Badge>
      {aside ? <span className="text-xs text-muted">{aside}</span> : null}
    </div>
  );
}
