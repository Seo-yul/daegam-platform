import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { listGroupMappings, putGroupMappings } from "@/api/resources";
import { ALL_ROLES, type GroupMapping } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { StateRow } from "@/components/StateBlock";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { useAdminAccess } from "./permission";

/** 저장 전에 걸린 표의 문제. 상태에는 문장 대신 무엇이 걸렸는지만 둔다. */
type FormError = { code: "required" } | { code: "adminTenant"; group: string };

/** Okta 그룹 → (테넌트, role). PUT 은 전체 교체다. platform-admin 은 tenant "*" 와만 짝이 된다. */
export function GroupMappingsPage() {
  const t = useT();
  const text = t.admin.groupMappings;
  const queryClient = useQueryClient();
  // 이 화면은 GET 부터 platform-admin 전용이다(platform/daegam/api/members.py). 못 읽었을 때 표가
  // "No group mappings." 라고 적으면 권한이 없는 것인지 매핑이 없는 것인지 구분되지 않는다
  const { readOnly } = useAdminAccess();
  const q = useQuery({ queryKey: ["group-mappings"], queryFn: listGroupMappings });
  const [rows, setRows] = useState<GroupMapping[]>([]);
  const [pending, setPending] = useState<GroupMapping[] | null>(null);
  const [formError, setFormError] = useState<FormError | null>(null);
  useEffect(() => {
    if (q.data) setRows(q.data.map((m) => ({ ...m })));
  }, [q.data]);

  const save = useMutation({
    mutationFn: (items: GroupMapping[]) => putGroupMappings(items),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["group-mappings"] }),
  });

  function update(i: number, patch: Partial<GroupMapping>) {
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  function submit() {
    const items = rows.map((r) => ({ group: r.group.trim(), tenant: r.tenant.trim(), role: r.role }));
    for (const r of items) {
      if (!r.group || !r.tenant) {
        setFormError({ code: "required" });
        return;
      }
      if ((r.role === "platform-admin") !== (r.tenant === "*")) {
        setFormError({ code: "adminTenant", group: r.group });
        return;
      }
    }
    setFormError(null);
    // PUT 은 전체 교체라 표에서 지운 매핑으로 들어오던 사람은 다음 로그인 때 소속을 잃는다.
    // 특히 platform-admin 매핑이 하나도 안 남으면 이 화면에 다시 못 들어온다. 저장 전에 한 번 묻는다.
    setPending(items);
  }

  function commit() {
    const items = pending;
    setPending(null);
    if (items) save.mutate(items);
  }

  const dirty = JSON.stringify(rows) !== JSON.stringify(q.data ?? []);
  const losesAdmin = pending !== null && !pending.some((r) => r.role === "platform-admin");
  // 못 읽었으면 지금 표는 실제 매핑이 아니다. 그 상태로 전체 교체를 보내면 남의 매핑을 지운다
  const locked = readOnly || Boolean(q.error);

  return (
    <PageWidth data-testid="group-mappings-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        actions={
          <>
            <Button variant="outline" data-testid="gm-add" disabled={locked} title={locked ? t.admin.readOnly : undefined} onClick={() => setRows([...rows, { group: "", tenant: "", role: "developer" }])}>
              {text.add}
            </Button>
            <Button data-testid="save-button" disabled={locked || !dirty || save.isPending} title={locked ? t.admin.readOnly : undefined} onClick={submit}>
              {save.isPending ? t.shared.saving : text.save}
            </Button>
          </>
        }
      />
      {readOnly ? (
        <Alert variant="info" className="mb-3" data-testid="readonly-notice">
          {t.admin.readOnly}
        </Alert>
      ) : null}
      {/* 조회 실패는 표 안에서 StateRow 가 말한다. 표 바깥에 또 적으면 빈 표와 붉은 줄이 서로를 부정한다 */}
      {formError ? (
        <Alert variant="error" className="mb-3" data-testid="gm-form-error">
          {formError.code === "adminTenant" ? text.formError.adminTenant(formError.group) : text.formError.required}
        </Alert>
      ) : null}
      <ErrorMessage error={save.error} testId="save-error" />
      {save.isSuccess && !dirty ? (
        <Alert variant="ok" className="mb-3" data-testid="gm-saved">
          {t.shared.saved}
        </Alert>
      ) : null}
      <Card>
        <CardContent className="px-0 py-0">
          <Table data-testid="gm-table">
            <THead>
              <tr>
                <TH>{text.columns.group}</TH>
                <TH>{text.columns.tenant}</TH>
                <TH>{text.columns.role}</TH>
                <TH></TH>
              </tr>
            </THead>
            <TBody>
              {rows.length === 0 ? (
                <StateRow
                  colSpan={4}
                  loading={q.isLoading}
                  error={q.error}
                  onRetry={() => void q.refetch()}
                  empty={text.empty}
                />
              ) : null}
              {rows.map((r, i) => (
                <TR key={i} data-testid={`gm-row-${i}`}>
                  <TD>
                    <Input aria-label={text.columns.group} data-testid={`gm-group-${i}`} className="h-8" value={r.group} onChange={(e) => update(i, { group: e.target.value })} />
                  </TD>
                  <TD>
                    <Input aria-label={text.columns.tenant} data-testid={`gm-tenant-${i}`} className="h-8" placeholder={text.tenantPlaceholder} value={r.tenant} onChange={(e) => update(i, { tenant: e.target.value })} />
                  </TD>
                  <TD>
                    <Select aria-label={text.columns.role} data-testid={`gm-role-${i}`} className="h-8" value={r.role} onChange={(e) => update(i, { role: e.target.value })}>
                      {ALL_ROLES.map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </Select>
                  </TD>
                  <TD className="text-right">
                    <Button size="sm" variant="ghost" data-testid={`gm-remove-${i}`} onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                      {t.shared.remove}
                    </Button>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
      <ConfirmDialog
        open={pending !== null}
        title={text.confirmTitle}
        description={
          <>
            {text.confirmBody(pending?.length ?? 0)}
            {losesAdmin ? (
              <strong className="mt-2 block text-err-fg" data-testid="gm-loses-admin">
                {text.losesAdmin}
              </strong>
            ) : null}
          </>
        }
        confirmLabel={text.confirm}
        pending={save.isPending}
        pendingLabel={t.shared.saving}
        onConfirm={commit}
        onCancel={() => setPending(null)}
      />
    </PageWidth>
  );
}
