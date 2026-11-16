/**
 * Agent 상세의 자동 실행 탭 (P41 11절). 일정, 웹훅 트리거, 실행 기록 셋이다. 내린 Agent 에도 멈춘 일정과 트리거가 남으므로
 * 배포와 상관없이 열린다. 다만 묶는 일(추가, 편집, 다시 시작, 비밀 새로 만들기, 지금 실행)은 턴을 받는 Agent 에만 되고 그 밖에는
 * 서버가 409 not_deployed 라 `live` 일 때만 단추가 선다. 멈춤과 지우기는 늘 된다. 자동 실행이 꺼진 플랫폼에서는 상세가 탭을
 * 감춘다(lib/runs.ts 의 useRunsEnabled).
 */
import { RunHistoryCard } from "@/components/runs/RunHistoryCard";
import { SchedulesCard } from "@/components/runs/SchedulesCard";
import { TriggersCard } from "@/components/runs/TriggersCard";
import { useAuthState } from "@/lib/auth";
import { canWriteRuns } from "@/lib/runs";
import { useTenant } from "@/lib/tenant";

export function RunsTab({ agentId, live }: { agentId: string; live: boolean }) {
  const tenant = useTenant();
  const { me, mode } = useAuthState();
  const canWrite = canWriteRuns(me, tenant, mode);
  return (
    <div className="flex flex-col gap-4" data-testid="runs-tab">
      <RunHistoryCard agentId={agentId} canWrite={canWrite} live={live} />
      <SchedulesCard agentId={agentId} canWrite={canWrite} live={live} />
      <TriggersCard agentId={agentId} canWrite={canWrite} live={live} />
    </div>
  );
}
