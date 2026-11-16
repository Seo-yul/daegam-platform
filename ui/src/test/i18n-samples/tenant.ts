import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "tenant.settings.title": [["acme"]],
  "tenant.settings.scopeHint": [[createElement("span", null, "acme"), createElement("a", null, "Platform Settings")]],
  "tenant.members.formError.duplicate": [["alice"]],
};
