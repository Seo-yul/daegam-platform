import { describe, expect, it } from "vitest";

import { RUNTIME_ATTACHMENTS, RUNTIME_CANCEL, RUNTIME_MEMORY_FILES, RUNTIME_REGENERATE, RUNTIME_SESSION_ADMIN, RUNTIME_SESSION_FILES } from "@/lib/runtimeFeatures";
import { endpointTestId, visibleEndpoints } from "@/pages/agents/externalEndpoints";

/** 배포된 런타임이 알린 능력 이름만 든 상태다. */
function status(...features: string[]) {
  return { runtime: { image: "daegam-agent-runtime:0.2.0", features } };
}

/** 보이는 행의 테스트 id 다. 표의 순서와 상관없이 견주도록 정렬한다. */
function ids(features: string[] | null): string[] {
  return visibleEndpoints(features ? status(...features) : null)
    .map(endpointTestId)
    .sort();
}

function sorted(list: string[]): string[] {
  return [...list].sort();
}

/** 모든 런타임에 있는 경로다. */
const BASE = [
  "external-endpoint-POST-chat",
  "external-endpoint-GET-sessions",
  "external-endpoint-GET-sessions-sid",
  "external-endpoint-GET-sessions-sid-pending",
  "external-endpoint-POST-sessions-sid-decisions",
];
/** 런타임이 그 능력을 알릴 때만 더 서는 경로다. */
const BY_FEATURE: [string, string[]][] = [
  // 제목 검색·이름 바꾸기·지우기
  [RUNTIME_SESSION_ADMIN, ["external-endpoint-GET-sessions-q", "external-endpoint-PATCH-sessions-sid", "external-endpoint-DELETE-sessions-sid"]],
  // 다시 생성
  [RUNTIME_REGENERATE, ["external-endpoint-POST-sessions-sid-regenerate"]],
  // 내 장기 기억 보기와 하나 삭제
  [RUNTIME_MEMORY_FILES, ["external-endpoint-GET-memories", "external-endpoint-GET-memories-files-key", "external-endpoint-DELETE-memories-files-key"]],
  // `/chat` 본문의 첨부
  [RUNTIME_ATTACHMENTS, ["external-endpoint-POST-chat-attachments"]],
  // 세션 파일의 목록과 다운로드
  [RUNTIME_SESSION_FILES, ["external-endpoint-GET-sessions-sid-files", "external-endpoint-GET-sessions-sid-files-path"]],
];

/** 외부 호출 카드의 경로 표 (P36). 런타임 능력에 기대는 경로는 그 이름이 기록에 있을 때만 선다. */
describe("visibleEndpoints", () => {
  it("능력을 알리지 않은 런타임(첫 배포, 옛 이미지)은 모든 런타임에 있는 경로만 낸다", () => {
    expect(ids(null)).toEqual(sorted(BASE));
    expect(ids([RUNTIME_CANCEL])).toEqual(sorted(BASE));
  });

  it.each(BY_FEATURE)("%s 능력을 알리면 그 경로가 더 선다", (feature, rows) => {
    expect(ids([feature])).toEqual(sorted([...BASE, ...rows]));
  });

  it("모두 알리면 모든 경로가 서고 행의 테스트 id 가 겹치지 않는다", () => {
    const shown = ids([RUNTIME_CANCEL, ...BY_FEATURE.map(([feature]) => feature)]);
    expect(shown).toEqual(sorted([...BASE, ...BY_FEATURE.flatMap(([, rows]) => rows)]));
    expect(new Set(shown).size).toBe(shown.length);
  });
});
