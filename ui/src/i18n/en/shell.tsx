import type { ReactNode } from "react";

import type { Messages } from "../types";
import { plural } from "./plural";

/** 사이드바, 머리, 홈, 홈 챗. 대화는 conversation, 비서는 assistant 로 적는다. */
export const shell: Messages["shell"] = {
  sidebar: {
    groups: { Tool: "Tools", Agent: "Agents", Admin: "Admin" },
    skipToMain: "Skip to main content",
    home: "Go to Daegam home",
    homeTitle: "Home",
    expand: "Expand sidebar",
    collapse: "Collapse sidebar",
  },
  theme: {
    toLight: "Switch to light theme",
    toDark: "Switch to dark theme",
    dark: "Dark",
    light: "Light",
    label: "Theme",
  },
  apiDocs: "API docs",
  names: { product: "Daegam", assistant: "Steward" },
  assistantToggle: {
    open: "Open Steward panel",
    close: "Close Steward panel",
    openTitle: "Opens Steward to handle platform tasks through chat.",
    closeTitle: "Closes the Steward panel.",
  },
  pageHeader: { thisResource: "this resource" },
  homeChat: {
    assistantOption: "Steward (platform assistant)",
    assistantOff: "Steward off",
    assistantNote: "Steward acts on the platform with the signed-in user's permissions. What the user cannot do, Steward cannot do either.",
    agentNote: "This Agent calls Tools with its own credentials. Tool calls do not carry the user's identity.",
    closeList: "Close conversation list",
    openList: (count: number): string => `Conversations (${count})`,
    empty: {
      assistant: "No conversations yet. Ask anything.",
      search: "No conversation titles match the search.",
      agent: "No conversations with this Agent yet.",
    },
    assistantSessionHint: "Each tenant keeps its own conversations with Steward. Deleting one keeps only its audit record.",
    notReady: (phase: string | null, detail: ReactNode): ReactNode => (
      <>
        This Agent is {phase ?? "in an unknown state"} right now, so you cannot chat with it. See {detail} for its state and the reason.
      </>
    ),
    detailLink: "details",
    emptyHome: {
      title: "Nothing to chat with yet",
      loadFailed: "Could not load the Agent list. Check your permissions or connection, then refresh the page. The options below still work.",
      lead: "There are two ways to start. Either way, come back here to chat.",
      agentLink: "Create and deploy an Agent",
      agent: (link: ReactNode): ReactNode => (
        <>{link}. Start from the single support agent template, and you only need to select a provider, an MCP source and a Database.</>
      ),
      assistantLink: "Turn on Steward in Platform Settings",
      assistant: (link: ReactNode): ReactNode => (
        <>{link}. Once it is on, you can ask in plain words, such as "Create an MCP Server from the Petstore spec". It needs one LLM provider.</>
      ),
    },
  },
  homeStart: {
    // 칩을 누르면 이 말이 그대로 청지기에게 가므로 사람이 쓰는 요청문이다
    quickStarts: {
      mcp: "Create an MCP Server from the Petstore spec",
      status: "Tell me the status of everything deployed now",
      hub: "Show me which Tools the hub has",
      agent: "Create a support Agent",
    },
    placeholder: "How can I help? Press Enter to send and Shift+Enter for a new line",
    greeting: (name: string): string => `Hello, ${name}`,
    ask: "How can I help?",
  },
  sessionList: {
    heading: "Conversations",
    newTitle: "Starts a new conversation.",
    search: "Search conversations",
    loading: "Loading the list…",
    closed: "Closed",
    rename: "Rename",
    trash: (name: string): string => `Delete conversation ${name}`,
    trashTitle: "Deletes this conversation. Its audit record stays.",
    deleteTitle: "Delete this conversation?",
    deleteBody: "This deletes the conversation, the files you uploaded and the files the Agent made. You cannot undo this. Long-term memory and audit records stay.",
    deleting: "Deleting…",
    deleteRunning: "A chat with an answer in progress cannot be deleted. Stop the answer first, then delete it.",
    agentDetail: (turns: number, at: string | null, pending: boolean): string =>
      `${plural(turns, "turn", "turns")}${at ? ` · ${at}` : ""}${pending ? " · Awaiting approval" : ""}`,
  },
  languageSwitch: {
    label: "Language",
  },
  versionBadge: {
    release: "release",
    api: "api",
    ui: "ui",
    stale: (build: string): string => `(server: ${build})`,
    staleTitle: (build: string): string => `The server serves UI build ${build}`,
  },
};
