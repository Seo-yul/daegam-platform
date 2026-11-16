import type { Messages } from "../types";
import { plural } from "./plural";

/** 여러 화면이 같이 쓰는 말. F1·F2 만 더한다. */
export const common: Messages["common"] = {
  confirm: "Confirm",
  cancel: "Cancel",
  processing: "Working…",
  loading: "Loading…",
  networkError: "Could not reach the server. Try again in a moment.",
  nameConfirm: { label: "Confirm name", hint: "Type the name to delete it." },
  menu: { label: "Actions", more: "More actions" },
  // label 은 문장 가운데 들어가므로 소문자 명사다(token). 기본값 URL 은 약어라 그대로 쓴다
  copy: {
    defaultLabel: "URL",
    action: (label: string): string => `Copy ${label}`,
    copied: (label: string): string => `Copied ${label}`,
    failed: (label: string): string => `Could not copy ${label}`,
    title: "Copy",
    copiedTitle: "Copied",
    failedTitle: (label: string): string => `Could not copy. Select the ${label} and copy it yourself`,
  },
  delete: "Delete",
  deleting: "Deleting…",
  // 값 칸에도 문장 가운데에도 들어가므로 소문자로 시작한다
  time: {
    underMinute: "less than a minute",
    seconds: (seconds: number): string => `${seconds}s`,
    minutesSeconds: (minutes: number, seconds: number): string => `${minutes}m ${seconds}s`,
    ms: (value: string): string => `${value} ms`,
    minutes: (minutes: number): string => plural(minutes, "minute", "minutes"),
    hours: (hours: number): string => plural(hours, "hour", "hours"),
    hoursMinutes: (hours: number, minutes: number): string => `${plural(hours, "hour", "hours")} ${plural(minutes, "minute", "minutes")}`,
    days: (days: number): string => plural(days, "day", "days"),
    daysHours: (days: number, hours: number): string => `${plural(days, "day", "days")} ${plural(hours, "hour", "hours")}`,
    justNow: "just now",
    minutesAgo: (minutes: number): string => `${plural(minutes, "minute", "minutes")} ago`,
    hoursAgo: (hours: number): string => `${plural(hours, "hour", "hours")} ago`,
    daysAgo: (days: number): string => `${plural(days, "day", "days")} ago`,
  },
};
