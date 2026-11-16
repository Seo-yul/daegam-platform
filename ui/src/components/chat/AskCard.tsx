/**
 * 질문 카드 (D31). 청지기의 `ask_user` 는 `interrupt{kind: question}` 으로 오고, 사용자의 답은 다음 턴의 메시지가 된다
 * (서버가 그 메시지를 ask_user 호출의 ToolMessage 로 붙인다). 선택지가 있으면 버튼으로, 언제나 자유 입력으로도 답할 수 있다.
 */
import { useState, type FormEvent } from "react";

import type { AssistantQuestion } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { statusCardClass } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";

export function AskCard({ question, onAnswer, disabled = false, resolved = false, testId = "ask-card" }: { question: AssistantQuestion; onAnswer?: (text: string) => void; disabled?: boolean; resolved?: boolean; testId?: string }) {
  const t = useT();
  const text = t.chat.ask;
  const [draft, setDraft] = useState("");
  const options = question.options ?? [];
  const canAnswer = Boolean(onAnswer) && !resolved;

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const answer = draft.trim();
    if (!answer || disabled || !canAnswer) return;
    setDraft("");
    onAnswer?.(answer);
  }

  return (
    // 승인 카드와 같은 꼴이고 띠만 안내색이다
    <div
      className={statusCardClass("info", resolved)}
      data-testid={testId}
      data-resolved={resolved ? "true" : undefined}
    >
      <div className="flex items-center gap-2">
        <Badge variant="info">{text.badge}</Badge>
        {question.field ? (
          <span className="font-mono text-[10.5px] text-muted" data-testid={`${testId}-field`}>
            {question.field}
          </span>
        ) : null}
      </div>
      <p className="whitespace-pre-wrap text-sm text-heading" data-testid={`${testId}-text`}>
        {question.text}
      </p>
      {canAnswer && options.length ? (
        <div className="flex flex-wrap gap-2">
          {options.map((opt, i) => (
            <Button key={`${i}-${opt}`} size="sm" variant="secondary" data-testid={`ask-option-${i}`} disabled={disabled} onClick={() => onAnswer?.(opt)}>
              {opt}
            </Button>
          ))}
        </div>
      ) : null}
      {canAnswer ? (
        <form className="flex items-center gap-2" onSubmit={submit}>
          <Input aria-label={text.input} data-testid="ask-input" className="h-8 flex-1 font-sans" placeholder={options.length ? text.optionsPlaceholder : text.placeholder} value={draft} disabled={disabled} onChange={(e) => setDraft(e.target.value)} />
          <Button type="submit" size="sm" data-testid="ask-submit" disabled={disabled || !draft.trim()}>
            {text.submit}
          </Button>
        </form>
      ) : null}
      {resolved ? <p className="text-xs text-muted">{text.sent}</p> : null}
    </div>
  );
}
