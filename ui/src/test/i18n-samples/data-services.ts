import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "dataServices.list.empty": [["default"]],
  "dataServices.verify.modules": [["search, ReJSON"]],
  "dataServices.capability.checkpointer.dbNotZero": [[2]],
  "dataServices.detail.labels.tlsEndpoint": [[6380, "ds-ds-redis-ca"]],
  "dataServices.detail.labels.plainConsumersCount": [[2, 1], [1, 0], [0, 2]],
  "dataServices.capability.note.checkpointer.dbNotZero": [[2]],
};
