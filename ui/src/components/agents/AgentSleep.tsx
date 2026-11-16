/**
 * 쓰지 않는 Agent 를 0 으로 줄이기의 화면 부품 (P44 6절, 8절, 10절).
 *
 * - 깨우기(`useWakeAgent`), 깨우기 단추와 휴면 줄. 개요, 세션 탭, 승인함, 홈 챗의 대화 목록·기억 서랍·고르개가 같이 쓴다.
 *   깨우기는 `POST /wake`(agent:chat)이고 파드가 뜨기를 기다리지 않는다. 답을 받으면 Agent 쿼리를 무효화해 상태 폴링이 Waking 을
 *   거쳐 Ready 를 보이게 한다. 깨우는 중(phase Waking)이거나 요청이 도는 동안 단추는 "깨우는 중…" 으로 잠긴다. agent:chat 이
 *   없으면 단추가 없다.
 * - 개요의 휴면 안내와 "지금 내려 두기"(agent:deploy, `POST /sleep`). 지금 내려 두기는 Ready 이고 깨어 있으며 잠들 수 있는
 *   Agent 에만 선다.
 * - 배포·재배포 대화상자와 배포 탭의 Scale to zero 스위치. 켜져 있으면 그래도 0 으로 줄이지 않는 까닭이나, 없으면 설치의 쉬는
 *   창(분)을 적은 풀이가 아래에 선다. 창이 0 이면 직접 내려 둘 때만 줄인다는 풀이, 첫 배포처럼 창을 모르면 분 없는 풀이다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import { sleepAgent, wakeAgent } from "@/api/resources";
import type { Agent } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { useT, type Messages } from "@/i18n";
import type { ScaleToZeroBlock } from "@/lib/agentSleep";
import { hasScope, useAuthState } from "@/lib/auth";
import { useTenant } from "@/lib/tenant";
import { cn, formatDate } from "@/lib/utils";

/** 상태를 바꾼 뒤 Agent 쿼리를 다시 읽는다. ["agents", tenant] 가 접두어라 상세·목록·홈 챗의 상태가 함께 다시 읽힌다. */
function useRefreshAgents(): () => Promise<void> {
  const tenant = useTenant();
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: ["agents", tenant] });
}

/** 깨우기. 부를 때 Agent id 를 준다. 답을 받으면 Agent 쿼리를 다시 읽는다. 단추와 홈 챗의 고르개가 같이 쓴다. */
export function useWakeAgent() {
  const refresh = useRefreshAgents();
  return useMutation({ mutationFn: (agentId: string) => wakeAgent(agentId), onSuccess: refresh });
}

/** 깨우기 단추. phase 가 Waking 이면 누를 것이 없어 잠긴다. */
export function WakeButton({ agentId, phase = null, testId = "agent-wake" }: { agentId: string; phase?: string | null; testId?: string }) {
  const t = useT();
  const tenant = useTenant();
  const { me } = useAuthState();
  const wake = useWakeAgent();
  if (!hasScope(me, tenant, "agent:chat")) return null;
  const busy = phase === "Waking" || wake.isPending;
  return (
    <span className="inline-flex flex-col items-start gap-1">
      <Button size="sm" variant="secondary" data-testid={testId} disabled={busy} onClick={() => wake.mutate(agentId)}>
        {busy ? t.agents.detail.waking : t.agents.detail.wake}
      </Button>
      <ErrorMessage error={wake.error} testId={`${testId}-error`} />
    </span>
  );
}

/**
 * 런타임을 읽지 않는 자리의 휴면 줄과 깨우기 단추. text 는 그 자리의 문장이다(목록이면 agents.sleepingList, 승인함이면
 * shared.approvalInbox.sleeping, 개요면 agents.detail.sleepingNote). phase 는 상태의 phase 이고 Waking 이면 단추가 잠긴다.
 */
