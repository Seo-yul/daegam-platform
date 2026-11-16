/**
 * 한 번만 보이는 토큰 칸. 평문은 발급 응답에만 있으므로 화면을 떠나면 다시 볼 수 없다.
 *
 * Backend API 의 읽기 토큰과 개인 MCP 토큰 발급 폼, 자동 실행의 웹훅 서명 비밀(P41)이 같이 쓴다. 복사는 평문 http 에서도 되는
 * `CopyButton` 이다. `notice` 와 `copyLabel` 을 주면 토큰 안내와 복사 단추의 이름 대신 그 말을 쓴다.
 */
import type { ReactNode } from "react";

import { Alert } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { useT } from "@/i18n";

export function OneTimeToken({
  token,
  note,
  notice,
  copyLabel,
  testId = "issued-token",
}: {
  token: string;
  note?: ReactNode;
  notice?: string;
  copyLabel?: string;
  testId?: string;
}) {
  const t = useT();
  return (
    <Alert variant="warn" data-testid={testId}>
      <div className="flex items-center justify-between gap-2 text-xs">
        <span>{notice ?? t.account.oneTime.notice}</span>
        <CopyButton value={token} label={copyLabel ?? t.account.oneTime.copyLabel} testId={`${testId}-copy`} />
      </div>
      <code className="mt-1 block font-mono text-xs break-all" data-testid={`${testId}-value`}>
        {token}
      </code>
      {note ? (
        <div className="mt-1 text-xs" data-testid={`${testId}-note`}>
          {note}
        </div>
      ) : null}
    </Alert>
  );
}
