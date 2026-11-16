import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { approveConsent, denyConsent, inspectConsent } from "@/api/resources";
import type { ConsentView } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import {
  initAuth,
  login,
  loginPath,
  markReauthFailed,
  needsFreshSignIn,
  passwordPath,
  startFreshSignIn,
  tokenAuth,
  useAuthState,
} from "@/lib/auth";
import { leave } from "@/lib/leave";
import { clientOrigin } from "@/lib/oauthClients";
import { clientName } from "@/lib/serverLiterals";
import { ownValue } from "@/lib/utils";
import { AuthFrame } from "@/pages/auth/AuthFrame";

/**
 * /oauth/consent: MCP 클라이언트가 연 브라우저 탭의 연결 동의 (P33 16절). /login 처럼 껍데기(Layout) 밖이고 스스로
 * initAuth 를 부른다. 인가 요청은 서버에 저장되지 않고 서명한 핸들(`?request=`)로 온다. 화면은 핸들을 본문으로 되돌려
 * 보내 내용을 받고, 허용·거절하면 서버가 준 주소로만 `window.location.assign` 한다. 폼 POST 가 없다.
 *
 * 클라이언트의 이름과 CIMD 문서는 신원이 아니다. 이 컴퓨터의 어떤 프로그램이든 내세울 수 있으므로 CIMD 와 DCR 모두 같은
 * 크기의 "직접 시작하지 않았다면 거절" 경고를 허용 버튼 위에 둔다. `?error=` 는 알려진 코드만 정해진 문구로 바꾸고 그
 * 밖의 글자는 화면에 싣지 않는다. 허용이 403 reauth_required 면 다시 로그인한 뒤 같은 핸들로 돌아온다.
 *
 * oidc 모드(P53b 6.4절·6.5절)는 IdP 로그인 뒤의 화면이다. 로그인하지 않았으면 IdP 로 보내고 같은 주소로 돌아온다. 허용하기
 * 전에 IdP 로그인이 10분보다 오래면 다시 로그인하러 떠났다가 같은 핸들로 돌아오고, 두 번 다시 로그인해도 새 로그인을 받지
 * 못했으면 떠나지 않고 멈춰 알린다.
 */

/**
 * 실패를 문장으로. 서버 오류 코드(`?error=` 의 글자나 API 오류)는 사전의 표에 있는 것만 그 문구이고, 표에 없는 코드와
 * 코드 없는 실패는 글자를 보이지 않고 generic 이다. 서버에 닿지 못했으면 공통 문장이다.
 */
export function consentMessage(t: Messages, err: unknown): string {
  const text = t.auth.consent;
  if (typeof err === "string") return ownValue(text.errors, err) ?? text.generic;
  if (err instanceof ApiError) return ownValue(text.errors, err.code) ?? text.generic;
  if (err === null || err === undefined) return text.generic;
  return t.common.networkError;
}

export function ConsentPage() {
  const t = useT();
  const text = t.auth.consent;
  const auth = useAuthState();
  const [params] = useSearchParams();
  const location = useLocation();
  const handle = params.get("request");
  const error = params.get("error");
  const here = location.pathname + location.search;

  useEffect(() => {
    void initAuth();
  }, []);

  if (auth.loading || auth.mode === "unknown") return <Notice>{text.checking}</Notice>;
  if (!tokenAuth(auth.mode)) return <Notice>{text.headerMode}</Notice>;
  if (error !== null || !handle) return <Failure error={error} />;
  if (!auth.token) return auth.mode === "oidc" ? <IdpSignIn returnTo={here} /> : <Navigate to={loginPath(here)} replace />;
  if (auth.mustChangePassword) return <Navigate to={passwordPath(here)} replace />;
  return <ConsentBody handle={handle} here={here} username={auth.me?.name ?? null} />;
}

/** oidc: 로그인하지 않았으면 IdP 로 떠난다. 같은 주소(핸들)로 돌아온다. */
function IdpSignIn({ returnTo }: { returnTo: string }) {
  const t = useT();
  useEffect(() => {
    void login(returnTo);
  }, [returnTo]);
  return <Notice>{t.auth.consent.checking}</Notice>;
}

function Notice({ children }: { children: ReactNode }) {
  const t = useT();
  return (
    <AuthFrame title={t.auth.consent.title} testId="consent-page" wide>
      <p className="text-sm text-muted" data-testid="consent-notice">
        {children}
      </p>
    </AuthFrame>
  );
}

/** error 는 `?error=` 의 글자나 요청을 읽다 난 오류다. */
function Failure({ error }: { error: unknown }) {
  const t = useT();
  return (
    <AuthFrame title={t.auth.consent.title} testId="consent-page" wide>
      <Alert variant="error" data-testid="consent-error">
        {consentMessage(t, error)}
      </Alert>
    </AuthFrame>
  );
}

