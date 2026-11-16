/**
 * 첨부 칩 (P36 9절). 입력칸 위의 칩, 보낸 말풍선 아래의 칩, 메시지 수정 칸의 칩이 같은 부품이다.
 *
 * 입력칸의 칩은 상태가 셋이다. 업로드 중이면 "업로드 중…", 끝나면 "보고서.pdf · 12쪽 · 38,211자" 처럼 사실 한 줄이고 50만 자에서
 * 잘렸으면 "일부만" 배지가 붙는다. 실패하면 사람 문구를 칩 안에 적는다. `onRemove` 를 주면 칩마다 삭제 단추가 서고, 삭제하면 초점이
 * 다음 칩(없으면 앞 칩)의 삭제 단추로, 칩이 다 없어지면 `afterLast`(입력칸)로 간다. 보내는 중인 칩(`sending`)은 스트림이 열리거나
 * 거절될 때까지 삭제 단추가 없다. 이미지는 썸네일을 `data:` URL 로 그린다(CSP 가 `blob:` 이미지를 막는다). 보낸 말풍선과 메시지 수정
 * 칸의 칩은 읽기 전용이고 썸네일이 없다.
 *
 * 입력칸의 칩 줄(`ComposerChips`)은 넣지 못한 파일의 안내, 첨부만으로는 보낼 수 없다는 안내, 업로드 결과를 낭독기에 알리는 줄을
 * 함께 둔다. 알림 줄은 늘 있어야 낭독기가 놓치지 않는다.
 *
 * data-testid 는 목록이 `{testId}`, 칩이 `{testId}-{i}` 이고 칩의 상태는 `data-status` 다. 삭제 단추는 `-remove` 를 붙인다. 입력칸의
 * 안내는 `chat-attachments-notice`·`chat-attachments-hint`, 알림 줄은 `chat-attachments-live` 다.
 */
import { FileText as DocumentIcon, Image as ImageIcon, LoaderCircle, X } from "lucide-react";
import { useRef, type RefObject } from "react";

import { Badge } from "@/components/ui/badge";
import { IconButton } from "@/components/ui/icon-button";
import { LiveRegion } from "@/components/ui/live-region";
import { useT } from "@/i18n";
import { factsText, type AttachmentFacts } from "@/lib/attachments";
import { cn } from "@/lib/utils";

import { describeChatError } from "./items";
import type { ChatAttachments } from "./useAttachments";

export interface AttachmentChipView {
  key: string;
  facts: AttachmentFacts;
  status: "uploading" | "ready" | "error";
  /** 실패한 까닭. `error` 일 때만 있다. 잡은 오류나 거절(AttachmentRefused)이고 문장은 그릴 때 만든다. */
  error?: unknown;
  /** 이미지의 `data:` URL. */
  thumbnail?: string;
  /** 보내는 중이다. 스트림이 열리거나 거절될 때까지 삭제할 수 없다. */
  sending?: boolean;
}

/** 보낸 말풍선의 칩. 전사에 남은 사실만 보인다. */
export function sentChips(attachments: AttachmentFacts[]): AttachmentChipView[] {
  return attachments.map((facts, i) => ({ key: `${i}:${facts.name}`, facts, status: "ready" }));
}

function ChipIcon({ chip }: { chip: AttachmentChipView }) {
  if (chip.status === "uploading") return <LoaderCircle size={14} className="shrink-0 text-muted motion-safe:animate-spin" aria-hidden />;
  if (chip.thumbnail) return <img src={chip.thumbnail} alt="" className="size-8 shrink-0 rounded-item object-cover" />;
  const Icon = chip.facts.kind === "image" ? ImageIcon : DocumentIcon;
  return <Icon size={14} className="shrink-0 text-label" aria-hidden />;
}

