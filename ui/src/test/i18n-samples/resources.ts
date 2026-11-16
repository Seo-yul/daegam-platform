import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "resources.phase.progress.Pending": [[5], [1]],
  "resources.phase.progress.Rolling": [[5], [1]],
  "resources.phase.slow.Pending": [[2], [1]],
  "resources.phase.slow.Rolling": [[3], [1]],
  "resources.phase.progress.Waking": [[5], [1]],
  "resources.phase.slow.Waking": [[2], [1]],
  "resources.delete.title": [["Backend API", "Unpublish"]],
  "resources.delete.rowLabel": [["billing-api"]],
};
