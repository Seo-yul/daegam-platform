/**
 * 허브 항목의 비밀 값을 바꾸는 대화상자. header 항목의 헤더 값(P86 6.1절)과 image 항목의 Secret env 값(P55 5절)이 같이 쓴다.
 *
 * 칸마다 password 입력이고 값은 저장할 때만 가며 다시 보이지 않는다. 값을 넣은 칸만 보낸다. required 인 칸이 비었거나 넣은 칸이
 * 없으면 저장할 수 없다. 저장한 답(값이 없는 항목)으로 상세의 캐시를 바꾼다. 열려 있는 동안만 두므로 닫으면 값과 오류가 남지 않는다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { HubItem } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { hubItemKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

export interface ValueField {
  /** 보낼 값의 이름. */
  name: string;
  label: string;
  hint?: string;
  required?: boolean;
  testId: string;
}

export function ReplaceValuesDialog({
  item,
  title,
  hint,
  fields,
  saveLabel,
  testId,
  save,
  onClose,
}: {
  item: HubItem;
  title: string;
  /** 칸 위의 한 줄. */
  hint?: string;
  fields: readonly ValueField[];
  saveLabel: string;
  /** 대화상자는 `<testId>-dialog`, 닫기 `<testId>-close`, 저장 `<testId>-save`, 오류 `<testId>-error` 다. */
  testId: string;
  /** 값을 넣은 칸만 이름과 값으로 받는다. */
  save: (values: Record<string, string>) => Promise<HubItem>;
  onClose: () => void;
}) {
  const t = useT();
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v));
  const ready = Object.keys(filled).length > 0 && fields.every((f) => !f.required || filled[f.name]);
  const mutation = useMutation({
    mutationFn: () => save(filled),
    onSuccess: (updated) => {
      queryClient.setQueryData(hubItemKey(tenant, item.id), updated);
      onClose();
    },
  });
  return (
    <Modal open title={title} testId={`${testId}-dialog`} closeTestId={`${testId}-close`} onClose={() => (mutation.isPending ? undefined : onClose())}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) mutation.mutate();
        }}
      >
        {hint ? <p className="text-xs text-muted">{hint}</p> : null}
        {fields.map((f) => (
          <Field key={f.name} label={f.label} hint={f.hint} htmlFor={f.testId}>
            <Input
              id={f.testId}
              data-testid={f.testId}
              type="password"
              autoComplete="new-password"
              required={f.required}
              value={values[f.name] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
            />
          </Field>
        ))}
        <ErrorMessage error={mutation.error} testId={`${testId}-error`} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={mutation.isPending}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid={`${testId}-save`} disabled={mutation.isPending || !ready}>
            {mutation.isPending ? t.shared.saving : saveLabel}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
