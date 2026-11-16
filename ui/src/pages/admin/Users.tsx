import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { createUser, deleteUser, listUserOAuthGrants, listUsers, listUserTokens, patchUser, resetUserPassword, revokeUserOAuthGrant, revokeUserToken } from "@/api/resources";
import type { UserOut } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { StateRow } from "@/components/StateBlock";
import { GrantList, type GrantSource } from "@/components/tokens/GrantList";
import { TokenTable } from "@/components/tokens/TokenTable";
import { ActionMenu } from "@/components/ui/action-menu";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { oauthEnabled, useAuthState } from "@/lib/auth";
import { MIN_PASSWORD_LENGTH, passwordLongEnough, passwordTooShort } from "@/lib/password";
import { userOAuthGrantsKey } from "@/lib/queryKeys";
import { formatDate } from "@/lib/utils";
import { useAdminAccess } from "./permission";

const USERS_KEY = ["users"];

/**
 * /admin/users: local 인증 모드(D40)의 사용자 관리. platform-admin 만 바꾼다.
 * 테넌트 role 은 여기가 아니라 Tenant Settings 의 Members 에서 준다. 여기서는 계정 자체(만들기·막기·
 * 비밀번호 재설정·platform-admin·지우기)와 그 사람의 개인 MCP 토큰(P32)·MCP 클라이언트 연결(P33)만 다룬다.
 * 마지막 활성 platform-admin 은 서버가 지킨다(422 last_admin).
 */
