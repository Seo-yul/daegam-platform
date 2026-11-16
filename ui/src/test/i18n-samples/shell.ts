import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "shell.versionBadge.staleTitle": [["4392c4fe"]],
  "shell.versionBadge.stale": [["4392c4fe"]],
  "shell.homeChat.openList": [[3], [1]],
  "shell.homeChat.notReady": [["Failed", createElement("a", null, "DETAIL")], [null, createElement("a", null, "DETAIL")]],
  "shell.homeChat.emptyHome.agent": [[createElement("a", null, "AGENT")]],
  "shell.homeChat.emptyHome.assistant": [[createElement("a", null, "SETTINGS")]],
  "shell.homeStart.greeting": [["Alice"]],
  "shell.sessionList.trash": [["orders"]],
  "shell.sessionList.agentDetail": [[3, "10/11/2026, 9:30:00 AM", true], [1, null, false]],
};
