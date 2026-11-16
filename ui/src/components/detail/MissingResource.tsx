/**
 * 상세를 열었는데 리소스가 없을 때의 화면 (D43). Agent·MCP Server·Gateway 상세가 같이 쓴다.
 *
 * 지워졌거나 원래 없는 것이면 빨간 줄 하나 대신 나갈 길이 있는 화면을 준다. 410 이면 언제 지워졌는지를
 * 먼저 말한다. 세 상세가 같은 두 덩어리를 각자 적고 있었다.
 */
import { NotFoundPage } from "@/components/NotFoundPage";
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";
import { formatDate } from "@/lib/utils";
import type { MissingResource } from "@/lib/warningText";

export function MissingResourceScreen({ missing, resource, backTo, backLabel }: { missing: MissingResource; resource: string; backTo: string; backLabel: string }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-4">
      {missing.gone ? (
        <Alert variant="warn" data-testid="not-found-gone">
          {t.detail.missingResource.gone(resource, formatDate(t, missing.deletedAt))}
        </Alert>
      ) : null}
      <NotFoundPage resource={resource} backTo={backTo} backLabel={backLabel} />
    </div>
  );
}
