import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "common.copy.action": [["token"]],
  "common.copy.copied": [["token"]],
  "common.copy.failed": [["token"]],
  "common.copy.failedTitle": [["token"]],
  "common.time.minutes": [[5], [1]],
  "common.time.seconds": [[45], [1]],
  "common.time.minutesSeconds": [[3, 12]],
  "common.time.ms": [["1,200"]],
  "common.time.hours": [[3], [1]],
  "common.time.hoursMinutes": [[2, 5], [1, 1]],
  "common.time.days": [[2], [1]],
  "common.time.daysHours": [[2, 3], [1, 1]],
  "common.time.minutesAgo": [[5], [1]],
  "common.time.hoursAgo": [[3], [1]],
  "common.time.daysAgo": [[20], [1]],
};
