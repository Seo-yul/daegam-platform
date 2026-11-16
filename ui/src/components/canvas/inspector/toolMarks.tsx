/**
 * tools 노드 패널의 표시 부품이다. 외부 내용을 가져오는 Tool 절, 읽기 전용 표시 절, 승인 설정이 없는 쓰기 Tool 의 배지다
 * (P39b 14절 4부, P43 2·4·8절). 규칙은 lib/toolSafety.ts 에 있다.
 */
import { useState } from "react";

import type { ToolEffect } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/input";
import { useT } from "@/i18n";
import type { ToolsData } from "@/lib/agentGraph";
import { canvasMarkApplies, isOutside, outsideFixed, withOutside, type ToolOrigin } from "@/lib/toolSafety";

/** name 을 뺀 목록이다. 체크를 끄거나 표시를 지울 때 쓴다. */
export function without(list: readonly string[], name: string): string[] {
  return list.filter((n) => n !== name);
}

/**
 * 외부 내용을 가져오는 Tool 을 고르는 절이다(P43 2절). 백엔드 종류의 기본값을 미리 고르고, 바꾼 것만 노드의 `outside` 에 싣는다.
 * 웹 Tool 과 백엔드 종류를 모르는 이름은 늘 외부 내용이라 바꿀 수 없다.
 */
export function OutsideSection({ effects, origins, outside, onChange }: { effects: Record<string, ToolEffect>; origins: Record<string, ToolOrigin>; outside: ToolsData["outside"]; onChange: (outside: Record<string, boolean>) => void }) {
  const text = useT().canvas.tools;
  return (
    <div className="flex flex-col gap-1.5 rounded-item border border-line px-2.5 py-2" data-testid="tools-outside">
      <Label>{text.outside}</Label>
      <p className="text-xs text-muted">{text.outsideHint}</p>
      {Object.entries(effects).map(([tool, effect]) => (
        <label key={tool} className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            data-testid={`tool-outside-${tool}`}
            checked={isOutside(tool, origins[tool], effect, outside)}
            disabled={outsideFixed(origins[tool], effect)}
            onChange={(e) => onChange(withOutside(outside, tool, origins[tool], e.target.checked))}
          />
          <span className="font-mono">{tool}</span>
        </label>
      ))}
    </div>
  );
}

/**
 * 읽기 전용 표시 절이다. 확인을 받은 뒤에만 저장한다. 서버가 읽기 전용 여부를 알려 주지 않는 Tool 만 표시할 수 있고, 서버나 허브
 * 항목이 정하는 Tool 에 남은 표시는 적용되지 않는다고 알린다(P43 4절, P52 4절). origins 와 marked 는 검사 전에 없다.
 */
export function ReadOnlySection({ readOnly, markable, origins, marked, onChange }: { readOnly: string[]; markable: string[]; origins: Record<string, ToolOrigin> | undefined; marked?: string[]; onChange: (readOnly: string[]) => void }) {
  const text = useT().canvas.tools;
  const [marking, setMarking] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  return (
    <div className="flex flex-col gap-1.5 rounded-item border border-line px-2.5 py-2" data-testid="tools-read-only">
      <Label>{text.readOnly}</Label>
      <p className="text-xs text-muted">{text.readOnlyHintOutside}</p>
      {readOnly.map((tool) => (
        <div key={tool} className="flex flex-col gap-0.5 text-xs">
          <div className="flex items-center gap-2">
            <span className="font-mono">{tool}</span>
            <Badge variant="muted">{text.effect.read}</Badge>
            <Button size="sm" variant="ghost" data-testid={`tool-read-only-clear-${tool}`} onClick={() => onChange(without(readOnly, tool))}>
              {text.readOnlyClear}
            </Button>
          </div>
          {origins && !canvasMarkApplies(origins[tool], marked?.includes(tool)) ? (
            <span className="text-muted" data-testid={`tool-read-only-ignored-${tool}`}>
              {text.readOnlyIgnored}
            </span>
          ) : null}
        </div>
      ))}
      {markable.map((tool) => (
        <label key={tool} className="flex items-center gap-2 text-xs">
          <input type="checkbox" data-testid={`tool-read-only-${tool}`} checked={marking.includes(tool)} onChange={(e) => setMarking(e.target.checked ? [...marking, tool] : without(marking, tool))} />
          <span className="font-mono">{tool}</span>
        </label>
      ))}
      {markable.length ? (
        <>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5" data-testid="tool-read-only-confirm" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            {text.readOnlyConfirm}
          </label>
          <Button
            size="sm"
            variant="outline"
            data-testid="tool-read-only-apply"
            disabled={!confirmed || !marking.length}
            onClick={() => {
              onChange([...readOnly, ...marking]);
              setMarking([]);
              setConfirmed(false);
            }}
          >
            {text.readOnlyApply}
          </Button>
        </>
      ) : null}
    </div>
  );
}

/**
 * 승인 설정이 없는 쓰기 Tool 의 배지다. memory 노드가 있으면 "외부 내용이 들어오면 승인", 없으면 "외부 내용이 들어오면 거절"
 * 이다(P43 4·8절).
 */
export function ApprovalBadge({ tool, unruled, hasMemory }: { tool: string; unruled: boolean; hasMemory: boolean }) {
  const text = useT().canvas.tools;
  if (!unruled) return null;
  return hasMemory ? (
    <Badge variant="warn" title={text.outsideApprovalHint} data-testid={`tool-outside-approval-${tool}`}>
      {text.outsideApproval}
    </Badge>
  ) : (
    <Badge variant="warn" title={text.outsideRefusedHint} data-testid={`tool-outside-refused-${tool}`}>
      {text.outsideRefused}
    </Badge>
  );
}
