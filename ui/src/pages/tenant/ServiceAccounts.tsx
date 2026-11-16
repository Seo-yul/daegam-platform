/**
 * Tenant Settings 의 Service accounts 카드 (P53b 6.7절). CI 처럼 사람 없이 MCP 를 부르는 프로그램의 계정과 그 토큰이다. 이
 * 테넌트의 `members:write` 를 가진 사람에게 보인다.
 *
 * 계정마다 이름, 역할, 만든 사람, 만든 날과 토큰 목록이다. 토큰은 개인 토큰과 같은 표와 발급 폼을 쓰고(만료를 고르지 않으면
 * 90일), local 은 내는 사람의 비밀번호를, oidc 는 10분 안의 IdP 로그인을 다시 확인한다. 다시 로그인하러 떠났다 돌아오면 그
 * 계정의 발급 폼이 입력을 되살린 채 열려 있다.
 *
 * `DAEGAM_SERVICE_ACCOUNTS` 가 누가 계정을 만들고 토큰을 내는지 정한다(`mode`, `can_manage`). off 면 계정을 쓸 수 없는 채로
 * 보이고, platform-admin 이면 플랫폼 관리자가 아닌 사람은 안내를 본다. 회수와 지우기는 어느 값에서나 된다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import {
  createServiceAccount,
  deleteServiceAccount,
  issueServiceAccountToken,
  listServiceAccounts,
  revokeServiceAccountToken,
} from "@/api/resources";
import { TENANT_ROLES, type ServiceAccount } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { IssueTokenForm } from "@/components/tokens/IssueTokenForm";
import { TokenTable } from "@/components/tokens/TokenTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { useConfirmAction } from "@/components/ui/useConfirmAction";
import { useT } from "@/i18n";
import { hasIssueDraft } from "@/lib/personalTokens";
import { serviceAccountsKey } from "@/lib/queryKeys";
import { formatDate } from "@/lib/utils";

/** 서버가 받는 이름의 길이 (services/service_accounts.py 의 NAME_MAX). */
const NAME_MAX = 64;
/** 고르지 않은 토큰의 만료(일). 서버의 기본값(DEFAULT_LIFETIME)과 같다. */
const DEFAULT_TOKEN_DAYS = 90;
type TenantRole = (typeof TENANT_ROLES)[number];

/** 계정마다의 발급 폼이 다시 로그인하러 떠날 때 입력을 남기는 이름. */
function tokenFormKey(account: ServiceAccount): string {
  return `sa-${account.account_id}`;
}

export function ServiceAccountsCard({ tenant }: { tenant: string }) {
  const t = useT();
  const text = t.tenant.serviceAccounts;
  const queryClient = useQueryClient();
  const key = serviceAccountsKey(tenant);
  const q = useQuery({ queryKey: key, queryFn: () => listServiceAccounts(tenant) });
  const refresh = () => queryClient.invalidateQueries({ queryKey: key });
  const remove = useConfirmAction((account: ServiceAccount) => deleteServiceAccount(tenant, account.account_id), refresh);
  const data = q.data;

  return (
    <Card data-testid="service-accounts-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm text-muted">{text.help}</p>
        {q.error ? <ErrorMessage error={q.error} testId="sa-load-error" /> : null}
        {data?.mode === "off" ? (
          <Alert variant="warn" data-testid="sa-off">
            {text.off}
          </Alert>
        ) : null}
        {data?.mode === "platform-admin" && !data.can_manage ? (
          <Alert variant="info" data-testid="sa-platform-admin-only">
            {text.platformAdminOnly}
          </Alert>
        ) : null}
        {data?.can_manage ? <NewAccountForm tenant={tenant} onCreated={refresh} /> : null}
        {data && !data.accounts.length ? (
          <p className="text-sm text-muted" data-testid="sa-empty">
            {text.empty}
          </p>
        ) : null}
        {data?.accounts.map((account) => (
          <AccountBlock key={account.account_id} tenant={tenant} account={account} canManage={data.can_manage} onDelete={() => remove.ask(account)} />
        ))}
      </CardContent>
      <ConfirmDialog
        {...remove.dialog}
        title={t.resources.delete.rowLabel(remove.target?.name ?? "")}
        description={text.deleteBody}
        confirmLabel={t.common.delete}
        pendingLabel={t.common.deleting}
        error={remove.error ? <ErrorMessage error={remove.error} testId="sa-delete-error" /> : null}
      />
    </Card>
  );
}

