/**
 * 텍스트 파일의 내용과 미리 보기 칸 (P36 8절·10절). 기억 서랍과 세션 파일 서랍이 함께 쓴다. `.md` 는 마크다운으로 그리고 나머지는
 * 글자 그대로 보인다. 마크다운은 원문 HTML 을 글자로 보이므로 에이전트가 쓴 파일도 화면에서 스크립트가 돌지 않는다.
 *
 * 미리 보기 칸(`FilePreviewPane`)은 선택 전, 오류, 불러오는 중, 텍스트 아님, 내용의 다섯 상태다. 오류의 data-testid 는 칸의
 * testId 에 `-error` 를 붙인 것이다. 목록 행의 크기·시각 줄은 `FileMeta`, 목록을 읽는 동안의 줄은 `FileListLoading` 이다.
 */
import type { ReactNode } from "react";

import { ErrorMessage } from "@/components/ErrorMessage";
import { Markdown } from "@/components/markdown/Markdown";
import { CodePre } from "@/components/ui/code-snippet";
import { useT, type Messages } from "@/i18n";
import { isMarkdownPath } from "@/lib/files";
import { formatBytes, formatDate } from "@/lib/utils";

function FileBody({ path, text, markdownTestId }: { path: string; text: string; markdownTestId?: string }) {
  if (isMarkdownPath(path)) return <Markdown text={text} testId={markdownTestId} />;
  return <CodePre className="whitespace-pre-wrap">{text}</CodePre>;
}

/** 서랍의 파일 목록을 읽는 동안의 줄. */
export function FileListLoading() {
  const t = useT();
  return <p className="text-xs text-muted">{t.shell.sessionList.loading}</p>;
}

/** 목록 행의 크기와 바뀐 시각 한 줄. */
export function FileMeta({ size, modifiedAt }: { size: number; modifiedAt: string | null | undefined }) {
  const t = useT();
  return (
    <span className="block truncate text-[10px] text-muted">
      {formatBytes(size)} · {formatDate(t, modifiedAt)}
    </span>
  );
}

interface FilePreviewPaneProps {
  /** 화면 낭독기가 읽는 칸 이름이다. */
  label: string;
  testId: string;
  /** 고른 파일의 경로다. null 이면 아직 고르지 않았다. */
  path: string | null;
  /** 고르기 전의 안내다. */
  hint: string;
  error: unknown;
  /** 파일의 글이다. undefined 면 아직 불러오는 중이고 null 이면 글로 풀 수 없는 파일이다. */
  text: string | null | undefined;
  markdownTestId: string;
}

/** 서랍 오른쪽의 미리 보기 칸. 테두리 안에서 스크롤한다. */
export function FilePreviewPane({ label, testId, ...state }: FilePreviewPaneProps) {
  const t = useT();
  return (
    <section className="min-h-0 overflow-y-auto rounded-item border border-line-soft p-3 text-sm" aria-label={label} data-testid={testId}>
      {previewBody(t, testId, state)}
    </section>
  );
}

function previewBody(t: Messages, testId: string, { path, hint, error, text, markdownTestId }: Omit<FilePreviewPaneProps, "label" | "testId">): ReactNode {
  if (path === null) return <p className="text-xs text-muted">{hint}</p>;
  if (error) return <ErrorMessage error={error} testId={`${testId}-error`} />;
  if (text === undefined) return <p className="text-xs text-muted">{t.chat.files.loadingContent}</p>;
  if (text === null) return <p className="text-xs text-muted">{t.chat.files.notText}</p>;
  return <FileBody path={path} text={text} markdownTestId={markdownTestId} />;
}
