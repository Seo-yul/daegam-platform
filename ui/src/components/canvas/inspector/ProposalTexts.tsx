/**
 * 고른 노드에서 청지기의 제안이 바꾸는 글의 앞과 뒤 전체 (P48 6.5절). 승인 카드는 줄 비교를 잘라 보이므로 다 읽으려면 여기서
 * 본다. 앞은 기준 리비전의 글, 뒤는 플랫폼이 짜 맞춘 정의의 글이다. 칸 이름은 인스펙터가 그 칸에 다는 이름이다.
 */
import { Label } from "@/components/ui/input";
import { useT } from "@/i18n";
import { textFieldLabel } from "@/lib/assistant";
import { cn } from "@/lib/utils";

import type { ProposalText } from "./types";

export function ProposalTexts({ texts }: { texts: ProposalText[] }) {
  const t = useT();
  const columns = t.shared.diffColumns;
  return (
    <div className="flex flex-col gap-2 rounded-item border border-accent-line bg-accent-soft p-2.5" data-testid="inspector-proposal-texts">
      <span className="text-xs font-semibold text-heading">{t.shared.approvalCard.proposal.textChanges}</span>
      {texts.map((x) => (
        <div key={x.field} className="flex flex-col gap-1" data-testid={`inspector-proposal-text-${x.field}`}>
          <Label>{textFieldLabel(t, x.field)}</Label>
          {(
            [
              ["before", columns.before, x.before, "text-err-fg"],
              ["after", columns.after, x.after, "text-ok-fg"],
            ] as const
          ).map(([key, label, value, tone]) => (
            <div key={key} className="flex flex-col gap-0.5">
              <span className={cn("text-[11px] font-semibold", tone)}>{label}</span>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-tile border border-line bg-surface px-2 py-1.5 font-sans text-xs text-fg" data-testid={`inspector-proposal-${key}-${x.field}`}>
                {value}
              </pre>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
