/**
 * 스펙 다듬기 결과의 좌우 diff 와 채택 목록 (D25 "채택은 사람이 한다").
 *
 * - 왼쪽은 원본 문서를 toYaml 로 편 것, 오른쪽은 채택한 변경(pointer)만 원본 위에 심어 다시 편 것이다.
 *   둘 다 같은 직렬화를 거치므로 서식 차이 없이 값 차이만 보인다. 서버의 draft_yaml 은 쓰지 않는다.
 * - 변경 목록은 /paths/{p}/{m} 접두로 operation 별 묶음이고, components.schemas 항목은 참조 operation 수를 보인다.
 * - "이동" 은 pointer 가 가리키는 원본 줄로 편집기를 옮긴다. added 처럼 원본에 없는 pointer 는 가장 가까운 조상 줄이다.
 * - renamed 가 있으면 operationId 변경 경고와 이 백엔드를 참조하는 MCP Server 목록을 보인다.
 * Monaco 는 lazy 다. 부모는 채택 집합을 들고 있다가 apply-draft 에 accepted_ids 로 보낸다.
 */
import { lazy, Suspense, useMemo, useState } from "react";
import { Link } from "react-router";

import type { MCPServer, OpenApiDocument, RenamedOperation, SpecChange, SpecChangeKind } from "@/api/types";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import { showValue } from "@/lib/diff";
import { DIFF_SIDE_BY_SIDE_MIN, useNarrowerThan } from "@/lib/media";
import { displayName } from "@/lib/names";
import { applyChanges, changeLabel, groupChanges, parentPointer, summarizeChanges } from "@/lib/pointer";
import { cn, toggleIn } from "@/lib/utils";
import { toYaml } from "@/lib/yaml";
import { linesForPointers } from "@/lib/yamlPaths";

import type { RevealRequest } from "./MonacoDiff";

const MonacoDiff = lazy(() => import("./MonacoDiff"));

export interface SpecDiffProps {
  original: OpenApiDocument;
  changes: SpecChange[];
  accepted: ReadonlySet<string>;
  onAcceptedChange: (next: Set<string>) => void;
  renamed?: RenamedOperation[] | null;
  /** 이 백엔드를 참조하는 MCP Server. undefined 면 아직 읽는 중이다. */
  referencingProfiles?: MCPServer[];
}

const KIND_VARIANT: Record<SpecChangeKind, BadgeProps["variant"]> = { changed: "info", added: "ok", removed: "warn" };

const PREVIEW_MAX = 160;

function preview(t: Messages, v: unknown): string {
  if (v === null || v === undefined) return t.detail.specDiff.none;
  const s = showValue(v).replace(/\s+/g, " ");
  return s.length > PREVIEW_MAX ? `${s.slice(0, PREVIEW_MAX)}…` : s;
}

/** pointer 와 그 조상 전부. 이동할 때 원본에 없는 pointer 는 가까운 조상으로 대신한다. */
function ancestorChain(pointer: string): string[] {
  const out: string[] = [];
  let cur: string | null = pointer;
  while (cur !== null && cur !== "") {
    out.push(cur);
    cur = parentPointer(cur);
  }
  return out;
}

