/**
 * Knowledge Base 화면·캔버스가 같이 쓰는 순수 규칙 (D32). 서버 `store/settings.py` 의 임베딩 모델 표를 따른다. 오류 코드의 문구는
 * 오류 사전(i18n/{ko,en}/errors.ts) 하나이고 Knowledge 의 코드도 거기서 푼다(lib/errorText.ts 의 `knowledgeErrorText`). 그
 * 사전이 읽는 받는 형식은 lib/limits.ts 에 두고 여기서 다시 내보낸다.
 */
import type { BadgeProps } from "@/components/ui/badge";
import type { KnowledgeBase, KnowledgeDocumentState, LLMProvider } from "@/api/types";
import { DOCUMENT_ACCEPT } from "@/lib/limits";

export { DOCUMENT_ACCEPT, DOCUMENT_FORMAT_LABEL, formatLabel } from "@/lib/limits";

/** 문서 인덱싱이 진행 중일 때의 폴링 주기(ms). 배포 화면의 DEPLOY_POLL_MS 와 같은 값이다. */
export const KNOWLEDGE_POLL_MS = 5000;

/** 알려진 임베딩 모델의 차원. 서버 EMBEDDING_DIMENSIONS 와 같다. 모르는 모델은 차원을 직접 적어야 한다(422 dimensions_required). */
export const EMBEDDING_DIMENSIONS: Record<string, number> = {
  "text-embedding-3-small": 1536,
  "text-embedding-3-large": 3072,
  "text-embedding-ada-002": 1536,
  stub: 64,
};

/** kind 별 기본 임베딩 모델. 여기 없는 kind 는 임베딩 provider 로 쓰지 않는다. */
export const DEFAULT_EMBEDDING_MODELS: Partial<Record<LLMProvider["kind"], string>> = { openai: "text-embedding-3-small", stub: "stub" };

export function supportsEmbedding(kind: LLMProvider["kind"] | null | undefined): boolean {
  return kind != null && kind in DEFAULT_EMBEDDING_MODELS;
}

/** provider 가 Knowledge Base 에 쓸 임베딩 모델. 지원하지 않는 kind 는 null 이다. */
export function effectiveEmbeddingModel(p: LLMProvider | null | undefined): string | null {
  if (!p || !supportsEmbedding(p.kind)) return null;
  return p.embedding_model || DEFAULT_EMBEDDING_MODELS[p.kind] || null;
}

export function knownDimensions(model: string | null | undefined): number | null {
  return model ? (EMBEDDING_DIMENSIONS[model] ?? null) : null;
}

export const DEFAULT_CHUNKING = { size: 1200, overlap: 150 } as const;
export const DEFAULT_MAX_CHUNKS = 100000;

/** 문서 하나의 크기 상한(MB). 서버 `daegam/documents/formats.py` 의 MAX_DOCUMENT_BYTES 와 같고 넘으면 413 too_large 다. Knowledge 업로드와 채팅 첨부가 고른 자리에서 미리 잰다. */
export const MAX_DOCUMENT_MB = 10;
export const MAX_DOCUMENT_BYTES = MAX_DOCUMENT_MB * 1024 * 1024;

/** 받는 확장자 하나하나. 채팅 첨부가 고른 파일의 형식을 가를 때 쓴다. */
export const DOCUMENT_EXTENSIONS = DOCUMENT_ACCEPT.split(",");

/** 인덱서가 아직 손댈 문서가 있는 상태. 있을 때만 5초 폴링한다. */
export const ACTIVE_DOCUMENT_STATES: readonly KnowledgeDocumentState[] = ["pending", "indexing", "deleting"];

/** 목록·상세·인스펙터의 Knowledge Base 상태 칩. deleting_at 이 있으면 삭제 중이다. */
export function knowledgeBaseBadge(kb: Pick<KnowledgeBase, "state" | "deleting_at">): { variant: BadgeProps["variant"]; text: string } {
  if (kb.state === "orphaned_table") return { variant: "error", text: "orphaned_table" };
  if (kb.deleting_at) return { variant: "warn", text: "deleting" };
  return { variant: "ok", text: "ready" };
}

export function documentStateVariant(state: KnowledgeDocumentState): BadgeProps["variant"] {
  switch (state) {
    case "indexed":
      return "ok";
    case "failed":
      return "error";
    case "indexing":
      return "info";
    case "deleting":
    case "purging":
      return "warn";
    default:
      return "muted";
  }
}

/** `provider · model · 차원`. 목록·상세·캔버스가 같은 글을 보인다. */
export function embeddingText(kb: Pick<KnowledgeBase, "embedding">): string {
  return `${kb.embedding.provider} · ${kb.embedding.model} · ${kb.embedding.dimensions}`;
}