export function SleepingNotice({ agentId, text, phase = null, className, testId = "agent-sleeping" }: { agentId: string; text: ReactNode; phase?: string | null; className?: string; testId?: string }) {
  return (
    <Alert variant="info" className={cn("flex flex-wrap items-center gap-3", className)} data-testid={testId}>
      <span className="min-w-0 flex-1">{text}</span>
      <WakeButton agentId={agentId} phase={phase} testId={`${testId}-wake`} />
    </Alert>
  );
}

/**
 * 개요의 쉬는 상태. 잠들었으면 언제부터인지와 세션·보류 수가 그때의 값이라는 안내, 깨우기 단추다. 깨어 있고 잠들 수 있으면
 * agent:deploy 에게 "지금 내려 두기" 가 선다. 턴이 돌면 409 agent_busy 가 그 아래에 선다.
 */
export function OverviewSleep({ agent, canDeploy }: { agent: Agent; canDeploy: boolean }) {
  const t = useT();
  const refresh = useRefreshAgents();
  // 409 in_progress 는 이미 잠들었고 남이 쥔 클레임 때문에 0 으로 줄이기를 워커가 마친다는 답이라 성공이다 (P44 7절)
  const put = useMutation({
    mutationFn: () =>
      sleepAgent(agent.id).catch((e: unknown) => {
        if (e instanceof ApiError && e.code === "in_progress") return null;
        throw e;
      }),
    onSuccess: refresh,
  });
  const st = agent.status;
  const sleep = st?.sleep;
  if (st?.phase === "Sleeping") {
    return <SleepingNotice agentId={agent.id} text={t.agents.detail.sleepingNote(formatDate(t, sleep?.since))} phase={st.phase} testId="overview-sleeping" />;
  }
  const canSleep = canDeploy && st?.phase === "Ready" && sleep?.state === "awake" && !sleep.ineligible;
  if (!canSleep && !put.error) return null;
  return (
    <div className="flex flex-col items-start gap-1">
      {canSleep ? (
        <Button size="sm" variant="secondary" data-testid="overview-sleep" disabled={put.isPending} onClick={() => put.mutate()}>
          {put.isPending ? t.agents.detail.sleeping : t.agents.detail.sleep}
        </Button>
      ) : null}
      <ErrorMessage error={put.error} testId="overview-sleep-error" />
    </div>
  );
}

/** 켜진 스위치 아래 줄. 줄이지 않는 까닭이 먼저고, 없으면 쉬는 창의 풀이다. 창이 0 이면 직접 내려 둘 때만 줄인다고, 모르면 분 없이 말한다. */
function scaleToZeroNote(text: Messages["agents"]["deployDialog"], block: ScaleToZeroBlock | null, minutes: number | null): string {
  if (block) return text.scaleToZeroOff[block];
  if (minutes === null) return text.scaleToZeroHintUnknown;
  return minutes === 0 ? text.scaleToZeroHintManual : text.scaleToZeroHint(minutes);
}

/**
 * Scale to zero 스위치. 켜져 있을 때만 아래 줄(scaleToZeroNote)이 선다. onChange 가 없으면 읽기 전용이다(배포 탭).
 */
export function ScaleToZeroField({
  checked,
  onChange,
  block,
  minutes,
  testId = "ag-scale-to-zero",
}: {
  checked: boolean;
  onChange?: (next: boolean) => void;
  block: ScaleToZeroBlock | null;
  minutes: number | null;
  testId?: string;
}) {
  const t = useT();
  const text = t.agents.deployDialog;
  const noteId = useId();
  const note = checked ? scaleToZeroNote(text, block, minutes) : null;
  return (
    <div className="flex flex-col gap-1">
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          data-testid={testId}
          checked={checked}
          disabled={!onChange}
          aria-describedby={note ? noteId : undefined}
          onChange={(e) => onChange?.(e.target.checked)}
        />
        {text.labels.scaleToZero}
      </label>
      {note ? (
        <p id={noteId} className="text-xs text-muted" data-testid={`${testId}-${block ? "off" : "hint"}`}>
          {note}
        </p>
      ) : null}
    </div>
  );
}
