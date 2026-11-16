import { useQuery, useQueryClient } from "@tanstack/react-query";

import { listMyOAuthGrants, listMyTokens, revokeMyOAuthGrant, revokeMyToken } from "@/api/resources";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { GrantList, type GrantSource } from "@/components/tokens/GrantList";
import { IssueTokenForm } from "@/components/tokens/IssueTokenForm";
import { TokenTable } from "@/components/tokens/TokenTable";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { idpConfirmDays, idpDataPlane, oauthConnections, tokenAuth, useAuthState } from "@/lib/auth";
import { memberTenants } from "@/lib/personalTokens";
import { MY_OAUTH_GRANTS_KEY, MY_TOKENS_KEY } from "@/lib/queryKeys";

const MY_GRANTS: GrantSource = { queryKey: MY_OAUTH_GRANTS_KEY, list: listMyOAuthGrants, revoke: revokeMyOAuthGrant };

/**
 * /account/tokens: 내 MCP 토큰 (P32 10절, P53b 6.5절). 플랫폼이 사람 토큰을 내는 모드(local, oidc)에서 밖의 MCP 클라이언트에
 * 넣을 개인 토큰을 발급하고 회수한다. 사람마다의 화면이라 메뉴 묶음이 아니라 사이드바 맨 아래의 계정 메뉴로 온다. 그 링크도
 * 두 모드에서 보인다(AccountMenu).
 *
 * oidc 모드는 두 안내를 더한다. 데이터 플레인이 IdP 토큰만 받는 동안(`data_plane_issuer` 가 idp)은 여기서 만든 토큰이
 * 플랫폼으로 전환한 뒤부터 쓰인다고 말하고, 긴 한도 동안 로그인하지 않으면 토큰과 연결이 회수된다고 말한다. 한도 전에 경고하지 않는 것은
 * 이 화면을 읽는 사람이 방금 로그인해 한도가 다시 시작됐기 때문이다(5절).
 *
 * OAuth 인가 서버가 일하거나 잠들었으면(P33, P53b 6.5절) 둘째 절 "연결된 MCP 클라이언트" 가 브라우저 로그인으로 맺은 연결을
 * 보이고 끊는다.
 */
export function TokensPage() {
  const t = useT();
  const text = t.account.tokens;
  const auth = useAuthState();
  const queryClient = useQueryClient();
  const minted = tokenAuth(auth.mode);
  const q = useQuery({ queryKey: MY_TOKENS_KEY, queryFn: listMyTokens, enabled: minted });

  if (auth.mode !== "unknown" && !minted) {
    return (
      <PageWidth data-testid="tokens-page">
        <PageHeader title={text.title} testId="page-title" />
        <Alert variant="info" data-testid="tokens-header-mode">
          {text.headerMode}
        </Alert>
      </PageWidth>
    );
  }

  const tenants = memberTenants(auth.me);
  const oidc = auth.mode === "oidc";
  return (
    <PageWidth data-testid="tokens-page" className="flex flex-col gap-4">
      <PageHeader title={text.title} testId="page-title" />
      {idpDataPlane(auth) ? (
        <Alert variant="info" data-testid="tokens-idp-issuer">
          {text.idpIssuer}
        </Alert>
      ) : null}
      {oidc ? (
        <p className="text-sm text-muted" data-testid="tokens-confirm-hint">
          {text.confirmHint(idpConfirmDays(auth.config))}
        </p>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle>{text.newToken}</CardTitle>
        </CardHeader>
        <CardContent>
          {!auth.me ? null : tenants.length ? (
            <IssueTokenForm tenants={tenants} />
          ) : (
            <Alert variant="info" data-testid="tokens-no-tenant">
              {text.noTenant}
            </Alert>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{text.issued}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {/* 토큰에 실린 역할은 낼 때의 것이다. 역할을 선택한 Tool 규칙(P52 3절)을 통과하려면 다시 만든다 */}
          <p className="text-sm text-muted" data-testid="tokens-roles-hint">
            {text.rolesHint}
          </p>
          <TokenTable
            tokens={q.data ?? []}
            loading={q.isLoading}
            error={q.error}
            onRetry={() => void q.refetch()}
            revoke={revokeMyToken}
            onRevoked={() => queryClient.invalidateQueries({ queryKey: MY_TOKENS_KEY })}
          />
        </CardContent>
      </Card>
      {oauthConnections(auth) ? (
        <Card data-testid="grants-card">
          <CardHeader>
            <CardTitle>{text.grantsTitle}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-sm text-muted">{text.grantsHelp}</p>
            <GrantList source={MY_GRANTS} />
          </CardContent>
        </Card>
      ) : null}
    </PageWidth>
  );
}
