/**
 * 검증 배지. 통과 / 오류 n · 경고 n 을 보이고 누르면 이슈 목록이 펼쳐진다. 이슈를 누르면 그 노드·간선으로 이동한다.
 * source 는 지금 보이는 이슈가 클라이언트 규칙인지 서버 `POST /validate` 응답인지다.
 * 줄에 보이는 것은 화면 언어의 문장(issueText)이고, 기계 어휘인 code 와 노드 id 는 뒤에 작은 회색 글씨로 밀어 둔다.
 */
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { countIssues, issueText, type Issue } from "@/lib/agentGraph";
import { cn } from "@/lib/utils";

export function ValidationBadge({
  issues,
  source,
  onFocusIssue,
  className,
}: {
  issues: Issue[];
  source: "client" | "server";
  onFocusIssue?: (issue: Issue) => void;
  className?: string;
}) {
  const t = useT();
  const text = t.canvas.validation;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { errors, warnings } = countIssues(issues);
  const variant = errors ? "error" : warnings ? "warn" : "ok";

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  return (
    <div className={cn("relative", className)} ref={ref}>
      <button type="button" data-testid="validation-badge" data-level={variant} aria-expanded={open} onClick={() => setOpen((v) => !v)} className="rounded-badge focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring">
        <Badge variant={variant} className="cursor-pointer">
          <span aria-hidden className={cn("inline-block h-1.5 w-1.5 rounded-full", errors ? "status-dot-err" : warnings ? "status-dot-warn" : "status-dot-ok")} />
          {text.summary(errors, warnings)}
          <span className="font-normal">{text.source[source]}</span>
        </Badge>
      </button>
      {open ? (
        <div className="popover-enter absolute left-0 top-full z-(--ui-z-popover) mt-1.5 w-[28rem] max-w-[80vw] overflow-hidden rounded-chip border border-line bg-surface-popover shadow-popover" data-testid="validation-list">
          <div className="border-b border-line-strong px-3 py-2 text-[11px] text-muted">
            {issues.length ? text.count(issues.length) : text.passed}
            {source === "client" ? ` ${text.serverHint}` : ""}
          </div>
          {issues.length ? (
            <ul className="max-h-72 overflow-y-auto p-1">
              {issues.map((issue, i) => (
                <li key={`${issue.code}-${issue.node_id ?? ""}-${issue.edge_id ?? ""}-${i}`}>
                  <button
                    type="button"
                    data-testid={`validation-issue-${i}`}
                    disabled={!issue.node_id && !issue.edge_id}
                    title={`${issue.code}${issue.node_id || issue.edge_id ? ` · ${issue.node_id ?? issue.edge_id}` : ""}`}
                    onClick={() => {
                      onFocusIssue?.(issue);
                      setOpen(false);
                    }}
                    className="flex w-full items-start gap-2 rounded-item px-2 py-1.5 text-left text-xs hover:bg-hover disabled:cursor-default disabled:hover:bg-transparent"
                  >
                    <Badge variant={issue.level === "error" ? "error" : "warn"} className="mt-px shrink-0 text-[10px]">
                      {t.skills.severity[issue.level]}
                    </Badge>
                    <span className="min-w-0 flex-1 text-fg-soft">
                      {issueText(t, issue)}
                      <span className="ml-1 font-mono text-[10px] text-muted">
                        {issue.node_id || issue.edge_id ? `${issue.node_id ?? issue.edge_id} ` : ""}
                        {issue.code}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
