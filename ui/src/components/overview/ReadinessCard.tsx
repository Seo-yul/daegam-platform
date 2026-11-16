/**
 * "준비 상태" 카드 (D44 셋째). 지금 떠 있나, 얼마나 떠 있었나, 도는 것이 최신 정의인가 셋을 한 카드에
 * 답한다.
 *
 * "가동 시간" 은 Ready 조건의 `last_transition` 에서 나온다. `status.since` 는 배포 쪽을 마지막으로
 * 쓴 시각이지 Ready 가 된 시각이 아니라서 쓸 수 없고, 그래서 conditions 매핑에 전이 시각을 살렸다
 * (D44 의 API 예외 하나). 오퍼레이터가 안 채우면 그 줄을 통째로 뺀다.
 *
 * 리비전이 어긋났을 때 무엇을 하라는 말은 여기가 아니라 페이지 위의 `revision-behind` 배너가 한다
 * (D43 둘째). 이 카드는 사실만 적는다.
 */
import type { ReactNode } from "react";

import type { InstanceCondition } from "@/api/types";
import { PhaseBadge } from "@/components/PhaseBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { elapsedSeconds, formatDate, formatDuration } from "@/lib/utils";
import { phaseText } from "@/lib/warningText";

export interface ReadinessCardProps {
  phase: string | null | undefined;
  /** 준비된 복제본 수. null 이면 그 줄을 뺀다 (Gateway 는 vMCP 가 이 숫자를 내지 않는다). */
  ready?: number | null;
  /** 원하는 복제본 수. */
  wanted?: number | null;
  /** 배포된 조건들. Ready 의 전이 시각만 쓴다. */
  conditions?: InstanceCondition[];
  /** 상태를 마지막으로 읽은 시각. */
  observedAt: string | null | undefined;
  /** 돌고 있는 리비전. 안 올렸으면 null 이다. */
  deployedRevision: number | null | undefined;
  /** 저장된 최신 리비전. */
  revision: number;
  /** 어디에서 돌고 있나. 클러스터 링크와 네임스페이스/이름이다. */
  location?: ReactNode;
  message?: string | null;
  error?: string | null;
  /** 카드 아래에 더 붙일 것. Agent 의 세션·승인 타일이 여기 온다. */
  children?: ReactNode;
  testId?: string;
  readyTestId?: string;
  /** "지금" 을 넘겨 시각 계산을 고정한다. 테스트만 쓴다. */
  now?: number;
}

/** Ready 조건이 지금 값으로 바뀐 시각. 조건이 없거나 서버가 안 채웠으면 null 이다. */
export function readySince(conditions: InstanceCondition[] | null | undefined): string | null {
  const ready = (conditions ?? []).find((c) => c.type === "Ready" && c.status === "True");
  return ready?.last_transition ?? null;
}

function Row({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd data-testid={testId}>{children}</dd>
    </>
  );
}

export function ReadinessCard({
  phase,
  ready = null,
  wanted = null,
  conditions,
  observedAt,
  deployedRevision,
  revision,
  location,
  message,
  error,
  children,
  testId = "readiness-card",
  readyTestId = "status-ready-replicas",
  now,
}: ReadinessCardProps) {
  const t = useT();
  const text = t.detail.readinessCard;
  const since = readySince(conditions);
  const up = since === null ? null : elapsedSeconds(since, now ?? Date.now());
  const behind = deployedRevision !== null && deployedRevision !== undefined && deployedRevision !== revision;
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <div className="flex items-center gap-2 text-xs text-muted">
          <PhaseBadge phase={phase} testId="status-phase" live />
          {phaseText(t, phase) ? <span data-testid="status-phase-text">{phaseText(t, phase)}</span> : null}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="grid grid-cols-[7.5rem_1fr] gap-y-1.5 text-sm">
          {ready !== null && ready !== undefined ? (
            <Row label={text.readyPods} testId={readyTestId}>
              {ready}
              {wanted !== null && wanted !== undefined ? <span className="text-muted"> / {wanted}</span> : null}
            </Row>
          ) : null}
          {up !== null ? (
            <Row label={text.uptime} testId="readiness-uptime">
              {formatDuration(t, up)}
              <span className="ml-2 text-xs text-muted">{text.since(formatDate(t, since))}</span>
            </Row>
          ) : null}
          <Row label={text.observed} testId="readiness-observed">
            {formatDate(t, observedAt)}
            <span className="ml-2 text-xs text-muted">{text.pollEvery(DEPLOY_POLL_MS / 1000)}</span>
          </Row>
          {location ? (
            <Row label={text.cluster} testId="readiness-location">
              {location}
            </Row>
          ) : null}
          {/* 값은 돌고 있는 리비전이고, 저장된 정의와의 관계는 그 옆에 말로 적는다. "배포 r2 = 정의 r2"
              는 사람 말이 아니라 식이고, 같을 때 같은 숫자를 두 번 읽히는 값도 없다 */}
          <Row label={text.revision} testId="status-revision">
            {deployedRevision === null || deployedRevision === undefined ? (
              <>
                -<span className="ml-2 text-xs text-muted">{text.notDeployed}</span>
              </>
            ) : behind ? (
              <>
                r{deployedRevision}
                <span className="ml-2 text-xs text-warn-fg">{text.savedRevision(revision)}</span>
              </>
            ) : (
              // 같으면 아무 말도 하지 않는다. 덧붙일 것이 없다는 사실이 곧 그 뜻이다
              <>r{deployedRevision}</>
            )}
          </Row>
          {message ? (
            <Row label={text.message} testId="status-message">
              {message}
            </Row>
          ) : null}
          {error ? (
            <>
              <dt className="text-muted">{text.error}</dt>
              <dd className="text-err-fg" data-testid="status-error">
                {error}
              </dd>
            </>
          ) : null}
        </dl>
        {children}
      </CardContent>
    </Card>
  );
}
