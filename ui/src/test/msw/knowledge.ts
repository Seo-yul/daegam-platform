/**
 * Knowledge Base 픽스처와 핸들러 (/knowledge-bases, D32). 응답 모양은 platform/daegam/api/knowledge.py 를 따른다.
 *  - kb-faq: ready. 문서 셋(indexed·pending·failed). DELETE 는 상담 에이전트가 쓰고 있어 409 in_use 다.
 *  - kb-orphan: 삭제가 DROP 에서 실패해 orphaned_table 로 남은 것(deleting_at 있음, 문서 0). DELETE 는 200 이다.
 * 상태는 knowledgeState 에 있다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { KnowledgeBase, KnowledgeDocument } from "@/api/types";
import { DOCUMENT_ACCEPT, EMBEDDING_DIMENSIONS } from "@/lib/knowledge";

import { NOW, apiError, docStore, http, record } from "./core";

export const kbFaq: KnowledgeBase = {
  id: "kb-faq",
  tenant: "default",
  name: "faq",
  display_name: "제품 FAQ",
  data_service_id: "ds-pg",
  embedding: { provider: "openai-main", model: "text-embedding-3-small", dimensions: 1536 },
  chunking: { size: 1200, overlap: 150 },
  limits: { max_chunks: 100000 },
  table: "daegam_kb_0123456789abcdef0123456789abcdef",
  state: "ready",
  deleting_at: null,
  status: { documents: 3, indexed: 1, failed: 1, chunks: 12, tokens: 3400, last_indexed_at: NOW },
  created_at: NOW,
  updated_at: NOW,
  updated_by: null,
};

export const kbOrphan: KnowledgeBase = {
  id: "kb-orphan",
  tenant: "default",
  name: "old-notes",
  display_name: "old-notes",
  data_service_id: "ds-pg",
  embedding: { provider: "openai-main", model: "text-embedding-3-large", dimensions: 3072 },
  chunking: { size: 800, overlap: 100 },
  limits: { max_chunks: 5000 },
  table: "daegam_kb_fedcba9876543210fedcba9876543210",
  state: "orphaned_table",
  deleting_at: NOW,
  status: { documents: 0, indexed: 0, failed: 0, chunks: 0, tokens: 0, last_indexed_at: null },
  created_at: NOW,
  updated_at: NOW,
  updated_by: null,
};

function doc(over: Partial<KnowledgeDocument> & Pick<KnowledgeDocument, "id" | "name" | "state">): KnowledgeDocument {
  return {
    knowledge_base_id: "kb-faq",
    tenant: "default",
    source: { kind: "upload", url: null },
    content_type: "text/markdown",
    bytes: 2048,
    sha256: "deadbeef",
    chunks: 0,
    usage: { tokens: 0 },
    error: null,
    attempts: 0,
    lease: null,
    created_at: NOW,
    updated_at: NOW,
    indexed_at: null,
    updated_by: null,
    ...over,
  };
}

export function knowledgeDocumentFixtures(): KnowledgeDocument[] {
  return [
    doc({ id: "doc-guide", name: "guide.md", state: "indexed", chunks: 12, usage: { tokens: 3400 }, indexed_at: NOW }),
    doc({ id: "doc-faq-url", name: "faq.html", state: "pending", source: { kind: "url", url: "https://example.com/faq.html" }, content_type: "text/html", bytes: 0, sha256: null }),
    doc({ id: "doc-broken", name: "broken.pdf", state: "failed", content_type: "application/pdf", bytes: 512000, attempts: 1, error: { code: "extract_failed", detail: "PdfReadError" } }),
  ];
}

/** `seq` 는 올린 문서 id(`doc-up-N`·`doc-url-N`)의 번호다. */
export const knowledgeState: { items: KnowledgeBase[]; documents: Record<string, KnowledgeDocument[]>; seq: number } = {
  items: [kbFaq, kbOrphan],
  documents: { "kb-faq": knowledgeDocumentFixtures(), "kb-orphan": [] },
  seq: 0,
};

export function resetKnowledgeState(): void {
  knowledgeState.items = [{ ...kbFaq }, { ...kbOrphan }];
  knowledgeState.documents = { "kb-faq": knowledgeDocumentFixtures(), "kb-orphan": [] };
  knowledgeState.seq = 0;
}

