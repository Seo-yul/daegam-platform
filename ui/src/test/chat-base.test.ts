/**
 * 채팅 라우트의 바탕 (P44b 6절). 채팅 패널은 Agent 의 라우트와 부르는 사람의 미리 보기의 라우트를 같은 이름으로 부르고 바탕 경로만
 * 다르다.
 */
import { describe, expect, it } from "vitest";

import { chatBase, chatPath, chatRoute, chatSessionPath } from "@/api/client";
import { sessionFilesKey } from "@/lib/queryKeys";

describe("채팅 라우트의 바탕", () => {
  it("Agent 는 /agents/{id} 아래, 미리 보기는 /agents/{id}/preview 아래다", () => {
    expect(chatPath(chatBase("ag-1"), "/chat")).toBe("/agents/ag-1/chat");
    expect(chatPath(chatBase("ag-1", true), "/chat")).toBe("/agents/ag-1/preview/chat");
    expect(chatPath(chatBase("ag-1", true), "/uploads")).toBe("/agents/ag-1/preview/uploads");
  });

  it("세션 아래 경로는 id 와 세션 id 를 조각 하나로 인코딩한다", () => {
    expect(chatSessionPath(chatBase("a/b"), "s 1", "/decisions")).toBe("/agents/a%2Fb/sessions/s%201/decisions");
    expect(chatSessionPath(chatBase("ag-1", true), "s1")).toBe("/agents/ag-1/preview/sessions/s1");
  });

  it("생성 클라이언트의 경로는 미리 보기면 /preview 아래의 같은 이름이다", () => {
    expect(chatRoute(chatBase("ag-1"), "/agents/{agent_id}/sessions/{session_id}/pending")).toBe("/agents/{agent_id}/sessions/{session_id}/pending");
    expect(chatRoute(chatBase("ag-1", true), "/agents/{agent_id}/sessions/{session_id}/pending")).toBe("/agents/{agent_id}/preview/sessions/{session_id}/pending");
  });

  it("세션 파일 캐시는 Agent 의 것과 미리 보기의 것을 나눈다", () => {
    expect(sessionFilesKey("t1", chatBase("ag-1"), "s1")).toEqual(["agents", "t1", "ag-1", "files", "s1"]);
    expect(sessionFilesKey("t1", chatBase("ag-1", true), "s1")).toEqual(["agents", "t1", "ag-1", "preview", "files", "s1"]);
  });
});
