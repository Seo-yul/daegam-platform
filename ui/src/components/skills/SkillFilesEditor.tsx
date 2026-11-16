/**
 * Skill 파일 편집기 (D64, P39 4절 "직접 작성"). 왼쪽은 파일 목록과 파일 추가, 오른쪽은 선택한 파일의 Monaco 다.
 *
 * 검사 결과는 파일마다 목록 옆에 수로, 선택한 파일 아래에 줄로 보인다. 경로 규칙은 서버가 보므로 파일 추가는 같은 경로만
 * 막는다. 같은 경로가 둘이면 편집기가 어느 쪽을 보일지 정할 수 없기 때문이다. Monaco 는 무거워 lazy 다.
 *
 * 파일을 지우면 누른 삭제 단추가 줄과 함께 사라지므로 초점을 그 뒤에 고른 파일의 줄로 옮긴다. body 로 떨어지면 키보드 사용자가
 * 화면 처음부터 다시 밟아야 한다.
 */
import { Trash2 } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState, type KeyboardEvent } from "react";

import type { SkillFileInput, SkillIssue } from "@/api/types";
import { EditorSkeleton } from "@/components/EditorSkeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FileListButton } from "@/components/ui/file-list";
import { IconButton } from "@/components/ui/icon-button";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { SKILL_MD, byPath, errorsIn, fileLanguage } from "@/lib/skills";

import { SkillIssueList } from "./SkillIssues";

const MonacoText = lazy(() => import("@/components/MonacoText"));

const EDITOR_HEIGHT = "26rem";

export function SkillFilesEditor({
  files,
  onChange,
  issues,
  disabled = false,
  testId = "skill-files",
}: {
  files: SkillFileInput[];
  onChange: (next: SkillFileInput[]) => void;
  /** 마지막 검사 결과. path 가 없거나 목록에 없는 경로의 것은 맨 위에 모은다. */
  issues: readonly SkillIssue[];
  disabled?: boolean;
  testId?: string;
}) {
  const t = useT();
  const text = t.skills.editor;
  const [selected, setSelected] = useState(SKILL_MD);
  const [newPath, setNewPath] = useState("");
  const list = useRef<HTMLUListElement>(null);
  const [refocus, setRefocus] = useState(false);
  const sorted = [...files].sort((a, b) => byPath(a.path, b.path));
  const current = files.find((f) => f.path === selected) ?? sorted[0];
  const paths = new Set(files.map((f) => f.path));
  const general = issues.filter((i) => i.path === null || !paths.has(i.path));
  const duplicate = paths.has(newPath.trim());

  function update(path: string, content: string) {
    onChange(files.map((f) => (f.path === path ? { ...f, content } : f)));
  }
  function add() {
    const path = newPath.trim();
    if (!path || duplicate) return;
    onChange([...files, { path, content: "" }]);
    setSelected(path);
    setNewPath("");
  }
  function remove(path: string) {
    onChange(files.filter((f) => f.path !== path));
    if (selected === path) setSelected(SKILL_MD);
    setRefocus(true);
  }
  // 지운 줄이 빠진 렌더가 그려진 뒤에 고른 파일의 줄로 초점을 옮긴다
  useEffect(() => {
    if (!refocus) return;
    setRefocus(false);
    list.current?.querySelector<HTMLElement>('button[aria-current="true"]')?.focus();
  }, [refocus]);
  // 이 칸의 Enter 는 폼 저장이 아니라 파일 추가다
  function onAddKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
    e.preventDefault();
    add();
  }

  return (
    <div className="flex flex-col gap-3" data-testid={testId}>
      <SkillIssueList issues={general} showPath testId={`${testId}-general-issues`} />
      <div className="grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
        <aside className="flex min-w-0 flex-col gap-2">
          <ul ref={list} className="flex flex-col gap-0.5" aria-label={t.skills.files.listLabel}>
            {sorted.map((f) => (
              <FileRow
                key={f.path}
                path={f.path}
                issues={issues.filter((i) => i.path === f.path)}
                active={f.path === current?.path}
                onSelect={() => setSelected(f.path)}
                onRemove={f.path === SKILL_MD || disabled ? undefined : () => remove(f.path)}
                testId={testId}
              />
            ))}
          </ul>
          <div className="flex gap-1.5">
            <Input
              className="h-8 font-mono text-xs"
              aria-label={text.addPath}
              placeholder="references/tone.md"
              data-testid={`${testId}-add-path`}
              value={newPath}
              disabled={disabled}
              onChange={(e) => setNewPath(e.target.value)}
              onKeyDown={onAddKey}
            />
            <Button size="sm" variant="secondary" data-testid={`${testId}-add`} disabled={disabled || !newPath.trim() || duplicate} onClick={add}>
              {text.add}
            </Button>
          </div>
          {duplicate ? <p className="text-xs text-warn-fg">{text.duplicate}</p> : null}
        </aside>
        {current ? (
          <div className="flex min-w-0 flex-col gap-2">
            <code className="font-mono text-xs text-muted" data-testid={`${testId}-current`}>
              {current.path}
            </code>
            {/* 편집기가 길어 아래에 두면 화면 밖으로 밀린다. 고칠 파일 바로 위에 둔다 */}
            <SkillIssueList issues={issues.filter((i) => i.path === current.path)} testId={`${testId}-issues`} />
            <Suspense fallback={<EditorSkeleton height={EDITOR_HEIGHT} />}>
              <MonacoText
                key={current.path}
                value={current.content}
                onChange={(next) => update(current.path, next)}
                language={fileLanguage(current.path)}
                readOnly={disabled}
                height={EDITOR_HEIGHT}
                testId={`${testId}-editor`}
              />
            </Suspense>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FileRow({
  path,
  issues,
  active,
  onSelect,
  onRemove,
  testId,
}: {
  path: string;
  issues: SkillIssue[];
  active: boolean;
  onSelect: () => void;
  /** 없으면 삭제 단추를 그리지 않는다. SKILL.md 는 지울 수 없다. */
  onRemove?: () => void;
  testId: string;
}) {
  const t = useT();
  const errors = errorsIn(issues).length;
  const warnings = issues.length - errors;
  return (
    <li className="flex items-center gap-1">
      <FileListButton active={active} onClick={onSelect} testId={`${testId}-file-${path}`} className="flex-1 font-mono">
        <span className="min-w-0 truncate">{path}</span>
        {errors ? (
          <Badge variant="error" data-testid={`${testId}-errors-${path}`}>
            {errors}
          </Badge>
        ) : null}
        {warnings ? (
          <Badge variant="warn" data-testid={`${testId}-warnings-${path}`}>
            {warnings}
          </Badge>
        ) : null}
      </FileListButton>
      {onRemove ? (
        <IconButton label={t.resources.delete.rowLabel(path)} testId={`${testId}-remove-${path}`} danger className="size-7" onClick={onRemove}>
          <Trash2 size={14} />
        </IconButton>
      ) : null}
    </li>
  );
}
