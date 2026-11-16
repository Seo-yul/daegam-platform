/**
 * 외부 노출 섹션 (resource-model 11절, D22). MCP Server·Gateway 의 배포 대화상자와
 * 상세 Basic 탭이 같은 컴포넌트를 쓴다. 클러스터·네임스페이스가 정해지면 `exposure-options` 를 불러
 * 허용 네임스페이스, IngressClass, TLS Secret 이름, ClusterIssuer, 기본값을 채운다.
 * 프로토콜은 TLS 선택에 종속된다. TLS 가 없으면 http, 있으면 https 이고 따로 고르지 않는다.
 * Agent(D27)는 `requireOidc` 로 같은 섹션을 쓰되 header 모드에서는 클러스터의 allow_unauthenticated 와
 * 무관하게 잠기고, `children` 으로 cors_origins·allowed_groups 를 끼운다 (AgentExposureSection).
 *
 * 폼 상태·본문 변환·호스트 규칙·422 코드 대응표는 화면에 안 걸리는 규칙이라 `lib/exposure.ts` 에 있고,
 * 상세의 상태 배지는 `ExposureStatus.tsx`, Basic 탭의 편집 카드는 `ExposureCard.tsx` 다.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import { getExposureOptions, getTenantSettings, listClusters } from "@/api/resources";
import type { ExposureOptions, ExposureTLSMode } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Alert } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { tokenAuth, useAuthState } from "@/lib/auth";
import { exposureInvalid, previewUrl, suggestHost, type ExposureErrors, type ExposureField, type ExposureForm } from "@/lib/exposure";
import { CLUSTERS_KEY, tenantSettingsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

// -- 클러스터 기본값 풀기 ------------------------------------------------------------------

/**
 * 생성 폼이 "(테넌트 기본값)" 을 고르면 서버 규칙(테넌트 기본 > 플랫폼 기본 > home)대로 클러스터를
 * 짐작해 옵션을 미리 조회한다. 플랫폼 기본은 settings:read 가 없으면 못 읽으므로 home 으로 대신한다.
 */
export function useResolvedClusterId(clusterId: string | null | undefined): string | null {
  const tenant = useTenant();
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  const settings = useQuery({ queryKey: tenantSettingsKey(tenant), queryFn: () => getTenantSettings(tenant), enabled: !clusterId });
  if (clusterId) return clusterId;
  if (settings.data?.default_cluster_id) return settings.data.default_cluster_id;
  return clusters.data?.find((c) => c.home)?.id ?? null;
}

// -- 섹션 ----------------------------------------------------------------------------------

export interface ExposureSectionProps {
  /** 이미 풀린 클러스터 id. null 이면 옵션을 조회하지 않는다. */
  clusterId: string | null;
  /** 리소스 이름. host 기본값 템플릿의 `{name}` 에 들어간다. */
  name: string;
  value: ExposureForm;
  onChange: (next: ExposureForm) => void;
  errors?: ExposureErrors;
  /** edit 이면 네임스페이스는 바꿀 수 없고 host 기본값을 제안하지 않는다. */
  mode?: "create" | "edit";
  /**
   * 네임스페이스 칸을 보일지. 복제 대화상자는 `exposure` 만 받고 네임스페이스는 받지 않으므로 끈다.
   * 꺼도 `value.namespace`(빈 문자열 = 테넌트 기본)는 옵션 조회에 그대로 쓴다.
   */
  showNamespace?: boolean;
  /**
   * Agent(D27): oidc 모드에서만 켤 수 있다. header 모드면 토글을 잠그고 클러스터의 allow_unauthenticated
   * 분기와 무인증 경고는 타지 않는다. 잠긴 이유의 안내문은 호출부가 보인다.
   */
  requireOidc?: boolean;
  /** 미리보기 URL 에 붙는 경로. MCP·Gateway 는 /mcp, Agent 는 빈 문자열이다. */
  urlPath?: string;
  /** 켰을 때 TLS 행 아래, 미리보기 위에 끼우는 추가 필드. */
  children?: ReactNode;
}

