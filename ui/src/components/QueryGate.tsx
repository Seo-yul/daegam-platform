/**
 * 상세 화면의 진입부. "아직 못 읽었으면 로딩, 읽지 못했으면 오류" 를 열 곳이 각자 두 줄씩 적고 있었다.
 * 문구와 규칙을 여기 한 곳에 둔다. 그릴 수 있으면 null 이므로 호출부는 아래 꼴이다.
 *
 *   const gate = queryGate(q);
 *   if (gate) return gate;
 */
import type { ReactElement } from "react";

import { ErrorMessage } from "@/components/ErrorMessage";
import { useT } from "@/i18n";

export interface QueryGateOptions {
  /** 로딩 문구. 기본은 사전의 `common.loading` 이다. */
  label?: string;
  /** 오류 화면의 data-testid. */
  testId?: string;
  /**
   * 앞서 받은 데이터가 있으면 오류를 무시하고 그것을 계속 보일 것인가. 기본은 그렇게 한다. 상태 폴링이
   * 잠깐 끊겨도 화면이 통째로 사라지지 않게 하려는 것이고, 끊겼다는 사실은 배너가 따로 말한다.
   * false 면 오류가 나는 즉시 오류 화면이다.
   */
  keepStale?: boolean;
}

/** 로딩·오류 화면을 돌려준다. 본문을 그려도 되면 null 이다. */
export function queryGate(
  query: { isLoading: boolean; error: unknown; data: unknown },
  { label, testId, keepStale = true }: QueryGateOptions = {},
): ReactElement | null {
  if (query.isLoading) return <Loading label={label} />;
  if (query.error && (!keepStale || query.data === undefined)) return <ErrorMessage error={query.error} testId={testId} />;
  return null;
}

/** 로딩 문구. `queryGate` 는 컴포넌트가 아니라서 기본 문구를 이 컴포넌트가 사전에서 읽는다. */
function Loading({ label }: { label?: string }) {
  const t = useT();
  return <p className="text-sm text-muted">{label ?? t.common.loading}</p>;
}
