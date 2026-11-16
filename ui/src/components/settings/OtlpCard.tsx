/**
 * OTLP 내보내기 카드 (P37 15절). Platform Settings 아래에 놓인다.
 *
 * 플랫폼이 이벤트 저장소에서 실행 단위로 내보내므로 설정은 설치에 하나다. Platform Settings 와 따로인 문서라 이 카드가 스스로
 * 읽고 따로인 PUT 하나로 저장한다. 런타임 기록이 나가는 곳을 바꾸므로 비서의 설정 도구로는 바꾸지 못한다. 헤더는 플랫폼
 * 네임스페이스의 Secret 이름만 받는다. 주소는 https 만, 평문 http 는 클러스터 안의 `*.svc` 만 서버가 받는다. 아래 줄은 마지막 성공,
 * 실패와 버림의 누적, 대기열의 실행 수이고 내보내기가 켜져 있을 때만 다시 읽는다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { getOtlpSettings, getOtlpStatus, putOtlpSettings } from "@/api/resources";
import type { OtlpSettings } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { queryGate } from "@/components/QueryGate";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { OTLP_SETTINGS_KEY, OTLP_STATUS_KEY } from "@/lib/queryKeys";
import { TELEMETRY_POLL_MS } from "@/lib/usage";
import { formatDate, formatNumber } from "@/lib/utils";

interface OtlpFormState {
  enabled: boolean;
  endpoint: string;
  headers_secret: string;
  sample_ratio: string;
  always_errors: boolean;
  capture_content: boolean;
}

function formOf(otlp: OtlpSettings): OtlpFormState {
  return {
    enabled: otlp.enabled,
    endpoint: otlp.endpoint ?? "",
    headers_secret: otlp.headers_secret ?? "",
    sample_ratio: String(otlp.sample_ratio),
    always_errors: otlp.always_errors,
    capture_content: otlp.capture_content,
  };
}

/** 표본 비율. 비었거나 0~1 밖이면 null 이고 저장하지 않는다. */
function ratioOf(text: string): number | null {
  const ratio = Number(text);
  return text.trim() !== "" && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? ratio : null;
}

export function OtlpCard({ readOnly }: { readOnly: boolean }) {
  const q = useQuery({ queryKey: OTLP_SETTINGS_KEY, queryFn: getOtlpSettings });
  const gate = queryGate(q, { testId: "ps-otlp-load-error" });
  if (gate) return gate;
  // 다시 읽은 값이 바뀌면 칸을 그 값으로 다시 연다
  return q.data ? <OtlpForm key={JSON.stringify(q.data)} otlp={q.data} readOnly={readOnly} /> : null;
}

function OtlpForm({ otlp, readOnly }: { otlp: OtlpSettings; readOnly: boolean }) {
  const t = useT();
  const text = t.admin.platformSettings.otlp;
  const queryClient = useQueryClient();
  const [form, setForm] = useState<OtlpFormState>(() => formOf(otlp));
  const status = useQuery({ queryKey: OTLP_STATUS_KEY, queryFn: getOtlpStatus, refetchInterval: otlp.enabled ? TELEMETRY_POLL_MS : false });
  const ratio = ratioOf(form.sample_ratio);
  const save = useMutation({
    mutationFn: () =>
      putOtlpSettings({
        enabled: form.enabled,
        endpoint: form.endpoint.trim() || null,
        headers_secret: form.headers_secret.trim() || null,
        sample_ratio: ratio ?? otlp.sample_ratio,
        always_errors: form.always_errors,
        capture_content: form.capture_content,
      }),
    onSuccess: (saved) => queryClient.setQueryData(OTLP_SETTINGS_KEY, saved),
  });
  const set = (patch: Partial<OtlpFormState>) => setForm((f) => ({ ...f, ...patch }));
  const s = status.data;
  return (
    <Card data-testid="ps-otlp">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-muted">{text.body}</p>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (ratio !== null) save.mutate();
          }}
        >
          <fieldset className="contents" disabled={readOnly}>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" data-testid="ps-otlp-enabled" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
              {text.enabled}
            </label>
            <div className="grid gap-3 md:grid-cols-2">
              <Field label={text.endpoint} hint={text.endpointHint} htmlFor="ps-otlp-endpoint">
                <Input id="ps-otlp-endpoint" data-testid="ps-otlp-endpoint" value={form.endpoint} onChange={(e) => set({ endpoint: e.target.value })} />
              </Field>
              <Field label={text.headersSecret} hint={text.headersSecretHint} htmlFor="ps-otlp-headers">
                <Input id="ps-otlp-headers" data-testid="ps-otlp-headers" className="font-mono" value={form.headers_secret} onChange={(e) => set({ headers_secret: e.target.value })} />
              </Field>
              <Field label={text.sampleRatio} hint={text.sampleRatioHint} error={ratio === null ? text.sampleRatioInvalid : null} htmlFor="ps-otlp-ratio">
                <Input id="ps-otlp-ratio" data-testid="ps-otlp-ratio" type="number" min={0} max={1} step={0.05} value={form.sample_ratio} onChange={(e) => set({ sample_ratio: e.target.value })} />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" data-testid="ps-otlp-errors" checked={form.always_errors} onChange={(e) => set({ always_errors: e.target.checked })} />
              {text.alwaysErrors}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" data-testid="ps-otlp-content" checked={form.capture_content} onChange={(e) => set({ capture_content: e.target.checked })} />
              {text.captureContent}
            </label>
            <ErrorMessage error={save.error} testId="ps-otlp-error" />
            <div className="flex items-center gap-3">
              <Button type="submit" size="sm" disabled={save.isPending || ratio === null} data-testid="ps-otlp-save">
                {t.shared.save}
              </Button>
              {save.isSuccess ? <span className="text-xs text-muted">{text.saved}</span> : null}
            </div>
          </fieldset>
        </form>
        {s ? (
          <p className="text-xs text-muted" data-testid="ps-otlp-status">
            {text.status(s.last_success ? formatDate(t, s.last_success) : null, formatNumber(t, s.failures), formatNumber(t, s.dropped), formatNumber(t, s.queued))}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
