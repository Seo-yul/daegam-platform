/**
 * 홈 챗의 왼쪽 대화 목록 (D35 F4). 청지기와 에이전트가 같은 모양을 쓰되 목록을 만드는 쪽은 다르다.
 * 두 저장소를 합치지 않기 때문이다(청지기는 플랫폼 Mongo, 에이전트는 그 인스턴스의 Database).
 * 데이터는 들지 않는다. 고르기·새로 만들기·지우기·이름 바꾸기·검색을 위로 올리고, 여기에는 행 안 입력칸과 확인 창 같은
 * 화면 상태만 있다.
 *
 * 행 끝의 동작(`actions`)은 상대마다 다르다. 청지기 행은 휴지통 단추 하나라 확인 없이 지운다. 에이전트 세션은 런타임이
 * `session_admin` 을 알리면 관리할 수 있다(P36 4절). 위에 "대화 검색" 칸이 서고 입력을 멈추고 300ms 가 지나면 서버의 제목
 * 검색으로 올린다. 행마다 더보기 메뉴에 "이름 바꾸기" 와 "삭제" 가 있고, 이름은 행 안의 입력칸에서 바꾸며 삭제는 확인 창에서
 * 한 번 더 묻는다. 이름 칸이 닫히면 포커스가 그 행으로 돌아온다.
 *
 * 시작 화면에서도 대화 화면에서도 같은 자리에 선다(D45 첫째). 자기 카드 안에 들어가므로 오른쪽
 * 경계선은 두지 않는다. 항목 아래 한 줄에는 그 대화에 붙은 provider 이름이 온다. 목록 아래에는 위가 넘긴 부품(`footer`,
 * 에이전트의 장기 기억 단추)이 선다.
 */
import { Plus, Trash2 } from "lucide-react";
import { useState, type ReactNode, type Ref } from "react";

import { ApiError } from "@/api/client";
import { describeError } from "@/components/ErrorMessage";
import { SessionSearch, SessionTitle } from "@/components/chat";
import { ActionMenu } from "@/components/ui/action-menu";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useFocusOnReturn } from "@/components/ui/focus";
import { IconButton } from "@/components/ui/icon-button";
import { ROW_ACTION_REVEAL } from "@/components/ui/reveal";
import { useConfirmAction } from "@/components/ui/useConfirmAction";
import { useT, type Messages } from "@/i18n";
import { sessionLabel } from "@/lib/sessionTitle";
import { cn } from "@/lib/utils";

export interface SessionRow {
  id: string;
  /** 목록에 보이는 이름. 비면 id 앞자리를 쓴다. */
  title: string;
  /** 제목 아래 한 줄(턴 수·시각 같은 것). 없으면 안 그린다. */
  detail?: string | null;
  /** 닫힌 대화인가 (D46). 열리기는 하지만 이어 쓸 수 없어 배지를 단다. */
  closed?: boolean;
  /** 턴이 도는 대화인가 (P78 4절). 마지막으로 읽은 목록의 상태이고 배지를 단다. 열면 패널이 따라가며 거기서 멈춘다. */
  running?: boolean;
}

/** 청지기 세션. 행마다 휴지통 단추 하나이고 확인 없이 지운다(감사 기록만 남는다). */
export interface SessionTrash {
  kind: "trash";
  onDelete: (id: string) => void;
}

