import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type MouseEvent, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import { ErrorBody } from "@/components/ErrorMessage";
import { ReferencesNotice, useReferences } from "@/components/ReferencesCard";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useT } from "@/i18n";
import { DELETE_RULES, deleteTitle, type DeletableResource, type DeleteTarget } from "@/lib/deletion";
import { referenceNames } from "@/lib/references";

/** 이어서 지운다는 알림이 떠 있는 시간(ms). */
const CONTINUING_NOTICE_MS = 8000;

/**
 * 리소스 삭제 확인 대화상자. 목록 행과 상세 화면이 같이 쓴다.
 * target 이 있으면 열리고, 삭제 mutation 과 목록 쿼리 무효화까지 여기서 끝낸다.
 * 서버가 거부하면(409 등) 오류를 대화상자 안에 보여 주고 열어 둔다.
 *
 * Backend API 는 read 토큰이 살아 있으면 tokens_active 로 거부된다. 그때는 토큰을 회수하며
 * 지우는 강제 삭제 버튼을 같은 대화상자 안에 보인다 (상세 화면의 기존 동작과 같다).
 *
 * 무엇이 이것을 붙들고 있는지는 서버 409 를 기다리지 않고 먼저 보인다 (D43). 그래도 409 가
 * 오면 그 응답의 id 를 같은 목록에서 이름으로 바꿔 보인다.
 */
export function ResourceDeleteDialog({
  resource,
  target,
  onClose,
  onDeleted,
  confirmLabel,
  description,
  extra,
}: {
  resource: DeletableResource;
  target: DeleteTarget | null;
  onClose: () => void;
  /** 삭제가 끝나고 쿼리를 무효화한 뒤 불린다 (상세 화면은 목록으로 이동). */
  onDeleted?: () => void;
  /** 확인 단추의 말이고 제목에도 들어간다. 기본은 삭제이고 허브 게시 항목은 게시 해제다. */
  confirmLabel?: string;
  /** 규칙표의 설명을 대신할 문장. 빈 글이면 설명 문단을 그리지 않는다. */
  description?: string;
  /** 설명 아래에 끼우는 추가 안내 (예: MCP Server 삭제 시 참조 중인 Gateway 경고). */
  extra?: ReactNode;
}) {
  const t = useT();
  const text = t.resources.delete;
  const rule = DELETE_RULES[resource];
  const sentence = description ?? text.description[resource];
  const action = confirmLabel ?? t.common.delete;
  const queryClient = useQueryClient();
  // 다른 작업이 문서를 쥐고 있으면 서버는 상태만 "삭제 중" 으로 옮기고 그 작업이 이어서 지운다 (P66).
  // 그때는 목록으로 옮기지 않고 그 사실을 화면 아래 알림으로 잠깐 보인다
  const [continuing, setContinuing] = useState(false);
  useEffect(() => {
    if (!continuing) return;
    const timer = setTimeout(() => setContinuing(false), CONTINUING_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [continuing]);
  const remove = useMutation({
    mutationFn: ({ id, force }: { id: string; force?: boolean }) => rule.remove(id, { force }),
    onSuccess: async (result) => {
      const deferred = result !== undefined && !result.deleted;
      for (const key of rule.invalidate) await queryClient.invalidateQueries({ queryKey: [key] });
      onClose();
      if (deferred) setContinuing(true);
      else onDeleted?.();
    },
  });
  const { reset } = remove;

  // 대상이 바뀌거나 닫히면 이전 오류를 지운다.
  useEffect(() => {
    reset();
  }, [target?.id, reset]);

  const tokensActive = resource === "backend-api" && remove.error instanceof ApiError && remove.error.code === "tokens_active";
  const references = useReferences(resource, target?.id ?? "", target !== null);

  return (
    <>
      {continuing ? (
        <p
          role="status"
          aria-live="polite"
          data-testid="delete-continuing"
          className="fixed right-4 bottom-4 z-(--ui-z-dialog) rounded-card border border-line bg-surface-popover px-4 py-3 text-sm text-heading shadow-popover"
        >
          {text.continuing}
        </p>
      ) : null}
      <ConfirmDialog
        open={target !== null}
        title={deleteTitle(t, resource, action)}
        description={
          <>
            <p className="font-medium text-heading" data-testid="confirm-target">
              {target?.name}
            </p>
            {sentence ? <p>{sentence}</p> : null}
            {extra}
            <ReferencesNotice resource={resource} id={target?.id} open={target !== null} testId="delete-references" />
          </>
        }
        confirmLabel={action}
        pendingLabel={t.common.deleting}
        requireName={rule.requireName ? target?.name : undefined}
        pending={remove.isPending}
        error={remove.error ? <ErrorBody error={remove.error} names={referenceNames(references.data ?? [])} /> : null}
        extraActions={
          tokensActive && target ? (
            <Button variant="destructive" data-testid="delete-force-button" disabled={remove.isPending} onClick={() => remove.mutate({ id: target.id, force: true })}>
              {text.force}
            </Button>
          ) : null
        }
        onConfirm={() => {
          if (target) remove.mutate({ id: target.id });
        }}
        onCancel={onClose}
      />
    </>
  );
}

function TrashIcon() {
  return (
    <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2.5 4h11" />
      <path d="M6 4V2.75A.75.75 0 0 1 6.75 2h2.5a.75.75 0 0 1 .75.75V4" />
      <path d="M12.5 4l-.6 8.4a1 1 0 0 1-1 .93H5.1a1 1 0 0 1-1-.93L3.5 4" />
      <path d="M6.5 7v3.5M9.5 7v3.5" />
    </svg>
  );
}

/**
 * 목록 행 오른쪽 끝의 휴지통. 아이콘만 있는 ghost 버튼이라 aria-label 로 이름을 읽어 준다.
 * 행 클릭·링크와 겹치지 않게 이벤트 전파를 끊는다.
 */
export function RowDeleteButton({ name, onClick, children }: { name: string; onClick: () => void; children?: ReactNode }) {
  const t = useT();
  return (
    <button
      type="button"
      aria-label={t.resources.delete.rowLabel(name)}
      title={t.common.delete}
      data-testid={`row-delete-${name}`}
      className="inline-flex size-7 items-center justify-center rounded-item text-muted transition-colors hover:bg-hover hover:text-err-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-err/50"
      onClick={(e: MouseEvent<HTMLButtonElement>) => {
        e.stopPropagation();
        e.preventDefault();
        onClick();
      }}
    >
      {children ?? <TrashIcon />}
    </button>
  );
}
