/**
 * 외부 노출 (P8 F3·F6, docs/guide/exposure.md, resource-model 11절). 클러스터 노출 옵션과 인스턴스 노출 스냅샷을 만드는 조각(MCP Server·Gateway 의
 * `exposureOf`, Agent 의 `agentExposureOf`), 노출 경고 문구, 이미 쓰인 host 검사 `hostInUse` 다.
 */
import type { AgentExposure, AgentExposureInput, Exposure, ExposureInput, ExposureOptions, ExposureStatus } from "@/api/types";

import { authConfigState } from "./auth";
import { apiError } from "./core";

export function exposureOptionsFixture(over: Partial<ExposureOptions> = {}): ExposureOptions {
  return {
    enabled: true,
    namespaces: ["daegam-default", "daegam-default-edge"],
    namespace: "daegam-default",
    ingress_classes: ["traefik", "nginx"],
    tls_secrets: ["calc-tls"],
    cluster_issuers: ["letsencrypt"],
    defaults: { host: "{name}.default.apps.local", ingress_class: "traefik", tls_mode: "none", issuer: null },
    allow_unauthenticated: true,
    ...over,
  };
}

/**
 * 요청 본문의 노출에 서버 기본값(host `<name>.default.apps.local`, ingress traefik, tls none)을 채운 스냅샷과 그 주소
 * `<scheme>://<host>` 다. 노출을 끄면 빈 스냅샷이고 주소는 null 이다. MCP Server·Gateway 와 Agent 가 같이 쓴다.
 */
function baseExposure(inp: ExposureInput | null | undefined, name: string): { exposure: Exposure; origin: string | null } {
  if (!inp?.enabled) {
    return { exposure: { enabled: false, host: null, ingress_class: null, tls: { mode: "none", secret_name: null, issuer: null } }, origin: null };
  }
  const host = inp.host ?? `${name}.default.apps.local`;
  const mode = inp.tls?.mode ?? "none";
  return {
    exposure: {
      enabled: true,
      host,
      ingress_class: inp.ingress_class ?? "traefik",
      tls: { mode, secret_name: inp.tls?.secret_name ?? null, issuer: inp.tls?.issuer ?? null },
    },
    origin: `${mode === "none" ? "http" : "https"}://${host}`,
  };
}

/** 응답의 exposure 스냅샷과 external_url 을 요청 본문에서 만든다 (서버 규칙: <scheme>://<host>/mcp). */
export function exposureOf(inp: ExposureInput | null | undefined, name: string): { exposure: Exposure; external_url: string | null } {
  const { exposure, origin } = baseExposure(inp, name);
  return { exposure, external_url: origin ? `${origin}/mcp` : null };
}

export const UNAUTH_WARNING = "unauthenticated_exposure: the platform runs in header mode, so this endpoint is exposed without authentication (cluster ingress.allow_unauthenticated is true)";

/** 목이 이미 쓰인 것으로 치는 노출 host 다. 노출 테스트가 이 host 를 넣어 409 host_conflict 를 본다. */
export const TAKEN_HOST = "taken.default.apps.local";

/**
 * 플랫폼 `services/exposure.py` 의 `_resolve_host` 처럼 이미 쓰인 host 면 409 host_conflict 응답이다. 노출을 끄거나
 * host 가 `TAKEN_HOST` 가 아니면 null.
 */
export function hostInUse(exposure: Pick<ExposureInput, "enabled" | "host"> | null | undefined): Response | null {
  if (!exposure?.enabled || exposure.host !== TAKEN_HOST) return null;
  return apiError(409, "host_conflict", `host ${TAKEN_HOST} is already exposed on this platform`);
}

const CORS_ORIGIN_RE = /^https?:\/\/[^/]+$/;

/**
 * 서버 prepare 의 노출 검증·렌더를 흉내 낸다 (D27). header 모드 422 `unauthenticated_exposure`, origin 형식 422
 * `invalid_cors_origin`, host 가 `TAKEN_HOST` 면 409 `host_conflict`. external_url 은 `<scheme>://<host>` 다.
 */
export function agentExposureOf(
  inp: AgentExposureInput | null | undefined,
  name: string,
  ready: boolean,
): { exposure: AgentExposure; external_url: string | null; exposure_status: ExposureStatus | null } | { error: Response } {
  const { exposure, origin } = baseExposure(inp, name);
  if (!inp?.enabled) return { exposure: { ...exposure, cors_origins: [], allowed_groups: [] }, external_url: null, exposure_status: null };
  if (authConfigState.mode !== "oidc") return { error: apiError(422, "unauthenticated_exposure", "Agent exposure requires oidc mode") };
  const bad = (inp.cors_origins ?? []).find((o) => !CORS_ORIGIN_RE.test(o) || o !== o.toLowerCase());
  if (bad !== undefined) return { error: apiError(422, "invalid_cors_origin", `invalid CORS origin '${bad}': expected ^https?://[^/]+$ in lower case`) };
  const taken = hostInUse(exposure);
  if (taken) return { error: taken };
  const mode = exposure.tls.mode;
  return {
    exposure: { ...exposure, cors_origins: [...(inp.cors_origins ?? [])], allowed_groups: [...(inp.allowed_groups ?? [])] },
    external_url: origin,
    exposure_status: { front_phase: null, address_assigned: ready, tls_secret_present: mode === "secret" || mode === "cert-manager" ? true : null, certificate_ready: mode === "cert-manager" ? ready : null },
  };
}
