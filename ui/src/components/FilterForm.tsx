/**
 * 목록 화면의 거르기 폼과 잘림 표시. 감사 화면(API·MCP)과 Agent 의 이벤트 탭이 같이 쓴다. 문구는 감사 사전(audit.apply·
 * clear·truncated·more)이다.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";

/** 한 번에 받아 오는 최대 건수. 서버(pydantic)가 받는 범위이기도 하다. */
export const LIMIT_MIN = 1;
export const LIMIT_MAX = 1000;
export const LIMIT_STEP = 100;

/**
 * 필터 폼. Apply 를 눌러야 쿼리가 나간다 (입력마다 요청하지 않는다).
 *
 * 필터 타입은 인덱스 시그니처가 없는 interface 라 `Record<string, …>` 로는 묶이지 않는다. 키마다 값이
 * 글자나 숫자인 타입이면 받게 해 두어야 호출부가 캐스트 없이 넘기고, 필드 key 도 그 타입의 키로 검사된다.
 */
export function FilterForm<T extends { [K in keyof T]?: string | number }>({
  fields,
  checks = [],
  values,
  defaults,
  onApply,
  testId = "audit-filters",
  testPrefix = "filter",
}: {
  /** options 가 있으면 고르개다. className 은 칸의 폭(기본 w-44)이다. */
  fields: { key: keyof T & string; label: string; type?: string; placeholder?: string; min?: number; max?: number; options?: { value: string; label: string }[]; className?: string }[];
  /** 체크박스 필드. 켜면 "1", 끄면 "" 이다. */
  checks?: { key: keyof T & string; label: string; title?: string }[];
  /** 칸에 채워 둘 지금 적용된 값. 밖에서 바뀌면 부모가 key 로 다시 태운다. */
  values: T;
  /** Clear 가 되돌릴 값. */
  defaults: T;
  onApply: (f: T) => void;
  testId?: string;
  /** 칸과 단추의 testId 앞머리다. 칸은 `<앞머리>-<key>`, 단추는 `<앞머리>-apply`·`<앞머리>-clear` 다. */
  testPrefix?: string;
}) {
  const t = useT();
  const [draft, setDraft] = useState<T>(values);
  return (
    <form
      className="mb-4 flex flex-wrap items-end gap-2"
      data-testid={testId}
      onSubmit={(e) => {
        e.preventDefault();
        onApply(draft);
      }}
    >
      {fields.map((f) => (
        <Field key={f.key} label={f.label} htmlFor={`${testPrefix}-f-${f.key}`}>
          {f.options ? (
            <Select id={`${testPrefix}-f-${f.key}`} data-testid={`${testPrefix}-${f.key}`} className={f.className ?? "w-44"} value={String(draft[f.key] ?? "")} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
              {f.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
          ) : (
            <Input
              id={`${testPrefix}-f-${f.key}`}
              data-testid={`${testPrefix}-${f.key}`}
              className={f.className ?? "w-44"}
              type={f.type ?? "text"}
              min={f.min}
              max={f.max}
              placeholder={f.placeholder}
              value={String(draft[f.key] ?? "")}
              onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
            />
          )}
        </Field>
      ))}
      {checks.map((c) => (
        <label key={c.key} className="flex h-[34px] items-center gap-2 text-[13px] text-fg" title={c.title}>
          <input type="checkbox" data-testid={`${testPrefix}-${c.key}`} checked={Boolean(draft[c.key])} onChange={(e) => setDraft({ ...draft, [c.key]: e.target.checked ? "1" : "" })} />
          {c.label}
        </label>
      ))}
      <Button type="submit" data-testid={`${testPrefix}-apply`}>
        {t.audit.apply}
      </Button>
      <Button
        type="button"
        variant="ghost"
        data-testid={`${testPrefix}-clear`}
        onClick={() => {
          setDraft(defaults);
          onApply(defaults);
        }}
      >
        {t.audit.clear}
      </Button>
    </form>
  );
}

/**
 * 표 아래의 잘림 표시.
 *
 * 서버는 최신순으로 limit 만큼 잘라 준다. 화면이 그 사실을 말하지 않으면 받은 것이 전부인지 잘린
 * 것인지 알 수 없고, 다음 묶음으로 넘어갈 방법도 없다.
 */
export function TruncationNotice({ shown, limit, onMore, testId = "audit" }: { shown: number; limit: number; onMore: (next: number) => void; testId?: string }) {
  const t = useT();
  if (shown < limit) return null;
  const next = Math.min(limit + LIMIT_STEP, LIMIT_MAX);
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-2 text-xs text-muted" data-testid={`${testId}-truncated`}>
      <span>{t.audit.truncated(limit)}</span>
      {limit < LIMIT_MAX ? (
        <Button type="button" size="sm" variant="outline" data-testid={`${testId}-more`} onClick={() => onMore(next)}>
          {t.audit.more(next)}
        </Button>
      ) : null}
    </div>
  );
}
