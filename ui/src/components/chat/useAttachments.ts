/**
 * 입력칸의 첨부 (P36 9절). 파일을 선택하거나 끌어 놓거나 붙여 넣는 순간 업로드하고, 보낼 때는 다 된 칩의 업로드 id 만 싣는다.
 *
 * 업로드는 한 번에 하나씩 차례로 한다. 서버가 사람마다 동시 추출을 하나로 묶어(429 upload_busy) 한꺼번에 보내면 둘째부터
 * 거절되기 때문이다. 이미지는 업로드하기 전에 상한 안으로 다시 굽고(lib/imageResize.ts) 썸네일을 `data:` URL 로 읽는다. 받지 않는
 * 형식, 상한을 넘는 파일, 실패한 업로드는 오류 칩으로 남아 사람이 삭제할 때까지 보내기를 잠근다. 업로드 중인 칩도 보내기를 잠근다.
 * 업로드를 마치거나 실패하면 낭독기에 알린다.
 *
 * 한 턴의 상한(파일 수, 이미지 수)을 넘는 파일은 칩을 만들지 않고 안내 한 줄로 알린다. 칩을 버리면(삭제, 대화 바꾸기, 패널이
 * 사라지거나 에이전트가 바뀜) 다 된 업로드는 서버에서 지우고, 줄에 선 것은 시작하지 않으며, 업로드 중인 것은 끝난 뒤에 지운다.
 * 남기면 1시간 동안 그 사람의 쓰지 않은 업로드 상한을 차지한다. 업로드 요청을 중간에 끊지는 않는다. 서버는 끊긴 요청도 끝까지
 * 읽어 업로드를 만들 수 있는데, 그러면 id 를 몰라 지울 수 없다.
 *
 * 보낼 때는 `take` 가 다 된 칩을 잡아(보내는 중에는 삭제할 수 없다) id 를 준다. 스트림이 열리면 서버가 그 업로드를 지웠으므로
 * `settle(ids, true)` 가 보낸 칩만 비우고, 거절되면 `settle(ids, false)` 가 잡은 칩을 놓는다. 보내는 사이에 새로 첨부한 칩은 남는다.
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

import { deleteChatAttachment, uploadChatAttachment } from "@/api/agentChat";
import type { ChatBase } from "@/api/client";
import { useAnnounce } from "@/components/ui/live-region";
import { useT } from "@/i18n";
import {
  AttachmentRefused,
  admit,
  attachmentAccept,
  byteLimit,
  sentFacts,
  takesAttachments,
  uploadFacts,
  type AttachmentFacts,
  type AttachmentKind,
  type OverLimit,
} from "@/lib/attachments";
import { ImageDecodeError, rebakeImage } from "@/lib/imageResize";
import type { ChatFeatures } from "@/lib/runtimeFeatures";
import { readDataUrl } from "@/lib/utils";

import type { ComposerAttach } from "./AttachControls";
import type { AttachmentChipView } from "./AttachmentChips";
import { describeChatError } from "./items";

/** 입력칸의 칩. 다 된 칩은 업로드 id 를 든다. */
interface ComposerChip extends AttachmentChipView {
  uploadId?: string;
}

/** 보낼 첨부. 업로드 id 와 보낸 말풍선에 남길 사실이다. */
export interface Outgoing {
  ids: string[];
  facts: AttachmentFacts[];
}

const NOTHING: Outgoing = { ids: [], facts: [] };

export interface ChatAttachments {
  /** 이 에이전트가 첨부를 받는가. 거짓이면 클립 단추가 없다. */
  enabled: boolean;
  accept: string;
  chips: ComposerChip[];
  /** 한 턴 상한을 넘어 넣지 못한 파일. 안내 문장은 그릴 때 만든다. */
  overLimit: OverLimit | null;
  /** 업로드 중이거나 실패한 칩이 있어 보낼 수 없다. */
  blocking: boolean;
  /** 낭독기에 알릴 말과 몇 번째 알림인지. 같은 말을 다시 알릴 때 수를 열쇠로 쓴다. */
  announcement: { message: string; count: number };
  addFiles: (files: File[]) => void;
  remove: (key: string) => void;
  /** 보낼 첨부를 잡는다. 첨부를 받지 않는 에이전트면 빈 것이다. */
  take: () => Outgoing;
  /** 보내기가 끝났다. 스트림이 열렸으면(`sent`) 보낸 칩을 비우고, 거절됐으면 잡은 칩을 놓는다. */
  settle: (ids: string[], sent: boolean) => void;
  /** 보내지 않은 첨부를 모두 버린다. 대화를 바꿀 때 부른다. */
  clear: () => void;
}

