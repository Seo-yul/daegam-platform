import { useEffect, useRef, useState } from "react";

import { ApiError } from "@/api/client";
import { Input, Label } from "@/components/ui/input";
import { useT } from "@/i18n";
import { k8sResourceName, slugify, validateDisplayName, validateName } from "@/lib/names";
import { cn } from "@/lib/utils";

/*
 * 리소스 폼의 이름 두 칸 (D24). 첫 칸이 Display name, 그 아래가 Name(K8s 이름)이다.
 *  - Display name 을 치면 Name 이 슬러그로 따라온다. Name 을 직접 고치면 따라가기를 멈추고, Name 을 비우면 다시 따라간다.
 *  - 규칙 검사는 칸을 떠날 때와 제출할 때다. 치는 동안 빨간 글씨가 따라다니면 아직 다 적지도 않은 것을 틀렸다고 하는 셈이다.
 *    서버 422(invalid_name·name_immutable·invalid_display_name)는 같은 자리에 온다.
 *  - preview 를 주면 클러스터에 생길 리소스 이름(`mcp-<name>`)을 고정폭으로 미리 보인다.
 *  - readOnlyName 이면 Name 은 읽기 전용 고정폭이다 (생성 뒤 바꿀 수 없다).
 * testid 는 `<prefix>-display-name` 과 `<prefix>-name` 이다.
 */

export type NameField = "name" | "display_name";

/** 서버 검증 코드가 가리키는 칸. 표에 없는 코드는 폼의 공통 오류로 간다. */
export const NAME_ERROR_FIELDS: Record<string, NameField> = {
  invalid_name: "name",
  name_immutable: "name",
  invalid_display_name: "display_name",
};

export type NameErrors = Partial<Record<NameField, string>>;

export function nameFieldErrors(error: unknown): NameErrors {
  if (!(error instanceof ApiError)) return {};
  const field = NAME_ERROR_FIELDS[error.code];
  return field ? { [field]: error.detail } : {};
}

/** 필드 오류로 흡수된 코드면 true. 공통 ErrorMessage 를 겹쳐 보이지 않게 한다. */
export function isNameFieldError(error: unknown): boolean {
  return error instanceof ApiError && error.code in NAME_ERROR_FIELDS;
}

export interface NameValue {
  displayName: string;
  name: string;
}

export interface NameFieldsProps {
  /** testid 접두어. "bp" 면 bp-display-name, bp-name 이다. */
  testId: string;
  value: NameValue;
  onChange: (next: NameValue) => void;
  /** Name 을 읽기 전용 고정폭으로. 편집 폼에서 쓴다. */
  readOnlyName?: boolean;
  /** 클러스터 리소스 이름 미리보기의 접두어 (mcp, gw, hub). */
  preview?: { prefix: string };
  /** 서버 422 를 칸별로 넘긴다. */
  errors?: NameErrors;
  /** Name 규칙 힌트 뒤에 붙는 추가 안내 (예: Cluster 의 home 예약). */
  nameHint?: string;
  /** Name 이 필수인지. 기본 true. Publish 대화상자처럼 비우면 서버 기본값을 쓰는 곳은 false 다. */
  required?: boolean;
  /** 두 칸을 한 줄에 나란히 놓는다 (대화상자·카드 안). */
  inline?: boolean;
  /** 빈 칸에 보이는 예시. 화면의 리소스 종류에 맞게 넘긴다(표시 이름과 Name 이 서로 대응되는 한 쌍). 기본은 사전의 REST API 예시다. */
  placeholders?: NamePlaceholders;
}

export interface NamePlaceholders {
  display: string;
  name: string;
}

