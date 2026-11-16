/**
 * 채팅 첨부의 업로드 핸들러 (P36 9절). 플랫폼 `api/agent_chat.py` 의 두 라우트를 흉내 낸다. `POST /agents/{id}/uploads` 는
 * multipart 파일 하나를 받아 업로드 한 건을 201 로 돌려주고, `DELETE /agents/{id}/uploads/{upload_id}` 는 늘 204 다. 기록에
 * `attachments` 가 없으면 플랫폼처럼 409 runtime_feature_missing 이다. 받은 업로드는 `agentUploadState.items` 에 남는다.
 *
 * 문서는 이름으로 결과를 정한다. `.pdf` 는 12쪽, 이름이 "스캔" 으로 시작하면 422 empty_document, "대용량" 으로 시작하면 50만 자에서
 * 잘린 글이다. 이미지는 받은 크기 그대로다. 형식과 한 턴 상한은 화면의 규칙(lib/attachments.ts)을 그대로 쓴다.
 */
import type { HttpHandler } from "msw";

import type { Agent, AgentUpload } from "@/api/types";
import { DEFAULT_MAX_ATTACHMENTS, DEFAULT_MAX_IMAGE_BYTES, DEFAULT_MAX_IMAGES, attachmentKind } from "@/lib/attachments";
import { RUNTIME_ATTACHMENTS, RUNTIME_CANCEL } from "@/lib/runtimeFeatures";
import { splitExtension } from "@/lib/utils";

import { agentStore, agentWithRuntimeFeatures, featureMissing } from "./agents";
import { HttpResponse, apiError, docStore, http, record } from "./core";

/** 런타임 `/metrics` 의 첨부 상한 기본값(agent-runtime/runtime/attachments.py)이다. 화면이 같은 기본값을 가진 칸은 그 값을 쓴다. */
export const CHAT_LIMITS = {
  max_attachments_per_turn: DEFAULT_MAX_ATTACHMENTS,
  max_images_per_turn: DEFAULT_MAX_IMAGES,
  max_session_images: 8,
  max_image_bytes: DEFAULT_MAX_IMAGE_BYTES,
  max_session_bytes: 8 * 1024 * 1024,
};

const EXTRACTED_CHARS = 38211;
const TRUNCATED_CHARS = 500000;
const PDF_PAGES = 12;

/**
 * 받은 업로드와 지운 id. `resetAgentUploadState` 는 id 의 번호(`seq`)만은 되돌리지 않는다. 앞 테스트의 패널이 사라지며 늦게 보낸
 * 삭제가 다음 테스트의 업로드와 같은 id 를 가리키지 않게 하려는 것이다.
 */
export const agentUploadState: { items: AgentUpload[]; deleted: string[]; seq: number } = { items: [], deleted: [], seq: 0 };

const agentUploadStore = docStore(agentUploadState, "upload");

export function resetAgentUploadState(): void {
  agentUploadState.items = [];
  agentUploadState.deleted = [];
}

/**
 * 첨부를 아는 런타임에 배포한 Agent 다. `chat` 은 상태의 받는 첨부이고 기본은 문서와 이미지를 모두 받는다. `features` 는 그 런타임이
 * 함께 알린 다른 능력이다.
 */
export function agentWithAttachments(
  chat: { documents?: boolean; images?: boolean; attachments_limit_fetch?: boolean } = {},
  ...features: string[]
): Agent {
  const agent = agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_ATTACHMENTS, ...features);
  const facts = { documents: chat.documents ?? true, images: chat.images ?? true, attachments_limit_fetch: chat.attachments_limit_fetch ?? false, limits: CHAT_LIMITS };
  return { ...agent, status: agent.status && { ...agent.status, chat: facts } };
}

function documentFacts(name: string): Pick<AgentUpload, "text_chars" | "pages" | "truncated"> {
  if (name.startsWith("대용량")) return { text_chars: TRUNCATED_CHARS, pages: null, truncated: true };
  return { text_chars: EXTRACTED_CHARS, pages: name.endsWith(".pdf") ? PDF_PAGES : null, truncated: false };
}

function uploaded(file: { name: string; text: string }): AgentUpload {
  const image = attachmentKind({ name: file.name, type: "" }) === "image";
  const base: AgentUpload = {
    id: `up-${++agentUploadState.seq}`,
    name: file.name,
    kind: image ? "image" : "document",
    format: splitExtension(file.name).ext.slice(1) || "text",
    bytes: file.text.length,
    text_chars: null,
    pages: null,
    sheets: null,
    truncated: false,
    expires_at: new Date(Date.now() + 3_600_000).toISOString(),
  };
  return image ? base : { ...base, ...documentFacts(file.name) };
}

export const agentUploadHandlers: HttpHandler[] = [
  http.post("/agents/:id/uploads", async ({ request, params }) => {
    const r = await record(request);
    const agent = agentStore.findOr404(params.id);
    if (agent instanceof Response) return agent;
    const missing = featureMissing(agent, RUNTIME_ATTACHMENTS);
    if (missing) return missing;
    const file = (r.body as { file?: { name: string; text: string } }).file;
    if (!file) return apiError(422, "validation_error", "file is required");
    if (file.name.startsWith("스캔")) return apiError(422, "empty_document", "no text could be extracted; a scanned document has no text layer");
    const upload = uploaded(file);
    agentUploadState.items.push(upload);
    return HttpResponse.json(upload, { status: 201 });
  }),
  http.delete("/agents/:id/uploads/:uploadId", async ({ request, params }) => {
    await record(request);
    const id = String(params.uploadId);
    agentUploadStore.remove(id);
    agentUploadState.deleted.push(id);
    return new HttpResponse(null, { status: 204 });
  }),
];
