/**
 * 채팅 첨부의 규칙 (P36 9절). 입력칸이 받는 형식, 업로드 전에 거르는 것, 칩에 보이는 글, 전달 확인을 한 곳에서 정한다.
 *
 * 받는 형식은 서버 `daegam/documents/formats.py` 의 표다(platform/tests/test_chat_parity.py). 문서는 Knowledge Base 와 같은
 * `DOCUMENT_ACCEPT` 를 쓰고 형식 문구도 거기서 만든다. 문서는 런타임의 파일 도구로 읽으므로 상태의 `chat.documents` 가 거짓이면
 * 받지 않고, 이미지는 `chat.images` 가 거짓이면 받지 않는다. 받지 않는 파일과 상한을 넘는 문서는 업로드하지 않고 그 까닭을 칩에
 * 적는다. 이미지의 크기는 다시 구운 뒤에 잰다. 까닭과 상한 안내는 문장 대신 코드와 수로 들고, 문장은 사전(chat.attachments)에
 * 있다.
 *
 * 칩의 사실은 두 곳에서 온다. 입력칸의 칩은 업로드 응답(`AgentUpload`)이고, 보낸 말풍선의 칩은 전사의 사용자 항목
 * `attachments[{name, kind, bytes, pages?, sheets?, truncated?}]` 다. 전사의 `kind` 는 런타임 이름(`text`·`image`)이라 여기서
 * 화면 이름(`document`·`image`)으로 맞춘다.
 */
import type { AgentUpload } from "@/api/types";
import type { Messages } from "@/i18n";
import type { ChatEvent, DoneEvent } from "@/lib/chatStream";
import { DOCUMENT_ACCEPT, DOCUMENT_EXTENSIONS, DOCUMENT_FORMAT_LABEL, MAX_DOCUMENT_BYTES, formatLabel } from "@/lib/knowledge";
import { finiteNumber } from "@/lib/json";
import { RUNTIME_ATTACHMENTS, type ChatFeatures } from "@/lib/runtimeFeatures";
import { formatBytes, formatNumber, splitExtension } from "@/lib/utils";

/** 이미지 첨부가 받는 확장자. 서버 formats.py 의 IMAGE_EXTENSIONS 와 같다. */
export const IMAGE_ACCEPT = ".gif,.jpeg,.jpg,.png,.webp";
/** 런타임이 상한을 알리지 않았을 때의 한 턴 첨부 수, 이미지 수, 이미지 하나의 바이트 상한. 런타임 attachments.py 의 기본값이다. */
export const DEFAULT_MAX_ATTACHMENTS = 5;
export const DEFAULT_MAX_IMAGES = 4;
export const DEFAULT_MAX_IMAGE_BYTES = 2_097_152;

export type AttachmentKind = AgentUpload["kind"];

/**
 * 첨부하지 못한 까닭. 칩은 문장 대신 이것을 든 `AttachmentRefused` 를 들고, 그릴 때 `refusalText` 가 화면 언어의 문장을 만든다.
 * 이 에이전트가 받지 않는 종류(`images_unsupported`, `attachments_need_filesystem`)는 런타임이 보낼 때 낼 오류 코드라 오류 사전의
 * 문장을 쓴다. `image_unreadable` 은 이미지를 풀지 못한 것이고 `image_not_resized` 는 풀었지만 줄여 굽지 못한 것이다.
 */
export type AttachmentRefusal =
  | { code: "unsupported" | "image_unreadable" | "image_not_resized" | "images_unsupported" | "attachments_need_filesystem" }
  | { code: "too_large"; kind: AttachmentKind; limit: number };

/** 거절을 오류로 든 것. 업로드를 준비하다 거절하면 던지고, 칩은 받은 오류를 그대로 든다(components/chat/items.ts describeChatError). */
export class AttachmentRefused extends Error {
  readonly refusal: AttachmentRefusal;

