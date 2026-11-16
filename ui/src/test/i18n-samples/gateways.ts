import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "gateways.form.selected": [[3]],
  "gateways.consumerNotice.closed": [["front, ops"]],
  "gateways.form.backendError": [["orders-pub", "It cannot be a backend.", "orders"], ["orders-pub", "Its names are unknown.", ""]],
  "gateways.form.toolCount": [[3], [1]],
  "gateways.detail.labels.toolCount": [[12, "prefix"], [1, "priority"]],
  "gateways.detail.labels.renames": [[2], [1]],
  "gateways.detail.labels.accessRevoked": [["github"]],
  "gateways.list.noMatch": [["support"]],
  "gateways.list.empty": [["default", createElement("a", null, "hub")]],
  "gateways.form.revisionNotice": [[3]],
  "gateways.form.notConsumableError": [["support-hub"]],
  "gateways.form.noTools": [[createElement("a", null, "Refresh")]],
  "gateways.form.unknownTools": [["orders-mcp, crm-mcp"]],
  "gateways.detail.deployBody": [[3]],
  "gateways.detail.cycle": [[createElement("span", null, "gw-a → gw-b → gw-a")]],
  "gateways.detail.redeployBehind": [[2, 3]],
  "gateways.detail.redeploySame": [[3]],
  "gateways.detail.revokedBackends": [["Some backends are no longer allowed.", "orders-mcp, crm-mcp"]],
  "gateways.overview.deployedSummary": [[3, 2], [1, 1]],
  "gateways.overview.auth": [[createElement("span", null, "oidc")]],
  "gateways.overview.alive": [[2, 3], ["?", 3]],
  "gateways.deployment.emptyBody": [[3]],
  "gateways.deployment.renamedFrom": [["orders_get"]],
};
