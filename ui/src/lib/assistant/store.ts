/**
 * 비서 패널의 UI 상태 (D31). 열림·"따라가기"·테넌트별 현재 세션이고 셋 다 localStorage 에 남아
 * useSyncExternalStore 로 구독한다. 감사 화면의 "세션 열기" 도 이 저장소로 패널을 연다.
 *
 * `daegam.assistant.follow` 는 스펙의 두 키 밖에 하나 더한 것이다. 따라가기를 끈 사용자가 새로고침할 때마다
 * 다시 꺼야 하면 `navigate` 도구가 화면을 뺏어 가므로 토글도 열림 상태처럼 기억한다.
 */
import { useSyncExternalStore } from "react";

import { ASSISTANT_PROVIDER_KEY_PREFIX, ASSISTANT_SESSION_KEY_PREFIX, STORAGE_KEYS, assistantProviderKey, assistantSessionKey, createStore, readItem, removeMatching, writeItem } from "@/lib/storage";

export const ASSISTANT_OPEN_KEY = STORAGE_KEYS.assistantOpen;
export const ASSISTANT_FOLLOW_KEY = STORAGE_KEYS.assistantFollow;
export { assistantProviderKey, assistantSessionKey };

/** `state=running` 인 세션을 다시 읽는 간격. 턴은 요청과 분리된 태스크라 SSE 를 놓쳐도 전사는 문서에 쌓인다. */
export const ASSISTANT_POLL_MS = 3000;

interface AssistantUiState {
  open: boolean;
  follow: boolean;
  /** 테넌트 → 세션 id. 아직 바꾸지 않은 테넌트는 키가 없고 localStorage 에서 읽는다. */
  sessions: Record<string, string | null>;
  /** 테넌트 → 다음 새 대화에 쓸 provider 이름 (D45). 세션이 가진 것이 있으면 그쪽이 이긴다. */
  providers: Record<string, string | null>;
}

let state: AssistantUiState | null = null;
const store = createStore();

function snapshot(): AssistantUiState {
  if (!state) state = { open: readItem(ASSISTANT_OPEN_KEY) === "true", follow: readItem(ASSISTANT_FOLLOW_KEY) !== "false", sessions: {}, providers: {} };
  return state;
}

export function isAssistantOpen(): boolean {
  return snapshot().open;
}

/**
 * 사용자가 방금 패널을 열었는가. 열릴 때 입력에 포커스를 줄지 정하는 데만 쓴다.
 *
 * 페이지를 열 때 저장된 상태로 패널이 서는 경우에는 포커스를 뺏으면 안 된다. 그러면 키보드로 오는 사람이
 * 첫 Tab 에서 본문 건너뛰기 링크 대신 오른쪽 패널 안에 떨어진다(실측). 직접 토글을 누른 경우에만
 * 참이고 한 번 읽으면 꺼진다.
 */
let openedByUser = false;

export function consumeAssistantAutoFocus(): boolean {
  const v = openedByUser;
  openedByUser = false;
  return v;
}

/** persist=false 는 캔버스 자동 접기처럼 저장하지 않는 변경이다(사이드바 레일과 같은 규칙). */
export function setAssistantOpen(open: boolean, persist = true): void {
  state = { ...snapshot(), open };
  // 저장하는 변경만 사용자의 행동이다. 캔버스를 벗어나며 되돌리는 것은 아니다
  if (open && persist) openedByUser = true;
  if (persist) writeItem(ASSISTANT_OPEN_KEY, open ? "true" : "false");
  store.emit();
}

export function setAssistantFollow(follow: boolean): void {
  state = { ...snapshot(), follow };
  writeItem(ASSISTANT_FOLLOW_KEY, follow ? "true" : "false");
  store.emit();
}

export function getAssistantSessionId(tenant: string): string | null {
  const s = snapshot();
  if (tenant in s.sessions) return s.sessions[tenant];
  return readItem(assistantSessionKey(tenant));
}

export function setAssistantSessionId(tenant: string, id: string | null): void {
  const s = snapshot();
  state = { ...s, sessions: { ...s.sessions, [tenant]: id } };
  writeItem(assistantSessionKey(tenant), id);
  store.emit();
}

export function getAssistantProvider(tenant: string): string | null {
  const s = snapshot();
  if (tenant in s.providers) return s.providers[tenant];
  return readItem(assistantProviderKey(tenant));
}

/** 홈이나 패널에서 모델을 고를 때. 다음 새 대화의 기본값이 된다. */
export function setAssistantProvider(tenant: string, name: string | null): void {
  const s = snapshot();
  state = { ...s, providers: { ...s.providers, [tenant]: name } };
  writeItem(assistantProviderKey(tenant), name);
  store.emit();
}

/** 다른 화면(감사 로그의 via 링크)이 세션을 열어 달라고 할 때. 세션을 고르고 패널을 연다. */
export function openAssistantSession(tenant: string, id: string): void {
  setAssistantSessionId(tenant, id);
  setAssistantOpen(true);
}

/**
 * Agent 캔버스에 저장하지 않은 변경이 있는가 (P48 6.5절). 캔버스가 바뀔 때마다 적고 떠날 때 지운다. 패널은 턴을 열 때
 * 읽어 문맥에 싣고, 청지기는 그 변경을 저장하거나 버려 달라고 먼저 말한다. 턴을 열 때만 읽으므로 구독하지 않고
 * 저장하지도 않는다.
 */
let canvasUnsaved = false;

export function setCanvasUnsaved(unsaved: boolean): void {
  canvasUnsaved = unsaved;
}

export function isCanvasUnsaved(): boolean {
  return canvasUnsaved;
}

/** 테스트용. 메모리 상태와 저장된 키를 지운다. */
export function resetAssistantStore(): void {
  state = null;
  openedByUser = false;
  canvasUnsaved = false;
  removeMatching((k) => k === ASSISTANT_OPEN_KEY || k === ASSISTANT_FOLLOW_KEY || k.startsWith(ASSISTANT_SESSION_KEY_PREFIX) || k.startsWith(ASSISTANT_PROVIDER_KEY_PREFIX));
  store.emit();
}

export function useAssistantOpen(): boolean {
  return useSyncExternalStore(store.subscribe, isAssistantOpen, () => false);
}

export function useAssistantFollow(): boolean {
  return useSyncExternalStore(
    store.subscribe,
    () => snapshot().follow,
    () => true,
  );
}

export function useAssistantSessionId(tenant: string): string | null {
  return useSyncExternalStore(
    store.subscribe,
    () => getAssistantSessionId(tenant),
    () => null,
  );
}

export function useAssistantProvider(tenant: string): string | null {
  return useSyncExternalStore(
    store.subscribe,
    () => getAssistantProvider(tenant),
    () => null,
  );
}
