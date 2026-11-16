/**
 * Skill 상세의 파일 탭 (D64, P39 4절). 왼쪽은 폴더 트리, 오른쪽은 선택한 파일의 보기다.
 *
 * `.md` 는 채팅과 같은 마크다운 부품(`components/markdown/Markdown.tsx`)으로 미리 보기를 그린다. 원시 HTML 과 이미지는 그리지
 * 않고 링크는 `lib/markdownLinks` 가 거른다. SKILL.md 의 머리말은 개요 탭이 보이므로 뗀다. 나머지 파일은 읽기 전용 Monaco 다.
 * 스크립트로 보는 파일(서버가 `script` 로 알린다)에는 실행되지 않는다는 표시가 붙는다.
 */
import { Folder } from "lucide-react";
import { lazy, Suspense, useState } from "react";

import type { SkillFile } from "@/api/types";
import { EditorSkeleton } from "@/components/EditorSkeleton";
import { Markdown } from "@/components/markdown/Markdown";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FileListButton } from "@/components/ui/file-list";
import { useT } from "@/i18n";
import { isMarkdownPath } from "@/lib/files";
import { SKILL_MD, fileTree, skillBody } from "@/lib/skills";
import { formatBytes } from "@/lib/utils";

const MonacoText = lazy(() => import("@/components/MonacoText"));

const VIEWER_HEIGHT = "28rem";
/** 트리 한 단계의 들여쓰기(rem). */
const INDENT_REM = 0.875;

function ScriptBadge({ testId }: { testId?: string }) {
  const t = useT();
  return (
    <Badge variant="warn" title={t.skills.issues.skill_scripts_inert} data-testid={testId}>
      {t.skills.files.scriptBadge}
    </Badge>
  );
}

export function SkillFileBrowser({ files, revision, testId = "skill-files" }: { files: SkillFile[]; revision: number; testId?: string }) {
  const t = useT();
  const text = t.skills.files;
  const [selected, setSelected] = useState(SKILL_MD);
  const current = files.find((f) => f.path === selected) ?? files.find((f) => f.path === SKILL_MD) ?? files[0];
  const rows = fileTree(files.map((f) => f.path));
  const scripts = new Set(files.filter((f) => f.script).map((f) => f.path));

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted">{text.count(revision, files.length)}</span>
      </CardHeader>
      <CardContent className="grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
        <ul className="flex min-w-0 flex-col gap-0.5" aria-label={text.listLabel}>
          {rows.map((row) =>
            row.dir ? (
              <li key={`dir:${row.path}`} className="flex items-center gap-1.5 px-2 py-1 text-xs text-muted" style={{ paddingLeft: `${0.5 + row.depth * INDENT_REM}rem` }}>
                <Folder size={13} aria-hidden />
                {row.name}
              </li>
            ) : (
              <li key={row.path}>
                <FileListButton
                  active={row.path === current?.path}
                  testId={`${testId}-file-${row.path}`}
                  className="font-mono"
                  style={{ paddingLeft: `${0.5 + row.depth * INDENT_REM}rem` }}
                  onClick={() => setSelected(row.path)}
                >
                  <span className="min-w-0 truncate">{row.name}</span>
                  {scripts.has(row.path) ? (
                    <span role="img" className="size-1.5 shrink-0 rounded-full bg-warn" aria-label={text.script} title={text.scriptBadge} />
                  ) : null}
                </FileListButton>
              </li>
            ),
          )}
        </ul>
        {current ? <FileView file={current} testId={testId} /> : null}
      </CardContent>
    </Card>
  );
}

function FileView({ file, testId }: { file: SkillFile; testId: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <code className="font-mono text-xs text-muted" data-testid={`${testId}-current`}>
          {file.path}
        </code>
        <span className="text-xs text-dim">{formatBytes(file.bytes)}</span>
        {file.script ? <ScriptBadge testId={`${testId}-script`} /> : null}
      </div>
      {isMarkdownPath(file.path) ? (
        <div className="rounded-md border border-line px-4 py-3 text-sm" data-testid={`${testId}-preview`}>
          <Markdown text={file.path === SKILL_MD ? skillBody(file.content) : file.content} testId={`${testId}-preview`} />
        </div>
      ) : (
        <Suspense fallback={<EditorSkeleton height={VIEWER_HEIGHT} />}>
          <MonacoText key={file.path} value={file.content} language="plaintext" readOnly height={VIEWER_HEIGHT} testId={`${testId}-viewer`} />
        </Suspense>
      )}
    </div>
  );
}