  constructor(refusal: AttachmentRefusal) {
    super(refusal.code);
    this.name = "AttachmentRefused";
    this.refusal = refusal;
  }
}

/** 거절의 문장. */
export function refusalText(t: Messages, refusal: AttachmentRefusal): string {
  const text = t.chat.attachments;
  switch (refusal.code) {
    case "unsupported":
      return text.unsupported(DOCUMENT_FORMAT_LABEL, formatLabel(IMAGE_ACCEPT));
    case "image_unreadable":
      return text.imageUnreadable;
    case "image_not_resized":
      return text.imageNotResized;
    case "too_large":
      return (refusal.kind === "image" ? text.imageTooLarge : text.documentTooLarge)(formatBytes(refusal.limit));
    default:
      return t.errors.codes[refusal.code];
  }
}

/** 칩 하나가 보이는 사실. */
export interface AttachmentFacts {
  name: string;
  kind: AttachmentKind;
  bytes: number;
  pages?: number | null;
  sheets?: number | null;
  /** 뽑은 글의 글자 수. 업로드 응답에만 있다. */
  textChars?: number | null;
  /** 뽑은 글이 50만 자에서 잘렸다. */
  truncated?: boolean;
}

const IMAGE_EXTENSIONS = IMAGE_ACCEPT.split(",");
const IMAGE_MEDIA_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

/** 확장자가 먼저이고, 확장자가 없거나 모르면 이미지의 media type 으로 가른다. 받지 않는 형식이면 null 이다. */
export function attachmentKind(file: Pick<File, "name" | "type">): AttachmentKind | null {
  const ext = splitExtension(file.name).ext.toLowerCase();
  if (IMAGE_EXTENSIONS.includes(ext)) return "image";
  if (DOCUMENT_EXTENSIONS.includes(ext)) return "document";
  return IMAGE_MEDIA_TYPES.includes(file.type) ? "image" : null;
}

/** 이 에이전트가 첨부를 받는가. 런타임이 `attachments` 를 알리고 문서나 이미지 가운데 하나라도 받아야 한다. */
export function takesAttachments(features: ChatFeatures): boolean {
  return features.has(RUNTIME_ATTACHMENTS) && (features.documents || features.images);
}

/** 파일 선택 창의 accept 다. 받는 종류만 싣는다. */
export function attachmentAccept(features: Pick<ChatFeatures, "documents" | "images">): string {
  return [features.documents ? DOCUMENT_ACCEPT : "", features.images ? IMAGE_ACCEPT : ""].filter(Boolean).join(",");
}

/** 파일 하나의 업로드 상한(바이트). 문서는 서버의 문서 상한, 이미지는 런타임이 알린 이미지 상한이다. */
export function byteLimit(kind: AttachmentKind, features: Pick<ChatFeatures, "limits">): number {
  return kind === "image" ? (features.limits?.max_image_bytes ?? DEFAULT_MAX_IMAGE_BYTES) : MAX_DOCUMENT_BYTES;
}

/** 한 턴 상한을 넘어 첨부하지 않은 파일. 입력칸이 문장 대신 이것을 들고 그릴 때 사전(chat.attachments.overLimit)으로 적는다. */
export interface OverLimit {
  maxFiles: number;
  maxImages: number;
  dropped: number;
}

/** 업로드하기 전에 거르는 까닭. 받으면 null 이다. 이미지의 크기는 다시 구운 뒤에 재므로 여기서 보지 않는다. */
function refusal(file: File, kind: AttachmentKind | null, features: Pick<ChatFeatures, "documents" | "images">): AttachmentRefused | null {
  if (kind === null) return new AttachmentRefused({ code: "unsupported" });
  if (kind === "image") return features.images ? null : new AttachmentRefused({ code: "images_unsupported" });
  if (!features.documents) return new AttachmentRefused({ code: "attachments_need_filesystem" });
  return file.size > MAX_DOCUMENT_BYTES ? new AttachmentRefused({ code: "too_large", kind, limit: MAX_DOCUMENT_BYTES }) : null;
}

