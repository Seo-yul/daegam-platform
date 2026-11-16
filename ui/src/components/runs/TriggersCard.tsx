/**
 * 자동 실행 탭의 웹훅 트리거 카드 (P41 4·11절). 이름, 호출 주소(이 화면의 공개 주소 + 트리거의 경로), 만료, 실행 이름, 상태와
 * 멈춘 까닭을 보이고 agent:write 와 agent:chat 이 있으면 더하고 바꾸고 비밀을 새로 만들고 멈추고 다시 시작하고 지운다. 서명 비밀은
 * 만들 때와 새로 만들 때의 답에만 있어 카드 위에 한 번만 보이고, 다시 읽은 목록에도 요청 캐시에도 남지 않는다. 이름만 바꾸면
 * 묶음이 그대로라 편집은 바뀐 칸만 보낸다. 더하기, 편집, 비밀 새로 만들기, 다시 시작은 턴을 받는 Agent 에만 단추가 선다(RunsTab).
 */
import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { createRunTrigger, deleteRunTrigger, listRunTriggers, rotateRunTriggerSecret, setRunTriggerPaused, updateRunTrigger } from "@/api/resources";
import type { RunTrigger, RunTriggerPatch, RunTriggerWithSecret } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { StateRow } from "@/components/StateBlock";
import { OneTimeToken } from "@/components/tokens/OneTimeToken";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Textarea } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Table, TBody, TD, TH, THead, TR, UrlTD } from "@/components/ui/table";
import { useT } from "@/i18n";
import { agentRunsKey } from "@/lib/queryKeys";
import { actsAsText } from "@/lib/runs";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";

import { DeleteSourceDialog, ExpirySelect, RUN_EXPIRY_DAYS, type RunsCardProps, SourceStateCell, useDeleteSource, useRunAction } from "./shared";

/** 동작 칸을 뺀 칸 수. */
const TRIGGER_COLUMNS = 6;

/** 다른 시스템이 부를 주소. 플랫폼 API 는 화면과 같은 공개 주소의 /api 아래에 있다. */
export function triggerUrl(trigger: Pick<RunTrigger, "path">): string {
  return `${window.location.origin}${trigger.path}`;
}