/** 에이전트 세션 관리(P36). 셋 다 위가 서버를 부르고, 실패하면 던진 오류를 목록이 그 자리에서 보인다. */
export interface SessionManage {
  kind: "manage";
  /** 검색칸의 글(앞뒤 공백을 뗀 것). 비면 검색을 끈 것이다. */
  onSearch: (q: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}

export interface SessionListProps {
  rows: SessionRow[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  onNew: () => void;
  /** 행 끝의 동작. 없으면 행에 단추가 없다(런타임이 `session_admin` 을 알리지 않은 에이전트). */
  actions?: SessionTrash | SessionManage;
  loading?: boolean;
  /** 목록이 비었을 때의 안내. 상대마다 다르다. */
  empty: string;
  /** 목록 아래에 붙는 성질 설명(세션 TTL 과 기록 보존 기간 같은 것, D46). */
  note?: string;
  /** 목록 아래, 안내 위에 서는 부품. 에이전트가 기억하는 것을 여는 단추가 쓴다(P36 8절). */
  footer?: ReactNode;
  /** 있으면 목록 대신 선다. 잠든 에이전트의 휴면 줄과 깨우기 단추가 쓴다(P44 8절). */
  notice?: ReactNode;
}

/** 행 끝의 작은 단추 모양. 포인터를 올리거나 초점이 오면 보이고, 포인터가 없는 화면에서는 늘 보인다. */
const ROW_ACTION_CLASS = cn("flex h-7 w-7 shrink-0 items-center justify-center rounded-item text-dim hover:bg-hover hover:text-fg", ROW_ACTION_REVEAL);

/**
 * 대화 목록. 폭과 바탕은 부르는 쪽(홈 챗의 조용한 열)이 정하고 이 부품은 그 안을 채운다 (P81 6절). 고른 대화는
 * 흰 카드로 떠서 옅은 바탕의 열에서 바로 보인다.
 */
export function SessionList({ rows, selected, onSelect, onNew, actions, loading = false, empty, note, footer, notice }: SessionListProps) {
  const t = useT();
  const text = t.shell.sessionList;
  const manage = actions?.kind === "manage" ? actions : null;
  const [editing, setEditing] = useState<string | null>(null);
  // 이름 칸이 닫히면 그 행의 단추가 다시 서며 포커스를 받는다
  const focusRow = useFocusOnReturn<HTMLButtonElement>();
  const closeEditor = () => {
    focusRow.arm();
    setEditing(null);
  };
  // 삭제 확인 창이다. 서버가 거절하면(진행 중인 턴이 있는 409 등) 창을 닫지 않고 까닭을 보인다
  const remove = useConfirmAction(async (row: SessionRow) => manage?.onDelete(row.id));
  return (
    <div className="flex min-h-0 w-full flex-1 flex-col" data-testid="home-session-list">
      <div className="flex items-center gap-1 pb-2 pl-4 pr-2.5 pt-3">
        <span className="text-[12.5px] font-semibold text-muted">{text.heading}</span>
        <IconButton className="ml-auto size-7" testId="home-new-session" label={t.shared.chatPanel.newChat} title={text.newTitle} onClick={onNew}>
          <Plus size={15} aria-hidden />
        </IconButton>
      </div>
      {manage ? (
        <div className="px-2.5 pb-2">
          <SessionSearch onSearch={manage.onSearch} placeholder={text.search} testId="home-sessions-search" />
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-2">
        {/* 목록 대신 선 안내가 있으면 행도, 불러오는 줄도, 빈 목록 안내도 없다 */}
        {notice ?? (
          <>
            {loading ? (
              <p className="px-2.5 py-1.5 text-xs text-muted" data-testid="home-sessions-loading">
                {text.loading}
              </p>
            ) : null}
            {!loading && !rows.length ? (
              <p className="px-2.5 py-1.5 text-xs text-muted" data-testid="home-sessions-empty">
                {empty}
              </p>
            ) : null}
            {rows.map((row) => (
              <div key={row.id} className="group/row flex items-center gap-0.5">
                {manage && editing === row.id ? (
                  <SessionTitle
                    initial={row.title}
                    testId={`home-session-title-${row.id}`}
                    onCancel={closeEditor}
                    onSave={async (title) => {
                      await manage.onRename(row.id, title);
                      closeEditor();
                    }}
                  />
                ) : (
                  <SessionButton ref={focusRow.ref} row={row} selected={row.id === selected} onSelect={onSelect} />
                )}
                {manage && editing !== row.id ? <SessionMenu row={row} onRename={() => setEditing(row.id)} onDelete={() => remove.ask(row)} /> : null}
                {actions?.kind === "trash" ? <TrashButton row={row} onDelete={actions.onDelete} /> : null}
              </div>
            ))}
          </>
        )}
      </div>
      {footer ? <div className="border-t border-line px-2 py-2">{footer}</div> : null}
      {note ? (
        <p className="border-t border-line px-4 pb-3.5 pt-3 text-xs leading-relaxed text-muted" data-testid="home-sessions-note">
          {note}
        </p>
      ) : null}
      {manage ? (
        <ConfirmDialog
          {...remove.dialog}
          title={text.deleteTitle}
          description={text.deleteBody}
          confirmLabel={t.common.delete}
          pendingLabel={text.deleting}
          error={remove.error ? deleteErrorText(t, remove.error) : null}
        />
      ) : null}
    </div>
  );
}

/** 대화 삭제의 실패 문장. 턴이 도는 대화의 409 turn_in_progress 는 공용 문장이 다시 보내라고 하므로 삭제에 맞춘 문장을 쓴다 (P78 4절). */
function deleteErrorText(t: Messages, error: unknown): string {
  if (error instanceof ApiError && error.code === "turn_in_progress") return t.shell.sessionList.deleteRunning;
  return describeError(t, error).text;
}

function SessionButton({ ref, row, selected, onSelect }: { ref?: Ref<HTMLButtonElement>; row: SessionRow; selected: boolean; onSelect: (id: string) => void }) {
  const t = useT();
  return (
    <button
      ref={ref}
      type="button"
      data-testid={`home-session-${row.id}`}
      data-selected={selected ? "true" : "false"}
      aria-current={selected ? "true" : undefined}
      className={cn(
        "min-w-0 flex-1 rounded-chip px-2.5 py-[7px] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring",
        selected ? "bg-surface text-heading shadow-hairline ring-1 ring-line" : "text-fg hover:bg-hover",
      )}
      onClick={() => onSelect(row.id)}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="truncate text-[13.5px] font-medium text-heading">{sessionLabel(row.id, row.title)}</span>
        {row.running ? (
          <Badge variant="info" className="h-[18px] shrink-0 px-1.5 text-[11px]" data-testid={`home-session-running-${row.id}`}>
            {t.chat.runningBadge}
          </Badge>
        ) : null}
        {row.closed ? (
          <Badge variant="muted" className="h-[18px] shrink-0 px-1.5 text-[11px]" data-testid={`home-session-closed-${row.id}`}>
            {t.shell.sessionList.closed}
          </Badge>
        ) : null}
      </span>
      {row.detail ? <span className="mt-px block truncate text-xs text-muted">{row.detail}</span> : null}
    </button>
  );
}

function TrashButton({ row, onDelete }: { row: SessionRow; onDelete: (id: string) => void }) {
  const t = useT();
  const text = t.shell.sessionList;
  return (
    <button
      type="button"
      data-testid={`home-session-delete-${row.id}`}
      aria-label={text.trash(sessionLabel(row.id, row.title))}
      title={text.trashTitle}
      className={ROW_ACTION_CLASS}
      onClick={() => onDelete(row.id)}
    >
      <Trash2 size={13} aria-hidden />
    </button>
  );
}

function SessionMenu({ row, onRename, onDelete }: { row: SessionRow; onRename: () => void; onDelete: () => void }) {
  const t = useT();
  const name = sessionLabel(row.id, row.title);
  return (
    <ActionMenu
      label={t.ui.cardGrid.menu(name)}
      triggerSize="sm"
      triggerLabel={t.ui.cardGrid.menuTrigger(name)}
      triggerClassName={ROW_ACTION_REVEAL}
      triggerTestId={`home-session-menu-${row.id}`}
      panelTestId={`home-session-menu-panel-${row.id}`}
      items={[
        { label: t.shell.sessionList.rename, onClick: onRename, testId: `home-session-rename-${row.id}` },
        { label: t.common.delete, danger: true, onClick: onDelete, testId: `home-session-menu-delete-${row.id}` },
      ]}
    />
  );
}
