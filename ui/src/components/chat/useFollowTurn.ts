/**
 * 연결과 떨어져 도는 Agent 의 턴을 세션 읽기로 따라간다 (P78 4절). 채팅 패널이 되살린 대화나 스트림이 끊긴 대화에서 쓴다.
 * 플랫폼이 준 간격(`poll_seconds`, Agent 에 바로 닿으면 3초, API 서버를 거치면 10초)으로 가벼운 읽기(`view=progress`)를
 * 되풀이하고, 턴이 끝났다고 답하면 부르는 쪽이 전체를 읽는다. 404 는 그 세션을 모르는 다른 파드일 수 있어 다른 답 없이 404 만
 * 30초 받아야 대화를 잃은 것으로 본다. 5xx·네트워크 오류·시간 초과·429 는 간격을 늘려 다시 읽고, 그 밖의 4xx(배포 해제, 권한)는
 * 다시 읽어도 소용없으므로 멈춘다.
 */
import { useEffect, useRef } from "react";

import { ApiError, type ChatBase } from "@/api/client";
import { getAgentSessionProgress } from "@/api/resources";
import { itemsFromTranscript } from "@/lib/assistant";
import { SESSION_GONE } from "@/lib/chatNotice";

import type { ChatItem } from "./items";

/** 다른 답 없이 404 만 이만큼 받으면 대화를 잃은 것으로 본다(ms). 한 번의 404 는 그 세션을 모르는 다른 파드일 수 있다. */
export const SESSION_GONE_MS = 30_000;
/** 첫 가벼운 읽기까지 기다리는 간격(초). 그 뒤로는 플랫폼이 준 간격으로 읽는다. */
const FIRST_POLL_SECONDS = 3;
/** 5xx·네트워크 오류 뒤 다시 읽기를 늦추는 상한(ms). */
const MAX_RETRY_MS = 30_000;

export interface FollowTurnHandlers {
  /** 가벼운 읽기가 턴이 돈다고 답했다. 저장된 항목과 지금 시도의 id 다. */
  onProgress: (items: ChatItem[], turnId: string | null) => void;
  /** 가벼운 읽기가 턴이 끝났다고 답했다. 전체를 읽어 보이고, 그사이 새 시도가 돌기 시작했으면 true 다. */
  onEnded: (sessionId: string) => Promise<boolean>;
  /** 따라가기를 멈춘다. 대화를 잃었거나(SESSION_GONE) 다시 읽어도 소용없는 거절이다. */
  onStop: (error: unknown) => void;
}

/** 다시 해 볼 만한 실패인가. 5xx, 네트워크 오류(ApiError 가 아닌 것), 시간 초과(408), 요청 한도(429)다. 404 는 따로 센다. */
export function retryable(e: unknown): boolean {
  return !(e instanceof ApiError) || e.status >= 500 || e.status === 408 || e.status === 429;
}

/** 세션을 찾지 못했다는 답인가. */
export function notFound(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

/** `sessionId` 를 따라간다. null 이면 따라가지 않는다. 처리기는 그릴 때마다 바뀌어도 되고 늘 마지막 것이 불린다. */
export function useFollowTurn(chat: ChatBase, sessionId: string | null, handlers: FollowTurnHandlers): void {
  const latest = useRef(handlers);
  latest.current = handlers;
  useEffect(() => {
    if (sessionId === null) return;
    const sid = sessionId;
    let alive = true;
    let timer: number | undefined;
    // 다른 답 없이 이어진 404 의 첫 시각이다
    let firstNotFound: number | null = null;
    let interval = FIRST_POLL_SECONDS * 1000;
    let retry = 0;
    const next = (ms: number) => {
      timer = window.setTimeout(() => void tick(), ms);
    };
    const tick = async () => {
      try {
        const progress = await getAgentSessionProgress(chat, sid);
        if (!alive) return;
        firstNotFound = null;
        retry = 0;
        interval = progress.pollSeconds * 1000;
        if (progress.session.running) {
          latest.current.onProgress(itemsFromTranscript(progress.transcript?.items), progress.session.turn_id ?? null);
          next(interval);
          return;
        }
        if ((await latest.current.onEnded(sid)) && alive) next(interval);
      } catch (e) {
        if (!alive) return;
        if (notFound(e)) {
          firstNotFound ??= Date.now();
          if (Date.now() - firstNotFound >= SESSION_GONE_MS) {
            latest.current.onStop(SESSION_GONE);
            return;
          }
          next(interval);
          return;
        }
        firstNotFound = null;
        if (!retryable(e)) {
          latest.current.onStop(e);
          return;
        }
        retry = Math.min(retry ? retry * 2 : interval * 2, MAX_RETRY_MS);
        next(retry);
      }
    };
    next(interval);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [chat, sessionId]);
}