export function TriggersCard({ agentId, canWrite, live }: RunsCardProps) {
  const t = useT();
  const text = t.runs.triggers;
  const tenant = useTenant();
  const q = useQuery({ queryKey: agentRunsKey(tenant, agentId, "triggers"), queryFn: () => listRunTriggers(agentId) });
  const [editing, setEditing] = useState<RunTrigger | "new" | null>(null);
  // 방금 만들었거나 새로 만든 비밀. 화면을 떠나면 다시 볼 수 없다
  const [issued, setIssued] = useState<RunTriggerWithSecret | null>(null);
  const pause = useRunAction(agentId, ({ id, paused }: { id: string; paused: boolean }) => setRunTriggerPaused(agentId, id, paused));
  // 비밀은 화면의 상태에만 둔다. 동작의 결과로 돌려주면 요청 캐시(mutation data)에 남는다
  const rotate = useRunAction(agentId, async (id: string) => {
    setIssued(await rotateRunTriggerSecret(agentId, id));
  });
  const remove = useDeleteSource(agentId, (tr: RunTrigger) => deleteRunTrigger(agentId, tr.id));
  const rows = q.data ?? [];
  const columns = canWrite ? TRIGGER_COLUMNS + 1 : TRIGGER_COLUMNS;
  const busy = pause.isPending || rotate.isPending;

  return (
    <Card data-testid="run-triggers">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {canWrite && live ? (
          <Button size="sm" className="ml-auto" data-testid="trigger-add" onClick={() => setEditing("new")}>
            {text.add}
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {issued ? (
          <OneTimeToken
            token={issued.secret}
            notice={text.secretOnce}
            copyLabel={text.secretLabel}
            testId="trigger-secret"
            note={
              <span className="flex items-center gap-2">
                <span>
                  {text.url}: <code className="font-mono break-all">{triggerUrl(issued)}</code>
                </span>
                <Button size="sm" variant="ghost" className="ml-auto" data-testid="trigger-secret-close" onClick={() => setIssued(null)}>
                  {t.ui.modal.close}
                </Button>
              </span>
            }
          />
        ) : null}
        <ErrorMessage error={pause.error ?? rotate.error} testId="trigger-action-error" />
        <Table data-testid="triggers-table">
          <THead>
            <tr>
              <TH>{t.account.tokenTable.columns.name}</TH>
              <TH>{text.url}</TH>
              <TH>{t.account.tokenTable.columns.expiry}</TH>
              <TH>{t.runs.history.columns.actsAs}</TH>
              <TH>{t.account.tokenTable.columns.status}</TH>
              <TH>{text.template}</TH>
              {canWrite ? <TH /> : null}
            </tr>
          </THead>
          <TBody>
            {rows.map((tr) => (
              <TR key={tr.id} data-testid={`trigger-${tr.id}`}>
                <TD className="text-sm">{tr.name}</TD>
                <UrlTD url={triggerUrl(tr)} testId={`trigger-${tr.id}-url`} />
                <TD className="text-xs whitespace-nowrap">{formatDate(t, tr.expires_at)}</TD>
                <TD className="text-xs">{actsAsText(t, tr.acts_as)}</TD>
                <TD>
                  <SourceStateCell source={tr} testId={`trigger-${tr.id}`} />
                </TD>
                <TD className="max-w-48 truncate font-mono text-xs" title={tr.template}>
                  {tr.template}
                </TD>
                {canWrite ? (
                  <TD className="text-right whitespace-nowrap">
                    <span className="inline-flex gap-1">
                      {live ? (
                        <Button size="sm" variant="outline" data-testid={`trigger-${tr.id}-rotate`} disabled={busy} onClick={() => rotate.mutate(tr.id)}>
                          {text.rotate}
                        </Button>
                      ) : null}
                      {!tr.paused || live ? (
                        <Button size="sm" variant="outline" data-testid={`trigger-${tr.id}-toggle`} disabled={busy} onClick={() => pause.mutate({ id: tr.id, paused: !tr.paused })}>
                          {tr.paused ? t.runs.resume : t.runs.pause}
                        </Button>
                      ) : null}
                      {live ? (
                        <Button size="sm" variant="outline" data-testid={`trigger-${tr.id}-edit`} onClick={() => setEditing(tr)}>
                          {t.runs.edit}
                        </Button>
                      ) : null}
                      <Button size="sm" variant="outline" data-testid={`trigger-${tr.id}-delete`} onClick={() => remove.ask(tr)}>
                        {t.common.delete}
                      </Button>
                    </span>
                  </TD>
                ) : null}
              </TR>
            ))}
            {!rows.length ? <StateRow colSpan={columns} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} /> : null}
          </TBody>
        </Table>
      </CardContent>
      {editing ? (
        <TriggerDialog
          agentId={agentId}
          trigger={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onCreated={(created) => {
            setIssued(created);
            setEditing(null);
          }}
        />
      ) : null}
      <DeleteSourceDialog {...remove.dialog} error={remove.error} label={remove.target?.name ?? null} />
    </Card>
  );
}

interface TriggerForm {
  name: string;
  template: string;
  expires_in_days: number;
}

function formOf(trigger: RunTrigger | null): TriggerForm {
  if (!trigger) return { name: "", template: "{{payload}}", expires_in_days: RUN_EXPIRY_DAYS };
  return { name: trigger.name, template: trigger.template, expires_in_days: trigger.expires_in_days };
}

function changedFields(before: TriggerForm, after: TriggerForm): RunTriggerPatch | null {
  const patch: RunTriggerPatch = {};
  if (after.name !== before.name) patch.name = after.name;
  if (after.template !== before.template) patch.template = after.template;
  if (after.expires_in_days !== before.expires_in_days) patch.expires_in_days = after.expires_in_days;
  return Object.keys(patch).length ? patch : null;
}

/** 트리거 추가와 편집. 추가의 답에 든 서명 비밀은 카드가 한 번 보인다. */
function TriggerDialog({
  agentId,
  trigger,
  onClose,
  onCreated,
}: {
  agentId: string;
  trigger: RunTrigger | null;
  onClose: () => void;
  onCreated: (created: RunTriggerWithSecret) => void;
}) {
  const t = useT();
  const text = t.runs.triggers;
  const [initial] = useState(() => formOf(trigger));
  const [form, setForm] = useState(initial);
  const [unchanged, setUnchanged] = useState(false);
  const save = useRunAction(agentId, async (f: TriggerForm) => {
    if (!trigger) onCreated(await createRunTrigger(agentId, f));
    else {
      await updateRunTrigger(agentId, trigger.id, changedFields(initial, f) ?? {});
      onClose();
    }
  });
  const update = (change: Partial<TriggerForm>) => {
    setForm({ ...form, ...change });
    setUnchanged(false);
  };
  const ready = form.name.trim() !== "" && form.template.trim() !== "";

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || save.isPending) return;
    if (trigger && !changedFields(initial, form)) {
      setUnchanged(true);
      return;
    }
    save.mutate({ ...form, name: form.name.trim() });
  }

  return (
    <Modal open title={trigger ? t.runs.edit : text.add} testId="trigger-dialog" onClose={onClose}>
      <form className="flex flex-col gap-3 p-5" onSubmit={submit}>
        <Field label={t.account.tokenTable.columns.name} htmlFor="trigger-name" required>
          <Input id="trigger-name" data-testid="trigger-name" maxLength={100} required value={form.name} onChange={(e) => update({ name: e.target.value })} />
        </Field>
        <Field label={text.template} htmlFor="trigger-template" hint={text.templateHint} required>
          <Textarea id="trigger-template" data-testid="trigger-template" className="font-mono" rows={4} required value={form.template} onChange={(e) => update({ template: e.target.value })} />
        </Field>
        <ExpirySelect id="trigger-expiry" value={form.expires_in_days} onChange={(days) => update({ expires_in_days: days })} />
        <ErrorMessage error={save.error} testId="trigger-save-error" />
        {unchanged ? (
          <Alert variant="info" data-testid="trigger-unchanged">
            {t.errors.noChange}
          </Alert>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="trigger-save" disabled={!ready || save.isPending}>
            {t.shared.save}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
