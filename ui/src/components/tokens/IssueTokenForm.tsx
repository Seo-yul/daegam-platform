/**
 * 개인 MCP 토큰 발급 폼 (P32 10절, P53b 6.4절). 내 MCP 토큰 화면, 연결 카드, Tenant Settings 의 서비스 어카운트가 같이 쓴다.
 *
 * 테넌트, 이름, 만료를 받는다. 12시간짜리 제어면 토큰만으로 1년짜리 토큰을 만들지 못하게, local 모드는 현재 비밀번호를 다시
 * 받고(틀리면 서버가 403 을 내며 로그인과 같은 잠금에 센다), oidc 모드는 IdP 로그인이 10분 안이어야 한다. 오래됐으면 입력을
 * 이 탭에 남기고 IdP 로 다시 로그인하러 떠났다가 같은 폼으로 돌아와 입력을 되살린다. 두 번 다시 로그인해도 새 로그인을 받지
 * 못했거나, 플랫폼이 다시 로그인하라는데 지금 ID 토큰이 이미 최근 로그인이면 떠나지 않고 멈춰 알린다. 연결 카드는 테넌트를 그 리소스의 것으로 고정하고 이름에 기본값을 준다. 발급한 토큰은 폼
 * 아래에 한 번만 보인다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { useLocation } from "react-router";

import { ApiError } from "@/api/client";
import { issueMyToken } from "@/api/resources";
import type { IssuedPersonalToken, PersonalTokenIssueIn } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { OneTimeToken } from "@/components/tokens/OneTimeToken";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { markReauthFailed, needsFreshSignIn, startFreshSignIn, useAuthState } from "@/lib/auth";
import {
  CUSTOM_EXPIRY,
  DEFAULT_MAX_DAYS,
  dateRange,
  defaultExpiry,
  expiresAtFor,
  expiryLabel,
  expiryPresets,
  clearIssueDraft,
  passwordFieldError,
  peekIssueDraft,
  saveIssueDraft,
} from "@/lib/personalTokens";
import { MY_TOKENS_KEY } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { formatClock } from "@/lib/utils";

/** 서버가 받는 이름의 길이 (services/personal_tokens.py 의 LABEL_MAX). */
const LABEL_MAX = 64;
/** 내 개인 토큰 폼이 다시 로그인하러 떠날 때 입력을 남기는 이름. */
const MY_TOKEN_FORM = "me";

export interface IssueTokenFormProps {
  /** 고를 수 있는 테넌트. 지금 선택한 테넌트가 들어 있으면 그것이 처음 값이다. */
  tenants?: string[];
  /** 주면 테넌트를 고르지 않고 이것으로 낸다(연결 카드, 서비스 어카운트). */
  fixedTenant?: string;
  defaultLabel?: string;
  /** 주면 이것으로 낸다(서비스 어카운트). 없으면 내 개인 토큰이다. */
  issue?: (body: PersonalTokenIssueIn) => Promise<IssuedPersonalToken>;
  /** 낸 뒤 다시 읽을 목록. */
  listKey?: readonly unknown[];
  /** 처음 고른 만료(일). 상한을 넘거나 미리 정한 값이 아니면 기본 선택이다. */
  defaultDays?: number;
  /** IdP 로 다시 로그인하러 떠날 때 입력을 남기는 이름. 한 화면에 폼이 여럿이면 저마다 다르다. */
  formKey?: string;
}

