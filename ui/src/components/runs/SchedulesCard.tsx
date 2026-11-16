/**
 * 자동 실행 탭의 일정 카드 (P41 3·11절). 반복 규칙, 시간대, 보낼 메시지, 다음 실행, 만료, 실행 이름, 상태와 멈춘 까닭을 보이고
 * agent:write 와 agent:chat 이 있으면 더하고 바꾸고 멈추고 다시 시작하고 지운다. 무엇을 바꾸든(다시 시작도) 바꾼 사람의 이름으로
 * 다시 묶인다(서버 규칙). 더하기, 편집, 다시 시작은 턴을 받는 Agent 에만 단추가 선다(RunsTab).
 */
import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { createRunSchedule, deleteRunSchedule, listRunSchedules, setRunSchedulePaused, updateRunSchedule } from "@/api/resources";
import type { RunSchedule, RunSchedulePatch } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { StateRow } from "@/components/StateBlock";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Textarea } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { agentRunsKey } from "@/lib/queryKeys";
import { actsAsText, sourceActive } from "@/lib/runs";
import { useTenant } from "@/lib/tenant";
import { browserZone, formatDate } from "@/lib/utils";

import { DeleteSourceDialog, ExpirySelect, RUN_EXPIRY_DAYS, type RunsCardProps, SourceStateCell, useDeleteSource, useRunAction } from "./shared";

/** 동작 칸을 뺀 칸 수. */
const SCHEDULE_COLUMNS = 7;