export function NameFields({ testId, value, onChange, readOnlyName = false, preview, errors = {}, nameHint, required = true, inline = false, placeholders }: NameFieldsProps) {
  const t = useT();
  const text = t.detail.nameFields;
  const examples = placeholders ?? text.placeholders;
  // Name 을 사용자가 직접 고쳤는지. 고치면 Display name 을 따라가지 않고, 비우면 다시 따라간다.
  const linked = useRef(true);
  // 규칙 검사를 보일 칸. 떠난 칸과, 제출했으면 두 칸 다다.
  const [touched, setTouched] = useState<Partial<Record<NameField, boolean>>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const touch = (field: NameField) => setTouched((seen) => (seen[field] ? seen : { ...seen, [field]: true }));

  // 한 번도 들르지 않은 칸도 제출할 때는 검사한다. 폼의 submit 을 캡처로 잡아 페이지의 핸들러보다 먼저 받는다
  useEffect(() => {
    const form = rootRef.current?.closest("form");
    if (!form) return;
    const onSubmit = () => setTouched({ name: true, display_name: true });
    form.addEventListener("submit", onSubmit, true);
    return () => form.removeEventListener("submit", onSubmit, true);
  }, []);

  function setDisplay(display: string) {
    if (linked.current && !readOnlyName) onChange({ displayName: display, name: slugify(display) });
    else onChange({ ...value, displayName: display });
  }
  function setName(name: string) {
    linked.current = name === "";
    onChange({ ...value, name });
  }

  const displayError = errors.display_name ?? (touched.display_name ? validateDisplayName(t, value.displayName) : null);
  // 읽기 전용(편집)에서는 이름을 바꿀 수 없으므로 규칙 검사를 하지 않고 서버 오류만 보인다 (D24)
  const nameError = errors.name ?? (touched.name && !readOnlyName && value.name ? validateName(t, value.name) : null);
  const displayId = `${testId}-display-name`;
  const nameId = `${testId}-name`;

  return (
    <div ref={rootRef} className={cn("gap-3", inline ? "grid grid-cols-2" : "flex flex-col")} data-testid={`${testId}-name-fields`}>
      <div className="flex flex-col gap-1">
        <Label htmlFor={displayId}>{text.labels.display}</Label>
        <Input
          id={displayId}
          data-testid={displayId}
          value={value.displayName}
          placeholder={examples.display}
          aria-invalid={displayError ? true : undefined}
          // 규칙과 오류는 바로 아래 줄이라 눈에는 이어져 보이지만 낭독기는 라벨만 읽고 지나간다
          aria-describedby={`${displayId}-desc`}
          onChange={(e) => setDisplay(e.target.value)}
          onBlur={() => touch("display_name")}
        />
        {displayError ? (
          <p id={`${displayId}-desc`} className="text-xs text-err-fg" data-testid={`${displayId}-error`} role="alert">
            {displayError}
          </p>
        ) : (
          <p id={`${displayId}-desc`} className="text-xs text-muted">
            {t.ui.names.displayNameRule}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={nameId}>{text.labels.name}</Label>
        <Input
          id={nameId}
          data-testid={nameId}
          className={cn("font-mono", readOnlyName && "bg-hover text-muted")}
          value={value.name}
          required={required && !readOnlyName}
          readOnly={readOnlyName}
          placeholder={examples.name}
          aria-invalid={nameError ? true : undefined}
          aria-describedby={preview ? `${nameId}-desc ${nameId}-preview` : `${nameId}-desc`}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => touch("name")}
        />
        {nameError ? (
          <p id={`${nameId}-desc`} className="text-xs text-err-fg" data-testid={`${nameId}-error`} role="alert">
            {nameError}
          </p>
        ) : readOnlyName ? (
          <p id={`${nameId}-desc`} className="text-xs text-muted">
            {text.immutable}
          </p>
        ) : (
          <p id={`${nameId}-desc`} className="text-xs text-muted">
            {t.ui.names.nameRule}
            {nameHint ? ` ${nameHint}` : ""}
          </p>
        )}
        {preview ? (
          <p id={`${nameId}-preview`} className="text-xs text-muted" data-testid={`${nameId}-preview`}>
            {text.preview(<code className="font-mono text-fg">{k8sResourceName(preview.prefix, value.name || "…")}</code>)}
          </p>
        ) : null}
      </div>
    </div>
  );
}