function statusOf(kbId: string): KnowledgeBase["status"] {
  const rows = knowledgeState.documents[kbId] ?? [];
  const indexed = rows.filter((d) => d.state === "indexed");
  return {
    documents: rows.length,
    indexed: indexed.length,
    failed: rows.filter((d) => d.state === "failed").length,
    chunks: rows.reduce((n, d) => n + d.chunks, 0),
    tokens: rows.reduce((n, d) => n + d.usage.tokens, 0),
    last_indexed_at: indexed.length ? NOW : null,
  };
}

function summary(kb: KnowledgeBase): KnowledgeBase {
  return { ...kb, status: statusOf(kb.id) };
}

const knowledgeStore = docStore(knowledgeState, "knowledge base");

export const knowledgeHandlers: HttpHandler[] = [
  http.get("/knowledge-bases", async ({ request }) => {
    await record(request);
    return HttpResponse.json(knowledgeState.items.map(summary));
  }),
  http.post("/knowledge-bases", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { name: string; display_name?: string | null; data_service_id: string; embedding: { provider: string; model?: string | null; dimensions?: number | null }; chunking?: KnowledgeBase["chunking"] | null; limits?: KnowledgeBase["limits"] | null };
    const model = body.embedding.model ?? "text-embedding-3-small";
    const dimensions = body.embedding.dimensions ?? EMBEDDING_DIMENSIONS[model];
    if (!dimensions) return apiError(422, "dimensions_required", `unknown embedding model ${model}; dimensions is required`);
    const kb: KnowledgeBase = {
      ...kbFaq,
      id: "kb-new",
      name: body.name,
      display_name: body.display_name ?? body.name,
      data_service_id: body.data_service_id,
      embedding: { provider: body.embedding.provider, model, dimensions },
      chunking: body.chunking ?? { size: 1200, overlap: 150 },
      limits: body.limits ?? { max_chunks: 100000 },
      table: "daegam_kb_00000000000000000000000000000000",
      status: { documents: 0, indexed: 0, failed: 0, chunks: 0, tokens: 0, last_indexed_at: null },
    };
    knowledgeState.items.push(kb);
    knowledgeState.documents["kb-new"] = [];
    return HttpResponse.json(summary(kb), { status: 201 });
  }),
  http.get("/knowledge-bases/:id", async ({ request, params }) => {
    await record(request);
    const kb = knowledgeStore.findOr404(params.id);
    return kb instanceof Response ? kb : HttpResponse.json(summary(kb));
  }),
  http.patch("/knowledge-bases/:id", async ({ request, params }) => {
    const r = await record(request);
    const kb = knowledgeStore.findOr404(params.id);
    if (kb instanceof Response) return kb;
    const body = r.body as Partial<Pick<KnowledgeBase, "display_name" | "chunking" | "limits">>;
    if (body.chunking && kb.deleting_at) return apiError(409, "deleting", "knowledge base is being deleted");
    if (body.chunking) {
      const rows = knowledgeState.documents[kb.id] ?? [];
      if (rows.some((d) => d.state === "indexing")) return apiError(409, "indexing", "a document is being indexed");
      for (const d of rows) if (d.state !== "deleting" && d.state !== "purging") d.state = "pending";
    }
    Object.assign(kb, body, { updated_by: "header-admin" });
    return HttpResponse.json(summary(kb));
  }),
  http.delete("/knowledge-bases/:id", async ({ request, params }) => {
    await record(request);
    const id = String(params.id);
    // 상담 에이전트(ag-support)가 이 Knowledge Base 를 쓴다는 설정. 참조 409 의 표본이다
    if (id === "kb-faq") return apiError(409, "in_use", "knowledge base is referenced by agents", { agent_ids: ["ag-support"] });
    const kb = knowledgeStore.findOr404(id);
    if (kb instanceof Response) return kb;
    knowledgeStore.remove(id);
    delete knowledgeState.documents[id];
    return HttpResponse.json({ id, deleted: true, table_dropped: true });
  }),
  http.get("/knowledge-bases/:id/documents", async ({ request, params }) => {
    await record(request);
    const id = String(params.id);
    const kb = knowledgeStore.findOr404(id);
    if (kb instanceof Response) return kb;
    return HttpResponse.json(knowledgeState.documents[id] ?? []);
  }),
  http.post("/knowledge-bases/:id/documents", async ({ request, params }) => {
    const r = await record(request);
    const id = String(params.id);
    const kb = knowledgeStore.findOr404(id);
    if (kb instanceof Response) return kb;
    if (kb.deleting_at) return apiError(409, "deleting", "knowledge base is being deleted");
    const file = (r.body as { file?: { name: string; text: string } } | null)?.file;
    if (!file) return apiError(422, "validation_error", "file is required");
    if (!DOCUMENT_ACCEPT.split(",").some((ext) => file.name.toLowerCase().endsWith(ext))) return apiError(422, "unsupported_format", `unsupported document ${file.name}`);
    const created = doc({ id: `doc-up-${++knowledgeState.seq}`, knowledge_base_id: id, name: file.name, state: "pending", bytes: file.text.length, content_type: "text/plain" });
    (knowledgeState.documents[id] ??= []).push(created);
    return HttpResponse.json(created, { status: 201 });
  }),
  http.post("/knowledge-bases/:id/documents/url", async ({ request, params }) => {
    const r = await record(request);
    const id = String(params.id);
    const kb = knowledgeStore.findOr404(id);
    if (kb instanceof Response) return kb;
    if (kb.deleting_at) return apiError(409, "deleting", "knowledge base is being deleted");
    const body = r.body as { url: string; name?: string | null };
    if (!/^https?:\/\//.test(body.url)) return apiError(422, "url_scheme", "only http(s) urls are allowed");
    const created = doc({ id: `doc-url-${++knowledgeState.seq}`, knowledge_base_id: id, name: body.name || body.url.split("/").filter(Boolean).pop() || "document", state: "pending", source: { kind: "url", url: body.url }, bytes: 0, sha256: null, content_type: "" });
    (knowledgeState.documents[id] ??= []).push(created);
    return HttpResponse.json(created, { status: 201 });
  }),
  http.post("/knowledge-bases/:id/reindex", async ({ request, params }) => {
    await record(request);
    const id = String(params.id);
    const kb = knowledgeStore.findOr404(id);
    if (kb instanceof Response) return kb;
    if (kb.deleting_at) return apiError(409, "deleting", "knowledge base is being deleted");
    const rows = knowledgeState.documents[id] ?? [];
    if (rows.some((d) => d.state === "indexing")) return apiError(409, "indexing", "a document is being indexed");
    let n = 0;
    for (const d of rows) {
      if (d.state === "deleting" || d.state === "purging") continue;
      d.state = "pending";
      n += 1;
    }
    return HttpResponse.json({ id, pending: n });
  }),
  http.get("/knowledge-bases/:id/documents/:doc", async ({ request, params }) => {
    await record(request);
    const d = (knowledgeState.documents[String(params.id)] ?? []).find((x) => x.id === params.doc);
    return d ? HttpResponse.json(d) : apiError(404, "not_found", `document ${String(params.doc)} not found`);
  }),
  http.post("/knowledge-bases/:id/documents/:doc/reindex", async ({ request, params }) => {
    await record(request);
    const kb = knowledgeStore.find(params.id);
    const d = (knowledgeState.documents[String(params.id)] ?? []).find((x) => x.id === params.doc);
    if (!kb || !d) return apiError(404, "not_found", `document ${String(params.doc)} not found`);
    if (kb.deleting_at) return apiError(409, "deleting", "knowledge base is being deleted");
    if (d.state === "indexing") return apiError(409, "indexing", `document ${d.name} is being indexed`);
    d.state = "pending";
    d.error = null;
    return HttpResponse.json(d);
  }),
  http.delete("/knowledge-bases/:id/documents/:doc", async ({ request, params }) => {
    await record(request);
    const d = (knowledgeState.documents[String(params.id)] ?? []).find((x) => x.id === params.doc);
    if (!d) return apiError(404, "not_found", `document ${String(params.doc)} not found`);
    if (d.state === "indexing") return apiError(409, "indexing", `document ${d.name} is being indexed`);
    d.state = "deleting";
    return HttpResponse.json({ id: d.id, state: "deleting" });
  }),
];
