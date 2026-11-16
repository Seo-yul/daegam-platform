/**
 * 연결된 MCP 클라이언트 (P33 12절·16절). 내 MCP 토큰 화면의 둘째 절과 Users 화면의 사용자 토큰 대화상자가 같이 쓴다.
 *
 * 연결은 MCP 클라이언트가 브라우저 로그인과 허용으로 받은 것이다. 줄마다 클라이언트 이름과 그 이름이 어디서 왔는지
 * (`clientOrigin`), 테넌트, 대상, 연결한 날, 마지막 갱신, 상태와 끊긴 이유다. 끊기는 확인
 * 대화상자를 거친다. 끊으면 그 연결의 갱신이 그 자리에서 막히고, 이미 나간 액세스 토큰은 16분 안에 거절된다.
 *
 * 두 화면이 서로 다른 API 로 읽으므로 쿼리 키와 읽기·끊기 함수를 `GrantSource` 하나로 받고 목록을 스스로 읽는다.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { OAuthGrant, OAuthGrantStatus } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { StateRow } from "@/components/StateBlock";
import type { BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useConfirmAction } from "@/components/ui/useConfirmAction";
import { useT } from "@/i18n";
import { clientOrigin } from "@/lib/oauthClients";
import { clientName } from "@/lib/serverLiterals";
import { formatDate } from "@/lib/utils";

import { StatusCell } from "./StatusCell";

const COLUMNS = 7;

export interface GrantSource {
  queryKey: readonly unknown[];
  list: () => Promise<OAuthGrant[]>;
  revoke: (grantId: string) => Promise<void>;
}

/** 상태 배지 색. 끝나거나 끊긴 연결은 서버가 30일 동안 남긴다(P33 11절). 라벨과 끊긴 이유는 사전의 `account.grants` 다. */
const GRANT_STATUS_VARIANT: Record<OAuthGrantStatus, BadgeProps["variant"]> = {
  active: "ok",
  expired: "muted",
  revoked: "muted",
};

const RESOURCE_KIND_LABEL: Record<OAuthGrant["resource_kind"], string> = { gateway: "Gateway", mcp_server: "MCP Server" };

export function GrantList({ source }: { source: GrantSource }) {
  const t = useT();
  const text = t.account.grants;
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: source.queryKey, queryFn: source.list });
  const disconnect = useConfirmAction(
    (g: OAuthGrant) => source.revoke(g.grant_id),
    () => queryClient.invalidateQueries({ queryKey: source.queryKey }),
  );
  const grants = q.data ?? [];

  return (
    <>
      <Table data-testid="grant-table">
        <THead>
          <tr>
            <TH>{text.columns.client}</TH>
            <TH>{text.columns.tenant}</TH>
            <TH>{text.columns.target}</TH>
            <TH className="whitespace-nowrap">{text.columns.connected}</TH>
            <TH className="whitespace-nowrap">{text.columns.refreshed}</TH>
            <TH>{text.columns.status}</TH>
            <TH></TH>
          </tr>
        </THead>
        <TBody>
          {grants.map((g) => (
            <TR key={g.grant_id} data-testid={`grant-row-${g.grant_id}`}>
              <TD>
                <ClientName grant={g} />
              </TD>
              <TD className="font-mono text-xs">{g.tenant}</TD>
              <TD>
                <div className="flex flex-col">
                  <span className="text-xs text-muted">{RESOURCE_KIND_LABEL[g.resource_kind]}</span>
                  <span className="break-all font-mono text-xs">{g.resource}</span>
                </div>
              </TD>
              <TD className="text-xs whitespace-nowrap">{formatDate(t, g.created_at)}</TD>
              <TD className="text-xs whitespace-nowrap">{formatDate(t, g.last_refresh_at)}</TD>
              <TD>
                <StatusCell
                  badge={{ label: text.status[g.status], variant: GRANT_STATUS_VARIANT[g.status] }}
                  aside={g.revoked_reason ? text.reason[g.revoked_reason] : null}
                  testId={`grant-status-${g.grant_id}`}
                />
              </TD>
              <TD className="text-right">
                {g.status === "active" ? (
                  <Button size="sm" variant="outline" data-testid={`grant-revoke-${g.grant_id}`} onClick={() => disconnect.ask(g)}>
                    {text.disconnect}
                  </Button>
                ) : null}
              </TD>
            </TR>
          ))}
          {!grants.length ? <StateRow colSpan={COLUMNS} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} /> : null}
        </TBody>
      </Table>
      <ConfirmDialog
        {...disconnect.dialog}
        title={text.disconnectTitle(disconnect.target ? clientName(t, disconnect.target.client_name) : "")}
        description={text.disconnectBody}
        confirmLabel={text.disconnect}
        pendingLabel={text.disconnecting}
        error={disconnect.error ? <ErrorMessage error={disconnect.error} testId="grant-revoke-error" /> : null}
      />
    </>
  );
}

/** 이름은 클라이언트가 밝힌 것이라 신원이 아니다. 그 이름이 어디서 왔는지 곁에 적는다. */
function ClientName({ grant }: { grant: OAuthGrant }) {
  const t = useT();
  return (
    <div className="flex flex-col" data-testid={`grant-client-${grant.grant_id}`}>
      <span>{clientName(t, grant.client_name)}</span>
      <span className="text-xs text-muted">{clientOrigin(t, grant.client_kind, grant.client_host)}</span>
    </div>
  );
}
