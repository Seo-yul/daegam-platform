/**
 * 상세 배포 탭의 노출 설정 카드. 생성 폼과 같은 ExposureSection 을 편집 모드로 쓴다.
 */
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { Exposure, ExposureInput } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { ExposureSection } from "@/components/ExposureSection";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { exposureFieldErrors, exposureFormOf, exposureHostBlank, exposureInvalid, isExposureFieldError, toExposureInput, type ExposureForm } from "@/lib/exposure";

/** Basic 탭의 노출 설정 카드. 저장은 PATCH `{exposure}` 이고 서버가 재배포한다. */
export function ExposureCard({
  clusterId,
  namespace,
  name,
  exposure,
  onSave,
  onSaved,
}: {
  clusterId: string;
  namespace: string;
  name: string;
  exposure: Exposure;
  onSave: (input: ExposureInput) => Promise<unknown>;
  onSaved?: () => unknown;
}) {
  const t = useT();
  const text = t.shared.exposureCard;
  const [form, setForm] = useState<ExposureForm>(() => exposureFormOf(exposure, namespace));
  useEffect(() => {
    setForm(exposureFormOf(exposure, namespace));
  }, [exposure, namespace]);
  const save = useMutation({
    mutationFn: (input: ExposureInput) => onSave(input),
    onSuccess: () => onSaved?.(),
  });
  const hostBlank = exposureHostBlank(form);
  const hostInvalid = exposureInvalid(form);
  const unchanged = JSON.stringify(toExposureInput(form)) === JSON.stringify(toExposureInput(exposureFormOf(exposure, namespace)));

  return (
    <Card data-testid="exposure-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted">{text.hint}</span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ExposureSection clusterId={clusterId} name={name} value={form} onChange={setForm} errors={exposureFieldErrors(save.error)} mode="edit" />
        {!isExposureFieldError(save.error) ? <ErrorMessage error={save.error} testId="exposure-error" /> : null}
        {save.isSuccess ? (
          <Alert variant="ok" data-testid="exposure-saved">
            {text.saved}
          </Alert>
        ) : null}
        <div className="flex justify-end">
          <Button data-testid="exposure-save" disabled={save.isPending || unchanged || hostBlank || hostInvalid} onClick={() => save.mutate(toExposureInput(form))}>
            {save.isPending ? t.shared.saving : text.save}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
