/**
 * MCP Server·Gateway·Agent 상세가 같이 쓰는 배너 셋. 셋이 글자까지 같은 배너를 각자 적고 있었고,
 * 문구를 손대는 사람이 셋 중 하나를 빠뜨리기 쉬운 자리였다.
 *
 * "언제 본 상태인가" 는 개요의 준비 상태 카드로 옮겼다 (D44 셋째).
 */
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { describeErrorLine } from "@/lib/errorText";

export interface InstanceAlertsProps {
  /** 상태 폴링이 실패하고 있는가. 마지막으로 받은 상태를 계속 보이는 중이라는 뜻이다. */
  stale: boolean;
  /** 서버가 남긴 마지막 오류. 없으면 null 이다. `코드: 설명` 꼴이다. */
  lastError: string | null | undefined;
  /** 지금 상태의 phase. 클러스터에 닿지 못하면 null 이다. */
  phase?: string | null;
  /** 상태 조회 자체가 실패한 이유. phase 가 null 인데 이것이 있으면 클러스터에 못 닿은 것이다. */
  statusError?: string | null;
}

/**
 * 상태 카드 위의 배너 셋. 폴링이 끊겼을 때, 클러스터에 닿지 못했을 때, 인스턴스에 마지막 오류가 남아
 * 있을 때다.
 */
export function InstanceAlerts({ stale, lastError, phase = undefined, statusError = null }: InstanceAlertsProps) {
  const t = useT();
  const text = t.shared.instanceStatus;
  // phase 가 null 이면 클러스터를 못 읽은 것이다. 진행도 실패도 아니라서 여기가 아니면 배너가 없다
  const unreachable = phase === null && statusError ? describeErrorLine(t, statusError) : null;
  return (
    <>
      {stale ? (
        <Alert variant="warn" data-testid="status-stale">
          {text.stale(DEPLOY_POLL_MS / 1000)}
        </Alert>
      ) : null}
      {unreachable ? (
        <Alert variant="error" data-testid="status-unreachable">
          <div>{text.unreachable}</div>
          <div className="mt-1">{unreachable.text}</div>
        </Alert>
      ) : null}
      <LastErrorAlert lastError={lastError} />
    </>
  );
}

/**
 * 서버가 남긴 마지막 오류 배너. 위 상세 셋과 허브 항목 상세가 같은 레이블로 쓴다. 없으면 아무것도 그리지 않는다.
 *
 * `last_error` 는 서버가 `코드: 설명` 으로 저장하므로 코드를 `errorText` 표에 태워 정상 경로와 같은
 * 한국어 처방을 보인다. 표에 없는 코드는 서버 원문을 그대로 두되 레이블은 한국어다 (D43 첫째).
 */
export function LastErrorAlert({ lastError, testId = "last-error" }: { lastError: string | null | undefined; testId?: string }) {
  const t = useT();
  if (!lastError) return null;
  const last = describeErrorLine(t, lastError);
  return (
    <Alert variant="error" data-testid={testId}>
      <div>{t.shared.instanceStatus.lastError(last.text)}</div>
      {last.known && last.code ? <div className="mt-1 text-right font-mono text-[10px] opacity-60">{last.code}</div> : null}
    </Alert>
  );
}
