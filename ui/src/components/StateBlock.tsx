/**
 * 로딩·오류·빈 상태를 구분해서 그린다.
 *
 * 조회에 실패한 화면이 "항목이 없습니다" 라고 말하는 자리가 다섯이다. 사용자는 없는 줄 알고 새로
 * 만들거나, 권한·네트워크 문제를 못 본다. 셋은 다른 상태이므로 다르게 말한다. 오류면 무엇이
 * 잘못됐는지와 "다시 시도" 를 주고, 빈 상태에서만 "없습니다" 와 만들기를 준다.
 *
 * 표 안에서는 StateRow 를 쓴다. 모양은 EmptyRow(components/ui/table.tsx)와 같다.
 */
import type { ReactNode } from "react";

import { ErrorBody } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";

export interface StateBlockProps {
  /** 아직 못 읽었는가. */
  loading?: boolean;
  /** 읽다가 실패했는가. 값이 있으면 오류 상태다. */
  error?: unknown;
  /** 비어 있을 때 보일 문장. 주면 "비어 있다" 는 뜻이고, 없으면 children 을 그린다. */
  empty?: ReactNode;
  /** 빈 상태의 문장 위에 둘 그림(components/illustrations). 안 주면 지금처럼 글만 그린다. */
  illustration?: ReactNode;
  /** 빈 상태에서 이어질 다음 행동 하나(만들기 버튼·링크). */
  emptyAction?: ReactNode;
  /** "다시 시도" 가 부를 것. 안 주면 화면을 통째로 다시 읽는다. */
  onRetry?: () => void;
  children?: ReactNode;
}

export function StateBlock({ loading, error, empty, illustration, emptyAction, onRetry, children }: StateBlockProps) {
  const t = useT();
  if (loading) {
    return (
      <p className="py-8 text-center text-sm text-muted" data-testid="state-loading">
        {t.common.loading}
      </p>
    );
  }
  if (error) {
    return (
      <Alert variant="error" className="text-left" data-testid="state-error">
        <ErrorBody error={error} />
        <div className="mt-2">
          <Button
            size="sm"
            variant="outline"
            data-testid="state-retry"
            onClick={onRetry ?? (() => window.location.reload())}
          >
            {t.detail.stateBlock.retry}
          </Button>
        </div>
      </Alert>
    );
  }
  if (empty !== undefined && empty !== null && empty !== false) {
    return (
      <div className="flex flex-col items-center gap-3 py-8 text-center text-sm text-muted" data-testid="state-empty">
        {/* 그림은 빈 자리에만 있고 로딩·오류에는 없다. 글 위에 붙는 장식이라 문장과 다음 행동의 순서는 그대로다 */}
        {illustration}
        <div>{empty}</div>
        {emptyAction ? <div>{emptyAction}</div> : null}
      </div>
    );
  }
  return <>{children}</>;
}

/** 표 본문에서 쓰는 같은 것. EmptyRow 자리에 그대로 들어간다. */
export function StateRow({ colSpan, ...props }: StateBlockProps & { colSpan: number }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-2 text-center text-sm text-muted">
        <StateBlock {...props} />
      </td>
    </tr>
  );
}
