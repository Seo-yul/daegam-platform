/**
 * 리비전 표의 두 조각. MCP Server 와 Gateway 상세의 정의 탭이 같이 쓴다.
 *
 * `RevisionDiff` 가 고르는 칸까지는 한 곳에 모았는데 그 아래 차이 표와 리비전 칸 배지는 두 화면이
 * 글자까지 같게 각자 적고 있었다.
 */
import type { ReactNode } from "react";

import type { RevisionDiff, RevisionLike } from "@/components/overview/RevisionDiff";
import { Badge } from "@/components/ui/badge";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { showValue } from "@/lib/diff";

/** 고른 두 리비전의 경로별 차이. 둘 중 하나라도 아직 안 골랐으면 그리지 않는다. */
export function RevisionDiffView<T extends RevisionLike>({ state }: { state: RevisionDiff<T> }): ReactNode {
  const t = useT();
  const text = t.detail.revisionDiffView;
  const columns = t.shared.diffColumns;
  const { revFrom, revTo, diff } = state;
  if (!revFrom || !revTo) return null;
  return (
    <div data-testid="revision-diff">
      <div className="mb-1 text-sm font-medium">
        {text.summary(revFrom.revision, revTo.revision, diff.length)}
      </div>
      {diff.length ? (
        <Table>
          <THead>
            <tr>
              <TH>{columns.path}</TH>
              <TH>{columns.before}</TH>
              <TH>{columns.after}</TH>
            </tr>
          </THead>
          <TBody>
            {diff.map((d) => (
              <TR key={d.path} data-testid={`diff-${d.path}`}>
                <TD className="font-mono text-xs">{d.path}</TD>
                <TD className="font-mono text-xs text-err-fg">{showValue(d.from)}</TD>
                <TD className="font-mono text-xs text-ok-fg">{showValue(d.to)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <p className="text-sm text-muted">{text.same}</p>
      )}
    </div>
  );
}

/** 리비전 칸: `rN` 과, 지금 정의인지·배포된 것인지를 가리키는 배지. */
export function RevisionBadges({ revision, current, deployed }: { revision: number; current: number; deployed: number | null }) {
  const text = useT().detail.revisionDiffView;
  return (
    <>
      r{revision}
      {revision === current ? (
        <Badge variant="info" className="ml-2">
          {text.current}
        </Badge>
      ) : null}
      {revision === deployed ? (
        <Badge variant="ok" className="ml-1">
          {text.deployed}
        </Badge>
      ) : null}
    </>
  );
}