export function SchedulesCard({ agentId, canWrite, live }: RunsCardProps) {
  const t = useT();
  const text = t.runs.schedules;
  const tenant = useTenant();
  const q = useQuery({ queryKey: agentRunsKey(tenant, agentId, "schedules"), queryFn: () => listRunSchedules(agentId) });
  const [editing, setEditing] = useState<RunSchedule | "new" | null>(null);
  const pause = useRunAction(agentId, ({ id, paused }: { id: string; paused: boolean }) => setRunSchedulePaused(agentId, id, paused));
  const remove = useDeleteSource(agentId, (s: RunSchedule) => deleteRunSchedule(agentId, s.id));
  const rows = q.data ?? [];
  const columns = canWrite ? SCHEDULE_COLUMNS + 1 : SCHEDULE_COLUMNS;

  return (
    <Card data-testid="run-schedules">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {canWrite && live ? (
          <Button size="sm" className="ml-auto" data-testid="schedule-add" onClick={() => setEditing("new")}>
            {text.add}
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ErrorMessage error={pause.error} testId="schedule-action-error" />
        <Table data-testid="schedules-table">
          <THead>
            <tr>
              <TH>{text.cron}</TH>
              <TH>{text.timeZone}</TH>
              <TH>{text.message}</TH>
              <TH>{text.nextRun}</TH>
              <TH>{t.account.tokenTable.columns.expiry}</TH>
              <TH>{t.runs.history.columns.actsAs}</TH>
              <TH>{t.account.tokenTable.columns.status}</TH>
              {canWrite ? <TH /> : null}
            </tr>
          </THead>
          <TBody>
            {rows.map((s) => (
              <TR key={s.id} data-testid={`schedule-${s.id}`}>
                <TD className="font-mono text-xs whitespace-nowrap">{s.cron}</TD>
                <TD className="text-xs">{s.time_zone}</TD>
                <TD className="max-w-48 truncate text-xs" title={s.message}>
                  {s.message}
                </TD>
                <TD className="text-xs whitespace-nowrap">{sourceActive(s) ? formatDate(t, s.next_run_at) : "-"}</TD>
                <TD className="text-xs whitespace-nowrap">{formatDate(t, s.expires_at)}</TD>
                <TD className="text-xs">{actsAsText(t, s.acts_as)}</TD>
                <TD>
                  <SourceStateCell source={s} testId={`schedule-${s.id}`} />
                </TD>
                {canWrite ? (
                  <TD className="text-right whitespace-nowrap">
                    <span className="inline-flex gap-1">
                      {!s.paused || live ? (
                        <Button size="sm" variant="outline" data-testid={`schedule-${s.id}-toggle`} disabled={pause.isPending} onClick={() => pause.mutate({ id: s.id, paused: !s.paused })}>
                          {s.paused ? t.runs.resume : t.runs.pause}
                        </Button>
                      ) : null}
                      {live ? (
                        <Button size="sm" variant="outline" data-testid={`schedule-${s.id}-edit`} onClick={() => setEditing(s)}>
                          {t.runs.edit}
                        </Button>
                      ) : null}
                      <Button size="sm" variant="outline" data-testid={`schedule-${s.id}-delete`} onClick={() => remove.ask(s)}>
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
      {editing ? <ScheduleDialog agentId={agentId} schedule={editing === "new" ? null : editing} onClose={() => setEditing(null)} /> : null}
      <DeleteSourceDialog {...remove.dialog} error={remove.error} label={remove.target?.cron ?? null} />
    </Card>
  );
}

/** 일정 하나의 칸. 만료는 일 수다. */
interface ScheduleForm {
  cron: string;
  time_zone: string;
  message: string;
  expires_in_days: number;
}

function formOf(schedule: RunSchedule | null): ScheduleForm {
  if (!schedule) return { cron: "", time_zone: browserZone(), message: "", expires_in_days: RUN_EXPIRY_DAYS };
  return { cron: schedule.cron, time_zone: schedule.time_zone, message: schedule.message, expires_in_days: schedule.expires_in_days };
}

/** 바뀐 칸만. 바뀐 것이 없으면 null 이라 저장하지 않는다. */
function changedFields(before: ScheduleForm, after: ScheduleForm): RunSchedulePatch | null {
  const patch: RunSchedulePatch = {};
  if (after.cron !== before.cron) patch.cron = after.cron;
  if (after.time_zone !== before.time_zone) patch.time_zone = after.time_zone;
  if (after.message !== before.message) patch.message = after.message;
  if (after.expires_in_days !== before.expires_in_days) patch.expires_in_days = after.expires_in_days;
  return Object.keys(patch).length ? patch : null;
}

/** 일정 추가와 편집. 편집은 바뀐 칸만 보낸다. 서버가 반복 규칙과 시간대를 검사하고 거절하면 그 까닭을 대화상자 안에 보인다. */
function ScheduleDialog({ agentId, schedule, onClose }: { agentId: string; schedule: RunSchedule | null; onClose: () => void }) {
  const t = useT();
  const text = t.runs.schedules;
  const [initial] = useState(() => formOf(schedule));
  const [form, setForm] = useState(initial);
  const [unchanged, setUnchanged] = useState(false);
  const save = useRunAction(agentId, (f: ScheduleForm) => {
    if (!schedule) return createRunSchedule(agentId, f);
    const patch = changedFields(initial, f) ?? {};
    return updateRunSchedule(agentId, schedule.id, patch);
  }, onClose);
  // 값을 고치면 "바뀐 것이 없다" 안내를 거둔다
  const update = (change: Partial<ScheduleForm>) => {
    setForm({ ...form, ...change });
    setUnchanged(false);
  };
  const ready = form.cron.trim() !== "" && form.time_zone.trim() !== "" && form.message.trim() !== "";

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || save.isPending) return;
    if (schedule && !changedFields(initial, form)) {
      setUnchanged(true);
      return;
    }
    save.mutate({ ...form, cron: form.cron.trim(), time_zone: form.time_zone.trim() });
  }

  return (
    <Modal open title={schedule ? t.runs.edit : text.add} testId="schedule-dialog" onClose={onClose}>
      <form className="flex flex-col gap-3 p-5" onSubmit={submit}>
        <Field label={text.cron} htmlFor="schedule-cron" hint={text.cronHint} required>
          <Input id="schedule-cron" data-testid="schedule-cron" className="font-mono" required value={form.cron} onChange={(e) => update({ cron: e.target.value })} />
        </Field>
        <Field label={text.timeZone} htmlFor="schedule-zone" required>
          <Input id="schedule-zone" data-testid="schedule-zone" required value={form.time_zone} onChange={(e) => update({ time_zone: e.target.value })} />
        </Field>
        <Field label={text.message} htmlFor="schedule-message" required>
          <Textarea id="schedule-message" data-testid="schedule-message" rows={4} required value={form.message} onChange={(e) => update({ message: e.target.value })} />
        </Field>
        <ExpirySelect id="schedule-expiry" value={form.expires_in_days} onChange={(days) => update({ expires_in_days: days })} />
        <ErrorMessage error={save.error} testId="schedule-save-error" />
        {unchanged ? (
          <Alert variant="info" data-testid="schedule-unchanged">
            {t.errors.noChange}
          </Alert>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="schedule-save" disabled={!ready || save.isPending}>
            {t.shared.save}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
