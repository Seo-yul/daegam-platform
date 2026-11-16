import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "mcpServers.form.versionOption": [[2, "openapi", 12], [1, "wsdl", 1]],
  "mcpServers.detail.labels.lockedPaths": [[4]],
  "mcpServers.list.noMatch": [["calc"]],
  "mcpServers.list.empty": [["default", createElement("a", null, "Backend API")]],
  "mcpServers.detail.deployBody": [[3]],
  "mcpServers.detail.redeployBehind": [[2, 3]],
  "mcpServers.detail.redeployBody": [[3]],
  "mcpServers.detail.saved": [[3], [1]],
  "mcpServers.detail.allTools": [[12], [null]],
  "mcpServers.detail.deployHint": [[3]],
  "mcpServers.form.clashes": [[2]],
  "mcpServers.form.clashUse": [[1, "getOrder"]],
  "mcpServers.form.clashMore": [[4]],
  "mcpServers.form.dropped": [
    [2, "Add, Divide"],
    [1, "Add"],
  ],
  "mcpServers.form.allExposed": [[40]],
  "mcpServers.form.hiddenPicked": [[3], [1]],
  "mcpServers.form.switchBody": [
    [4, 2],
    [1, 1],
  ],
  "mcpServers.form.revisionNotice": [[4]],
  "mcpServers.suggest.apply": [[4]],
};