/** 서버의 업로드를 지운다. 실패해도 1시간 뒤 서버가 지우므로 기다리지 않는다. */
function discard(chat: ChatBase, uploadId: string): void {
  void deleteChatAttachment(chat, uploadId).catch(() => undefined);
}

/** 파일 하나를 업로드할 모양으로 만든다. 이미지는 상한 안으로 다시 굽고 썸네일을 읽는다. */
async function prepared(file: File, kind: AttachmentKind, limit: number): Promise<{ file: File; thumbnail?: string }> {
  if (kind === "document") return { file };
  const baked = await rebakeImage(file, limit).catch((e: unknown) => {
    throw new AttachmentRefused({ code: e instanceof ImageDecodeError ? "image_unreadable" : "image_not_resized" });
  });
  return { file: baked, thumbnail: await readDataUrl(baked) };
}

type ChangeChips = (next: (prev: ComposerChip[]) => ComposerChip[]) => void;

/** 칩 목록. 콜백이 렌더를 기다리지 않고 지금 목록을 읽도록 바꿀 때 ref 도 함께 바꾼다. */
function useChipList() {
  const [chips, setChips] = useState<ComposerChip[]>([]);
  const current = useRef(chips);
  const change = useCallback<ChangeChips>((next) => {
    current.current = next(current.current);
    setChips(current.current);
  }, []);
  return { chips, current, change };
}

/**
 * 업로드 줄. `enqueue` 한 칩을 차례로 업로드하고 결과를 칩에 적는다. `dropped` 에 든 칩은 시작하지 않거나 끝난 뒤에 지운다.
 * 알림은 `announce` 로 한다. 칩에는 실패한 까닭을 오류 그대로 두고, 낭독기에 알리는 말은 그 순간의 언어로 만든다.
 */
function useUploadQueue(chat: ChatBase, features: ChatFeatures, change: ChangeChips, announce: (text: string) => void) {
  const t = useT();
  const queue = useRef<Promise<void>>(Promise.resolve());
  const dropped = useRef(new Set<string>());

  const upload = useCallback(
    async (key: string, original: File, kind: AttachmentKind) => {
      const patch = (fields: Partial<ComposerChip>) => change((prev) => prev.map((c) => (c.key === key ? { ...c, ...fields } : c)));
      try {
        const limit = byteLimit(kind, features);
        const { file, thumbnail } = await prepared(original, kind, limit);
        if (file.size > limit) throw new AttachmentRefused({ code: "too_large", kind, limit });
        // 다시 굽는 사이에 버렸으면 업로드하지 않고, 업로드하는 사이에 버렸으면 끝난 뒤에 지운다
        if (dropped.current.has(key)) return;
        const done = await uploadChatAttachment(chat, file);
        if (dropped.current.has(key)) return discard(chat, done.id);
        patch({ status: "ready", facts: uploadFacts(done), uploadId: done.id, thumbnail });
        announce(t.chat.attachments.uploaded(done.name));
      } catch (e) {
        if (dropped.current.has(key)) return;
        patch({ status: "error", error: e });
        announce(t.chat.attachments.uploadFailed(original.name, describeChatError(t, e)));
      }
    },
    [chat, announce, change, features, t],
  );

  const enqueue = useCallback(
    (key: string, file: File, kind: AttachmentKind) => {
      // 줄에 서 있는 사이에 버린 칩은 시작하지 않는다
      queue.current = queue.current.then(() => (dropped.current.has(key) ? undefined : upload(key, file, kind)));
    },
    [upload],
  );
  return { enqueue, dropped };
}

