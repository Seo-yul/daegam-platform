/**
 * 상세 머리의 배포 상태 배너 (D43). Agent·MCP Server·Gateway 상세가 같이 쓴다.
 *
 * 공용 배너 셋(`InstanceAlerts`) 아래에 진행 중·실패·정의가 앞선 상태를 차례로 세운다. 세 상세가 같은
 * 배너를 글자까지 같게 각자 적고 있었고, 다른 것은 재배포를 여는 방법과 낱말 둘뿐이었다.
 */
import { isBehind } from "@/components/BehindBadge";
import { InstanceAlerts } from "@/components/InstanceStatus";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { elapsedSeconds } from "@/lib/utils";
import { failureAdvice, isFailed, isProgressing, progressBanner } from "@/lib/warningText";

export interface DeploymentAlertsProps {
  /** 상태 폴링이 실패하고 있는가. 마지막으로 받은 상태를 계속 보이는 중이라는 뜻이다. */
  stale: boolean;
  lastError: string | null | undefined;
  /** sleep 은 Agent 만 갖는다. 깨우는 중(Waking)의 흐른 시간은 배포 쪽 since 가 아니라 깨우기 시작한 때부터 잰다 (P44 10절). */
  status: { phase?: string | null; since?: string | null; error?: string | null; sleep?: { since?: string | null } | null } | null | undefined;
  item: { revision: number; deployed_revision: number | null };
  live: boolean;
  canDeploy: boolean;
  busy: boolean;
  onRedeploy: () => void;
  onOpenAdvanced: () => void;
  /** 실패 배너에서 Advanced 탭으로 가는 링크의 문구. */
  manifestLabel: string;
  /** 뒤처짐 배너에서 바뀐 것. Agent 는 그래프이고 나머지는 정의다. 부르는 말은 사전의 detail.revisionSubject 다. */
  subject: keyof Messages["detail"]["revisionSubject"];
}

export function DeploymentAlerts({ stale, lastError, status, item, live, canDeploy, busy, onRedeploy, onOpenAdvanced, manifestLabel, subject }: DeploymentAlertsProps) {
  const t = useT();
  const phase = status?.phase;
  const since = phase === "Waking" ? status?.sleep?.since : status?.since;
  const progress = progressBanner(t, phase, DEPLOY_POLL_MS / 1000, elapsedSeconds(since));
  return (
    <>
      <InstanceAlerts stale={stale} lastError={lastError} phase={phase} statusError={status?.error} />
      {isProgressing(phase) ? (
        <Alert variant={progress?.variant ?? "info"} data-testid="status-progress">
          {progress?.text}
        </Alert>
      ) : null}
      {isFailed(phase) ? (
        <Alert variant="error" data-testid="status-failed">
          <div>{failureAdvice(t, phase)}</div>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <Button size="sm" variant="secondary" data-testid="failed-redeploy" disabled={busy} onClick={onRedeploy}>
              {t.shared.detailActions.redeploy}
            </Button>
            <button type="button" className="text-xs underline underline-offset-2" data-testid="failed-manifest" onClick={onOpenAdvanced}>
              {manifestLabel}
            </button>
          </div>
        </Alert>
      ) : null}
      {/* 정의만 고쳐 놓고 배포를 안 한 상태. 리비전 두 개를 그냥 보여 주면 어느 쪽이 도는지 알기 어렵다 */}
      {isBehind(item) ? (
        <Alert variant="warn" data-testid="revision-behind">
          {/* 할 일이 한 줄 아래 따로 서면 안내와 동작이 따로 노는 덩어리 둘로 읽힌다. 한 줄에 둔다 */}
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <span>{t.detail.deploymentAlerts.behind(item.revision, item.deployed_revision, t.detail.revisionSubject[subject])}</span>
            {live && canDeploy ? (
              <Button size="sm" variant="secondary" data-testid="behind-redeploy" disabled={busy} onClick={onRedeploy}>
                {t.shared.detailActions.redeploy}
              </Button>
            ) : null}
          </div>
        </Alert>
      ) : null}
    </>
  );
}
