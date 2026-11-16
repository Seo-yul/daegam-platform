/**
 * 개인 MCP 토큰 목록 (P32 10절). 내 MCP 토큰 화면과 Users 화면의 사용자 토큰 대화상자가 같이 쓴다.
 *
 * 줄마다 이름, 테넌트, 만든 날, 만료, 상태와 회수 이유다. 회수는 확인 대화상자를 거친다. 회수하면 그 토큰의
 * 서명 키가 JWKS 에서 빠지고, 검증기가 JWKS 를 다시 받는 16분 안에 모든 곳에서 거절된다.
 */
import type { PersonalToken } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { StateRow } from "@/components/StateBlock";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useConfirmAction } from "@/components/ui/useConfirmAction";
import { useT } from "@/i18n";
import { STATUS_VARIANT, isLive } from "@/lib/personalTokens";
import { formatClock, formatDate } from "@/lib/utils";

import { StatusCell } from "./StatusCell";

const COLUMNS = 6;

export interface TokenTableProps {
  tokens: PersonalToken[];
  /** 목록을 따로 읽을 때의 상태. 다른 응답에 실려 온 목록(서비스 어카운트)은 주지 않는다. */
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  revoke: (tokenId: string) => Promise<void>;
  onRevoked: () => unknown;
}

export function TokenTable({ tokens, loading = false, error = null, onRetry, revoke, onRevoked }: TokenTableProps) {
  const t = useT();
  const text = t.account.tokenTable;
  const remove = useConfirmAction((token: PersonalToken) => revoke(token.token_id), onRevoked);

  return (
    <>
      <Table data-testid="pat-table">
        <THead>
          <tr>
            <TH>{text.columns.name}</TH>
            <TH>{text.columns.tenant}</TH>
            <TH className="whitespace-nowrap">{text.columns.created}</TH>
            <TH>{text.columns.expiry}</TH>
            <TH>{text.columns.status}</TH>
            <TH></TH>
          </tr>
        </THead>
        <TBody>
          {tokens.map((token) => (
            <TR key={token.token_id} data-testid={`pat-row-${token.token_id}`}>
              <TD>{token.label}</TD>
              <TD className="font-mono text-xs">{token.tenant}</TD>
              <TD className="text-xs whitespace-nowrap">{formatDate(t, token.created_at)}</TD>
              <TD className="text-xs whitespace-nowrap">{formatDate(t, token.expires_at)}</TD>
              <TD>
                <TokenStatus token={token} />
              </TD>
              <TD className="text-right">
                {isLive(token) ? (
                  <Button size="sm" variant="outline" data-testid={`pat-revoke-${token.token_id}`} onClick={() => remove.ask(token)}>
                    {text.revoke}
                  </Button>
                ) : null}
              </TD>
            </TR>
          ))}
          {!tokens.length ? <StateRow colSpan={COLUMNS} loading={loading} error={error} onRetry={onRetry} empty={text.empty} /> : null}
        </TBody>
      </Table>
      <ConfirmDialog
        {...remove.dialog}
        title={text.revokeTitle(remove.target?.label ?? "")}
        description={text.revokeBody}
        confirmLabel={text.revoke}
        pendingLabel={text.revoking}
        error={remove.error ? <ErrorMessage error={remove.error} testId="pat-revoke-error" /> : null}
      />
    </>
  );
}

/** 곁의 한 줄은 준비 중이면 쓸 수 있게 되는 시각, 회수됐으면 이유다. */
function TokenStatus({ token }: { token: PersonalToken }) {
  const t = useT();
  const text = t.account.tokenTable;
  const aside = token.revoke_reason ? text.reason[token.revoke_reason] : token.status === "pending" && token.ready_at ? text.readyFrom(formatClock(token.ready_at)) : null;
  return <StatusCell badge={{ label: text.status[token.status], variant: STATUS_VARIANT[token.status] }} aside={aside} testId={`pat-status-${token.token_id}`} />;
}
