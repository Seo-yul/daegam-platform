/**
 * 이 대화의 파일 (P36 10절). 채팅 열 위의 "파일" 단추가 서랍을 열어 이 대화에 업로드한 파일과 에이전트가 만든 파일을 나눠 보인다.
 *
 * 행마다 이름, 크기, 시각과 "다운로드" 가 있고 `SESSION_FILE_PREVIEW_MAX_BYTES` 이하 파일은 "미리 보기" 로 내용을 본다. `.md` 는
 * 마크다운으로 그리고, UTF-8 로 풀리지 않는 파일(업로드한 이미지)은 안내만 보인다. P40 전에는 에이전트가 만드는 파일이 파일
 * 도구로 쓴 텍스트뿐이라 그 묶음의 안내가 그것을 말한다. 닫힌 대화는 파일이 지워져 목록 대신 그 사실을 보인다.
 *
 * 대화가 이어지며 파일이 바뀌므로 서랍을 열 때마다 목록과 미리 볼 내용을 새로 읽는다.
 *
 * 인증이 헤더라 링크를 그대로 걸 수 없으므로 다운로드는 `lib/download.ts` 가 받아 저장한다. 성공한 `write_file`·`edit_file` 도구
 * 카드의 다운로드(`WrittenFileDownload`)도 같은 길이고 단추는 인용 원문과 같은 `DownloadButton` 이다. 받을 에이전트와 세션은
 * `SessionFilesScope` 가 주고, 런타임이 `session_files` 를 알리지 않았거나 첫 턴 전이면 단추가 서지 않는다. 미리 보는 행은 기억
 * 서랍의 행처럼 `aria-current` 로 알린다.
 *
 * 코드 실행 Agent 는 작업 공간 파일(`origin: workspace`)이 "작업 공간에서 만든 파일" 묶음으로 따로 선다(P40 6절). 열면 코드가 돌 수
 * 있는 형식은 다운로드가 403 file_refused_executable 이고 오류 사전의 문장이 그것을 말한다.
 *
 * data-testid: 단추 `session-files-open`, 서랍 `session-files`, 묶음 `session-files-upload`·`session-files-workspace`·`session-files-agent`(빈 묶음은
 * `-empty`, 안내는 `-notice`), 행 `session-file-{경로}`, 미리 보기 `session-file-preview-{경로}`, 다운로드
 * `session-file-download-{경로}`(실패는 `-error`), 내용 `session-file-content`(읽기 실패는 `-error`), 닫힌 대화 `session-files-closed`.
 */
import { useQuery } from "@tanstack/react-query";
import { Files } from "lucide-react";
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { downloadSessionFile, listSessionFiles, readSessionFile } from "@/api/agentChat";
import { ApiError, type ChatBase } from "@/api/client";
import type { AgentSessionFileRow } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { sessionFilePreviewKey, sessionFilesKey } from "@/lib/queryKeys";
import { RUNTIME_SESSION_FILES, type ChatFeatures } from "@/lib/runtimeFeatures";
import { SESSION_FILE_PREVIEW_MAX_BYTES, sessionFileLabel, sessionFileName } from "@/lib/sessionFiles";
import { useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";

import { DownloadButton } from "./DownloadButton";
import { FileListLoading, FileMeta, FilePreviewPane } from "./FileText";

/** 닫힌 대화의 목록 요청이 받는 런타임 코드다. 서랍을 연 사이에 닫혔으면 이 코드로 안다. */
const SESSION_CLOSED = "session_closed";

/** 파일을 받을 채팅 라우트와 세션이다. 닫힌 대화는 파일이 지워졌다. */
interface SessionFileTarget {
  chat: ChatBase;
  sessionId: string;
  closed: boolean;
}

const SessionFilesContext = createContext<SessionFileTarget | null>(null);

/** 아래 "파일" 단추와 도구 카드에 받을 세션을 준다. 런타임이 `session_files` 를 알리지 않았거나 세션이 아직 없으면 주지 않는다. */
export function SessionFilesScope({ chat, sessionId, closed, features, children }: { chat: ChatBase; sessionId: string | null; closed: boolean; features: ChatFeatures; children: ReactNode }) {
  const enabled = features.has(RUNTIME_SESSION_FILES);
  const value = useMemo(() => (enabled && sessionId ? { chat, sessionId, closed } : null), [enabled, chat, sessionId, closed]);
  return <SessionFilesContext.Provider value={value}>{children}</SessionFilesContext.Provider>;
}

/** 채팅 열 위의 "파일" 단추 줄과 서랍. 받을 세션이 없으면 아무것도 없다. */
export function SessionFilesBar() {
  const t = useT();
  const target = useContext(SessionFilesContext);
  const [open, setOpen] = useState(false);
  if (!target) return null;
  return (
    <div className="flex justify-end px-4 pt-2">
      <Button size="sm" variant="ghost" data-testid="session-files-open" onClick={() => setOpen(true)}>
        <Files size={14} aria-hidden />
        {t.chat.sessionFiles.open}
      </Button>
      {open ? <SessionFilesModal target={target} onClose={() => setOpen(false)} /> : null}
    </div>
  );
}

/** 성공한 `write_file`·`edit_file` 카드의 다운로드. 받을 세션이 없거나 대화가 닫혀 파일이 지워졌으면 없다. */
export function WrittenFileDownload({ path, testId }: { path: string; testId: string }) {
  const t = useT();
  const target = useContext(SessionFilesContext);
  if (!target || target.closed) return null;
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2">
      <DownloadButton download={() => downloadSessionFile(target.chat, target.sessionId, path)} label={t.chat.sessionFiles.download(sessionFileName(path))} testId={testId} />
    </div>
  );
}

