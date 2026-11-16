/**
 * Skill 검사 결과 (D64). 편집기의 파일 옆, 저장·가져오기가 받은 422 `invalid_skill` 이 같은 줄 모양을 쓴다.
 *
 * 문장은 사전의 검사 표(`skills.issues`)에서 `issueText` 로 온다. 서버 message 는 영어라 보이지 않되, "U+202E at line 3" 처럼 고칠 글자의 자리를 담은
 * code(`issueLocation`)만 문장 아래에 작게 둔다.
 */
import type { SkillIssue } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { invalidSkillIssues, issueLocation, issueText } from "@/lib/skills";

export function SkillIssueList({ issues, showPath = false, testId }: { issues: readonly SkillIssue[]; showPath?: boolean; testId: string }) {
  const t = useT();
  if (!issues.length) return null;
  return (
    <ul className="flex flex-col gap-1.5" data-testid={testId}>
      {issues.map((issue, i) => {
        const location = issueLocation(issue);
        return (
          <li key={`${issue.path ?? ""}:${issue.code}:${i}`} className="flex items-start gap-2 text-xs" data-testid={`${testId}-${issue.code}`} data-severity={issue.severity}>
            <Badge variant={issue.severity === "error" ? "error" : "warn"} className="shrink-0">
              {t.skills.severity[issue.severity]}
            </Badge>
            <div className="min-w-0">
              {showPath && issue.path ? <span className="mr-1.5 font-mono text-muted">{issue.path}</span> : null}
              <span className={issue.severity === "error" ? "text-err-fg" : "text-warn-fg"}>{issueText(t, issue)}</span>
              {location ? <div className="break-all font-mono text-[11px] text-muted">{location}</div> : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * 저장·가져오기의 오류. 형식 검사에 걸렸으면 문장 아래에 issue 를 경로와 함께 보인다. 직접 작성은 그 issue 를 파일 옆에 세우므로
 * `withIssues` 를 끄고 문장만 보인다.
 */
export function SkillSaveError({ error, testId, withIssues = true }: { error: unknown; testId: string; withIssues?: boolean }) {
  if (!error) return null;
  const issues = withIssues ? invalidSkillIssues(error) : null;
  return (
    <div className="flex flex-col gap-2">
      <ErrorMessage error={error} testId={testId} />
      {issues?.length ? <SkillIssueList issues={issues} showPath testId={`${testId}-issues`} /> : null}
    </div>
  );
}