export function UsersPage() {
  const t = useT();
  const text = t.admin.users;
  const auth = useAuthState();
  const { readOnly } = useAdminAccess();
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: USERS_KEY, queryFn: listUsers, enabled: auth.mode === "local" });
  const users = q.data ?? [];
  const invalidate = () => queryClient.invalidateQueries({ queryKey: USERS_KEY });
  const tokensHint = oauthEnabled(auth) ? text.tokensHint.grants : text.tokensHint.tokens;

  const [showCreate, setShowCreate] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserOut | null>(null);
  const [tokensTarget, setTokensTarget] = useState<UserOut | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UserOut | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof patchUser>[1] }) => patchUser(id, body),
    onSuccess: invalidate,
    onError: (e) => setActionError(e),
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: () => {
      setDeleteTarget(null);
      void invalidate();
    },
  });

  if (auth.mode !== "unknown" && auth.mode !== "local") {
    return (
      <PageWidth data-testid="users-page">
        <PageHeader title={text.title} testId="page-title" />
        <Alert variant="info" data-testid="users-not-local">
          {text.notLocal(auth.mode)}
        </Alert>
      </PageWidth>
    );
  }

  return (
    <PageWidth data-testid="users-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        actions={
          <Button data-testid="user-create-open" disabled={readOnly} title={readOnly ? t.admin.readOnly : undefined} onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? t.ui.modal.close : text.add}
          </Button>
        }
      />
      {readOnly ? (
        <Alert variant="info" data-testid="users-readonly">
          {t.admin.readOnly}
        </Alert>
      ) : null}
      {/* 목록을 못 읽은 것은 표 안이 말한다. 위 줄은 동작 실패와, 읽어 둔 표가 있는데 새로 읽지 못한 경우다 */}
      <ErrorMessage error={actionError ?? (users.length ? q.error : null)} testId="users-error" />
      {showCreate && !readOnly ? (
        <CreateUserCard
          onDone={() => {
            setShowCreate(false);
            void invalidate();
          }}
        />
      ) : null}
      <Card>
        <CardContent>
          <Table data-testid="users-table">
            <THead>
              <tr>
                <TH className="whitespace-nowrap">{text.username}</TH>
                <TH className="whitespace-nowrap">{text.displayName}</TH>
                <TH className="whitespace-nowrap">{text.email}</TH>
                <TH className="whitespace-nowrap">{text.status}</TH>
                <TH className="whitespace-nowrap">{text.lastLogin}</TH>
                <TH></TH>
              </tr>
            </THead>
            <TBody>
              {users.map((u) => (
                <TR key={u.id} data-testid={`user-row-${u.username}`}>
                  <TD className="font-mono text-xs">{u.username}</TD>
                  <TD>{u.display_name ?? <span className="text-dim">·</span>}</TD>
                  <TD className="text-xs">{u.email ?? <span className="text-dim">·</span>}</TD>
                  <TD className="flex flex-wrap gap-1">
                    {u.platform_admin ? <Badge variant="info">platform-admin</Badge> : null}
                    {u.disabled ? <Badge variant="warn">{text.disabled}</Badge> : null}
                    {u.must_change_password ? <Badge>{text.mustChangePassword}</Badge> : null}
                  </TD>
                  <TD className="whitespace-nowrap text-xs text-muted">{u.last_login_at ? formatDate(t, u.last_login_at) : text.never}</TD>
                  <TD className="text-right">
                    <div className="flex flex-nowrap justify-end gap-1 whitespace-nowrap">
                      <Button size="sm" variant="ghost" data-testid={`user-admin-${u.username}`} disabled={readOnly || patch.isPending} onClick={() => patch.mutate({ id: u.id, body: { platform_admin: !u.platform_admin } })}>
                        {u.platform_admin ? text.revokeAdmin : text.makeAdmin}
                      </Button>
                      <Button size="sm" variant="ghost" data-testid={`user-disable-${u.username}`} disabled={readOnly || patch.isPending} onClick={() => patch.mutate({ id: u.id, body: { disabled: !u.disabled } })}>
                        {u.disabled ? text.enable : text.disable}
                      </Button>
                      <ActionMenu
                        label={t.ui.cardGrid.menu(u.username)}
                        triggerTestId={`user-menu-${u.username}`}
                        items={[
                          { label: text.tokens, hint: tokensHint, disabled: readOnly, testId: `user-tokens-${u.username}`, onClick: () => setTokensTarget(u) },
                          { label: text.resetPassword, disabled: readOnly, testId: `user-reset-${u.username}`, onClick: () => setResetTarget(u) },
                          { label: text.delete, danger: true, disabled: readOnly, testId: `user-delete-${u.username}`, onClick: () => setDeleteTarget(u) },
                        ]}
                      />
                    </div>
                  </TD>
                </TR>
              ))}
              {!users.length ? <StateRow colSpan={6} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} /> : null}
            </TBody>
          </Table>
        </CardContent>
      </Card>
      {resetTarget ? (
        <ResetPasswordDialog
          user={resetTarget}
          onDone={() => {
            setResetTarget(null);
            void invalidate();
          }}
          onCancel={() => setResetTarget(null)}
        />
      ) : null}
      {tokensTarget ? <UserTokensDialog user={tokensTarget} onClose={() => setTokensTarget(null)} /> : null}
      <ConfirmDialog
        open={deleteTarget !== null}
        title={text.deleteTitle(deleteTarget?.username ?? "")}
        description={text.deleteBody}
        requireName={deleteTarget?.username}
        confirmLabel={text.delete}
        pending={remove.isPending}
        pendingLabel={text.deleting}
        error={remove.error ? <ErrorMessage error={remove.error} testId="user-delete-error" /> : null}
        onConfirm={() => deleteTarget && remove.mutate(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </PageWidth>
  );
}

