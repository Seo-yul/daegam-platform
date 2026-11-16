/**
 * Backend API 버전의 OpenAPI 문서 팝업. 열릴 때 /versions/{n}/spec 을 받아 OpenApiDocs 뷰어에 넘긴다.
 * 뷰어는 lazy 청크다. 문서 본문은 버전마다 불변이라 캐시를 오래 둔다. 받는 중·오류·뷰어를 그리는 몸통(ApiDocsBody)은
 * 플랫폼 자신의 API 문서 팝업(PlatformApiDocsDialog)과 함께 쓴다.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { lazy, Suspense } from "react";

import { getVersionSpec } from "@/api/resources";
import type { OpenApiDocument } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { useTenant } from "@/lib/tenant";

const OpenApiDocs = lazy(() => import("./OpenApiDocs"));

export interface ApiDocsDialogProps {
  profileId: string;
  profileName: string;
  /** 보여 줄 버전. null 이면 저장된 버전이 없다는 안내만 보인다. */
  version: number | null;
  open: boolean;
  /** 열릴 때 선택해 둘 오퍼레이션(도구 이름). */
  operationId?: string | null;
  onClose: () => void;
}

export function ApiDocsDialog({ profileId, profileName, version, open, operationId, onClose }: ApiDocsDialogProps) {
  const t = useT();
  const text = t.shared.apiDocsDialog;
  const tenant = useTenant();
  const spec = useQuery({
    queryKey: ["backend-apis", tenant, profileId, "versions", version, "spec"],
    queryFn: () => getVersionSpec(profileId, version as number),
    enabled: open && version !== null,
    staleTime: Infinity,
  });

  return (
    <Modal
      open={open}
      size="lg"
      testId="api-docs-dialog"
      closeTestId="api-docs-close"
      title={text.title(profileName)}
      titleExtra={version !== null ? <Badge variant="muted">v{version}</Badge> : null}
      onClose={onClose}
    >
      {version === null ? (
        <p className="p-5 text-sm text-muted">{text.noVersion}</p>
      ) : (
        <ApiDocsBody query={spec} operationId={operationId} />
      )}
    </Modal>
  );
}

/**
 * 받은 문서를 뷰어로 그리는 몸통. 받는 중·오류·뷰어 청크를 받는 중을 같은 꼴로 보인다. toolNames 는 operationId 옆의
 * "Tool 이름이 됩니다" 안내를 보일지다(Backend API 문서만 참).
 */
export function ApiDocsBody({
  query,
  operationId,
  toolNames = true,
}: {
  query: UseQueryResult<OpenApiDocument>;
  operationId?: string | null;
  toolNames?: boolean;
}) {
  const t = useT();
  const text = t.shared.apiDocsDialog;
  // 아직 데이터가 없으면 받는 중이다. 꺼져 기다리는 쿼리(판을 받는 동안의 플랫폼 문서)도 같다
  if (query.isPending) {
    return (
      <p className="p-5 text-sm text-muted" data-testid="api-docs-loading">
        {text.loading}
      </p>
    );
  }
  if (query.error) {
    return (
      <div className="p-5">
        <ErrorMessage error={query.error} testId="api-docs-error" />
      </div>
    );
  }
  if (!query.data) return null;
  return (
    <Suspense
      fallback={
        <p className="p-5 text-sm text-muted" data-testid="api-docs-loading">
          {text.loadingViewer}
        </p>
      }
    >
      <OpenApiDocs doc={query.data} initialOperationId={operationId} toolNames={toolNames} />
    </Suspense>
  );
}
