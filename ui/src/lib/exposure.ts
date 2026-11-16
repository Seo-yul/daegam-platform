/**
 * 인스턴스 외부 노출의 순수 규칙 (resource-model 11절, D22·D27). 폼 상태 모양과 요청 본문 변환, 서버와 같은
 * 호스트·이름 정규화, 422 코드 → 필드 대응표다.
 *
 * 화면에 걸리지 않으므로 lib 에 둔다. 컴포넌트 파일에 있으면 상세 화면이 배지 하나를 쓰려고 생성 폼까지
 * 통째로 import 하게 된다 (lib/instances.ts 와 같은 이유다).
 */
import { ApiError } from "@/api/client";
import type { Exposure, ExposureInput, ExposureTLSMode } from "@/api/types";

export interface ExposureForm {
  /** 배포 네임스페이스. 빈 문자열은 테넌트 기본이다. */
  namespace: string;
  enabled: boolean;
  host: string;
  /** IngressClass. 빈 문자열은 클러스터 기본(서버가 채운다). */
  ingressClass: string;
  tlsMode: ExposureTLSMode;
  secretName: string;
  /** ClusterIssuer. 빈 문자열은 클러스터 기본(서버가 채운다). */
  issuer: string;
}

export const EMPTY_EXPOSURE: ExposureForm = {
  namespace: "",
  enabled: false,
  host: "",
  ingressClass: "",
  tlsMode: "none",
  secretName: "",
  issuer: "",
};

/** 저장된 인스턴스의 exposure 스냅샷을 폼 상태로. */
export function exposureFormOf(exp: Exposure, namespace: string): ExposureForm {
  return {
    namespace,
    enabled: exp.enabled,
    host: exp.host ?? "",
    ingressClass: exp.ingress_class ?? "",
    tlsMode: exp.tls.mode,
    secretName: exp.tls.secret_name ?? "",
    issuer: exp.tls.issuer ?? "",
  };
}

/** 생성·PATCH 본문의 `exposure`. 끄면 `{enabled: false}` 만 보낸다. */
export function toExposureInput(f: ExposureForm): ExposureInput {
  if (!f.enabled) return { enabled: false };
  return {
    enabled: true,
    host: f.host.trim() || null,
    ingress_class: f.ingressClass || null,
    tls: {
      mode: f.tlsMode,
      secret_name: f.tlsMode === "secret" ? f.secretName || null : null,
      issuer: f.tlsMode === "cert-manager" ? f.issuer || null : null,
    },
  };
}

/** 미리보기 URL. 서버의 `external_url` 규칙과 같다 (MCP·Gateway 는 `<scheme>://<host>/mcp`, Agent 는 경로 없음). */
export function previewUrl(f: Pick<ExposureForm, "host" | "tlsMode">, path = "/mcp"): string {
  const scheme = f.tlsMode === "none" ? "http" : "https";
  return `${scheme}://${f.host.trim() || "<host>"}${path}`;
}

const HOST_LABEL_RE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

/** 소문자 DNS-1123 호스트명인지. 서버 normalize_host 와 같은 규칙이고 뒤의 점은 뗀다. */
export function isDns1123Host(raw: string): boolean {
  const host = raw.trim().toLowerCase().replace(/\.+$/, "");
  if (!host || host.length > 253) return false;
  return host.split(".").every((l) => HOST_LABEL_RE.test(l));
}

/**
 * 켠 상태에서 클라이언트가 이미 아는 규칙에 걸리는 것이 있으면 true. 저장·복제 버튼을 잠그는 데 쓴다.
 *
 * `ExposureSection` 이 칸 아래에 같은 판정으로 빨간 줄을 그린다. 그 판정이 컴포넌트 안에만 있으면
 * 폼을 감싼 쪽은 막을 수가 없어, 화면에는 "규칙에 맞지 않습니다" 가 떠 있는데 저장은 눌리고 서버가
 * 422 로 돌려주는 일이 난다. Agent 쪽 `agentExposureInvalid` 와 같은 자리다.
 */
export function exposureInvalid(f: ExposureForm): boolean {
  return f.enabled && f.host.trim() !== "" && !isDns1123Host(f.host);
}

/** 켰는데 호스트를 비워 둔 상태. 규칙 위반(exposureInvalid)과 따로 버튼을 잠그고, 재배포 창은 따로 안내한다. */
export function exposureHostBlank(f: ExposureForm): boolean {
  return f.enabled && !f.host.trim();
}

/** 서버 k8s_name 과 같은 정규화. 템플릿의 `{name}` 자리에 들어간다. */
export function k8sLabel(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

/** `defaults.host` 템플릿(`{name}.<tenant>.<domain>`)에 인스턴스 이름을 넣는다. */
export function suggestHost(template: string | null | undefined, name: string): string {
  if (!template) return "";
  const label = k8sLabel(name);
  if (!label) return "";
  return template.replace("{name}", label);
}

// -- 서버 오류 코드가 가리키는 필드 ---------------------------------------------------------

export type ExposureField = "namespace" | "enabled" | "class" | "host" | "tls-mode" | "tls-secret" | "issuer" | "cors";

/** 서버 검증 코드가 가리키는 필드. 표에 없는 코드는 폼 위의 공통 오류로 간다. cors 는 Agent 전용이다 (D27). */
export const EXPOSURE_ERROR_FIELDS: Record<string, ExposureField> = {
  invalid_host: "host",
  host_conflict: "host",
  host_reserved: "host",
  ingress_disabled: "enabled",
  unauthenticated_exposure: "enabled",
  unknown_ingress_class: "class",
  ingress_class_not_allowed: "class",
  tls_secret_invalid: "tls-secret",
  cert_manager_unavailable: "tls-mode",
  unknown_issuer: "issuer",
  namespace_not_allowed: "namespace",
  invalid_cors_origin: "cors",
};

export type ExposureErrors = Partial<Record<ExposureField, string>>;

export function exposureFieldErrors(error: unknown): ExposureErrors {
  if (!(error instanceof ApiError)) return {};
  const field = EXPOSURE_ERROR_FIELDS[error.code];
  return field ? { [field]: error.detail } : {};
}

/** 필드 오류로 흡수된 코드면 true. 공통 ErrorMessage 를 겹쳐 보이지 않게 한다. */
export function isExposureFieldError(error: unknown): boolean {
  return error instanceof ApiError && error.code in EXPOSURE_ERROR_FIELDS;
}