function ConsentBody({ handle, here, username }: { handle: string; here: string; username: string | null }) {
  const t = useT();
  const text = t.auth.consent;
  const auth = useAuthState();
  const navigate = useNavigate();
  const inspect = useQuery({ queryKey: ["oauth-consent", handle], queryFn: () => inspectConsent(handle) });
  const [pending, setPending] = useState(false);
  // 허용·거절이 실패한 까닭. 문장은 그릴 때 consentMessage 가 만든다
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);
  const [leaving, setLeaving] = useState(false);

  /**
   * 플랫폼의 403 reauth_required. local 은 로그인 화면으로 가서 같은 핸들로 돌아온다. oidc 는 지금 ID 토큰이 오래면 IdP 로
   * 다시 로그인하러 떠나고, 이미 최근이면 다시 로그인해도 같으니 떠나지 않고 멈춘다.
   */
  async function answerReauth(): Promise<void> {
    if (auth.mode !== "oidc") navigate(loginPath(here, { reauth: true }));
    else if (await needsFreshSignIn()) await startFreshSignIn(here);
    else markReauthFailed();
  }

  /** approving 이면 허용이다. 허용은 오래 가는 연결을 만들므로 oidc 는 보내기 전에 IdP 로그인이 10분 안인지 본다. */
  async function answer(approving: boolean) {
    setPending(true);
    setFailure(null);
    try {
      if (approving && (await needsFreshSignIn())) {
        await startFreshSignIn(here);
        return;
      }
      const { redirect_to } = await (approving ? approveConsent : denyConsent)(handle);
      setLeaving(true);
      leave.assign(redirect_to);
    } catch (err) {
      if (err instanceof ApiError && err.code === "reauth_required") await answerReauth();
      else setFailure({ error: err });
    } finally {
      setPending(false);
    }
  }

  if (leaving) return <Notice>{text.leaving}</Notice>;
  if (inspect.isPending) return <Notice>{text.inspecting}</Notice>;
  if (inspect.isError) return <Failure error={inspect.error} />;
  const view = inspect.data;
  return (
    <AuthFrame title={text.heading(clientName(t, view.client.name))} testId="consent-page" wide>
      <ClientNote client={view.client} />
      {view.member ? <Target view={view} username={username} /> : <OutsiderTarget url={view.resource.url} />}
      <LoopbackWarning redirect={view.redirect} />
      {auth.mode === "oidc" && auth.reauthFailed ? (
        <Alert variant="error" data-testid="consent-reauth-failed">
          {t.auth.reauthFailed}
        </Alert>
      ) : null}
      {failure ? (
        <Alert variant="error" data-testid="consent-failure">
          {consentMessage(t, failure.error)}
        </Alert>
      ) : null}
      <div className="flex gap-2">
        {view.member ? (
          <Button data-testid="consent-approve" disabled={pending} onClick={() => void answer(true)}>
            {text.approve}
          </Button>
        ) : null}
        <Button variant="outline" data-testid="consent-deny" disabled={pending} onClick={() => void answer(false)}>
          {text.deny}
        </Button>
      </div>
    </AuthFrame>
  );
}

/** 클라이언트가 누구라고 말하는가. CIMD 도 신원 확인이 아니라 문서를 공개한 호스트만 말한다. */
function ClientNote({ client }: { client: ConsentView["client"] }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-muted" data-testid="consent-client">
        {t.auth.consent.origin(clientOrigin(t, client.kind, client.host))}
      </p>
      {client.name_collides ? (
        <Alert variant="warn" data-testid="consent-name-collides">
          {t.auth.consent.nameCollides}
        </Alert>
      ) : null}
    </div>
  );
}

/** 대상과 권한. 소속인 사람에게만 테넌트와 표시 이름이 온다. */
function Target({ view, username }: { view: ConsentView; username: string | null }) {
  const t = useT();
  const text = t.auth.consent;
  const { resource } = view;
  const tenant = resource.tenant ?? "";
  const minutes = Math.round(view.access_token_seconds / 60);
  return (
    <div className="flex flex-col gap-3" data-testid="consent-target">
      <dl className="grid grid-cols-[5rem_1fr] gap-y-1 text-sm">
        <dt className="text-muted">{resource.kind === "gateway" ? t.resources.kind.gateway : t.resources.kind["mcp-server"]}</dt>
        <dd>{resource.display_name}</dd>
        <dt className="text-muted">{text.publicUrl}</dt>
        <dd className="break-all font-mono text-xs">{resource.url}</dd>
        <dt className="text-muted">{text.tenant}</dt>
        <dd>{tenant}</dd>
      </dl>
      <p className="text-sm" data-testid="consent-scope">
        {text.scope(tenant, username, minutes, view.grant_days)}
      </p>
    </div>
  );
}

function OutsiderTarget({ url }: { url: string }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-2" data-testid="consent-target">
      <p className="break-all font-mono text-xs">{url}</p>
      <Alert variant="info" data-testid="consent-not-member">
        {t.auth.consent.notAMember}
      </Alert>
    </div>
  );
}

/** 결과는 언제나 이 컴퓨터(loopback)로 간다. 포트를 보지 않으므로 누가 내세웠는지는 알 수 없다. */
function LoopbackWarning({ redirect }: { redirect: ConsentView["redirect"] }) {
  const t = useT();
  const host = redirect.host.includes(":") ? `[${redirect.host}]` : redirect.host;
  const where = redirect.port ? `${host}:${redirect.port}` : host;
  return (
    <Alert variant="warn" data-testid="consent-loopback">
      {t.auth.consent.loopback(where)}
    </Alert>
  );
}
