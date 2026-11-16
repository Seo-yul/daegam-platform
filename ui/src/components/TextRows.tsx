/**
 * 글 한 줄짜리 값의 목록 칸. 이름표와 도움말 아래에 줄마다 입력과 제거 단추가 있고, 끝에 추가 단추가 있다. 평가 사례의 메시지와
 * 기준, image 항목의 Args, 허브 이미지 카드의 공개 키·회수한 키·거부한 digest 가 같이 쓴다.
 *
 * 묶음은 fieldset 이고 이름표는 legend 다. 줄이 없을 때도 이름표가 가리킬 것이 있어야 하기 때문이다. 입력의 낭독 이름은
 * "<이름표> <번호>" 다. data-testid 는 줄 `<testId>-<i>`, 제거 `<testId>-<i>-remove`, 추가 `<testId>-add` 다.
 */
import { type ReactNode, useId } from "react";

import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export function TextRows({
  label,
  hint,
  values,
  onChange,
  testId,
  multiline = false,
  inputClassName,
  min = 0,
  max = Number.POSITIVE_INFINITY,
  invalid,
  note,
  footer,
}: {
  label: string;
  hint?: string;
  values: string[];
  onChange: (next: string[]) => void;
  testId: string;
  /** 여러 줄 칸(공개 키의 PEM, 평가 사례의 글)이면 참이다. */
  multiline?: boolean;
  inputClassName?: string;
  /** 줄 수의 하한과 상한. 하한이면 제거 단추가, 상한이면 추가 단추가 없다. */
  min?: number;
  max?: number;
  /** 이 줄이 틀렸는가. 테두리와 `aria-invalid` 로 보인다. */
  invalid?: (index: number) => boolean;
  /** 줄 아래에 서는 한 줄(계산한 값 등). */
  note?: (value: string, index: number) => ReactNode;
  /** 목록 아래에 서는 것. */
  footer?: ReactNode;
}) {
  const t = useT();
  const id = useId();
  const change = (i: number, value: string) => onChange(values.map((v, j) => (j === i ? value : v)));
  return (
    <fieldset className="flex flex-col gap-1.5" aria-describedby={hint ? `${id}-hint` : undefined}>
      <legend className="mb-1 text-sm font-medium text-fg-soft">{label}</legend>
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted">
          {hint}
        </p>
      ) : null}
      {values.map((value, i) => {
        const bad = invalid?.(i) ?? false;
        const props = {
          "aria-label": `${label} ${i + 1}`,
          "data-testid": `${testId}-${i}`,
          "aria-invalid": bad || undefined,
          value,
          className: cn("min-w-0 flex-1", inputClassName, bad && "border-err-fg"),
        };
        return (
          <div key={i} className="flex flex-col gap-0.5">
            <div className="flex items-start gap-2">
              {multiline ? <Textarea {...props} onChange={(e) => change(i, e.target.value)} /> : <Input {...props} autoComplete="off" onChange={(e) => change(i, e.target.value)} />}
              {values.length > min ? (
                <Button size="sm" variant="ghost" data-testid={`${testId}-${i}-remove`} onClick={() => onChange(values.filter((_, j) => j !== i))}>
                  {t.shared.remove}
                </Button>
              ) : null}
            </div>
            {note?.(value, i)}
          </div>
        );
      })}
      {values.length < max ? (
        <div>
          <Button size="sm" variant="outline" data-testid={`${testId}-add`} onClick={() => onChange([...values, ""])}>
            {t.shared.add}
          </Button>
        </div>
      ) : null}
      {footer}
    </fieldset>
  );
}
