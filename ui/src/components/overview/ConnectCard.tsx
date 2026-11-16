/**
 * "연결" 카드 (P32 10절, P33 16절). Gateway 개요와 노출한 MCP Server 개요에서 주소 카드 바로 아래에 선다.
 *
 * 밖의 MCP 클라이언트(Claude Code·Cursor·VS Code·Codex·curl)가 외부 주소를 부르는 설정 조각을 보이고, 플랫폼이 낸
 * 토큰을 받는 경계면 그 자리에서 개인 MCP 토큰을 받는다. 갈래는 인증 경계다. 플랫폼 토큰을 받는 경계(local, 데이터
 * 플레인이 플랫폼을 믿는 oidc, P53b 6.1절)는 발급 폼과 토큰 조각, IdP 를 믿는 oidc 는 IdP 토큰을 쓴다는 안내와 같은
 * 조각, anonymous(header 모드)는 헤더 없는 조각과 누구나 부를 수 있다는 경고다.
 *
 * 플랫폼 토큰을 받는 경계이고 OAuth 인가 서버가 켜졌으면 "로그인으로 연결" 탭이 맨 앞에 서고 위의 토큰 갈래는 "토큰으로
 * 연결" 로 묶인다. 로그인 조각에는 주소만 있고 클라이언트가 401 에서 스스로 브라우저 로그인과 동의 화면을 연다.
 *
 * 조각에는 토큰 값을 넣지 않고 env `DAEGAM_PAT` 을 가리킨다(`lib/connectSnippets`). 발급 폼은 그 리소스의 테넌트로
 * 고정하고, 보는 사람이 그 테넌트의 멤버가 아니면 폼 대신 멤버 계정으로 로그인하라고 말한다.
 */
import { useState } from "react";

import { IssueTokenForm } from "@/components/tokens/IssueTokenForm";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { TabBar } from "@/components/ui/tabs";
import { useT } from "@/i18n";
import { oauthEnabled, takesPlatformTokens, useAuthState, type AuthBoundary } from "@/lib/auth";
import { CLIENT_LABEL, CONNECT_MODES, PAT_ENV, READ_PAT_COMMAND, connectClients, connectSnippets, type ConnectClient, type ConnectMode, type ConnectTarget } from "@/lib/connectSnippets";
import { hasIssueDraft, memberTenants } from "@/lib/personalTokens";

/**
 * 이 카드의 발급 폼이 IdP 로 다시 로그인하러 떠날 때 입력을 남기는 이름. 리소스마다 따로 두어, 돌아오지 않은 입력이 다른
 * Gateway 나 MCP Server 의 카드에 되살아나지 않게 하고 내 MCP 토큰 화면의 폼과도 가른다.
 */
function connectFormKey(tenant: string, name: string): string {
  return `connect:${tenant}:${name}`;
}

export interface ConnectCardProps {
  /** K8s 이름. 클라이언트 설정의 서버 이름이 된다. */
  name: string;
  /** 발급 폼의 이름 기본값("<표시 이름> 연결")에 쓴다. */
  displayName: string;
  tenant: string;
  externalUrl: string | null | undefined;
  auth: AuthBoundary;
  /** 외부 주소가 없을 때 배포 탭으로 간다. */
  onDeploy: () => void;
}