/**
 * 목록에 없는 지금 값을 옵션 하나로 덧붙인다. 옵션 조회가 늦거나 그 값이 목록에서 빠졌을 때 Select 가
 * 빈칸으로 보여 저장된 값이 사라진 것처럼 읽히지 않게 한다.
 */
function currentOption(list: readonly string[], value: string): ReactNode {
  return value && !list.includes(value) ? <option value={value}>{value}</option> : null;
}

export function ExposureSection({ clusterId, name, value, onChange, errors = {}, mode = "create", requireOidc = false, urlPath = "/mcp", showNamespace = true, children }: ExposureSectionProps) {
  const t = useT();
  const text = t.shared.exposureSection;
  const auth = useAuthState();
  // requireOidc 면 header 모드의 무인증 노출 규칙(D16) 대신 oidc 전용 규칙(D27)을 탄다
  const headerMode = !requireOidc && auth.mode === "header";
  const oidcLocked = requireOidc && !tokenAuth(auth.mode);
  const q = useQuery({
    queryKey: ["exposure-options", clusterId, value.namespace],
    queryFn: () => getExposureOptions(clusterId as string, value.namespace || null),
    enabled: Boolean(clusterId),
  });
  const opts: ExposureOptions | undefined = q.data;
  const forbidden = q.error instanceof ApiError && q.error.status === 403 ? q.error : null;
  const missing = forbidden?.extra<string[]>("missing") ?? [];

  // 사용자가 손댄 필드는 옵션이 다시 와도 덮어쓰지 않는다. edit 모드는 저장된 값이 있으니 전부 손댄 것으로 본다
  const edited = mode === "edit";
  const touched = useRef({ host: edited, tls: edited, class: edited, secret: edited, issuer: edited });

  // 옵션이 오면 손대지 않은 필드를 기본값으로 채우고, host 는 템플릿의 {name} 에 리소스 이름을 넣어
  // 이름을 따라간다. 한 effect 에서 한 번의 onChange 로 합치고 (value, opts, suggested) 만으로 결과가
  // 정해지게 둔다. 따로 두거나 "한 번 적용" 플래그에 기대면 같은 커밋에서 두 번 돌 때(StrictMode 포함)
  // 뒤의 onChange 가 앞의 것을 같은 value 클로저로 덮어쓴다.
  const suggested = suggestHost(opts?.defaults.host, name);
  // 앞단 프록시가 TLS 를 끝내는 클러스터는 edge 만 받는다 (D54)
  const edgeCluster = opts?.defaults.tls_mode === "edge";
  useEffect(() => {
    if (!opts) return;
    const next = { ...value };
    const userSet = touched.current;
    if (!userSet.class && !next.ingressClass && opts.defaults.ingress_class) next.ingressClass = opts.defaults.ingress_class;
    if (!userSet.tls) {
      const d = opts.defaults.tls_mode;
      if (d === "edge") next.tlsMode = "edge";
      else if (d === "secret" && opts.tls_secrets.length) next.tlsMode = "secret";
      else if (d === "cert-manager" && opts.cluster_issuers.length) next.tlsMode = "cert-manager";
      else next.tlsMode = "none";
    }
    if (!userSet.secret && next.tlsMode === "secret" && !opts.tls_secrets.includes(next.secretName)) next.secretName = opts.tls_secrets[0] ?? "";
    if (!userSet.issuer && next.tlsMode === "cert-manager" && !next.issuer && opts.defaults.issuer) next.issuer = opts.defaults.issuer;
    if (!userSet.host && next.host !== suggested) next.host = suggested;
    if (JSON.stringify(next) !== JSON.stringify(value)) onChange(next);
  }, [opts, suggested]); // eslint-disable-line react-hooks/exhaustive-deps

  // 노출이 막힌 클러스터면 켜 둔 값을 내린다
  const disabledReason = !opts
    ? null
    : !opts.enabled
      ? text.disabled.cluster
      : headerMode && !opts.allow_unauthenticated
        ? text.disabled.unauthenticated
        : null;
  // 인증 모드를 아직 모르는 동안(unknown)은 잠그기만 하고 값은 내리지 않는다. 저장된 값이 있는 edit 폼이 깜빡이지 않게 한다
  const forceOff = Boolean(disabledReason) || (requireOidc && auth.mode === "header");
  useEffect(() => {
    if (forceOff && value.enabled) onChange({ ...value, enabled: false });
  }, [forceOff]); // eslint-disable-line react-hooks/exhaustive-deps

  const hostInvalid = exposureInvalid(value);
  const secretsAvailable = Boolean(opts?.tls_secrets.length);
  const issuersAvailable = Boolean(opts?.cluster_issuers.length);
  const namespaces = opts?.namespaces ?? (value.namespace ? [value.namespace] : []);
  const shownNamespace = value.namespace || opts?.namespace || "";

  function set<K extends keyof ExposureForm>(key: K, v: ExposureForm[K]) {
    onChange({ ...value, [key]: v });
  }

  const fieldError = (f: ExposureField) =>
    errors[f] ? (
      <p className="text-xs text-err-fg" data-testid={`exp-${f}-error`}>
        {errors[f]}
      </p>
    ) : null;

  return (
    <div className="flex flex-col gap-3" data-testid="exposure-section">
      {showNamespace ? (
        <Field label={text.labels.namespace} htmlFor="exp-namespace" hint={mode === "edit" ? text.namespaceFixed : text.namespaceHint}>
          <Select
            id="exp-namespace"
            data-testid="exp-namespace"
            value={shownNamespace}
            disabled={mode === "edit" || !clusterId}
            placeholder={clusterId ? t.shared.tenantDefault : text.clusterFirst}
            onChange={(e) => onChange({ ...value, namespace: e.target.value })}
          >
            {namespaces.map((ns) => (
              <option key={ns} value={ns}>
                {ns}
                {opts && ns === opts.namespace && !value.namespace ? ` ${t.shared.defaultValue}` : ""}
              </option>
            ))}
            {currentOption(namespaces, shownNamespace)}
          </Select>
          {fieldError("namespace")}
        </Field>
      ) : null}

      {q.isLoading ? <p className="text-xs text-muted">{text.loading}</p> : null}
      {forbidden ? (
        <Alert variant="warn" data-testid="exp-forbidden">
          {text.forbidden(missing.length ? missing.join(", ") : "secrets:list@ns")}
        </Alert>
      ) : q.error ? (
        <ErrorMessage error={q.error} testId="exp-options-error" />
      ) : null}

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          data-testid="exp-enabled"
          checked={value.enabled}
          disabled={Boolean(disabledReason) || oidcLocked || !clusterId}
          onChange={(e) => set("enabled", e.target.checked)}
        />
        <span>{text.enable}</span>
        <span className="text-xs text-muted">{text.enableHint}</span>
      </label>
      {disabledReason ? (
        <p className="text-xs text-muted" data-testid="exp-disabled-reason">
          {disabledReason}
        </p>
      ) : null}
      {fieldError("enabled")}

      {value.enabled ? (
        <div className="flex flex-col gap-3 rounded-chip border border-line px-3 py-3">
          {headerMode ? (
            <Alert variant="warn" data-testid="exp-warning">
              {text.unauthenticated}
            </Alert>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-[12rem_1fr]">
            <Field label={text.labels.ingressClass} hint={text.classHint} htmlFor="exp-class">
              <Select
                id="exp-class"
                data-testid="exp-class"
                value={value.ingressClass}
                onChange={(e) => {
                  touched.current.class = true;
                  set("ingressClass", e.target.value);
                }}
              >
                <option value="">{t.shared.clusterDefault}</option>
                {(opts?.ingress_classes ?? []).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
                {currentOption(opts?.ingress_classes ?? [], value.ingressClass)}
              </Select>
              {fieldError("class")}
            </Field>
            <Field label={text.labels.host} htmlFor="exp-host" hint={text.hostHint}>
              <Input
                id="exp-host"
                data-testid="exp-host"
                value={value.host}
                placeholder={opts?.defaults.host ?? "mcp.example.com"}
                aria-invalid={hostInvalid || Boolean(errors.host)}
                onChange={(e) => {
                  touched.current.host = true;
                  set("host", e.target.value);
                }}
              />
              {hostInvalid ? (
                <p className="text-xs text-err-fg" data-testid="exp-host-invalid">
                  {text.hostInvalid}
                </p>
              ) : null}
              {fieldError("host")}
            </Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-[12rem_1fr]">
            <Field
              label={text.labels.tls}
              hint={edgeCluster ? text.tlsHint.edge : text.tlsHint.choose}
              htmlFor="exp-tls-mode"
            >
              <Select
                id="exp-tls-mode"
                data-testid="exp-tls-mode"
                value={value.tlsMode}
                disabled={edgeCluster}
                onChange={(e) => {
                  touched.current.tls = true;
                  const mode = e.target.value as ExposureTLSMode;
                  onChange({
                    ...value,
                    tlsMode: mode,
                    secretName: mode === "secret" ? value.secretName || opts?.tls_secrets[0] || "" : value.secretName,
                    issuer: mode === "cert-manager" ? value.issuer || opts?.defaults.issuer || "" : value.issuer,
                  });
                }}
              >
                {edgeCluster ? (
                  <option value="edge">{text.tlsEdge}</option>
                ) : (
                  <>
                    <option value="none">{text.tlsNone}</option>
                    <option value="secret" disabled={!secretsAvailable}>
                      {text.tlsSecretOption}
                    </option>
                    <option value="cert-manager" disabled={!issuersAvailable}>
                      {text.certManagerOption}
                    </option>
                  </>
                )}
              </Select>
              {opts && !secretsAvailable ? (
                <p className="text-xs text-muted" data-testid="exp-no-secrets">
                  {text.noSecrets}
                </p>
              ) : null}
              {forbidden ? (
                <p className="text-xs text-muted" data-testid="exp-no-secrets">
                  {text.secretsForbidden}
                </p>
              ) : null}
              {opts && !issuersAvailable ? (
                <p className="text-xs text-muted" data-testid="exp-no-issuers">
                  {text.noIssuers}
                </p>
              ) : null}
              {fieldError("tls-mode")}
            </Field>
            {value.tlsMode === "secret" ? (
              <Field label={text.labels.tlsSecret} htmlFor="exp-tls-secret" hint={text.secretHint}>
                <Select
                  id="exp-tls-secret"
                  data-testid="exp-tls-secret"
                  value={value.secretName}
                  placeholder={text.select}
                  disabled={!secretsAvailable}
                  onChange={(e) => {
                    touched.current.secret = true;
                    set("secretName", e.target.value);
                  }}
                >
                  {(opts?.tls_secrets ?? []).map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                  {currentOption(opts?.tls_secrets ?? [], value.secretName)}
                </Select>
                {fieldError("tls-secret")}
              </Field>
            ) : value.tlsMode === "cert-manager" ? (
              <Field label={text.labels.issuer} htmlFor="exp-issuer" hint={text.issuerHint}>
                <Select
                  id="exp-issuer"
                  data-testid="exp-issuer"
                  value={value.issuer}
                  placeholder={text.select}
                  disabled={!issuersAvailable}
                  onChange={(e) => {
                    touched.current.issuer = true;
                    set("issuer", e.target.value);
                  }}
                >
                  {opts?.defaults.issuer ? <option value="">{text.issuerDefault(opts.defaults.issuer)}</option> : null}
                  {(opts?.cluster_issuers ?? []).map((i) => (
                    <option key={i} value={i}>
                      {i}
                    </option>
                  ))}
                  {currentOption(opts?.cluster_issuers ?? [], value.issuer)}
                </Select>
                {fieldError("issuer")}
              </Field>
            ) : edgeCluster ? null : (
              <div className="self-end text-xs text-muted">{text.tlsNote}</div>
            )}
          </div>
          {children}
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted">external_url</span>
            <span className="break-all font-mono text-xs" data-testid="exp-preview">
              {previewUrl(value, urlPath)}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
