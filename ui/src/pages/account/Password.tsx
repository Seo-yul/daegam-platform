import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { listMyTokens } from "@/api/resources";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { LoginError, changePassword, safeReturnTo, useAuthState } from "@/lib/auth";
import { MIN_PASSWORD_LENGTH, lockedMessage, passwordLongEnough, passwordTooShort } from "@/lib/password";
import { isLive } from "@/lib/personalTokens";
import { MY_TOKENS_KEY } from "@/lib/queryKeys";

/**
 * /account/password: local 인증 모드(D40)의 본인 비밀번호 바꾸기.
 * 첫 로그인이거나 관리자가 재설정한 뒤(must_change_password)에는 껍데기가 다른 화면 대신 여기를 연다.
 * 바꾸면 다른 세션은 끝나고 이 세션은 새 토큰으로 이어진다. 현재 비밀번호를 거듭 틀리면 로그인과 같은 잠금에 걸린다.
 * 바꾼 뒤에는 `returnTo` 로 돌아간다. 처음 로그인한 사람이 MCP 클라이언트의 연결 허용 화면으로 돌아가는 길이다 (P33 16절).
 * 개인 MCP 토큰도 모두 회수되므로(P32 7절) 몇 개가 회수되는지 폼 위에 먼저 말한다.
 */
export function PasswordPage() {
  const t = useT();
  const text = t.account.password;
  const auth = useAuthState();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  // 강제 변경 중에는 서버가 다른 API 를 403 으로 막는다. 그때는 묻지 않는다
  const tokens = useQuery({ queryKey: MY_TOKENS_KEY, queryFn: listMyTokens, enabled: auth.mode === "local" && !auth.mustChangePassword });
  const liveTokens = (tokens.data ?? []).filter(isLive).length;
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  // 문장이 아니라 오류를 둔다. 문장은 그릴 때 지금 언어로 만든다
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const mismatch = again.length > 0 && next !== again;
  const short = passwordTooShort(t, next);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (mismatch) return;
    setError(null);
    setPending(true);
    try {
      await changePassword(current, next);
      void queryClient.invalidateQueries({ queryKey: MY_TOKENS_KEY });
      navigate(safeReturnTo(params.get("returnTo")), { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  }

  return (
    <PageWidth data-testid="password-page" className="max-w-lg">
      <PageHeader title={text.title} testId="page-title" />
      <Card>
        <CardContent className="flex flex-col gap-4">
          {auth.mustChangePassword ? (
            <Alert variant="info" data-testid="password-required-notice">
              {text.required}
            </Alert>
          ) : null}
          {liveTokens ? (
            <Alert variant="warn" data-testid="pw-token-warning">
              {text.tokenWarning(liveTokens)}
            </Alert>
          ) : null}
          <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)}>
            <Field
              label={text.current}
              htmlFor="pw-current"
              hint={auth.mustChangePassword ? text.currentHint : undefined}
            >
              <Input id="pw-current" data-testid="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
            </Field>
            <Field label={text.next} htmlFor="pw-next" hint={text.nextHint(MIN_PASSWORD_LENGTH)} error={short}>
              <Input id="pw-next" data-testid="pw-next" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
            </Field>
            <Field label={text.again} htmlFor="pw-again">
              <Input id="pw-again" data-testid="pw-again" type="password" autoComplete="new-password" aria-invalid={mismatch || undefined} value={again} onChange={(e) => setAgain(e.target.value)} />
            </Field>
            {mismatch ? (
              <Alert variant="error" data-testid="pw-mismatch">
                {text.mismatch}
              </Alert>
            ) : null}
            {error ? (
              <Alert variant="error" data-testid="pw-error">
                {passwordMessage(t, error)}
              </Alert>
            ) : null}
            <div className="flex gap-2">
              <Button type="submit" data-testid="pw-submit" disabled={pending || !current || !passwordLongEnough(next) || mismatch}>
                {pending ? text.changing : text.submit}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </PageWidth>
  );
}

export function passwordMessage(t: Messages, err: unknown): string {
  if (err instanceof LoginError) {
    if (err.code === "invalid_credentials") return t.account.password.wrongCurrent;
    if (err.code === "login_locked") return lockedMessage(t, err.retryAfter);
    if (err.code === "weak_password") return t.account.password.weak(MIN_PASSWORD_LENGTH);
    return t.account.password.failed(err.code);
  }
  return t.common.networkError;
}
