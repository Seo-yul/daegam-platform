import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

import type { Messages } from "@/i18n";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** 글을 코드 포인트 max 자에서 자르고 넘쳤으면 "…" 를 붙인다. 이모지 같은 서로게이트 쌍을 가르지 않는다. */
export function clipChars(text: string, max: number): string {
  const chars = Array.from(text);
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : text;
}

/** 경로가 root 로 시작하면 그 뒤만, 아니면 경로 그대로다. 기억 파일의 키와 업로드한 파일의 이름이 쓴다. */
export function stripRoot(path: string, root: string): string {
  return path.startsWith(root) ? path.slice(root.length) : path;
}

/** 파일 이름을 확장자 앞(`stem`)과 점을 포함한 확장자(`ext`)로 가른다. 점이 없으면 확장자가 빈 글이다. 서버 `format_of` 처럼 맨 앞의 점도 확장자로 본다. */
export function splitExtension(name: string): { stem: string; ext: string } {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? { stem: name, ext: "" } : { stem: name.slice(0, dot), ext: name.slice(dot) };
}

/** 중단(AbortController)이 끊은 것인가. 스트림 읽기(lib/chatStream.ts)와 깨우는 동안의 기다림(lib/agentSleep.ts)이 이렇게 던진다. */
export function isAbortError(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

/** FileReader 한 번을 Promise 로. File.text() 는 jsdom 에 없고 FileReader 는 브라우저·jsdom 모두 있다. */
function readWith(read: (reader: FileReader) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
    read(reader);
  });
}

export function readFileText(file: Blob): Promise<string> {
  return readWith((reader) => reader.readAsText(file));
}

/** 파일을 `data:` URL 로 읽는다. 화면의 CSP 가 `blob:` 이미지를 막으므로 미리 보기 그림은 이것으로 그린다. */
export function readDataUrl(file: Blob): Promise<string> {
  return readWith((reader) => reader.readAsDataURL(file));
}

/** 체크 하나를 켜거나 끈 새 Set. 받은 Set 은 건드리지 않는다(React 상태). */
export function toggleIn<T>(set: ReadonlySet<T>, key: T, on: boolean): Set<T> {
  const next = new Set(set);
  if (on) next.add(key);
  else next.delete(key);
  return next;
}

const KIB = 1024;
export const MIB = 1024 * KIB;

/** 하루의 밀리초. 개인 토큰의 만료 선택과 평가가 남는 날 수가 같이 쓴다. */
export const DAY_MS = 86_400_000;

/** 바이트 수를 B·KB·MB 한 덩이로. Knowledge 문서 목록과 장기 기억 서랍이 쓴다. */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "-";
  if (n < KIB) return `${n} B`;
  if (n < MIB) return `${(n / KIB).toFixed(1)} KB`;
  return `${(n / MIB).toFixed(1)} MB`;
}

/** 바이트 수를 MiB 로. 상한을 MiB 로 적는 문장(Skill 크기) 곁의 상세가 같은 단위를 쓴다. */
export function formatMib(n: number): string {
  return `${(n / MIB).toFixed(1)} MiB`;
}

/** 시각을 날짜와 시간 한 덩이로. 브라우저 대신 화면 언어의 형식을 따른다(P49). 시간대는 브라우저의 것이다. */
export function formatDate(t: Messages, iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(t.locale);
}

/**
 * 시각을 "HH:MM" 으로. 몇 시간 안에 오는 때(개인 토큰의 워밍업, 미리 보기의 끝)를 날짜 없이 말한다. 시간대는 브라우저의 것이고
 * 24시간 표기라 두 언어가 같게 적는다.
 */