/** 보낼 칩을 잡고 놓는다. 첨부를 받지 않는 에이전트(`enabled` 거짓)면 숨은 칩이 있어도 싣지 않는다. */
function useSendHold(current: RefObject<ComposerChip[]>, change: ChangeChips, enabled: boolean, onSent: () => void) {
  const take = useCallback((): Outgoing => {
    const ready = enabled ? current.current.filter((c) => c.uploadId && !c.sending) : [];
    if (!ready.length) return NOTHING;
    change((prev) => prev.map((c) => (ready.includes(c) ? { ...c, sending: true } : c)));
    return { ids: ready.map((c) => c.uploadId as string), facts: ready.map((c) => sentFacts(c.facts)) };
  }, [change, current, enabled]);

  const settle = useCallback(
    (ids: string[], sent: boolean) => {
      const taken = (c: ComposerChip) => c.uploadId !== undefined && ids.includes(c.uploadId);
      change((prev) => (sent ? prev.filter((c) => !taken(c)) : prev.map((c) => (taken(c) ? { ...c, sending: false } : c))));
      if (sent) onSent();
    },
    [change, onSent],
  );
  return { take, settle };
}

export function useAttachments(chat: ChatBase, features: ChatFeatures): ChatAttachments {
  const t = useT();
  const { chips, current, change } = useChipList();
  const [overLimit, setOverLimit] = useState<OverLimit | null>(null);
  const { message, count, announce } = useAnnounce();
  const { enqueue, dropped } = useUploadQueue(chat, features, change, announce);
  const nextKey = useRef(0);
  const enabled = takesAttachments(features);

  const addFiles = useCallback(
    (files: File[]) => {
      const { admitted, overLimit: over } = admit(files, current.current.map((c) => c.facts.kind), features);
      const added = admitted.map(({ file, kind, refusal }): ComposerChip => {
        const key = String(nextKey.current++);
        const facts = { name: file.name, kind: kind ?? "document", bytes: file.size };
        if (refusal || !kind) return { key, facts, status: "error", error: refusal ?? undefined };
        enqueue(key, file, kind);
        return { key, facts, status: "uploading" };
      });
      change((prev) => [...prev, ...added]);
      setOverLimit(over);
      if (over) announce(t.chat.attachments.overLimit(over.maxFiles, over.maxImages, over.dropped));
    },
    [announce, change, current, enqueue, features, t],
  );

  /** 고른 칩을 버린다. 다 된 업로드는 지금 지우고 나머지는 업로드 줄이 맡는다. */
  const drop = useCallback(
    (picked: (chip: ComposerChip) => boolean) => {
      for (const chip of current.current.filter(picked)) {
        dropped.current.add(chip.key);
        if (chip.uploadId) discard(chat, chip.uploadId);
      }
      change((prev) => prev.filter((c) => !dropped.current.has(c.key)));
      setOverLimit(null);
    },
    [chat, change, current, dropped],
  );
  const remove = useCallback((key: string) => drop((c) => c.key === key), [drop]);
  const clear = useCallback(() => drop(() => true), [drop]);
  // 패널이 사라지거나 에이전트가 바뀌면 보내지 않은 첨부를 버린다
  useEffect(() => clear, [clear]);

  const clearOverLimit = useCallback(() => setOverLimit(null), []);
  const { take, settle } = useSendHold(current, change, enabled, clearOverLimit);

  return {
    enabled,
    accept: attachmentAccept(features),
    chips,
    overLimit,
    blocking: chips.some((c) => c.status !== "ready"),
    announcement: { message, count },
    addFiles,
    remove,
    take,
    settle,
    clear,
  };
}

/** 입력칸(Composer)에 넘기는 첨부. 이 에이전트가 첨부를 받지 않으면 없다. 칩은 `ComposerChips` 가 따로 그린다. */
export function composerAttach(attach: ChatAttachments): ComposerAttach | undefined {
  return attach.enabled ? { accept: attach.accept, onFiles: attach.addFiles, blocking: attach.blocking, testId: "chat-attach" } : undefined;
}