/** 새 계정. 이름과 역할이다. 역할은 토큰에 실리고 Tool 규칙이 그것으로 판정한다. */
function NewAccountForm({ tenant, onCreated }: { tenant: string; onCreated: () => unknown }) {
  const t = useT();
  const text = t.tenant.serviceAccounts;
  const [name, setName] = useState("");
  const [role, setRole] = useState<TenantRole>("developer");
  const trimmed = name.trim();
  const create = useMutation({
    mutationFn: () => createServiceAccount(tenant, { name: trimmed, role }),
    onSuccess: () => {
      setName("");
      void onCreated();
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (trimmed && !create.isPending) create.mutate();
  }

  return (
    <form className="flex flex-col gap-3" data-testid="sa-new" onSubmit={submit}>
      <h3 className="text-sm font-medium">{text.new}</h3>
      <div className="grid gap-3 md:grid-cols-2">
        <Field label={t.account.issue.name} htmlFor="sa-name">
          <Input id="sa-name" data-testid="sa-name" maxLength={NAME_MAX} autoComplete="off" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t.tenant.members.columns.role} htmlFor="sa-role" hint={text.roleHint}>
          <Select id="sa-role" data-testid="sa-role" value={role} onChange={(e) => setRole(e.target.value as TenantRole)}>
            {TENANT_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <ErrorMessage error={create.error} testId="sa-create-error" />
      <div>
        <Button type="submit" data-testid="sa-create" disabled={!trimmed || trimmed.length > NAME_MAX || create.isPending}>
          {t.shared.create}
        </Button>
      </div>
    </form>
  );
}

/** 계정 하나와 그 토큰. 다시 로그인하고 돌아왔으면 이 계정의 발급 폼이 열려 있다. */
function AccountBlock({
  tenant,
  account,
  canManage,
  onDelete,
}: {
  tenant: string;
  account: ServiceAccount;
  canManage: boolean;
  onDelete: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const formKey = tokenFormKey(account);
  const [issuing, setIssuing] = useState(() => hasIssueDraft(formKey));
  const key = serviceAccountsKey(tenant);
  const id = account.account_id;
  return (
    <section className="flex flex-col gap-2 border-t border-border pt-3" data-testid={`sa-${id}`}>
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-medium" data-testid={`sa-${id}-name`}>
          {account.name}
        </span>
        <Badge variant="muted" data-testid={`sa-${id}-role`}>
          {account.role}
        </Badge>
        <span className="text-xs text-muted" data-testid={`sa-${id}-creator`}>
          {t.skills.revisions.columns.author} {account.created_by_name ?? account.created_by} · {t.account.tokenTable.columns.created}{" "}
          {formatDate(t, account.created_at)}
        </span>
        <div className="ml-auto flex gap-2">
          {canManage ? (
            <Button size="sm" variant="outline" data-testid={`sa-${id}-new-token`} onClick={() => setIssuing((open) => !open)}>
              {t.account.tokens.newToken}
            </Button>
          ) : null}
          <Button size="sm" variant="outline" data-testid={`sa-${id}-delete`} onClick={onDelete}>
            {t.common.delete}
          </Button>
        </div>
      </div>
      {canManage && issuing ? (
        <IssueTokenForm
          fixedTenant={tenant}
          defaultDays={DEFAULT_TOKEN_DAYS}
          formKey={formKey}
          listKey={key}
          issue={({ label, expires_at, password }) => issueServiceAccountToken(tenant, id, { label, expires_at, password })}
        />
      ) : null}
      <TokenTable
        tokens={account.tokens}
        revoke={(tokenId) => revokeServiceAccountToken(tenant, id, tokenId)}
        onRevoked={() => queryClient.invalidateQueries({ queryKey: key })}
      />
    </section>
  );
}
