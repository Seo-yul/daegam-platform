import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "auth.login.failed": [["server_error"]],
  "auth.consent.heading": [["Claude Code"]],
  "auth.consent.origin": [["Client details published by claude.ai"]],
  "auth.consent.scope": [
    ["acme", "alice", 15, 30],
    ["acme", null, 1, 1],
  ],
  "auth.consent.loopback": [["127.0.0.1:53682"]],
};
