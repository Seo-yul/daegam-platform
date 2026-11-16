import type { GrantRevokeReason, RevokeReason } from "@/api/types";

import type { Messages } from "../types";
import { plural } from "./plural";

const accountEvents = {
  disabled: "Account disabled",
  deleted: "Account deleted",
  password: "Password changed",
  membership: "Removed from the tenant",
  issuer_changed: "Issuer address changed",
  key_rotated: "Signing key rotated",
} satisfies Partial<Record<Extract<RevokeReason, GrantRevokeReason>, string>>;

/** 2.0.0-rc1 이 모르는 회수 까닭 (P53b 6.5절). 개인 토큰과 연결이 같은 말을 쓴다. */
const revokeCauses = {
  role_changed: "Role lowered",
  unconfirmed: "Not signed in for too long",
  groups_changed: "IdP groups changed",
  wrong_group: "Tenant group mismatch",
  accounts_off: "Service accounts turned off",
} satisfies Partial<Record<Extract<RevokeReason, GrantRevokeReason>, string>>;

const rule = (min: number): string => `Use at least ${min} characters.`;

/** 계정 화면 (내 MCP 토큰, 비밀번호 바꾸기)과 사이드바 맨 아래의 계정 메뉴(AccountMenu). 로그인은 sign in 하나로 적는다. */
export const account: Messages["account"] = {
  menu: {
    tokens: "My MCP tokens",
    password: "Change password",
    logout: "Sign out",
    login: "Sign in",
    signedOut: "Not signed in",
    headerMode: (sub: string | null): string => `Header auth mode${sub !== null ? ` · ${sub}` : ""}`,
    signedIn: "signed in",
    approvalChoices: "Remembered choices",
  },
  approvalChoices: {
    title: "Remembered choices",
    empty: "No remembered choices.",
    choice: "Choice",
    approve: "Always approve",
    reject: "Always reject",
  },
  runsAsMe: {
    title: "Runs that act as me",
    empty: "No schedules or triggers act as you.",
  },
  tokens: {
    title: "My MCP tokens",
    headerMode: "Personal MCP tokens do not exist in header auth mode. This deployment runs in development mode without authentication.",
    idpIssuer:
      "This deployment's Gateways, exposed MCP Servers and exposed Agents still accept only tokens from the IdP. Tokens made here work once a platform admin switches to platform tokens.",
    confirmHint: (days: number): string =>
      `If you do not sign in to Daegam for ${plural(days, "day", "days")}, every token and MCP client connection on this page is revoked. Each sign-in tells Daegam that the IdP still accepts this account.`,
    newToken: "New token",
    noTenant: "You are not a member of any tenant, so you cannot get a token. Ask a tenant admin to add you as a member.",
    issued: "Issued tokens",
    grantsTitle: "Connected MCP clients",
    grantsHelp: "MCP clients appear here after you sign in and allow them in the browser. Each client renews its own access token every 15 minutes.",
    rolesHint:
      "A token carries the roles you had when you created it. If your roles are lowered, the token is revoked; roles you gain later do not reach tokens you already created. Create the token again to pass Tool rules that select roles.",
  },
  issue: {
    tenant: "Tenant",
    tenantHint: "The token can call this tenant's Gateways and the MCP Servers and Agents it exposes.",
    tenantPlaceholder: "Select a tenant",
    name: "Name",
    nameHint: "A name that tells you which device or client holds the token.",
    expiry: "Expires",
    expiryDays: (days: number): string => plural(days, "day", "days"),
    expiryYear: "1 year",
    pickDate: "Custom date",
    expiryDate: "Expiry date",
    dateHint: (maxDays: number): string => `Choose a date between tomorrow and ${plural(maxDays, "day", "days")} from now.`,
    password: "Current password",
    passwordHint: "Re-enter your password to issue a long-lived token.",
    reauthHint: "This token lasts a long time, so you sign in at the IdP again unless you signed in within the last 10 minutes.",
    wrongPassword: "Incorrect password.",
    issuing: "Issuing…",
    submit: "Issue token",
    readyAt: (clock: string): string => `Ready to use at ${clock}.`,
  },
  oneTime: { notice: "This token is shown only once. Copy it now.", copyLabel: "token" },
  tokenTable: {
    columns: { name: "Name", tenant: "Tenant", created: "Created", expiry: "Expires", status: "Status" },
    revoke: "Revoke",
    empty: "No tokens issued yet.",
    revokeTitle: (label: string): string => `Revoke token "${label}"`,
    revokeBody: "MCP clients that use this token are rejected within 16 minutes. You cannot restore a revoked token.",
    revoking: "Revoking…",
    readyFrom: (clock: string): string => `Ready at ${clock}`,
    status: { active: "Active", pending: "Pending", revoking: "Revoking", revoked: "Revoked", expired: "Expired" },
    reason: {
      user: "Revoked by you",
      admin: "Revoked by an admin",
      ...accountEvents,
      ...revokeCauses,
      key_missing: "Signing key missing",
    },
  },
  grants: {
    columns: { client: "Client", tenant: "Tenant", target: "Target", connected: "Connected on", refreshed: "Last refreshed", status: "Status" },
    disconnect: "Disconnect",
    empty: "No MCP clients are connected.",
    disconnectTitle: (client: string): string => `Disconnect ${client}`,
    disconnectBody: "This client can no longer refresh its token, and its requests are rejected within 16 minutes. To use it again, sign in again from the client.",
    disconnecting: "Disconnecting…",
    status: { active: "Connected", expired: "Expired", revoked: "Disconnected" },
    reason: {
      user: "Disconnected by you",
      admin: "Disconnected by an admin",
      client: "Disconnected by the client",
      ...accountEvents,
      ...revokeCauses,
      resource_gone: "Target no longer exposed",
      oauth_disabled: "MCP sign-in turned off",
      client_disallowed: "Client removed from the allow list",
      code_reuse: "Authorization code reused",
      refresh_reuse: "Refresh token reused",
      evicted: "Connection limit reached",
    },
    origin: {
      cimd: (host: string): string => `Client details published by ${host}`,
      self: "Name chosen by the client itself",
    },
  },
  password: {
    title: "Change password",
    required: "You signed in for the first time, or an admin reset your password. Change your password to continue.",
    tokenWarning: (count: number): string =>
      count === 1 ? "Changing your password revokes your personal MCP token." : `Changing your password revokes all ${count} of your personal MCP tokens.`,
    current: "Current password",
    currentHint: "The password you first received, or the temporary password an admin set for you.",
    next: "New password",
    nextHint: (min: number): string => `${rule(min)} A long passphrase is stronger than a short, complex password.`,
    again: "Confirm new password",
    mismatch: "The two new passwords do not match.",
    changing: "Changing…",
    submit: "Change password",
    wrongCurrent: "Your current password is incorrect.",
    weak: (min: number): string => `The new password must be at least ${min} characters long and different from your username.`,
    failed: (code: string): string => `Could not change the password (${code}).`,
    rule,
    tooShort: (length: number, more: number): string => `${plural(length, "character", "characters")} so far. Enter ${more} more.`,
    locked: "Too many sign-in attempts. Your account is locked for now. Try again later.",
    lockedFor: (minutes: number): string => `Too many sign-in attempts. Your account is locked. Try again in ${plural(minutes, "minute", "minutes")}.`,
  },
};
