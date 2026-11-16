import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "skills.exportRevision": [[2]],
  "skills.newerRevision": [[3]],
  "skills.list.empty": [["default"]],
  "skills.list.owner": [[createElement("span", null, "acme")]],
  "skills.detail.ownerBadge": [["acme"]],
  "skills.detail.skipped": [["__MACOSX/._SKILL.md"]],
  "skills.detail.cloneBody": [[2]],
  "skills.summary.size": [
    ["1.2 KiB", 3],
    ["120 B", 1],
  ],
  "skills.files.count": [
    [2, 3],
    [1, 1],
  ],
  "skills.importField.wrongType": [["guide.pdf"]],
  "skills.importField.sizeLimit": [["2.0 MiB"]],
  "skills.revisions.latest": [[2]],
  "skills.revisions.clonedFrom": [["acme", "web-research", 3]],
  "skills.form.newRevisionTitle": [["Report style"]],
  "skills.form.revisionSubtitle": [[2]],
  "skills.form.validation.errors": [
    [2, 3],
    [1, 1],
  ],
  "skills.form.validation.warnings": [[2], [1]],
};