export function ConnectCard({ name, displayName, tenant, externalUrl, auth, onDeploy }: ConnectCardProps) {
  const t = useT();
  const text = t.detail.connectCard;
  return (
    <Card data-testid="connect-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {externalUrl ? (
          <ConnectBody name={name} displayName={displayName} tenant={tenant} url={externalUrl} auth={auth} />
        ) : (
          <>
            <p className="text-sm text-muted" data-testid="connect-no-address">
              {text.noAddress}
            </p>
            <div>
              <Button size="sm" variant="outline" data-testid="connect-deploy" onClick={onDeploy}>
                {text.toDeploy}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

interface BodyProps {
  name: string;
  displayName: string;
  tenant: string;
  url: string;
  auth: AuthBoundary;
}

/** 토큰 갈래의 입력. platform 은 경계가 플랫폼 토큰을 받는가(`takesPlatformTokens`)다. */
interface TokenProps extends BodyProps {
  platform: boolean;
}

function ConnectBody(props: BodyProps) {
  const authState = useAuthState();
  const platform = takesPlatformTokens(props.auth, authState);
  return platform && oauthEnabled(authState) ? <ConnectModes {...props} /> : <TokenConnect {...props} platform={platform} />;
}

function ConnectModes(props: BodyProps) {
  const t = useT();
  // 발급 폼이 IdP 로 다시 로그인하러 떠났다 돌아왔으면 그 입력이 남아 있는 토큰 갈래에서 연다
  const [mode, setMode] = useState<ConnectMode>(() => (hasIssueDraft(connectFormKey(props.tenant, props.name)) ? "token" : "login"));
  return (
    <>
      <TabBar tabs={CONNECT_MODES} value={mode} onChange={setMode} label={(m) => t.detail.connectCard.mode[m]} />
      {mode === "login" ? <LoginConnect name={props.name} url={props.url} /> : <TokenConnect {...props} platform />}
    </>
  );
}

/** 주소만 넣는 조각. `resource` 를 보내지 않는 클라이언트는 로그인을 시작하지 못하므로 토큰 갈래를 가리킨다. */
function LoginConnect({ name, url }: { name: string; url: string }) {
  const t = useT();
  const text = t.detail.connectCard;
  return (
    <>
      <p className="text-sm text-muted" data-testid="connect-login">
        {text.login}
      </p>
      <ClientSnippets target={{ name, url, via: "login" }} />
      <p className="text-xs text-muted" data-testid="connect-login-fallback">
        {text.loginFallback}
      </p>
    </>
  );
}

function TokenConnect({ name, displayName, tenant, url, auth, platform }: TokenProps) {
  const t = useT();
  const text = t.detail.connectCard;
  const withToken = auth !== "anonymous";
  return (
    <>
      {platform ? <PlatformIssue tenant={tenant} defaultLabel={text.defaultLabel(displayName)} formKey={connectFormKey(tenant, name)} /> : null}
      {auth === "oidc" && !platform ? (
        <Alert variant="info" data-testid="connect-oidc">
          {text.oidc(PAT_ENV)}
        </Alert>
      ) : null}
      {auth === "anonymous" ? (
        <Alert variant="warn" data-testid="connect-anonymous">
          {text.anonymous}
        </Alert>
      ) : null}
      {withToken ? <CodeSnippet title={text.readPatTitle} code={READ_PAT_COMMAND} copyLabel={text.readPatCopy} testId="connect-read-pat" copyTestId="connect-read-pat-copy" note={text.readPatNote} /> : null}
      <ClientSnippets target={{ name, url, via: withToken ? "token" : "open" }} />
      {platform ? (
        <p className="text-xs text-muted" data-testid="connect-scope">
          {text.scope(tenant)}
        </p>
      ) : null}
    </>
  );
}

/**
 * 플랫폼 토큰을 받는 경계의 발급. 그 테넌트의 소속 줄이 있어야 서버가 내주므로 없으면 폼 대신 안내다. 확인은 폼이 모드를
 * 보고 한다(local 은 비밀번호, oidc 는 10분 안의 IdP 로그인).
 */
function PlatformIssue({ tenant, defaultLabel, formKey }: { tenant: string; defaultLabel: string; formKey: string }) {
  const t = useT();
  const { me } = useAuthState();
  if (!memberTenants(me).includes(tenant)) {
    return (
      <Alert variant="info" data-testid="connect-not-member">
        {t.detail.connectCard.notMember}
      </Alert>
    );
  }
  return <IssueTokenForm fixedTenant={tenant} defaultLabel={defaultLabel} formKey={formKey} />;
}

function ClientSnippets({ target }: { target: ConnectTarget }) {
  const t = useT();
  const [client, setClient] = useState<ConnectClient>("claude");
  return (
    <div className="flex flex-col gap-3" data-testid="connect-snippets">
      <TabBar tabs={connectClients(target.via)} value={client} onChange={setClient} label={(c) => CLIENT_LABEL[c]} />
      {connectSnippets(t, client, target).map((s) => (
        <CodeSnippet key={s.id} title={s.title} code={s.code} note={s.note} copyLabel={t.detail.connectCard.copySettings(CLIENT_LABEL[client])} testId={`snippet-${s.id}`} copyTestId={`snippet-${s.id}-copy`} />
      ))}
    </div>
  );
}
