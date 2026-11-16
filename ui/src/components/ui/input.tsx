import { cloneElement, isValidElement, useId } from "react";
import type { ComponentProps, ReactNode, LabelHTMLAttributes } from "react";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

import { fieldClass } from "./field";

/** cloneElement 로 얹는 속성. */
type AriaProps = { "aria-describedby"?: string; "aria-invalid"?: boolean | "true" | "false" };

/**
 * 자식 요소에 describedby 와 invalid 를 얹는다.
 *
 * `isValidElement<AriaProps>` 는 제네릭이라 `as` 캐스트와 달리 타입 검사를 지우지 않는다. 좁혀진 뒤로는
 * `node.props` 도 `cloneElement` 의 인자도 `AriaProps` 로 검사되므로 이름을 틀리면 컴파일이 막는다.
 * 자식이 이 속성을 실제로 버튼·입력까지 내려보내는지는 그 컴포넌트의 몫이다(Select 는 `...rest` 로 편다).
 */
function withAria(node: ReactNode, describedBy: string, invalid: boolean): ReactNode {
  if (!isValidElement<AriaProps>(node)) return node;
  return cloneElement(node, {
    "aria-describedby": [node.props["aria-describedby"], describedBy].filter(Boolean).join(" "),
    "aria-invalid": invalid ? true : node.props["aria-invalid"],
  });
}

export { Select, type SelectChangeEvent, type SelectProps } from "./select";

/** ComponentProps 라 ref 도 받는다 (React 19 는 ref 가 일반 prop 이다). */
export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(fieldClass, "h-[34px]", className)} {...props} />;
}

/** Input 과 같은 이유로 ComponentProps 다. 입력칸(Composer)이 키를 재려고 ref 를 준다. */
export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return <textarea className={cn(fieldClass, "min-h-24 font-mono text-xs leading-5", className)} {...props} />;
}

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("text-sm font-medium text-fg-soft", className)} {...props} />;
}

/**
 * 라벨·입력·도움말·오류 한 벌.
 *
 * 도움말과 오류는 `aria-describedby` 로 입력에 이어 붙는다. 눈으로는 바로 아래 줄이라 이어져 보이지만
 * 화면 낭독기는 라벨만 읽고 지나가므로, 안 이어 두면 "이름은 생성 뒤 바꿀 수 없습니다" 같은 제약이
 * 들리지 않는다. `children` 이 요소 하나면 `cloneElement` 로 얹고(이미 있는 `aria-describedby` 는 앞에
 * 두고 뒤에 잇는다), 여럿이면 얹을 자리가 없으므로 문구만 보인다.
 *
 * `error` 를 주면 도움말 대신 그것을 보이고 입력에 `aria-invalid` 를 단다. 오류 문단은 `role="alert"`
 * 이라 고쳐 쓰는 동안 생긴 오류도 읽힌다.
 */
export function Field({
  label,
  htmlFor,
  hint,
  error,
  required,
  children,
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  /** 이 칸의 오류. 있으면 도움말 대신 보이고 입력이 `aria-invalid` 가 된다. */
  error?: string | null;
  /** 라벨 옆에 필수 표시를 단다. 입력의 required 속성과 짝을 맞춰서 준다. */
  required?: boolean;
  children: React.ReactNode;
}) {
  const t = useT();
  const auto = useId();
  const base = htmlFor ?? auto;
  const hintId = hint ? `${base}-hint` : undefined;
  const errorId = error ? `${base}-error` : undefined;
  const described = [errorId, hintId].filter(Boolean).join(" ");
  const child = described ? withAria(children, described, Boolean(error)) : children;
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={htmlFor}>
        {label}
        {required ? (
          // 별표만 두면 무슨 뜻인지 모르는 사람이 있어 보조 기술에는 낱말로 읽힌다
          <span className="ml-1 text-err-fg" aria-hidden>
            *
          </span>
        ) : null}
        {required ? <span className="sr-only"> {t.ui.input.required}</span> : null}
      </Label>
      {child}
      {error ? (
        <p id={errorId} className="text-xs text-err-fg" role="alert">
          {error}
        </p>
      ) : null}
      {hint ? (
        <p id={hintId} className="text-xs text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
