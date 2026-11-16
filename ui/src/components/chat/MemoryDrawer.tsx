/**
 * 이 에이전트가 기억하는 내 파일 (P36 8절). 홈 챗 대화 목록 아래의 단추가 서랍을 연다.
 *
 * 런타임이 장기 기억 저장소에서 내 것만 읽어 주므로(`memory_files`) 다른 사용자의 기억은 어떤 권한으로도 보이지 않는다.
 * 목록에서 파일을 선택하면 내용이 보이고 `.md` 는 마크다운으로 그린다. 파일 하나 삭제와 모두 삭제는 확인 창에서 한 번 더
 * 묻고, 모두 삭제는 런타임 없이 저장소에서 지우는 D34 라우트다. 테스트 채팅은 기억을 남기지 않으므로 Agent 상세에는 없다.
 * 에이전트가 잠들었거나 깨우는 중이면(`phase`) 목록을 읽지 않고 휴면 줄과 깨우기 단추를 보인다. 상태가 바뀌면 다시 읽는다(P44 8절).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Trash2 } from "lucide-react";
import { useState } from "react";

import { deleteAllMyMemories, deleteMyMemory, listMyMemories, readMyMemory } from "@/api/agentChat";
import { ApiError } from "@/api/client";
import type { AgentMemoryFileRow } from "@/api/types";
import { SleepingNotice } from "@/components/agents/AgentSleep";
import { describeError, ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FileListButton } from "@/components/ui/file-list";
import { IconButton } from "@/components/ui/icon-button";
import { Modal } from "@/components/ui/modal";
import { useConfirmAction } from "@/components/ui/useConfirmAction";
import { useT, type Messages } from "@/i18n";
import { pausesRuntimeReads } from "@/lib/agentSleep";
import { memoryKey } from "@/lib/memories";
import { myMemoriesKey, myMemoryFileKey, myMemoryListKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

import { FileListLoading, FileMeta, FilePreviewPane } from "./FileText";

/** 보존 기간 한 줄. null 이면 만료가 없다. */
function retentionText(t: Messages, days: number | null | undefined): string {
  return days ? t.chat.memory.retention(days) : t.chat.memory.retentionForever;
}

export function MemoryDrawer({ agentId, userSub, phase = null }: { agentId: string; userSub: string | null; /** 에이전트 상태의 phase. */ phase?: string | null }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="ghost" className="w-full justify-start" data-testid="memory-drawer-open" onClick={() => setOpen(true)}>
        <Brain size={14} aria-hidden />
        {t.chat.memory.title}
      </Button>
      {open ? <MemoryModal agentId={agentId} userSub={userSub} phase={phase} onClose={() => setOpen(false)} /> : null}
    </>
  );
}

