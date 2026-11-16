/**
 * 캔버스 맨 위 줄. 제목·리비전 셀렉트·검증 배지와, 되돌리기/다시 하기/자동 배치/검증/저장/배포 툴바다.
 * 상태는 갖지 않고 전부 페이지에서 받는다.
 *
 * 캔버스는 Agent 의 정의다 (D42). `/agents/new` 에서는 화면 하나로 서고, 저장된 뒤에는 상세의 정의 탭
 * 안에 들어간다. 그래서 배포 버튼은 화면을 옮기지 않고 같은 문서의 배포 탭으로 보낸다.
 *
 * 꺼진 버튼은 왜 꺼졌는지 말한다. 버튼 자신에는 `disabled:pointer-events-none` 이 걸려 있어 title 이 뜨지 않으므로
 * 감싸는 span 이 title 을 갖고, 저장은 이유를 옆에 글로도 보인다 (이름 칸은 노드를 고르면 화면에서 사라지므로).
 */
import { Redo2, Undo2 } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import type { Agent } from "@/api/types";
import { ValidationBadge } from "@/components/canvas/ValidationBadge";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import type { Issue } from "@/lib/agentGraph";

export interface CanvasHeaderProps {
  isNew: boolean;
  title: string;
  agent: Agent | undefined;
  /** 지금 보고 있는 리비전. null 이면 현재 리비전이다. */
  viewingRevision: number | null;
  onSelectRevision: (n: number) => void;
  dirty: boolean;
  hasNodes: boolean;
  issues: Issue[];
  /** 표식이 서버에서 온 것인가. 아니면 캔버스가 스스로 낸 것이다. */
  showServer: boolean;
  onFocusIssue: (issue: Issue) => void;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onAutoLayout: () => void;
  onValidate: () => void;
  validating: boolean;
  onSave: () => void;
  saving: boolean;
  canSave: boolean;
  /** 저장이 꺼진 이유. 누를 수 있으면 null 이다. */
  saveDisabledReason: string | null;
  /** 검증 문맥(provider·소스·Database·Knowledge Base 목록)을 아직 못 읽었다. 그 종류의 표식은 보류 중이다. */
  contextLoading?: boolean;
  /** 상세의 정의 탭 안에서는 제목과 breadcrumb 을 상세 머리가 이미 그렸으므로 접는다. */
  embedded?: boolean;
}

/** 꺼진 버튼 하나를 감싸 이유를 title 로 말한다. 버튼 자신은 꺼지면 hover 를 받지 않는다. */
function Reason({ text, children }: { text: string | null; children: ReactNode }) {
  return (
    <span className="inline-flex" title={text ?? undefined}>
      {children}
    </span>
  );
}

