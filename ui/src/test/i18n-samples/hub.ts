import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "hub.access.scope.tenant": [["publishing tenant"]],
  "hub.access.subjects.owner": [["Billing team"]],
  "hub.access.subjects.tenants": [["billing, ops"]],
  "hub.access.subjects.minRole": [["viewer"]],
  "hub.access.subjects.roles": [["tenant-admin"]],
  "hub.access.subjects.groups": [["sre"]],
  "hub.list.empty": [["default", createElement("a", null, "MCP Server")]],
  "hub.list.scope": [[createElement("span", null, "tenant")]],
  "hub.export.tokenHint": [["default"], ["team.a"]],
  "hub.export.tokenSteps": [[createElement("a", null, "My MCP tokens"), "DAEGAM_PAT_DEFAULT"]],
  "hub.export.allowlistSkipped": [["home, search"]],
  "hub.tools.count": [[3], [1]],
  "hub.tools.countLive": [[3], [1]],
  "hub.tools.countStored": [
    [3, "10/1/2026, 9:30:00 AM"],
    [1, null],
  ],
  "hub.tools.truncated": [[50]],
  "hub.tools.mismatch.storedOnly": [["Add, Divide"]],
  "hub.tools.mismatch.liveOnly": [["Echo"]],
  "hub.tools.mismatch.both": [["Add", "Echo"]],
  "hub.tools.stale": [["Echo differs."]],
  "hub.tools.staleFromOrigin": [["MCP Server"]],
  "hub.tools.unknownFromOrigin": [["Gateway"]],
  "hub.toolSchemas.noMatch": [["calc"]],
  "hub.catalog.noMatch": [["osv"], ["sqlite"]],
  "hub.catalog.headerImportHint": [["x-api-key"]],
  "hub.new.fromCatalog": [["OSV"]],
  "hub.detail.replacedBy": [["alice", "2026-11-16 10:00"], ["dev01", "2026-11-16 10:00"]],
};
