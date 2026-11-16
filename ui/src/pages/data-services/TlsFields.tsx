import { useId } from "react";

import type { DataServiceCreate } from "@/api/resources";
import { Field, Input, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";

/** Redis 의 TLS 끝점 칸 값이다 (P61). 칸에 적힌 문자열 그대로라 서버로 보낼 때는 tlsBody 로 바꾼다. */
export interface TlsValue {
  enabled: boolean;
  port: string;
  ca: string;
}

/** 서버 본문의 TLS 끝점이다. ca 가 null 이면 서버가 지금 CA 를 그대로 둔다(고칠 때만). */
export type TlsBody = NonNullable<DataServiceCreate["tls"]>;

export const NO_TLS: TlsValue = { enabled: false, port: "6380", ca: "" };

/** CA 칸의 예시. PEM 의 첫 줄이라 번역하지 않는다. */
const PEM_PLACEHOLDER = "-----BEGIN CERTIFICATE-----";

/** 켜져 있으면 서버 본문, 꺼져 있으면 null 이다. 빈 CA 는 null 이다. */
export function tlsBody(v: TlsValue): TlsBody | null {
  if (!v.enabled) return null;
  return { port: Number(v.port), ca: v.ca.trim() || null };
}

/**
 * Redis 의 TLS 포트와 CA 인증서 칸이다. 평문 Port 는 아직 TLS 로 옮겨 가지 않은 소비자를 위해 함께 둔다.
 *
 * 이미 CA 가 있는 끝점(`hasCa`)은 "비우면 지금 CA 를 둔다" 라 CA 칸이 필수가 아니다.
 */
export function TlsFields({ value, onChange, hasCa }: { value: TlsValue; onChange: (patch: Partial<TlsValue>) => void; hasCa: boolean }) {
  const t = useT();
  const text = t.dataServices.tls;
  const hintId = useId();
  return (
    <div className="flex flex-col gap-3" data-testid="ds-tls-fields">
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" data-testid="ds-tls" aria-describedby={hintId} checked={value.enabled} onChange={(e) => onChange({ enabled: e.target.checked })} />
        {text.enable}
      </label>
      <p id={hintId} className="text-xs text-muted">
        {text.enableHint}
      </p>
      {value.enabled ? (
        <>
          <Field label={text.port} hint={text.portHint} htmlFor="ds-tls-port" required>
            <Input id="ds-tls-port" data-testid="ds-tls-port" type="number" min={1} max={65535} required value={value.port} onChange={(e) => onChange({ port: e.target.value })} />
          </Field>
          <Field label={text.ca} hint={hasCa ? text.caKeepHint : text.caHint} htmlFor="ds-tls-ca" required={!hasCa}>
            <Textarea
              id="ds-tls-ca"
              data-testid="ds-tls-ca"
              rows={5}
              required={!hasCa}
              placeholder={PEM_PLACEHOLDER}
              value={value.ca}
              onChange={(e) => onChange({ ca: e.target.value })}
            />
          </Field>
        </>
      ) : null}
    </div>
  );
}
