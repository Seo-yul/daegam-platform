/**
 * Advanced 탭 (D42). 문서 묶음을 편집기에 넘긴다. 저장은 PUT overrides {yaml}, 드리프트는 reset/absorb 다.
 * Agent·MCP Server·Gateway 상세가 같이 쓴다.
 *
 * 세 상세가 같은 조회·같은 새로고침·같은 게이트를 각자 적고 있었다. 리소스마다 다른 것은 API 셋과
 * Gateway 의 안내 한 줄, 저장 전 검사뿐이라 그것만 인자로 받는다.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, type ReactNode } from "react";

import type { ManifestResponse } from "@/api/types";
import type { AdvancedEditorProps } from "@/components/AdvancedEditor";
import { queryGate } from "@/components/QueryGate";
import { useT } from "@/i18n";

// Monaco 를 포함한 Advanced 편집기는 탭을 열 때만 내려받는다
const AdvancedEditor = lazy(() => import("@/components/AdvancedEditor").then((m) => ({ default: m.AdvancedEditor })));

export interface ManifestTabProps {
  /** 리소스 한 건의 쿼리 키 (["agents", tenant, id]). 매니페스트는 그 아래에 두고, 저장·드리프트 뒤 이 키를 무효화한다. */
  queryKey: readonly unknown[];
  load: () => Promise<ManifestResponse>;
  save: AdvancedEditorProps["onSave"];
  drift: (action: "reset" | "absorb") => Promise<unknown>;
  /** 편집기 위에 두는 안내. */
  note?: ReactNode;
}

export function ManifestTab({ queryKey, load, save, drift, note }: ManifestTabProps) {
  const t = useT();
  const text = t.detail.manifestTab;
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: [...queryKey, "manifest"], queryFn: load });
  const refresh = async () => {
    await q.refetch();
    await queryClient.invalidateQueries({ queryKey });
  };
  const gate = queryGate(q, { label: text.loading, testId: "manifest-error" });
  if (gate) return gate;
  return (
    <Suspense fallback={<p className="text-sm text-muted">{text.editorLoading}</p>}>
      {note}
      <AdvancedEditor documents={q.data!.documents} onSave={save} onReset={() => drift("reset")} onAbsorb={() => drift("absorb")} onChanged={refresh} />
    </Suspense>
  );
}
