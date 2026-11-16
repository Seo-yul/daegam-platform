/**
 * 쓰지 않는 Agent 를 0 으로 줄이기의 화면 쪽 규칙 (P44, D103).
 *
 * 잠든 Agent(phase `Sleeping`)는 배포된 것이고 다음 턴이 깨운다. 그래서 대화를 받는 phase 는 Ready 와 Sleeping·Waking 이다.
 * 턴(채팅·다시 생성·결정)은 깨우는 동안 503 `agent_waking` 과 Retry-After 로 답받고, 화면은 보낼 것을 쥔 채 Retry-After 마다
 * 다시 보낸다(`untilAwake`). 플랫폼의 깨우기 한도와 같은 5분을 넘기면 마지막 오류를 보인다. 읽기는 깨우지 않으므로 잠들었거나
 * 깨우는 중인 Agent 의 목록 화면은 런타임을 읽지 않고 휴면 줄과 깨우기 단추를 보이며, 상태가 Sleeping·Waking 을 떠나면 다시
 * 읽는다(`pausesRuntimeReads`).
 */
import { ApiError } from "@/api/client";
import type { AgentStatus, SleepIneligible } from "@/api/types";

/** 깨우는 중이라 Retry-After 뒤에 다시 보내라는 오류 코드다(503). */
export const AGENT_WAKING = "agent_waking";
/** 잠든 Agent 에 런타임을 읽는 요청을 보냈다는 오류 코드다(409). 읽기는 깨우지 않는다. */
export const AGENT_ASLEEP = "agent_asleep";
/** 깨우는 동안 다시 보내는 상한(ms). 플랫폼이 준비된 파드 없이 끝내는 깨우기 한도(5분)와 같다. */
export const WAKE_WAIT_MS = 5 * 60_000;
/** Retry-After 가 없거나 읽을 수 없을 때 다시 보내기까지의 간격(ms). 플랫폼의 게이트가 보내는 값이다. */
export const WAKE_RETRY_MS = 5_000;

/** 잠든 Agent 인가(phase Sleeping). 홈 챗이 고르개에서 고른 Agent 를 깨울지 가른다. */
export function isSleeping(phase: string | null | undefined): boolean {
  return phase === "Sleeping";
}

/**
 * 목록 화면이 런타임 읽기를 멈추는 phase 인가. 잠든 Agent 의 읽기는 409 agent_asleep 이고 깨우는 중에는 503 agent_waking 이라 둘 다
 * 읽을 것이 없다. 화면은 휴면 줄을 보이고 상태가 이 둘을 떠나면 다시 읽는다.
 */
export function pausesRuntimeReads(phase: string | null | undefined): boolean {
  return phase === "Sleeping" || phase === "Waking";
}

/** 대화를 받는 phase 인가. 잠든 Agent 와 깨우는 중인 Agent 도 보낸 턴이 깨우고 기다린다. */
export function acceptsTurns(phase: string | null | undefined): boolean {
  return phase === "Ready" || phase === "Sleeping" || phase === "Waking";
}

/** 깨우는 중이라는 오류 답인가(503 agent_waking). */
export function isWakingError(e: unknown): e is ApiError {
  return e instanceof ApiError && e.code === AGENT_WAKING;
}

/**
 * 다시 보내기까지의 간격(ms). 응답의 Retry-After(초, `ApiError.retryAfter`)를 읽고, 없거나 숫자가 아니면 기본값이다. 1초 아래로는
 * 내리지 않는다.
 */
export function retryAfterMs(e: ApiError): number {
  const raw = e.retryAfter;
  const seconds = raw === null ? NaN : Number(raw);
  return Number.isFinite(seconds) && seconds > 0 ? Math.max(1_000, seconds * 1_000) : WAKE_RETRY_MS;
}

