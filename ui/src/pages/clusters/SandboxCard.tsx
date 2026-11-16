/**
 * 코드 실행 격리 카드 (P40 8절). 클러스터 상세에서 플랫폼 관리자가 격리 점검을 돌리고, 그 결과와 지금 센 용량, 메모리 한도 없이 센
 * 파드, 샌드박스가 다른 워크로드와 노드를 함께 쓰는지와 그 허용을 본다. 게이트가 닫혔으면 그 까닭을, 개발용 우회가 들면 그 경고를
 * 보인다.
 *
 * 판정과 마지막 실행은 Cluster 문서(`sandbox.check`·`sandbox.run`, 읽을 때 정한 `void_reason`·`override`)이고, 게이트와 용량은
 * `GET /clusters/{id}/sandbox` 가 물을 때 센다. 점검은 뒤에서 돌므로 도는 동안 두 쪽을 몇 초마다 다시 읽는다.
 *
 * 허용은 판정이 통과했고 무효로 읽히지 않으며 노드를 함께 쓸 때만 고를 수 있다. 화면이 본 판정의 `checked_at` 을 함께 보내고, 그사이
 * 새 점검이나 무효가 지나갔으면 409 sandbox_check_stale 이다.
 *
 * data-testid: 카드 `cluster-sandbox-card`, 점검 단추 `sandbox-check-button`, 결과 `sandbox-result`, 용량 `sandbox-capacity`,
 * 메모리 한도 없이 센 파드 `sandbox-assumed`, 노드를 함께 씀 `sandbox-shared-nodes`, 허용 `sandbox-accept`·`sandbox-accepted`,
 * 게이트의 까닭 `sandbox-gate-reason`, 우회 `sandbox-override`.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { acceptClusterSandbox, checkClusterSandbox, getClusterSandbox } from "@/api/resources";
import type { Cluster } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Check } from "@/components/canvas/inspector/fields";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT, type Messages } from "@/i18n";
import { gateReason } from "@/lib/codeExecution";
import { formatDate } from "@/lib/utils";

/** 점검이 도는 동안 다시 읽는 간격(밀리초). */
const CHECK_POLL_MS = 3000;

export function sandboxStatusKey(clusterId: string): readonly unknown[] {
  return ["clusters", clusterId, "sandbox"];
}

type Result = { variant: "ok" | "warn" | "error"; text: string } | null;

/** 판정과 마지막 실행의 한 줄. 점검하지 못했으면 그 까닭이 먼저이고, 그다음 읽을 때 정한 무효, 써 둔 무효, 통과, 실패 차례다. */
export function sandboxResult(t: Messages, sandbox: Cluster["sandbox"]): Result {
  const text = t.clusters.sandbox;
  const { check, run } = sandbox;
  if (run.error) return { variant: "warn", text: text.couldNotRun(run.error) };
  if (check.void_reason) return { variant: "error", text: text.failedSelfCheck };
  if (check.passed && sandbox.void_reason) return { variant: "warn", text: text.voided };
  if (check.passed && check.checked_at) return { variant: "ok", text: text.passed(formatDate(t, check.checked_at)) };
  if (!check.checked_at) return null;
  if (check.userns === false) return { variant: "error", text: text.failedUserns };
  if (check.network === false) return { variant: "error", text: text.failedNetwork };
  return null;
}

export function SandboxCard({ cluster, readOnly }: { cluster: Cluster; readOnly: boolean }) {
  const t = useT();
  const text = t.clusters.sandbox;
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: sandboxStatusKey(cluster.id),
    queryFn: () => getClusterSandbox(cluster.id),
    refetchInterval: (q) => (q.state.data?.check_running ? CHECK_POLL_MS : false),
  });
  const running = status.data?.check_running ?? false;

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["clusters", cluster.id] });
  }

  // 점검이 끝나면 판정과 마지막 실행이 든 Cluster 문서를 다시 읽는다
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) void queryClient.invalidateQueries({ queryKey: ["clusters", cluster.id] });
    wasRunning.current = running;
  }, [running, queryClient, cluster.id]);

  const check = useMutation({ mutationFn: () => checkClusterSandbox(cluster.id), onSuccess: refresh });
  const accept = useMutation({
    mutationFn: () => acceptClusterSandbox(cluster.id, cluster.sandbox.check.checked_at as string),
    onSuccess: refresh,
  });

  const result = sandboxResult(t, cluster.sandbox);
  const verdict = cluster.sandbox.check;
  const shared = status.data?.shared_nodes ?? verdict.shared_nodes;
  const reason = status.data && !status.data.gate.open ? gateReason(status.data.gate.reason) : null;
  const acceptable = verdict.passed && !cluster.sandbox.void_reason && !verdict.accepted_at && Boolean(verdict.checked_at);

  return (
    <Card data-testid="cluster-sandbox-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <Button
          variant="secondary"
          size="sm"
          data-testid="sandbox-check-button"
          disabled={readOnly || running || check.isPending}
          title={readOnly ? t.clusters.readOnly.title : undefined}
          onClick={() => check.mutate()}
        >
          {text.check}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <ErrorMessage error={check.error ?? accept.error} testId="sandbox-error" />
        {result ? (
          result.variant === "ok" ? (
            <Badge variant="ok" className="self-start" data-testid="sandbox-result">
              {result.text}
            </Badge>
          ) : (
            <Alert variant={result.variant} data-testid="sandbox-result">
              {result.text}
            </Alert>
          )
        ) : null}
        {status.error ? <ErrorMessage error={status.error} testId="sandbox-status-error" /> : null}
        {status.data ? (
          <>
            <p data-testid="sandbox-capacity">{text.capacity(status.data.capacity)}</p>
            {status.data.assumed ? (
              <div data-testid="sandbox-assumed" className="text-xs text-muted">
                <p>{text.assumedLimits(status.data.assumed)}</p>
                {status.data.assumed_pods.length ? <p className="mt-1 font-mono">{status.data.assumed_pods.join(", ")}</p> : null}
              </div>
            ) : null}
          </>
        ) : null}
        {shared ? (
          <Alert variant="warn" data-testid="sandbox-shared-nodes">
            {text.sharedNodes}
          </Alert>
        ) : null}
        {shared && verdict.accepted_at ? (
          <p className="text-xs text-muted" data-testid="sandbox-accepted">
            {text.accepted(verdict.accepted_by ?? "-", formatDate(t, verdict.accepted_at))}
          </p>
        ) : null}
        {shared && acceptable && !readOnly ? (
          <Check id="sandbox-accept" label={text.acceptSharedNodes} checked={false} disabled={accept.isPending} onChange={(v) => v && accept.mutate()} />
        ) : null}
        {reason ? (
          <Alert variant="info" data-testid="sandbox-gate-reason">
            {text.reason[reason]}
          </Alert>
        ) : null}
        {cluster.sandbox.override ? (
          <Alert variant="warn" data-testid="sandbox-override">
            {t.warnings.codes.sandbox_override.text}
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  );
}
