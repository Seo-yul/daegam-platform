import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { deleteMyApprovalChoice, listMyApprovalChoices } from "@/api/resources";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { StateRow } from "@/components/StateBlock";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { MY_APPROVAL_CHOICES_KEY } from "@/lib/queryKeys";
import { formatDate } from "@/lib/utils";

const COLUMNS = 5;

/**
 * /account/approval-choices: 기억한 선택 (P43 5절). 승인 카드에서 "다음부터 묻지 않고" 고른 승인과 거부를 Agent 와 Tool 마다
 * 보이고 지운다. 지우면 다음 턴부터 그 Tool 을 다시 묻는다. 사람마다의 화면이라 모든 테넌트의 것을 한 목록으로 보이고, 지운 Agent 의
 * 선택은 이름 대신 id 를 보인다. 되돌릴 것이 없는 지우기라 확인 대화상자를 두지 않는다.
 */
export function ApprovalChoicesPage() {
  const t = useT();
  const text = t.account.approvalChoices;
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: MY_APPROVAL_CHOICES_KEY, queryFn: listMyApprovalChoices });
  const remove = useMutation({
    mutationFn: deleteMyApprovalChoice,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MY_APPROVAL_CHOICES_KEY }),
  });
  const choices = q.data ?? [];

  return (
    <PageWidth data-testid="approval-choices-page" className="flex flex-col gap-4">
      <PageHeader title={text.title} testId="page-title" />
      <Card>
        <CardContent className="flex flex-col gap-3">
          {remove.error ? <ErrorMessage error={remove.error} testId="approval-choice-delete-error" /> : null}
          <Table data-testid="approval-choices-table">
            <THead>
              <tr>
                <TH>{t.resources.kind.agent}</TH>
                <TH>{t.shared.approvalInbox.tool}</TH>
                <TH>{text.choice}</TH>
                <TH>{t.account.tokenTable.columns.expiry}</TH>
                <TH></TH>
              </tr>
            </THead>
            <TBody>
              {choices.map((c) => (
                <TR key={c.id} data-testid={`approval-choice-${c.id}`}>
                  <TD className={c.agent_name ? undefined : "font-mono text-xs"}>{c.agent_name ?? c.agent_id}</TD>
                  <TD className="font-mono text-xs">{c.tool}</TD>
                  <TD>
                    <Badge variant={c.choice === "approve" ? "ok" : "muted"}>{text[c.choice]}</Badge>
                  </TD>
                  <TD className="text-xs whitespace-nowrap">{formatDate(t, c.expires_at)}</TD>
                  <TD className="text-right">
                    <Button size="sm" variant="outline" data-testid={`approval-choice-delete-${c.id}`} disabled={remove.isPending} onClick={() => remove.mutate(c.id)}>
                      {t.common.delete}
                    </Button>
                  </TD>
                </TR>
              ))}
              {!choices.length ? <StateRow colSpan={COLUMNS} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} /> : null}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </PageWidth>
  );
}