export function formatClock(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** 수를 자릿수 구분이 있는 글로. 화면 언어의 형식을 따른다(P49). */
export function formatNumber(t: Messages, n: number): string {
  return n.toLocaleString(t.locale);
}

/** 메트릭의 숫자 한 칸. 값이 없으면 0 이 아니라 "-" 다. 0 과 "못 받았다" 는 다른 말이다. */
export function metricNumber(t: Messages, v: number | null | undefined): string {
  return typeof v === "number" ? formatNumber(t, v) : "-";
}

/**
 * 흐른 시간을 한 덩이로. 개요의 "가동 시간" 이 쓴다 (D44 셋째). 단위는 사전(common.time)에서 온다.
 *
 * 초까지 말하면 5초 폴링마다 숫자가 흔들려 오히려 못 읽는다. 분 아래는 뭉치고 위로 갈수록
 * 단위를 둘까지만 적는다.
 */
export function formatDuration(t: Messages, seconds: number, { exact = false }: { exact?: boolean } = {}): string {
  const time = t.common.time;
  // exact 는 한 시간 아래를 초까지 적는다. 승인을 기다린 시간(트레이스 창)은 초가 뜻이 있다
  if (seconds < 60) return exact ? time.seconds(Math.round(seconds)) : time.underMinute;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return exact ? time.minutesSeconds(minutes, Math.floor(seconds) % 60) : time.minutes(minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const restMinutes = minutes % 60;
    return restMinutes ? time.hoursMinutes(hours, restMinutes) : time.hours(hours);
  }
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? time.daysHours(days, restHours) : time.days(days);
}

/** 날 하나(시각 없이). 날마다 막대의 이름표가 쓴다. */
export function formatDay(t: Messages, iso: string): string {
  return new Date(iso).toLocaleDateString(t.locale);
}

/**
 * 한 시간의 시작(날과 시). 시간마다 막대의 이름표가 쓴다. 서머타임이 끝나는 날에는 같은 이름표가 두 번 나온다. 시간대의
 * 오프셋에 분이 있으면(Asia/Kolkata 의 +5:30) UTC 정시가 그곳의 30분이므로 분까지 적는다.
 */
export function formatHour(t: Messages, iso: string): string {
  const d = new Date(iso);
  const minute = d.getTimezoneOffset() % 60 !== 0 ? "2-digit" : undefined;
  return d.toLocaleString(t.locale, { month: "numeric", day: "numeric", hour: "numeric", minute });
}

/** 날짜로 적기 시작하는 날 수. 한 달이 넘은 일은 며칠 전보다 날짜가 빨리 읽힌다. */
const AGO_DATE_AFTER_DAYS = 30;

/** 어느 시각(배포 시작 `status.since`, 수정 시각)부터 흐른 시간(초). 값이 없거나 읽지 못하면 null 이다. */
export function elapsedSeconds(since: string | null | undefined, now: number = Date.now()): number | null {
  if (!since) return null;
  const started = Date.parse(since);
  if (Number.isNaN(started)) return null;
  return Math.max(0, Math.round((now - started) / 1000));
}

/**
 * 지금으로부터 얼마 전인지. 목록 카드의 "누가 언제 고쳤는지" 가 쓴다. 가동 시간(formatDuration)과 달리 단위를 하나만
 * 적는다. 카드의 흐린 한 줄에 "35일 15시간 전" 은 길고, 그만큼 정확할 일이 없다. 한 달이 넘으면 화면 언어의 날짜다.
 */
export function formatAgo(t: Messages, iso: string | null | undefined, now: number = Date.now()): string | null {
  const seconds = elapsedSeconds(iso, now);
  if (iso == null || seconds === null) return null;
  const time = t.common.time;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return time.justNow;
  if (minutes < 60) return time.minutesAgo(minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return time.hoursAgo(hours);
  const days = Math.floor(hours / 24);
  return days < AGO_DATE_AFTER_DAYS ? time.daysAgo(days) : new Date(iso).toLocaleDateString(t.locale);
}

/**
 * 표에서 key 의 값. 서버가 준 코드로 사전의 표를 찾을 때 쓴다. 표가 직접 가진 키만 보므로 `constructor` 같은 코드가 와도
 * 프로토타입의 값을 돌려주지 않는다. 없으면 undefined 다.
 */
export function ownValue<V>(table: Readonly<Record<string, V>>, key: string): V | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/** 날짜 칸(YYYY-MM-DD)에서 offsetDays 날 뒤 0시(브라우저 시간대)의 ISO. 비면 undefined 다. 끝을 들지 않는 기간의
 * 끝은 고른 날의 다음 날로 보낸다(offsetDays 1). */
export function dayStart(value: string, offsetDays = 0): string | undefined {
  if (!value) return undefined;
  const day = new Date(`${value}T00:00:00`);
  day.setDate(day.getDate() + offsetDays);
  return day.toISOString();
}

/** 브라우저의 시간대. 서버가 날을 이 시간대로 자른다. */
export function browserZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