/** ms 만큼 기다린다. signal 이 끊기면 AbortError 로 거절한다. */
function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface UntilAwakeOptions {
  /** 기다림을 끝낸다. 끊기면 AbortError 로 거절한다. */
  signal?: AbortSignal;
  /** 처음 agent_waking 을 받아 기다리기 시작할 때 한 번 부른다. 기다림 줄을 세우는 자리다. */
  onWaiting?: () => void;
  /** 다시 보내는 상한(ms). 넘기면 마지막 agent_waking 오류를 던진다. */
  limitMs?: number;
}

/**
 * call 을 부르고, 깨우는 중(503 agent_waking)이면 Retry-After 마다 다시 부른다. 다른 답이면 그대로 돌려주거나 던진다. 보낼 것(메시지,
 * 첨부, 결정)은 부르는 쪽이 call 안에 쥐고 있으므로 다시 불러도 같은 것이 간다. 플랫폼의 게이트는 위임을 열거나 업로드를 쓰기 전에
 * 답하므로 다시 보내도 남는 것이 없다.
 */
export async function untilAwake<T>(call: () => Promise<T>, { signal, onWaiting, limitMs = WAKE_WAIT_MS }: UntilAwakeOptions = {}): Promise<T> {
  const deadline = Date.now() + limitMs;
  let waiting = false;
  for (;;) {
    try {
      return await call();
    } catch (e) {
      if (!isWakingError(e)) throw e;
      const wait = retryAfterMs(e);
      if (Date.now() + wait > deadline) throw e;
      if (!waiting) {
        waiting = true;
        onWaiting?.();
      }
      await pause(wait, signal);
    }
  }
}

/** Scale to zero 아래 보이는 까닭. 스위치 자체(agent_off)는 까닭이 아니라 꺼진 체크다. */
export type ScaleToZeroBlock = Exclude<SleepIneligible, "agent_off">;

/** 배포 탭의 까닭. 서버가 지금 도는 리비전과 이미지로 판정한 `sleep.ineligible` 그대로이고, 스위치 자체는 빼고 없으면 null 이다. */
export function savedScaleToZeroBlock(status: AgentStatus | null | undefined): ScaleToZeroBlock | null {
  const saved = status?.sleep?.ineligible ?? null;
  return saved === "agent_off" ? null : saved;
}

/**
 * 배포·재배포 대화상자의 까닭. 배포는 지금의 정의를 지금의 이미지로 핀하므로 memory 노드는 정의에서, 노출은 대화상자의 입력에서
 * 읽고, 런타임 이미지는 까닭이 아니다. 배포가 바꾸지 않는 까닭(Advanced 의 spec.replicas, 플랫폼 스위치)은 상태의 까닭 목록
 * `sleep.ineligible_all` 에서 읽는다. 첫 까닭만 보면 앞의 까닭(노출 같은 것)에 가려 뒤의 까닭을 놓친다. 없으면 null 이다.
 */
export function scaleToZeroBlock({
  remembers,
  exposed,
  status,
}: {
  /** 배포할 정의의 메인 에이전트에 memory 노드가 있는가(lib/agentGraph.ts mainHasMemory). */
  remembers: boolean;
  exposed: boolean;
  status: AgentStatus | null | undefined;
}): ScaleToZeroBlock | null {
  if (!remembers) return "no_memory";
  if (exposed) return "exposed";
  const saved = status?.sleep?.ineligible_all ?? [];
  return saved.find((code): code is ScaleToZeroBlock => code === "replicas_override" || code === "setting_off") ?? null;
}

/** 쉬는 창(초)을 분으로. 0 이면 0(스스로 잠들지 않는다)이고, 상태가 없어(첫 배포) 모르면 null 이다. */
export function windowMinutes(status: AgentStatus | null | undefined): number | null {
  const seconds = status?.sleep?.window_seconds;
  if (seconds === undefined) return null;
  return seconds > 0 ? Math.max(1, Math.round(seconds / 60)) : 0;
}
