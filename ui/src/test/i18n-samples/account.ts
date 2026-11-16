import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "account.menu.headerMode": [["alice"], [null]],
  "account.tokens.confirmHint": [[7], [1]],
  "account.issue.expiryDays": [[7], [1]],
  "account.issue.dateHint": [[365], [1]],
  "account.issue.readyAt": [["09:30"]],
  "account.tokenTable.revokeTitle": [["laptop"]],
  "account.tokenTable.readyFrom": [["09:30"]],
  "account.grants.disconnectTitle": [["Claude Code"]],
  "account.grants.origin.cimd": [["claude.ai"]],
  "account.password.tokenWarning": [[3], [1]],
  "account.password.nextHint": [[12]],
  "account.password.weak": [[12]],
  "account.password.failed": [["bad_code"]],
  "account.password.rule": [[12]],
  "account.password.tooShort": [[5, 7]],
  "account.password.lockedFor": [[4], [1]],
};

export const selectors: SampleFile["selectors"] = {};