export function SpecDiff({ original, changes, accepted, onAcceptedChange, renamed, referencingProfiles }: SpecDiffProps) {
  const t = useT();
  const text = t.detail.specDiff;
  const [sideBySide, setSideBySide] = useState(true);
  // 좌우로 보기는 칸이 넓어야 뜻이 있다. 좁은 칸에서 Monaco 는 나란히 두기를 고집해 원본 쪽을 몇
  // 픽셀로 찌그러뜨리고, 그러면 글자 한 칸씩만 비어져 나오는 띠가 남는다 (실측: 칸 384px 에 원본 5px).
  // Monaco 의 useInlineViewWhenSpaceIsLimited 는 이 조합에서 듣지 않아 우리가 재서 정한다
  const [diffBox, tooNarrow] = useNarrowerThan<HTMLDivElement>(DIFF_SIDE_BY_SIDE_MIN);
  const splitView = sideBySide && !tooNarrow;
  const [reveal, setReveal] = useState<RevealRequest | null>(null);

  const originalYaml = useMemo(() => toYaml(original), [original]);
  const modifiedYaml = useMemo(
    () => toYaml(applyChanges(original, changes.filter((c) => accepted.has(c.id)))),
    [original, changes, accepted],
  );
  const groups = useMemo(() => groupChanges(t, original, changes), [t, original, changes]);
  const summary = useMemo(() => summarizeChanges(original, groups), [original, groups]);

  // 원본 YAML 을 한 번만 파싱해 모든 pointer 와 조상의 줄 번호를 둔다
  const lineIndex = useMemo(() => {
    const wanted = new Set<string>();
    for (const c of changes) for (const p of ancestorChain(c.pointer)) wanted.add(p);
    return new Map(linesForPointers(originalYaml, [...wanted]).map((p) => [p.path, p.lines] as const));
  }, [originalYaml, changes]);

  function jump(change: SpecChange) {
    for (const p of ancestorChain(change.pointer)) {
      const line = lineIndex.get(p)?.[0];
      if (line) {
        setReveal({ line, nonce: Date.now() });
        return;
      }
    }
  }

  function toggle(id: string, on: boolean) {
    onAcceptedChange(toggleIn(accepted, id, on));
  }

  const acceptedCount = changes.filter((c) => accepted.has(c.id)).length;

  return (
    <div data-testid="spec-diff" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-fg" data-testid="spec-diff-summary">
          {text.summary(summary.totalOperations, summary.changedOperations)}
        </p>
        <span className="text-xs text-muted" data-testid="spec-diff-count">
          {t.detail.changeCount(changes.length, acceptedCount)}
        </span>
      </div>

      {renamed?.length ? (
        <Alert variant="warn" data-testid="renamed-warning">
          <div className="font-medium">{text.renamed}</div>
          <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-xs" data-testid="renamed-list">
            {renamed.map((r) => (
              <li key={`${r.from}>${r.to}`}>
                {r.from} → {r.to}
              </li>
            ))}
          </ul>
          <div className="mt-1 text-xs" data-testid="renamed-profiles">
            {referencingProfiles === undefined
              ? text.checkingProfiles
              : referencingProfiles.length
                ? text.profiles(
                    referencingProfiles.map((p, i) => (
                      <span key={p.id}>
                        {i > 0 ? ", " : null}
                        <Link to={`/mcp-servers/${p.id}`} className="underline underline-offset-2" data-testid={`renamed-profile-${p.name}`}>
                          {displayName(p)}
                        </Link>
                      </span>
                    )),
                  )
                : text.noProfiles}
          </div>
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
        <aside className="flex min-w-0 flex-col gap-2" data-testid="change-list">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-fg-soft">{text.changeList}</span>
            <div className="ml-auto flex gap-1">
              <Button size="sm" variant="ghost" data-testid="accept-all" onClick={() => onAcceptedChange(new Set(changes.map((c) => c.id)))}>
                {text.acceptAll}
              </Button>
              <Button size="sm" variant="ghost" data-testid="accept-none" onClick={() => onAcceptedChange(new Set())}>
                {text.acceptNone}
              </Button>
            </div>
          </div>
          {groups.length === 0 ? <p className="text-sm text-muted">{text.noChanges}</p> : null}
          <div className="flex max-h-[32rem] flex-col gap-2 overflow-y-auto pr-1">
            {groups.map((g) => (
              <section key={g.key} data-testid={`change-group-${g.key}`} className="rounded-chip border border-line bg-surface">
                <header className="flex items-baseline gap-2 border-b border-line-soft px-3 py-1.5">
                  <span className="font-mono text-xs font-semibold text-heading">{g.title}</span>
                  {g.subtitle ? (
                    <span className="truncate text-[11px] text-muted" data-testid="group-subtitle">
                      {g.subtitle}
                    </span>
                  ) : null}
                </header>
                <ul className="flex flex-col">
                  {g.changes.map((c) => {
                    const on = accepted.has(c.id);
                    return (
                      <li key={c.id} data-testid={`change-${c.id}`} data-accepted={on ? "true" : "false"} className={cn("flex flex-col gap-1 px-3 py-2 text-xs", !on && "opacity-60")}>
                        <div className="flex items-center gap-2">
                          <label className="flex min-w-0 flex-1 items-center gap-2">
                            <input type="checkbox" data-testid={`change-${c.id}-accept`} checked={on} onChange={(e) => toggle(c.id, e.target.checked)} />
                            <Badge variant={KIND_VARIANT[c.kind]}>{text.kind[c.kind]}</Badge>
                            <span className="truncate font-mono" title={c.pointer}>
                              {changeLabel(g, c)}
                            </span>
                          </label>
                          <button type="button" className="shrink-0 text-muted underline underline-offset-2 hover:text-fg" data-testid={`change-${c.id}-jump`} onClick={() => jump(c)}>
                            {text.jump}
                          </button>
                        </div>
                        {c.kind !== "added" ? (
                          <div className="truncate pl-6 text-err-fg" title={showValue(c.before)}>
                            − {preview(t, c.before)}
                          </div>
                        ) : null}
                        {c.kind !== "removed" ? (
                          <div className="truncate pl-6 text-ok-fg" title={showValue(c.after)}>
                            + {preview(t, c.after)}
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        </aside>

        <div className="flex min-w-0 flex-col gap-2" ref={diffBox}>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted" data-testid="diff-layout-note">
              {splitView ? text.splitNote : text.inlineNote}
            </span>
            {/* 칸이 좁으면 좌우로 두어도 읽을 수 없다. 그때는 버튼을 세우되 왜 안 되는지 말한다 */}
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              data-testid="diff-layout"
              aria-pressed={splitView}
              disabled={tooNarrow}
              title={tooNarrow ? text.tooNarrow : undefined}
              onClick={() => setSideBySide((v) => !v)}
            >
              {splitView ? text.inlineView : text.splitView}
            </Button>
          </div>
          <Suspense fallback={<div className="h-[32rem] animate-pulse rounded-md bg-hover" data-testid="diff-loading" />}>
            {/* 좌우·인라인이 바뀌면 편집기를 다시 만든다. Monaco 는 renderSideBySide 를 뜬 뒤에 바꿔도
                 듣지 않아서, 폭을 재서 정하는 답이 늦게 와도 첫 모습 그대로 남는다 */}
            <MonacoDiff key={splitView ? "split" : "inline"} original={originalYaml} modified={modifiedYaml} sideBySide={splitView} inlineBreakpoint={DIFF_SIDE_BY_SIDE_MIN} reveal={reveal} testId="spec-diff-editor" />
          </Suspense>
        </div>
      </div>
    </div>
  );
}
