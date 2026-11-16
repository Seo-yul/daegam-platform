import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "detail.revisionDiffView.summary": [[2, 3, 4], [2, 3, 1]],
  "detail.changeCount": [[3, 1], [1, 0]],
  "detail.listControls.pageInfo": [[23, 11, 20], [1, 1, 1]],
  "detail.listControls.noMatch": [["orders"]],
  "detail.nameFields.preview": [[createElement("code", null, "hub-orders-mcp")]],
  "detail.notFoundPage.body": [["MCP Server"]],
  "detail.missingResource.gone": [["MCP Server", "10/1/2026, 9:30:00 AM"]],
  "detail.phaseBadge.dot": [["Ready"]],
  "detail.phaseBadge.live": [["Ready"]],
  "detail.publishSection.deleteWarning": [[3], [1]],
  "detail.publishSection.lead": [["orders-mcp", createElement("span", null, "orders-mcp")]],
  "detail.referencesCard.notice": [["MCP Server (2)"]],
  "detail.referencesCard.tokens": [[2], [1]],
  "detail.resourcesEditor.jsonOnly": [["limits.nvidia.com/gpu"]],
  "detail.resourcesEditor.limitBelowRequest": [["cpu", "500m", "1"]],
  "detail.specDiff.summary": [[12, 3], [1, 1]],
  "detail.specDiff.profiles": [[createElement("a", null, "orders-mcp")]],
  "detail.specJobForm.defaultProvider": [["openrouter"]],
  "detail.specJobForm.providerOption": [
    ["openrouter", true, false],
    ["openrouter", false, true],
  ],
  "detail.specJobForm.noProvider": [[createElement("a", null, "Platform Settings")]],
  "detail.specJobForm.instructionHint": [[12, 200]],
  "detail.specJobForm.iterations": [[2, 5]],
  "detail.specJobForm.tokens": [["1,234"]],
  "detail.markdown.image": [["chart"], [undefined]],
  "detail.conditionsDetails.summary": [["2 conditions, all healthy"]],
  "detail.conditionsDetails.problems": [[2], [1]],
  "detail.conditionsDetails.allOk": [[3], [1]],
  "detail.connectCard.defaultLabel": [["support"]],
  "detail.connectCard.oidc": [["DAEGAM_PAT"]],
  "detail.connectCard.scope": [["default"]],
  "detail.connectCard.copySettings": [["Claude Code"]],
  "detail.readinessCard.since": [["9/16/2026, 9:30:00 AM"]],
  "detail.readinessCard.pollEvery": [[5], [1]],
  "detail.readinessCard.savedRevision": [[5]],
  "detail.recentCard.event.saved": [[2]],
  "detail.recentCard.event.deployed": [[2]],
  "detail.cloneDialog.title": [["MCP Server"]],
  "detail.cloneDialog.copyName": [["Orders MCP"]],
  "detail.cloneDialog.body": [["graph", 3, "Agent"]],
  "detail.deploymentAlerts.behind": [[3, 2, "graph"]],
};

export const selectors: SampleFile["selectors"] = {
  // 표시 이름은 한국어 조사를 고르는 데만 쓰고, 글자는 꾸민 요소(target)가 적는다
  "detail.publishSection.lead": [0],
  "detail.specJobForm.providerOption": [1, 2],
};