export function IssueTokenForm({
  tenants = [],
  fixedTenant,
  defaultLabel = "",
  issue = issueMyToken,
  listKey = MY_TOKENS_KEY,
  defaultDays,
  formKey = MY_TOKEN_FORM,
}: IssueTokenFormProps) {
  const t = useT();
  const text = t.account.issue;
  const queryClient = useQueryClient();
  const auth = useAuthState();
  const oidc = auth.mode === "oidc";
  const maxDays = auth.config?.personal_tokens?.max_days ?? DEFAULT_MAX_DAYS;
  const current = useTenant();
  const location = useLocation();
  // 다시 로그인하고 돌아왔으면 떠나기 전의 입력이다. 처음 그릴 때 읽고, 그린 뒤에 지운다
  const [draft] = useState(() => peekIssueDraft(formKey));
  useEffect(() => clearIssueDraft(formKey), [formKey]);
  // 소속은 /me 가 끝나야 오므로 고른 값만 상태로 두고 테넌트는 렌더마다 목록에서 푼다
  const [picked, setPicked] = useState(draft?.tenant ?? "");
  const tenant = fixedTenant ?? [picked, current].find((name) => tenants.includes(name)) ?? tenants[0] ?? "";
  const [label, setLabel] = useState(draft?.label ?? defaultLabel);
  const [expiry, setExpiry] = useState(() => draft?.expiry ?? defaultExpiry(maxDays, defaultDays));
  const [date, setDate] = useState(draft?.date ?? "");
  const [password, setPassword] = useState("");
  const [issued, setIssued] = useState<IssuedPersonalToken | null>(null);
  const trimmed = label.trim();

  /** IdP 로 다시 로그인하러 떠난다. 돌아오면 이 폼이 입력을 되살린다. */
  async function signInAgain(): Promise<void> {
    saveIssueDraft(formKey, { tenant: picked, label, expiry, date });
    await startFreshSignIn(location.pathname + location.search);
  }

  /** 플랫폼의 403 reauth_required. 지금 ID 토큰이 오래면 다시 로그인하고, 이미 최근이면 다시 로그인해도 같으니 멈춘다. */
  async function answerReauth(): Promise<void> {
    if (await needsFreshSignIn()) await signInAgain();
    else markReauthFailed();
  }

  const mutation = useMutation({
    // 만료는 누른 순간의 시각에서 센다. 화면을 오래 열어 두어도 1일·상한 경계가 밀리지 않는다
    mutationFn: (expiresAt: string) => issue({ tenant, label: trimmed, expires_at: expiresAt, ...(oidc ? {} : { password }) }),
    onSuccess: (token) => {
      setIssued(token);
      setPassword("");
      void queryClient.invalidateQueries({ queryKey: listKey });
    },
    onError: (err) => {
      if (oidc && err instanceof ApiError && err.code === "reauth_required") void answerReauth();
    },
  });
  const passwordError = passwordFieldError(t, mutation.error);
  const ready =
    Boolean(tenant) && trimmed.length > 0 && trimmed.length <= LABEL_MAX && (oidc || Boolean(password)) && expiresAtFor(expiry, date, maxDays) !== null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || mutation.isPending) return;
    if (await needsFreshSignIn()) {
      await signInAgain();
      return;
    }
    const expiresAt = expiresAtFor(expiry, date, maxDays);
    if (expiresAt) mutation.mutate(expiresAt);
  }

  const range = dateRange(maxDays);
  const reauthPending = oidc && mutation.error instanceof ApiError && mutation.error.code === "reauth_required";
  return (
    <form className="flex flex-col gap-3" data-testid="issue-token-form" onSubmit={(e) => void submit(e)}>
      <div className="grid gap-3 md:grid-cols-2">
        {fixedTenant ? null : (
          <Field label={text.tenant} htmlFor="it-tenant" hint={text.tenantHint}>
            <Select id="it-tenant" data-testid="it-tenant" value={tenant} placeholder={text.tenantPlaceholder} onChange={(e) => setPicked(e.target.value)}>
              {tenants.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field label={text.name} htmlFor={`it-label-${formKey}`} hint={text.nameHint}>
          <Input
            id={`it-label-${formKey}`}
            data-testid="it-label"
            maxLength={LABEL_MAX}
            autoComplete="off"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </Field>
        <Field label={text.expiry} htmlFor={`it-expiry-${formKey}`}>
          <Select id={`it-expiry-${formKey}`} data-testid="it-expiry" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
            {expiryPresets(maxDays).map((d) => (
              <option key={d} value={String(d)}>
                {expiryLabel(t, d)}
              </option>
            ))}
            <option value={CUSTOM_EXPIRY}>{text.pickDate}</option>
          </Select>
        </Field>
        {expiry === CUSTOM_EXPIRY ? (
          <Field label={text.expiryDate} htmlFor={`it-date-${formKey}`} hint={text.dateHint(maxDays)}>
            <Input
              id={`it-date-${formKey}`}
              data-testid="it-date"
              type="date"
              min={range.min}
              max={range.max}
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </Field>
        ) : null}
        {oidc ? null : (
          <Field label={text.password} htmlFor={`it-password-${formKey}`} hint={text.passwordHint} error={passwordError}>
            <Input
              id={`it-password-${formKey}`}
              data-testid="it-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                if (passwordError) mutation.reset();
              }}
            />
          </Field>
        )}
      </div>
      {oidc ? (
        <p className="text-sm text-muted" data-testid="it-reauth-hint">
          {text.reauthHint}
        </p>
      ) : null}
      {oidc && auth.reauthFailed ? (
        <Alert variant="error" data-testid="it-reauth-failed">
          {t.auth.reauthFailed}
        </Alert>
      ) : null}
      {mutation.error && !passwordError && !reauthPending ? <ErrorMessage error={mutation.error} testId="it-error" /> : null}
      <div>
        <Button type="submit" data-testid="it-submit" disabled={!ready || mutation.isPending}>
          {mutation.isPending ? text.issuing : text.submit}
        </Button>
      </div>
      {issued ? <OneTimeToken token={issued.token} note={issued.ready_at ? text.readyAt(formatClock(issued.ready_at)) : undefined} /> : null}
    </form>
  );
}