function CreateUserCard({ onDone }: { onDone: () => void }) {
  const t = useT();
  const text = t.admin.users;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [platformAdmin, setPlatformAdmin] = useState(false);
  const create = useMutation({
    mutationFn: () =>
      createUser({
        username: username.trim().toLowerCase(),
        password,
        email: email.trim() || null,
        display_name: displayName.trim() || null,
        platform_admin: platformAdmin,
      }),
    onSuccess: onDone,
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <Card data-testid="user-create-card" className="mb-4">
      <CardHeader>
        <CardTitle>{text.add}</CardTitle>
      </CardHeader>
      <CardContent>
        <form className="grid gap-3 md:grid-cols-2" onSubmit={submit}>
          <Field label={text.username} htmlFor="uc-username" hint={text.create.usernameHint}>
            <Input id="uc-username" data-testid="uc-username" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} />
          </Field>
          <Field label={text.create.password} htmlFor="uc-password" hint={text.create.passwordHint(MIN_PASSWORD_LENGTH)} error={passwordTooShort(t, password)}>
            <Input id="uc-password" data-testid="uc-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Field label={text.displayName} htmlFor="uc-display-name">
            <Input id="uc-display-name" data-testid="uc-display-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </Field>
          <Field label={text.email} htmlFor="uc-email">
            <Input id="uc-email" data-testid="uc-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2 text-sm md:col-span-2">
            <input type="checkbox" data-testid="uc-platform-admin" checked={platformAdmin} onChange={(e) => setPlatformAdmin(e.target.checked)} />
            {text.create.platformAdmin}
          </label>
          <ErrorMessage error={create.error} testId="uc-error" />
          <div className="flex gap-2 md:col-span-2">
            <Button type="submit" data-testid="uc-submit" disabled={create.isPending || username.trim().length < 2 || !passwordLongEnough(password)}>
              {create.isPending ? text.create.creating : t.shared.create}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function ResetPasswordDialog({ user, onDone, onCancel }: { user: UserOut; onDone: () => void; onCancel: () => void }) {
  const t = useT();
  const text = t.admin.users.reset;
  const [password, setPassword] = useState("");
  const reset = useMutation({ mutationFn: () => resetUserPassword(user.id, password), onSuccess: onDone });
  const inputRef = useRef<HTMLInputElement>(null);
  const ready = passwordLongEnough(password);
  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !reset.isPending) reset.mutate();
  }
  return (
    <ConfirmDialog
      open
      title={text.title(user.username)}
      description={
        // Enter 로도 재설정한다. 확인 버튼은 대화상자의 것을 쓴다
        <form className="flex flex-col gap-2" onSubmit={submit}>
          <p>{text.body}</p>
          <Field label={t.account.password.next} htmlFor="reset-password" hint={t.account.password.rule(MIN_PASSWORD_LENGTH)} error={passwordTooShort(t, password)}>
            <Input ref={inputRef} id="reset-password" data-testid="reset-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
        </form>
      }
      confirmLabel={text.confirm}
      confirmDisabled={!ready}
      initialFocus={inputRef}
      pending={reset.isPending}
      pendingLabel={text.pending}
      error={reset.error ? <ErrorMessage error={reset.error} testId="reset-error" /> : null}
      onConfirm={() => ready && reset.mutate()}
      onCancel={onCancel}
    />
  );
}

/**
 * 한 사람의 개인 MCP 토큰 (P32 10절)과 MCP 클라이언트 연결 (P33 16절). 관리자가 하나씩 회수하거나 끊고 이유는 admin 이다.
 * 연결 목록은 OAuth 인가 서버가 켜졌을 때만 있다.
 */
function UserTokensDialog({ user, onClose }: { user: UserOut; onClose: () => void }) {
  const t = useT();
  const text = t.admin.users.tokensDialog;
  const auth = useAuthState();
  const queryClient = useQueryClient();
  const key = ["users", user.id, "tokens"];
  const q = useQuery({ queryKey: key, queryFn: () => listUserTokens(user.id) });
  const withGrants = oauthEnabled(auth);
  const grants: GrantSource = {
    queryKey: userOAuthGrantsKey(user.id),
    list: () => listUserOAuthGrants(user.id),
    revoke: (grantId) => revokeUserOAuthGrant(user.id, grantId),
  };
  return (
    <Modal open title={text.title(user.username)} testId="user-tokens-dialog" closeTestId="user-tokens-close" onClose={onClose}>
      <div className="flex flex-col gap-4 px-5 py-4">
        <section className="flex flex-col gap-2">
          {withGrants ? <h3 className="text-xs font-semibold text-heading">{text.personal}</h3> : null}
          <TokenTable
            tokens={q.data ?? []}
            loading={q.isLoading}
            error={q.error}
            onRetry={() => void q.refetch()}
            revoke={(tokenId) => revokeUserToken(user.id, tokenId)}
            onRevoked={() => queryClient.invalidateQueries({ queryKey: key })}
          />
        </section>
        {withGrants ? (
          <section className="flex flex-col gap-2" data-testid="user-grants">
            <h3 className="text-xs font-semibold text-heading">{t.account.tokens.grantsTitle}</h3>
            <GrantList source={grants} />
          </section>
        ) : null}
      </div>
    </Modal>
  );
}
