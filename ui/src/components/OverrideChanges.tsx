/**
 * 도구 오버라이드 제안의 변경 목록 (D37).
 *
 * `SpecDiff` 를 쓰지 않는다. 그쪽은 OpenAPI 문서를 전제해서 `groupChanges` 가 오버라이드 pointer 를
 * `other:backends` 한 묶음에 떨어뜨리고 상단 요약이 "operation 0개" 가 된다. `OpenApiDocument` 가
 * `Record<string, unknown>` 이라 프로필 조각도 타입을 만족해 `tsc` 가 잡아 주지 않는다. 컴파일은
 * 지나고 화면에서만 틀리므로 묶음 기준을 여기서 직접 정한다.
 *
 * 채택 단위가 두 모양이다. 오버라이드가 이미 있던 operation 은 `.../{op}/name` 처럼 leaf 하나이고,
 * 없던 operation 은 `.../{op}` 통째가 한 단위라 이름만 받고 설명은 거절할 수 없다. 화면이 그 차이를
 * 말해 주어야 사용자가 왜 체크박스가 하나뿐인지 안다.
 */
import { useT } from "@/i18n";
import { parsePointer } from "@/lib/pointer";
import { ownValue, toggleIn } from "@/lib/utils";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import type { SpecChange } from "@/api/types";

/** 오버라이드 항목 하나의 값. 서버 모델(ToolOverride)과 같은 모양이다. */
interface OverrideValue {
  name?: string | null;
  description?: string | null;
}

function asOverride(value: unknown): OverrideValue | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const name = typeof v.name === "string" ? v.name : null;
  const description = typeof v.description === "string" ? v.description : null;
  return { name, description };
}

function asText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

export interface OverrideChangeGroup {
  key: string;
  backendIndex: number;
  operationId: string;
  /** 항목 통째가 한 단위인 변경. 있으면 leaf 변경은 없다. */
  whole: SpecChange | null;
  /** field 별 leaf 변경 (name·description). */
  leaves: { field: string; change: SpecChange }[];
}

/**
 * pointer 를 backend 와 operation 으로 묶는다. 문서 순서를 지킨다.
 *
 * 모양이 맞지 않는 pointer 는 버린다. 표면 보호가 이미 그런 것을 막으므로 여기 올 일이 없고,
 * 와도 화면이 깨지는 것보다 안 보이는 편이 낫다.
 */
export function groupOverrideChanges(changes: readonly SpecChange[]): OverrideChangeGroup[] {
  const groups = new Map<string, OverrideChangeGroup>();
  for (const c of changes) {
    const parts = parsePointer(c.pointer);
    if (parts[0] !== "backends" || parts[2] !== "tool_overrides" || parts.length < 4) continue;
    const backendIndex = Number(parts[1]);
    const operationId = parts[3];
    if (!Number.isInteger(backendIndex)) continue;
    const key = `${backendIndex}/${operationId}`;
    let g = groups.get(key);
    if (!g) {
      g = { key, backendIndex, operationId, whole: null, leaves: [] };
      groups.set(key, g);
    }
    if (parts.length === 4) g.whole = c;
    else g.leaves.push({ field: parts[4], change: c });
  }
  return [...groups.values()];
}

interface Props {
  changes: readonly SpecChange[];
  accepted: Set<string>;
  onAcceptedChange: (next: Set<string>) => void;
  /** backend 를 사람이 아는 이름으로. 여러 backend 를 가진 프로필에서 어느 쪽인지 말해 준다. */
  backendLabel?: (index: number) => string | undefined;
}

export function OverrideChanges({ changes, accepted, onAcceptedChange, backendLabel }: Props) {
  const t = useT();
  const text = t.detail.overrideChanges;
  const groups = groupOverrideChanges(changes);
  const ids = changes.map((c) => c.id);
  const allOn = ids.length > 0 && ids.every((id) => accepted.has(id));

  function toggle(id: string, on: boolean) {
    onAcceptedChange(toggleIn(accepted, id, on));
  }

  if (!groups.length) {
    return (
      <Card data-testid="override-changes">
        <CardContent className="text-sm text-muted" data-testid="override-changes-empty">
          {text.empty}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card data-testid="override-changes">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <Badge variant="muted" data-testid="override-changes-count">
          {t.detail.changeCount(ids.length, ids.filter((id) => accepted.has(id)).length)}
        </Badge>
        <Button
          type="button"
          variant="ghost"
          className="ml-auto h-7 text-xs"
          data-testid="override-toggle-all"
          onClick={() => onAcceptedChange(allOn ? new Set() : new Set(ids))}
        >
          {allOn ? text.clearAll : text.selectAll}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {groups.map((g) => {
          const label = backendLabel?.(g.backendIndex);
          return (
            <section key={g.key} data-testid={`override-group-${g.key}`} className="flex flex-col gap-1.5">
              <h3 className="flex items-center gap-2 text-sm">
                <span className="font-mono text-xs">{g.operationId}</span>
                <span className="text-xs text-muted" data-testid={`override-group-${g.key}-backend`}>
                  {text.backend} #{g.backendIndex + 1}
                  {label ? ` · ${label}` : ""}
                </span>
              </h3>
              {g.whole ? (
                <Row
                  change={g.whole}
                  checked={accepted.has(g.whole.id)}
                  onToggle={toggle}
                  rows={fieldsOfWhole(g.whole)}
                  note={text.wholeNote}
                />
              ) : (
                g.leaves.map(({ field, change }) => (
                  <Row
                    key={change.id}
                    change={change}
                    checked={accepted.has(change.id)}
                    onToggle={toggle}
                    rows={[{ field, before: asText(change.before), after: asText(change.after) }]}
                  />
                ))
              )}
            </section>
          );
        })}
      </CardContent>
    </Card>
  );
}

function fieldsOfWhole(change: SpecChange): { field: string; before: string | null; after: string | null }[] {
  const after = asOverride(change.after);
  const before = asOverride(change.before);
  return (["name", "description"] as const)
    .filter((f) => after?.[f])
    .map((f) => ({ field: f, before: before?.[f] ?? null, after: after?.[f] ?? null }));
}

function Row({
  change,
  checked,
  onToggle,
  rows,
  note,
}: {
  change: SpecChange;
  checked: boolean;
  onToggle: (id: string, on: boolean) => void;
  rows: { field: string; before: string | null; after: string | null }[];
  note?: string;
}) {
  const t = useT();
  const text = t.detail.overrideChanges;
  return (
    <label className="flex items-start gap-2 rounded-item bg-surface px-2 py-1.5 text-sm" data-testid={`override-change-${change.id}`}>
      <input
        type="checkbox"
        className="mt-1"
        data-testid={`override-accept-${change.id}`}
        checked={checked}
        onChange={(e) => onToggle(change.id, e.target.checked)}
      />
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        {rows.map((r) => (
          <span key={r.field} className="flex min-w-0 flex-col gap-0.5">
            <span className="text-xs text-muted">{ownValue(text.field, r.field) ?? r.field}</span>
            <span className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-2">
              <span className="min-w-0 break-words text-xs text-muted line-through" data-testid={`override-before-${change.id}-${r.field}`}>
                {r.before ?? text.none}
              </span>
              <span className="min-w-0 break-words" data-testid={`override-after-${change.id}-${r.field}`}>
                {r.after}
              </span>
            </span>
          </span>
        ))}
        {note ? <span className="text-xs text-muted">{note}</span> : null}
      </span>
    </label>
  );
}
