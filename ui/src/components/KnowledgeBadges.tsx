/** Knowledge 페이지 셋이 같이 쓰는 조각 (D32). 상태 칩과 자주 나오는 코드에 사람 문구를 붙이는 오류 상자다. */
import { ApiError } from "@/api/client";
import type { KnowledgeBase, KnowledgeDocumentState } from "@/api/types";
import { ErrorBody } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";
import { knowledgeErrorText } from "@/lib/errorText";
import { documentStateVariant, knowledgeBaseBadge } from "@/lib/knowledge";
import { formatDate } from "@/lib/utils";

/** ready·orphaned_table·deleting. orphaned_table 은 title 로 남은 테이블 이름을 알려 준다. */
export function KnowledgeBaseStateBadge({ kb, testId }: { kb: Pick<KnowledgeBase, "state" | "deleting_at" | "table">; testId?: string }) {
  const t = useT();
  const text = t.shared.knowledgeBadges;
  const b = knowledgeBaseBadge(kb);
  const title = kb.state === "orphaned_table" ? text.orphanedTable(kb.table) : kb.deleting_at ? text.deletingSince(formatDate(t, kb.deleting_at)) : undefined;
  return (
    <Badge variant={b.variant} data-testid={testId} title={title}>
      {b.text}
    </Badge>
  );
}

export function DocumentStateBadge({ state, testId }: { state: KnowledgeDocumentState; testId?: string }) {
  return (
    <Badge variant={documentStateVariant(state)} data-testid={testId}>
      {state}
    </Badge>
  );
}

/** ErrorMessage 와 같되 자주 나오는 코드(pgvector_missing·dimensions_required·indexing·in_use·too_large 등)에는 합니다체 한 줄을 붙인다. */
export function KnowledgeErrorMessage({ error, testId = "error-message" }: { error: unknown; testId?: string }) {
  const t = useT();
  if (!error) return null;
  const hint = error instanceof ApiError ? knowledgeErrorText(t, error.code) : null;
  return (
    <Alert variant="error" data-testid={testId}>
      <ErrorBody error={error} />
      {hint ? (
        <p className="mt-1 text-xs" data-testid={`${testId}-hint`}>
          {hint}
        </p>
      ) : null}
    </Alert>
  );
}
