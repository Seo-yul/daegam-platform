/**
 * 자동 실행 탭(P41 11절)의 일정과 트리거 카드가 같이 쓰는 조각. 상태 칸, 만료 고르개, 바꾼 뒤 다시 읽기, 지우기 확인이다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { RunSchedule, RunTrigger } from "@/api/types";
import { ErrorBody } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, Select } from "@/components/ui/input";
import { useConfirmAction } from "@/components/ui/useConfirmAction";
import { useT } from "@/i18n";
import { idpConfirmDays, useAuthState } from "@/lib/auth";
import { EXPIRY_PRESETS, expiryLabel } from "@/lib/personalTokens";
import { MY_RUNS_AS_ME_KEY, agentRunsKey } from "@/lib/queryKeys";
import { pausedReasonText, sourceActive } from "@/lib/runs";
import { useTenant } from "@/lib/tenant";

/** 서버의 만료 기본값(services/runs/service.py 의 EXPIRY_DEFAULT_DAYS)과 같다. */
export const RUN_EXPIRY_DAYS = 90;

/** 카드마다 같은 권한. `canWrite` 는 쓰기 권한과 테넌트 소속, `live` 는 Agent 가 턴을 받는지다. */
export interface RunsCardProps {
  agentId: string;
  canWrite: boolean;
  live: boolean;
}

/** 일정과 트리거를 바꾸면 그 Agent 의 자동 실행 전부와 나를 대신하는 목록을 다시 읽는다. 묶인 사람이 바뀔 수 있다. */
export function useRunsInvalidate(agentId: string): () => Promise<void> {
  const tenant = useTenant();
  const queryClient = useQueryClient();
  return async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: agentRunsKey(tenant, agentId) }),
      queryClient.invalidateQueries({ queryKey: MY_RUNS_AS_ME_KEY }),
    ]);
  };
}

/** 한 줄에 하는 동작(멈춤·다시 시작·비밀 새로 만들기). 끝나면 목록을 다시 읽고, 실패하면 카드 위에 까닭을 보인다. */
export function useRunAction<T, R>(agentId: string, action: (arg: T) => Promise<R>, onSuccess?: (result: R) => void) {
  const invalidate = useRunsInvalidate(agentId);
  return useMutation({
    mutationFn: action,
    onSuccess: async (result) => {
      onSuccess?.(result);
      await invalidate();
    },
  });
}

/** 사용 중·일시 정지됨 배지와 멈춘 까닭 한 줄. */
export function SourceStateCell({ source, testId }: { source: RunSchedule | RunTrigger; testId: string }) {
  const t = useT();
  const active = sourceActive(source);
  const auth = useAuthState();
  const reason = pausedReasonText(t, source, idpConfirmDays(auth.config));
  return (
    <div className="flex flex-col gap-0.5">
      <Badge variant={active ? "ok" : "muted"} data-testid={`${testId}-state`}>
        {active ? t.runs.state.active : t.runs.state.paused}
      </Badge>
      {reason ? (
        <span className="max-w-64 text-xs text-muted" data-testid={`${testId}-reason`}>
          {reason}
        </span>
      ) : null}
    </div>
  );
}

/** 만료 길이(일). 개인 토큰과 같은 고르개이고 서버가 받는 1~365일 안이다. */
export function ExpirySelect({ id, value, onChange }: { id: string; value: number; onChange: (days: number) => void }) {
  const t = useT();
  return (
    <Field label={t.account.issue.expiry} htmlFor={id}>
      <Select id={id} data-testid={id} value={String(value)} onChange={(e) => onChange(Number(e.target.value))}>
        {EXPIRY_PRESETS.map((days) => (
          <option key={days} value={days}>
            {expiryLabel(t, days)}
          </option>
        ))}
      </Select>
    </Field>
  );
}

/** 지우기를 한 번 더 묻는다. 지운 일정과 트리거는 되살릴 수 없고 그 열린 실행이 멈춘다. */
export function useDeleteSource<T>(agentId: string, remove: (item: T) => Promise<void>) {
  const invalidate = useRunsInvalidate(agentId);
  return useConfirmAction(remove, invalidate);
}

interface DeleteSourceDialogProps {
  open: boolean;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  error: unknown;
  /** 무엇을 지우는지. 일정은 반복 규칙, 트리거는 이름이다. */
  label: string | null;
}

export function DeleteSourceDialog({ error, label, ...dialog }: DeleteSourceDialogProps) {
  const t = useT();
  return (
    <ConfirmDialog
      {...dialog}
      title={t.common.delete}
      description={
        <span className="flex flex-col gap-1">
          <span>{t.runs.deleteBody}</span>
          {label ? <code className="font-mono text-xs break-all">{label}</code> : null}
        </span>
      }
      confirmLabel={t.common.delete}
      error={error ? <ErrorBody error={error} /> : undefined}
    />
  );
}