function SessionFilesModal({ target, onClose }: { target: SessionFileTarget; onClose: () => void }) {
  const t = useT();
  const text = t.chat.sessionFiles;
  const tenant = useTenant();
  const { chat, sessionId } = target;
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({
    queryKey: sessionFilesKey(tenant, chat, sessionId),
    queryFn: () => listSessionFiles(chat, sessionId),
    enabled: !target.closed,
    refetchOnMount: "always",
  });
  const closed = target.closed || (list.error instanceof ApiError && list.error.code === SESSION_CLOSED);
  const files = list.data?.files ?? [];
  const workspace = files.filter((f) => f.origin === "workspace");
  const group = { target, loading: list.isLoading, selected, onPreview: setSelected };

  return (
    <Modal open title={text.title} size="lg" testId="session-files" closeTestId="session-files-close" onClose={onClose}>
      <div className="flex min-h-0 flex-1 flex-col gap-3 px-5 py-4">
        {closed ? (
          <p className="text-xs text-muted" data-testid="session-files-closed">
            {text.closed}
          </p>
        ) : list.error ? (
          <ErrorMessage error={list.error} testId="session-files-list-error" />
        ) : (
          <div className="grid min-h-0 flex-1 gap-3 md:grid-cols-[minmax(0,18rem)_minmax(0,1fr)]">
            <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
              <FileGroup {...group} title={text.uploaded} rows={files.filter((f) => f.origin === "upload")} empty={text.uploadedEmpty} testId="session-files-upload" />
              {/* 코드 실행 Agent 의 작업 공간 파일(P40 6절). 파일 Tool 도 그곳에 쓰므로 에이전트 파일 묶음과 따로 보이고, 없으면 묶음이 없다 */}
              {workspace.length ? <FileGroup {...group} title={t.chat.files.madeInWorkspace} rows={workspace} empty="" testId="session-files-workspace" /> : null}
              <FileGroup {...group} title={text.agent} notice={text.agentNotice} rows={files.filter((f) => f.origin === "agent")} empty={text.agentEmpty} testId="session-files-agent" />
            </div>
            <FilePreview target={target} path={selected} />
          </div>
        )}
      </div>
    </Modal>
  );
}

interface FileGroupProps {
  title: string;
  notice?: string;
  rows: AgentSessionFileRow[];
  empty: string;
  testId: string;
  target: SessionFileTarget;
  loading: boolean;
  selected: string | null;
  onPreview: (path: string) => void;
}

/** 한 묶음의 파일 목록. 행마다 미리 보기(상한 이하만)와 다운로드가 있다. */
function FileGroup({ title, notice, rows, empty, testId, target, loading, selected, onPreview }: FileGroupProps) {
  return (
    <section className="flex flex-col gap-1.5" aria-label={title} data-testid={testId}>
      <h3 className="text-xs font-semibold text-heading">{title}</h3>
      {notice ? (
        <p className="text-[11px] text-muted" data-testid={`${testId}-notice`}>
          {notice}
        </p>
      ) : null}
      {loading ? (
        <FileListLoading />
      ) : rows.length ? (
        <ul className="flex flex-col gap-0.5">
          {rows.map((row) => (
            <FileRow key={row.path} row={row} target={target} current={row.path === selected} onPreview={onPreview} />
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted" data-testid={`${testId}-empty`}>
          {empty}
        </p>
      )}
    </section>
  );
}

function FileRow({ row, target, current, onPreview }: { row: AgentSessionFileRow; target: SessionFileTarget; current: boolean; onPreview: (path: string) => void }) {
  const t = useT();
  const text = t.chat.sessionFiles;
  const label = sessionFileLabel(row.path);
  return (
    <li className={cn("flex flex-wrap items-center gap-1 rounded-item px-2 py-1.5", current && "bg-accent-soft")} data-testid={`session-file-${row.path}`}>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-xs text-fg" title={row.path}>
          {label}
        </span>
        <FileMeta size={row.size} modifiedAt={row.modified_at} />
      </span>
      {row.size <= SESSION_FILE_PREVIEW_MAX_BYTES ? (
        <Button size="sm" variant="ghost" aria-current={current ? "true" : undefined} aria-label={text.previewLabel(label)} onClick={() => onPreview(row.path)} data-testid={`session-file-preview-${row.path}`}>
          {text.preview}
        </Button>
      ) : null}
      <DownloadButton download={() => downloadSessionFile(target.chat, target.sessionId, row.path)} variant="ghost" plain label={text.download(label)} testId={`session-file-download-${row.path}`} />
    </li>
  );
}

/** 바이트를 UTF-8 글로 푼다. 풀리지 않으면(이미지 같은 바이너리) null 이다. */
function utf8Text(bytes: ArrayBuffer): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

function FilePreview({ target, path }: { target: SessionFileTarget; path: string | null }) {
  const t = useT();
  const text = t.chat.sessionFiles;
  const tenant = useTenant();
  const { chat, sessionId } = target;
  const content = useQuery({
    queryKey: sessionFilePreviewKey(tenant, chat, sessionId, path),
    queryFn: async () => utf8Text(await readSessionFile(chat, sessionId, path!)),
    enabled: path !== null,
    refetchOnMount: "always",
  });
  return (
    <FilePreviewPane
      label={text.content}
      testId="session-file-content"
      path={path}
      hint={text.hint}
      error={content.error}
      text={content.data}
      markdownTestId="session-file-markdown"
    />
  );
}
