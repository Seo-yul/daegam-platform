/**
 * 입력칸에서 파일을 첨부하는 길 셋 (P36 9절). 클립 단추가 여는 파일 선택 창, 붙여 넣기, 끌어 놓기다. 칩과 업로드는
 * useAttachments 와 AttachmentChips 가 맡는다.
 *
 * 붙여 넣기는 글을 막지 않는다. 클립보드에 글이 있으면 글만 붙인다. Office 에서 복사한 셀·문단은 글과 함께 그림 표현도 싣기
 * 때문이다. 화면 캡처처럼 그림만 있을 때 첨부한다.
 *
 * 끌어 놓을 자리는 입력칸이 아니라 채팅 열 전체다(`useFileDrop`). 파일을 끌어 오는 동안은 첨부를 받지 않을 때도 기본 동작을
 * 막는다. 막지 않으면 빗나가 전사 위에 놓은 파일을 브라우저가 열어 앱을 떠난다.
 */
import { Paperclip } from "lucide-react";
import { useRef, useState, type ClipboardEvent, type DragEvent } from "react";

import { IconButton } from "@/components/ui/icon-button";
import { useT } from "@/i18n";

/** 입력칸의 파일 첨부. 클립 단추는 `{testId}`, 숨은 파일 칸은 `{testId}-input` 이다. */
export interface ComposerAttach {
  /** 파일 선택 창이 받는 형식(accept). */
  accept: string;
  /** 선택하거나 붙여 넣은 파일. */
  onFiles: (files: File[]) => void;
  /** 업로드 중이거나 실패한 칩이 있어 보낼 수 없다. */
  blocking: boolean;
  testId: string;
}

/** 클립 단추와 그것이 여는 숨은 파일 칸. */
export function AttachButton({ attach, disabled }: { attach: ComposerAttach; disabled: boolean }) {
  const t = useT();
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <IconButton label={t.chat.attachments.attach} testId={attach.testId} disabled={disabled} onClick={() => fileRef.current?.click()}>
        <Paperclip size={16} aria-hidden />
      </IconButton>
      <input
        ref={fileRef}
        type="file"
        multiple
        hidden
        accept={attach.accept}
        data-testid={`${attach.testId}-input`}
        onChange={(e) => {
          attach.onFiles(Array.from(e.target.files ?? []));
          // 같은 파일을 다시 선택해도 onChange 가 오게 비운다
          e.target.value = "";
        }}
      />
    </>
  );
}

/** 붙여 넣은 파일을 첨부한다. 글이 함께 있으면 첨부하지 않고 입력칸의 기본 동작(글 붙이기)에 맡긴다. */
export function pasteFiles(e: ClipboardEvent, onFiles: (files: File[]) => void): void {
  const data = e.clipboardData;
  if (Array.from(data.types).includes("text/plain")) return;
  const files = Array.from(data.files);
  if (files.length) onFiles(files);
}

/**
 * 자리 안으로 파일을 끌어 오는가. 글이나 링크를 끌어 놓을 때는 기본 동작을 그대로 둔다. 서랍과 인용 창처럼 body 에 띄운 것(portal)의
 * 이벤트도 React 트리를 따라 올라오므로 화면에서 자리 안에 있는 것만 본다.
 */
function draggingFiles(e: DragEvent): boolean {
  return e.currentTarget.contains(e.target as Node) && Array.from(e.dataTransfer.types).includes("Files");
}

/**
 * 파일을 끌어 놓을 자리. `props` 를 자리 요소에 펴고 `over` 일 때 테두리로 강조한다. `onFiles` 가 없으면(첨부를 받지 않거나 입력칸이
 * 잠겼다) 기본 동작만 막고 받지 않는다.
 */
export function useFileDrop(onFiles?: (files: File[]) => void) {
  const [over, setOver] = useState(false);
  // 안쪽 요소를 지날 때마다 dragenter·dragleave 가 오므로 깊이를 세어 자리를 완전히 벗어났을 때만 강조를 끈다
  const depth = useRef(0);
  const accepting = Boolean(onFiles);
  return {
    over: over && accepting,
    props: {
      onDragEnter: (e: DragEvent) => {
        if (!draggingFiles(e)) return;
        depth.current += 1;
        setOver(true);
      },
      onDragLeave: (e: DragEvent) => {
        if (!draggingFiles(e)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (!depth.current) setOver(false);
      },
      onDragOver: (e: DragEvent) => {
        if (!draggingFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = accepting ? "copy" : "none";
      },
      onDrop: (e: DragEvent) => {
        if (!draggingFiles(e)) return;
        e.preventDefault();
        depth.current = 0;
        setOver(false);
        onFiles?.(Array.from(e.dataTransfer.files));
      },
    },
  };
}