/** 넣을 파일 하나. `refusal` 이 있으면 업로드하지 않고 오류 칩이 된다. */
export interface Admitted {
  file: File;
  kind: AttachmentKind | null;
  refusal: AttachmentRefused | null;
}

/**
 * 한 턴 상한 안에서 넣을 파일과 넣지 못한 파일. 이미 있는 칩(`present` 는 그 종류)의 수와 그 가운데 이미지 수를 뺀 자리만큼
 * 앞에서부터 넣는다. 받지 않는 형식은 문서 자리를 쓴다.
 */
export function admit(files: File[], present: AttachmentKind[], features: Pick<ChatFeatures, "documents" | "images" | "limits">): { admitted: Admitted[]; overLimit: OverLimit | null } {
  const maxFiles = features.limits?.max_attachments_per_turn ?? DEFAULT_MAX_ATTACHMENTS;
  const maxImages = features.limits?.max_images_per_turn ?? DEFAULT_MAX_IMAGES;
  let room = maxFiles - present.length;
  let imageRoom = maxImages - present.filter((kind) => kind === "image").length;
  const admitted: Admitted[] = [];
  for (const file of files) {
    const kind = attachmentKind(file);
    if (room <= 0 || (kind === "image" && imageRoom <= 0)) continue;
    room -= 1;
    if (kind === "image") imageRoom -= 1;
    admitted.push({ file, kind, refusal: refusal(file, kind, features) });
  }
  const dropped = files.length - admitted.length;
  return { admitted, overLimit: dropped ? { maxFiles, maxImages, dropped } : null };
}

/** 칩의 사실 한 줄. "12쪽 · 38,211자", 시트면 "시트 3개", 이미지면 크기다. */
export function factsText(t: Messages, facts: AttachmentFacts): string {
  if (facts.kind === "image") return formatBytes(facts.bytes);
  const text = t.chat.attachments;
  const parts = [facts.pages ? text.pages(facts.pages) : facts.sheets ? text.sheets(facts.sheets) : null, facts.textChars != null ? text.chars(formatNumber(t, facts.textChars)) : null];
  return parts.filter(Boolean).join(" · ");
}

export function uploadFacts(upload: AgentUpload): AttachmentFacts {
  return { name: upload.name, kind: upload.kind, bytes: upload.bytes, pages: upload.pages, sheets: upload.sheets, textChars: upload.text_chars, truncated: upload.truncated };
}

/** 보낸 말풍선에 남길 사실. 전사가 드는 칸만 남겨 새로고침한 뒤와 같은 줄을 보인다. 글자 수는 전사에 없다. */
export function sentFacts({ name, kind, bytes, pages, sheets, truncated }: AttachmentFacts): AttachmentFacts {
  return { name, kind, bytes, pages, sheets, truncated };
}

/** 전사 사용자 항목의 `attachments` 를 칩의 사실로. 모양이 틀린 항목은 버린다. */
export function transcriptAttachments(raw: unknown): AttachmentFacts[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((a): a is Record<string, unknown> => Boolean(a) && typeof a === "object" && typeof (a as { name?: unknown }).name === "string")
    .map((a) => ({
      name: a.name as string,
      kind: a.kind === "image" ? "image" : "document",
      bytes: finiteNumber(a.bytes) ?? 0,
      pages: finiteNumber(a.pages),
      sheets: finiteNumber(a.sheets),
      truncated: a.truncated === true,
    }));
}

/** 보낸 첨부가 전달되지 않았는가. `done.attachments_received` 가 보낸 수와 다르면 옛 런타임이 버렸거나 세지 못한 것이다. */
export function attachmentsLost(sent: number, ev: ChatEvent): boolean {
  return sent > 0 && ev.event === "done" && (ev.data as DoneEvent).attachments_received !== sent;
}
