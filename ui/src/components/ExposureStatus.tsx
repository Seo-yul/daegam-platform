/**
 * 인스턴스 상세의 노출 상태 표시. 배지 묶음과 external_url 복사 줄이다. 생성 폼(ExposureSection)과는
 * 아무 관계가 없어서 따로 둔다. 상세 화면은 이 파일만 import 하면 된다.
 */
import type { Exposure, ExposureStatus } from "@/api/types";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { CopyButton } from "@/components/ui/copy-button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { phaseText } from "@/lib/warningText";

/**
 * 앞단 프록시(front)의 phase 색. 인스턴스 phase 를 칠하는 `PhaseBadge.phaseVariant` 는 모르는 phase 를
 * 흐리게(muted) 두지만, 앞단은 아직 안 뜬 것을 진행 중(warn)으로 본다. 규칙이 달라 이름을 나눈다.
 */
function frontPhaseVariant(phase: string | null): BadgeProps["variant"] {
  switch (phase) {
    case "Ready":
      return "ok";
    case "Failed":
    case "Missing":
      return "error";
    case null:
      return "muted";
    default:
      return "warn";
  }
}

function triVariant(v: boolean | null, falseVariant: BadgeProps["variant"] = "warn"): BadgeProps["variant"] {
  if (v === true) return "ok";
  if (v === false) return falseVariant;
  return "muted";
}

/** 참, 거짓, 모름(아직 못 읽음)의 배지 글자. 색은 triVariant 가 같은 값으로 고른다. */
function triLabel(v: boolean | null | undefined, labels: { yes: string; no: string; unknown: string }): string {
  if (v) return labels.yes;
  if (v === false) return labels.no;
  return labels.unknown;
}

export const UNAUTHENTICATED_PREFIX = "unauthenticated_exposure";

/** 노출 상태 배지. 앞단 phase 는 MCP Server 에만 있다 (Gateway 는 vMCP phase 가 곧 앞단). */
export function ExposureBadges({
  exposure,
  status,
  warnings,
  front = false,
}: {
  exposure: Exposure;
  status: ExposureStatus | null | undefined;
  warnings: string[];
  front?: boolean;
}) {
  const t = useT();
  const text = t.shared.exposureStatus;
  if (!exposure.enabled) return null;
  const unauth = warnings.some((w) => w.startsWith(UNAUTHENTICATED_PREFIX));
  return (
    <span className="inline-flex flex-wrap items-center gap-1" data-testid="exposure-badges">
      {front ? (
        <Badge variant={frontPhaseVariant(status?.front_phase ?? null)} data-testid="exp-badge-front">
          {text.front(phaseText(t, status?.front_phase) ?? status?.front_phase ?? "?")}
        </Badge>
      ) : null}
      {exposure.tls.mode === "edge" ? (
        // 앞단이 공개하는 클러스터는 LoadBalancer 가 없어 Ingress 에 주소가 쓰이지 않는다 (D54)
        <Badge variant="info" data-testid="exp-badge-address">
          {text.frontPublishes}
        </Badge>
      ) : (
        <Badge variant={triVariant(status?.address_assigned ?? null)} data-testid="exp-badge-address">
          {triLabel(status?.address_assigned, text.address)}
        </Badge>
      )}
      {exposure.tls.mode === "secret" || exposure.tls.mode === "cert-manager" ? (
        <Badge variant={triVariant(status?.tls_secret_present ?? null, "error")} data-testid="exp-badge-tls-secret">
          {triLabel(status?.tls_secret_present, text.tlsSecret)}
        </Badge>
      ) : null}
      {exposure.tls.mode === "cert-manager" ? (
        <Badge variant={triVariant(status?.certificate_ready ?? null)} data-testid="exp-badge-cert">
          {triLabel(status?.certificate_ready, text.certificate)}
        </Badge>
      ) : null}
      {unauth ? (
        <Badge variant="warn" data-testid="exp-badge-unauth">
          {text.unauthenticated}
        </Badge>
      ) : null}
    </span>
  );
}

/** `status.external_url` 과 복사 버튼. 노출하지 않았으면 그렇다고 적는다. */
export function ExternalUrl({ url, testId = "status-external-url", copyTestId = "copy-external-url" }: { url: string | null | undefined; testId?: string; copyTestId?: string }) {
  const t = useT();
  const text = t.shared.exposureStatus;
  return (
    <span className="flex items-center gap-2">
      <span className={cn("break-all text-xs", url && "font-mono")} data-testid={testId}>
        {url ?? text.notExposed}
      </span>
      {url ? <CopyButton value={url} label={text.copyLabel} testId={copyTestId} /> : null}
    </span>
  );
}
