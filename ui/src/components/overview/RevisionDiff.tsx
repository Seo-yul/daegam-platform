/**
 * 리비전 둘을 골라 견주는 자리. MCP Server 와 Gateway 상세의 정의 탭, Skill 상세의 리비전 탭이 같이 쓴다.
 *
 * 세 화면이 같은 상태·같은 기본값·같은 선택칸을 각자 적고 있었다. 기본값 규칙("마지막과 그 앞")이
 * 여러 곳에 있으면 한쪽만 고치는 날이 오고, 그때 같은 화면처럼 보이는 둘이 다르게 움직인다.
 *
 * 무엇을 견줄지(`specOf`)는 리소스마다 다르므로 인자로 받는다. Skill 은 스펙 하나가 아니라 파일별로 견주므로 고른 두 리비전
 * (`useRevisionPair`)만 쓴다. 목록의 차례도 다르다. MCP Server·Gateway 는 오래된 것부터, Skill 은 새것부터 온다.
 * Agent 는 캔버스라 이 꼴이 아니다.
 */
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { diffJson, type DiffEntry } from "@/lib/diff";

export interface RevisionLike {
  revision: number;
}

/** 고른 두 리비전. 고르개의 값은 문자열이다. */
export interface RevisionPair<T extends RevisionLike> {
  from: string;
  to: string;
  setFrom: (v: string) => void;
  setTo: (v: string) => void;
  revFrom: T | undefined;
  revTo: T | undefined;
}

export interface RevisionDiff<T extends RevisionLike> extends RevisionPair<T> {
  diff: DiffEntry[];
}

/** 목록의 차례. MCP Server·Gateway 의 리비전 목록은 오래된 것부터, Skill 의 것은 새것부터 온다. */
export type RevisionOrder = "oldest-first" | "newest-first";

/** 마지막 리비전과 그 앞. 리비전이 하나면 둘 다 그것이다. */
function defaultPair<T extends RevisionLike>(revisions: T[], order: RevisionOrder): { from: string; to: string } {
  if (!revisions.length) return { from: "", to: "" };
  const last = revisions.length - 1;
  const [latest, previous] = order === "newest-first" ? [revisions[0], revisions[Math.min(1, last)]] : [revisions[last], revisions[Math.max(0, last - 1)]];
  return { from: String(previous.revision), to: String(latest.revision) };
}

/**
 * 고른 두 리비전. 처음 열면 마지막 리비전과 그 앞을 고른다.
 *
 * 목록이 나중에 도착할 수 있으므로 `to` 가 비어 있을 때만 채운다. 사람이 고른 뒤에는 목록이 다시 와도
 * 덮지 않는다.
 */
export function useRevisionPair<T extends RevisionLike>(revisions: T[], order: RevisionOrder = "oldest-first"): RevisionPair<T> {
  const [from, setFrom] = useState(() => defaultPair(revisions, order).from);
  const [to, setTo] = useState(() => defaultPair(revisions, order).to);
  useEffect(() => {
    if (revisions.length && !to) {
      const pair = defaultPair(revisions, order);
      setTo(pair.to);
      setFrom(pair.from);
    }
  }, [revisions, to, order]);
  const revFrom = revisions.find((r) => String(r.revision) === from);
  const revTo = revisions.find((r) => String(r.revision) === to);
  return { from, to, setFrom, setTo, revFrom, revTo };
}

/** 고른 두 리비전과 그 스펙의 차이. */
export function useRevisionDiff<T extends RevisionLike>(revisions: T[], specOf: (r: T) => unknown): RevisionDiff<T> {
  const pair = useRevisionPair(revisions);
  const diff = pair.revFrom && pair.revTo ? diffJson(specOf(pair.revFrom), specOf(pair.revTo)) : [];
  return { ...pair, diff };
}

/** 카드 머리의 `diff rN → rM` 고르개. */
export function RevisionDiffSelects<T extends RevisionLike>({ revisions, state }: { revisions: T[]; state: RevisionPair<T> }): ReactNode {
  const text = useT().detail.revisionDiff;
  return (
    <div className="flex items-center gap-2 text-xs">
      {text.diff}
      <Select aria-label={text.from} data-testid="diff-from" className="h-8 w-20" value={state.from} onChange={(e) => state.setFrom(e.target.value)}>
        {revisions.map((r) => (
          <option key={r.revision} value={String(r.revision)}>
            r{r.revision}
          </option>
        ))}
      </Select>
      →
      <Select aria-label={text.to} data-testid="diff-to" className="h-8 w-20" value={state.to} onChange={(e) => state.setTo(e.target.value)}>
        {revisions.map((r) => (
          <option key={r.revision} value={String(r.revision)}>
            r{r.revision}
          </option>
        ))}
      </Select>
    </div>
  );
}

/**
 * 미리보기 고르개: 리비전·클러스터와 실행 버튼.
 *
 * 클러스터를 비우면 테넌트 기본값이다. 두 화면이 같은 세 칸을 적고 있었다.
 */
export function PreviewPicker({
  revisions,
  clusters,
  revision,
  onRevision,
  cluster,
  onCluster,
  pending,
  onRun,
}: {
  revisions: number[];
  clusters: { id: string; name: string }[];
  revision: string;
  onRevision: (v: string) => void;
  cluster: string;
  onCluster: (v: string) => void;
  pending: boolean;
  onRun: () => void;
}): ReactNode {
  const t = useT();
  const text = t.detail.revisionDiff;
  return (
    <div className="flex items-center gap-2">
      <Select aria-label={text.previewRevision} data-testid="preview-revision" className="h-8 w-24" value={revision} onChange={(e) => onRevision(e.target.value)}>
        {revisions.map((n) => (
          <option key={n} value={String(n)}>
            r{n}
          </option>
        ))}
      </Select>
      <Select aria-label={text.previewCluster} data-testid="preview-cluster" className="h-8 w-40" value={cluster} onChange={(e) => onCluster(e.target.value)}>
        <option value="">{t.shared.tenantDefault}</option>
        {clusters.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </Select>
      <Button size="sm" data-testid="preview-button" disabled={pending || !revision} onClick={onRun}>
        {pending ? text.previewing : text.preview}
      </Button>
    </div>
  );
}
