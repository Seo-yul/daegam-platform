import { act } from "@testing-library/react";
import { onTestFinished, vi } from "vitest";

/**
 * 가짜 시계 (vitest fake timers). 켠 테스트가 끝나면(실패해도) 진짜 시계로 돌아가므로 부르는 쪽은 되돌리지 않는다.
 * 테스트 한가운데서만 잠깐 가짜 시계를 쓰려면 vi.useFakeTimers 를 try/finally 로 감싼다.
 */

/** 멈춘 가짜 시계. 시간은 테스트가 돌릴 때만 간다. */
export function fakeClock(): void {
  install();
}

/**
 * 스스로도 가는 가짜 시계. Testing Library 의 waitFor·findBy 는 vitest 의 가짜 시계를 알아보지 못하고 가짜로 바뀐
 * setTimeout 으로 기다린다. 그래서 시계가 멈춰 있으면 기다림이 끝나지 않는다. 폴링하는 화면은 이것을 켜고 주기는
 * advanceClock 으로 건너뛴다.
 */
export function tickingClock(): void {
  install({ shouldAdvanceTime: true });
}

/** 가짜 시계를 ms 만큼 돌리고 그동안 생긴 React 갱신까지 마친다. */
export async function advanceClock(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function install(options?: Parameters<typeof vi.useFakeTimers>[0]): void {
  vi.useFakeTimers(options);
  onTestFinished(() => {
    vi.useRealTimers();
  });
}
