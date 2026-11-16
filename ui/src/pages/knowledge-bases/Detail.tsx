import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { getKnowledgeBase, listDataServices, listKnowledgeDocuments, patchKnowledgeBase, reindexKnowledgeBase } from "@/api/resources";
import type { KnowledgeBasePatch, KnowledgeDocument } from "@/api/types";
import { KnowledgeBaseStateBadge, KnowledgeErrorMessage } from "@/components/KnowledgeBadges";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { ReferencesCard } from "@/components/ReferencesCard";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { queryGate } from "@/components/QueryGate";
import { type Messages, useT } from "@/i18n";
import { ACTIVE_DOCUMENT_STATES, KNOWLEDGE_POLL_MS, embeddingText } from "@/lib/knowledge";
import { displayName, secondaryName } from "@/lib/names";
import { dataServicesKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";
import { missingResource } from "@/lib/warningText";

import { ChunkingFields, type ChunkingValue } from "./ChunkingFields";
import { KnowledgeBaseDeleteDialog } from "./DeleteDialog";
import { KnowledgeDocumentsCard, type KnowledgeNotice } from "./DocumentsCard";

/** 인덱서가 아직 손댈 문서가 있는가. 문서 쿼리와 Knowledge Base 쿼리가 이것으로 폴링을 켠다. */
function hasActiveDocuments(list: KnowledgeDocument[] | undefined): boolean {
  return Boolean(list?.some((d) => ACTIVE_DOCUMENT_STATES.includes(d.state)));
}

/** 페이지 위 알림의 문장. 상태에는 갈래와 값만 두고 그릴 때 만든다. */
function noticeText(t: Messages, notice: KnowledgeNotice): string {
  const text = t.knowledgeBases.notice;
  return notice.kind === "reindexAll" ? text.reindexAll(notice.count) : text[notice.kind](notice.name);
}

/** 편집 폼 안의 알림. 바뀐 것이 없었는가, 저장했는가, 저장해서 다시 인덱싱하는가. */
type FormNotice = "noChange" | "saved" | "savedReindex";

/**
 * Knowledge Base 상세 (D32). 헤더(상태·테이블·임베딩·Database), 문서 표(업로드·URL·상태·다시 인덱싱·삭제·다시 삭제),
 * chunking·limits 편집, 삭제 대화상자. pending·indexing·deleting 문서가 있을 때만 5초 폴링이다.
 */
export function KnowledgeBaseDetailPage() {
  const t = useT();
  const text = t.knowledgeBases.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const docs = useQuery({
    queryKey: ["knowledge-bases", tenant, id, "documents"],
    queryFn: () => listKnowledgeDocuments(id),
    refetchInterval: (query) => (hasActiveDocuments(query.state.data) ? KNOWLEDGE_POLL_MS : false),
  });
  const active = hasActiveDocuments(docs.data);
  const q = useQuery({ queryKey: ["knowledge-bases", tenant, id], queryFn: () => getKnowledgeBase(id), refetchInterval: active ? KNOWLEDGE_POLL_MS : false });
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });

  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [chunking, setChunking] = useState<ChunkingValue>({ size: "", overlap: "", maxChunks: "" });
  const [notice, setNotice] = useState<KnowledgeNotice | null>(null);
  // 폼에서 난 알림은 폼 안에 둔다. 페이지 맨 위에 띄우면 긴 화면에서는 눌러도 아무 일이 없어 보인다
  const [formNotice, setFormNotice] = useState<FormNotice | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    const kb = q.data;
    if (!kb) return;
    setNames({ displayName: displayName(kb), name: kb.name });
    setChunking({ size: String(kb.chunking.size), overlap: String(kb.chunking.overlap), maxChunks: String(kb.limits.max_chunks) });
  }, [q.data]);

  // 접두사 하나로 목록·상세·문서 쿼리가 모두 새로 읽힌다
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });

  const reindexAll = useMutation({
    mutationFn: () => reindexKnowledgeBase(id),
    onSuccess: async (r) => {
      setNotice({ kind: "reindexAll", count: r.pending });
      await invalidate();
    },
  });

  // 바뀐 것만 담는다. 비면 PATCH 가 422 empty_patch 라 보내지 않고 화면에서 알려 준다
  function buildPatch(): KnowledgeBasePatch {
    const kb = q.data!;
    const body: KnowledgeBasePatch = {};
    const display = names.displayName.trim();
    if (display && display !== displayName(kb)) body.display_name = display;
    const next = { size: Number(chunking.size), overlap: Number(chunking.overlap) };
    if (next.size !== kb.chunking.size || next.overlap !== kb.chunking.overlap) body.chunking = next;
    const limits = { max_chunks: Number(chunking.maxChunks) };
    if (limits.max_chunks !== kb.limits.max_chunks) body.limits = limits;
    return body;
  }

  const patch = useMutation({
    mutationFn: (body: KnowledgeBasePatch) => patchKnowledgeBase(id, body),
    onSuccess: async (_kb, body) => {
      setFormNotice(body.chunking ? "savedReindex" : "saved");
      await invalidate();
    },
  });

  // 지워진 Knowledge Base 의 북마크를 따라온 사람에게 빨간 줄 하나만 주지 않는다
  if (missingResource(q.error, q.data)) {
    return <NotFoundPage resource={t.resources.kind["knowledge-base"]} backTo="/knowledge-bases" backLabel={text.backToList} />;
  }
  const gate = queryGate(q);
  if (gate) return gate;
  const kb = q.data!;
  const writesLocked = Boolean(kb.deleting_at);
  const chunkingChanged = Number(chunking.size) !== kb.chunking.size || Number(chunking.overlap) !== kb.chunking.overlap;
  const ds = dataServices.data?.find((d) => d.id === kb.data_service_id) ?? null;

  return (
    <PageWidth data-testid="knowledge-base-detail-page" className="flex flex-col gap-4">
      <PageHeader
        title={displayName(kb)}
        name={secondaryName(kb)}
        testId="page-title"
        actions={
          <>
            <Button variant="secondary" data-testid="knowledge-reindex-all" disabled={writesLocked || reindexAll.isPending || !docs.data?.length} title={text.reindexAllTitle} onClick={() => reindexAll.mutate()}>
              {reindexAll.isPending ? text.requesting : text.reindexAll}
            </Button>
            <Button variant="danger" data-testid="delete-button" onClick={() => setDeleting(true)}>
              {t.common.delete}
            </Button>
          </>
        }
      />
      <KnowledgeBaseDeleteDialog target={deleting ? { id: kb.id, name: kb.name, table: kb.table } : null} onClose={() => setDeleting(false)} onDeleted={() => navigate("/knowledge-bases")} />
      {writesLocked ? (
        <Alert variant="warn" data-testid="kb-deleting-notice">
          {text.deleting(formatDate(t, kb.deleting_at), kb.state === "orphaned_table")}
        </Alert>
      ) : null}
      <KnowledgeErrorMessage error={reindexAll.error} testId="reindex-error" />
      {notice ? (
        <Alert variant="info" data-testid="kb-notice" className="flex items-center justify-between">
          <span>{noticeText(t, notice)}</span>
          <button type="button" className="text-xs underline-offset-2 hover:underline" onClick={() => setNotice(null)}>
            {t.ui.modal.close}
          </button>
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="kb-summary">
          <CardHeader>
            <CardTitle>{t.detail.summaryFields.title}</CardTitle>
            <KnowledgeBaseStateBadge kb={kb} testId="kb-state" />
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
              <dt className="text-muted">{t.detail.summaryFields.id}</dt>
              <dd className="font-mono text-xs">{kb.id}</dd>
              <dt className="text-muted">{text.labels.table}</dt>
              <dd className="font-mono text-xs" data-testid="kb-table">
                {kb.table}
              </dd>
              <dt className="text-muted">{text.labels.embedding}</dt>
              <dd className="font-mono text-xs" data-testid="kb-embedding">
                {embeddingText(kb)}
              </dd>
              <dt className="text-muted">{text.labels.database}</dt>
              <dd data-testid="kb-data-service">
                <Link className="underline-offset-2 hover:underline" to={`/data-services/${kb.data_service_id}`}>
                  {ds ? displayName(ds) : t.ui.names.deleted}
                </Link>
                {ds ? <span className="ml-2 font-mono text-xs text-muted">{ds.host}:{ds.port}/{ds.database ?? ""}</span> : null}
                {!ds && dataServices.isError ? (
                  <span className="ml-2 text-xs text-warn-fg" data-testid="kb-data-service-error">
                    {text.dataServicesError}
                  </span>
                ) : null}
              </dd>
              <dt className="text-muted">{text.labels.documents}</dt>
              <dd data-testid="kb-documents">
                {text.labels.indexed(kb.status.indexed, kb.status.documents)}
                {kb.status.failed ? <span className="ml-1 text-err-fg">{t.shared.failedDocuments(kb.status.failed)}</span> : null}
              </dd>
              <dt className="text-muted">{text.labels.chunks}</dt>
              <dd>{kb.status.chunks}</dd>
              <dt className="text-muted">{text.labels.tokens}</dt>
              <dd data-testid="kb-tokens" title={t.knowledgeBases.tokensHint}>
                {text.tokens(kb.status.tokens)}
              </dd>
              <dt className="text-muted">{text.labels.lastIndexed}</dt>
              <dd>{formatDate(t, kb.status.last_indexed_at)}</dd>
              <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
              <dd>{formatDate(t, kb.updated_at)}</dd>
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{text.labels.edit}</CardTitle>
          </CardHeader>
          <CardContent>
            <form
              className="flex flex-col gap-3"
              data-testid="knowledge-base-edit-form"
              onSubmit={(e) => {
                e.preventDefault();
                const body = buildPatch();
                if (!Object.keys(body).length) {
                  setFormNotice("noChange");
                  return;
                }
                setFormNotice(null);
                patch.mutate(body);
              }}
            >
              <NameFields testId="kb" placeholders={t.knowledgeBases.placeholders} value={names} onChange={setNames} readOnlyName errors={nameFieldErrors(patch.error)} />
              <ChunkingFields value={chunking} onChange={(p) => setChunking((c) => ({ ...c, ...p }))} />
              {chunkingChanged ? (
                <Alert variant="warn" data-testid="kb-chunking-notice">
                  {text.chunkingNotice}
                </Alert>
              ) : null}
              {!isNameFieldError(patch.error) ? <KnowledgeErrorMessage error={patch.error} testId="save-error" /> : null}
              {formNotice ? (
                <Alert variant={formNotice === "noChange" ? "info" : "ok"} data-testid="kb-form-notice">
                  {formNotice === "noChange" ? t.errors.noChange : formNotice === "saved" ? t.shared.saved : t.knowledgeBases.notice.savedReindex}
                </Alert>
              ) : null}
              <div className="flex justify-end">
                <Button type="submit" data-testid="save-button" disabled={patch.isPending || (writesLocked && chunkingChanged)}>
                  {patch.isPending ? t.shared.saving : t.shared.save}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>

      <ReferencesCard resource="knowledge-base" id={kb.id} />

      <KnowledgeDocumentsCard kbId={id} docs={docs} active={active} writesLocked={writesLocked} onNotice={setNotice} />
    </PageWidth>
  );
}
