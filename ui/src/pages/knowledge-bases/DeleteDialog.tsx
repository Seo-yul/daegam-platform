/**
 * Knowledge Base 삭제 확인 대화상자 (D32).
 *
 * 공용 ResourceDeleteDialog 를 쓰지 않는 이유는 응답 때문이다. 서버는 벡터 테이블 DROP 이 실패해도
 * 200 으로 `{deleted: false, warning: "table_not_dropped"}` 를 돌려주고 리소스를 orphaned_table 로
 * 남긴다(services/knowledge/service.py). 공용 대화상자는 200 을 성공으로 보고 닫으므로, 지웠다고 믿은
 * Knowledge Base 가 목록에 그대로 있고 남의 Postgres 에 벡터 테이블도 그대로 산다. 여기서는 `deleted`
 * 를 보고 거짓이면 닫지 않고 대화상자 안에서 이유를 말한다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError } from "@/api/client";
import { deleteKnowledgeBase } from "@/api/resources";
import type { KnowledgeBaseDeleteResponse } from "@/api/types";
import { ErrorBody } from "@/components/ErrorMessage";
import { ReferencesNotice, useReferences } from "@/components/ReferencesCard";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useT } from "@/i18n";
import { DELETE_RULES, deleteTitle } from "@/lib/deletion";
import { knowledgeErrorText } from "@/lib/errorText";
import { referenceNames } from "@/lib/references";

const rule = DELETE_RULES["knowledge-base"];

/** 삭제 대상. 목록 행과 상세 화면이 넘긴다. 벡터 테이블 이름은 확인 문구에 그대로 보인다. */
export interface KnowledgeBaseDeleteTarget {
  id: string;
  name: string;
  table?: string;
}

export function KnowledgeBaseDeleteDialog({
  target,
  onClose,
  onDeleted,
}: {
  target: KnowledgeBaseDeleteTarget | null;
  onClose: () => void;
  /** 지워지고 목록 쿼리를 무효화한 뒤 불린다 (상세 화면은 목록으로 이동). */
  onDeleted?: () => void;
}) {
  const t = useT();
  const text = t.knowledgeBases.deleteDialog;
  const queryClient = useQueryClient();
  const [leftover, setLeftover] = useState<KnowledgeBaseDeleteResponse | null>(null);

  const remove = useMutation({
    mutationFn: (id: string) => deleteKnowledgeBase(id),
    onSuccess: async (r) => {
      for (const key of rule.invalidate) await queryClient.invalidateQueries({ queryKey: [key] });
      // 200 이어도 테이블이 남았으면 지운 것이 아니다. 대화상자를 열어 둔 채 이유를 보인다
      if (!r.deleted) {
        setLeftover(r);
        return;
      }
      onClose();
      onDeleted?.();
    },
  });
  const { reset } = remove;

  // 대상이 바뀌거나 닫히면 이전 오류와 경고를 지운다.
  useEffect(() => {
    reset();
    setLeftover(null);
  }, [target?.id, reset]);

  const hint = remove.error instanceof ApiError ? knowledgeErrorText(t, remove.error.code) : null;
  // 409 in_use 가 실어 오는 agent_ids 를 이름으로 바꾼다 (D43)
  const names = referenceNames(useReferences("knowledge-base", target?.id ?? "", target !== null).data ?? []);

  return (
    <ConfirmDialog
      open={target !== null}
      title={deleteTitle(t, "knowledge-base")}
      description={
        <>
          <p className="font-medium text-heading" data-testid="confirm-target">
            {target?.name}
          </p>
          <p>{t.resources.delete.description["knowledge-base"]}</p>
          {target?.table ? (
            <p className="font-mono text-xs" data-testid="confirm-table">
              {text.dropTable(target.table)}
            </p>
          ) : null}
          <ReferencesNotice resource="knowledge-base" id={target?.id} open={target !== null} testId="delete-references" />
        </>
      }
      confirmLabel={t.common.delete}
      pendingLabel={t.common.deleting}
      pending={remove.isPending}
      error={
        remove.error ? (
          <>
            <ErrorBody error={remove.error} names={names} />
            {hint ? (
              <p className="mt-1 text-xs" data-testid="confirm-error-hint">
                {hint}
              </p>
            ) : null}
          </>
        ) : leftover ? (
          <div data-testid="confirm-table-not-dropped">{text.tableNotDropped(leftover.warning ?? "table_not_dropped")}</div>
        ) : null
      }
      onConfirm={() => {
        if (target) remove.mutate(target.id);
      }}
      onCancel={onClose}
    />
  );
}
