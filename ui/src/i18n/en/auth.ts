import type { Messages } from "../types";
import { plural } from "./plural";

const restart = "Start the connection again from the client.";
const notAMember = "Only members of this tenant can connect.";

/** 로그인(D40), OIDC 콜백과 MCP 클라이언트 연결 허용(P33 16절). 로그인은 sign in 하나로 적는다. */
export const auth: Messages["auth"] = {
  username: "Username",
  password: "Password",
  signingIn: "Signing in…",
  unnamedClient: "Unnamed client",
  reauthFailed: "The IdP did not confirm a new sign-in, so this cannot continue. Try again later, and tell a platform admin if it keeps happening.",
  login: {
    title: "Sign in to Daegam",
    reauth: "For your security, sign in again.",
    errors: {
      account_disabled: "This account is disabled. Contact a platform admin.",
      invalid_credentials: "Incorrect username or password.",
    },
    failed: (code: string): string => `Could not sign in (${code}).`,
  },
  callback: {
    failed: "Could not finish signing in. Sign in again, or try again in a moment.",
    retry: "Sign in again",
    home: "Go to home page",
  },
  consent: {
    title: "Connect an MCP client",
    checking: "Checking…",
    headerMode: "MCP sign-in is turned off in this deployment.",
    inspecting: "Checking the request…",
    leaving: "Returning to the client. You can close this tab.",
    heading: (client: string): string => `${client} wants to connect to Daegam`,
    origin: (origin: string): string => `${origin}.`,
    nameCollides:
      "This client uses the name of a well-known client, but any client can claim that name. If you did not start this connection yourself, deny it.",
    publicUrl: "Public URL",
    tenant: "Tenant",
    scope: (tenant: string, user: string | null, minutes: number, days: number): string =>
      `As ${user ?? "this account"}, the client can call the Gateways in ${tenant} and the MCP Servers and Agents exposed there. It gets a new access token every ${plural(minutes, "minute", "minutes")} and stays connected for up to ${plural(days, "day", "days")}.`,
    loopback: (where: string): string =>
      `The sign-in result goes to this computer (${where}). Any program on this computer can pose as this client. If you did not start this connection yourself, deny it.`,
    approve: "Allow",
    deny: "Deny",
    notAMember,
    generic: "Could not process the request. Start again from the client.",
    errors: {
      invalid_client: `Could not verify the client. ${restart}`,
      invalid_redirect_uri: `The redirect URI the client sent does not match the registered one. ${restart}`,
      client_metadata_unavailable: "Could not get the client details. Wait a moment, then start again from the client.",
      request_expired: `The request expired. ${restart}`,
      request_used: `This request was already answered. ${restart}`,
      invalid_target: `The target is no longer exposed. ${restart}`,
      not_a_member: notAMember,
      consent_limit: "You allowed too many connections in a short time. Try again in 10 minutes.",
      temporarily_unavailable: "MCP sign-in connections cannot be allowed right now. Start again from the client after a platform admin switches to platform tokens.",
    },
  },
};
