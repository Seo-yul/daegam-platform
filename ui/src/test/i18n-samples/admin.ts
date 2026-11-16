import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "admin.groupMappings.confirmBody": [[3], [1]],
  "admin.groupMappings.formError.adminTenant": [["okta-admins"]],
  "admin.platformSettings.scopeHint": [[createElement("a", null, "Tenant Settings")]],
  "admin.platformSettings.savedKeys": [["openai, anthropic"]],
  "admin.platformSettings.network.staleCa": [[1], [3]],
  "admin.platformSettings.otlp.status": [["2026-10-20 10:00", "2", "1", "0"], [null, "0", "0", "3"]],
  "admin.platformSettings.assistant.maxToolCallsHint": [[50]],
  "admin.platformSettings.hubImages.refusedUsage": [[1], [3]],
  "admin.platformSettings.hubImages.overlapWarning": [["home"]],
  "admin.platformSettings.provider.key.managed": [["daegam-llm-key-openai"]],
  "admin.platformSettings.provider.key.operator": [["openai-key"]],
  "admin.platformSettings.provider.checkFailed": [["forbidden"]],
  "admin.platformSettings.webTools.latency": [[412]],
  "admin.platformSettings.provider.gateway.keySynced": [["2026-11-09 10:00"]],
  "admin.platformSettings.llmGateway.revision": [["3f2a9c1e", "7b4d0e22"], [null, "7b4d0e22"]],
  "admin.platformSettings.llmGateway.pods": [[2], [1]],
  "admin.platformSettings.llmGateway.agents": [[2, 1, 0]],
  "admin.platformSettings.llmGateway.cleanups": [[3], [1]],
  "admin.platformSettings.llmGateway.revocations": [[12, 0], [1, 1]],
  "admin.platformSettings.llmGateway.ingest": [["2026-11-24 10:00", "2026-11-09 10:05"], [null, null]],
  "admin.platformSettings.llmGateway.clashing": [["llm"]],
  "admin.platformSettings.issuer.pending": [[3], [1]],
  "admin.platformSettings.issuer.jwksUnreachable": [["edge"], ["edge, branch"]],
  "admin.platformSettings.issuer.tokenLife": [[3540, 4440], [null, 4440]],
  "admin.users.notLocal": [["oidc"], ["header"]],
  "admin.users.deleteTitle": [["alice"]],
  "admin.users.create.passwordHint": [[12]],
  "admin.users.reset.title": [["alice"]],
  "admin.users.tokensDialog.title": [["alice"]],
};

export const selectors: SampleFile["selectors"] = {
  "admin.users.notLocal": [0],
};