function MemoryModal({ agentId, userSub, phase, onClose }: { agentId: string; userSub: string | null; phase: string | null; onClose: () => void }) {
  const t = useT();
  const text = t.chat.memory;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const paused = pausesRuntimeReads(phase);
  const list = useQuery({ queryKey: myMemoryListKey(tenant, agentId), queryFn: () => listMyMemories(agentId), enabled: !paused });
  const files = list.data?.files ?? [];

  const refresh = (removed: string | null) => {
    if (removed === null || removed === selected) setSelected(null);
    return queryClient.invalidateQueries({ queryKey: myMemoriesKey(tenant, agentId) });
  };
  const removeOne = useConfirmAction(async (row: AgentMemoryFileRow) => {
    await deleteMyMemory(agentId, row.path);
    await refresh(row.path);
  });
  const removeAll = useConfirmAction<true>(async () => {
    await deleteAllMyMemories(agentId, userSub ?? "");
    await refresh(null);
  });

  return (
    <Modal open title={text.title} size="lg" testId="memory-drawer" closeTestId="memory-drawer-close" onClose={onClose}>
      <div className="flex min-h-0 flex-1 flex-col gap-3 px-5 py-4">
        <p className="text-xs leading-relaxed text-muted" data-testid="memory-notice">
          {text.notice}
        </p>
        {/* 잠든 동안에는 목록을 읽지 않는다. 목록을 읽지 못했으면 빈 목록 안내 대신 오류만 보인다 */}
        {paused ? (
          <SleepingNotice agentId={agentId} text={t.agents.sleepingList} phase={phase} testId="memory-sleeping" />
        ) : list.error ? (
          <ErrorMessage error={list.error} testId="memory-list-error" />
        ) : (
          <div className="grid min-h-0 flex-1 gap-3 md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
            <MemoryList files={files} loading={list.isLoading} selected={selected} onSelect={setSelected} onDelete={removeOne.ask} />
            <MemoryPreview agentId={agentId} path={selected} />
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 border-t border-line-soft pt-3">
          <p className="text-xs text-muted" data-testid="memory-retention">
            {list.data ? retentionText(t, list.data.retention_days) : null}
          </p>
          <Button size="sm" variant="danger" className="ml-auto" data-testid="memory-delete-all" disabled={!files.length || !userSub} onClick={() => removeAll.ask(true)}>
            {text.deleteAll}
          </Button>
        </div>
      </div>
      <ConfirmDialog
        {...removeOne.dialog}
        title={text.deleteTitle}
        description={removeOne.target ? text.deleteBody(memoryKey(removeOne.target.path)) : null}
        confirmLabel={t.common.delete}
        pendingLabel={t.shell.sessionList.deleting}
        error={removeOne.error ? describeError(t, removeOne.error).text : null}
      />
      <ConfirmDialog
        {...removeAll.dialog}
        title={text.deleteAllTitle}
        description={text.deleteAllBody}
        confirmLabel={text.deleteAll}
        pendingLabel={t.shell.sessionList.deleting}
        error={removeAll.error ? describeError(t, removeAll.error).text : null}
      />
    </Modal>
  );
}

function MemoryList({
  files,
  loading,
  selected,
  onSelect,
  onDelete,
}: {
  files: AgentMemoryFileRow[];
  loading: boolean;
  selected: string | null;
  onSelect: (path: string) => void;
  onDelete: (row: AgentMemoryFileRow) => void;
}) {
  const t = useT();
  if (loading) return <FileListLoading />;
  if (!files.length) {
    return (
      <p className="text-xs text-muted" data-testid="memory-empty">
        {t.chat.memory.empty}
      </p>
    );
  }
  return (
    <ul className="flex min-h-0 flex-col gap-0.5 overflow-y-auto" data-testid="memory-files">
      {files.map((row) => {
        const name = memoryKey(row.path);
        const current = row.path === selected;
        return (
          <li key={row.path} className="flex items-center gap-0.5">
            <FileListButton active={current} testId={`memory-file-${name}`} className="flex-1 flex-col items-stretch gap-0" onClick={() => onSelect(row.path)}>
              <span className="block truncate font-mono">{name}</span>
              <FileMeta size={row.size} modifiedAt={row.modified_at} />
            </FileListButton>
            <IconButton label={t.resources.delete.rowLabel(name)} testId={`memory-delete-${name}`} danger onClick={() => onDelete(row)}>
              <Trash2 size={13} aria-hidden />
            </IconButton>
          </li>
        );
      })}
    </ul>
  );
}

function MemoryPreview({ agentId, path }: { agentId: string; path: string | null }) {
  const t = useT();
  const text = t.chat.memory;
  const tenant = useTenant();
  const file = useQuery({
    queryKey: myMemoryFileKey(tenant, agentId, path),
    queryFn: () => readMyMemory(agentId, path!),
    enabled: path !== null,
  });
  const content = file.data && (file.data.encoding === "utf-8" ? file.data.content : null);
  return (
    <FilePreviewPane
      label={text.content}
      testId="memory-preview"
      path={path}
      hint={text.hint}
      // 다운로드가 없고 기억을 삭제할 수 있는 이 서랍은 file_too_large 에 제 문장(chat.memory.tooLarge)을 쓴다. 그릴 때마다 만든다
      error={file.error instanceof ApiError && file.error.code === "file_too_large" ? new Error(text.tooLarge) : file.error}
      text={content}
      markdownTestId="memory-preview-markdown"
    />
  );
}
