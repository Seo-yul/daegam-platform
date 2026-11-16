/**
 * 리소스 링크 카드 (D31). 도구 결과에 리소스 id 가 있으면 서비스가 `resource{kind, id, name, url}` 이벤트를 내고 패널이 이 카드로
 * 그린다. url 은 앱 안 경로라 Link 로 옮기고, 혹시 절대 URL 이면 새 탭이다. 종류 이름은 deletion.ts 규칙표의 label 이다.
 */
import { ExternalLink } from "lucide-react";
import { Link } from "react-router";

import type { AssistantResource } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { resourceLabel } from "@/lib/deletion";

export function ResourceCard({ resource, testId = "resource-card" }: { resource: AssistantResource; testId?: string }) {
  const t = useT();
  const label = resourceLabel(t, resource.kind);
  // 사람이 붙인 이름이 먼저다(D24). 400px 패널에서 36자 id 를 다 적으면 이름 칸이 0 폭으로 잘려 id 만 남았다.
  // id 는 앞 여덟 자만 적고 전체는 title 로 둔다
  const second = resource.display_name && resource.display_name !== resource.name ? resource.name : null;
  const body = (
    <>
      <Badge variant="accent">{label}</Badge>
      <span className="min-w-0 flex-1 truncate text-sm font-semibold text-heading" data-testid={`${testId}-name`}>
        {resource.display_name || resource.name || resource.id}
      </span>
      {second ? (
        <span className="max-w-[40%] shrink truncate font-mono text-[10.5px] text-muted" data-testid={`${testId}-k8s-name`}>
          {second}
        </span>
      ) : null}
      <code className="shrink-0 font-mono text-[10.5px] text-dim" title={resource.id} data-testid={`${testId}-id`}>
        {resource.id.slice(0, 8)}
      </code>
      <ExternalLink size={14} className="shrink-0 text-muted" aria-hidden />
    </>
  );
  const className = "flex items-center gap-2 rounded-chip border border-accent-line bg-surface-strong px-3 py-2 hover:bg-hover";
  if (!resource.url) {
    return (
      <div className={className} data-testid={testId} data-kind={resource.kind}>
        {body}
      </div>
    );
  }
  if (/^https?:\/\//.test(resource.url)) {
    return (
      <a className={className} href={resource.url} target="_blank" rel="noreferrer" data-testid={testId} data-kind={resource.kind}>
        {body}
      </a>
    );
  }
  return (
    <Link className={className} to={resource.url} data-testid={testId} data-kind={resource.kind}>
      {body}
    </Link>
  );
}
