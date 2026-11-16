import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "usage.csvName": [["agent"]],
  "usage.truncated": [["200"]],
  "usage.health.warning": [["writer_conflicts 2"]],
  "usage.health.oldAgents": [[1], [3]],
  "usage.pricing.reprice.progress": [["120", "400", "3m"], ["0", "4", null]],
  "usage.pricing.reprice.done": [["400"]],
};