export function CanvasHeader({
  isNew,
  title,
  agent,
  viewingRevision,
  onSelectRevision,
  dirty,
  hasNodes,
  issues,
  showServer,
  onFocusIssue,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onAutoLayout,
  onValidate,
  validating,
  onSave,
  saving,
  canSave,
  saveDisabledReason,
  contextLoading = false,
  embedded = false,
}: CanvasHeaderProps) {
  const t = useT();
  const text = t.agents.canvasHeader;
  const noNodes = hasNodes ? null : text.noNodes;
  // 정의 탭 안이든 독립 화면이든 같은 셀렉트다. 저장한 적이 없으면 고를 리비전이 없다
  const revisionSelect = agent ? (
    <Select aria-label={t.agents.revision} data-testid="canvas-revision" className="h-8 w-28 text-xs" value={String(viewingRevision ?? agent.revision)} onChange={(e) => onSelectRevision(Number(e.target.value))}>
      {[...agent.revisions].reverse().map((n) => (
        <option key={n} value={String(n)}>
          {text.revisionOption(n, n === agent.revision)}
        </option>
      ))}
    </Select>
  ) : null;
  return (
    <div className="flex flex-wrap items-center gap-3" data-testid="canvas-header">
      {embedded ? (
        revisionSelect
      ) : (
        <div className="flex min-w-0 flex-col">
          <Link to="/agents" className="text-[11px] text-muted underline-offset-2 hover:underline">
            {text.back}
          </Link>
          <div className="flex items-center gap-2">
            <h1 className="truncate text-xl font-bold tracking-tight text-heading" data-testid="page-title">
              {title}
            </h1>
            {revisionSelect ?? <Badge variant="muted">{text.neverSaved}</Badge>}
          </div>
        </div>
      )}
      {/* 아직 저장한 적이 없으면 "미저장" 이 이미 그 말을 한다. 배지 둘이 같은 뜻을 겹쳐 말하지 않는다 */}
      {dirty && agent ? (
        <Badge variant="warn" data-testid="canvas-dirty">
          {text.dirty}
        </Badge>
      ) : null}
      {/* 아직 아무것도 그리지 않은 캔버스에서 빨간 오류 개수를 앞세우지 않는다. 무엇이 필요한지는 빈 캔버스 안내가 말한다 */}
      {hasNodes ? (
        <ValidationBadge issues={issues} source={showServer ? "server" : "client"} onFocusIssue={onFocusIssue} />
      ) : (
        <Badge variant="muted" data-testid="canvas-validation-idle">
          {text.validationIdle}
        </Badge>
      )}
      {contextLoading ? (
        <span className="text-[11px] text-muted" data-testid="canvas-context-loading">
          {text.contextLoading}
        </span>
      ) : null}
      <div className="ml-auto flex items-center gap-2">
        {saveDisabledReason ? (
          <span className="max-w-[22rem] text-right text-[11px] text-muted" data-testid="canvas-save-reason">
            {saveDisabledReason}
          </span>
        ) : null}
        <Button variant="ghost" size="sm" data-testid="canvas-undo" disabled={!canUndo} title={text.undoTitle} aria-label={text.undo} onClick={onUndo}>
          <Undo2 size={15} />
        </Button>
        <Button variant="ghost" size="sm" data-testid="canvas-redo" disabled={!canRedo} title={text.redoTitle} aria-label={text.redo} onClick={onRedo}>
          <Redo2 size={15} />
        </Button>
        <Reason text={noNodes}>
          <Button variant="outline" size="sm" data-testid="canvas-autolayout" disabled={!hasNodes} aria-label={noNodes ? text.blocked(text.autoLayout, noNodes) : undefined} onClick={onAutoLayout}>
            {text.autoLayout}
          </Button>
        </Reason>
        <Reason text={validating ? text.validating : noNodes}>
          <Button variant="outline" size="sm" data-testid="canvas-validate" disabled={validating || !hasNodes} aria-label={noNodes ? text.blocked(text.validate, noNodes) : undefined} onClick={onValidate}>
            {validating ? text.validatingButton : text.validate}
          </Button>
        </Reason>
        <Reason text={saveDisabledReason}>
          <Button size="sm" data-testid="canvas-save" disabled={!canSave} title={canSave ? text.saveTitle : undefined} aria-label={saveDisabledReason ? text.blocked(t.shared.save, saveDisabledReason) : text.saveTitle} onClick={onSave}>
            {saving ? t.shared.saving : t.shared.save}
          </Button>
        </Reason>
        {/* 정의 탭 안에서는 배포 탭이 바로 옆에 있다. 버튼을 하나 더 두면 같은 자리로 가는 길이 둘이 된다 */}
        {embedded ? null : agent && !dirty ? (
          <Link to={`/agents/${agent.id}?tab=deployment`} className={buttonVariants({ variant: "secondary", size: "sm" })} data-testid="canvas-deploy">
            {t.shared.deployDialog.submit}
          </Link>
        ) : (
          <Reason text={isNew ? text.deployReason.new : text.deployReason.dirty}>
            <Button variant="secondary" size="sm" data-testid="canvas-deploy" disabled aria-label={isNew ? text.deployBlocked.new : text.deployBlocked.dirty}>
              {t.shared.deployDialog.submit}
            </Button>
          </Reason>
        )}
      </div>
    </div>
  );
}
