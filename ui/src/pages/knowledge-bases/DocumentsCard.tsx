import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { addKnowledgeDocumentUrl, deleteKnowledgeDocument, reindexKnowledgeDocument, uploadKnowledgeDocument } from "@/api/resources";
import type { KnowledgeDocument } from "@/api/types";
import { ErrorBody } from "@/components/ErrorMessage";
import { DocumentStateBadge, KnowledgeErrorMessage } from "@/components/KnowledgeBadges";
import { StateRow } from "@/components/StateBlock";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { fileButtonClass } from "@/components/ui/field";
import { Field, Input } from "@/components/ui/input";
import { ActionTD, ActionTH, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { knowledgeErrorText } from "@/lib/errorText";
import { fileTooLargeText } from "@/lib/files";
import { DOCUMENT_ACCEPT, DOCUMENT_FORMAT_LABEL, KNOWLEDGE_POLL_MS, MAX_DOCUMENT_BYTES, MAX_DOCUMENT_MB } from "@/lib/knowledge";
import { cn, formatBytes } from "@/lib/utils";

/** Knowledge Base 하나의 문서 수 상한. 서버 services/knowledge/service.py 의 MAX_DOCUMENTS 와 같다. */
const MAX_DOCUMENTS = 200;

/** 페이지 위 알림의 갈래와 값. 문장은 상세가 그릴 때 사전의 knowledgeBases.notice 로 만든다. */
export type KnowledgeNotice = { kind: "reindexAll"; count: number } | { kind: "uploaded" | "added" | "reindexDoc"; name: string };

/** 워커가 손대는 중이라 행 동작을 잠그는 상태. purging 은 Knowledge Base 삭제가 남긴 것이라 손댈 수 없다. */
function busy(d: KnowledgeDocument): boolean {
  return d.state === "indexing" || d.state === "deleting" || d.state === "purging";
}

/**
 * Knowledge Base 상세의 문서 카드 (D32). 업로드·URL 추가·행별 다시 인덱싱과 삭제, 문서 삭제 확인을 맡는다.
 *
 * 문서 쿼리는 부모가 갖는다. 인덱싱 중인 문서가 있는지가 Knowledge Base 쿼리의 폴링과 "전체 다시 인덱싱"
 * 버튼까지 정하기 때문이다. 동작이 끝나면 알릴 갈래와 값을 `onNotice` 로 올려 페이지 위의 알림 자리에 띄운다.
 */
export function KnowledgeDocumentsCard({
  kbId,
  docs,
  active,
  writesLocked,
  onNotice,
}: {
  kbId: string;
  docs: UseQueryResult<KnowledgeDocument[]>;
  /** pending·indexing·deleting 문서가 있어 폴링 중인가. */
  active: boolean;
  writesLocked: boolean;
  onNotice: (notice: KnowledgeNotice) => void;
}) {
  const t = useT();
  const text = t.knowledgeBases.documents;
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [urlName, setUrlName] = useState("");
  const [docTarget, setDocTarget] = useState<KnowledgeDocument | null>(null);
  // 올리는 중인 파일 이름과, 크기를 미리 재서 되돌린 파일. 둘 다 파일 칸 옆에 그리고 사유 문장은 그릴 때 만든다
  const [uploadingName, setUploadingName] = useState<string | null>(null);
  const [tooLarge, setTooLarge] = useState<{ name: string; size: number } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // 접두사 하나로 목록·상세·문서 쿼리가 모두 새로 읽힌다
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });

  const upload = useMutation({
    mutationFn: (file: File) => uploadKnowledgeDocument(kbId, file),
    onSuccess: async (doc) => {
      if (fileRef.current) fileRef.current.value = "";
      setUploadingName(null);
      onNotice({ kind: "uploaded", name: doc.name });
      await invalidate();
    },
    onError: () => {
      if (fileRef.current) fileRef.current.value = "";
      setUploadingName(null);
    },
  });
  const addUrl = useMutation({
    mutationFn: () => addKnowledgeDocumentUrl(kbId, { url: url.trim(), name: urlName.trim() || null }),
    onSuccess: async (doc) => {
      setUrl("");
      setUrlName("");
      onNotice({ kind: "added", name: doc.name });
      await invalidate();
    },
  });
  const reindexDoc = useMutation({
    mutationFn: (docId: string) => reindexKnowledgeDocument(kbId, docId),
    onSuccess: async (doc) => {
      onNotice({ kind: "reindexDoc", name: doc.name });
      await invalidate();
    },
  });
  const deleteDoc = useMutation({
    mutationFn: (docId: string) => deleteKnowledgeDocument(kbId, docId),
    onSuccess: async () => {
      setDocTarget(null);
      await invalidate();
    },
  });
  const { reset: resetDeleteDoc } = deleteDoc;
  useEffect(() => {
    resetDeleteDoc();
  }, [docTarget?.id, resetDeleteDoc]);

  const rows = docs.data ?? [];
  const hasFailed = rows.some((d) => d.state === "failed");

  return (
    <>
      <Card data-testid="kb-documents-card">
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
          <span className="text-xs text-muted">
            {text.limits(DOCUMENT_FORMAT_LABEL, MAX_DOCUMENT_MB, MAX_DOCUMENTS)}
            {active ? <span data-testid="docs-polling">{text.polling(KNOWLEDGE_POLL_MS / 1000)}</span> : null}
          </span>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-3">
            <Field label={text.upload} htmlFor="knowledge-upload" hint={text.uploadHint}>
              <input
                ref={fileRef}
                id="knowledge-upload"
                data-testid="knowledge-upload"
                type="file"
                accept={DOCUMENT_ACCEPT}
                className={cn("text-sm", fileButtonClass)}
                disabled={writesLocked || upload.isPending}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  // 한도를 넘는 파일은 다 올린 뒤 413 을 받는 대신 고른 자리에서 되돌린다
                  if (f.size > MAX_DOCUMENT_BYTES) {
                    setTooLarge({ name: f.name, size: f.size });
                    e.target.value = "";
                    return;
                  }
                  setTooLarge(null);
                  setUploadingName(f.name);
                  upload.mutate(f);
                }}
              />
            </Field>
            {upload.isPending && uploadingName ? (
              <p className="mb-5 text-xs text-muted" data-testid="upload-pending">
                {text.uploading(uploadingName)}
              </p>
            ) : null}
            <form
              className="flex flex-wrap items-end gap-2"
              data-testid="knowledge-url-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (url.trim()) addUrl.mutate();
              }}
            >
              <Field label={text.addUrl} htmlFor="knowledge-url" hint={text.urlHint}>
                <Input id="knowledge-url" data-testid="knowledge-url" type="url" className="w-80" placeholder="https://example.com/guide.html" value={url} onChange={(e) => setUrl(e.target.value)} disabled={writesLocked} />
              </Field>
              <Field label={text.columns.name} htmlFor="knowledge-url-name" hint={text.nameHint}>
                <Input id="knowledge-url-name" data-testid="knowledge-url-name" className="w-48" value={urlName} onChange={(e) => setUrlName(e.target.value)} disabled={writesLocked} />
              </Field>
              <Button type="submit" variant="outline" data-testid="knowledge-url-add" disabled={writesLocked || addUrl.isPending || !url.trim()} className="mb-5">
                {addUrl.isPending ? text.adding : t.shared.add}
              </Button>
            </form>
          </div>
          {tooLarge ? (
            <Alert variant="warn" data-testid="upload-too-large">
              {fileTooLargeText(t, tooLarge, text.sizeLimit(MAX_DOCUMENT_MB), { advice: text.splitAdvice })}
            </Alert>
          ) : null}
          <KnowledgeErrorMessage error={upload.error} testId="upload-error" />
          <KnowledgeErrorMessage error={addUrl.error} testId="url-error" />
          <KnowledgeErrorMessage error={reindexDoc.error} testId="doc-reindex-error" />
          {docs.isError && rows.length ? (
            // 폴링이 잠깐 끊긴 것과 문서가 사라진 것은 다르다. 아래 표는 마지막으로 읽은 것이다
            <Alert variant="warn" data-testid="documents-stale">
              {text.stale}
            </Alert>
          ) : null}
          {hasFailed ? (
            <Alert variant="info" data-testid="documents-failed-notice">
              {text.failedNotice}
            </Alert>
          ) : null}
          <Table data-testid="knowledge-docs-table">
            <THead>
              <tr>
                <TH>{text.columns.name}</TH>
                <TH>{text.columns.source}</TH>
                <TH>{text.columns.size}</TH>
                <TH>{text.columns.state}</TH>
                <TH>{text.columns.chunks}</TH>
                <TH>{text.columns.tokens}</TH>
                <TH>{text.columns.error}</TH>
                <ActionTH />
              </tr>
            </THead>
            <TBody>
              {docs.isLoading || !rows.length ? (
                // 못 불러온 것을 "아직 문서가 없습니다" 로 보이면 같은 파일을 다시 올리게 된다
                <StateRow
                  colSpan={8}
                  loading={docs.isLoading}
                  error={docs.error}
                  onRetry={() => void docs.refetch()}
                  empty={text.empty}
                />
              ) : (
                rows.map((d, i) => {
                  const errorHint = d.error ? knowledgeErrorText(t, d.error.code) : null;
                  return (
                    <TR key={d.id} data-testid={`knowledge-doc-${i}`} data-doc-id={d.id}>
                      <TD>
                        <span className="font-medium">{d.name}</span>
                        {d.source.kind === "url" && d.source.url ? (
                          <a href={d.source.url} target="_blank" rel="noreferrer" className="ml-2 font-mono text-xs text-muted underline-offset-2 hover:underline">
                            {d.source.url}
                          </a>
                        ) : null}
                      </TD>
                      <TD>
                        <Badge variant="muted">{d.source.kind}</Badge>
                      </TD>
                      <TD className="text-xs">{formatBytes(d.bytes)}</TD>
                      <TD>
                        <DocumentStateBadge state={d.state} testId={`knowledge-doc-state-${i}`} />
                      </TD>
                      <TD>{d.chunks}</TD>
                      <TD title={t.knowledgeBases.tokensHint}>{d.usage.tokens}</TD>
                      <TD>
                        {d.error ? (
                          // 사람 문구가 title 안에만 있으면 키보드·터치로는 열리지 않는다. 코드 아래에 그대로 편다
                          <>
                            <Badge variant="error" data-testid={`knowledge-doc-error-${i}`} title={d.error.detail ?? undefined}>
                              {d.error.code}
                            </Badge>
                            {errorHint ? (
                              <p className="mt-1 max-w-56 text-xs text-muted" data-testid={`knowledge-doc-error-text-${i}`}>
                                {errorHint}
                              </p>
                            ) : null}
                          </>
                        ) : null}
                      </TD>
                      <ActionTD>
                        <div className="flex justify-end gap-1">
                          <Button size="sm" variant="outline" data-testid={`knowledge-doc-reindex-${i}`} disabled={busy(d) || writesLocked || reindexDoc.isPending} onClick={() => reindexDoc.mutate(d.id)}>
                            {text.reindex}
                          </Button>
                          <Button size="sm" variant="ghost" className="text-err-fg" data-testid={`knowledge-doc-delete-${i}`} disabled={busy(d)} onClick={() => setDocTarget(d)}>
                            {d.state === "failed" ? text.deleteAgain : t.common.delete}
                          </Button>
                        </div>
                      </ActionTD>
                    </TR>
                  );
                })
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <ConfirmDialog
        open={docTarget !== null}
        title={docTarget?.state === "failed" ? text.deleteAgain : text.deleteTitle}
        description={
          <>
            <p className="font-medium text-heading" data-testid="confirm-target">
              {docTarget?.name}
            </p>
            <p>{text.deleteBody}</p>
          </>
        }
        confirmLabel={t.common.delete}
        pendingLabel={t.common.deleting}
        pending={deleteDoc.isPending}
        error={deleteDoc.error ? <ErrorBody error={deleteDoc.error} /> : null}
        onConfirm={() => {
          if (docTarget) deleteDoc.mutate(docTarget.id);
        }}
        onCancel={() => setDocTarget(null)}
      />
    </>
  );
}
