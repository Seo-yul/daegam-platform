import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "backendApis.detail.reportTitle": [[3]],
  "backendApis.list.noMatch": [["orders"]],
  "backendApis.list.empty": [["default"]],
  "backendApis.detail.docsTitle": [[2]],
  "backendApis.detail.toolCount": [[3], [1], [null]],
  "backendApis.new.saveTarget": [["spec.yaml"]],
  "backendApis.new.target.file": [["orders.yaml"]],
  "backendApis.new.previewHint": [[createElement("b", null, "Check"), createElement("b", null, "Convert")]],
  "backendApis.new.versionNote": [[2]],
  "backendApis.new.nameExists": [[createElement("a", null, "orders"), 2]],
  "backendApis.refine.subtitle": [
    ["Refine with LLM", 3],
    ["Clean up with rules", null],
  ],
  "backendApis.refine.job.batch": [[1, 3]],
  "backendApis.refine.job.failed": [[2], [1]],
  "backendApis.refine.job.tokens": [["1,234"]],
  "backendApis.refine.job.more": [[4]],
  "backendApis.verdict.caseCollision": [["updateUser", "Id · id"]],
  "backendApis.verdict.summary": [
    [21, 18, 1, 0, 2],
    [3, 3, 0, 0, 0],
  ],
  "backendApis.verdict.toolCount": [[2], [1]],
  "backendApis.verdict.details": [["Name rules"]],
  "backendApis.verdict.truncated": [['{"a": 1}', 120]],
  "backendApis.verdict.moreTools": [[3]],
};
