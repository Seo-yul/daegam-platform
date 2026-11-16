import { type FormEvent, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { LoginError, initAuth, loginLocal, passwordPath, safeReturnTo, useAuthState } from "@/lib/auth";
import { lockedMessage } from "@/lib/password";
import { ownValue } from "@/lib/utils";
import { AuthFrame } from "@/pages/auth/AuthFrame";

/**
 * /login: local 인증 모드(D40)의 로그인. 껍데기(Layout) 밖에 있다.
 * 없는 사용자와 틀린 비밀번호는 서버가 같은 401 을 내므로 화면도 한 문장으로 말한다.
 * 429 는 잠긴 것이라 남은 시간을 말한다. 첫 로그인(must_change_password)은 비밀번호 바꾸기로 먼저 보내고, 바꾼 뒤
 * returnTo 로 돌아가게 그것을 넘긴다. `reauth=1` 이면 저장된 토큰이 있어도 폼을 보인다. MCP 연결의 허용이 최근
 * 10분 안의 로그인을 요구할 때 동의 화면이 여기로 보낸다 (P33 7절).
 */
export function LoginPage() {
  const t = useT();
  const auth = useAuthState();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const returnTo = safeReturnTo(params.get("returnTo"));
  const reauth = params.get("reauth") === "1";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  // 실패한 까닭(서버 오류나 연결 실패). 문장은 그릴 때 loginMessage 가 만든다
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    void initAuth();
  }, []);
  // 이미 로그인돼 있거나 local 모드가 아니면 여기 있을 이유가 없다. 다시 로그인하러 왔으면 폼에 머문다
  useEffect(() => {
    if (auth.loading || auth.mode === "unknown") return;
    if (auth.mode !== "local") navigate("/", { replace: true });
    else if (auth.token && !reauth) navigate(afterSignIn(returnTo, auth.mustChangePassword), { replace: true });
  }, [auth.loading, auth.mode, auth.token, auth.mustChangePassword, navigate, returnTo, reauth]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setFailure(null);
    setPending(true);
    try {
      const r = await loginLocal(username.trim(), password);
      navigate(afterSignIn(returnTo, r.mustChangePassword), { replace: true });
    } catch (err) {
      setFailure({ error: err });
    } finally {
      setPending(false);
    }
  }

  return (
    <AuthFrame title={t.auth.login.title} testId="login-page">
      {reauth ? (
        <Alert variant="info" data-testid="login-reauth">
          {t.auth.login.reauth}
        </Alert>
      ) : null}
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)}>
        <Field label={t.auth.username} htmlFor="login-username">
          <Input id="login-username" data-testid="login-username" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label={t.auth.password} htmlFor="login-password">
          <Input id="login-password" data-testid="login-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {failure ? (
          <Alert variant="error" data-testid="login-error">
            {loginMessage(t, failure.error)}
          </Alert>
        ) : null}
        <Button type="submit" data-testid="login-submit" disabled={pending || !username.trim() || !password}>
          {pending ? t.auth.signingIn : t.account.menu.login}
        </Button>
      </form>
    </AuthFrame>
  );
}

/** 로그인 뒤 갈 곳. 비밀번호를 바꿔야 하면 그 화면을 거쳐 돌아간다. */
function afterSignIn(returnTo: string, mustChangePassword: boolean): string {
  return mustChangePassword ? passwordPath(returnTo) : returnTo;
}

/** 서버 오류 코드를 사용자 문장으로. 잠김은 비밀번호 바꾸기·토큰 발급과 같은 문장이고, 서버에 닿지 못했으면 공통 문장이다. */
export function loginMessage(t: Messages, err: unknown): string {
  if (!(err instanceof LoginError)) return t.common.networkError;
  if (err.code === "login_locked") return lockedMessage(t, err.retryAfter);
  return ownValue(t.auth.login.errors, err.code) ?? t.auth.login.failed(err.code);
}
