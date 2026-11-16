/**
 * 마지막 턴의 다시 생성과 메시지 수정 (P36 5절). Agent 채팅(ChatPanel)이 쓰고, 런타임이 `regenerate` 를 알렸고 세션이 활성이며
 * 진행 중인 턴이 없을 때만 단추가 선다. 마지막 답 아래에 "다시 생성", 마지막 사용자 말풍선에 연필이 온다.
 *
 * 연필을 누르면 그 말풍선 자리가 입력칸으로 바뀐다. Enter 로 보내고 Escape 로 그만둔다. 글을 그대로 두고 보내면 다시 생성과
 * 같다. 그 말과 함께 보낸 첨부는 런타임이 그대로 두므로 입력칸 위에 읽기 전용 칩으로 남는다. 마지막 턴에 도구 호출이 있었으면 보내기 전에 한 번 묻는다. 다시 생성하면 도구가 다시 실행될 수 있고 이미 실행된 작업은
 * 취소되지 않기 때문이다. 묻는 창을 취소하면 입력칸과 고친 글이 그대로 남는다. 보내는 일(미리 걷기, 거절되면 되돌리기)은
 * 부르는 쪽이 맡는다.
 */
import { RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type Ref } from "react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useFocusOnReturn } from "@/components/ui/focus";
import { useT } from "@/i18n";
import type { AttachmentFacts } from "@/lib/attachments";

import { AttachmentChips, sentChips } from "./AttachmentChips";
import { Composer } from "./Composer";
import { lastTurn, type ChatItem, type LastTurn } from "./items";

/** 다시 보낼 것. message 가 없으면 마지막 질문 그대로다. */
export type RegenerateRun = (turn: LastTurn, message?: string) => void;

interface LastTurnActions {
  /** 단추를 세울 마지막 턴. 없으면 단추가 없다. */
  turn: LastTurn | null;
  /** 마지막 사용자 말풍선이 입력칸으로 바뀌었는가. */
  editing: boolean;
  startEdit: () => void;
  /** 입력칸을 닫고 연필 단추로 포커스를 돌려준다. */
  cancelEdit: () => void;
  /** 연필 단추에 단다. 입력칸이 닫히며 다시 선 단추가 포커스를 받는다. */
  editButtonRef: Ref<HTMLButtonElement>;
  /** 다시 생성(인자 없음)이나 메시지 수정. 도구를 실행한 턴이면 먼저 묻는다. */
  request: (message?: string) => void;
  /** 묻는 창의 상태다. `RegenerateConfirm` 에 그대로 넘긴다. */
  confirm: { open: boolean; onConfirm: () => void; onCancel: () => void };
}

export function useLastTurnActions(items: ChatItem[], enabled: boolean, run: RegenerateRun): LastTurnActions {
  const turn = enabled ? lastTurn(items) : null;
  // 자리 번호가 아니라 마지막 사용자 말 항목 자체를 쥔다. 세션을 바꾸거나 되살리면 항목이 새 객체라 저절로 풀린다.
  // 번호만 쥐면 다른 세션의 같은 자리에서 입력칸이 다시 열려 포커스를 가져간다
  const userItem = turn ? items[turn.userAt] : null;
  const [editingItem, setEditingItem] = useState<ChatItem | null>(null);
  const [asked, setAsked] = useState<{ item: ChatItem; message?: string } | null>(null);
  const focusEdit = useFocusOnReturn<HTMLButtonElement>();
  const current = (item: ChatItem | null | undefined) => item != null && item === userItem;

  function request(message?: string) {
    if (!turn || !userItem) return;
    const edited = message !== undefined && message !== turn.text ? message : undefined;
    // 묻는 동안 입력칸은 그대로 둔다. 확인한 뒤에 닫는다
    if (turn.calledTools) return setAsked({ item: userItem, message: edited });
    setEditingItem(null);
    run(turn, edited);
  }

  return {
    turn,
    editing: current(editingItem),
    startEdit: () => setEditingItem(userItem),
    cancelEdit: () => {
      focusEdit.arm();
      setEditingItem(null);
    },
    editButtonRef: focusEdit.ref,
    request,
    confirm: {
      open: current(asked?.item),
      onConfirm: () => {
        if (turn && asked && current(asked.item)) {
          setEditingItem(null);
          run(turn, asked.message);
        }
        setAsked(null);
      },
      onCancel: () => setAsked(null),
    },
  };
}

/** 마지막 답 아래의 "다시 생성" 단추다. */
export function RegenerateButton({ onClick }: { onClick: () => void }) {
  const t = useT();
  return (
    <div className="flex justify-start">
      <Button type="button" size="sm" variant="ghost" data-testid="chat-regenerate" onClick={onClick}>
        <RotateCcw size={13} aria-hidden />
        {t.chat.turnActions.regenerate}
      </Button>
    </div>
  );
}

/** 마지막 사용자 말풍선 자리에 서는 입력칸이다. 열리면 곧바로 초점을 받는다. 칩은 `chat-edit-attachments` 다. */
export function MessageEditor({ text, attachments, onSubmit, onCancel }: { text: string; attachments?: AttachmentFacts[]; onSubmit: (text: string) => void; onCancel: () => void }) {
  const t = useT();
  const [value, setValue] = useState(text);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  return (
    <Composer
      card
      className="self-end"
      value={value}
      onChange={setValue}
      onSubmit={() => onSubmit(value.trim())}
      onCancel={onCancel}
      chips={attachments?.length ? <AttachmentChips chips={sentChips(attachments)} testId="chat-edit-attachments" /> : null}
      inputRef={ref}
      left={
        <Button type="button" size="sm" variant="ghost" data-testid="chat-edit-cancel" onClick={onCancel}>
          {t.common.cancel}
        </Button>
      }
      inputLabel={t.chat.turnActions.editInput}
      inputTestId="chat-edit-input"
      sendTestId="chat-edit-send"
      formTestId="chat-edit"
    />
  );
}

/** 도구를 실행한 턴을 다시 생성하기 전에 묻는 창이다. */
export function RegenerateConfirm({ open, onConfirm, onCancel }: LastTurnActions["confirm"]) {
  const t = useT();
  const text = t.chat.turnActions;
  return <ConfirmDialog open={open} title={text.confirmTitle} description={text.confirmBody} confirmLabel={text.regenerate} onConfirm={onConfirm} onCancel={onCancel} />;
}
