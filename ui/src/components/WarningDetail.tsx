import type { ReactElement } from "react";

import type { DescribedWarning } from "@/lib/warningText";

/** 사전 문장 아래에 붙이는 서버 원문의 이름 부분(describeWarning 의 detail). 없으면 아무것도 그리지 않는다. */
export function WarningDetail({ detail }: { detail: string | null }): ReactElement | null {
  return detail ? <code className="mt-1 block break-all text-xs" data-testid="warning-detail">{detail}</code> : null;
}

/** 경고 한 줄의 본문: 사전 문장과 그 아래의 이름. */
export function WarningBody({ d }: { d: DescribedWarning }): ReactElement {
  return (
    <>
      {d.text}
      <WarningDetail detail={d.detail} />
    </>
  );
}