function Chip({ chip, testId, remove }: { chip: AttachmentChipView; testId: string; remove?: { onClick: () => void; ref: (el: HTMLButtonElement | null) => void } }) {
  const t = useT();
  const text = t.chat.attachments;
  const detail = chip.status === "uploading" ? text.uploading : chip.status === "ready" ? factsText(t, chip.facts) : null;
  return (
    <li
      className={cn(
        "flex max-w-full min-w-0 items-center gap-2 rounded-chip border px-2.5 py-1.5 text-xs",
        chip.status === "error" ? "border-err-line bg-err-soft" : "border-line bg-surface",
      )}
      data-testid={testId}
      data-status={chip.status}
    >
      <ChipIcon chip={chip} />
      <span className="flex min-w-0 flex-col">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="max-w-[16rem] truncate font-medium text-fg" title={chip.facts.name}>
            {chip.facts.name}
          </span>
          {detail ? <span className="shrink-0 text-muted">· {detail}</span> : null}
          {chip.status === "ready" && chip.facts.truncated ? <Badge variant="warn">{text.truncated}</Badge> : null}
        </span>
        {chip.error !== undefined ? (
          <span className="text-err-fg" data-testid={`${testId}-error`}>
            {describeChatError(t, chip.error)}
          </span>
        ) : null}
      </span>
      {remove ? (
        <IconButton ref={remove.ref} label={text.remove(chip.facts.name)} testId={`${testId}-remove`} onClick={remove.onClick} danger className="size-6">
          <X size={13} aria-hidden />
        </IconButton>
      ) : null}
    </li>
  );
}

interface AttachmentChipsProps {
  chips: AttachmentChipView[];
  onRemove?: (key: string) => void;
  /** 마지막 칩을 삭제하면 초점이 갈 곳. */
  afterLast?: RefObject<HTMLElement | null>;
  testId: string;
  className?: string;
}

export function AttachmentChips({ chips, onRemove, afterLast, testId, className }: AttachmentChipsProps) {
  const t = useT();
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  if (!chips.length) return null;

  // 누른 단추가 사라지면 초점이 body 로 떨어진다. 사라지기 전에 다음 칩이나 앞 칩의 삭제 단추로 옮긴다
  function removeAt(key: string) {
    const removable = chips.filter((c) => !c.sending).map((c) => c.key);
    const at = removable.indexOf(key);
    const next = removable[at + 1] ?? removable[at - 1];
    (next ? buttons.current.get(next) : afterLast?.current)?.focus();
    onRemove?.(key);
  }

  return (
    <ul className={cn("flex flex-wrap gap-1.5", className)} data-testid={testId} aria-label={t.chat.attachments.list}>
      {chips.map((chip, i) => (
        <Chip
          key={chip.key}
          chip={chip}
          testId={`${testId}-${i}`}
          remove={
            onRemove && !chip.sending
              ? {
                  onClick: () => removeAt(chip.key),
                  ref: (el) => {
                    if (el) buttons.current.set(chip.key, el);
                    else buttons.current.delete(chip.key);
                  },
                }
              : undefined
          }
        />
      ))}
    </ul>
  );
}

/** 입력칸 위의 칩 줄과 안내. `needsText` 는 입력칸이 비었는가이고, 다 된 칩이 있으면 첨부만으로는 보낼 수 없다고 알린다. */
export function ComposerChips({
  attach,
  needsText,
  inputRef,
  limitsFetch = false,
}: {
  attach: ChatAttachments;
  needsText: boolean;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  /** 첨부가 웹 fetch 를 허용 목록으로 좁히는 에이전트면 칩이 있을 때 알린다(P39b 14절 2부). */
  limitsFetch?: boolean;
}) {
  const t = useT();
  const text = t.chat.attachments;
  const ready = attach.chips.some((c) => c.status === "ready");
  const over = attach.overLimit;
  return (
    <>
      <AttachmentChips chips={attach.chips} onRemove={attach.remove} afterLast={inputRef} testId="chat-attachments" />
      {over ? (
        <p className="text-xs text-muted" data-testid="chat-attachments-notice">
          {text.overLimit(over.maxFiles, over.maxImages, over.dropped)}
        </p>
      ) : null}
      {needsText && ready ? (
        <p className="text-xs text-muted" data-testid="chat-attachments-hint">
          {text.needsText}
        </p>
      ) : null}
      {limitsFetch && attach.chips.length ? (
        <p className="text-xs text-muted" data-testid="chat-attachments-fetch">
          {text.limitsFetch}
        </p>
      ) : null}
      <LiveRegion key={attach.announcement.count} message={attach.announcement.message} testId="chat-attachments-live" />
    </>
  );
}
