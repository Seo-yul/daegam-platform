import type { ReactNode } from "react";

import type { Messages } from "../types";
import { plural } from "./plural";

/** Knowledge Base 의 목록, 폼, 상세, 문서 카드와 삭제 대화상자 (F6). 다시 인덱싱은 reindex 로 적는다. */
export const knowledgeBases: Messages["knowledgeBases"] = {
  placeholders: { display: "Product FAQ", name: "product-faq" },
  tokensHint: "An estimate counted with tiktoken cl100k_base.",
  list: {
    create: "Create Knowledge Base",
    columns: { name: "Name", embedding: "Embedding", documents: "Documents", state: "Status" },
    empty: (tenant: string): string => `Tenant ${tenant} has no Knowledge Bases.`,
    title: "Knowledge Base",
    search: "Search knowledge bases…",
  },
  chunking: {
    sizeHint: (min: number, max: number): string => `Characters (${min}..${max}).`,
    overlapHint: "Characters shared with the end of the previous chunk (0..size/2).",
    maxChunksHint: "Maximum chunks in the whole Knowledge Base.",
    size: "Chunk size",
    overlap: "Overlap",
    maxChunks: "Max chunks",
  },
  form: {
    dataServiceHint: "The Database that holds the vector table. You cannot change it after creation.",
    dataServicesError:
      "Could not load the Database list, so Postgres Databases you registered do not appear here. Reload the page and try again.",
    noPostgres: "This tenant has no Postgres Database. Register one in Tenant › Database first.",
    pgvectorMissing:
      "The last connection check of this Database did not find the pgvector extension. A superuser must run CREATE EXTENSION vector before you can create a Knowledge Base on it.",
    pgvectorAvailable:
      "The pgvector extension is not installed in this Database yet. Creating the Knowledge Base tries CREATE EXTENSION vector, which fails at that point if this account is not a superuser.",
    unverified: (link: ReactNode): ReactNode => (
      <>The connection to this Database has not been checked yet. Select "Check connection" on its {link} first to see here whether pgvector is available.</>
    ),
    unverifiedLink: "details page",
    providerHint: "Only providers in Platform Settings with an embeddings API (the openai family) appear. You cannot change it after creation.",
    settingsError: "Could not load the provider list, so providers you registered do not appear here. Reload the page and try again.",
    noProvider: "No provider supports embeddings. Register an openai provider in Platform Settings.",
    modelHint: "Leave empty to use the provider's embedding model.",
    dimensionsRequired: "Unknown model. Enter the dimensions.",
    dimensionsDefault: (dimensions: number): string => `Leave empty for ${dimensions}.`,
    dimensionsPick: "Filled in when you select a model.",
    creating: "Creating…",
    title: "New Knowledge Base",
    dataService: "Database (Postgres)",
    provider: "Embedding provider",
    model: "Embedding model",
    dimensions: "Dimensions",
  },
  detail: {
    backToList: "Back to Knowledge Bases",
    reindexAllTitle: "Sets every document back to pending.",
    requesting: "Requesting…",
    reindexAll: "Reindex all",
    deleting: (at: string, orphaned: boolean): string =>
      `This Knowledge Base is being deleted (${at}). You cannot add documents or reindex.${orphaned ? ' The vector table could not be dropped and remains. Select "Delete" again to retry the same steps.' : ""}`,
    dataServicesError: "Could not load the Database list, so its name is unknown.",
    tokens: (tokens: number): string => `${tokens} (est.)`,
    chunkingNotice: "Changing chunking reindexes every document when you save. Saving is refused while documents are being indexed (409 indexing).",
    labels: {
      table: "table",
      embedding: "embedding",
      database: "Database",
      documents: "documents",
      indexed: (indexed: number, total: number): string => `${indexed}/${total} indexed`,
      chunks: "chunks",
      tokens: "tokens",
      lastIndexed: "last indexed",
      edit: "Edit",
    },
  },
  notice: {
    reindexAll: (count: number): string => `Reindexing ${plural(count, "document", "documents")}.`,
    uploaded: (name: string): string => `Uploaded "${name}". The indexer will process it shortly.`,
    added: (name: string): string => `Added "${name}". The indexer will fetch the URL and process it.`,
    reindexDoc: (name: string): string => `Reindexing "${name}".`,
    savedReindex: "Saved. Chunking changed, so every document is being reindexed.",
  },
  documents: {
    limits: (formats: string, mb: number, max: number): string => `${formats}, ${mb}MB per document, up to ${plural(max, "document", "documents")} per Knowledge Base`,
    polling: (seconds: number): string => ` · Refreshes every ${plural(seconds, "second", "seconds")} until indexing finishes`,
    upload: "Upload file",
    uploadHint: "Uploads as soon as you select a file.",
    sizeLimit: (mb: number): string => `${mb}MB per document`,
    splitAdvice: "Split it into smaller files.",
    uploading: (name: string): string => `Uploading "${name}"…`,
    addUrl: "Add URL",
    urlHint: "Only public addresses work. Private and cluster addresses are rejected.",
    nameHint: "Leave empty to use the last part of the URL.",
    adding: "Adding…",
    stale: "Could not refresh the document list. The list below is the last one read and may be out of date.",
    failedNotice: 'Failed documents are not retried automatically. Fix the problem, then select "Reindex".',
    columns: { name: "Name", source: "Source", size: "Size", state: "Status", chunks: "Chunks", tokens: "Tokens (est.)", error: "Error" },
    empty: "No documents yet. Upload a file or add a URL.",
    reindex: "Reindex",
    deleteAgain: "Delete again",
    deleteTitle: "Delete document",
    deleteBody: "Its rows in the vector table and its text are deleted. A worker does the deletion, so it shows as deleting for a moment.",
    title: "Documents",
  },
  deleteDialog: {
    dropTable: (table: string): string => `Drops the ${table} table in the Database.`,
    tableNotDropped: (warning: string): string =>
      `The vector table could not be dropped, so the Knowledge Base remains (${warning}). Check that the Database is reachable, then select "Delete" again.`,
  },
};
