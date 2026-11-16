import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "warnings.prose.prefixFallback.text": [["fix-hint"]],
  "warnings.prose.priorityConflict.text": [["fix-hint"]],
  "warnings.prose.filterUnchecked.text": [["fix-hint"]],
  "warnings.codes.runs_paused_by_deploy.text": [[{ schedules: 2, triggers: 1, broken: 3 }]],
};
