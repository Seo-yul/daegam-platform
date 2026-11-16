import { vi } from "vitest";

import { ApiError } from "@/api/client";
import type { AgentStatus } from "@/api/types";
import { mainHasMemory } from "@/lib/agentGraph";
import { WAKE_RETRY_MS, WAKE_WAIT_MS, acceptsTurns, pausesRuntimeReads, retryAfterMs, savedScaleToZeroBlock, scaleToZeroBlock, untilAwake, windowMinutes } from "@/lib/agentSleep";

import { agentFixture, sleepOf } from "./msw";
import { fakeClock } from "./timers";

/** 깨우는 중의 답. 플랫폼의 게이트처럼 503 과 Retry-After 를 싣는다. */
function waking(retryAfter: string | null = "5"): ApiError {
  return new ApiError(503, { detail: { code: "agent_waking", detail: "the agent is waking" } }, retryAfter);
}

function statusWith(sleep: Partial<NonNullable<AgentStatus["sleep"]>> | null): AgentStatus {
  const base = agentFixture().status!;
  return { ...base, sleep: sleep === null ? null : sleepOf({ window_seconds: 0, ...sleep }) };
}

/** 쓰지 않는 Agent 를 0 으로 줄이기의 화면 규칙 (P44). */
describe("agent sleep lib", () => {
  it("잠든 Agent 와 깨우는 중인 Agent 도 대화를 받고, 목록 읽기는 둘 다 멈춘다", () => {
    expect(["Ready", "Sleeping", "Waking"].map(acceptsTurns)).toEqual([true, true, true]);
    expect(["Pending", "Rolling", "Failed", null].map(acceptsTurns)).toEqual([false, false, false, false]);
    expect(["Sleeping", "Waking"].map(pausesRuntimeReads)).toEqual([true, true]);
    expect(["Ready", "Rolling", null].map(pausesRuntimeReads)).toEqual([false, false, false]);
  });

  it("Retry-After 의 초를 ms 로 읽고, 없거나 읽을 수 없으면 게이트의 5초다", () => {
    expect(retryAfterMs(waking("3"))).toBe(3_000);
    expect(retryAfterMs(waking(null))).toBe(WAKE_RETRY_MS);
    expect(retryAfterMs(waking("곧"))).toBe(WAKE_RETRY_MS);
    // 0 이나 너무 짧은 값으로 서버를 두드리지 않는다
    expect(retryAfterMs(waking("0.1"))).toBe(1_000);
  });

  it("깨우는 중이면 Retry-After 마다 다시 보내고 기다림의 시작을 한 번만 알린다", async () => {
    fakeClock();
    const call = vi.fn<() => Promise<string>>().mockRejectedValueOnce(waking("5")).mockRejectedValueOnce(waking("5")).mockResolvedValue("열림");
    const onWaiting = vi.fn();
    const done = untilAwake(call, { onWaiting });
    await vi.advanceTimersByTimeAsync(0);
    expect(call).toHaveBeenCalledTimes(1);
    expect(onWaiting).toHaveBeenCalledTimes(1);
    // Retry-After 전에는 다시 보내지 않는다
    await vi.advanceTimersByTimeAsync(4_999);
    expect(call).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(call).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(done).resolves.toBe("열림");
    expect(call).toHaveBeenCalledTimes(3);
    expect(onWaiting).toHaveBeenCalledTimes(1);
  });

  it("다른 오류는 다시 보내지 않고 그대로 던진다", async () => {
    const refused = new ApiError(409, { detail: { code: "session_closed", detail: "closed" } });
    const call = vi.fn<() => Promise<string>>().mockRejectedValue(refused);
    await expect(untilAwake(call)).rejects.toBe(refused);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("5분을 넘기면 마지막 agent_waking 을 던진다", async () => {
    fakeClock();
    const call = vi.fn<() => Promise<string>>().mockImplementation(() => Promise.reject(waking("5")));
    const done = untilAwake(call);
    const caught = done.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(WAKE_WAIT_MS + 10_000);
    const error = await caught;
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("agent_waking");
    // 처음과 5분 안의 5초마다 보냈다
    expect(call.mock.calls.length).toBe(WAKE_WAIT_MS / 5_000 + 1);
  });

  it("중단하면 기다림을 AbortError 로 끝내고 더 보내지 않는다", async () => {
    fakeClock();
    const call = vi.fn<() => Promise<string>>().mockImplementation(() => Promise.reject(waking("5")));
    const controller = new AbortController();
    const done = untilAwake(call, { signal: controller.signal });
    const caught = done.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(1_000);
    controller.abort();
    const error = await caught;
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe("AbortError");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("메인 에이전트에 memory 간선으로 붙은 memory 노드만 센다", () => {
    const graph = agentFixture().graph;
    expect(mainHasMemory(graph)).toBe(true);
    const without = { nodes: graph.nodes, edges: graph.edges.filter((e) => e.kind !== "memory") };
    expect(mainHasMemory(without)).toBe(false);
    expect(mainHasMemory({ nodes: [], edges: [] })).toBe(false);
  });

  it("대화상자의 까닭은 정의와 노출을 먼저 보고, 배포가 바꾸지 않는 까닭만 상태에서 읽는다", () => {
    const eligible = statusWith({});
    expect(scaleToZeroBlock({ remembers: false, exposed: true, status: eligible })).toBe("no_memory");
    expect(scaleToZeroBlock({ remembers: true, exposed: true, status: eligible })).toBe("exposed");
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: eligible })).toBeNull();
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: statusWith({ ineligible: "replicas_override" }) })).toBe("replicas_override");
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: statusWith({ ineligible: "setting_off" }) })).toBe("setting_off");
    // 배포는 지금의 이미지로 올리므로 옛 이미지는 대화상자의 까닭이 아니다
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: statusWith({ ineligible: "runtime_image" }) })).toBeNull();
    // 스위치 자체와, 노출을 끄면 풀리는 상태의 까닭은 대화상자의 입력이 정한다
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: statusWith({ ineligible: "agent_off" }) })).toBeNull();
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: statusWith({ ineligible: "exposed" }) })).toBeNull();
    // 첫 배포에는 상태가 없다
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: null })).toBeNull();
    // 서버의 첫 까닭이 노출이라도 뒤의 까닭은 목록에 있다. 대화상자가 노출을 끄면 그 까닭이 선다
    const behind = statusWith({ ineligible: "exposed", ineligible_all: ["exposed", "replicas_override", "setting_off"] });
    expect(scaleToZeroBlock({ remembers: true, exposed: false, status: behind })).toBe("replicas_override");
  });

  it("배포 탭의 까닭은 서버가 판정한 그대로이고 스위치 자체만 뺀다", () => {
    expect(savedScaleToZeroBlock(statusWith({ ineligible: "runtime_image" }))).toBe("runtime_image");
    expect(savedScaleToZeroBlock(statusWith({ ineligible: "no_memory" }))).toBe("no_memory");
    expect(savedScaleToZeroBlock(statusWith({ ineligible: "agent_off" }))).toBeNull();
    expect(savedScaleToZeroBlock(statusWith({}))).toBeNull();
    expect(savedScaleToZeroBlock(null)).toBeNull();
  });

  it("쉬는 창은 분으로 적고, 0 이면 0, 모르면 null 이다", () => {
    expect(windowMinutes(statusWith({ window_seconds: 1800 }))).toBe(30);
    expect(windowMinutes(statusWith({ window_seconds: 150 }))).toBe(3);
    expect(windowMinutes(statusWith({ window_seconds: 0 }))).toBe(0);
    expect(windowMinutes(statusWith(null))).toBeNull();
    expect(windowMinutes(null)).toBeNull();
  });
});
